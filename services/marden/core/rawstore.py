"""Raw store: every fetched file kept unchanged, content-addressed by sha256 (ARCHITECTURE rule 3).

Keys are `raw/<source_id>/<sha[:2]>/<sha>`. The same bytes from the same source are stored once.
R2 (Cloudflare, S3 API) is used when R2 credentials are configured; otherwise the local directory
MARDEN_RAW_DIR. The `document` table is the index (url, content type, fetch time) for both.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from marden.core.config import Settings, deploy_marker, get_settings
from marden.core.jsonutil import sha256_hex

_SOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9_\-]*$")
_KEY = re.compile(r"^raw/[a-z0-9][a-z0-9_\-]*/[0-9a-f]{2}/[0-9a-f]{64}$")


class RawStoreError(RuntimeError):
    pass


class IntegrityError(RawStoreError):
    """Stored bytes do not hash to the sha256 in their key."""


def make_key(source_id: str, sha: str) -> str:
    if not _SOURCE_ID.match(source_id):
        raise ValueError(f"invalid source_id for a storage key: {source_id!r}")
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ValueError(f"not a sha256 hex digest: {sha!r}")
    return f"raw/{source_id}/{sha[:2]}/{sha}"


def _check_key(key: str) -> str:
    if not _KEY.match(key):
        raise ValueError(f"not a raw store key: {key!r}")
    return key.rsplit("/", 1)[1]


def _verify(key: str, data: bytes) -> bytes:
    if sha256_hex(data) != _check_key(key):
        raise IntegrityError(f"raw object {key} does not match its sha256")
    return data


class RawStore(Protocol):
    def exists(self, key: str) -> bool: ...
    def write(self, key: str, data: bytes, *, content_type: str | None, metadata: dict[str, str]) -> None: ...
    def read(self, key: str) -> bytes: ...


class LocalStore:
    """Files under a root directory; writes are atomic (temp file + rename)."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def path(self, key: str) -> Path:
        _check_key(key)
        return self.root / key

    def exists(self, key: str) -> bool:
        return self.path(key).is_file()

    def write(self, key: str, data: bytes, *, content_type: str | None, metadata: dict[str, str]) -> None:
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".incoming-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def read(self, key: str) -> bytes:
        path = self.path(key)
        if not path.is_file():
            raise KeyError(key)
        return path.read_bytes()


class R2Store:
    """Cloudflare R2 through its S3-compatible API."""

    def __init__(self, settings: Settings):
        import boto3
        from botocore.config import Config

        self.bucket = settings.r2_bucket_raw
        self.endpoint_url = settings.r2_endpoint_url()
        self.client: Any = boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            region_name="auto",
            config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"}),
        )

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        _check_key(key)
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        return True

    def write(self, key: str, data: bytes, *, content_type: str | None, metadata: dict[str, str]) -> None:
        _check_key(key)
        extra: dict[str, Any] = {"Metadata": metadata}
        if content_type:
            extra["ContentType"] = content_type
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, **extra)

    def read(self, key: str) -> bytes:
        from botocore.exceptions import ClientError

        _check_key(key)
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                raise KeyError(key) from exc
            raise
        return obj["Body"].read()


_lock = threading.Lock()
_store: RawStore | None = None
_store_settings: Settings | None = None


def get_store(settings: Settings | None = None) -> RawStore:
    """The configured store: R2 when R2 credentials are set, else LocalStore(MARDEN_RAW_DIR).

    A deployed service (Railway) never falls back to local disk: its filesystem is replaced on every
    deploy, so `document` rows would point at files that are gone. Without R2 it raises, and the
    collector's run records "R2 is not configured" on its source instead of storing anything.
    """
    global _store, _store_settings
    settings = settings or get_settings()
    if not settings.has_r2():
        marker = deploy_marker()
        if marker:
            raise RawStoreError(
                "R2 is not configured (R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY): a deployed "
                f"service ({marker} is set) keeps no raw files on local disk"
            )
    with _lock:
        if _store is None or _store_settings != settings:
            _store = R2Store(settings) if settings.has_r2() else LocalStore(settings.raw_dir)
            _store_settings = settings
        return _store


def _ascii_meta(value: str) -> str:
    # S3/R2 user metadata must be ASCII; keep URLs readable but safe.
    return quote(value, safe=":/?&=%#@+,;~!$'()*[]")[:1024]


def put(data: bytes, *, source_id: str, url: str, content_type: str | None, store: RawStore | None = None) -> str:
    """Store bytes once and return the storage key. Re-putting the same bytes is a no-op."""
    if not isinstance(data, bytes | bytearray | memoryview):
        raise TypeError("rawstore.put takes bytes")
    data = bytes(data)
    key = make_key(source_id, sha256_hex(data))
    store = store or get_store()
    if not store.exists(key):
        meta = {"source-id": source_id, "url": _ascii_meta(url)}
        store.write(key, data, content_type=content_type, metadata=meta)
    return key


def get(key: str, *, store: RawStore | None = None) -> bytes:
    """Read stored bytes, verifying they still hash to the sha256 in the key."""
    store = store or get_store()
    return _verify(key, store.read(key))
