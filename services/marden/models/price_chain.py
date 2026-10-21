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
- recovery: buildings flat at BUILDING_RECOVERY; GPU collateral (DDTLs, GPU leases, a gpu_model fact)
  decays from GPU_RECOVERY on the chip's half-life, fitted to the stored residual-value bands
  (series gpu_residual:<chip>, 'moderate').
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

BASE_COMPLETION = 0.015
PROTECTION_PRE_RENT = 0.5  # locked (Jupiter backtest)
PROTECTION_POST_RENT = 0.5  # v0.1 assumption: the methodology gives no post-rent value
CONTRACT_PROTECTION = 0.5
BUILDING_RECOVERY = 0.55
GPU_RECOVERY = 0.55
COPULA_RHO = 0.3
GPU_KINDS = ("ddtl", "lease")
CHIP_FAMILY = {"GB200": "B200", "GB300": "B300", "H100": "H100", "H200": "H200", "B200": "B200", "B300": "B300"}
_PAREN = re.compile(r"\(([^()]+)\)")


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


def recovery_fn(ch: Chain) -> float | Callable[[np.ndarray], np.ndarray]:
    if ch.collateral == "gpus" and ch.half_life:
        h = ch.half_life
        return lambda t: GPU_RECOVERY * np.power(0.5, np.asarray(t) / h)
    return BUILDING_RECOVERY
