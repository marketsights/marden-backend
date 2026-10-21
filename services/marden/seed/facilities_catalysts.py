"""Future catalysts (aiwc/q3x/cat.json) and the curated SEC exhibit documents.

Catalysts: one event (kind 'catalyst', future dates allowed) per subject: the case's financed site
and every listed instrument that exists. A date the archive only expects ('expected': not yet
announced) is the archive's estimate, so tag M; other dates follow the source URL. Each subject
also gets ``next_catalyst``: its earliest catalyst on or after the archive date. Prices on the
catalyst rows (px, debt[].px) are not read.

SEC exhibits: q3x/docs/library.json (400) merged with q3x/docs.json (199, adds title / party /
dated). The exhibit bytes are not in the archive, so the merged JSON record is stored as the raw
bytes of a `document` row with the exhibit URL as its url and the exhibit type as doc_type.
"""

from __future__ import annotations

import json
from typing import Any

from marden.core.db import Conn
from marden.core.documents import record_document
from marden.seed.common import SEED_ARCHIVE, SEED_SOURCE_ID, parse_date, tag_for_url
from marden.seed.facilities_archive import CATALYSTS, DOCS, LIBRARY, Archive
from marden.seed.facilities_sink import ARCHIVE_CUTOFF, EventSink, FactSink, clean, first_url

CAT_SEVERITY = {"severe": "red", "material": "amber", "watch": "none"}


def write_catalysts(conn: Conn, facts: FactSink, events: EventSink, arc: Archive,
                    existing_sites: set[str]) -> dict[str, Any]:
    instruments = {r["id"] for r in conn.execute("SELECT id FROM instrument").fetchall()}
    src = arc.sources[CATALYSTS]
    stats: dict[str, Any] = {"catalysts": 0, "events": 0, "skipped": []}
    nxt: dict[tuple[str, str], tuple[str, dict[str, Any], str | None, str]] = {}
    for r in arc.data[CATALYSTS]["rows"]:
        subjects: list[tuple[str, str]] = []
        slug = arc.site_slugs.get(r.get("case")) if r.get("case") else None
        if slug in existing_sites:
            subjects.append(("site", slug))
        missing = [d["id"] for d in r.get("debt") or [] if d.get("id") and d["id"] not in instruments]
        subjects += [("instrument", d["id"]) for d in r.get("debt") or [] if d.get("id") in instruments]
        if missing:
            stats["skipped"].append(f"catalyst {r.get('d')} {r.get('ev')}: instruments not loaded {missing}")
        if not subjects:
            stats["skipped"].append(f"catalyst {r.get('d')} {r.get('ev')}: no site or instrument subject")
            continue
        url = first_url(r.get("src"))
        tag = "M" if r.get("dq") == "expected" else tag_for_url(url)
        title = clean(r.get("ev"))
        if clean(r.get("evl")) and clean(r.get("evl")) != title:
            title = f"{title}: {clean(r.get('evl'))}"
        for st, sid in subjects:
            events.add(st, sid, r.get("d"), "catalyst", title, src=src, url=url, tag=tag,
                       severity=CAT_SEVERITY.get(r.get("sev") or ""), source=CATALYSTS)
            stats["events"] += 1
        stats["catalysts"] += 1
        day = parse_date(r.get("d"))
        if day is None or day < ARCHIVE_CUTOFF:
            continue
        value = {k: v for k, v in {
            "date": day.isoformat(), "event": clean(r.get("ev")), "detail": clean(r.get("evl")),
            "certainty": r.get("dq"), "category": r.get("cat"), "level": r.get("sev"),
            "issue": clean(r.get("issue")), "why": clean(r.get("why")), "who": clean(r.get("who")),
            "source_url": url,
        }.items() if v}
        for subject in subjects:
            if subject not in nxt or day.isoformat() < nxt[subject][0]:
                nxt[subject] = (day.isoformat(), value, url, tag)
    for (st, sid), (_, value, url, tag) in sorted(nxt.items()):
        facts.add(st, sid, "next_catalyst", value, src=src, url=url, judged=tag == "M",
                  tag=None if tag == "M" else tag)
    return stats


def write_exhibit_documents(conn: Conn, arc: Archive) -> dict[str, int]:
    """One document row per curated SEC exhibit URL (metadata only; idempotent on url + sha256)."""
    curated = {d["url"]: d for d in arc.data[DOCS]["docs"]}
    stats = {"documents": 0, "new": 0}
    for rec in arc.data[LIBRARY]["docs"]:
        url = rec.get("url")
        if not url:
            continue
        merged = dict(rec)
        extra = curated.get(url) or {}
        for k in ("title", "party", "dated"):
            if extra.get(k) is not None:
                merged[k] = extra[k]
        if extra.get("kind") and extra["kind"] != rec.get("kind"):
            merged["kind_label"] = extra["kind"]
        files = [LIBRARY] + ([DOCS] if extra else [])
        body = json.dumps(merged, sort_keys=True, ensure_ascii=False).encode()
        doc = record_document(
            SEED_SOURCE_ID, url, body, content_type="application/json",
            doc_type=rec.get("ex") or rec.get("form"), title=clean(merged.get("title") or rec.get("desc")),
            doc_date=parse_date(rec.get("date")), external_id=rec.get("acc"),
            meta={"archive": SEED_ARCHIVE, "record_only": True, "bytes_in_archive": False, "files": files,
                  "form": rec.get("form"), "kind": rec.get("kind"), "issuer": rec.get("iss"),
                  "cik": str(rec["cik"]) if rec.get("cik") else None,
                  "document_ids": [arc.sources[f].doc_id for f in files]},
            conn=conn,
        )
        stats["documents"] += 1
        stats["new"] += int(doc.was_new)
    return stats
