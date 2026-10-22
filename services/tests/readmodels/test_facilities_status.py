"""One status per campus and building, decided from the strongest, newest record (facilities_status).

Pure tests of `resolve` on fact-shaped rows: the cases owners flagged (Cipher Barber Lake read
"Under construction" next to "1 of 1 bldgs already operating"; Epoch AI's July count against its October
footprint map; a count that says how many but not which).
"""

from __future__ import annotations

from datetime import date
from itertools import count

from marden.readmodels.context import building
from marden.readmodels.views import facilities_status as fs

_ids = count(1)
EPOCH = "Epoch AI"
TDLR = "Texas Department of Licensing and Regulation (TABS)"
DAY = date(2026, 10, 22)


def row(field: str, value, *, tag: str, source: str, day: str, url: str | None = None) -> dict:
    return {"id": next(_ids), "field": field, "value": value, "tag": tag, "source_name": source,
            "source_url": url, "valid_from": date.fromisoformat(day), "method_version": None}


def footprint(bid: str, complete: bool, day: str = "2026-10-08") -> list[fs.Claim]:
    status = "Operating" if complete else "Under construction"
    return fs.building_claims(bid, {"status": [row("status", status, tag="M", source=EPOCH, day=day)]})


def registration(bid: str, *, status: str, completion: str, read: str = "2026-10-22") -> list[fs.Claim]:
    v = {"agency": "TDLR TABS", "number": f"TABS-{bid}", "status": status, "completion": completion}
    return fs.building_claims(bid, {"state_registration": [
        row("state_registration", v, tag="F", source=TDLR, day=read,
            url=f"https://www.tdlr.texas.gov/TABS/Search/Project/TABS-{bid}")]})


def campus(count_rows=(), status=None) -> tuple[list[fs.Claim], dict | None]:
    counts = [row("buildings_operational", n, tag="R", source=EPOCH, day=d) for n, d in count_rows]
    rows = {"buildings_operational": counts}
    stated = row("status", status[0], tag=status[1], source=status[2], day=status[3]) if status else None
    rows["status"] = [stated] if stated else []
    return rows, stated


def decide(labels: dict[str, str], per: dict[str, list[fs.Claim]], counts=(), status=None) -> fs.Resolution:
    rows, stated = campus(counts, status)
    with building(DAY):
        return fs.resolve(labels, per, fs.campus_claims(rows, len(labels)), stated)


def test_a_permit_record_beats_a_newer_tracker_mark_and_the_campus_shows_one_status():
    """Barber Lake: Epoch marks the footprint complete (Oct 8) but the state registration dates completion
    Dec 8, 2026: one status, Under construction, with the imagery mark only in the note."""
    per = {"b1": footprint("b1", True) + registration("b1", status="Review Complete", completion="2026-12-08")}
    res = decide({"b1": "Building 1"}, per, counts=[(0, "2026-07-30")],
                 status=("Under construction", "M", "Marden research archive", "2026-10-19"))
    assert res.operating == 0 and res.total == 1
    assert res.status["value"] == "Under construction"
    assert res.buildings["b1"]["value"] == "Under construction"
    assert res.buildings["b1"]["tag"] == "F"
    assert "Dec 8, 2026" in res.note and "weaker record" in res.note
    assert "imagery" in res.building_notes["b1"]


def test_tracker_alone_all_buildings_operating_reads_operating_even_when_the_archive_status_lags():
    per = {"b1": footprint("b1", True)}
    res = decide({"b1": "Building 1"}, per, counts=[(0, "2026-07-30")],
                 status=("Under construction", "M", "Marden research archive", "2026-10-19"))
    assert (res.operating, res.status["value"], res.buildings["b1"]["value"]) == (1, "Operating", "Operating")
    assert res.note is None  # Epoch's July count of zero, older than its October footprint map, is growth


