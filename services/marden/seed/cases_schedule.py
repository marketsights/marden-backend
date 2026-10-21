"""Delivery schedule, debt facilities and power economics per case (field table: seed/cases.py).

- aiwc/aiw_proj.json (the project delivery tracker), joined by ``aiwcredit.cases[].aiw_idx``
  (array positions; GLXY_H1 and GLXY_H2 both list entry 0, CoreWeave's single-site lease, so it
  lands on both sites): each entry, its dated ready-for-service statements and its dated changes.
- aiwc/q3x/fac.json rows that carry a case: commitments, drawn amounts, lenders, collateral,
  covenants, advance rates and GPU counts (spec 26 "Draws and funding" and "Equipment"). The
  `price` text of a row is a legacy mark and is not written.
- aiwcredit.json gpu.pwp: the archive's per-case power-cost estimate (M).
"""

from __future__ import annotations

from typing import Any

from marden.seed.cases_archive import AIWCREDIT, FAC, PROJ, Archive
from marden.seed.cases_quarantine import PRICE_KEYS, scrub_text, strip
from marden.seed.cases_sink import FactSink, first_url, num, urls_in, usd_m


def _project(sink: FactSink, arc: Archive, slug: str, idx: int, p: dict[str, Any]) -> None:
    src = arc.src(PROJ)
    latest = p.get("rfs_latest") or {}
    value = {
        "tracker_index": idx, "issuer": p.get("issuer"), "project": p.get("project"), "site": p.get("site"),
        "mw": p.get("mw"), "counterparty": p.get("counterparty"), "term": p.get("term"),
        "status": p.get("status"), "status_why": scrub_text(p.get("status_why")),
        "financing": [{"instrument": f.get("instrument"), "amount_usd": num(f.get("amount_usd")),
                       "lenders": f.get("lenders")} for f in p.get("financing") or []],
    }
    sink.add("site", slug, "delivery_project", value, src=src, url=first_url(latest.get("src")), judged=True)
    for which in ("rfs_prior", "rfs_latest"):
        r = p.get(which) or {}
        url = first_url(r.get("src"))
        sink.add("site", slug, "rfs_statement",
                 {"project": p.get("project"), "which": which.removeprefix("rfs_"), "text": r.get("text"),
                  "date": r.get("date")} if r.get("text") else None,
                 src=src, url=url, valid_from=r.get("date"))
    for c in p.get("changes") or []:
        url = first_url(c.get("src"))
        sink.add("site", slug, "schedule_change", {"project": p.get("project"), "date": c.get("date"),
                                                   "what": c.get("what")},
                 src=src, url=url, quote=c.get("quote"), valid_from=c.get("date"))


def write_projects(sink: FactSink, arc: Archive, case: str, slug: str, stats: dict[str, Any]) -> None:
    rows = arc.data[PROJ]
    for idx in (arc.credit_cases.get(case) or {}).get("aiw_idx") or []:
        if isinstance(idx, int) and 0 <= idx < len(rows):
            _project(sink, arc, slug, idx, rows[idx])
        else:
            stats["skipped"].setdefault("aiw_idx out of range", []).append(f"{case}:{idx}")


def write_facilities(sink: FactSink, arc: Archive, slugs: dict[str, str]) -> int:
    """q3x/fac.json rows with a case -> one `debt_facility` fact per row on that case's site."""
    src, n = arc.src(FAC), 0
    for row in arc.data[FAC].get("rows") or []:
        slug = arc.site_of(row.get("case"))
        if slug is None or row.get("case") not in slugs:
            continue
        row = strip(row, PRICE_KEYS)
        docs = row.get("docs") or []
        url = first_url([d.get("url") or d.get("u") for d in docs if isinstance(d, dict)])
        value = {
            "fid": row.get("fid"), "name": row.get("name"), "borrower": row.get("borrower"),
            "sponsor": row.get("sponsor"), "type": row.get("type"), "size_usd": usd_m(row.get("size_m")),
            "drawn_usd": usd_m(row.get("drawn_m")), "drawn_asof": row.get("drawn_asof"),
            "close_date": row.get("date"), "maturity": row.get("maturity"), "rate": row.get("rate"),
            "lenders": row.get("lenders"), "collateral": row.get("collateral"), "chips": row.get("chips"),
            "chips_basis": row.get("chips_basis"), "gpu_count": row.get("gpu_count"),
            "gpu_count_source": row.get("gpu_count_src"), "site": row.get("site"), "it_mw": row.get("it_mw"),
            "mw_basis": row.get("mw_basis"), "customer": row.get("customer"), "contract": row.get("contract"),
            "advance_rate": row.get("advance_rate"), "ltv": row.get("ltv"), "covenants": row.get("covenants"),
            "rating": row.get("rating"), "status": row.get("status"), "confidence": row.get("confidence"),
            "notes": scrub_text(row.get("notes")) if isinstance(row.get("notes"), str) else row.get("notes"),
            "documents": [{"title": d.get("t"), "url": d.get("url") or d.get("u")} for d in docs
                          if isinstance(d, dict)],
        }
        sink.add("site", slug, "debt_facility", value, src=src, url=url, quote=row.get("gpu_count_quote"),
                 valid_from=row.get("drawn_asof") or row.get("date"))
        n += 1
    return n


def write_power_costs(sink: FactSink, arc: Archive, slugs: dict[str, str]) -> int:
    src, n = arc.src(AIWCREDIT), 0
    for p in arc.gpu.get("pwp") or []:
        slug = slugs.get(p.get("id"))
        if slug is None:
            continue
        value = {
            "it_mw": p.get("mw"), "mw_basis": p.get("mwb"), "pue": p.get("pue"), "state": p.get("st"),
            "power_price_usd_per_mwh": p.get("px"), "power_price_basis": p.get("pxb"),
            "annual_power_cost_2026_usd": usd_m(p.get("b26")), "annual_power_cost_2030_usd": usd_m(p.get("b30")),
            "annual_power_cost_2030_stress_usd": usd_m(p.get("b30s")), "contract_rent_usd": usd_m(p.get("rent")),
            "power_payer": p.get("payer"), "if_tenant_leaves": p.get("ifleaves"), "kw_per_rack": p.get("kw"),
            "liquid_cooling": p.get("liq"), "gpu_generation": p.get("gen"), "debt_usd": usd_m(p.get("debt_m")),
            "coupon_pct": p.get("cpn"), "sources": urls_in(p.get("src")) or None,
        }
        sink.add("site", slug, "power_cost_estimate", value, src=src, url=first_url(p.get("src")), judged=True)
        n += 1
    return n
