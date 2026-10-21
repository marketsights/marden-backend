# Risk list v0.1 (rule-based; odds v0)

Written 2026-10-21. Code: `services/marden/models/risk_list.py`, `risk_note.py`. Model `risk_list`, version `0.1`. Specs 10 and 22 ("Risk ranking: rule-based score until the backtest supports a fitted model").

## Output

- One `RiskList` per published week: subject ('week', the Monday).
- A Sunday-night run publishes the next day's list. Any other day's run writes its own week's list only if that week has none yet.
- A published list is frozen (spec 10: past weeks are read-only). `--force` recomputes a week for a backtest.

## Universe and score

The universe is the V1 instruments the price model has priced on or before the as-of day. Points:

| Record | Points |
| --- | --- |
| Site chain status (status model): Broken / Strained | 3 / 2 |
| Cushion (cushion model): below 0 / 0–3 / 3–6 months | 3 / 2 / 1 |
| Rent-start P50 later on the week (cushion deltaWeek < 0) | 1 |
| A dated schedule change on the site in the last 30 days | 1 |
| Mark gap (mark-gap model): 5+ / 2+ points | 2 / 1 |
| Tenant downgrade risk (payer_capacity): High / Watch | 2 / 1 |
| Marden price moved 2+ points on the week | 1 |

- **odds** of a 3+ point move in 90 days = score ÷ (score + 6), labelled 'odds v0' (uncalibrated) until the backtest fits it.
- Rows are ranked by odds, ties by size. Up to 15 rows with a positive score.
- `why`: the records behind the points. The status link's fact passes through with its own tag and source; the model figures are tag M with their method pages.
- `move` compares with the previous published week.
- `earlierCalls`: earlier lists' rows whose Marden price later moved 3+ points (first price output that did; leadDays from the list's week).

## The note

There is one paragraph per top-four row. Code templates fill each with the row's rank, odds and the text of its first three reasons. `refs` hold each record's reference: its source URL, else `model:<name>@<subject>:<day>`. No LLM writes any of it. It is a draft for the human check before email (spec 10).

## Runs

- Mondays 03:40 UTC (Sunday 23:40 ET).
- The dirty-mark runner (new price, mark-gap, status or cushion outputs) only fills a week that has no list yet.
