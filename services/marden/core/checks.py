"""Code validators shared by the fact, price, event and holding writers (spec 22, layer 3)."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

TAGS = frozenset({"F", "R", "M"})
SUBJECT_TYPES = frozenset(
    {"site", "campus", "building", "plant", "unit", "instrument", "entity", "contract", "obligation", "holding",
     "series",  # series: time series that are not instruments (rate curves, indexes); subject_id = series id
     "queue_project",  # an ISO/RTO interconnection queue entry; subject_id = queue_project.id ('<iso>:<queue id>')
     "eia_generator"}  # an EIA-860M generator; subject_id '<EIA plant id>_<generator id>' (collectors/eia_860m)
)
SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")
PRODUCER = re.compile(r"^[a-z][a-z_]*:\S+$")
EXTRACTED_PREFIX = "extractor:"
# Producers whose values were read from documents by a model ('extractor:...' and the agent system's
# 'agent:<area>'): they need the exact quote and a document, and publish only once verified.
EXTRACTED_PREFIXES = (EXTRACTED_PREFIX, "agent:")
MODEL_PREFIX = "model:"
# The latest calendar date anywhere on Earth (UTC+14): a date after it is in the future everywhere.
MAX_UTC_OFFSET = timedelta(hours=14)


class ValidationError(ValueError):
    """A write was refused by a code validator."""


def latest_today() -> date:
    return (datetime.now(UTC) + MAX_UTC_OFFSET).date()


def as_date(value: date | str, name: str) -> date:
    if isinstance(value, datetime):
        raise ValidationError(f"{name} must be a date, not a datetime")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise ValidationError(f"{name} must be an ISO date, got {value!r}") from None
    raise ValidationError(f"{name} must be a date or ISO date string, got {type(value).__name__}")


def not_future(value: date | str, name: str) -> date:
    day = as_date(value, name)
    if day > latest_today():
        raise ValidationError(f"{name} {day.isoformat()} is in the future")
    return day


def tag(value: str) -> str:
    if value not in TAGS:
        raise ValidationError(f"tag must be F, R or M, got {value!r}")
    return value


def subject_type(value: str) -> str:
    if value not in SUBJECT_TYPES:
        raise ValidationError(f"subject_type must be one of {sorted(SUBJECT_TYPES)}, got {value!r}")
    return value


def non_empty(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} is required")
    return value


def snake(value: str, name: str) -> str:
    if not isinstance(value, str) or not SNAKE.match(value):
        raise ValidationError(f"{name} must be snake_case, got {value!r}")
    return value


def producer(value: str) -> str:
    if not isinstance(value, str) or not PRODUCER.match(value):
        raise ValidationError(f"producer must look like 'kind:name' (e.g. 'collector:nport'), got {value!r}")
    return value


def is_extracted(producer_name: str) -> bool:
    return producer_name.startswith(EXTRACTED_PREFIXES)


def model_rules(tag_value: str, method_version: str | None, producer_name: str) -> None:
    if tag_value == "M" and not (method_version and method_version.strip()):
        raise ValidationError("model (M) values require method_version")
    if producer_name.startswith(MODEL_PREFIX) and tag_value != "M":
        raise ValidationError("model producers write tag M")


def confidence(value: float | None) -> float | None:
    if value is None:
        return None
    if not 0.0 <= float(value) <= 1.0:
        raise ValidationError(f"confidence must be in [0, 1], got {value!r}")
    return float(value)


def decimal_text(value: Any, name: str) -> str | None:
    """Canonical decimal text (99.50 -> '99.5') for dedupe keys; None stays None."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValidationError(f"{name} must be a number")
    try:
        dec = Decimal(str(value))
    except InvalidOperation:
        raise ValidationError(f"{name} must be a number, got {value!r}") from None
    if not dec.is_finite():
        raise ValidationError(f"{name} must be finite")
    text = format(dec.normalize(), "f")
    return "0" if text in ("-0", "0") else text
