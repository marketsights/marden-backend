"""Watchdog: every check computed by code on real-shaped rows; issues to the review queue, idempotent; titles."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from ops_rows import fake_llm

import marden.models.status  # noqa: F401  (registers the status model's inputs)
from marden.agents.ops import watch_checks as wc
from marden.agents.ops import watch_staleness as ws
from marden.agents.ops import watchdog
from marden.agents.ops.watch_moves import unexplained_moves
from marden.agents.ops.watch_titles import check_title
from marden.agents.review import decide
from marden.core import db
from marden.core.facts import FactWriter
from marden.core.jsonutil import jsonb

TODAY = date(2026, 10, 21)
NOW = datetime(2026, 10, 22, 2, 0, tzinfo=UTC)


def fact(st: str, sid: str, field: str, value, *, tag: str = "F", source: str = "SEC EDGAR", day: str = "2026-10-17",
         producer: str = "seed:sites") -> int:
    with db.transaction() as conn:
        return FactWriter(conn).add(st, sid, field, value, tag=tag, source_name=source, valid_from=day,
                                    producer=producer, status="published").id


def seed_debt() -> None:
    """Abilene and CoreWeave DDTL 3.0 as in the seed: tranche lists and debt facilities."""
    with db.transaction() as conn:
        conn.execute("""INSERT INTO site (slug, name, state) VALUES ('abilene', 'Stargate Abilene', 'TX'),
                        ('coreweave-ddtl-3', 'CoreWeave DDTL 3.0 (OpenAI servers)', 'TX')""")
    fact("site", "abilene", "tranches", [
        {"name": "$2.3bn Phase 1 construction loan (206 MW, two buildings)", "instrument_id": "EX-ABILENE-000",
         "amount_usd": 2300000000},
        {"name": "$7.1bn Phase 2 construction loan (six buildings)", "instrument_id": "EX-ABILENE-001",
         "amount_usd": 7100000000},
        {"name": "About $5bn sponsor cash equity", "amount_usd": 5000000000}],
         tag="R", source="Marden research archive")
    fact("site", "abilene", "debt_facility", {"fid": "ABILENE_P2_FLASH", "size_usd": 7100000000}, tag="R",
         source="Nasdaq")
    fact("site", "coreweave-ddtl-3", "tranches",
         [{"name": "DDTL 3.0", "instrument_id": "EX-CRWV-094", "amount_usd": 2215000000}],
         tag="R", source="Marden research archive")
    fact("site", "coreweave-ddtl-3", "debt_facility", {"size_usd": 2600000000, "drawn_usd": 2215000000})


def test_tranches_must_add_to_size_or_drawn():
    seed_debt()
    with db.transaction() as conn:
        found = wc.tranche_sums(conn)
    assert [i.key for i in found] == ["site:abilene:tranches_vs_facility"]
    assert found[0].title == "abilene: debt tranches add to $9.40B, not the facility size $7.10B"
    assert found[0].evidence["tranche_total_usd"] == 9400000000


def test_payer_total_against_its_sites():
    with db.transaction() as conn:
        for key, total, parts in (("oracle", 81.556, [40.0, 41.556]), ("meta", 42.678, [27.294, 12.547])):
            doc = {"kpis": {"siteDebtB": {"value": total, "tag": "M"}},
                   "sites": [{"slug": f"s{i}", "debtB": p} for i, p in enumerate(parts)]}
            conn.execute("INSERT INTO read_model (view, key, as_of, doc) VALUES ('payer', %s, %s, %s)",
                         (key, TODAY, jsonb(doc)))
        found = wc.payer_sums(conn)
    assert [i.key for i in found] == ["payer:meta:site_debt"]
    assert "its sites add to $39.841B" in found[0].title


def test_reported_value_against_a_filed_one():
    with db.transaction() as conn:
        conn.execute("INSERT INTO campus (id, name) VALUES ('iren-childress', 'IREN Childress')")
    fact("campus", "iren-childress", "utility", "AEP connection agreement (750 MW campus)")
    fact("campus", "iren-childress", "utility", "ERCOT direct transmission connection (IREN-owned substations)",
         tag="R", source="Mgrid", day="2026-10-19", producer="seed:facilities")
    fact("campus", "iren-childress", "it_mw", 650.0)
    fact("campus", "iren-childress", "it_mw", 652.0, tag="R", source="Mgrid", day="2026-10-19",
         producer="seed:facilities")  # within 1%: rounding
    with db.transaction() as conn:
        found = wc.reported_vs_filed(conn)
    assert [i.key for i in found] == ["campus:iren-childress:utility"]
    assert found[0].evidence["filed"]["value"] == "AEP connection agreement (750 MW campus)"


def test_screens_that_disagree():
    with db.transaction() as conn:
        conn.execute("INSERT INTO read_model (view, key, as_of, doc) VALUES ('sites', 'all', %s, %s)",
                     (TODAY, jsonb([{"slug": "jupiter", "status": "Broken", "debtB": {"value": 18}},
                                    {"slug": "abilene", "status": "Strained"}])))
        for slug, status in (("jupiter", "Strained"), ("abilene", "Strained")):
            conn.execute("INSERT INTO read_model (view, key, as_of, doc) VALUES ('site_detail', %s, %s, %s)",
                         (slug, TODAY, jsonb({"status": status, "debtB": {"value": 18}})))
        found = wc.screens(conn)
    assert [i.key for i in found] == ["site:jupiter:sites_vs_site_detail"]
    assert found[0].evidence["differences"] == {"status": {"sites": "Broken", "site_detail": "Strained"}}


def test_stale_sources_by_schedule_and_cadence():
    with db.transaction() as conn:
        conn.execute("""INSERT INTO source (id, name, kind, cadence_minutes, terms_note, last_ok_at, created_at)
                        VALUES ('ferc', 'FERC eLibrary', 'portal', 60, 't', %s, %s),
                               ('sec_index', 'SEC', 'api', 10, 't', %s, %s),
                               ('fred_ice_bofa', 'FRED', 'api', 1440, 't', NULL, %s)""",
                     (NOW - timedelta(hours=5), NOW - timedelta(days=9), NOW - timedelta(minutes=50),
                      NOW - timedelta(days=9), NOW - timedelta(days=5)))
        conn.execute("""INSERT INTO source (id, name, kind, cadence_minutes, terms_note, blocked, blocked_reason)
                        VALUES ('miso_queue', 'MISO', 'portal', 10080, 't', true, 'Cloudflare')""")
        crons = {"ferc": ["23 * * * *"], "sec_index": ["*/10 10-23 * * 1-5", "*/10 0-4 * * 2-6"]}
        found = {i.key: i for i in ws.stale_sources(conn, NOW, crons)}
    # ferc missed its last two hourly runs; sec_index (Thursday 02:00 UTC: its evening schedule) ran 50 min ago;
    # fred never ran well in five days; a blocked source is known, not an issue
    assert set(found) == {"source:ferc", "source:fred_ice_bofa"}
    assert found["source:ferc"].evidence["rule"] == "missed its last two scheduled runs"
    assert found["source:fred_ice_bofa"].title == "Source fred_ice_bofa has never run successfully"


def test_backlogs_and_missed_inputs():
    with db.transaction() as conn:
        conn.execute("INSERT INTO read_model_dirty (view, key, marked_at) VALUES ('instrument_detail', 'EX-1', %s),"
                     " ('site_detail', 'fresh', %s)", (NOW - timedelta(minutes=20), NOW - timedelta(minutes=2)))
        conn.execute("INSERT INTO model_dirty (model, key, marked_at) VALUES ('mark_gap', 'EX-1', %s)",
                     (NOW - timedelta(hours=8),))
        conn.execute("INSERT INTO site (slug, name) VALUES ('jupiter', 'Project Jupiter')")
        conn.execute("""INSERT INTO read_model (view, key, as_of, doc, built_at) VALUES
                        ('site_detail', 'jupiter', %s, '{}', %s)""", (TODAY, NOW - timedelta(days=1)))
    fact("site", "jupiter", "outside_date", "2028", tag="R", source="Cp24", day="2026-09-24")
    with db.transaction() as conn:
        conn.execute("DELETE FROM read_model_dirty WHERE key = 'jupiter'")  # the mark the writer made is lost
        backlog = {i.key: i for i in ws.dirty_backlog(conn, NOW)}
        missed = ws.missed_inputs(conn)
    assert set(backlog) == {"view:instrument_detail:backlog", "model:mark_gap:backlog"}
    assert backlog["model:mark_gap:backlog"].check == "stale_model"
    assert [i.key for i in missed] == ["site_detail:jupiter:missed_input"]


def _status(slug: str, day: str, status: str, created: datetime) -> None:
    with db.transaction() as conn:
        conn.execute("""INSERT INTO model_output (model, version, subject_type, subject_id, as_of, output, created_at)
                        VALUES ('status', '0.4', 'site', %s, %s, %s, %s)""",
                     (slug, day, jsonb({"status": status, "chain": []}), created))


def test_unexplained_move_and_its_cause():
    with db.transaction() as conn:
        conn.execute("INSERT INTO site (slug, name) VALUES ('the-barn', 'Saline'), ('helios-2', 'Helios Phase II')")
    for slug in ("the-barn", "helios-2"):
        _status(slug, "2026-10-20", "Strained", NOW - timedelta(hours=26))
        _status(slug, "2026-10-21", "Broken", NOW - timedelta(hours=2))
    fact("site", "helios-2", "risk_evidence", "Construction halted at Helios Phase II", tag="R", source="Press",
         day="2026-10-21")
    with db.transaction() as conn:
        found, report = unexplained_moves(conn, TODAY)
    assert [i.key for i in found] == ["status:the-barn:2026-10-21"]
    assert found[0].title == ("the-barn: chain status moved from Strained to Broken (Oct 20 to Oct 21) with no "
                              "input change")
    assert report["status"] == {"compared": 2, "moved": 2, "unexplained": 1}


def test_title_validator():
    code = "abilene: debt tranches add to $9.40B, not the facility size $7.10B"
    assert check_title("Abilene debt tranches total $9.40B against a $7.10B facility size", code, "sums") == []
    assert check_title("Abilene tranches total $9.40B against $7.1B", code, "sums") == []  # the same number
    assert check_title("Abilene tranches total $9.40B against $7.2B", code, "sums")  # a number changed
    assert check_title("Abilene tranches total $9.40B", code, "sums")  # a number dropped
    assert check_title("Abilene: Oracle tranches add to $9.40B vs $7.10B", code, "sums")  # a name added
    assert check_title("Abilene debt jumped to $9.40B, not $7.10B", code, "sums")  # a claim added


def test_run_is_idempotent_and_follows_decisions():
    seed_debt()

    def answer(req):
        items = json.loads(req.content[0].split("\n", 1)[1].rsplit("\n", 1)[0])
        out = []
        for it in items:
            if it["id"].startswith("sums|site:abilene"):
                out.append({"id": it["id"],
                            "title": "Abilene debt tranches total $9.40B against a $7.10B facility size"})
            else:
                out.append({"id": it["id"], "title": it["title"] + " (about 3 sites)"})  # adds a number: refused
        return {"titles": out}

    llm, backend = fake_llm(answer)
    first = watchdog.run(today=TODAY, now=NOW, llm=llm)
    assert first["issues"]["new"] >= 1 and first["checks"]["sums"]["found"] == 1
    assert first["model_status"] == "cli" and first["titles"]["accepted"] >= 1
    with db.transaction() as conn:
        item = conn.execute("SELECT * FROM review_item WHERE ref_type = 'watchdog:sums'").fetchone()
    assert item["title"] == "Abilene debt tranches total $9.40B against a $7.10B facility size"
    assert item["payload"]["code_title"].startswith("abilene: debt tranches add to $9.40B")
    assert item["payload"]["evidence"]["tranche_total_usd"] == 9400000000
    assert item["ref_id"].startswith("site:abilene:tranches_vs_facility#")

    second = watchdog.run(today=TODAY, now=NOW, llm=llm)
    assert second["issues"].get("new", 0) == 0 and second["issues"]["still_open"] == first["issues"]["new"]
    assert len(backend.requests) == 1  # nothing new to word on the second run

    out = decide(item["id"], "reject", "staff@marden.ai", reason="phase 1 is a separate facility")
    assert out["ok"] and out["issue"] == "dismissed"
    third = watchdog.run(today=TODAY, now=NOW, use_model=False)
    assert third["issues"]["already_decided"] == 1 and third["model_status"] == "not_used"

    fact("site", "abilene", "debt_facility", {"fid": "ABILENE", "size_usd": 9000000000}, tag="R", source="Nasdaq",
         day="2026-10-20")
    fourth = watchdog.run(today=TODAY, now=NOW, use_model=False)
    with db.transaction() as conn:
        rows = conn.execute("SELECT status, title FROM review_item WHERE ref_type = 'watchdog:sums' "
                            "ORDER BY id").fetchall()
    assert fourth["issues"]["new"] >= 1 and [r["status"] for r in rows] == ["rejected", "open"]
    assert "$9.00B" in rows[1]["title"]

    fact("site", "abilene", "debt_facility", {"fid": "ABILENE", "size_usd": 9400000000}, tag="R", source="Nasdaq",
         day="2026-10-21")
    fifth = watchdog.run(today=TODAY, now=NOW, use_model=False)
    with db.transaction() as conn:
        last = conn.execute("SELECT status, decided_by, payload FROM review_item WHERE ref_type = 'watchdog:sums' "
                            "ORDER BY id DESC LIMIT 1").fetchone()
        run_rows = conn.execute("SELECT status FROM agent_ops_run WHERE role = 'watchdog'").fetchall()
    assert fifth["issues"]["resolved"] >= 1
    assert last["status"] == "approved" and last["decided_by"] == "agent:watchdog" and last["payload"]["resolved"]
    assert {r["status"] for r in run_rows} == {"ok"}


@pytest.mark.parametrize("action", ["approve", "edit"])
def test_issue_decisions(action):
    seed_debt()
    watchdog.run(today=TODAY, now=NOW, use_model=False)
    with db.transaction() as conn:
        item = conn.execute("SELECT id FROM review_item WHERE ref_type = 'watchdog:sums'").fetchone()
    out = decide(item["id"], action, "staff@marden.ai", edit={"value": 1} if action == "edit" else None)
    if action == "approve":
        assert out["ok"] and out["issue"] == "resolved"
    else:
        assert not out["ok"] and "not edited" in out["error"]


def test_evidence_back_to_a_decided_one_closes_the_open_item():
    a = wc.Issue("sums", "site:x:tranches_vs_facility", "x: tranches add to $1.00B, not $2.00B", {"v": 1})
    b = wc.Issue("sums", "site:x:tranches_vs_facility", "x: tranches add to $1.00B, not $3.00B", {"v": 2})
    with db.transaction() as conn:
        assert watchdog.store(conn, a, a.title, "code", 1)[0] == "new"
        conn.execute("UPDATE review_item SET status = 'rejected' WHERE kind = 'issue'")
        assert watchdog.store(conn, b, b.title, "code", 2)[0] == "new"
        assert watchdog.store(conn, a, a.title, "code", 3) == ("already_decided", None)
        rows = conn.execute("SELECT status, decided_by FROM review_item ORDER BY id").fetchall()
    assert [(r["status"], r["decided_by"]) for r in rows] == [("rejected", None), ("approved", "agent:watchdog")]
