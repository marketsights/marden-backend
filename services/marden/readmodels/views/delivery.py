"""Read model `delivery` (all): GET /api/delivery, DeliveryPage (contract apps/api/src/contracts/delivery.ts;
method docs/methods/delivery-record-v0.1.md). Who keeps having construction delays.

For each company behind the tracked sites (developer or sponsor, issuer of the site's debt, tenant;
delivery_parties), across all its sites (the Completion list): the milestones, buildings and in-service dates
delivered on time, late or still open against the first target a source stated (delivery_sites), the average and
worst slip in months, the schedule pushes of the last 90 days, the share of its sites with a lease deadline that
the Marden projection now puts behind it, and the debt on those sites. Worst first (average slip, then pushes,
then worst slip), each row against the median developer.

Every count and slip is worked by Marden from sourced records (tag M); each item keeps the records it compares
(their own F/R/M tags and links), the debt keeps its source. Reads the stored Completion and Schedule documents
of the build day (`completion`, `site_schedule`), the status and event-impact outputs and building facts.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable
from typing import Any

from marden.core.db import Conn
from marden.readmodels.registry import builder
from marden.readmodels.views import catalysts as cat
from marden.readmodels.views import delivery_parties as dp
from marden.readmodels.views import delivery_sites as ds
from marden.readmodels.views.instrument_inputs import day, num

Doc = dict[str, Any]
LABEL = "Marden delivery record"
METHOD = "delivery v0.1"
METHOD_URL = "docs/methods/delivery-record-v0.1.md"
PUSH_DAYS = 90
JUDGED = ("on_time", "late", "open", "open_late")


def m(value: Any) -> Doc:
    return {
        "value": value,
        "tag": "M",
        "asOf": day().isoformat(),
        "sourceName": LABEL,
        "sourceUrl": METHOD_URL,
        "methodVersion": METHOD,
    }


def stored(conn: Conn, view: str, key: str) -> Any:
    row = conn.execute(
        "SELECT doc FROM read_model WHERE view = %s AND key = %s AND as_of <= %s ORDER BY as_of DESC LIMIT 1",
        (view, key, day()),
    ).fetchone()
    return row["doc"] if row else None


def deadline_of(row: Doc) -> Doc | None:
    """The lease deadline (outside date) and the projection's cushion against it; None without a deadline."""
    proj = row.get("projection") or {}
    out_date = (proj.get("outsideDate") or {}).get("value")
    if not out_date:
        return None
    cushion = proj.get("cushionMonths") if isinstance(proj.get("cushionMonths"), dict) else None
    months = num((cushion or {}).get("value"))
    return {
        "outsideDate": str(out_date),
        "assumed": bool(proj.get("outsideDateAssumed")),
        "cushionMonths": cushion,
        "behind": None if months is None else months < 0,
    }


def site_doc(conn: Conn, row: Doc, roles: Iterable[str]) -> Doc:
    site = {"slug": row["slug"], "name": row["name"]}
    schedule = stored(conn, "site_schedule", row["slug"])
    items = ds.milestone_items(site, schedule) + ds.building_items(conn, site)
    if (d1 := ds.in_service_item(conn, site)) is not None:
        items.append(d1)
    debt = row.get("debtB") if isinstance(row.get("debtB"), dict) else None
    return {
        "slug": row["slug"],
        "name": row["name"],
        "href": f"/sites/{row['slug']}",
        "roles": sorted(roles),
        "debtB": debt,
        "deadline": deadline_of(row),
        "items": items,
        "pushes": ds.pushes(conn, site, schedule),
    }


def debt_sum(sites: list[Doc]) -> tuple[Doc | None, str | None]:
    parts = [s["debtB"] for s in sites if s.get("debtB") and num(s["debtB"].get("value")) is not None]
    if not parts:
        return None, "No debt amount on file for their sites"
    worst = max(parts, key=lambda p: ds.TAG_RANK.get(p.get("tag"), 2))
    names = "; ".join(
        f"{s['name']} ${num(s['debtB']['value']):,.2f}B ({s['debtB'].get('sourceName')})"
        for s in sites
        if s.get("debtB") and num(s["debtB"].get("value")) is not None
    )
    out = {
        "value": round(sum(num(p["value"]) or 0 for p in parts), 3),
        "tag": worst.get("tag") or "M",
        "asOf": max(str(p.get("asOf") or "") for p in parts),
        "sourceName": f"Sum of {len(parts)} sites' debt: {names}",
    }
    if len(parts) == 1 and parts[0].get("sourceUrl"):
        out["sourceUrl"] = parts[0]["sourceUrl"]
    return out, None


