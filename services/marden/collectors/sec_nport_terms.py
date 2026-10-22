"""`sec_nport_terms`: fund positions in instruments that carry no identifier Marden knows, found by their filed
terms in the DERA quarterly Form N-PORT data set (docs/methods/price-sources.md §4-5).

    cd services && uv run python -m marden collect sec_nport_terms [--dataset 2026q3] [--dry-run]

Why: `sec_nport` finds a fund's position only by CUSIP / ISIN / LoanX. Construction loans, DDTLs, private
placements and most ABS / CMBS tranches in the catalog have none on file, so their holders were never
looked for; and a filer's own loan code (PGIM: 'B8AFB9GT6', CUSIP 000000000) is never watched. An
instrument whose coupon was read from a fund's N-PORT could show "No fund reports it".

How: every debt position of the data set (all N-PORT filers of the quarter) whose issuer words meet an
instrument's is checked against that instrument's facts by `sec_nport_terms_match` (issuer words,
maturity, coupon, tranche, uniqueness), repeated until nothing new links, because a linked line
teaches its filer code and its class CUSIP to the next round. Then per instrument the CUSIPs are checked
for consistency (a minority CUSIP under 20% of the instrument's CUSIP lines is dropped; no dominant
CUSIP drops them all) and a CUSIP two instruments claim links to neither. Unfunded commitment lines
follow their funded line. Kept lines are written through `sec_nport_store.write_filing` (holding rows,
holding facts, fund marks, net assets; a line another loader stored already is linked if it was not)
from the data set's filed values, with the filing's primary_doc.xml as the source URL and the data set
as the document. Each linked line gets a holding fact `link_basis` (tag M: Marden's matching, method
`sec_nport_terms_match.METHOD`) with the agreeing terms, and each instrument a fact `nport_link_keys`
(the CUSIPs / ISINs and filer codes so learned, tag M) that `sec_nport` watches from then on
(CUSIP / ISIN only). Medians are recomputed. Reruns write nothing new.
"""

from __future__ import annotations

import argparse
import io
import logging
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from marden import cli
from marden.collectors import sec_nport_store as store
from marden.collectors import sec_nport_terms_match as tm
from marden.collectors.sec_index_parse import normalize_cik
from marden.collectors.sec_nport import _in_transaction
from marden.collectors.sec_nport_fx import FxVotes
from marden.collectors.sec_nport_marks import write_medians
from marden.collectors.sec_nport_parse import (
    NportHeader,
    Position,
    dataset_label,
    dataset_url,
    norm_id,
    parse_label,
    previous_quarter,
    primary_doc_url,
)
from marden.core import db, documents, heartbeat, sources
from marden.core.facts import FactWriter
from marden.core.queue import app
from marden.core.sources import declare_source
from marden.seed.holdings_link import groupable_weak, name_tokens
from marden.seed.instruments_parse import isin_valid

log = logging.getLogger(__name__)

SOURCE_ID = "sec_nport_terms"
NPORT_SOURCE_ID = "sec_nport"
PRODUCER = f"collector:{SOURCE_ID}"
DATASET_DOC_TYPE = "nport_dataset_zip"
MAX_ROUNDS = 6
DOMINANT_CUSIP_SHARE = 0.8
MEDIAN_WINDOW_DAYS = 400
FACT_FIELDS = ("coupon", "coupon_type", "coupon_rate_pct", "maturity", "identifiers")
# Asset categories that are not debt (N-PORT Item C.4): equity, derivatives, short-term vehicles, real estate.
NOT_DEBT = frozenset({"EC", "EP", "DE", "DCO", "DCR", "DFE", "DIR", "DO", "STIV", "RE"})
LINK_SOURCE = "Marden holding link (N-PORT filed terms)"

