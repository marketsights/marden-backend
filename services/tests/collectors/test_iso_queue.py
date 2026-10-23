"""iso_queue / iso_queue_match: value helpers, the match rule, and the store's change-only writes, links,
events and large-load lists. Entries come from the real NYISO workbook excerpt (data/nyiso_queue)."""

from __future__ import annotations

import dataclasses
from datetime import date
from pathlib import Path

from marden.collectors import iso_queue as q
from marden.collectors import iso_queue_match as qm
from marden.collectors import nyiso_queue as nyiso
from marden.collectors.rates_common import RunStats
from marden.core.facts import FactWriter

DATA = Path(__file__).parent / "data" / "nyiso_queue"
REPORT = date(2026, 9, 30)


def _entries() -> dict[str, q.QueueEntry]:
    wb = (DATA / "NYISO-Interconnection-Queue-09302026_excerpt.xlsx").read_bytes()
    return {e.queue_id: e for e in nyiso.parse_workbook(wb)}


def add_site(conn, slug: str, name: str, state: str, county: str, *, tracking: tuple[str, ...] = (),
             developer: str | None = None, facts: dict[str, str] | None = None) -> None:
    """A site row as the seed loader writes it (identity + chain party + location facts)."""
    conn.execute("INSERT INTO site (slug, name, state, county, tracking_names) VALUES (%s, %s, %s, %s, %s)",
                 (slug, name, state, county, list(tracking)))
    if developer:
        conn.execute("INSERT INTO chain_party (site_slug, role, label) VALUES (%s, 'developer', %s)", (slug, developer))
    writer = FactWriter(conn)
    for field, value in (facts or {}).items():
        writer.add("site", slug, field, value, tag="R", source_name="test", valid_from=date(2026, 10, 1),
                   producer="seed:test", status="published")


def add_lake_mariner(conn) -> None:
    add_site(conn, "lake-mariner", "Lake Mariner", "NY", "Niagara",
             tracking=("WULF Compute LLC", "La Lupa Data LLC", "TeraWulf Brookings"), developer="TeraWulf",
             facts={"place": "Barker, New York",
                    "location": "Lake Mariner Data Campus, Barker (Town of Somerset), Niagara County, NY"})


def test_value_helpers():
    assert q.text("  Kintigh \n 345kV ") == "Kintigh 345kV" and q.text("N/A") is None and q.text(849.0) == "849"
    assert q.number("1,206.0") == 1206 and q.number("90.5") == 90.5 and q.number("TBD") is None
    assert q.us_date("9/18/2008") == date(2008, 9, 18) and q.us_date("1900-01-14") is None
    assert q.month_or_date("04-2028") == "2028-04" and q.month_or_date("06/2027") == "2027-06"
    assert q.month_or_date("1-2031") == "2031-01" and q.month_or_date("I/S") is None
    assert q.month_or_date("12/31/2029") == "2029-12-31" and q.month_or_date("2030") == "2030"


def test_status_classes():
    cases = {
        "Withdrawn": "withdrawn", "Retracted": "withdrawn", "Annulled": "withdrawn", "Pending Termination": "active",
        "Rejected Cost Allocation/Next FS Pending": "active", "Deactivated": "retired", "In Service": "in_service",
        "In Service Commercial": "in_service", "Commercial": "in_service", "Under Construction": "under_construction",
        "UC-ISP": "under_construction", "Partially in Service - Under Construction": "under_construction",
        "Partial In-Service": "under_construction", "Suspended": "suspended", "EP": "active", "Active": "active",
    }
    assert {s: q.status_class(s) for s in cases} == cases


