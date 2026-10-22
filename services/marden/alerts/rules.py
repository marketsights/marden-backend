"""Findings: what one trigger (a published event, a rating fact, a model-output change) says, as alert text.

Materiality is `marden.models.materiality.is_material` on the event text and on the before / after
states (projected completion, cushion, chain links); the engine adds cushion bands (C3, CX), Marden price
moves at the user's threshold (PX), fund marks filed (MK), rating actions (RT) and opposition events (OP).
Each Finding carries its codes, AlertSettings types, severity, a headline and body built by the code
templates in `marden.alerts.text`, the numbers behind them (`facts`) and the fact refs (`sources`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from marden.alerts import text as t
from marden.core.jsonutil import canonical_json
from marden.models.materiality import SEVERITY, is_material
from marden.models.status_labels import CHAIN_CLAUSE, LINK_CLAUSE, chain_label, link_label

THRESHOLDS = (1, 2, 3)  # AlertSettings.priceThresholdPts
MARKED_HIGH_PTS = 5  # spec 11: "fund marks 5+ pts high"
MAX_LISTED = 8


@dataclass
class Finding:
    codes: list[str]
    severity: str
    headline: str
    body: str
    tag: str
    ref_url: str
    alert_types: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    sources: list[dict[str, Any]] = field(default_factory=list)
    part: str = ""  # tells several findings of one trigger apart (a link role, a price threshold)
    threshold_pts: int | None = None  # price moves reach only users whose threshold this is

    @property
    def code(self) -> str:
        return self.codes[0]

    @property
    def rule_label(self) -> str:
        return t.rule_label(self.code)


def _method_line(model: str, method: str | None) -> str:
    return f"Marden {model} model, {method}." if method else f"Marden {model} model."


def _model_source(name: str, method: str | None, as_of: date, url: str) -> dict[str, Any]:
    return {"name": name, "tag": "M", "asOf": as_of.isoformat(), "methodVersion": method, "url": url}


def _sourced_ref(value: Any, key: str) -> dict[str, Any] | None:
    """A Sourced<T> from a model output as a source entry (None when it has no source)."""
    if not isinstance(value, dict) or not (value.get("sourceUrl") or value.get("sourceName")):
        return None
    out = {"key": key, "name": value.get("sourceName"), "url": value.get("sourceUrl"), "tag": value.get("tag"),
           "asOf": value.get("asOf"), "value": t.value_text(value.get("value"))}
    return {k: v for k, v in out.items() if v is not None}


# ------------------------------------------------------------------ events
def event_finding(event: dict[str, Any], name: str, ref_url: str) -> Finding | None:
    """One finding per material (or opposition) event; several codes share it."""
    m = is_material(event)
    codes, reasons = list(m.codes), list(m.reasons)
    severity_of = {c: t.rule_severity(c) for c in codes}
    if event.get("kind") == "opposition":
        codes.append("OP")
        # the collector's own grade; an ungraded one ('none') is news, not a warning
        severity_of["OP"] = event["severity"] if event.get("severity") in ("red", "amber") else "green"
        reasons.append(f"OP: {event.get('kind')} event")
    if not codes:
        return None
    primary = t.primary_code(codes, severity_of)
    codes = [primary, *[c for c in codes if c != primary]]
    lines = [f"{t.SHORT.get(primary, primary)} ({primary}): {t.rule_text(primary)}."]
    lines += [f"Why it is material: {r}" for r in reasons]
    lines.append(f"Event: {event['title']}")
    tag_label = t.TAG_LABEL.get(event["tag"], event["tag"])
    lines.append(f"Dated {t.day(event['event_date'])}; {event.get('kind')}; {tag_label} ({event['tag']}), "
                 f"{event['source_name']}.")
    source = {"key": f"event:{event['id']}", "name": event["source_name"], "url": event.get("source_url"),
              "tag": event["tag"], "asOf": event["event_date"].isoformat(), "eventId": event["id"],
              "factId": event.get("fact_id"), "title": event["title"]}
    return Finding(
        codes=codes, severity=severity_of[primary], tag=event["tag"], ref_url=ref_url,
        headline=f"{name}: {t.clip(event['title'], 110)}", body="\n".join(lines),
        alert_types=sorted({t.alert_type(c) for c in codes}),
        facts={"eventId": event["id"], "eventDate": event["event_date"].isoformat(), "kind": event.get("kind"),
               "codes": codes, "reasons": reasons},
        sources=[{k: v for k, v in source.items() if v is not None}],
    )


# ------------------------------------------------------------------ chain status (status model)
def _rank(status: Any) -> int:
    return SEVERITY.get(str(status), 0)


def status_findings(name: str, slug: str, before: dict[str, Any], after: dict[str, Any], as_of: date,
                    before_as_of: date) -> list[Finding]:
    if not is_material({"title": ""}, {"chain": before.get("chain")}, {"chain": after.get("chain")}):
        return []
    old = {link["role"]: link for link in before.get("chain") or []}
    method = after.get("methodVersion")
    out: list[Finding] = []
    for link in after.get("chain") or []:
        prev = old.get(link["role"])
        if prev is None or prev.get("status") == link.get("status"):
            continue
        o, n = str(prev.get("status")), str(link.get("status"))
        if {o, n} <= {"none", "green"}:
            continue  # no data <-> clear: nothing for a lender to act on
        role = link["role"]
        code = link.get("ruleCode") or prev.get("ruleCode") or role
        worse = _rank(n) > _rank(o)
        severity = n if worse and n in ("amber", "red") else "green"
        label = t.ROLE_LABEL.get(role, role)
        moved = f"{link_label(o)} {t.ARROW} {link_label(n)}"
        lines = [f"{label} ({link.get('party')}): {moved} between {t.day(before_as_of)} and {t.day(as_of)}."]
        chain_before, chain_after = before.get("status"), after.get("status")
        chain_moved = chain_before != chain_after
        if chain_moved:
            lines.append(f"Site status {chain_label(chain_before)} {t.ARROW} {chain_label(chain_after)}.")
        lines.append(f"{'Why' if worse else 'Was'}: {(link if worse else prev).get('reason')}")
        lines.append(f"Rule {code}: {t.rule_text(code)}.")
        lines.append(_method_line("status", method))
        sources = [s for s in (_sourced_ref((link if worse else prev).get("fact"), f"status:{slug}:{role}"),) if s]
        sources.append(_model_source("Marden status model", method, as_of, "docs/methods/status-rules-v0.1.md"))
        out.append(Finding(
            codes=[code], severity=severity, tag="M", ref_url=f"/sites/{slug}",
            headline=f"{name}: {label.lower()} link now {LINK_CLAUSE.get(n, n)}"
            + (f" ({code})" if code in t.SHORT else "")
            + (f"; site {CHAIN_CLAUSE.get(str(chain_after), str(chain_after))}" if chain_moved else ""),
            body="\n".join(lines), alert_types=[t.alert_type(code, role)], part=role,
            facts={"role": role, "party": link.get("party"), "before": o, "after": n, "ruleCode": code,
                   "chainBefore": before.get("status"), "chainAfter": after.get("status"),
                   "asOf": as_of.isoformat(), "beforeAsOf": before_as_of.isoformat()},
            sources=sources,
        ))
    return out


# ------------------------------------------------------------------ cushion model
def _number(value: Any) -> float | None:
    return value if isinstance(value, int | float) and not isinstance(value, bool) else None


def _month_index(value: Any) -> int | None:
    if isinstance(value, str) and len(value) >= 7 and value[4] == "-" and value[:4].isdigit() and value[5:7].isdigit():
        return int(value[:4]) * 12 + int(value[5:7])
    return None


def cushion_finding(name: str, slug: str, before: dict[str, Any], after: dict[str, Any], as_of: date,
                    before_as_of: date) -> Finding | None:
    c0, c1 = _number(before.get("cushion_months")), _number(after.get("cushion_months"))
    p0, p1 = before.get("p50"), after.get("p50")
    m = is_material({"title": ""}, {"p50": p0, "cushion_months": c0}, {"p50": p1, "cushion_months": c1})
    codes, reasons = list(m.codes), list(m.reasons)
    if c1 is not None and 0 <= c1 < 3 and not (c0 is not None and c0 < 3):
        codes.append("C3")
        reasons.append(f"C3: cushion {t.whole(c0)} {t.ARROW} {t.whole(c1)} months")
    if c0 is not None and c1 is not None and ((c0 < 0 <= c1) or (c0 < 3 <= c1)):
        codes.append("CX")
        reasons.append(f"CX: cushion {t.whole(c0)} {t.ARROW} {t.whole(c1)} months")
    if not codes:
        return None
    severity_of = {c: t.rule_severity(c) for c in codes}
    primary = t.primary_code(codes, severity_of)
    codes = [primary, *[c for c in codes if c != primary]]
    if primary == "M2":
        headline = f"{name} cushion below zero: {t.whole(c1)} months"
    elif primary == "C3":
        headline = f"{name} cushion under 3 months: {t.whole(c1)} months"
    elif primary == "CX":
        headline = f"{name} cushion back to {t.whole(c1)} months"
    else:
        i0, i1 = _month_index(p0), _month_index(p1)
        moved = f" moved {t.signed(i1 - i0, 0)} months to" if i0 is not None and i1 is not None else " now"
        headline = f"{name} projected completion{moved} {t.month_label(p1)}"
    method = after.get("methodVersion")
    span = f"({t.day(before_as_of)} {t.ARROW} {t.day(as_of)})"
    lines = []
    if c0 is not None or c1 is not None:
        lines.append(f"Cushion {t.whole(c0)} {t.ARROW} {t.whole(c1)} months {span}.")
    lines.append(f"Projected completion (P50) {t.month_label(p0)} {t.ARROW} {t.month_label(p1)}"
                 + ("." if lines else f" {span}."))
    outside = after.get("outside")
    if outside and (outside.get("value") if isinstance(outside, dict) else outside) is not None:
        assumed = after.get("outside_assumed")
        outside_value = outside.get("value") if isinstance(outside, dict) else outside
        lines.append(f"Lease outside date {t.value_text(outside_value)}"
                     + (f" (assumed {t.day(assumed)})" if assumed else "") + ".")
    lines += [f"Why: {r}" for r in reasons]
    lines.append(_method_line("cushion", method))
    sources = [s for s in (_sourced_ref(outside, f"cushion:{slug}:outside"),) if s]
    sources.append(_model_source("Marden cushion model", method, as_of, "docs/methods/cushion-v0.1.md"))
    return Finding(
        codes=codes, severity=severity_of[primary], tag="M", ref_url=f"/sites/{slug}?tab=schedule",
        headline=headline, body="\n".join(lines), alert_types=sorted({t.alert_type(c) for c in codes}),
        facts={"cushionBefore": c0, "cushionAfter": c1, "p50Before": p0, "p50After": p1, "codes": codes,
               "asOf": as_of.isoformat(), "beforeAsOf": before_as_of.isoformat()},
        sources=sources,
    )


# ------------------------------------------------------------------ Marden price
def price_findings(name: str, instrument_id: str, refs: dict[str, list[Any]], after: dict[str, Any],
                   as_of: date) -> tuple[list[Finding], dict[str, list[Any]]]:
    """A move at each threshold (1, 2, 3 pts) from the price at that threshold's last alert (or the first
    price the engine saw). Returns the findings and the updated references."""
    price = _number(after.get("price"))
    refs = dict(refs)
    if price is None:
        return [], refs
    out: list[Finding] = []
    for pts in THRESHOLDS:
        ref = refs.get(str(pts))
        if ref is None:
            refs[str(pts)] = [price, as_of.isoformat()]
            continue
        move = round(price - ref[0], 1)
        if abs(move) < pts:
            continue
        refs[str(pts)] = [price, as_of.isoformat()]
        method = after.get("methodVersion")
        lines = [f"Marden price {t.num(ref[0])} ({t.day(ref[1])}) {t.ARROW} {t.num(price)} ({t.day(as_of)}): "
                 f"{t.signed(move)} pts, at or beyond your {pts}-pt threshold."]
        if _number(after.get("change1w")) is not None:
            lines.append(f"1-week change {t.signed(after['change1w'])} pts.")
        band = after.get("band")
        if isinstance(band, list) and len(band) == 2 and all(_number(b) is not None for b in band):
            lines.append(f"Range {t.num(band[0])}\u2013{t.num(band[1])} (80%, model v0).")
        if after.get("tier"):
            lines.append(f"Tier: {after['tier']}.")
        lines.append(_method_line("price", method))
        out.append(Finding(
            codes=["PX"], severity="amber" if move < 0 else "green", tag="M", ref_url=f"/instruments/{instrument_id}",
            headline=f"{name} {'down' if move < 0 else 'up'} {t.num(abs(move))} pts to {t.num(price)}",
            body="\n".join(lines), alert_types=["price_or_mark"], part=f"t{pts}", threshold_pts=pts,
            facts={"price": price, "reference": ref[0], "referenceAsOf": ref[1], "movePts": move,
                   "thresholdPts": pts, "change1w": after.get("change1w"), "asOf": as_of.isoformat()},
            sources=[_model_source("Marden price model", method, as_of, "docs/methods/pricing-v0.1.md")],
        ))
    return out, refs


# ------------------------------------------------------------------ fund marks (mark_gap model)
def _gap_text(mark: dict[str, Any]) -> str:
    """gapPts = Marden price - the fund's mark (marden.models.mark_gap): negative = the fund marks above."""
    gap = _number(mark.get("gapPts"))
    if gap is None:
        return ""
    size = t.num(abs(gap))
    if float(size) == 0:
        return ", at the Marden price"
    return f", {size} pts {'above' if gap < 0 else 'below'} the Marden price"


