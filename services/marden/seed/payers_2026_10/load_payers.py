"""Payer scale facts researched in October 2026 (spec 24 Payers "Scale" column, PayerCapacity.scale): the
reported revenue, funding, valuation and cash burn of payers no agency rates (OpenAI, Anthropic, xAI / SpaceX,
Fluidstack, Lambda, Nscale, Firmus), and SpaceX's listing. Each value is checked against its source document
before it is written; a figure no reputable report or filing gives is left out, never estimated.

    cd services
    uv run python marden/seed/payers_2026_10/load_payers.py                      # dry run: verify and print
    uv run python marden/seed/payers_2026_10/load_payers.py --write              # write the verified facts
    (then `python -m marden rebuild payers` and `python -m marden rebuild payer_capacity`)

facts.json items (one fact each), as the leases loader (seed/leases_2026_10/load_leases.py) reads them:
  {"subject_type": "entity", "subject_id", "field", "value", "tag", "source_name", "url", "find": "<regex>",
   "valid_from"?: "YYYY-MM-DD", "doc_date"?: "YYYY-MM-DD"}
Fields (payer_scale.py): `reported_revenue`, `funding_raised`, `valuation`, `cash_burn` as
{"usd": number, "basis": "<what the number is>"}; `listing` as {"ticker", "exchange"}.

The document is fetched with the project's fetch tooling unless a stored copy of the URL exists; its text must
match `find` or the item is reported and not written. The fact keeps the matched passage as its quote, the
document id and the URL (tag R for reporting, F for filings). Without --write nothing is written; writes are
idempotent (FactWriter dedupe keys).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from marden.core import db
from marden.core.facts import FactWriter
from marden.seed.leases_2026_10.load_leases import verify

PRODUCER = "curation:payers"
FACTS = Path(__file__).with_name("facts.json")
FIELDS = ("reported_revenue", "funding_raised", "valuation", "cash_burn", "listing")


def check(items: list[dict]) -> list[str]:
    """Shape errors: unknown field, a scale value without a positive usd and a basis, a source without a URL."""
    errors = []
    for i, item in enumerate(items):
        where = f"item {i} {item.get('subject_id')}.{item.get('field')}"
        value = item.get("value")
        if item.get("field") not in FIELDS:
            errors.append(f"{where}: field must be one of {FIELDS}")
        elif item["field"] == "listing":
            if not (isinstance(value, dict) and value.get("ticker")):
                errors.append(f"{where}: listing needs a ticker")
        elif not (
            isinstance(value, dict)
            and isinstance(value.get("usd"), int | float)
            and value["usd"] > 0
            and str(value.get("basis") or "").strip()
        ):
            errors.append(f"{where}: value needs a positive usd and a basis")
        if item.get("tag") not in ("F", "R") or not str(item.get("url") or "").startswith("https://"):
            errors.append(f"{where}: needs tag F or R and an https source URL")
        if not item.get("find"):
            errors.append(f"{where}: needs the passage to find in the source")
    return errors


def main(path: Path, write: bool) -> int:
    items = json.loads(path.read_text())
    if errors := check(items):
        print("\n".join(errors))
        return 2
    ok, bad = verify(items)
    for item, doc_id, quote, _ in ok:
        what = f"{item['subject_id']}.{item['field']} = {item['value']!r} [{item['tag']}]"
        print(f"OK   {what} doc {doc_id}: {quote[:140]!r}")
    for item, why in bad:
        print(f"FAIL {item['subject_id']}.{item['field']} = {item['value']!r}: {why} ({item['url']})")
    if not write:
        print(f"dry run: {len(ok)} verified, {len(bad)} not; rerun with --write to write the verified facts")
        return 1 if bad else 0
    with db.transaction() as conn:
        facts = FactWriter(conn)
        for item, doc_id, quote, pos in ok:
            res = facts.add(
                item.get("subject_type", "entity"),
                item["subject_id"],
                item["field"],
                item["value"],
                tag=item["tag"],
                source_name=item["source_name"],
                source_url=item["url"],
                valid_from=item.get("valid_from") or item["doc_date"],
                producer=PRODUCER,
                status="published",
                unit="USD" if item["field"] != "listing" else None,
                document_id=doc_id,
                quote=quote,
                quote_loc=f"text offset {pos}",
            )
            print(f"wrote fact {res.id} (new={res.created}) {item['subject_id']}.{item['field']}")
    return 1 if bad else 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sys.exit(main(Path(args[0]) if args else FACTS, "--write" in sys.argv[1:]))
