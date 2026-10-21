"""Shared rules for loading marden_data_2026-10-20.zip (docs/seed-data/DATA_DICTIONARY.md).

Every seed loader uses these helpers so tags, source names, entities and seed documents are
derived the same way:

- ``tag_for_url`` / ``source_name_for_url``: the seed data carries no F/R/M tag or source name,
  so both come from the source URL's host (dictionary section 3, "Tag and source rules").
- ``seed_document``: the seed file itself is stored raw and recorded as a ``document`` of the
  ``seed_marden_data`` source, so every seeded fact can point at the file it came from.
- ``ensure_entity``: one place that turns a party name into an ``entity`` row, matching on id,
  alias or CIK before inserting.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from marden.core.config import get_settings
from marden.core.db import Conn
from marden.core.documents import DocumentRow, record_document
from marden.core.sources import declare_source

SEED_SOURCE_ID = "seed_marden_data"
SEED_ARCHIVE = "marden_data_2026-10-20"
SEED_AS_OF = date(2026, 10, 20)  # zip date: fallback as-of when a record and its file have none

declare_source(
    SEED_SOURCE_ID,
    "Marden research archive (Oct 20, 2026)",
    "seed",
    None,
    None,
    "Marden's own research files; every value keeps the public URL it was taken from.",
)

# Hosts whose documents are filed records (tag F). Matched on the host or any parent domain.
FILED_HOSTS = (
    "sec.gov", "ferc.gov", "finra.org", "ercot.com", "pjm.com", "misoenergy.org", "spp.org",
    "eia.gov", "puc.texas.gov", "tceq.texas.gov", "rrc.texas.gov", "env.nm.gov", "nmprc.state.nm.us",
    "psc.wi.gov", "lpsc.louisiana.gov", "psc.nd.gov", "scc.virginia.gov", "dps.ny.gov",
    "michigan.gov", "ohio.gov", "epa.ohio.gov", "deq.virginia.gov", "gaepd.org", "azdeq.gov",
    "in.gov", "pa.gov", "courtlistener.com", "pacer.uscourts.gov", "uscourts.gov",
    "civicclerk.com", "granicus.com", "legistar.com", "municode.com", "boarddocs.com",
    "revize.com",  # county and city document CMS (e.g. Doña Ana County IRB ordinances)
    "wfparish.org",  # West Feliciana Parish government (site plans, permits)
)
# Government hosts not listed above are filed records too (counties, state agencies).
GOV_SUFFIXES = (".gov", ".us", ".gov.au")

SOURCE_NAMES = {
    "sec.gov": "SEC EDGAR",
    "elibrary.ferc.gov": "FERC eLibrary",
    "ferc.gov": "FERC",
    "finra.org": "FINRA TRACE",
    "ercot.com": "ERCOT",
    "pjm.com": "PJM",
    "misoenergy.org": "MISO",
    "spp.org": "SPP",
    "eia.gov": "EIA",
    "puc.texas.gov": "PUCT",
    "tceq.texas.gov": "TCEQ",
    "rrc.texas.gov": "Texas RRC",
    "env.nm.gov": "NMED",
    "psc.wi.gov": "Wisconsin PSC",
    "lpsc.louisiana.gov": "Louisiana PSC",
    "psc.nd.gov": "North Dakota PSC",
    "scc.virginia.gov": "Virginia SCC",
    "dps.ny.gov": "New York DPS",
    "courtlistener.com": "CourtListener",
    "epoch.ai": "Epoch AI",
    "reuters.com": "Reuters",
    "bloomberg.com": "Bloomberg",
    "ft.com": "Financial Times",
    "wsj.com": "Wall Street Journal",
    "theinformation.com": "The Information",
    "datacenterdynamics.com": "DCD",
    "baxtel.com": "Baxtel",
    "pgjonline.com": "Pipeline & Gas Journal",
    "kbra.com": "KBRA",
    "spglobal.com": "S&P Global",
    "moodys.com": "Moody's",
    "fitchratings.com": "Fitch",
    "boerse-frankfurt.de": "Börse Frankfurt",
    "boerse-stuttgart.de": "Börse Stuttgart",
    "openstreetmap.org": "OpenStreetMap",
    "news.google.com": "Google News",
}


def host_of(url: str | None) -> str:
    if not url:
        return ""
    host = urlparse(url if "//" in url else f"//{url}").hostname or ""
    return host.lower().removeprefix("www.")


def _matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def tag_for_url(url: str | None, *, default: str = "R") -> str:
    """F for filed records (SEC, FERC, state and county portals, ISOs, courts); else ``default``."""
    host = host_of(url)
    if not host:
        return default
    if any(_matches(host, d) for d in FILED_HOSTS) or host.endswith(GOV_SUFFIXES):
        return "F"
    return default


def source_name_for_url(url: str | None, *, fallback: str = "Marden research archive") -> str:
    host = host_of(url)
    if not host:
        return fallback
    best = ""
    for domain in SOURCE_NAMES:
        if _matches(host, domain) and len(domain) > len(best):
            best = domain
    if best:
        return SOURCE_NAMES[best]
    labels = host.split(".")
    if host.endswith(".us") and len(labels) >= 4 and labels[0] in ("co", "ci", "city", "county"):
        place = labels[1].replace("-", " ").title()
        state = labels[-2].upper()
        return f"{place} County ({state})" if labels[0] in ("co", "county") else f"City of {place} ({state})"
    root = labels[-2] if len(labels) >= 2 else host
    return root.replace("-", " ").title()


def tag_for_basis(basis: str | None, url: str | None = None) -> str:
    """Map the archive's ``basis`` strings (disclosed / reported / presumed ...) to a tag.

    Presumed, inferred and assumed values are Marden judgments, so they are M (with a method
    version set by the caller); disclosed is F unless the cited URL is not a filed record (a press
    release, rating-agency note or news story is R: the fact is only as strong as the source shown);
    reported is R; otherwise the URL decides.
    """
    b = (basis or "").strip().lower()
    if b.startswith(("disclosed", "filed")):
        return "R" if host_of(url) and tag_for_url(url) != "F" else "F"
    if b.startswith("reported"):
        return "R"
    if b.startswith(("presumed", "inferred", "assumed", "estimated", "derived")):
        return "M"
    return tag_for_url(url)


# ---------------------------------------------------------------- seed files


@lru_cache(maxsize=1)
def seed_dir() -> Path:
    return Path(get_settings().require_seed_dir())


def load_json(relpath: str) -> Any:
    return json.loads((seed_dir() / relpath).read_text(encoding="utf-8"))


def seed_url(relpath: str) -> str:
    return f"seed://{SEED_ARCHIVE}/{relpath}"


def seed_document(relpath: str, *, doc_type: str = "seed_file", conn: Conn | None = None) -> DocumentRow:
    """Store a seed file raw and record it as a document (idempotent on url + sha256)."""
    path = seed_dir() / relpath
    data = path.read_bytes()
    ctype = "application/json" if path.suffix == ".json" else "application/octet-stream"
    return record_document(
        SEED_SOURCE_ID, seed_url(relpath), data, content_type=ctype, doc_type=doc_type,
        title=relpath, doc_date=SEED_AS_OF, meta={"archive": SEED_ARCHIVE}, conn=conn,
    )


# ---------------------------------------------------------------- dates


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def parse_date(value: Any) -> date | None:
    """Parse the archive's date shapes: ISO dates/datetimes, 'YYYY-MM', 'YYYY', 'Mon D, YYYY'."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    m = re.match(r"^(\d{4})-(\d{2})$", s)
    if m:
        return date(int(m[1]), int(m[2]), 1)
    m = re.match(r"^([A-Za-z]{3})[a-z]*\.? (\d{1,2}),? (\d{4})$", s)
    if m and m[1].lower() in _MONTHS:
        return date(int(m[3]), _MONTHS[m[1].lower()], int(m[2]))
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        return date(int(m[3]), int(m[1]), int(m[2]))
    return None


