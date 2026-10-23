"""The default intensity of a facility, built up along its payment chain (pricing-v0.1 "Facility
adjustments"; the Jupiter backtest's locked assumptions are the v0.1 parameters for every facility):

    lambda(t) = pass_T(t) lambda_tenant + pass_O lambda_obligor
              + 1[t < completion] (BASE_COMPLETION x 1.75^denials (cap 6x) + 0.15 x P(force majeure delay))

- tenant: the site's chain party with role 'tenant' (`chain_party`); its bond-implied intensity
  (price_issuer, flat at its CDS-maturity level). pass_T = 1 - PROTECTION_PRE_RENT before rent starts
  (lease protection 0.5, locked) and 1 - PROTECTION_POST_RENT after (0.5 too in v0.1: the methodology
  gives no post-rent value, and the traded project bonds of CoreWeave's sites price far tighter than
  full pass-through of CoreWeave's own intensity).
- obligor: the issuer, or the nearest parent with a bond-implied intensity, when it is not the tenant
  (an SPV of a CDS issuer: the CoreWeave DDTLs). pass_O = 1 - CONTRACT_PROTECTION when customer
  contracts are named on the instrument, else 1.
- construction: rent start / completion is the site's cushion projection (model 'cushion' P50, the
  month's last day) when it is after the pricing day; signals from price_signals.
- backstop: a guarantor named in the tenant's label ('Fluidstack (Google)') or the `backstop` fact; the
  facility then defaults only jointly with the guarantor, Gaussian copula with COPULA_RHO.
- recovery (price v0.2): the `recovery` model's central estimate for the instrument (its collateral
  waterfall: model_output 'recovery', the latest on or before the pricing day and at most
  RECOVERY_MAX_AGE_DAYS older), `recoveryBasis` 'site_waterfall':
  - powered shell: the base case (50th-percentile comp), after the claims ranking ahead of the
    instrument and shared with the claims at its rank (the waterfall applies the seniority facts), flat
    over the life;
  - GPU collateral: the recovery at each scheduled balance over the loan's life (`pathCents`: the GPUs
    age while the loan amortizes), interpolated in time and held flat after the last one; this replaces
    the half-life decay, which the waterfall already covers.
  - issuer waterfall (price v0.3): an issuer-level instrument priced as a facility takes its rank's recovery in
    the issuer's balance-sheet waterfall (recovery v0.3, marden.models.recovery_issuer), `recoveryBasis`
    'issuer_waterfall'.
  Issuer bonds (no chain) price at the issuer's unsecured recovery from the same waterfall (`issuer_recovery`:
  the entity's 'recovery' output), and the issuer's bond-implied curve is solved from its bond prices at that
  same recovery (price_issuer.build), so curve and prices stay consistent; an issuer without a balance sheet
  (no SEC filings, or not collected) keeps 40%, the bond-implied CDS convention, and says why.
  Only when there is no such estimate (no output, an n/d waterfall, or a waterfall of other collateral
  than the price chain's) the v0.1 defaults apply, `recoveryBasis` 'default_assumption': buildings flat
  at BUILDING_RECOVERY; GPU collateral (DDTLs, GPU leases, a gpu_model fact) decays from GPU_RECOVERY on
  the chip's half-life, fitted to the stored residual-value bands (series gpu_residual:<chip>,
  'moderate'). The recovery is the pricing day's estimate, applied to the calibration at issue as well
  (as the GPU half-life is), so the liquidity premium is solved with it.
A leg whose input is missing is left out and named in `missing`.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
from scipy.stats import multivariate_normal, norm

from marden.core.db import Conn
from marden.models import pit, runs
from marden.models.price_curve import year_frac
from marden.models.price_terms import Instrument
from marden.models.sourced import num

BASE_COMPLETION = 0.015
PROTECTION_PRE_RENT = 0.5  # locked (Jupiter backtest)
PROTECTION_POST_RENT = 0.5  # v0.1 assumption: the methodology gives no post-rent value
CONTRACT_PROTECTION = 0.5
BUILDING_RECOVERY = 0.55
GPU_RECOVERY = 0.55
COPULA_RHO = 0.3
RECOVERY_MAX_AGE_DAYS = 14  # the recovery model runs weekly (Mondays) and within a minute of its inputs
SITE_WATERFALL, DEFAULT_ASSUMPTION = "site_waterfall", "default_assumption"
ISSUER_WATERFALL = "issuer_waterfall"
ISSUER_LABEL = "issuer balance-sheet waterfall"
RECOVERY_SOURCE = "Marden recovery model"
WATERFALL_COLLATERAL = {"powered_shell": "buildings", "gpu_collateral": "gpus"}
LIEN = {1: "first lien (senior secured)", 2: "junior / subordinated", 3: "unsecured"}
GPU_KINDS = ("ddtl", "lease")
CHIP_FAMILY = {"GB200": "B200", "GB300": "B300", "H100": "H100", "H200": "H200", "B200": "B200", "B300": "B300"}
_PAREN = re.compile(r"\(([^()]+)\)")


@dataclass(frozen=True)
class SiteRecovery:
    """The recovery model's central estimate for one instrument (its collateral waterfall)."""

    method: str  # 'powered_shell' | 'gpu_collateral' | 'issuer_waterfall'
    base: float  # share of par, a default today
    as_of: date
    version: str
    source: dict[str, Any]  # the output's Sourced recovery (tag, asOf, sourceName, methodVersion)
    low: float | None = None
    high: float | None = None
    path: tuple[tuple[float, float], ...] = ()  # GPU collateral: (years from the as-of day, share of par)
    inputs: dict[str, Any] = field(default_factory=dict)


