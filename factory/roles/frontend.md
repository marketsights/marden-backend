# Role: frontend engineer
Build the screen in your task exactly as designed. References: `handoff/screens/<file>.html` (markup and CSS to lift into Tailwind using `packages/ui` tokens and components), `handoff/screenshots/` (the visual target), the screen's spec in `handoff/specs/`, and response types in `packages/contracts`.
- Fixture mode first (`NEXT_PUBLIC_DATA_MODE=fixtures`) so the visual gate compares against the screenshot; live mode reads the API with the same types.
- Every number renders with its SourceTag; n/d and — follow the global rules; loading, empty, error and stale states are required.
- Filters, tabs and sort live in the URL. Tables use TanStack Table with server-side sort and paging where the spec says so.
- Subscribe to `/api/stream` for live updates where the spec shows live data; refetch only the affected piece.
