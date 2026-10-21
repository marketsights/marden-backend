# Before you start the overnight run

Everything here is a human step: an account, a key or a decision. Agents can't create these. Without them the related tasks come back `blocked`, and they show up under "Needs you" in the morning report.

## Required

| # | What | Where | Goes in `.env` as |
| --- | --- | --- | --- |
| 1 | Neon project with a **staging branch**; run `CREATE EXTENSION postgis;` on it once | console.neon.tech | `DATABASE_URL` |
| 2 | Cloudflare R2 enabled and an API token with read/write on R2 | Cloudflare dashboard → R2 → Manage API tokens | `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` |
| 3 | Railway account and a token that can create services in one project (staging environment) | railway.com → Account → Tokens | `RAILWAY_TOKEN` |
| 3b | Clerk **development** instance, set up as in spec 23. Sign-up restricted to invitations; organizations on and personal accounts off; custom roles `org:owner` and `org:viewer` added (`org:admin` and `org:member` exist by default); MFA required for admins; webhook endpoint added once the staging API URL exists (the deploy task prints it in reports/deploy.md; until then, the webhook tests use signed sample payloads). | dashboard.clerk.com | `CLERK_PUBLISHABLE_KEY`, `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`, `CLERK_SECRET_KEY`, `CLERK_WEBHOOK_SIGNING_SECRET` |
| 4 | SEC User-Agent with a contact address | — | `SEC_USER_AGENT="Marden research cody@marden.ai"` |
| 5 | Anthropic API key on a usage tier that allows about 48 concurrent agents. A personal Claude subscription will hit rate limits within minutes. | console.anthropic.com | `ANTHROPIC_API_KEY` |
| 6 | A Linux machine that stays on all night: 16+ vCPU, 64 GB RAM, 200 GB disk. Use a cloud VM, not a laptop that sleeps. Install git, Node 20+, pnpm, Python 3.12, uv, unzip, and Claude Code (`npm i -g @anthropic-ai/claude-code`). | Any cloud | — |
| 7 | The three inputs: the handoff folder (in this package), `marden_code_2026-10-20.zip` and `marden_data_2026-10-20.zip` | Your machine | passed to `bootstrap.sh` |

## Optional (features run in stub mode without these)

- **Postmark** (`POSTMARK_TOKEN`): alert emails. Without it, emails are written to a log.
- **FRED** (`FRED_API_KEY`): free; needed for Treasury and SOFR curves, which pricing uses. Get one.
- **Market data** (`MARKET_DATA_API_KEY`): end-of-day equity prices (Polygon or Tiingo).
- **Sentry** (`SENTRY_DSN`).
- **GitHub** (`GITHUB_REMOTE`): when set, `main` is pushed at the end of the run.

## Decisions agents will bring back to you

- **Paid record access.** Some county recorders and Secretary of State UCC portals charge or block automated searches. Agents list which states and counties, and the cost, instead of working around them.
- **Site terms.** Any source whose terms forbid automated collection is returned as blocked, not scraped.
- **TRACE.** Bond prints need a licensed FINRA feed. Until then, prices use the legacy TRACE refresh and fund marks.
- **Material facts.** Status changes, cushion-band changes and alerts wait in the review queue for your click, by design (spec 22).

## Known data limits for the first night

- The legacy `marks/*.pkl` panels (~4.4 GB) were not in the data zip. N-PORT history has to be rebuilt from SEC downloads at under 10 requests per second, which takes many hours. Current holdings and marks from `fundpos.json` and `q3snap.json` load immediately; full history fills in behind them.
- Satellite images are not in the zip. They are re-fetched per site from Sentinel-2.
