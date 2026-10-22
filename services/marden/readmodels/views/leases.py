"""Off-balance-sheet lease map (spec 25): `leases` (GET /api/leases, LeaseMapPage; GET /api/leases/:id cuts
one LeaseRecord from the same document).

One LeaseRecord per lease or compute contract on the obligation map that Marden has a source for: lessee
and lessor are the edge's two ends (the lessor Sourced by the edge's own type fact; a lessee no source
names shows as "Tenant not disclosed", with the sources' description of it in `tenantNote`), amounts,
start, term and contracted MW are the edge's facts, `sites` every linked site with its display name,
`siteSlug` / `siteName` the single linked site (null when it spans several or none) and the campus the
edge's own, else the one approved campus all its linked sites share (lease_common). Attribution follows
the edge's sources: Filed if any is filed, Reported if any is reported, else Inferred. Dependent debt is the
debt behind the edge (sizes, holders, whether its documents quote a rating-tied term); the lessee band is
the payer_capacity model's downgradeRisk for the lessee.

The `leases` model (subject 'all') owns the rest and is read as is: `reconciliation` (LesseeReconciliation
rows, each tying a lessee's filed not-yet-commenced total to filed + inferred + unattributed),
`leases{<id>: {turnsAdded, headroomBeforeTurns, triggerLease, priorityScore, confidence}}` and `inferred`
(LeaseRecord rows it built from the trail). Until it runs: no reconciliation rows, turns, headroom and
priority are n/d, no lease is flagged as a trigger lease, and the totals cover the mapped leases only
(disclosedB and unattributedB sum the reconciliation rows; attributedShare is attributed / disclosed
over those rows, 0 when none).
"""

from __future__ import annotations

from typing import Any

from marden.core.db import Conn, Row
from marden.readmodels.registry import builder
from marden.readmodels.views.instrument_inputs import latest_output as any_subject_output
from marden.readmodels.views.lease_common import (
    NOT_OUTSTANDING,
    STATUS_LABELS,
    lessee_label,
    single_campus,
    site_docs,
)
from marden.readmodels.views.payers_common import (
    LEASE_TYPES,
    Doc,
    billions_of,
    day,
    edge_inputs,
    edge_instruments,
    edge_known,
    edge_sites,
    edge_status,
    entity_rows,
    evidence_of,
    latest_output,
    model_field,
    model_value,
    nd,
    number_of,
    obligation_rows,
    output_field,
    site_names,
    text_of,
    with_value,
)
from marden.readmodels.views.payers_debt import instrument_info
from marden.readmodels.views.sites_common import day_end, site_slugs

CONFIDENCE = ("High", "Medium", "Low")
RISKS = ("Low", "Watch", "High", "n/d")


def _value(doc: Doc) -> float:
    v = doc.get("value")
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else 0.0


def site_campuses(conn: Conn, slug: str) -> list[str]:
    rows = conn.execute(
        """SELECT DISTINCT campus_id FROM site_link WHERE site_slug = %s AND status = 'approved'
             AND (approved_at IS NULL OR approved_at < %s)""",
        (slug, day_end()),
    ).fetchall()
    return [r["campus_id"] for r in rows]


def site_campus(conn: Conn, slug: str | None) -> str | None:
    return single_campus(lambda s: site_campuses(conn, s), [slug] if slug else [])


def attribution(facts: dict[str, Row], rows: dict[str, list[Row]]) -> str:
    tags = {r["tag"].strip() for r in rows["evidence"]}
    return "Filed" if "F" in tags else "Reported" if "R" in tags else "Inferred"


def lessee_band(conn: Conn, entity_id: str, cache: dict[str, Doc]) -> Doc:
    if entity_id not in cache:
        value = model_field(latest_output(conn, "payer_capacity", "entity", entity_id), "downgradeRisk")
        cache[entity_id] = value if value["value"] in RISKS else nd("M")
    return cache[entity_id]


def dependent_debt(conn: Conn, ids: list[str]) -> list[Doc]:
    info = instrument_info(conn, ids)
    rows = [
        {
            "instrumentId": iid,
            "name": i["name"],
            "amountB": i["amountB"],
            "ratingLinked": i["ratingRow"] is not None,
            "holdersCount": i["holdersCount"],
        }
        for iid, i in info.items()
    ]
    return sorted(rows, key=lambda r: (-_value(r["amountB"]), r["name"]))


def lease_record(conn: Conn, ob: Row, ctx: dict[str, Any]) -> Doc | None:
    facts, rows = edge_inputs(conn, ob)
    if not edge_known(facts, rows):
        return None
    names, model_out = ctx["names"], ctx["model"]
    given = model_leases(model_out).get(ob["id"])
    given = given if isinstance(given, dict) else {}
    sites = edge_sites(ob, facts, ctx["sites"])
    site = sites[0] if len(sites) == 1 else None
    kind_attr = attribution(facts, rows)
    confidence = given.get("confidence") if given.get("confidence") in CONFIDENCE else None
    return {
        "id": ob["id"],
        "lesseeId": ob["from_entity_id"],
        "lessee": lessee_label(ob["from_entity_id"], {k: v["name"] for k, v in names.items()}),
        "tenantNote": text_of(facts.get("tenant_description")),
        "lessor": with_value(facts["edge_type"], names[ob["to_entity_id"]]["name"]),
        "siteSlug": site,
        "siteName": ctx["site_names"].get(site) if site else None,
        "sites": site_docs(sites, ctx["site_names"]),
        "campusId": ob.get("campus_id") or single_campus(lambda s: site_campuses(conn, s), sites),
        "mw": number_of(facts.get("contracted_mw")),
        "kind": "Compute contract" if ob["type"] == "Compute purchase" else "Lease",
        "status": STATUS_LABELS[edge_status(facts, rows)],
        "totalB": billions_of(facts.get("total_usd")),
        "annualB": billions_of(facts.get("annual_usd")),
        "commencement": text_of(facts.get("start")),
        "termYears": number_of(facts.get("term_years")),
        "attribution": kind_attr,
        "confidence": confidence if kind_attr == "Inferred" else None,
        "turnsAdded": model_value(model_out, given.get("turnsAdded")),
        "headroomBeforeTurns": model_value(model_out, given.get("headroomBeforeTurns")),
        "triggerLease": given.get("triggerLease") is True,
        "lesseeBand": lessee_band(conn, ob["from_entity_id"], ctx["bands"]),
        "priorityScore": model_value(model_out, given.get("priorityScore")),
        "dependentDebt": dependent_debt(conn, edge_instruments(ob, facts)),
        "evidence": sorted((evidence_of(r) for r in rows["evidence"]), key=lambda e: e["date"], reverse=True),
    }


