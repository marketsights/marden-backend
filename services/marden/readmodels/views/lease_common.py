"""Shared pieces of a LeaseRecord (types.ts) for the `leases` model (models/lease_map.py) and the `leases`
read model (readmodels/views/leases.py), so both build the same document:

- the lessee as shown: the edge's payer, or "Tenant not disclosed" when no source names it (the
  `unnamed-tenants` placeholder entity), with the sources' own description of that tenant
  (`tenant_description`: "a high-investment-grade company rated AA- or higher") as `tenantNote`;
- every linked site with its display name (`sites`), not only the single one `siteSlug` carries;
- the campus: the edge's own, else the one campus every linked site's approved link points to;
- the lease's contracted capacity (`contracted_mw`, MW of critical IT load unless the source says gross);
- the per-lessee reconciliation (spec 25 "What 100% mapped means") from each lessee's latest 10-K/10-Q
  "leases not yet commenced" total (`leases_not_commenced_usd`, with the filing's form, period, commencement
  window and term range in `leases_not_commenced_filing`): disclosed = attributed + not yet attributed, to
  the dollar. Attributed = the stated totals of the lessee's named leases (kind Lease, not commenced or
  terminated): filed or reported, plus inferred (none built in v0). When the named totals exceed the
  disclosed figure (a named lease has partly commenced, or the filer leaves variable-rent leases out of the
  note) attribution is capped at the disclosed total, the residual is 0 and the row says why in `note`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any

from marden.core.db import Row

Doc = dict[str, Any]

UNNAMED_TENANT = "unnamed-tenants"
TENANT_NOT_DISCLOSED = "Tenant not disclosed"
RECON_FIELDS = ("leases_not_commenced_usd", "leases_not_commenced_filing")
# EdgeStatus -> the status a lease shows, in the obligation map's words (a lease "starts" when rent begins).
STATUS_LABELS = {
    "Started": "Started",
    "Signed": "Signed, not started",
    "Announced": "Announced",
    "Amended": "Amended",
    "Terminated": "Terminated",
    "Disputed": "Disputed",
}
NOT_OUTSTANDING = ("Started", "Terminated")


def lessee_label(entity_id: str, names: dict[str, str]) -> str:
    if entity_id == UNNAMED_TENANT:
        return TENANT_NOT_DISCLOSED
    return names.get(entity_id, entity_id)


def site_docs(slugs: Iterable[str], site_names: dict[str, str]) -> list[Doc]:
    """[{slug, name}] for each linked site, in the edge's order (name = the site's display name)."""
    return [{"slug": s, "name": site_names.get(s) or s} for s in slugs]


def single_campus(campuses_by_site: Callable[[str], list[str]], slugs: Sequence[str]) -> str | None:
    """The one campus all linked sites sit on (each site's approved links), else None."""
    if not slugs:
        return None
    found: set[str] = set()
    for slug in slugs:
        ids = campuses_by_site(slug)
        if len(ids) != 1:
            return None
        found.update(ids)
    return found.pop() if len(found) == 1 else None


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def reconciliation_rows(
    lessee_facts: dict[str, dict[str, Row]],
    leases: Sequence[Doc],
    names: dict[str, str],
    sourced: Callable[[Row, Any], Doc],
) -> tuple[list[Doc], list[str]]:
    """LesseeReconciliation rows (one per lessee with a disclosed total) and `missing` notes.

    `lessee_facts`: entity id -> {field: current fact row} for RECON_FIELDS. `leases`: LeaseRecords carrying
    `lesseeId`. `sourced(row, value)` builds the Sourced<number> of the disclosed total ($B)."""
    rows: list[Doc] = []
    notes: list[str] = []
    for eid in sorted(lessee_facts):
        facts = lessee_facts[eid]
        total_row = facts.get("leases_not_commenced_usd")
        usd = _num(total_row["value"]) if total_row else None
        if total_row is None or usd is None:
            continue
        filing_row = facts.get("leases_not_commenced_filing")
        filing = filing_row["value"] if filing_row and isinstance(filing_row["value"], dict) else {}
        disclosed = round(usd / 1e9, 3)
        named = [
            r
            for r in leases
            if r.get("lesseeId") == eid
            and r["kind"] == "Lease"
            and r["status"] not in NOT_OUTSTANDING
            and _num(r["totalB"].get("value")) is not None
        ]
        filed = round(sum(r["totalB"]["value"] for r in named if r["attribution"] != "Inferred"), 3)
        inferred = round(sum(r["totalB"]["value"] for r in named if r["attribution"] == "Inferred"), 3)
        raw = round(filed + inferred, 3)
        note = None
        if raw > disclosed:
            note = (
                f"Named lease totals (${raw:,.1f}B) exceed the filed not-commenced total (${disclosed:,.1f}B): "
                "some named leases have partly commenced or are outside the filing's figure, so attribution is "
                "capped at the filed total."
            )
            notes.append(f"{lessee_label(eid, names)}: {note}")
            # Named (filed or reported) leases count first; inferred ones are scaled back to fit (spec 25 step 5).
            filed = min(filed, disclosed)
            inferred = round(disclosed - filed, 3) if inferred else 0.0
        attributed = round(filed + inferred, 3)
        rows.append(
            {
                "lessee": lessee_label(eid, names),
                "lesseeId": eid,
                "filing": {
                    "form": str(filing.get("form") or "10-K/10-Q"),
                    "period": str(filing.get("period") or total_row["valid_from"].isoformat()),
                    "url": str(total_row.get("source_url") or ""),
                },
                "commencement": filing.get("commence") if isinstance(filing.get("commence"), str) else None,
                "terms": filing.get("terms") if isinstance(filing.get("terms"), str) else None,
                "disclosedNotCommencedB": sourced(total_row, disclosed),
                "namedLeases": len(named),
                "namedTotalB": raw,
                "filedOrReportedB": filed,
                "inferredB": inferred,
                "attributedB": attributed,
                "unattributedB": round(disclosed - attributed, 3),
                "attributedShare": round(attributed / disclosed, 4) if disclosed else 0.0,
                "note": note,
            }
        )
    rows.sort(key=lambda r: -r["disclosedNotCommencedB"]["value"])
    return rows, notes
