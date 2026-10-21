"""Registered grid regions: interconnections, the seven US ISO/RTOs, Canadian system operators and European TSOs.

These rows carry no boundary and need no fetch; the US ISO/RTO and interconnection boundaries are added by
`hifld_grid`, the balancing authorities by `eia930_ref` / `eia861_ba` / `hifld_grid`. Every other row is a
registration only: `meta.coverage = 'registered, no collector (V2)'` until a queue collector exists for it
(docs/grid-coverage.md).

Sources of the lists (checked 2026-10-21):
- US ISO/RTOs: the seven features of the HIFLD "Independent System Operators" layer (EIA ids below) and the
  ISO/RTO balancing authorities of EIA-930 (CISO, ERCO, ISNE, MISO, NYIS, PJM, SWPP, SWPW).
- Canada: EIA-930 reference table balancing authorities in region CAN (AESO, BCHA, HQT, IESO, MHEB, NBSO,
  SPC) plus Nova Scotia Power, which EIA-930 does not list.
- Europe: ENTSO-E "Member Companies" (https://www.entsoe.eu/about/inside-entsoe/members/: 40 TSOs from 36
  countries and observer member Moldelectrica) plus NESO, Great Britain's system operator, which left
  ENTSO-E with the UK's EU exit. Synchronous areas: Baltic TSOs joined Continental Europe on 2025-02-09,
  Ukrenergo and Moldelectrica on 2022-03-16; Energinet spans Continental Europe (DK1) and Nordic (DK2).

`python -m marden collect grid_registry` (no network); the grid collectors also call `ensure_registry`.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from marden import cli
from marden.collectors.grid_store import Region, UpsertStats, slug, upsert
from marden.core import db
from marden.core.db import Conn

CHECKED = "2026-10-21"
V2 = "registered, no collector (V2)"
ENTSOE_URL = "https://www.entsoe.eu/about/inside-entsoe/members/"

EASTERN, WESTERN, ERCOT, QUEBEC = (
    "interconnection:eastern", "interconnection:western", "interconnection:ercot", "interconnection:quebec")
CONTINENTAL, NORDIC, GB, IRELAND = (
    "interconnection:continental-europe", "interconnection:nordic", "interconnection:great-britain",
    "interconnection:ireland")

# id -> (name, country or None, short label used as the `interconnection` fact value, countries)
INTERCONNECTIONS: dict[str, tuple[str, str | None, str, tuple[str, ...]]] = {
    EASTERN: ("Eastern Interconnection", None, "Eastern", ("US", "CA")),
    WESTERN: ("Western Interconnection", None, "Western", ("US", "CA", "MX")),
    ERCOT: ("ERCOT Interconnection", "US", "ERCOT", ("US",)),
    QUEBEC: ("Québec Interconnection", "CA", "Québec", ("CA",)),
    CONTINENTAL: ("Continental Europe synchronous area", None, "Continental Europe", ()),
    NORDIC: ("Nordic synchronous area", None, "Nordic", ("NO", "SE", "FI", "DK")),
    GB: ("Great Britain synchronous area", "GB", "Great Britain", ("GB",)),
    IRELAND: ("Ireland and Northern Ireland synchronous area", None, "Ireland", ("IE", "GB")),
}
US_INTERCONNECTIONS = (EASTERN, WESTERN, ERCOT)

# short -> (name, HIFLD/EIA id of the ISO feature, EIA-930 BA codes, parent, website)
US_ISOS: dict[str, tuple[str, str, tuple[str, ...], str | None, str]] = {
    "ERCOT": ("Electric Reliability Council of Texas", "5723", ("ERCO",), ERCOT, "https://www.ercot.com"),
    "PJM": ("PJM Interconnection", "14725", ("PJM",), EASTERN, "https://www.pjm.com"),
    "MISO": ("Midcontinent Independent System Operator", "56669", ("MISO",), EASTERN, "https://www.misoenergy.org"),
    # SPP's RTO West (EIA-930 BA SWPW, active 2026-04-01) is in the Western Interconnection: SPP spans two.
    "SPP": ("Southwest Power Pool", "59504", ("SWPP", "SWPW"), None, "https://www.spp.org"),
    "CAISO": ("California Independent System Operator", "2775", ("CISO",), WESTERN, "https://www.caiso.com"),
    "NYISO": ("New York Independent System Operator", "13501", ("NYIS",), EASTERN, "https://www.nyiso.com"),
    "ISO-NE": ("ISO New England", "13434", ("ISNE",), EASTERN, "https://www.iso-ne.com"),
}
NON_RTO = "non-RTO"

# (id, kind, name, parent, EIA-930 code or None, website)
CANADA: tuple[tuple[str, str, str, str, str | None, str], ...] = (
    ("iso:AESO", "iso_rto", "Alberta Electric System Operator (AESO)", WESTERN, "AESO", "https://www.aeso.ca"),
    ("iso:IESO", "iso_rto", "Independent Electricity System Operator (IESO, Ontario)", EASTERN, "IESO",
     "https://www.ieso.ca"),
    ("ba:HQT", "balancing_authority", "Hydro-Québec TransÉnergie", QUEBEC, "HQT",
     "https://www.hydroquebec.com/transenergie/en/"),
    ("ba:BCHA", "balancing_authority", "BC Hydro (British Columbia Hydro and Power Authority)", WESTERN, "BCHA",
     "https://www.bchydro.com"),
    ("ba:SPC", "balancing_authority", "SaskPower (Saskatchewan Power Corporation)", EASTERN, "SPC",
     "https://www.saskpower.com"),
    ("ba:MHEB", "balancing_authority", "Manitoba Hydro", EASTERN, "MHEB", "https://www.hydro.mb.ca"),
    ("ba:NBSO", "balancing_authority", "NB Power (New Brunswick System Operator)", EASTERN, "NBSO",
     "https://www.nbpower.com"),
    ("ba:nova-scotia-power", "balancing_authority", "Nova Scotia Power", EASTERN, None, "https://www.nspower.ca"),
)

# (country, abbreviation, company as listed by ENTSO-E, website as linked by ENTSO-E, synchronous area)
ENTSOE_MEMBERS: tuple[tuple[str, str, str, str, str | None], ...] = (
    ("AL", "OST", "OST sh.a – Albanian Transmission System Operator", "https://www.ost.al/en/", CONTINENTAL),
    ("AT", "APG", "Austrian Power Grid AG", "http://www.apg.at/", CONTINENTAL),
    ("AT", "VUEN", "Vorarlberger Übertragungsnetz GmbH", "http://www.vuen.at/", CONTINENTAL),
    ("BA", "NOS BiH", "Nezavisni operator sustava u Bosni i Hercegovini", "http://www.nosbih.ba/", CONTINENTAL),
    ("BE", "Elia", "Elia System Operator SA", "http://www.elia.be/", CONTINENTAL),
    ("BG", "ESO", "Electroenergien Sistemen Operator EAD", "http://www.tso.bg/", CONTINENTAL),
    ("CH", "Swissgrid", "Swissgrid ag", "http://www.swissgrid.ch/swissgrid/en/home.html", CONTINENTAL),
    ("CY", "Cyprus TSO", "Cyprus Transmission System Operator", "https://tsoc.org.cy/", None),
    ("CZ", "ČEPS", "ČEPS a.s.", "https://www.ceps.cz/en/homepage", CONTINENTAL),
    ("DE", "TransnetBW", "TransnetBW GmbH", "http://www.transnetbw.de/de", CONTINENTAL),
    ("DE", "TenneT DE", "TenneT TSO GmbH", "http://www.tennet.eu/de/home.html", CONTINENTAL),
    ("DE", "Amprion", "Amprion GmbH", "http://www.amprion.net/en/", CONTINENTAL),
    ("DE", "50Hertz", "50Hertz Transmission GmbH", "http://www.50hertz.com/de/", CONTINENTAL),
    ("DK", "Energinet", "Energinet", "https://en.energinet.dk/", None),
    ("EE", "Elering", "Elering AS", "http://elering.ee/", CONTINENTAL),
    ("ES", "REE", "Red Eléctrica de España S.A.", "http://www.ree.es/en", CONTINENTAL),
    ("FI", "Fingrid", "Fingrid Oyj", "https://www.fingrid.fi/en/", NORDIC),
    ("FR", "RTE", "Réseau de Transport d'Electricité", "http://www.rte-france.com/fr/", CONTINENTAL),
    ("GR", "IPTO", "Independent Power Transmission Operator S.A.", "https://www.admie.gr/en", CONTINENTAL),
    ("HR", "HOPS", "HOPS d.d.", "https://www.hops.hr/en/Home/Index", CONTINENTAL),
    ("HU", "MAVIR", "MAVIR Magyar Villamosenergia-ipari Átviteli Rendszerirányító Zrt.", "https://www.mavir.hu/web/mavir-en",
     CONTINENTAL),
    ("IE", "EirGrid", "EirGrid plc", "http://www.eirgrid.com/", IRELAND),
    ("IS", "Landsnet", "Landsnet hf", "https://www.landsnet.is/english/", None),
    ("IT", "Terna", "Terna - Rete Elettrica Nazionale SpA", "http://www.terna.it/", CONTINENTAL),
    ("LT", "Litgrid", "Litgrid AB", "https://www.litgrid.eu/index.php?lang=2", CONTINENTAL),
    ("LU", "Creos Luxembourg", "Creos Luxembourg S.A.", "http://www.creos-net.lu/index.php?id=241", CONTINENTAL),
    ("LV", "AST", "AS Augstsprieguma tīkls", "https://www.ast.lv/en", CONTINENTAL),
    ("ME", "CGES", "Crnogorski Elektroprenosni Sistem AD", "http://cges.me/en/", CONTINENTAL),
    ("GB", "SONI", "System Operator for Northern Ireland Ltd", "http://www.soni.ltd.uk/", IRELAND),
    ("NL", "TenneT NL", "TenneT TSO B.V.", "http://www.tennet.org/", CONTINENTAL),
    ("NO", "Statnett", "Statnett SF", "https://www.statnett.no/en/", NORDIC),
    ("MK", "MEPSO", "Transmission System Operator of the Republic of North Macedonia",
     "https://www.mepso.com.mk/index.php/en/", CONTINENTAL),
    ("PL", "PSE", "Polskie Sieci Elektroenergetyczne S.A.", "https://www.pse.pl/web/pse-eng", CONTINENTAL),
    ("PT", "REN", "Rede Eléctrica Nacional, S.A.", "https://www.ren.pt/en-GB", CONTINENTAL),
    ("RO", "Transelectrica", "C.N. Transelectrica S.A.", "https://www.transelectrica.ro/en/web/tel/home", CONTINENTAL),
    ("RS", "EMS", "Akcionarsko društvo Elektromreža Srbije", "https://ems.rs/en/pocetna-english/", CONTINENTAL),
    ("SE", "Svenska kraftnät", "Svenska Kraftnät", "https://www.svk.se/en/", NORDIC),
    ("SI", "ELES", "ELES, d.o.o.", "https://www.eles.si/en", CONTINENTAL),
    ("SK", "SEPS", "Slovenská elektrizačná prenosová sústava, a.s.", "https://www.sepsas.sk/en/", CONTINENTAL),
    ("UA", "Ukrenergo", "National Power Company Ukrenergo", "https://ua.energy/en/", CONTINENTAL),
)
ENTSOE_OBSERVERS: tuple[tuple[str, str, str, str, str | None], ...] = (
    ("MD", "Moldelectrica", "Moldelectrica", "https://moldelectrica.md/ro/", CONTINENTAL),
)
NESO = ("GB", "NESO", "National Energy System Operator (NESO), Great Britain", "https://www.neso.energy", GB)


def tso_id(abbr: str) -> str:
    return f"tso:{slug(abbr)}"


def regions() -> list[Region]:
    """Every registered row, parents first."""
    out: list[Region] = []
    for rid, (name, country, label, countries) in INTERCONNECTIONS.items():
        meta: dict[str, Any] = {"label": label, "countries": list(countries)}
        if rid not in US_INTERCONNECTIONS:
            meta["coverage"] = V2
        out.append(Region(rid, "interconnection", name, country, None, meta))
    for short, (name, eia_id, ba_codes, parent, website) in US_ISOS.items():
        out.append(Region(f"iso:{short}", "iso_rto", name, "US", parent, {
            "label": short, "eia_id": eia_id, "ba_codes": list(ba_codes), "website": website}))
    for rid, kind, name, parent, code, website in CANADA:
        meta = {"coverage": V2, "website": website, "registry": {"checked": CHECKED, "basis": (
            "EIA-930 reference table, region CAN" if code else "not in EIA-930; NERC NPCC member")}}
        if kind == "iso_rto":
            meta["label"] = rid.split(":", 1)[1]
        if code:
            meta["eia930_code"] = code
        out.append(Region(rid, kind, name, "CA", parent, meta))
    for rows, member in ((ENTSOE_MEMBERS, "member"), (ENTSOE_OBSERVERS, "observer member")):
        for country, abbr, company, website, area in rows:
            out.append(Region(tso_id(abbr), "tso", company, country, area, {
                "coverage": V2, "label": abbr, "website": website,
                "registry": {"source": "ENTSO-E Member Companies", "url": ENTSOE_URL, "checked": CHECKED,
                             "entsoe": member}}))
    country, abbr, name, website, area = NESO
    out.append(Region(tso_id(abbr), "tso", name, country, area, {
        "coverage": V2, "label": abbr, "website": website,
        "registry": {"checked": CHECKED, "entsoe": "not a member (UK left ENTSO-E in 2021)",
                     "note": "National Grid Electricity Transmission owns the GB transmission network; NESO operates it"}}))
    return out


def ensure_registry(conn: Conn) -> dict[str, int]:
    stats = UpsertStats()
    for region in regions():
        stats.add(upsert(conn, region))
    return stats.as_dict()


@cli.action("collect", "grid_registry", help="Register interconnections, US ISO/RTOs, Canadian and European operators")
def collect_cli(args: argparse.Namespace) -> int:
    with db.transaction() as conn:
        out = ensure_registry(conn)
    print(json.dumps({"ok": True, **out}, indent=2))
    return 0