def _is_sourced(value: Any) -> bool:
    return isinstance(value, dict) and "value" in value and value.get("tag") in ("F", "R", "M") and "asOf" in value


def reconciliation(model_out: Row | None) -> list[Doc]:
    """The leases model's reconciliation rows (each already ties to its filing; passed through)."""
    rows = []
    numbers = ("filedOrReportedB", "inferredB", "attributedB", "unattributedB", "attributedShare", "namedTotalB")
    for item in output_field(model_out, "reconciliation") or []:
        if not isinstance(item, dict) or not isinstance(item.get("lessee"), str):
            continue
        filing = item.get("filing")
        if not (isinstance(filing, dict) and all(isinstance(filing.get(k), str) for k in ("form", "period", "url"))):
            continue
        if not _is_sourced(item.get("disclosedNotCommencedB")):
            continue
        if not all(isinstance(item.get(k), int | float) for k in numbers):
            continue
        def text(key: str, item: dict[str, Any] = item) -> str | None:
            return item[key] if isinstance(item.get(key), str) else None

        rows.append(
            {
                "lessee": item["lessee"],
                "lesseeId": text("lesseeId"),
                "filing": {k: filing[k] for k in ("form", "period", "url")},
                "commencement": text("commencement"),
                "terms": text("terms"),
                "disclosedNotCommencedB": item["disclosedNotCommencedB"],
                "namedLeases": item["namedLeases"] if isinstance(item.get("namedLeases"), int) else 0,
                **{k: item[k] for k in numbers},
                "note": text("note"),
            }
        )
    return rows


def model_leases(model_out: Row | None) -> dict[str, Any]:
    """The leases model's per-lease values by lease id: its `leases` is LeaseRecord[] (types.ts
    LeaseMapPage); a {id: {...}} map is read as is."""
    raw = output_field(model_out, "leases")
    if isinstance(raw, list):
        return {r["id"]: r for r in raw if isinstance(r, dict) and isinstance(r.get("id"), str)}
    return raw if isinstance(raw, dict) else {}


def inferred(model_out: Row | None) -> list[Doc]:
    rows = output_field(model_out, "inferred") or []
    if not rows:  # LeaseMapPage form: the inferred records sit in `leases` with attribution 'Inferred'
        rows = [r for r in model_leases(model_out).values() if r.get("attribution") == "Inferred"]
    return [
        r for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str) and r.get("attribution") == "Inferred"
    ]


def totals(leases: list[Doc], recon: list[Doc]) -> Doc:
    outstanding = [r for r in leases if r["status"] not in NOT_OUTSTANDING]
    disclosed = sum(_value(r["disclosedNotCommencedB"]) for r in recon)
    recon_attributed = sum(float(r["attributedB"]) for r in recon)
    debt: dict[str, float] = {}
    for lease in leases:
        for d in lease["dependentDebt"]:
            debt.setdefault(d["instrumentId"], _value(d["amountB"]))
    return {
        "disclosedB": round(disclosed, 3),
        "attributedB": round(sum(_value(r["totalB"]) for r in outstanding), 3),
        "unattributedB": round(sum(float(r["unattributedB"]) for r in recon), 3),
        "attributedShare": round(recon_attributed / disclosed, 4) if disclosed else 0.0,
        "triggerLeases": sum(1 for r in leases if r["triggerLease"]),
        "debtExposedB": round(sum(debt.values()), 3),
    }


def _rank(lease: Doc) -> tuple:
    score = lease["priorityScore"].get("value")
    return (not isinstance(score, int | float), -(score or 0), -_value(lease["totalB"]), lease["lessee"], lease["id"])


@builder("leases", many=True)
def build_leases(conn: Conn):
    """Every mapped lease and compute contract, highest priority score first (n/d last, then by size)."""
    obligations = [ob for ob in obligation_rows(conn) if ob["type"] in LEASE_TYPES]
    model_out = any_subject_output(conn, "leases", "all")  # the model's 'all' output (subject type not fixed)
    ctx = {
        "names": entity_rows(
            conn, [ob["from_entity_id"] for ob in obligations] + [ob["to_entity_id"] for ob in obligations]
        ),
        "model": model_out,
        "sites": site_slugs(conn),
        "site_names": site_names(conn),
        "bands": {},
    }
    leases = [r for ob in obligations if (r := lease_record(conn, ob, ctx)) is not None]
    edge_ids = {ob["id"] for ob in obligations}
    leases.extend(r for r in inferred(model_out) if r["id"] not in edge_ids)
    leases.sort(key=_rank)
    recon = reconciliation(model_out)
    yield "all", {"asOf": day().isoformat(), "totals": totals(leases, recon), "reconciliation": recon, "leases": leases}
