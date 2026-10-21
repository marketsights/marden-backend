"""Model `capacity` (spec 27): facility MW, IT MW, GPU count and implied GPU value per campus and building,
triangulated from the equipment and records Marden holds. Labelled M 'capacity v0.1 (model v0)'.

    python -m marden model capacity [--as-of YYYY-MM-DD] [--campus ID ...] [--json]

Outputs, one per subject: ('campus', id) and ('building', id) = CapacityEstimate (types.ts) + `missing`
(the dc_tab:capacity read model lists the campus and then its buildings). Every campus is estimated;
buildings on campuses linked to a V1 site are estimated (the others are listed in the campus's missing).

1. Facility MW: the methods of capacity_methods (filed, utility load, prime generation, backup generators,
   footprint, disclosed GPU count) combined: a filed figure is the answer and the rest are checks; else the
   overlap of the stronger methods; `disagreement` when their figures differ by more than 25%.
2. IT MW = facility MW / PUE. PUE: the published data-center PUE range (series `data_center_pue`,
   pue_range low / high; base = midpoint). The assumption is shown in `pue`.
3. GPUs = IT MW x GPU share of IT / kW per GPU. Generation: the campus's disclosed chip types, else its
   online year (2026 or later GB300, 2025 GB200, earlier H100: the rule is in the generation's methodVersion).
   kW per GPU: the chip's power from `gpu_model:<chip>` tdp_w (as published). GPU share of IT: calibrated on
   every campus that discloses both its chips and its IT MW (median, quartiles for the range); n/d below
   three such campuses. Implied GPU value = GPUs x the chip's new price range (gpu_model new_price_usd).
4. A filed facility or IT MW replaces the estimate (`replacedByFiling`). Confidence: High with a filed
   figure, Medium when two or more methods agree within 25%, else Low.

Runs: weekly, Mondays 09:45 UTC, and within a minute of a campus's or building's capacity facts, a serving
plant's units or capacity, or the GPU / PUE series. A past --as-of is a backtest.
"""

from __future__ import annotations

import argparse
import json
import statistics
from datetime import date
from typing import Any

from marden import cli
from marden.core.db import Conn, Row
from marden.core.queue import app
from marden.models import pit, triggers
from marden.models import capacity_methods as cm
from marden.models.gpu_inputs import chip_key
from marden.models.sourced import Batch, fact_value, m_value, method_version, num, run_outputs

MODEL, VERSION = "capacity", "0.1"
METHOD = method_version(MODEL, VERSION)
CRON_UTC = "45 9 * * 1"
CAMPUS_FIELDS = ("facility_mw", "it_mw", "large_load_request", "grid_mw", "backup_generator_count", "sqft_total",
                 "developer", "chips", "chip_types", "online_year")
PLANT_FIELDS = ("capacity_mw", "status")
UNIT_FIELDS = ("count", "rating_mw_each", "fuel")
GEN_BY_YEAR = ((2026, "GB300"), (2025, "GB200"), (0, "H100"))


def _r3(low: float | None, base: float | None, high: float | None, digits: int = 0) -> dict[str, float | None]:
    def r(x: float | None) -> float | None:
        return None if x is None else round(x, digits) if digits else float(round(x))
    return {"low": r(low), "base": r(base), "high": r(high)}


def _evidence(rows: list[Row]) -> list[dict[str, Any]]:
    out = []
    for r in rows[:6]:
        out.append({"date": r["valid_from"].isoformat(), "text": f"{r['subject_type']} {r['subject_id']}: {r['field']} = "
                    f"{json.dumps(r['value'], ensure_ascii=False)[:160]}", "tag": r["tag"].strip(),
                    "sourceName": r.get("source_name") or "", "sourceUrl": r.get("source_url") or ""})
    return out


def _grouped(rows: list[Row]) -> dict[str, dict[str, Row]]:
    by: dict[str, dict[str, list[Row]]] = {}
    for r in rows:
        by.setdefault(r["subject_id"], {}).setdefault(r["field"], []).append(r)
    return {s: {f: cur for f, rs in fs.items() if (cur := pit.current_of(rs)) is not None} for s, fs in by.items()}


