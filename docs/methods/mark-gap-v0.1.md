# Mark gap and NAV hit v0.1

Written 2026-10-21. Code: `services/marden/models/mark_gap.py`, `mark_gap_inputs.py`. Model `mark_gap`, version `0.1`, method label `mark_gap v0.1`.

## Question

Who carries an instrument above or below the Marden price, and what marking it to that price would cost each holder (spec 22 "Mark gap and NAV hit", spec 07 Rules).

## Inputs (point in time)

| Input | Source | Usable from |
| --- | --- | --- |
| Holder lines: par, fair value, mark | `holding` rows linked to the instrument from N-PORT and BDC 10-Q / 10-K schedules. Insurer NAIC lines are excluded (held for legal review, `price-sources.md` §3). | N-PORT: 60 days after the period end, and never before the filing date (`pricing-v0.1.md` "Data"). BDC: the filing date, else 45 days after the quarter end. |
| Which funds still hold it | Each fund's newest usable filing over all its lines. A fund counts only if that newest filing lists the instrument. | As above |
| Mark of a non-USD line | Holding fact `mark_price` (in the line's own currency). Its USD par is value ÷ mark. | As above |
| Fund net assets | The line's `fund_net_assets` (BDC). Else the fund's `net_assets_usd` fact for that period (N-PORT). Else value ÷ pctVal of the same line. | Same filing |
| Marden price | Newest `price` model output on or before the day (`mardenPrice` / `price`). Else the newest Marden daily price (`price_obs` kind `marden`). | Its as-of date |

## Arithmetic

For each holder (fund):

- `lastMark` = 100 × Σ value ÷ Σ par over the fund's lines in that filing.
- `gapPts` = Marden price − lastMark. It is negative when the fund carries the instrument above market.
- `navHitMm` = parMm × gapPts ÷ 100.
- `shareOfNav` = navHitMm ÷ fund net assets.

The sign follows spec 07 and the web app: a negative hit is a fall in NAV. Spec 22 writes the same magnitude as (fund mark − Marden price) × par ÷ net assets.

Instrument level:

- `markGap` = Σ navHitMm ÷ Σ parMm × 100, a par-weighted gap. The KPI, the chart label and the holders-table total therefore use the same number (spec 07 Acceptance).
- If no usable filing lists the instrument, `markGap` = `'no fund'`.
- `fundMarks`: the low and high of the holders' last marks on the newest report date that at least two funds share (else the newest date).
- `nextMarksPublic`: the first date after the as-of day on which new marks become usable. For N-PORT that is a month end + 60 days. For BDC filings it is a quarter end + 45 days.

## Missing inputs

Fields are never guessed. Without a Marden price, `gapPts`, `navHitMm`, `shareOfNav` and `markGap` are null and `missing` says so. Without net assets, `shareOfNav` is null.

## Runs

- Daily at 09:48 UTC.
- Within a minute of any of these: a `price` output, a Marden daily price, a holder fund's new holding line, or a fund's net assets (`models.run_dirty`).
- A run for today marks the instrument's read models (`@model:mark_gap`).
- `--as-of` with a past day runs an honest backtest on what was public that day.
