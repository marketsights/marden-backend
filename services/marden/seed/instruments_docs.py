"""docs/seed-data/v1-instruments.md and unpriced.md, written from a loader run (``--write-docs``)."""

from __future__ import annotations

from collections import Counter
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from marden.core import db
from marden.seed import instruments_facts as F
from marden.seed import instruments_prices as P
from marden.seed.instruments_v1 import V1_PICKS

if TYPE_CHECKING:
    from marden.seed.instruments import LoadReport

STATUS_TEXT = {
    P.TRACE_COPY: "legacy px was a copy of the finra.org TRACE pull (blocked)",
    P.FRANKFURT_COPY: "legacy px was a Börse Frankfurt fixing (needs licence)",
    P.ISSUER_FAIR_VALUE: "legacy px was the issuer's 'approximates fair value' statement (fact only)",
    P.MARK_NOT_RECOMPUTABLE: "legacy px cites a fund filing, but no line for it is in the archive",
    P.NOT_A_FUND_FILING: "legacy px source is not a fund filing or press dealer quote",
    P.DEALER_LOADED: "dealer quote loaded",
    P.MARK_RECOMPUTED: "fund mark recomputed",
}


def _facts(conn: Any, ids: list[str], fields: tuple[str, ...]) -> dict[tuple[str, str], Any]:
    rows = conn.execute(
        """SELECT subject_id, field, value FROM fact_current
           WHERE subject_type = 'instrument' AND subject_id = ANY(%s) AND field = ANY(%s)""",
        (ids, list(fields)),
    ).fetchall()
    return {(r["subject_id"], r["field"]): r["value"] for r in rows}


def _usd_b(value: Any) -> str:
    return f"{float(value) / 1e9:,.2f}" if value is not None else "n/d"


