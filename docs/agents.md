# The document agents

Spec 22 "The agents" and "How every number stays accurate", as built in `services/marden/agents/`.
Documents from every source are read by Claude-based agents that know their area; code checks every value,
an independent verifier re-reads the passage, a person approves what matters, and only then does a fact
reach the screens. Agents read and check; code computes every number and decides what publishes.

```
collectors ─┐  document.extract_state='queued'  (trigger NOTIFY marden_agents)
uploads ────┤  POST /api/documents -> agent_upload -> raw store (R2) -> document 'upload'
CLI ────────┤  python -m marden agents ingest <file|dir|url>
R2 inbox/ ──┘  objects under inbox/ in the raw bucket (R2 only)
      │ agents.sweep (every 30 s): agent_job per (document, prompt version), with a priority
      ▼ agents.work (every minute): claim by priority, per-source caps, shared token budget
router (Haiku) ── document kind, language, scanned, areas, and for long documents the sections per area
      ▼
specialists (Sonnet), one per area and section, each with its domain brief and strict JSON schema
      ▼
code validators (quote verbatim, every number of the value in the quote, units, ranges, dates, sums) ── hard
                failure: rejected
      ▼
verifier (Opus, a different prompt) re-reads the passage: agree / disagree / unsupported
      ▼
linker: exact identifiers accepted; names, upload guesses and model proposals with evidence go to review
      ▼
publish: verified, routine, unconflicted facts on an accepted subject -> fact (published) -> read models
         and models marked dirty; everything else -> review_item (staff approve / reject / edit)
```

## Intake: continuous feeding

| Source | How it arrives | Notes |
| --- | --- | --- |
| Collectors | a `document` row with `extract_state = 'queued'` (8-K, EX-10, FERC issuances and submittals, permits, agendas) | the `document_agent_notify` trigger NOTIFYs `marden_agents`; `agents.sweep` (every 30 s) queues it; `agents watch` reacts at once |
| Staff upload | `POST /api/documents` (multipart: `file`, optional `site`, `instrument`, `entity`, `filed`, `sourceUrl`, `title`, `docDate`) | PDF, DOCX, XLSX, HTML, TXT sniffed from the bytes; at most `MARDEN_UPLOAD_MAX_BYTES` (50 MB); linked subjects must exist; staged in `agent_upload`, stored raw (R2) as a `document` of source `upload` by the sweep, then queued; audit-logged |
| CLI | `python -m marden agents ingest <file|dir|url> [--site S] [--instrument I] [--entity E] [--filed|--reported] [--run]` | URLs are fetched politely (declared User-Agent, per-host limits; source `agent_url`) |
| R2 inbox | objects under `inbox/` in the raw bucket | `agents.r2_inbox` every 10 min, once per key and etag (`agent_inbox`); "not configured" without R2 |

Idempotent: one `agent_job` per (document, prompt version); every model call is cached by a hash of the
whole request (model, prompt, schema, document bytes) in `agent_call`, so identical bytes under another URL,
a retry after a crash and a re-run cost nothing. A new prompt version (`PROMPT_VERSION` in `config.py`, now
0.2; facts carry `method_version = 'agents v0.2 <area>'`) re-reads documents when they are queued again.

Priority (higher first): 90 a document about a watched subject (the filer's instruments or chain sites on any
watchlist, or the upload's own site / instrument); 85 a document about a material subject (the filer is a
party to a financed chain or issues a site-backed instrument, or staff tied the document to a site or
instrument); 80 an 8-K current report; 70 staff uploads; 50 routine;
20 backfill (dated more than 30 days ago; `agents backfill` sends these through the Batch API).
Concurrency: at most `MARDEN_AGENT_SOURCE_CAP` (default 2) documents of one source at once
(`MARDEN_AGENT_SOURCE_CAPS="sec_index=3,ferc=1"`), `MARDEN_AGENT_CONCURRENCY` threads (default 3).

## Router and sections

`router.py`: code measures the file (kind, pages, chars, text layer) and splits long documents into sections
on headings (ARTICLE, Section 2.01, Item 1.01, Note 7, SCHEDULE, EXHIBIT) or page breaks, each with its page
range (`sections.py`; PDFs split on pages and sent as their own sub-PDF). The router model (Haiku, low effort)
reads the metadata, the opening and the section list and answers the document kind, language, scanned or not,
the areas that apply and the sections each area should read. No area: the job is skipped.

