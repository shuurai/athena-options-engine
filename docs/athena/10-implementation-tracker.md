# 10 — Implementation tracker

Checklist for building Athena. Architecture stays in [02-architecture.md](02-architecture.md). This file only tracks work.

**Status values:** `not_started` · `in_progress` · `done` · `blocked` · `deferred`  
**Last updated:** 2026-10-09

`design/` is a static mock. It is not a runtime app and has no row here.

## Locked for Phase 1

- Strategy: News Front-Running only. Tactics: CSP, CC, Strangle, Hold, Roll, Close.
- Privilege default: `all_orders`. Every open, roll, and close needs Approve.
- Python 3.12+. Workspace tool: **uv** (not Poetry).
- IB adapter later: `ib_insync`. Nautilus is deferred.
- Store: local Postgres. Timescale waits until bar volume needs it.
- Redis waits until approve tokens need it. Phase 1 does not use it.
- Control web later: TypeScript, Next.js, Vercel, one-time signed links.
- Bots later: Python long-poll on the box. Feishu after Telegram.
- Live quotes: iTick. IB: history, positions, orders, inside Athena slots.
- Backup later: local files, then MinIO via the S3 env vars.
- Settings: versioned Postgres rows. Env vars are secrets and bootstrap only.
- IB account: `ATHENA_IB_PAPER_ACCOUNT_ID` in paper mode, `ATHENA_IB_ACCOUNT_IDS` in live mode. fanit sync is later.

## Open, not blocking the skeleton

| Item | Status | Needed by |
|------|--------|-----------|
| Weekday Athena vs Mobile slot hours | `done` | Monday–Friday HKT: 09:30 and 13:00 are Athena, 20:00 is Mobile, 12:30 lunch is idle. Saturday and Sunday stay idle. |
| Telegram bot token and allowlist | `done` | `@athena_hk_bot` sends the draft. Allowlisted Approve and Reject update the paper draft. |
| Tailscale on the Windows box | `not_started` | Phase 2, before Vercel calls the box |
| Feishu app credentials | `deferred` | After Telegram works |

## Phase 1 — Monorepo skeleton

Exit for the phase: orchestrator starts, loads Settings version 1 from Postgres, logs health, and sleeps.

| ID | Task | Status | Exit proof |
|----|------|--------|------------|
| P1-01 | uv workspace, Python 3.12, `apps/` and `packages/` layout, root README | `done` | `uv sync` succeeds. `uv run athena-orchestrator` prints the skeleton line. |
| P1-02 | `packages/store`: Postgres connection, migrations, empty `settings_versions` | `done` | Migration applies to local Postgres. Table exists and is empty. |
| P1-03 | `packages/settings`: risk profile, privilege, IB slots, news, AI, iTick. Seed v1 = Standard (symbol 20, cash 30, halt 5, privilege `all_orders`) | `done` | One active settings row. Orchestrator can read it. |
| P1-04 | `apps/orchestrator`: asyncio loop, log `INIT` then `IDLE`, load active settings, health log, sleep | `done` | Process stays up, logs both states, then sleeps. No orders. |
| P1-05 | `.env.example` remains the bootstrap contract. Document `uv run` on Mac against local Postgres | `done` | README steps boot the loop against `DATABASE_URL`. |
| P1-06 | `deploy/windows` service sketch only. No Windows service install | `done` | Sketch names the process to start. Nothing is installed as a service. |

## Phase 2 — Paper HITL

Exit: draft, then Approve, then a simulated fill, then an audit row. No live orders.

| ID | Task | Status | Exit proof |
|----|------|--------|------------|
| P2-01 | iTick writes bars for a small watchlist | `done` | NVDA, QCOM, and T bars are stored with `source=seed` when `ITICK_API_KEY` is empty. |
| P2-02 | IB slot clock. Mobile hours log disconnected. Athena hours log connected, read-only | `done` | 20:00 HKT is mobile, 22:00 HKT is athena, 15:00 HKT is idle. No Gateway is attached in this phase. Position sync is Phase 3. |
| P2-03 | NFR phase and tag stubs. Draft generator. No live orders | `done` | One pending NVDA `OPEN_STRANGLE` draft, phase P2, tags `NEWS_WASH,HIGH_IV`. No IB order is sent. |
| P2-04 | Risk Guard on drafts | `done` | A 9-contract draft against Standard max 3 is stored as `blocked`. |
| P2-05 | Telegram notify plus Approve and Reject. Feishu stays stubbed | `done` | `@athena_hk_bot` sends the draft. Allowlisted Approve and Reject update the paper draft. Feishu stays a log line. |
| P2-06 | One-time Approve page | `done` | `GET/POST /c/<token>` records the decision once. The second use returns 410. `ATHENA_TUNNEL=cloudflared` publishes that page on a `trycloudflare.com` URL. Vercel signed webhook waits on Tailscale. |
| P2-07 | Local backup job | `done` | `athena-*.json` lands in `ATHENA_LOCAL_BACKUP_DIR` (`var/backups` by default). |

