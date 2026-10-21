"""The field vocabulary each specialist may write (docs/agents.md "Field vocabulary").

Fields reuse the names the seed loaders and collectors already write (`SELECT subject_type, field,
count(*) FROM fact GROUP BY 1, 2`); a field marked `new=True` is introduced by the agents and documented
here and in docs/agents.md. Value types:

    usd mw pct bp ratio years count number   a JSON number in the unit named (USD, not millions)
    date                                     ISO date YYYY-MM-DD
    month                                    YYYY-MM, or a full ISO date when the document gives the day
    text                                     a short string in the document's words
    bool                                     true / false
    list                                     a JSON array of strings
    object                                   a JSON object (keys listed in the definition)

`material`: a value that can change a chain status, cushion band or fire an alert (spec 00 "Materiality
and alerts"); such facts always wait for a person. `event`: the event kind written next to the fact
(published with it), so the Events / Opposition tabs and the alert engine see it.
"""

from __future__ import annotations

from dataclasses import dataclass

NUMERIC = {"usd": "USD", "mw": "MW", "pct": "pct", "bp": "bp", "ratio": "x", "years": "years", "count": "count",
           "number": None}
VTYPES = frozenset({*NUMERIC, "date", "month", "text", "bool", "list", "object"})
SUBJECT_KINDS = ("entity", "instrument", "site", "campus", "building", "plant", "unit", "obligation", "queue_project")
RANGES = {"usd": (0, 5e12), "mw": (0, 20_000), "pct": (0, 200), "bp": (0, 5_000), "ratio": (0, 100),
          "years": (0, 99), "count": (0, 10_000_000)}


@dataclass(frozen=True)
class Field:
    name: str
    kinds: tuple[str, ...]
    vtype: str
    desc: str
    new: bool = False
    material: bool = False
    event: str | None = None

    @property
    def unit(self) -> str | None:
        if self.vtype in NUMERIC:
            return NUMERIC[self.vtype]
        return "date" if self.vtype in ("date", "month") else None


def F(name: str, kinds: str, vtype: str, desc: str, **kw: object) -> Field:
    assert vtype in VTYPES, vtype
    ks = tuple(kinds.split())
    assert all(k in SUBJECT_KINDS for k in ks), kinds
    return Field(name, ks, vtype, desc, **kw)  # type: ignore[arg-type]