def test_phrases_drop_generic_and_place_words():
    assert qm.phrase("Lake Mariner Data LLC") == ("lake", "mariner")
    assert qm.phrase("WULF Compute LLC") == ("wulf",)
    assert qm.phrase("Applied Digital") == ("applied", "digital")  # common word keeps its generic partner
    assert qm.phrase("Data Center LLC") is None and qm.phrase("Solar") is None and qm.phrase("Hut 8") == ("hut", "8")
    t = qm.make_target("site", "childress", "Childress (Microsoft)", "TX", "Childress County",
                       [("Childress (Microsoft)", "site name"), ("IREN", "site developer")], place="Childress, Texas")
    assert t is not None and [w for w, _ in t.phrases] == [("microsoft",), ("iren",)]  # the town is not a name
    assert qm.states_of("TN/MS") == {"TN", "MS"} and qm.states_of("VIC/TAS (Australia)") == frozenset()
    assert qm.make_target("site", "x", "X Campus", "multi", "multiple", [("X Campus", "site name")]) is None


def test_match_needs_state_county_and_name(conn):
    add_lake_mariner(conn)
    targets = qm.load_targets(conn)
    entries = _entries()
    hits = {qid: qm.match(e.values, targets) for qid, e in entries.items()}
    assert sorted(qid for qid, h in hits.items() if h) == ["1670", "1732"]
    link = hits["1670"]["site"][0]
    assert link["id"] == "lake-mariner"
    assert link["evidence"]["phrase"] == "lake mariner" and link["evidence"]["entry_field"] == "name"
    assert hits["1732"]["site"][0]["evidence"]["phrase"] == "wulf"
    assert not hits["0849"]  # 'Somerset Operating Company': same county, no name of ours -> left unmatched
    other_county = {**entries["1670"].values, "county": "Erie"}
    assert qm.match(other_county, targets) == {}


def _store(conn, stats: RunStats, day: date = REPORT) -> q.QueueStore:
    return q.QueueStore(conn, "nyiso", "NYISO", "collector:nyiso_queue", day, stats)


def test_store_writes_changes_only_links_and_large_loads(conn):
    add_lake_mariner(conn)
    entries = _entries()
    stats = RunStats()
    store = _store(conn, stats)
    for e in entries.values():
        store.write(e, source_url="https://www.nyiso.com/x.xlsx", document_id=None)
    store.finish(source_url="https://www.nyiso.com/x.xlsx", document_id=None)
    first = stats.facts_new
    assert first > 0 and store.counts["entries_new"] == len(entries) and store.counts.get("events_new", 0) == 0

    cur = {(r["subject_id"], r["field"]): r for r in conn.execute(
        "SELECT * FROM fact_current WHERE subject_type = 'queue_project' AND subject_id LIKE 'nyiso:%'").fetchall()}
    assert cur[("nyiso:1670", "fuel")]["value"] == "LOAD" and cur[("nyiso:1670", "mw_requested")]["value"] == 250
    assert cur[("nyiso:1670", "status")]["value"] == "SRIS/SIS Approved"
    assert cur[("nyiso:1670", "status")]["valid_from"] == date(2026, 7, 31)  # the row's own last-updated date
    assert (cur[("nyiso:1670", "status")]["tag"], cur[("nyiso:1670", "status")]["source_name"]) == ("F", "NYISO")
    linked = cur[("nyiso:1670", "linked_site")]
    assert linked["tag"] == "M" and linked["method_version"] == qm.METHOD_VERSION
    assert [x["id"] for x in linked["value"]] == ["lake-mariner"]
    assert ("nyiso:0849", "linked_site") not in cur
    loads = conn.execute("SELECT value FROM fact_current WHERE subject_type = 'site' AND subject_id = 'lake-mariner' "
                         "AND field = 'large_load_queue_nyiso'").fetchone()["value"]
    assert [(r["queue_id"], r["mw_requested"], r["status"]) for r in loads] == [
        ("1670", 250, "SRIS/SIS Approved"), ("1732", 250, "SRIS/SIS Approved")]
    assert conn.execute("SELECT count(*) AS n FROM queue_project WHERE iso = 'nyiso'").fetchone()["n"] == len(entries)

    again = RunStats()
    store = _store(conn, again)
    for e in entries.values():
        store.write(e, source_url="https://www.nyiso.com/x.xlsx", document_id=None)
    store.finish(source_url="https://www.nyiso.com/x.xlsx", document_id=None)
    assert again.facts_new == 0 and again.facts_seen == stats.facts_seen and store.counts["entries_new"] == 0


