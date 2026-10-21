"""Model calls for the agents: one interface, a request cache, token accounting and budgets.

    llm = Llm(backend, settings, job_id=7)
    result = llm.call(request)          # LlmResult (cached when the same request was answered before)

A request is the whole prompt: role (router | extractor | verifier | linker -> model and effort), the stable
system prompt, one strict tool (the output schema) and the user content (text blocks and PDF blocks).
Its hash keys `agent_call`: a rerun, a retry after a crash, or a duplicate document with the same bytes is
answered from the table at no cost, which is also how Batch API results feed the pipeline (batch mode
fills the cache; the pipeline then runs on cache hits). Budgets: MARDEN_AGENT_RUN_TOKENS per run and
MARDEN_AGENT_DAY_TOKENS per UTC day, checked before every uncached call. Logs carry ids and token counts
only, never document text.
"""

from __future__ import annotations

import base64
import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from marden.agents.config import PROMPT_VERSION, AgentSettings, cost_usd
from marden.core import db
from marden.core.jsonutil import hash_parts, jsonb, sha256_hex

log = logging.getLogger(__name__)


class LlmError(RuntimeError):
    """A model call failed in a way a retry will not fix (bad request, refusal, unreadable input)."""


class BudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class PdfBlock:
    """A PDF given to the model natively (API) or as its text with page markers (CLI fallback)."""

    data: bytes
    text: str
    title: str | None = None
    scanned: bool = False

    @property
    def sha256(self) -> str:
        return sha256_hex(self.data)

    def b64(self) -> str:
        return base64.standard_b64encode(self.data).decode("ascii")


Block = str | PdfBlock


@dataclass
class LlmRequest:
    agent: str  # 'router', 'extractor:<area>', 'verifier', 'linker'
    role: str  # key into AgentSettings.models / effort
    system: str
    tool: dict[str, Any]
    content: list[Block]
    max_tokens: int = 16000
    job_id: int | None = None
    document_sha256: str | None = None
    section: str | None = None

    def key_parts(self, model: str, effort: str, view: str) -> list[Any]:
        blocks = [b if isinstance(b, str) else {"pdf": b.sha256, "view": view} for b in self.content]
        return [PROMPT_VERSION, model, effort, self.system, self.tool, blocks, self.max_tokens]


@dataclass
class LlmResult:
    tool_input: dict[str, Any] | None
    stop_reason: str | None
    model: str
    backend: str
    usage: dict[str, int] = field(default_factory=dict)
    cached: bool = False
    call_id: int | None = None
    cost: float | None = None

    @property
    def tokens(self) -> int:
        return sum(self.usage.get(k, 0) for k in ("input_tokens", "output_tokens", "cache_read_tokens",
                                                   "cache_write_tokens"))


class Backend(Protocol):
    name: str  # 'api' | 'cli'
    view: str  # how PDFs are seen: 'native' | 'text'

    def complete(self, req: LlmRequest, model: str, effort: str) -> LlmResult: ...


USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def day_tokens_used(conn: db.Conn | None = None) -> int:
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    with db.transaction(conn) as c:
        row = c.execute(
            """SELECT coalesce(sum(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens), 0) AS n
               FROM agent_call WHERE created_at >= %s""",
            (start,),
        ).fetchone()
    return int(row["n"]) if row else 0


def estimate_tokens(req: LlmRequest) -> int:
    text = len(req.system) + sum(len(b) if isinstance(b, str) else len(b.text) for b in req.content)
    return text // 3 + req.max_tokens // 4


@dataclass
class RunBudget:
    """Tokens one run may spend; shared by the threads of a run."""

    limit: int
    used: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, n: int) -> None:
        with self._lock:
            self.used += n