SOURCE = declare_source(
    SOURCE_ID, "SEC Form N-PORT data set: positions linked by filed terms", "api",
    "https://www.sec.gov/data-research/sec-markets-data/form-n-port-data-sets", 7 * 24 * 60,
    "Reads the DERA Form N-PORT data set the sec_nport collector stored (same SEC terms: public information, "
    "cite the SEC; no request beyond the stored zip unless it is missing, then /files/dera/ with SEC_USER_AGENT "
    "at <= 5 req/s).",
)


# ---------------------------------------------------------------- profiles from the database
def load_profiles(conn: db.Conn) -> list[tm.Profile]:
    rows = conn.execute(
        """SELECT i.id, i.kind, i.name, i.issuer_name, i.cusip, i.isin, e.name AS entity_name FROM instrument i
             LEFT JOIN entity e ON e.id = i.issuer_entity_id WHERE i.id NOT LIKE 'BDC-%%' ORDER BY i.id"""
    ).fetchall()
    facts: dict[str, dict[str, Any]] = defaultdict(dict)
    cited: dict[str, set[str]] = defaultdict(set)
    parents: dict[str, str] = {}
    for r in conn.execute(
        "SELECT subject_id, field, value, source_url FROM fact_current WHERE subject_type = 'instrument'"
    ).fetchall():
        if r["field"] in FACT_FIELDS:
            facts[r["subject_id"]][r["field"]] = r["value"]
        if r["field"] == "tranche_of" and isinstance(r["value"], str):
            parents[r["subject_id"]] = r["value"]
        urls = [r["source_url"]]
        if r["field"] == "source_urls" and isinstance(r["value"], list):
            urls += [u for u in r["value"] if isinstance(u, str)]
        for url in urls:
            if url and "primary_doc.xml" in url and (acc := tm.accession_of(url)):
                cited[r["subject_id"]].add(acc)
    codes: dict[str, set[str]] = defaultdict(set)
    for r in conn.execute(  # lines filed with no CUSIP / ISIN: their line key starts with the filer's own code
        "SELECT DISTINCT instrument_id, split_part(line_key, '|', 1) AS code FROM holding"
        " WHERE instrument_id IS NOT NULL AND filing_type = 'N-PORT' AND cusip IS NULL AND isin IS NULL"
    ).fetchall():
        if _filer_code(r["code"]):
            codes[r["instrument_id"]].add(r["code"])
    return [tm.profile(r, facts[r["id"]], cited=cited[r["id"]], filer_codes=codes[r["id"]],
                       parent=parents.get(r["id"])) for r in rows]


def _filer_code(code: str | None) -> bool:
    """A filer's own security code, filed under 'other' on a line with no CUSIP / ISIN (PGIM: 'B8AFB9GT6',
    which happens to pass the CUSIP check digit), distinctive enough to follow across the filer's funds."""
    return code is not None and groupable_weak(code) and not isin_valid(code) and not code.startswith("LX")


def _line_filer_codes(pos: tm.Pos) -> set[str]:
    return set() if pos.cusip_valid or pos.isin_valid else {c for c in pos.others if _filer_code(c)}


# ---------------------------------------------------------------- the data set
@dataclass
class Filing:
    accession: str
    filed: date | None = None
    form: str = ""
    period: date | None = None
    cik: str | None = None
    reg_name: str = ""
    reg_lei: str | None = None
    series_name: str | None = None
    series_id: str | None = None
    series_lei: str | None = None
    net_assets: Decimal | None = None

    def header(self) -> NportHeader:
        assert self.period is not None
        return NportHeader(self.form or "NPORT-P", self.reg_name, self.cik, self.reg_lei, self.series_name,
                           self.series_id, self.series_lei, self.period, None, None, None, self.net_assets)


def _tsv(zf: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    with zf.open(name) as fh:
        rows = io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline="\n")
        header = rows.readline().rstrip("\r\n").split("\t")
        for line in rows:
            values = line.rstrip("\r\n").split("\t")
            yield dict(zip(header, values, strict=False))


def _dec(text: str | None) -> Decimal | None:
    try:
        return Decimal(text) if text not in (None, "") else None
    except InvalidOperation:
        return None


