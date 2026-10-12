# 10 — Implementation tracker

Checklist for building Athena. Architecture stays in [02-architecture.md](02-architecture.md). This file only tracks work.

**Status values:** `not_started` · `in_progress` · `done` · `blocked` · `deferred`  
**Last updated:** 2026-10-12

## Current product rules

These are the live rules for price, safety, audit, indicators, and the strike menu. They are specified in [03](03-strategy-news-frontrun.md), [04](04-settings-and-risk.md), [05](05-human-control.md), and [06](06-data-and-ib.md). Older rows below keep the proof written on that day. Where a row disagrees with this list, this list wins.

- One daily bar. A same-day delayed quote updates the close only. `seed` bars are not written. Short history uses `HIGH_RANGE`.
- A covered call needs enough shares. A short put and a covered call may coexist unless `strict_conflict` is on. A ticket that breaks the cash reserve blocks every leg.
- Audit outcomes are `drafted`, `quiet`, `no_data`, `invalid`, `observed`, and `dropped`. A bad model reply is retried once. Fewer than 63 daily bars is `no_data`.
- `HIGH_IV` is not produced. The volatility tags are `RV_HIGH` and `RV_LOW`. `estimated_iv()` is empty. At-the-money mids are stored on `chain_marks` and not read.
- The strike menu is ranked by a delta target. The fetch width is `chain_band_pct` (25). The old fixed distance cut is not read. The RV multiple is an assumption, not implied volatility.
- The wheel is the only opening decision. Reasons are `signaled_strangle`, `high_cc`, `cover_gap`, `dip_long`, and `dip_csp`. `NAKED_CALL` and `MULTI_CONFLICT` open nothing. `symbol_phases.tactic` stays `HOLD`.
- After the drafts, the cycle budget walks closes and rolls, then `dip_long`, then `high_cc`, then the rest. A band is kept or blocked together. Caps are bucket 35, assignment 40, cash after assignment 10.
- `/book` plots `RV20` against premium yield. It does not draw a market line. Positions are grouped by intent.
- Athena windows are HK cash 09:30–12:00 and 13:00–16:00, plus US regular 21:30–04:00 HKT. Mobile is 19:00–21:30. Friday's US session stays open until Saturday 04:00. Monday before 04:00 stays closed. Lunch stays idle. Drafts use the Athena window. Observe default is weekdays from 07:00.

A row is `in_progress` before the code changes. The matching check in the test tracker starts as `pending`.  
**Next:** Tailscale and the Vercel host stay deferred.

`design/` is the visual reference for the console. It is not the runtime. The tasks are in Console web below.

## Locked for Phase 1

- Strategy: News Front-Running only. Tactics: CSP, CC, Strangle, Hold, Roll, Close.
- Privilege default: `all_orders`. Every open, roll, and close needs Approve.
- Python 3.12+. Workspace tool: **uv** (not Poetry).
- IB adapter later: `ib_insync`. Nautilus is deferred.
- Store: local Postgres. Timescale waits until bar volume needs it.
- Redis waits until approve tokens need it. Phase 1 does not use it.
- Console web: local operator pages first. The one-time phone page is already live. Vercel waits on Tailscale.
- Bots later: Python long-poll on the box. Feishu after Telegram.
- Live quotes: the newest IB delayed print. IB also supplies daily bars, positions, and orders, inside Athena slots. iTick is outside the daily window.
- Backup later: local files, then MinIO via the S3 env vars.
- Settings: versioned Postgres rows. Env vars are secrets and bootstrap only.
- IB account: `ATHENA_IB_PAPER_ACCOUNT_ID` in paper mode, `ATHENA_IB_ACCOUNT_IDS` in live mode. fanit sync is later.

## Open, not blocking the skeleton

| Item | Status | Needed by |
|------|--------|-----------|
| Weekday Athena vs Mobile slot hours | `done` | HK cash 09:30–12:00 and 13:00–16:00, US regular 21:30–04:00 HKT, Mobile 19:00–21:30. Lunch is idle. Saturday 02:00 is still Friday's US session. Monday 02:00 is idle. |
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