def _range(text: Any) -> tuple[float, float] | None:
    got = re.fullmatch(r"(\d+)(?:\u2013(\d+))?", text) if isinstance(text, str) else None
    return (int(got[1]) / 100, int(got[2] or got[1]) / 100) if got else None


def site_recovery(conn: Conn, instrument_id: str, as_of: date) -> tuple[SiteRecovery | None, str | None]:
    """The instrument's waterfall recovery current on `as_of` (point in time: the latest output on or before
    the day, at most RECOVERY_MAX_AGE_DAYS old), else None and why, in plain words."""
    row = runs.latest_output(conn, "recovery", "instrument", instrument_id, as_of)
    if row is None or not isinstance(row["output"], dict):
        return None, f"the recovery model has no output for this instrument on or before {as_of}"
    if (as_of - row["as_of"]).days > RECOVERY_MAX_AGE_DAYS:
        return None, (f"the recovery model's latest output ({row['as_of']}) is more than {RECOVERY_MAX_AGE_DAYS} "
                      "days old")
    out = row["output"]
    method = out.get("method")
    basis: dict[str, Any] = out["basis"] if isinstance(out.get("basis"), dict) else {}
    missing = [str(m) for m in out.get("missing") or []]
    if method not in WATERFALL_COLLATERAL and method != ISSUER_WATERFALL:
        return None, "; ".join(missing) or "no collateral waterfall for this instrument"
    rec: dict[str, Any] = out["recovery"] if isinstance(out.get("recovery"), dict) else {}
    span = basis.get("rangeCents")
    lo_hi = ((float(span[0]) / 100, float(span[1]) / 100) if isinstance(span, list) and len(span) == 2
             else _range(rec.get("value")))
    base = num(basis.get("baseCents"))
    central = base / 100 if base is not None else (sum(lo_hi) / 2 if lo_hi else None)
    if central is None:
        return None, "the waterfall gives no number: " + ("; ".join(missing) or "n/d")
    path = tuple((float(p["years"]), float(p["cents"]) / 100) for p in basis.get("pathCents") or []
                 if isinstance(p, dict) and num(p.get("years")) is not None and num(p.get("cents")) is not None)
    claims = [c for c in basis.get("claims") or [] if isinstance(c, dict)]
    rank = next((c.get("rank") for c in claims if c.get("instrumentId") == instrument_id), None)
    value: dict[str, Any] = out["valueB"] if isinstance(out.get("valueB"), dict) else {}
    inputs: dict[str, Any] = {"valueB": value.get("value")}
    for k in ("seniorClaimsB", "pariClaimsB", "compMedianUsdPerMw", "compQuartilesUsdPerMw", "grossMw",
              "grossMwBasis", "completion", "completionBasis", "units", "valuePerUnitUsd", "haircut",
              "poolBalanceB", "pool", "valuedModel", "site", "completionEstimated", "completionEstimators",
              "energizedShare", "unfinishedHaircut", "worksUsd", "unspentProceedsUsd", "notInV0"):
        if basis.get(k) is not None:
            inputs[k] = basis[k]
    inputs["comps"] = len(basis.get("comps") or []) or None
    if method == ISSUER_WATERFALL:
        for k in ("issuer", "issuerName", "rank", "filing", "periodEnd", "assetsUsd", "securedUsd", "leftUsd",
                  "unsecuredClaimsUsd", "lines"):
            if basis.get(k) is not None:
                inputs[k] = basis[k]
    inputs["lien"] = (LIEN.get(rank) if method == "powered_shell" and isinstance(rank, int) else None
                      if method == "powered_shell" else
                      {"unsecured": LIEN[3], "parent": "secured at the parent", "junior": LIEN[2]}.get(
                          str(basis.get("rank"))) if method == ISSUER_WATERFALL else
                      "first lien on the GPUs (no claims known ahead)")
    inputs = {k: v for k, v in inputs.items() if v is not None}
    if base is None:
        inputs["central"] = "midpoint of the range"
    source = {k: rec[k] for k in ("tag", "asOf", "sourceName", "sourceUrl", "methodVersion") if rec.get(k)}
    source.setdefault("sourceName", RECOVERY_SOURCE)
    source.setdefault("asOf", row["as_of"].isoformat())
    return SiteRecovery(str(method), central, row["as_of"], row["version"], source,
                        low=lo_hi[0] if lo_hi else None, high=lo_hi[1] if lo_hi else None,
                        path=path, inputs=inputs), None