DEBT = [
    F("amount_usd", "instrument", "usd", "Facility size or principal amount of the instrument (total, all tranches)."),
    F("commitment_usd", "instrument", "usd", "Total lender commitments of a delayed-draw or revolving facility."),
    F("drawn_usd", "instrument", "usd", "Amount drawn / outstanding under the facility at the stated date."),
    F("tranches", "instrument", "object", "Tranches: {\"tranches\": [{\"name\", \"amount_usd\", \"coupon\", "
      "\"maturity\"}]} exactly as listed; amounts must add to the facility size.", new=True),
    F("coupon", "instrument", "text", "Coupon or rate as written, e.g. '7.00%' or 'Term SOFR + 5.50%'."),
    F("coupon_type", "instrument", "text", "'fixed' or 'floating'."),
    F("coupon_benchmark", "instrument", "text",
      "Floating-rate benchmark: 'SOFR', 'Term SOFR', 'EURIBOR', 'base rate'."),
    F("coupon_spread_bp", "instrument", "bp", "Margin over the benchmark for benchmark (SOFR) loans, in basis points "
      "(5.50% = 550)."),
    F("coupon_rate_pct", "instrument", "pct", "Fixed interest rate in percent per annum (7.375% = 7.375)."),
    F("sofr_floor_pct", "instrument", "pct", "Floor on the benchmark rate, percent (0.00% floor = 0).", new=True),
    F("issue_price_pct", "instrument", "pct", "Issue price / OID as percent of par (issued at 98.5% = 98.5).",
      new=True),
    F("upfront_fee_pct", "instrument", "pct", "Upfront or closing fee, percent of commitments.", new=True),
    F("undrawn_fee_pct", "instrument", "pct", "Commitment / undrawn / unused fee, percent per annum.", new=True),
    F("maturity", "instrument", "month", "Final maturity date; the month when only the month is stated "
      "(\"matures in September 2031\" -> 2031-09)."),
    F("commitment_termination_date", "instrument", "month", "Last date (or month) loans may be drawn.", new=True),
    F("closing_date", "instrument", "date", "Date the agreement or indenture was entered into / dated as of.",
      new=True),
    F("amortization", "instrument", "text", "Amortization / repayment schedule as written."),
    F("min_dscr", "instrument", "ratio", "Minimum debt service coverage ratio covenant (1.35x = 1.35).", new=True),
    F("max_ltv_pct", "instrument", "pct", "Maximum loan-to-value covenant, percent.", new=True),
    F("min_liquidity_usd", "instrument entity", "usd", "Minimum liquidity covenant, USD.", new=True),
    F("covenant_test", "instrument", "object", "Any other financial covenant: {\"metric\", \"threshold\", "
      "\"tested\", \"consequence\"}."),
    F("collateral_text", "instrument", "text", "What secures the debt, in the document's words (GPUs, equity pledge, "
      "real estate, contracts, accounts)."),
    F("guarantors", "instrument", "list", "Guarantors by name (parent guarantee, subsidiary guarantors).", new=True),
    F("recourse", "instrument", "text", "Recourse: 'full recourse to <parent>', 'limited recourse', 'non-recourse "
      "to <sponsor>' as written.", new=True),
    F("borrower", "instrument", "text", "Borrower or issuer legal name as written.", new=True),
    F("admin_agent", "instrument", "text", "Administrative agent (or trustee for notes).", new=True),
    F("collateral_agent", "instrument", "text", "Collateral agent / security trustee.", new=True),
    F("arrangers", "instrument", "list", "Lead arrangers / bookrunners / initial purchasers.", new=True),
    F("lenders", "instrument", "list", "Lenders named as parties (not 'the lenders party thereto').", new=True),
    F("seniority", "instrument", "text", "Ranking: 'senior secured first lien', 'senior unsecured', 'subordinated'."),
    F("events_of_default", "instrument", "list", "Events of default listed (short labels in the document's words).",
      new=True),
    F("cross_default_threshold_usd", "instrument", "usd", "Cross-default / cross-acceleration threshold amount.",
      new=True),
    F("change_of_control", "instrument", "text", "What a change of control triggers (event of default, put at 101%).",
      new=True),
    F("use_of_proceeds", "instrument", "text", "Stated use of proceeds.", new=True),
    F("gpu_model", "instrument", "text", "GPU model financed or pledged (GB300, H100) when named."),
    F("customer_text", "instrument", "text", "Customer contracts the debt finances, as written."),
]

PERIODIC = [
    F("total_debt_usd", "entity", "usd", "Total debt (principal) at the balance-sheet date."),
    F("long_term_debt_usd", "entity", "usd", "Long-term debt, total, at the balance-sheet date."),
    F("debt_current_usd", "entity", "usd", "Current portion of debt."),
    F("cash_usd", "entity", "usd", "Cash and cash equivalents at the balance-sheet date."),
    F("liquidity_text", "entity", "text", "Management's liquidity statement (sufficiency for the next 12 months).",
      new=True),
    F("going_concern", "entity", "bool", "true when the filing states substantial doubt about the ability to continue "
      "as a going concern; false only when it says such doubt was alleviated.", new=True, material=True),
    F("debt_maturities", "entity", "object", "Scheduled principal maturities by year: {\"2026\": usd, ...}.", new=True),
    F("purchase_obligation_usd", "entity", "usd", "Purchase commitments / obligations disclosed."),
    F("remaining_performance_obligation_usd", "entity", "usd", "Remaining performance obligations (backlog)."),
    F("outstanding_usd", "instrument", "usd", "Principal outstanding on one named facility or note at period end.",
      new=True),
    F("drawn_usd", "instrument", "usd", "Amount drawn under a named facility at period end."),
    F("covenant_compliance", "entity instrument", "text", "Statement on covenant compliance (in compliance / waiver "
      "obtained / breach).", new=True, material=True),
]