class Llm:
    """Calls through one backend with the request cache, accounting and budgets."""

    def __init__(self, backend: Backend | None, settings: AgentSettings, *, budget: RunBudget | None = None,
                 collect: bool = False):
        self.backend = backend
        self.settings = settings
        self.budget = budget or RunBudget(settings.run_tokens)
        self.collect = collect  # batch planning: misses are recorded, not called
        self.pending: list[tuple[str, LlmRequest, str, str]] = []
        self.calls = 0
        self.cache_hits = 0

    def model_for(self, req: LlmRequest) -> tuple[str, str]:
        return self.settings.models[req.role], self.settings.effort[req.role]

    def request_hash(self, req: LlmRequest) -> str:
        model, effort = self.model_for(req)
        view = self.backend.view if self.backend else "native"
        return hash_parts(*req.key_parts(model, effort, view))

    def cached(self, req: LlmRequest) -> LlmResult | None:
        h = self.request_hash(req)
        with db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM agent_call WHERE request_hash = %s AND error IS NULL", (h,)
            ).fetchone()
        if row is None:
            return None
        return LlmResult(row["response"], row["stop_reason"], row["model"], row["backend"], cached=True,
                         call_id=row["id"])

    def call(self, req: LlmRequest) -> LlmResult | None:
        """The answer (cached or new). None only in collect mode, when the request still needs a call."""
        hit = self.cached(req)
        if hit is not None:
            self.cache_hits += 1
            return hit
        model, effort = self.model_for(req)
        if self.collect:
            self.pending.append((self.request_hash(req), req, model, effort))
            return None
        if self.backend is None:
            raise LlmError("agents are not configured (no ANTHROPIC_API_KEY and no CLI fallback)")
        self.check_budget(req)
        result = self.backend.complete(req, model, effort)
        self.calls += 1
        self.budget.add(result.tokens)
        result.cost = cost_usd(result.model, result.usage)
        result.call_id = self.record(req, result)
        log.info("agent call %s %s model=%s tokens=%d stop=%s", req.agent, req.section or "-", result.model,
                 result.tokens, result.stop_reason)
        return result

    def check_budget(self, req: LlmRequest) -> None:
        estimate = estimate_tokens(req)
        if self.budget.used + estimate > self.budget.limit:
            raise BudgetExceeded(f"run token budget reached ({self.budget.used:,} of {self.budget.limit:,})")
        used = day_tokens_used()
        if used + estimate > self.settings.day_tokens:
            raise BudgetExceeded(f"daily token budget reached ({used:,} of {self.settings.day_tokens:,})")

    def record(self, req: LlmRequest, result: LlmResult, *, batch_id: str | None = None,
               request_hash: str | None = None) -> int:
        h = request_hash or self.request_hash(req)
        with db.transaction() as conn:
            row = conn.execute(
                """INSERT INTO agent_call (request_hash, agent, backend, model, prompt_version, job_id,
                                           document_sha256, section, response, stop_reason, input_tokens,
                                           output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, batch_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (request_hash) DO UPDATE SET request_hash = EXCLUDED.request_hash
                   RETURNING id""",
                (h, req.agent, result.backend, result.model, PROMPT_VERSION, req.job_id, req.document_sha256,
                 req.section, jsonb(result.tool_input) if result.tool_input is not None else None,
                 result.stop_reason, *(int(result.usage.get(k, 0)) for k in USAGE_KEYS), result.cost, batch_id),
            ).fetchone()
        assert row is not None
        return int(row["id"])


def usage_summary(job_id: int | None = None, since: datetime | None = None) -> dict[str, Any]:
    """Tokens and cost by agent and model (for reports and the status API)."""
    where, params = [], []
    if job_id is not None:
        where.append("job_id = %s")
        params.append(job_id)
    if since is not None:
        where.append("created_at >= %s")
        params.append(since)
    sql = f"""SELECT agent, model, backend, count(*) AS calls, sum(input_tokens) AS input_tokens,
                     sum(output_tokens) AS output_tokens, sum(cache_read_tokens) AS cache_read_tokens,
                     sum(cache_write_tokens) AS cache_write_tokens, sum(cost_usd) AS cost_usd
              FROM agent_call {"WHERE " + " AND ".join(where) if where else ""}
              GROUP BY 1, 2, 3 ORDER BY 1, 2"""
    with db.transaction() as conn:
        rows = conn.execute(sql, params).fetchall()
    total = {k: sum(int(r[k] or 0) for r in rows) for k in USAGE_KEYS}
    total["cost_usd"] = float(sum(float(r["cost_usd"] or 0) for r in rows))
    return {"by_agent": rows, "total": total}
