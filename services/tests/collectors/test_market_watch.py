"""market_watch: identifier checks and the watched tickers / ISINs read from the database."""

from __future__ import annotations

import json

from marden.collectors import market_watch as W


def test_isin_from_cusip_matches_published_isins():
    # ISINs as Börse Frankfurt and the archive show them (docs/methods/price-sources.md research table).
    assert W.isin_from_cusip("21873SAL2") == "US21873SAL25"
    assert W.isin_from_cusip("U2069EAC4") == "USU2069EAC40"  # Reg S CINS number
    assert W.isin_from_cusip("68389XBX2") == "US68389XBX21"
    assert W.isin_from_cusip("21873SAL3") is None  # bad check digit
    assert W.isin_valid("XS2941363637") and not W.isin_valid("US21873SAL26")
    assert W.yahoo_symbol("brk.b") == "BRK-B"


def test_watched_universe_comes_from_instruments_identifiers_and_entities(conn):
    conn.execute("INSERT INTO entity (id, name, kind, ticker) VALUES ('blue-owl', 'Blue Owl', 'company', 'OWL'),"
                 " ('nobody', 'Nobody', 'company', NULL), ('bad', 'Bad', 'company', 'not a ticker')")
    conn.execute("""INSERT INTO instrument (id, name, kind, issuer_name, cusip, isin) VALUES
                    ('A', 'a', 'bond', 'x', '21873SAL2', NULL), ('B', 'b', 'bond', 'x', NULL, 'XS2941363637'),
                    ('C', 'c', 'bond', 'x', NULL, NULL)""")
    ident = [{"type": "cusip", "value": "21873SAL2"}, {"type": "isin", "value": "USU2069EAC40"},
             {"type": "loanx", "value": "LX123456"}]
    conn.execute(
        """INSERT INTO fact (subject_type, subject_id, field, value, tag, source_name, valid_from, status, producer,
                             dedupe_key) VALUES ('instrument', 'C', 'identifiers', %s, 'F', 'SEC EDGAR', '2026-10-20',
                             'published', 'seed:test', 'k1')""",
        (json.dumps(ident),),
    )
    tickers = W.watched_tickers(conn)
    assert set(W.BASE_TICKERS) | {"OWL"} == set(tickers) and "BTDR" in tickers
    assert W.watched_isins(conn) == {"US21873SAL25": ["A", "C"], "USU2069EAC40": ["C"], "XS2941363637": ["B"]}
    assert W.watched_cusips(conn) == {"21873SAL2": ["A", "C"], "U2069EAC4": ["C"]}
