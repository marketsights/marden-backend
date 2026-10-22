"""Site debt ($B) summed over several instruments keeps its source link when every part cites the same
document (IREN Childress: two loans, one 10-Q). Parts citing different documents show the joined source
names and no single link, since no one document holds the total."""

from __future__ import annotations

from datetime import date

from marden.readmodels.views.sites_common import debt_value

TENQ = "https://www.sec.gov/Archives/edgar/data/1878848/000187884826000052/iren-20260630.htm"


def _inst(usd: float, url: str | None, tag: str = "F", source: str = "SEC EDGAR") -> dict:
    return {"facts": {"amount_usd": {"value": usd, "tag": tag, "source_name": source, "source_url": url,
                                     "valid_from": date(2026, 6, 30), "method_version": None}}}


def test_a_sum_of_parts_from_one_filing_links_to_it():
    out = debt_value([_inst(525_000_000, TENQ), _inst(413_000_000, TENQ)])
    assert out["value"] == 0.938 and out["tag"] == "F" and out["sourceUrl"] == TENQ


def test_a_sum_of_parts_from_different_documents_has_no_single_link():
    out = debt_value([_inst(525_000_000, TENQ), _inst(413_000_000, "https://news.example.test/x", "R", "Bisnow")])
    assert out["value"] == 0.938 and out["tag"] == "R" and "sourceUrl" not in out
    assert out["sourceName"] == "Bisnow + SEC EDGAR"
    assert "sourceUrl" not in debt_value([_inst(1e9, None), _inst(2e9, None)])
