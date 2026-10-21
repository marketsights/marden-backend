"""Agent settings and the model backend in use (docs/agents.md "Configuration").

Every setting is an environment variable; none is a secret except ANTHROPIC_API_KEY, which is read by
`marden.core.config` and never logged or stored. Backends, in order:

    api             ANTHROPIC_API_KEY is set: the Anthropic Python SDK (prompt caching, native PDF
                    blocks, Files API, Message Batches)
    cli             local runs only, no API key: the `claude` CLI answers non-interactively
                    (`claude -p ... --output-format json --json-schema ...`), text only
    not_configured  neither: nothing is enqueued and every status says so

MARDEN_AGENT_BACKEND=auto|api|cli|off forces a choice (tests use it). A deployed service (Railway) never
uses the CLI fallback.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field

from marden.core.config import ConfigError, deploy_marker, get_settings

PROMPT_VERSION = "0.1"
METHOD = "agents v0.1"
ROLES = ("router", "extractor", "verifier", "linker")

# Current model ids (claude-api skill, 2026-10): Haiku for triage, Sonnet for extraction (spec 21), and a
# different, stronger model for the independent verifier pass.
DEFAULT_MODELS = {
    "router": "claude-haiku-5-5",
    "extractor": "claude-sonnet-5-5",
    "verifier": "claude-opus-5-5",
    "linker": "claude-sonnet-5-5",
}
DEFAULT_EFFORT = {"router": "low", "extractor": "medium", "verifier": "medium", "linker": "low"}
EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})

# USD per million tokens (input, output); cache writes 1.25x input, cache reads 0.1x input, batches half.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}


def _int_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw.replace("_", ""))
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None
    if value <= 0:
        raise ConfigError(f"{name} must be positive")
    return value


def _caps(raw: str | None) -> dict[str, int]:
    """'sec_index=3,ferc=2' -> {'sec_index': 3, 'ferc': 2}."""
    out: dict[str, int] = {}
    for part in (raw or "").split(","):
        if not part.strip():
            continue
        name, _, num = part.partition("=")
        try:
            out[name.strip()] = max(1, int(num))
        except ValueError:
            raise ConfigError(f"MARDEN_AGENT_SOURCE_CAPS: bad entry {part!r}") from None
    return out


@dataclass(frozen=True)
class AgentSettings:
    models: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MODELS))
    effort: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_EFFORT))
    run_tokens: int = 3_000_000  # tokens one run (CLI command or worker task) may spend
    day_tokens: int = 30_000_000  # tokens all runs together may spend per UTC day
    source_cap: int = 2  # documents of one source processed at once
    source_caps: dict[str, int] = field(default_factory=dict)
    files_api_min_bytes: int = 4_000_000  # PDFs this large go through the Files API once, then by file id
    section_chars: int = 120_000  # ~30k tokens of text per extraction call
    max_sections: int = 16  # per area and document; the router picks the relevant ones
    cli_timeout_s: int = 900
    backend: str = "auto"

    @classmethod
    def from_env(cls) -> AgentSettings:
        models = {r: (os.environ.get(f"MARDEN_AGENT_MODEL_{r.upper()}") or "").strip() or m
                  for r, m in DEFAULT_MODELS.items()}
        effort = {}
        for role, default in DEFAULT_EFFORT.items():
            value = (os.environ.get(f"MARDEN_AGENT_EFFORT_{role.upper()}") or "").strip() or default
            if value not in EFFORTS:
                raise ConfigError(f"MARDEN_AGENT_EFFORT_{role.upper()} must be one of {sorted(EFFORTS)}")
            effort[role] = value
        backend = (os.environ.get("MARDEN_AGENT_BACKEND") or "auto").strip().lower()
        if backend not in ("auto", "api", "cli", "off"):
            raise ConfigError("MARDEN_AGENT_BACKEND must be auto, api, cli or off")
        return cls(
            models=models,
            effort=effort,
            run_tokens=_int_env("MARDEN_AGENT_RUN_TOKENS", cls.run_tokens),
            day_tokens=_int_env("MARDEN_AGENT_DAY_TOKENS", cls.day_tokens),
            source_cap=_int_env("MARDEN_AGENT_SOURCE_CAP", cls.source_cap),
            source_caps=_caps(os.environ.get("MARDEN_AGENT_SOURCE_CAPS")),
            files_api_min_bytes=_int_env("MARDEN_AGENT_FILES_API_MIN_BYTES", cls.files_api_min_bytes),
            section_chars=_int_env("MARDEN_AGENT_SECTION_CHARS", cls.section_chars),
            max_sections=_int_env("MARDEN_AGENT_MAX_SECTIONS", cls.max_sections),
            cli_timeout_s=_int_env("MARDEN_AGENT_CLI_TIMEOUT", cls.cli_timeout_s),
            backend=backend,
        )

    def cap_for(self, source_id: str) -> int:
        return self.source_caps.get(source_id, self.source_cap)


def cost_usd(model: str, usage: dict[str, int], *, batch: bool = False) -> float | None:
    price = PRICES.get(model)
    if price is None:
        return None
    pin, pout = price
    total = (usage.get("input_tokens", 0) * pin + usage.get("cache_write_tokens", 0) * pin * 1.25
             + usage.get("cache_read_tokens", 0) * pin * 0.1 + usage.get("output_tokens", 0) * pout) / 1e6
    return round(total * (0.5 if batch else 1.0), 6)


@dataclass(frozen=True)
class BackendStatus:
    kind: str  # api | cli | not_configured
    detail: str

    @property
    def configured(self) -> bool:
        return self.kind != "not_configured"


_lock = threading.Lock()
_status: BackendStatus | None = None


def claude_cli() -> str | None:
    return shutil.which("claude")


def cli_env() -> dict[str, str]:
    """The environment for a `claude` child process: this session's id removed so it runs on its own."""
    env = dict(os.environ)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    return env


