"""Price v0.2 recovery: a facility prices with its site collateral waterfall (model 'recovery') when that model
has a current central estimate, else the v0.1 default, and says which (`recoveryBasis`), from what
(`recoveryInputs`) and how much it matters (`recoverySensitivity`, the price change per 10c of recovery).
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import pytest
from price_rows import copy_price_inputs

from marden.core import db
from marden.models import runs
from marden.models.price_chain import (
    BUILDING_RECOVERY,
    GPU_RECOVERY,
    RECOVERY_MAX_AGE_DAYS,
    Chain,
    SiteRecovery,
    recovery_basis,
    recovery_doc,
    recovery_fn,
    site_recovery,
)
from marden.models.recovery import _path

DAY = date(2026, 10, 14)


def _site(**kw: Any) -> SiteRecovery:
    base = {"method": "powered_shell", "base": 0.7, "as_of": DAY, "version": "0.1",
            "source": {"tag": "M", "asOf": DAY.isoformat(), "sourceName": "Marden recovery model"}}
    return SiteRecovery(**{**base, **kw})


def test_recovery_fn_uses_the_site_waterfall_else_the_default():
    shell = Chain("x", recovery=_site())
    assert recovery_fn(shell) == 0.7 and recovery_fn(shell, 0.05) == pytest.approx(0.75)
    assert recovery_fn(Chain("x", recovery=_site(base=0.98)), 0.05) == 1.0  # capped at 100c
    gpu = Chain("g", collateral="gpus", half_life=0.9,
                recovery=_site(method="gpu_collateral", base=0.8, path=((0.0, 0.8), (1.0, 0.7), (3.0, 0.9))))
    r = recovery_fn(gpu)
    assert callable(r)
    assert np.allclose(r(np.array([0.0, 0.5, 2.0, 10.0])), [0.8, 0.75, 0.8, 0.9])  # held flat after the path
    assert recovery_fn(Chain("d")) == BUILDING_RECOVERY
    decay = recovery_fn(Chain("d", collateral="gpus", half_life=2.0))
    assert callable(decay) and decay(np.array([2.0]))[0] == pytest.approx(GPU_RECOVERY / 2)
    assert recovery_basis(shell) == "site_waterfall" and recovery_basis(Chain("d")) == "default_assumption"
    assert recovery_basis(None) == "default_assumption"


def test_recovery_doc_names_the_basis_inputs_and_why():
    site = recovery_doc(Chain("x", recovery=_site(inputs={"valueB": 10.9, "seniorClaimsB": 0.0})), 0.7,
                        "price v0.2", 0.4)
    assert site["basis"] == "site_waterfall" and site["cents"] == 70.0 and site["inputs"]["valueB"] == 10.9
    assert site["source"]["sourceName"] == "Marden recovery model" and site["defaultCents"] == 55
    dflt = recovery_doc(Chain("d", recovery_why="nothing delivered"), 0.55, "price v0.2", 0.4)
    assert dflt["basis"] == "default_assumption" and dflt["why"] == "nothing delivered"
    bond = recovery_doc(None, 0.4, "price v0.2", 0.4)
    assert bond["method"] == "bond_convention" and bond["cents"] == 40.0


def test_gpu_path_keeps_one_point_per_time():
    assert _path([(0.0, 0.5), (0.0, 0.6), (0.25, 0.7)]) == [(0.0, 0.6), (0.25, 0.7)]


def _write(conn: Any, iid: str, day: date, output: dict[str, Any]) -> None:
    run_id = runs.start_run("recovery", "0.1", trigger="test")
    runs.write_output(conn, run_id, "recovery", "0.1", "instrument", iid, day, output)


def _shell(base: int | None, missing: list[str] | None = None) -> dict[str, Any]:
    return {"recovery": {"value": None if base is None else f"{base - 2}–{base + 3}", "tag": "M",
                         "asOf": DAY.isoformat(), "sourceName": "Marden recovery model",
                         "methodVersion": "recovery v0.1"},
            "valueB": {"value": 4.0, "tag": "M"}, "method": "powered_shell", "missing": missing or [],
            "basis": {"baseCents": base, "rangeCents": None if base is None else [base - 2, base + 3],
                      "seniorClaimsB": 1.0, "pariClaimsB": 2.0,
                      "claims": [{"instrumentId": "I-1", "rank": 1, "amountUsd": 2e9}]}}


def test_site_recovery_reads_a_current_point_in_time_output(committed):
    _write(committed, "I-1", DAY, _shell(52))
    got, why = site_recovery(committed, "I-1", DAY)
    assert why is None and got is not None and got.base == 0.52 and (got.low, got.high) == (0.5, 0.55)
    assert got.inputs["seniorClaimsB"] == 1.0 and got.inputs["lien"] == "first lien (senior secured)"
    assert site_recovery(committed, "I-1", date(2026, 10, 13)) == (None, "the recovery model has no output for "
                                                                         "this instrument on or before 2026-10-13")
    stale, why = site_recovery(committed, "I-1", date(2026, 10, 14 + RECOVERY_MAX_AGE_DAYS + 1))
    assert stale is None and "days old" in (why or "")
    _write(committed, "I-2", DAY, _shell(None, ["completion of the works (nothing delivered)"]))
    none, why = site_recovery(committed, "I-2", DAY)
    assert none is None and "completion of the works" in (why or "")


def test_a_facility_prices_with_its_site_waterfall_and_reports_the_sensitivity(committed):
    copy_price_inputs(["EX-JUPITER-190"])  # Oracle's curve is copied: the tenant leg carries default odds
    from marden.models.price import run_day

    with db.transaction() as conn:
        before = run_day(conn, DAY, ["EX-JUPITER-190"])["prices"]["EX-JUPITER-190"]
    out = _shell(30)
    out["basis"]["claims"] = [{"instrumentId": "EX-JUPITER-190", "rank": 1, "amountUsd": 18e9}]
    _write(committed, "EX-JUPITER-190", DAY, out)
    with db.transaction() as conn:
        after = run_day(conn, DAY, ["EX-JUPITER-190"])["prices"]["EX-JUPITER-190"]
    assert before["recoveryBasis"] == "default_assumption" and before["recovery"] == 0.55
    assert after["recoveryBasis"] == "site_waterfall" and after["recovery"] == 0.3
    ri = after["recoveryInputs"]
    assert ri["method"] == "powered_shell" and ri["rangeCents"] == [28, 33] and ri["inputs"]["seniorClaimsB"] == 1.0
    assert ri["source"]["sourceName"] == "Marden recovery model" and ri["source"]["model"] == "recovery"
    rec_input = next(i for i in after["inputs"] if i["key"] == "recoveryCents")
    assert rec_input["value"] == 30.0 and rec_input["sourceName"] == "Marden recovery model"
    sens = after["recoverySensitivity"]
    assert sens["holdingSpreadPer10cPts"] > 0 and sens["per10cPts"] > 0 and sens["stepCents"] == 10.0
    # Oracle's intensity rose after issue: less recovery, a lower price (the premium is re-solved at issue)
    assert after["price"] < before["price"]
