"""sites_milestones: the Schedule tab's stated milestone dates, read from the cushion model's inputs."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

from marden.models.cushion_inputs import SiteInputs
from marden.readmodels.views import sites_milestones as sm


def _row(field: str, text: str, day: date, tag: str = "R", source: str = "Press") -> dict:
    return {"id": hash((field, text)) % 10_000, "field": field, "value": text, "tag": tag, "source_name": source,
            "source_url": f"https://example.org/{field}", "valid_from": day,
            "learned_at": datetime(2026, 10, 21, tzinfo=UTC)}


def _site(**facts: list[dict]) -> SiteInputs:
    return SiteInputs("s", "Site", date(2026, 10, 21), {k: v for k, v in facts.items()})


def test_original_first_hall_target_is_the_earliest_stated_plan_with_its_row():
    orig = _row("original_energization", "Initial operations Q4 2026 (County)", date(2026, 10, 17), "F", "NMED")
    later = _row("rfs_statement", "first hall delivery Q3 2028", date(2026, 9, 24))
    found = sm._first_hall_original(_site(original_energization=[orig], rfs_statement=[later]))
    assert found is not None
    when, row = found
    assert when.text == "Q4 2026" and row is orig
    assert sm._when(when, row) == {
        "from": "2026-10", "to": "2026-12", "text": "Q4 2026", "tag": "F", "sourceName": "NMED",
        "asOf": "2026-10-17", "sourceUrl": "https://example.org/original_energization",
    }


def test_without_the_dossier_field_the_earliest_dated_statement_is_the_original():
    early = _row("rfs_statement", "first hall delivery Q4 2026", date(2026, 3, 1))
    later = _row("rfs_statement", "first hall delivery Q3 2028", date(2026, 9, 24))
    found = sm._first_hall_original(_site(rfs_statement=[later, early]))
    assert found is not None and found[0].text == "Q4 2026" and found[1] is early


def test_no_stated_plan_gives_no_original_target():
    assert sm._first_hall_original(_site()) is None


def test_rent_path_follows_the_predecessors():
    ms = {k: SimpleNamespace(preds=p) for k, p in {
        "land_zoning": [], "shells": ["land_zoning"], "air_permit": [], "onsite_generation": ["air_permit"],
        "first_hall": ["shells", "onsite_generation"], "rent_start": ["first_hall"], "utility_agreement": []}.items()}
    assert sm._ancestors(ms, "rent_start") == {"first_hall", "shells", "land_zoning", "onsite_generation", "air_permit"}


def test_without_a_cushion_run_the_milestones_are_returned_as_stored():
    stored = [{"key": "rent_start", "label": "Rent start", "status": "Projected", "events": []}]
    assert sm.with_dates(None, "s", None, stored) == (stored, None)  # type: ignore[arg-type]
