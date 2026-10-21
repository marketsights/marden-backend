"""Code templates for alert text: rule labels, number and date formats (00-global "Formats").

Every headline and body line is filled in from computed numbers and fact refs; nothing here is written by
a model. Signed changes carry a sign and the true minus sign; prices one decimal; cushion whole months.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from marden.models.materiality import RULE_CODES, SEVERITY

MINUS = "−"
ARROW = "→"
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
TAG_LABEL = {"F": "filed", "R": "reported", "M": "Marden model"}
ROLE_LABEL = {"end_user": "End user", "tenant": "Tenant", "developer": "Developer", "lenders": "Lenders"}

# Rules the engine adds to the materiality codes (marden.models.materiality.RULE_CODES):
# code -> (what it means, AlertSettings type, default severity).
ENGINE_RULES: dict[str, tuple[str, str, str]] = {
    "C3": ("Cushion under 3 months", "cushion", "amber"),
    "CX": ("Cushion back above zero or above 3 months", "cushion", "green"),
    "PX": ("Marden price moved by at least your price-move threshold", "price_or_mark", "amber"),
    "MK": ("Fund mark filed (N-PORT or BDC schedule)", "price_or_mark", "green"),
    "RT": ("Rating action (a new rating differs from the last one on record)", "tenant", "amber"),
    "OP": ("Opposition event (county vote, moratorium, lawsuit)", "opposition", "amber"),
}
SHORT: dict[str, str] = {
    "M1": "Schedule slip", "M2": "Cushion below zero", "M3": "Power denied or withdrawn",
    "M4": "Tenant notice", "M5": "Lender action", "E1": "End user funding", "E2": "End user exits",
    "T1": "Tenant credit", "T2": "Tenant credit and power", "D1": "Developer slip", "D2": "Construction halted",
    "L1": "Lenders: price 95–98", "L2": "Lenders: price below 95", "C3": "Cushion under 3 months",
    "CX": "Cushion recovered", "PX": "Price threshold", "MK": "Fund mark filed", "RT": "Rating action",
    "OP": "Opposition",
}
ROLE_TYPES = {"end_user": "tenant", "tenant": "tenant", "developer": "milestone", "lenders": "price_or_mark"}


def rule_text(code: str) -> str:
    if code in RULE_CODES:
        return RULE_CODES[code][0]
    if code in ENGINE_RULES:
        return ENGINE_RULES[code][0]
    return f"{ROLE_LABEL.get(code, code)} link status changed"


def rule_label(code: str) -> str:
    """Alert.ruleLabel: the code and a short name ('M2 Cushion below zero')."""
    short = SHORT.get(code)
    return f"{code} {short}" if short else f"{ROLE_LABEL.get(code, code)} link"


def alert_type(code: str, role: str | None = None) -> str:
    """The AlertSettings type a rule belongs to. Lender actions (M5, no type of their own) count as
    price_or_mark, the lenders' alert type; a link change without a rule code follows its role."""
    if code in RULE_CODES and RULE_CODES[code][1]:
        return str(RULE_CODES[code][1])
    if code in ENGINE_RULES:
        return ENGINE_RULES[code][1]
    if code == "M5":
        return "price_or_mark"
    return ROLE_TYPES.get(role or code, "tenant")


def rule_severity(code: str) -> str:
    if code in RULE_CODES:
        return RULE_CODES[code][2]
    if code in ENGINE_RULES:
        return ENGINE_RULES[code][2]
    return "amber"


def worst(severities: list[str]) -> str:
    return max(severities, key=lambda s: SEVERITY.get(s, 0), default="green")


def primary_code(codes: list[str], severity_of: dict[str, str]) -> str:
    """The first code with the worst severity (codes keep the order the rules found them in)."""
    top = worst([severity_of[c] for c in codes])
    return next(c for c in codes if severity_of[c] == top)


def num(value: float, digits: int = 1) -> str:
    text = f"{abs(value):.{digits}f}"
    return f"{MINUS}{text}" if value < 0 and float(text) != 0 else text


def signed(value: float, digits: int = 1) -> str:
    text = f"{abs(value):.{digits}f}"
    if float(text) == 0:
        return text
    return f"+{text}" if value > 0 else f"{MINUS}{text}"


def whole(value: Any) -> str:
    """Cushion months: a whole number with the true minus sign, n/d when unknown."""
    if not isinstance(value, int | float) or isinstance(value, bool):
        return "n/d"
    return num(round(value), 0)


def month_label(value: Any) -> str:
    """'2029-01' -> "Jan '29"; anything else as given (n/d when empty)."""
    if not isinstance(value, str) or not value:
        return "n/d"
    try:
        year, month = int(value[:4]), int(value[5:7])
    except ValueError:
        return value
    if len(value) >= 7 and value[4] == "-" and 1 <= month <= 12:
        return f"{MONTHS[month - 1]} '{year % 100:02d}"
    return value


def day(value: Any) -> str:
    """A date as 'Oct 21, 2026' (emails are read later, so the year is always shown)."""
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value[:10])
        except ValueError:
            return value
    if isinstance(value, date):
        return f"{MONTHS[value.month - 1]} {value.day}, {value.year}"
    return "n/d"


def clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def value_text(value: Any) -> str:
    """A fact value (JSON) as one line of text."""
    if value is None:
        return "n/d"
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "; ".join(f"{k}: {value_text(v)}" for k, v in value.items())
    if isinstance(value, list):
        return ", ".join(value_text(v) for v in value)
    return str(value)
