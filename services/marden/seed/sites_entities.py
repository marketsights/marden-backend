"""Party registry for the `sites` loader: one `entity` row per real party, merged across files.

Names in the archive are free text ("Oracle", "Oracle Corporation", "ORACLE CORP"), so a party is
matched on a normalized name (case, punctuation, parentheticals and legal suffixes dropped), its
CIK, or an alias before a new entity is created with ``seed.common.ensure_entity``. A ticker is
kept only when the SEC ticker file's company title normalizes to the same name, so a parent's
ticker on a subsidiary row (gx: Entergy Louisiana -> ETR) is not attached. Placeholder names
("Not disclosed", "Noteholders", "About 20 banks") never become entities.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from marden.core.db import Conn
from marden.seed.common import ensure_entity

_SUFFIXES = frozenset({"inc", "incorporated", "corp", "corporation", "co", "company", "llc", "lp", "llp", "ltd",
                       "limited", "plc", "nv", "bv", "sa", "ag", "pty", "pte", "group"})
_DOTTED = ((r"\bl\.\s?l\.\s?c\b\.?", " llc "), (r"\bl\.\s?p\b\.?", " lp "), (r"\bn\.\s?v\b\.?", " nv "),
           (r"\bb\.\s?v\b\.?", " bv "), (r"\bs\.\s?a\b\.?", " sa "))
_PLACEHOLDER = re.compile(
    r"not disclosed|\bn/d\b|^n/?a$|unnamed|not named|names? not|name not|undisclosed|^various\b|\bsubs\b|"
    r"subsidiaries|noteholders|bondholders|\babout \d+ banks|customers\b|tenants\b|not public|ground landlord|"
    r"^parent\b|^none\b|^tenant\b|special-purpose companies|\bsub(sidiary)?$|consolidated\b|\bVIEs\b",
    re.I,
)
_QUALIFIER = re.compile(r"\s*\((presumed|former|reported|agent|inferred)\)\s*$", re.I)
_EXCHANGE = re.compile(r"^(nasdaq|nyse|tsx|asx|nasdaq/tsx)\s*:\s*", re.I)

TENANT_KIND = {"AI lab": "ai_lab", "Hyperscaler": "cloud", "Neocloud": "neocloud", "Chipmaker": "chipmaker",
               "Other": "company", "Undisclosed": "other"}
SECTOR_KIND = {"Project debt": "spv", "Hyperscaler JV": "spv", "GPU lease": "spv", "HPC developer": "developer",
               "DC developer": "developer", "Neocloud": "neocloud", "Private neocloud": "neocloud",
               "Hyperscaler": "cloud"}
ROLE_KIND = {"Site owner": "company", "Project company": "spv", "Borrower": "spv", "Power plant": "power_provider",
             "Fuel supply": "power_provider", "Utility": "utility", "Grid operator": "agency",
             "Sponsor": "developer", "Developer": "developer", "Tenant": "company", "End user": "company",
             "Customer": "company", "Guarantor": "guarantor", "Backstop": "guarantor", "Lender": "lender",
             "Arranger": "bank", "Equity investor": "company", "Contractor": "company", "Operator": "company"}
# Listed parents of the aichain tenants that the sheet carries without a ticker. Validated against
# the SEC ticker file at load time (its title becomes an alias: "Meta Platforms, Inc.").
TENANT_TICKERS = {"meta": "META", "microsoft": "MSFT", "amazon": "AMZN", "google": "GOOGL", "nvidia": "NVDA",
                  "amd": "AMD"}


def norm(name: str) -> str:
    """Matching key: ASCII, lower case, no parentheticals, punctuation or trailing legal suffixes."""
    s = re.sub("[\u2010-\u2015]", " ", name)  # en/em dashes: 'Red Chiles A\u2013D' -> 'red chiles a d'
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\([^()]*\)", " ", s)
    s = re.sub(r"\s*/[a-z]{2}/?\s*$", " ", s)  # SEC titles: "ENTERGY CORP /DE/"
    for pattern, repl in _DOTTED:
        s = re.sub(pattern, repl, s)
    s = re.sub(r"[^a-z0-9]+", " ", s.replace("&", " and "))
    toks = s.split()
    while toks and toks[-1] in _SUFFIXES:
        toks.pop()
    while toks and toks[0] == "the":
        toks.pop(0)
    return " ".join(toks)


def display_name(name: str) -> str:
    """A party name without descriptive parentheticals: 'Green Chile Ventures LLC (Oracle subsidiary)'."""
    s = re.sub(r"\s*\([^()]*\)", "", name)
    s = re.split(r"\s+or (?:one of its|an?)\b|\s+-\s+|\s*\(|;", s)[0]
    s = " ".join(s.split()).strip(" ,;-")
    return s or " ".join(name.split())


def is_placeholder(name: str) -> bool:
    return bool(_PLACEHOLDER.search(name))


_LEGAL_END = re.compile(r"\b(inc|llc|lp|ltd|corp|corporation|plc|fund|trust|n\.v)\.?$", re.I)


def split_parties(name: str, role: str) -> list[str]:
    """One chain row can name several parties ('Wells Fargo, BNP Paribas, Goldman Sachs')."""
    text = _QUALIFIER.sub("", name.strip())
    text = re.sub(r"^(noteholders|lenders) including\s+", "", text, flags=re.I)
    if role in ("Lender", "Arranger", "Equity investor", "Contractor"):
        text = re.sub(r"\s*\([^()]*\)", "", text)
        parts = re.split(r",(?!\s*(?:inc|llc|ltd|lp|l\.p|corp|co)\b)\s*(?:with\s+)?|\s+with\s+|\s+and\s+", text,
                         flags=re.I)
    else:
        parts = [text]
        halves = re.split(r"\s+and\s+", text)
        if len(halves) == 2 and all(len(h.split()) >= 3 and _LEGAL_END.search(h.strip(" ,")) for h in halves):
            parts = halves
    return [p.strip(" ,;") for p in parts if p.strip(" ,;")]


def clean_ticker(ticker: Any) -> str | None:
    if not isinstance(ticker, str):
        return None
    t = _EXCHANGE.sub("", ticker.strip()).split()[0].strip(";,").upper() if ticker.strip() else ""
    return t if t and t not in ("PRIVATE", "N/D") and re.fullmatch(r"[A-Z.\-]{1,6}", t) else None


@dataclass(frozen=True)
class Listing:
    ticker: str
    cik: str
    title: str


class Registry:
    """Resolves party names to entity ids inside the loader's transaction."""

    def __init__(self, conn: Conn, tickers: dict[str, Listing]):
        self.conn = conn
        self.tickers = tickers
        self.by_norm: dict[str, str] = {}
        self.by_cik: dict[str, str] = {}
        self.cik_of: dict[str, str] = {}  # entity id -> its CIK
        self.names: dict[str, str] = {}
        self.grp: dict[str, str] = {}     # aidebt group code -> entity id
        self.tenant: dict[str, str] = {}  # aichain tenant id -> entity id
        self.skipped: set[str] = set()
        # Oldest first: when two stored entities normalize to the same name, the earlier one keeps
        # the match on every rerun (so edges and links keep their ids).
        for row in conn.execute("SELECT id, name, aliases, cik FROM entity ORDER BY created_at, id").fetchall():
            self._index(row["id"], row["name"], *row["aliases"], cik=row["cik"])
            self.names[row["id"]] = row["name"]

    def _index(self, eid: str, *names: str, cik: str | None = None) -> None:
        for n in names:
            key = norm(n) if n else ""
            if key:
                self.by_norm.setdefault(key, eid)
        if cik:
            self.by_cik.setdefault(cik, eid)
            self.cik_of.setdefault(eid, cik)

    def _same_registrant(self, eid: str | None, cik: str | None) -> str | None:
        """A name match is dropped when both sides carry different CIKs: two registrants whose names only
        differ by a legal suffix ('Blue Owl Capital Corporation', OBDC, vs 'Blue Owl Capital', OWL)."""
        if eid is None or cik is None or self.cik_of.get(eid) in (None, cik):
            return eid
        return None

    def listing(self, name: str, ticker: Any, *, trusted: bool = False) -> Listing | None:
        """The SEC listing for a ticker, if its title is this party (or the ticker is curated)."""
        t = clean_ticker(ticker)
        hit = self.tickers.get(t) if t else None
        if hit and (trusted or norm(hit.title) in (norm(name), norm(display_name(name)))):
            return hit
        return None

    def lookup(self, name: str | None) -> str | None:
        """An existing entity for a name (no insert)."""
        if not name or not name.strip():
            return None
        return self.by_norm.get(norm(display_name(name))) or self.by_norm.get(norm(name))

    def resolve(
        self,
        name: Any,
        kind: str,
        *,
        ticker: Any = None,
        cik: Any = None,
        aliases: Iterable[str] = (),
        exact: bool = False,
        trusted_ticker: bool = False,
    ) -> str | None:
        """The entity id for a party name, creating the entity when nothing matches.

        ``exact`` keeps the name as given (curated display names such as 'QTS (Blackstone)') and
        skips the placeholder check; otherwise descriptive parentheticals are dropped."""
        if not isinstance(name, str) or not name.strip():
            return None
        raw = " ".join(_QUALIFIER.sub("", name).split())
        if not exact and is_placeholder(raw):
            known = None if is_placeholder(display_name(raw)) else self.lookup(raw)
            if known is None:
                self.skipped.add(raw)
            else:
                self.skipped.discard(raw)
            return known
        disp = raw if exact else display_name(raw)
        key = norm(disp) or norm(raw)
        if not key:
            self.skipped.add(raw)
            return None
        hit = self.listing(disp, ticker, trusted=trusted_ticker)
        cik_text = str(cik).lstrip("0") if cik else (hit.cik if hit else None)
        alias_set = {raw, *aliases, *( [hit.title] if hit else [])}
        alias_list = sorted(a for a in alias_set if a and a != disp)
        eid = self.by_cik.get(cik_text) if cik_text else None
        for candidate in (key, *(norm(a) for a in alias_list)):
            if eid is None:
                eid = self._same_registrant(self.by_norm.get(candidate), cik_text)
        call_name = self.names.get(eid, disp) if eid else disp
        found = ensure_entity(
            self.conn, call_name, kind, aliases=alias_list, cik=cik_text, ticker=hit.ticker if hit else None,
        )
        self.names.setdefault(found, call_name)
        self._index(found, call_name, disp, raw, *alias_list, cik=cik_text)
        return found
