"""`sec_nport_terms`: N-PORT positions linked to instruments that have no identifier, by their filed terms.

Rule tests on real filed lines (PGIM's 'VANTAGE ORACLE TX' / 'WI' loans of 31 Jul 2026, filed with CUSIP
000000000 and PGIM's own codes; ABS and CMBS tranches) and the instruments' own facts; no database.
"""

from __future__ import annotations

from datetime import date

from marden.collectors import sec_nport_terms as run
from marden.collectors import sec_nport_terms_match as tm

PGIM = "0000940400-26-038879"  # PGIM Total Return Bond Fund, N-PORT for 31 Jul 2026 (cited by the Vantage facts)
OTHER_PGIM = "0000940400-26-038880"  # PGIM Short Duration Multi-Sector Bond Fund, same period


def vantage(iid: str, name: str, issuer: str, coupon: str, **kw) -> tm.Profile:
    row = {"id": iid, "kind": "construction_loan", "name": name, "issuer_name": issuer, "entity_name": None}
    facts = {"coupon": coupon, "coupon_type": "floating", "maturity": "2030-04"}
    return tm.profile(row, facts, cited=[PGIM], **kw)


TX = vantage("EX-VANTAGE_ORCL-367", "VANTAGE ORACLE TX loan (PGIM-held portion of the $38B)", "Vantage Frontier (TX)",
             "Floating, 6.17%", parent="EX-VANTAGE_ORCL-366")
WI = vantage("EX-VANTAGE_ORCL-368", "VANTAGE ORACLE WI loan (PGIM-held portion of the $38B)",
             "Vantage Port Washington (WI)", "Floating, 6.19%", parent="EX-VANTAGE_ORCL-366")
PACKAGE = tm.profile({"id": "EX-VANTAGE_ORCL-366", "kind": "construction_loan", "name": "Construction debt (TX + WI)",
                      "issuer_name": "Vantage Frontier / Port Washington SPVs"},
                     {"coupon": "~SOFR+2.50% (6.17% at 31 Jul per PGIM N-PORT); two 1-yr extensions",
                      "coupon_type": "floating", "maturity": "2030-04-15"})


def pgim(name: str, code: str, rate: float, acc: str = PGIM, key: str = "") -> tm.Pos:
    return tm.Pos(accession=acc, name=name, title=name, cusip=None, isin=None, others=(code,),
                  maturity=date(2030, 4, 15), coupon_kind="Floating", rate=rate, key=key or f"{acc}:{code}")


def test_the_cited_filing_links_each_pgim_line_to_its_own_tranche():
    tx, wi = pgim("VANTAGE ORACLE TX", "B8AFB91T2", 6.17), pgim("VANTAGE ORACLE WI - CML", "B8AFB9GT6", 6.19)
    ev, ids = tm.decide([TX, WI, PACKAGE], tx, {})
    assert ev is not None and ev.instrument_id == TX.id and ids == [TX.id]
    assert "the instrument's facts cite this filing" in ev.basis
    assert "floating, 6.17% as the instrument's facts state" in ev.basis
    ev, _ = tm.decide([TX, WI, PACKAGE], wi, {})
    assert ev is not None and ev.instrument_id == WI.id  # 'WI' vs 'TX' and 6.19% vs 6.17% tell them apart


def test_one_issuer_word_is_not_enough_without_the_cited_filing_or_the_filers_code():
    other_fund = pgim("VANTAGE ORACLE WI - CML", "B8AFB9GT6", 6.19, acc=OTHER_PGIM)
    assert tm.match(WI, other_fund) is None  # only 'vantage' in common, filing not cited
    learned = tm.Profile(**{**WI.__dict__, "filer_codes": frozenset({"B8AFB9GT6"})})
    ev = tm.match(learned, other_fund)
    assert ev is not None and "filer code of a line already linked" in ev.basis


def test_a_stated_current_rate_must_agree_on_the_cited_filing():
    assert tm.match(TX, pgim("VANTAGE ORACLE TX", "B8AFB91T2", 6.40)) is None
    # a placeholder rate (unfunded lines file 0.01) is not compared
    assert tm.match(WI, pgim("VANTAGE ORACLE WI - UNFUNDED", "B8AFB9HP3", 0.01)) is not None


def test_the_tranche_wins_over_the_package_it_is_part_of():
    tranche = tm.Profile(**{**TX.__dict__, "tokens": frozenset({"vantage", "oracle"})})
    package = tm.Profile(**{**PACKAGE.__dict__, "tokens": frozenset({"vantage", "oracle"}), "states": frozenset()})
    ev, _ = tm.decide([package, tranche], pgim("VANTAGE ORACLE TX", "B8AFB91T2", 6.17), {})
    assert ev is not None and ev.instrument_id == TX.id


def test_an_unfunded_commitment_follows_its_funded_line():
    funded = pgim("VANTAGE ORACLE WI - CML", "B8AFB9GT6", 6.19, key="a")
    unfunded = pgim("VANTAGE ORACLE WI - UNFUNDED", "B8AFB9HP3", 0.01, key="b")
    assert funded.stem == "vantage oracle wi cml"
    unfunded_cml = pgim("VANTAGE ORACLE WI - CML - UNFUNDED", "B8AFB9ZZ9", 0.01, key="c")
    got = tm.follow_unfunded({"a": (funded, tm.Evidence(WI.id, []))}, [unfunded, unfunded_cml])
    assert set(got) == {"c"} and got["c"].instrument_id == WI.id  # same stem and maturity only


