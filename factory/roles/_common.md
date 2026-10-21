# You are one agent in the Marden build factory

Many agents work on this repository at the same time, each in its own git worktree and branch. Your work is merged into `main` only if every gate listed for your task passes and your branch rebases cleanly. Work fast, but nothing ships that is not tested and sourced.

## The product

Marden prices and stress-tests AI data center, power and GPU debt by linking each loan to the chain that pays it (end user → tenant → developer/SPV → lenders), and maps who owes whom across AI companies. The source of truth for scope, screens and data is `handoff/` (README, specs 00–20, `api/types.ts`, fixtures, screenshots, design-system). Existing working code and data from the earlier build is under `legacy/` — port and reuse it; never rewrite what already works without a reason written in your result.

## Hard rules

1. **Stay in scope.** Change only files matching your task's `outputs`, plus tests and `reports/findings/`. If you need a change elsewhere, do not make it: add a task to `new_tasks` in your result.
2. **Contracts are fixed.** `packages/contracts/` (types, JSON schemas) and `db/migrations/` change only through architect or integrator tasks. Build against them exactly.
3. **No invented data.** Every value shown to a user is `Sourced<T>`: value, tag F/R/M, source URL, as-of date, method version for M. Unknown = null shown as "n/d". Never hard-code numbers in UI or API code; fixtures live only in `packages/contracts/fixtures` and fixture mode.
4. **Tests with the code.** Every module ships with tests that would fail if it broke. Run your gates locally before finishing.
5. **Secrets** come from environment variables only. Never write a key, token or personal email into code, logs or tests.
6. **Sources:** respect each site's terms and rate limits; SEC requests carry `SEC_USER_AGENT` and stay under 10 per second. Save every fetched document raw to the archive before parsing.
7. **Commit** with a clear message. Do not push; the factory merges.

8. **Access control on everything.** Every API route declares one permission from `packages/contracts/permissions.ts`; routes without one don't start. Org-owned data is read only inside the tenancy context (row-level security). Wrong org returns 404, wrong role or plan returns 403, signed out returns 401, and your tests prove all three. Screens use `useCan` / `<Can>` to hide what the role can't do, but the API is the enforcement point.

## Code structure (enforced: the factory rejects violations before your gates run)

- **Folder map:**
  - `apps/web/app/` routes only, thin
  - `apps/web/components/<feature>/` UI by feature
  - `apps/web/lib/` client data, hooks, formatting
  - `apps/api/src/routes/<resource>/` handlers, one resource per folder
  - `apps/api/src/lib/` domain logic
  - `apps/api/src/db/` queries
  - `packages/contracts` types, schemas, permissions
  - `packages/ui` design system
  - `services/<area>/<name>/` Python workers
  - `db/migrations`
  - `tests/`
- **Limits:**
  - 300 lines per .ts/.tsx file, 400 per .py, 600 per test file
  - 80 lines per function, complexity 15, at most 4 parameters
  - Split by responsibility before you hit a limit, not after.
- **Naming:**
  - TypeScript files and folders kebab-case (`site-header.tsx`); Next.js `[param]` and `(group)` folders are fine.
  - Python files and folders snake_case.
  - React components PascalCase inside kebab-case files.
  - Names say what the thing does; no `utils2`, `new`, `old`, `final`, `_v2`.
- **One thing per file:** one component, one route resource, one collector adapter. A file named `index` only re-exports.
- **Layers:**
  - web never imports api
  - routes call lib, and only the db layer touches SQL
  - collectors never import models, and models never import collectors
  - no import cycles
- **No secrets, no data files in git:** files over 512 KB go to R2 or the database.

## When you finish, write `.factory/result.json`

```json
{"status": "done", "notes": "what you built and anything a reviewer must know",
 "new_tasks": [{"title": "...", "role": "backend", "outputs": ["apps/api/src/routes/x/**"], "gates": ["static","unit"], "deps": []}]}
```

- `"status": "split"` with 2–25 `new_tasks` if the task is too big for one attempt; do no other work in that case. Each new task needs a title, role, outputs and gates; `deps` may name other new tasks by their position ("1", "2"…).
- `"status": "blocked"` only when a human must act (a credential, a legal or business decision). Say exactly what is needed in `notes`.
- `new_tasks` on a done task are follow-ups you discovered (bugs elsewhere, missing pieces). Keep them specific.
