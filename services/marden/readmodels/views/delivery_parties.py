"""Who is behind each site, for the delivery record (readmodels/views/delivery.py): the developer or sponsor,
the issuer of the site's debt and the tenant, grouped into one party per company.

The chain rows name parties three ways: an entity id (`chain_party.entity_id`), a free label ('Crusoe / Blue
Owl', 'QTS (Blackstone)') and, for the issuer, the instrument's issuer entity with its parent ('Cipher Compute
LLC' -> Cipher Digital). One company shows up under several entities and labels (Cipher Digital Inc. and Cipher
Digital, TeraWulf Inc. and WULF Compute LLC), so a party is matched by name against the companies below, whose
keys are the entity ids the payer pages use (oracle, meta, coreweave, iren-ltd ...). A label no company matches
becomes its own party, split on '/' and ';' with any parenthesis dropped ('Solaris Energy Infrastructure (50.1%)
/ MZX Tech (49.9%)' -> two parties). An issuer SPV no company matches is left out: its developer already
carries the site. Generic labels ('n/d', 'Colocation customers') are not parties.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from marden.core.db import Conn

ROLES = ("developer", "issuer", "tenant")

# (key, name, pattern). Order matters only for display; every pattern that matches a text adds its party.
COMPANIES: tuple[tuple[str, str, str], ...] = (
    ("oracle", "Oracle", r"\boracle\b"),
    ("meta", "Meta", r"\bmeta\b"),
    ("microsoft", "Microsoft", r"\bmicrosoft\b"),
    ("amazon-aws", "Amazon", r"\bamazon\b|\baws\b"),
    ("google", "Google", r"\bgoogle\b"),
    ("openai", "OpenAI", r"\bopenai\b"),
    ("anthropic", "Anthropic", r"\banthropic\b"),
    ("xai-spacex", "xAI", r"\bxai\b"),
    ("fluidstack", "Fluidstack", r"\bfluidstack\b"),
    ("coreweave", "CoreWeave", r"\bcoreweave\b"),
    ("core42", "Core42", r"\bcore42\b"),
    ("nscale", "Nscale", r"\bnscale\b"),
    ("nebius", "Nebius", r"\bnebius\b"),
    ("firmus", "Firmus", r"\bfirmus\b"),
    ("vantage-data-centers", "Vantage", r"\bvantage\b"),
    ("crusoe-energy-systems", "Crusoe", r"\bcrusoe\b"),
    ("blue-owl-capital", "Blue Owl", r"\bblue owl\b"),
    ("stack-infrastructure", "STACK Infrastructure", r"\bstack\b"),
    ("related-digital", "Related Digital", r"\brelated digital\b"),
    ("applied-digital-corp", "Applied Digital", r"\bapplied digital\b|\bapld\b"),
    ("core-scientific-inc", "Core Scientific", r"\bcore scientific\b"),
    ("cipher-digital-inc", "Cipher Digital", r"\bcipher\b"),
    ("terawulf-inc", "TeraWulf", r"\bterawulf\b|\bwulf\b"),
    ("iren-ltd", "IREN", r"\biren\b|\bie us hardware\b"),
    ("galaxy-digital-inc", "Galaxy Digital", r"\bgalaxy\b"),
    ("hut-8-corp", "Hut 8", r"\bhut 8\b"),
    ("qts-blackstone", "QTS (Blackstone)", r"\bqts\b"),
    ("blackstone", "Blackstone", r"\bblackstone\b"),
    ("edgeconnex-eqt", "EdgeConneX", r"\bedgeconnex\b"),
    ("cloudhq", "CloudHQ", r"\bcloudhq\b"),
    ("databank", "DataBank", r"\bdatabank\b"),
    ("valor-compute", "Valor Compute", r"\bvalor\b"),
    ("chirisa", "Chirisa", r"\bchirisa\b"),
    ("powerhouse-data-centers", "PowerHouse Data Centers", r"\bpowerhouse\b"),
)
_COMPILED = tuple((k, n, re.compile(p, re.IGNORECASE)) for k, n, p in COMPANIES)
NAMES = {k: n for k, n, _ in COMPANIES}
GENERIC = {"", "n/d", "—", "-", "colocation customers", "ig hyperscalers", "noteholders", "bondholders"}
_PARENS = re.compile(r"\s*\([^)]*\)")


@dataclass(frozen=True)
class Party:
    key: str
    name: str


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def matched(text: str | None) -> list[Party]:
    """Every listed company the text names, in list order."""
    if not text:
        return []
    return [Party(k, n) for k, n, rx in _COMPILED if rx.search(text)]


def from_label(label: str | None) -> list[Party]:
    """The parties a chain label names: the listed companies it names outside parentheses ('STACK (Blue Owl)' is
    STACK's), else each '/'- or ';'-separated name as its own party."""
    text = _PARENS.sub("", label or "").strip()
    if text.lower() in GENERIC:
        return []
    found = matched(text)
    if found:
        return found
    out: list[Party] = []
    for part in re.split(r"\s*[/;]\s*", text):
        name = part.strip(" ,.")
        if name.lower() not in GENERIC and _slug(name):
            out.append(Party(_slug(name), name))
    return out


def from_issuer(issuer_name: str | None, entity_name: str | None, parent_name: str | None) -> list[Party]:
    """The companies behind an issuer: its parent's, else the ones its own name names, with the parenthesis
    ('Abilene JV (Crusoe / Blue Owl / Primary Digital)'). Empty for an SPV that names none."""
    for text in (parent_name, entity_name):
        found = matched(_PARENS.sub("", text or ""))
        if found:
            return found
    return matched(issuer_name)


@dataclass
class SiteParties:
    """site slug -> {party key: roles}, and the party names."""

    roles: dict[str, dict[str, set[str]]]
    names: dict[str, str]

    def add(self, slug: str, role: str, parties: Iterable[Party]) -> None:
        for p in parties:
            self.roles.setdefault(slug, {}).setdefault(p.key, set()).add(role)
            self.names.setdefault(p.key, p.name)


def load(conn: Conn) -> SiteParties:
    """Developer, issuer and tenant parties of every site."""
    out = SiteParties({}, {})
    chain = conn.execute(
        """SELECT cp.site_slug, cp.role, cp.label, e.name AS entity_name, p.name AS parent_name
             FROM chain_party cp LEFT JOIN entity e ON e.id = cp.entity_id LEFT JOIN entity p ON p.id = e.parent_id
            WHERE cp.role IN ('developer', 'tenant')"""
    ).fetchall()
    for r in chain:
        named = matched(_PARENS.sub("", r["parent_name"] or "")) or matched(_PARENS.sub("", r["entity_name"] or ""))
        out.add(r["site_slug"], r["role"], named or from_label(r["label"]))
    issuers = conn.execute(
        """SELECT DISTINCT i.site_slug, i.issuer_name, e.name AS entity_name, p.name AS parent_name
             FROM instrument i LEFT JOIN entity e ON e.id = i.issuer_entity_id LEFT JOIN entity p ON p.id = e.parent_id
            WHERE i.site_slug IS NOT NULL"""
    ).fetchall()
    for r in issuers:
        out.add(r["site_slug"], "issuer", from_issuer(r["issuer_name"], r["entity_name"], r["parent_name"]))
    return out
