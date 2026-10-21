"""Payer capacity and downgrade risk (spec 24): `payer_capacity` (GET /api/payers/:slug/capacity,
PayerCapacity, keyed by entity id) and `payers` (GET /api/payers, PayersPage: one row per payer, sorted by
downgrade risk).

Filed inputs are shown as filed: operating cash flow and capex per fiscal year from SEC XBRL
(`operating_cash_flow_12m_usd`, `capex_12m_usd`; fiscal year named by the year the period ends, the last
three filed). The exposed instruments are the debt behind the leases, compute contracts and guarantees
the payer owes on the obligation map, each with its size, the first rating-tied term its documents quote
(`agreement_term` / `covenant_test`, tag F) and its holder count.

Everything spec 24 calls "the model" is read from the `payer_capacity` model output (subject ('entity',
id)) and is n/d until that model runs: `ratings` (RatingState[] from agency releases), `spreadBp`,
`leaseAdjLeverageToday`, `leaseAdjLeveragePeak`, `triggerHeadroomTurns`, `breachYear`, `fundingGap3yB`,
`aiRevenueCoverage`, `topCounterparty` {name, share}, `downgradeRisk`, `years[]` (fiscalYear with
`leaseCommencingB`, `computeCommitmentsB`, `fundingGapB`, `leaseAdjLeverage`, and guided `ocfB` /
`capexB` for years not yet filed) and `exposed{instrumentId: {ratingTrigger, onTrip}}`. A value an output
carries is a bare number/string (tag M, its as-of and method version) or a Sourced<T>, passed through.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from marden.core.db import Conn, Row
from marden.readmodels.registry import builder
from marden.readmodels.views.payers_common import (
    LINK_LABELS,
    OWED_TYPES,
    Doc,
    billions_of,
    day,
    edge_inputs,
    edge_instruments,
    edge_known,
    entity_row,
    latest_output,
    model_field,
    model_value,
    nd,
    obligation_rows,
    output_field,
    payer_ids,
)
from marden.readmodels.views.payers_debt import instrument_info, trigger_texts
from marden.readmodels.views.sites_common import known_fact_rows

KIND_LABELS = {"cloud": "Cloud", "ai_lab": "AI lab", "neocloud": "Neocloud"}
AGENCIES = ("S&P", "Moody's", "Fitch")
RISKS = ("Low", "Watch", "High", "n/d")
RISK_ORDER = {"High": 0, "Watch": 1, "Low": 2}
CASH_FIELDS = {"ocfB": "operating_cash_flow_12m_usd", "capexB": "capex_12m_usd"}
MODEL_YEAR_FIELDS = ("leaseCommencingB", "computeCommitmentsB", "fundingGapB", "leaseAdjLeverage")
FILED_YEARS = 3
SCALARS = (
    "spreadBp",
    "leaseAdjLeverageToday",
    "leaseAdjLeveragePeak",
    "triggerHeadroomTurns",
    "breachYear",
    "fundingGap3yB",
    "aiRevenueCoverage",
)


# ---------------------------------------------------------------- filed cash flow by fiscal year
def _record(row: Row) -> dict[str, Any]:
    """The companyfacts record an XBRL fact quotes ({start, end, val, accn, fy, fp, form, filed})."""
    try:
        record = json.loads(row.get("quote") or "{}")
    except ValueError:
        return {}
    return record if isinstance(record, dict) else {}


def annual_rows(conn: Conn, entity_id: str, field: str) -> dict[str, Row]:
    """fiscal year label -> the latest-filed fiscal-year value of `field` known on the build date. Only
    values an annual report states (fp 'FY': a 10-K / 20-F year, not a trailing twelve months in a 10-Q);
    the year is named by the calendar year the period ends in."""
    out: dict[str, Row] = {}
    for row in known_fact_rows(conn, "entity", entity_id, [field])[field]:  # newest period first
        if _record(row).get("fp") not in (None, "FY"):
            continue
        end: date = row["valid_from"]
        out.setdefault(f"FY{end.year}", row)
    return out


def _model_years(out: Row | None) -> dict[str, dict[str, Any]]:
    years = {}
    for item in output_field(out, "years") or []:
        if isinstance(item, dict) and isinstance(item.get("fiscalYear"), str):
            years[item["fiscalYear"]] = item
    return years


def capacity_years(conn: Conn, entity_id: str, out: Row | None) -> list[Doc]:
    filed = {key: annual_rows(conn, entity_id, field) for key, field in CASH_FIELDS.items()}
    labels = sorted(set(filed["ocfB"]) | set(filed["capexB"]))[-FILED_YEARS:]
    modeled = _model_years(out)
    rows = []
    for label in sorted(set(labels) | set(modeled)):
        item = modeled.get(label, {})
        row: Doc = {"fiscalYear": label}
        for key in CASH_FIELDS:
            fact = filed[key].get(label)
            row[key] = billions_of(fact, "F") if fact is not None else model_value(out, item.get(key), "F")
        for key in MODEL_YEAR_FIELDS:
            row[key] = model_value(out, item.get(key), "F" if key.endswith("CommitmentsB") else "M")
        rows.append(
            {
                k: row[k]
                for k in (
                    "fiscalYear",
                    "ocfB",
                    "capexB",
                    "leaseCommencingB",
                    "computeCommitmentsB",
                    "fundingGapB",
                    "leaseAdjLeverage",
                )
            }
        )
    return rows


# ---------------------------------------------------------------- exposed instruments
def exposed(conn: Conn, entity_id: str, out: Row | None) -> list[Doc]:
    """ExposedInstrument per instrument behind an edge the payer owes (first link wins: lease, then compute
    contract, then guarantee)."""
    links: dict[str, str] = {}
    obligations = [
        ob for ob in obligation_rows(conn, entity_id) if ob["from_entity_id"] == entity_id and ob["type"] in OWED_TYPES
    ]
    obligations.sort(key=lambda ob: (OWED_TYPES.index(ob["type"]), ob["id"]))
    for ob in obligations:
        facts, rows = edge_inputs(conn, ob)
        if not edge_known(facts, rows):
            continue
        for iid in edge_instruments(ob, facts):
            links.setdefault(iid, LINK_LABELS[ob["type"]])
    info = instrument_info(conn, list(links))
    modeled = output_field(out, "exposed")
    if isinstance(modeled, list):  # the model writes ExposedInstrument[] (types.ts); index it by instrument
        modeled = {
            r["instrumentId"]: r for r in modeled if isinstance(r, dict) and isinstance(r.get("instrumentId"), str)
        }
    modeled = modeled if isinstance(modeled, dict) else {}
    rows = []
    for iid, link in links.items():
        if iid not in info:
            continue
        inst = info[iid]
        trigger, trip = trigger_texts(inst["ratingRow"])
        given = modeled.get(iid) if isinstance(modeled.get(iid), dict) else {}
        if given.get("ratingTrigger") is not None:
            trigger = model_value(out, given["ratingTrigger"], "F")
        if given.get("onTrip") is not None:
            trip = model_value(out, given["onTrip"], "F")
        rows.append(
            {
                "instrumentId": iid,
                "name": inst["name"],
                "link": link,
                "amountB": inst["amountB"],
                "ratingTrigger": trigger,
                "onTrip": trip,
                "holdersCount": inst["holdersCount"],
            }
        )
    rows.sort(key=lambda r: (-(r["amountB"].get("value") or 0), r["name"]))
    return rows


# ---------------------------------------------------------------- model fields
def _sourced_ok(value: Any) -> bool:
    return isinstance(value, dict) and "value" in value and value.get("tag") in ("F", "R", "M") and "asOf" in value


def ratings(out: Row | None) -> list[Doc]:
    """RatingState rows the model carries (agency releases; each value already Sourced)."""
    rows = []
    for item in output_field(out, "ratings") or []:
        if not isinstance(item, dict) or item.get("agency") not in AGENCIES:
            continue
        if not all(_sourced_ok(item.get(k)) for k in ("rating", "outlook", "lastAction")):
            continue
        triggers = [
            t
            for t in item.get("triggers") or []
            if isinstance(t, dict)
            and isinstance(t.get("metric"), str)
            and isinstance(t.get("threshold"), str)
            and isinstance(t.get("evidence"), dict)
        ]
        rows.append(
            {
                "agency": item["agency"],
                "rating": item["rating"],
                "outlook": item["outlook"],
                "lastAction": item["lastAction"],
                "triggers": triggers,
            }
        )
    return rows


def top_counterparty(out: Row | None) -> Doc | None:
    raw = output_field(out, "topCounterparty")
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
        return None
    share = model_value(out, raw.get("share"))
    return {"name": raw["name"], "share": share} if share["value"] is not None else None


def downgrade_risk(out: Row | None) -> Doc:
    value = model_field(out, "downgradeRisk")
    return value if value["value"] in RISKS else nd("M")


def capacity_doc(conn: Conn, entity: Row) -> Doc:
    out = latest_output(conn, "payer_capacity", "entity", entity["id"])
    doc: Doc = {
        "slug": entity["id"],
        "name": entity["name"],
        "kind": KIND_LABELS.get(entity["kind"], entity["kind"].replace("_", " ").capitalize()),
        "ratings": ratings(out),
    }
    for key in SCALARS:
        doc[key] = model_field(out, key)
    doc["topCounterparty"] = top_counterparty(out)
    doc["downgradeRisk"] = downgrade_risk(out)
    doc["years"] = capacity_years(conn, entity["id"], out)
    doc["exposed"] = exposed(conn, entity["id"], out)
    order = (
        "slug",
        "name",
        "kind",
        "ratings",
        "spreadBp",
        "leaseAdjLeverageToday",
        "leaseAdjLeveragePeak",
        "triggerHeadroomTurns",
        "breachYear",
        "fundingGap3yB",
        "aiRevenueCoverage",
        "topCounterparty",
        "downgradeRisk",
        "years",
        "exposed",
    )
    return {k: doc[k] for k in order}


@builder("payer_capacity", keys=payer_ids)
def build_payer_capacity(conn: Conn, key: str) -> Doc | None:
    entity = entity_row(conn, key)
    if entity is None or key not in payer_ids(conn):
        return None
    return capacity_doc(conn, entity)


def _risk_order(row: Doc) -> tuple:
    exposed_b = sum(r["amountB"].get("value") or 0 for r in row["exposed"])
    return (RISK_ORDER.get(row["downgradeRisk"].get("value"), 3), -exposed_b, row["name"])


@builder("payers", many=True)
def build_payers(conn: Conn):
    """Every payer's capacity row, highest downgrade risk first (then debt exposed, then name)."""
    rows = []
    for key in payer_ids(conn):
        entity = entity_row(conn, key)
        if entity is not None:
            rows.append(capacity_doc(conn, entity))
    rows.sort(key=_risk_order)
    yield "all", {"asOf": day().isoformat(), "rows": rows}
