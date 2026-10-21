"""Idempotent writers for price observations, dated events and fund holdings.

Same rules as facts: validated, keyed for safe reruns (ON CONFLICT DO NOTHING), never overwritten,
and new published rows mark dependent read models dirty in the caller's transaction. Pseudo-fields
'@price', '@event' and '@holding' route these to read models through marden.readmodels.deps.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from marden.core import checks
from marden.core.checks import ValidationError
from marden.core.db import Conn
from marden.core.facts import WriteResult
from marden.core.jsonutil import hash_parts, jsonb

PRICE_KINDS = frozenset({"trade", "quote", "fund_mark", "exchange", "dealer", "marden", "legacy"})
SEVERITIES = frozenset({"red", "amber", "green", "none"})
EVENT_STATUSES = frozenset({"candidate", "published"})
FILING_TYPES = frozenset({"N-PORT", "10-Q", "10-K"})
_CUSIP = re.compile(r"^[0-9A-Z*@#]{9}$")
_ISIN = re.compile(r"^[A-Z]{2}[0-9A-Z]{9}[0-9]$")

Number = int | float | Decimal


def _mark(conn: Conn, subject_type: str, subject_id: str, field: str, reason: str) -> None:
    from marden.readmodels.deps import mark_dirty_for

    mark_dirty_for(subject_type, subject_id, field, conn=conn, reason=reason)


class PriceWriter:
    """price_obs rows. Dedupe on instrument, date, kind, source (url or name), price; plus obs_at
    when given, so distinct intraday prints at the same price are kept."""

    def __init__(self, conn: Conn):
        self.conn = conn

    def add(
        self,
        instrument_id: str,
        obs_date: date | str,
        kind: str,
        price: Number | None,
        *,
        tag: str,
        source_name: str,
        source_url: str | None = None,
        obs_at: datetime | None = None,
        yield_pct: Number | None = None,
        spread_bp: Number | None = None,
        volume: Number | None = None,
        method_version: str | None = None,
        document_id: int | None = None,
    ) -> WriteResult:
        checks.non_empty(instrument_id, "instrument_id")
        day = checks.not_future(obs_date, "obs_date")
        if kind not in PRICE_KINDS:
            raise ValidationError(f"price kind must be one of {sorted(PRICE_KINDS)}, got {kind!r}")
        checks.tag(tag)
        checks.non_empty(source_name, "source_name")
        if tag == "M" and not method_version:
            raise ValidationError("model (M) prices require method_version")
        if kind == "marden" and tag != "M":
            raise ValidationError("Marden prices are model values (tag M)")
        price_text = checks.decimal_text(price, "price")
        for name, val in (("yield_pct", yield_pct), ("spread_bp", spread_bp), ("volume", volume)):
            checks.decimal_text(val, name)
        if price_text is None and yield_pct is None and spread_bp is None:
            raise ValidationError("a price observation needs a price, yield or spread")
        if price_text is not None and Decimal(price_text) < 0:
            raise ValidationError("price (percent of par) cannot be negative")

        key = hash_parts(
            "price_obs", instrument_id, day.isoformat(), kind, source_url or source_name, price_text,
            obs_at.isoformat() if obs_at else None,
        )
        row = self.conn.execute(
            """INSERT INTO price_obs (instrument_id, obs_date, obs_at, kind, price, yield_pct, spread_bp, volume,
                                      tag, source_name, source_url, method_version, document_id, dedupe_key)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (dedupe_key) DO NOTHING RETURNING id""",
            (
                instrument_id, day, obs_at, kind, price, yield_pct, spread_bp, volume, tag, source_name,
                source_url, method_version, document_id, key,
            ),
        ).fetchone()
        if row is None:
            existing = self.conn.execute("SELECT id FROM price_obs WHERE dedupe_key = %s", (key,)).fetchone()
            assert existing is not None
            return WriteResult(existing["id"], False, "published", key)
        _mark(self.conn, "instrument", instrument_id, "@price", f"price_obs:{row['id']}")
        return WriteResult(row["id"], True, "published", key)


class EventWriter:
    """event rows. Dedupe on subject, date, kind, title, source (url or name).
    Event dates may be in the future (hearings, deadlines: 'coming up')."""

    def __init__(self, conn: Conn):
        self.conn = conn

    def add(
        self,
        subject_type: str,
        subject_id: str,
        event_date: date | str,
        kind: str,
        title: str,
        *,
        tag: str,
        source_name: str,
        source_url: str | None = None,
        severity: str | None = None,
        rule_code: str | None = None,
        material: bool = False,
        document_id: int | None = None,
        fact_id: int | None = None,
        status: str = "candidate",
    ) -> WriteResult:
        checks.subject_type(subject_type)
        checks.non_empty(subject_id, "subject_id")
        day = checks.as_date(event_date, "event_date")
        checks.snake(kind, "kind")
        checks.non_empty(title, "title")
        checks.tag(tag)
        checks.non_empty(source_name, "source_name")
        if severity is not None and severity not in SEVERITIES:
            raise ValidationError(f"severity must be one of {sorted(SEVERITIES)}")
        if status not in EVENT_STATUSES:
            raise ValidationError("status must be candidate or published")
        if material and status == "published":
            raise ValidationError("material events wait for a person: add as candidate, publish on approval")

        key = hash_parts("event", subject_type, subject_id, day.isoformat(), kind, title, source_url or source_name)
        row = self.conn.execute(
            """INSERT INTO event (subject_type, subject_id, event_date, kind, title, severity, rule_code, material,
                                  tag, source_name, source_url, document_id, fact_id, status, dedupe_key)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (dedupe_key) DO NOTHING RETURNING id, status""",
            (
                subject_type, subject_id, day, kind, title, severity, rule_code, material, tag, source_name,
                source_url, document_id, fact_id, status, key,
            ),
        ).fetchone()
        if row is None:
            existing = self.conn.execute("SELECT id, status FROM event WHERE dedupe_key = %s", (key,)).fetchone()
            assert existing is not None
            return WriteResult(existing["id"], False, existing["status"], key)
        if status == "published":
            _mark(self.conn, subject_type, subject_id, "@event", f"event:{row['id']}")
        return WriteResult(row["id"], True, row["status"], key)

    def publish(self, event_ids: Iterable[int], *, decided_by: str | None = None) -> list[int]:
        """Publish candidate events; material ones need `decided_by` (a person)."""
        ids = sorted(set(event_ids))
        if not ids:
            return []
        if decided_by is None:
            material = self.conn.execute(
                "SELECT id FROM event WHERE id = ANY(%s) AND status = 'candidate' AND material", (ids,)
            ).fetchall()
            if material:
                raise ValidationError(f"material events {[r['id'] for r in material]} need a person")
        rows = self.conn.execute(
            """UPDATE event SET status = 'published' WHERE id = ANY(%s) AND status = 'candidate'
               RETURNING id, subject_type, subject_id""",
            (ids,),
        ).fetchall()
        for r in rows:
            _mark(self.conn, r["subject_type"], r["subject_id"], "@event", f"event:{r['id']}")
        return [r["id"] for r in rows]

    def reject(self, event_ids: Iterable[int], reason: str, *, decided_by: str | None = None) -> list[int]:
        checks.non_empty(reason, "reason")
        ids = sorted(set(event_ids))
        if not ids:
            return []
        rows = self.conn.execute(
            "UPDATE event SET status = 'rejected' WHERE id = ANY(%s) AND status = 'candidate' RETURNING id",
            (ids,),
        ).fetchall()
        for r in rows:
            self.conn.execute(
                "INSERT INTO audit_log (user_id, action, target, meta) VALUES (%s, 'event.reject', %s, %s)",
                (decided_by, f"event:{r['id']}", jsonb({"reason": reason})),
            )
        return [r["id"] for r in rows]


class HoldingWriter:
    """holding rows (N-PORT, BDC 10-Q/10-K schedule lines; filed values). Unique on
    (fund_entity_id, accession, line_key)."""

    def __init__(self, conn: Conn):
        self.conn = conn

    def add(
        self,
        fund_entity_id: str,
        accession: str,
        line_key: str,
        *,
        period_end: date | str,
        filing_type: str,
        filing_url: str,
        filed_at: date | str | None = None,
        instrument_id: str | None = None,
        issuer_name: str | None = None,
        title: str | None = None,
        cusip: str | None = None,
        isin: str | None = None,
        par: Number | None = None,
        value_usd: Number | None = None,
        mark_price: Number | None = None,
        pct_nav: Number | None = None,
        fund_net_assets: Number | None = None,
        non_accrual: bool | None = None,
        document_id: int | None = None,
    ) -> WriteResult:
        checks.non_empty(fund_entity_id, "fund_entity_id")
        checks.non_empty(accession, "accession")
        checks.non_empty(line_key, "line_key")
        checks.non_empty(filing_url, "filing_url")
        period = checks.not_future(period_end, "period_end")
        filed = checks.not_future(filed_at, "filed_at") if filed_at is not None else None
        if filed is not None and filed < period:
            raise ValidationError("filed_at is before period_end")
        if filing_type not in FILING_TYPES:
            raise ValidationError(f"filing_type must be one of {sorted(FILING_TYPES)}")
        if cusip is not None and not _CUSIP.match(cusip):
            raise ValidationError(f"invalid CUSIP {cusip!r}")
        if isin is not None and not _ISIN.match(isin):
            raise ValidationError(f"invalid ISIN {isin!r}")
        for name, val in (("par", par), ("value_usd", value_usd), ("mark_price", mark_price),
                          ("pct_nav", pct_nav), ("fund_net_assets", fund_net_assets)):
            checks.decimal_text(val, name)

        values: dict[str, Any] = {
            "fund_entity_id": fund_entity_id, "instrument_id": instrument_id, "issuer_name": issuer_name,
            "title": title, "cusip": cusip, "isin": isin, "period_end": period, "filing_type": filing_type,
            "filing_url": filing_url, "accession": accession, "filed_at": filed, "par": par,
            "value_usd": value_usd, "mark_price": mark_price, "pct_nav": pct_nav,
            "fund_net_assets": fund_net_assets, "non_accrual": non_accrual, "document_id": document_id,
            "line_key": line_key,
        }
        cols = ", ".join(values)
        row = self.conn.execute(
            f"""INSERT INTO holding ({cols}) VALUES ({", ".join(f"%({c})s" for c in values)})
                ON CONFLICT (fund_entity_id, accession, line_key) DO NOTHING RETURNING id""",
            values,
        ).fetchone()
        key = f"{fund_entity_id}|{accession}|{line_key}"
        if row is None:
            existing = self.conn.execute(
                "SELECT id FROM holding WHERE fund_entity_id = %s AND accession = %s AND line_key = %s",
                (fund_entity_id, accession, line_key),
            ).fetchone()
            assert existing is not None
            return WriteResult(existing["id"], False, "published", key)
        self._mark(row["id"])
        return WriteResult(row["id"], True, "published", key)

    def link_instrument(self, holding_id: int, instrument_id: str) -> bool:
        """Link an unlinked holding line to an instrument. Returns True if it changed."""
        row = self.conn.execute(
            "UPDATE holding SET instrument_id = %s WHERE id = %s AND instrument_id IS NULL RETURNING id",
            (instrument_id, holding_id),
        ).fetchone()
        if row is None:
            return False
        self._mark(holding_id)
        return True

    def _mark(self, holding_id: int) -> None:
        # Routed through the `holding` subject: deps map it to its instrument's views once linked.
        _mark(self.conn, "holding", str(holding_id), "@holding", f"holding:{holding_id}")