## Specialists

One extractor per area (`areas.py`), each with a written domain brief (`briefs/<area>.md`: what matters for
credit on data-center and infrastructure debt, where the facts are in such documents, traps, and examples
quoted from real stored documents) and the area's field vocabulary (`fields.py`) as a strict tool schema.
The system prompt and the schema are stable per prompt version and cached.

| Area | Reads | Fields (new fields marked in `fields.py`) |
| --- | --- | --- |
| `debt` | credit agreements, indentures, notes, 8-K Items 1.01 / 2.03, offering memoranda, ABS | amount_usd, commitment_usd, drawn_usd, tranches, coupon, coupon_type, coupon_benchmark, coupon_spread_bp, coupon_rate_pct, sofr_floor_pct, issue_price_pct, upfront_fee_pct, undrawn_fee_pct, maturity, commitment_termination_date, closing_date, amortization, min_dscr, max_ltv_pct, min_liquidity_usd, covenant_test, collateral_text, guarantors, recourse, borrower, admin_agent, collateral_agent, arrangers, lenders, seniority, events_of_default, cross_default_threshold_usd, change_of_control, use_of_proceeds, gpu_model, customer_text |
| `periodic` | 10-Q / 10-K debt and commitments footnotes, MD&A liquidity | total_debt_usd, long_term_debt_usd, debt_current_usd, cash_usd, liquidity_text, going_concern, debt_maturities, purchase_obligation_usd, remaining_performance_obligation_usd, outstanding_usd, drawn_usd, covenant_compliance |
| `lease` | leases, colocation / compute / master services agreements, offtake | lease_tenant, lease_landlord, lease_mw, lease_term_years / term_years, rent_start / start, outside_date, lease_annual_rent_usd / annual_usd, lease_tcv_usd / total_usd, lease_renewal, lease_termination, force_majeure, lease_guarantee, take_or_pay |
| `power` | FERC orders and filings, state commission dockets, ISO / utility large-load and queue reports, ESAs | capacity_mw, expected_cod, mw_requested, proposed_cod, withdrawn_date, large_load_request, expected_energization, energized_mw, power_status, power_contract, upgrade_cost_usd, cost_responsibility, docket_order, milestone |
| `permits` | air permits and notices, building / zoning, certificates of occupancy, liens | air_permit, permitted_units, capacity_mw, emission_limits, hearing_date, contractors, construction_stage, pct_complete, certificate_of_occupancy, mechanics_lien, zoning |
| `local` | county / city agendas and minutes, court dockets | opposition_event, tax_abatement, hearing_date, lawsuit |
| `ratings` | rating agency releases, credit events | rating, rating_action, credit_event, tenant_rating |
| `gpu` | earnings releases, presentations, GPU financings | gpu_model, gpu_count, active_it_mw, contracted_mw, delivered_mw, chip_types, backlog_rpo_usd, top_customers, gpu_rental_rate_usd_hr |

Every field reuses the fact store's existing names where one exists; new ones are documented in `fields.py`
(definition, unit, subject kinds). Each fact carries `quote` (verbatim), `page`, `as_of` and `confidence`.

## How every number stays accurate (spec 22 layers)

1. **No quote, no fact.** `validators.py` rejects a candidate whose quote is not in Marden's own copy of the
   document (whitespace, typographic quotes and dashes, ligatures and PDF line-break hyphens normalized; no
   paraphrase, no ellipses).
2. **Two independent reads.** `verify.py`: a different prompt and model (Opus by default) sees only the field
   definition, subject, value, the quote and the passage around it, and answers agree / disagree /
   unsupported. It never sees the extractor's own note (its reasoning, and model output derived from
   untrusted document text). Anything but agree goes to review.
