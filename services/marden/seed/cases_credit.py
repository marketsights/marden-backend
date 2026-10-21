"""Rating and covenant triggers, contagion, OpenAI exposure, relative value and new-issue comps
(field table: marden/seed/cases.py).

- aiwc/q3x/trg.json: each tested trigger (the filed threshold with its verbatim quote, F; the
  archive's headroom reading, M), every quoted agreement term, and the rating ladder.
- aiwc/q3x/contagion.json, oa.json: legacy model tables (``model_output``) and the per-instrument
  OpenAI share (M). Their bond prices, yields, betas and loss-at-+100/+300 bp figures come from
  TRACE prices and are removed (cases_quarantine.CTG_KEYS / OA_KEYS).
- aiwc/q3x/rv.json: per-case relative value as a legacy model output, without the market price,
  yield, spread and cheap/rich call; ``rv.curves`` (TRACE yields) is not loaded.
- aiwc/q3x/comp.json: every new-issue comp as its own series ``issue_comp:<fid>`` with its issue
  date, tier and spread at issue (the inputs of the tier curves in docs/methods/pricing-v0.1.md);
  today's traded level (`now`, `chg`) is TRACE and is not loaded.
"""

from __future__ import annotations

from typing import Any

from marden.seed.cases_archive import COMP, CTG, OA, RV, TRG, UW, Archive, Parties, norm
from marden.seed.cases_quarantine import COMP_KEYS, CTG_KEYS, OA_KEYS, RV_KEYS, TABLE_KEYS, scrub_all, scrub_text, strip
from marden.seed.cases_sink import FactSink, ModelSink, first_url, usd_m
from marden.seed.common import parse_date, tag_for_url

_TILE_MARKET = ("trade", "trading", "price", "yield", "spread", " bp", "cheap", "rich", "trace")


def _clean_tiles(tiles: Any) -> list[Any]:
    """Summary tiles without the ones that report market prices, yields or spreads."""
    out = []
    for t in tiles or []:
        text = " ".join(str(x) for x in (t.values() if isinstance(t, dict) else t)).lower()
        if not any(word in text for word in _TILE_MARKET):
            out.append(t)
    return out


def _fac_key(name: Any) -> str:
    return norm(str(name or "").split("(")[0].split("—")[0])


def _term_ids(arc: Archive, rows: list[dict[str, Any]], fac: str, url: str | None) -> list[str]:
    """Instrument ids for an agreement: by facility name (trigger rows, GPU loan table), else by
    the agreement URL the trigger rows quote."""
    by_name: dict[str, list[str]] = {}
    for r in rows:
        by_name.setdefault(_fac_key(r.get("fac")), []).extend(r.get("ids") or [])
    for loan in arc.data[UW].get("loans") or []:
        by_name.setdefault(_fac_key(loan.get("loan")), []).extend(loan.get("ids") or [])
    ids = by_name.get(_fac_key(fac))
    if not ids and url:
        ids = [i for r in rows if (r.get("q") or {}).get("url") == url for i in r.get("ids") or []]
    return arc.known(ids or [])


def write_trigger_rows(sink: FactSink, arc: Archive, rows: list[dict[str, Any]], stats: dict[str, Any]) -> None:
    src = arc.src(TRG)
    for r in strip(rows, TABLE_KEYS):
        ids, q = arc.known(r.get("ids")), r.get("q") or {}
        if not ids:
            stats["skipped"].setdefault("trigger row: instrument not loaded", []).append(r.get("fac"))
        test = {"facility": r.get("fac"), "metric": r.get("trig"), "threshold": r.get("thr"),
                "consequence": r.get("cons"), "section": q.get("sec")}
        headroom = {"facility": r.get("fac"), "metric": r.get("trig"), "current": r.get("now"),
                    "headroom_pct": r.get("head"), "headroom_low_pct": r.get("head_lo"), "status": r.get("st"),
                    "calculation": r.get("calc"), "watch": r.get("live"), "notches": r.get("notch"),
                    "computed_automatically": r.get("auto"), "quarterly_ebitda_hurdle_usd": usd_m(r.get("hurdle_q"))}
        for iid in ids:
            sink.add("instrument", iid, "covenant_test", test, src=src, url=q.get("url"), quote=q.get("quote"))
            sink.add("instrument", iid, "covenant_headroom", headroom, src=src, url=first_url(r.get("src")),
                     judged=True, valid_from=r.get("asof"))


def write_agreement_terms(sink: FactSink, arc: Archive, agreements: list[dict[str, Any]],
                          stats: dict[str, Any]) -> None:
    src, rows = arc.src(TRG), arc.data[TRG].get("rows") or []
    for agreement in agreements:
        ids = _term_ids(arc, rows, agreement.get("fac"), agreement.get("url"))
        if not ids:
            stats["skipped"].setdefault("agreement terms: no loaded instrument matches", []).append(
                agreement.get("fac"))
        for t in agreement.get("terms") or []:
            value = {"agreement": agreement.get("fac"), "type": t.get("type"), "threshold": t.get("threshold"),
                     "consequence": t.get("consequence"), "section": t.get("section")}
            for iid in ids:
                sink.add("instrument", iid, "agreement_term", value, src=src, url=agreement.get("url"),
                         quote=t.get("quote"))


def write_trigger_summary(sink: FactSink, models: ModelSink, arc: Archive, parties: Parties,
                          stats: dict[str, Any]) -> None:
    trg, src = arc.data[TRG], arc.src(TRG)
    for name, rating, notches in trg.get("ratings") or []:
        eid = parties.find(name)
        if eid:
            sink.add("entity", eid, "rating_summary", {"rating": rating, "notches_above_high_yield": notches},
                     src=src, judged=True)
        else:
            stats["skipped"].setdefault("rating ladder: party not found", []).append(name)
    models.add("legacy_rating_triggers", "series", "rating_triggers", src.as_of,
               {"tiles": trg.get("tiles"), "not_public": trg.get("notfound")})