def render_v1(conn: Any, report: LoadReport) -> str:
    ids = [p.id for p in V1_PICKS]
    names = {r["id"]: r for r in conn.execute(
        "SELECT id, name, issuer_name, kind, site_slug FROM instrument WHERE id = ANY(%s)", (ids,)).fetchall()}
    facts = _facts(conn, ids, ("amount_usd", "amount_basis", "maturity", "coupon"))
    seg_counts = Counter(p.segment for p in V1_PICKS)
    lines = [
        "# V1 instruments (50)",
        "",
        f"Written by `python -m marden seed instruments --write-docs` on {date.today().isoformat()}. The list itself",
        "lives in `services/marden/seed/instruments_v1.py`; `instrument.in_v1` is true for exactly these ids.",
        "",
        "## How they were picked",
        "",
        "specs/00a-product-spec.md sets the V1 universe: the 50 largest AI-linked instruments of $1B or more, US",
        "only, 2024-2026 vintages, with stabilized colocation, CMBS and non-US deals left for V2. No locked list",
        "exists in the archive (DATA_DICTIONARY 3.9), so the picks come from:",
        "",
        "1. the case-linked rows (`aiwcredit.rowinfo.c`, a case's own debt), keeping US cases with debt of $1B+",
        "   and dropping the pool/ABS cases (QTS, DATABANK, CLOUDHQ) and SOUTHGATE (Australia), as site_map.json does;",
        "2. the spec-named examples resolved in DATA_DICTIONARY 1.1 (Jupiter, Frontier/Lighthouse, Abilene, The",
        "   Barn, Hyperion, CoreWeave DDTL 1.0-5.0, IREN, Nebius, Nscale, CoreWeave 2031s/2032s, the Vantage sub",
        "   loan, Oracle and CoreWeave as tenant credits);",
        "3. sizes checked against the facility registry (`aiwc/q3x/fac.json`), because aidebt `amt_m` is the drawn",
        "   amount on DDTLs (DDTL 4.0: 2,837 drawn vs 8,500 committed) and blank on the xAI VCI loans;",
        "4. vintages from fac `date` (Oracle tenant credits are the Sep-2025 and Feb-2026 issues, not older notes).",
        "",
        "Exceptions, kept because the spec names them: the Nebius US GPU term loan ($775M) and the Vantage HoldCo",
        "sub loan ($662M). Gaps: Anthropic Big Sky / North Star and OpenAI, Anthropic and Microsoft reference",
        "credits have no instrument in the archive (Big Sky exists only as fac `ANTH_COMPUTE_SPV_35B`), so the",
        "GPU-lease segment has 7 rows (target 8) and tenant credits 6 (target 7); construction loans carry 17",
        "(target 15) because every US case-linked project debt of $1B+ is in.",
        "",
        "| Segment | Count | Spec target |",
        "|---|---|---|",
    ]
    targets = {"Construction and project loans": 15, "Chip and GPU lease vehicles": 8,
               "GPU delayed-draw facilities": 12, "Neocloud and developer bonds": 8,
               "Tenant and end-user credits (reference)": 7}
    lines += [f"| {seg} | {n} | {targets.get(seg, '')} |" for seg, n in seg_counts.items()]
    lines += ["", "## The list", "",
              "| # | Id | Instrument | Issuer | Kind | Site | Size $B | Basis | Maturity | Seeded price | Why |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for n, p in enumerate(V1_PICKS, 1):
        r = names.get(p.id, {})
        lines.append(
            f"| {n} | `{p.id}` | {r.get('name', '')} | {r.get('issuer_name', '')} | {r.get('kind', '')} | "
            f"{r.get('site_slug') or ''} | {_usd_b(facts.get((p.id, 'amount_usd')))} | "
            f"{facts.get((p.id, 'amount_basis')) or ''} | {facts.get((p.id, 'maturity')) or 'n/d'} | "
            f"{F.price_basis_value(report.price_kinds.get(p.id, set()))} | {p.reason} |")
    return "\n".join(lines) + "\n"


def render_unpriced(conn: Any, report: LoadReport) -> str:
    unpriced = sorted(i for i, kinds in report.price_kinds.items() if not kinds)
    rows = {r["id"]: r for r in conn.execute(
        "SELECT id, name, issuer_name, kind, in_v1 FROM instrument WHERE id = ANY(%s)", (unpriced,)).fetchall()}
    by_status = Counter(report.legacy_status.get(i, "no legacy px") for i in unpriced)
    v1 = [i for i in unpriced if rows.get(i, {}).get("in_v1")]
    lines = [
        "# Instruments without a usable seeded price",
        "",
        f"Written by `python -m marden seed instruments --write-docs` on {date.today().isoformat()}:",
        f"{len(unpriced)} of {len(report.price_kinds)} instruments have no `price_obs` row after the instruments",
        "loader (press dealer quotes and fund marks recomputed from the filing each instrument cites, per",
        "docs/methods/price-sources.md). The holdings loader (N-PORT / BDC lines by CUSIP) can add fund marks",
        "later; rerun with `--write-docs` after it to refresh this list.",
        "",
        "| Why there is no seeded price | Instruments |",
        "|---|---|",
    ]
    lines += [f"| {STATUS_TEXT.get(s, s)} | {n} |" for s, n in by_status.most_common()]
    lines += ["", f"## V1 instruments without a seeded price ({len(v1)})", "",
              "| Id | Instrument | Issuer | Kind | Why |", "|---|---|---|---|---|"]
    lines += [_row(i, rows, report) for i in v1]
    lines += ["", f"## All other instruments without a seeded price ({len(unpriced) - len(v1)})", "",
              "| Id | Instrument | Issuer | Kind | Why |", "|---|---|---|---|---|"]
    lines += [_row(i, rows, report) for i in unpriced if i not in v1]
    return "\n".join(lines) + "\n"


def _row(iid: str, rows: dict[str, Any], report: LoadReport) -> str:
    r = rows.get(iid, {})
    why = STATUS_TEXT.get(report.legacy_status.get(iid, ""), "no legacy price in the archive")
    name = str(r.get("name", "")).replace("|", "/")
    issuer = str(r.get("issuer_name", "")).replace("|", "/")
    return f"| `{iid}` | {name} | {issuer} | {r.get('kind', '')} | {why} |"


def write_docs(out_dir: Path, report: LoadReport) -> list[Path]:
    with db.transaction() as conn:
        files = {out_dir / "v1-instruments.md": render_v1(conn, report),
                 out_dir / "unpriced.md": render_unpriced(conn, report)}
    for path, text in files.items():
        path.write_text(text, encoding="utf-8")
    return list(files)