3. **Code validators.** Hard (rejected): quote found, value shape, subject kind, the number written in the
   quote with its scale word ("$2.6 billion", "5.50%" for 550 bp, "25,000 kW" for 25 MW, a table's "(dollars
   in millions)"), the date's year in the quote, ranges per unit; for text, list and object values every
   number inside the value (`value_numbers`: the JSON numbers of a tranche or maturity table, "760.33" in an
   emission-limits object, the "5.50%" in "Term SOFR + 5.50%"), so a table quoted only by its heading or model
   prose with its own numbers never publishes; identifiers inside an object value (docket, permit or case
   number) printed somewhere in the document (`identifier_in_document`; they key later subject links).
   ISO dates inside an object are left to the verifier. Soft (review): a number matching only
   through an unstated table scale, the month missing from the quote (full dates and month values alike),
   date order (closing <= commitment termination <= maturity), tranches adding to the facility (0.5%).
   Month-precision dates are values, not failures: `maturity`, `commitment_termination_date`, `rent_start`,
   `outside_date`, `expected_cod` and the other month fields take YYYY-MM when the document gives only the
   month ("matures in September 2031"), as the fact store already holds for some instruments.
4. **Source ranking.** Filed (F: SEC, FERC, state, county and court records, uploads marked filed) beats
   reported (R). A value that differs from another source's published value for the same as-of date, or a
   reported value that differs from a filed one, is a conflict and goes to review; never resolved silently.
5. **Approval where it matters.** Facts that can change a chain status, cushion band or price, or fire an
   alert, wait for a person: fields marked material (outside date, rent start, energization, power status,
   termination, force majeure, ratings and credit events, opposition events, going concern...), the status and
   cushion models' input fields on chain subjects (and events of the kinds the cushion model reads there:
   opposition, permit), every field whose event the alert engine alerts on (every `opposition` event: hearing
   dates and tax abatements included), the price model's inputs when the value would change the current one
   (instrument terms: size, coupon, spread, benchmark, maturity, seniority...; the issuer's debt and share
   counts; site price signals: material code `price_input`), and any quote or event title the materiality
   rules flag (`marden.models.materiality`).
   Routine verified filed facts on an accepted subject publish automatically
   (tag F, `producer = 'agent:<area>'`, `method_version = 'agents v0.2 <area>'`, quote, document id, source
   url), and the fact writer marks read models and models dirty in the same transaction. A fact read from a
   reported source (tag R: press, a deck, an upload not marked filed) passes the same layers but always waits
   for a click; approving it publishes it with tag R. Event-like fields
   also write an `event` (opposition, rating, notice, docket, permit, milestone) published with the fact.
6. **Measured.** The eval below re-runs on every prompt or model change.

## Linker

`linker.py` / `linking.py`. Accepted without a person only on exact identifiers: CUSIP / ISIN (instrument),
CIK / LEI (entity), the filing's own CIK for the registrant ("CoreWeave, Inc.", "the Registrant"), EIA plant
ids, permit and docket numbers on plants and sites, ISO queue ids; the upload's own subject when the mention
is its name or an alias exactly (any other mention of that kind is only proposed for it: "Building 3" in a
Building 4 lease scores high on fuzzy matching); or a mention a person accepted before (a link the linker
accepted on its own does not count as a confirmation). Exact names and the linker model's choices among fuzzy candidates (rapidfuzz) are
proposed links with an exact evidence passage (`agent_link`, review item kind `link`); the model also proposes
chain links ("Red Chiles A-D LLC" spv_of site jupiter). When a person accepts a proposed link, the facts that
waited only for it publish (`publish.release_after_link`).

## Review queue and status API (staff)

| Method | Path | Permission |
| --- | --- | --- |
| POST | `/api/documents` | `documents:upload` (staff) |
| GET | `/api/documents?status=`, `/api/documents/:id`, `/api/uploads/:id`, `/api/agents/status` | `documents:read` (staff) |
| GET | `/api/review?status=&kind=&area=`, `/api/review/:id` | `review:read` (staff) |
| POST | `/api/review/:id/approve`, `/reject` (`{reason}`), `/edit` (`{value, quote, unit?, asOf?, subjectType?, subjectId?, field?}`) | `review:approve` (staff) |

The worker writes `agent_status` (backend, models, never a secret) on every sweep; `GET /api/agents/status` and
the upload response carry `checkedAt` and `stale` (no report for 10 minutes: the worker is not running), and an
upload says `configured: true` only while the status is fresh.