def load_plants(conn: Conn, day: date) -> dict[str, list[dict[str, Any]]]:
    serves = conn.execute("SELECT plant_id, campus_id FROM plant_serves").fetchall()
    plant_ids = sorted({s["plant_id"] for s in serves})
    pf = _grouped(pit.fact_rows(conn, day, "plant", plant_ids, list(PLANT_FIELDS)))
    units = conn.execute("SELECT id, plant_id, count FROM power_unit WHERE plant_id = ANY(%s)", (plant_ids,)).fetchall()
    uf = _grouped(pit.fact_rows(conn, day, "unit", [u["id"] for u in units], list(UNIT_FIELDS)))
    by_plant: dict[str, list[dict[str, Any]]] = {}
    for u in units:
        f = uf.get(u["id"], {})
        by_plant.setdefault(u["plant_id"], []).append({
            "count": num(f["count"]["value"]) if "count" in f else (u["count"] if f else None),
            "rating": num(f["rating_mw_each"]["value"]) if "rating_mw_each" in f else None,
            "fuel": str((f.get("fuel") or {}).get("value") or "").lower(), "rows": list(f.values())})
    out: dict[str, list[dict[str, Any]]] = {}
    for s in serves:
        f = pf.get(s["plant_id"], {})
        out.setdefault(s["campus_id"], []).append({
            "id": s["plant_id"], "status": (f.get("status") or {}).get("value"),
            "capacity_mw": cm.mw_of(f["capacity_mw"]["value"]) if "capacity_mw" in f else None,
            "capacity_row": f.get("capacity_mw"), "units": by_plant.get(s["plant_id"], [])})
    return out


def chips_of(cur: dict[str, Row]) -> tuple[float | None, Row | None, str | None]:
    row = cur.get("chips")
    if row is None or not isinstance(row["value"], dict):
        return None, None, None
    counts = {k: num(v) for k, v in row["value"].items() if num(v)}
    if not counts:
        return None, None, None
    top = max(counts, key=lambda k: counts[k])  # type: ignore[arg-type,return-value]
    return float(sum(counts.values())), row, chip_key(top)  # type: ignore[arg-type]


class Market:
    """PUE, chip power and price, and the GPU share of IT calibrated across campuses (as of the day)."""

    def __init__(self, conn: Conn, day: date, campuses: dict[str, dict[str, Row]]) -> None:
        self.day = day
        pue = pit.current_of(pit.fact_rows(conn, day, "series", ["data_center_pue"], ["pue_range"]))
        v = pue["value"] if pue is not None and isinstance(pue["value"], dict) else {}
        lo, hi = num(v.get("low")), num(v.get("high"))
        self.pue_row, self.pue = pue, ((lo, (lo + hi) / 2, hi) if lo and hi else None)
        rows = pit.fact_rows(conn, day, "series", None, ["tdp_w", "new_price_usd"])
        g = _grouped([r for r in rows if r["subject_id"].startswith("gpu_model:")])
        self.chips = {sid.split(":", 1)[1]: f for sid, f in g.items()}
        samples = []
        for cid, cur in campuses.items():
            n, _, chip = chips_of(cur)
            it = cm.mw_of(cur["it_mw"]["value"]) if "it_mw" in cur else None
            kw = self.kw(chip)
            if n and it and kw:
                samples.append((cid, min(n * kw / 1000 / it, 1.0)))
        self.samples = samples

    def kw(self, chip: str | None) -> float | None:
        row = self.chips.get(chip or "", {}).get("tdp_w")
        return num(row["value"]) / 1000 if row is not None and num(row["value"]) else None

    def price(self, chip: str | None) -> tuple[float, float] | None:
        row = self.chips.get(chip or "", {}).get("new_price_usd")
        v = row["value"] if row is not None and isinstance(row["value"], dict) else {}
        lo, hi = num(v.get("low")), num(v.get("high"))
        return (lo, hi) if lo and hi else None

    def share(self, exclude: str) -> tuple[float, float, float] | None:
        pool = [s for c, s in self.samples if c != exclude]
        if len(pool) < 3:
            return None
        q = statistics.quantiles(pool, n=4)
        return q[0], statistics.median(pool), q[2]


