"""Materiality of an event (00-global "Materiality and alerts"), for the alert engine and the review queue.

    result = is_material(event, before, after)
    if result:                      # truthy when material
        result.codes, result.reasons, result.severity

An event is material when it moves projected completion 3+ months, turns the cushion negative, denies or
withdraws a power source, comes with a tenant notice, or triggers a lender action; a change of a chain
link's status (the status model's output before vs after) is material too (ARCHITECTURE: facts that
change a chain status wait for a person). `event` is an `event` row or a dict with at least `title`
(optionally `text`, `kind`). `before` / `after` describe the subject around the event, any key may be
missing: `p50` ('YYYY-MM', the cushion model's projected completion), `cushion_months` (number) and
`chain` (ChainLink[] from the status model). The text tests are the status model's
(`marden.models.status_text`), so an event and a chain link are judged by the same words; a scheduled
step (kind 'catalyst') has not happened, so only its before / after states are tested.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from marden.models import status_dates as dates
from marden.models import status_text as tx

SLIP_MONTHS = 3
SEVERITY = {"none": 0, "green": 1, "amber": 2, "red": 3}

# code -> (what it means, alert type in AlertSettings.types or None, severity for SiteDetail.materialEvents)
RULE_CODES: dict[str, tuple[str, str | None, str]] = {
    "M1": ("Moves projected completion 3+ months (slip of 3+ months)", "milestone", "amber"),
    "M2": ("Turns the cushion negative (cushion below zero)", "cushion", "red"),
    "M3": ("Denies or withdraws a power source", "power", "red"),
    "M4": ("Comes with a tenant notice (force majeure, termination, contract cut)", "tenant", "red"),
    "M5": ("Triggers a lender action (default notice, acceleration, waiver, draw stop, stalled syndication)",
           None, "red"),
    "E1": ("End user: its plan needs new funding", "tenant", "amber"),
    "E2": ("End user: contract cut, or end user exits", "tenant", "red"),
    "T1": ("Tenant: rated BBB- or lower, or must post contested collateral", "tenant", "amber"),
    "T2": ("Tenant: BBB- or lower and power denied or withdrawn, or force majeure filed", "tenant", "red"),
    "D1": ("Developer: in-service date slips 3+ months", "milestone", "amber"),
    "D2": ("Developer: construction halted", "construction_stall", "red"),
    "L1": ("Lenders: Marden price 95-98, or syndication stalled", "price_or_mark", "amber"),
    "L2": ("Lenders: Marden price below 95, or default notice", "price_or_mark", "red"),
}

TENANT_NOTICE = re.compile(
    r"\b(?:notice of (?:termination|non-renewal|default|delay)|termination notice|non-renewal notice"
    r"|(?:tenant|lessee|customer)\b[^;.]{0,30}\bnotice)\b",
    re.I,
)
LENDER_ACTION = re.compile(
    r"\b(?:waiver|forbearance|reservation of rights|draw stop|stop(?:ped)? (?:funding|draws)|cash sweep|amendment"
    r"|accelerat\w*\b(?!\s+filers?\b)|enforc\w*)\b",  # a 'large accelerated filer' is an SEC filer status
    re.I,
)
# A lender action needs the debt in the same clause: a docket's 'request for waiver' is not one.
DEBT_CONTEXT = re.compile(r"\b(?:lenders?|loans?|credit (?:agreement|facility)|covenants?|noteholders?|bondholders?"
                          r"|indenture|DDTL|term loan|debt|facility|arrangers?|agent)\b", re.I)
WITHDRAWN_STATUS = re.compile(r"^\s*(?:WITHDRAWN|CANCEL+ED|DEACTIVATED|TERMINATED)\b", re.I)
QUEUE_KINDS = frozenset({"queue", "interconnection"})
# A scheduled step ('CoreWeave Q3 10-Q: leverage covenant recomputed') has not happened: its text is not
# tested (as in the status model's inputs); only the before / after states can make it material.
SCHEDULED_KINDS = frozenset({"catalyst"})


@dataclass
class Materiality:
    material: bool
    codes: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.material

    @property
    def severity(self) -> str:
        return max((RULE_CODES[c][2] for c in self.codes if c in RULE_CODES), key=lambda s: SEVERITY.get(s, 0),
                   default="none")

    @property
    def alert_types(self) -> list[str]:
        return sorted({t for c in self.codes if c in RULE_CODES and (t := RULE_CODES[c][1])})


def _month(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    period = dates.first_period(value)
    return period.end if period else None


def _event_text(event: Any) -> tuple[str, str]:
    get = event.get if isinstance(event, dict) else (lambda k, d=None: getattr(event, k, d))
    text = " ".join(str(v) for v in (get("title"), get("text")) if v)
    return text, str(get("kind") or "")


def _links(state: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    return {link["role"]: link for link in (state or {}).get("chain") or [] if isinstance(link, dict)}


def is_material(event: Any, before: dict[str, Any] | None = None, after: dict[str, Any] | None = None) -> Materiality:
    """Whether an event is material, with the rule codes (RULE_CODES) and one reason per code."""
    text, kind = _event_text(event)
    if kind in SCHEDULED_KINDS:
        text = ""
    codes: list[str] = []
    reasons: list[str] = []

    def add(code: str, reason: str) -> None:
        if code not in codes:
            codes.append(code)
            reasons.append(f"{code}: {reason}")

    p0, p1 = _month((before or {}).get("p50")), _month((after or {}).get("p50"))
    if p0 is not None and p1 is not None and abs(p1 - p0) >= SLIP_MONTHS:
        add("M1", f"projected completion moved {p1 - p0:+d} months "
                  f"({(before or {}).get('p50')} -> {(after or {}).get('p50')})")
    stated = dates.stated_slip(text)
    if stated is not None and abs(stated) >= SLIP_MONTHS:
        add("M1", f"the event states a {stated}-month slip")
    c0, c1 = (before or {}).get("cushion_months"), (after or {}).get("cushion_months")
    if isinstance(c1, int | float) and c1 < 0 and not (isinstance(c0, int | float) and c0 < 0):
        add("M2", f"cushion {'n/d' if c0 is None else c0} -> {c1} months")
    if (hit := tx.power_denied(text)) is not None:
        add("M3", hit.clause)
    elif kind in QUEUE_KINDS and WITHDRAWN_STATUS.search(text):
        add("M3", text)
    for detect in (tx.force_majeure, tx.contract_cut):
        if (hit := detect(text)) is not None:
            add("M4", hit.clause)
    if "M4" not in codes and (m := TENANT_NOTICE.search(text)):
        add("M4", m.group(0))
    for detect in (tx.default_notice, tx.syndication_stalled):
        if (hit := detect(text)) is not None:
            add("M5", hit.clause)
    if "M5" not in codes:
        for clause in tx.clauses(text):
            if (m := LENDER_ACTION.search(clause)) and DEBT_CONTEXT.search(clause):
                add("M5", m.group(0))
                break
    old, new = _links(before), _links(after)
    for role, link in new.items():
        prev = old.get(role)
        if prev and prev.get("status") != link.get("status") and link.get("status") in SEVERITY:
            add(link.get("ruleCode") or prev.get("ruleCode") or role,
                f"{role} link {prev.get('status')} -> {link.get('status')}")
    return Materiality(bool(codes), codes, reasons)


def rule_code_table() -> list[dict[str, str | None]]:
    """RULE_CODES as rows (for docs and the Track record screen)."""
    return [{"code": c, "rule": r, "alert": a, "severity": s} for c, (r, a, s) in RULE_CODES.items()]
