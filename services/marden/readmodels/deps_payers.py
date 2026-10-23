"""Dependency rules of the payers and obligations area (readmodels/views/payers*.py, payer_capacity.py,
obligations.py, leases.py), installed when `marden.readmodels.deps` is imported (see its last lines).
"""

from __future__ import annotations

from marden.readmodels.deps import _SITE_PAYERS, _many, depends, singleton, via

# A payer's page also shows the sites its own leases, compute contracts and guarantees link to, its own
# notes (issuer or its subsidiaries) and their holders, and who pays it (the end users of its tenant sites
# and the payers of edges owed to it, with their links' statements and funding facts); its capacity row
# lists the debt behind the edges it owes (sizes, rating terms, holders). The lease map shows that debt
# too, and each lessee's downgrade band (the payer_capacity output on the entity).
_OBLIGATION_PAYEE = via("SELECT to_entity_id FROM obligation WHERE id = %s")
# A payer's exposed debt leaves out debt another party's lease services (payers_exposure.py): a change to any
# edge reaches every payer owing an edge over the same instruments.
_EDGE_SHARING_OWERS = via(
    """SELECT o2.from_entity_id FROM obligation o1 JOIN obligation o2 ON o2.instrument_ids && o1.instrument_ids
        WHERE o1.id = %s"""
)
_EDGE_INSTRUMENT_OWERS = via(
    """SELECT from_entity_id FROM obligation WHERE %(id)s = ANY(instrument_ids)
       UNION SELECT o.from_entity_id FROM fact f JOIN obligation o ON o.id = f.subject_id
        WHERE f.subject_type = 'obligation' AND f.field = 'linked_instruments'
          AND f.status IN ('published', 'superseded') AND jsonb_typeof(f.value) = 'array'
          AND f.value @> jsonb_build_array(%(id)s::text)"""
)
_EDGE_SITE_OWERS_SQL = """SELECT from_entity_id FROM obligation WHERE site_slug = {site}
       UNION SELECT o.from_entity_id FROM fact f JOIN obligation o ON o.id = f.subject_id
        WHERE f.subject_type = 'obligation' AND f.field = 'linked_sites'
          AND f.status IN ('published', 'superseded') AND jsonb_typeof(f.value) = 'array'
          AND f.value @> jsonb_build_array({site}::text)"""
_EDGE_SITE_OWERS = via(_EDGE_SITE_OWERS_SQL.format(site="%(id)s"))
_INSTRUMENT_EDGE_SITE_OWERS = via(
    _EDGE_SITE_OWERS_SQL.format(site="(SELECT site_slug FROM instrument WHERE id = %(id)s)")
)
_NOTE_ISSUERS = via(
    """SELECT e.id FROM instrument i JOIN entity e ON e.id = i.issuer_entity_id WHERE i.id = %(id)s
       UNION SELECT e.parent_id FROM instrument i JOIN entity e ON e.id = i.issuer_entity_id
        WHERE i.id = %(id)s AND e.parent_id IS NOT NULL"""
)
_HOLDING_NOTE_ISSUERS = via(
    """SELECT e.id FROM holding h JOIN instrument i ON i.id = h.instrument_id JOIN entity e ON e.id = i.issuer_entity_id
        WHERE h.id = %(id)s
       UNION SELECT e.parent_id FROM holding h JOIN instrument i ON i.id = h.instrument_id
         JOIN entity e ON e.id = i.issuer_entity_id WHERE h.id = %(id)s AND e.parent_id IS NOT NULL""",
    int_id=True,  # holding.id as an integer, so the primary key serves the lookup
)
_HOLDING_EDGE_OWERS = via(
    """SELECT o.from_entity_id FROM holding h JOIN obligation o ON h.instrument_id = ANY(o.instrument_ids)
        WHERE h.id = %s""",
    int_id=True,
)
_HOLDING_ON_EDGE = via(
    """SELECT 'all' FROM holding h WHERE h.id = %s
          AND EXISTS (SELECT 1 FROM obligation o WHERE h.instrument_id = ANY(o.instrument_ids))""",
    int_id=True,
)
_CO_PARTIES = via(
    """SELECT DISTINCT b.entity_id FROM chain_party a JOIN chain_party b ON b.site_slug = a.site_slug
        WHERE a.entity_id = %s AND b.entity_id IS NOT NULL AND b.entity_id <> a.entity_id"""
)
_COUNTERPARTIES = via(
    """SELECT to_entity_id FROM obligation WHERE from_entity_id = %(id)s
       UNION SELECT from_entity_id FROM obligation WHERE to_entity_id = %(id)s"""
)