def abs_profile(iid: str, name: str, coupon: str, maturity: str, ctype: str = "fixed") -> tm.Profile:
    row = {"id": iid, "kind": "abs" if ctype == "fixed" else "cmbs", "name": name,
           "issuer_name": "Compass Datacenters Issuer II, LLC" if ctype == "fixed"
           else "CONE CMT 2026-DFW3 (SASB CMBS)"}
    return tm.profile(row, {"coupon": coupon, "coupon_type": ctype, "maturity": maturity})


def line(name: str, title: str, cusip: str, maturity: date, kind: str, rate: float) -> tm.Pos:
    return tm.Pos(accession="0000106444-26-000499", name=name, title=title, cusip=cusip, isin=None, others=(),
                  maturity=maturity, coupon_kind=kind, rate=rate, key=cusip)


def test_abs_tranches_link_by_issuer_coupon_maturity_and_class():
    a1 = abs_profile("EX-COMPASS-047", "II 2024-1 A1", "5.25%", "2049-02")
    a2 = abs_profile("EX-COMPASS-048", "II 2024-1 A2", "5.75%", "2049-02")
    pos = line("COMPASS DATACENTERS ISSUER II LLC", "CMPDC 2024-1A A1", "20469AAA7", date(2049, 2, 25), "Fixed", 5.25)
    ev, ids = tm.decide([a1, a2], pos, {})
    assert ev is not None and ev.instrument_id == "EX-COMPASS-047" and ids == ["EX-COMPASS-047"]
    assert "series 2024-1 class A1" in ev.basis
    # the coupon tells the classes apart even when the title omits the class
    assert tm.match(a2, line("Compass Datacenters Issuer II LLC", "Compass Datacenters Issuer II LLC", "20469AAB5",
                             date(2049, 2, 25), "Fixed", 5.75)) is not None
    # a CUSIP another instrument owns goes the identifier route
    assert tm.decide([a1], pos, {"20469AAA7": "EX-OTHER"}) == (None, ["EX-OTHER"])


def test_floating_cmbs_classes_need_the_class_or_its_learned_cusip():
    a = abs_profile("EX-CYRUSONE-122", "CONE 2026-DFW3 A", "Floating", "2043-05-15", ctype="floating")
    assert (a.series, a.tranche) == ("2026-DFW3", "A")
    class_a = line("CONE 2026-DFW3", "CONE 2026-DFW3 A", "20681AAA9", date(2043, 5, 15), "Floating", 5.9)
    class_b = line("CONE 2026-DFW3", "CONE 2026-DFW3 B", "20681AAE1", date(2043, 5, 15), "Floating", 6.4)
    bare = line("CONE Commercial Mortgage Trust 2026-DFW3", "CONE Commercial Mortgage Trust 2026-DFW3", "20681AAA9",
                date(2043, 5, 15), "Floating", 5.9)
    assert tm.match(a, class_a) is not None and tm.match(a, class_b) is None and tm.match(a, bare) is None
    learned = tm.Profile(**{**a.__dict__, "tranche_keys": frozenset({"20681AAA9"})})
    assert tm.match(learned, bare) is not None


def test_generic_words_alone_never_link():
    swch = tm.profile({"id": "EX-SWITCH-339", "kind": "cmbs", "name": "SWCH 2025-DATA F",
                       "issuer_name": "SWCH Commercial Mortgage Trust 2025-DATA"},
                      {"coupon": "S+418.9", "coupon_type": "floating", "maturity": "2042-03-15"})
    intown = line("INTOWN Mortgage Trust", "INTOWN 2025-STAY Mortgage Trust", "46117WAE2", date(2042, 3, 15),
                  "Floating", 6.0)
    assert tm.match(swch, intown) is None


def test_plan_follows_the_filer_codes_to_the_other_funds():
    positions = {
        "1": pgim("VANTAGE ORACLE WI - CML", "B8AFB9GT6", 6.19, key="1"),
        "2": pgim("VANTAGE ORACLE WI - UNFUNDED", "B8AFB9HP3", 0.01, key="2"),  # cited filing: matched directly
        "3": pgim("VANTAGE ORACLE WI - CML", "B8AFB9GT6", 6.19, acc=OTHER_PGIM, key="3"),
        "4": pgim("VANTAGE ORACLE WI - UNFUNDED", "B8AFB9HP3", 0.01, acc=OTHER_PGIM, key="4"),
        "5": pgim("VANTAGE ORACLE TX", "B8AFB91T2", 6.17, acc=OTHER_PGIM, key="5"),  # TX code never learned here
    }
    plan = run.plan_links([TX, WI], positions)
    assert {k: e.instrument_id for k, e in plan.links.items()} == {k: WI.id for k in "1234"}
    assert plan.rounds >= 2


def test_parsers():
    assert tm.accession_of("https://www.sec.gov/Archives/edgar/data/929523/000094040026038879/primary_doc.xml") == PGIM
    assert tm.deal_series("COLO 2021-2A A2") == ("2021-2", "A2")
    assert tm.deal_series("BX 2025-VLT7 C") == ("2025-VLT7", "C")
    assert tm.current_rate("~SOFR+2.50% (6.17% at 31 Jul per PGIM N-PORT); two 1-yr extensions") == 6.17
    assert tm.current_rate("SOFR+2.50%") is None
    assert tm.dera_date("15-APR-2030") == date(2030, 4, 15) and tm.dera_date("2030-04-15") == date(2030, 4, 15)
    assert tm.state_codes("Construction debt (TX + WI)") == frozenset({"TX", "WI"})
    assert run._filer_code("B8AFB9GT6") and not run._filer_code("LX276526") and not run._filer_code("US00253XAB73")
