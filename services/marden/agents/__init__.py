"""Marden's agent system (spec 22 "The agents"; docs/agents.md).

Documents from collectors, staff uploads, the CLI and the R2 inbox are read by Claude-based agents:

    intake -> router -> specialist extractors (one per area, each with a domain brief) -> code validators
           -> verifier (independent second read) -> linker (subjects, with evidence) -> publish | review queue

Agents read and check; code computes every number and decides what publishes. Modules:

    config       settings, models per role, budgets, the backend in use (api | cli | not_configured)
    text         document text with pages (PDF, HTML, DOCX, PPTX, XLSX, TXT) and verbatim quote finding
    sections     long documents split into sections with page references
    fields       the field vocabulary per area (reuses the fact store's names)
    areas        area registry, briefs (briefs/<area>.md), tool schemas, system prompts
    llm          the model-call interface: request cache, token accounting, budgets
    backends     Anthropic API (caching, PDFs, Files API, refusal fallback) and the claude CLI fallback
    router / extract / validators / verify / linker / linking / publish / review
    pipeline     one document through every stage; intake / runner / batch / tasks / commands / evals
"""