## Phase 3 — Live approve-gated

Exit: one small live trade on the configured account with Approve on every order. A Mobile slot does not corrupt state.

| ID | Task | Status | Exit proof |
|----|------|--------|------------|
| P3-01 | `ib_insync` submits an approved limit | `done` | Unapproved, paper, Mobile-slot, and contract-less drafts never call `placeOrder`. An approved stock limit is submitted only when the gate is open. No live account order has been sent: mode is still `paper`, the account id is empty, and Gateway is not running. |
| P3-02 | Fill callbacks update positions | `done` | A broker fill writes `simulated=false` and a position with the same quantity and price. |
| P3-03 | `/halt`, reject-streak halt, daily loss halt | `done` | Three rejects, a 5% NAV drop, or `/halt` block new drafts. `/resume` clears the halt. |
| P3-04 | MinIO sync through the existing S3 env vars | `done` | `athena/windows-box/phase3-proof.json` is in the `athena-backups` bucket on `127.0.0.1:9000`. |
| P3-05 | Ops agent can restart Gateway and Athena | `done` | A non-allowlisted actor is refused. `/ops athena restart` schedules a process exit. Gateway restart does nothing while `ATHENA_GATEWAY_RESTART_CMD` is empty. |

## Phase 4 — NFR and news

Exit: one full event cycle, paper or tiny live, with a settings change written to the audit trail. A knob change writes `settings_audits`. Privilege stays `all_orders`.

| ID | Task | Status | Exit proof |
|----|------|--------|------------|
| P4-01 | Full phase machine and CSP, CC, Strangle rules | `done` | One symbol walks P1 HOLD, P2 OPEN_STRANGLE, P3 HOLD, P4 CLOSE, P4 OPEN_CSP, P5 OPEN_CC. Each pair is stored on `symbol_phases`. |
| P4-02 | Yahoo, Finviz, Fed, earnings intake. NEWS_WASH density | `done` | Yahoo and Finviz headlines are stored. NVDA and T are tagged `NEWS_WASH` from those headlines. FOMC dates are stored. Earnings dates are stored when Nasdaq lists a watchlist name. |
| P4-03 | DSH sentiment enrichment | `deferred` | Optional. Engine still runs if DSH is absent. |
| P4-04 | Roll and profit-take drafts | `done` | A short option at or under 21 DTE drafts `ROLL`. A short that has captured 50% of premium drafts `CLOSE`. Both stay pending until Approve. |
| P4-05 | Settings change audit | `done` | `/settings roll_dte 14` writes a new settings version and a `settings_audits` diff. Privilege cannot be changed on this path. |
| P4-06 | Expire unapproved drafts | `done` | A pending draft older than `draft_ttl_minutes` (default 60) becomes `expired`. A fresh pending draft and an old approved draft stay. Telegram is told. No order is sent. |

## Phase 5 — Hardening

| ID | Task | Status | Exit proof |
|----|------|--------|------------|
| P5-01 | Second and third IB account | `done` | `paper` uses `ATHENA_IB_PAPER_ACCOUNT_ID`. `live` uses each id in `ATHENA_IB_ACCOUNT_IDS`. Switching the env changes the active book. Nothing is deleted. |
| P5-02 | fanit fill export | `deferred` | Athena stays the engine source of truth until then. |
| P5-03 | Privilege `opens_only` | `deferred` | Only after live metrics are stable. Dual confirm required. |
| P5-04 | Tier B and C news, including social | `deferred` | Blocked on a compliance review. |
| P5-05 | Nautilus Trader | `deferred` | Only if `ib_insync` cannot carry execution. |
| P5-06 | Cloud trading host | `deferred` | Not the primary runtime. |
