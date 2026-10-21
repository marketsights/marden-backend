"""Email delivery settings, read from the environment (names in .env.example).

Sending needs a provider key (POSTMARK_TOKEN or RESEND_API_KEY; ALERT_EMAIL_PROVIDER picks one when both
are set), ALERT_FROM_EMAIL (a sender verified with that provider), ALERT_LINK_SECRET (signs the one-click
unsubscribe links; the API verifies them with the same value) and MARDEN_APP_URL (the web origin; /api/*
there is proxied to the API, so screen links and the unsubscribe link share it). Anything missing makes
`EmailConfig.configured` false and `reason` names the variables, never their values.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

PROVIDERS = ("postmark", "resend")
KEY_VARS = {"postmark": "POSTMARK_TOKEN", "resend": "RESEND_API_KEY"}
MIN_SECRET_LENGTH = 32


def _get(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


@dataclass(frozen=True)
class EmailConfig:
    provider: str | None
    api_key: str | None = field(default=None, repr=False)
    from_email: str | None = None
    link_secret: str | None = field(default=None, repr=False)
    app_url: str | None = None
    missing: tuple[str, ...] = ()

    @property
    def configured(self) -> bool:
        return not self.missing

    @property
    def reason(self) -> str | None:
        if not self.missing:
            return None
        return "email is not configured: set " + ", ".join(self.missing)


def email_config(env: Mapping[str, str] | None = None) -> EmailConfig:
    env = os.environ if env is None else env
    missing: list[str] = []
    chosen = (_get(env, "ALERT_EMAIL_PROVIDER") or "").lower() or None
    if chosen is not None and chosen not in PROVIDERS:
        missing.append("ALERT_EMAIL_PROVIDER (postmark or resend)")
        chosen = None
    keys = {p: _get(env, var) for p, var in KEY_VARS.items()}
    provider = chosen or next((p for p in PROVIDERS if keys[p]), None)
    api_key = keys[provider] if provider else None
    if provider is None:
        missing.append("POSTMARK_TOKEN or RESEND_API_KEY")
    elif api_key is None:
        missing.append(KEY_VARS[provider])
    from_email = _get(env, "ALERT_FROM_EMAIL")
    if from_email is None or "@" not in from_email:
        missing.append("ALERT_FROM_EMAIL")
        from_email = None
    secret = _get(env, "ALERT_LINK_SECRET")
    if secret is None or len(secret) < MIN_SECRET_LENGTH:
        missing.append(f"ALERT_LINK_SECRET (at least {MIN_SECRET_LENGTH} characters)")
        secret = None
    app_url = _get(env, "MARDEN_APP_URL")
    if app_url is None or not app_url.startswith(("https://", "http://")):
        missing.append("MARDEN_APP_URL")
        app_url = None
    return EmailConfig(
        provider=provider,
        api_key=api_key,
        from_email=from_email,
        link_secret=secret,
        app_url=app_url.rstrip("/") if app_url else None,
        missing=tuple(missing),
    )
