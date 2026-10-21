"""docs/seed-data/unpriced.md from the database after the holdings loader (``--write-docs``).

An instrument counts as priced when a seeded ``price_obs`` row with a price exists for it (dealer quote,
fund mark or adjusted mark, from any seed loader: its document is a seed file). Prices and holdings that
collectors stored are not counted; generate it on a database the collectors have not written to (a row a
collector stored first keeps the collector's document). The reasons for the rest come from the instruments
loader's ``legacy_price_status`` facts and from the instrument's linked seeded holding lines.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from marden.core import db
from marden.seed.common import SEED_SOURCE_ID
from marden.seed.instruments_docs import STATUS_TEXT

NAIC_ONLY = "held only in insurer NAIC Schedule D statements (held for legal review, never a price)"
NO_USABLE_MARK = "held in seeded filings, but no linked line gives a usable mark (non-USD, unfunded, no par)"
NO_LEGACY = "no legacy price in the archive and no linked fund holding"


def _rows(conn: Any) -> list[dict[str, Any]]:
    return conn.execute(
        """WITH seed_doc AS (SELECT id FROM document WHERE source_id = %(seed)s),
                hold AS (SELECT h.* FROM holding h WHERE h.document_id IN (SELECT id FROM seed_doc))
           SELECT i.id, i.name, i.issuer_name, i.kind, i.in_v1,
                  EXISTS (SELECT 1 FROM price_obs p WHERE p.instrument_id = i.id AND p.price IS NOT NULL
                            AND p.document_id IN (SELECT id FROM seed_doc)) AS priced,
                  (SELECT count(*) FROM hold h WHERE h.instrument_id = i.id) AS n_hold,
                  (SELECT count(*) FROM hold h WHERE h.instrument_id = i.id
                     AND h.filing_type <> 'NAIC Schedule D') AS n_fund,
                  (SELECT f.value #>> '{}' FROM fact_current f WHERE f.subject_type = 'instrument'
                     AND f.subject_id = i.id AND f.field = 'legacy_price_status') AS legacy
             FROM instrument i ORDER BY i.id""",
        {"seed": SEED_SOURCE_ID},
    ).fetchall()


def _why(r: dict[str, Any]) -> str:
    if r["n_hold"] and not r["n_fund"]:
        return NAIC_ONLY
    if r["n_fund"]:
        return NO_USABLE_MARK
    if r["legacy"]:
        return STATUS_TEXT.get(r["legacy"], r["legacy"])
    return NO_LEGACY


def _line(r: dict[str, Any]) -> str:
    name = str(r["name"] or "").replace("|", "/")
    issuer = str(r["issuer_name"] or "").replace("|", "/")
    return f"| `{r['id']}` | {name} | {issuer} | {r['kind']} | {r['n_hold']} | {_why(r)} |"


def render_unpriced(conn: Any) -> str:
    rows = _rows(conn)
    unpriced = [r for r in rows if not r["priced"]]
    v1 = [r for r in unpriced if r["in_v1"]]
    reasons = Counter(_why(r) for r in unpriced)
    lines = [
        "# Instruments without a usable seeded price",
        "",
        f"Written by `python -m marden seed holdings --write-docs` on {date.today().isoformat()}, after the",
        "instruments loader (press dealer quotes, fund marks recomputed from each instrument's cited filing) and the",
        "holdings loader (N-PORT and BDC 10-Q marks of every linked holding line, cross-fund medians, the",
        f"unfunded-adjusted DDTL mark). {len(unpriced)} of {len(rows)} instruments have no `price_obs` row. TRACE,",
        "Börse Frankfurt and Yahoo values are never loaded (docs/methods/price-sources.md), so most bonds stay",
        "unpriced until a licensed TRACE feed or newer N-PORT filings arrive.",
        "",
        "| Why there is no seeded price | Instruments |",
        "|---|---|",
    ]
    lines += [f"| {why} | {n} |" for why, n in reasons.most_common()]
    for title, items in ((f"V1 instruments without a seeded price ({len(v1)})", v1),
                         (f"All other instruments without a seeded price ({len(unpriced) - len(v1)})",
                          [r for r in unpriced if not r["in_v1"]])):
        lines += ["", f"## {title}", "", "| Id | Instrument | Issuer | Kind | Linked holdings | Why |",
                  "|---|---|---|---|---|---|"]
        lines += [_line(r) for r in items]
    return "\n".join(lines) + "\n"


def write_unpriced(out_dir: Path) -> Path:
    with db.transaction() as conn:
        text = render_unpriced(conn)
    path = out_dir / "unpriced.md"
    path.write_text(text, encoding="utf-8")
    return path