Document status: `queued`, `running`, `routed`, `extracted`, `verified`, `linked`, `published`, `needs_review`,
`failed`, `skipped` (no area applies), `budget` (paused by a token budget), `not_configured`. Decisions are
recorded in `agent_decision` (one open per item) and applied by the worker within seconds through the fact and
event writers (`review.py`); the API waits up to 3 s and answers 200 with the result or 202 pending. An edit
needs a quote that is in the document verbatim and passes the hard checks (a field outside the vocabulary
still needs every number of its value in the quote); it publishes under the person's name
(`producer = 'review:<user>'`) and rejects the original.

Re-checking: `python -m marden agents recheck [--doc ID]` re-runs the code checks on every agent candidate that
is published or waiting (run it after a validator is tightened). A fact the pipeline published that no longer
passes is withdrawn: `status = 'superseded'` (point-in-time reads still show what Marden showed and when), its
published events rejected, read models and models marked dirty, `audit_log` `fact.withdraw`, and the candidate
back in the review queue (`ref_type = 'agent_candidate'`), where a person re-publishes it only by an edit with a
quote that holds the value. A value a person approved or edited stands. A waiting candidate that now fails a hard
check is rejected. A later read of the same withdrawn value goes to review, never back on screen by itself. CLI: `python -m marden agents review list |
approve ID | reject ID --reason R | edit ID --value V --quote Q`.

## Eval

`services/tests/agents/eval/*.json`: excerpts of real stored documents (document id, url, sha256) with the
facts a careful analyst records, each with its quote; tests confirm every gold quote is in its excerpt and
passes the code checks. `python -m marden agents eval` runs each area's specialist on its cases and reports
precision and recall per area (`--recorded` replays the answers saved in `tests/agents/recorded/` with
`--record`; CI uses the recorded answers). Live runs on 2026-10-21, claude-sonnet-5-5, 41 gold facts in 10
cases:

