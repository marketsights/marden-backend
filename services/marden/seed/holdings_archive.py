"""Archive files the holdings loader reads, stored raw first and parsed as data only.

- N-PORT position lines: ``aiwc/pxsrc/w2/**/rows_*.json`` and ``aiwc/pxsrc/agg_<CUSIP>.json``.
- BDC schedules of investments (30 Jun 2026): ``fundpos.json`` and ``watch/ref_meta.json``; each BDC's filing
  URL, filed date and NAV come from the ``data`` and ``realnav`` JSON blocks of ``parallax-credit-marks.html``.
- Insurer NAIC Schedule D lines (31 Dec 2025): ``aiwc/q3x/ins.json``.

Every file is recorded with ``seed_document`` before it is parsed (the instruments loader's
``read_archive`` stores the N-PORT, fundpos and ref_meta files plus the files its fund-mark matching needs).
The HTML page is never executed: only the text of two ``<script type="application/json">`` blocks is parsed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from marden.core.db import Conn
from marden.seed.common import load_json, parse_date, seed_dir, seed_document, slugify
from marden.seed.holdings_lines import FundSpec, HoldingLine, NavRecord, bdc_title, dec, naic_title, nport_line_key
from marden.seed.instruments_archive import Archive, read_archive
from marden.seed.instruments_nport import BDC_PERIOD, EXCLUDED_BKEYS, FUNDPOS, REF_META, fund_mark_price, nport_files
from marden.seed.instruments_parse import LOANX_IN_TEXT, PLACEHOLDER_CUSIPS, cusip_valid, isin_valid, normalize_name

HTML = "parallax-credit-marks.html"
INSURERS = "aiwc/q3x/ins.json"
NAIC_PERIOD = date(2025, 12, 31)  # ins.json tiles: "127 positions · 23 insurers · 31 Dec 2025"
BDC_MARK_MAX = Decimal(150)  # fundpos marks above this (or below 0) are unit errors (DATA_DICTIONARY section 5)
BADNA_SHARE = 0.45  # the page's own rule: a filer flagging > 45% of positions non-accrual has unusable flags
_BLOCK = '<script id="{}" type="application/json">(.*?)</script>'
_URL = re.compile(r"/edgar/data/0*(\d+)/(\d{10})(\d{2})(\d{6})/")
_FILER_CIK = re.compile(r"\s*\(CIK 0*(\d+)\)\s*$")
_STRONG_OTHER = re.compile(r"^(LX\d{6}|BL\d{7})(?:\s|$)")
LINKABLE_ASSET_CATS = ("DBT", "LON", "ABS", "SN")  # N-PORT asset categories that can be debt instruments


@dataclass
class HoldingsArchive:
    base: Archive  # the instruments loader's view (aidebt, citations, N-PORT records, fundpos)
    lines: list[HoldingLine] = field(default_factory=list)
    funds: dict[str, FundSpec] = field(default_factory=dict)
    navs: list[NavRecord] = field(default_factory=list)
    docs: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, list[str]] = field(default_factory=dict)

    def skip(self, reason: str, item: str) -> None:
        self.skipped.setdefault(reason, []).append(item)


def html_blocks(text: str, ids: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for block_id in ids:
        m = re.search(_BLOCK.format(re.escape(block_id)), text, re.S)
        if not m:
            raise ValueError(f"{HTML}: no {block_id} JSON block")
        out[block_id] = json.loads(m.group(1))
    return out


def accession_from_url(url: str) -> str | None:
    m = _URL.search(url or "")
    return f"{m[2]}-{m[3]}-{m[4]}" if m else None


def cik_from_url(url: str) -> str | None:
    m = _URL.search(url or "")
    return m[1] if m else None


def read_holdings_archive(conn: Conn) -> HoldingsArchive:
    base = read_archive(conn)
    arc = HoldingsArchive(base=base, docs=dict(base.docs))
    for relpath in (HTML, INSURERS):
        arc.docs[relpath] = seed_document(relpath, conn=conn).id
    blocks = html_blocks((seed_dir() / HTML).read_text(encoding="utf-8"), ("data", "realnav"))
    _nport(arc)
    _bdc(arc, load_json(FUNDPOS), load_json(REF_META), blocks["data"]["funds"], blocks["realnav"])
    _naic(arc, load_json(INSURERS))
    return arc


# ---------------------------------------------------------------- N-PORT


def _rate(value: Any) -> float | None:
    """annualizedRt comes as '7.75' and as '0.0775' (DATA_DICTIONARY section 5): below 0.2 it is a fraction."""
    try:
        r = float(value)
    except (TypeError, ValueError):
        return None
    return round(r * 100, 6) if 0 < r < 0.2 else r


def _coupon_kind(value: Any) -> str | None:
    v = str(value or "").strip().lower()
    return {"fixed": "fixed", "floating": "floating", "variable": "floating"}.get(v)


def _codes(cusip: str | None, isin: str | None, other: str | None, *texts: str) -> tuple[list[str], list[str]]:
    """(strong identifiers: CUSIP, ISIN, LoanX, Bloomberg loan id; other codes used only to look up instruments)."""
    strong: list[str] = []
    weak: list[str] = []
    for c in (cusip, isin):
        if c and (cusip_valid(c) or isin_valid(c)) and c not in strong:
            strong.append(c)
    first = (other or "").strip().split()[0] if (other or "").strip() else ""
    if first and first not in PLACEHOLDER_CUSIPS:
        m = _STRONG_OTHER.match(first)
        if m or cusip_valid(first) or isin_valid(first):
            if first not in strong:
                strong.append(m[1] if m else first)
        elif first not in weak:
            weak.append(first)
    for t in texts:
        for code in LOANX_IN_TEXT.findall(t or ""):
            if code not in strong:
                strong.append(code)
    return strong, weak


def _nport(arc: HoldingsArchive) -> None:
    files = nport_files(seed_dir())
    files.sort(key=lambda p: (not p.startswith("aiwc/pxsrc/w2/"), p))  # w2 lines carry more fields: kept first
    seen: set[tuple[str, str, str]] = set()
    for relpath in files:
        agg_cusip = relpath.rsplit("/", 1)[-1][4:-5] if relpath.rsplit("/", 1)[-1].startswith("agg_") else None
        for i, r in enumerate(load_json(relpath)):
            line = _nport_line(relpath, r, agg_cusip, arc)
            if line is None:
                continue
            key = (line.fund_key, line.accession, line.line_key)
            if key in seen:
                arc.skip("duplicate N-PORT line (same filing, identifier, units, balance and value)", f"{relpath}#{i}")
                continue
            seen.add(key)
            arc.lines.append(line)


def _nport_line(relpath: str, r: dict[str, Any], agg_cusip: str | None, arc: HoldingsArchive) -> HoldingLine | None:
    url = str(r.get("url") or "")
    period = parse_date(r.get("period") or r.get("per"))
    accession = r.get("adsh") or accession_from_url(url)
    series = " ".join(str(r.get("fund") or "").split())
    if not url or period is None or not accession or not series:
        arc.skip("N-PORT line without filing URL, period, accession or series name", relpath)
        return None
    filer = str(r.get("filer") or "")
    m = _FILER_CIK.search(filer)
    reg_cik = m[1] if m else cik_from_url(url)
    reg_name = " ".join((_FILER_CIK.sub("", filer) if filer else str(r.get("reg") or "")).split()) or None
    cusip = agg_cusip or r.get("cusip")
    cusip = cusip if cusip and cusip_valid(cusip) else None
    isin = r.get("isin") if isin_valid(r.get("isin")) else None
    name = None if str(r.get("name") or "").strip() in ("", "N/A") else " ".join(str(r["name"]).split())
    title = " ".join(str(r.get("title") or "").split()) or None
    strong, weak = _codes(cusip, isin, r.get("other"), title or "", name or "")
    units = r.get("units")
    bal, val = r.get("bal"), r.get("val")
    usd = agg_cusip is not None or r.get("curCd") == "USD"  # agg files: 13 domestic USD CUSIPs, no currency field
    code = cusip or (strong[0] if strong else None) or (weak[0] if weak else None) or isin
    code = code or f"name:{normalize_name(name or title)[:60]}"
    asset_cat = r.get("assetCat")
    fund_key = f"nport:{reg_cik}:{series.lower()}"
    if fund_key not in arc.funds:
        arc.funds[fund_key] = FundSpec("fund", series, None, reg_name, reg_cik, {"role": "nport_series",
                                                                                 "registrant_cik": reg_cik})
    return HoldingLine(
        source="nport", relpath=relpath, fund_key=fund_key, fund_name=series, accession=str(accession),
        filing_url=url, filing_type="N-PORT", period_end=period, filed_at=parse_date(r.get("filed")),
        line_key=nport_line_key(code, units, bal, val), issuer_name=name, title=title, cusip=cusip, isin=isin,
        codes=tuple(strong), weak_codes=tuple(weak), units=units,
        par=dec(bal) if units == "PA" else None, value=dec(val), usd=usd,
        raw_mark=fund_mark_price(val, bal) if units == "PA" else None,
        pct_nav=dec(r.get("pctVal")), maturity=r.get("maturityDt") or r.get("mat"),
        coupon_kind=_coupon_kind(r.get("couponKind")), coupon_rate=_rate(r.get("annualizedRt") or r.get("cpn")),
        asset_cat=asset_cat, unfunded=bool(re.search(r"unfunded", f"{title} {name}", re.I)),
        linkable=asset_cat is None or str(asset_cat).startswith(LINKABLE_ASSET_CATS),
    )


# ---------------------------------------------------------------- BDC schedules


def _bdc(arc: HoldingsArchive, fundpos: dict[str, Any], ref_meta: dict[str, Any], funds: list[list[Any]],
         realnav: dict[str, Any]) -> None:
    cols = fundpos["cols"]
    ix = {c: cols.index(c) for c in cols}
    names = {str(cik).lstrip("0"): str(n) for cik, n in ref_meta.get("universe", [])}
    fund_idx = {str(k).lstrip("0"): int(v) for k, v in ref_meta.get("fund_idx", {}).items()}
    for cik in sorted(set(names) - set(fund_idx)):
        arc.skip("BDC in ref_meta universe without a fund index or schedule lines", f"CIK {cik} {names[cik]}")
    for raw_cik, rows in fundpos["f"].items():
        cik = str(raw_cik).lstrip("0")
        fi = fund_idx.get(cik)
        if fi is None or fi >= len(funds):
            arc.skip("fundpos CIK without a fund index in ref_meta", f"CIK {cik}")
            continue
        fund = funds[fi]
        url, filed = str(fund[3] or ""), parse_date(_yyyymmdd(fund[4]))
        accession = accession_from_url(url)
        name = names.get(cik) or str(fund[0])
        if not accession:
            arc.skip("BDC filing URL without an accession", f"CIK {cik} {url}")
            continue
        nav = (realnav.get("nav") or {}).get(str(fi))
        nav_usd = dec(nav[0]) if nav and nav[0] is not None else None
        if nav_usd is not None:
            arc.navs.append(NavRecord(f"bdc:{cik}", nav_usd, dec(nav[1]) if nav[1] is not None else None,
                                      parse_date(nav[2]) or BDC_PERIOD, url))
        pos, na = fund[5], fund[7]
        badna = bool(pos) and na is not None and na / pos > BADNA_SHARE
        if badna:
            arc.skip("BDC non-accrual flags unusable (> 45% of positions flagged): non_accrual left null", name)
        arc.funds[f"bdc:{cik}"] = FundSpec("bdc", name, cik, None, None,
                                           {"role": "bdc", "fund_index": fi, "manager": fund[1], "type": fund[2]})
        form = "10-K" if re.search(r"10-?k", url.rsplit("/", 1)[-1], re.I) else "10-Q"
        seen: set[str] = set()
        for x in rows:
            _bdc_line(arc, x, ix, cik, name, url, accession, form, filed, nav_usd, badna, seen)


def _yyyymmdd(value: Any) -> str | None:
    s = str(value or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if re.fullmatch(r"\d{8}", s) else None


def _bdc_line(arc: HoldingsArchive, x: list[Any], ix: dict[str, int], cik: str, name: str, url: str, accession: str,
              form: str, filed: date | None, nav_usd: Decimal | None, badna: bool, seen: set[str]) -> None:
    bkey = str(x[ix["bkey"]] or "").strip().lower()
    borrower = " ".join(str(x[ix["b"]] or "").split())
    if bkey in EXCLUDED_BKEYS:
        arc.skip("fundpos 'vantage' / 'Vantage Bidco' line (Vantage Specialty Chemicals, not data centers)",
                 f"{name}: {borrower}")
        return
    lien, sp, mat = x[ix["lien"]], x[ix["sp_bp"]], x[ix["mat"]]
    par_k, fv_k = x[ix["par_k"]], x[ix["fv_k"]]
    key = "|".join(str(v if v is not None else "") for v in (bkey, borrower, lien, sp, mat, par_k, fv_k))
    if key in seen:
        arc.skip("exact duplicate fundpos line in the same BDC schedule", f"{name}: {borrower}")
        return
    seen.add(key)
    par = dec(par_k) * 1000 if par_k is not None and float(par_k) > 0 else None
    raw = fund_mark_price(fv_k, par_k) if par is not None else None
    mark = raw if raw is not None and Decimal(0) <= raw <= BDC_MARK_MAX else None
    if raw is not None and mark is None:
        arc.skip("BDC mark outside 0-150 (unit error in fundpos): holding kept, mark not used", f"{name}: {borrower}")
    if par is None:
        arc.skip("BDC line without par: holding kept, no mark", f"{name}: {borrower}")
    arc.lines.append(HoldingLine(
        source="bdc", relpath=FUNDPOS, fund_key=f"bdc:{cik}", fund_name=name, accession=accession, filing_url=url,
        filing_type=form, period_end=BDC_PERIOD, filed_at=filed, line_key=key, issuer_name=borrower or None,
        title=bdc_title(borrower, lien, sp, mat), cusip=None, isin=None, codes=(), weak_codes=(), units="PA",
        par=par, value=dec(fv_k) * 1000 if fv_k is not None else None, usd=True, raw_mark=raw, mark_override=mark,
        pct_nav=None, maturity=mat, coupon_kind=None, coupon_rate=None, asset_cat=None, unfunded=False,
        linkable=True, non_accrual=None if badna else bool(x[ix["na"]]), fund_net_assets=nav_usd,
        spread_bp=float(sp) if sp is not None else None, lien=lien, bkey=bkey,
    ))


# ---------------------------------------------------------------- insurer Schedule D


def naic_url(family: str, state: str, sources: list[list[str]]) -> str | None:
    """The statement source the archive names for an insurer: its group's statutory-statement page, else the
    Ohio Department of Insurance directory for Ohio-domiciled insurers."""
    head = family.split()[0].lower() if family.strip() else ""
    for label, url in sources:
        if head and head in label.lower() and "ohio" not in label.lower():
            return url
    if state == "OH":
        return next((url for label, url in sources if "ohio department of insurance" in label.lower()), None)
    return None


def _naic(arc: HoldingsArchive, ins: dict[str, Any]) -> None:
    seen: set[tuple[str, str]] = set()
    for r in ins.get("rows", []):
        ent, fam, st = " ".join(str(r["ent"]).split()), " ".join(str(r["fam"]).split()), str(r.get("st") or "")
        url = naic_url(fam, st, ins.get("src", []))
        cusip = str(r.get("cusip") or "").replace("-", "").upper()
        if url is None:
            arc.skip("insurer line without a statement source in ins.json", f"{ent}: {r.get('desc')}")
            continue
        key = f"{cusip}|{r.get('par')}|{r.get('fv')}|{r.get('bv')}"
        if (ent, key) in seen:
            arc.skip("exact duplicate insurer line", f"{ent}: {r.get('desc')}")
            continue
        seen.add((ent, key))
        fund_key = f"naic:{ent.lower()}"
        arc.funds.setdefault(fund_key, FundSpec("company", ent, None, fam if fam.lower() != ent.lower() else None,
                                                None, {"role": "insurer", "domicile_state": st}))
        par = dec(r.get("par")) * 1_000_000 if r.get("par") else None
        valid = bool(re.fullmatch(r"[0-9A-Z*@#]{9}", cusip))
        arc.lines.append(HoldingLine(
            source="naic", relpath=INSURERS, fund_key=fund_key, fund_name=ent,
            accession=f"naic-schedule-d:{NAIC_PERIOD.isoformat()}:{slugify(ent)}", filing_url=url,
            filing_type="NAIC Schedule D", period_end=NAIC_PERIOD, filed_at=None, line_key=key, issuer_name=None,
            title=naic_title(r.get("desc"), r.get("naic")), cusip=cusip if valid else None, isin=None,
            codes=(cusip,) if valid else (), weak_codes=(), units="PA", par=par,
            value=dec(r.get("fv")) * 1_000_000 if r.get("fv") is not None else None, usd=True,
            raw_mark=fund_mark_price(r.get("fv"), r.get("par")) if par else None, pct_nav=None, maturity=None,
            coupon_kind=None, coupon_rate=None, asset_cat=None, unfunded=False, linkable=True,
        ))
