"""The model backends: the Anthropic API request shape (caching, strict tool, PDFs, Files API, fallbacks,
retries), the claude CLI fallback, the request cache, and Message Batches backfills; no network."""

from __future__ import annotations

import json
from types import SimpleNamespace

import anthropic
import httpx2
import pymupdf
import pytest
from agent_rows import agree_all, case, no_links, recorded, route_to, store_case_document

from marden.agents import batch
from marden.agents.areas import extract_tool, system_prompt
from marden.agents.backends import FALLBACK_BETA, ApiBackend, CliBackend
from marden.agents.config import AgentSettings
from marden.agents.llm import Llm, LlmError, LlmRequest, PdfBlock
from marden.agents.pipeline import enqueue_document, set_job
from marden.core import db

DEBT = "debt_coreweave_8k_ddtl55"


def message(tool_input=None, stop="tool_use", name="record_facts"):
    blocks = [SimpleNamespace(type="text", text="ok", model_dump=lambda **k: {"type": "text", "text": "ok"})]
    if tool_input is not None:
        blocks.append(SimpleNamespace(type="tool_use", name=name, input=tool_input,
                                      model_dump=lambda **k: {"type": "tool_use"}))
    usage = SimpleNamespace(input_tokens=1200, output_tokens=300, cache_read_input_tokens=900,
                            cache_creation_input_tokens=0)
    return SimpleNamespace(content=blocks, stop_reason=stop, usage=usage, model="claude-sonnet-5-5")


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.sent: list[dict] = []
        self.uploads = 0
        self.messages = SimpleNamespace(create=self._create, batches=SimpleNamespace())
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self.files = SimpleNamespace(upload=self._upload)

    def _create(self, **params):
        self.sent.append(params)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def _upload(self, file):
        self.uploads += 1
        return SimpleNamespace(id=f"file_{self.uploads}")


def debt_request(content=None):
    return LlmRequest(agent="extractor:debt", role="extractor", system=system_prompt("debt"), tool=extract_tool("debt"),
                      content=content or ["<document>text</document>"])


def test_api_request_caches_the_system_prompt_uses_a_strict_tool_and_server_fallbacks():
    client = FakeClient([message(recorded(DEBT))])
    api = ApiBackend(AgentSettings(), client=client)
    res = api.complete(debt_request(), "claude-sonnet-5-5", "medium")
    sent = client.sent[0]
    assert sent["system"] == [{"type": "text", "text": system_prompt("debt"), "cache_control": {"type": "ephemeral"}}]
    assert sent["tools"][0]["strict"] is True and sent["tool_choice"] == {"type": "auto"}
    assert sent["output_config"] == {"effort": "medium"} and "thinking" not in sent
    assert sent["betas"] == [FALLBACK_BETA] and sent["fallbacks"] == "default"
    assert "Answer by calling the record_facts tool." in sent["messages"][0]["content"][-1]["text"]
    assert res.tool_input == recorded(DEBT)
    assert res.usage == {"input_tokens": 1200, "output_tokens": 300, "cache_read_tokens": 900, "cache_write_tokens": 0}
    haiku = FakeClient([message({"doc_kind": "8k"}, name="record_facts")])
    ApiBackend(AgentSettings(), client=haiku).complete(debt_request(), "claude-haiku-5-5", "low")
    assert "fallbacks" not in haiku.sent[0]  # no server-side fallback for Haiku


def test_api_pdfs_are_native_blocks_and_large_ones_go_through_the_files_api_once():
    pdf = pymupdf.open()
    pdf.new_page().insert_text((72, 72), "Number 182126, GHGPSDTX263 and PSDTX1688")
    data = pdf.tobytes()
    small = ApiBackend(AgentSettings(), client=FakeClient([]))
    assert small.block(PdfBlock(data, "text"))["source"]["type"] == "base64"
    client = FakeClient([])
    big = ApiBackend(AgentSettings(files_api_min_bytes=10), client=client)
    first = big.block(PdfBlock(data, "text", title="TCEQ notice"))
    again = big.block(PdfBlock(data, "text"))
    assert first == {"type": "document", "source": {"type": "file", "file_id": "file_1"}, "title": "TCEQ notice"}
    assert again["source"] == {"type": "file", "file_id": "file_1"} and client.uploads == 1


