"""Capacity zone of every site, campus and power plant -> facts capacity_market, capacity_zone, capacity_market_reason.

A model over facts Marden already holds (tag M, producer 'model:capacity_zones', METHOD_VERSION):
- the market comes from the subject's `iso` fact (grid_join, HIFLD boundaries), else `grid_region`, else the
  seed's free-text `power_iso`; when none says, from a single market named in the utility text;
- PJM: the transmission zone from the utility text (`utility`, `power_utility`, or the iso text itself, e.g.
  "PJM (Dominion zone)"), with pjm_load_forecast's utility -> zone table plus the HIFLD legal names below,
  checked against the zones of PJM's latest stored auction results;
- ISO-NE: the capacity zone of the latest stored FCA that holds the subject's state (from each zone's
  published `load_zones`); a state split across capacity zones is left unassigned;
- MISO: the Local Resource Zone, NYISO: the load zone A-K, only where the facts name it ("MISO (Zone 9)");
- ERCOT, SPP, CAISO and non-RTO areas have no capacity auction: capacity_market 'none' and the reason, no
  capacity_zone. Text naming several markets (multi-site portfolios) is left unassigned, as is a subject
  with no grid fact; both are listed in the run meta, nothing is guessed.
source_name / source_url / quote are those of the fact the market was read from; valid_from is the newest
of the inputs. `python -m marden model capacity_zones`; the capacity collectors run it after each load.
"""

from __future__ import annotations

import argparse
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from marden import cli
from marden.collectors import capacity_common as cap
from marden.collectors.pjm_load_forecast import zones_for
from marden.collectors.rates_common import RunStats, collect, print_stats
from marden.core import db
from marden.core.db import Conn, Row
from marden.core.facts import FactWriter
from marden.core.queue import app
from marden.core.sources import declare_source

SOURCE_ID = "capacity_zones"
PRODUCER = "model:capacity_zones"
METHOD_VERSION = "capacity_zones-0.1"
MARKET_FIELDS = ("iso", "grid_region", "power_iso")
UTILITY_FIELDS = ("utility", "power_utility")
SUBJECT_TABLES = (("site", "site", "slug"), ("campus", "campus", "id"), ("plant", "power_plant", "id"))
CRON_UTC = "41 9 * * 1"  # Mondays 09:41 UTC: picks up new grid facts between monthly auction checks
# Markets whose collectors announce through capacity_common.emit_auction_events (one price per zone and
# auction); NYISO and MISO announce their own headlines (capacity_headlines).
EVENT_MARKETS = {"pjm": "PJM", "isone": "ISO-NE"}
SOURCE = declare_source(
    SOURCE_ID, "Marden capacity zone map", "model", None, 7 * 24 * 60,
    "Internal model over published site, campus and plant grid facts and the stored PJM / ISO-NE auction "
    "results (zone lists, ISO-NE load zones per capacity zone); makes no external requests.",
)