def read_dataset(zf: zipfile.ZipFile, words: set[str]) -> tuple[dict[str, dict[str, Any]], dict[str, Filing]]:
    """Debt positions whose issuer name or title shares a word with `words` (holding id -> row with
    maturity, coupon and identifiers added), and their filings."""
    rows: dict[str, dict[str, Any]] = {}
    for r in _tsv(zf, "FUND_REPORTED_HOLDING.tsv"):
        if r.get("ASSET_CAT") in NOT_DEBT or not r.get("HOLDING_ID"):
            continue
        if name_tokens(r.get("ISSUER_NAME"), r.get("ISSUER_TITLE")) & words:
            rows[r["HOLDING_ID"]] = r
    for r in _tsv(zf, "DEBT_SECURITY.tsv"):
        if (h := rows.get(r.get("HOLDING_ID", ""))) is not None:
            h.update(MAT=r.get("MATURITY_DATE"), CPN=r.get("COUPON_TYPE"), RATE=r.get("ANNUALIZED_RATE"))
    for r in _tsv(zf, "IDENTIFIERS.tsv"):
        if (h := rows.get(r.get("HOLDING_ID", ""))) is not None:
            h.setdefault("IDS", []).append((r.get("IDENTIFIER_ISIN") or "", r.get("OTHER_IDENTIFIER") or "",
                                            r.get("OTHER_IDENTIFIER_DESC") or ""))
    filings = {r["ACCESSION_NUMBER"]: Filing(r["ACCESSION_NUMBER"]) for r in rows.values()}
    for r in _tsv(zf, "SUBMISSION.tsv"):
        if (f := filings.get(r.get("ACCESSION_NUMBER", ""))) is not None:
            f.filed, f.period = tm.dera_date(r.get("FILING_DATE")), tm.dera_date(r.get("REPORT_DATE"))
            f.form = (r.get("SUB_TYPE") or "").strip().upper()
    for r in _tsv(zf, "REGISTRANT.tsv"):
        if (f := filings.get(r.get("ACCESSION_NUMBER", ""))) is not None:
            f.cik, f.reg_name = normalize_cik(r.get("CIK")), r.get("REGISTRANT_NAME") or ""
            f.reg_lei = r.get("LEI") or None
    for r in _tsv(zf, "FUND_REPORTED_INFO.tsv"):
        if (f := filings.get(r.get("ACCESSION_NUMBER", ""))) is not None:
            f.series_name, f.series_id = r.get("SERIES_NAME") or None, (r.get("SERIES_ID") or "").upper() or None
            f.series_lei, f.net_assets = r.get("SERIES_LEI") or None, _dec(r.get("NET_ASSETS"))
    return rows, filings


def position(row: dict[str, Any]) -> Position:
    """The data set row as the parser's Position (same fields primary_doc.xml gives), so the line key and
    the written values are those the other N-PORT loaders write for the same filed line."""
    ids = row.get("IDS") or []
    isin = next((norm_id(i[0]) for i in ids if norm_id(i[0])), None)
    others = tuple((i[2].strip(), v) for i in ids if (v := norm_id(i[1])))
    return Position(
        ordinal=0, name=row.get("ISSUER_NAME") or "", title=row.get("ISSUER_TITLE") or "",
        lei=row.get("ISSUER_LEI") or None, cusip=norm_id(row.get("ISSUER_CUSIP")), isin=isin, others=others,
        balance=_dec(row.get("BALANCE")), units=(row.get("UNIT") or "").upper() or None,
        cur_cd=(row.get("CURRENCY_CODE") or "").upper() or None, exchange_rt=_dec(row.get("EXCHANGE_RATE")),
        val_usd=_dec(row.get("CURRENCY_VALUE")), pct_val=_dec(row.get("PERCENTAGE")),
        payoff=row.get("PAYOFF_PROFILE") or None, asset_cat=row.get("ASSET_CAT") or None,
        issuer_cat=row.get("ISSUER_TYPE") or None, country=row.get("INVESTMENT_COUNTRY") or None,
        fair_value_level=row.get("FAIR_VALUE_LEVEL") or None,
    )