def estimate(cid: str, name: str, scope: str, cur: dict[str, Row], plants: list[dict[str, Any]], mk: Market,
             dens: dict[str, list[tuple[str, float]]], sqft: tuple[float | None, Row | None, str]) -> dict[str, Any]:
    day = mk.day
    n_chips, chip_row, disclosed_chip = chips_of(cur) if scope == "campus" else (None, None, None)
    year = num((cur.get("online_year") or {}).get("value"))
    gen = disclosed_chip or (next(c for y, c in GEN_BY_YEAR if (year or 0) >= y) if year else None)
    kw, share = mk.kw(gen), mk.share(cid)
    methods = [m for m in (cm.filed(cur), cm.utility_load(cur), cm.prime_generation(plants), cm.backup(cur),
                           cm.footprint(sqft[0], sqft[1], cid, cm.developer_key(cur), dens, sqft[2]),
                           cm.gpu_implied(n_chips, chip_row, kw, share, mk.pue)) if m is not None]
    best, disagree, confidence = cm.combine(methods)
    missing = []
    if best is None:
        missing.append(f"{scope} {cid}: no filed MW, utility load, generation, footprint or chip count")
    elif sum(1 for m in methods if m.known) < 2:
        missing.append(f"{scope} {cid}: one method only (no {'campus-level records' if scope == 'building' else 'second'}"
                       " facility MW source: permit generator ratings, utility load, cooling capacity)")
    if mk.pue is None:
        missing.append("series data_center_pue pue_range")
    fac = _r3(best.low, best.base, best.high) if best else _r3(None, None, None)
    it = _r3(None, None, None)
    if best and mk.pue:
        it = _r3(best.low / mk.pue[2], best.base / mk.pue[1], best.high / mk.pue[0])
    gpus = _r3(None, None, None)
    if it["base"] is not None and share and kw:
        gpus = _r3(it["low"] * share[0] * 1000 / kw, it["base"] * share[1] * 1000 / kw,
                   it["high"] * share[2] * 1000 / kw)
    else:
        missing.append(f"GPU estimate: {'kW per GPU for ' + str(gen) if not kw else 'GPU share of IT (fewer than 3 campuses disclose chips and IT MW)' if not share else 'IT MW'}")
    price = mk.price(gen)
    value = _r3(gpus["low"] * price[0] / 1e9, gpus["base"] * sum(price) / 2 / 1e9, gpus["high"] * price[1] / 1e9, 2) \
        if gpus["base"] is not None and price else _r3(None, None, None)
    replaced = [{"field": f, "value": fact_value(cur[k], cm.mw_of(cur[k]["value"]))}
                for f, k in (("facilityMw", "facility_mw"), ("itMw", "it_mw"))
                if k in cur and cur[k]["tag"].strip() == "F" and cm.mw_of(cur[k]["value"]) is not None]
    gen_doc = fact_value(cur["chip_types"], gen) if disclosed_chip and "chip_types" in cur else \
        m_value(gen, day, f"{METHOD}: generation by online year", uncalibrated=True)
    kw_row = mk.chips.get(gen or "", {}).get("tdp_w")
    return {
        "scope": scope, "id": cid, "name": name,
        "facilityMw": fac, "itMw": it, "gpus": gpus, "gpuValueB": value,
        "pue": fact_value(mk.pue_row, round(mk.pue[1], 3)) if mk.pue else fact_value(None, as_of=day),
        "gpuGeneration": gen_doc if gen else fact_value(None, as_of=day, tag="M"),
        "kwPerGpu": fact_value(kw_row, round(kw, 3)) if kw and kw_row is not None else fact_value(None, as_of=day),
        "gpuShareOfIt": m_value(round(share[1], 3) if share else None, day, METHOD, uncalibrated=True),
        "confidence": confidence,
        "methods": [{"method": m.method, "facilityMw": _r3(m.low, m.base, m.high), "assumptions": m.assumptions,
                     "evidence": _evidence(m.rows)} for m in methods],
        "disagreement": disagree,
        "replacedByFiling": replaced,
        "methodVersion": f"{METHOD} (model v0)",
        "missing": missing,
    }


