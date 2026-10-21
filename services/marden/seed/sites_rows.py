"""`site` rows and site facts for the `sites` loader (field table: marden/seed/sites.py)."""

from __future__ import annotations

import re
from typing import Any

from marden.core.db import Conn
from marden.seed.common import tag_for_url
from marden.seed.sites_archive import Archive
from marden.seed.sites_entities import display_name, is_placeholder, norm
from marden.seed.sites_facts import FactSink, Src, is_nd, judged, number, pick_url, usd, weakest

# Cases that share a campus with another case's site.json record (dictionary 3.5: by name only).
SHARED_CAMPUS = {"APLD_PF1B4": "APLD_PF1", "GLXY_H1": "GLXY_H2"}
_OSM = re.compile(r"\bOSM (way|relation|node) (\d+)")
_EPOCH_MAP = "epoch.ai/data/ai-data-centers/map"
_FUEL = re.compile(r"\b(gas|turbines?|fuel cells?|engines?|gensets?)\b", re.I)


def coord_url(coord_src: str | None) -> str | None:
    """The public map a coordinate was read from, when the note names one."""
    text = coord_src or ""
    if _EPOCH_MAP in text:
        return f"https://{_EPOCH_MAP}"
    m = _OSM.search(text)
    return f"https://www.openstreetmap.org/{m[1]}/{m[2]}" if m else None


def power_source(supply: str | None, onsite: str | None, contract: str | None) -> str | None:
    """Map the free-text supply to types.ts PowerSource (Grid / On-site gas / Hybrid); None if unclear."""
    s = (supply or "").strip().lower()
    if s.startswith("grid"):
        return "Hybrid" if "+" in s else "Grid"
    if "on-site" in s or "off-grid" in s:
        return "On-site gas" if _FUEL.search(" ".join(filter(None, (supply, onsite, contract)))) else None
    return None


def tracking_names(arc: Archive, case: str) -> list[str]:
    """SPV and project-company names the site is tracked under (memo.chain, cases.debt issuers)."""
    names: list[str] = []
    for r in arc.memo(case).get("chain", []):
        if r.get("role") in ("Borrower", "Project company") and r.get("name"):
            names.append(display_name(r["name"]))
    for d in arc.case(case).get("debt", []):
        if d.get("issuer"):
            names.append(display_name(d["issuer"].split(";")[0]))
    out: dict[str, str] = {}
    for n in names:
        if n and not is_placeholder(n) and norm(n) not in out:
            out[norm(n)] = n
    return list(out.values())


def site_record(arc: Archive, case: str) -> tuple[dict[str, Any] | None, bool]:
    """The case's q3x/site.json record, and whether it is borrowed from a shared campus."""
    by_case = {r["case"]: r for r in arc.site_json.values() if r.get("case")}
    if case in by_case:
        return by_case[case], False
    other = SHARED_CAMPUS.get(case)
    return (by_case[other], True) if other in by_case else (None, False)


def upsert_site(conn: Conn, arc: Archive, case: str, slug: str, in_v1: bool) -> None:
    title = arc.data["aiwc/memo/titles.json"].get(case) or [arc.case(case).get("name") or case, None]
    site = arc.case(case).get("site") or {}
    rec, _ = site_record(arc, case)
    lat = number(rec.get("lat")) if rec else None
    lon = number(rec.get("lon")) if rec else None
    conn.execute(
        """INSERT INTO site (slug, name, legacy_group, state, county, geom, tracking_names, in_v1)
           VALUES (%(slug)s, %(name)s, %(case)s, %(state)s, %(county)s,
                   CASE WHEN %(lat)s::float8 IS NULL OR %(lon)s::float8 IS NULL THEN NULL
                        ELSE ST_SetSRID(ST_MakePoint(%(lon)s::float8, %(lat)s::float8), 4326) END,
                   %(names)s, %(in_v1)s)
           ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name, legacy_group = EXCLUDED.legacy_group,
             state = EXCLUDED.state, county = EXCLUDED.county, geom = EXCLUDED.geom,
             tracking_names = EXCLUDED.tracking_names, in_v1 = EXCLUDED.in_v1""",
        {"slug": slug, "name": title[0], "case": case, "state": site.get("state"), "county": site.get("county"),
         "lat": lat, "lon": lon, "names": tracking_names(arc, case), "in_v1": in_v1},
    )