@dataclass
class Chain:
    instrument_id: str
    tenant: str | None = None
    tenant_label: str | None = None
    obligor: str | None = None
    obligor_pass: float = 1.0
    guarantor: str | None = None
    collateral: str = "buildings"
    chip: str | None = None
    half_life: float | None = None
    completion: date | None = None
    signals_site: str | None = None
    recovery: SiteRecovery | None = None
    recovery_why: str | None = None  # why the default recovery applies (no site waterfall)
    missing: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def doc(self) -> dict[str, Any]:
        return {"tenant": self.tenant, "tenantLabel": self.tenant_label, "obligor": self.obligor,
                "obligorPassThrough": self.obligor_pass, "guarantor": self.guarantor, "collateral": self.collateral,
                "chip": self.chip, "gpuHalfLifeYears": None if self.half_life is None else round(self.half_life, 2),
                "completion": None if self.completion is None else self.completion.isoformat(), "notes": self.notes}


def gpu_half_lives(conn: Conn, as_of: date) -> dict[str, float]:
    """Half-life (years) per chip from the residual bands: log-linear fit of the 'moderate' values."""
    out: dict[str, float] = {}
    for r in pit.fact_rows(conn, as_of, "series", None, ["residual_pct_of_new"]):
        if not r["subject_id"].startswith("gpu_residual:") or not isinstance(r["value"], dict):
            continue
        pts = [(int(y), float(v["moderate"])) for y, v in r["value"].items()
               if isinstance(v, dict) and isinstance(v.get("moderate"), int | float) and v["moderate"] > 0]
        if len(pts) < 2:
            continue
        slope = float(np.polyfit([p[0] for p in pts], [math.log(p[1]) for p in pts], 1)[0])
        if slope < 0:
            out.setdefault(r["subject_id"].split(":", 1)[1], -math.log(2.0) / slope)
    return out