Exit: draft, then Approve, then a real order on the paper account, then an audit row. No live-account orders.

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
| P4-01 | Full phase machine and CSP, CC, Strangle rules | `done` | One symbol walks P1 through P5. `symbol_phases.tactic` stays `HOLD`. Opening is the wheel, not a stored tactic. |
| P4-02 | Yahoo, Finviz, Fed, earnings intake. NEWS_WASH density | `done` | Yahoo and Finviz headlines are stored. NVDA and T are tagged `NEWS_WASH` from those headlines. FOMC dates are stored. Earnings dates are stored when Nasdaq lists a watchlist name. |
| P4-03 | DSH sentiment enrichment | `done` | `DSH_BIN` runs `dsh --profile headless` on new headlines. A keyword −0.6 became −0.2, and a keyword 0.4 became 0.8. An empty `DSH_BIN` leaves the keyword score. |
| P4-04 | Roll and profit-take drafts | `done` | A short option at or under 21 DTE drafts `ROLL`. A short that has captured 50% of premium drafts `CLOSE`. Both stay pending until Approve. |
| P4-05 | Settings change audit | `done` | `/settings roll_dte 14` writes a new settings version and a `settings_audits` diff. Privilege cannot be changed on this path. |
| P4-06 | Expire unapproved drafts | `done` | A pending draft older than `draft_ttl_minutes` (default 60) becomes `expired`. A fresh pending draft and an old approved draft stay. Telegram is told. No order is sent. |
| P4-07 | Fill notice | `done` | A new execution returns a fill row and a Telegram message with the price. An IB status of Filled with no print still sends a notice. No live fill is open, so no message was sent to the chat. |
| P4-08 | `/slots`, `/backup now`, positions on `/status` | `done` | `/slots` shows the weekday Athena and Mobile windows and the current slot. `/status` says no open position. `/backup now` wrote a snapshot and synced it. |
| P4-09 | Draft card shows the contract | `done` | A Tencent strangle card lists expiry 2026-11-27, class TCH × 100, put 380 limit 3.5, and call 470 limit 5.1. The approve page shows the same lines. |
| P4-10 | Amend quantity and limits | `done` | The approve page edits contracts and leg limits. A 2-contract amend queued put 4.5 and call 6.5. Nine contracts is refused and the link stays open. Tactic stays. Strike and expiry are P4-11. |
| P4-11 | Amend a listed strike and expiry | `done` | The page offers expiries from 21 to 60 days and a short strike list. A fixture moved to 2026-12-30, put 360, call 490. An unlisted put was refused. A close cannot change its contract. |
| P4-12 | Store the reject reason | `done` | `/reject <id> [reason]` writes the words on the approval. Fixture `draft_zzrej` stored `too wide`. An empty reason still rejected `draft_zzrej2`. Both rows were deleted. |
| P4-13 | Buttons on `/drafts` | `done` | Each pending draft is its own message with Approve and Reject. Fixtures `draft_zzbtn` and `draft_zzrisk` had both buttons. Rows deleted. |
| P4-14 | Risk line on the draft card | `done` | One contract says Risk pass. Nine contracts says Risk fail because contracts exceed max_contracts_per_symbol. Rows deleted. |
| P4-15 | Price-weak and volume-spike tags | `done` | A falling series with double volume is `PRICE_WEAK` and `VOLUME_SPIKE`, and that pair moves the phase to P2. News wash plus high IV still opens the strangle. |

## Console web

The phone page at `/c/<token>` is already live. This section is the operator console from `design/`. It reads the active account book. Approve and Reject use the same decision path as Telegram. The console does not place an order by itself. Connect is not a nav item. A risk profile fills limits and does not change privilege. Vercel hosting waits on Tailscale.

