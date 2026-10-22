"""iShares fund option-adjusted spreads -> series facts `etf_oas_<ticker>` (tag R): proxies for the ICE BofA OAS.

Why: the tier curves (docs/methods/pricing-v0.1.md "Tier curves") move with the ICE BofA US High Yield OAS
(series ice_bofa_us_hy_oas, collectors/fred). On 2026-10-21 fred.stlouisfed.org, ALFRED and api.stlouisfed.org
served a "Site Maintenance" page, so no ICE value could be loaded. iShares product pages publish each fund's
option-adjusted spread daily; a fund whose benchmark is an ICE BofA index stands in for that index's OAS. It is
the fund's own portfolio OAS (BlackRock's analytics), not the index value, so it is labelled a proxy wherever a
model uses it (marden.models.price_tiers) and the ICE series wins whenever it has a value.

Funds (product URLs from https://www.ishares.com/us/product-sitemap.xml; each page's benchmark checked
2026-10-21 and again on every run):
- USHY  iShares Broad USD High Yield Corporate Bond ETF, "ICE BofA US High Yield Constrained Index" (HUC0)
        -> proxy for ice_bofa_us_hy_oas (FRED BAMLH0A0HYM2, the unconstrained ICE BofA US High Yield Index H0A0)
- HYBB  iShares BB Rated Corporate Bond ETF, "ICE BofA BB US High Yield Constrained Index" (HUC1)
        -> proxy for ice_bofa_us_bb_oas (BAMLH0A1HYBB, H0A1)
- USIG  iShares Broad USD Investment Grade Corporate Bond ETF, "ICE BofA US Corporate Index" (C0A0)
        -> proxy for ice_bofa_us_ig_oas (BAMLC0A0CM, C0A0)
Not used: LQDB (iShares BBB Rated Corporate Bond ETF) tracks the "iBoxx USD Liquid Investment Grade BBB 0+
Index", not ICE BofA BBB; BondBloxx XCCC tracks the "Bloomberg US High Yield 300MM CCC 2% Issuer Capped Index"
and bondbloxxetf.com's terms forbid "any robot, data mining, spider, scraper, crawler". So BBB, single-B and CCC
have no proxy and stay missing.

What is read: the component data the page embeds for its own rendering, `componentprops="{json}"` on the
`<walrus-render-on-client>` elements (HTML-escaped JSON): fundHeader fundTicker.ticker; keyFundFacts
indexSeriesName and indexTicker; fundamentalsAndRisk containers' dataPointsByNameMap optionAdjustedSpread
(raw `value` in bps, `asOfDate` yyyymmdd), modelOad ("Effective Duration", years), and the data points labelled
"Yield to Worst" and "Average Yield to Maturity" when present (percent). The page is stored first (raw-first,
`fetch_document`) and parsed from the stored bytes. A page whose ticker or benchmark is not the expected one
writes nothing for that fund (BenchmarkMismatch); the other funds are still written and the run is recorded
failed with the reason.

Facts on subject_type 'series', subject_id 'etf_oas_<ticker>' (tag R, source_name
"iShares <TICKER> fund OAS (BlackRock)", licence-flagged unlicensed-commercial in marden.core.licensing):
- on the page's as-of date: 'value' = OAS in percent (bps / 100), 'effective_duration_years',
  'yield_to_worst_pct', 'avg_yield_to_maturity_pct' (when the page has them), 'benchmark_index' and
  'index_ticker' (the page's own words);
- on TERMS_CHECKED: 'proxy_for' (the ICE series id), 'proxy_method' and 'display_allowed' = false, like the ICE
  series themselves: model input only, never displayed.
A rerun on the same page data writes nothing new (fact dedupe on value and as-of date).

Access (docs/methods/licensing.md section 4): declared User-Agent "Marden research cody@marden.ai" and an
ordinary Accept header, www.ishares.com capped at 1 request/second plus 0.2-0.8 s jitter, one request per fund
per weekday after the close. HTTP 401/403/429 (after one retry), a Cloudflare `cf-mitigated` header, a login or
consent redirect, or a CAPTCHA / bot-check page raises `Challenged`: the run stops and is recorded failed, with
no retry under other headers. Cadence: weekdays 18:30 ET (`30 22,23 * * 1-5` UTC, due_et keeps the right one);
a run before 18:30 ET or after a full ok run that evening is skipped unless forced.
`python -m marden collect etf_oas [--fund USHY] [--force]`.
"""

