"""grid_registry, eia930_ref and eia861_ba: parsers on real EIA files, grid_region rows and reruns, schedules,
live runs."""

from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path

import httpx
import pytest

from marden import cli
from marden.collectors import eia861_ba, eia930_ref, grid_registry
from marden.collectors.grid_store import counts, get
from marden.collectors.rates_common import ParseError, RunStats
from marden.core import db, documents, sources
from marden.core.http import Fetcher
from marden.core.queue import app

DATA = Path(__file__).parent / "data"
EIA930_XLSX = (DATA / "eia930_ref" / "EIA930_Reference_Tables.xlsx").read_bytes()
EIA861_XLSX = (DATA / "eia861_ba" / "Balancing_Authority_2025.xlsx").read_bytes()


def _registry() -> dict[str, int]:
    with db.transaction() as conn:
        return grid_registry.ensure_registry(conn)


def _stored_930() -> documents.DocumentRow:
    sources.upsert_spec(eia930_ref.SOURCE)
    return documents.record_document(eia930_ref.SOURCE_ID, eia930_ref.URL, EIA930_XLSX,
                                     content_type="application/vnd.ms-excel", doc_type="eia930_reference_xlsx")


def _zip_of(xlsx: bytes, year: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(eia861_ba.member_name(year), xlsx)
    return buf.getvalue()


def test_registry_covers_us_isos_canada_and_entsoe_and_reruns_cleanly(committed):
    first = _registry()
    assert first["inserted"] == len(grid_registry.regions()) and first["changed"] == 0
    assert _registry() == {"inserted": 0, "changed": 0, "unchanged": len(grid_registry.regions())}

    rows = {r["id"]: r for r in committed.execute("SELECT * FROM grid_region").fetchall()}
    isos = {i for i, r in rows.items() if r["kind"] == "iso_rto" and r["country"] == "US"}
    assert isos == {"iso:ERCOT", "iso:PJM", "iso:MISO", "iso:SPP", "iso:CAISO", "iso:NYISO", "iso:ISO-NE"}
    assert rows["iso:ERCOT"]["parent_id"] == grid_registry.ERCOT
    assert rows["iso:CAISO"]["parent_id"] == grid_registry.WESTERN
    assert rows["iso:SPP"]["parent_id"] is None  # RTO West (SWPW) is Western, the rest Eastern

    canada = {i for i, r in rows.items() if r["country"] == "CA" and r["kind"] != "interconnection"}
    assert canada == {"iso:AESO", "iso:IESO", "ba:HQT", "ba:BCHA", "ba:SPC", "ba:MHEB", "ba:NBSO",
                      "ba:nova-scotia-power"}
    assert rows["ba:HQT"]["parent_id"] == grid_registry.QUEBEC

    tsos = [r for r in rows.values() if r["kind"] == "tso"]
    assert len(tsos) == 42  # 40 ENTSO-E members, observer Moldelectrica, NESO
    labels = {r["meta"]["label"] for r in tsos}
    assert {"NESO", "EirGrid", "TenneT NL", "TenneT DE", "Amprion", "50Hertz", "TransnetBW", "RTE", "Elia",
            "Energinet", "Statnett", "Svenska kraftnät", "Fingrid", "REE", "Terna", "PSE", "Swissgrid"} <= labels
    assert rows["tso:statnett"]["parent_id"] == grid_registry.NORDIC and rows["tso:energinet"]["parent_id"] is None
    registered = [r for r in rows.values() if r["kind"] == "tso" or r["id"] in canada]
    assert all(r["meta"]["coverage"] == "registered, no collector (V2)" and r["geom"] is None for r in registered)


def test_parses_the_real_eia930_ba_table():
    bas = {b["code"]: b for b in eia930_ref.parse(EIA930_XLSX)}
    assert len(bas) == 81
    assert bas["ERCO"]["region_code"] == "TEX" and bas["ERCO"]["active"] and bas["ERCO"]["us_ba"]
    assert bas["WACM"]["active"] is False and bas["WACM"]["retirement_date"] == "2026-04-01"
    assert bas["SWPW"]["active"] and bas["SWPW"]["activation_date"] == "2026-04-01"
    assert {c for c, b in bas.items() if b["region_code"] == "CAN"} == {
        "AESO", "BCHA", "HQT", "IESO", "MHEB", "NBSO", "SPC"}
    assert eia930_ref.interconnection_for("ERCO", "TEX") == grid_registry.ERCOT
    assert eia930_ref.interconnection_for("CISO", "CAL") == grid_registry.WESTERN
    assert eia930_ref.interconnection_for("SWPW", "NW") == grid_registry.WESTERN
    assert eia930_ref.interconnection_for("PJM", "MIDA") == grid_registry.EASTERN
    assert eia930_ref.interconnection_for("HQT", "CAN") == grid_registry.QUEBEC
    assert eia930_ref.interconnection_for("CEN", "MEX") is None
    assert eia930_ref.interconnection_for("WAUE", bas["WAUE"]["region_code"]) == grid_registry.EASTERN
    with pytest.raises(ParseError, match="not a readable workbook"):
        eia930_ref.parse(b"BA Code,BA Name\n")


def test_eia930_rows_written_once_and_registry_names_kept(committed):
    _registry()
    doc = _stored_930()
    first = eia930_ref.load(doc, RunStats())
    assert first["inserted"] == 81 - 5 and first["changed"] == 5  # the registry's five Canadian BAs gain EIA data
    assert eia930_ref.load(doc, RunStats()) == {"inserted": 0, "changed": 0, "unchanged": 81}
    hqt = get(committed, "ba:HQT")
    assert hqt["name"] == "Hydro-Québec TransÉnergie" and hqt["meta"]["eia930"]["name"] == "Hydro-Quebec TransEnergie"
    erco = get(committed, "ba:ERCO")
    assert erco["parent_id"] == grid_registry.ERCOT and erco["country"] == "US"
    assert erco["meta"]["eia930"]["document_id"] == doc.id and erco["meta"]["eia930"]["source_url"] == eia930_ref.URL
    with db.transaction() as conn:
        assert counts(conn)["balancing_authority"]["rows"] == 81 + 1  # + Nova Scotia Power (registry only)


def test_parses_the_real_eia861_ba_table():
    table = eia861_ba.parse_xlsx(EIA861_XLSX)
    assert table["MISO"]["ba_ids"] == [56669] and "LA" in table["MISO"]["states"]
    assert table["BANC"]["ba_ids"] == [65289]  # HIFLD's Control Areas carry 16534 for BANC: matched by name
    assert table["SCEG"]["names"] == ["Dominion Energy South Carolina"]
    assert {"AMPL", "CEA", "HECO"} <= set(table)  # Alaska and Hawaii BAs that EIA-930 does not report
    with pytest.raises(ParseError, match=r"has no Balancing_Authority_2025\.xlsx"):
        eia861_ba.parse_zip(_zip_of(EIA861_XLSX, 2024), 2025)


def test_eia861_falls_back_a_year_and_adds_ids_without_renaming(committed):
    _registry()
    eia930_ref.load(_stored_930(), RunStats())
    seen: list[str] = []
    archive = _zip_of(EIA861_XLSX, 2025)  # one download: the same bytes on every request

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith("f8612026.zip"):
            return httpx.Response(404)
        return httpx.Response(200, content=archive)

    fetcher = Fetcher(client=httpx.Client(transport=httpx.MockTransport(handler)), max_retries=0)
    out = eia861_ba.run(fetcher=fetcher, today=date(2027, 3, 1))
    assert out["ok"], out
    assert seen == [eia861_ba.zip_url(2026), eia861_ba.zip_url(2025)] and out["year"] == 2025
    scegs = get(committed, "ba:SCEG")
    assert scegs["name"] == "Dominion Energy South Carolina, Inc." and scegs["meta"]["eia861"]["ba_ids"] == [17539]
    assert get(committed, "ba:HECO")["country"] == "US" and get(committed, "ba:NBSO")["country"] == "CA"
    again = eia861_ba.run(fetcher=fetcher, today=date(2027, 3, 1))
    assert again["ok"] and again["rows"]["inserted"] == 0 and again["rows"]["changed"] == 0


def test_tasks_are_scheduled_monthly_in_the_collect_queue():
    for name, cron in (("collect.eia930_ref", eia930_ref.CRON_UTC), ("collect.eia861_ba", eia861_ba.CRON_UTC)):
        assert app.tasks[name].queue == "collect"
        periodic = {t.cron for (n, _), t in app.periodic_registry.periodic_tasks.items() if n == name}
        assert periodic == {cron}
    actions = cli._groups["collect"]
    assert {"eia930_ref", "eia861_ba", "grid_registry"} <= set(actions)


@pytest.mark.network
def test_live_eia_runs_twice_write_nothing_new(committed):
    first = eia930_ref.run()
    assert first["ok"], first
    assert first["bas"] >= 80 and first["documents_new"] == 1
    second = eia930_ref.run()
    assert second["ok"] and second["documents_new"] == 0 and second["rows"]["unchanged"] == first["bas"]
    ba = eia861_ba.run()
    assert ba["ok"], ba
    assert ba["bas"] >= 60
    assert eia861_ba.run()["rows"]["changed"] == 0
