"""Model versions out of the text the screens show (00-global "Model labels"; web src/lib/model-version.ts).

Model versions ('price v0.2', 'odds v0', 'recovery v0.2 (model v0)') stay in the data: every Sourced value
keeps its `methodVersion`, model rows keep `version`, and the Track record view (its models table) keeps
everything. They mean nothing to a reader of a number, so the prose a read model sends (method labels,
reasons, notes, 'missing' lines, captions) is written without them. `strip_doc` runs on every doc as it is
written (`rebuild.upsert_doc`), so text that model outputs pass through to a screen is covered as well:

- a parenthesis that is only a version goes: 'Default recovery assumption (price v0.2)' -> 'Default
  recovery assumption'; 'Powered-shell waterfall (recovery v0.2 (model v0))' -> 'Powered-shell waterfall';
- a version first in a parenthesis goes: '(odds v0, uncalibrated)' -> '(uncalibrated)';
- 'in v0.2', 'not in model v0' (what a version does not cover yet) read 'yet', 'not modelled yet';
- a version mid-sentence names the model: 'the default that price v0.1 assumed' -> 'the default that the
  price model assumed'; a label that is only a version names the model: 'Recovery v0.1' -> 'Recovery model';
- a method page named in prose ('pricing-v0.1 Tier curves') reads 'pricing method Tier curves'.

URLs and paths are left as they are (a link's target is not text), as are the keys that hold versions.
"""

from __future__ import annotations

import re
from typing import Any

KEEP_VIEWS = frozenset({"track_record"})  # the models table shows each model's version
KEEP_KEYS = frozenset({"methodVersion", "version", "sourceUrl", "methodUrl", "url", "href", "refs", "path"})

_V = r"v\d+(?:\.\d+)*"
_NAME = r"[A-Za-z][\w-]*"
VERSION = re.compile(rf"\b{_V}\b")
_URLISH = re.compile(r"^(?:https?://|/|docs/|[\w.-]+/)\S*$")
_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # a parenthesis that holds only versions: ' (price v0.2)', ' (recovery v0.2 (model v0))', ' (model v0)'
    (re.compile(rf"\s*\((?:{_NAME} )?{_V}(?:\s*\((?:{_NAME} )?{_V}\))?\)"), ""),
    # a version leading a parenthesis: '(odds v0, uncalibrated)'
    (re.compile(rf"\((?:{_NAME} )?{_V}(?: \((?:{_NAME} )?{_V}\))?[,;:]\s*"), "("),
    # what a version does not cover yet
    (re.compile(rf"\bnot (?:modell?ed )?in (?:the )?model {_V}\b"), "not modelled yet"),
    (re.compile(rf"\bin (?:model )?{_V}\b"), "yet"),
    # a method page named in prose: 'pricing-v0.1 Tier curves'
    (re.compile(rf"\b([a-z][a-z_]*)-{_V}\b"), r"\1 method"),
    # 'uncalibrated: model v0' / ", model v0" trailing labels
    (re.compile(rf"[,;:]?\s*'?model {_V}'?"), ""),
)
_PATHISH = re.compile(r"\S*(?:/|\.md\b)\S*")  # a URL or path inside prose: kept as written
_ONLY = re.compile(rf"^\s*([A-Z][\w-]*(?: [\w-]+){{0,2}}) {_V}\s*$")  # a label: 'Recovery v0.1'
_MID = re.compile(rf"\b(?:the )?([a-z][\w-]*) {_V}\b")  # 'that price v0.1 assumed'


def strip_text(text: str) -> str:
    """`text` without model versions (unchanged when it has none, or is a URL / path)."""
    if not VERSION.search(text) or _URLISH.match(text.strip()):
        return text
    m = _ONLY.match(text)
    if m:
        return f"{m.group(1)} model"
    paths: list[str] = []

    def hold(mm: re.Match[str]) -> str:
        paths.append(mm.group(0))
        return f"\x00{len(paths) - 1}\x00"

    out = _PATHISH.sub(hold, text)
    for pattern, repl in _RULES:
        out = pattern.sub(repl, out)
    out = _MID.sub(lambda mm: f"the {mm.group(1)} model", out)
    out = VERSION.sub("", out)  # anything left: a bare version
    out = re.sub(r"\(\s*\)", "", out)
    out = re.sub(r"\s+([,.;:)])", r"\1", out)
    out = re.sub(r"\(\s+", "(", out)
    out = re.sub(r" {2,}", " ", out).strip()
    return re.sub(r"\x00(\d+)\x00", lambda mm: paths[int(mm.group(1))], out)


def strip_doc(doc: Any, view: str | None = None) -> Any:
    """A read-model doc with the versions taken out of its text (every view but Track record)."""
    if view in KEEP_VIEWS:
        return doc
    return _walk(doc)


def _walk(value: Any) -> Any:
    if isinstance(value, str):
        return strip_text(value)
    if isinstance(value, dict):
        return {k: (v if k in KEEP_KEYS else _walk(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v) for v in value]
    return value