def test_status_and_cod_changes_become_events_on_the_linked_site(conn):
    add_lake_mariner(conn)
    entries = _entries()
    before = dataclasses.replace(entries["1732"], values={**entries["1732"].values, "status": "SRIS/SIS in Progress",
                                                          "proposed_cod": "2027-06"}, updated=date(2026, 8, 31))
    store = _store(conn, RunStats(), date(2026, 8, 31))
    store.write(before, source_url="https://www.nyiso.com/old.xlsx", document_id=None)
    store.finish(source_url="https://www.nyiso.com/old.xlsx", document_id=None)

    store = _store(conn, RunStats())
    for qid in ("1732", "1670"):
        store.write(entries[qid], source_url="https://www.nyiso.com/new.xlsx", document_id=None)
    store.finish(source_url="https://www.nyiso.com/new.xlsx", document_id=None)
    events = conn.execute(
        "SELECT * FROM event WHERE subject_type = 'site' AND subject_id = 'lake-mariner' ORDER BY title").fetchall()
    titles = [e["title"] for e in events]
    assert titles == [
        "NYISO queue 1670 (Lake Mariner Data II) filed: 250 MW LOAD, status SRIS/SIS Approved",
        "NYISO queue 1732 (Wulf Compute Data Center II): proposed cod 2027-06 → 2027-12",
        "NYISO queue 1732 (Wulf Compute Data Center II): status SRIS/SIS in Progress → SRIS/SIS Approved",
    ]
    assert {(e["kind"], e["tag"], e["status"], e["source_name"]) for e in events} == {
        ("interconnection", "F", "published", "NYISO")}
    status = conn.execute("SELECT value, valid_from FROM fact_current WHERE subject_id = 'nyiso:1732' "
                          "AND field = 'status'").fetchone()
    assert status["value"] == "SRIS/SIS Approved" and status["valid_from"] == date(2026, 8, 31)  # never sorts below


def test_a_link_that_stops_matching_is_cleared(conn):
    add_lake_mariner(conn)
    e = _entries()["1670"]
    store = _store(conn, RunStats())
    store.write(e, source_url="https://www.nyiso.com/x.xlsx", document_id=None)
    store.finish(source_url="https://www.nyiso.com/x.xlsx", document_id=None)
    conn.execute("UPDATE site SET county = 'Orleans' WHERE slug = 'lake-mariner'")
    store = _store(conn, RunStats())
    store.write(e, source_url="https://www.nyiso.com/x.xlsx", document_id=None)
    store.finish(source_url="https://www.nyiso.com/x.xlsx", document_id=None)
    cur = conn.execute("SELECT value FROM fact_current WHERE subject_id = 'nyiso:1670' AND field = 'linked_site'"
                       ).fetchone()
    assert cur["value"] == []
    loads = conn.execute("SELECT value FROM fact_current WHERE subject_id = 'lake-mariner' "
                         "AND field = 'large_load_queue_nyiso'").fetchone()
    assert loads["value"] == []


def test_links_mark_the_linked_subjects_pages_dirty(conn):
    from marden.readmodels import deps

    add_lake_mariner(conn)
    store = _store(conn, RunStats())
    store.write(_entries()["1670"], source_url="https://www.nyiso.com/x.xlsx", document_id=None)
    store.finish(source_url="https://www.nyiso.com/x.xlsx", document_id=None)
    assert deps.targets("queue_project", "nyiso:1670", "linked_site", conn=conn) == {
        ("site_detail", "lake-mariner"), ("site_tab:power", "lake-mariner")}
    # no page per queue entry; the Pipeline lists queue load requests
    assert deps.targets("queue_project", "nyiso:1670", "status", conn=conn) == {("pipeline", "all")}
    dirty = {(r["view"], r["key"]) for r in conn.execute("SELECT view, key FROM read_model_dirty").fetchall()}
    assert {("site_detail", "lake-mariner"), ("site_tab:power", "lake-mariner")} <= dirty