def tm_pos(holding_id: str, row: dict[str, Any]) -> tm.Pos:
    p = position(row)
    return tm.Pos(
        accession=row["ACCESSION_NUMBER"], name=p.name, title=p.title, cusip=p.cusip, isin=p.isin,
        others=tuple(v for _, v in p.others), maturity=tm.dera_date(row.get("MAT")),
        coupon_kind=row.get("CPN") or None, rate=tm._float(row.get("RATE")), asset_cat=p.asset_cat,
        balance=row.get("BALANCE"), value_usd=row.get("CURRENCY_VALUE"), key=holding_id,
    )


# ---------------------------------------------------------------- matching
@dataclass
class Plan:
    links: dict[str, tm.Evidence] = field(default_factory=dict)  # holding id -> evidence
    ambiguous: Counter[str] = field(default_factory=Counter)
    dropped: list[str] = field(default_factory=list)
    rounds: int = 0


def plan_links(profiles: list[tm.Profile], positions: dict[str, tm.Pos]) -> Plan:
    """Rounds of `tm.decide` until no new line links, then unfunded lines, CUSIP consistency."""
    plan = Plan()
    owned: dict[str, str] = {}
    for p in profiles:
        for k in p.keys:
            owned.setdefault(k, p.id)
    by_word: dict[str, list[int]] = defaultdict(list)
    for i, p in enumerate(profiles):
        for w in p.tokens:
            by_word[w].append(i)
    cands = {key: sorted({i for w in name_tokens(pos.name, pos.title) for i in by_word.get(w, ())})
             for key, pos in positions.items()}
    current = list(profiles)
    for plan.rounds in range(1, MAX_ROUNDS + 1):
        found: dict[str, tm.Evidence] = {}
        plan.ambiguous.clear()
        for key, pos in positions.items():
            if not cands[key]:
                continue
            ev, ids = tm.decide([current[i] for i in cands[key]], pos, owned)
            if ev is not None:
                found[key] = ev
            elif len(ids) > 1 and not any(k in owned for k in pos.keys):
                plan.ambiguous[f"{pos.name} / {pos.title}: {', '.join(ids)}"] += 1
        learned = _learn(current, found, positions)
        if found.keys() == plan.links.keys() and learned == current:
            break
        plan.links, current = found, learned
    by_filing: dict[str, dict[str, tuple[tm.Pos, tm.Evidence]]] = defaultdict(dict)
    for key, ev in plan.links.items():
        by_filing[positions[key].accession][key] = (positions[key], ev)
    unfunded: dict[str, list[tm.Pos]] = defaultdict(list)
    for pos in positions.values():
        if pos.unfunded and pos.key not in plan.links:
            unfunded[pos.accession].append(pos)
    for acc, linked in by_filing.items():
        plan.links.update(tm.follow_unfunded(linked, unfunded.get(acc, ())))
    _consistent_cusips(plan, positions)
    return plan


def _learn(profiles: list[tm.Profile], found: dict[str, tm.Evidence], positions: dict[str, tm.Pos]) -> list[tm.Profile]:
    """Profiles with the filer codes and (for a class-stating line) the CUSIPs of the lines just linked."""
    codes: dict[str, set[str]] = defaultdict(set)
    tranche_keys: dict[str, set[str]] = defaultdict(set)
    for key, ev in found.items():
        pos = positions[key]
        codes[ev.instrument_id] |= _line_filer_codes(pos)
        if any(b.startswith("series ") and " class " in b for b in ev.basis) and pos.cusip_valid:
            tranche_keys[ev.instrument_id].add(pos.cusip_valid)
    out = []
    for p in profiles:
        c, t = p.filer_codes | codes.get(p.id, set()), p.tranche_keys | tranche_keys.get(p.id, set())
        out.append(p if (c, t) == (p.filer_codes, p.tranche_keys) else
                   tm.Profile(**{**p.__dict__, "filer_codes": frozenset(c), "tranche_keys": frozenset(t)}))
    return out


