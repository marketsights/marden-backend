"""CUSIP / ISIN / LoanX identifiers per instrument, from the sources price-sources.md section 4 allows.

Allowed: ``aiwcredit.ids``, the CUSIP suffix of ``NP-`` ids (aidebt), CUSIPs and LoanX codes named in the
instrument's own research text (aidebt inst/cpn, aiwcredit.pxs and pxsrc out1/out2 notes), and N-PORT
position lines (w2/agg). Never ``trace/secs*.json`` or ``trace/match.json``. ISINs from
``watch/bondq_list.json`` are kept only when an allowed source corroborates them (the ISIN body is one of
the instrument's allowed CUSIPs, or the national number aiwcredit.ids gives for an XS bond, or an N-PORT
line shows it).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from marden.seed.instruments_nport import NportRecord
from marden.seed.instruments_parse import cusip_valid, isin_valid, nsin_like, text_identifiers


@dataclass
class Identifiers:
    cusips: list[str] = field(default_factory=list)  # primary first
    isins: list[str] = field(default_factory=list)  # primary first
    loanx: list[str] = field(default_factory=list)
    nsin: str | None = None
    origins: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    nport_records: list[NportRecord] = field(default_factory=list)  # lines showing one of the CUSIPs/ISINs

    def _add(self, kind: str, value: str, origin: str) -> None:
        bucket = {"cusip": self.cusips, "isin": self.isins, "loanx": self.loanx}[kind]
        if value not in bucket:
            bucket.append(value)
        if origin not in self.origins[value]:
            self.origins[value].append(origin)

    @property
    def cusip(self) -> str | None:
        return self.cusips[0] if self.cusips else None

    @property
    def isin(self) -> str | None:
        return self.isins[0] if self.isins else None

    def join_keys(self) -> set[str]:
        return set(self.cusips) | set(self.isins) | set(self.loanx)

    def as_fact(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for kind, values in (("cusip", self.cusips), ("isin", self.isins), ("loanx", self.loanx)):
            items.extend({"type": kind, "value": v, "from": sorted(self.origins[v])} for v in values)
        if self.nsin:
            items.append({"type": "common_code", "value": self.nsin, "from": ["aiwcredit.ids"]})
        return items


@dataclass
class IdentifierReport:
    bondq_uncorroborated: list[tuple[str, str]] = field(default_factory=list)
    bondq_unknown_instrument: list[tuple[str, str]] = field(default_factory=list)
    cusip_collisions: dict[str, list[str]] = field(default_factory=dict)
    invalid_ids: list[tuple[str, str]] = field(default_factory=list)


def build_identifiers(
    rows: Mapping[str, Mapping[str, Any]],
    ids: Mapping[str, str],
    notes: Mapping[str, Iterable[str]],
    records: Iterable[NportRecord],
    bondq_list: Iterable[list[Any]],
) -> tuple[dict[str, Identifiers], IdentifierReport]:
    out: dict[str, Identifiers] = {i: Identifiers() for i in rows}
    report = IdentifierReport()
    for iid, row in rows.items():
        ident = out[iid]
        legacy = ids.get(iid)
        if legacy and cusip_valid(legacy):
            ident._add("cusip", legacy, "aiwcredit.ids")
        elif legacy and nsin_like(legacy):
            ident.nsin = legacy
        elif legacy:
            report.invalid_ids.append((iid, legacy))
        suffix = iid.rsplit("-", 1)[-1]
        if iid.startswith("NP-") and cusip_valid(suffix):
            ident._add("cusip", suffix, "aidebt.id")
        cusips, loanx = text_identifiers(row.get("inst"), row.get("cpn"))
        for c in cusips:
            ident._add("cusip", c, "aidebt.text")
        for c in loanx:
            ident._add("loanx", c, "aidebt.text")
        cusips, loanx = text_identifiers(*notes.get(iid, ()))
        for c in cusips:
            ident._add("cusip", c, "research.note")
        for c in loanx:
            ident._add("loanx", c, "research.note")

    by_key: dict[str, list[str]] = defaultdict(list)
    for iid, ident in out.items():
        for c in ident.cusips + ident.loanx:
            by_key[c].append(iid)
    report.cusip_collisions = {k: v for k, v in sorted(by_key.items()) if len(v) > 1}

    for rec in sorted(records, key=lambda r: (r.url, r.relpath, r.fund)):
        for iid in by_key.get(rec.cusip or "", []) + by_key.get(rec.other or "", []):
            ident = out[iid]
            ident.nport_records.append(rec)
            if rec.isin and isin_valid(rec.isin) and rec.isin[2:11] in ident.cusips:
                ident._add("isin", rec.isin, "nport")

    for entry in bondq_list:
        if len(entry) < 4:
            continue
        isin, iid = str(entry[1] or ""), str(entry[3] or "")
        ident = out.get(iid)
        if ident is None:
            report.bondq_unknown_instrument.append((iid, isin))
            continue
        body = isin[2:11]
        if isin_valid(isin) and (body in ident.cusips or (ident.nsin and body == ident.nsin)):
            ident._add("isin", isin, "bondq_list+corroborated")
        elif isin not in ident.isins:
            report.bondq_uncorroborated.append((iid, isin))
    for ident in out.values():
        primary = ident.cusip
        ident.isins.sort(key=lambda s: (s[2:11] != primary, not s.startswith("US"), s))
    return out, report
