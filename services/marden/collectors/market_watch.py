"""The watched universe for market-data collectors: tickers, CUSIPs and ISINs Marden tracks.

- Tickers: BASE_TICKERS (the issuers, payers and neoclouds named in the price plan) plus every
  `entity.ticker`.
- CUSIPs / ISINs: `instrument.cusip`, `instrument.isin` and the cusip/isin entries of each
  instrument's current `identifiers` fact. A US CUSIP (or CINS) also gives its US ISIN.

Each identifier maps to every instrument that carries it (a few identifiers are shared).
"""

from __future__ import annotations

import re
from collections import defaultdict

from marden.core.db import Conn

BASE_TICKERS = (
    "CRWV", "ORCL", "META", "MSFT", "GOOGL", "AMZN", "NVDA", "IREN", "WULF", "CIFR", "APLD", "CORZ",
    "GLXY", "HUT", "BTDR", "NBIS",
)
_TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_CUSIP = re.compile(r"^[0-9A-Z*@#]{9}$")
_ISIN = re.compile(r"^[A-Z]{2}[0-9A-Z]{9}[0-9]$")


def _char_value(c: str) -> int:
    if c.isdigit():
        return int(c)
    if c.isalpha():
        return ord(c) - 55
    return {"*": 36, "@": 37, "#": 38}[c]


def cusip_valid(cusip: str | None) -> bool:
    if not cusip or not _CUSIP.match(cusip) or not cusip[8].isdigit():
        return False
    total = 0
    for i, c in enumerate(cusip[:8]):
        v = _char_value(c)
        if i % 2 == 1:
            v *= 2
        total += v // 10 + v % 10
    return (10 - total % 10) % 10 == int(cusip[8])


def _luhn_digits(body: str) -> str:
    return "".join(str(int(c, 36)) for c in body)


def isin_check_digit(body: str) -> str:
    """Check digit for the first 11 characters of an ISIN."""
    total = 0
    for i, d in enumerate(reversed(_luhn_digits(body))):
        n = int(d)
        if i % 2 == 0:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return str((10 - total % 10) % 10)


def isin_valid(isin: str | None) -> bool:
    return bool(isin) and bool(_ISIN.match(isin)) and isin_check_digit(isin[:11]) == isin[11]  # type: ignore[index]


def isin_from_cusip(cusip: str, country: str = "US") -> str | None:
    """The ISIN of a CUSIP/CINS number (US prefix), or None for an invalid CUSIP."""
    if not cusip_valid(cusip):
        return None
    body = f"{country}{cusip}"
    return body + isin_check_digit(body)


def yahoo_symbol(ticker: str) -> str:
    """Yahoo writes share classes with a dash (BRK.B -> BRK-B)."""
    return ticker.strip().upper().replace(".", "-")


def watched_tickers(conn: Conn) -> list[str]:
    rows = conn.execute("SELECT DISTINCT upper(trim(ticker)) AS t FROM entity WHERE ticker IS NOT NULL").fetchall()
    found = {r["t"] for r in rows if r["t"] and _TICKER.match(r["t"])}
    return sorted(set(BASE_TICKERS) | found)


def _identifier_rows(conn: Conn) -> list[tuple[str, str, str]]:
    """(instrument id, 'cusip' | 'isin', value) for every identifier Marden holds."""
    out: list[tuple[str, str, str]] = []
    for r in conn.execute("SELECT id, cusip, isin FROM instrument").fetchall():
        if r["cusip"]:
            out.append((r["id"], "cusip", r["cusip"].strip().upper()))
        if r["isin"]:
            out.append((r["id"], "isin", r["isin"].strip().upper()))
    for r in conn.execute(
        """SELECT f.subject_id AS id, e->>'type' AS kind, e->>'value' AS value
           FROM fact_current f, jsonb_array_elements(
                CASE WHEN jsonb_typeof(f.value) = 'array' THEN f.value ELSE '[]'::jsonb END) e
           WHERE f.subject_type = 'instrument' AND f.field = 'identifiers'
             AND e->>'type' IN ('cusip', 'isin') AND e->>'value' IS NOT NULL"""
    ).fetchall():
        out.append((r["id"], r["kind"], r["value"].strip().upper()))
    return out


def watched_cusips(conn: Conn) -> dict[str, list[str]]:
    """CUSIP -> instrument ids (valid CUSIPs only, including the body of US ISINs)."""
    out: dict[str, set[str]] = defaultdict(set)
    for iid, kind, value in _identifier_rows(conn):
        cusip = value if kind == "cusip" else (value[2:11] if value.startswith("US") and isin_valid(value) else None)
        if cusip and cusip_valid(cusip):
            out[cusip].add(iid)
    return {k: sorted(v) for k, v in sorted(out.items())}


def watched_isins(conn: Conn) -> dict[str, list[str]]:
    """ISIN -> instrument ids: stated ISINs plus the US ISIN of every valid CUSIP."""
    out: dict[str, set[str]] = defaultdict(set)
    for iid, kind, value in _identifier_rows(conn):
        isin = value if kind == "isin" else isin_from_cusip(value)
        if isin and isin_valid(isin):
            out[isin].add(iid)
    return {k: sorted(v) for k, v in sorted(out.items())}
