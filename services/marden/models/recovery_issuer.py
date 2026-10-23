"""The issuer balance-sheet waterfall (recovery v0.3, docs/methods/recovery-v0.1.md "v0.3"): what an issuer's
unsecured and parent-level creditors recover if it defaults, from its latest 10-Q / 10-K.

Replaces the flat 40c that corporate notes, converts and other issuer-level debt priced with. Per issuer
(the top of the issuer's parent chain; a holding company without a CIK takes the filer with its ticker:
Cipher Digital -> Cipher Digital Inc.):

1. Inputs, point in time. The balance sheet of the latest periodic filing public on the day (XBRL
   companyfacts, collector sec_xbrl; tag F, each linked to its filing): total assets, cash, restricted
   cash, receivables, PP&E net, right-of-use assets, goodwill and intangibles, total liabilities (else
   liabilities and equity less total equity), lease liabilities and balance-sheet debt. The debt stack is
   the instrument table (amount drawn else amount, seniority, collateral and ranking text; F or R facts),
   read with today's knowledge like the price model's terms and left out before its issue date. Leases
   signed but not commenced come from the lease curation (entity fact, F).
2. Assets after haircuts (tag M, every share stated in the output): cash and restricted cash 100%,
   receivables 80%; PP&E split into GPUs / servers (the stated share, else a share by the issuer's role)
   valued at their resale value over depreciated cost (the gpu_collateral pools of the issuer's own
   facilities: units x value per GPU less the 20% resale haircut over units x new price x the life left;
   else the GPU model's value by age at a 1.5-year-old fleet) and the rest (buildings, power, works) at the
   site recovery model's value of the issuer's own sites (works only, capped at their cost) and 55% beyond
   them; right-of-use assets, goodwill and intangibles 0%; other assets 25%; debt raised after the balance
   sheet date counts its proceeds at the PP&E blend.
3. Claims by priority:
   - secured at an SPV / opco (DDTLs, site notes, project debt): ahead of the holding company's creditors
     on their own collateral (structural subordination). Each collateral pool takes the lesser of its
     claims and its collateral value (the gpu_collateral or powered-shell value of this run); a pool with
     no collateral model is taken as covered. A shortfall is an unsecured claim at the parent only when the
     parent guarantees the debt (collateral or ranking text); non-recourse shortfalls stay at the SPV;
   - secured at the parent (revolvers, equipment financing, finance leases): next, from what is left;
   - unsecured, pro rata: notes, converts and unsecured loans, balance-sheet debt the instrument table does
     not itemise, lease rejection claims capped as 11 U.S.C. 502(b)(6) caps them (the rent for the greater
     of one year and 15% of the remaining term, at most three years; commenced leases from the liability
     at LEASE_RATE, signed-not-commenced ones from the commitment over the stated term), parent-guaranteed
     SPV shortfalls, and the other liabilities (payables, accruals, deferred revenue);
   - subordinated: what is left after the unsecured claims.
4. Recovery = value left for the rank / claims at the rank, clamped to [5c, 95c]; the range re-runs the
   waterfall with every non-cash asset 20% lower and higher.

An issuer without SEC filings (private) has no balance sheet to run: its issuer-level debt keeps a stated
assumption (the price model's 40c senior unsecured convention) and the output says why.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from marden.core.db import Conn, Row
from marden.models import pit
from marden.models.gpu_value import HAIRCUT as GPU_RESALE_HAIRCUT
from marden.models.sourced import fact_value, num, stored_outputs
from marden.readmodels.context import business_today

METHOD_KEY = "issuer_waterfall"
FLOOR, CAP = 0.05, 0.95
STRESS = 0.20  # the range: every non-cash asset 20% lower / higher
KEEP = {"cash": 1.0, "restricted": 1.0, "receivables": 0.80, "rou": 0.0, "goodwill": 0.0, "other": 0.25}
BUILDING_KEEP = 0.55  # buildings, power and works without a site model (price_chain BUILDING_RECOVERY)
FLEET_AGE_YEARS = 1.5  # a GPU fleet's age when no facility of the issuer is valued
GPU_LIFE_YEARS = 5.0
MIN_LIFE_LEFT = 0.1
LEASE_RATE = 0.07  # discount rate of the lease liabilities (rent = current portion + rate x liability)
LEASE_CAP_MIN_YEARS, LEASE_CAP_SHARE, LEASE_CAP_MAX_YEARS = 1.0, 0.15, 3.0
UNKNOWN_LEASE_YEARS = 10.0
PRIVATE_RECOVERY = 0.40  # the stated assumption for an issuer without filings (price_bonds.RECOVERY)

XBRL_FIELDS = ("total_assets_usd", "total_liabilities_usd", "liabilities_and_equity_usd", "equity_total_usd",
               "equity_usd", "cash_usd", "cash_and_restricted_usd", "restricted_cash_current_usd",
               "restricted_cash_noncurrent_usd", "receivables_usd", "ppe_net_usd", "ppe_and_finance_lease_rou_net_usd",
               "construction_in_progress_usd", "operating_lease_rou_asset_usd", "goodwill_usd", "intangibles_usd",
               "operating_lease_liability_usd", "operating_lease_liability_current_usd", "finance_lease_liability_usd",
               "lease_liability_usd", "total_debt_usd", "long_term_debt_usd", "short_term_borrowings_usd",
               "commercial_paper_usd", "secured_debt_usd", "unsecured_debt_usd", "convertible_debt_usd")
ENTITY_FIELDS = ("operator_role", "ppe_gpu_share", "gpu_useful_life_years", "leases_not_commenced_usd",
                 "leases_not_commenced_filing")
INSTRUMENT_FIELDS = ("amount_usd", "drawn_usd", "seniority", "status", "collateral_text", "ranking_text", "backstop",
                     "issue_date")
CLAIM_KINDS = ("bond", "convert", "term_loan", "facility", "ddtl", "construction_loan", "other", "abs", "cmbs")
_YEARS = re.compile(r"(\d+(?:\.\d+)?)\s*(?:to|-|\u2013)\s*(\d+(?:\.\d+)?)\s*years?", re.I)
_YEAR = re.compile(r"(\d+(?:\.\d+)?)\s*years?", re.I)
_GUARANTEED = re.compile(r"guarant", re.I)
_NOT_GUARANTEED = re.compile(r"\bno (?:parent )?guarantee|non-?recourse|without recourse", re.I)
_SPV_WORDS = ("spv", "project", "asset-level", "jv", "abs", "cmbs", "construction", "devco", "master issuer", "trust")


# ---------------------------------------------------------------- inputs


@dataclass
class Claim:
    instrument_id: str
    name: str
    kind: str
    rank: str  # 'spv' | 'parent' | 'unsecured' | 'junior'
    amount: float
    source: dict[str, Any]  # Sourced<number> of the amount
    issue_date: date | None = None
    pool: str | None = None  # collateral pool key (SPV debt)
    recourse: bool = False  # parent guarantee of an SPV's debt


@dataclass
class Collateral:
    key: str
    label: str
    value: float | None  # USD, after the collateral model's own haircuts
    works: float | None = None  # site works only (the building value, without unspent proceeds)
    book: float | None = None  # site cost spent (total project cost x share finished)


@dataclass
class GpuPool:
    units: float
    value_per_unit: float
    new_price: float
    age: float
    life: float

    @property
    def book(self) -> float:
        return self.units * self.new_price * max(1.0 - max(self.age, 0.0) / self.life, MIN_LIFE_LEFT)

    @property
    def value(self) -> float:
        return self.units * self.value_per_unit * (1.0 - GPU_RESALE_HAIRCUT)


@dataclass
class IssuerInputs:
    issuer: str
    name: str
    filer: str | None
    period_end: date | None = None
    values: dict[str, float] = field(default_factory=dict)  # XBRL field -> USD at the period end
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)  # XBRL field -> Sourced (F)
    filing: dict[str, Any] = field(default_factory=dict)  # {form, filed, url, sourceName}
    claims: list[Claim] = field(default_factory=list)
    collateral: dict[str, Collateral] = field(default_factory=dict)
    gpu_share: float = 0.0
    gpu_share_basis: str = ""
    gpu_share_source: dict[str, Any] | None = None
    gpu_keep: float = 0.0
    gpu_keep_basis: str = ""
    sites_value: float = 0.0  # the issuer's own sites: works value (site recovery model)
    sites_book: float = 0.0  # ... and their cost spent
    sites: list[str] = field(default_factory=list)
    unstarted_leases: float | None = None  # leases signed, not commenced: undiscounted commitment
    unstarted_years: float | None = None
    unstarted_source: dict[str, Any] | None = None
    missing: list[str] = field(default_factory=list)
    listed: bool = False
    incomplete: bool = False


def classify(seniority: Any, kind: str, issuer: str | None, top: set[str], sited: bool) -> str | None:
    """The claim's rank in the issuer waterfall, or None when it is not debt of the waterfall (leases come
    from the balance sheet, guarantees and preferred are not claims, BDC rows repeat loans)."""
    text = str(seniority or "").strip().lower()
    if kind == "lease" or not text:
        return None
    if any(w in text for w in ("guarantee", "preferred", "private credit", "tbd", "per filing", "lease")):
        return None
    if "subordinat" in text or "junior" in text:
        return "junior"
    if "unsecured" in text or "convertible" in text:
        return "unsecured"
    if "secured" in text or "mortgage" in text:
        if "(parent)" in text:
            return "parent"
        if sited or any(w in text for w in _SPV_WORDS) or (issuer is not None and issuer not in top):
            return "spv"
        return "parent"
    return None


def guaranteed(*texts: Any) -> bool:
    joined = " ".join(str(t) for t in texts if isinstance(t, str))
    return bool(_GUARANTEED.search(joined)) and not _NOT_GUARANTEED.search(joined)


def lease_cap(annual_rent: float, years: float) -> float:
    """11 U.S.C. 502(b)(6): the rent for the greater of one year and 15% of the remaining term, at most 3."""
    return annual_rent * max(LEASE_CAP_MIN_YEARS, min(LEASE_CAP_SHARE * years, LEASE_CAP_MAX_YEARS))


def annuity(years: float, rate: float = LEASE_RATE) -> float:
    return (1.0 - (1.0 + rate) ** -years) / rate


def commenced_lease(liability: float, current: float | None) -> tuple[float, float, float]:
    """(annual rent, remaining years, capped claim) of the commenced leases from the balance sheet: the rent
    is the current portion plus a year's interest at LEASE_RATE, the term what makes the liability its
    present value. Without a current portion (IFRS 16) the term is UNKNOWN_LEASE_YEARS."""
    if current is not None and current > 0:
        rent = current + LEASE_RATE * liability
        ratio = liability / rent
        top = annuity(80.0)
        years = 80.0 if ratio >= top else -math.log(1.0 - ratio * LEASE_RATE) / math.log(1.0 + LEASE_RATE)
    else:
        years = UNKNOWN_LEASE_YEARS
        rent = liability / annuity(years)
    return rent, years, min(liability, lease_cap(rent, years))


def term_years(text: Any) -> float | None:
    """'7 to 16 years' -> 11.5; '15 years' -> 15."""
    if not isinstance(text, str):
        return None
    got = _YEARS.search(text)
    if got:
        return (float(got[1]) + float(got[2])) / 2.0
    one = _YEAR.search(text)
    return float(one[1]) if one else None


def gpu_share_by_role(role: Any, kind: str | None) -> tuple[float, str]:
    """The GPU / server share of PP&E when the filer does not state it (judgment, tag M)."""
    text = str(role or "").lower()
    if "landlord" in text:
        return 0.0, "landlord: it leases powered shells, the GPUs are its tenants'"
    if "gpu owner" in text and "data center" in text:
        return 0.6, "owns both its GPUs and its data centers"
    if "gpu owner" in text:
        return 0.8, "GPU cloud in leased data centers: PP&E is mostly GPUs and servers"
    if kind == "cloud":
        return 0.5, "cloud provider: servers and network about half of PP&E, buildings and works the rest"
    return 0.3, "no stated split and no role on file"


# ---------------------------------------------------------------- the waterfall (pure)


def _usd(v: float | None) -> float:
    return 0.0 if v is None else float(v)


def _total_liabilities(vals: dict[str, float]) -> tuple[float | None, str]:
    if vals.get("total_liabilities_usd") is not None:
        return vals["total_liabilities_usd"], "filed"
    le = vals.get("liabilities_and_equity_usd") or vals.get("total_assets_usd")
    eq = vals.get("equity_total_usd") if vals.get("equity_total_usd") is not None else vals.get("equity_usd")
    if le is not None and eq is not None:
        return le - eq, "liabilities and equity less total equity"
    return None, "n/d"


def restricted_cash(vals: dict[str, float]) -> float:
    if vals.get("cash_and_restricted_usd") is not None and vals.get("cash_usd") is not None:
        return max(vals["cash_and_restricted_usd"] - vals["cash_usd"], 0.0)
    return _usd(vals.get("restricted_cash_current_usd")) + _usd(vals.get("restricted_cash_noncurrent_usd"))


def ppe_book(vals: dict[str, float]) -> float:
    got = vals.get("ppe_net_usd")
    return _usd(got if got is not None else vals.get("ppe_and_finance_lease_rou_net_usd"))


def _keep(base: float, stress: float) -> float:
    return min(1.0, base * stress)


def waterfall(inp: IssuerInputs, stress: float = 1.0) -> dict[str, Any]:
    """The waterfall of one issuer: asset lines, secured pools, unsecured claims and each rank's recovery
    (shares of par, unclamped; `stress` scales every non-cash asset's keep ratio, for the range)."""
    v = inp.values
    total = _usd(v.get("total_assets_usd"))
    cash, restricted, recv = _usd(v.get("cash_usd")), restricted_cash(v), _usd(v.get("receivables_usd"))
    ppe = ppe_book(v)
    rou, gw = _usd(v.get("operating_lease_rou_asset_usd")), _usd(v.get("goodwill_usd")) + _usd(v.get("intangibles_usd"))
    other = max(total - cash - restricted - recv - ppe - rou - gw, 0.0)
    gpu_book = ppe * inp.gpu_share
    rest_book = ppe - gpu_book
    gpu_value = gpu_book * _keep(inp.gpu_keep, stress)
    if inp.sites_book > 0 and rest_book > 0:
        scale = min(1.0, rest_book / inp.sites_book)
        sited = min(inp.sites_value, inp.sites_book) * scale
        rest_value = min(sited * stress, inp.sites_book * scale) + max(rest_book - inp.sites_book, 0.0) * _keep(
            BUILDING_KEEP, stress)
    else:
        rest_value = rest_book * _keep(BUILDING_KEEP, stress)
    ppe_value = gpu_value + rest_value
    blend = ppe_value / ppe if ppe > 0 else _keep(BUILDING_KEEP, stress)
    after = [c for c in inp.claims if c.issue_date is not None and inp.period_end is not None
             and c.issue_date > inp.period_end]
    raised = sum(c.amount for c in after)
    assets = [
        {"key": "cash", "label": "Cash and equivalents", "bookUsd": cash, "keep": KEEP["cash"], "valueUsd": cash},
        {"key": "restricted", "label": "Restricted cash", "bookUsd": restricted, "keep": KEEP["restricted"],
         "valueUsd": restricted},
        {"key": "receivables", "label": "Receivables", "bookUsd": recv, "keep": _keep(KEEP["receivables"], stress),
         "valueUsd": recv * _keep(KEEP["receivables"], stress)},
        {"key": "gpus", "label": "PP&E: GPUs and servers", "bookUsd": gpu_book, "keep": _keep(inp.gpu_keep, stress),
         "valueUsd": gpu_value},
        {"key": "buildings", "label": "PP&E: buildings, power and works", "bookUsd": rest_book,
         "keep": rest_value / rest_book if rest_book else None, "valueUsd": rest_value},
        {"key": "rou", "label": "Right-of-use assets", "bookUsd": rou, "keep": KEEP["rou"], "valueUsd": 0.0},
        {"key": "goodwill", "label": "Goodwill and intangibles", "bookUsd": gw, "keep": KEEP["goodwill"],
         "valueUsd": 0.0},
        {"key": "other", "label": "Other assets", "bookUsd": other, "keep": _keep(KEEP["other"], stress),
         "valueUsd": other * _keep(KEEP["other"], stress)},
    ]
    if raised > 0:
        assets.append({"key": "raised", "label": f"Debt raised after {inp.period_end}: proceeds, at the PP&E blend",
                       "bookUsd": raised, "keep": blend, "valueUsd": raised * blend})
    value = sum(a["valueUsd"] for a in assets)

    # secured at an SPV / opco: per collateral pool
    pools: dict[str, list[Claim]] = defaultdict(list)
    for c in inp.claims:
        if c.rank == "spv":
            pools[c.pool or f"none:{c.instrument_id}"].append(c)
    spv: list[dict[str, Any]] = []
    deficiency = 0.0
    for key, cs in sorted(pools.items()):
        claims = sum(c.amount for c in cs)
        col = inp.collateral.get(key)
        cval = None if col is None or col.value is None else col.value * (stress if stress < 1 else 1.0)
        covered = claims if cval is None else min(claims, cval)
        short = claims - covered
        recourse = any(c.recourse for c in cs)
        if recourse:
            deficiency += short
        spv.append({"pool": key, "label": col.label if col else cs[0].name, "claimsUsd": claims,
                    "collateralUsd": cval, "coveredUsd": covered, "shortfallUsd": short, "recourse": recourse,
                    "instruments": [c.instrument_id for c in cs]})
    spv_covered = sum(p["coveredUsd"] for p in spv)
    left = max(value - spv_covered, 0.0)

    # secured at the parent, then finance leases (the lessor keeps title)
    fin = _usd(v.get("finance_lease_liability_usd"))
    parent_claims = sum(c.amount for c in inp.claims if c.rank == "parent")
    parent_total = parent_claims + fin
    parent_covered = min(parent_total, left)
    parent_short = parent_total - parent_covered
    left -= parent_covered

    # unsecured, pro rata
    itemised = [c for c in inp.claims if c.rank in ("spv", "parent", "unsecured", "junior")]
    on_bs = sum(c.amount for c in itemised if c not in after)
    bs_debt = v.get("total_debt_usd")
    if bs_debt is None and v.get("long_term_debt_usd") is not None:
        bs_debt = _usd(v.get("long_term_debt_usd")) + _usd(v.get("short_term_borrowings_usd")) + _usd(
            v.get("commercial_paper_usd"))
    unitemised = max(_usd(bs_debt) - on_bs, 0.0) if bs_debt is not None else 0.0
    op_l = v.get("operating_lease_liability_usd")
    ifrs_l = v.get("lease_liability_usd")
    lease_l = op_l if op_l is not None else ifrs_l
    lease_claim, lease_rent, lease_years = 0.0, None, None
    if lease_l:
        lease_rent, lease_years, lease_claim = commenced_lease(
            lease_l, v.get("operating_lease_liability_current_usd") if op_l is not None else None)
    unstarted = 0.0
    if inp.unstarted_leases:
        yrs = inp.unstarted_years or UNKNOWN_LEASE_YEARS
        unstarted = lease_cap(inp.unstarted_leases / yrs, yrs)
    tl, tl_basis = _total_liabilities(v)
    other_liab = max(_usd(tl) - max(_usd(bs_debt), on_bs) - _usd(lease_l) - fin, 0.0) if tl is not None else 0.0
    notes = sum(c.amount for c in inp.claims if c.rank == "unsecured" and c.kind != "convert")
    converts = sum(c.amount for c in inp.claims if c.rank == "unsecured" and c.kind == "convert")
    unsecured = [
        {"key": "notes", "label": "Senior notes and unsecured loans", "short": "notes and loans", "claimsUsd": notes},
        {"key": "converts", "label": "Convertible notes", "short": "converts", "claimsUsd": converts},
        {"key": "unitemised", "label": "Balance-sheet debt not itemised (ranked unsecured)",
         "short": "other balance-sheet debt", "claimsUsd": unitemised},
        {"key": "leases", "label": "Lease rejection claims, commenced leases (capped)",
         "short": "lease rejection (capped)", "claimsUsd": lease_claim, "liabilityUsd": lease_l,
         "annualRentUsd": lease_rent, "remainingYears": lease_years},
        {"key": "unstarted", "label": "Lease rejection claims, signed not commenced (capped)",
         "short": "leases not yet commenced (capped)", "claimsUsd": unstarted,
         "commitmentUsd": inp.unstarted_leases, "termYears": inp.unstarted_years},
        {"key": "shortfall", "label": "Shortfalls of parent-guaranteed secured debt", "short": "guaranteed shortfalls",
         "claimsUsd": deficiency + parent_short},
        {"key": "other", "label": "Other liabilities (payables, accruals, deferred revenue)",
         "short": "other liabilities", "claimsUsd": other_liab, "totalLiabilitiesUsd": tl,
         "totalLiabilitiesBasis": tl_basis},
    ]
    u_claims = sum(x["claimsUsd"] for x in unsecured)
    r_u = left / u_claims if u_claims > 0 else None
    junior = sum(c.amount for c in inp.claims if c.rank == "junior")
    r_j = (max(left - u_claims, 0.0) / junior) if junior > 0 else None
    r_p = None
    if parent_claims > 0:
        r_p = (parent_covered + parent_short * (min(r_u, 1.0) if r_u is not None else 0.0)) / parent_total
    return {"assets": assets, "assetsUsd": value, "spv": spv, "spvCoveredUsd": spv_covered,
            "parent": {"claimsUsd": parent_claims, "financeLeasesUsd": fin, "coveredUsd": parent_covered,
                       "shortfallUsd": parent_short},
            "securedUsd": spv_covered + parent_covered, "leftUsd": left, "unsecured": unsecured,
            "unsecuredClaimsUsd": u_claims, "juniorClaimsUsd": junior,
            "rates": {"unsecured": r_u, "parent": r_p, "junior": r_j}, "ppeBlend": blend}


def clamp(x: float | None) -> float | None:
    return None if x is None else min(max(x, FLOOR), CAP)


# ---------------------------------------------------------------- display


def money(usd: float | None) -> str:
    if usd is None:
        return "n/d"
    b = usd / 1e9
    if abs(b) >= 10:
        return f"${b:,.1f}B"
    if abs(b) >= 1:
        return f"${b:,.2f}B"
    return f"${usd / 1e6:,.0f}mm"


def _pct(x: float | None) -> str:
    return "n/d" if x is None else f"{round(x * 100)}%"


def summary_lines(inp: IssuerInputs, w: dict[str, Any], rank: str, cents: int | None) -> list[dict[str, Any]]:
    """The short waterfall the instrument page's recovery hover shows: assets after haircuts, less the secured
    claims, left for unsecured, the recovery (each with a plain note; tag M, the filing behind it F)."""
    by = {a["key"]: a for a in w["assets"]}
    parts = [f"cash {_pct(by['cash']['keep'])}"]
    if by["restricted"]["bookUsd"]:
        parts.append(f"restricted cash {_pct(by['restricted']['keep'])}")
    if by["receivables"]["bookUsd"]:
        parts.append(f"receivables {_pct(by['receivables']['keep'])}")
    if by["gpus"]["bookUsd"]:
        parts.append(f"GPUs {_pct(by['gpus']['keep'])} of book ({money(by['gpus']['bookUsd'])})")
    if by["buildings"]["bookUsd"]:
        parts.append(f"buildings and works {_pct(by['buildings']['keep'])} ({money(by['buildings']['bookUsd'])})")
    if by["rou"]["bookUsd"] or by["goodwill"]["bookUsd"]:
        parts.append("right-of-use, goodwill and intangibles 0%")
    if by["other"]["bookUsd"]:
        parts.append(f"other {_pct(by['other']['keep'])}")
    if "raised" in by:
        parts.append(f"{money(by['raised']['bookUsd'])} raised since at {_pct(by['raised']['keep'])}")
    total = _usd(inp.values.get("total_assets_usd"))
    spv = [p for p in w["spv"] if p["coveredUsd"] > 0]
    sec_parts = []
    if spv:
        sec_parts.append(f"{money(w['spvCoveredUsd'])} SPV and site debt on its own collateral, ahead of the "
                         "holding company")
    if w["parent"]["coveredUsd"]:
        sec_parts.append(f"{money(w['parent']['coveredUsd'])} parent secured debt and finance leases")
    claim_parts = [f"{x['short']} {money(x['claimsUsd'])}" for x in w["unsecured"] if x["claimsUsd"] > 0]
    lines = [
        {"label": "Assets after haircuts", "usd": w["assetsUsd"], "op": None,
         "note": f"of {money(total)} total assets: " + ", ".join(parts)},
        {"label": "Less secured claims", "usd": w["securedUsd"], "op": "minus",
         "note": "; ".join(sec_parts) or "no secured debt"},
        {"label": "Left for unsecured", "usd": w["leftUsd"], "op": "equals",
         "note": f"over {money(w['unsecuredClaimsUsd'])} of unsecured claims: " + ", ".join(claim_parts)},
    ]
    if rank != "unsecured":
        label = {"parent": "parent secured", "junior": "subordinated"}[rank]
        lines.append({"label": f"Recovery ({label})", "cents": cents, "op": "equals", "note": None})
    else:
        lines.append({"label": "Recovery", "cents": cents, "op": "equals",
                      "note": (f"{money(w['leftUsd'])} / {money(w['unsecuredClaimsUsd'])}, "
                               f"held within {round(FLOOR * 100)}{chr(8211)}{round(CAP * 100)}c")})
    return lines


# ---------------------------------------------------------------- loading


@dataclass
class Group:
    issuer: str  # the top of the parent chain
    name: str
    filer: str | None
    members: set[str] = field(default_factory=set)
    instruments: list[Row] = field(default_factory=list)
    listed: bool = False  # a member has a ticker or a CIK: an SEC filer whose balance sheet is not collected


def _top(ents: dict[str, Row], eid: str | None) -> str | None:
    seen: list[str] = []
    while eid and eid in ents and eid not in seen and len(seen) < 8:
        seen.append(eid)
        parent = ents[eid]["parent_id"]
        eid = parent if parent and parent != eid else None
    return seen[-1] if seen else eid


def groups(conn: Conn, day: date) -> tuple[dict[str, Group], dict[str, str]]:
    """Issuer groups (by top entity) and instrument -> group key, with each group's filer: the top entity, else
    the entity sharing its ticker, with a balance sheet public on the day."""
    ents = {r["id"]: r for r in conn.execute("SELECT id, name, parent_id, ticker, cik, kind FROM entity").fetchall()}
    filed = {r["subject_id"] for r in pit.fact_rows(conn, day, "entity", None, ["total_assets_usd"])}
    by_ticker: dict[str, list[str]] = defaultdict(list)
    for e in ents.values():
        if e["ticker"]:
            by_ticker[str(e["ticker"]).upper()].append(e["id"])
    out: dict[str, Group] = {}
    of: dict[str, str] = {}
    for r in conn.execute("SELECT id, name, kind, issuer_entity_id, site_slug, in_v1 FROM instrument "
                          "WHERE issuer_entity_id IS NOT NULL ORDER BY id").fetchall():
        top = _top(ents, r["issuer_entity_id"]) or r["issuer_entity_id"]
        if top not in out:
            filer = top if top in filed else None
            ticker = str(ents[top]["ticker"] or "").upper() if top in ents else ""
            if filer is None and ticker:
                filer = next((e for e in sorted(by_ticker[ticker]) if e in filed), None)
            out[top] = Group(top, ents[top]["name"] if top in ents else top, filer)
        g = out[top]
        g.members.add(r["issuer_entity_id"])
        g.instruments.append(r)
        of[r["id"]] = top
    for g in out.values():
        g.members.add(g.issuer)
        if g.filer:
            g.members.add(g.filer)
        g.listed = any(ents[m]["ticker"] or ents[m]["cik"] for m in g.members if m in ents)
    return out, of


def _rows_by(rows: list[Row]) -> dict[tuple[str, str], list[Row]]:
    by: dict[tuple[str, str], list[Row]] = defaultdict(list)
    for r in rows:
        by[(r["subject_id"], r["field"])].append(r)
    return by


def _balance(conn: Conn, day: date, filer: str, inp: IssuerInputs) -> None:
    rows = pit.fact_rows(conn, day, "entity", [filer], list(XBRL_FIELDS))
    rows = [r for r in rows if (r.get("producer") or "").startswith("collector:sec_xbrl")]
    ends = [r["valid_from"] for r in rows if r["field"] == "total_assets_usd" and num(r["value"]) is not None]
    if not ends:
        inp.missing.append(f"entity {filer}: no balance sheet (XBRL total assets) public by {day}")
        return
    end = max(ends)
    inp.period_end = end
    for (_, fld), rs in _rows_by([r for r in rows if r["valid_from"] == end]).items():
        best = pit.current_of(rs)
        if best is not None and num(best["value"]) is not None:
            inp.values[fld] = float(best["value"])
            inp.sources[fld] = fact_value(best, float(best["value"]))
    head = pit.current_of([r for r in rows if r["valid_from"] == end and r["field"] == "total_assets_usd"])
    if head is not None:
        loc = str(head.get("quote_loc") or "")
        filed = re.search(r"filed (\d{4}-\d{2}-\d{2})", loc)
        form = re.search(r";\s*(\S+)\s+\d{10}-", loc)
        inp.filing = {"form": form[1] if form else None, "filed": filed[1] if filed else None,
                      "periodEnd": end.isoformat(), "sourceUrl": head.get("source_url"),
                      "sourceName": head.get("source_name")}


def _entity_facts(conn: Conn, ids: list[str]) -> dict[tuple[str, str], Row]:
    with pit.unaudited():
        rows = pit.fact_rows(conn, business_today(), "entity", ids, list(ENTITY_FIELDS))
    out: dict[tuple[str, str], Row] = {}
    for key, rs in _rows_by(rows).items():
        best = pit.current_of(rs)
        if best is not None:
            out[key] = best
    return out


def _instrument_facts(conn: Conn, ids: list[str]) -> dict[str, dict[str, Row]]:
    """Terms and amounts with today's knowledge (the archive dates them its load day; price_terms)."""
    with pit.unaudited():
        rows = pit.fact_rows(conn, business_today(), "instrument", ids, list(INSTRUMENT_FIELDS))
    out: dict[str, dict[str, Row]] = defaultdict(dict)
    for (iid, fld), rs in _rows_by(rows).items():
        usable = [r for r in rs if str(r["tag"]).strip() in ("F", "R")] or rs
        best = pit.current_of(usable)
        if best is not None:
            out[iid][fld] = best
    return out


def _iso(v: Any) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _gpu_keep(conn: Conn, day: date, group_ids: list[str], life: float | None) -> tuple[float, str]:
    """GPU resale value over depreciated cost: the issuer's own valued pools, else the market by age."""
    models = stored_outputs(conn, "gpu_model", "gpu_model", None, day)
    new_price = {m: num((r["output"].get("newPrice") or {}).get("value")) for m, r in models.items()
                 if isinstance(r["output"], dict)}
    pools: list[GpuPool] = []
    for r in stored_outputs(conn, "gpu_collateral", "instrument", group_ids, day).values():
        o = r["output"] if isinstance(r["output"], dict) else {}
        b = o.get("basis") or {}
        units = num((o.get("units") or {}).get("value"))
        per, age, lf = num(b.get("valuePerUnitUsd")), num(b.get("unitAgeYears")), num(b.get("usefulLifeYears"))
        new = new_price.get(str(b.get("valuedModel") or ""))
        if units and per and new and age is not None and lf:
            pools.append(GpuPool(units, per, new, age, lf))
    if pools:
        book = sum(p.book for p in pools)
        ratio = min(sum(p.value for p in pools) / book, 1.0) if book else 0.0
        return ratio, (f"resale value over depreciated cost of its {len(pools)} valued GPU pool(s) (gpu_collateral: "
                       f"value per GPU less the {GPU_RESALE_HAIRCUT:.0%} resale haircut)")
    life = life or GPU_LIFE_YEARS
    ratios = []
    for m, r in models.items():
        o = r["output"] if isinstance(r["output"], dict) else {}
        pts = [(num(p.get("age")), num((p.get("usd") or {}).get("value"))) for p in o.get("valueByAgeYears") or []
               if isinstance(p, dict)]
        pts = [(a, u) for a, u in pts if a is not None and u is not None]
        new = new_price.get(m)
        if len(pts) < 2 or not new:
            continue
        pts.sort()
        lo = max((p for p in pts if p[0] <= FLEET_AGE_YEARS), default=pts[0])
        hi = min((p for p in pts if p[0] >= FLEET_AGE_YEARS), default=pts[-1])
        u = lo[1] if hi[0] == lo[0] else lo[1] + (hi[1] - lo[1]) * (FLEET_AGE_YEARS - lo[0]) / (hi[0] - lo[0])
        book = new * max(1.0 - FLEET_AGE_YEARS / life, MIN_LIFE_LEFT)
        ratios.append(min(u * (1.0 - GPU_RESALE_HAIRCUT) / book, 1.0))
    if not ratios:
        return 0.5, "no GPU market model on file: half of book (judgment)"
    ratios.sort()
    mid = ratios[len(ratios) // 2] if len(ratios) % 2 else (ratios[len(ratios) // 2 - 1] + ratios[len(ratios) // 2]) / 2
    return mid, (f"median over {len(ratios)} GPU models of the resale value of a {FLEET_AGE_YEARS:g}-year-old GPU less "
                 f"the {GPU_RESALE_HAIRCUT:.0%} haircut, over its cost depreciated on a {life:g}-year life")


def load(conn: Conn, day: date, g: Group, collateral: dict[str, dict[str, Any]],
         sites: dict[str, Any] | None = None) -> IssuerInputs:
    """One issuer's waterfall inputs. `collateral`: this recovery run's SPV collateral by instrument
    ({pool, label, valueUsd, worksUsd, bookUsd, site})."""
    inp = IssuerInputs(g.issuer, g.name, g.filer, listed=g.listed)
    if g.filer is None:
        return inp
    _balance(conn, day, g.filer, inp)
    gaps = [label for label, ok in (
        ("total assets", inp.values.get("total_assets_usd") is not None),
        ("cash", inp.values.get("cash_usd") is not None),
        ("PP&E net", bool(ppe_book(inp.values))),
        ("total liabilities", _total_liabilities(inp.values)[0] is not None)) if not ok]
    if inp.period_end is not None and gaps:
        inp.incomplete = True
        form = inp.filing.get("form") or "filing"
        inp.missing.append(f"entity {g.filer}: {', '.join(gaps)} not tagged in the {form} for {inp.period_end} "
                           "(XBRL), so the waterfall is not run")
    efacts = _entity_facts(conn, sorted({g.issuer, g.filer}))

    def ef(fld: str) -> Row | None:
        return efacts.get((g.filer or "", fld)) or efacts.get((g.issuer, fld))

    ids = [r["id"] for r in g.instruments]
    facts = _instrument_facts(conn, ids)
    top = {g.issuer, g.filer}
    for r in g.instruments:
        f = facts.get(r["id"], {})
        if r["kind"] not in CLAIM_KINDS:
            continue
        status = f.get("status")
        if status is not None and str(status["value"]).strip().lower() not in ("outstanding", ""):
            continue
        issued = _iso(f["issue_date"]["value"]) if "issue_date" in f else None
        if issued is not None and issued > day:
            continue
        sen = f.get("seniority")
        gpu = r["id"] in collateral and collateral[r["id"]].get("kind") == "gpus"
        rank = classify(sen["value"] if sen else None, r["kind"], r["issuer_entity_id"], top,
                        bool(r["site_slug"]) or gpu)
        if rank is None:
            continue
        amt_row = f.get("drawn_usd") if f.get("drawn_usd") is not None and num(f["drawn_usd"]["value"]) else f.get(
            "amount_usd")
        amount = num(amt_row["value"]) if amt_row is not None else None
        if not amount or amount <= 0:
            continue
        col = collateral.get(r["id"])
        pool = col["pool"] if col else (f"site:{r['site_slug']}" if r["site_slug"] else None)
        texts = [f[k]["value"] for k in ("collateral_text", "ranking_text", "backstop") if k in f]
        inp.claims.append(Claim(r["id"], r["name"], r["kind"], rank, float(amount), fact_value(amt_row, float(amount)),
                                issued, pool if rank == "spv" else None, rank == "spv" and guaranteed(*texts)))
        if col and rank == "spv" and col["pool"] not in inp.collateral:
            inp.collateral[col["pool"]] = Collateral(col["pool"], col["label"], col.get("valueUsd"),
                                                     col.get("worksUsd"), col.get("bookUsd"))
    for c in inp.collateral.values():
        if c.key.startswith("site:") and c.works is not None and c.book:
            inp.sites.append(c.key.removeprefix("site:"))
            inp.sites_value += c.works
            inp.sites_book += c.book
    # PP&E split: stated GPU share, else by role
    stated = ef("ppe_gpu_share")
    share = num(stated["value"]) if stated is not None else None
    if share is not None:
        inp.gpu_share = share / 100.0 if share > 1 else share
        inp.gpu_share_basis, inp.gpu_share_source = "stated", fact_value(stated, inp.gpu_share)
    else:
        role = ef("operator_role")
        kind = conn.execute("SELECT kind FROM entity WHERE id = %s", (g.issuer,)).fetchone()
        inp.gpu_share, inp.gpu_share_basis = gpu_share_by_role(role["value"] if role else None,
                                                               kind["kind"] if kind else None)
    life_row = ef("gpu_useful_life_years")
    inp.gpu_keep, inp.gpu_keep_basis = _gpu_keep(conn, day, ids, num(life_row["value"]) if life_row else None)
    unstarted, unstarted_terms = ef("leases_not_commenced_usd"), ef("leases_not_commenced_filing")
    if unstarted is not None and num(unstarted["value"]):
        inp.unstarted_leases = float(unstarted["value"])
        terms = unstarted_terms["value"].get("terms") if unstarted_terms is not None and isinstance(
            unstarted_terms["value"], dict) else None
        inp.unstarted_years = term_years(terms)
        inp.unstarted_source = fact_value(unstarted, inp.unstarted_leases)
    return inp


# ---------------------------------------------------------------- outputs


def issuer_doc(inp: IssuerInputs, day: date, method: str) -> dict[str, Any]:
    """The entity's output: the waterfall at base, low and high haircuts, every input with its source."""
    keeps = f"its issuer-level debt keeps the {PRIVATE_RECOVERY:.0%} senior unsecured convention"
    if inp.filer is None:
        why = (f"{inp.name} files with the SEC but Marden holds no XBRL balance sheet for it (no CIK linked for the "
               f"sec_xbrl collector), so the waterfall cannot run; {keeps}" if inp.listed else
               f"{inp.name} has no SEC filings on file (a private company, or a filer not linked to one), so "
               f"there is no balance sheet to run the waterfall; {keeps}")
        return {"issuer": inp.issuer, "name": inp.name, "filer": None, "method": None, "private": not inp.listed,
                "unsecuredCents": None, "assumedCents": round(PRIVATE_RECOVERY * 100), "missing": [why],
                "methodVersion": method}
    if inp.period_end is None or inp.incomplete:
        return {"issuer": inp.issuer, "name": inp.name, "filer": inp.filer, "method": None, "private": False,
                "unsecuredCents": None, "assumedCents": round(PRIVATE_RECOVERY * 100),
                "missing": [*inp.missing, keeps], "methodVersion": method}
    w = waterfall(inp)
    lo, hi = waterfall(inp, 1.0 - STRESS), waterfall(inp, 1.0 + STRESS)

    def cents(x: dict[str, Any], rank: str) -> int | None:
        got = clamp(x["rates"][rank])
        return None if got is None else round(got * 100)

    ranks = {k: {"cents": cents(w, k), "rangeCents": [cents(lo, k), cents(hi, k)]}
             for k in ("unsecured", "parent", "junior") if w["rates"][k] is not None}
    inputs = {k: inp.sources[k] for k in XBRL_FIELDS if k in inp.sources}
    return {
        "issuer": inp.issuer, "name": inp.name, "filer": inp.filer, "method": METHOD_KEY, "methodVersion": method,
        "filing": inp.filing, "periodEnd": inp.period_end.isoformat(), "inputs": inputs,
        "assets": w["assets"], "assetsUsd": w["assetsUsd"], "spv": w["spv"], "parent": w["parent"],
        "securedUsd": w["securedUsd"], "leftUsd": w["leftUsd"], "unsecured": w["unsecured"],
        "unsecuredClaimsUsd": w["unsecuredClaimsUsd"], "juniorClaimsUsd": w["juniorClaimsUsd"],
        "ranks": ranks, "unsecuredCents": ranks.get("unsecured", {}).get("cents"),
        "rawUnsecured": w["rates"]["unsecured"],
        "claims": [{"instrumentId": c.instrument_id, "name": c.name, "rank": c.rank, "amountUsd": c.amount,
                    "source": c.source, "pool": c.pool, "parentGuarantee": c.recourse,
                    "issueDate": c.issue_date.isoformat() if c.issue_date else None} for c in inp.claims],
        "haircuts": {"cash": KEEP["cash"], "restricted": KEEP["restricted"], "receivables": KEEP["receivables"],
                     "gpus": round(inp.gpu_keep, 4), "gpusBasis": inp.gpu_keep_basis,
                     "gpuShareOfPpe": round(inp.gpu_share, 4), "gpuShareBasis": inp.gpu_share_basis,
                     **({"gpuShareSource": inp.gpu_share_source} if inp.gpu_share_source else {}),
                     "buildingsDefault": BUILDING_KEEP, "sites": inp.sites, "sitesWorksUsd": inp.sites_value,
                     "sitesCostUsd": inp.sites_book, "rightOfUse": KEEP["rou"], "goodwill": KEEP["goodwill"],
                     "other": KEEP["other"], "leaseRate": LEASE_RATE, "rangeStress": STRESS,
                     **({"unstartedLeasesSource": inp.unstarted_source} if inp.unstarted_source else {})},
        "rule": ("assets after haircuts, less SPV / site debt on its own collateral (structurally ahead), less parent "
                 "secured debt and finance leases, shared pro rata by the unsecured claims (lease rejection capped "
                 "under 11 U.S.C. 502(b)(6)); clamped to 5-95c"),
        "lines": {k: summary_lines(inp, w, k, ranks[k]["cents"]) for k in ranks},
        "missing": inp.missing,
    }


def ranked_instruments(inp: IssuerInputs) -> dict[str, str]:
    """Issuer-level instruments (not SPV debt) and their rank."""
    return {c.instrument_id: c.rank for c in inp.claims if c.rank in ("unsecured", "parent", "junior")}


def issuer_level(conn: Conn, ids: list[str] | None = None) -> dict[str, str]:
    """Instruments the issuer waterfall gives the recovery of, and their rank: unsited, not GPU-backed, and
    ranked at the issuer (unsecured, parent secured or subordinated) by their seniority fact."""
    rows = conn.execute(
        """SELECT i.id, i.kind, i.issuer_entity_id, f.value AS seniority FROM instrument i
             JOIN fact f ON f.subject_type = 'instrument' AND f.subject_id = i.id AND f.field = 'seniority'
                        AND f.status = 'published'
            WHERE i.site_slug IS NULL AND i.issuer_entity_id IS NOT NULL
              AND (%(ids)s::text[] IS NULL OR i.id = ANY(%(ids)s))
              AND i.id NOT IN (SELECT subject_id FROM fact WHERE subject_type = 'instrument'
                                 AND field IN ('gpu_model', 'gpu_collateral'))""", {"ids": ids}).fetchall()
    ents = {r["id"]: r["parent_id"] for r in conn.execute("SELECT id, parent_id FROM entity").fetchall()}
    out: dict[str, str] = {}
    for r in rows:
        if r["kind"] not in CLAIM_KINDS:
            continue
        top, steps = r["issuer_entity_id"], 0
        while ents.get(top) and ents[top] != top and steps < 8:
            top, steps = ents[top], steps + 1
        rank = classify(r["seniority"], r["kind"], r["issuer_entity_id"], {top}, False)
        if rank in ("unsecured", "parent", "junior"):
            out[r["id"]] = rank
    return dict(sorted(out.items()))


def group_instruments(conn: Conn, instrument_id: str) -> list[str]:
    """The issuer-level instruments of the instrument's issuer group (a claim changes every share)."""
    ents = {r["id"]: r["parent_id"] for r in conn.execute("SELECT id, parent_id FROM entity").fetchall()}

    def top(e: str | None) -> str | None:
        seen = 0
        while e and ents.get(e) and ents[e] != e and seen < 8:
            e, seen = ents[e], seen + 1
        return e

    row = conn.execute("SELECT issuer_entity_id FROM instrument WHERE id = %s", (instrument_id,)).fetchone()
    mine = top(row["issuer_entity_id"]) if row else None
    if mine is None:
        return [instrument_id]
    same = [r["id"] for r in conn.execute("SELECT id, issuer_entity_id FROM instrument").fetchall()
            if top(r["issuer_entity_id"]) == mine]
    return sorted(set(issuer_level(conn, same)) | {instrument_id})
