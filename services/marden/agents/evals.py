"""The extraction eval (spec 22 layer 6, "measured, not assumed").

Gold cases live in services/tests/agents/eval/*.json: an excerpt of a real stored document (with its
document id, url and sha256), the area, and the facts a careful analyst records from it, each with its
quote. Tests check that every gold quote is in its excerpt and passes the code validators, so the gold set
is confirmed against its quotes. `python -m marden agents eval` runs the specialist for each case on its
excerpt (live, or replaying the answers recorded in tests/agents/recorded/ with --recorded), applies the
code validators, and reports precision and recall per area. Re-run it on every prompt or model change.

Scoring: candidates that fail a hard code check are counted as rejected (they would never publish).
Candidates are deduplicated by (field, subject kind, value) as the fact store would. A candidate matches a
gold fact of the same field and subject kind when the values agree: numbers within 1e-6 relative, dates
equal, text by normalized containment or half the words in common, lists and objects by their gold items.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from marden.agents import extract
from marden.agents.config import AgentSettings, PROMPT_VERSION
from marden.agents.extract import Candidate
from marden.agents.sections import split
from marden.agents.text import DocText, normalize, read_plain
from marden.agents.validators import validate
from marden.core.jsonutil import canonical_json

TESTS = Path(__file__).resolve().parents[2] / "tests" / "agents"
GOLD_DIR = TESTS / "eval"
RECORDED_DIR = TESTS / "recorded"


@dataclass
class Case:
    id: str
    area: str
    document: dict[str, Any]
    excerpt: str
    gold: list[dict[str, Any]]

    def text(self) -> DocText:
        return read_plain(self.excerpt.encode("utf-8"))


def load_cases(directory: Path = GOLD_DIR) -> list[Case]:
    out = []
    for path in sorted(directory.glob("*.json")):
        raw = json.loads(path.read_text())
        out.append(Case(raw["id"], raw["area"], raw["document"], raw["excerpt"], raw["gold"]))
    return out


class _Doc:
    """The minimal document the extractor's prompt needs (type, title, date)."""

    def __init__(self, case: Case):
        self.doc_type = case.document.get("doc_type")
        self.title = case.document.get("title")
        self.doc_date = case.document.get("date")
        self.sha256 = case.document.get("sha256")


def _words(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9.]+", normalize(s).lower()))


def text_match(gold: Any, got: Any) -> bool:
    g, c = normalize(str(gold)).lower(), normalize(str(got)).lower()
    if not g or not c:
        return False
    if g in c or c in g:
        return True
    gw, cw = _words(g), _words(c)
    return bool(gw) and len(gw & cw) / len(gw) >= 0.5


def value_match(gold: Any, got: Any) -> bool:
    if isinstance(gold, bool) or isinstance(got, bool):
        return gold is got
    if isinstance(gold, int | float) and isinstance(got, int | float):
        return abs(float(gold) - float(got)) <= max(1e-9, abs(float(gold)) * 1e-6)
    if isinstance(gold, list) and isinstance(got, list):
        return all(any(text_match(g, c) for c in got) for g in gold)
    if isinstance(gold, dict) and isinstance(got, dict):
        lower = {str(k).lower(): v for k, v in got.items()}
        return all(str(k).lower() in lower and (value_match(v, lower[str(k).lower()]) if isinstance(v, int | float)
                                                 else text_match(v, lower[str(k).lower()]))
                   for k, v in gold.items())
    if isinstance(gold, str) and isinstance(got, str) and re.fullmatch(r"\d{4}-\d{2}(-\d{2})?", gold):
        return got == gold
    return text_match(gold, got)


def score(case: Case, cands: list[Candidate]) -> dict[str, Any]:
    kept: dict[tuple[str, str, str], Candidate] = {}
    rejected = 0
    for c in cands:
        if c.hard_fail:
            rejected += 1
            continue
        kept.setdefault((c.field, c.subject_kind, canonical_json(c.value)), c)
    used: set[int] = set()
    matched = 0
    misses = []
    for g in case.gold:
        kinds = g["subject_kind"] if isinstance(g["subject_kind"], list) else [g["subject_kind"]]
        hit = next((i for i, c in enumerate(kept.values()) if i not in used and c.field == g["field"]
                    and c.subject_kind in kinds and value_match(g["value"], c.value)), None)
        if hit is None:
            misses.append({"field": g["field"], "value": g["value"]})
        else:
            used.add(hit)
            matched += 1
    extras = [{"field": c.field, "value": c.value, "quote": c.quote[:160]}
              for i, c in enumerate(kept.values()) if i not in used]
    return {"case": case.id, "area": case.area, "gold": len(case.gold), "candidates": len(kept),
            "rejected": rejected, "matched": matched, "misses": misses, "extras": extras}


def recorded_path(case: Case) -> Path:
    return RECORDED_DIR / f"{case.id}.json"


def run_case(case: Case, llm: Any | None, *, recorded: bool, record: bool) -> dict[str, Any]:
    text = case.text()
    section = split(text, 10**9)[0]
    if recorded:
        path = recorded_path(case)
        if not path.exists():
            return {"case": case.id, "area": case.area, "error": "no recorded answer"}
        out = json.loads(path.read_text())["tool_input"]
    else:
        req = extract.request(case.area, _Doc(case), text, section, case.excerpt.encode(), None, native_pdf=False)
        res = llm.call(req)
        out = res.tool_input if res else None
        if record and out is not None:
            RECORDED_DIR.mkdir(parents=True, exist_ok=True)
            recorded_path(case).write_text(json.dumps(
                {"case": case.id, "prompt_version": PROMPT_VERSION, "model": res.model, "tool_input": out},
                indent=1, ensure_ascii=False) + "\n")
    cands, _, _ = extract.parse(case.area, section.id, out)
    validate(cands, text)
    return score(case, cands)


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_area: dict[str, dict[str, Any]] = {}
    for r in results:
        if "error" in r:
            continue
        a = by_area.setdefault(r["area"], {"cases": 0, "gold": 0, "candidates": 0, "matched": 0, "rejected": 0})
        for k in ("gold", "candidates", "matched", "rejected"):
            a[k] += r[k]
        a["cases"] += 1
    for a in by_area.values():
        a["precision"] = round(a["matched"] / a["candidates"], 3) if a["candidates"] else None
        a["recall"] = round(a["matched"] / a["gold"], 3) if a["gold"] else None
    tot = {k: sum(a[k] for a in by_area.values()) for k in ("gold", "candidates", "matched", "rejected")}
    tot["precision"] = round(tot["matched"] / tot["candidates"], 3) if tot["candidates"] else None
    tot["recall"] = round(tot["matched"] / tot["gold"], 3) if tot["gold"] else None
    return {"by_area": by_area, "total": tot}


def run_eval(*, areas: list[str] | None = None, recorded: bool = False, record: bool = False,
             cases: list[Case] | None = None, llm: Any | None = None) -> dict[str, Any]:
    cases = [c for c in (cases or load_cases()) if not areas or c.area in areas]
    if not recorded and llm is None:
        from marden.agents.runner import make_llm

        llm, status = make_llm(settings=AgentSettings.from_env())
        if not status.configured:
            return {"status": "not_configured", "detail": status.detail}
    results = [run_case(c, llm, recorded=recorded, record=record) for c in cases]
    report = {"status": "ok", "prompt_version": PROMPT_VERSION, "mode": "recorded" if recorded else "live",
              "results": results, **summarize(results)}
    if llm is not None:
        report["tokens"] = llm.budget.used
        report["models"] = {"extractor": llm.settings.models["extractor"]}
    return report
