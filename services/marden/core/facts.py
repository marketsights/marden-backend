"""The fact store writer (ARCHITECTURE "The fact store"; spec 22 accuracy layers).

    with db.transaction() as conn:
        facts = FactWriter(conn)
        res = facts.add("site", "abilene", "facility_mw", 1200, tag="F", source_name="TCEQ",
                        source_url=url, valid_from=date(2026, 9, 1), producer="collector:tceq",
                        status="published", document_id=doc.id)

Writes are idempotent (dedupe_key, ON CONFLICT DO NOTHING) and never overwrite history. Newly
published facts mark dependent read models dirty in the same transaction.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from marden.core import checks
from marden.core.checks import ValidationError
from marden.core.db import Conn, Row
from marden.core.jsonutil import canonical_json, hash_parts, jsonb, to_jsonable

ADD_STATUSES = frozenset({"candidate", "published"})
VERIFIER_VERDICTS = frozenset({"agree", "disagree", "unsupported"})


@dataclass(frozen=True)
class WriteResult:
    id: int
    created: bool
    status: str
    dedupe_key: str


def fact_dedupe_key(
    subject_type: str,
    subject_id: str,
    field: str,
    value: Any,
    tag: str,
    source: str,
    valid_from: date,
    producer: str,
) -> str:
    """sha256 over (subject, field, canonical value, tag, source url-or-name, valid_from, producer)."""
    return hash_parts(
        subject_type, subject_id, field, canonical_json(value), tag, source, valid_from.isoformat(), producer
    )


def _mark_dirty(conn: Conn, subject_type: str, subject_id: str, field: str, reason: str) -> None:
    from marden.readmodels.deps import mark_dirty_for  # readmodels depends on core, not the reverse

    mark_dirty_for(subject_type, subject_id, field, conn=conn, reason=reason)


class FactWriter:
    """Writes facts on a connection the caller holds; use inside a transaction."""

    def __init__(self, conn: Conn):
        self.conn = conn

    def add(
        self,
        subject_type: str,
        subject_id: str,
        field: str,
        value: Any,
        *,
        tag: str,
        source_name: str,
        source_url: str | None = None,
        valid_from: date | str,
        producer: str,
        status: str = "candidate",
        unit: str | None = None,
        document_id: int | None = None,
        quote: str | None = None,
        quote_loc: str | None = None,
        method_version: str | None = None,
        confidence: float | None = None,
        material: bool = False,
    ) -> WriteResult:
        checks.subject_type(subject_type)
        checks.non_empty(subject_id, "subject_id")
        checks.snake(field, "field")
        checks.tag(tag)
        checks.non_empty(source_name, "source_name")
        checks.producer(producer)
        checks.model_rules(tag, method_version, producer)
        day = checks.not_future(valid_from, "valid_from")
        conf = checks.confidence(confidence)
        if status not in ADD_STATUSES:
            raise ValidationError(f"status must be candidate or published, got {status!r}")
        if value is None:
            raise ValidationError("a fact needs a value; an unsourced value is simply not written")
        try:
            value_json = to_jsonable(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"value is not JSON: {exc}") from None
        if quote is not None and not quote.strip():
            raise ValidationError("quote must not be blank")
        if checks.is_extracted(producer):
            if not quote:
                raise ValidationError("extracted facts require the exact source quote")
            if document_id is None:
                raise ValidationError("extracted facts require document_id")
            if status == "published":
                raise ValidationError("extracted facts are added as candidates and published after verification")
        if material and status == "published":
            raise ValidationError("material facts wait for a person: add as candidate, publish on approval")

        key = fact_dedupe_key(
            subject_type, subject_id, field, value_json, tag, source_url or source_name, day, producer
        )
        row = self.conn.execute(
            """INSERT INTO fact (subject_type, subject_id, field, value, unit, tag, source_name, source_url,
                                 document_id, quote, quote_loc, valid_from, status, method_version, producer,
                                 confidence, material, dedupe_key)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (dedupe_key) DO NOTHING
               RETURNING id, status""",
            (
                subject_type, subject_id, field, jsonb(value_json), unit, tag, source_name, source_url,
                document_id, quote, quote_loc, day, status, method_version, producer, conf, material, key,
            ),
        ).fetchone()
        if row is None:
            existing = self.conn.execute(
                "SELECT id, status FROM fact WHERE dedupe_key = %s", (key,)
            ).fetchone()
            assert existing is not None
            return WriteResult(existing["id"], False, existing["status"], key)
        if status == "published":
            _mark_dirty(self.conn, subject_type, subject_id, field, f"fact:{row['id']}")
        return WriteResult(row["id"], True, row["status"], key)

    def publish(self, fact_ids: Iterable[int], *, decided_by: str | None = None) -> list[int]:
        """Publish candidates. Without `decided_by` (automatic publishing) material facts and
        extracted facts the verifier has not agreed with are refused; a person may publish anything.
        Returns the ids that changed to published."""
        ids = sorted(set(fact_ids))
        if not ids:
            return []
        if decided_by is None:
            blocked = self.conn.execute(
                """SELECT id FROM fact WHERE id = ANY(%s) AND status = 'candidate'
                   AND (material OR (producer LIKE 'extractor:%%' AND verifier IS DISTINCT FROM 'agree'))""",
                (ids,),
            ).fetchall()
            if blocked:
                raise ValidationError(
                    f"facts {[r['id'] for r in blocked]} need a person: material or not verified"
                )
        rows = self.conn.execute(
            """UPDATE fact SET status = 'published' WHERE id = ANY(%s) AND status = 'candidate'
               RETURNING id, subject_type, subject_id, field""",
            (ids,),
        ).fetchall()
        for r in rows:
            _mark_dirty(self.conn, r["subject_type"], r["subject_id"], r["field"], f"fact:{r['id']}")
        self._close_reviews([r["id"] for r in rows], "approved", decided_by)
        return [r["id"] for r in rows]

    def reject(self, fact_ids: Iterable[int], reason: str, *, decided_by: str | None = None) -> list[int]:
        """Reject candidates (published history is never rewritten). Records the reason in audit_log."""
        checks.non_empty(reason, "reason")
        ids = sorted(set(fact_ids))
        if not ids:
            return []
        rows = self.conn.execute(
            "UPDATE fact SET status = 'rejected' WHERE id = ANY(%s) AND status = 'candidate' RETURNING id",
            (ids,),
        ).fetchall()
        rejected = [r["id"] for r in rows]
        self._close_reviews(rejected, "rejected", decided_by)
        for fact_id in rejected:
            self.conn.execute(
                "INSERT INTO audit_log (user_id, action, target, meta) VALUES (%s, 'fact.reject', %s, %s)",
                (decided_by, f"fact:{fact_id}", jsonb({"reason": reason})),
            )
        return rejected

    def set_verifier(self, fact_id: int, verdict: str) -> None:
        if verdict not in VERIFIER_VERDICTS:
            raise ValidationError(f"verifier verdict must be one of {sorted(VERIFIER_VERDICTS)}")
        self.conn.execute("UPDATE fact SET verifier = %s WHERE id = %s", (verdict, fact_id))

    def request_review(
        self, fact_id: int, *, title: str, reason: str, payload: dict[str, Any] | None = None
    ) -> int:
        """Open (or return the existing) review item for a fact."""
        checks.non_empty(title, "title")
        checks.non_empty(reason, "reason")
        row = self.conn.execute(
            """INSERT INTO review_item (kind, ref_type, ref_id, title, reason, payload)
               VALUES ('fact', 'fact', %s, %s, %s, %s)
               ON CONFLICT (kind, ref_type, ref_id) DO UPDATE SET kind = EXCLUDED.kind
               RETURNING id""",
            (str(fact_id), title, reason, jsonb(payload or {})),
        ).fetchone()
        assert row is not None
        return row["id"]

    def _close_reviews(self, fact_ids: Sequence[int], status: str, decided_by: str | None) -> None:
        if not fact_ids:
            return
        self.conn.execute(
            """UPDATE review_item SET status = %s, decided_by = %s, decided_at = now()
               WHERE kind = 'fact' AND ref_type = 'fact' AND ref_id = ANY(%s) AND status = 'open'""",
            (status, decided_by, [str(i) for i in fact_ids]),
        )


# ---------------------------------------------------------------- reads
def current_facts(
    conn: Conn, subject_type: str, subject_id: str, fields: Sequence[str] | None = None
) -> dict[str, Row]:
    """field -> current published fact row (fact_current precedence)."""
    sql = "SELECT * FROM fact_current WHERE subject_type = %s AND subject_id = %s"
    params: list[Any] = [subject_type, subject_id]
    if fields is not None:
        sql += " AND field = ANY(%s)"
        params.append(list(fields))
    return {r["field"]: r for r in conn.execute(sql, params).fetchall()}


def facts_asof(
    conn: Conn, as_of: date, subject_type: str, subject_id: str, fields: Sequence[str] | None = None
) -> dict[str, Row]:
    """field -> the fact Marden knew on `as_of` (fact_asof precedence)."""
    sql = "SELECT * FROM fact_asof(%s) WHERE subject_type = %s AND subject_id = %s"
    params: list[Any] = [as_of, subject_type, subject_id]
    if fields is not None:
        sql += " AND field = ANY(%s)"
        params.append(list(fields))
    return {r["field"]: r for r in conn.execute(sql, params).fetchall()}


def sourced(row: Row | None) -> dict[str, Any] | None:
    """A fact row as the API's Sourced<T> shape (None when there is no fact: shown as n/d)."""
    if row is None:
        return None
    out: dict[str, Any] = {"value": row["value"], "tag": row["tag"], "asOf": row["valid_from"].isoformat()}
    if row.get("source_url"):
        out["sourceUrl"] = row["source_url"]
    if row.get("source_name"):
        out["sourceName"] = row["source_name"]
    if row.get("method_version"):
        out["methodVersion"] = row["method_version"]
    return out
