"""Entities for every party the `sites` loader meets, created in a fixed order so merges are stable.

Order: aichain tenants (the obligation map's payers) -> GPU operators (name, ticker, CIK) ->
aidebt / power.json groups -> aichain issuer sheets (tickers) -> SEC document issuers (CIKs) ->
aidebt issuer strings -> memo.chain parties. Earlier, better-identified names win the merge.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from marden.seed.sites_archive import Archive
from marden.seed.sites_entities import (
    ROLE_KIND,
    SECTOR_KIND,
    TENANT_KIND,
    TENANT_TICKERS,
    Registry,
    display_name,
    norm,
    split_parties,
)

_OPERATOR_KIND = (("GPU owner", "neocloud"), ("landlord", "developer"), ("both", "developer"))
_CAT_KIND = {"util": "utility", "abs": "spv", "proj": "spv", "bdc": "company", "corp": "company"}
_GF_KIND = {"u": "utility", "g": "power_provider"}
_SPV_WORDS = ("issuer", "spv", "trust", "borrower", "compute", "funding", "finance", "investor llc", "project")


@dataclass(frozen=True)
class ChainRow:
    """One memo.chain row with the entities its name resolved to."""

    case: str
    role: str
    name: str
    what: str | None
    basis: str | None
    ticker: str | None
    grp: str | None
    entity_ids: tuple[str, ...]


def seed_tenants(arc: Archive, reg: Registry) -> None:
    chain = arc.aichain
    own_ticker = {k: v.get("ticker") for k, v in chain["issuers"].items()}
    for t in chain["tenants"]:
        ticker = TENANT_TICKERS.get(t["id"])
        trusted = ticker is not None
        if ticker is None and t.get("own"):
            ticker = own_ticker.get(t["own"][0])
        parts = [p.strip() for p in t["name"].split(" / ")] if " / " in t["name"] else []
        eid = reg.resolve(t["name"], TENANT_KIND.get(t.get("kind"), "company"), ticker=ticker, exact=True,
                          trusted_ticker=trusted, aliases=parts)
        if eid:
            reg.tenant[t["id"]] = eid


def _operator_kind(role: str | None) -> str:
    return next((k for word, k in _OPERATOR_KIND if (role or "").startswith(word)), "company")


def seed_operators(arc: Archive, reg: Registry) -> None:
    """GPU operators: aiwc/gpu/operators.json (name, ticker, CIK) and the aiwcredit.gpu.ops profiles."""
    for op in arc.data["aiwc/gpu/operators.json"]["operators"]:
        reg.resolve(op["name"], _operator_kind(op.get("role")), ticker=op.get("ticker"), cik=op.get("cik"))
    for op in arc.data["aiwcredit.json"]["gpu"].get("ops") or []:
        reg.resolve(op.get("full") or op.get("n"), _operator_kind(op.get("role_full") or op.get("role")),
                    ticker=op.get("tk"), aliases=[op["n"]] if op.get("n") else ())


def _group_name(code: str, arc: Archive, issuers: Counter[str]) -> tuple[str, Any, str | None, bool]:
    """(name, ticker, aichain sector, curated?) for an aidebt / power.json group code. Curated
    display names (aidebt.gn, aichain sheets, power.gx) are kept as written."""
    gn = arc.data["aidebt.json"]["gn"].get(code)
    gx = arc.data["power.json"]["gx"].get(code)
    sheet = arc.aichain["issuers"].get(code)
    sector = sheet.get("sector") if sheet else None
    if gn and gn[1]:
        return gn[0], gn[1], sector, True
    if sheet:
        return sheet["name"], sheet.get("ticker"), sector, True
    if gn:
        return gn[0], None, sector, True
    if gx:
        return gx[0], gx[1], sector, True
    return issuers.most_common(1)[0][0], None, sector, False


def seed_groups(arc: Archive, reg: Registry) -> None:
    rows = arc.debt_rows.values()
    issuers: dict[str, Counter[str]] = defaultdict(Counter)
    cats: dict[str, Counter[str]] = defaultdict(Counter)
    for r in rows:
        issuers[r["grp"]][r["issuer"]] += 1
        cats[r["grp"]][r["cat"]] += 1
    gf = arc.data["power.json"].get("gf", {})
    codes = sorted(set(issuers) | set(arc.data["power.json"]["gx"]))
    for code in codes:
        name, ticker, sector, curated = _group_name(code, arc, issuers[code] or Counter({code: 1}))
        if sector:
            kind = SECTOR_KIND.get(sector, "company")
        elif code in gf:
            kind = _GF_KIND.get(gf[code], "company")
        else:
            kind = _CAT_KIND.get(cats[code].most_common(1)[0][0], "company") if cats[code] else "company"
        eid = reg.resolve(name, kind, ticker=ticker, exact=curated)
        if eid:
            reg.grp[code] = eid


def _registrants(arc: Archive) -> dict[str, str]:
    """CIK -> SEC registrant name from the archive's SEC lists (ticker file, BDC universe)."""
    out = {str(cik).lstrip("0"): name for cik, name in arc.data["watch/ref_meta.json"].get("universe") or []}
    for listing in arc.tickers.values():
        out.setdefault(listing.cik, listing.title)
    return out


