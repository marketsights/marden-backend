"""Writers for staff-curated campus financing (spec 16/17, spec 22 "no quote, no fact").

Every value cites a document Marden stored raw first (`agents ingest <url>`): the writer finds the newest
stored copy of the URL and refuses a quote that is not in its text (verbatim after the agents' whitespace and
typography normalization; a quote given as several fragments is checked fragment by fragment and stored
joined with " ... "). Facts go through FactWriter (dirty marks in the same transaction); site, instrument and
chain-party rows use the same upserts as the seed loaders (seed/sites_rows.py, seed/instruments.py,
seed/sites_chain.py).

    propose_link   site_link 'proposed' + its evidence fact (campus.site_link_evidence) + a 'link' review item,
                   as seed/facilities_links.py does for the archive's links
    approve_link   the decision through the review queue (agents.review.decide): agent_decision, audit_log
                   review.approve, site_link approved_by, dirty marks
    withdraw_link  take back an approved link that turned out wrong: the review item is reopened (audit_log
                   review.reopen) and rejected through the same queue, so site_link reads 'rejected' with who
                   and why; the evidence fact stays as history
    upsert_site / upsert_instrument / set_chain_party / add_fact   a financed site and its debt
    add_financing_note   campus.financing_note (tag M): owner-funded / not found, with what was searched
    apply_spec     all of the above from one JSON file (marden.facilities.commands `apply`), plus quoted
                   campus and building facts (`campuses` / `buildings`: [{id, facts}], e.g. a building's floor area,
                   state registration and stated completion date from a permit record), and `supersede`: published
                   facts a corrected value replaces ([{fact_id, reason}]; never deleted)
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from marden.core import checks, db, documents
from marden.core.business_day import business_today
from marden.core.checks import ValidationError
from marden.core.db import Conn, Row
from marden.core.facts import FactWriter
from marden.core.jsonutil import jsonb

PRODUCER = "curation:facilities"
NOTE_METHOD = "financing note v0.1"
NOTE_FIELD = "financing_note"
NOTE_KINDS = frozenset({"owner_funded", "not_found"})
CHAIN_ROLES = frozenset({"end_user", "tenant", "developer", "lenders"})
SOURCE_NAMES = {"sec.gov": "SEC EDGAR", "epoch.ai": "Epoch AI", "dbrs.morningstar.com": "Morningstar DBRS",
                "kbra.com": "KBRA", "bisnow.com": "Bisnow", "baxtel.com": "Baxtel",
                "tdlr.texas.gov": "Texas Department of Licensing and Regulation (TABS)"}


def source_name(url: str) -> str:
    host = (urlparse(url).hostname or "").removeprefix("www.")
    for suffix, name in SOURCE_NAMES.items():
        if host == suffix or host.endswith("." + suffix):
            return name
    return host or url


def stored_document(conn: Conn, url: str) -> Row:
    """The newest stored copy of `url` (any source). Raw first: a URL never ingested is refused."""
    row = conn.execute("SELECT * FROM document WHERE url = %s ORDER BY fetched_at DESC, id DESC LIMIT 1",
                       (url,)).fetchone()
    if row is None:
        raise ValidationError(f"{url} is not stored: run `agents ingest {url}` first")
    return row


def checked_quote(conn: Conn, url: str, quote: str | Sequence[str]) -> tuple[int, str]:
    """(document id, quote as stored) once every fragment is found verbatim in the stored text."""
    from marden.agents.text import find_quote, read_text

    parts = [quote] if isinstance(quote, str) else list(quote)
    parts = [p.strip() for p in parts if isinstance(p, str) and p.strip()]
    if not parts:
        raise ValidationError(f"a quote from {url} is required")
    row = stored_document(conn, url)
    doc = documents.DocumentRow.from_row(row, was_new=False)
    text = read_text(doc.read(), doc.content_type, doc.url)
    for part in parts:
        if find_quote(text, part) is None:
            raise ValidationError(f"quote not in the stored copy of {url} (document {doc.id}): {part[:120]!r}")
    return doc.id, " ... ".join(parts)


def add_fact(conn: Conn, subject_type: str, subject_id: str, field: str, value: Any, *, tag: str, url: str,
             quote: str | Sequence[str], valid_from: date | str, unit: str | None = None,
             method_version: str | None = None, source: str | None = None) -> int:
    """A published, quoted fact citing a stored document. Idempotent (the fact writer's dedupe key)."""
    if value is None:
        raise ValidationError(f"{subject_id}.{field}: an unknown value is not written (n/d)")
    doc_id, stored = checked_quote(conn, url, quote)
    res = FactWriter(conn).add(subject_type, subject_id, field, value, tag=tag, source_name=source or source_name(url),
                               source_url=url, valid_from=valid_from, producer=PRODUCER, status="published",
                               unit=unit, document_id=doc_id, quote=stored,
                               method_version=method_version or ("curation v0.1" if tag == "M" else None))
    return res.id


def _mark(conn: Conn, subject_type: str, subject_id: str, field: str, reason: str) -> None:
    from marden.readmodels.deps import mark_dirty_for

    mark_dirty_for(subject_type, subject_id, field, conn=conn, reason=reason)


def upsert_site(conn: Conn, slug: str, name: str, *, state: str | None = None, county: str | None = None,
                lat: float | None = None, lon: float | None = None, tracking_names: Sequence[str] = (),
                in_v1: bool = False) -> None:
    checks.non_empty(slug, "slug")
    checks.non_empty(name, "name")
    conn.execute(
        """INSERT INTO site (slug, name, state, county, geom, tracking_names, in_v1)
           VALUES (%(slug)s, %(name)s, %(state)s, %(county)s,
                   CASE WHEN %(lat)s::float8 IS NULL OR %(lon)s::float8 IS NULL THEN NULL
                        ELSE ST_SetSRID(ST_MakePoint(%(lon)s::float8, %(lat)s::float8), 4326) END,
                   %(names)s, %(in_v1)s)
           ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name, state = EXCLUDED.state, county = EXCLUDED.county,
             geom = EXCLUDED.geom, tracking_names = EXCLUDED.tracking_names""",
        {"slug": slug, "name": name, "state": state, "county": county, "lat": lat, "lon": lon,
         "names": list(tracking_names), "in_v1": in_v1},
    )
    _mark(conn, "site", slug, "name", f"{PRODUCER}:site")


def set_chain_party(conn: Conn, slug: str, role: str, label: str, entity_id: str | None = None) -> None:
    if role not in CHAIN_ROLES:
        raise ValidationError(f"chain role must be one of {sorted(CHAIN_ROLES)}, got {role!r}")
    checks.non_empty(label, "label")
    conn.execute(
        """INSERT INTO chain_party (site_slug, role, entity_id, label) VALUES (%s, %s, %s, %s)
           ON CONFLICT (site_slug, role) DO UPDATE SET entity_id = EXCLUDED.entity_id, label = EXCLUDED.label""",
        (slug, role, entity_id, label),
    )
    _mark(conn, "site", slug, "chain", f"{PRODUCER}:chain")


def upsert_instrument(conn: Conn, inst_id: str, *, name: str, kind: str, issuer_name: str,
                      site_slug: str | None, cusip: str | None = None, isin: str | None = None,
                      issuer_entity_id: str | None = None) -> None:
    """Create the instrument, or (an existing one) put it on `site_slug`, keeping its name and identifiers."""
    for value, label in ((inst_id, "id"), (name, "name"), (kind, "kind"), (issuer_name, "issuer_name")):
        checks.non_empty(value, label)
    conn.execute(
        """INSERT INTO instrument (id, name, kind, issuer_entity_id, issuer_name, site_slug, cusip, isin)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (id) DO UPDATE SET site_slug = EXCLUDED.site_slug""",
        (inst_id, name, kind, issuer_entity_id, issuer_name, site_slug, cusip, isin),
    )
    _mark(conn, "instrument", inst_id, "site_slug", f"{PRODUCER}:instrument")
    if site_slug:
        _mark(conn, "site", site_slug, "instrument", f"{PRODUCER}:instrument")


def propose_link(conn: Conn, site: str, campus: str, *, url: str, quote: str | Sequence[str], tag: str,
                 basis: str, valid_from: date | str | None = None) -> dict[str, Any]:
    """site_link 'proposed' with its evidence fact and an open review item (a person or claude-review approves)."""
    for table, key, value in (("site", "slug", site), ("campus", "id", campus)):
        if conn.execute(f"SELECT 1 FROM {table} WHERE {key} = %s", (value,)).fetchone() is None:
            raise ValidationError(f"{table} {value!r} does not exist")
    checks.non_empty(basis, "basis")
    evidence = {"site_slug": site, "campus_id": campus, "basis": basis}
    fid = add_fact(conn, "campus", campus, "site_link_evidence", evidence, tag=tag, url=url, quote=quote,
                   valid_from=valid_from or business_today())
    created = conn.execute(
        """INSERT INTO site_link (site_slug, campus_id, evidence_fact_id, status) VALUES (%s, %s, %s, 'proposed')
           ON CONFLICT (site_slug, campus_id) DO NOTHING RETURNING site_slug""", (site, campus, fid)).fetchone()
    item = conn.execute(
        """INSERT INTO review_item (kind, ref_type, ref_id, title, reason, payload)
           VALUES ('link', 'site_link', %s, %s, %s, %s)
           ON CONFLICT (kind, ref_type, ref_id) DO UPDATE SET kind = EXCLUDED.kind RETURNING id, status""",
        (f"{site}|{campus}", f"Link financed site {site} to campus {campus}",
         "site links are material: a person approves each one once (spec 17)",
         jsonb({**evidence, "evidence_fact_id": fid, "producer": PRODUCER, "source_url": url})),
    ).fetchone()
    assert item is not None
    return {"site_link": "proposed" if created else "exists", "evidence_fact_id": fid,
            "review_item_id": item["id"], "review_status": item["status"]}


def approve_link(site: str, campus: str, *, by: str, reason: str) -> dict[str, Any]:
    """Approve through the review queue (agent_decision -> worker rule -> audit_log), as staff do in the UI."""
    from marden.agents.review import decide

    checks.non_empty(by, "by")
    checks.non_empty(reason, "reason")
    with db.transaction() as conn:
        item = conn.execute("""SELECT id, status FROM review_item WHERE kind = 'link' AND ref_type = 'site_link'
                               AND ref_id = %s""", (f"{site}|{campus}",)).fetchone()
        link = conn.execute("SELECT status FROM site_link WHERE site_slug = %s AND campus_id = %s",
                            (site, campus)).fetchone()
    if item is None or link is None:
        raise ValidationError(f"no proposed link {site} -> {campus}: propose it first")
    if item["status"] != "open":
        return {"review_item_id": item["id"], "site_link": link["status"], "already": item["status"]}
    return decide(item["id"], "approve", by, reason=reason)


def withdraw_link(site: str, campus: str, *, by: str, reason: str) -> dict[str, Any]:
    """Withdraw an approved link: reopen its review item, then reject it through the review queue."""
    from marden.agents.review import decide

    checks.non_empty(by, "by")
    checks.non_empty(reason, "reason")
    with db.transaction() as conn:
        item = conn.execute("""SELECT id, status FROM review_item WHERE kind = 'link' AND ref_type = 'site_link'
                               AND ref_id = %s FOR UPDATE""", (f"{site}|{campus}",)).fetchone()
        link = conn.execute("SELECT status FROM site_link WHERE site_slug = %s AND campus_id = %s",
                            (site, campus)).fetchone()
        if item is None or link is None:
            raise ValidationError(f"no link {site} -> {campus}")
        if link["status"] == "rejected":
            return {"review_item_id": item["id"], "site_link": "rejected", "already": "rejected"}
        if item["status"] != "open":
            conn.execute("UPDATE review_item SET status = 'open', decided_by = NULL, decided_at = NULL WHERE id = %s",
                         (item["id"],))
            conn.execute("INSERT INTO audit_log (user_id, action, target, meta) VALUES (%s, %s, %s, %s)",
                         (by, "review.reopen", f"review_item:{item['id']}",
                          jsonb({"was": item["status"], "reason": reason})))
    return decide(item["id"], "reject", by, reason=reason)


def add_financing_note(conn: Conn, campus: str, *, kind: str, note: str, url: str, quote: str | Sequence[str],
                       owner: str | None = None, payer_id: str | None = None, searched: Sequence[str] = (),
                       as_of: date | str | None = None) -> int:
    """Marden's conclusion (tag M) that the campus has no asset-level debt to link: `owner_funded` (built on
    the owner's balance sheet, its filing the source) or `not_found` (what was searched, and when)."""
    if kind not in NOTE_KINDS:
        raise ValidationError(f"financing note kind must be one of {sorted(NOTE_KINDS)}, got {kind!r}")
    checks.non_empty(note, "note")
    if kind == "owner_funded" and not owner:
        raise ValidationError("an owner-funded note names the owner")
    if conn.execute("SELECT 1 FROM campus WHERE id = %s", (campus,)).fetchone() is None:
        raise ValidationError(f"campus {campus!r} does not exist")
    day = checks.not_future(as_of or business_today(), "as_of")
    value = {"kind": kind, "owner": owner, "payerId": payer_id, "note": note,
             "searched": [s for s in searched if s], "asOf": day.isoformat()}
    return add_fact(conn, "campus", campus, NOTE_FIELD, value, tag="M", url=url, quote=quote, valid_from=day,
                    method_version=NOTE_METHOD)


# ---------------------------------------------------------------- one JSON file
def _facts(conn: Conn, subject_type: str, subject_id: str, facts: Sequence[dict[str, Any]]) -> int:
    for f in facts:
        add_fact(conn, subject_type, subject_id, f["field"], f["value"], tag=f["tag"], url=f["url"],
                 quote=f["quote"], valid_from=f["valid_from"], unit=f.get("unit"), source=f.get("source"),
                 method_version=f.get("method_version"))
    return len(facts)


def apply_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """sites (+chain, facts) -> instruments (+facts) -> notes, in one transaction; then links proposed and,
    when the spec says who approves, approved through the review queue one by one."""
    out: dict[str, Any] = {"sites": 0, "instruments": 0, "facts": 0, "notes": 0, "links": []}
    # campuses / buildings: [{"id": ..., "facts": [{field, value, tag, url, quote, valid_from, ...}]}]
    with db.transaction() as conn:
        for s in spec.get("sites", []):
            upsert_site(conn, s["slug"], s["name"], state=s.get("state"), county=s.get("county"), lat=s.get("lat"),
                        lon=s.get("lon"), tracking_names=s.get("tracking_names", ()))
            for p in s.get("chain", []):
                set_chain_party(conn, s["slug"], p["role"], p["label"], p.get("entity_id"))
            out["facts"] += _facts(conn, "site", s["slug"], s.get("facts", []))
            out["sites"] += 1
        for i in spec.get("instruments", []):
            upsert_instrument(conn, i["id"], name=i["name"], kind=i["kind"], issuer_name=i["issuer_name"],
                              site_slug=i.get("site_slug"), cusip=i.get("cusip"), isin=i.get("isin"))
            out["facts"] += _facts(conn, "instrument", i["id"], i.get("facts", []))
            out["instruments"] += 1
        for kind, table in (("campus", "campus"), ("building", "building")):
            for c in spec.get(f"{kind}es" if kind == "campus" else f"{kind}s", []):
                if conn.execute(f"SELECT 1 FROM {table} WHERE id = %s", (c["id"],)).fetchone() is None:
                    raise ValidationError(f"{kind} {c['id']!r} does not exist")
                out["facts"] += _facts(conn, kind, c["id"], c.get("facts", []))
        for item in spec.get("supersede", []):
            out.setdefault("superseded", []).extend(
                FactWriter(conn).supersede([item["fact_id"]], item["reason"], decided_by=PRODUCER))
        for n in spec.get("notes", []):
            add_financing_note(conn, n["campus"], kind=n["kind"], note=n["note"], url=n["url"], quote=n["quote"],
                               owner=n.get("owner"), payer_id=n.get("payer_id"), searched=n.get("searched", ()),
                               as_of=n.get("as_of"))
            out["notes"] += 1
        proposed = [propose_link(conn, lk["site"], lk["campus"], url=lk["url"], quote=lk["quote"], tag=lk["tag"],
                                 basis=lk["basis"]) | {"site": lk["site"], "campus": lk["campus"]}
                    for lk in spec.get("links", [])]
    by = spec.get("approve_by")
    for lk, p in zip(spec.get("links", []), proposed, strict=True):
        if by:
            p["decision"] = approve_link(lk["site"], lk["campus"], by=by, reason=lk["reason"])
        out["links"].append(p)
    return out


def load_spec(path: Path) -> dict[str, Any]:
    spec = json.loads(path.read_text())
    if not isinstance(spec, dict):
        raise ValidationError("the spec is a JSON object")
    return spec
