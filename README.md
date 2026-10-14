# Athena Options Engine

[![CI](https://github.com/shuurai/athena-options-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/shuurai/athena-options-engine/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)

Windows-first, human-in-the-loop **options engine** for Interactive Brokers.
Athena refines **one** strategy for the long haul: the **News Front-Running
(NFR) cycle**. Develop on macOS; run on a dedicated Windows box next to IB
Gateway or TWS.

**This is trading software.** You can lose money. Paper and live both send real
broker orders after Approve. Review risk limits and privilege before enabling
trading.

---

## What Athena does

Athena watches a configurable universe, scores news and price structure, drafts
tactics (cash-secured puts, covered calls, share buys, rolls, closes), runs them
through a **risk guard**, and parks drafts for **human Approve**. Only then does
the exec layer place orders through Interactive Brokers.

| Layer | Role |
|-------|------|
| Orchestrator | ~5-minute loop: sync, draft, notify, expire |
| Strategy (NFR wheel) | Tags, phases, openings — CSP / CC / buy / hold / roll / close |
| Risk + Settings | Versioned limits, privilege, slots, notify prefs |
| AI agents | Optional DeepSeek calls (Narrator, news scoring, standing prompts) |
| Exec (`ib_insync`) | History, chains, positions, order send after Approve |
| Control | Local console, Approve page, Telegram (Feishu stub) |

Screen mockups in `design/` are reference only. They are not the running app.

---

## Core concepts

### News Front-Running cycle

One product, many tactics. Phases label where a name sits in the cycle; the
**wheel** writes the ticket. Openings such as `dip_csp`, `high_cc`, `dip_long`,
and signaled short premium are decided in code. The Narrator can gate whether to
open a draft — it does **not** invent strikes.

### Human-in-the-loop

Default privilege is `all_orders`: every open, roll, and close needs Approve
(console, Telegram, or an ephemeral control link). Privilege can be raised later
under Settings with audit; emergency halt still stops trading.

### Account books

`ATHENA_TRADING_MODE=paper` uses `ATHENA_IB_PAPER_ACCOUNT_ID`. `live` uses each id
in `ATHENA_IB_ACCOUNT_IDS`. Each account keeps its own drafts, orders, fills,
positions, and settings versions. No broker id is hard-coded in the engine.

### Slots

Settings define when Athena may hold the API client versus when you use Mobile
IB. Typical HKT windows: HK cash sessions, US regular hours, and mobile gaps.
Athena reconnects when a mobile session ends.

---

## AI in Athena

AI is an assistant to the wheel, not the exchange.

- **News scoring** — headlines can be scored (DeepSeek / optional DSH) and stored
  as events; adversarial or low-quality noise is filtered over time.
- **Standing agents** — Narrator, CSP, Call, and related prompts live in
  versioned Settings. They receive tags, strategy knobs, and risk context.
- **Narrator open gate** — when the wheel has a candidate, Narrator may return
  whether to open a draft (`open: true|false`). Quiet means no draft.
- **Audit trail** — model calls, decisions, and reasons show on `/audit`.

Secrets (`DEEPSEEK_TOKEN`, bot tokens, market-data keys) belong in `.env` or the
Settings UI — never in git. Empty tokens disable those features without breaking
the deterministic path.

---

## Options and risk controls

Athena sells and manages short option premium (and related stock legs) under
hard limits. Risk Guard runs on every draft, independent of privilege.

| Control | What it caps |
|---------|----------------|
| `max_symbol_pct_nav` | Exposure in one underlying |
| `max_short_opt_margin_pct` | Short-option margin vs NAV |
| `cash_reserve_pct` | Cash floor for CSP collateral |
| `max_contracts_per_symbol` | Contract count per name |
| `max_position_per_symbol` | Notional cap (ticker currency; 0 = off) |
| `max_orders_per_day` | **Per-symbol** daily order count |
| `min_rv_pctl_new_short` / `min_dte_new` | Volatility and DTE floors for new shorts |
| `earnings_blackout_days` | No new opens near earnings |
| Cycle budget | Closes first, then dip buys / CCs, then the rest across the book |

Named profiles (`defensive`, `standard`, `income`, `aggressive`, `paper`) fill
exposure knobs. Any other mix is `custom`. Covered-call sizing respects free
shares; conflicting shorts are blocked unless Settings allow them.

Full surface: [docs/athena/04-settings-and-risk.md](docs/athena/04-settings-and-risk.md).

---

## Requirements

### Runtime

| Requirement | Notes |
|-------------|--------|
| **Python 3.12+** | Workspace managed with [uv](https://docs.astral.sh/uv/) |
| **Postgres 16** | Local store (docker defaults in `.env.example`) |
| **Interactive Brokers Gateway or TWS** | API enabled; paper and/or live accounts |
| **Network** | Box can reach IB, optional iTick / DeepSeek / Telegram |

### Interactive Brokers

1. Install **IB Gateway** (headless-friendly) or **TWS** on the trading machine.
2. Enable API access; note host/port (`IB_GATEWAY_HOST`, `IB_GATEWAY_PORT`,
   typically `4001`/`4002` for Gateway paper/live — confirm in your install).
3. Set a stable Athena `IB_CLIENT_ID` that does not collide with other tools.
4. Put paper and live account ids in `.env` (never commit them).
5. Respect **mobile slots**: opening Mobile IB can disconnect the API client;
   Athena is built to idle and reconnect around those windows.

Athena uses IB for daily bars, option chains, account sync, streaming quotes, and
execution. Optional iTick is a separate live-print path outside the IB daily
window.

### IB market data subscriptions (for streaming quotes)

Subscribe in **IB Account Management → Settings → User settings → Market Data
Subscriptions**. Packages are billed per **username** (shared by paper and live
under that login). Names below match IBKR’s usual catalog labels; IB renames
bundles occasionally — pick the Level I / Top-of-Book line for each venue.

Athena’s API client currently requests **delayed** market data
(`reqMarketDataType(3)`). That still needs exchange entitlements for the watchlist
to stream into the cockpit, universe, positions, and orders pages. Without the
right package, IB returns permission errors (e.g. code **162** / no market data)
and rows stay frozen on the last stored print.

| Your universe | Subscribe (Level I / streaming Top of Book) | Why Athena needs it |
|---------------|---------------------------------------------|---------------------|
| **US stocks** (SMART / USD) | **NYSE (Network A/CTA)**, **NASDAQ (Network C/UTP)**, and **NYSE American (Network B/CTA)** — or the combined **US Equity and Options Add-On Streaming** (or similar) bundle that includes those three | Streaming last/bid/ask for US names; pulse and live P&L |
| **US equity options** | **OPRA** (US Options Exchanges Top of Book), usually in the same US equity+options add-on | Option chain asks, CSP/CC limit marks, option quotes |
| **Hong Kong stocks** (SEHK / HKD digit codes) | **Hong Kong Securities Exchange (Stocks, Warrants & CBBCs)** Level I | Streaming quotes for HK names (`700`, `1810`, …) |
| **Canada stocks** (if used) | **Toronto Stock Exchange (TSX)** Level I | CAD SMART names |

**Historical daily bars (HMDS)** — Athena also pulls IB history for indicators and
backfill. History often works with the same equity entitlements; if you see
permission **162** on history while quotes work, enable the US/HK equity package
that includes historical for that venue (IB’s snapshot/value bundles alone may
not cover every permission).

**Practical defaults**

| Goal | What to buy |
|------|-------------|
| US NFR book (stocks + short options) | US equity Network A/B/C **and** OPRA (or one US equity+options streaming add-on) |
| HK + US book | Above **plus** Hong Kong Securities Exchange Level I |
| Quotes “good enough” without paying | IB free **delayed** data where available — cockpit still updates, but prints lag (~10–15 minutes on US). Athena is built around that delayed path today. |
| Non-IB live prints | Optional `ITICK_API_KEY` — does not replace IB for chains, history, or orders |

After changing subscriptions, restart Gateway/TWS (and Athena) so the session picks
up the new entitlements. Confirm in TWS/Gateway that a watchlist symbol shows a
live or delayed last without a market-data warning before expecting Athena pulse
rows to move.

### Optional

| Piece | Purpose |
|-------|---------|
| DeepSeek token | News + Narrator / agent calls |
| Telegram bot | Draft cards, Approve, fill notices |
| iTick API key | Optional live prints outside IB delayed path |
| cloudflared | Public Approve URL (`ATHENA_TUNNEL=cloudflared`) |
| MinIO / S3 | Offsite backup of local snapshots |

---

## How to run

### 1. Database

```bash
docker run -d --name athena-pg \
  -e POSTGRES_USER=athena \
  -e POSTGRES_PASSWORD=athena \
  -e POSTGRES_DB=athena \
  -p 55432:5432 \
  postgres:16
```

### 2. Environment

```bash
cp .env.example .env
# Edit .env: DATABASE_URL, ATHENA_TRADING_MODE, IB_* , account ids, optional tokens
uv sync
```

**Do not commit `.env`.**

### 3. Start the orchestrator

With Gateway/TWS running and API listening:

```bash
uv run athena-orchestrator
```

On start Athena logs `state=INIT`, applies migrations, seeds Settings v1 if
needed, then `state=IDLE` and a health line. The loop sleeps for
`ATHENA_LOOP_SECONDS` (or Settings “Cycle minutes”).

One-shot smoke (no long sleep):

```bash
ATHENA_LOOP_CYCLES=1 ATHENA_LOOP_SECONDS=1 uv run athena-orchestrator
```

### 4. Operator UI

| URL | Purpose |
|-----|---------|
| `http://127.0.0.1:8788` | Console (cockpit, universe, drafts, positions, orders, history, settings) |
| Control port (`ATHENA_CONTROL_PORT`) | Ephemeral Approve page; optional Cloudflare tunnel |

Configure watchlist and risk on **Settings**, enable symbols on **Universe**,
Approve on **Drafts** / Telegram when a card appears.

---

## How to develop

Monorepo layout (uv workspace):

```
apps/
  orchestrator/   # main loop, console HTTP, paper/live session glue
  exec-ib/        # ib_insync broker, order watch
  bots/           # Telegram (and Feishu stub)
packages/
  strategy-nfr/   # wheel, tags, drafts, agents, screener
  risk/           # risk guard + cycle budget
  settings/       # versioned payloads
  store/          # migrations + Postgres helpers
  marketdata/     # bars / quotes helpers
docs/athena/      # design docs and trackers
```

Useful commands:

```bash
# Pure unit tests (no IB, no database) — same suite as CI
uv run --package athena-orchestrator python -m unittest apps.orchestrator.tests.test_rules -q

# Compile + entrypoint check
uv run python -m compileall -q apps packages
uv run python -c "from athena_orchestrator import main; assert callable(main)"

# Changelog ledger when you add logs/YYYY-MM-DD.md entries
node scripts/check-changelog.mjs
```

DB-backed workflow tests live under `apps/orchestrator/tests/test_workflows.py`
and need a migrated Postgres. Prefer fixture accounts (`ZZTEST`, `PAPER1`, `U1`)
in docs and tests — never personal live ids.

See [CONTRIBUTING.md](CONTRIBUTING.md) for PR expectations.

---

## Configuration cheat sheet

| Mode | Account env |
|------|-------------|
| `ATHENA_TRADING_MODE=paper` | `ATHENA_IB_PAPER_ACCOUNT_ID` |
| `ATHENA_TRADING_MODE=live` | `ATHENA_IB_ACCOUNT_IDS` (comma-separated) |

| Variable | Role |
|----------|------|
| `DATABASE_URL` | Postgres |
| `IB_GATEWAY_HOST` / `IB_GATEWAY_PORT` / `IB_CLIENT_ID` | TWS/Gateway API |
| `DEEPSEEK_TOKEN` | AI calls (optional) |
| `TELEGRAM_BOT_TOKEN` / allowlist | Notifications + Approve |
| `ITICK_API_KEY` | Optional live quotes |
| `ATHENA_CONSOLE_PORT` | Operator UI (default `8788`) |
| `ATHENA_CONTROL_PORT` / `ATHENA_TUNNEL` | Approve page + optional tunnel |

Full template: [`.env.example`](.env.example).

---

## Documentation

| Doc | Contents |
|-----|----------|
| [docs/athena/README.md](docs/athena/README.md) | Proposal index and locked decisions |
| [docs/athena/02-architecture.md](docs/athena/02-architecture.md) | Planes, monorepo, process map |
| [docs/athena/03-strategy-news-frontrun.md](docs/athena/03-strategy-news-frontrun.md) | NFR phases and tactics |
| [docs/athena/04-settings-and-risk.md](docs/athena/04-settings-and-risk.md) | Risk, privilege, profiles |
| [docs/athena/05-human-control.md](docs/athena/05-human-control.md) | Bots and Approve UX |
| [docs/athena/06-data-and-ib.md](docs/athena/06-data-and-ib.md) | IB vs iTick, slots, sync |
| [SECURITY.md](SECURITY.md) | Vulnerability reporting and secrets |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Dev setup and checks |

---

## License

[MIT](LICENSE)
