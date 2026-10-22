"""sentinel2: search/item parsers and the change score on real Planetary Computer responses and real COG
windows (Project Jupiter box, May-Sep 2025), the terms quotes, facts and reruns, and a live check.

Fixtures (tests/collectors/data/sentinel2): the STAC search response for the Jupiter box trimmed to
May-Sep 2025, the five monthly picks' item JSON as served, and their SCL/B04/B08/B11 windows as
CogReader returned them (np.savez_compressed), plus the SCL window of a cloudy pass (18 Jul 2025).
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import httpx
import numpy as np
import pytest

from marden import cli
from marden.collectors import sentinel2
from marden.collectors import sentinel2_raster as sr
from marden.collectors import sentinel2_stac as st
from marden.collectors.rates_common import ParseError, RunStats
from marden.collectors.sentinel2_store import CHANGE_THRESHOLD, PRODUCER
from marden.core.http import Fetcher
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "sentinel2"
JUPITER = (-106.6653, 31.8183)
BOX = st.Box.around(*JUPITER)
MAY, JUN, JUL, AUG, SEP = (
    "S2B_MSIL2A_20250522T173909_R098_T13SCR_20250522T213012",
    "S2A_MSIL2A_20250608T174731_R098_T13SCR_20250608T225513",
    "S2C_MSIL2A_20250726T173931_R098_T13SCR_20250726T224713",
    "S2A_MSIL2A_20250807T174731_R098_T13SCR_20250808T000013",
    "S2B_MSIL2A_20250909T173859_R098_T13SCR_20250909T214235",
)
CLOUDY_JUL = "S2A_MSIL2A_20250718T174741_R098_T13SCR_20250718T223114"


def _window(scene_id: str) -> dict[str, np.ndarray]:
    with np.load(DATA / f"window_{scene_id}.npz") as z:
        return {k: z[k] for k in z.files}


def _item(scene_id: str) -> dict:
    return st.parse_item((DATA / f"item_{scene_id}.json").read_bytes())


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text)


class FixtureReader:
    """Serves the captured windows by asset href, as CogReader would have read them."""

    def __init__(self) -> None:
        self.by_href: dict[str, tuple[str, str]] = {}
        search = json.loads((DATA / "search_jupiter_2025-05_2025-09.json").read_text())
        for f in search["features"]:
            self.by_href[f["assets"]["SCL"]["href"]] = (f["id"], "SCL")
        for sid in (MAY, JUN, JUL, AUG, SEP):
            for band, href in _item(sid)["hrefs"].items():
                self.by_href[href] = (sid, band)
        self.calls = 0

    def read(self, hrefs, box):
        assert box == BOX
        self.calls += 1
        out = {}
        for band, href in hrefs.items():
            sid, b = self.by_href[href]
            assert b == band
            path = DATA / f"window_{sid}.npz"
            if not path.exists():
                raise sr.ReadError(f"no captured window for {sid}")
            out[band] = _window(sid)[band]
        return out


def _transport(status: int = 200) -> tuple[Fetcher, list[str]]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if status != 200:
            return httpx.Response(status, text="unavailable")
        if request.url.path.endswith("/search"):
            return httpx.Response(200, content=(DATA / "search_jupiter_2025-05_2025-09.json").read_bytes(),
                                  headers={"content-type": "application/geo+json"})
        sid = request.url.path.rsplit("/", 1)[1]
        return httpx.Response(200, content=(DATA / f"item_{sid}.json").read_bytes(),
                              headers={"content-type": "application/geo+json"})

    return Fetcher(client=httpx.Client(transport=httpx.MockTransport(handler)), backoff_base=0, max_retries=0), seen


# ---------------------------------------------------------------- terms
def test_terms_quotes_are_verbatim_from_the_captured_pages():
    note = sentinel2.TERMS_NOTE
    pages = {
        "pc_terms_excerpt.txt": ["Previews are not meant for production use (though such use is not prohibited)",
                                 "may not be sublicensed to, distributed to, or shared with third parties",
                                 "Tokens may not be monetized",
                                 "rate limiting may be put into place to limit the amount of egress"],
        "pc_sas_docs_excerpt.md": ["The data hosted by the Planetary Computer is free for anyone to use",
                                   "available to all users and does not require an account or a token to use"],
        "msa_code_of_conduct_excerpt.txt": [
            "Don\u2019t circumvent any restrictions on access to, usage, or availability of the Services (e.g., "
            "attempting to “jailbreak” an AI system or impermissible scraping)", "Effective: September 30, 2026"],
        "sentinel_legal_notice.txt": [
            "users shall have a free, full and open access to Copernicus Sentinel Data",
            "(a) reproduction; (b) distribution; (c) communication to the public; (d) adaptation, modification and "
            "combination with other data and information",
            "Where the Copernicus Sentinel Data and Service Information have been adapted or modified, the user "
            "shall provide the following notice",
            "'Contains modified Copernicus Sentinel data [Year]' for Sentinel data"],
    }
    for name, quotes in pages.items():
        page = _norm((DATA / name).read_text())
        for q in quotes:
            assert q in page, (name, q)
            if not q.startswith("Effective"):
                assert q in _norm(note), q
    assert "STAC metatdata [sic] API" in note and "metatdata API" in (DATA / "pc_sas_docs_excerpt.md").read_text()
    robots = (DATA / "pc_robots.txt").read_text()
    assert "User-agent: *" in robots and "Disallow" not in robots
    assert not sentinel2.SOURCE.blocked


# ---------------------------------------------------------------- parse and select
def test_box_urls_and_months():
    assert BOX.text() == "-106.67323,31.81156,-106.65737,31.82504"
    url = st.search_url(BOX, st.window_start(date(2026, 10, 21)))
    assert st.window_start(date(2026, 10, 21)) == date(2025, 5, 1)
    assert "datetime=2025-05-01T00%3A00%3A00Z%2F.." in url and "eo%3Acloud_cover+%3C+20" in url
    img = st.imagery_url(SEP, BOX)
    assert img.startswith("https://planetarycomputer.microsoft.com/api/data/v1/item/bbox/-106.67323,31.81156,")
    assert "sig=" not in img and "se=" not in img and "item=" + SEP in img
    assert st.item_url(SEP).endswith(f"/collections/sentinel-2-l2a/items/{SEP}")
    assert st.base_months("2025-07") == ["2025-04", "2025-03", "2025-05"]
    assert st.month_end("2024-02") == date(2024, 2, 29)
    assert st.month_final("2025-09", date(2025, 10, 6)) and not st.month_final("2025-09", date(2025, 10, 5))
    assert st.caption(date(2025, 9, 9), 0.996) == ("Sentinel-2 L2A, 9 Sep 2025: 1.5 km view, 100% cloud-free. "
                                                  "Contains modified Copernicus Sentinel data 2025")


def test_parses_the_real_search_response_and_picks_candidates():
    scenes, nxt = st.parse_search((DATA / "search_jupiter_2025-05_2025-09.json").read_bytes(), BOX)
    assert nxt is None and len(scenes) == 25
    assert all(s.tile == "13SCR" and s.contains_box and s.scl_href for s in scenes)
    assert st.choose_tile(scenes) == "13SCR"
    months = st.candidates_by_month(scenes, "13SCR")
    assert list(months) == ["2025-05", "2025-06", "2025-07", "2025-08", "2025-09"]
    assert [c[0].id for c in months.values()] == [MAY, JUN, JUL, AUG, SEP]
    assert months["2025-07"][-1].id == CLOUDY_JUL and months["2025-07"][-1].cloud_pct > 16
    far = st.Box.around(-100.0, 40.0)  # a box outside every footprint
    assert not any(st.parse_search((DATA / "search_jupiter_2025-05_2025-09.json").read_bytes(), far)[0][i].contains_box
                   for i in range(3))
    with pytest.raises(ParseError):
        st.parse_search(b'{"type": "Feature"}', BOX)


def test_parses_real_item_json():
    item = _item(SEP)
    assert item["id"] == SEP and item["baseline"] == "05.11" and item["tile"] == "13SCR"
    assert item["acquired"].date() == date(2025, 9, 9)
    assert set(item["hrefs"]) == {"SCL", "B04", "B08", "B11"}
    assert item["hrefs"]["B11"].endswith("_B11_20m.tif") and "?" not in item["hrefs"]["B11"]
    with pytest.raises(ParseError):
        st.parse_item(b'{"id": "x", "properties": {}, "assets": {}}')


# ---------------------------------------------------------------- change score
def test_change_scores_on_real_windows():
    w = {sid: _window(sid) for sid in (MAY, JUN, JUL, AUG, SEP)}
    assert w[SEP]["SCL"].shape == (77, 77) and w[SEP]["B04"].shape == (154, 154)
    assert sr.clear_fraction(w[SEP]["SCL"]) == 1.0
    assert sr.clear_fraction(_window(CLOUDY_JUL)["SCL"]) == pytest.approx(0.0444, abs=1e-4)
    assert sr.boa_offset("05.11") == -1000 and sr.boa_offset("03.01") == 0
    refl, valid = sr.reflectance(w[SEP], "05.11")
    assert refl.shape == (3, 77, 77) and valid.all() and 0 < float(refl.mean()) < 0.6
    jul = sr.change_score(w[JUL], "05.11", w[MAY], "05.11")
    aug = sr.change_score(w[AUG], "05.11", w[MAY], "05.11")
    sep = sr.change_score(w[SEP], "05.11", w[JUN], "05.11")
    assert (jul.score, aug.score, sep.score) == (0.0086, 0.0318, 0.0581)  # works start at Jupiter by Sep 2025
    assert sep.valid_pixels == 77 * 77 and sep.valid_fraction == 1.0
    assert sep.score >= CHANGE_THRESHOLD > aug.score
    cloudy = dict(w[JUL], SCL=_window(CLOUDY_JUL)["SCL"])
    assert sr.change_score(cloudy, "05.11", w[MAY], "05.11") is None  # < half the box clear in both


def test_pixel_window_covers_the_box_and_signed_urls_are_redacted():
    transform = (20.0, 0.0, 300000.0, 0.0, -20.0, 3600000.0)
    assert sr.pixel_window((300010.0, 3598490.0, 301490.0, 3599990.0), transform) == (0, 0, 75, 76)
    signed = "'/vsicurl/https://sentinel2l2a01.blob.core.windows.net/a/B04.tif?st=2026&se=2026&sp=rl&sig=AbC%3D' x"
    assert sr.redact(signed) == "'/vsicurl/https://sentinel2l2a01.blob.core.windows.net/a/B04.tif?<signed>' x"


def test_rank_skips_a_cloudy_pass():
    reader = FixtureReader()
    ctx = sentinel2.Context(sentinel2.SOURCE, RunStats(), None, reader, date(2025, 10, 20), 18)
    scenes, _ = st.parse_search((DATA / "search_jupiter_2025-05_2025-09.json").read_bytes(), BOX)
    by_id = {s.id: s for s in scenes}
    scene, frac = sentinel2.rank([by_id[CLOUDY_JUL], by_id[JUL]], BOX, ctx)
    assert scene.id == JUL and frac == 1.0 and reader.calls == 2
    scene, _ = sentinel2.rank([by_id[JUL], by_id[CLOUDY_JUL]], BOX, ctx)
    assert scene.id == JUL and reader.calls == 3  # the first clear pass ends the month's ranking


# ---------------------------------------------------------------- facts and reruns
def _jupiter(committed) -> None:
    committed.execute("""INSERT INTO site (slug, name, state, in_v1, geom)
                         VALUES ('jupiter', 'Project Jupiter', 'NM', true, ST_SetSRID(ST_MakePoint(%s, %s), 4326)),
                                ('no-point', 'No point', 'TX', true, NULL)""", JUPITER)
    committed.execute("""INSERT INTO campus (id, name, geom)
                         VALUES ('project-jupiter', 'Project Jupiter', ST_SetSRID(ST_MakePoint(%s, %s), 4326))""",
                      JUPITER)
    committed.execute("INSERT INTO site_link (site_slug, campus_id) VALUES ('jupiter', 'project-jupiter')")


def _facts(committed) -> list[dict]:
    return committed.execute(
        "SELECT * FROM fact WHERE producer = %s ORDER BY subject_type, field, valid_from", (PRODUCER,)
    ).fetchall()


def test_run_writes_imagery_scores_and_last_change_then_reruns_clean(committed):
    _jupiter(committed)
    fetcher, seen = _transport()
    reader = FixtureReader()
    out = sentinel2.run(sites=["jupiter", "no-point"], fetcher=fetcher, reader=reader, today=date(2025, 10, 20))
    assert out["ok"], out
    assert out["sites_without_point"] == ["no-point"] and out["subjects"] == 2 and out["points"] == 1
    assert out["documents_new"] == 5 and out["new_by_series"] == {
        "imagery": 10, "change_score": 6, "last_visible_change": 2}
    assert out["by_point"][0]["scores"] == {"2025-07": 0.0086, "2025-08": 0.0318, "2025-09": 0.0581}
    assert out["by_point"][0]["last_visible_change"] == "2025-09-09"
    assert all("sig=" not in u for u in seen) and sum("/search" in u for u in seen) == 1

    facts = _facts(committed)
    imagery = [f for f in facts if f["field"] == "imagery" and f["subject_type"] == "site"]
    assert [f["value"]["month"] for f in imagery] == ["2025-05", "2025-06", "2025-07", "2025-08", "2025-09"]
    sep = imagery[-1]
    assert sep["value"]["scene_id"] == SEP and sep["valid_from"] == date(2025, 9, 9) and sep["tag"] == "R"
    assert sep["value"]["url"] == st.imagery_url(SEP, BOX) and sep["source_url"] == st.item_url(SEP)
    assert sep["value"]["caption"].endswith("Contains modified Copernicus Sentinel data 2025")
    doc = committed.execute("SELECT * FROM document WHERE id = %s", (sep["document_id"],)).fetchone()
    assert doc["source_id"] == sentinel2.SOURCE_ID and doc["external_id"] == SEP and doc["doc_type"] == "stac_item"
    score = next(f for f in facts if f["field"] == "change_score" and f["valid_from"] == date(2025, 9, 9))
    assert score["value"] == 0.0581 and score["tag"] == "M" and score["method_version"] == "change-score@0.1"
    assert score["unit"] == "reflectance" and score["quote_loc"].startswith(f"{SEP} vs {JUN}; 5929 px")
    last = [f for f in facts if f["field"] == "last_visible_change"]
    assert {(f["subject_type"], f["subject_id"], f["value"]) for f in last} == {
        ("site", "jupiter", "2025-09-09"), ("campus", "project-jupiter", "2025-09-09")}
    run_row = committed.execute("SELECT * FROM source_run WHERE source_id = %s", (sentinel2.SOURCE_ID,)).fetchone()
    assert run_row["ok"] and run_row["items_new"] == 18

    calls, n_facts = reader.calls, len(facts)
    again = sentinel2.run(sites=["jupiter"], fetcher=fetcher, reader=reader, today=date(2025, 10, 20))
    assert again["ok"] and again["facts_new"] == 0 and again["documents_new"] == 0
    assert again["picks_reused"] == 5 and again["scores_reused"] == 3 and reader.calls == calls
    assert len(_facts(committed)) == n_facts


def test_open_month_is_reranked_without_new_facts(committed):
    _jupiter(committed)
    fetcher, _ = _transport()
    reader = FixtureReader()
    assert sentinel2.run(sites=["jupiter"], fetcher=fetcher, reader=reader, today=date(2025, 10, 3))["ok"]
    again = sentinel2.run(sites=["jupiter"], fetcher=fetcher, reader=reader, today=date(2025, 10, 3))
    assert again["picks_reused"] == 4 and again["scl_reads"] == 1  # September is still open
    assert again["facts_new"] == 0 and again["documents_new"] == 0


def test_failed_search_is_recorded_and_writes_nothing(committed):
    _jupiter(committed)
    fetcher, _ = _transport(status=503)
    out = sentinel2.run(sites=["jupiter"], fetcher=fetcher, reader=FixtureReader(), today=date(2025, 10, 20))
    assert not out["ok"] and "503" in out["error"]
    source = committed.execute("SELECT * FROM source WHERE id = %s", (sentinel2.SOURCE_ID,)).fetchone()
    assert source["last_error"] and "503" in source["last_error"]
    assert _facts(committed) == []


def test_task_schedule_and_cli():
    task = app.tasks["collect.sentinel2"]
    assert task.queue == "collect"
    crons = {name: t.cron for (name, _), t in app.periodic_registry.periodic_tasks.items()}
    assert crons["collect.sentinel2"] == "17 7 */5 * *"
    args = cli.build_parser().parse_args(["collect", "sentinel2", "--site", "jupiter", "--site", "abilene"])
    assert args.sites == ["jupiter", "abilene"] and args._handler is sentinel2.collect_cli


# ---------------------------------------------------------------- live
@pytest.mark.network
def test_live_search_item_and_window():
    from marden.core.config import get_settings
    from marden.core.http import marden_user_agent

    fetcher = Fetcher()
    scenes, _ = st.parse_search(fetcher.get(st.search_url(BOX, date(2025, 9, 1))).content, BOX)
    september = [s for s in scenes if s.month == "2025-09" and s.tile == "13SCR"]
    assert SEP in {s.id for s in september}
    item = st.parse_item(fetcher.get(st.item_url(SEP)).content)
    assert item["hrefs"] == _item(SEP)["hrefs"]
    raw = sr.CogReader(marden_user_agent(get_settings())).read({"SCL": item["hrefs"]["SCL"]}, BOX)
    np.testing.assert_array_equal(raw["SCL"], _window(SEP)["SCL"])


def test_image_is_rendered_over_the_campus_buildings_and_rerendered_when_they_widen_the_view(committed):
    _jupiter(committed)
    fetcher, _ = _transport()
    reader = FixtureReader()
    assert sentinel2.run(sites=["jupiter"], fetcher=fetcher, reader=reader, today=date(2025, 10, 20))["ok"]
    # A building record east of the 1.5 km box (about 1 km from the point): the view widens to take it in.
    lon0, lat0 = JUPITER
    committed.execute(
        """INSERT INTO building (id, campus_id, label, footprint) VALUES ('pj-b9', 'project-jupiter', 'Building 9',
             ST_SetSRID(ST_MakeEnvelope(%s, %s, %s, %s), 4326))""",
        (lon0 + 0.0085, lat0 - 0.001, lon0 + 0.0105, lat0 + 0.001),
    )
    calls = reader.calls
    out = sentinel2.run(sites=["jupiter"], fetcher=fetcher, reader=reader, today=date(2025, 10, 20))
    assert out["ok"] and out["picks_reused"] == 5 and out["scores_reused"] == 3
    assert reader.calls == calls  # nothing re-ranked or re-scored
    assert out["images_rerendered"] == 5
    view = st.Box(*(float(x) for x in out["by_point"][0]["view"].split(",")))
    assert view.west == BOX.west and view.south == BOX.south and view.north == BOX.north
    assert view.east > lon0 + 0.0105  # the building plus the padding
    live = committed.execute(
        """SELECT value, status FROM fact WHERE producer = %s AND field = 'imagery' AND subject_type = 'site'
            ORDER BY id""", (PRODUCER,)).fetchall()
    published = [r["value"] for r in live if r["status"] == "published"]
    assert len(published) == 5 and all(v["url"] == st.imagery_url(v["scene_id"], view) for v in published)
    assert {r["status"] for r in live[:5]} == {"superseded"}
    km = st.view_km(view)
    assert published[-1]["caption"].startswith(f"Sentinel-2 L2A, 9 Sep 2025: {km:g} km view, 100% cloud-free")
    again = sentinel2.run(sites=["jupiter"], fetcher=fetcher, reader=reader, today=date(2025, 10, 20))
    assert again["facts_new"] == 0 and "images_rerendered" not in again
