"""What the payment-chain cards say about each party (site header, spec 04), from facts Marden already holds.

The status model's links (status, rule code, reason, fact) pass through unchanged; this adds, per link:

- `details`: Sourced<string> lines, each with a `label` and its own tag and source:
  - lenders: the debt itself ("Noteholders: 6.75% Sr Secured Notes 2031, $2.15B"), then the named lender group
    by role ("Agents: SMBC, BNP Paribas, ..."): the site's instruments' `lender_<slug>` facts (and those of the
    package a site's `debt_items` puts its instrument in), then the Lender / Arranger entries of `chain_parties`,
    the holders its `tranches` name and each instrument's `end_user_share.banks`. An entry with no source link
    of its own borrows the link of a filed or reported lender fact, or of a debt item, naming the same bank.
  - developer: the SPV / borrower names (instrument `issuer_name`, chain_parties Borrower / Project company /
    Issuer), the developer's completion support (`credit_support`) and its equity (`tranches`).
  - tenant: who the lease documents define the tenant to be (`tenant_definition`, `lease_parties`).
  - end user: the named end user, else why none is named (`end_user_disclosure`), else that the customer
    buys the capacity for itself.
- `party`: the named lenders when the status model's party is n/d or a placeholder ("About 20 banks").
- `assessed`: false when the status model ran no test on the link (status none) or reported an input it lacks
  for it ('lenders: market price of ...'); such a green link reads "Not fully tested", not "No flags".
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any
from urllib.parse import urlparse

from marden.core.db import Conn, Row
from marden.readmodels.context import known_facts
from marden.readmodels.views.sites_common import billions, counts_toward_debt, output_field, src

Doc = dict[str, Any]
NOTE_KINDS = frozenset({"bond", "abs", "convert", "cmbs", "muni"})
LENDER_ROLES = ("lender", "arranger", "agent", "trustee", "bookrunner", "purchaser", "noteholder", "underwriter")
SPV_ROLES = ("borrower", "project company", "issuer", "spv")
PLACEHOLDER = re.compile(r"not (named|disclosed)|^about \d+|consortium|more than|^other|banks and investors|^n/d$|"
                         r"placeholder|^(bond|note)holders\b|presumed", re.I)
# One key per bank however a source spells it (first pattern that matches wins), and its short card name.
ALIASES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("jpmorgan", "JPMorgan", ("jpmorgan", "j.p. morgan", "jp morgan", "j. p. morgan")),
    ("mufg", "MUFG", ("mufg", "mitsubishi ufj")),
    ("smbc", "SMBC", ("smbc", "sumitomo mitsui")),
    ("goldman", "Goldman Sachs", ("goldman",)),
    ("bnp", "BNP Paribas", ("bnp",)),
    ("wells", "Wells Fargo", ("wells fargo",)),
    ("socgen", "Societe Generale", ("societe generale", "socgen")),
    ("bofa", "Bank of America", ("bank of america", "bofa")),
    ("morgan_stanley", "Morgan Stanley", ("morgan stanley",)),
    ("citi", "Citi", ("citigroup", "citibank", "citi ")),
    ("deutsche", "Deutsche Bank", ("deutsche bank",)),
    ("wilmington", "Wilmington Trust", ("wilmington trust",)),
    ("credit_agricole", "Crédit Agricole", ("credit agricole",)),
)
_SUFFIX = re.compile(r"\b(group|inc|co|corp|corporation|n\.?a|sa|plc|llc|bank usa|& co)\b\.?", re.I)


# ---------------------------------------------------------------- names
def _plain(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def bank_key(name: str) -> str:
    low = _plain(name) + " "
    for key, _, pats in ALIASES:
        if any(p in low for p in pats):
            return key
    return re.sub(r"[^a-z0-9]+", " ", _SUFFIX.sub(" ", low)).strip()


def short_name(name: str) -> str:
    key = bank_key(name)
    for k, short, _ in ALIASES:
        if k == key:
            return short
    return re.sub(r"\s*\(.*?\)\s*$", "", name).strip()


def split_names(text: str) -> list[tuple[str, str | None]]:
    """'JPMorgan and MUFG (lead arrangers), Wells Fargo, SMBC' -> [(name, role-in-parens or None), ...]."""
    out: list[tuple[str, str | None]] = []
    for chunk in re.split(r",\s*(?![^()]*\))", text):
        m = re.match(r"\s*(.*?)\s*(?:\((.*?)\))?\s*$", chunk)
        names, role = (m.group(1), m.group(2)) if m else (chunk, None)
        role = re.sub(r",?\s*\b(reported|disclosed|unverified)\b", "", role or "").strip(" ,") or None
        for name in re.split(r"\s+and\s+", names):
            if name.strip():
                out.append((name.strip(), role))
    return out


def _host(url: str | None) -> str | None:
    host = urlparse(url or "").netloc.removeprefix("www.")
    return host or None


# ---------------------------------------------------------------- lender group
def _entry(name: str, role: str | None, source: Doc, instrument_ids: list[str]) -> Doc:
    return {"name": name, "role": role, "source": {**source, "value": role or name}, "instrumentIds": instrument_ids}


def _row_source(row: Row, tag: str | None = None) -> Doc:
    out = src(row) or {}
    if tag in ("F", "R", "M"):
        out["tag"] = tag
    return out


def package_ids(conn: Conn, slug: str, own: list[str]) -> list[str]:
    """The site's instruments and the packages its debt items put them in (Lighthouse's tranche is part of the
    $38bn Vantage package recorded on Frontier)."""
    row = known_facts(conn, "site", slug, ["debt_items"]).get("debt_items")
    ids = list(own)
    for item in row["value"] if row and isinstance(row["value"], list) else []:
        for iid in item.get("instrument_ids") or [] if isinstance(item, dict) else []:
            if isinstance(iid, str) and iid not in ids:
                ids.append(iid)
    return ids


def fact_lenders(conn: Conn, instrument_ids: list[str]) -> list[Doc]:
    out: list[Doc] = []
    for iid in instrument_ids:
        for field, row in known_facts(conn, "instrument", iid).items():
            value = row["value"]
            if field.startswith("lender_") and isinstance(value, dict) and value.get("name"):
                out.append(_entry(value["name"], value.get("role") or None, _row_source(row), [iid]))
    return out


def archive_lenders(conn: Conn, slug: str, instruments: list[Doc]) -> list[Doc]:
    """Lender entries of the site's research records: chain_parties, tranches, end_user_share.banks."""
    facts = known_facts(conn, "site", slug, ["chain_parties", "tranches"])
    out: list[Doc] = []
    if (row := facts.get("chain_parties")) and isinstance(row["value"], list):
        for p in row["value"]:
            role = str(p.get("role") or "") if isinstance(p, dict) else ""
            if any(r in role.lower() for r in LENDER_ROLES) and p.get("name"):
                for name, sub in split_names(str(p["name"])):
                    out.append(_entry(name, sub or role.lower(), _row_source(row, p.get("tag")), []))
    if (row := facts.get("tranches")) and isinstance(row["value"], list):
        for t in row["value"]:
            if not isinstance(t, dict) or not t.get("third_party"):
                continue
            for holder in t.get("holders") or []:
                tag = "F" if "disclosed" in str(holder) else row["tag"]
                for name, sub in split_names(str(holder)):
                    out.append(_entry(name, sub, _row_source(row, tag), [t.get("instrument_id") or ""]))
    for inst in instruments:
        share = inst["facts"].get("end_user_share") if "end_user_share" in inst["facts"] else known_facts(
            conn, "instrument", inst["id"], ["end_user_share"]).get("end_user_share")
        banks = share["value"].get("banks") if share and isinstance(share["value"], dict) else None
        if isinstance(banks, str):
            for name, sub in split_names(banks.split(";")[0]):
                out.append(_entry(name, sub, _row_source(share), [inst["id"]]))
    return out