def _entity_by_name(conn: Conn, name: str) -> str | None:
    row = conn.execute(
        """SELECT id FROM entity WHERE lower(name) = lower(%(n)s) OR lower(id) = lower(%(n)s)
              OR lower(%(n)s) = ANY(SELECT lower(a) FROM unnest(aliases) a) ORDER BY id LIMIT 1""", {"n": name}
    ).fetchone()
    return row["id"] if row else None


def _parents(conn: Conn, entity_id: str | None) -> list[str]:
    out: list[str] = []
    while entity_id and entity_id not in out and len(out) < 5:
        out.append(entity_id)
        row = conn.execute("SELECT parent_id FROM entity WHERE id = %s", (entity_id,)).fetchone()
        entity_id = row["parent_id"] if row and row["parent_id"] != entity_id else None
    return out


def _completion(conn: Conn, slug: str, as_of: date) -> tuple[date | None, str | None]:
    out = runs.latest_output(conn, "cushion", "site", slug, as_of)
    if out is None:
        return None, f"site {slug}: cushion projection (model 'cushion') on or before {as_of}"
    p50 = (out["output"].get("projection") or {}).get("p50") if isinstance(out["output"], dict) else None
    if not isinstance(p50, str) or not re.fullmatch(r"\d{4}-\d{2}", p50):
        return None, f"site {slug}: cushion P50 rent start"
    y, m = int(p50[:4]), int(p50[5:7])
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1), None


def resolve(conn: Conn, inst: Instrument, as_of: date, has_curve: Callable[[str], bool],
            half_lives: dict[str, float]) -> Chain:
    ch = Chain(inst.id)
    tenant_row = None
    if inst.site_slug:
        tenant_row = conn.execute("SELECT entity_id, label FROM chain_party WHERE site_slug = %s AND role = 'tenant'",
                                  (inst.site_slug,)).fetchone()
    if tenant_row is not None and tenant_row["label"] not in ("—", "n/d"):
        ch.tenant_label = tenant_row["label"]
        ent = tenant_row["entity_id"]
        if ent and has_curve(ent):
            ch.tenant = ent
        elif ent:
            ch.missing.append(f"tenant {ent}: bond-implied intensity (no straight bonds priced)")
        else:
            ch.missing.append(f"tenant '{tenant_row['label']}': not one entity")
        for name in _PAREN.findall(tenant_row["label"]):
            g = _entity_by_name(conn, name.strip())
            if g:
                ch.guarantor = g
    backstop = inst.value("backstop")
    guaranteed = isinstance(backstop, str) and re.search(r"guarant", backstop, re.I)
    if guaranteed and not re.search(r"\bno guarantee", str(backstop), re.I):
        ch.notes.append(f"backstop text: {backstop[:120]}")
    if ch.guarantor and not has_curve(ch.guarantor):
        ch.missing.append(f"guarantor {ch.guarantor}: bond-implied intensity (backstop not priced)")
        ch.guarantor = None
    for ent in _parents(conn, inst.issuer):
        if has_curve(ent) and ent != ch.tenant:
            ch.obligor = ent
            break
    if ch.obligor and ch.obligor != inst.issuer:
        contracts = inst.value("tenants")
        ch.obligor_pass = 1.0 - CONTRACT_PROTECTION if isinstance(contracts, str) and contracts.strip() else 1.0
    chip_fact = inst.value("gpu_model")
    coll = inst.value("gpu_collateral")
    chip = chip_fact if isinstance(chip_fact, str) else (coll.get("chip") if isinstance(coll, dict) else None)
    if inst.kind in GPU_KINDS or chip:
        ch.collateral = "gpus"
        lives = [half_lives[CHIP_FAMILY[c]] for c in re.findall(r"GB[23]00|[HB][123]00", chip or "")
                 if CHIP_FAMILY.get(c) in half_lives]
        if lives:
            ch.chip, ch.half_life = chip, float(np.mean(lives))
        elif half_lives:
            ch.chip, ch.half_life = chip, float(np.median(list(half_lives.values())))
            ch.notes.append("GPU half-life: median of the stored chips (this instrument's chip not stored)")
        else:
            ch.missing.append("series gpu_residual:<chip> residual_pct_of_new (GPU half-life)")
    ch.recovery, ch.recovery_why = site_recovery(conn, inst.id, as_of)
    if (ch.recovery is not None and ch.recovery.method != ISSUER_WATERFALL
            and WATERFALL_COLLATERAL[ch.recovery.method] != ch.collateral):
        ch.recovery_why = (f"the recovery model values the {WATERFALL_COLLATERAL[ch.recovery.method]}, the price "
                           f"chain prices {ch.collateral} collateral")
        ch.recovery = None
    if inst.site_slug:
        ch.completion, miss = _completion(conn, inst.site_slug, as_of)
        if miss:
            ch.missing.append(miss)
        ch.signals_site = inst.site_slug
    return ch


