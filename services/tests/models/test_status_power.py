"""Status v0.4: a withdrawn permit or queue position trips T2's power test only when nothing replaces it.

On Stargate Abilene's real rows (copied from the local database): the on-site plant n-abilene-onsite has
TCEQ air permit 182267 withdrawn (2026-04-20) while the PSD permit 182126 for the plant is at public notice
(2026-08-05 / 08-11) and its other permits are issued, so the tenant link stays T1 (Oracle at BBB-), not T2.
With the plant's other permit records taken out, the same withdrawal trips T2 again. STACK Danville's three
gas units withdrawn from the PJM queue (2026-09-24) have nothing live beside them, so they count.
"""

from __future__ import annotations

from datetime import date

import pytest
from status_rows import copy_status_rows

from marden.core import db
from marden.models import status as status_model
from marden.models import status_inputs as si
from marden.models import status_power as sp
from marden.models import status_rules as sr
from marden.models import status_text as tx

DAY = date(2026, 10, 20)  # a backtest day: the result does not move with the calendar
PLANT = "plant:n-abilene-onsite"
WITHDRAWN = "TCEQ air permit 182267: withdrawn (project 401169)"


@pytest.fixture
def abilene(committed):
    copy_status_rows(["abilene"], plants=["n-stack-danville"])
    rows = committed.execute(
        "SELECT title, event_date FROM event WHERE subject_type = 'plant' AND subject_id = 'n-abilene-onsite' "
        "AND kind = 'permit' AND status = 'published' ORDER BY event_date, id").fetchall()
    if not any(r["title"] == WITHDRAWN for r in rows):
        pytest.skip("Abilene's air-permit events are not in the source database")
    return rows


def _tenant(output: dict) -> dict:
    return next(link for link in output["chain"] if link["role"] == "tenant")


def _site(day: date, slug: str = "abilene") -> si.SiteInputs:
    with db.transaction() as conn:
        return si.load(conn, day).sites[slug]


def test_the_withdrawal_is_its_own_text_rule():
    hit = tx.power_denied(WITHDRAWN)
    assert hit is not None and hit.rule == "power_withdrawn"
    # Jupiter's right-of-way denial (site fact 15517) stays a denial; a denial beside a withdrawal wins.
    assert tx.power_denied("NM State Land Office denied trust-land ROW twice (Apr, Jul 2026)").rule == "power_denied"
    both = f"{WITHDRAWN}; NM State Land Office denied trust-land ROW twice (Apr, Jul 2026)"
    assert tx.power_denied(both).rule == "power_denied"


def test_abilene_withdrawn_permit_is_replaced_by_182126(abilene, committed):
    site = _site(DAY)
    item = next(it for it in site.items if it.text == WITHDRAWN)
    assert item.subject == PLANT and item.day == date(2026, 4, 20)
    by = sp.replacement(site, item)
    psd = [r for r in abilene if r["title"].startswith("TCEQ air permit 182126:")]
    assert by is not None and by.key == "182126" and by.pending
    assert (by.text, by.day) in {(r["title"], r["event_date"]) for r in psd} and by.day == max(r["event_date"]
                                                                                              for r in psd)
    out = status_model.run(DAY, ["abilene"])["outputs"]["abilene"]
    tenant = _tenant(out)
    assert (tenant["status"], tenant["ruleCode"]) == ("amber", "T1") and "BBB-" in tenant["reason"]
    assert "Not T2" in tenant["reason"] and "182267" in tenant["reason"] and "182126" in tenant["reason"]
    assert out["status"] != "Broken"
    stored = committed.execute("SELECT version FROM model_output WHERE model = 'status' AND subject_id = 'abilene'"
                               ).fetchone()
    assert stored["version"] == status_model.VERSION == "0.4"


def test_the_replacement_is_read_point_in_time(abilene):
    """Before 182126's first public notice (2026-08-05) the plant's issued permits were what stood beside the
    withdrawal: the latest of them on 08-04 is the Title V permit O4721 (decision 2026-01-16)."""
    first_notice = min(r["event_date"] for r in abilene if r["title"].startswith("TCEQ air permit 182126:"))
    before = _site(date(2026, 8, 4))
    assert first_notice == date(2026, 8, 5)
    item = next(it for it in before.items if it.text == WITHDRAWN)
    by = sp.replacement(before, item)
    issued = [r for r in abilene if r["title"].endswith(": decision (project 37589)")]
    assert by is not None and not by.pending and by.key == "O4721" and by.day == issued[-1]["event_date"]
    assert not any(r.key == "182126" for r in sp.records(before))  # not public yet


