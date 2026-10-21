# Shared components

Build these once, before any screen. Every screen in `screens/` is made only of these. Class names in brackets are the reference CSS classes in the HTML files.

## Layout

| Component | Spec |
| --- | --- |
| **AppShell** | Row with wrap: Sidebar (212 px, white, right border `line`) + main (`flex: 999 1 560px`). Below ~800 px the sidebar stacks on top. Page background `ground`. |
| **Sidebar** | Logo (18 px brand square + "Marden" 16/700). Search input with ⌘K hint (opens global search: sites, issuers, CUSIPs). Nav: Home; MONITOR: Completion (red count badge = sites past or under 3 months), Risk list (date of latest list), Stress; BROWSE: Sites, Instruments, Payers; YOURS: Watchlist (brand badge = unread alerts), Track record, Account. Footer: "Updated {date} · {time} ET" = last successful pipeline run. Selected item: `brand-tint` background, `brand-ink` text. |
| **PageHeader** | White bar, bottom border. Optional breadcrumb (14 px), H1 22/600, one-line subtitle in `ink-2`, actions right-aligned (wrap on narrow screens). |
| **Card** [`card`, `w`] | White, 1 px `line`, radius 10. Title 17/600. Optional link right ("All →"). |
| **KPI strip** | Cards or one bordered strip split by dividers. Label (13 px uppercase, `ink-3`), value 24/600 with tag, one sub-line 14 px. Red value only when the number is bad news. |

## Data display

| Component | Spec |
| --- | --- |
| **DataTable** [`t`] | TanStack Table. Fixed layout, explicit column widths (px for numbers, flexible for names). Header 13 px uppercase `ink-3` on `surface-2`. Rows 15 px, 9–10 px vertical padding, divider `line-2`, hover `hover`. Numbers right-aligned, tabular. Sortable headers. Horizontal scroll inside the card only when the viewport is narrower than the table's min width. Every table has Excel export. |
| **SourceTag** [`tg`] | 17×17 px square, 11.5/700. F = black fill, white text (filed record). R = grey fill (reported). M = white with brand outline (Marden model). Tooltip: source name, date, link. Click opens the source document or method. Never wraps away from its value. |
| **StatusPill** [`st`] | Rounded pill with dot. Broken (red), Strained (amber), Holding (green), No build / No data (grey). Text always present; color is never the only signal. |
| **ChainGlyph** [`ch`] | Four 13 px squares joined by short lines: end user · tenant · developer/SPV · lenders. Each square colored by that link's status. Tooltip per square: "Tenant · Oracle · Red: BBB- with power withdrawn; force majeure notice". |
| **CushionChip** [`cu`] | Months, signed (−2, +7). Red bg below 0; amber 0–3; light amber 3–6; grey 6–12; green 12+. |
| **TimelineBar** [`tl`] | Track from today to Dec 2029 with year ticks ('27 '28 '29). Band = projected completion P10–P90 (grey; amber if it crosses the deadline; red if the median is past it). Black tick = P50. Red line = lease outside date. Tooltip shows all four dates. |
| **MilestoneGantt** [`gt`] | Per milestone row: done/denied/slipped squares at their dates, projected range band + P50 tick for future milestones, light-red band for the outside-date year, dashed line for today. Columns: milestone, timeline, latest evidence (with tag), status pill. |
| **OddsBar** [`odds`] | 6 px bar + percent. Red ≥ 60%, amber 40–59%, slate below. |
| **Delta** | Signed change; red for negative price moves; ▲/▼ for rank moves. |
| **AlertItem** [`al`] | Brand dot if unread, bold headline with tag, sub-line: rule · date · "emailed". |

## Controls

| Component | Spec |
| --- | --- |
| **Button** [`btn`] | 32 px, radius 6, 1 px `line-strong`, 600 weight. Primary = brand fill (one per page at most: Watch / Watching / Done). |
| **FilterChip** [`chip`] | 32 px pill-ish button; selected = ink fill. Dropdown chips show the current value in bold ("Tenant **All**"). |
| **Tabs** [`tabs`] | Underline tabs; selected = ink text + 2 px ink underline. Counts in smaller grey text. |
| **SegmentedControl** [`seg`] | Two to three options, selected = ink fill. |
| **Toggle row** [`tgl`, `rl`] | Label left, checkbox right, row divider. |
| **WidgetFrame** [`w`, `wh`] | Card with header: 6-dot drag handle (Edit layout mode), title 16/600, "Open view →" link, ⋯ menu (Remove, Move to top). Fixed widget sizes: narrow = 1 column, wide = 2 columns, strip = full width. |

## Charts (ECharts)

| Chart | Used on | Notes |
| --- | --- | --- |
| Debt by cushion stacked bar | Completion KPI | 4 segments: past deadline, 0–3, 6–12, 12+ months. |
| Cushion step chart | Site detail | Weekly cushion; dots + labels at each change; dashed zero line. |
| Price vs fund marks | Instrument detail | Marden price line; filed mark range as a bar at the filing date; dashed carry-forward of last mark; shaded gap. |
| Fitted credit curves | Instruments | Log x-axis (2y–40y). Dots = instruments; solid = fitted over observed range; dashed = extrapolated. Labels in right gutter with leader lines. BB HY index dashed reference. |
| Stress flow | Stress | Sankey: shock → sites (band = loan size) + bar of price change per site. |
| GW by rent-start year | Payer | Stacked bars per year, segment per site, colored by cushion. |
| Lead time | Track record | Per case: flag dot → price-move dot, label days. Unfinished cases shown as dashed "backtest running". |

All charts: `aria-label` stating the finding, hover tooltips with exact values and source tags, no legends where direct labels fit.
