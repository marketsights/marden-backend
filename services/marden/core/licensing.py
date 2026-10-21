"""Licence status of every price and fact source, by the exact `source_name` written on rows.

Marden collects some market data without a commercial licence because the business owner decided
to (docs/methods/licensing.md). Those sources are flagged 'unlicensed-commercial' here so the API,
the read models and the models can filter them out with one switch:

    MARDEN_SHOW_UNLICENSED_PRICES=true   (default) unlicensed values are served and used
    MARDEN_SHOW_UNLICENSED_PRICES=false  every row whose source_name is unlicensed is left out

Collectors and loaders for these sources write EXACTLY the names below in `price_obs.source_name`
and `fact.source_name`; `tests/collectors/test_licensing.py` checks that they do.

Statuses: 'public' (public record, commercial reuse allowed, attribution or notice may apply),
'licensed' (Marden holds a licence), 'unlicensed-commercial' (the publisher's terms require a paid
licence or forbid commercial or automated use, and Marden has none).
"""

from __future__ import annotations

from typing import Any, Literal

from marden.core.config import Settings, get_settings

Status = Literal["unlicensed-commercial", "licensed", "public"]
STATUSES: frozenset[str] = frozenset({"unlicensed-commercial", "licensed", "public"})

FINRA_TRACE = "FINRA TRACE (finra.org)"
BOERSE_FRANKFURT = "Börse Frankfurt"
YAHOO_FINANCE = "Yahoo Finance"

SOURCE_LICENCE: dict[str, dict[str, str]] = {
    FINRA_TRACE: {
        "status": "unlicensed-commercial",
        "note": (
            "TRACE prints read from finra.org's public bond pages and the backend they call. "
            "https://www.finra.org/terms-of-use (last modified November 9, 2023): content \"shall be used "
            "ONLY for your own non-commercial personal or professional use\"; \"In no case may you: ... d. "
            "develop or create a database of data using the FINRA Website ...; e. use any process to monitor "
            "or copy the FINRA Website in bulk, or use any data mining, scraping or harvesting tools "
            "(including robots) ...; m. use any portion of the FINRA Website ... in conjunction with any "
            "machine learning ... predictive analytics\". The licensed route is a FINRA Rule 7730 TRACE "
            "market-data subscription (Corporate Bond, Rule 144A, Securitized Products Data Sets)."
        ),
    },
    BOERSE_FRANKFURT: {
        "status": "unlicensed-commercial",
        "note": (
            "Frankfurt Stock Exchange (XFRA) specialist quotes and fixings from live.deutsche-boerse.com and "
            "its api.live.deutsche-boerse.com backend. https://live.deutsche-boerse.com/disclaimer, \"Download "
            "Price History\": \"Any access to and use of the FWB-Data is subject to a licence which is only "
            "granted free of charge if the FWB-Data User does not commercialize the FWB-Data. ... FWB-Data User "
            "creates and offers value-added services using FWB-Data which are then provided to third parties "
            "... a separate fee liable licence with DBAG is required and any usage without a licence in place "
            "is strictly prohibited.\" Licensed route: Deutsche Börse MDDA (Börse Frankfurt Pre-Trade / "
            "Post-Trade, distribution or non-display)."
        ),
    },
    YAHOO_FINANCE: {
        "status": "unlicensed-commercial",
        "note": (
            "End-of-day equity prices from Yahoo Finance's chart endpoint (query1.finance.yahoo.com/v8/"
            "finance/chart). https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html (last updated "
            "4 August 2026): you may not \"access or collect data, or attempt to access or collect data, from "
            "our Services using any automated means, ... including but not limited to robots, spiders, "
            "scrapers, data mining tools, or data gathering or extraction tools, for any purpose without our "
            "express, prior permission\" and \"Unless otherwise expressly stated, you may not access or reuse "
            "the Services, or any portion thereof, for any commercial purpose.\" "
            "https://query1.finance.yahoo.com/robots.txt: \"User-agent: * Disallow: /\". Licensed route: an "
            "exchange or vendor equity feed with redistribution rights (Nasdaq Basic, Cboe, a data vendor)."
        ),
    },
    "SEC EDGAR": {
        "status": "public",
        "note": (
            "https://www.sec.gov/about/privacy-information: \"Information presented on sec.gov is considered "
            "public information and may be copied or further distributed by users of the web site without the "
            "SEC's permission.\" Declared User-Agent, under 10 requests/second."
        ),
    },
    "U.S. Treasury": {
        "status": "public",
        "note": "Daily Treasury par yield curve rates: a U.S. Government work (17 U.S.C. 105), no copyright.",
    },
    "Federal Reserve Bank of New York": {
        "status": "public",
        "note": (
            "https://www.newyorkfed.org/privacy/termsofuse grants a licence \"to use, copy, and distribute "
            "Content for your personal or business purposes\"; reference-rate data must carry the NY Fed notice "
            "(fact series/sofr/notice)."
        ),
    },
}

# Source names built per filing ("N-PORT: <fund> (<period>)", "N-PORT median of 3 funds (<period>)",
# "BDC 10-Q: <bdc> (<period>)"): matched on their prefix.
SOURCE_LICENCE_PREFIXES: dict[str, dict[str, str]] = {
    "N-PORT": {
        "status": "public",
        "note": "Fund marks as filed on SEC Form N-PORT (SEC public information; cite \"as filed by <fund>\").",
    },
    "BDC 10-Q": {
        "status": "public",
        "note": "BDC schedule-of-investments marks as filed with the SEC (SEC public information).",
    },
}

for _name, _entry in (*SOURCE_LICENCE.items(), *SOURCE_LICENCE_PREFIXES.items()):
    if _entry["status"] not in STATUSES or not _entry["note"].strip():
        raise ValueError(f"licence entry for {_name!r} needs a known status and a note")


def licence_for(source_name: str | None) -> dict[str, str] | None:
    """The licence entry for a row's source_name: exact name first, then the per-filing prefixes.
    None for a source this table does not list."""
    if not source_name:
        return None
    entry = SOURCE_LICENCE.get(source_name)
    if entry is not None:
        return entry
    for prefix, prefixed in SOURCE_LICENCE_PREFIXES.items():
        if source_name.startswith(prefix):
            return prefixed
    return None


def is_unlicensed(source_name: str | None) -> bool:
    entry = licence_for(source_name)
    return entry is not None and entry["status"] == "unlicensed-commercial"


def unlicensed_source_names() -> list[str]:
    """Exact source names to exclude in SQL (`source_name <> ALL(%s)`) when the switch is off."""
    return sorted(n for n, e in SOURCE_LICENCE.items() if e["status"] == "unlicensed-commercial")


def show_unlicensed_prices(settings: Settings | None = None) -> bool:
    """MARDEN_SHOW_UNLICENSED_PRICES (default true)."""
    return (settings or get_settings()).show_unlicensed_prices


def visible(source_name: str | None, settings: Settings | None = None) -> bool:
    """Whether a row from this source may be served or used under the current switch."""
    return show_unlicensed_prices(settings) or not is_unlicensed(source_name)


def filter_rows(rows: list[dict[str, Any]], settings: Settings | None = None) -> list[dict[str, Any]]:
    """Rows (price_obs / fact dicts with a 'source_name') allowed under the current switch."""
    if show_unlicensed_prices(settings):
        return list(rows)
    return [r for r in rows if not is_unlicensed(r.get("source_name"))]