def _link_for(conn: Conn, slug: str, entries: list[Doc]) -> None:
    """Give an entry with no source link that of a debt item whose text names the same bank."""
    row = known_facts(conn, "site", slug, ["debt_items"]).get("debt_items")
    items = [i for i in (row["value"] if row and isinstance(row["value"], list) else []) if isinstance(i, dict)]
    for e in entries:
        if e["source"].get("sourceUrl"):
            continue
        key = bank_key(e["name"])
        for item in items:
            text = _plain(f"{item.get('quote') or ''} {item.get('note') or ''}")
            if item.get("source_url") and (key in text or _plain(short_name(e["name"])) in text):
                e["source"]["sourceUrl"] = item["source_url"]
                e["source"]["sourceName"] = _host(item["source_url"])
                break


def lender_group(conn: Conn, slug: str, instruments: list[Doc]) -> list[Doc]:
    """The named lender group of a site: filed and reported lender facts first, then research records; one
    entry per bank (its best-sourced naming), placeholders ('About 20 banks') kept last."""
    own = [i["id"] for i in instruments]
    entries = fact_lenders(conn, package_ids(conn, slug, own)) + archive_lenders(conn, slug, instruments)
    seen: dict[str, Doc] = {}
    for e in entries:
        key = "~" + _plain(e["name"]) if PLACEHOLDER.search(e["name"]) else bank_key(e["name"])
        best = seen.get(key)
        if best is None or (not best["source"].get("sourceUrl") and e["source"].get("sourceUrl")):
            seen[key] = e
    out = list(seen.values())
    _link_for(conn, slug, out)
    out.sort(key=lambda e: (bool(PLACEHOLDER.search(e["name"])), _role_rank(e["role"]),
                            {"F": 0, "R": 1}.get(e["source"].get("tag"), 2)))
    return out