LEASE = [
    F("lease_tenant", "site campus", "text", "Tenant / customer (lessee or offtaker) as written."),
    F("lease_landlord", "site campus", "text", "Landlord / lessor / developer counterparty as written.", new=True),
    F("lease_mw", "site campus obligation", "mw", "Critical IT MW leased or contracted, as the document states it: "
      "the total, or one phase's MW when the document states MW per phase (the note says which phase).",
      new=True),
    F("lease_term_years", "site campus", "years", "Initial term in years."),
    F("term_years", "obligation", "years", "Initial term in years."),
    F("rent_start", "site campus", "month", "Rent commencement (date or month).", material=True),
    F("start", "obligation", "month", "Contract start / rent commencement.", material=True),
    F("outside_date", "site campus obligation", "month", "Outside / long-stop delivery date after which the tenant "
      "may terminate or get credits.", material=True),
    F("lease_annual_rent_usd", "site campus", "usd", "Annual rent or contract value per year."),
    F("annual_usd", "obligation", "usd", "Annual payment under the contract."),
    F("lease_tcv_usd", "site campus", "usd", "Total contract value over the term."),
    F("total_usd", "obligation", "usd", "Total contract value over the term."),
    F("lease_renewal", "site campus", "text", "Extension / renewal options as written."),
    F("lease_termination", "site campus", "text", "Termination rights (for delay, convenience, default) as written.",
      material=True),
    F("force_majeure", "site campus obligation", "text", "Force majeure clause or a force majeure notice as written.",
      new=True, material=True, event="notice"),
    F("lease_guarantee", "site campus", "text", "A guarantee or credit support in the lease, saying whose "
      "obligations it covers (tenant parent guarantee, landlord guaranty, letter of credit)."),
    F("take_or_pay", "site campus obligation", "text", "Take-or-pay / minimum payment terms as written.", new=True),
]

POWER = [
    F("capacity_mw", "plant", "mw", "Generating capacity of the plant (nameplate unless stated)."),
    F("expected_cod", "plant", "month", "Expected commercial operation date.", material=True),
    F("mw_requested", "queue_project", "mw", "MW requested in the interconnection / large-load request."),
    F("proposed_cod", "queue_project", "month", "Proposed in-service / COD of the queue request."),
    F("withdrawn_date", "queue_project", "date", "Date the request was withdrawn.", material=True),
    F("large_load_request", "site campus", "text", "Large-load interconnection request as written (MW, utility, "
      "status)."),
    F("expected_energization", "site campus", "month", "Expected energization date as stated.", material=True),
    F("energized_mw", "site campus entity", "mw", "MW energized / approved to energize at the stated date.", new=True),
    F("power_status", "site campus", "text", "Status of the power supply (approved, pending, denied, withdrawn).",
      material=True),
    F("power_contract", "site campus", "text", "Power supply / large-load / ESA agreement as written."),
    F("upgrade_cost_usd", "site campus plant queue_project", "usd", "Network upgrade or interconnection cost.",
      new=True),
    F("cost_responsibility", "site campus plant queue_project", "text", "Who pays the upgrades / collateral.",
      new=True),
    F("docket_order", "entity site campus plant", "object", "An order or decision: {\"docket\", \"order\", \"date\", "
      "\"outcome\" (granted, denied, dismissed, deficient, approved)}.", new=True, material=True, event="docket"),
    F("milestone", "site campus plant queue_project", "object", "A dated power milestone: {\"name\", \"date\", "
      "\"status\"}.", new=True, material=True, event="milestone"),
]