def write_contagion(sink: FactSink, models: ModelSink, arc: Archive, parties: Parties, stats: dict[str, Any]) -> None:
    ctg, src = arc.data[CTG], arc.src(CTG)
    for key, table in (ctg.get("ten") or {}).items():
        eid = parties.find(table.get("name"), ticker=key)
        if eid is None:
            stats["skipped"].setdefault("contagion tenant: party not found", []).append(key)
            continue
        models.add("legacy_contagion", "entity", eid, src.as_of,
                   {"tenant_group": key, **strip(table, CTG_KEYS), "removed": "prices, yields, durations, spread "
                    "moves, betas and loss-at-+100/+300 bp (derived from blocked price sources)"})
    oa, osrc = arc.data[OA], arc.src(OA)
    openai = parties.find("OpenAI")
    if openai:
        for label, value, note, url in (p for p in oa.get("payer") or [] if len(p) == 4):
            sink.add("entity", openai, "funding_fact", {"label": label, "value": value, "note": note},
                     src=osrc, url=url)
        summary = strip({k: v for k, v in oa.items() if k not in ("rows", "payer", "tiles")}, OA_KEYS)
        models.add("legacy_openai_exposure", "entity", openai, osrc.as_of,
                   {**summary, "tiles": _clean_tiles(oa.get("tiles"))})


def write_openai_shares(sink: FactSink, arc: Archive, rows: list[dict[str, Any]]) -> None:
    osrc = arc.src(OA)
    for r in rows:
        value = {"end_user": "OpenAI", "share": r.get("share"), "end_user_usd": usd_m(r.get("oa_m")),
                 "amount_usd": usd_m(r.get("amt_m")), "bucket": r.get("bucket"), "chain": r.get("chain"),
                 "why": r.get("why"), "rating": r.get("rating"), "maturity": r.get("mat"), "banks": r.get("banks")}
        for iid in arc.known(r.get("id")):
            sink.add("instrument", iid, "end_user_share", value, src=osrc, judged=True)


def write_relative_value(models: ModelSink, arc: Archive, stats: dict[str, Any]) -> None:
    rv, src = arc.data[RV], arc.src(RV)
    groups = {g.get("k"): g.get("n") for g in rv.get("groups") or []}
    for r in rv.get("rows") or []:
        slug = arc.slugs.get(r.get("id"))
        if slug is None:
            continue
        row = scrub_all(strip(r, RV_KEYS))
        models.add("legacy_relative_value", "site", slug, src.as_of,
                   {**row, "group": groups.get(r.get("g")), "fair_price_basis": "model price at +200 bp (px_200)",
                    "removed": "market price, yield, spread, spread after loss, value vs fair, call "
                               "(blocked price sources)"})
    stats["skipped"].setdefault("rv.curves and rv.tiles: TRACE yields and spread comparisons", []).append(
        f"{sum(len(v) for v in (rv.get('curves') or {}).values())} curve points")


def write_comps(sink: FactSink, arc: Archive, rows: list[dict[str, Any]]) -> None:
    src = arc.src(COMP)
    for r in strip(rows, COMP_KEYS):
        docs = [{"title": d.get("t"), "url": d.get("u")} for d in r.get("docs") or [] if isinstance(d, dict)]
        url = first_url([d["url"] for d in docs])
        sid, day = f"issue_comp:{r.get('fid')}", parse_date(r.get("close"))
        # Floating deals state the margin over SOFR (the document's own figure); fixed-rate spreads
        # are the archive's arithmetic: yield at issue minus the Treasury par yield (M).
        stated = r.get("bench") == "SOFR"
        sink.add("series", sid, "issue_spread_bp", r.get("bp"), src=src, url=url, unit="bp", valid_from=day,
                 tag=tag_for_url(url) if stated else "M")
        sink.add("series", sid, "tier", {"code": r.get("tier"), "label": r.get("tierl")}, src=src, url=url,
                 judged=True, valid_from=day)
        sink.add("series", sid, "issue_date", r.get("close"), src=src, url=url, valid_from=day)
        lev = r.get("lev") or {}
        terms = {
            "name": r.get("name"), "borrower": r.get("bor"), "sponsor": r.get("spon"), "type": r.get("type"),
            "type_code": r.get("traw"), "size_usd": usd_m(r.get("size")), "tenor_years": r.get("ten"),
            "maturity": r.get("mat"), "benchmark": r.get("bench"), "coupon_pct": r.get("cpn"),
            "issue_price": r.get("ipx"), "rate": r.get("rate"), "spread_method": scrub_text(r.get("how")),
            "tenant_summary": r.get("ten_s"), "rating": r.get("rt"), "rating_detail": r.get("rt_raw"),
            "leverage": lev.get("txt"), "loan_to_cost_pct": lev.get("ltc"), "collateral": r.get("coll"),
            "covenants": r.get("cov"), "contract": r.get("ctr"), "lenders": r.get("lend"), "chips": r.get("chips"),
            "case": r.get("case"), "site_slug": arc.site_of(r.get("case")), "status": r.get("status"),
            "confidence": r.get("conf"), "documents": docs,
        }
        sink.add("series", sid, "deal_terms", terms, src=src, url=url, valid_from=day)


def write_comp_summary(models: ModelSink, arc: Archive) -> None:
    comp, src = arc.data[COMP], arc.src(COMP)
    models.add("legacy_issue_comps", "series", "issue_comps", src.as_of,
               {"segments": comp.get("seg"), "note": comp.get("note"), "tiles": _clean_tiles(comp.get("tiles"))})