def mark_finding(name: str, instrument_id: str, before: dict[str, Any], after: dict[str, Any],
                 as_of: date) -> Finding | None:
    """Marks for a period after the newest one the engine had seen (`filingDate` is the filing's period end;
    the first coverage is not news)."""
    seen = [m["filingDate"] for m in before.get("marks") or [] if m.get("filingDate")]
    if not seen:
        return None
    newest = max(seen)
    new = sorted((m for m in after.get("marks") or [] if (m.get("filingDate") or "") > newest
                  and _number(m.get("lastMark")) is not None), key=lambda m: (m["filingDate"], m["name"]))
    if not new:
        return None
    marks = [m["lastMark"] for m in new]
    high = [m for m in new if _number(m.get("gapPts")) is not None and m["gapPts"] <= -MARKED_HIGH_PTS]
    if len(new) == 1:
        headline = f"{name}: fund mark filed at {t.num(marks[0])} ({t.clip(new[0]['name'], 50)})"
    else:
        headline = f"{name}: {len(new)} fund marks filed, {t.num(min(marks))}\u2013{t.num(max(marks))}"
    lines = []
    for m in new[:MAX_LISTED]:
        filing = m.get("filing") or "filing"
        lines.append(f"{m['name']}: {t.num(m['lastMark'])} ({filing} for {t.day(m['filingDate'])}){_gap_text(m)}")
    if len(new) > MAX_LISTED:
        lines.append(f"\u2026and {len(new) - MAX_LISTED} more on the instrument page.")
    if _number(after.get("marden_price")) is not None:
        lines.append(f"Marden price {t.num(after['marden_price'])}.")
    if high:
        lines.append(f"{len(high)} of these marks sit {MARKED_HIGH_PTS}+ pts above the Marden price.")
    lines.append(_method_line("mark-gap", after.get("methodVersion")))
    sources = [{"key": f"mark:{instrument_id}:{m['name']}:{m['filingDate']}", "name": m.get("filing") or "filing",
                "url": m.get("filingUrl"), "tag": "F", "asOf": m["filingDate"], "title": m["name"]}
               for m in new[:MAX_LISTED]]
    return Finding(
        codes=["MK"], severity="amber" if high else "green", tag="F", ref_url=f"/instruments/{instrument_id}",
        headline=headline, body="\n".join(lines), alert_types=["price_or_mark"],
        facts={"newMarks": len(new), "minMark": min(marks), "maxMark": max(marks), "markedHigh": len(high),
               "newestBefore": newest, "asOf": as_of.isoformat()},
        sources=[{k: v for k, v in s.items() if v is not None} for s in sources],
    )


