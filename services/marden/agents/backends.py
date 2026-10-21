"""The two ways a request reaches a model: the Anthropic API (production) and the claude CLI (local fallback).

ApiBackend (Anthropic Python SDK, `anthropic` 1.x):
- the system prompt is one block with `cache_control` (tools render before it, so tool schema and system
  prompt are cached together, per area and prompt version);
- the output is one strict tool (`strict: true`, `tool_choice` auto: forced tool choice is a 400 on Claude
  Opus 5.5 / Sonnet 5.5) and the prompt names the tool; if no tool call comes back, the turn is appended and
  the model is asked once more;
- PDFs are native `document` blocks: base64, or a Files API `file_id` for large or reused files
  (`agent_file` keeps the id per sha256);
- Sonnet 5.5 / Opus 5.5 calls send `fallbacks: "default"` (beta server-side-fallback-2026-07-01), so a
  safety decline is retried server-side on a suitable model; a final `refusal` is reported, never retried;
- the SDK retries 408/409/429/5xx and connection errors with backoff (max_retries); on top of that a
  rate limit or overload gets a few longer waits before the job is failed.
CliBackend: `claude -p --output-format json --json-schema ... --system-prompt ...` with tools disabled and no
session saved; documents go in as text with page markers (a scanned PDF needs the API).
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import time
from typing import Any

from marden.agents.config import AgentSettings, claude_cli, cli_env
from marden.agents.llm import LlmError, LlmRequest, LlmResult, PdfBlock
from marden.core import db

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-opus-5")
RETRY_WAITS = (20.0, 60.0, 180.0)


def wrap_document(text: str, label: str) -> str:
    return f"<document {label}>\n{text}\n</document>"


class ApiBackend:
    name = "api"
    view = "native"

    def __init__(self, settings: AgentSettings, client: Any | None = None, sleep: Any = time.sleep):
        if client is None:
            import anthropic

            client = anthropic.Anthropic(max_retries=4, timeout=600.0)
        self.client = client
        self.settings = settings
        self.sleep = sleep

    # -------------------------------------------------------------- request building
    def file_id(self, pdf: PdfBlock) -> str:
        with db.transaction() as conn:
            row = conn.execute("SELECT file_id FROM agent_file WHERE sha256 = %s", (pdf.sha256,)).fetchone()
        if row:
            return str(row["file_id"])
        uploaded = self.client.files.upload(file=(f"{pdf.sha256[:16]}.pdf", pdf.data, "application/pdf"))
        with db.transaction() as conn:
            conn.execute(
                """INSERT INTO agent_file (sha256, file_id, bytes) VALUES (%s, %s, %s)
                   ON CONFLICT (sha256) DO UPDATE SET file_id = EXCLUDED.file_id, uploaded_at = now()""",
                (pdf.sha256, uploaded.id, len(pdf.data)),
            )
        return str(uploaded.id)

    def block(self, b: str | PdfBlock, *, allow_files: bool = True) -> dict[str, Any]:
        if isinstance(b, str):
            return {"type": "text", "text": b}
        if allow_files and len(b.data) >= self.settings.files_api_min_bytes:
            source: dict[str, Any] = {"type": "file", "file_id": self.file_id(b)}
        else:
            source = {"type": "base64", "media_type": "application/pdf", "data": b.b64()}
        out: dict[str, Any] = {"type": "document", "source": source}
        if b.title:
            out["title"] = b.title[:200]
        return out

    def params(self, req: LlmRequest, model: str, effort: str, *, allow_files: bool = True) -> dict[str, Any]:
        content = [self.block(b, allow_files=allow_files) for b in req.content]
        content.append({"type": "text", "text": f"Answer by calling the {req.tool['name']} tool."})
        return {
            "model": model,
            "max_tokens": req.max_tokens,
            "system": [{"type": "text", "text": req.system, "cache_control": {"type": "ephemeral"}}],
            "tools": [{**req.tool, "strict": True}],
            "tool_choice": {"type": "auto"},
            "output_config": {"effort": effort},
            "messages": [{"role": "user", "content": content}],
        }

    # -------------------------------------------------------------- calls
    def _create(self, params: dict[str, Any]) -> Any:
        import anthropic

        for attempt, wait in enumerate((*RETRY_WAITS, None)):
            try:
                if params["model"] in FALLBACK_MODELS:
                    return self.client.beta.messages.create(**params, betas=[FALLBACK_BETA], fallbacks="default")
                return self.client.messages.create(**params)
            except (anthropic.BadRequestError, anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                    anthropic.NotFoundError) as exc:
                raise LlmError(f"{type(exc).__name__}: {getattr(exc, 'message', exc)}"[:500]) from None
            except (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError) as exc:
                if wait is None:
                    raise LlmError(f"{type(exc).__name__} after {attempt + 1} attempts") from None
                log.warning("agent call: %s, waiting %.0fs", type(exc).__name__, wait)
                self.sleep(wait)
        raise AssertionError("unreachable")

    @staticmethod
    def usage(msg: Any) -> dict[str, int]:
        u = msg.usage
        return {
            "input_tokens": int(getattr(u, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(u, "output_tokens", 0) or 0),
            "cache_read_tokens": int(getattr(u, "cache_read_input_tokens", 0) or 0),
            "cache_write_tokens": int(getattr(u, "cache_creation_input_tokens", 0) or 0),
        }

    @staticmethod
    def tool_input(msg: Any, name: str) -> dict[str, Any] | None:
        for block in msg.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == name:
                return dict(block.input)
        return None

    def complete(self, req: LlmRequest, model: str, effort: str) -> LlmResult:
        params = self.params(req, model, effort)
        msg = self._create(params)
        usage = self.usage(msg)
        name = req.tool["name"]
        found = self.tool_input(msg, name)
        if found is None and msg.stop_reason == "end_turn":
            # auto tool choice does not guarantee a call: append the turn and ask once more (append-only).
            prior = [b.model_dump(exclude_none=True) for b in msg.content]
            params["messages"] = [*params["messages"], {"role": "assistant", "content": prior},
                                  {"role": "user", "content": [{"type": "text", "text": f"Call {name} now."}]}]
            msg = self._create(params)
            usage = {k: usage[k] + v for k, v in self.usage(msg).items()}
            found = self.tool_input(msg, name)
        return LlmResult(found, msg.stop_reason, getattr(msg, "model", model) or model, self.name, usage)


class CliBackend:
    name = "cli"
    view = "text"

    def __init__(self, settings: AgentSettings, run: Any = subprocess.run):
        self.settings = settings
        self.run = run

    @staticmethod
    def prompt(req: LlmRequest) -> str:
        parts = []
        for b in req.content:
            if isinstance(b, str):
                parts.append(b)
            elif b.scanned:
                raise LlmError("a scanned PDF needs the Anthropic API backend (native PDF reading)")
            else:
                parts.append(wrap_document(b.text, f'kind="pdf" title="{(b.title or "")[:80]}"'))
        parts.append(f"Answer with the {req.tool['name']} structured output.")
        return "\n\n".join(parts)

    def complete(self, req: LlmRequest, model: str, effort: str) -> LlmResult:
        exe = claude_cli()
        if exe is None:
            raise LlmError("the claude CLI is not installed")
        system = req.system + f"\n\nThe structured output schema is the input schema of the {req.tool['name']} tool."
        cmd = [exe, "-p", "--output-format", "json", "--model", model, "--effort", effort,
               "--system-prompt", system, "--json-schema", json.dumps(req.tool["input_schema"]),
               "--tools", "", "--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands",
               "--setting-sources", ""]
        with tempfile.TemporaryDirectory(prefix="marden-agent-") as cwd:
            try:
                proc = self.run(cmd, input=self.prompt(req), capture_output=True, text=True,
                                timeout=self.settings.cli_timeout_s, cwd=cwd, env=cli_env(), check=False)
            except subprocess.TimeoutExpired:
                raise LlmError(f"the claude CLI timed out after {self.settings.cli_timeout_s}s") from None
        try:
            out = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            raise LlmError(f"the claude CLI exited {proc.returncode} without JSON") from None
        if out.get("is_error"):
            raise LlmError(f"the claude CLI reported an error ({out.get('subtype') or 'error'})")
        u = out.get("usage") or {}
        usage = {
            "input_tokens": int(u.get("input_tokens") or 0),
            "output_tokens": int(u.get("output_tokens") or 0),
            "cache_read_tokens": int(u.get("cache_read_input_tokens") or 0),
            "cache_write_tokens": int(u.get("cache_creation_input_tokens") or 0),
        }
        served = next(iter(out.get("modelUsage") or {}), model)
        structured = out.get("structured_output")
        stop = out.get("stop_reason") or ("end_turn" if structured is not None else out.get("subtype"))
        return LlmResult(structured if isinstance(structured, dict) else None, stop, served, self.name, usage)


def make_backend(kind: str, settings: AgentSettings) -> ApiBackend | CliBackend | None:
    if kind == "api":
        return ApiBackend(settings)
    if kind == "cli":
        return CliBackend(settings)
    return None