def test_a_withdrawal_with_nothing_else_live_still_trips_t2(abilene, committed):
    """The same real withdrawal with the plant's other permit records (events and the site's air_permits
    fact) taken out: nothing replaces it, so Oracle at BBB- with power withdrawn is T2 again."""
    committed.execute("DELETE FROM event WHERE subject_type = 'plant' AND subject_id = 'n-abilene-onsite' "
                      "AND kind = 'permit' AND title <> %s", (WITHDRAWN,))
    committed.execute("DELETE FROM fact WHERE field = 'air_permits'")
    site = _site(DAY)
    assert sp.replacement(site, next(it for it in site.items if it.text == WITHDRAWN)) is None
    out = status_model.run(DAY, ["abilene"])["outputs"]["abilene"]
    tenant = _tenant(out)
    assert (tenant["status"], tenant["ruleCode"]) == ("red", "T2") and out["status"] == "Broken"
    assert tenant["fact"]["value"] == WITHDRAWN and tenant["fact"]["asOf"] == "2026-04-20"


def test_queue_withdrawals_with_nothing_live_count(abilene, committed):
    """STACK Danville's three gas units withdrawn from PJM's queue on 2026-09-24: each is its own queue record,
    ended; none replaces another. (The plant is not linked to a financed site; it is read here as if it
    served Abilene's campus, to run the rule on its real rows.)"""
    titles = [r["title"] for r in committed.execute(
        "SELECT title FROM event WHERE subject_type = 'plant' AND subject_id = 'n-stack-danville' AND kind = 'queue'"
    ).fetchall()]
    if len(titles) != 3:
        pytest.skip("STACK Danville's queue events are not in the source database")
    with db.transaction() as conn:
        items = si.event_items(conn, DAY, "plant", ["n-stack-danville"])
    site = si.SiteInputs("abilene", "Stargate Abilene", ["Abilene"], {}, [], items,
                         links={"stargate-abilene": ["n-stack-danville"]})
    records = sp.records(site)
    assert len(records) == 3 and not any(r.live for r in records) and {r.kind for r in records} == {"queue"}
    hits = sr.power_failures(site)
    assert {it.text for it, _ in hits} == set(titles) and {h.rule for _, h in hits} == {"power_withdrawn"}


def test_records_read_each_real_title_form():
    """One record per permit number or queue id, in the state of its latest action, for every title form the
    collectors write (TCEQ, NMED, Iowa DNR permits; ERCOT interconnection changes; PJM queue rows)."""
    rows = {r["title"]: r for r in db_rows()}
    if not rows:
        pytest.skip("no permit or queue events in the source database")
    cases = {
        "TCEQ air permit 182126: public notice (project 400428)": ("permit", "182126", True, True),
        "TCEQ air permit O4721: decision (project 37589)": ("permit", "O4721", True, False),
        "NMED air permit NSR 10883: preliminary decision": ("permit", "10883", True, True),
        "Iowa DNR air permit 26-A-117, 26-A-118, 26-A-119, 26-A-120, 26-A-121: decision (project 25-350)":
            ("permit", "26A117", True, False),
        "ERCOT queue 23INR0637 (Goodnight Wind II): proposed cod 2027-01-09 → 2027-06-14":
            ("queue", "23INR0637", True, True),
        WITHDRAWN: ("permit", "182267", False, False),
    }
    for title, want in cases.items():
        if title not in rows:
            continue
        r = rows[title]
        item = si.Item(title, r["event_date"], r["tag"], r["source_name"], None, f"event:{r['id']}",
                       f"{r['subject_type']}:{r['subject_id']}", f"event:{r['kind']}")
        rec = sp.event_record(item)
        assert rec is not None and (rec.kind, rec.key, rec.live, rec.pending) == want, title


def db_rows() -> list[dict]:
    """The permit, queue and interconnection events of the local database (read-only)."""
    import psycopg
    from conftest import BASE_URL
    from psycopg.rows import dict_row

    try:
        with psycopg.connect(BASE_URL, connect_timeout=5, row_factory=dict_row) as src:
            return src.execute("SELECT id, subject_type, subject_id, event_date, kind, title, tag, source_name "
                               "FROM event WHERE kind IN ('permit', 'queue', 'interconnection') "
                               "AND status = 'published'").fetchall()
    except psycopg.OperationalError:
        return []
