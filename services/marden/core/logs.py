"""Process logging: one line per record, secrets redacted, JSON on deployed services.

    MARDEN_LOG_FORMAT=json   one JSON object per line (ts, level, logger, message, exc); the worker
                             image sets it, and Railway's log explorer indexes the fields
    MARDEN_LOG_FORMAT=text   (default) `2026-10-21 18:00:00,000 INFO name: message`
    MARDEN_LOG_LEVEL         DEBUG | INFO (default) | WARNING | ERROR

Redaction runs on the formatted line (message and traceback): the value of every credential in the
environment (names ending in _KEY, _SECRET, _TOKEN, _PASSWORD; DATABASE_URL*), the password in any
URL (`scheme://user:***@host`), and secret-looking query parameters (`api_key=***`). Collectors
already keep keys out of stored URLs and silence httpx's request log; this is the backstop.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime

MASK = "***"
_SECRET_NAME = re.compile(r"(_KEY|_SECRET|_TOKEN|_PASSWORD|_PASS|_CREDENTIALS)$|^DATABASE_URL|^NEON_DATABASE_URL")
_URL_PASSWORD = re.compile(r"(?P<scheme>[a-z][a-z0-9+.\-]*://)(?P<user>[^:/@\s]*):(?P<pw>[^@\s/]+)@", re.IGNORECASE)
_QUERY_SECRET = re.compile(
    r"(?P<k>[?&](?:api[_-]?key|apikey|access[_-]?token|token|secret|password|passwd|signature|sig|key|"
    r"client[_-]?secret|subscription[_-]?key)=)(?P<v>[^&\s\"']+)",
    re.IGNORECASE,
)
_MIN_SECRET_LEN = 6
_FORMATS = ("text", "json")
_OWN = "_marden_handler"


def secret_values(env: Mapping[str, str]) -> list[str]:
    """Credential values from the environment, longest first (so a URL is masked before its parts)."""
    values: set[str] = set()
    for name, value in env.items():
        value = (value or "").strip()
        if len(value) < _MIN_SECRET_LEN or not _SECRET_NAME.search(name.upper()):
            continue
        values.add(value)
        match = _URL_PASSWORD.search(value)
        if match and len(match.group("pw")) >= _MIN_SECRET_LEN:
            values.add(match.group("pw"))
    return sorted(values, key=len, reverse=True)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    for value in secrets:
        if value in text:
            text = text.replace(value, MASK)
    text = _URL_PASSWORD.sub(lambda m: f"{m.group('scheme')}{m.group('user')}:{MASK}@", text)
    return _QUERY_SECRET.sub(lambda m: f"{m.group('k')}{MASK}", text)


class RedactingFormatter(logging.Formatter):
    """The text format with secrets masked."""

    def __init__(self, secrets: Iterable[str], fmt: str | None = None):
        super().__init__(fmt or "%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.secrets = list(secrets)

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self.secrets)


class JsonFormatter(logging.Formatter):
    """One JSON object per record: ts (UTC ISO), level, logger, message, exc (traceback, if any)."""

    def __init__(self, secrets: Iterable[str]):
        super().__init__()
        self.secrets = list(secrets)

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": redact(record.getMessage(), self.secrets),
        }
        if record.exc_info:
            out["exc"] = redact(self.formatException(record.exc_info), self.secrets)
        elif record.exc_text:
            out["exc"] = redact(record.exc_text, self.secrets)
        return json.dumps(out, ensure_ascii=False, default=str)


def setup_logging(env: Mapping[str, str] | None = None) -> logging.Handler | None:
    """Install Marden's stderr handler on the root logger. Like logging.basicConfig, it leaves logging
    alone when something else already configured it (pytest's capture, an embedding app); calling it
    again replaces only its own handler. Returns the handler, or None when it left logging alone."""
    env = os.environ if env is None else env
    fmt = (env.get("MARDEN_LOG_FORMAT") or "text").strip().lower()
    if fmt not in _FORMATS:
        raise ValueError(f"MARDEN_LOG_FORMAT must be one of {', '.join(_FORMATS)}")
    level_name = (env.get("MARDEN_LOG_LEVEL") or "INFO").strip().upper()
    level = logging.getLevelName(level_name)
    if not isinstance(level, int):
        raise ValueError("MARDEN_LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR")
    logging.getLogger("httpx").setLevel(max(level, logging.WARNING))  # full request URLs at INFO
    root = logging.getLogger()
    ours = [h for h in root.handlers if getattr(h, _OWN, False)]
    if len(ours) < len(root.handlers):
        return None
    for old in ours:
        root.removeHandler(old)
    secrets = secret_values(env)
    handler = logging.StreamHandler()
    setattr(handler, _OWN, True)
    handler.setFormatter(JsonFormatter(secrets) if fmt == "json" else RedactingFormatter(secrets))
    root.addHandler(handler)
    root.setLevel(level)
    return handler
