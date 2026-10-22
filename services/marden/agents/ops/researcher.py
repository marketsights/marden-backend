"""Researcher (spec 22: "Each week, searches for fields still marked not disclosed" -> new documents for the
Extractor). Weekly on queue 'agents' (`agents.research`), or `python -m marden agents research`.

1. The worklist (`gaps.collect`): n/d items on watched and financed subjects, from the project-file checklist,
   the cushion model's missing inputs and instruments without size, pricing or maturity facts.
2. Searches (`plan`): EDGAR full-text search, one query per subject phrase and gap term; FERC eLibrary text
   search on the subject's names for power and fuel gaps; items an existing collector covers are checked on
   that collector's source (and the collector is run for the state when it has missed its cadence); items no
   source Marden may read covers are listed with why. Watched subjects first, then queries never or least
   recently run, heavier gaps first; at most MARDEN_RESEARCH_MAX_QUERIES (EDGAR) and MARDEN_RESEARCH_MAX_FERC
   queries a run, the rest wait for the next run. Each query's window starts where its last good search ended
   (less a week of overlap), else a year back.
3. Every answer is stored raw; each wanted hit Marden does not hold is fetched raw (at most
   MARDEN_RESEARCH_MAX_DOCS a run), checked to name the subject phrase, and handed to the intake
   (extract_state 'queued'; with the agents configured, an agent_job at priority 90 watched / 85 financed). A
   hit Marden holds but never queued is queued; one already read or waiting is recorded as such.
4. The report (agent_ops_run, research_search, research_find) says what was searched, found and queued.
No model is called. A refusal (401/403/429, a challenge page) stops that endpoint's searches for the run.
"""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any

from marden.agents.ops import gaps as gaps_mod
from marden.agents.ops import research_store as store
from marden.agents.ops import runs
from marden.agents.ops.research_plan import Query, doc_text, plan, relevance, window
from marden.agents.ops.research_sources import check_collectors
from marden.agents.ops.search_edgar import SOURCE as EDGAR_SOURCE
from marden.agents.ops.search_edgar import EdgarSearch, Hit, SearchBlocked
from marden.agents.ops.search_ferc import SOURCE as FERC_SOURCE
from marden.agents.ops.search_ferc import FercSearch
from marden.collectors.market_http import Challenged
from marden.core import db, documents, heartbeat
from marden.core.documents import DocumentRow
from marden.core.http import FetchError
from marden.core.sources import SourceError, upsert_spec
from marden.readmodels.context import business_today

log = logging.getLogger(__name__)

HITS_PER_QUERY = 3


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