_ROLE_ORDER = ("administrative agent", "agent", "lead", "arranger", "bookrunner", "trustee", "purchaser")


def _role_rank(role: str | None) -> int:
    text = (role or "").lower()
    return next((i for i, w in enumerate(_ROLE_ORDER) if w in text), len(_ROLE_ORDER))


def lenders_label(group: list[Doc], n: int = 4) -> str | None:
    """'SMBC, BNP Paribas, Goldman Sachs, MUFG +3': the named banks, agents and leads first."""
    names = list(dict.fromkeys(short_name(e["name"]) for e in group if not PLACEHOLDER.search(e["name"])))
    if not names:
        return None
    return ", ".join(names[:n]) + (f" +{len(names) - n}" if len(names) > n else "")


def _role_word(role: str | None) -> str:
    text = (role or "").lower()
    for word, label in (("trustee", "Trustee"), ("agent", "Agents"), ("lead", "Lead banks"), ("arranger", "Arrangers"),
                        ("bookrunner", "Bookrunners"), ("purchaser", "Initial purchasers"), ("holder", "Holders"),
                        ("fund", "Holders")):
        if word in text:
            return label
    return "Lenders"


def lender_details(instruments: list[Doc], group: list[Doc]) -> list[Doc]:
    lines: list[Doc] = []
    counted = sorted((i for i in instruments if counts_toward_debt(i)),
                     key=lambda i: -((i["facts"].get("amount_usd") or {}).get("value") or 0))
    for inst in counted[:2]:
        row = inst["facts"]["amount_usd"]
        size = billions(row["value"])
        label = "Noteholders" if inst.get("kind") in NOTE_KINDS else "Lenders"
        amount = f"${size:.2f}B" if size is not None and size < 10 else f"${size:.1f}B"
        lines.append({**src(row), "label": label, "value": f"{inst['name']}, {amount}"})
    groups: dict[tuple[str, str | None], list[Doc]] = {}
    for e in group:
        groups.setdefault((_role_word(e["role"]), e["source"].get("sourceUrl")), []).append(e)
    for (label, _), members in groups.items():
        names = ", ".join(dict.fromkeys(short_name(m["name"]) if not PLACEHOLDER.search(m["name"]) else m["name"]
                                        for m in members))
        lines.append({**members[0]["source"], "label": label, "value": names})
    return lines