def _consistent_cusips(plan: Plan, positions: dict[str, tm.Pos]) -> None:
    by_cusip: dict[str, set[str]] = defaultdict(set)
    per_inst: dict[str, Counter[str]] = defaultdict(Counter)
    for key, ev in plan.links.items():
        if c := positions[key].cusip_valid:
            by_cusip[c].add(ev.instrument_id)
            per_inst[ev.instrument_id][c] += 1
    shared = {c for c, ids in by_cusip.items() if len(ids) > 1}
    keep: dict[str, set[str]] = {}
    for iid, counts in per_inst.items():
        top, n = counts.most_common(1)[0]
        total = sum(counts.values())
        keep[iid] = {top} if n / total >= DOMINANT_CUSIP_SHARE and top not in shared else set()
        for c in counts:
            if c not in keep[iid]:
                why = "claimed by several instruments" if c in shared else "not the instrument's dominant CUSIP"
                plan.dropped.append(f"{iid}: CUSIP {c} ({counts[c]} lines) {why}")
    for key in list(plan.links):
        ev, c = plan.links[key], positions[key].cusip_valid
        if c and c not in keep.get(ev.instrument_id, set()):
            del plan.links[key]


# ---------------------------------------------------------------- writing
@dataclass(frozen=True)
class _Doc:
    """What `write_filing` reads of a document: the filing's URL, the stored data set it was read from."""

    id: int
    url: str
    external_id: str


@dataclass
class RunStats:
    dataset: str = ""
    positions: int = 0
    rounds: int = 0
    lines_linked: int = 0
    filings: int = 0
    holdings_new: int = 0
    holdings_linked: int = 0
    facts_new: int = 0
    prices_new: int = 0
    by_instrument: Counter[str] = field(default_factory=Counter)
    gained: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    ok: bool = True
    error: str | None = None

    def add(self, part: RunStats) -> None:
        """Counts of one filing's write (kept apart until its transaction commits)."""
        for name in ("lines_linked", "filings", "holdings_new", "holdings_linked", "facts_new", "prices_new"):
            setattr(self, name, getattr(self, name) + getattr(part, name))
        self.by_instrument.update(part.by_instrument)
        self.skipped += part.skipped

    def meta(self) -> dict[str, Any]:
        return {k: (dict(v) if isinstance(v, Counter) else v) for k, v in self.__dict__.items()}

    def summary(self) -> str:
        head = (f"sec_nport_terms {self.dataset}: {self.positions} candidate positions, {self.rounds} rounds, "
                f"{self.lines_linked} lines in {self.filings} filings for {len(self.by_instrument)} instruments "
                f"({len(self.gained)} had no holder lines); holdings new {self.holdings_new}, linked "
                f"{self.holdings_linked}; facts {self.facts_new}; prices {self.prices_new}")
        lines = [head, *(f"  {i}: {n} lines" for i, n in sorted(self.by_instrument.items()))]
        lines += [f"  dropped: {d}" for d in self.dropped[:40]]
        lines += [f"  ambiguous: {a}" for a in self.ambiguous[:40]]
        lines += [f"  skipped: {s}" for s in self.skipped[:20]]
        return "\n".join(lines)


