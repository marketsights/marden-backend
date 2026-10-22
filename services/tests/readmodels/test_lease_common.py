"""lease_common: the per-lessee reconciliation ties to the filed total to the dollar (spec 25), capping
attribution when named leases add up to more than the filing, and the display helpers (unnamed tenant,
site names, one shared campus)."""

from datetime import date

from marden.readmodels.views.lease_common import (
    TENANT_NOT_DISCLOSED,
    lessee_label,
    reconciliation_rows,
    single_campus,
    site_docs,
)


def _fact(value, field="leases_not_commenced_usd"):
    return {"value": value, "field": field, "tag": "F", "valid_from": date(2026, 6, 30),
            "source_url": "https://www.sec.gov/x.htm", "source_name": "10-Q"}


def _sourced(row, value):
    return {"value": value, "tag": row["tag"], "asOf": row["valid_from"].isoformat(), "sourceUrl": row["source_url"]}


def _lease(lessee, total, status="Signed, not started", kind="Lease", attribution="Filed"):
    return {"lesseeId": lessee, "kind": kind, "status": status, "attribution": attribution,
            "totalB": {"value": total, "tag": "F", "asOf": "2026-10-01"}}


def test_reconciliation_ties_to_the_filing_and_caps_over_attribution():
    facts = {
        "oracle": {"leases_not_commenced_usd": _fact(288e9),
                   "leases_not_commenced_filing": _fact(
                       {"form": "10-Q", "period": "2026-08-31", "commence": "FY27-FY29", "terms": "15 to 19 years"},
                       "leases_not_commenced_filing")},
        "coreweave": {"leases_not_commenced_usd": _fact(35.5e9)},
        "google": {},  # no total: no row
    }
    leases = [
        _lease("oracle", 5.0), _lease("oracle", None), _lease("oracle", 3.0, status="Started"),
        _lease("oracle", 300.0, kind="Compute contract"),  # not a lease: not in the lease note
        _lease("coreweave", 10.4), _lease("coreweave", 11.0), _lease("coreweave", 20.0),
    ]
    rows, notes = reconciliation_rows(facts, leases, {"oracle": "Oracle", "coreweave": "CoreWeave"}, _sourced)
    by = {r["lesseeId"]: r for r in rows}
    assert set(by) == {"oracle", "coreweave"}
    o = by["oracle"]
    assert (o["namedLeases"], o["filedOrReportedB"], o["unattributedB"]) == (1, 5.0, 283.0)
    assert o["filing"] == {"form": "10-Q", "period": "2026-08-31", "url": "https://www.sec.gov/x.htm"}
    assert o["terms"] == "15 to 19 years" and o["note"] is None
    c = by["coreweave"]  # 41.4 named > 35.5 filed: capped, residual 0, the row says why
    assert (c["namedTotalB"], c["attributedB"], c["unattributedB"], c["attributedShare"]) == (41.4, 35.5, 0, 1)
    assert c["note"] and notes and notes[0].startswith("CoreWeave:")
    for r in rows:
        assert r["attributedB"] + r["unattributedB"] == r["disclosedNotCommencedB"]["value"]
    assert [r["lessee"] for r in rows] == ["Oracle", "CoreWeave"]  # largest filed total first


def test_display_helpers():
    assert lessee_label("unnamed-tenants", {}) == TENANT_NOT_DISCLOSED
    assert lessee_label("oracle", {"oracle": "Oracle"}) == "Oracle"
    assert site_docs(["the-barn", "x"], {"the-barn": 'Saline "The Barn"'}) == [
        {"slug": "the-barn", "name": 'Saline "The Barn"'}, {"slug": "x", "name": "x"}]
    links = {"helios-1": ["galaxy-helios"], "helios-2": ["galaxy-helios"], "frontier": ["vantage-frontier"],
             "lighthouse": ["vantage-lighthouse"], "multi": ["a", "b"]}
    assert single_campus(lambda s: links.get(s, []), ["helios-1", "helios-2"]) == "galaxy-helios"
    assert single_campus(lambda s: links.get(s, []), ["frontier", "lighthouse"]) is None
    assert single_campus(lambda s: links.get(s, []), ["multi"]) is None
    assert single_campus(lambda s: links.get(s, []), []) is None
