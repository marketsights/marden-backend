"""What the stress and contagion models read about each instrument, point in time (spec 09, 18):
its name and size, the Marden price, its holders and the collateral recovery floor.

- price now: the price model's stored output on or before the as-of day (model 'price', subject
  instrument; marden.models.price_run), with its own as-of;
- size: the instrument's `amount_usd` fact as public that day ($B);
- holders: the mark-gap model's stored holder lines (funds from N-PORT and BDC filings), as
  "Vanguard Intermediate-Term ..., The Hartford ... +32 more", or "no fund holds it";
- recovery floor: the low end of the recovery model's range (cents per dollar), the price an instrument
  cannot fall below when its cash flows are cut (collateral still there).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from marden.core.db import Conn, Row
from marden.models import pit
from marden.models.runs import latest_output
from marden.models.sourced import fact_value, num

_RANGE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:[–-]\s*(\d+(?:\.\d+)?))?\s*$")
HOLDER_NAMES = 2


@dataclass
class Inst:
    id: str
    name: str
    kind: str
    issuer: str | None
    site: str | None
    size: Row | None = None
    price: float | None = None
    price_as_of: date | None = None
    recovery_low: float | None = None
    holders: list[dict[str, Any]] = field(default_factory=list)
    holder_count: int = 0

    @property
    def size_usd(self) -> float | None:
        return float(num(self.size["value"])) if self.size is not None and num(self.size["value"]) else None

    def size_b(self, day: date) -> dict[str, Any]:
        if self.size is None or self.size_usd is None:
            return fact_value(None, as_of=day)
        return fact_value(self.size, round(self.size_usd / 1e9, 3))

    def holders_text(self) -> str:
        if not self.holder_count:
            return "no fund holds it"
        names = [h["name"].title() for h in sorted(self.holders, key=lambda h: -(num(h.get("parMm")) or 0))
                 if isinstance(h.get("name"), str)][:HOLDER_NAMES]
        more = self.holder_count - len(names)
        return ", ".join(names) + (f" +{more} more" if more > 0 else "")

    def holder_nav_hit_mm(self, change_pts: float) -> float | None:
        """Sum over holders of par x price change / 100 ($mm; negative = loss)."""
        pars = [num(h.get("parMm")) for h in self.holders]
        known = [p for p in pars if p is not None]
        return round(sum(known) * change_pts / 100.0, 3) if known else (0.0 if not self.holder_count else None)


def recovery_low(out: Row | None) -> float | None:
    if out is None or not isinstance(out["output"], dict):
        return None
    rec = out["output"].get("recovery")
    text = rec.get("value") if isinstance(rec, dict) else rec
    m = _RANGE.match(str(text or ""))
    return float(m.group(1)) if m else None


def load(conn: Conn, day: date, ids: list[str]) -> dict[str, Inst]:
    rows = conn.execute("SELECT id, name, kind, issuer_entity_id, site_slug FROM instrument WHERE id = ANY(%s)",
                        (sorted(set(ids)),)).fetchall()
    sizes: dict[str, list[Row]] = {}
    for r in pit.fact_rows(conn, day, "instrument", [r["id"] for r in rows], ["amount_usd"]):
        sizes.setdefault(r["subject_id"], []).append(r)
    out: dict[str, Inst] = {}
    for r in rows:
        inst = Inst(r["id"], r["name"], r["kind"], r["issuer_entity_id"], r["site_slug"],
                    pit.current_of(sizes.get(r["id"], [])))
        px = latest_output(conn, "price", "instrument", r["id"], day)
        if px is not None and isinstance(px["output"], dict) and num(px["output"].get("price")) is not None:
            inst.price, inst.price_as_of = float(num(px["output"]["price"])), px["as_of"]
        inst.recovery_low = recovery_low(latest_output(conn, "recovery", "instrument", r["id"], day))
        gap = latest_output(conn, "mark_gap", "instrument", r["id"], day)
        if gap is not None and isinstance(gap["output"], dict):
            inst.holders = [h for h in gap["output"].get("holders") or [] if isinstance(h, dict)]
            inst.holder_count = int(num(gap["output"].get("count")) or len(inst.holders))
        out[r["id"]] = inst
    return out


def issued_by(conn: Conn, entity_ids: list[str]) -> dict[str, list[str]]:
    """entity -> the instruments it or its subsidiaries issued."""
    rows = conn.execute(
        """SELECT i.id, coalesce(e.parent_id, e.id) AS top, e.id AS own FROM instrument i
             JOIN entity e ON e.id = i.issuer_entity_id
            WHERE e.id = ANY(%(ids)s) OR e.parent_id = ANY(%(ids)s) ORDER BY i.id""", {"ids": entity_ids}).fetchall()
    out: dict[str, list[str]] = {}
    for r in rows:
        key = r["own"] if r["own"] in entity_ids else r["top"]
        out.setdefault(key, []).append(r["id"])
    return out