@dataclass(frozen=True)
class Legs:
    """The intensities of one pricing day."""

    tenant: float | None
    obligor: float | None
    guarantor: float | None
    denials: int
    delays: int


def hazard_fn(ch: Chain, legs: Legs, day: date) -> Callable[[np.ndarray], np.ndarray]:
    t_done = year_frac(day, ch.completion) if ch.completion and ch.completion > day else 0.0
    completion = BASE_COMPLETION * min(1.75**legs.denials, 6.0)
    fm = 0.15 * min(0.25 * legs.delays, 1.0)

    def chain(t: np.ndarray) -> np.ndarray:
        lam = np.zeros_like(t, dtype=float)
        if legs.tenant is not None:
            lam += np.where(t < t_done, 1.0 - PROTECTION_PRE_RENT, 1.0 - PROTECTION_POST_RENT) * legs.tenant
        if legs.obligor is not None:
            lam += ch.obligor_pass * legs.obligor
        return lam + np.where(t < t_done, completion + fm, 0.0)

    if legs.guarantor is None:
        return chain
    return _copula(chain, legs.guarantor)


def _copula(chain: Callable[[np.ndarray], np.ndarray], lam_g: float) -> Callable[[np.ndarray], np.ndarray]:
    """Joint-default intensity of the chain and its guarantor (Gaussian copula, COPULA_RHO)."""
    mvn = multivariate_normal(mean=[0.0, 0.0], cov=[[1.0, COPULA_RHO], [COPULA_RHO, 1.0]])
    grid = np.linspace(0.0, 40.0, 481)
    lam = chain(grid[:-1] + np.diff(grid) / 2.0)
    s_chain = np.exp(-np.concatenate([[0.0], np.cumsum(lam * np.diff(grid))]))
    pd_c = np.clip(1.0 - s_chain, 1e-12, 1 - 1e-12)
    pd_g = np.clip(1.0 - np.exp(-lam_g * grid), 1e-12, 1 - 1e-12)
    joint = np.array([mvn.cdf([norm.ppf(a), norm.ppf(b)]) for a, b in zip(pd_c, pd_g, strict=True)])
    s_joint = np.clip(1.0 - joint, 1e-12, 1.0)
    lam_joint = np.maximum(-np.diff(np.log(s_joint)) / np.diff(grid), 0.0)
    mids = grid[:-1] + np.diff(grid) / 2.0
    return lambda t: np.interp(t, mids, lam_joint)


def _clip(x: float) -> float:
    return min(max(x, 0.0), 1.0)


def recovery_fn(ch: Chain, shift: float = 0.0) -> float | Callable[[np.ndarray], np.ndarray]:
    """Recovery share of par at default time t (years); `shift` moves the whole curve (sensitivity), within
    0..100c."""
    site = ch.recovery
    if site is not None:
        if len(site.path) > 1:
            ys = np.array([p[0] for p in site.path])
            rs = np.clip(np.array([p[1] for p in site.path]) + shift, 0.0, 1.0)
            return lambda t: np.interp(np.asarray(t, dtype=float), ys, rs)
        return _clip(site.base + shift)
    if ch.collateral == "gpus" and ch.half_life:
        h, r0 = ch.half_life, _clip(GPU_RECOVERY + shift)
        return lambda t: r0 * np.power(0.5, np.asarray(t) / h)
    return _clip(BUILDING_RECOVERY + shift)


