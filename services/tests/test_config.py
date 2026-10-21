from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from marden.core import config


@pytest.fixture
def env(monkeypatch) -> Iterator[pytest.MonkeyPatch]:
    yield monkeypatch
    monkeypatch.undo()
    config.reload_settings()


def test_defaults(env):
    for name in ("MARDEN_RAW_DIR", "R2_BUCKET_RAW", "SEC_USER_AGENT", "ANTHROPIC_API_KEY", "FRED_API_KEY",
                 "FINRA_CLIENT_ID", "FINRA_CLIENT_SECRET", "MARDEN_SEED_DIR"):
        env.delenv(name, raising=False)
    s = config.reload_settings()
    assert s.raw_dir == config.REPO_ROOT / ".raw"
    assert s.r2_bucket_raw == "marden-raw"
    assert s.sec_user_agent == "Marden research cody@marden.ai" and s.has_sec_user_agent()
    assert not (s.has_r2() or s.has_anthropic() or s.has_fred() or s.has_finra() or s.has_seed_dir())


def test_credentials_and_paths(env, tmp_path: Path):
    env.setenv("R2_ACCOUNT_ID", "acct")
    env.setenv("R2_ACCESS_KEY_ID", "key-id")
    env.setenv("R2_SECRET_ACCESS_KEY", "key-secret")
    env.setenv("FINRA_CLIENT_ID", "client")
    env.setenv("MARDEN_SEED_DIR", str(tmp_path))
    env.setenv("FRED_API_KEY", "  ")  # blank counts as unset
    s = config.reload_settings()
    assert s.has_r2() and s.r2_endpoint_url() == "https://acct.r2.cloudflarestorage.com"
    assert not s.has_finra()  # secret missing
    assert not s.has_fred()
    assert s.require_seed_dir() == tmp_path
    assert "key-secret" not in repr(s) and "key-id" not in repr(s)


def test_missing_database_url_is_a_clear_error(env):
    env.delenv("DATABASE_URL")
    with pytest.raises(config.ConfigError, match="DATABASE_URL"):
        config.reload_settings().require_database_url()
