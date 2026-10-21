"""Filing records the instruments loader recomputes fund marks from (docs/methods/price-sources.md section 4).

- N-PORT position lines: ``aiwc/pxsrc/w2/**/rows_*.json`` and ``aiwc/pxsrc/agg_<CUSIP>.json``.
- BDC schedule-of-investments lines (30 Jun 2026): ``fundpos.json``, BDC names from ``watch/ref_meta.json``.

``fund_mark_price`` and the source-name helpers are the shared dedupe contract with the holdings loader:
both loaders write ``price_obs(kind='fund_mark', source_url=<filing URL>, obs_date=<period end>,
price=fund_mark_price(value, balance))`` so the same filing line never becomes two rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

from marden.seed.instruments_parse import PLACEHOLDER_CUSIPS

NPORT_GLOBS = ("aiwc/pxsrc/w2/**/*.json", "aiwc/pxsrc/agg_*.json")
FUNDPOS = "fundpos.json"
REF_META = "watch/ref_meta.json"
BDC_PERIOD = date(2026, 6, 30)  # fundpos.json is the 30 Jun 2026 schedule (holdings-prices.md section 1)
# Vantage Specialty Chemicals and other name collisions, not data-center borrowers (price-sources.md section 3).
EXCLUDED_BKEYS = frozenset({"vantage", "vantage bidco"})
PRICE_QUANTUM = Decimal("0.0001")


def fund_mark_price(value: Any, balance: Any) -> Decimal | None:
    """100 x value / balance (percent of par), rounded half-even to 4 decimals; None without a balance."""
    if value is None or balance is None:
        return None
    bal = Decimal(str(balance))
    if bal <= 0:
        return None
    return (Decimal(100) * Decimal(str(value)) / bal).quantize(PRICE_QUANTUM, rounding=ROUND_HALF_EVEN)


def nport_source_name(fund: str, period: date) -> str:
    return f"N-PORT: {' '.join(fund.split())} ({period.isoformat()})"


def bdc_source_name(bdc: str, period: date) -> str:
    return f"BDC 10-Q: {' '.join(bdc.split())} ({period.isoformat()})"


@dataclass(frozen=True)
class NportRecord:
    relpath: str
    url: str
    fund: str
    period: date
    units: str | None
    balance: float | None
    value: float | None
    cusip: str | None
    isin: str | None
    other: str | None
    title: str
    name: str
    asset_cat: str | None

    @property
    def is_par(self) -> bool:
        return self.units == "PA" and bool(self.balance) and self.value is not None

    def keys(self) -> set[str]:
        """Identifiers on the line usable for joins (placeholders dropped)."""
        out = {k for k in (self.cusip, self.other) if k and k not in PLACEHOLDER_CUSIPS}
        if self.isin:
            out.add(self.isin)
        return out


def nport_files(seed_root: Path) -> list[str]:
    files: set[str] = set()
    for pattern in NPORT_GLOBS:
        files.update(str(p.relative_to(seed_root)) for p in seed_root.glob(pattern) if p.is_file())
    return sorted(files)


def _num(value: Any) -> float | None:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def parse_nport_file(relpath: str, rows: list[dict[str, Any]]) -> list[NportRecord]:
    """Records from one w2 or agg file (agg files carry the CUSIP only in their file name)."""
    agg_cusip = Path(relpath).stem[4:] if Path(relpath).name.startswith("agg_") else None
    out: list[NportRecord] = []
    for r in rows:
        period = r.get("period") or r.get("per")
        url = r.get("url")
        if not period or not url:
            continue
        try:
            day = date.fromisoformat(str(period)[:10])
        except ValueError:
            continue
        out.append(NportRecord(
            relpath=relpath, url=url, fund=str(r.get("fund") or "").strip(), period=day,
            units=r.get("units"), balance=_num(r.get("bal")), value=_num(r.get("val")),
            cusip=agg_cusip or r.get("cusip"), isin=r.get("isin") or None, other=r.get("other") or None,
            title=str(r.get("title") or ""), name=str(r.get("name") or ""), asset_cat=r.get("assetCat"),
        ))
    return out


@dataclass(frozen=True)
class BdcLine:
    cik: str
    bkey: str
    borrower: str
    lien: str | None
    maturity: str | None  # YYYY-MM
    par_k: float
    fv_k: float


def parse_fundpos(data: dict[str, Any]) -> dict[str, list[BdcLine]]:
    """CIK -> schedule lines, minus excluded name collisions and lines without par."""
    cols = data["cols"]
    idx = {c: cols.index(c) for c in ("bkey", "b", "lien", "mat", "par_k", "fv_k")}
    out: dict[str, list[BdcLine]] = {}
    for raw_cik, lines in data["f"].items():
        cik = str(raw_cik).lstrip("0")
        for x in lines:
            bkey = str(x[idx["bkey"]] or "").strip().lower()
            par, fv = _num(x[idx["par_k"]]), _num(x[idx["fv_k"]])
            if bkey in EXCLUDED_BKEYS or not par or par <= 0 or fv is None:
                continue
            out.setdefault(cik, []).append(BdcLine(
                cik=cik, bkey=bkey, borrower=str(x[idx["b"]] or ""), lien=x[idx["lien"]],
                maturity=x[idx["mat"]], par_k=par, fv_k=fv,
            ))
    return out


def bdc_names(ref_meta: dict[str, Any]) -> dict[str, str]:
    return {str(cik).lstrip("0"): str(name) for cik, name in ref_meta.get("universe", [])}
