"""The risk list's reasons and weekly note, written by code templates from computed numbers only (spec 10,
spec 22 "Note writer": every figure is filled in by code; no LLM writes a word of it).

Each reason is an Evidence (types.ts) built from one record: the status model's tripped link (its fact
passes through with its own tag and source), the cushion model's projection, a dated schedule change, the
mark-gap model's fund marks, the payer-capacity band, or the price model's week change. The note has one
paragraph per top-ranked instrument (four at most): a bold lead (the name) and sentences assembled from the
row's reasons, each followed by the reference of its record (`refs`: the fact id, else the source URL, else
the model output it comes from).
"""

from __future__ import annotations

from datetime import date
from typing import Any

METHOD_URLS = {
    "cushion": "docs/methods/cushion-v0.1.md",
    "mark_gap": "docs/methods/mark-gap-v0.1.md",
    "payer_capacity": "docs/methods/payer-capacity-v0.1.md",
    "price": "docs/methods/pricing-v0.1.md",
}
NOTE_PARAGRAPHS = 4


def evidence(day: str, text: str, tag: str, source: str, url: str, ref: str) -> dict[str, Any]:
    return {"date": day, "text": text, "tag": tag, "sourceName": source, "sourceUrl": url, "_ref": ref}


def status_reason(link: dict[str, Any], site: str, out_day: date) -> dict[str, Any] | None:
    fact = link.get("fact") if isinstance(link.get("fact"), dict) else None
    code = link.get("ruleCode") or ""
    head = f"{link.get('party') or link.get('role')} ({link.get('role')}, {code})"
    if fact and fact.get("value"):
        return evidence(
            str(fact.get("asOf") or out_day.isoformat()),
            f"{head}: {fact['value']}",
            fact.get("tag") or "R",
            fact.get("sourceName") or "",
            fact.get("sourceUrl") or "",
            fact.get("sourceUrl") or f"model:status@site:{site}",
        )
    reason = str(link.get("reason") or "").split(". ")[0]
    if not reason:
        return None
    return evidence(
        out_day.isoformat(),
        f"{head}: {reason}",
        "M",
        "Marden status rules",
        "docs/methods/status-rules-v0.1.md",
        f"model:status@site:{site}",
    )


def cushion_reason(
    months: int, p50: str | None, outside: str | None, delta: int | None, site: str, out_day: date
) -> dict[str, Any]:
    moved = f"; {delta:+d} months on the week" if delta else ""
    return evidence(
        out_day.isoformat(),
        f"Cushion {months:+d} months: rent start P50 {p50 or 'n/d'} against outside date {outside or 'n/d'}{moved}",
        "M",
        "Marden cushion model",
        METHOD_URLS["cushion"],
        f"model:cushion@site:{site}:{out_day.isoformat()}",
    )


def slip_reason(row: dict[str, Any]) -> dict[str, Any]:
    v = row["value"] if isinstance(row["value"], dict) else {"what": str(row["value"])}
    return evidence(
        str(v.get("date") or row["valid_from"].isoformat()),
        str(v.get("what") or v.get("text") or "")[:240],
        row["tag"].strip(),
        row.get("source_name") or "",
        row.get("source_url") or "",
        f"fact:{row['id']}",
    )


def gap_reason(
    gap: float, count: int, filing: str | None, filed: str | None, iid: str, out_day: date
) -> dict[str, Any]:
    side = "above" if gap < 0 else "below"
    return evidence(
        out_day.isoformat(),
        f"Fund marks {abs(gap):.1f} pts {side} the Marden price ({count} holder lines, "
        f"{filing or 'filings'} {filed or ''})".rstrip(),
        "M",
        "Marden mark gap",
        METHOD_URLS["mark_gap"],
        f"model:mark_gap@instrument:{iid}:{out_day.isoformat()}",
    )


def band_reason(tenant: str, band: str, reasons: list[str], out_day: date) -> dict[str, Any]:
    why = f": {reasons[0]}" if reasons else ""
    return evidence(
        out_day.isoformat(),
        f"Tenant {tenant} downgrade risk {band} (Marden view, not a rating){why}",
        "M",
        "Marden payer capacity",
        METHOD_URLS["payer_capacity"],
        f"model:payer_capacity@entity:{tenant}:{out_day.isoformat()}",
    )


def price_reason(change: float, price: float, iid: str, out_day: date) -> dict[str, Any]:
    return evidence(
        out_day.isoformat(),
        f"Marden price {price:.1f}, {change:+.1f} on the week",
        "M",
        "Marden model price",
        METHOD_URLS["price"],
        f"model:price@instrument:{iid}:{out_day.isoformat()}",
    )


def public(ev: dict[str, Any]) -> dict[str, Any]:
    """The Evidence as types.ts has it (the reference stays in the note's refs)."""
    return {k: v for k, v in ev.items() if not k.startswith("_")}


def note(week: date, rows: list[dict[str, Any]], reasons: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    paragraphs = []
    for r in rows[:NOTE_PARAGRAPHS]:
        evs = reasons.get(r["instrumentId"], [])
        sentences = [
            f"Ranked {r['rank']} of {len(rows)}, with {round(r['odds']['value'] * 100)}% odds of a 3-point move "
            f"in 90 days (odds v0, uncalibrated)."
        ]
        sentences += [f"{e['text']} ({e['date']})." for e in evs[:3]]
        paragraphs.append({"lead": r["name"], "text": " ".join(sentences), "refs": [e["_ref"] for e in evs[:3]]})
    return {"title": f"Risk list, week of {week.strftime('%b')} {week.day}, {week.year}", "paragraphs": paragraphs}