def probe_cli(timeout_s: int = 120) -> tuple[bool, str]:
    """Does `claude -p "say ok" --output-format json` answer here? (One tiny call.)"""
    exe = claude_cli()
    if exe is None:
        return False, "the claude CLI is not installed"
    with tempfile.TemporaryDirectory(prefix="marden-agent-probe-") as cwd:
        try:
            proc = subprocess.run(
                [exe, "-p", "say ok", "--output-format", "json", "--model", DEFAULT_MODELS["router"],
                 "--tools", "", "--no-session-persistence", "--strict-mcp-config", "--setting-sources", ""],
                capture_output=True, text=True, timeout=timeout_s, cwd=cwd, env=cli_env(), check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"the claude CLI did not answer ({type(exc).__name__})"
    try:
        out = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return False, f"the claude CLI exited {proc.returncode} without JSON"
    if out.get("is_error") or proc.returncode != 0:
        return False, f"the claude CLI answered with an error ({out.get('subtype') or proc.returncode})"
    return True, "claude CLI fallback (local runs only; no API key)"


def detect_backend(settings: AgentSettings | None = None, *, refresh: bool = False) -> BackendStatus:
    """The backend this process uses (cached; `refresh` re-checks)."""
    global _status
    with _lock:
        if _status is not None and not refresh:
            return _status
        settings = settings or AgentSettings.from_env()
        has_key = get_settings().has_anthropic()
        pref = settings.backend
        if pref == "off":
            status = BackendStatus("not_configured", "MARDEN_AGENT_BACKEND=off")
        elif has_key and pref in ("auto", "api"):
            status = BackendStatus("api", "Anthropic API (ANTHROPIC_API_KEY)")
        elif pref == "api":
            status = BackendStatus("not_configured", "ANTHROPIC_API_KEY is not set")
        elif (marker := deploy_marker()) is not None:
            status = BackendStatus(
                "not_configured",
                f"ANTHROPIC_API_KEY is not set ({marker} is set: no CLI fallback on a deployed service)")
        else:
            ok, detail = probe_cli()
            status = BackendStatus("cli", detail) if ok else BackendStatus(
                "not_configured", f"ANTHROPIC_API_KEY is not set and {detail}")
        _status = status
        return status


def set_backend_status(status: BackendStatus | None) -> None:
    """Tests: pin (or clear) the detected backend."""
    global _status
    with _lock:
        _status = status