@dataclass(frozen=True)
class IssuerRecovery:
    """An issuer's unsecured recovery from its balance-sheet waterfall (model 'recovery', subject entity), or
    the 40% convention and why there is none."""

    entity_id: str
    base: float | None  # share of par; None = no waterfall (the convention applies)
    why: str | None = None
    as_of: date | None = None
    version: str | None = None
    low: float | None = None
    high: float | None = None
    source: dict[str, Any] = field(default_factory=dict)
    inputs: dict[str, Any] = field(default_factory=dict)

    def value(self, convention: float) -> float:
        return convention if self.base is None else self.base


def issuer_recovery(conn: Conn, entity_id: str, as_of: date) -> IssuerRecovery:
    """The issuer's unsecured recovery current on `as_of` (point in time, at most RECOVERY_MAX_AGE_DAYS old)."""
    row = runs.latest_output(conn, "recovery", "entity", entity_id, as_of)
    if row is None or not isinstance(row["output"], dict):
        return IssuerRecovery(entity_id, None, f"the recovery model has no balance-sheet waterfall for {entity_id} on "
                                               f"or before {as_of}")
    if (as_of - row["as_of"]).days > RECOVERY_MAX_AGE_DAYS:
        return IssuerRecovery(entity_id, None, f"the issuer waterfall's latest output ({row['as_of']}) is more than "
                                               f"{RECOVERY_MAX_AGE_DAYS} days old")
    out = row["output"]
    unsec = (out.get("ranks") or {}).get("unsecured") or {}
    cents = num(unsec.get("cents"))
    if out.get("method") != ISSUER_WATERFALL or cents is None:
        return IssuerRecovery(entity_id, None, "; ".join(str(m) for m in out.get("missing") or [])
                              or "no balance-sheet waterfall for the issuer", row["as_of"], row["version"])
    span = unsec.get("rangeCents") or [None, None]
    lines = (out.get("lines") or {}).get("unsecured") or []
    source = {"tag": "M", "asOf": row["as_of"].isoformat(), "sourceName": RECOVERY_SOURCE,
              "methodVersion": out.get("methodVersion") or f"recovery v{row['version']}"}
    inputs = {k: out.get(k) for k in ("name", "filing", "periodEnd", "assetsUsd", "securedUsd", "leftUsd",
                                      "unsecuredClaimsUsd") if out.get(k) is not None}
    inputs["lines"] = lines
    inputs["rank"] = "unsecured"
    return IssuerRecovery(entity_id, cents / 100, None, row["as_of"], row["version"],
                          None if span[0] is None else span[0] / 100, None if span[1] is None else span[1] / 100,
                          source, inputs)


def recovery_basis(ch: Chain | None, issuer: IssuerRecovery | None = None) -> str:
    if ch is None:
        return ISSUER_WATERFALL if issuer is not None and issuer.base is not None else DEFAULT_ASSUMPTION
    if ch.recovery is None:
        return DEFAULT_ASSUMPTION
    return ISSUER_WATERFALL if ch.recovery.method == ISSUER_WATERFALL else SITE_WATERFALL


ISSUER_RULE = ("assets after stated haircuts, less SPV and site debt on its own collateral and the parent's secured "
               "debt, shared by the claims at the instrument's rank (5-95c)")


def _issuer_doc(cents: float | None, base: float, low: float | None, high: float | None, inputs: dict[str, Any],
                source: dict[str, Any], version: str | None, calibration: str | None) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "basis": ISSUER_WATERFALL, "cents": cents, "method": ISSUER_WATERFALL,
        "label": f"{ISSUER_LABEL}: {ISSUER_RULE}", "baseCents": round(base * 100, 1),
        "rangeCents": None if low is None or high is None else [round(low * 100), round(high * 100)],
        "inputs": inputs, "waterfall": inputs.get("lines") or [],
        "source": {**source, "model": "recovery", "version": version},
    }
    if calibration:
        doc["calibration"] = calibration
    return doc