# ------------------------------------------------------------------ rating facts
def rating_finding(name: str, ref_url: str, fact: dict[str, Any], prior: dict[str, Any] | None) -> Finding | None:
    if prior is None or canonical_json(prior["value"]) == canonical_json(fact["value"]):
        return None
    old, new = t.value_text(prior["value"]), t.value_text(fact["value"])
    lines = [f"Rating {old} ({t.day(prior['valid_from'])}, {prior['source_name']}) {t.ARROW} {new} "
             f"({t.day(fact['valid_from'])}, {fact['source_name']})."]
    if fact.get("quote"):
        lines.append(f"Source text: \u201c{t.clip(fact['quote'], 300)}\u201d")
    sources = [{"key": f"fact:{row['id']}", "name": row["source_name"], "url": row.get("source_url"),
                "tag": str(row["tag"]).strip(), "asOf": row["valid_from"].isoformat(), "factId": row["id"],
                "value": t.value_text(row["value"])} for row in (fact, prior)]
    return Finding(
        codes=["RT"], severity="amber", tag=str(fact["tag"]).strip(), ref_url=ref_url,
        headline=f"{name} rating: {t.clip(old, 40)} {t.ARROW} {t.clip(new, 40)}", body="\n".join(lines),
        alert_types=["tenant"], facts={"before": old, "after": new, "field": fact["field"],
                                       "asOf": fact["valid_from"].isoformat()},
        sources=[{k: v for k, v in s.items() if v is not None} for s in sources],
    )
