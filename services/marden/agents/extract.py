"""Specialist extractors: one request per (area, section); outputs parsed into candidate facts.

The request is cached by its content (marden.agents.llm), so the same document bytes read by the same area
under the same prompt version are never paid for twice. Text documents are sent as their text with page
markers; PDFs go natively to the API (a section is its own sub-PDF) and as text to the CLI fallback.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from marden.agents.areas import extract_tool, system_prompt
from marden.agents.backends import wrap_document
from marden.agents.fields import NUMERIC, fields_for
from marden.agents.llm import Llm, LlmRequest, PdfBlock
from marden.agents.sections import Section, section_text
from marden.agents.text import DocText, pdf_pages

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_MONTH = re.compile(r"^\d{4}-\d{2}$")
MAX_QUOTE = 1200


@dataclass
class Candidate:
    area: str
    section: str
    subject_kind: str
    subject_name: str
    subject_identifier: str | None
    field: str
    value: Any
    unit: str | None
    as_of: date | None
    quote: str
    page: int | None
    confidence: float | None
    note: str | None = None
    problems: list[str] = field(default_factory=list)  # value-shape problems found while parsing
    checks: dict[str, Any] = field(default_factory=dict)
    hit: Any = None  # marden.agents.text.QuoteHit when the quote was found
    verdict: str | None = None
    verdict_reason: str | None = None
    corrected_value: str | None = None
    subject_type: str | None = None
    subject_id: str | None = None
    link_id: int | None = None
    link_status: str | None = None
    id: int | None = None  # agent_candidate.id once stored

    @property
    def hard_fail(self) -> bool:
        return any(c.get("hard") and not c.get("ok") for c in self.checks.values())

    @property
    def soft_fail(self) -> bool:
        return any(not c.get("hard") and not c.get("ok") for c in self.checks.values())


def doc_metadata(doc: Any, text: DocText) -> str:
    """Opening of an extraction prompt: what the document is (no url, so equal bytes give equal requests)."""
    return (f"Document: type={doc.doc_type or 'unknown'}; title={(doc.title or text.title or '')[:160]!r}; "
            f"date={doc.doc_date or 'unknown'}; file={text.kind}; pages={text.page_count}")


def request(area: str, doc: Any, text: DocText, section: Section, data: bytes, job_id: int | None,
            *, native_pdf: bool) -> LlmRequest:
    body = section_text(text, section)
    label = f'section="{section.id}" pages="{section.pages}"'
    content: list[str | PdfBlock] = [doc_metadata(doc, text) + f"; reading section {section.id} ({section.pages})"]
    if text.kind == "pdf" and native_pdf:
        part = data if section.id == "all" else pdf_pages(data, section.first_page, section.last_page)
        content.append(PdfBlock(part, body, title=doc.title, scanned=text.scanned))
        if section.first_page > 1:
            content.append(f"This PDF section starts at page {section.first_page} of the document; cite pages "
                           "of the whole document.")
    else:
        content.append(wrap_document(body, label))
    return LlmRequest(agent=f"extractor:{area}", role="extractor", system=system_prompt(area), tool=extract_tool(area),
                      content=content, max_tokens=16000, job_id=job_id, document_sha256=doc.sha256,
                      section=f"{area}:{section.id}")


def _number(v: Any) -> int | float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return int(v) if float(v).is_integer() and abs(v) < 2**53 else float(v)


def _value(spec_vtype: str, item: dict[str, Any], problems: list[str]) -> Any:
    if spec_vtype in NUMERIC:
        num = _number(item.get("value_number"))
        if num is None:
            problems.append("a number field without value_number")
        return num
    if spec_vtype in ("date", "month"):
        raw = (item.get("value_date") or "").strip()
        ok = _ISO_DATE.match(raw) or (spec_vtype == "month" and _ISO_MONTH.match(raw))
        if not ok:
            problems.append(f"value_date {raw!r} is not ISO")
            return None
        return raw
    if spec_vtype == "bool":
        v = item.get("value_bool")
        if not isinstance(v, bool):
            problems.append("a true/false field without value_bool")
            return None
        return v
    if spec_vtype == "text":
        v = (item.get("value_text") or "").strip()
        if not v:
            problems.append("a text field without value_text")
            return None
        return v[:2000]
    raw = item.get("value_json")
    try:
        parsed = json.loads(raw) if isinstance(raw, str) and raw.strip() else None
    except json.JSONDecodeError:
        parsed = None
    if spec_vtype == "list":
        if parsed is None and (item.get("value_text") or "").strip():
            parsed = [item["value_text"].strip()]
        if not (isinstance(parsed, list) and parsed and all(isinstance(x, str) and x.strip() for x in parsed)):
            problems.append("a list field without a JSON list of strings")
            return None
        return [x.strip() for x in parsed]
    if not (isinstance(parsed, dict) and parsed):
        problems.append("an object field without a JSON object")
        return None
    return parsed


def _as_of(raw: Any) -> date | None:
    if isinstance(raw, str) and _ISO_DATE.match(raw.strip()):
        try:
            return date.fromisoformat(raw.strip())
        except ValueError:
            return None
    return None


def parse(area: str, section: str, out: dict[str, Any] | None) -> tuple[list[Candidate], list[dict[str, Any]], str]:
    """Candidates, the subjects the extractor listed, and its summary."""
    if not out:
        return [], [], ""
    specs = fields_for(area)
    cands: list[Candidate] = []
    for item in out.get("facts") or []:
        spec = specs.get(item.get("field"))
        if spec is None:
            continue  # the schema's enum makes this unreachable on the API; the CLI is checked here
        problems: list[str] = []
        value = _value(spec.vtype, item, problems)
        conf = item.get("confidence")
        cands.append(Candidate(
            area=area, section=section, subject_kind=str(item.get("subject_kind") or ""),
            subject_name=str(item.get("subject_name") or "").strip()[:300],
            subject_identifier=(str(item["subject_identifier"]).strip()[:120] or None)
            if item.get("subject_identifier") else None,
            field=spec.name, value=value, unit=spec.unit,
            as_of=_as_of(item.get("as_of")), quote=str(item.get("quote") or "")[:MAX_QUOTE],
            page=item.get("page") if isinstance(item.get("page"), int) else None,
            confidence=max(0.0, min(1.0, float(conf))) if isinstance(conf, int | float) else None,
            note=(str(item.get("note"))[:500] if item.get("note") else None), problems=problems,
        ))
    subjects = [s for s in out.get("subjects") or [] if isinstance(s, dict) and s.get("name")]
    return cands, subjects, str(out.get("summary") or "")[:1000]


def extract(llm: Llm, area: str, doc: Any, text: DocText, section: Section, data: bytes,
            job_id: int | None) -> tuple[list[Candidate], list[dict[str, Any]], str, str | None] | None:
    """(candidates, subjects, summary, problem). `problem` says why a read is incomplete: the model declined
    ('refusal'), stopped at max_tokens ('truncated'), or answered without the tool call ('no answer'); the job
    then goes to review instead of looking like a document with nothing in it."""
    native = bool(llm.backend and llm.backend.view == "native")
    res = llm.call(request(area, doc, text, section, data, job_id, native_pdf=native))
    if res is None:
        return None
    if res.stop_reason == "refusal":
        return [], [], "", "refusal"
    cands, subjects, summary = parse(area, section.id, res.tool_input)
    problem = "truncated" if res.stop_reason == "max_tokens" else ("no answer" if res.tool_input is None else None)
    return cands, subjects, summary, problem
