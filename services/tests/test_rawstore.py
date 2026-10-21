from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from marden.core import rawstore
from marden.core.config import Settings, get_settings


def test_local_round_trip_is_content_addressed(tmp_path: Path):
    store = rawstore.LocalStore(tmp_path)
    data = b"<xml>N-PORT</xml>"
    sha = hashlib.sha256(data).hexdigest()
    key = rawstore.put(data, source_id="sec_edgar", url="https://www.sec.gov/x", content_type="text/xml",
                       store=store)
    assert key == f"raw/sec_edgar/{sha[:2]}/{sha}"
    assert (tmp_path / key).read_bytes() == data
    assert rawstore.get(key, store=store) == data
    # same bytes again: same key, file untouched
    mtime = (tmp_path / key).stat().st_mtime_ns
    assert rawstore.put(data, source_id="sec_edgar", url="https://www.sec.gov/y", content_type=None,
                        store=store) == key
    assert (tmp_path / key).stat().st_mtime_ns == mtime
    assert not list((tmp_path / key).parent.glob(".incoming-*"))


def test_get_detects_tampering(tmp_path: Path):
    store = rawstore.LocalStore(tmp_path)
    key = rawstore.put(b"original", source_id="ferc", url="https://elibrary.ferc.gov/x", content_type=None,
                       store=store)
    (tmp_path / key).write_bytes(b"changed")
    with pytest.raises(rawstore.IntegrityError):
        rawstore.get(key, store=store)


def test_missing_key_and_bad_keys(tmp_path: Path):
    store = rawstore.LocalStore(tmp_path)
    with pytest.raises(KeyError):
        rawstore.get(rawstore.make_key("ferc", "0" * 64), store=store)
    with pytest.raises(ValueError):
        rawstore.get("raw/../../etc/passwd", store=store)
    with pytest.raises(ValueError):
        rawstore.make_key("Bad Source", "0" * 64)
    with pytest.raises(TypeError):
        rawstore.put("text", source_id="ferc", url="u", content_type=None, store=store)  # type: ignore[arg-type]


def test_default_store_is_local_raw_dir_without_r2(raw_dir: Path):
    settings = get_settings()
    assert not settings.has_r2()
    store = rawstore.get_store()
    assert isinstance(store, rawstore.LocalStore) and store.root == raw_dir
    key = rawstore.put(b"default store", source_id="sec_edgar", url="https://www.sec.gov/z", content_type=None)
    assert (raw_dir / key).is_file()


def test_r2_selected_when_credentials_present():
    # Configuration only: builds the S3 client for the account endpoint, makes no request.
    settings = Settings(r2_account_id="acct123", r2_access_key_id="id", r2_secret_access_key="secret")
    store = rawstore.get_store(settings)
    assert isinstance(store, rawstore.R2Store)
    assert store.endpoint_url == "https://acct123.r2.cloudflarestorage.com"
    assert store.bucket == "core-raw"
    rawstore.get_store(get_settings())  # restore the process default
