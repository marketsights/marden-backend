"""FERC eLibrary docket watcher: new filings and issuances in the FERC dockets tied to our subjects.

Watched dockets (`watched_dockets`) come from the seed load, never from a guess: FERC docket entities
(`entity.kind = 'docket'`, proceeding key `FERC_<number>`) with the financed sites in their
`linked_sites` fact, plants whose `dockets` fact lists the FERC number, and the sites, plants and
campuses the archive tied to the docket sheet (published `docket` events citing
elibrary.ferc.gov/eLibrary/docketsheet?DOCKET=<number>). Dockets with none of these are listed in
the run meta and not polled.

Hourly (spec 22 "What runs when"): for each watched docket, one eLibrary Advanced Search for items
posted in the trailing window (30 days, reaching back to a week before the last good run after an
outage), stored raw. Each filing or issuance not yet recorded gets its main file (first public PDF,
else the first public file) fetched raw as a `document` (doc_type ferc_issuance / ferc_submittal,
external_id = accession, meta: dates, FERC class/type, dockets, filers, file links). Orders,
deficiency letters and responses, in-service notices and certificate filings get
extract_state='queued' for the extraction workers; nothing is extracted here. Every item in the
window is an event (kind 'docket', tag F, source_url = its eLibrary file list, as the seed load
wrote them) on each of the docket's subjects; idempotent through the event dedupe key. An item
whose title meets the materiality rule (`material_text`: an order denying, a deficiency letter ...)
is a candidate with an open review item: a person publishes it.

`python -m marden collect ferc [--docket CP26-80] [--list]`.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from marden import cli
from marden.collectors.ferc_api import ElibraryClient
from marden.collectors.ferc_parse import Filing, check_docket, parse_search
from marden.collectors.rates_common import ET, ParseError, today_et
from marden.core import db, heartbeat
from marden.core.db import Conn
from marden.core.http import FetchError
from marden.core.jsonutil import jsonb
from marden.core.queue import app
from marden.core.records import EventWriter
from marden.core.sources import SourceBlocked, declare_source, get_source, upsert_spec
from marden.seed.facilities_sink import material_text

log = logging.getLogger(__name__)

SOURCE_ID = "ferc"
SOURCE_NAME = "FERC eLibrary"
PRODUCER = f"collector:{SOURCE_ID}"
CRON_UTC = "23 * * * *"  # hourly, off the top of the hour
WINDOW_DAYS = 30
OVERLAP_DAYS = 7
MAX_LOOKBACK_DAYS = 365
MAX_PAGES = 20
FILING_DOC_TYPES = ("ferc_issuance", "ferc_submittal")
SUBJECT_TABLES = {"site": ("site", "slug"), "plant": ("power_plant", "id"), "campus": ("campus", "id")}

TERMS_NOTE = (
    "Checked 2026-10-21. robots.txt: https://elibrary.ferc.gov/robots.txt answers 404 (none); "
    "https://www.ferc.gov/robots.txt disallows only site paths (/core/, /profiles/, /admin/, /search/, /user/..., "
    "/node/add/) and does not cover the eLibrary host. FERC's website policy pages (www.ferc.gov/privacy-policy, "
    "/website-policies, /comment-policy) answer the declared MardenBot User-Agent with a Cloudflare \"Just a "
    "moment...\" challenge, so their text could not be read (not bypassed); no FERC clause restricting automated "
    "access to eLibrary was found. Public-record basis: 18 CFR 388.106(a) (eCFR, 2026-10-01): \"Publicly available "
    "documents may be obtained electronically from the Commission's website, https://www.ferc.gov, or by requesting "
    "them from the public.referenceroom@ferc.gov by reasonably describing the records sought.\" 388.106(b): \"The "
    "public records of the Commission that are available for inspection and copying upon request via the "
    "Commission's website, include: (1) Applications, declarations, complaints, petitions, and other papers seeking "
    "Commission action; ... (7) Commission orders, notices, findings, opinions, determinations, and other actions in "
    "a matter or proceeding\". eLibrary Privacy Impact Assessment (April 2, 2019, "
    "https://www.ferc.gov/sites/default/files/2020-05/PIA-eLibrary.pdf): \"The public and FERC staff have access to "
    "eLibrary, and they have the ability to search the text of scanned documents using optical character "
    "recognition and the option to request that documents be printed or downloaded at one’s computer or in the "  # noqa: RUF001 (verbatim)
    "Public Reference Room.\" and \"End Users: All end users have read access to the public data in the eLibrary "
    "repository.\" Commission issuances are U.S. Government works (17 U.S.C. 105); third-party filings keep their "
    "authors' rights, so Marden stores them for extraction and links to eLibrary. Access: the JSON endpoints the "
    "eLibrary web app calls (POST /eLibraryWebAPI/api/Search/AdvancedSearch and /File/DownloadP8File; no login or "
    "key), declared MardenBot User-Agent with contact, 1 request every 2 s, hourly, only dockets tied to our sites, "
    "plants and campuses, only public (availability P) files, one main file per filing. A Cloudflare challenge, "
    "401/403 or a persisting 429 stops the run and is recorded here; nothing is retried with other headers."
)

SOURCE = declare_source(SOURCE_ID, SOURCE_NAME, "portal", "https://elibrary.ferc.gov/eLibrary", 60, TERMS_NOTE)


# ---------------------------------------------------------------- watch list
@dataclass
class Watched:
    docket: str
    subjects: dict[tuple[str, str], list[str]] = field(default_factory=dict)  # (type, id) -> why

    def add(self, subject_type: str, subject_id: str, why: str) -> None:
        reasons = self.subjects.setdefault((subject_type, subject_id), [])
        if why not in reasons:
            reasons.append(why)

    def subject_keys(self) -> list[str]:
        return [f"{t}:{i}" for t, i in sorted(self.subjects)]


def _exists(subject_type: str) -> str:
    table, key = SUBJECT_TABLES[subject_type]
    return f"EXISTS (SELECT 1 FROM {table} x WHERE x.{key} = e.subject_id)"


_ENTITY_DOCKETS = (
    "SELECT id, meta->>'proceeding_key' AS key FROM entity WHERE kind = 'docket' AND meta->>'proceeding_key' "
    "LIKE 'FERC\\_%' ORDER BY id"
)
_LINKED_SITES = """
SELECT f.subject_id AS entity_id, s.slug FROM fact_current f
  CROSS JOIN LATERAL jsonb_array_elements_text(
      CASE WHEN jsonb_typeof(f.value) = 'array' THEN f.value ELSE '[]'::jsonb END) AS l(slug)
  JOIN site s ON s.slug = l.slug
 WHERE f.subject_type = 'entity' AND f.field = 'linked_sites'"""
_PLANT_DOCKETS = """
SELECT f.subject_id AS plant_id, d->>'number' AS docket FROM fact_current f
  CROSS JOIN LATERAL jsonb_array_elements(
      CASE WHEN jsonb_typeof(f.value) = 'array' THEN f.value ELSE '[]'::jsonb END) AS d
  JOIN power_plant p ON p.id = f.subject_id
 WHERE f.subject_type = 'plant' AND f.field = 'dockets' AND upper(trim(d->>'agency')) = 'FERC'
   AND coalesce(trim(d->>'number'), '') <> ''"""
_SHEET_EVENTS = f"""
SELECT DISTINCT e.subject_type, e.subject_id,
       substring(e.source_url from 'DOCKET=([A-Za-z]{{2}}[0-9]{{2}}-[0-9]+)') AS docket
  FROM event e
 WHERE e.status = 'published' AND e.kind = 'docket'
   AND e.source_url ILIKE 'https://elibrary.ferc.gov/eLibrary/docketsheet?DOCKET=%'
   AND ((e.subject_type = 'site' AND {_exists("site")}) OR (e.subject_type = 'plant' AND {_exists("plant")})
        OR (e.subject_type = 'campus' AND {_exists("campus")}))"""


def _number(text: str | None) -> str | None:
    try:
        return check_docket(text or "")
    except ValueError:
        return None


def watched_dockets(conn: Conn) -> tuple[list[Watched], list[str]]:
    """(dockets with at least one site, plant or campus, FERC docket numbers in our data with none)."""
    found: dict[str, Watched] = {}
    by_entity: dict[str, str] = {}

    def get(number: str) -> Watched:
        return found.setdefault(number, Watched(number))

    for row in conn.execute(_ENTITY_DOCKETS).fetchall():
        number = _number(row["key"][len("FERC_"):])
        if number:
            by_entity[row["id"]] = number
            get(number)
    for row in conn.execute(_LINKED_SITES).fetchall():
        if row["entity_id"] in by_entity:
            get(by_entity[row["entity_id"]]).add("site", row["slug"], f"linked_sites of {row['entity_id']}")
    for row in conn.execute(_PLANT_DOCKETS).fetchall():
        number = _number(row["docket"])
        if number:
            get(number).add("plant", row["plant_id"], f"dockets fact of plant {row['plant_id']}")
    for row in conn.execute(_SHEET_EVENTS).fetchall():
        number = _number(row["docket"])
        if number:
            get(number).add(row["subject_type"], row["subject_id"], "seed docket-sheet event")
    watched = [w for _, w in sorted(found.items()) if w.subjects]
    unlinked = sorted(n for n, w in found.items() if not w.subjects)
    return watched, unlinked


def window_start(today: date, last_ok: date | None, days: int = WINDOW_DAYS) -> date:
    """The trailing window, reaching back to OVERLAP_DAYS before the last good run after an outage.
    Fixed within a day, so a rerun asks the same question (and stores nothing new)."""
    since = today - timedelta(days=days)
    if last_ok is not None and last_ok - timedelta(days=OVERLAP_DAYS) < since:
        since = max(last_ok - timedelta(days=OVERLAP_DAYS), today - timedelta(days=MAX_LOOKBACK_DAYS))
    return since


# ---------------------------------------------------------------- run
@dataclass
class Stats:
    filings_seen: int = 0
    documents_seen: int = 0
    documents_new: int = 0
    files_fetched: int = 0
    queued: int = 0
    events_seen: int = 0
    events_new: int = 0
    events_candidate: int = 0
    reviews_opened: int = 0
    skipped: Counter = field(default_factory=Counter)
    dockets: dict[str, dict[str, Any]] = field(default_factory=dict)
    newest: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def as_meta(self) -> dict[str, Any]:
        newest = sorted(self.newest, key=lambda r: (r["date"], r["accession"]), reverse=True)[:15]
        return {
            **self.extra, "filings_seen": self.filings_seen, "documents_seen": self.documents_seen,
            "documents_new": self.documents_new, "files_fetched": self.files_fetched,
            "queued_for_extraction": self.queued, "files_not_fetched": dict(sorted(self.skipped.items())),
            "events_seen": self.events_seen, "events_new": self.events_new,
            "events_candidate": self.events_candidate, "reviews_opened": self.reviews_opened,
            "dockets": self.dockets, "newest_new_items": newest,
        }


def _known_document(conn: Conn, accession: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM document WHERE source_id = %s AND external_id = %s AND doc_type = ANY(%s) ORDER BY id LIMIT 1",
        (SOURCE_ID, accession, list(FILING_DOC_TYPES)),
    ).fetchone()
    return row["id"] if row else None


def _review(conn: Conn, event_id: int, filing: Filing, rule: str, subject: tuple[str, str]) -> bool:
    row = conn.execute(
        """INSERT INTO review_item (kind, ref_type, ref_id, title, reason, payload)
           VALUES ('event', 'event', %s, %s, %s, %s) ON CONFLICT (kind, ref_type, ref_id) DO NOTHING RETURNING id""",
        (str(event_id), filing.title[:300], f"material event ({rule}): a person publishes it",
         jsonb({"subject_type": subject[0], "subject_id": subject[1], "source_url": filing.filelist_url,
                "accession": filing.accession, "producer": PRODUCER})),
    ).fetchone()
    return row is not None


class DocketRun:
    """One run's work: search pages per docket, main files for unseen filings, events."""

    def __init__(self, client: ElibraryClient, stats: Stats, since: date, until: date):
        self.client = client
        self.stats = stats
        self.since = since
        self.until = until
        self.documents: dict[str, int] = {}  # accession -> document id (this run)

    def filings(self, docket: str) -> tuple[list[tuple[Filing, int]], dict[str, Any]]:
        out: list[tuple[Filing, int]] = []
        info: dict[str, Any] = {"pages": 0, "hits": 0}
        for page in range(MAX_PAGES):
            doc = self.client.search(docket, self.since, self.until, page)
            self._count_doc(doc.was_new)
            total, filings = parse_search(doc.read())
            info["pages"], info["hits"] = page + 1, total
            out.extend((f, doc.id) for f in filings)
            if not filings or len(out) >= total:
                break
        else:
            info["truncated"] = True  # more than MAX_PAGES pages in the window: the rest waits for the next run
        return out, info

    def _count_doc(self, was_new: bool) -> None:
        self.stats.documents_seen += 1
        self.stats.documents_new += int(was_new)

    def document_for(self, filing: Filing, search_doc_id: int) -> int:
        """The filing's main-file document: known, fetched now, or (nothing fetchable) the search page."""
        if filing.accession in self.documents:
            return self.documents[filing.accession]
        with db.transaction() as conn:
            known = _known_document(conn, filing.accession)
        if known is not None:
            self.documents[filing.accession] = known
            return known
        main, why = filing.main_file()
        if main is None:
            self.stats.skipped[why or "unknown"] += 1
            return search_doc_id
        reasons = filing.queue_reasons()
        doc = self.client.download(filing, main, meta={**filing.as_meta(), "queue_reasons": reasons},
                                   extract_state="queued" if reasons else "none")
        self._count_doc(doc.was_new)
        self.stats.files_fetched += 1
        self.stats.queued += int(doc.was_new and doc.extract_state == "queued")
        self.documents[filing.accession] = doc.id
        return doc.id

    def docket(self, watched: Watched) -> None:
        filings, info = self.filings(watched.docket)
        subjects = sorted(watched.subjects)
        info["subjects"] = watched.subject_keys()
        new_items = 0
        for filing, search_doc_id in filings:
            self.stats.filings_seen += 1
            doc_id = self.document_for(filing, search_doc_id)
            rule = material_text(filing.title)
            created_any = False
            with db.transaction() as conn:
                events = EventWriter(conn)
                for subject in subjects:
                    res = events.add(
                        subject[0], subject[1], filing.day, "docket", filing.title, tag="F", source_name=SOURCE_NAME,
                        source_url=filing.filelist_url, document_id=doc_id, material=bool(rule),
                        status="candidate" if rule else "published",
                    )
                    self.stats.events_seen += 1
                    if res.created:
                        created_any = True
                        self.stats.events_new += 1
                        self.stats.events_candidate += int(res.status == "candidate")
                        if rule:
                            self.stats.reviews_opened += int(_review(conn, res.id, filing, rule, subject))
            if created_any:  # new to Marden on at least one of the docket's subjects
                new_items += 1
                self.stats.newest.append({"docket": watched.docket, "accession": filing.accession,
                                          "date": filing.day.isoformat(), "category": filing.category,
                                          "title": filing.title[:160]})
        info["items_with_new_events"] = new_items
        if filings:
            top = max(filings, key=lambda p: (p[0].day, p[0].accession))[0]
            info["newest"] = {"accession": top.accession, "date": top.day.isoformat(), "title": top.title[:160]}
        self.stats.dockets[watched.docket] = info