def _capacity(sink: FactSink, slug: str, cap: dict[str, Any], src: Src) -> None:
    url, quote, inferred = pick_url(cap.get("src")), cap.get("quote"), bool(cap.get("inferred"))
    tag = "M" if inferred else None
    for fld, key, unit in (("critical_it_mw", "critical_it_mw", "MW"), ("gross_mw", "gross_mw", "MW"),
                           ("delivered_mw", "delivered_mw", "MW"), ("pct_complete", "pct_complete", "pct"),
                           ("expected_energization", "scheduled_completion", None),
                           ("original_energization", "original_completion", None)):
        sink.add("site", slug, fld, cap.get(key), src=src, url=url, quote=quote, unit=unit, tag=tag)


def _site_power(sink: FactSink, slug: str, proj: dict[str, Any], src: Src) -> None:
    it = proj.get("it_mw") or {}
    url = pick_url(it.get("src"))
    for fld, key in (("critical_it_mw", "value"), ("gross_mw", "gross_utility_mw"), ("delivered_mw", "delivered_mw")):
        sink.add("site", slug, fld, number(it.get(key)), src=src, url=url, unit="MW")
    sink.add("site", slug, "it_mw_estimate", number(it.get("est_it_mw")), src=src,
             url=pick_url(it.get("src"), tag="R"), unit="MW", tag="M")
    sp = proj.get("site_power") or {}
    url = pick_url(sp.get("src"))
    for fld, key in (("power_iso", "iso"), ("power_utility", "utility"), ("power_supply", "supply"),
                     ("onsite_generation", "onsite_generation"), ("power_contract", "contract")):
        text = sp.get(key)
        sink.add("site", slug, fld, text, src=src, url=url, tag="M" if judged(text) else None)
    sink.add("site", slug, "power_source", power_source(sp.get("supply"), sp.get("onsite_generation"),
                                                        sp.get("contract")), src=src, url=url, tag="M", rule=True)
    pays = proj.get("who_pays") or {}
    basis = pays.get("basis")
    sink.add("site", slug, "power_payer", pays.get("payer"), src=src,  # who_pays cites no URL of its own
             tag="M" if judged(basis, pays.get("payer")) else None, basis=basis)


def _dcmap(sink: FactSink, slug: str, row: dict[str, Any], src: Src) -> None:
    for fld, key, unit in (("gross_mw", "gross_mw", "MW"), ("delivered_mw", "deliv_mw", "MW"),
                           ("planned_it_mw", "it_mw_plan", "MW")):
        sink.add("site", slug, fld, number(row.get(key)), src=src, unit=unit)
    for fld, key in (("power_source_class", "psrc"), ("power_status", "pwr_status"), ("power_detail", "pwr_detail")):
        sink.add("site", slug, fld, row.get(key), src=src)
    sink.add("site", slug, "power_utility", row.get("util"), src=src, url=pick_url(row.get("pw_src")),
             tag="M" if judged(row.get("util")) else None)


def _site_json(sink: FactSink, slug: str, rec: dict[str, Any], shared: bool, src: Src) -> None:
    lat, lon = number(rec.get("lat")), number(rec.get("lon"))
    if lat is not None and lon is not None:
        value = {"lat": lat, "lon": lon, "accuracy_note": rec.get("accuracy_note"),
                 "coord_src": rec.get("coord_src"), "campus_key": rec.get("key")}
        sink.add("site", slug, "coordinates", {k: v for k, v in value.items() if v is not None}, src=src,
                 url=coord_url(rec.get("coord_src")), tag="M" if shared else "R", unit="deg")
    if shared:
        return  # schedule and imagery reads belong to the other case's buildings
    if not is_nd(rec.get("first_delivery")):
        value = {"text": rec["first_delivery"], "source_text": rec.get("delivery_src")}
        sink.add("site", slug, "first_delivery", {k: v for k, v in value.items() if v}, src=src)
    sink.add("site", slug, "construction_stage", rec.get("flag_txt"), src=src, tag="M")