from __future__ import annotations

import argparse
import html
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from marden import cli
from marden.collectors import market_http
from marden.collectors.market_http import FULL_RUN, Challenged, MarketFetcher, closed_window
from marden.collectors.rates_common import (
    ET,
    ParseError,
    RunStats,
    SeriesWriter,
    check_pct,
    collect,
    due_et,
    print_stats,
)
from marden.core import db, http, licensing
from marden.core.db import Conn
from marden.core.documents import DocumentRow
from marden.core.http import FetchResponse, Validators, fetch_document
from marden.core.queue import app
from marden.core.sources import declare_source

SOURCE_ID = "etf_oas"
PRODUCER = f"collector:{SOURCE_ID}"
HOST = "www.ishares.com"
SITEMAP_URL = f"https://{HOST}/us/product-sitemap.xml"
ROBOTS_URL = f"https://{HOST}/robots.txt"
SERIES_PREFIX = "etf_oas_"
DOC_TYPE = "ishares_product_html"
RUN_AT_ET = (18, 30)
WINDOW_ET = (18, 30)  # after the US close, like the other market collectors
CRON_UTC = "30 22,23 * * 1-5"  # 18:30 ET is 22:30 UTC (EDT) or 23:30 UTC (EST); due_et keeps the right one
TERMS_CHECKED = date(2026, 10, 21)
BPS_PER_PCT = Decimal(100)

TERMS_NOTE = (
    "UNLICENSED for commercial use; collected at the business owner's request of 2026-10-21 to find the ICE BofA "
    "spreads elsewhere while FRED is down, handled like the other unlicensed market data (docs/methods/licensing.md; "
    "licence flag in marden.core.licensing). Checked 2026-10-21: the iShares footer's Terms & Conditions "
    f"({licensing.BLACKROCK_TERMS_URL}) say the content \"is solely for your personal, non-commercial use\", "
    "\"You may not distribute, modify, transmit, reuse, repost, or use the content of this Website for public or "
    "commercial purposes, including all text, images, audio, and video, without BlackRock's written permission\", "
    "and, under Prohibited Uses, \"Except as otherwise stated in these Terms or as expressly authorized by BlackRock "
    "in writing, you may not: ... Use any robot, spider, intelligent agent, other automatic device, or manual "
    "process to search, monitor or copy this Website or the reports, data, information, content, software, "
    "products services, or other materials on, generated by or obtained from this Website, whether through links "
    "or otherwise (collectively, \"Materials\"), without BlackRock's permission, provided that generally available "
    f"third-party web browsers may be used without such permission\". {ROBOTS_URL}: \"User-agent: *\" disallows "
    "only \"/*?truepdf*\", \"/*?norepdf*\", \"/*.dl$\", the sign-on paths, \"/search/\" and \"/us/MYCATEGORYURL\" "
    "(product pages are not disallowed; \"User-agent: Brightbot 1.0 / Disallow: /\" names another crawler). No "
    "BlackRock permission has been obtained. Use: model input only, never displayed (display_allowed = false). "
    "Access: declared User-Agent \"Marden research cody@marden.ai\", 1 request/second plus jitter, one product page "
    "per fund per weekday after 18:30 ET; any challenge, login wall, 401/403 or repeated 429 is recorded here and "
    "the run stops (no evasion). Licensed route: BlackRock's written permission, or the ICE BofA indices under an "
    "ICE Data Indices licence."
)
SOURCE = declare_source(SOURCE_ID, "iShares fund OAS (BlackRock)", "portal", f"https://{HOST}/us/products/",
                        24 * 60, TERMS_NOTE)
http.set_rate_limit(HOST, 1.0)


@dataclass(frozen=True)
class Fund:
    ticker: str
    product_id: str  # from the product sitemap, never guessed
    slug: str
    benchmark: str  # the page's indexSeriesName, exactly
    proxy_for: str  # the ICE BofA series id (collectors/fred) this fund's OAS stands in for
    ice_index: str  # what that FRED series is

    @property
    def url(self) -> str:
        return f"https://{HOST}/us/products/{self.product_id}/{self.slug}"

    @property
    def series_id(self) -> str:
        return f"{SERIES_PREFIX}{self.ticker.lower()}"

    @property
    def source_name(self) -> str:
        return licensing.ishares_oas_name(self.ticker)

    @property
    def method(self) -> str:
        return (f"fund OAS of {self.ticker}, which tracks the {self.benchmark}; proxy for the {self.ice_index} OAS "
                f"(series {self.proxy_for})")