NO_AUCTION = {
    "ercot": "ERCOT is an energy-only market: it runs no capacity auction and puts no capacity obligation on load.",
    "spp": "SPP runs no capacity auction: load-serving entities meet SPP's planning reserve margin requirement "
           "with their own or bilaterally contracted capacity.",
    "caiso": "CAISO runs no capacity auction: resource adequacy is procured bilaterally under CPUC and local "
             "regulatory authority programs.",
    "non_rto": "Outside the RTO capacity markets (vertically integrated utility or bilateral region): no "
               "capacity auction.",
}
_MARKETS = (
    ("pjm", r"\bpjm\b"),
    ("miso", r"\bmiso\b|midcontinent independent"),
    ("nyiso", r"\bnyiso\b|new york independent system operator"),
    ("isone", r"\biso[- ]?ne\b|\biso new england\b"),
    ("ercot", r"\bercot\b"),
    ("spp", r"\bspp\b|southwest power pool"),
    ("caiso", r"\bcaiso\b|california independent system operator"),
    ("non_rto", r"\bnon[- ]rto\b|\btva\b|vertically integrated"),
)
_WEAK_NON_RTO = r"\bwecc\b|\bserc\b"  # reliability regions: a market only when no ISO/RTO is named
_NEGATED = re.compile(r"\bnot\s+(?:in\s+)?(?:the\s+)?[a-z][a-z-]*", re.I)
# HIFLD retail service territory names (grid_join `utility`) that pjm_load_forecast's table does not cover.
PJM_LEGAL_NAMES = (
    (r"virginia electric (&|and) power", "DOM"),
    (r"monongahela power", "APS"),
    (r"rockland electric", "RECO"),
    (r"ohio valley electric", "OVEC"),
    (r"pennsylvania power co\b", "ATSI"),
)
RPM_ZONES = ("AE", "AEP", "APS", "ATSI", "BGE", "COMED", "DAYTON", "DEOK", "DLCO", "DOM", "DPL", "EKPC", "JCPL",
             "METED", "OVEC", "PECO", "PENLC", "PEPCO", "PL", "PS", "RECO")  # until PJM results are stored
ISONE_LOAD_ZONE_STATE = {"CT": "CT", "ME": "ME", "NH": "NH", "VT": "VT", "RI": "RI", "NEMA": "MA", "SEMA": "MA",
                         "WCMA": "MA"}
_MISO_ZONE = re.compile(r"\b(?:lrz|zone)\s*(\d{1,2})\b", re.I)
_NYISO_ZONE = re.compile(r"\bzone\s*([a-k])\b", re.I)
_STATE = re.compile(r"^[A-Z]{2}$")


@dataclass
class Basis:
    field: str
    fact_id: int
    text: str
    source_name: str
    source_url: str | None
    valid_from: date


@dataclass
class Assignment:
    market: str | None = None  # pjm / isone / miso / nyiso / none; None = unassigned
    zone: str | None = None
    reason: str | None = None  # why 'none', or why the subject is unassigned / has no zone
    basis: Basis | None = None
    zone_loc: str | None = None
    extra_from: list[date] = field(default_factory=list)  # other inputs' dates (ISO-NE zone definitions)


def markets_in(text: str) -> set[str]:
    low = _NEGATED.sub(" ", text.lower())
    found = {m for m, pattern in _MARKETS if re.search(pattern, low)}
    if not found and re.search(_WEAK_NON_RTO, low):
        found.add("non_rto")
    return found


def pjm_zone(texts: list[str], known: set[str]) -> tuple[str | None, str | None]:
    """(zone, note) from utility / iso texts; several zones or an unknown code -> (None, why)."""
    zones: list[str] = []
    for text in texts:
        for zone, _area in zones_for(text):
            if zone not in zones:
                zones.append(zone)
        for pattern, zone in PJM_LEGAL_NAMES:
            if re.search(pattern, text.lower()) and zone not in zones:
                zones.append(zone)
    if len(zones) != 1:
        return None, ("utility text names no PJM zone" if not zones else f"utility text names several PJM zones {zones}")
    if zones[0] not in known:
        return None, f"PJM zone {zones[0]} is not in PJM's latest auction results"
    return zones[0], f"PJM transmission zone {zones[0]} from the utility name"


def pjm_zones(conn: Conn) -> set[str]:
    rows = conn.execute(
        """SELECT DISTINCT split_part(subject_id, ':', 3) AS slug FROM fact_current
           WHERE subject_type = 'series' AND subject_id LIKE %s AND field = 'zonal_capacity_price'""",
        ("capacity:pjm:%",),
    ).fetchall()
    slugs = {r["slug"] for r in rows}
    return {z for z in RPM_ZONES if z.lower() in slugs} if slugs else set(RPM_ZONES)