def test_api_asks_again_without_a_tool_call_and_backs_off_on_rate_limits():
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    limited = anthropic.RateLimitError("rate limited", response=httpx2.Response(429, request=req), body=None)
    client = FakeClient([limited, message(None, stop="end_turn"), message(recorded(DEBT))])
    waits: list[float] = []
    api = ApiBackend(AgentSettings(), client=client, sleep=waits.append)
    res = api.complete(debt_request(), "claude-sonnet-5-5", "medium")
    assert waits == [20.0] and res.tool_input == recorded(DEBT)
    assert client.sent[-1]["messages"][-1]["content"][0]["text"] == "Call record_facts now."
    bad = anthropic.BadRequestError("bad", response=httpx2.Response(400, request=req), body=None)
    with pytest.raises(LlmError, match="BadRequestError"):
        ApiBackend(AgentSettings(), client=FakeClient([bad])).complete(debt_request(), "claude-sonnet-5-5", "medium")


def test_cli_fallback_sends_text_with_the_schema_and_reads_structured_output(monkeypatch):
    seen = {}

    def run(cmd, **kw):
        seen["cmd"], seen["input"], seen["env"] = cmd, kw["input"], kw["env"]
        out = {"is_error": False, "structured_output": recorded(DEBT), "stop_reason": "end_turn",
               "usage": {"input_tokens": 2, "output_tokens": 3760, "cache_creation_input_tokens": 10519},
               "modelUsage": {"claude-sonnet-5-5": {}}}
        return SimpleNamespace(stdout=json.dumps(out), returncode=0)

    monkeypatch.setattr("marden.agents.backends.claude_cli", lambda: "/usr/bin/claude")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-session")
    cli = CliBackend(AgentSettings(), run=run)
    pdf = PdfBlock(b"%PDF-1.4", "[page 1]\nNumber 182126", title="TCEQ")
    res = cli.complete(debt_request(["metadata", pdf]), "claude-sonnet-5-5", "medium")
    cmd = seen["cmd"]
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-5-5" and cmd[cmd.index("--tools") + 1] == ""
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == extract_tool("debt")["input_schema"]
    assert "--no-session-persistence" in cmd and "CLAUDE_CODE_SESSION_ID" not in seen["env"]
    assert '<document kind="pdf" title="TCEQ">' in seen["input"] and "Number 182126" in seen["input"]
    assert res.tool_input == recorded(DEBT) and res.usage["cache_write_tokens"] == 10519
    with pytest.raises(LlmError, match="scanned PDF"):
        cli.complete(debt_request([PdfBlock(b"%PDF", "", scanned=True)]), "claude-sonnet-5-5", "medium")


def test_batch_backfill_fills_the_cache_stage_by_stage_then_publishes():
    doc = store_case_document(DEBT)
    job = enqueue_document(doc.id, priority=20)
    set_job(job, mode="batch")
    answers = {"route_document": route_to("debt")(None), "record_facts": recorded(DEBT)}
    client = FakeClient([])
    submitted = []

    def create(requests):
        submitted.append(requests)
        return SimpleNamespace(id=f"msgbatch_{len(submitted)}")

    def results(batch_id):
        for item in submitted[int(batch_id.split("_")[1]) - 1]:
            params = item["params"]
            name = params["tools"][0]["name"]
            if name == "record_verdicts":
                out = agree_all(SimpleNamespace(content=[params["messages"][0]["content"][0]["text"]]))
            elif name == "record_links":
                out = no_links(None)
            else:
                out = answers[name]
            msg = message(out, name=name)
            yield SimpleNamespace(custom_id=item["custom_id"], result=SimpleNamespace(type="succeeded", message=msg))

    client.messages.batches = SimpleNamespace(create=create, results=results,
                                              retrieve=lambda bid: SimpleNamespace(processing_status="ended"))
    settings = AgentSettings()
    api = ApiBackend(settings, client=client)
    stages = []
    for _ in range(5):
        out = batch.advance(settings, api)
        if out["batch_id"]:
            stages.append([r["params"]["tools"][0]["name"] for r in submitted[-1]])
            assert all("fallbacks" not in r["params"] for r in submitted[-1])
            batch.apply_results(out["batch_id"], api, settings)
        if out["finished"]:
            break
    # One batch per stage: route, extract, verify, then link (no identity matches this document exactly).
    assert stages == [["route_document"], ["record_facts"], ["record_verdicts"], ["record_links"]]
    assert out["finished"] == [{"job_id": job, "state": "needs_review"}]
    with db.transaction() as conn:
        calls = conn.execute("SELECT backend, cost_usd, batch_id FROM agent_call ORDER BY id").fetchall()
    assert {c["backend"] for c in calls} == {"batch"} and all(c["batch_id"] for c in calls)
    full = Llm(None, settings)
    assert full.cached(debt_request()) is None  # a different request is not answered from the cache
    assert case(DEBT).excerpt.startswith("Item 1.01")