FUNDS: tuple[Fund, ...] = (
    Fund("USHY", "291299", "ishares-broad-usd-high-yield-corporate-bond-etf",
         "ICE BofA US High Yield Constrained Index", "ice_bofa_us_hy_oas",
         "ICE BofA US High Yield Index (FRED BAMLH0A0HYM2)"),
    Fund("HYBB", "305259", "ishares-bb-rated-corporate-bond-etf",
         "ICE BofA BB US High Yield Constrained Index", "ice_bofa_us_bb_oas",
         "ICE BofA BB US High Yield Index (FRED BAMLH0A1HYBB)"),
    Fund("USIG", "239460", "ishares-broad-usd-investment-grade-corporate-bond-etf",
         "ICE BofA US Corporate Index", "ice_bofa_us_ig_oas", "ICE BofA US Corporate Index (FRED BAMLC0A0CM)"),
)
PROXY_OF: dict[str, Fund] = {f.proxy_for: f for f in FUNDS}  # ICE series id -> fund


class BenchmarkMismatch(ParseError):
    """The page is not for the expected fund, or the fund no longer tracks the expected ICE BofA index."""


# ---------------------------------------------------------------- fetch
class PageFetcher(MarketFetcher):
    """MarketFetcher for HTML product pages: also stops at a CAPTCHA or bot-check page served with 200."""

    def __init__(self, **kwargs: Any):
        super().__init__(expect_json=False, **kwargs)

    def get(self, url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
            validators: Validators | None = None, use_cache: bool = True) -> FetchResponse:
        sent = {"Accept": "text/html,application/xhtml+xml"}
        sent.update(headers or {})
        resp = super().get(url, params=params, headers=sent, validators=validators, use_cache=use_cache)
        if b"componentprops=" not in resp.content and market_http.CHALLENGE.search(resp.content[:200000]):
            raise Challenged(resp.url, resp.status, "stopped: bot-check or CAPTCHA page (no evasion attempted)")
        return resp


# ---------------------------------------------------------------- parse
@dataclass(frozen=True)
class FundPage:
    ticker: str
    fund_name: str | None
    benchmark: str
    index_ticker: str | None
    as_of: date
    oas_bps: Decimal
    effective_duration_years: Decimal | None
    yield_to_worst_pct: Decimal | None
    avg_yield_to_maturity_pct: Decimal | None

    @property
    def oas_pct(self) -> Decimal:
        return self.oas_bps / BPS_PER_PCT


_PROPS = re.compile(r'\bcomponentprops="([^"]*)"')


def components(data: bytes) -> dict[str, dict[str, Any]]:
    """The page's embedded component data by componentId (the first block of each id)."""
    text = data.decode("utf-8", errors="replace")
    out: dict[str, dict[str, Any]] = {}
    for m in _PROPS.finditer(text):
        try:
            block = json.loads(html.unescape(m.group(1)), parse_float=Decimal)
        except ValueError:
            continue  # a block that is not JSON is not one the collector reads
        cid = block.get("componentId") if isinstance(block, dict) else None
        if isinstance(cid, str) and cid not in out:
            out[cid] = block
    return out


def _need(block: dict[str, Any] | None, *path: str) -> Any:
    node: Any = block
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise ParseError(f"page data has no {'.'.join(path)}")
        node = node[key]
    return node


def _as_of(raw: Any, what: str) -> date:
    text = str(raw)
    if not re.fullmatch(r"\d{8}", text):
        raise ParseError(f"{what}: as-of date {raw!r} is not yyyymmdd")
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        raise ParseError(f"{what}: as-of date {raw!r} is not a date") from None


def _number(raw: Any, what: str) -> Decimal:
    if isinstance(raw, bool) or not isinstance(raw, Decimal | int):
        raise ParseError(f"{what} is not a number: {raw!r}")
    value = Decimal(raw)
    if not value.is_finite():
        raise ParseError(f"{what} is not finite: {raw!r}")
    return value