def _link_keys(positions: list[tm.Pos]) -> list[dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for pos in positions:
        for kind, value in (("cusip", pos.cusip_valid), ("isin", pos.isin_valid)):
            if value:
                out[(kind, value)] = {"type": kind, "value": value}
        for code in _line_filer_codes(pos):
            out[("filer_code", code)] = {"type": "filer_code", "value": code}
    return [out[k] for k in sorted(out)]


def write(plan: Plan, rows: dict[str, dict[str, Any]], positions: dict[str, tm.Pos], filings: dict[str, Filing],
          dataset_doc: documents.DocumentRow, stats: RunStats) -> None:
    """One transaction per filing (short locks: every holding write marks read models dirty), then the learned
    keys, then the medians."""
    fx = FxVotes()
    with db.transaction() as conn:
        fx.load_stored(conn, date.today() - timedelta(days=MEDIAN_WINDOW_DAYS))
        before = {r["instrument_id"] for r in conn.execute(
            "SELECT DISTINCT instrument_id FROM holding WHERE instrument_id IS NOT NULL").fetchall()}
    by_acc: dict[str, list[str]] = defaultdict(list)
    for key in plan.links:
        by_acc[positions[key].accession].append(key)
    for acc in sorted(by_acc):
        f = filings.get(acc)
        if f is None or f.period is None or f.cik is None or f.filed is None or f.form not in ("NPORT-P", "NPORT-P/A"):
            stats.skipped.append(f"{acc}: filing data incomplete in the data set")
            continue
        keys = sorted(by_acc[acc])

        def one(conn: db.Conn, f: Filing = f, acc: str = acc, keys: list[str] = keys) -> RunStats:
            part = RunStats()  # fresh on each attempt: a deadlock retry starts the filing over
            _write_filing(conn, f, acc, keys, plan, rows, dataset_doc, fx, part)
            return part

        stats.add(_in_transaction(one))
    per_inst: dict[str, list[tm.Pos]] = defaultdict(list)
    for key, ev in plan.links.items():
        per_inst[ev.instrument_id].append(positions[key])
    day = date.today()
    def link_keys(conn: db.Conn) -> int:
        facts, new = FactWriter(conn), 0
        for iid, poss in sorted(per_inst.items()):
            if value := _link_keys(poss):
                new += facts.add(
                    "instrument", iid, "nport_link_keys", value, tag="M", method_version=tm.METHOD,
                    source_name=LINK_SOURCE, source_url=dataset_doc.url, valid_from=day, producer=PRODUCER,
                    status="published", document_id=dataset_doc.id,
                ).created
        return new

    stats.facts_new += _in_transaction(link_keys)
    stats.gained = sorted(set(per_inst) - before)
    res = _in_transaction(lambda conn: write_medians(
        conn, store.watched_identifiers(conn), since=day - timedelta(days=MEDIAN_WINDOW_DAYS), producer=PRODUCER))
    stats.prices_new += res.prices_new
    stats.facts_new += res.facts_new


def _write_filing(conn: db.Conn, f: Filing, acc: str, keys: list[str], plan: Plan, rows: dict[str, dict[str, Any]],
                  dataset_doc: documents.DocumentRow, fx: FxVotes, stats: RunStats) -> None:
    assert f.period is not None and f.cik is not None and f.filed is not None
    facts = FactWriter(conn)
    doc = _Doc(dataset_doc.id, primary_doc_url(f.cik, acc), acc)
    found = [store.Match(position(rows[k]), (), (plan.links[k].instrument_id,)) for k in keys]
    w = store.write_filing(conn, doc, f.header(), found, filing_date=f.filed, producer=PRODUCER, fx=fx)  # type: ignore[arg-type]
    stats.filings += 1
    stats.holdings_new += w.holdings_new
    stats.holdings_linked += w.holdings_linked
    stats.facts_new += w.facts_new
    stats.prices_new += w.prices_new
    for k, m in zip(keys, found, strict=True):
        ev = plan.links[k]
        hid = _holding_id(conn, m.position, f, acc)
        stats.lines_linked += 1
        stats.by_instrument[ev.instrument_id] += 1
        if hid is None:
            continue
        linked_to = conn.execute("SELECT instrument_id FROM holding WHERE id = %s", (hid,)).fetchone()
        if linked_to and linked_to["instrument_id"] != ev.instrument_id:
            stats.skipped.append(f"holding {hid} already linked to {linked_to['instrument_id']}, "
                                 f"not {ev.instrument_id}")
            continue
        stats.facts_new += facts.add(
            "holding", str(hid), "link_basis", {"instrument_id": ev.instrument_id, "basis": ev.basis},
            tag="M", method_version=tm.METHOD, source_name=LINK_SOURCE, source_url=doc.url,
            valid_from=f.period, producer=PRODUCER, status="published", document_id=dataset_doc.id,
        ).created


def _holding_id(conn: db.Conn, p: Position, f: Filing, acc: str) -> int | None:
    from marden.seed.holdings_lines import nport_line_key

    fund = store.ensure_fund(conn, f.header())
    row = conn.execute(
        "SELECT id FROM holding WHERE fund_entity_id = %s AND accession = %s AND line_key = %s",
        (fund, acc, nport_line_key(store.line_code(p), p.units, p.balance, p.val_usd)),
    ).fetchone()
    return row["id"] if row else None


# ---------------------------------------------------------------- run
def latest_dataset(conn: db.Conn) -> str | None:
    row = conn.execute(
        "SELECT url FROM document WHERE source_id = %s AND doc_type = %s ORDER BY doc_date DESC NULLS LAST,"
        " fetched_at DESC LIMIT 1", (NPORT_SOURCE_ID, DATASET_DOC_TYPE),
    ).fetchone()
    if row is None:
        return None
    label = str(row["url"]).rsplit("/", 1)[-1].removesuffix("_nport.zip")
    return dataset_label(*parse_label(label))


def run(*, dataset: str | None = None, dry_run: bool = False) -> RunStats:
    stats = RunStats()
    sources.upsert_spec(SOURCE)
    with heartbeat.run(SOURCE_ID) as hb:
        with db.transaction() as conn:
            label = dataset or latest_dataset(conn) or dataset_label(*previous_quarter(date.today()))
            stats.dataset = label
            profiles = load_profiles(conn)
        doc = documents.latest_document(NPORT_SOURCE_ID, dataset_url(label))
        if doc is None:
            stats.ok, stats.error = False, f"data set {label} is not stored: run `collect sec_nport --dataset {label}`"
            return stats
        words = {w for p in profiles for w in p.tokens} - tm.GENERIC
        with zipfile.ZipFile(io.BytesIO(doc.read())) as zf:
            rows, filings = read_dataset(zf, words)
        positions = {k: tm_pos(k, r) for k, r in rows.items()}
        stats.positions = len(positions)
        plan = plan_links(profiles, positions)
        stats.rounds = plan.rounds
        stats.ambiguous = [f"{n}x {a}" for a, n in plan.ambiguous.most_common(60)]
        stats.dropped = plan.dropped
        if dry_run:
            for ev in plan.links.values():
                stats.by_instrument[ev.instrument_id] += 1
            stats.lines_linked = len(plan.links)
            return stats
        write(plan, rows, positions, filings, doc, stats)
        hb.seen(stats.positions)
        hb.new(stats.holdings_new + stats.holdings_linked)
        hb.meta.update({k: v for k, v in stats.meta().items() if k not in ("ambiguous",)})
    return stats


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dataset", help="DERA data set quarter, e.g. 2026q3 (default: the newest stored)")
    p.add_argument("--dry-run", action="store_true", help="match and report; write nothing")


@cli.action("collect", SOURCE_ID, help="N-PORT positions linked by filed terms (instruments without identifiers)",
            configure=_args)
def collect_command(args: argparse.Namespace) -> int:
    stats = run(dataset=args.dataset, dry_run=args.dry_run)
    print(stats.summary())
    return 0 if stats.ok else 1


@app.periodic(cron="40 13 * * 1", periodic_id="weekly_mon_0940_et")
@app.task(name="collect.sec_nport_terms", queue="collect", queueing_lock="collect.sec_nport_terms",
          lock="collect.sec_nport")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    """Weekly, after `sec_nport` has read the quarter's data set: link new positions by their filed terms."""
    return run().meta()