def _contract(sink: FactSink, slug: str, ctr: dict[str, Any], src: Src) -> None:
    url, quote = pick_url(ctr.get("src")), ctr.get("quote")
    term = ctr.get("term")
    rows: list[tuple[str, Any, str | None]] = [
        ("rent_start", ctr.get("start"), None), ("outside_date", ctr.get("outside"), None),
        ("lease_tenant", ctr.get("tenant"), None),
        ("lease_term_years", number(term), "years"),
        ("lease_term", term if isinstance(term, str) else None, None),
        ("lease_tcv_usd", usd(ctr.get("tcv_m")), "USD"), ("lease_annual_rent_usd", usd(ctr.get("rent_m")), "USD"),
        ("lease_rent_basis", ctr.get("rb"), None), ("lease_termination", ctr.get("exit"), None),
        ("lease_renewal", ctr.get("renew"), None), ("lease_guarantee", ctr.get("guar"), None),
    ]
    for fld, value, unit in rows:
        sink.add("site", slug, fld, value, src=src, url=url, quote=quote, unit=unit,
                 tag="M" if judged(value) else None)
    sink.add("site", slug, "lease_kind", ctr.get("idle"), src=src, url=url, tag="M")


def _lease_item(lease: dict[str, Any]) -> tuple[dict[str, Any], str]:
    url = pick_url(lease.get("src"))
    item = {
        "tenant": lease.get("tenant"), "mw": lease.get("mw"), "term_years": lease.get("term_yrs"),
        "start": lease.get("start"), "tcv_usd": usd(lease.get("tcv_m")),
        "annual_rent_usd": usd(lease.get("annual_rent_m")), "rent_basis": lease.get("rent_basis"),
        "termination_rights": lease.get("termination_rights"), "outside_date": lease.get("outside_date"),
        "renewals": lease.get("renewals"), "inferred": True if lease.get("inferred") else None,
        "source_url": url, "quote": lease.get("quote"),
    }
    tag = "M" if lease.get("inferred") else tag_for_url(url)
    return {k: v for k, v in item.items() if not is_nd(v)}, tag


def _leases(sink: FactSink, slug: str, leases: list[dict[str, Any]], src: Src) -> None:
    items = [_lease_item(lease) for lease in leases]
    items = [(i, t) for i, t in items if i.get("tenant")]
    if items:
        sink.add("site", slug, "leases", [i for i, _ in items], src=src, tag=weakest(t for _, t in items))
    for lease in leases:
        url, tag = pick_url(lease.get("src")), "M" if lease.get("inferred") else None
        sink.add("site", slug, "outside_date", lease.get("outside_date"), src=src, url=url,
                 quote=lease.get("quote"), tag=tag)
    if len(leases) == 1:
        lease = leases[0]
        sink.add("site", slug, "rent_start", lease.get("start"), src=src, url=pick_url(lease.get("src")),
                 quote=lease.get("quote"), tag="M" if lease.get("inferred") or judged(lease.get("start")) else None)


def write_site_facts(sink: FactSink, arc: Archive, case: str, slug: str) -> None:
    titles = arc.data["aiwc/memo/titles.json"].get(case)
    if titles and len(titles) > 1:
        sink.add("site", slug, "place", titles[1], src=arc.src("aiwc/memo/titles.json"))
    sink.add("site", slug, "headline", arc.memo(case).get("headline"), src=arc.src(f"aiwc/memo/{case}.json"))
    dossier, csrc = arc.case(case), arc.src(f"aiwc/cases/{case}.json")
    site = dossier.get("site") or {}
    url, tag = pick_url(site.get("src")), "M" if site.get("inferred") else None
    for fld in ("county", "state", "location", "land"):
        sink.add("site", slug, fld, site.get(fld), src=csrc, url=url, tag=tag)
    _capacity(sink, slug, dossier.get("capacity") or {}, csrc)
    _leases(sink, slug, dossier.get("lease") or [], csrc)
    proj = arc.data["aiwc/gpu/site_power.json"]["projects"].get(case)
    if proj:
        _site_power(sink, slug, proj, arc.src("aiwc/gpu/site_power.json"))
    row = arc.dcmap_by_case.get(case)
    if row:
        _dcmap(sink, slug, row, arc.src("aiwc/q3x/dcmap.json"))
    rec, shared = site_record(arc, case)
    if rec:
        _site_json(sink, slug, rec, shared, arc.src("aiwc/q3x/site.json"))
    ctr = arc.ctr_by_case.get(case)
    if ctr:
        _contract(sink, slug, ctr, arc.src("aiwc/q3x/ctr.json"))