def _same_day(points: dict[str, dict[str, Any]], name: str | None, label: str | None, day: date,
              unit: str) -> Decimal | None:
    """An optional statistic (by data point name, else by label) dated the OAS as-of day; None when absent,
    blank or of another day (never carried across dates)."""
    dp = points.get(name) if name else None
    if dp is None and label:
        dp = next((p for p in points.values() if isinstance(p, dict) and p.get("label") == label), None)
    if not isinstance(dp, dict) or dp.get("value") is None or dp.get("asOfDate") is None:
        return None
    if not str(dp.get("formattedValue") or "").strip().endswith(unit):
        raise ParseError(f"{dp.get('label') or name}: {dp.get('formattedValue')!r} is not in {unit!r}")
    if _as_of(dp["asOfDate"], str(dp.get("label") or name)) != day:
        return None
    return _number(dp["value"], str(dp.get("label") or name))


def parse_page(data: bytes, fund: Fund) -> FundPage:
    """The fund's OAS and statistics from one product page. Raises BenchmarkMismatch when the page is for
    another fund or names another benchmark; ParseError when the data is not in the shape described above."""
    comps = components(data)
    if not comps:
        raise ParseError(f"{fund.ticker}: no embedded component data on the page")
    header = comps.get("fundHeader")
    ticker = _need(header, "containersByNameMap", "fundTicker", "dataPointsByNameMap", "ticker", "value")
    if str(ticker).strip().upper() != fund.ticker:
        raise BenchmarkMismatch(f"{fund.ticker}: the page is for {ticker!r}")
    facts = _need(comps.get("keyFundFacts"), "dataPoints")
    benchmark = str(_need(facts, "indexSeriesName", "formattedValue") or "").strip()
    if benchmark != fund.benchmark:
        raise BenchmarkMismatch(f"{fund.ticker}: benchmark is {benchmark!r}, expected {fund.benchmark!r}; "
                                "nothing written for this fund")
    index_ticker = (facts.get("indexTicker") or {}).get("formattedValue")
    containers = _need(comps.get("fundamentalsAndRisk"), "containersByNameMap")
    points: dict[str, dict[str, Any]] = {}
    for c in containers.values() if isinstance(containers, dict) else []:
        points.update((c or {}).get("dataPointsByNameMap") or {})
    oas = points.get("optionAdjustedSpread")
    if not isinstance(oas, dict) or oas.get("value") is None:
        raise ParseError(f"{fund.ticker}: no optionAdjustedSpread value on the page")
    if oas.get("label") != "Option Adjusted Spread" or not str(oas.get("formattedValue") or "").endswith("bps"):
        raise ParseError(f"{fund.ticker}: optionAdjustedSpread is {oas.get('label')!r} "
                         f"{oas.get('formattedValue')!r}, not 'Option Adjusted Spread' in bps")
    day = _as_of(oas.get("asOfDate"), f"{fund.ticker} OAS")
    name = _need(header, "containersByNameMap", "fundName", "dataPointsByNameMap", "fundName", "value") \
        if "fundName" in (header or {}).get("containersByNameMap", {}) else None
    return FundPage(
        ticker=fund.ticker, fund_name=str(name) if name else None, benchmark=benchmark,
        index_ticker=str(index_ticker).strip() if index_ticker else None, as_of=day,
        oas_bps=_number(oas["value"], f"{fund.ticker} OAS"),
        effective_duration_years=_same_day(points, "modelOad", "Effective Duration", day, "yrs"),
        yield_to_worst_pct=_same_day(points, None, "Yield to Worst", day, "%"),
        avg_yield_to_maturity_pct=_same_day(points, None, "Average Yield to Maturity", day, "%"),
    )


