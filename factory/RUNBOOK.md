# Marden factory: runbook

One command builds Marden overnight with up to 48 Claude Code agents working in parallel. Each agent gets one task in its own git worktree. Nothing reaches `main` unless the task's tests pass and its branch rebases cleanly. In the morning you read one report saying what works and what needs you.

## 1. Set up (about 20 minutes, once)

```bash
# on the build machine (see PREREQS.md)
unzip marden-factory.zip && cd marden-factory/factory
./bootstrap.sh ../handoff /path/marden_code_2026-10-20.zip /path/marden_data_2026-10-20.zip
cp env.example .env && nano .env        # fill in keys; MARDEN_SEED_DIR is printed by bootstrap
source .env
pip install pyyaml                      # factory dependency
python3 factory.py preflight            # must end with READY
```

`bootstrap.sh` does four things:
- creates `../marden`, the product repo, with `handoff/` (specs 00–22, 31 screens, contracts, design system) and `legacy/` (your existing code)
- creates `../marden-seed`, your code and data unzipped together, read-only
- commits both to `main`
- writes `tasks.json`: 260 starting tasks

## 2. Start

```bash
python3 factory.py init tasks.json
tmux new -s factory 'source .env && python3 factory.py run 2>&1 | tee run.log'
# detach with Ctrl-b d; the run keeps going
```

## 3. Watch (optional)

- `python3 factory.py status`: counts by status, plus what's running, failed or waiting on you.
- `reports/status.html`: refreshed every 30 seconds. To view it, copy it to your laptop, or run `python3 -m http.server -d reports 8080` behind an SSH tunnel. Never expose it publicly.
- Logs for each task attempt are in `logs/<task>/attempt-N.log`.

## 4. Morning

Open `reports/morning_report.md`:
- totals and spend
- progress by area
- **Needs you**: credentials, paid access, decisions
- failed tasks with their last error
- the acceptance run (`reports/acceptance.json`): every screen, endpoint and V1 instrument, pass or fail with the cause

Staging URL: `../marden/reports/deploy.md`.

To fix what it found:
```bash
python3 factory.py retry failed          # after providing what "Needs you" asked for
python3 factory.py add extra_tasks.json  # new work, same format as tasks.json
python3 factory.py run
```

## How it works

| Piece | What it does |
| --- | --- |
| Task graph (`gen_tasks.py`) | **Phase 0:** scaffold, contracts, schema, infra, service framework, design system, app shell, API core, test harness. **Phase 1:** one task per screen (37), endpoint (44), port of existing code (17), seed load (9), collector (32), extraction schema (11), model (12), read model (17) and platform feature (13): alerts, weekly note, review queue, snapshots, Excel, auth and account, team roles, audit log, API keys, observability, backups, CI, and the role-based access test suite. **Phase 2:** wiring, backfill, live schedules, performance, security, data acceptance. **Phase 3:** staging deploy and the acceptance run. |
| Priority | Construction progress comes first. The Completion monitor, site detail, cushion model, review queue, and every collector and extractor behind power, permits, construction progress and local opposition (69 tasks) take the first slots for each agent type. Next in line: the off-balance-sheet lease map and payer downgrade risk (specs 24–25). The acceptance run fails any V1 site with an unexplained n/d. |
| Scheduler | Starts any task whose dependencies are merged, up to 48 at once, with per-role caps so one kind of work can't starve the rest. |
| Scope guard | An agent may change only its declared files, plus tests. Contracts, migrations and gate scripts are protected and owned by architect, integrator or devops tasks. This is what lets 48 agents work at once without breaking each other's work. |
| Structure rules | The factory itself rejects any task that breaks these, before its tests run: 300 lines per TypeScript file, 400 per Python file; kebab-case TypeScript and snake_case Python names; no throwaway names (`_v2`, `old`, `final`); no secrets; no `.env` files; no data files over 512 KB. Lint, enforced on every merge, adds function length, complexity, no import cycles and layer boundaries (web never imports the API; collectors and models stay separate). |
| Auth (Clerk) | Clerk handles sign-in, MFA, organizations, invitations and SSO. Marden decides permissions. "Working" means the spec 23 tests pass: token checks, role mapping and webhooks on every merge; real sign-ins against your Clerk dev instance for every role, endpoint and team flow before deploy and again on staging. |
| Role-based access | Roles: owner, admin, member, viewer, plus Marden staff. Plans: trial, design partner, paid. One permission matrix maps every endpoint to the permission, scope and plans it needs. Access is denied by default: the API won't start if a route has no permission. Org data is isolated by Postgres row-level security. Every endpoint has tests for signed out (401), wrong role or plan (403) and another org's data (404). A red-team suite checks every role against every endpoint. Screens hide what a role can't do. Every write goes to an append-only audit log. |
| Gates | Lint, type-check, unit tests, contract tests, visual diff against the design screenshots, e2e, data validators, migrations, performance and security. They run on the task branch, then again after rebasing onto the latest `main`. |
| Merge queue | One merge at a time, fast-forward only, so `main` is always a commit that passed. |
| Retries and fixers | A failed attempt is retried with the error attached. After 3 failures a fixer agent (Opus) takes it over. |
| Splitting and discovery | An agent can split a large task (up to 25 parts, 2 levels deep) or file follow-up tasks (up to 10 each, 3 levels deep). Total tasks are capped at 5,000. |
| Follow-on QA | Every 4 merges in an area, a tester task runs; every 10, a red-team task runs. |
| Watchdog | Runs the full suite on `main` every 15 minutes. If `main` is red, new work stops and a fixer runs. If it's still red after 3 fixers, the factory halts rather than build on a broken base. |
| Budget | Each attempt has its own spend cap. The whole run stops at `budget_usd` in `factory.yaml` (default $2,500). |

## Settings worth knowing (`factory.yaml`)

- `max_concurrency`: 48. Lower it to 24 on a smaller machine; gates run Playwright and builds in parallel.
- `budget_usd`: set this to what you're willing to spend tonight.
- `models`: Sonnet for most build work; Opus for architecture, models, integration, fixing and red team.

## Stop or resume

- `python3 factory.py stop`: running tasks finish and nothing new starts.
- `python3 factory.py run`: resumes from the saved state, so nothing is redone.

## Tested before shipping

- **Full graph, dry run:** all 260 tasks plus follow-ons merged in dependency order through to ACCEPT, using a stub agent.
- **Failure scenarios:**
  - retry then pass
  - escalate to a fixer
  - split, with the depth limit enforced
  - capped discovery
  - scope violation caught
  - protected-path violation caught
  - merge conflict rebased
  - task waiting on 30 dependencies
  - red `main` halts after 3 fixers
  - structure rules reject bad names, oversize files and embedded secrets, and pass valid Next.js `[param]` and `(group)` folders
- **Real agent:** one task merged with tests passing, for $0.20.
