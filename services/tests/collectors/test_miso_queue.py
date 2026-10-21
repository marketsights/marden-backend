"""miso_queue: the Cloudflare challenge MISO returned on 2026-10-21 (captured) is recorded as a failed run
with nothing written; the JSON parser refuses payloads it does not know; a live attempt (network)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from marden.collectors import miso_queue as miso
from marden.collectors.rates_common import ParseError
from marden.core.http import Fetcher
from marden.core.queue import app

DATA = Path(__file__).parent / "data" / "miso_queue"


def _replay_challenge(request: httpx.Request) -> httpx.Response:
    """MISO's real answer to Marden's crawler: HTTP 403, cf-mitigated: challenge, the captured body."""
    headers = {"content-type": "text/html; charset=UTF-8", "cf-mitigated": "challenge", "server": "cloudflare"}
    return httpx.Response(403, headers=headers, content=(DATA / "getprojects_403_challenge.html").read_bytes())


def test_challenge_is_a_failed_run_and_writes_nothing(committed):
    captured = (DATA / "getprojects_403_headers.txt").read_text()
    assert "HTTP/2 403" in captured and "cf-mitigated: challenge" in captured
    out = miso.run(fetcher=Fetcher(client=httpx.Client(transport=httpx.MockTransport(_replay_challenge)),
                                   max_retries=0))
    assert out["ok"] is False and out["error"].startswith("BotChallenge: Cloudflare bot challenge")
    run = committed.execute("SELECT * FROM source_run WHERE source_id = 'miso_queue'").fetchone()
    assert run["ok"] is False and "not bypassed" in run["error"]
    assert committed.execute("SELECT count(*) AS n FROM document WHERE source_id = 'miso_queue'").fetchone()["n"] == 0
    assert committed.execute("SELECT count(*) AS n FROM fact WHERE producer = 'collector:miso_queue'"
                             ).fetchone()["n"] == 0


def test_parser_refuses_unknown_payloads():
    with pytest.raises(ParseError, match="not JSON"):
        miso.parse_projects((DATA / "getprojects_403_challenge.html").read_bytes())
    with pytest.raises(ParseError, match="lack"):
        miso.parse_projects(b'[{"queueNumber": "J1234"}]')
    with pytest.raises(ParseError, match="non-empty list"):
        miso.parse_projects(b'{"projects": []}')


def test_terms_note_and_schedule():
    assert "cf-mitigated: challenge" in miso.TERMS_NOTE and not miso.SOURCE.blocked
    assert app.tasks["collect.miso_queue"].queue == "collect"
    crons = {name: t.cron for (name, _), t in app.periodic_registry.periodic_tasks.items()}
    assert crons["collect.miso_queue"] == miso.CRON_UTC


@pytest.mark.network
def test_live_attempt_is_refused_or_parsed(committed):
    out = miso.run()
    if out["ok"]:  # MISO let the crawler through: the queue must parse into many projects
        assert out["iso"]["miso"]["entries"] > 1000
    else:
        assert out["error"].startswith("BotChallenge"), out