# ---------------------------------------------------------------- write
def write_page(conn: Conn, fund: Fund, page: FundPage, doc: DocumentRow, stats: RunStats) -> None:
    """Published facts for one fund page (idempotent: a rerun on the same data writes nothing new)."""
    w = SeriesWriter(conn, stats, tag="R", source_name=fund.source_name, producer=PRODUCER)
    sid = fund.series_id
    w.observation(sid, page.as_of, check_pct(sid, page.as_of, page.oas_pct), source_url=fund.url,
                  document_id=doc.id)
    for field_name, value, unit in (("effective_duration_years", page.effective_duration_years, "years"),
                                    ("yield_to_worst_pct", page.yield_to_worst_pct, "pct"),
                                    ("avg_yield_to_maturity_pct", page.avg_yield_to_maturity_pct, "pct")):
        if value is not None:
            w.attribute(sid, field_name, value, valid_from=page.as_of, source_url=fund.url, document_id=doc.id,
                        unit=unit)
    w.attribute(sid, "benchmark_index", page.benchmark, valid_from=page.as_of, source_url=fund.url,
                document_id=doc.id, quote=page.benchmark, quote_loc="keyFundFacts.indexSeriesName")
    if page.index_ticker:
        w.attribute(sid, "index_ticker", page.index_ticker, valid_from=page.as_of, source_url=fund.url,
                    document_id=doc.id, quote_loc="keyFundFacts.indexTicker")
    w.attribute(sid, "proxy_for", fund.proxy_for, valid_from=TERMS_CHECKED, source_url=fund.url)
    w.attribute(sid, "proxy_method", fund.method, valid_from=TERMS_CHECKED, source_url=fund.url)
    w.attribute(sid, "display_allowed", False, valid_from=TERMS_CHECKED, source_url=licensing.BLACKROCK_TERMS_URL)


# ---------------------------------------------------------------- run
def run(
    *, funds: tuple[Fund, ...] = FUNDS, fetcher: MarketFetcher | None = None, force: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One collection run over `funds`. Skipped unless forced before 18:30 ET or after a full ok run this
    evening (once a day after the close)."""
    now = now or datetime.now(UTC)
    if not force and (why := closed_window(SOURCE_ID, now, ET, WINDOW_ET, "ET")):
        return {"ok": True, "skipped": why}
    own = fetcher is None
    market = fetcher or PageFetcher()

    def body(stats: RunStats) -> None:
        stats.extra.update(scope=FULL_RUN if set(funds) == set(FUNDS) else "subset", funds={})
        refused: list[str] = []
        try:
            for fund in funds:
                doc = fetch_document(SOURCE_ID, fund.url, doc_type=DOC_TYPE,
                                     title=f"iShares {fund.ticker} product page", external_id=fund.ticker,
                                     meta={"ticker": fund.ticker}, fetcher=market)
                stats.document(doc)
                try:
                    page = parse_page(doc.read(), fund)
                except BenchmarkMismatch as exc:
                    refused.append(str(exc))
                    stats.extra.setdefault("refused", {})[fund.ticker] = str(exc)
                    continue
                with db.transaction() as conn:
                    write_page(conn, fund, page, doc, stats)
                stats.extra["funds"][fund.ticker] = {
                    "series": fund.series_id, "benchmark": page.benchmark, "index_ticker": page.index_ticker,
                    "as_of": page.as_of.isoformat(), "oas_bps": float(page.oas_bps),
                    "effective_duration_years": _f(page.effective_duration_years),
                    "yield_to_worst_pct": _f(page.yield_to_worst_pct),
                    "avg_yield_to_maturity_pct": _f(page.avg_yield_to_maturity_pct),
                    "proxy_for": fund.proxy_for, "document_id": doc.id}
        finally:
            stats.extra["requests"] = market.requests
        if refused:
            raise BenchmarkMismatch("; ".join(refused))

    try:
        return collect(SOURCE, body)
    finally:
        if own:
            market.close()


def _f(v: Decimal | None) -> float | None:
    return None if v is None else float(v)


# ---------------------------------------------------------------- schedule and CLI
@app.periodic(cron=CRON_UTC, periodic_id="weekdays_1830_et")
@app.task(name="collect.etf_oas", queue="collect", queueing_lock="collect.etf_oas", lock="collect.etf_oas")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    if not due_et(timestamp, *RUN_AT_ET):
        return {"skipped": "not 18:30 ET"}
    return run()


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--fund", action="append", dest="funds", choices=[f.ticker for f in FUNDS],
                   help="only this fund (repeatable)")
    p.add_argument("--force", action="store_true", help="run outside the daily window (before 18:30 ET or again)")


@cli.action("collect", SOURCE_ID, help="iShares fund OAS (USHY, HYBB, USIG) -> series etf_oas_<ticker> "
            "(proxies for the ICE BofA OAS; model input only; unlicensed-commercial)", configure=_args)
def collect_cli(args: argparse.Namespace) -> int:
    chosen = tuple(f for f in FUNDS if f.ticker in set(args.funds)) if args.funds else FUNDS
    return print_stats(run(funds=chosen, force=args.force))