PERMITS = [
    F("air_permit", "plant site", "object", "Air permit: {\"number\", \"agency\", \"type\" (PSD, NSR, PBR, Title V), "
      "\"status\", \"date\"}.", event="permit"),
    F("permitted_units", "plant site", "object", "Permitted generating units: {\"units\": [{\"oem\", \"model\", "
      "\"count\", \"rating_mw_each\", \"fuel\"}]}.", new=True),
    F("capacity_mw", "plant", "mw", "Total permitted generating capacity, MW."),
    F("emission_limits", "plant", "object", "Emission limits, or proposed annual emissions, as the document "
      "states them: {\"basis\": \"limit\" or \"proposed emissions, tons per year\", \"pollutant\": \"amount as "
      "written\"}.", new=True),
    F("hearing_date", "plant site campus", "date", "Public hearing or comment deadline date.", new=True,
      event="permit"),
    F("contractors", "site campus", "list", "General contractor / EPC / builders named."),
    F("construction_stage", "site campus building", "text", "Construction status as written.", material=True),
    F("pct_complete", "site campus building", "pct", "Percent complete when stated."),
    F("certificate_of_occupancy", "site campus building", "date", "Date a certificate of occupancy was issued.",
      new=True),
    F("mechanics_lien", "site campus", "object", "Mechanic's lien: {\"claimant\", \"amount_usd\", \"date\"}.",
      new=True, material=True, event="notice"),
    F("zoning", "site campus", "text", "Zoning / land-use approval status as written.", new=True),
]

LOCAL = [
    F("opposition_event", "site campus plant", "object", "A step before a local body or court about the project, "
      "whether opposition or approval: {\"kind\" (vote, moratorium, lawsuit, hearing, petition, appeal, "
      "presentation, appointment), \"body\", \"date\", \"outcome\" (\"on the agenda\" for an agenda), "
      "\"case_number\"}.",
      new=True, material=True, event="opposition"),
    F("tax_abatement", "site campus", "object", "Tax abatement / IRB / PILOT: {\"body\", \"amount_usd\", \"term\", "
      "\"date\", \"status\"}.", new=True, event="opposition"),
    F("hearing_date", "site campus plant", "date", "Scheduled hearing or vote date.", new=True, event="opposition"),
    F("lawsuit", "site campus plant entity", "object", "A lawsuit: {\"court\", \"case_number\", \"parties\", "
      "\"claims\", \"status\", \"filed\"}.", new=True, material=True, event="opposition"),
]

RATINGS = [
    F("rating", "instrument entity", "text", "Current rating as written with agency, e.g. \"Moody's Ba3 (stable)\".",
      material=True),
    F("rating_action", "instrument entity", "object", "A rating action: {\"agency\", \"rating\", \"outlook\", "
      "\"watch\", \"action\" (assigned, upgraded, downgraded, affirmed, placed on watch, withdrawn), \"date\"}.",
      new=True, material=True, event="rating"),
    F("credit_event", "instrument entity", "object", "A credit event: {\"kind\" (default, forbearance, amendment, "
      "waiver, acceleration, reservation of rights, draw stop), \"date\", \"description\"}.", new=True,
      material=True, event="notice"),
    F("tenant_rating", "site", "text", "The tenant's rating as written.", material=True),
]

GPU = [
    F("gpu_model", "instrument", "text", "GPU model financed or deployed (GB300, H100) when named."),
    F("gpu_count", "entity campus site instrument", "count", "Number of GPUs deployed / delivered / financed.",
      new=True),
    F("active_it_mw", "entity", "mw", "Active (energized, in use) power across the company, MW."),
    F("contracted_mw", "entity", "mw", "Contracted power across the company, MW."),
    F("delivered_mw", "site campus", "mw", "MW delivered to the tenant at the site."),
    F("chip_types", "campus", "list", "GPU / accelerator types at the campus."),
    F("backlog_rpo_usd", "entity", "usd", "Revenue backlog / remaining performance obligations."),
    F("top_customers", "entity", "list", "Named major customers."),
    F("gpu_rental_rate_usd_hr", "entity instrument", "number", "Contracted or quoted rental rate, USD per GPU-hour.",
      new=True),
]

AREA_FIELDS: dict[str, list[Field]] = {
    "debt": DEBT, "periodic": PERIODIC, "lease": LEASE, "power": POWER, "permits": PERMITS, "local": LOCAL,
    "ratings": RATINGS, "gpu": GPU,
}


def fields_for(area: str) -> dict[str, Field]:
    return {f.name: f for f in AREA_FIELDS[area]}


def all_fields() -> dict[tuple[str, str], Field]:
    """(area, field) -> Field."""
    return {(a, f.name): f for a, fs in AREA_FIELDS.items() for f in fs}


def new_fields() -> list[tuple[str, Field]]:
    return [(a, f) for a, fs in AREA_FIELDS.items() for f in fs if f.new]
