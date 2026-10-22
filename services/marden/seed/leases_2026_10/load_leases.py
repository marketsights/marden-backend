"""Lease-map facts researched in October 2026 (spec 25): lessees' "leases not yet commenced" totals and lease
values, starts, terms and capacities, each verified against its source document before it is written.

    cd services
    uv run python marden/seed/leases_2026_10/load_leases.py marden/seed/leases_2026_10/batch1.json          # dry run
    uv run python marden/seed/leases_2026_10/load_leases.py marden/seed/leases_2026_10/batch1.json --write
    (then batch2.json and batch3.json; then `python -m marden model leases` and `python -m marden rebuild leases`)

Each item is one fact:
  {"subject_type", "subject_id", "field", "value", "tag", "source_name", "url", "find": "<regex>",
   "doc_date"?: "YYYY-MM-DD", "valid_from"?: "YYYY-MM-DD", "method_version"?: str (tag M), "unit"?: str,
   "supersede"?: [{"value", "source_name"}], "supersede_reason"?: str}

- The document is fetched with the project's fetch tooling (`http.fetch_document`: declared User-Agent, per-host
  rate limits, raw bytes stored, a `document` row; SEC filings under the `sec_index` source, other pages under
  `agent_url`) unless a stored copy of the same URL exists. Its text must match `find`, or the item is reported and
  not written. The fact keeps the matched passage as `quote`, the document id and the original URL. A SEC
  filing's `doc_date` (and so `valid_from`) is its filing date from the EDGAR submissions API when not given.
- An item may instead derive from facts Marden already holds: "from_facts": [{"subject_type", "subject_id",
  "field", "value"}] plus "quote". Each must still be the current published value, or the item is skipped; the
  first one's document is the fact's document.
- "supersede" withdraws the subject's current published rows of the same field with that value and source name
  (status 'superseded', audit-logged, replaced_by the new fact). Nothing is deleted.
- Without --write nothing is written. Writes are idempotent (FactWriter dedupe keys).
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from marden.core import db
from marden.core.documents import DocumentRow, get_document
from marden.core.facts import FactWriter

PRODUCER = "curation:leases"
_SUBS: dict[str, dict[str, str]] = {}


def text_of(raw: bytes, content_type: str | None) -> str:
    """Plain text of an HTML page or a PDF (whitespace collapsed)."""
    if raw[:4] == b"%PDF" or (content_type or "").startswith("application/pdf"):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            f.write(raw)
            f.flush()
            out = subprocess.run(["pdftotext", "-layout", f.name, "-"], capture_output=True, check=False)
            text = out.stdout.decode("utf-8", "replace")
    else:
        text = raw.decode("utf-8", "replace")
        text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text)


def sec_filed(url: str) -> str | None:
    """Filing date of an EDGAR archive URL (submissions API through the project's SEC fetcher)."""
    m = re.search(r"edgar/data/(\d+)/(\d{18})/", url)
    if not m:
        return None
    cik, acc = m.group(1), m.group(2)
    if cik not in _SUBS:
        from marden.core import http

        resp = http.default_fetcher().get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", use_cache=False)
        recent = resp.json()["filings"]["recent"]
        _SUBS[cik] = {
            a.replace("-", ""): f for a, f in zip(recent["accessionNumber"], recent["filingDate"], strict=True)
        }
    return _SUBS[cik].get(acc)


def document_for(item: dict[str, Any]) -> DocumentRow:
    from marden.core.http import fetch_document

    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM document WHERE url = %s AND bytes > 2000 ORDER BY fetched_at DESC LIMIT 1", (item["url"],)
        ).fetchone()
    if row:
        doc = get_document(row["id"])
        assert doc is not None
        return doc
    return fetch_document(
        item.get("fetch_source", "agent_url"),
        item["url"],
        doc_type=item.get("doc_type", "url_document"),
        doc_date=item.get("doc_date"),
        title=item.get("source_name"),
        meta={"producer": PRODUCER, "source_url": item["url"]},
        extract_state="none",
    )