def _install_payer_deps() -> None:
    payer_views = ("payer", "payer_capacity")
    for subject_type in ("obligation", "contract"):
        _many(subject_type, payer_views, _OBLIGATION_PAYEE)  # paidBy; neocloud obligors' rows
    _many("obligation", ("payer_capacity",), _EDGE_SHARING_OWERS)  # exposed debt counted at another payer
    _many("instrument", payer_views, _EDGE_INSTRUMENT_OWERS)  # exposed debt
    _many("instrument", ("payer",), _INSTRUMENT_EDGE_SITE_OWERS)  # debt on an edge-linked site
    _many("instrument", ("payer",), _NOTE_ISSUERS)  # its own notes
    _many("instrument", ("leases",), singleton)  # dependent debt
    _many("site", ("payer",), _EDGE_SITE_OWERS)
    _many("holding", ("payer",), _HOLDING_NOTE_ISSUERS)  # note holders
    _many("holding", payer_views, _HOLDING_EDGE_OWERS)  # exposed debt holders
    _many("holding", ("payers", "leases"), _HOLDING_ON_EDGE)
    _many("entity", ("payer",), _CO_PARTIES)  # paidBy: the end user's statements
    _many("entity", ("payer", "obligation_company"), _COUNTERPARTIES)  # paidBy; node names and statuses
    depends("entity", "@model:payer_capacity", "leases", singleton)  # lessee band
    # Node statuses on the map are the parties' own links in the status model's outputs.
    depends("site", "@model:status", "obligation_company", _SITE_PAYERS)
    depends("site", "@model:status", "obligations", singleton)
    depends("site", "@model:status", "payer", _PAID_BY_SITE_PARTIES)
    depends("site", "tenant_rating", "payer", _RATED_PARTY_PAYEES)  # paidBy facts: the party's rating statement
    # Model outputs for subject 'all' (the leases model) may sit on subject_type 'series'.
    depends("series", "@model:leases", "leases", singleton)


# paidBy shows a party's worst link across all its sites: a site's new status reaches the payers its
# end user pays on other sites, and the payees of edges its chain parties owe.
_PAID_BY_SITE_PARTIES = via(
    """SELECT t.entity_id FROM chain_party b JOIN chain_party eu ON eu.entity_id = b.entity_id AND eu.role = 'end_user'
          JOIN chain_party t ON t.site_slug = eu.site_slug AND t.role = 'tenant'
        WHERE b.site_slug = %(id)s AND b.role = 'end_user' AND t.entity_id IS NOT NULL
       UNION SELECT o.to_entity_id FROM chain_party cp JOIN obligation o ON o.from_entity_id = cp.entity_id
        WHERE cp.site_slug = %(id)s"""
)
# paidBy lists each party's latest rating statement, a `tenant_rating` on a site where it is tenant: a new
# one reaches the payers that party pays (tenants of the sites it is end user of, payees of its edges).
_RATED_PARTY_PAYEES = via(
    """SELECT t.entity_id FROM chain_party r JOIN chain_party eu ON eu.entity_id = r.entity_id AND eu.role = 'end_user'
          JOIN chain_party t ON t.site_slug = eu.site_slug AND t.role = 'tenant'
        WHERE r.site_slug = %(id)s AND r.role = 'tenant' AND t.entity_id IS NOT NULL AND t.entity_id <> r.entity_id
       UNION SELECT o.to_entity_id FROM chain_party r JOIN obligation o ON o.from_entity_id = r.entity_id
        WHERE r.site_slug = %(id)s AND r.role = 'tenant'"""
)

_install_payer_deps()