| ID | Task | Status | Exit proof |
|----|------|--------|------------|
| CW-01 | Console shell | `done` | `http://127.0.0.1:8788` has Cockpit, Drafts, NFR board, News, Universe, History, Accounts, Risk control, and Settings. System font. No Connect item. The phone port does not serve these pages. |
| CW-02 | Cockpit | `done` | Paper account `DUR253115`, the current slot, pending drafts, and the watchlist phase. Latest NAV says no mark is stored. |
| CW-03 | Draft queue | `done` | Fixture `draft_zzcon` showed Approve and Reject. Reject stored `console check`. The row was deleted. |
| CW-04 | Phase board | `done` | The watchlist page lists phase, tactic, and tags from `symbol_phases`. |
| CW-05 | News | `done` | Watchlist headlines show the stored sentiment. |
| CW-06 | Settings and risk | `done` | Defensive filled the limits, then Standard was restored. Privilege stayed `all_orders`. Connections show set or empty, not the secrets. |
| CW-07 | Accounts | `done` | The page shows the active book and the configured ids. It does not switch mode. |
| CW-08 | History | `done` | Fills and approvals for the active account are listed. |
| CW-09 | Universe | `done` | The watchlist matches settings. |
| CW-28 | Strategy prompts | `done` | `/strategy` shows seven collapsed agent cards, the cycle diagram, and a help popover. A Location prompt save was restored. Privilege stayed `all_orders`. |
| CW-29 | AI audit | `done` | `/audit` lists time, agent, symbol, latency, tokens, and result. The empty book says no model calls yet. |
| CW-30 | Universe intent | `done` | Each of the 28 symbols has Intent and Long. NVDA was set to Accumulate and restored to Income. |
| CW-10 | Vercel host | `deferred` | After Tailscale. The local console does not wait on this. |

## Console fidelity

CW-02 is the first data shell. These rows make the cockpit match `design/01-cockpit-overview`. Fixture numbers stay in `design/`. A missing series shows an empty state.

| ID | Task | Status | Exit proof |
|----|------|--------|------------|
| CW-11 | Design shell | `done` | All nine pages on port 8788 use the design shell and the Strangle mark. Connect is absent. Port 8787 `/risk` is 404. |
| CW-12 | Cockpit stats and charts | `done` | Sleeve NLV and premium show a dash. The page does not show the fixture `HK$226,480`. |
| CW-13 | Today's schedule and slot save | `done` | Friday evening shows Mobile 19:00–21:30 as Reserved. A slot save wrote settings version 10, then version 11 restored the windows. Privilege stayed `all_orders`. |
| CW-14 | Health and ops actions | `done` | `/api/health` reported 4/5: iTick is not configured, Telegram is polling. Backup wrote a snapshot and synced it. Gateway restart reported an empty command. Halt and resume on `ZZHALT` were deleted. The paper account was not halted. |
| CW-15 | Watchlist pulse | `done` | NVDA shows last 218.42, phase P1, IV rank dash, and side FLAT. |
| CW-16 | Draft cards | `done` | An empty queue shows one card. A fixture card had Approve, Amend, Reject, and a live control link. The reject reason appeared under Recent decisions, then the fixture was deleted. |
| CW-17 | NFR board | `done` | Five phase cards, an NVDA IV series with rank dash, phase counts, and the watchlist table render on port 8788. |
| CW-18 | News intake | `done` | T is the densest name. A ZZPAGE headline scored −0.6, then a false alarm dropped it from the wash count. The row was deleted. |
| CW-19 | Universe pool | `done` | Turning NVDA Strangle off saved a version and `propose` returned none. Version 13 restored the watchlist, slots, and privilege `all_orders`. |
| CW-20 | History filters | `done` | Filters and CSV return 200. Six-month premium and monthly net are 0. The fixture amount is absent. |
| CW-21 | Account cards | `done` | The paper card shows the full id, mode, NLV dash, and position count. It has no switch button. A book query opens a read-only panel. |
| CW-22 | Risk editor | `done` | Profile cards and editable limits render. Privilege choices other than `all_orders` are visible and disabled. The chip is v13. |
| CW-23 | Settings sections | `done` | IB, market data, news, AI, notify, and strategy render. The bot, allowlist, and polling are shown. The bot token stays set, not printed. |
| CW-24 | Live settings override | `done` | A saved iTick token, model, and Telegram id override the environment. The audit redacts secrets. Gateway host and port stay read-only. |
| CW-25 | Settings badges and Telegram link | `done` | Cards use ON, OFF, SET, and EMPTY badges. An unlinked bot offers a one-time start link. A linked id is shown. |
| CW-26 | Add account dialog | `done` | The control opens the mock dialog: account id, label, Cancel, and Add account. A new id is stored and stays off the Gateway session. |
| CW-27 | Account sync and trading toggles | `done` | Sync and Trading are buttons. Sync refreshes marks. Trading gates new drafts and queued orders. |