def as_of(*candidates: Any) -> date:
    """First parseable date among the candidates, else the archive date. Never in the future."""
    for c in candidates:
        d = parse_date(c)
        if d is not None:
            return min(d, date.today())
    return SEED_AS_OF


# ---------------------------------------------------------------- entities


def slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s or "unnamed"


def ensure_entity(
    conn: Conn,
    name: str,
    kind: str,
    *,
    aliases: tuple[str, ...] | list[str] = (),
    cik: str | None = None,
    parent_id: str | None = None,
    ticker: str | None = None,
    meta: dict[str, Any] | None = None,
) -> str:
    """Return the entity id for ``name``, creating it if no entity matches by id, alias or CIK."""
    name = " ".join(name.split())
    eid = slugify(name)
    cik = cik.lstrip("0") if cik else None
    row = conn.execute(
        """SELECT id FROM entity
            WHERE id = %(id)s OR %(name)s = ANY(aliases) OR lower(name) = lower(%(name)s)
               OR (%(cik)s::text IS NOT NULL AND cik = %(cik)s)
            ORDER BY (id = %(id)s) DESC LIMIT 1""",
        {"id": eid, "name": name, "cik": cik},
    ).fetchone()
    if row:
        found = row["id"]
        conn.execute(
            """UPDATE entity SET
                 aliases = coalesce((SELECT array_agg(DISTINCT a) FROM unnest(aliases || %(al)s::text[]) a),
                                    '{}'),
                 cik = coalesce(cik, %(cik)s), ticker = coalesce(ticker, %(ticker)s),
                 parent_id = coalesce(parent_id, %(parent)s)
               WHERE id = %(id)s""",
            {"al": [a for a in aliases if a and a != name], "cik": cik, "ticker": ticker,
             "parent": parent_id, "id": found},
        )
        return found
    conn.execute(
        """INSERT INTO entity (id, name, kind, parent_id, aliases, cik, ticker, meta)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (eid, name, kind, parent_id, [a for a in aliases if a and a != name], cik, ticker,
         json.dumps(meta or {})),
    )
    return eid


def producer(loader: str) -> str:
    return f"seed:{loader}"
