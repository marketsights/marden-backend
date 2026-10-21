"""Canonical JSON: one stable text form per value, used for dedupe keys, jsonb writes and hashes."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb


def to_jsonable(value: Any) -> Any:
    """Normalize a value to plain JSON types.

    Dates become ISO strings, Decimals become numbers, tuples become lists, and integral floats
    become ints so that 2200, 2200.0 and Decimal('2200.00') produce the same canonical text.
    """
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError("NaN and infinity are not valid JSON values")
        return int(value) if value.is_integer() and abs(value) < 2**53 else value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("NaN and infinity are not valid JSON values")
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"JSON object keys must be strings, got {type(key).__name__}")
            out[key] = to_jsonable(item)
        return out
    if isinstance(value, list | tuple):
        return [to_jsonable(item) for item in value]
    raise TypeError(f"value of type {type(value).__name__} is not JSON serializable")


def canonical_json(value: Any) -> str:
    """Sorted keys, no whitespace, UTF-8 text: the same value always gives the same string."""
    return json.dumps(
        to_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def dumps(value: Any) -> str:
    return canonical_json(value)


def jsonb(value: Any) -> Jsonb:
    """Wrap a value for a jsonb parameter, normalized the same way as canonical_json."""
    return Jsonb(to_jsonable(value), dumps=dumps)


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def hash_parts(*parts: Any) -> str:
    """sha256 of the canonical JSON array of parts (unambiguous, unlike joining with a separator)."""
    return sha256_hex(canonical_json(list(parts)))