def instrument_lenders(conn: Conn, inst: Row) -> list[Doc]:
    """Fallback for an instrument whose own lender facts name nobody: the package's lender facts, then the
    site's research records, as the Lender shape ({name, role, source}); roles note where they come from."""
    slug = inst.get("site_slug")
    if not slug:
        return []
    site_insts = [{"id": inst["id"], "facts": {}}]
    pkg = [i for i in package_ids(conn, slug, [inst["id"]]) if i != inst["id"]]
    entries = fact_lenders(conn, pkg) if pkg else []
    entries += [e for e in archive_lenders(conn, slug, site_insts)
                if not e["instrumentIds"] or e["instrumentIds"] in ([inst["id"]], [""])]
    seen: dict[str, Doc] = {}
    for e in entries:
        key = "~" + _plain(e["name"]) if PLACEHOLDER.search(e["name"]) else bank_key(e["name"])
        seen.setdefault(key, e)
    out = list(seen.values())
    _link_for(conn, slug, out)
    for e in out:
        if e["instrumentIds"] and e["instrumentIds"][0] in pkg:
            e["role"] = f"{e['role'] or 'lender'}, across the package this loan is part of"
            e["source"]["value"] = e["role"]
    out.sort(key=lambda e: (bool(PLACEHOLDER.search(e["name"])), _role_rank(e["role"])))
    return [{"name": e["name"], "role": e["role"], "source": e["source"]} for e in out]


# ---------------------------------------------------------------- developer, tenant, end user
def _parties(conn: Conn, slug: str) -> tuple[Row | None, list[dict]]:
    row = known_facts(conn, "site", slug, ["chain_parties"]).get("chain_parties")
    return row, [p for p in (row["value"] if row and isinstance(row["value"], list) else []) if isinstance(p, dict)]


def developer_details(conn: Conn, slug: str, instruments: list[Doc], developer: str | None) -> list[Doc]:
    lines: list[Doc] = []
    issuers = []
    for inst in instruments:
        if (row := known_facts(conn, "instrument", inst["id"], ["issuer_name"]).get("issuer_name")) and \
                isinstance(row["value"], str) and row["value"] not in {r["value"] for r in issuers}:
            issuers.append({**src(row), "label": "Issuer", "value": re.sub(r"\s*\(.*\)$", "", row["value"])})
    lines += issuers[:2]
    row, parties = _parties(conn, slug)
    shown = {_plain(i["value"]) for i in issuers}
    for p in parties:
        role = str(p.get("role") or "")
        if role.lower() in SPV_ROLES and p.get("name") and _plain(str(p["name"])) not in shown:
            label = "SPV" if role.lower() != "project company" else "Project companies"
            lines.append({**_row_source(row, p.get("tag")), "label": label, "value": str(p["name"])})
            shown.add(_plain(str(p["name"])))
    facts = known_facts(conn, "site", slug, ["credit_support", "tranches"])
    support = facts.get("credit_support")
    words = {w for w in re.findall(r"[a-z]{4,}", _plain(developer or ""))}
    if support and isinstance(support["value"], dict) and words & set(re.findall(r"[a-z]{4,}",
                                                                                  _plain(str(support["value"].get("provider") or "")))):
        kind = str(support["value"].get("type") or "credit support")
        lines.append({**src(support), "label": kind[:1].upper() + kind[1:], "value": str(support["value"]["provider"])})
    if (tr := facts.get("tranches")) and isinstance(tr["value"], list):
        for t in tr["value"]:
            if isinstance(t, dict) and "equity" in str(t.get("name") or "").lower() and t.get("amount_usd"):
                who = ", ".join(str(h) for h in t.get("holders") or [])
                size = billions(t["amount_usd"])
                lines.append({**src(tr), "label": "Equity", "value": f"${size:g}B" + (f" ({who})" if who else "")})
                break
    return lines