def _held(item: dict[str, Any]) -> tuple[int | None, str | None]:
    """(document id of the first source fact, None) when every source fact is still current; else (None, why)."""
    doc_id = None
    with db.connect() as conn:
        for i, f in enumerate(item["from_facts"]):
            row = conn.execute(
                """SELECT document_id FROM fact_current WHERE subject_type = %s AND subject_id = %s AND field = %s
                     AND value = %s::jsonb""",
                (f["subject_type"], f["subject_id"], f["field"], json.dumps(f["value"])),
            ).fetchone()
            if row is None:
                return None, f"source fact changed or missing: {f}"
            if i == 0:
                doc_id = row["document_id"]
    return doc_id, None


def verify(items: list[dict[str, Any]]) -> tuple[list[tuple[dict[str, Any], int | None, str, int]], list[tuple]]:
    cache: dict[str, tuple[int, str]] = {}
    ok: list[tuple[dict[str, Any], int | None, str, int]] = []
    bad: list[tuple[dict[str, Any], str]] = []
    for item in items:
        if item.get("from_facts"):
            doc_id, why = _held(item)
            if why:
                bad.append((item, why))
            else:
                ok.append((item, doc_id, item["quote"], 0))
            continue
        if "sec.gov" in item["url"]:
            item.setdefault("fetch_source", "sec_index")
            if not item.get("doc_date"):
                item["doc_date"] = sec_filed(item["url"])
        try:
            if item["url"] not in cache:
                doc = document_for(item)
                cache[item["url"]] = (doc.id, text_of(doc.read(), doc.content_type))
            doc_id, text = cache[item["url"]]
        except Exception as exc:
            bad.append((item, f"fetch failed: {exc}"))
            continue
        m = re.search(item["find"], text, re.I)
        if not m:
            bad.append((item, f"not found in document {doc_id} ({len(text)} chars)"))
            continue
        ok.append((item, doc_id, m.group(0).strip(), m.start()))
    return ok, bad


def _superseded_ids(conn: db.Conn, item: dict[str, Any], new_id: int) -> list[int]:
    ids: list[int] = []
    for crit in item.get("supersede") or []:
        rows = conn.execute(
            """SELECT id FROM fact WHERE subject_type = %s AND subject_id = %s AND field = %s AND status = 'published'
                 AND value = %s::jsonb AND source_name = %s AND id <> %s""",
            (
                item.get("subject_type", "obligation"),
                item["subject_id"],
                item["field"],
                json.dumps(crit["value"]),
                crit["source_name"],
                new_id,
            ),
        ).fetchall()
        ids.extend(r["id"] for r in rows)
    return ids


def main(path: str, write: bool) -> int:
    items = json.loads(Path(path).read_text())
    ok, bad = verify(items)
    for item, doc_id, quote, _ in ok:
        print(
            f"OK   {item['subject_id']}.{item['field']} = {item['value']!r} [{item['tag']}] doc {doc_id}"
            f": {quote[:160]!r}"
        )
    for item, why in bad:
        print(f"FAIL {item['subject_id']}.{item['field']} = {item['value']!r}: {why} ({item['url']})")
    if not write:
        return 1 if bad else 0
    with db.transaction() as conn:
        facts = FactWriter(conn)
        for item, doc_id, quote, pos in ok:
            res = facts.add(
                item.get("subject_type", "obligation"),
                item["subject_id"],
                item["field"],
                item["value"],
                tag=item["tag"],
                source_name=item["source_name"],
                source_url=item["url"],
                valid_from=item.get("valid_from") or item["doc_date"],
                producer=PRODUCER,
                status="published",
                unit=item.get("unit"),
                document_id=doc_id,
                quote=quote,
                quote_loc=f"text offset {pos}",
                method_version=item.get("method_version"),
            )
            print(f"wrote fact {res.id} (new={res.created}) {item['subject_id']}.{item['field']}")
            old = _superseded_ids(conn, item, res.id)
            if old:
                reason = item.get("supersede_reason", "replaced by a value traced to its source document")
                done = facts.supersede(old, reason, replaced_by={i: res.id for i in old})
                print(f"     superseded {done}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], "--write" in sys.argv[2:]))