def seed_documents_issuers(arc: Archive, reg: Registry) -> None:
    """SEC document issuers with their CIK. A CIK that an SEC list names as another registrant is not
    attached (library.json labels CIK 2052153, Apollo Origination II (UL) Capital Trust, 'Valor Compute')."""
    registrants = _registrants(arc)
    for rel in ("aiwc/q3x/docs.json", "aiwc/q3x/docs/library.json"):
        for d in arc.data[rel]["docs"]:
            if d.get("iss") and d.get("cik"):
                cik = str(d["cik"]).lstrip("0")
                filer = registrants.get(cik)
                if filer and not set(norm(filer).split()) & set(norm(display_name(d["iss"])).split()):
                    reg.skipped.add(f"{d['iss']} (CIK {cik} is {filer} in the SEC lists: CIK not attached)")
                    reg.resolve(d["iss"], "company")
                    continue
                reg.resolve(d["iss"], "company", cik=cik)


def _issuer_kind(name: str, group_kind: str) -> str:
    low = name.lower()
    return "spv" if any(w in low for w in _SPV_WORDS) else group_kind


def seed_debt_issuers(arc: Archive, reg: Registry) -> None:
    kinds = {r["id"]: r["kind"] for r in reg.conn.execute("SELECT id, kind FROM entity").fetchall()}
    for issuer, grp in sorted({(r["issuer"], r["grp"]) for r in arc.debt_rows.values()}):
        reg.resolve(issuer, _issuer_kind(issuer, kinds.get(reg.grp.get(grp, ""), "company")))


def seed_chain(arc: Archive, reg: Registry, cases: list[str]) -> dict[str, list[ChainRow]]:
    """memo.chain parties per case (roles kept as written; entities resolved per named party)."""
    out: dict[str, list[ChainRow]] = {}
    for case in cases:
        rows: list[ChainRow] = []
        for r in arc.memo(case).get("chain", []):
            role, name = r.get("role") or "", r.get("name") or ""
            parts = split_parties(name, role)
            kind = ROLE_KIND.get(role, "company")
            if role == "Site owner" and any(w in name.lower() for w in ("county", "parish", "city of", "authority")):
                kind = "county"
            ticker = r.get("ticker") if len(parts) == 1 else None
            ids = tuple(dict.fromkeys(e for p in parts if (e := reg.resolve(p, kind, ticker=ticker))))
            rows.append(ChainRow(case, role, name, r.get("what"), r.get("basis"), r.get("ticker"), r.get("grp"),
                                 ids))
        out[case] = rows
    return out


def seed_all(arc: Archive, reg: Registry, cases: list[str]) -> dict[str, list[ChainRow]]:
    seed_tenants(arc, reg)
    seed_operators(arc, reg)
    seed_groups(arc, reg)
    seed_documents_issuers(arc, reg)
    seed_debt_issuers(arc, reg)
    return seed_chain(arc, reg, cases)
