"""Power plant debt (spec 20): `plant_debt` (types.ts PlantDebt), the debt and contracts behind a plant.

Every list holds only what a record states, as known on the build date; "nothing filed" is an empty
list (shown as n/d), never zero:

- owner: the plant's `owner` fact (state business or commission filings) else the air permit's
  applicant (the permit record, spec 20's first source); n/d otherwise.
- liens: `ucc_lien` facts (one per UCC-1 financing statement: secured party, debtor, state, file
  number, filed date, collateral, URL) once the Secretary of State collectors write them.
- mortgages: `mortgage` facts (county deed of trust / mortgage records: lender, amount_usd, recorded).
- financing: `financing` facts (lender, type, amount_usd from SEC exhibits, supplier filings, press).
- powerBuyer: the obligation-map Power purchase edge (spec 18) whose seller is the plant's operator
  entity and whose site or campus the plant serves; term from the edge's `term_years`.
- equipmentOrders: `equipment_order` facts (OEM, units, delivery as stated by the supplier).
"""

from __future__ import annotations

from typing import Any

from marden.core.db import Conn, Row
from marden.readmodels.context import known_fact_rows, known_facts
from marden.readmodels.registry import builder
from marden.readmodels.views.facilities_common import plant_ids, plant_row, served_campuses
from marden.readmodels.views.sites_common import Doc, day, fact_evidence, nd, next_day, src_or_nd, with_value

ITEM_FIELDS = ("ucc_lien", "mortgage", "financing", "equipment_order")


def _items(rows: list[Row]) -> list[tuple[Row, dict[str, Any]]]:
    return [(r, r["value"]) for r in rows if isinstance(r["value"], dict) and r["valid_from"] <= day()]


def _money(row: Row, value: Any) -> Doc:
    return with_value(row, value) if isinstance(value, int | float) and not isinstance(value, bool) else nd(row["tag"])


def owner(facts: dict[str, Row]) -> Doc:
    if facts.get("owner"):
        return src_or_nd(facts["owner"], "F")
    permit = facts.get("air_permit")
    applicant = permit["value"].get("applicant") if permit and isinstance(permit["value"], dict) else None
    return with_value(permit, applicant) if permit and isinstance(applicant, str) and applicant else nd("F")


def liens(rows: list[Row]) -> list[Doc]:
    out = []
    for row, v in _items(rows):
        if not (v.get("secured_party") and v.get("file_number")):
            continue
        out.append({"securedParty": str(v["secured_party"]), "debtor": str(v.get("debtor") or ""),
                    "state": str(v.get("state") or ""), "fileNumber": str(v["file_number"]),
                    "filed": str(v.get("filed") or row["valid_from"].isoformat()),
                    "collateral": str(v.get("collateral") or ""), "url": v.get("url") or row.get("source_url") or ""})
    return sorted(out, key=lambda lien: lien["filed"], reverse=True)


def mortgages(rows: list[Row]) -> list[Doc]:
    return [{"lender": str(v["lender"]), "amount": _money(row, v.get("amount_usd")),
             "recorded": str(v.get("recorded") or row["valid_from"].isoformat()),
             "url": v.get("url") or row.get("source_url") or ""}
            for row, v in _items(rows) if v.get("lender")]


def financing(rows: list[Row]) -> list[Doc]:
    return [{"lender": str(v["lender"]), "type": str(v.get("type") or "n/d"), "amount": _money(row, v.get("amount_usd")),
             "evidence": fact_evidence(row, v.get("text") or v.get("quote") or row.get("quote") or None)}
            for row, v in _items(rows) if v.get("lender")]


def equipment_orders(rows: list[Row]) -> list[Doc]:
    out = []
    for row, v in _items(rows):
        units = v.get("units")
        if v.get("oem") and isinstance(units, int) and not isinstance(units, bool):
            delivery = with_value(row, str(v["delivery"])) if v.get("delivery") else nd(row["tag"])
            out.append({"oem": str(v["oem"]), "units": units, "delivery": delivery})
    return out


def power_buyer(conn: Conn, plant_id: str, facts: dict[str, Row]) -> Doc | None:
    """The Power purchase edge sold by the plant's operator for a site or campus the plant serves."""
    operator = (facts.get("operator_entity_id") or {}).get("value")
    if not isinstance(operator, str):
        return None
    campuses = [c["id"] for c in served_campuses(conn, plant_id)]
    sites = (facts.get("serves_sites") or {}).get("value")
    sites = [s for s in sites if isinstance(s, str)] if isinstance(sites, list) else []
    rows = conn.execute(
        """SELECT o.id, o.from_entity_id, e.name AS buyer FROM obligation o
             LEFT JOIN entity e ON e.id = o.from_entity_id
            WHERE o.type = 'Power purchase' AND o.to_entity_id = %s AND o.created_at < %s::date
              AND (o.campus_id = ANY(%s) OR o.site_slug = ANY(%s)) ORDER BY o.id""",
        (operator, next_day(), campuses, sites),
    ).fetchall()
    for row in rows:
        of = known_facts(conn, "obligation", row["id"], ["edge_type", "term_years"])
        if not of.get("edge_type"):
            continue  # the edge was not public on the build date
        term = of.get("term_years")
        years = term["value"] if term else None
        term_doc = with_value(term, f"{years:g} years") if isinstance(years, int | float) else nd("F")
        return {"name": row["buyer"] or row["from_entity_id"], "term": term_doc, "edgeId": row["id"]}
    return None


def plant_debt(conn: Conn, plant_id: str) -> Doc | None:
    if plant_row(conn, plant_id) is None:
        return None
    facts = known_facts(conn, "plant", plant_id, ["owner", "air_permit", "operator_entity_id", "serves_sites"])
    items = known_fact_rows(conn, "plant", plant_id, list(ITEM_FIELDS))
    return {
        "owner": owner(facts),
        "liens": liens(items["ucc_lien"]),
        "mortgages": mortgages(items["mortgage"]),
        "financing": financing(items["financing"]),
        "powerBuyer": power_buyer(conn, plant_id, facts),
        "equipmentOrders": equipment_orders(items["equipment_order"]),
    }


@builder("plant_debt", keys=plant_ids)
def build_plant_debt(conn: Conn, plant_id: str) -> Doc | None:
    return plant_debt(conn, plant_id)
