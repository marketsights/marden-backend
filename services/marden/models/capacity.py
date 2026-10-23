"""Model `capacity` (spec 27): facility MW, IT MW, GPU count and implied GPU value per campus and building,
triangulated from the equipment and records Marden holds. Labelled M 'capacity v0.1 (model v0)'.

    python -m marden model capacity [--as-of YYYY-MM-DD] [--campus ID ...] [--json]

Outputs, one per subject: ('campus', id) and ('building', id) = CapacityEstimate (types.ts) + `missing`
(the dc_tab:capacity read model lists the campus and then its buildings). Every campus and every building
is estimated. A building's methods: its own stated MW (`facility_mw` on the building), its share of the
campus's stated facility MW by traced footprint area ("Campus share"), and its floor area (stated `sqft`,
else the footprint polygon, reported as `footprintSqft`) x developer density; its generation is the campus's.

1. Facility MW: the methods of capacity_methods (filed, utility load, prime generation, backup generators,
   footprint, disclosed GPU count) combined: a filed figure is the answer and the rest are checks; else the
   overlap of the stronger methods; `disagreement` when their figures differ by more than 25%.
2. IT MW = facility MW / PUE. PUE: the published data-center PUE range (series `data_center_pue`,
   pue_range low / high; base = midpoint). The assumption is shown in `pue`.
3. GPUs = IT MW x GPU share of IT / kW per GPU. Platform: the one a source states (the building's
   `gpu_type`, else the campus's disclosed chips by count, planned chips, chip types; for a campus, its
   buildings' `gpu_type`), tagged with that source. Otherwise a range across the platforms plausible for the
   campus's online year (`online_year`, else the first year in its schedule or large-load request, else
   today's): 2027 or later GB300 / VR200, 2026 GB200 / GB300, 2025 H200 / GB200, earlier H100 / H200, cut to
   the families the tenant runs at its other campuses when it discloses any. The low GPU count assumes the
   most power-hungry platform on the low IT MW and share, the high count the least on the high; the base
   is the year rule's generation (2026 or later GB300, 2025 GB200, earlier H100), all tagged M with
   `gpuBasis` saying which platform each end assumes. kW per GPU: the chip's power from
   `gpu_model:<chip>` tdp_w (as published; a B300 / B200 without one takes its GB300 / GB200 sibling's).
   GPU share of IT: calibrated on every campus that discloses both its chips and its IT MW (median,
   quartiles for the range); n/d below three such campuses. Racks = GPUs / 72 for the rack-scale GB200 /
   GB300 NVL72 platforms (`racks`, `gpusPerRack`). Implied GPU value = GPUs x the platform's new price
   range (gpu_model new_price_usd): low end the platform giving the least, high end the most.
4. A filed facility or IT MW replaces the estimate (`replacedByFiling`). Confidence: High with a filed
   figure, Medium when two or more methods agree within 25%, else Low; `confidenceReason` says why in a
   line. Every method's evidence is a sentence built from the fact (capacity_text), and every headline
   left n/d carries its reason in `why`.

Runs: weekly, Mondays 09:45 UTC, and within a minute of a campus's or building's capacity facts, a serving
plant's units or capacity, or the GPU / PUE series. A past --as-of is a backtest.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from datetime import date
from typing import Any

from marden import cli
from marden.core.db import Conn, Row
from marden.core.queue import app
from marden.models import capacity_methods as cm
from marden.models import capacity_text as ct
from marden.models import pit, triggers
from marden.models.gpu_inputs import chip_key
from marden.models.sourced import Batch, fact_value, m_value, method_version, num, run_outputs

MODEL, VERSION = "capacity", "0.1"
METHOD = method_version(MODEL, VERSION)
CRON_UTC = "45 9 * * 1"
CAMPUS_FIELDS = (
    "facility_mw",
    "it_mw",
    "large_load_request",
    "grid_mw",
    "backup_generator_count",
    "sqft_total",
    "developer",
    "chips",
    "chips_planned",
    "chip_types",
    "online_year",
    "schedule_text",
    "original_schedule_text",
    "tenant",
    "users",
)
PLANT_FIELDS = ("capacity_mw", "status")
UNIT_FIELDS = ("count", "rating_mw_each", "fuel")
GEN_BY_YEAR = ((2026, "GB300"), (2025, "GB200"), (0, "H100"))
# The platforms plausible for a campus coming online in a year when no source states one: the newest two
# NVIDIA platforms in volume shipment by then (gpu_model first_volume_ship); the base is GEN_BY_YEAR's.
PLATFORMS_BY_YEAR = ((2027, ("GB300", "VR200")), (2026, ("GB200", "GB300")), (2025, ("H200", "GB200")),
                     (0, ("H100", "H200")))
# The same silicon sold as an 8-GPU server and as an NVL72 rack: one's published power or price stands in
# for the other's when gpu_model has only one; and the rack platform a tenant's server chips belong to.
SIBLING = {"B300": "GB300", "B200": "GB200", "GB300": "B300", "GB200": "B200"}
FAMILY = {"B200": "GB200", "B300": "GB300"}
BUILDING_FIELDS = ("facility_mw", "it_mw", "sqft", "gpu_type")
# Rack-scale platforms: GPUs per rack as NVIDIA states it (GB200 / GB300 NVL72: 72 Blackwell GPUs per rack,
# about 120-140 kW of IT per rack). Other generations ship as 8-GPU servers whose rack count depends on the
# hall's power density, so no rack count is derived for them.
GPUS_PER_RACK = {"GB200": 72, "GB300": 72}
_YEAR = re.compile(r"(?<!\d)(20[2-4]\d)(?!\d)")  # "2H CY2026" -> 2026


def _r3(low: float | None, base: float | None, high: float | None, digits: int = 0) -> dict[str, float | None]:
    def r(x: float | None) -> float | None:
        return None if x is None else round(x, digits) if digits else float(round(x))

    return {"low": r(low), "base": r(base), "high": r(high)}


def _sig3(x: float | None) -> float | None:
    """A GPU count to three significant figures (68,143 -> 68,100): the estimate is not more precise."""
    if x is None or x <= 0:
        return x
    step = 10 ** max(len(str(int(x))) - 3, 0)
    return float(round(x / step) * step)


def gpu_chip(text: Any) -> str | None:
    """The gpu_model key a stated chip names: 'GB200 NVL72' -> 'GB200', 'Vera Rubin' -> 'VR200'."""
    t = str(text or "")
    if key := chip_key(t):
        return key
    if re.search(r"\brubin\s+ultra\b", t, re.I):
        return "RU"
    return "VR200" if re.search(r"\brubin\b", t, re.I) else None


def tenant_key(cur: dict[str, Row]) -> tuple[str, str] | None:
    """(key, name) of the campus's tenant: the tenant fact's first name, else the first named user."""
    v = (cur.get("tenant") or {}).get("value")
    if not v:
        users = (cur.get("users") or {}).get("value")
        v = users[0].get("name") if isinstance(users, list) and users and isinstance(users[0], dict) else None
    name = re.split(r"[(/,;]| with | and ", str(v or ""))[0].strip()
    return (name.lower(), name) if name else None


def online_year(cur: dict[str, Row], day: date) -> tuple[int, str]:
    """(year, how it is known): the online year, else the first year a schedule or large-load request names,
    else the as-of year (today's platforms)."""
    if (y := num((cur.get("online_year") or {}).get("value"))) is not None:
        return int(y), "online year"
    for fld, how in (("schedule_text", "the company's schedule"), ("original_schedule_text", "the original schedule")):
        if (m := _YEAR.search(str((cur.get(fld) or {}).get("value") or ""))) is not None:
            return int(m.group(1)), how
    llr = (cur.get("large_load_request") or {}).get("value")
    if isinstance(llr, dict) and (m := _YEAR.search(str(llr.get("expected_cod") or ""))) is not None:
        return int(m.group(1)), "the large-load request's expected start"
    return day.year, "no online date on record"


def stated_platform(rows: list[Row | None]) -> tuple[str | None, Row | None, str | None]:
    """(gpu_model key, the fact stating it, a stated accelerator that is not a GPU) from the first fact
    that names a chip: a chip-count object is read by its largest GPU count."""
    accelerator = None
    for row in rows:
        if row is None:
            continue
        v = row["value"]
        if isinstance(v, dict):
            counts = sorted(((k, num(n) or 0) for k, n in v.items()), key=lambda kv: -kv[1])
            names = [k for k, n in counts if n > 0]
        elif isinstance(v, list):
            names = [str(x) for x in v]
        else:
            names = [str(v or "")]
        for name in names:
            if key := gpu_chip(name):
                return key, row, None
        accelerator = accelerator or next((n for n in names if n.strip()), None)
    return None, None, accelerator


def _grouped(rows: list[Row]) -> dict[str, dict[str, Row]]:
    by: dict[str, dict[str, list[Row]]] = {}
    for r in rows:
        by.setdefault(r["subject_id"], {}).setdefault(r["field"], []).append(r)
    return {s: {f: cur for f, rs in fs.items() if (cur := pit.current_of(rs)) is not None} for s, fs in by.items()}


def load_plants(conn: Conn, day: date) -> dict[str, list[dict[str, Any]]]:
    serves = conn.execute(
        "SELECT ps.plant_id, ps.campus_id, p.name FROM plant_serves ps LEFT JOIN power_plant p ON p.id = ps.plant_id"
    ).fetchall()
    plant_ids = sorted({s["plant_id"] for s in serves})
    pf = _grouped(pit.fact_rows(conn, day, "plant", plant_ids, list(PLANT_FIELDS)))
    units = conn.execute("SELECT id, plant_id, count FROM power_unit WHERE plant_id = ANY(%s)", (plant_ids,)).fetchall()
    uf = _grouped(pit.fact_rows(conn, day, "unit", [u["id"] for u in units], list(UNIT_FIELDS)))
    by_plant: dict[str, list[dict[str, Any]]] = {}
    for u in units:
        f = uf.get(u["id"], {})
        by_plant.setdefault(u["plant_id"], []).append(
            {
                "count": num(f["count"]["value"]) if "count" in f else (u["count"] if f else None),
                "rating": num(f["rating_mw_each"]["value"]) if "rating_mw_each" in f else None,
                "fuel": str((f.get("fuel") or {}).get("value") or "").lower(),
                "rows": list(f.values()),
            }
        )
    out: dict[str, list[dict[str, Any]]] = {}
    for s in serves:
        f = pf.get(s["plant_id"], {})
        out.setdefault(s["campus_id"], []).append(
            {
                "id": s["plant_id"],
                "name": s["name"] or s["plant_id"],
                "unitIds": [u["id"] for u in units if u["plant_id"] == s["plant_id"]],
                "status": (f.get("status") or {}).get("value"),
                "capacity_mw": cm.mw_of(f["capacity_mw"]["value"]) if "capacity_mw" in f else None,
                "capacity_row": f.get("capacity_mw"),
                "units": by_plant.get(s["plant_id"], []),
            }
        )
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

    def __init__(self, conn: Conn, day: date, campuses: dict[str, dict[str, Row]],
                 names: dict[str, str] | None = None) -> None:
        self.day = day
        # tenant key -> [(campus name, the platform families its chips belong to)], for other campuses
        self.tenant_chips: dict[str, list[tuple[str, str, list[str]]]] = {}
        for cid, cur in campuses.items():
            t = tenant_key(cur)
            fams: list[str] = []
            for fld in ("chips", "chips_planned", "chip_types"):
                v = (cur.get(fld) or {}).get("value")
                for name in (v if isinstance(v, dict | list) else []):
                    if (k := gpu_chip(name)) and (f := FAMILY.get(k, k)) not in fams:
                        fams.append(f)
            if t and fams:
                self.tenant_chips.setdefault(t[0], []).append((cid, (names or {}).get(cid, cid), fams))
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

    def _row(self, chip: str | None, fld: str) -> Row | None:
        """The chip's gpu_model fact, else its same-silicon sibling's (B300 <-> GB300, B200 <-> GB200)."""
        own = self.chips.get(chip or "", {}).get(fld)
        return own if own is not None else self.chips.get(SIBLING.get(chip or "", ""), {}).get(fld)

    def kw_row(self, chip: str | None) -> Row | None:
        row = self._row(chip, "tdp_w")
        return row if row is not None and num(row["value"]) else None

    def kw(self, chip: str | None) -> float | None:
        row = self.kw_row(chip)
        return num(row["value"]) / 1000 if row is not None else None  # type: ignore[operator]

    def price(self, chip: str | None) -> tuple[float, float] | None:
        row = self._row(chip, "new_price_usd")
        v = row["value"] if row is not None and isinstance(row["value"], dict) else {}
        lo, hi = num(v.get("low")), num(v.get("high"))
        return (lo, hi) if lo and hi else None

    def plausible(self, year: int, tenant: tuple[str, str] | None, exclude: str) -> tuple[list[str], str, str]:
        """(platforms, base, why) for a campus no source states a platform for: the year's two platforms
        with a published chip power, cut to the families the tenant runs at its other campuses."""
        plats = [c for c in next(p for y, p in PLATFORMS_BY_YEAR if year >= y) if self.kw(c)]
        base = next(c for y, c in GEN_BY_YEAR if year >= y)
        tenant_note = ""
        elsewhere = [e for e in self.tenant_chips.get(tenant[0], []) if e[0] != exclude] if tenant else []
        if tenant and elsewhere:
            runs = {f for _, _, fams in elsewhere for f in fams}
            fit = [c for c in plats if c in runs]
            if fit:
                plats = fit
            where = ", ".join(n.split(" (")[0] for _, n, _ in elsewhere[:2])
            fams = sorted(runs)
            listed = fams[0] if len(fams) == 1 else f"{', '.join(fams[:-1])} and {fams[-1]}"
            tenant_note = f" {tenant[1]}'s other campuses state {listed}-class chips ({where})."
        if not plats:
            return [], base, ""
        if base not in plats:
            base = plats[-1]
        return plats, base, tenant_note

    def share(self, exclude: str) -> tuple[float, float, float] | None:
        pool = [s for c, s in self.samples if c != exclude]
        if len(pool) < 3:
            return None
        q = statistics.quantiles(pool, n=4)
        return q[0], statistics.median(pool), q[2]


def _gpu_estimate(
    it: dict[str, float | None],
    share: tuple[float, float, float],
    plats: list[str],
    base: str,
    mk: Market,
) -> tuple[dict[str, float | None], dict[str, float | None], dict[str, Any]] | None:
    """(GPUs, value $B, basis) across the platforms: the GPU low end is the platform that gives the fewest
    GPUs (most kW each) on the low IT MW and share, the high end the most on the high; the value ends are
    the platforms giving the least and most at their low and high new prices."""
    if it["base"] is None or it["low"] is None or it["high"] is None:
        return None
    per: dict[str, tuple[float, float, float]] = {}
    for c in plats:
        if kw := mk.kw(c):
            per[c] = (it["low"] * share[0] * 1000 / kw, it["base"] * share[1] * 1000 / kw,
                      it["high"] * share[2] * 1000 / kw)
    if base not in per:
        return None
    lo_c, hi_c = min(per, key=lambda c: per[c][0]), max(per, key=lambda c: per[c][2])
    gpus = {"low": _sig3(per[lo_c][0]), "base": _sig3(per[base][1]), "high": _sig3(per[hi_c][2])}
    priced = {c: p for c in per if (p := mk.price(c))}
    value = _r3(None, None, None)
    vends = {"low": base, "base": base, "high": base}
    if base in priced:
        vlo = {c: per[c][0] * priced[c][0] for c in priced}
        vhi = {c: per[c][2] * priced[c][1] for c in priced}
        vends = {"low": min(vlo, key=vlo.__getitem__), "base": base, "high": max(vhi, key=vhi.__getitem__)}
        value = _r3(vlo[vends["low"]] / 1e9, per[base][1] * sum(priced[base]) / 2 / 1e9, vhi[vends["high"]] / 1e9, 2)
    basis = {
        "platforms": [
            {"chip": c, "kwPerGpu": round(mk.kw(c) or 0, 3), "itKwPerGpu": round((mk.kw(c) or 0) / share[1], 2),
             **({"priceUsd": {"low": round(priced[c][0]), "high": round(priced[c][1])}} if c in priced else {})}
            for c in per
        ],
        "gpus": {"low": lo_c, "base": base, "high": hi_c},
        "value": vends,
    }
    return gpus, value, basis


def estimate(
    cid: str,
    name: str,
    scope: str,
    cur: dict[str, Row],
    plants: list[dict[str, Any]],
    mk: Market,
    dens: dict[str, list[tuple[str, float]]],
    sqft: tuple[float | None, Row | None, str],
    *,
    campus_cur: dict[str, Row] | None = None,
    share: tuple[float | None, float | None] = (None, None),
    building_types: list[Row] | None = None,
    names: dict[str, str] | None = None,
) -> dict[str, Any]:
    day = mk.day
    n_chips, chip_row, _ = chips_of(cur) if scope == "campus" else (None, None, None)
    site = campus_cur if scope == "building" and campus_cur is not None else cur  # campus-level facts
    campus_mw = cm.mw_of(campus_cur["facility_mw"]["value"]) if campus_cur and "facility_mw" in campus_cur else None
    # the platform: a source's (building gpu_type, the campus's chips, planned chips, chip types), else assumed
    stated_rows = [cur.get("gpu_type")] if scope == "building" else sorted(
        building_types or [], key=lambda r: r["valid_from"], reverse=True)[:1]
    stated, stated_row, accelerator = stated_platform(
        [*stated_rows, site.get("chips"), site.get("chips_planned"), site.get("chip_types")])
    campus_id = cid if scope == "campus" else str((campus_cur or {}).get("_id", cid))
    year, year_how = online_year(site, day)
    if stated:
        plats, base, tenant_note = [stated], stated, ""
    elif accelerator:
        plats, base, tenant_note = [], "", ""
    else:
        plats, base, tenant_note = mk.plausible(year, tenant_key(site), campus_id)
    kw, gpu_share = mk.kw(base or None), mk.share(campus_id)
    methods = [
        m
        for m in (
            cm.filed(cur),
            cm.utility_load(cur),
            cm.prime_generation(plants),
            cm.backup(cur),
            cm.campus_share(campus_mw, (campus_cur or {}).get("facility_mw"), share[0], share[1], name)
            if scope == "building"
            else None,
            cm.footprint(sqft[0], sqft[1], cid, cm.developer_key(cur), dens, sqft[2]),
            cm.gpu_implied(n_chips, chip_row, mk.kw(stated), gpu_share, mk.pue),
        )
        if m is not None
    ]
    best, disagree, confidence = cm.combine(methods)
    known = sorted((m for m in methods if m.known), key=cm._rank)
    missing, why = [], {}
    if best is None:
        missing.append(f"{scope} {cid}: no filed MW, utility load, generation, footprint or chip count")
        why["facilityMw"] = ("No filing, utility load request, serving plant, floor area or chip count gives a "
                             "facility MW yet.")
    elif len(known) < 2:
        missing.append(
            f"{scope} {cid}: one method only (no {'campus-level records' if scope == 'building' else 'second'}"
            " facility MW source: permit generator ratings, utility load, cooling capacity)"
        )
    if mk.pue is None:
        missing.append("series data_center_pue pue_range")
    fac = _r3(best.low, best.base, best.high) if best else _r3(None, None, None)
    it = _r3(None, None, None)
    if best and mk.pue:
        it = _r3(best.low / mk.pue[2], best.base / mk.pue[1], best.high / mk.pue[0])
    elif best:
        why["itMw"] = "No published data-center PUE range to turn facility MW into IT MW."
    else:
        why["itMw"] = "Needs a facility MW first."
    gpus, value, basis = _r3(None, None, None), _r3(None, None, None), None
    got = _gpu_estimate(it, gpu_share, plats, base, mk) if gpu_share and plats else None
    if got is not None:
        gpus, value, basis = got
        if stated:
            by = (stated_row or {}).get("source_name") or "its record"
            basis.update({"stated": True, "statedBy": by})
        else:
            on = f"online {year} ({year_how})" if year_how != "no online date on record" else (
                f"no online date on record, so the platforms shipping in {year}")
            ends = " and ".join(p["chip"] for p in basis["platforms"])
            basis.update({"stated": False, "onlineYear": year,
                          "why": f"No source states the GPU platform; {on}: {ends} plausible.{tenant_note}"})
        basis["note"] = ct.gpu_note(basis, gpu_share, it)
        if value["base"] is not None:
            basis["valueNote"] = ct.value_note(basis)
        else:
            why["gpuValueB"] = f"No published new price for {base}."
            missing.append(f"GPU value: new_price_usd for {base}")
    else:
        lacking = (
            f"the stated chips are {accelerator}, with no chip power or price on record"
            if accelerator
            else "IT MW"
            if it["base"] is None
            else "GPU share of IT (fewer than 3 campuses disclose chips and IT MW)"
            if not gpu_share
            else f"kW per GPU for {base or 'the platform'}"
        )
        missing.append(f"GPU estimate: {lacking}")
        reason = (f"The stated chips are {accelerator}, with no chip power or price on record, so no GPU count is "
                  "derived." if accelerator
                  else "Needs IT MW first." if it["base"] is None
                  else "Fewer than three campuses disclose both their chips and IT MW, so the GPU share of IT "
                       "load is not calibrated." if not gpu_share
                  else f"No published chip power for {base or 'the platform'}.")
        why.update({"gpus": reason, "gpuValueB": reason, "kwPerGpu": reason})
    replaced = [
        {"field": f, "value": fact_value(cur[k], cm.mw_of(cur[k]["value"]))}
        for f, k in (("facilityMw", "facility_mw"), ("itMw", "it_mw"))
        if k in cur and cur[k]["tag"].strip() == "F" and cm.mw_of(cur[k]["value"]) is not None
    ]
    if stated and stated_row is not None:
        gen_doc = fact_value(stated_row, stated)
    elif base and not accelerator:
        gen_doc = m_value(base, day, f"{METHOD}: platform assumed for the online year",
                          source_name="Marden capacity model", uncalibrated=True)
    else:
        gen_doc = fact_value(None, as_of=day, tag="M")
        why["gpuGeneration"] = (f"The stated chips are {accelerator}, with no chip power or price on record."
                                if accelerator
                                else "No source states the platform and none is plausible for the year.")
    kw_row = mk.kw_row(base or None)
    if kw and kw_row is not None:
        kw_doc = fact_value(kw_row, round(kw, 3))
        kw_doc["sourceName"] = f"{kw_row.get('source_name') or 'gpu_model'}: {base} chip power"
    else:
        kw_doc = fact_value(None, as_of=day)
    if mk.pue is None:
        why["pue"] = "No published data-center PUE range on record."
    if not gpu_share:
        why["gpuShareOfIt"] = "Fewer than three campuses disclose both their chips and IT MW."
    gpu_part = (f"GPU platform stated by {(stated_row or {}).get('source_name') or 'a record'}" if stated
                else f"stated chips ({accelerator}) have no power or price on record" if accelerator
                else "no stated GPU platform")
    reason = ct.confidence_reason(confidence, [(m.method, m.rows) for m in known], gpu_part)
    return {
        "scope": scope,
        "id": cid,
        "name": name,
        "facilityMw": fac,
        "itMw": it,
        "gpus": gpus,
        "gpuValueB": value,
        "pue": fact_value(mk.pue_row, round(mk.pue[1], 3)) if mk.pue else fact_value(None, as_of=day),
        "gpuGeneration": gen_doc,
        "kwPerGpu": kw_doc,
        "gpuShareOfIt": m_value(round(gpu_share[1], 3) if gpu_share else None, day, METHOD,
                                source_name="Marden capacity model: calibrated on campuses disclosing chips and IT MW",
                                uncalibrated=True),
        "confidence": confidence,
        "confidenceReason": reason,
        "facilityBasis": ct.facility_basis(best.method if best else None, [(m.method, m.rows) for m in known]),
        "methods": [
            {
                "method": m.method,
                "facilityMw": _r3(m.low, m.base, m.high),
                "assumptions": m.assumptions,
                "evidence": ct.evidence(m.rows, names),
            }
            for m in methods
        ],
        "disagreement": disagree,
        "replacedByFiling": replaced,
        "methodVersion": f"{METHOD} (model v0)",
        "why": why,
        "missing": missing,
        **({"gpuBasis": basis} if basis else {}),
        **_racks(base or None, gpus),
        **({"footprintSqft": round(sqft[0])} if scope == "building" and sqft[0] and sqft[1] is None else {}),
    }


def _racks(gen: str | None, gpus: dict[str, float | None]) -> dict[str, Any]:
    """Racks = GPUs / GPUs per rack for a rack-scale platform (GB200 / GB300 NVL72); omitted otherwise."""
    per = GPUS_PER_RACK.get(gen or "")
    if not per or gpus["base"] is None:
        return {}
    return {"racks": _r3(*(g / per if g is not None else None for g in (gpus["low"], gpus["base"], gpus["high"]))),
            "gpusPerRack": per}


def compute(conn: Conn, day: date, ids: list[str] | None = None) -> Batch:
    camps = conn.execute("SELECT id, name FROM campus ORDER BY id").fetchall()
    campuses = _grouped(pit.fact_rows(conn, day, "campus", [c["id"] for c in camps], list(CAMPUS_FIELDS)))
    cnames = {c["id"]: c["name"] for c in camps}
    mk, dens, plants = Market(conn, day, campuses, cnames), cm.densities(campuses), load_plants(conn, day)
    # names evidence sentences use: a plant's (its units' facts read as the plant's), a building's label
    names = {p["id"]: p["name"] for ps in plants.values() for p in ps}
    names |= {u: p["name"] for ps in plants.values() for p in ps for u in p["unitIds"]}
    blds = conn.execute(
        """SELECT b.id, b.campus_id, b.label, ST_Area(b.footprint::geography) AS m2 FROM building b ORDER BY b.id"""
    ).fetchall()
    bfacts = _grouped(pit.fact_rows(conn, day, "building", [b["id"] for b in blds], list(BUILDING_FIELDS)))
    names |= {b["id"]: b["label"] or b["id"] for b in blds}
    footprint_total: dict[str, float] = {}
    for b in blds:
        footprint_total[b["campus_id"]] = footprint_total.get(b["campus_id"], 0.0) + float(b["m2"] or 0)
    batch = Batch()
    for c in camps:
        if ids and c["id"] not in ids:
            continue
        cur = campuses.get(c["id"], {})
        sq = cur.get("sqft_total")
        out = estimate(
            c["id"],
            c["name"],
            "campus",
            cur,
            plants.get(c["id"], []),
            mk,
            dens,
            (num(sq["value"]) if sq else None, sq, "campus floor area"),
            building_types=[bfacts[b["id"]]["gpu_type"] for b in blds
                            if b["campus_id"] == c["id"] and "gpu_type" in bfacts.get(b["id"], {})],
            names=names,
        )
        batch.add("campus", c["id"], out)
        for b in (b for b in blds if b["campus_id"] == c["id"]):
            own = bfacts.get(b["id"], {})
            sq_row = own.get("sqft")
            stated_sqft = num(sq_row["value"]) if sq_row else None
            sqft = (stated_sqft, sq_row, "building floor area (stated)") if stated_sqft else (
                float(b["m2"]) * cm.SQFT_PER_M2 if b["m2"] else None, None, "building footprint (polygon area)")
            bcur = {**({"developer": cur["developer"]} if "developer" in cur else {}), **own}
            batch.add(
                "building",
                b["id"],
                estimate(
                    b["id"],
                    b["label"] or b["id"],
                    "building",
                    bcur,
                    [],
                    mk,
                    dens,
                    sqft,
                    campus_cur={**cur, "_id": c["id"]},  # type: ignore[dict-item]
                    share=(float(b["m2"]) if b["m2"] else None, footprint_total.get(c["id"])),
                    names=names,
                ),
            )
    batch.inputs = [len(campuses), [s for _, s in mk.samples]]
    return batch


def run(
    as_of: date | None = None, ids: list[str] | None = None, *, trigger: str = "manual", mark: bool = True
) -> dict[str, Any]:
    return run_outputs(MODEL, VERSION, as_of, lambda c, d: compute(c, d, ids), trigger=trigger, mark=mark)


def _run_keys(keys: list[str], trigger: str) -> dict[str, Any]:
    r = run(ids=None if "*" in keys else keys, trigger=trigger)
    return {"subjects": len(r["outputs"]), "changed": len(r["changed"]), "run_id": r["run_id"]}


_PLANT_CAMPUSES = "SELECT campus_id FROM plant_serves WHERE plant_id = %s"
_UNIT_CAMPUSES = (
    "SELECT ps.campus_id FROM power_unit u JOIN plant_serves ps ON ps.plant_id = u.plant_id WHERE u.id = %s"
)
_BUILDING_CAMPUS = "SELECT campus_id FROM building WHERE id = %s"


def _install_triggers() -> None:
    from marden.readmodels.deps import via

    for fld in CAMPUS_FIELDS:
        triggers.model_depends(MODEL, "campus", fld)
    for fld in ("facility_mw", "it_mw", "sqft", "gpu_type", "footprint_area_m2"):
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
            print(
                f"  {sid:36} {o['confidence']:6} fac {f['low']}-{f['base']}-{f['high']} it {o['itMw']['base']} gpus "
                f"{g['base']} ({o['gpuGeneration']['value']}) methods {[m['method'] for m in o['methods']]}"
                f"{' DISAGREE' if o['disagreement'] else ''}"
            )
    return 0