class Run:
    def __init__(self, run_id: int, today: date, *, edgar: EdgarSearch, ferc: FercSearch, configured: bool,
                 max_docs: int):
        self.run_id, self.today, self.edgar, self.ferc = run_id, today, edgar, ferc
        self.configured = configured
        self.docs_left = max_docs
        self.seen_urls: set[str] = set()
        self.outcomes: dict[str, int] = {}
        self.queued: list[dict[str, Any]] = []
        self.errors: list[str] = []

    def research_meta(self, q: Query) -> dict[str, Any]:
        return {"run_id": self.run_id, "query": q.query, "endpoint": q.endpoint, "subjects": q.subjects(),
                "items": q.items()}

    def _job(self, doc_id: int, q: Query) -> int | None:
        if not self.configured:
            return None
        from marden.agents.pipeline import enqueue_document

        return enqueue_document(doc_id, priority=90 if q.watched else 85,
                                reason=f"researcher: {', '.join(q.items())}"[:200])

    def _record(self, search_id: int, url: str, outcome: str, **kw: Any) -> None:
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1
        with db.transaction() as conn:
            store.add_find(conn, search_id, url=url, outcome=outcome, **kw)

    def take(self, search_id: int, q: Query, url: str, title: str, form: str | None, filed: date | None,
             fetch: Any, filer_cik: str | None = None) -> None:
        """One hit: recorded as read / waiting / queued (fetched, or already held) / skipped / failed."""
        info = {"title": title, "form": form, "filed": filed}
        with db.transaction() as conn:
            held = store.held(conn, url)
        if held is not None:
            outcome, reason = store.outcome_for_held(held)
            if outcome == "queued":  # stored but never queued: the same check as a new file first
                stored = documents.get_document(int(held["id"]))
                ok, why = relevance(doc_text(stored) if stored else None, q, filer_cik)
                if not ok:
                    self._record(search_id, url, "skipped", reason=f"held, not queued: {why}",
                                 document_id=held["id"], **info)
                    return
                with db.transaction() as conn:
                    store.queue_held(conn, held["id"], self.research_meta(q))
                reason = f"{reason}: {why}"
            job = self._job(held["id"], q) if outcome == "queued" else held["job_id"]
            if outcome == "queued":
                self.queued.append({"document_id": held["id"], "url": url, "title": title, "items": q.items(),
                                    "subjects": q.subjects(), "job_id": job, "fetched": False})
            self._record(search_id, url, outcome, reason=reason, document_id=held["id"], job_id=job, **info)
            return
        if self.docs_left <= 0:
            self._record(search_id, url, "skipped", reason="document budget for this run reached (next run)", **info)
            return
        try:
            doc = fetch()
        except (FetchError, SourceError, OSError, ValueError) as exc:
            self.errors.append(f"{url}: {exc}"[:300])
            self._record(search_id, url, "failed", reason=str(exc)[:300], **info)
            return
        self.docs_left -= 1
        ok, why = relevance(doc_text(doc), q, filer_cik)
        if not ok:
            self._record(search_id, url, "skipped", reason=f"stored raw, not queued: {why}", document_id=doc.id,
                         **info)
            return
        with db.transaction() as conn:
            conn.execute("UPDATE document SET extract_state = 'queued' WHERE id = %s AND extract_state = 'none'",
                         (doc.id,))
        job = self._job(doc.id, q)
        self.queued.append({"document_id": doc.id, "url": url, "title": title, "items": q.items(),
                            "subjects": q.subjects(), "job_id": job, "fetched": True})
        self._record(search_id, url, "queued", reason=f"fetched raw and queued for the extractor: {why}",
                     document_id=doc.id, job_id=job, **info)

    def edgar_query(self, q: Query) -> None:
        with db.transaction() as conn:
            since, until = window(conn, q, self.today)
        try:
            doc, total, hits = self.edgar.search(q.query, since, until, q.ciks)
        except SearchBlocked:
            with db.transaction() as conn:
                store.add_search(conn, run_id=self.run_id, endpoint=q.endpoint, query=q.query, since=since,
                                 until=until, subjects=q.subjects(), gaps=q.items(), status="blocked",
                                 error="EDGAR refused or answered a page that is not its JSON")
            raise
        except FetchError as exc:
            self.errors.append(f"{q.query}: {exc}"[:300])
            with db.transaction() as conn:
                store.add_search(conn, run_id=self.run_id, endpoint=q.endpoint, query=q.query, since=since,
                                 until=until, subjects=q.subjects(), gaps=q.items(), status="failed", error=str(exc))
            return
        with db.transaction() as conn:
            sid = store.add_search(conn, run_id=self.run_id, endpoint=q.endpoint, query=q.query, since=since,
                                   until=until, subjects=q.subjects(), gaps=q.items(), status="ok",
                                   document_id=doc.id, hits=total)
        taken = 0
        for hit in hits:
            if taken >= HITS_PER_QUERY:
                break
            if not hit.wanted or hit.url in self.seen_urls:
                continue
            self.seen_urls.add(hit.url)
            taken += 1
            self.take(sid, q, hit.url, f"{hit.form} {hit.file_type} {hit.filer}", hit.form, hit.filed,
                      lambda h=hit, qq=q: self._fetch_edgar(h, qq), filer_cik=hit.cik)

    def _fetch_edgar(self, hit: Hit, q: Query) -> DocumentRow:
        doc = self.edgar.fetch(hit, {"research": self.research_meta(q)})
        return doc

    def ferc_query(self, q: Query) -> None:
        with db.transaction() as conn:
            since, until = window(conn, q, self.today)
        try:
            doc, total, filings = self.ferc.search(q.phrase, since, until)
        except Challenged:
            with db.transaction() as conn:
                store.add_search(conn, run_id=self.run_id, endpoint=q.endpoint, query=q.query, since=since,
                                 until=until, subjects=q.subjects(), gaps=q.items(), status="blocked",
                                 error="eLibrary refused or answered a challenge page")
            raise
        except (FetchError, ValueError) as exc:
            self.errors.append(f"eLibrary {q.query}: {exc}"[:300])
            with db.transaction() as conn:
                store.add_search(conn, run_id=self.run_id, endpoint=q.endpoint, query=q.query, since=since,
                                 until=until, subjects=q.subjects(), gaps=q.items(), status="failed", error=str(exc))
            return
        with db.transaction() as conn:
            sid = store.add_search(conn, run_id=self.run_id, endpoint=q.endpoint, query=q.query, since=since,
                                   until=until, subjects=q.subjects(), gaps=q.items(), status="ok",
                                   document_id=doc.id, hits=total)
        taken = 0
        for filing in filings:
            if taken >= HITS_PER_QUERY:
                break
            main, why = filing.main_file()
            url = main.url if main is not None else filing.filelist_url
            if url in self.seen_urls:
                continue
            self.seen_urls.add(url)
            taken += 1
            if main is None:
                self._record(sid, url, "skipped", reason=f"no public main file ({why})", title=filing.title,
                             form=filing.category, filed=filing.day)
                continue
            meta = {"research": self.research_meta(q)}
            self.take(sid, q, url, filing.title, filing.category, filing.day,
                      lambda f=filing, m=main, mm=meta: self.ferc.fetch(f, m, mm))


