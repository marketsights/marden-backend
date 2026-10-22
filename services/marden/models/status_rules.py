"""The fixed chain status rules (00-global "Status rules"), applied to one site's inputs.

| Link | Amber | Red |
| --- | --- | --- |
| end_user | E1 its plan needs new funding | E2 contract cut, or the end user exits |
| tenant | T1 rated BBB- or lower, or must post contested collateral | T2 BBB- or lower and power denied or |
| | | withdrawn (a withdrawal only if nothing replaces it), or force majeure filed |
| developer | D1 in-service date slips 3+ months | D2 construction halted |
| lenders | L1 Marden price 95-98 on credit alone, or | L2 Marden price below 95 on credit alone, or |
| | syndication stalled | default notice |

Chain status = worst link: any red -> Broken, any amber -> Strained, else Holding; 'No build' when a
statement says construction has not started. A link without a named party is 'none' (lenders are judged
whenever the site has debt). Each tripped rule keeps the statement that tripped it (the ChainLink `fact`);
when several statements trip one rule, the filed one first, then the earliest. The price tests of L1 and L2
are rate-neutral: a fixed-rate instrument trips L2 when its spread move since the reference day (issue, else
the first Marden price) alone is worth 5+ points and L1 when it is worth 2 up to 5 points
(`marden.models.status_credit`); a floating-rate loan trips L2 when its Marden price is below 95 and L1 at
95-98. A withdrawn permit or queue position counts for T2 only when no other permit, application or queue
position for the same plant / campus is live on the day (v0.4, `marden.models.status_power`). Text rules:
`marden.models.status_text`; dates:
`marden.models.status_dates`; method: docs/methods/status-rules-v0.1.md.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

from marden.models import status_dates as dates
from marden.models import status_power as sp
from marden.models import status_text as tx
from marden.models.status_credit import LOSS_AMBER_POINTS, LOSS_RED_POINTS
from marden.models.status_inputs import Inputs, Item, Party, Price, SiteInputs

METHOD_VERSION = "status v0.4"
PRICE_RED_BELOW = 95.0
PRICE_AMBER_AT_OR_BELOW = 98.0
L1_PRICE = "Marden price 95-98"  # a floating-rate loan (or terms not on file)
L1_SPREAD = "Marden spread loss of 2-5 points"  # a fixed coupon: the spread move alone (status_credit)
L2_PRICE = "Marden price below 95"  # a floating-rate loan (or terms not on file)
L2_SPREAD = "Marden spread loss of 5+ points"  # a fixed coupon: the spread move alone (status_credit)
PRICE_FINDINGS = (f"L1 {L1_PRICE}", f"L1 {L1_SPREAD}", f"L2 {L2_PRICE}", f"L2 {L2_SPREAD}")  # a link's reason
SLIP_MONTHS = 3
TAG_RANK = {"F": 0, "R": 1, "M": 2}
SEVERITY = {"none": 0, "green": 1, "amber": 2, "red": 3}

LEASE_FIELDS = frozenset({"lease_termination", "leases"})
POWER_FIELDS = frozenset({"power_contract", "power_detail", "power_status", "power_supply", "onsite_generation",
                          "risk_evidence", "schedule_change", "rfs_statement"})
DEBT_FIELDS = frozenset({"debt_facility", "tranches", "debt_items", "research_terms", "status"})
NO_SCAN = frozenset({"place", "tenant_rating", "expected_energization", "original_energization"})
DELIVERY = re.compile(r"\b(deliver\w*|in[- ]service|complet\w*|energi[sz]\w*|operation\w*|COD|ready[- ]for[- ]service"
                      r"|RFS|rent (?:start|commence\w*)|go live|online)\b", re.I)
POWER_SUPPLY = re.compile(r"\b(pipeline|gas|lateral|permit|turbine|interconnect\w*|transmission|fuel)\b", re.I)
RATING_ACTION = re.compile(r"\b(downgrad\w*|upgrad\w*|cut|cuts|rated|rating|affirm\w*|outlook)\b", re.I)
NO_BUILD = re.compile(r"\bconstruction (?:has not|hasn't|had not|not yet) (?:started|begun)\b|\bpre-construction\b"
                      r"|\bno construction (?:has )?(?:started|begun)\b", re.I)


@dataclass
class Finding:
    code: str
    status: str
    label: str
    item: Item | None = None
    detail: str = ""
    fact: dict[str, Any] | None = None  # a Sourced<string> that is not an Item (the Marden price)


FIELD_RANK = {"rfs_statement": 1}  # a schedule statement that mentions the event cites it least directly


def _rank(item: Item) -> tuple[int, date, int, str]:
    return TAG_RANK.get(item.tag, 3), item.day, FIELD_RANK.get(item.field, 0), item.origin


def _pick(hits: list[tuple[Item, tx.Hit]]) -> tuple[Item, tx.Hit] | None:
    return min(hits, key=lambda h: _rank(h[0])) if hits else None


def _scan(items: Iterable[Item], detect: Callable[[str], tx.Hit | None], *, site: SiteInputs,
          names: list[str] | None = None, unless_fields: frozenset[str] = frozenset(),
          fields: frozenset[str] | None = None) -> list[tuple[Item, tx.Hit]]:
    """Statements that trip `detect`. Generic news must name the site; with `names`, the statement must name
    the party too (except in `unless_fields`, e.g. the site's own lease terms)."""
    hits = []
    for it in items:
        if it.field in NO_SCAN or (fields is not None and it.field not in fields and not it.field.startswith("event")):
            continue
        if it.generic and not tx.mentions(it.text, site.keywords):
            continue
        if names is not None and it.field not in unless_fields and not tx.mentions(it.text, names):
            continue
        hit = detect(it.text)
        if hit:
            hits.append((it, hit))
    return hits


def _cite(item: Item) -> str:
    return f"{item.source_name}, {item.day.isoformat()}"


# ---------------------------------------------------------------- end user
def end_user(site: SiteInputs, inputs: Inputs, missing: list[str]) -> list[Finding]:
    party = site.parties["end_user"]
    own = inputs.entity_items.get(party.entity_id or "", [])
    found = []
    cut = _pick(_scan(site.items + own, tx.contract_cut, site=site, names=party.names))
    if cut:
        found.append(Finding("E2", "red", "Contract cut or end user exit", cut[0], cut[1].clause))
    funding_rows = [it for it in own if it.field == "funding_fact"]
    need = _pick(_scan(own, tx.funding_need, site=site) + _scan(site.items, tx.funding_need, site=site,
                                                                 names=party.names))
    if need:
        found.append(Finding("E1", "amber", "End user's plan needs new funding", need[0], need[1].clause))
    if not funding_rows and not need:
        missing.append(f"end_user: funding facts for {party.label}")
    return found


# ---------------------------------------------------------------- tenant
@dataclass(frozen=True)
class RatingEvidence:
    rating: tx.Rating
    item: Item


def rating_pool(inputs: Inputs) -> dict[str, list[RatingEvidence]]:
    """Rating statements per tenant entity, from every site that has the entity as tenant: the sites'
    tenant_rating texts and dated rating headlines that name the entity."""
    pool: dict[str, list[RatingEvidence]] = defaultdict(list)
    for site in inputs.sites.values():
        tenant = site.parties.get("tenant")
        if tenant is None or tenant.entity_id is None:
            continue
        for it in site.items:
            if it.field == "tenant_rating" or (
                it.field in ("risk_evidence", "schedule_change") and tx.mentions(it.text, tenant.names)
                and RATING_ACTION.search(it.text)
            ):
                parsed = tx.parse_rating(it.text, None if it.field == "tenant_rating" else tenant.names)
                if parsed is not None:
                    pool[tenant.entity_id].append(RatingEvidence(parsed, it))
    return pool


def tenant_rating(site: SiteInputs, tenant: Party, pool: dict[str, list[RatingEvidence]]) -> RatingEvidence | None:
    """The latest rating statement for the tenant (same day: filed first, then the lowest grade)."""
    if tenant.entity_id:
        evidence = pool.get(tenant.entity_id, [])
    else:
        evidence = [RatingEvidence(r, it) for it in site.items if it.field == "tenant_rating"
                    if (r := tx.parse_rating(it.text)) is not None]
    if not evidence:
        return None
    return min(evidence, key=lambda e: (-e.item.day.toordinal(), TAG_RANK.get(e.item.tag, 3),
                                        -(e.rating.notch or 0)))


def power_failures(site: SiteInputs, notes: list[str] | None = None) -> list[tuple[Item, tx.Hit]]:
    """T2's power statements: denials, and withdrawals that nothing replaces on the day (v0.4,
    `status_power`). A replaced withdrawal is left out, with a note naming what replaced it."""
    out = []
    for it, hit in _scan(site.items, tx.power_denied, site=site, fields=POWER_FIELDS):
        if hit.rule == "power_withdrawn" and (by := sp.replacement(site, it)) is not None:
            if notes is not None:
                notes.append(f"Not T2: '{hit.clause}' ({_cite(it)}) is replaced by '{by.text}' ({by.cite})")
            continue
        out.append((it, hit))
    return out


def tenant(site: SiteInputs, inputs: Inputs, pool: dict[str, list[RatingEvidence]],
           missing: list[str], notes: list[str] | None = None) -> tuple[list[Finding], RatingEvidence | None]:
    party = site.parties["tenant"]
    own = inputs.entity_items.get(party.entity_id or "", [])
    found = []
    fm = _scan(site.items, tx.force_majeure, site=site, names=party.names, unless_fields=LEASE_FIELDS)
    fm += [h for h in _scan(own, tx.force_majeure, site=site) if tx.mentions(h[0].text, site.keywords)]
    if (pick := _pick(fm)) is not None:
        found.append(Finding("T2", "red", "Force majeure filed", pick[0], pick[1].clause))
    rated = tenant_rating(site, party, pool)
    if rated is not None and rated.rating.at_or_below_bbb_minus:
        power = _pick(power_failures(site, notes))
        if power:
            found.append(Finding("T2", "red", f"Rated {rated.rating.grade} and power denied or withdrawn", power[0],
                                 f"{power[1].clause}; rating: {rated.rating.clause} ({_cite(rated.item)})"))
        found.append(Finding("T1", "amber", f"Rated {rated.rating.grade} (BBB- or lower)", rated.item,
                             rated.rating.clause))
    collateral = _pick(_scan(site.items + own, tx.collateral_contested, site=site, names=party.names))
    if collateral:
        found.append(Finding("T1", "amber", "Must post contested collateral", collateral[0], collateral[1].clause))
    if rated is None:
        missing.append(f"tenant: rating for {party.label}")
    return found, rated


# ---------------------------------------------------------------- developer
@dataclass(frozen=True)
class Slip:
    months: int
    before: dates.Period
    after: dates.Period
    item: Item
    before_item: Item


def rfs_slips(site: SiteInputs) -> list[Slip]:
    """Per project: the latest ready-for-service target against the first one stated."""
    by_project: dict[str, list[Item]] = defaultdict(list)
    for it in site.items:
        if it.field == "rfs_statement" and isinstance(it.value, dict):
            by_project[str(it.value.get("project"))].append(it)
    slips = []
    for statements in by_project.values():
        statements.sort(key=lambda it: (it.day, it.value.get("which") == "latest", it.origin))
        dated = [(it, p) for it in statements if (p := dates.first_period(it.text)) is not None]
        if len(dated) >= 2:
            (first, p0), (last, p1) = dated[0], dated[-1]
            slips.append(Slip(dates.slip_months(p0, p1), p0, p1, last, first))
    return slips


def _latest(site: SiteInputs, field: str) -> Item | None:
    rows = [it for it in site.items if it.field == field]
    return min(rows, key=lambda it: (-it.day.toordinal(), TAG_RANK.get(it.tag, 3))) if rows else None


def energization_slip(site: SiteInputs) -> Slip | None:
    original, expected = _latest(site, "original_energization"), _latest(site, "expected_energization")
    if not original or not expected:
        return None
    p0, p1 = dates.first_period(original.text), dates.first_period(expected.text)
    if not p0 or not p1:
        return None
    return Slip(dates.slip_months(p0, p1), p0, p1, expected, original)


def developer(site: SiteInputs, missing: list[str]) -> list[Finding]:
    found = []
    halted = _pick(_scan(site.items, tx.construction_halted, site=site))
    expected = _latest(site, "expected_energization")
    if expected and tx.CANCELLED.search(expected.text):
        halted = halted or (expected, tx.Hit("construction_halted", expected.text))
    if halted:
        found.append(Finding("D2", "red", "Construction halted", halted[0], halted[1].clause))
    slips = rfs_slips(site)
    if (pair := energization_slip(site)) is not None:
        slips.append(pair)
    stated = [(it, n) for it in site.items if it.field == "schedule_change" and DELIVERY.search(it.text)
              and not POWER_SUPPLY.search(it.text) and (n := dates.stated_slip(it.text)) is not None]
    big = [s for s in slips if s.months >= SLIP_MONTHS]
    if big:
        worst = max(big, key=lambda s: (s.months, -s.item.day.toordinal()))
        detail = (f"target moved {worst.months} months, {worst.before.month} -> {worst.after.month}: "
                  f"'{worst.before.text}' ({_cite(worst.before_item)}) -> '{worst.after.text}'")
        found.append(Finding("D1", "amber", f"In-service date slipped {worst.months} months", worst.item, detail))
    elif any(n >= SLIP_MONTHS for _, n in stated):
        it, n = max(stated, key=lambda x: x[1])
        found.append(Finding("D1", "amber", f"In-service date slipped {n} months", it, it.text))
    if not slips and not stated:
        missing.append("developer: two dated in-service targets to compare")
    return found


# ---------------------------------------------------------------- lenders
def _price_fact(p: Price, value: str, method: str | None = None) -> dict[str, Any]:
    fact = {"value": value, "tag": "M", "asOf": p.as_of.isoformat(), "sourceName": "Marden model price",
            "methodVersion": method or p.method_version or "price"}
    if p.source_url:
        fact["sourceUrl"] = p.source_url
    return fact


def credit_shortfall(p: Price) -> float | None:
    """Points below par on credit alone: a fixed coupon's spread-only loss since its reference day, a
    floating-rate loan's (or a price without terms) 100 - price. None when a fixed coupon has no reading."""
    if p.coupon_type == "fixed":
        return p.credit.loss if p.credit is not None else None
    return 100.0 - p.value


def price_rule(p: Price) -> str | None:
    """'L2' | 'L1' | None: the price rule one instrument trips. A fixed coupon by its spread-only loss (5+
    points: L2; 2 up to 5: L1), a floating-rate loan (or a price without terms) by its price (below 95: L2;
    95-98: L1). A fixed coupon without a rate-neutral reading trips neither."""
    if p.coupon_type == "fixed":
        if p.credit is None:
            return None
        loss = p.credit.loss
        return "L2" if loss >= LOSS_RED_POINTS else "L1" if loss >= LOSS_AMBER_POINTS else None
    return "L2" if p.value < PRICE_RED_BELOW else "L1" if p.value <= PRICE_AMBER_AT_OR_BELOW else None


def _price_finding(code: str, p: Price) -> Finding:
    """The L1 / L2 finding on one instrument's Marden price (fixed coupon: its spread-only loss)."""
    status = "red" if code == "L2" else "amber"
    if p.credit is None:  # floating rate: the price is already a credit price
        kind = "floating rate" if p.coupon_type == "floating" else "coupon type n/d"
        text = f"Marden price {p.value:.1f} ({p.instrument_id}, {kind})"
        label = L2_PRICE if code == "L2" else L1_PRICE
        return Finding(code, status, label, detail=text, fact=_price_fact(p, text))
    c = p.credit
    text = (f"{p.instrument_id}: Marden spread {c.spread_bp:.0f} bp vs {c.ref.spread_bp:.0f} bp {c.ref.at}, "
            f"{c.loss:.1f} points on spread alone (spread duration {c.spread_duration:.1f}; rate-neutral price "
            f"{c.credit_price:.1f} vs {c.ref_price:.1f}); Marden price {p.value:.1f}")
    fact = _price_fact(p, f"Marden spread loss {c.loss:.1f} points ({p.instrument_id}: {c.spread_bp:.0f} bp vs "
                          f"{c.ref.spread_bp:.0f} bp {c.ref.at})",
                       f"{METHOD_VERSION} on {p.method_version or 'price'}")
    return Finding(code, status, L2_SPREAD if code == "L2" else L1_SPREAD, detail=text, fact=fact)


def lenders(site: SiteInputs, missing: list[str]) -> list[Finding]:
    found = []
    debt_items = site.items + site.instrument_items
    default = _scan(debt_items, tx.default_notice, site=site, fields=DEBT_FIELDS)
    default += [(it, tx.Hit("default_notice", it.text)) for it in site.instrument_items
                if it.field == "status" and it.text.strip().lower() in tx.DEFAULTED_STATUSES]
    if (pick := _pick(default)) is not None:
        found.append(Finding("L2", "red", "Default notice", pick[0], pick[1].clause))
    if site.prices:
        for p in site.prices:
            if p.coupon_type == "fixed" and p.credit is None:
                missing.append(f"lenders: {p.credit_missing or p.instrument_id} (rate-neutral L1 / L2 test)")
        tripped: dict[str, list[tuple[float, str, Price]]] = defaultdict(list)
        for p in site.prices:
            if (code := price_rule(p)) is not None:
                tripped[code].append((credit_shortfall(p) or 0.0, p.instrument_id, p))
        for code in ("L2", "L1"):  # the worst rule; within it the largest loss on credit alone
            if tripped[code]:
                found.append(_price_finding(code, max(tripped[code], key=lambda r: r[:2])[2]))
                break
    else:
        missing.append("lenders: Marden price (price model output)")
    stalled = _pick(_scan(debt_items, tx.syndication_stalled, site=site, fields=DEBT_FIELDS))
    if stalled:
        found.append(Finding("L1", "amber", "Syndication stalled", stalled[0], stalled[1].clause))
    return found


def _price_note(p: Price) -> str:
    """The green lenders link's price clause: the lowest Marden price and, for a fixed coupon, its spread move."""
    if p.credit is None:
        return f"Marden price {p.value:.1f}"
    return (f"Marden price {p.value:.1f} ({p.instrument_id}: spread-only loss {p.credit.loss:.1f} points "
            f"{p.credit.ref.since})")


# ---------------------------------------------------------------- links and chain
def _link(role: str, party: str, findings: list[Finding], green_reason: str,
          green_fact: dict[str, Any] | None = None, notes: list[str] | None = None) -> dict[str, Any]:
    """One ChainLink; `notes` (statements a rule read and set aside, e.g. a replaced withdrawal) end the reason."""
    tail = "".join(f". {n}" for n in dict.fromkeys(notes or []))
    if not findings:
        out: dict[str, Any] = {"role": role, "party": party, "status": "green", "reason": green_reason + tail}
        if green_fact:
            out["fact"] = green_fact
        return out
    ordered = sorted(findings, key=lambda f: -SEVERITY[f.status])
    top = ordered[0]
    parts = []
    for f in ordered[:3]:
        cite = f" ({_cite(f.item)})" if f.item else ""
        parts.append(f"{f.code} {f.label}: {f.detail}{cite}" if f.detail else f"{f.code} {f.label}{cite}")
    link: dict[str, Any] = {"role": role, "party": party, "status": top.status, "ruleCode": top.code,
                            "reason": ". ".join(parts) + tail}
    fact = top.fact or (top.item.sourced() if top.item else None)
    if fact:
        link["fact"] = fact
    return link


def _none(role: str, why: str) -> dict[str, Any]:
    return {"role": role, "party": "—", "status": "none", "reason": why}


def chain_status(chain: list[dict[str, Any]], no_build: bool) -> str:
    if no_build:
        return "No build"
    worst = max((SEVERITY[link["status"]] for link in chain), default=0)
    return {3: "Broken", 2: "Strained"}.get(worst, "Holding")


def evaluate(site: SiteInputs, inputs: Inputs, pool: dict[str, list[RatingEvidence]]) -> dict[str, Any]:
    """The 'status' model output for one site: {chain: ChainLink[4], status: ChainStatus, missing: [...]}."""
    missing: list[str] = []
    parties = site.parties
    chain = []
    if "end_user" in parties:
        chain.append(_link("end_user", parties["end_user"].label, end_user(site, inputs, missing),
                           "No end-user rule met: no contract cut, exit or funding need on file"))
    else:
        chain.append(_none("end_user", "No separate end user is named for this site"))
    if "tenant" in parties:
        notes: list[str] = []
        found, rated = tenant(site, inputs, pool, missing, notes)
        grade = (rated.rating.grade or "unrated") if rated else "n/d"
        chain.append(_link("tenant", parties["tenant"].label, found,
                           f"No tenant rule met: rated {grade}; no force majeure or contested collateral on file",
                           rated.item.sourced() if rated else None,
                           notes if not any(f.code == "T2" and f.label.startswith("Rated") for f in found) else []))
    else:
        chain.append(_none("tenant", "No tenant is named for this site"))
    dev = parties.get("developer")
    chain.append(_link("developer", dev.label if dev else "n/d", developer(site, missing),
                       "No developer rule met: no in-service slip of 3+ months and no halt on file"))
    if "lenders" in parties or site.instruments:
        lend = parties.get("lenders")
        price = min(site.prices, key=lambda p: p.value) if site.prices else None
        green = "No lender rule met: no default notice or stalled syndication on file"
        green += f"; {_price_note(price)}" if price else "; no Marden price yet"
        chain.append(_link("lenders", lend.label if lend else "n/d", lenders(site, missing), green))
    else:
        chain.append(_none("lenders", "No debt is linked to this site"))
    if not site.links:
        missing.append("campus / plant events: no approved site link")
    no_build = any(NO_BUILD.search(it.text) for it in site.items if it.tag in ("F", "R") and it.field != "place")
    return {"chain": chain, "status": chain_status(chain, no_build), "missing": missing}
