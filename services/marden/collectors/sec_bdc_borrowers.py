"""AI-infrastructure borrowers in BDC schedules of investments, and the text rules for SOI lines.

`BORROWERS` lists the borrowers Marden follows (the bkeys and names linked to our instruments,
sites and entities; docs/seed-data/holdings-prices.md, DATA_DICTIONARY.md "Borrower key"), most
specific first: a line belongs to the first borrower whose pattern matches its identifier text.
Each borrower names the instruments its tranches can link to (same legal borrower) and, where the
seed has one, its borrower-level `BDC-*` placeholder instrument ("link to product BDC marks by
borrower name"). `EXCLUDED` are the name collisions from the data dictionary (Vantage Specialty
Chemicals, Vantage Bidco, Oracle Vision Bidco, Meta Buyer, Galaxy Universal) plus the ones found
in the SEC data (Crusoe Bidco, NRG Controls, AQ Helios, the Jupiter refuel/buyer names, ...).
Lambda Systems, Inc. (one Robinhood Ventures line) is left out: nothing ties it to Lambda, Inc.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class Borrower:
    key: str
    name: str  # legal name written as holding.issuer_name
    pattern: re.Pattern[str]
    tranches: tuple[str, ...] = ()  # instruments of this legal borrower a line may link to
    placeholder: str | None = None  # borrower-level BDC-* instrument (loans only, no prices)
    sole: bool = False  # the one tranche is this borrower's only debt (an SPV's single facility)


def _b(key: str, name: str, pattern: str, tranches: tuple[str, ...] = (), placeholder: str | None = None,
       *, sole: bool = False) -> Borrower:
    return Borrower(key, name, re.compile(pattern, re.I), tranches, placeholder, sole)


def _ccac(numeral: str) -> str:
    return rf"core\s*weave\s+compute\s+acquisition\s+co(?:mpany)?\.?\s*,?\s*{numeral}\b"


def _ids(prefix: str, *numbers: int) -> tuple[str, ...]:
    return tuple(f"{prefix}-{n:03d}" for n in numbers)


CRWV = "BDC-CRWV-CoreWeave"
CRWV_DEBT = _ids("EX-CRWV", *range(91, 110))
VANTAGE = "BDC-VANTAGE-VantageDataCente"
VCI = "BDC-XAI_VCI-VCIAssetHoldings"
NSCALE = "BDC-NSCALE-Nscale"
FIRMUS = "BDC-FIRMUS-Firmus"
EDGECONNEX = "BDC-EDGECONNEX-EdgeConneX"
CYRUSONE = "BDC-CYRUSONE-CyrusOne"
VISTRA = "BDC-VST-Vistra"
CALPINE = "BDC-CEG-Calpine"

BORROWERS: tuple[Borrower, ...] = (
    # CoreWeave SPVs first (DDTL V-V before DDTL V; CCAC numerals are matched whole)
    _b("coreweave_ddtl_v_v", "CoreWeave Financing DDTL V-V, LLC", r"core\s*weave\s+financing\s+ddtl\s+v\s*-\s*v\b",
       ("EX-CRWV-097",), CRWV, sole=True),
    _b("coreweave_ddtl_v", "CoreWeave Financing DDTL V, LLC", r"core\s*weave\s+financing\s+ddtl\s+v\b",
       ("EX-CRWV-096",), CRWV, sole=True),
    _b("coreweave_ccac_viii", "CoreWeave Compute Acquisition Co. VIII, LLC", _ccac("viii"), ("EX-CRWV-095",), CRWV),
    _b("coreweave_ccac_vii", "CoreWeave Compute Acquisition Co. VII, LLC", _ccac("vii"), ("EX-CRWV-094",), CRWV),
    _b("coreweave_ccac_v", "CoreWeave Compute Acquisition Co. V, LLC", _ccac("v"), ("EX-CRWV-094",), CRWV),
    _b("coreweave_ccac_iv", "CoreWeave Compute Acquisition Co. IV, LLC", _ccac("iv"), ("EX-CRWV-092", "EX-CRWV-093"),
       CRWV),
    _b("coreweave_ccac_ii", "CoreWeave Compute Acquisition Co. II, LLC", _ccac("ii"), ("EX-CRWV-091",), CRWV),
    _b("coreweave", "CoreWeave, Inc.", r"core\s*weave", CRWV_DEBT, CRWV),
    _b("crusoe", "Crusoe Energy Systems LLC", r"crusoe\s+energy\s+systems", _ids("EX-CRUSOE", *range(74, 84)),
       "BDC-CRUSOE-CrusoeEnergy"),
    _b("galaxy_helios", "Galaxy Helios I, LLC", r"galaxy\s+helios", ("EX-GLXY-162",), sole=True),
    _b("retained_vantage", "Retained Vantage Data Centers Intermediate Holdco, L.P.", r"retained\s+vantage",
       ("EX-VANTAGE-361",), VANTAGE),
    _b("vantage_dc_europe", "Vantage Data Centers Europe S.a r.l.", r"vantage\s+data\s+cent(?:er|re)s?\s+europe"),
    _b("vantage_dc", "Vantage Data Centers, LLC", r"vantage\s+data\s+cent(?:er|re)s", ("EX-VANTAGE-362",), VANTAGE),
    _b("qts_project_ram", "QTS Project Ram", r"project\s+ram\b", ("EX-QTS-302",), sole=True),
    _b("vci_1", "VCI Asset Holdings 1 LLC", r"vci\s+asset\s+holdings,?\s+(?:1|one)\b", ("EX-XAI_VCI-388",), VCI),
    _b("vci_2", "VCI Asset Holdings 2 LLC", r"vci\s+asset\s+holdings,?\s+(?:2|two)\b", ("EX-XAI_VCI-389",), VCI),
    _b("vci_3", "VCI Asset Holdings 3 LLC", r"vci\s+asset\s+holdings,?\s+(?:3|three)\b", ("EX-XAI_VCI-390",), VCI),
    _b("vci", "VCI Asset Holdings LLC", r"vci\s+asset\s+holdings", _ids("EX-XAI_VCI", 388, 389, 390), VCI),
    _b("valor_compute", "Valor Compute Infrastructure L.P.", r"valor\s+compute\s+infrastructure", (),
       "BDC-XAI_VCI-ValorCompute"),
    _b("nscale_uk", "Nscale Services UK Ltd", r"nscale\s+services\s+uk", ("EX-NSCALE-205",), NSCALE),
    _b("nscale", "Nscale Global Holdings Ltd", r"\bnscale\b", _ids("EX-NSCALE", *range(206, 212)), NSCALE),
    _b("firmus_supercloud", "Firmus Supercloud Pty Ltd", r"firmus\s+supercloud", _ids("EX-FIRMUS", 141, 142, 143, 144),
       FIRMUS),
    _b("firmus", "Firmus Technologies", r"\bfirmus\b", _ids("EX-FIRMUS", 146, 147), FIRMUS),
    _b("edgeconnex_mcn", "EdgeConneX MCN US Finance Co 1, LLC", r"edgeconnex\s+mcn", ("EX-EDGECONNEX-136",),
       EDGECONNEX),
    _b("edgeconnex", "EdgeConneX", r"edgeconnex", (), EDGECONNEX),
    _b("stack_sj", "Stack SJ Holdings Parent A-I, LLC", r"stack\s+sj\s+holdings", ("EX-STACK-321",)),
    _b("ark", "Ark Data Centers, LLC", r"\bark\s+data\s+cent(?:er|re)s", _ids("EX-ARK", 19, 20, 21),
       "BDC-ARK-ArkDataCenters"),
    _b("applied_digital", "Applied Digital Corporation", r"applied\s+digital\s+corp", (), "BDC-APLD-AppliedDigital"),
    _b("together", "Together Computer, Inc.", r"together\s+computer", _ids("EX-TOGETHER", 341, 342),
       "BDC-TOGETHER-TogetherComputer"),
    _b("switch_bbf", "Switch BBF, LLC", r"switch\s+bbf", ("EX-SWITCH-340",), sole=True),
    _b("cyrusone_warehouse", "CyrusOne Revolving Warehouse", r"cyrusone\s+revolving\s+warehouse",
       ("EX-CYRUSONE-125",), CYRUSONE, sole=True),
    _b("cyrusone", "CyrusOne Holdco LLC", r"cyrusone", ("EX-CYRUSONE-124",), CYRUSONE),
    _b("flexential_issuer", "Flexential Issuer, LLC", r"flexential\s+issuer", _ids("EX-FLEXENTIAL", *range(148, 154))),
    _b("flexential", "Flexential Topco Corporation", r"flexential", ("EX-FLEXENTIAL-154",),
       "BDC-FLEXENTIAL-Flexential"),
    _b("dartpoints", "DartPoints", r"dart\s*points", (), "BDC-DARTPOINTS-DartPoints"),
    _b("dc_blox", "DC BLOX", r"\bdc\s*blox\b", (), "BDC-DCBLOX-DCBlox"),
    _b("us_signal", "US Signal Company, LLC", r"\bu\.?s\.?\s+signal\b", (), "BDC-USSIGNAL-USSignal"),
    _b("tensorwave", "TensorWave", r"tensor\s*wave", (), "BDC-TENSORWAVE-TensorWave"),
    _b("hyscaleix", "Hyscaleix Data Centers Holdings LLC", r"hyscaleix"),
    _b("north_america_dc", "North America Data Center Acquisition LLC",
       r"north\s+america\s+data\s+cent(?:er|re)\s+acquisition"),
    _b("yucca_oracle", "Yucca Growth Infrastructure, LLC", r"yucca\s+growth\s+infrastructure"),  # Oracle-tagged
    _b("oracle", "Oracle Corporation", r"\boracle\s+corp", ("EX-ORCL-271", "EX-ORCL-272")),
    # Power: plants and IPPs serving AI load that the seed tracks as BDC borrowers
    _b("calpine_ccfc", "Calpine Construction Finance Company, L.P.", r"calpine\s+construction\s+finance",
       ("MAN-CEG-CCFCFirstLienSeniorSecur",), CALPINE),
    _b("calpine", "Calpine Corporation", r"\bcalpine\b", (), CALPINE),
    _b("cogentrix", "Cogentrix Finance Holdco I, LLC", r"cogentrix", (), "BDC-COGENTRIX-Cogentrix"),
    _b("alpha_generation", "Alpha Generation, LLC", r"alpha\s+generation", (), "BDC-ALPHAGEN-AlphaGeneration"),
    _b("lightning_power", "Lightning Power, LLC", r"lightning\s+power", ("MAN-NRG-LightningTermLoandue2031",),
       "BDC-NRG-LightningPower"),
    _b("talen", "Talen Energy Supply, LLC", r"talen\s+energy",
       ("NP-TLN-67507122", "NP-TLN-87422LAV2", "NP-TLN-87422LAY6", "NP-TLN-87422LBA7"), "BDC-TLN-TalenEnergy"),
    _b("vistra_zero", "Vistra Zero Operating Company, LLC", r"vistra\s+zero", ("NP-VST-92841DAB7", "NP-VST-56060649"),
       VISTRA),
    _b("vistra_operations", "Vistra Operations Company LLC", r"vistra\s+operations",
       ("MAN-VST-SeniorSecuredTermLoanB3",), VISTRA),
    _b("nrg", "NRG Energy, Inc.", r"\bnrg\s+energy\b", ("NP-NRG-62937NBC0", "NP-NRG-62937NBF3")),
    _b("invenergy_thermal", "Invenergy Thermal Operating I LLC", r"invenergy", (), "BDC-INVENERGY-Invenergy"),
    _b("hunterstown", "Hunterstown Generation, LLC", r"hunterstown", (), "BDC-HUNTERSTOWN-Hunterstown"),
)
BY_KEY = {b.key: b for b in BORROWERS}

EXCLUDED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(p, re.I)) for label, p in (
        ("Vantage Specialty Chemicals", r"vantage\s+specialty"),
        ("Vantage Bidco", r"vantage\s+bidco"),
        ("Vantage Insurance Partners", r"vantage\s+insurance"),
        ("Oracle Vision Bidco", r"oracle\s+vision"),
        ("Meta Buyer", r"meta\s+buyer"),
        ("Galaxy Universal", r"galaxy\s+universal"),
        ("AQ Helios Buyer", r"aq\s+helios"),
        ("Crusoe Bidco", r"crusoe\s+bidco"),
        ("NRG Controls", r"nrg\s+controls"),
        ("Jupiter (Refuel, Borrower, Buyer, Purchaser, Blocker)", r"jupiter\s+(?:refuel|borrower|buyer|purchaser|"
         r"ultimate|blocker)|gtcr/jupiter|bidco\s+jupiter"),
        ("Valor (Buyco, CI Blocker, PayTech)", r"valor\s+(?:buyco|ci\s+blocker|paytech)"),
    )
)
# One search per SOI row before the per-borrower patterns (a big monthly file has ~190k rows).
PREFILTER = re.compile("|".join([*(f"(?:{b.pattern.pattern})" for b in BORROWERS),
                                 *(f"(?:{p.pattern})" for _, p in EXCLUDED)]), re.I)
# Lines an unconsolidated JV or senior loan program holds (the BDC discloses the JV's schedule).
_JV = re.compile(r"[|,]\s*[^|,]*\b(?:JV|Joint Venture|Senior Loan Program|SLF)\b")


def excluded_by(text: str) -> str | None:
    return next((label for label, pat in EXCLUDED if pat.search(text)), None)


def held_by_jv(identifier: str) -> bool:
    return bool(_JV.search(identifier))


def match_borrower(text: str) -> Borrower | None:
    return next((b for b in BORROWERS if b.pattern.search(text)), None)


# ---------------------------------------------------------------- text rules for linking
_LOANX = re.compile(r"\bLX\d{6}\b")
_SPREAD = re.compile(
    r"(?:\b(?:\d+M\s+)?(?:SOFR|S|L|LIBOR|E|EURIBOR|SONIA|SN|P|PRIME|BSBY|CDOR|C)(?:\s*\([A-Z0-9]+\))?\s*\+\s*"
    r"|\bspread\s+)(\d+(?:\.\d+)?)\s*(%|bps?\b)?", re.I)
_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_MATURITY_TEXT = re.compile(r"(?:maturity|matures|due)(?:\s+date)?\s*:?\s*(\d{1,2}/\d{1,2}/\d{4})", re.I)


def loanx_ids(text: str) -> frozenset[str]:
    return frozenset(m.upper() for m in _LOANX.findall(text))


def spread_bp_from_text(text: str) -> Decimal | None:
    """'SOFR + 6.000%' -> 600, 'S+450' -> 450, 'Spread 4.50%' -> 450; None when absent or unclear."""
    m = _SPREAD.search(text)
    if not m:
        return None
    value, unit = Decimal(m.group(1)), (m.group(2) or "").lower()
    if unit == "%" or (not unit and value <= 25):
        value *= 100
    return value if 0 < value <= 3000 else None


def maturity_from_text(text: str) -> date | None:
    m = _MATURITY_TEXT.search(text)
    found = m.group(1) if m else None
    if found is None:
        dates = _DATE.findall(text)
        if not dates or "|" not in text:
            return None
        found = "/".join(dates[-1])  # pipe-separated lines end with the maturity date
    mm, dd, yyyy = (int(x) for x in found.split("/"))
    try:
        return date(yyyy, mm, dd)
    except ValueError:
        return None


_EQUITY = re.compile(
    r"\b(?:lp|llc|membership|partnership)\s+interests?\b|\bclass\s+[a-z0-9]+\s+(?:common\s+)?(?:units|interests?)\b"
    r"|\bpreferred\s+(?:equity|stock|shares|units)\b|\bseries\s+[a-z0-9-]+\s+preferred\b"
    r"|\bcommon\s+(?:stock|equity|units|shares)\b|\bwarrants?\b|\bequity\s+(?:investments?|securities)\b"
    r"|[|,]\s*equity\b|\btype of investment equity\b", re.I)


def line_class(text: str) -> str:
    """'equity', 'revolver', 'security' (bonds, notes, ABS) or 'loan' from a schedule line's text."""
    t = text.lower()
    if _EQUITY.search(t):
        return "equity"
    if "revolv" in t:
        return "revolver"
    if re.search(r"\bbonds?\b|\bnotes?\b|asset[- ]backed|\bstructured\b|\bclass [a-z0-9-]+\b", t):
        return "security"
    return "loan"


def facility_type(text: str) -> str | None:
    t = text.lower()
    if "revolv" in t:
        return "revolver"
    if re.search(r"delayed[- ]draw|\bddtl\b|\(dd\)", t):
        return "ddtl"
    if "term loan" in t:
        return "term"
    return None


def decimal_or_none(text: str | None) -> Decimal | None:
    if text is None or not text.strip():
        return None
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        return None
    return value if value.is_finite() else None