def _endpoint(name: str, source_id: str, queries: list[Query], limit: int, each: Any,
              stop: type[Exception]) -> dict[str, Any]:
    """Run up to `limit` queries inside the source's heartbeat; a refusal stops the rest and fails the beat."""
    out: dict[str, Any] = {"planned": len(queries), "run": 0, "deferred": max(0, len(queries) - limit),
                           "blocked": None}
    if not queries:
        return out
    try:
        with heartbeat.run(source_id) as hb:
            for q in queries[:limit]:
                out["run"] += 1
                hb.seen()
                try:
                    each(q)
                except stop as exc:
                    out["blocked"] = str(exc)[:300]
                    out["deferred"] = len(queries) - out["run"]
                    raise
            hb.meta.update({k: v for k, v in out.items() if k != "blocked"})
    except stop:
        log.warning("researcher: %s searches stopped: %s", name, out["blocked"])
    return out


def run(today: date | None = None, *, trigger: str = "manual", edgar: EdgarSearch | None = None,
        ferc: FercSearch | None = None, max_queries: int | None = None, max_ferc: int | None = None,
        max_docs: int | None = None, collectors: bool = True) -> dict[str, Any]:
    """One researcher run (module docstring); returns the report stored on agent_ops_run."""
    from marden.agents.config import detect_backend

    today = today or business_today()
    run_id = runs.start("researcher", trigger=trigger)
    report: dict[str, Any] = {"run_id": run_id, "today": today.isoformat()}
    try:
        for spec in (EDGAR_SOURCE, FERC_SOURCE):
            upsert_spec(spec)
        status = detect_backend()
        with db.transaction() as conn:
            all_gaps = gaps_mod.collect(conn)
            last = {e: store.last_searched(conn, e) for e in ("edgar_fts", "ferc_elibrary")}
        p = plan(all_gaps, last)
        r = Run(run_id, today, edgar=edgar or EdgarSearch(), ferc=ferc or FercSearch(), configured=status.configured,
                max_docs=_env_int("MARDEN_RESEARCH_MAX_DOCS", 25) if max_docs is None else max_docs)
        subjects = {(g.subject.type, g.subject.id) for g in all_gaps}
        report["gaps"] = {"total": len(all_gaps), "subjects": len(subjects),
                          "watched_subjects": len({(g.subject.type, g.subject.id) for g in all_gaps
                                                   if g.subject.watched}),
                          "by_origin": _count(g.origin for g in all_gaps)}
        report["edgar"] = _endpoint("EDGAR", EDGAR_SOURCE.id, p.edgar,
                                    (_env_int("MARDEN_RESEARCH_MAX_QUERIES", 120) if max_queries is None
                                     else max_queries),
                                    r.edgar_query,
                                    SearchBlocked)
        report["ferc"] = _endpoint("eLibrary", FERC_SOURCE.id, p.ferc,
                                   _env_int("MARDEN_RESEARCH_MAX_FERC", 20) if max_ferc is None else max_ferc,
                                   r.ferc_query, Challenged)
        report["collectors"] = check_collectors(p.collector_gaps, run=collectors)
        report["requests"] = {"edgar": r.edgar.requests, "ferc": r.ferc.requests}
        report["finds"] = r.outcomes
        report["queued"] = r.queued
        report["not_searched"] = p.not_searched
        report["errors"] = r.errors[:50]
        report["agents"] = {"backend": status.kind, "detail": status.detail,
                            "jobs": sum(1 for q in r.queued if q["job_id"] is not None)}
        if not status.configured and r.queued:
            report["agents"]["note"] = (f"agents not configured: {len(r.queued)} document(s) wait in the intake "
                                        "(extract_state 'queued') until a backend exists")
        partial = bool(r.errors or report["edgar"]["blocked"] or report["ferc"]["blocked"])
        runs.finish(run_id, "partial" if partial else "ok", report=report, model_status="not_used")
        return report
    except Exception as exc:
        runs.finish(run_id, "failed", report=report, model_status="not_used", detail=f"{type(exc).__name__}: {exc}")
        raise


def _count(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))
