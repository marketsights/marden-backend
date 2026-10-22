"""Test helpers for the operations agents: real subject rows, recorded search answers and network stand-ins.

The recorded answers in tests/agents/ops/recorded/ are real responses saved on 2026-10-22: EDGAR full-text
search for '"Galaxy Helios" "commencement date"' (13 hits) and an eLibrary text search for "Project Jupiter"
(trimmed to its first 3 hits). The sites, parties and CIKs are the real records (seed and SEC data). The
stand-ins replace only the network: every search answer and fetched file is stored and parsed by the code
under test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from marden.agents.ops.search_edgar import SEARCH_URL
from marden.core import db
from marden.core.config import get_settings
from marden.core.http import FetchError, FetchResponse
from marden.core.jsonutil import jsonb

RECORDED = Path(__file__).resolve().parent / "recorded"
GALAXY_CIK = "1859392"
COREWEAVE_CIK = "1769628"
ORACLE_CIK = "1341439"


def recorded(name: str) -> bytes:
    return (RECORDED / name).read_bytes()


def page(*sentences: str) -> bytes:
    """An HTML filing body long enough to have a real text layer."""
    filler = " ".join(["The Company files this report under the Securities Exchange Act of 1934."] * 12)
    body = "".join(f"<p>{s}</p>" for s in (*sentences, filler))
    return f'<html><head><meta charset="utf-8"></head><body>{body}</body></html>'.encode()


def checklist(*items: tuple[str, str, str]) -> dict[str, Any]:
    return {"siteSlug": "x", "completeness": 50, "checklist": [
        {"area": a, "item": i, "state": st, "value": {"tag": "F", "asOf": "2026-10-21", "value": None}}
        for a, i, st in items]}


def seed_sites(*, watch_helios: bool = True) -> None:
    """helios-1 (Galaxy Digital / CoreWeave, Dickens County TX) and jupiter (Oracle / OpenAI / STACK, Dona Ana NM),
    as in the seed, each with its project-file checklist's open items."""
    with db.transaction() as conn:
        conn.execute("""INSERT INTO entity (id, name, kind, cik) VALUES
                          ('galaxy-digital', 'Galaxy Digital Inc.', 'developer', %s),
                          ('coreweave', 'CoreWeave', 'neocloud', %s), ('oracle', 'Oracle', 'hyperscaler', %s),
                          ('openai', 'OpenAI', 'ai_lab', NULL)
                        ON CONFLICT (id) DO NOTHING""", (GALAXY_CIK, COREWEAVE_CIK, ORACLE_CIK))
        conn.execute("""INSERT INTO site (slug, name, state, county, tracking_names) VALUES
                          ('helios-1', 'Helios Phase I', 'TX', 'Dickens', ARRAY['Galaxy Helios I LLC']),
                          ('jupiter', 'Project Jupiter (Stargate New Mexico)', 'NM', 'Dona Ana',
                           ARRAY['Red Chiles A\u2013D LLC', 'Project Jupiter SPV'])
                        ON CONFLICT (slug) DO NOTHING""")
        conn.execute("""INSERT INTO chain_party (site_slug, role, entity_id, label) VALUES
                          ('helios-1', 'developer', 'galaxy-digital', 'Galaxy Digital'),
                          ('helios-1', 'tenant', 'coreweave', 'CoreWeave'),
                          ('jupiter', 'tenant', 'oracle', 'Oracle'), ('jupiter', 'end_user', 'openai', 'OpenAI')""")
        rows = {"helios-1": checklist(("Lease", "Commencement", "n/d"), ("Permits", "Zoning", "n/d"),
                                      ("Lease", "Rent", "sourced"), ("Insurance", "Builder's risk", "n/p")),
                "jupiter": checklist(("Power", "Fuel supply and pipeline laterals", "n/d"))}
        for key, doc in rows.items():
            conn.execute("INSERT INTO read_model (view, key, as_of, doc) VALUES ('project_file', %s, '2026-10-21', %s)",
                         (key, jsonb(doc)))
        if watch_helios:
            conn.execute("INSERT INTO org (id, name, plan) VALUES ('o1', 'o1', 'paid') ON CONFLICT DO NOTHING")
            conn.execute("INSERT INTO watchlist_item (org_id, kind, ref_id) VALUES ('o1', 'site', 'helios-1')")


class FakeSec:
    """Stands in for the SEC hosts: the search answer for every query, and the files by URL."""

    def __init__(self, answer: bytes, files: dict[str, bytes], status: int = 200):
        self.answer, self.files, self.status = answer, files, status
        self.settings = get_settings()
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def get(self, url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
            validators: Any = None, use_cache: bool = True) -> FetchResponse:
        self.calls.append((url, params))
        if url == SEARCH_URL:
            if self.status != 200:
                raise FetchError(url, self.status, f"HTTP {self.status}")
            return FetchResponse(url, url, 200, self.answer, {"content-type": "application/json"})
        if url not in self.files:
            raise FetchError(url, 404, "HTTP 404")
        return FetchResponse(url, url, 200, self.files[url], {"content-type": "text/html"})


class _FercHttp:
    def __init__(self, answer: bytes, files: dict[str, bytes]):
        self.answer, self.files = answer, files
        self.bodies: list[dict[str, Any]] = []

    def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        self.bodies.append(json)
        request = httpx.Request("POST", url)
        if url.endswith("/Search/AdvancedSearch"):
            return httpx.Response(200, content=self.answer, headers={"content-type": "application/json"},
                                  request=request)
        file_id = json["fileidLst"][0]
        data = self.files.get(file_id)
        if data is None:
            return httpx.Response(404, content=b"not found", request=request)
        return httpx.Response(200, content=data, headers={"content-type": "application/octet-stream"},
                              request=request)


class FakeFercFetcher:
    def __init__(self, answer: bytes, files: dict[str, bytes]):
        self.client = _FercHttp(answer, files)
        self.settings = get_settings()


def ferc_answer() -> bytes:
    return recorded("ferc_text_project_jupiter.json")


def ferc_file_ids() -> list[str]:
    hits = json.loads(ferc_answer())["searchHits"]
    return [h["transmittals"][0]["fileId"] for h in hits]