def _last_ok(source_id: str) -> date | None:
    row = get_source(source_id)
    return row["last_ok_at"].astimezone(ET).date() if row and row["last_ok_at"] else None


def run(
    *,
    dockets: list[str] | None = None,
    today: date | None = None,
    window_days: int = WINDOW_DAYS,
    client: ElibraryClient | None = None,
) -> dict[str, Any]:
    """One collection run (module docstring). Failures are recorded on the source and returned."""
    upsert_spec(SOURCE)
    today = today or today_et()
    stats = Stats()
    with db.transaction() as conn:
        watched, unlinked = watched_dockets(conn)
    if dockets:
        wanted = {check_docket(d) for d in dockets}
        missing = sorted(wanted - {w.docket for w in watched})
        if missing:
            raise ValueError(f"not watched (no site, plant or campus tied to them): {missing}")
        watched = [w for w in watched if w.docket in wanted]
    client = client or ElibraryClient(SOURCE_ID)
    try:
        with heartbeat.run(SOURCE_ID) as hb:
            hb.meta = stats.extra
            try:
                since = window_start(today, _last_ok(SOURCE_ID), window_days)
                stats.extra.update(window={"posted_from": since.isoformat(), "posted_to": today.isoformat()},
                                   dockets_watched=len(watched), dockets_unlinked=unlinked)
                work = DocketRun(client, stats, since, today)
                for w in watched:
                    work.docket(w)
            finally:
                stats.extra["requests"] = client.requests
                hb.meta = stats.as_meta()
            hb.seen(stats.filings_seen)
            hb.new(stats.events_new)
    except (SourceBlocked, FetchError, ParseError) as exc:
        log.warning("%s: %s: %s", SOURCE_ID, type(exc).__name__, exc)
        return {**stats.as_meta(), "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {**stats.as_meta(), "ok": True}


# ---------------------------------------------------------------- schedule and CLI
@app.periodic(cron=CRON_UTC, periodic_id="hourly")
@app.task(name="collect.ferc", queue="collect", queueing_lock="collect.ferc", lock="collect.ferc")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    return run()


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--docket", action="append", dest="dockets", help="only this watched docket (repeatable)")
    p.add_argument("--list", action="store_true", help="print the watch list and exit (no requests)")


@cli.action("collect", SOURCE_ID, help="FERC eLibrary: new filings in watched dockets -> documents, events",
            configure=_args)
def collect_cli(args: argparse.Namespace) -> int:
    if args.list:
        with db.transaction() as conn:
            watched, unlinked = watched_dockets(conn)
        listing: dict[str, Any] = defaultdict(dict)
        for w in watched:
            listing["watched"][w.docket] = {f"{t}:{i}": why for (t, i), why in sorted(w.subjects.items())}
        listing["unlinked"] = unlinked
        print(json.dumps(listing, indent=2))
        return 0
    try:
        out = run(dockets=args.dockets)
    except ValueError as exc:
        print(f"ferc: {exc}")
        return 2
    print(json.dumps(out, indent=2, default=str))
    return 0 if out.get("ok") else 1
