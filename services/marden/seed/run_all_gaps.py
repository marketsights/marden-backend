"""docs/seed-data/gaps.md: the V1 sites' critical fields the seed archive does not carry (``seed all --write-docs``).

Each V1 site (``site.in_v1``) is checked through ``fact_current`` and the identity tables:

- outside_date: a published ``outside_date`` fact on the site, or on an obligation whose ``linked_sites`` names it;
- debt: an instrument with ``site_slug`` = the site and an ``amount_usd`` fact;
- lead_lenders: a ``chain_party`` row with role ``lenders`` that resolves to an entity (a label is not enough);
- power_source: a published ``power_source`` fact;
- coordinates (``site.geom``) and campus_link (a ``site_link`` row), which the power, buildings and imagery tabs need.

A gap is not filled with anything: the value stays n/d until the collector or extraction agent named in the
summary table (DATA_DICTIONARY section 4) finds it.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from marden.core import db

FIELDS = ("outside_date", "debt", "lead_lenders", "power_source", "coordinates", "campus_link")
FILL_WITH = {
    "outside_date": "Extraction agent on lease and credit agreements (8-K EX-10, lessee 10-K/10-Q lease notes; "
                    "spec 25); press is R",
    "debt": "SEC EDGAR full-text search and 8-K / credit-agreement extraction; county IRB and PILOT records for "
            "project bonds",
    "lead_lenders": "Credit-agreement extraction (lender schedules in EX-10 exhibits), issuer 8-K releases (F), "
                    "press (R)",
    "power_source": "State utility dockets, ISO/RTO large-load and queue reports, state air-permit portals "
                    "(on-site generation)",
    "coordinates": "County parcel (assessor) geometry, else the geocoded permit address (spec 17)",
    "campus_link": "Linker agent proposing a campus from coordinates and names; a person approves it (spec 17)",
}


def _short(value: Any, n: int = 60) -> str:
    text = value if isinstance(value, str) else str(value)
    text = text.replace("|", "/").replace("\n", " ")
    return text if len(text) <= n + 3 else text[:n] + "..."


def site_gaps(conn: Any) -> list[dict[str, Any]]:
    sites = conn.execute(
        "SELECT slug, legacy_group, geom IS NOT NULL AS has_geom FROM site WHERE in_v1 ORDER BY slug").fetchall()
    slugs = [s["slug"] for s in sites]
    site_facts = {(r["subject_id"], r["field"]): r["value"] for r in conn.execute(
        """SELECT subject_id, field, value FROM fact_current
            WHERE subject_type = 'site' AND subject_id = ANY(%s) AND field IN ('outside_date', 'power_source')""",
        (slugs,)).fetchall()}
    obligation_outside: dict[str, Any] = {}
    for r in conn.execute(
        """SELECT ls.value AS sites, od.value AS outside FROM fact_current ls
             JOIN fact_current od ON od.subject_type = 'obligation' AND od.subject_id = ls.subject_id
                                 AND od.field = 'outside_date'
            WHERE ls.subject_type = 'obligation' AND ls.field = 'linked_sites'""").fetchall():
        for slug in r["sites"] or []:
            obligation_outside.setdefault(slug, r["outside"])
    debts: dict[str, list[bool]] = {}
    for r in conn.execute(
        """SELECT i.site_slug, f.value IS NOT NULL AS sized FROM instrument i
             LEFT JOIN fact_current f ON f.subject_type = 'instrument' AND f.subject_id = i.id
                                     AND f.field = 'amount_usd'
            WHERE i.site_slug = ANY(%s)""", (slugs,)).fetchall():
        debts.setdefault(r["site_slug"], []).append(r["sized"])
    lenders = {r["site_slug"]: r for r in conn.execute(
        "SELECT site_slug, entity_id, label FROM chain_party WHERE role = 'lenders'").fetchall()}
    linked = {r["site_slug"] for r in conn.execute("SELECT DISTINCT site_slug FROM site_link").fetchall()}
    out = []
    for s in sites:
        slug = s["slug"]
        outside = site_facts.get((slug, "outside_date"), obligation_outside.get(slug))
        lender = lenders.get(slug)
        have = {
            "outside_date": outside is not None, "debt": any(debts.get(slug, [])),
            "lead_lenders": bool(lender and lender["entity_id"]),
            "power_source": (slug, "power_source") in site_facts, "coordinates": s["has_geom"],
            "campus_link": slug in linked,
        }
        out.append({"slug": slug, "case": s["legacy_group"], "outside_date": outside,
                    "debt": f"{len(debts.get(slug, []))} ({sum(debts.get(slug, []))})",
                    "lenders": (lender["label"] + ("" if lender["entity_id"] else " (label only)")) if lender else None,
                    "power_source": site_facts.get((slug, "power_source")),
                    "gaps": [f for f in FIELDS if not have[f]]})
    return out


def render_gaps(conn: Any) -> str:
    rows = site_gaps(conn)
    lines = [
        "# V1 site gaps after the seed load", "",
        f"Written by `python -m marden seed all --write-docs` on {date.today().isoformat()} from published facts",
        "(`fact_current`) and the identity tables. One row per V1 site. A gap is a critical field the seed archive",
        "does not carry for the site: nothing is written for it (the UI shows n/d), and it is a target for the",
        "collector or extraction agent in the summary (DATA_DICTIONARY section 4). The checks are listed in",
        "`services/marden/seed/run_all_gaps.py`; there is no site-level debt total in the seed (aiwcredit",
        "`cases[].amt_m` has no source), so `debtB` needs a sum a model makes from the instruments.", "",
        "## Summary", "", f"| Field | V1 sites missing it (of {len(rows)}) | Fill with |", "|---|---|---|",
    ]
    lines += [f"| {f} | {sum(f in r['gaps'] for r in rows)} | {FILL_WITH[f]} |" for f in FIELDS]
    lines += ["", "## Per site", "",
              "| Site | Case | Outside date | Debt instruments (with size) | Lead lenders | Power source | Gaps |",
              "|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(
            f"| {r['slug']} | {r['case']} | {_short(r['outside_date']) if r['outside_date'] is not None else 'n/d'} | "
            f"{r['debt']} | {_short(r['lenders']) if r['lenders'] else 'n/d'} | {r['power_source'] or 'n/d'} | "
            f"{', '.join(r['gaps']) or 'none'} |")
    return "\n".join(lines) + "\n"


def write_gaps(out_dir: Path) -> Path:
    with db.connection() as conn:
        text = render_gaps(conn)
    path = out_dir / "gaps.md"
    path.write_text(text, encoding="utf-8")
    return path
