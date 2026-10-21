"""Parsing for `finra_trace`: the finra.org bond page backend's JSON, and refusal detection.

The finra.org bond page (https://www.finra.org/finra-data/fixed-income/bond?cusip=...) is a shell that
loads FINRA's dynamic-reporting app; the app reads two datasets from services-dynarep.ddwa.finra.org:

- the bond's security master and last sale (one record: cusip, issuerName, couponRate, maturityDate,
  is144A, lastTradeDate, lastTradeTime, lastSalePrice, lastSaleYield, ...), and
- its end-of-day price/yield history (records: cusip, productType, tradeDate, lastSalePrice, lastSaleYield).

Both answer `{"returnBody": {"statusCode": 200, "headers": {...}, "data": "<JSON list as a string>"}}`
(the legacy archive keeps the same envelope with a leading "status": "success"). The bond page shows no
high/low, volume or trade count, so none is parsed here. Times are US Eastern (TRACE runs on ET).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

from dateutil import tz

from marden.core.licensing import FINRA_TRACE

ET = tz.gettz("America/New_York")
SOURCE_NAME = FINRA_TRACE  # exactly this on every price_obs and fact (marden.core.licensing)
BOND_PAGE = "https://www.finra.org/finra-data/fixed-income/bond"
# Prices are percent of par; converts and equity-linked notes trade far above par (Bloom Energy 1,388).
PRICE_RANGE = (Decimal(0), Decimal(5000))
YIELD_RANGE = (Decimal(-10000), Decimal(10000))  # converts far above par print yields of -150% and below
YIELD_SENTINEL = Decimal(99999)  # FINRA prints 999999.99 when a yield is not meaningful: stored as no yield
CHALLENGE_MARKERS = (b"Just a moment...", b"cf-chl", b"challenge-platform", b"captcha", b"Attention Required!")


class ParseError(ValueError):
    """A response is not in the shape the parser knows; nothing from it is written."""


def bond_page_url(cusip: str) -> str:
    """The finra.org bond page for a CUSIP (the source_url of every TRACE row Marden writes for it)."""
    return f"{BOND_PAGE}?cusip={cusip}&bondType=CORP"


# ---------------------------------------------------------------- refusals
def refusal_reason(status: int, headers: dict[str, str], body: bytes, *, expect_json: bool,
                   final_url: str = "") -> str | None:
    """Why a response means the site is not serving Marden (None when it is an ordinary answer).

    Challenge pages, 401/403/407/429, login walls and an HTML 'Not Found' page in place of the JSON the
    bond page itself requests are all refusals: the run stops and records them, it never works around them.
    """
    h = {k.lower(): v for k, v in headers.items()}
    if h.get("cf-mitigated"):
        return f"HTTP {status} with cf-mitigated: {h['cf-mitigated']} (Cloudflare challenge)"
    head = body[:4096]
    if any(m.lower() in head.lower() for m in CHALLENGE_MARKERS) and (expect_json or status >= 400):
        return f"HTTP {status}: challenge page"
    if status in (401, 403, 407):
        return f"HTTP {status}: access refused"
    if status == 429:
        return f"HTTP 429: rate limited (Retry-After {h.get('retry-after', 'not given')})"
    if "login" in final_url.lower() or "gateway.finra.org" in final_url.lower():
        return f"redirected to a login page ({final_url})"
    ctype = h.get("content-type", "")
    if expect_json and status == 404 and "json" not in ctype:
        return "HTTP 404: an HTML 'Not Found' page instead of the JSON the bond page requests"
    if expect_json and 200 <= status < 300 and "html" in ctype:
        return f"HTTP {status}: an HTML page instead of JSON"
    return None


# ---------------------------------------------------------------- envelopes
def parse_envelope(body: bytes | str) -> list[dict[str, Any]]:
    """The records in a dynamic-reporting response (the `data` string inside `returnBody`)."""
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise ParseError(f"response is not JSON: {exc}") from None
    rb = payload.get("returnBody") if isinstance(payload, dict) else None
    if not isinstance(rb, dict):
        raise ParseError("response has no returnBody")
    code = rb.get("statusCode", 200)
    if code != 200:
        raise ParseError(f"returnBody.statusCode is {code!r}")
    data = rb.get("data")
    if data is None or data == "":
        return []
    if isinstance(data, str):
        try:
            data = json.loads(data, parse_float=Decimal)
        except ValueError as exc:
            raise ParseError(f"returnBody.data is not JSON: {exc}") from None
    if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
        raise ParseError("returnBody.data is not a list of records")
    return data


def parse_template(body: bytes | str) -> tuple[str, str, list[str]]:
    """(datasetGroup, datasetName, schema field names) from a template composite response."""
    try:
        payload = json.loads(body)
        rb = payload["returnBody"]
        meta = rb["layout"]["layoutMetadata"]
        fields = list(rb["schema"]["properties"])
        group, name = str(meta["datasetGroup"]), str(meta["datasetName"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ParseError(f"template response is not in the known shape: {exc!r}") from None
    if not group or not name or not fields:
        raise ParseError("template names no dataset or fields")
    return group, name, fields


# ---------------------------------------------------------------- values
def number(value: Any, what: str, bounds: tuple[Decimal, Decimal] | None = None) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ParseError(f"{what} is not a number: {value!r}")
    try:
        dec = Decimal(str(value))
    except InvalidOperation:
        raise ParseError(f"{what} is not a number: {value!r}") from None
    if not dec.is_finite():
        raise ParseError(f"{what} is not finite: {value!r}")
    if bounds and not bounds[0] <= dec <= bounds[1]:
        raise ParseError(f"{what} {dec} is outside {bounds[0]}..{bounds[1]}")
    return dec


def yield_pct(value: Any, what: str) -> Decimal | None:
    y = number(value, what)
    if y is None or abs(y) >= YIELD_SENTINEL:
        return None
    if not YIELD_RANGE[0] <= y <= YIELD_RANGE[1]:
        raise ParseError(f"{what} {y} is outside {YIELD_RANGE[0]}..{YIELD_RANGE[1]}")
    return y


def day(value: Any, what: str) -> date | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ParseError(f"{what} is not a date: {value!r}") from None


def flag(value: Any) -> bool | None:
    return {"Y": True, "N": False}.get(str(value).strip().upper()) if value is not None else None


def eastern_to_utc(d: date, hhmmss: str) -> datetime:
    """A TRACE trade time (ET wall clock) on a trade date, as an aware UTC datetime."""
    try:
        t = time.fromisoformat(hhmmss.strip())
    except ValueError:
        raise ParseError(f"lastTradeTime is not HH:MM:SS: {hhmmss!r}") from None
    return datetime.combine(d, t, tzinfo=ET).astimezone(UTC)


# ---------------------------------------------------------------- records
@dataclass(frozen=True)
class Security:
    cusip: str
    issuer_name: str | None
    coupon_rate_pct: Decimal | None
    maturity: date | None
    is_144a: bool | None
    is_convertible: bool | None
    product: str | None
    last_trade_date: date | None
    last_trade_at: datetime | None  # UTC, when the record gives lastTradeTime
    last_sale_price: Decimal | None
    last_sale_yield: Decimal | None


@dataclass(frozen=True)
class EodRow:
    cusip: str
    trade_date: date
    price: Decimal
    yield_pct: Decimal | None
    product: str | None


def parse_security(rec: dict[str, Any]) -> Security:
    cusip = str(rec.get("cusip") or "").strip().upper()
    if len(cusip) != 9:
        raise ParseError(f"security record without a 9-character cusip: {rec!r}"[:200])
    last_date = day(rec.get("lastTradeDate"), f"{cusip} lastTradeDate")
    at = None
    if last_date and rec.get("lastTradeTime"):
        at = eastern_to_utc(last_date, str(rec["lastTradeTime"]))
    issuer = " ".join(str(rec.get("issuerName") or "").split()) or None
    return Security(
        cusip=cusip, issuer_name=issuer,
        coupon_rate_pct=number(rec.get("couponRate"), f"{cusip} couponRate", (Decimal(0), Decimal(100))),
        maturity=day(rec.get("maturityDate"), f"{cusip} maturityDate"),
        is_144a=flag(rec.get("is144A")), is_convertible=flag(rec.get("isConvertible")),
        product=rec.get("productSubTypeCode") or rec.get("productType"),
        last_trade_date=last_date, last_trade_at=at,
        last_sale_price=number(rec.get("lastSalePrice"), f"{cusip} lastSalePrice", PRICE_RANGE) or None,
        last_sale_yield=yield_pct(rec.get("lastSaleYield"), f"{cusip} lastSaleYield"),
    )


def parse_eod(records: list[dict[str, Any]], cusip: str | None = None) -> list[EodRow]:
    """End-of-day rows. A record without a price, or with price 0 and no yield (FINRA's day record when no
    last sale was disseminated), is skipped: nothing traded is nothing to write."""
    out: dict[date, EodRow] = {}
    for rec in records:
        c = str(rec.get("cusip") or "").strip().upper()
        if cusip is not None and c != cusip:
            raise ParseError(f"price history for {cusip} holds a row for {c!r}")
        d = day(rec.get("tradeDate"), f"{c} tradeDate")
        if d is None:
            raise ParseError(f"{c}: price history row without tradeDate")
        price = number(rec.get("lastSalePrice"), f"{c} {d} lastSalePrice", PRICE_RANGE)
        y = yield_pct(rec.get("lastSaleYield"), f"{c} {d} lastSaleYield")
        if price is None or (price == 0 and y is None):
            continue
        row = EodRow(c, d, price, y, rec.get("productType"))
        if d in out and out[d].price != row.price:
            raise ParseError(f"{c}: two different end-of-day prices on {d.isoformat()}")
        out[d] = row
    return [out[d] for d in sorted(out)]
