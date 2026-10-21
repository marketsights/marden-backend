"""Typed settings read from the environment.

Every setting comes from an environment variable listed in `.env.example`. Credentials are never
given defaults; code checks `settings.has_*()` and records a blocked/failed source instead of
inventing data when a credential is missing (ARCHITECTURE hard rules 1-2).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RAW_DIR = REPO_ROOT / ".raw"
DEFAULT_R2_BUCKET_RAW = "marden-raw"
DEFAULT_SEC_USER_AGENT = "Marden research cody@marden.ai"


class ConfigError(RuntimeError):
    """A required setting is missing or invalid."""


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


@dataclass(frozen=True)
class Settings:
    database_url: str | None = field(default=None, repr=False)
    raw_dir: Path = DEFAULT_RAW_DIR
    r2_account_id: str | None = None
    r2_access_key_id: str | None = field(default=None, repr=False)
    r2_secret_access_key: str | None = field(default=None, repr=False)
    r2_bucket_raw: str = DEFAULT_R2_BUCKET_RAW
    sec_user_agent: str = DEFAULT_SEC_USER_AGENT
    anthropic_api_key: str | None = field(default=None, repr=False)
    fred_api_key: str | None = field(default=None, repr=False)
    finra_client_id: str | None = field(default=None, repr=False)
    finra_client_secret: str | None = field(default=None, repr=False)
    seed_dir: Path | None = None

    @classmethod
    def from_env(cls) -> Settings:
        raw_dir = _env("MARDEN_RAW_DIR")
        seed_dir = _env("MARDEN_SEED_DIR")
        return cls(
            database_url=_env("DATABASE_URL"),
            raw_dir=Path(raw_dir).expanduser() if raw_dir else DEFAULT_RAW_DIR,
            r2_account_id=_env("R2_ACCOUNT_ID"),
            r2_access_key_id=_env("R2_ACCESS_KEY_ID"),
            r2_secret_access_key=_env("R2_SECRET_ACCESS_KEY"),
            r2_bucket_raw=_env("R2_BUCKET_RAW") or DEFAULT_R2_BUCKET_RAW,
            sec_user_agent=_env("SEC_USER_AGENT") or DEFAULT_SEC_USER_AGENT,
            anthropic_api_key=_env("ANTHROPIC_API_KEY"),
            fred_api_key=_env("FRED_API_KEY"),
            finra_client_id=_env("FINRA_CLIENT_ID"),
            finra_client_secret=_env("FINRA_CLIENT_SECRET"),
            seed_dir=Path(seed_dir).expanduser() if seed_dir else None,
        )

    # ------------------------------------------------------------ credential checks
    def has_database(self) -> bool:
        return self.database_url is not None

    def has_r2(self) -> bool:
        return all((self.r2_account_id, self.r2_access_key_id, self.r2_secret_access_key))

    def has_sec_user_agent(self) -> bool:
        # SEC requires a declared agent with a contact address.
        return "@" in self.sec_user_agent

    def has_anthropic(self) -> bool:
        return self.anthropic_api_key is not None

    def has_fred(self) -> bool:
        return self.fred_api_key is not None

    def has_finra(self) -> bool:
        return self.finra_client_id is not None and self.finra_client_secret is not None

    def has_seed_dir(self) -> bool:
        return self.seed_dir is not None and self.seed_dir.is_dir()

    # ------------------------------------------------------------ required values
    def require_database_url(self) -> str:
        if self.database_url is None:
            raise ConfigError("DATABASE_URL is not set (see .env.example)")
        return self.database_url

    def require_seed_dir(self) -> Path:
        if not self.has_seed_dir():
            raise ConfigError("MARDEN_SEED_DIR is not set or is not a directory")
        assert self.seed_dir is not None
        return self.seed_dir

    def r2_endpoint_url(self) -> str:
        if not self.r2_account_id:
            raise ConfigError("R2_ACCOUNT_ID is not set")
        return f"https://{self.r2_account_id}.r2.cloudflarestorage.com"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


def reload_settings() -> Settings:
    """Re-read the environment (tests and long-lived shells after changing env vars)."""
    get_settings.cache_clear()
    return get_settings()