def recovery_doc(ch: Chain | None, rec0: float | None, version: str, bond_recovery: float,
                 issuer: IssuerRecovery | None = None) -> dict[str, Any]:
    """`recoveryInputs` of the price output: which recovery was used, from what, and why."""
    cents = None if rec0 is None else round(rec0 * 100, 1)
    if ch is None:  # issuer bond: the bond-implied curve is solved at the same recovery
        if issuer is not None and issuer.base is not None:
            return _issuer_doc(cents, issuer.base, issuer.low, issuer.high, issuer.inputs, issuer.source,
                               issuer.version, "the issuer's bond-implied curve is solved from its bond prices at this "
                                               "same issuer unsecured recovery (one recovery for all its notes)")
        return {"basis": DEFAULT_ASSUMPTION, "cents": cents, "method": "bond_convention",
                "label": f"{bond_recovery:.0%} of par, the bond-implied CDS convention (the issuer curve's "
                         "default intensity is solved from bond prices at the same recovery)",
                "why": (issuer.why if issuer is not None and issuer.why else
                        "no balance-sheet waterfall for the issuer"),
                "source": {"tag": "M", "sourceName": "Marden price engine", "methodVersion": version}}
    site = ch.recovery
    if site is not None and site.method == ISSUER_WATERFALL:
        return _issuer_doc(cents, site.base, site.low, site.high, site.inputs, site.source, site.version, None)
    if site is not None:
        doc: dict[str, Any] = {
            "basis": SITE_WATERFALL, "cents": cents, "method": site.method,
            "label": ("powered shell waterfall: comparable value per MW x MW x share finished, less the claims "
                      "ahead, over the claims at the loan's rank (50th-percentile comp)"
                      if site.method == "powered_shell" else
                      "GPU collateral waterfall: GPUs x value per GPU less the resale haircut, over the pool "
                      "balance, at each scheduled balance as the GPUs age and the loan amortizes"),
            "baseCents": round(site.base * 100, 1),
            "rangeCents": None if site.low is None else [round(site.low * 100), round(site.high * 100)],  # type: ignore[operator]
            "inputs": site.inputs, "defaultCents": round(BUILDING_RECOVERY * 100),
            "source": {**site.source, "model": "recovery", "version": site.version},
        }
        if site.path:
            doc["pathCents"] = [{"years": round(y, 2), "cents": round(r * 100, 1)} for y, r in site.path]
        return doc
    gpu = ch.collateral == "gpus" and ch.half_life
    return {"basis": DEFAULT_ASSUMPTION, "cents": cents,
            "method": "gpu_default_half_life" if gpu else "building_default",
            "label": (f"{GPU_RECOVERY:.0%} of par at default today, halving every {ch.half_life:.1f} years (the "
                      "chip's half-life)" if gpu else f"{BUILDING_RECOVERY:.0%} of par, flat (buildings)"),
            "why": ch.recovery_why or "no site collateral waterfall", "defaultCents": round(BUILDING_RECOVERY * 100),
            **({"gpuHalfLifeYears": round(ch.half_life, 2)} if gpu and ch.half_life else {}),
            "source": {"tag": "M", "sourceName": "Marden price engine", "methodVersion": version}}


def recovery_input(out: dict[str, Any], as_of: date, version: str) -> dict[str, Any] | None:
    """The price output's `inputs` item for the recovery used (Sourced<number>, cents)."""
    rec, doc = out.get("recovery"), out.get("recoveryInputs") or {}
    if not isinstance(rec, int | float) or not doc:
        return None
    src = doc.get("source") or {}
    site = doc.get("basis") == SITE_WATERFALL
    label = ("site collateral model" if site else ISSUER_LABEL if doc.get("basis") == ISSUER_WATERFALL else "assumed")
    return {"key": "recoveryCents",
            "label": "Recovery if default, cents per dollar (" + label + ")",
            "value": round(rec * 100, 1), "unit": "c", "tag": "M", "asOf": src.get("asOf") or as_of.isoformat(),
            "methodVersion": src.get("methodVersion") or version, "sourceName": src.get("sourceName") or
            "Marden price engine", **({"sourceUrl": src["sourceUrl"]} if src.get("sourceUrl") else {})}