def test_completed_state_inspections_outrank_a_tracker_count():
    labels = {f"b{i}": f"Building {i}" for i in range(1, 9)}
    per = {f"b{i}": registration(f"b{i}", status="Inspection Complete", completion="2026-05-26") for i in range(1, 7)}
    per |= {f"b{i}": registration(f"b{i}", status="Review Complete", completion="2026-12-21") for i in (7, 8)}
    res = decide(labels, per, counts=[(4, "2026-07-28")])
    assert res.operating == 6
    assert [res.buildings[f"b{i}"]["value"] for i in range(1, 9)] == ["Operating"] * 6 + ["Under construction"] * 2
    assert res.status["value"] == "Operating"
    assert res.note.startswith("6 of 8 buildings finished (state inspections complete, Texas TDLR")
    assert "count (4 operating) is a weaker record" in res.note


def test_a_count_says_how_many_but_not_which():
    labels = {f"b{i}": f"Building {i}" for i in range(1, 6)}
    per = {b: footprint(b, False, "2026-10-13") for b in labels}  # no completion mark: not a record of anything
    res = decide(labels, per, counts=[(4, "2026-05-15")])
    assert res.operating == 4 and res.status["value"] == "Operating"
    assert all(d["value"] is None for d in res.buildings.values())
    assert all("which ones is not public" in res.building_notes[b] for b in labels)
    assert res.note == "4 of 5 buildings operating (Epoch AI, May 15, 2026); which ones is not public."


def test_named_buildings_keep_their_status_and_the_rest_are_not_confirmed():
    labels = {f"b{i}": f"Building {i}" for i in range(1, 9)}
    per = {b: footprint(b, b in ("b1", "b2"), "2026-10-15") for b in labels}
    res = decide(labels, per, counts=[(4, "2026-07-28")])
    assert res.operating == 4
    assert res.buildings["b1"]["value"] == res.buildings["b2"]["value"] == "Operating"
    assert all(res.buildings[f"b{i}"]["value"] is None for i in range(3, 9))
    assert "Buildings 1 and 2 have a completion record; which other 2 operate is not public" in res.note


def test_the_same_sources_older_lower_count_is_growth_not_a_disagreement():
    labels = {f"b{i}": f"Building {i}" for i in range(1, 13)}
    per = {b: footprint(b, int(b[1:]) <= 8, "2026-10-13") for b in labels}
    res = decide(labels, per, counts=[(5, "2026-06-04")])
    assert res.operating == 8
    assert res.note is None  # one line only when it matters


def test_a_newer_lower_footprint_count_does_not_lower_a_count():
    labels = {"b1": "Building 1", "b2": "Building 2", "b3": "Building 3", "b4": "Building 4"}
    per = {b: footprint(b, b != "b4", "2026-10-15") for b in labels}
    res = decide(labels, per, counts=[(4, "2026-06-14")])
    assert res.operating == 4
    assert all(d["value"] == "Operating" for d in res.buildings.values())


def test_paused_and_cancelled_campus_statuses_are_kept():
    res = decide({"b1": "Building 1"}, {"b1": footprint("b1", False)},
                 status=("Paused", "R", "Local news", "2026-09-01"))
    assert res.status["value"] == "Paused"


def test_rank_of_a_record():
    co = row("co_date", "2026-08-12", tag="F", source="Madison County", day="2026-08-12")
    assert fs.rank_of(co, operating=True) == 4
    assert fs.rank_of(row("status", "Operating", tag="F", source="City of Temple building permits", day="2026-08-12"),
                      operating=True) == 3
    assert fs.rank_of(row("status", "Operating", tag="F", source="SEC EDGAR", day="2026-08-12",
                          url="https://www.sec.gov/x"), operating=True) == 2
    assert fs.rank_of(row("buildings_operational", 2, tag="R", source=EPOCH, day="2026-08-12"), operating=True) == 1
    unmarked = row("status", "Under construction", tag="M", source=EPOCH, day="2026-08-12")
    assert fs.rank_of(unmarked, operating=False) == 0