## Still specified

These are in the Athena docs and are not built. They do not start before CW-01.

| ID | Task | Status | Exit proof |
|----|------|--------|------------|
| P4-16 | Apply the stored risk limits | `done` | A draft is blocked for symbol NAV, cash reserve, margin, IV rank, DTE, earnings blackout, or the daily order cap. Nine contracts is still blocked. Missing marks are skipped. |
| P4-17 | Strike distance and DTE band from settings | `done` | A 20% distance on a price of 100 picks put 80 and call 120. The open band still uses the settings DTE window. |
| P4-18 | No second short put | `done` | An existing short put blocks a new cash-secured put (`I3: duplicate short put`). |
| P4-18b | Wheel still allows a cash-secured put beside a short call | `done` | An existing short call still allows a new cash-secured put. A covered call still needs enough shares. |
| P4-19 | Fills Athena did not draft | `done` | Fill `exec-zz` was stored as `EXTERNAL`, the position updated, and a second copy was ignored. Rows deleted. |
| P4-20 | Cash and margin snapshot | `done` | A test mark stored cash 1000 and margin 100, then the row was deleted. An Athena slot writes the live values. |
| P4-21 | Manual news forward | `done` | Three pasted `ZZNEWS` headlines tagged `NEWS_WASH`. Rows deleted. |
| P4-22 | Phase tags on the draft card | `done` | Fixture `draft_zztag` listed `NEWS_WASH` and `HIGH_IV`. The row was deleted. |
| P4-23 | Read settings by domain | `done` | `/settings get risk` prints the limits and `privilege all_orders`. |
| P4-24 | Blocked symbols | `done` | `ZZBLK` on the deny list produced no draft. |
| P4-25 | Strangle with shares | `done` | `NEWS_WASH` and `RV_HIGH` on a symbol that allows a strangle, with at least 100 shares, drafts `OPEN_CSP` then `OPEN_CC`. Under 100 shares it does not. |
| P4-26 | Cycle budget | `done` | Two opens that together break the cash reserve are both blocked. A full `AI_NARRATIVE` bucket blocks the next ticket. A put that would leave cash under 10% of NAV is blocked. `/book` shows realized risk and prints `market_unknown`. |
| P5-07 | Ops health and backup | `done` | `/ops health` checks Orchestrator, iTick, Store, backup, and Telegram. Backup writes a local snapshot. Halt and resume apply to the active account. |
| P5-08 | Backup retention | `done` | A snapshot older than 14 days was removed. The newest file stayed. |
| P5-09 | Reconnect only in an Athena slot | `done` | An Athena slot retries a failed Gateway login. A Mobile slot disconnects and does not retry. |

## Phase 5 — Hardening

| ID | Task | Status | Exit proof |
|----|------|--------|------------|
| P5-01 | Second and third IB account | `done` | `paper` uses `ATHENA_IB_PAPER_ACCOUNT_ID`. `live` uses each id in `ATHENA_IB_ACCOUNT_IDS`. Switching the env changes the active book. Nothing is deleted. |
| P5-02 | fanit fill export | `deferred` | Athena stays the engine source of truth until then. |
| P5-03 | Privilege `opens_only` and `auto_within_risk` | `deferred` | Only after live metrics are stable. Dual confirm required. `paper_only` is not a separate mode: paper already sends a real paper-account order. |
| P5-04 | Tier B and C news, including social | `deferred` | Blocked on a compliance review. |
| P5-05 | Nautilus Trader | `deferred` | Only if `ib_insync` cannot carry execution. |
| P5-06 | Cloud trading host | `deferred` | Not the primary runtime. |