def isone_state_zones(conn: Conn) -> tuple[dict[str, set[str]], date | None, str | None]:
    """state -> capacity zones of the latest stored FCA (from each zone's `load_zones`), its date and period."""
    rows = conn.execute(
        """SELECT subject_id, value, valid_from FROM fact_current
           WHERE subject_type = 'series' AND subject_id LIKE %s AND field = 'load_zones'""",
        ("capacity:isone:%",),
    ).fetchall()
    if not rows:
        return {}, None, None
    period = max(r["subject_id"].split(":")[3] for r in rows)
    out: dict[str, set[str]] = {}
    when: date | None = None
    for r in rows:
        _, _, zone, p = r["subject_id"].split(":")
        if p != period:
            continue
        when = max(when, r["valid_from"]) if when else r["valid_from"]
        for load_zone in r["value"]:
            state = ISONE_LOAD_ZONE_STATE.get(str(load_zone).upper())
            if state:
                out.setdefault(state, set()).add(zone.upper())
    return out, when, period


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_text(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(_text(v) for v in value)
    return "" if value is None else str(value)


def _basis(row: Row) -> Basis:
    return Basis(row["field"], row["id"], _text(row["value"]).strip(), row["source_name"], row["source_url"],
                 row["valid_from"])


def classify(facts: dict[str, Row], state: str | None, ctx: dict[str, Any]) -> Assignment:
    market_rows = [facts[f] for f in MARKET_FIELDS if f in facts and _text(facts[f]["value"]).strip()]
    util_rows = [facts[f] for f in UTILITY_FIELDS if f in facts and _text(facts[f]["value"]).strip()]
    basis, found = None, set()
    for row in market_rows:  # the first field that names a market decides
        found = markets_in(_text(row["value"]))
        if found:
            basis = _basis(row)
            break
    if not found:
        for row in util_rows:
            found = markets_in(_text(row["value"]))
            if found:
                basis = _basis(row)
                break
    if not found:
        return Assignment(reason="no grid fact names a market" if (market_rows or util_rows) else "no grid facts")
    if len(found) > 1:
        return Assignment(reason=f"facts name several markets {sorted(found)}", basis=basis)
    market = found.pop()
    if market in NO_AUCTION:
        return Assignment(market="none", reason=NO_AUCTION[market], basis=basis)
    texts = [basis.text]
    texts += [t for r in market_rows + util_rows if (t := _basis(r).text) not in texts]
    out = Assignment(market=market, basis=basis)
    if market == "pjm":
        out.zone, out.zone_loc = pjm_zone(texts, ctx["pjm_zones"])
    elif market == "miso":
        m = next((m for t in texts if (m := _MISO_ZONE.search(t)) and 1 <= int(m[1]) <= 10), None)
        out.zone, out.zone_loc = (f"LRZ{int(m[1])}", f"MISO zone named in the text: {m[0]!r}") if m else (
            None, "no MISO Local Resource Zone named")
    elif market == "nyiso":
        m = next((m for t in texts if (m := _NYISO_ZONE.search(t))), None)
        out.zone, out.zone_loc = (m[1].upper(), f"NYISO zone named in the text: {m[0]!r}") if m else (
            None, "no NYISO load zone named")
    elif market == "isone":
        zones = ctx["isone_states"].get(state or "", set())
        if len(zones) == 1:
            out.zone = next(iter(zones))
            out.zone_loc = f"ISO-NE capacity zone holding state {state} in {ctx['isone_period']} (load_zones)"
            out.extra_from.append(ctx["isone_date"])
        else:
            out.zone_loc = f"state {state!r} maps to capacity zones {sorted(zones)} in the latest FCA"
    if out.zone is None:
        out.reason = out.zone_loc
    return out


def _subjects(conn: Conn) -> list[tuple[str, str, str | None, bool]]:
    out: list[tuple[str, str, str | None, bool]] = []
    for stype, table, key in SUBJECT_TABLES:
        v1 = "in_v1" if table == "site" else "false"
        for r in conn.execute(f"SELECT {key} AS id, state, {v1} AS v1 FROM {table} ORDER BY {key}").fetchall():
            state = (r["state"] or "").strip().upper()
            out.append((stype, r["id"], state if _STATE.match(state) else None, r["v1"]))
    return out


def _facts(conn: Conn) -> dict[tuple[str, str], dict[str, Row]]:
    rows = conn.execute(
        """SELECT * FROM fact_current WHERE subject_type IN ('site', 'campus', 'plant') AND field = ANY(%s)""",
        (list(MARKET_FIELDS + UTILITY_FIELDS),),
    ).fetchall()
    out: dict[tuple[str, str], dict[str, Row]] = {}
    for r in rows:
        out.setdefault((r["subject_type"], r["subject_id"]), {})[r["field"]] = r
    return out


def _write(facts: FactWriter, stype: str, sid: str, a: Assignment, stats: RunStats) -> None:
    assert a.basis is not None and a.market is not None
    valid_from = max([a.basis.valid_from, *a.extra_from])
    loc = f"from {stype}.{a.basis.field} (fact {a.basis.fact_id})" + (f"; {a.zone_loc}" if a.zone_loc else "")
    values: dict[str, str] = {"capacity_market": a.market}
    if a.zone:
        values["capacity_zone"] = a.zone
    if a.market == "none" and a.reason:
        values["capacity_market_reason"] = a.reason
    for name, value in values.items():
        res = facts.add(
            stype, sid, name, value, tag="M", source_name=a.basis.source_name, source_url=a.basis.source_url,
            valid_from=valid_from, producer=PRODUCER, status="published", method_version=METHOD_VERSION,
            quote=a.basis.text[:1000] or None, quote_loc=loc,
        )
        stats.facts_seen += 1
        stats.facts_new += int(res.created)


def assign(conn: Conn, stats: RunStats) -> dict[str, Any]:
    """Classify every site, campus and plant and write the facts. Returns a summary (also in stats.extra)."""
    isone_states, isone_date, isone_period = isone_state_zones(conn)
    ctx = {"pjm_zones": pjm_zones(conn), "isone_states": isone_states, "isone_date": isone_date,
           "isone_period": isone_period}
    facts_by_subject = _facts(conn)
    writer = FactWriter(conn)
    by_market: Counter[str] = Counter()
    v1: dict[str, str] = {}
    unassigned: dict[str, str] = {}
    for stype, sid, state, in_v1 in _subjects(conn):
        a = classify(facts_by_subject.get((stype, sid), {}), state, ctx)
        if a.market is None:
            unassigned[f"{stype}:{sid}"] = a.reason or "unassigned"
            by_market["unassigned"] += 1
        else:
            _write(writer, stype, sid, a, stats)
            by_market[a.market] += 1
        if in_v1:
            v1[sid] = (f"{a.market}:{a.zone}" if a.zone else f"{a.market} ({a.reason})") if a.market else (
                f"unassigned ({a.reason})")
    summary = {"by_market": dict(sorted(by_market.items())), "v1_sites": v1, "unassigned": unassigned}
    stats.extra["capacity_zones"] = summary
    return summary


def run() -> dict[str, Any]:
    def body(stats: RunStats) -> None:
        with db.transaction() as conn:
            assign(conn, stats)
            cap.emit_auction_events(conn, EVENT_MARKETS, stats)

    return collect(SOURCE, body)


@app.periodic(cron=CRON_UTC, periodic_id="weekly_mon_0941_utc")
@app.task(name="models.capacity_zones", queue="models", queueing_lock="models.capacity_zones",
          lock="models.capacity_zones")
def assign_task(timestamp: int | None = None) -> dict[str, Any]:
    return run()


@cli.action("model", SOURCE_ID, help="Capacity market and zone of every site, campus and plant (+ auction events)")
def assign_cli(args: argparse.Namespace) -> int:
    return print_stats(run())