def compute(conn: Conn, day: date, ids: list[str] | None = None) -> Batch:
    camps = conn.execute("SELECT id, name FROM campus ORDER BY id").fetchall()
    campuses = _grouped(pit.fact_rows(conn, day, "campus", [c["id"] for c in camps], list(CAMPUS_FIELDS)))
    mk, dens, plants = Market(conn, day, campuses), cm.densities(campuses), load_plants(conn, day)
    v1 = {r["campus_id"] for r in conn.execute(
        "SELECT sl.campus_id FROM site_link sl JOIN site s ON s.slug = sl.site_slug WHERE s.in_v1").fetchall()}
    blds = conn.execute(
        """SELECT b.id, b.campus_id, b.label, ST_Area(b.footprint::geography) AS m2 FROM building b
            WHERE b.campus_id = ANY(%s) ORDER BY b.id""", (sorted(v1),)).fetchall()
    batch = Batch()
    for c in camps:
        if ids and c["id"] not in ids:
            continue
        cur = campuses.get(c["id"], {})
        sq = cur.get("sqft_total")
        out = estimate(c["id"], c["name"], "campus", cur, plants.get(c["id"], []), mk, dens,
                       (num(sq["value"]) if sq else None, sq, "campus floor area"))
        if c["id"] not in v1:
            out["missing"].append("buildings: estimated for campuses linked to a V1 site only")
        batch.add("campus", c["id"], out)
        for b in (b for b in blds if b["campus_id"] == c["id"]):
            sqft = float(b["m2"]) * cm.SQFT_PER_M2 if b["m2"] else None
            bdev = {"developer": cur["developer"]} if "developer" in cur else {}
            batch.add("building", b["id"], estimate(b["id"], b["label"] or b["id"], "building", bdev, [], mk, dens,
                                                    (sqft, None, "building footprint (polygon area)")))
    batch.inputs = [len(campuses), [s for _, s in mk.samples]]
    return batch


def run(as_of: date | None = None, ids: list[str] | None = None, *, trigger: str = "manual",
        mark: bool = True) -> dict[str, Any]:
    return run_outputs(MODEL, VERSION, as_of, lambda c, d: compute(c, d, ids), trigger=trigger, mark=mark)


def _run_keys(keys: list[str], trigger: str) -> dict[str, Any]:
    r = run(ids=None if "*" in keys else keys, trigger=trigger)
    return {"subjects": len(r["outputs"]), "changed": len(r["changed"]), "run_id": r["run_id"]}


_PLANT_CAMPUSES = "SELECT campus_id FROM plant_serves WHERE plant_id = %s"
_UNIT_CAMPUSES = ("SELECT ps.campus_id FROM power_unit u JOIN plant_serves ps ON ps.plant_id = u.plant_id "
                  "WHERE u.id = %s")
_BUILDING_CAMPUS = "SELECT campus_id FROM building WHERE id = %s"


def _install_triggers() -> None:
    from marden.readmodels.deps import via

    for fld in CAMPUS_FIELDS:
        triggers.model_depends(MODEL, "campus", fld)
    for fld in ("facility_mw", "it_mw", "footprint_area_m2"):
        triggers.model_depends(MODEL, "building", fld, via(_BUILDING_CAMPUS))
    for fld in PLANT_FIELDS:
        triggers.model_depends(MODEL, "plant", fld, via(_PLANT_CAMPUSES))
    for fld in UNIT_FIELDS:
        triggers.model_depends(MODEL, "unit", fld, via(_UNIT_CAMPUSES))
    for fld in ("pue_range", "tdp_w", "new_price_usd"):
        triggers.model_depends(MODEL, "series", fld, triggers.everything)
    triggers.register_runner(MODEL, _run_keys)


_install_triggers()


@app.periodic(cron=CRON_UTC, periodic_id="capacity_weekly")
@app.task(name="models.capacity", queue="models", queueing_lock="models.capacity", lock="models.capacity")
def capacity_task(timestamp: int | None = None) -> dict[str, Any]:
    return _run_keys(["*"], "schedule")


def _args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--as-of", type=date.fromisoformat, help="YYYY-MM-DD (default today, US Eastern)")
    p.add_argument("--campus", action="append", dest="campuses", help="campus id (repeatable)")
    p.add_argument("--no-mark", action="store_true", help="do not mark read models dirty")
    p.add_argument("--json", action="store_true")


@cli.action("model", MODEL, help="Capacity and GPU estimate per campus and building (spec 27)", configure=_args)
def capacity_cli(args: argparse.Namespace) -> int:
    r = run(args.as_of, args.campuses, trigger="manual", mark=not args.no_mark)
    if args.json:
        print(json.dumps(r["outputs"], indent=2, ensure_ascii=False))
        return 0
    print(f"capacity {VERSION} as of {r['as_of']} (run {r['run_id']}): {len(r['outputs'])} subjects")
    for sid, o in r["outputs"].items():
        if o["scope"] == "campus" and o["methods"]:
            f, g = o["facilityMw"], o["gpus"]
            print(f"  {sid:36} {o['confidence']:6} fac {f['low']}-{f['base']}-{f['high']} it {o['itMw']['base']} gpus "
                  f"{g['base']} ({o['gpuGeneration']['value']}) methods {[m['method'] for m in o['methods']]}"
                  f"{' DISAGREE' if o['disagreement'] else ''}")
    return 0
