# Athena · Design ideation

Offline HTML mockups for **Athena Options Engine** control surfaces.  
Visual language adapted from `reference/design` (Naviator tokens) — that folder is **gitignored**; copy standards from there locally.

## How to view

Open [`index.html`](./index.html) in a browser (no build step).

## Canvases

| # | Screen | Purpose |
|---|--------|---------|
| 0 | [Design base](./00-m0-design-base/) | Tokens + components + toast/modal/panel/loading demos |
| 1 | [Cockpit overview](./01-cockpit-overview/) | NLV/P&L charts, IB slots, health, watchlist sparklines |
| 2 | [Draft approve](./02-draft-approve/) | HITL draft queue, confirm modals, toasts |
| 3 | [Ephemeral control](./03-ephemeral-control/) | One-time Approve / Amend web page, TTL drain |
| 4 | [Settings](./04-settings-risk/) | IBKR, iTick, news API, AI API, notify, strategy shape |
| 11 | [Risk control](./11-risk-control/) | Risk profiles plus limits and approval privilege |
| 5 | [NFR phase board](./05-nfr-phase-board/) | Phase rail, IVR chart, phase distribution |
| 14 | [Strategy](./14-strategy/) | Editable system and execution prompts. Intent is per symbol on Universe |
| 13 | [AI audit](./13-ai-audit/) | Latency, tokens, call meta, and the JSON response |
| 6 | [Telegram link](./06-telegram-connect/) | Settings page: login widget or one-time start link |
| 7 | [News wash](./07-news-wash/) | Density/source charts, event modals, manual ingest |
| 8 | [History](./08-history/) | 6-month ledger, filters, row modals, CSV export |
| 12 | [Positions](./12-positions/) | IBKR stock and option book, filters, row detail |
| 9 | [Universe](./09-universe/) | Symbol pool, tactic toggles, screening profile |
| 10 | [Accounts](./10-accounts/) | Switch active IB account, per-account toggles |

Each canvas: `index.html` (visual) + `spec.md` (structure & copy).

## Interaction layer

[`shared/athena.js`](./shared/athena.js) provides:

- `Athena.toast(title, sub, kind)` — ok / err / info / warn
- `Athena.modal({ title, body, footer })` — sharp-corner dialog
- `Athena.panel({ title, body })` — right slide-in
- `Athena.confirmAction(title, body, onConfirm)` — gated confirms (halt / approve / privilege)
- `Athena.simulateLoading(el, done, ms)` — shimmer + spinner
- `Athena.sparkline(el, values, opts)` / `Athena.bars(el, values, opts)` — inline SVG charts
- `Athena.DATA` — 6-month fixtures (NLV weekly, premium, P&L, IVR, trades, win rate)
- `Athena.historyRows(n)` — generated fill ledger

Buttons with `data-action="approve|reject|halt|save|ops|backup|amend|link"` toast automatically; `approve` and `halt` open confirm modals first.

## Rules

- Realistic trading copy and numbers (ideation fixtures — not live account data).
- Use [`tokens/tokens.css`](./tokens/tokens.css); register new colors there first.
- Angled UI: sharp corners (2–6px), clipped accents, dark console default.
- IB account id is **masked / configurable** in UI — never treat one hard-coded account as product truth.
