"""Dependency rules of the payment-chain cards and lender groups (readmodels/views/sites_chain.py), installed when
`marden.readmodels.deps` is imported (see its last lines).

- A lender fact on an instrument (`lender_<slug>`) is shown on every site whose debt items put that instrument in
  their package (Lighthouse reads the $38bn Vantage package recorded on Frontier), and on the Instrument detail
  of the other instruments in that package.
- A site's research records (`chain_parties`, `tranches`, `debt_items`) are the fallback lender group on the
  Instrument detail of its instruments.
"""

from __future__ import annotations

from marden.readmodels.deps import depends, singleton, via

_PACKAGE_SITES = via(
    """SELECT DISTINCT subject_id FROM fact_current
        WHERE subject_type = 'site' AND field = 'debt_items' AND value::text LIKE '%%"' || %s || '"%%'"""
)
_PACKAGE_INSTRUMENTS = via(
    """SELECT DISTINCT i.id FROM fact_current f JOIN instrument i ON i.site_slug = f.subject_id
        WHERE f.subject_type = 'site' AND f.field = 'debt_items' AND f.value::text LIKE '%%"' || %s || '"%%'"""
)
_SITE_INSTRUMENTS = via("SELECT id FROM instrument WHERE site_slug = %s")


def _install_deps() -> None:
    for view in ("site_detail", "sites", "completion"):
        keys = _PACKAGE_SITES if view == "site_detail" else singleton
        depends("instrument", "lender_", view, keys)
    depends("instrument", "lender_", "instrument_detail", _PACKAGE_INSTRUMENTS)
    for field in ("chain_parties", "tranches", "debt_items"):
        depends("site", field, "instrument_detail", _SITE_INSTRUMENTS)


_install_deps()
