"""Chain-card facts researched in October 2026: values the earlier site-chain research artifacts held that the
fact store lacked (or that the chain cards need to say who a party is), each re-verified in the document the
artifact cites before it is written. The artifact itself is never the source.

    cd services
    uv run python marden/seed/chains_2026_10/load_chains.py marden/seed/chains_2026_10/facts.json          # dry run
    uv run python marden/seed/chains_2026_10/load_chains.py marden/seed/chains_2026_10/facts.json --write
    (then `python -m marden model cushion` and `python -m marden model late_cost` for the sites written, and
    `python -m marden rebuild` for their site views)

Each item is one fact: {"subject_type", "subject_id", "field", "value", "tag" (F filing, R press or report),
"source_name", "url", "find": "<regex>", "doc_date"?: "YYYY-MM-DD", "artifact": "<where the artifact had it>"}.

- The document is the stored copy of `url` when Marden has one; else it is fetched. A dry run reads the fetched
  text and stores nothing; --write stores the raw document first (`http.fetch_document`: declared User-Agent,
  per-host rate limits; SEC filings under `sec_index`, other pages under `agent_url`). The text must match
  `find`, or the item is reported and not written. The fact keeps the matched passage as its `quote`, the
  document id and the original URL. A SEC filing's date (and so `valid_from`) comes from the EDGAR
  submissions API when `doc_date` is not given.
- Values the artifacts held that no document supports are listed in rejected.json with the reason; they are
  never written.
- Without --write nothing is written. Writes are idempotent (FactWriter dedupe keys). Safe on production.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from marden.core import db
from marden.core.documents import get_document
from marden.core.facts import FactWriter
from marden.seed.leases_2026_10.load_leases import sec_filed, text_of

PRODUCER = "curation:chains"


def stored_text(url: str) -> tuple[int | None, str] | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM document WHERE url = %s AND bytes > 2000 ORDER BY fetched_at DESC LIMIT 1", (url,)
        ).fetchone()
    if row is None:
        return None
    doc = get_document(row["id"])
    assert doc is not None
    return doc.id, text_of(doc.read(), doc.content_type)


def fetched_text(item: dict[str, Any], write: bool) -> tuple[int | None, str]:
    """(document id, text): stored with --write, read in memory on a dry run."""
    from marden.core import http

    if not write:
        resp = http.default_fetcher().get(item["url"])
        return None, text_of(resp.content, resp.content_type)
    doc = http.fetch_document(
        "sec_index" if "sec.gov" in item["url"] else "agent_url",
        item["url"],
        doc_type="url_document",
        doc_date=item.get("doc_date"),
        title=item.get("source_name"),
        meta={"producer": PRODUCER, "source_url": item["url"]},
        extract_state="none",
    )
    return doc.id, text_of(doc.read(), doc.content_type)


def verify(items: list[dict[str, Any]], write: bool) -> tuple[list[tuple[dict, int | None, str, int]], list[tuple]]:
    cache: dict[str, tuple[int | None, str]] = {}
    ok: list[tuple[dict[str, Any], int | None, str, int]] = []
    bad: list[tuple[dict[str, Any], str]] = []
    for item in items:
        if "sec.gov" in item["url"] and not item.get("doc_date"):
            item["doc_date"] = sec_filed(item["url"])
        try:
            if item["url"] not in cache:
                cache[item["url"]] = stored_text(item["url"]) or fetched_text(item, write)
            doc_id, text = cache[item["url"]]
        except Exception as exc:  # a fetch failure is reported, never written around
            bad.append((item, f"fetch failed: {exc}"))
            continue
        m = re.search(item["find"], text, re.I)
        if not m:
            bad.append((item, f"not found in the document ({len(text)} chars)"))
            continue
        if not item.get("doc_date"):
            bad.append((item, "no document date"))
            continue
        ok.append((item, doc_id, m.group(0).strip(), m.start()))
    return ok, bad


def main(path: str, write: bool) -> int:
    items = json.loads(Path(path).read_text())
    ok, bad = verify(items, write)
    for item, doc_id, quote, _ in ok:
        print(f"OK   {item['subject_id']}.{item['field']} [{item['tag']}] doc {doc_id or '(not stored: dry run)'}: "
              f"{quote[:160]!r}")
    for item, why in bad:
        print(f"FAIL {item['subject_id']}.{item['field']}: {why} ({item['url']})")
    rejected = Path(path).with_name("rejected.json")
    if rejected.exists():
        for r in json.loads(rejected.read_text()):
            print(f"REJECTED {r['site']}.{r['field']}: {r['why'][:160]}")
    if not write:
        print("dry run: nothing written (pass --write)")
        return 1 if bad else 0
    with db.transaction() as conn:
        facts = FactWriter(conn)
        for item, doc_id, quote, pos in ok:
            res = facts.add(
                item["subject_type"],
                item["subject_id"],
                item["field"],
                item["value"],
                tag=item["tag"],
                source_name=item["source_name"],
                source_url=item["url"],
                valid_from=item["doc_date"],
                producer=PRODUCER,
                status="published",
                document_id=doc_id,
                quote=quote,
                quote_loc=f"text offset {pos}",
            )
            print(f"wrote fact {res.id} (new={res.created}) {item['subject_id']}.{item['field']}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], "--write" in sys.argv[2:]))
