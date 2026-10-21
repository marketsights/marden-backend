"""Pure parsers and rules for the `sec_nport` collector (no network, no database).

- `prescan`: the identifier tokens in an N-PORT `primary_doc.xml` (fast byte regexes), used to
  decide whether a filing holds a watched instrument before it is parsed or stored.
- `parse_nport`: an N-PORT `primary_doc.xml` -> header (registrant, series, report date, net
  assets) and the positions (Item C: identifiers, balance, units, currency, value, level, ...).
- `mark_price`: 100 x value / balance in percent of par, for units PA only (price-sources.md §5);
  non-USD positions in their own currency once the rate's direction is known (sec_nport_fx).
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from lxml import etree

from marden.collectors.sec_index_parse import IndexLine, filing_folder, normalize_cik, quarter
from marden.collectors.sec_nport_fx import PER_USD, USD_PER
from marden.seed.instruments_nport import fund_mark_price
from marden.seed.instruments_parse import PLACEHOLDER_CUSIPS, cusip_valid, isin_valid

NPORT_FORMS = frozenset({"NPORT-P", "NPORT-P/A"})
NS = "http://www.sec.gov/edgar/nport"
DATASETS = "https://www.sec.gov/files/dera/data/form-n-port-data-sets"
PAR_UNITS = "PA"
# ISIN prefixes whose national number is the CUSIP (CUSIP Global Services numbers US and CA ISINs).
CUSIP_ISIN_PREFIXES = ("US", "CA")
_ID_TOKEN = re.compile(rb"^[0-9A-Z*@#]{6,20}$")


class NportParseError(ValueError):
    """The document is not an N-PORT submission in the shape this parser knows."""


# ---------------------------------------------------------------- URLs and dates
def primary_doc_url(cik: str, accession: str) -> str:
    return f"{filing_folder(cik, accession)}/primary_doc.xml"


def dataset_label(year: int, q: int) -> str:
    return f"{year}q{q}"


def dataset_url(label: str) -> str:
    return f"{DATASETS}/{label}_nport.zip"


def parse_label(label: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{4})q([1-4])", label.strip().lower())
    if m is None:
        raise ValueError(f"not a data set quarter like 2026q3: {label!r}")
    return int(m.group(1)), int(m.group(2))


def quarter_end(year: int, q: int) -> date:
    return date(year + (q == 4), 3 * q % 12 + 1, 1) - timedelta(days=1)


def previous_quarter(day: date) -> tuple[int, int]:
    q = quarter(day)
    return (day.year, q - 1) if q > 1 else (day.year - 1, 4)


def dera_date(text: str) -> date | None:
    """DERA dates look like 24-JUL-2026."""
    text = (text or "").strip()
    try:
        return datetime.strptime(text, "%d-%b-%Y").date() if text else None
    except ValueError:
        return None


def nport_lines(lines: Iterable[IndexLine]) -> list[IndexLine]:
    """NPORT-P and NPORT-P/A lines of a daily form index (one per accession)."""
    seen: set[str] = set()
    out = []
    for line in lines:
        if line.form.strip().upper() in NPORT_FORMS and line.accession not in seen:
            seen.add(line.accession)
            out.append(line)
    return out


# ---------------------------------------------------------------- identifiers
def norm_id(value: str | bytes | None) -> str | None:
    """An identifier as written, upper-cased; None for blanks and placeholders (000000000, N/A)."""
    if value is None:
        return None
    text = (value.decode("utf-8", "replace") if isinstance(value, bytes) else value).strip().upper()
    return None if text in PLACEHOLDER_CUSIPS or set(text) <= {"0", "9"} else text


def isin_cusip(isin: str | None) -> str | None:
    """The CUSIP inside a valid US or CA ISIN (its national number); None otherwise."""
    if isin and isin[:2] in CUSIP_ISIN_PREFIXES and isin_valid(isin):
        return isin[2:11]
    return None


def id_keys(*values: str | None) -> set[str]:
    """Join keys for a set of identifiers: each value, plus the CUSIP inside a US/CA ISIN."""
    out: set[str] = set()
    for value in values:
        v = norm_id(value)
        if v:
            out.add(v)
            if (c := isin_cusip(v)) is not None:
                out.add(c)
    return out


_PRESCAN = (
    re.compile(rb"<(?:\w+:)?cusip>\s*([^<\s]+)\s*</(?:\w+:)?cusip>"),
    re.compile(rb"<(?:\w+:)?isin\b[^>]*?\bvalue=\"([^\"]+)\""),
    re.compile(rb"<(?:\w+:)?other\b[^>]*?\bvalue=\"([^\"]+)\""),
)


def prescan(data: bytes) -> set[str]:
    """Every CUSIP, ISIN and 'other' identifier value in the document (anywhere, including the
    reference instruments of derivatives), as join keys. Cheap enough to run on every filing."""
    tokens: set[str] = set()
    for pattern in _PRESCAN:
        for raw in pattern.findall(data):
            raw = raw.strip().upper()
            if _ID_TOKEN.match(raw):
                tokens.add(raw.decode("ascii"))
    return id_keys(*tokens)


# ---------------------------------------------------------------- primary_doc.xml
@dataclass(frozen=True)
class NportHeader:
    submission_type: str
    reg_name: str
    reg_cik: str | None
    reg_lei: str | None
    series_name: str | None
    series_id: str | None
    series_lei: str | None
    period: date  # repPdDate: the date as of which the holdings are reported
    fiscal_year_end: date | None  # repPdEnd
    total_assets: Decimal | None
    total_liabilities: Decimal | None
    net_assets: Decimal | None

    @property
    def fund_name(self) -> str:
        return self.series_name or self.reg_name


@dataclass(frozen=True)
class Position:
    ordinal: int  # 1-based order of the invstOrSec element in the filing
    name: str
    title: str
    lei: str | None
    cusip: str | None  # as filed (placeholders dropped)
    isin: str | None
    others: tuple[tuple[str, str], ...] = ()  # (otherDesc, value)
    balance: Decimal | None = None
    units: str | None = None
    cur_cd: str | None = None
    exchange_rt: Decimal | None = None
    val_usd: Decimal | None = None
    pct_val: Decimal | None = None
    payoff: str | None = None
    asset_cat: str | None = None
    issuer_cat: str | None = None
    country: str | None = None
    fair_value_level: str | None = None
    keys: frozenset[str] = field(default_factory=frozenset)

    @property
    def cusip_valid(self) -> str | None:
        return self.cusip if self.cusip and cusip_valid(self.cusip) else None

    @property
    def isin_valid(self) -> str | None:
        return self.isin if self.isin and isin_valid(self.isin) else None


@dataclass(frozen=True)
class NportDoc:
    header: NportHeader
    positions: tuple[Position, ...]


def _local(tag: object) -> str:
    return etree.QName(tag).localname if isinstance(tag, str) else ""


def _child(node: etree._Element, name: str) -> etree._Element | None:
    for c in node:
        if _local(c.tag) == name:
            return c
    return None


def _text(node: etree._Element | None, name: str) -> str | None:
    c = _child(node, name) if node is not None else None
    text = (c.text or "").strip() if c is not None else ""
    return text or None


def _dec(text: str | None) -> Decimal | None:
    if text is None or text.strip().upper() in ("", "N/A", "NA"):
        return None
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        raise NportParseError(f"not a number: {text!r}") from None
    if not value.is_finite():
        raise NportParseError(f"not a finite number: {text!r}")
    return value


def _date(text: str | None) -> date | None:
    try:
        return date.fromisoformat(text.strip()[:10]) if text else None
    except ValueError:
        return None


def _position(ordinal: int, node: etree._Element) -> Position:
    isin: str | None = None
    others: list[tuple[str, str]] = []
    ids = _child(node, "identifiers")
    if ids is not None:
        for c in ids:
            kind = _local(c.tag)
            if kind == "isin":
                isin = norm_id(c.get("value"))
            elif kind == "other" and (v := norm_id(c.get("value"))):
                others.append(((c.get("otherDesc") or "").strip(), v))
    cur_cd, exchange_rt = _text(node, "curCd"), None
    if (cond := _child(node, "currencyConditional")) is not None:
        cur_cd, exchange_rt = (cond.get("curCd") or "").strip() or None, _dec(cond.get("exchangeRt"))
    asset_cat = _text(node, "assetCat")
    if asset_cat is None and (cond := _child(node, "assetConditional")) is not None:
        asset_cat = (cond.get("assetCat") or "").strip() or None
    issuer_cat = _text(node, "issuerCat")
    if issuer_cat is None and (cond := _child(node, "issuerConditional")) is not None:
        issuer_cat = (cond.get("issuerCat") or "").strip() or None
    cusip = norm_id(_text(node, "cusip"))
    return Position(
        ordinal=ordinal, name=_text(node, "name") or "", title=_text(node, "title") or "", lei=_text(node, "lei"),
        cusip=cusip, isin=isin, others=tuple(others), balance=_dec(_text(node, "balance")),
        units=(_text(node, "units") or "").upper() or None, cur_cd=(cur_cd or "").upper() or None,
        exchange_rt=exchange_rt, val_usd=_dec(_text(node, "valUSD")), pct_val=_dec(_text(node, "pctVal")),
        payoff=_text(node, "payoffProfile"), asset_cat=asset_cat, issuer_cat=issuer_cat,
        country=_text(node, "invCountry"), fair_value_level=_text(node, "fairValLevel"),
        keys=frozenset(id_keys(cusip, isin, *(v for _, v in others))),
    )


def parse_nport(data: bytes) -> NportDoc:
    """Header and positions of an N-PORT primary_doc.xml (streamed, so large funds stay cheap)."""
    header_parts: dict[str, etree._Element] = {}
    positions: list[Position] = []
    submission_type = None
    try:
        for _, node in etree.iterparse(io.BytesIO(data), events=("end",), huge_tree=True, resolve_entities=False):
            name = _local(node.tag)
            if name == "invstOrSec":
                positions.append(_position(len(positions) + 1, node))
                node.clear(keep_tail=False)
            elif name in ("genInfo", "fundInfo"):
                header_parts[name] = node
            elif name == "submissionType" and submission_type is None:
                submission_type = (node.text or "").strip()
            elif name == "invstOrSecs":
                node.clear(keep_tail=False)
    except etree.XMLSyntaxError as exc:
        raise NportParseError(f"not well-formed XML: {exc}") from None
    gen = header_parts.get("genInfo")
    fund = header_parts.get("fundInfo")
    period = _date(_text(gen, "repPdDate")) if gen is not None else None
    if gen is None or period is None or not submission_type:
        raise NportParseError("not an N-PORT submission: no genInfo/repPdDate/submissionType")
    header = NportHeader(
        submission_type=submission_type.upper(), reg_name=_text(gen, "regName") or "",
        reg_cik=normalize_cik(_text(gen, "regCik")), reg_lei=_text(gen, "regLei"),
        series_name=_text(gen, "seriesName"), series_id=_series_id(_text(gen, "seriesId")),
        series_lei=_text(gen, "seriesLei"), period=period, fiscal_year_end=_date(_text(gen, "repPdEnd")),
        total_assets=_dec(_text(fund, "totAssets")), total_liabilities=_dec(_text(fund, "totLiabs")),
        net_assets=_dec(_text(fund, "netAssets")),
    )
    return NportDoc(header, tuple(positions))


def _series_id(text: str | None) -> str | None:
    text = (text or "").strip().upper()
    return text if re.fullmatch(r"S\d{9}", text) else None


def mark_price(p: Position, fx: str | None = None) -> Decimal | None:
    """Percent of par, 4 decimals: 100 x value / balance for a long par-amount (PA) position. A
    non-USD position is valued in its own currency first, which needs the direction of its exchange
    rate (`fx`, see sec_nport_fx): valUSD x rate for currency per dollar, valUSD / rate for dollars
    per currency. None when no price can be read: other units, no balance, a negative value (short),
    no currency, or a foreign currency whose rate direction is not verified."""
    if p.units != PAR_UNITS or p.balance is None or p.balance <= 0 or p.val_usd is None or p.val_usd < 0:
        return None
    if p.cur_cd == "USD":
        return fund_mark_price(p.val_usd, p.balance)
    if not p.cur_cd or p.exchange_rt is None or p.exchange_rt <= 0 or fx not in (PER_USD, USD_PER):
        return None
    value = p.val_usd * p.exchange_rt if fx == PER_USD else p.val_usd / p.exchange_rt
    return fund_mark_price(value, p.balance)
