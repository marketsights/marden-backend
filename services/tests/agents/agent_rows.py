"""Test helpers for the agents: a backend that answers from recorded model responses, and real identities.

The recorded extractor answers are the live answers saved by `python -m marden agents eval --record`
(tests/agents/recorded/); router, verifier and linker answers are built per test. The identity rows are
the real CoreWeave records (ids, names and CIK as in the seed and SEC data), so linking runs on real names.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from marden.agents.config import AgentSettings
from marden.agents.evals import load_cases
from marden.agents.llm import Llm, LlmRequest, LlmResult, RunBudget
from marden.core import db, documents

RECORDED = Path(__file__).resolve().parent / "recorded"
COREWEAVE_CIK = "1769628"


def recorded(case_id: str) -> dict[str, Any]:
    return json.loads((RECORDED / f"{case_id}.json").read_text())["tool_input"]


def case(case_id: str):
    return next(c for c in load_cases() if c.id == case_id)


class FakeBackend:
    """Answers each request with `answers[agent](request)`; counts calls. Text view, like the CLI."""

    name = "cli"
    view = "text"

    def __init__(self, answers: dict[str, Callable[[LlmRequest], dict[str, Any] | None]]):
        self.answers = answers
        self.calls: list[str] = []

    def complete(self, req: LlmRequest, model: str, effort: str) -> LlmResult:
        self.calls.append(req.agent)
        key = req.agent if req.agent in self.answers else req.agent.split(":")[0]
        out = self.answers[key](req)
        return LlmResult(out, "tool_use", model, self.name,
                         {"input_tokens": 1000, "output_tokens": 200, "cache_read_tokens": 0, "cache_write_tokens": 0})


def route_to(*areas: str) -> Callable[[LlmRequest], dict[str, Any]]:
    return lambda req: {"doc_kind": "8k", "language": "en", "is_scanned": False, "summary": "test",
                        "parties": [], "areas": [{"area": a, "sections": [], "reason": "r"} for a in areas]}


def agree_all(req: LlmRequest) -> dict[str, Any]:
    body = req.content[0]
    n = body.count("<fact id=")
    return {"verdicts": [{"id": i, "verdict": "agree", "reason": "stated", "corrected_value": None} for i in range(n)]}


def no_links(req: LlmRequest) -> dict[str, Any]:
    return {"links": []}


def make_llm(answers: dict[str, Callable[[LlmRequest], dict[str, Any] | None]],
             settings: AgentSettings | None = None) -> tuple[Llm, FakeBackend]:
    backend = FakeBackend(answers)
    settings = settings or AgentSettings()
    return Llm(backend, settings, budget=RunBudget(settings.run_tokens)), backend


def seed_coreweave() -> None:
    with db.transaction() as conn:
        conn.execute("""INSERT INTO entity (id, name, kind, cik) VALUES ('coreweave', 'CoreWeave', 'neocloud', %s),
                          ('coreweave-financing-ddtl-v-v-llc', 'CoreWeave Financing DDTL V-V, LLC', 'spv', NULL)
                        ON CONFLICT (id) DO NOTHING""", (COREWEAVE_CIK,))
        conn.execute("""INSERT INTO instrument (id, name, kind, issuer_entity_id, issuer_name)
                        VALUES ('EX-CRWV-097', 'DDTL 5.5', 'ddtl', 'coreweave-financing-ddtl-v-v-llc',
                                'CoreWeave Financing DDTL V-V, LLC') ON CONFLICT (id) DO NOTHING""")


def store_case_document(case_id: str, *, source_id: str = "sec_index", meta: dict[str, Any] | None = None,
                        doc_type: str | None = None) -> documents.DocumentRow:
    """The case's real excerpt stored as a document (text), queued for reading."""
    c = case(case_id)
    with db.transaction() as conn:  # a plain row: the collectors' own declarations stay the registered ones
        conn.execute("""INSERT INTO source (id, name, kind, terms_note) VALUES (%s, %s, 'api', 'test')
                        ON CONFLICT (id) DO NOTHING""",
                     (source_id, "SEC EDGAR" if source_id == "sec_index" else source_id))
    return documents.record_document(source_id, c.document["url"], c.excerpt.encode("utf-8"), content_type="text/plain",
                                     doc_type=doc_type or c.document.get("doc_type"), title=c.document.get("title"),
                                     doc_date=c.document.get("date"), meta=meta or {}, extract_state="queued")
