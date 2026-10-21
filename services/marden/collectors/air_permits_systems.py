"""The public air permit system of every state where Marden tracks a power plant, and whether `air_permits` reads it.

Status: built (an adapter reads it) - blocked (robots.txt or terms forbid automated access) - unreachable (a bot
challenge, an access-denied wall or a broken TLS chain refuses Marden's declared client; never worked around) -
v2 (public and reachable, no adapter yet) - n/a (the state's plants are reactors or contracts with no combustion
permit to look for). Checked 2026-10-21 with User-Agent "Marden research cody@marden.ai".

Blocked and unreachable systems are declared as blocked sources, so /api/sources/health shows them with the
reason; no request is made to them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from marden.core.sources import SourceSpec, declare_source, get_spec


@dataclass(frozen=True)
class System:
    state: str
    agency: str
    name: str
    url: str
    status: str
    note: str
    source_id: str | None = None


SYSTEMS: tuple[System, ...] = (
    System("TX", "TCEQ", "Central Registry Files (Texas Open Data Portal)", "https://data.texas.gov", "built",
           "Socrata API; robots allows /resource; TCEQ content public domain", "tceq_cr_open_data"),
    System("TX", "TCEQ", "Records Online", "https://records.tceq.texas.gov", "built",
           "no robots.txt; permit documents (technical reviews, notices, final actions)", "tceq_records_online"),
    System("TX", "TCEQ", "Air Permits Allowable Database, NSR project reports, CID, Central Registry query",
           "https://www2.tceq.texas.gov/airperm/", "blocked", "robots.txt 'User-agent: * Disallow: /' on www2, www14, "
           "www15, www17", "tceq_air_permits_web"),
    System("NM", "NMED", "Air Quality Bureau public notices and OPF docketed matters", "https://www.env.nm.gov/public-notices/",
           "built", "WordPress REST API; robots allows all but /wp-admin/", "nmed_aqb"),
    System("IA", "Iowa DNR", "Air Quality construction permit search (AQRR)", "https://idnraqrr.iowadnr.gov/ConPermitSearch",
           "built", "no robots.txt; issued construction permits by plant, with permit PDFs", "iowa_dnr_aqrr"),
    System("LA", "LDEQ", "Electronic Document Management System (EDMS)", "https://edms.deq.louisiana.gov/app/doc/querydef.aspx",
           "blocked", "robots.txt 'User-agent: * Disallow: /'", "ldeq_edms"),
    System("LA", "LDEQ", "Public notices", "https://www.deq.louisiana.gov/page/public-notices", "v2",
           "robots allows all but /admin; notices name permit numbers, documents link into EDMS (blocked)"),
    System("KS", "KDHE", "Bureau of Air public notices and permits", "https://www.kdhe.ks.gov", "unreachable",
           "every page, robots.txt included, answers 403 'Sorry, you have been blocked' (Cloudflare, CivicPlus)",
           "kdhe_air"),
    System("KS", "Kansas Secretary of State", "Kansas Register (KDHE air permit notices)", "https://sos.ks.gov/publications/Register/",
           "v2", "robots allows /publications; notices of proposed KDHE air construction permits (e.g. Viola Energy Center)"),
    System("TN", "Shelby County Health Department", "Pollution Control air permits (xAI Memphis)", "https://www.shelbycountytn.gov",
           "unreachable", "403 'Sorry, you have been blocked' (Cloudflare, CivicPlus) on every page", "shelby_county_air"),
    System("TN", "TDEC", "Division of Air Pollution Control permits", "https://dataviewers.tdec.tn.gov", "n/a",
           "the Tennessee plant outside Shelby County (Kairos Hermes 2) is a reactor; no combustion permit to look for"),
    System("MN", "MPCA", "Air permits and permit documents", "https://www.pca.state.mn.us/business-with-us/air-permits",
           "unreachable", "Radware 'Verifying your browser before proceeding' challenge on permit pages", "mpca_air"),
    System("IN", "IDEM", "Office of Air Quality permits online", "https://permits.air.idem.in.gov", "unreachable",
           "403 'Access is denied' to the declared client", "idem_air"),
    System("VA", "Virginia DEQ", "Air permits", "https://www.deq.virginia.gov", "unreachable",
           "Akamai 403 'Access Denied' on every page, robots.txt included", "va_deq"),
    System("PA", "PA DEP", "eFACTS (permits, plan approvals)", "https://www.ahs.dep.pa.gov/eFACTSWeb/", "unreachable",
           "TLS certificate chain incomplete ('unable to get local issuer certificate'); verification is never "
           "disabled", "pa_efacts"),
    System("PA", "PA DEP", "Regional office project files and Pennsylvania Bulletin notices",
           "https://files.dep.state.pa.us", "v2", "plan approval files (e.g. Homer City 32-00457A) and Bulletin notices; "
           "no search API"),
    System("OH", "Ohio EPA", "eDocument Search (eDocs) and Air Services", "https://edocpub.epa.ohio.gov/publicportal/edochome.aspx",
           "v2", "ASP.NET search form, reachable; no adapter yet"),
    System("ND", "NDDEQ", "Air Quality public comments and notices", "https://deq.nd.gov/AQ/PublicCom.aspx", "v2",
           "reachable list of permit-to-construct notices with PDFs; no adapter yet"),
    System("NE", "Nebraska DWEE (formerly NDEE)", "Public notices (air construction permits)",
           "https://dwee.nebraska.gov/news-events/public-notices", "v2", "reachable Drupal notice pages; no adapter yet"),
    System("OK", "Oklahoma DEQ", "Air Quality Division permits", "https://oklahoma.gov/deq.html", "v2",
           "www.deq.ok.gov redirects to oklahoma.gov; no permit search adapter yet"),
    System("MS", "MDEQ", "enSearch / permit board documents (xAI Southaven)", "https://www.mdeq.ms.gov/permits/", "v2",
           "reachable; no adapter yet"),
    System("WY", "Wyoming DEQ", "Air Quality Division new source review permits", "https://deq.wyoming.gov/aqd/new-source-review/",
           "v2", "robots allows all; no adapter yet"),
    System("WI", "Wisconsin DNR", "Air permits", "https://dnr.wisconsin.gov/topic/AirPermits", "v2", "reachable; no adapter yet"),
    System("SD", "SD DANR", "Air Quality Program permits", "https://danr.sd.gov/Environment/AirQuality/default.aspx", "v2",
           "reachable; no adapter yet"),
    System("IL", "Illinois EPA", "Bureau of Air permits", "https://epa.illinois.gov", "n/a",
           "the Illinois plants are nuclear PPAs / uprates; no combustion permit to look for"),
    System("WA", "Washington Ecology / EFSEC", "Air permits", "https://ecology.wa.gov", "n/a",
           "the Washington plant is an advanced reactor (X-energy); no combustion permit to look for"),
)

for _s in SYSTEMS:
    if _s.status in ("blocked", "unreachable") and _s.source_id and _s.source_id != "tceq_air_permits_web":
        declare_source(_s.source_id, f"{_s.agency} {_s.name}", "portal", _s.url, None,
                       f"Checked 2026-10-21. {_s.note}. Not collected: Marden does not crawl robots-disallowed paths or "
                       "pass bot challenges.", blocked=True, blocked_reason=_s.note)


def systems_for(state: str) -> list[dict[str, str | None]]:
    return [asdict(s) for s in SYSTEMS if s.state == state]


def blocked_specs() -> list[SourceSpec]:
    """The declared sources of the blocked and unreachable systems."""
    return [get_spec(s.source_id) for s in SYSTEMS if s.status in ("blocked", "unreachable") and s.source_id]