def party_row(key: str, name: str, roles: set[str], sites: list[Doc], payers: set[str]) -> Doc:
    items = [i for s in sites for i in s["items"]]
    slips = [i for i in items if i["slipMonths"] is not None and i["outcome"] in JUDGED]
    pushes = [p for s in sites for p in s["pushes"]]
    recent = [p for p in pushes if ds.within(p, PUSH_DAYS)]
    with_deadline = [s for s in sites if s["deadline"] is not None and s["deadline"]["behind"] is not None]
    behind = [s for s in with_deadline if s["deadline"]["behind"]]
    debt, debt_why = debt_sum(sites)
    worst = max(slips, key=lambda i: i["slipMonths"], default=None)
    why: Doc = {}
    if not slips:
        why["slip"] = "No milestone of theirs has both a first stated target and a current or actual date"
    if not with_deadline:
        why["behindDeadline"] = "No lease deadline (outside date) on file for their sites"
    if debt_why:
        why["debtB"] = debt_why
    assumed = sum(1 for s in with_deadline if s["deadline"]["assumed"])
    return {
        "key": key,
        "name": name,
        "roles": [r for r in dp.ROLES if r in roles],
        "payerHref": f"/payers/{key}" if key in payers else None,
        "sitesTracked": m(len(sites)),
        "onTime": m(sum(1 for i in items if i["outcome"] == "on_time")),
        "late": m(sum(1 for i in items if i["outcome"] == "late")),
        "open": m(sum(1 for i in items if i["outcome"] in ("open", "open_late"))),
        "openLate": sum(1 for i in items if i["outcome"] == "open_late"),
        "unjudged": sum(1 for i in items if i["outcome"] == "done_unjudged"),
        "avgSlipMonths": m(round(statistics.fmean(i["slipMonths"] for i in slips), 1) if slips else None),
        "worstSlipMonths": m(worst["slipMonths"] if worst else None),
        "worst": {
            "siteName": worst["siteName"],
            "what": worst["what"],
            "href": worst["href"],
            "atLeast": worst["atLeast"],
        }
        if worst
        else None,
        "judged": len(slips),
        "pushes90d": m(len(recent)),
        "behindDeadline": m(round(len(behind) / len(with_deadline), 3) if with_deadline else None),
        "behindCount": len(behind),
        "deadlineSites": len(with_deadline),
        "assumedDeadlines": assumed,
        "debtB": debt,
        "why": why,
        "vsPeers": None,
        "rank": None,
        "sites": sorted(sites, key=lambda s: -max((i["slipMonths"] or 0 for i in s["items"]), default=0)),
    }


def peers(rows: list[Doc]) -> Doc:
    devs = [
        r["avgSlipMonths"]["value"]
        for r in rows
        if "developer" in r["roles"] and r["avgSlipMonths"]["value"] is not None
    ]
    median = round(statistics.median(devs), 1) if devs else None
    for r in rows:
        v = r["avgSlipMonths"]["value"]
        if v is None or median is None:
            r["vsPeers"] = {
                "deltaMonths": None,
                "text": "No slip to compare" if median is not None else "No developer median yet",
            }
        else:
            delta = round(v - median, 1)
            r["vsPeers"] = {
                "deltaMonths": delta,
                "text": "In line with the median developer"
                if abs(delta) < 0.5
                else f"{abs(delta):.1f} months {'worse' if delta > 0 else 'better'} than the median developer "
                f"({median:.1f})",
            }
    return {"medianAvgSlipMonths": m(median), "developers": len(devs)}


def rank_key(r: Doc) -> tuple:
    avg = r["avgSlipMonths"]["value"]
    return (
        avg is None,
        -(avg or 0),
        -r["pushes90d"]["value"],
        -(r["worstSlipMonths"]["value"] or 0),
        -r["sitesTracked"]["value"],
        r["name"],
    )


@builder("delivery", many=True)
def build_delivery(conn: Conn):
    completion = stored(conn, "completion", "all") or {}
    rows = [r for r in completion.get("rows") or [] if isinstance(r, dict) and r.get("slug")]
    parties = dp.load(conn)
    payers = {r["key"] for r in conn.execute("SELECT DISTINCT key FROM read_model WHERE view = 'payer'").fetchall()}
    by_party: dict[str, list[tuple[Doc, set[str]]]] = {}
    for row in rows:
        for key, roles in parties.roles.get(row["slug"], {}).items():
            by_party.setdefault(key, []).append((row, roles))
    out: list[Doc] = []
    cache: dict[str, Doc] = {}
    for key, entries in by_party.items():
        sites = []
        for row, roles in entries:
            if row["slug"] not in cache:
                cache[row["slug"]] = site_doc(conn, row, [])
            base = cache[row["slug"]]
            sites.append({**base, "roles": sorted(roles)})
        out.append(party_row(key, parties.names[key], set().union(*(r for _, r in entries)), sites, payers))
    summary = peers(out)
    out.sort(key=rank_key)
    for i, r in enumerate(out, 1):
        r["rank"] = i if r["avgSlipMonths"]["value"] is not None else None
    named = {row["slug"] for entries in by_party.values() for row, _ in entries}
    unnamed = [r["name"] for r in rows if r["slug"] not in named]
    gaps = [f"No developer, issuer or tenant company named for: {', '.join(unnamed)}"] if unnamed else []
    undated = [r["name"] for r in rows if not cache.get(r["slug"], {}).get("items")]
    if undated:
        gaps.append(f"{len(undated)} of {len(rows)} sites have no dated milestone or building target in any source")
    yield (
        "all",
        {
            "asOf": day().isoformat(),
            "catalysts": cat.delivery_catalysts(
                conn,
                list(cache.values()),
                {slug: sorted(parties.names[k] for k in roles) for slug, roles in parties.roles.items()},
                lambda p: ds.within(p, PUSH_DAYS),
            ),
            "rows": out,
            "peers": summary,
            "pushDays": PUSH_DAYS,
            "gaps": gaps,
            "method": {"label": LABEL, "methodVersion": METHOD, "sourceUrl": METHOD_URL},
        },
    )