| Backend | Precision | Recall | Missed | Tokens |
| --- | --- | --- | --- | --- |
| `claude` CLI fallback (first prompt text) | 1.00 | 0.95 | debt 1, gpu 1 | 73,927 |
| Anthropic API (refined field definitions) | 1.00 | 0.93 | debt `events_of_default`, `change_of_control`; gpu one `top_customers` list | 58,316 (about $0.18) |
| Anthropic API (`maturity` takes a month), prompt 0.1, 22:18 UTC | 1.00 | 0.95 | debt `events_of_default`; gpu one `top_customers` list | 10,414, $0.04 (debt re-read; the other 9 cases answered from the request cache) |
| same answers scored with the `value_numbers` / `identifier_in_document` checks | 1.00 | 0.90 | as above, plus permits `emission_limits` (quoted by the table's heading only) and `air_permit` (before identifiers were split on "/") | replay, no calls |
| Anthropic API, prompt 0.2 (list / object quotes hold every number), 23:18 UTC; the recorded set | 1.00 | 0.93 | debt `events_of_default`, `change_of_control`; gpu one `top_customers` list | 59,101, $0.20 (all 10 cases re-read); permits re-read after its brief stopped suggesting the table heading as the quote: 2 of 2, 5,368 tokens |

## Live run (Anthropic API, 2026-10-21)

### Re-proof after the independent review (prompt 0.2, 23:20 UTC)

The same eight documents, every call new (prompt 0.2), 121 s, 958,633 tokens, $2.27 (router $0.006, specialists
$1.61, linker $0.05, verifier $0.60; prompt caching read 96k tokens); with the TCEQ re-read after the permits
brief fix, 968,552 tokens and $2.30 (781k input, 41k output, 100k cache reads, 46k cache writes). Verdicts: 70 agree, 12 unsupported,
0 disagree; 6 rejected by the code checks before the verifier. Before the run, `agents recheck` withdrew the one
published fact that failed the new checks: TCEQ 182126's proposed emissions (fact 1473945, quoted by the
table's heading "Below is the total amount for each pollutant...", none of its seven numbers in the quote).

| Document | Areas routed | New candidates | Published | Review | Rejected | Already published | Tokens |
| --- | --- | --- | --- | --- | --- | --- | --- |
| CoreWeave 8-K (DDTL 5.5) | debt | 17 | 0 | 17 (instrument link proposed; size, maturity, benchmark also `price_input`) | 0 | 0 | 46,108 |
| CoreWeave DDTL 5.5 press release | debt, ratings, gpu | 10 | 0 | 10 (ratings material; 2 rating actions unsupported: no date in the passage) | 0 | 0 | 40,075 |
| CoreWeave Q2 2026 earnings release | gpu, periodic, debt | 6 | 0 | 3 (backlog $104B conflicts with the filed $103.7B; top customers unsupported; a $3.1B facility with no Marden subject) | 0 | 3 (active 1,500 MW, contracted 3,700 MW, cash) | 60,846 |
| CoreWeave 10-Q | 8 areas | 39 | 0 | 30 (instrument links proposed; `price_input`; covenant compliance material) | 3 | 6 | 600,272 |
| FERC order, Oklo v. PJM | power | 2 | 0 | 1 (docket order: Oklo link proposed) | 1 (a milestone with "750 MW" not in its quote, on a wrong subject kind) | 0 | 29,267 |
| Applied Digital Building 4 lease (EX-10.1) | lease, power | 11 | 0 | 4 (tenant twice: model input; force majeure: material; a "large-load request" the verifier found unsupported; the Building 4 mention is not an exact alias, so its link is proposed) | 2 (a termination summary with "100%" and a power summary with dates, neither in its quote) | 5 | 124,845 |
| TCEQ air permit 182126 | permits | 2 (+1 on the re-read) | 0 | 3 (air permit: cushion input, conflicts with the permit fact the collector holds; emission limits, both reads: the same value was withdrawn, a person re-publishes it with the new quote, which holds all seven numbers) | 0 | 0 | 22,745 |
| Doña Ana County BOCC agenda | local | 2 | 0 | 2 (site link proposed; `alert_event`, chain input) | 0 | 0 | 44,394 |

Nothing new published by itself, by design: every new value was either already published, on a subject linked
only by a proposal, material under the tightened rules, in conflict, or unsupported. Accepting a proposed link
releases the routine ones (tested end to end in `tests/agents/test_pipeline.py`). The first TCEQ run of this
re-proof failed ("reason is required": the re-read value collided with the withdrawn fact); fixed (it goes to
review) and re-run from the request cache at no cost.

### First run (prompt 0.1)

Eight real stored documents through the whole pipeline (router, specialists, code checks, verifier, linker,
publish), 145 s, about $2.02 (router $0.006, verifier $0.72; prompt caching read 103k tokens):

| Document | Areas routed | Published | To review | Rejected | Tokens |
| --- | --- | --- | --- | --- | --- |
| CoreWeave 8-K (DDTL 5.5) | debt | 0 | 17 (instrument link proposed) | 0 | 46,012 |
| CoreWeave DDTL 5.5 press release | debt, ratings, gpu | 0 | 10 | 0 | 39,898 |
| CoreWeave Q2 2026 earnings release | gpu, periodic, debt | 3 | 4 | 0 | 62,191 |
| CoreWeave 10-Q | 8 areas | 5 | 44 | 2 | 483,690 |
| FERC order, Oklo (PJM) | power | 0 | 2 | 0 | 30,635 |
| Applied Digital Building 4 lease (EX-10.1) | lease | 4 (+1 repeat) | 4 | 0 | 76,037 |
| TCEQ permit 182126 summary | permits | 1 | 1 | 0 | 12,685 |
| Doña Ana County BOCC agenda (Project Jupiter) | local | 0 | 2 | 0 | 44,307 |

Published only where the code checks passed, the verifier agreed, the value is routine and unconflicted, and
the subject was linked by identifier (filer CIK, permit number) or by the upload: CoreWeave active power
1,500 MW, contracted power 3,700 MW and cash $5,524,000,000; the Building 4 landlord, 25 MW and extension
options. Held for review: everything about an instrument linked only by name (the linker proposed the right
one, DDTL 5.5), ratings (material), backlog $104B against the XBRL RPO $103.7B (conflict), lease tenant and
force majeure terms (material), agenda events on Project Jupiter (proposed site link, model input), and the
facts the verifier found unsupported.

Re-run of the 10-Q after `maturity` took month precision (22:16 UTC, 179,645 tokens, about $0.62: the three
debt sections, three verifier groups and the linker; every other call answered from the request cache): the
two candidates rejected before for "value_date '2031-09' is not ISO" are now candidates the verifier agreed
with, DDTL 5.5 matures 2031-09 and DDTL 4.0 matures 2032-03 ("The DDTL 5.5 Facility matures in September
2031."), waiting in review with their proposed instrument links (EX-CRWV-097, EX-CRWV-095); 0 rejected, 46 to
review, 6 already published.

## Cost and safety

- Budgets: `MARDEN_AGENT_RUN_TOKENS` per run (default 3,000,000) and `MARDEN_AGENT_DAY_TOKENS` per UTC day
  (default 30,000,000), checked before every uncached call; a job that hits one is paused (`budget`) and the
  sweep puts it back in the queue when there is room again (the next run; the next UTC day for the daily
  budget); model calls already made are cached, so it continues where it stopped. A job paused 5 times stays
  paused and visible in `GET /api/agents/status`.
- Every call's tokens and cost are in `agent_call` (`agents status`, `GET /api/agents/status`).
- Caching: per request hash (document bytes, area, prompt version); prompt caching of each agent's system
  prompt and schema; large PDFs uploaded once to the Files API (`agent_file`).
- Backfills: `python -m marden agents backfill` sends old documents through the Message Batches API (half
  price) one stage per batch; `agents.batch_poll` applies results and submits the next stage. A submission
  takes only the requests whose estimated tokens fit what is left of the run and daily budgets (the rest wait
  for the next poll); a request the Batches API errors on or lets expire is resubmitted with the next stage,
  and after 3 such results its job fails with the error.
- Prompt injection: document text is wrapped as data, every prompt says to ignore instructions inside it,
  outputs are strict-schema tool calls, and code validates every value against the document. Logs carry ids,
  stages and token counts, never document text.
- Refusals: Sonnet / Opus calls send `fallbacks: "default"` (server-side refusal fallback); a final refusal is
  recorded and the document goes to review or fails honestly. An extraction the model declined, cut short at
  `max_tokens` or answered without the tool call, and sections beyond `MARDEN_AGENT_MAX_SECTIONS`, are listed
  on the job (`stats.incomplete`, `error`) and the job ends `needs_review`, never `published` with nothing.
- Raw store outage: on a deployed service without R2, staff uploads stay `pending` in `agent_upload` with the
  error ("not stored yet: R2 is not configured ...") and are stored once R2 is set; the sweep still applies
  staff decisions and queues documents.

## Configuration

| Variable | Default | |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | none | the production backend. Without it a local run uses the `claude` CLI if `claude -p "say ok" --output-format json` answers; a deployed service (Railway or Render: any deploy marker) reports not configured and queues nothing |
| `MARDEN_AGENT_BACKEND` | auto | auto, api, cli, off |
| `MARDEN_AGENT_MODEL_ROUTER` / `_EXTRACTOR` / `_VERIFIER` / `_LINKER` | claude-haiku-5-5 / claude-sonnet-5-5 / claude-opus-5-5 / claude-sonnet-5-5 | |
| `MARDEN_AGENT_EFFORT_<ROLE>` | low / medium / medium / low | |
| `MARDEN_AGENT_RUN_TOKENS`, `MARDEN_AGENT_DAY_TOKENS` | 3,000,000 / 30,000,000 | |
| `MARDEN_AGENT_SOURCE_CAP`, `MARDEN_AGENT_SOURCE_CAPS`, `MARDEN_AGENT_CONCURRENCY` | 2 / none / 3 | |
| `MARDEN_AGENT_FILES_API_MIN_BYTES`, `MARDEN_AGENT_SECTION_CHARS`, `MARDEN_AGENT_MAX_SECTIONS` | 4,000,000 / 120,000 / 16 | |
| `MARDEN_UPLOAD_MAX_BYTES` (API) | 52,428,800 | |

Tables: `db/migrations/20261022001000_agents.sql`.