def tenant_details(conn: Conn, slug: str) -> list[Doc]:
    facts = known_facts(conn, "site", slug, ["tenant_definition", "lease_parties"])
    out = []
    if row := facts.get("tenant_definition"):
        out.append({**src(row), "label": "Lessee"})
    if row := facts.get("lease_parties"):
        out.append({**src(row), "label": "Lease"})
    return out


def end_user_link(conn: Conn, slug: str, link: Doc) -> Doc:
    """An end-user link the status model has no party for: the party research names, else why none is named."""
    if link.get("status") != "none":
        return link
    row, parties = _parties(conn, slug)
    named = next((p for p in parties if str(p.get("role") or "").lower() == "end user" and p.get("name")), None)
    if named:
        return {**link, "party": str(named["name"]),
                "details": [{**_row_source(row, named.get("tag")), "label": "End user", "value": str(named.get("what")
                                                                                                    or named["name"])}]}
    disclosure = known_facts(conn, "site", slug, ["end_user_disclosure"]).get("end_user_disclosure")
    if disclosure:
        return {**link, "party": "Not named", "details": [{**src(disclosure), "label": "Why"}]}
    buyer = next((p for p in parties if str(p.get("role") or "").lower() == "customer" and p.get("name")), None)
    if buyer:
        return {**link, "party": "The customer itself",
                "details": [{**_row_source(row, buyer.get("tag")), "label": str(buyer["name"]),
                             "value": str(buyer.get("what") or "Buys the capacity for its own use")}]}
    return {**link, "party": "Not named"}


# ---------------------------------------------------------------- the four links
def _assessed(link: Doc, missing: list[str]) -> Doc:
    prefix = {"end_user": "end_user:", "tenant": "tenant:", "developer": "developer:", "lenders": "lenders:"}[link["role"]]
    lacks = [m.split(":", 1)[1].strip() for m in missing if isinstance(m, str) and m.startswith(prefix)]
    if link.get("status") == "none":
        return {**link, "assessed": False}
    if link.get("status") == "green" and lacks:
        return {**link, "assessed": False, "statusLabel": "Not fully tested",
                "reason": f"{link.get('reason') or ''}. Not tested for lack of: {'; '.join(lacks)}".lstrip(". ")}
    return {**link, "assessed": True}


def is_placeholder(party: str | None) -> bool:
    return not party or party in ("n/d", "—") or bool(PLACEHOLDER.search(party))


def decorate_chain(conn: Conn, slug: str, chain: list[Doc], status_out: Row | None, instruments: list[Doc]) -> list[Doc]:
    missing = output_field(status_out, "missing") or []
    out: list[Doc] = []
    for link in chain:
        link = dict(link)
        role = link["role"]
        if role == "lenders":
            group = lender_group(conn, slug, instruments)
            if is_placeholder(link.get("party")) and (label := lenders_label(group)):
                link["party"] = label
            elif is_placeholder(link.get("party")) and instruments:
                link["party"] = "Noteholders" if any(i.get("kind") in NOTE_KINDS for i in instruments) else "Lenders"
            link["details"] = lender_details(instruments, group)
        elif role == "developer":
            link["details"] = developer_details(conn, slug, instruments, link.get("party"))
        elif role == "tenant":
            link["details"] = tenant_details(conn, slug)
        elif role == "end_user":
            link = end_user_link(conn, slug, link)
        if not link.get("details"):
            link.pop("details", None)
        out.append(_assessed(link, missing if isinstance(missing, list) else []))
    return out
