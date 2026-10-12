# 01 — Proposal: Goals, Non-Goals, HITL

## Purpose

Build **Athena Options Engine (AOE)**: a local, settings-driven system that runs the **News Front-Running Cycle** on a Windows trading machine, with you as human-in-the-loop for trading decisions, while automating order plumbing, position sync, IB session management, and ops restart when you are away.

Goal horizon: refine this one strategy so a dedicated options income sleeve can compound for **5–10 years** (and beyond), with less emotion and fewer impulse trades.

## Goals

1. **One strategy, deep refinement** — News Front-Running Cycle. The wheel writes CSP, CC, share, roll, and close tickets. Phases stay as context.
2. **Automate the boring, gate the dangerous** — Sync, monitoring, draft orders, rolls candidates, risk checks automated; execution gated by Settings privilege (default: approve every order action).
3. **Remove impulse** — No silent live orders at default privilege. Comments and improvisation happen via bots / control pages / settings versions, not ad-hoc IB clicks mid-panic.
4. **Remote operable** — Feishu + Telegram for status and Approve/Reject; one-time web views for richer selections; ops agent can restart IB Gateway / Athena services.
5. **Survive laptop lifestyle** — Engine runs on Windows box; Mac is for development and occasional control.
6. **Durable state** — Local DB + local backups first; S3-compatible offsite sync second.
7. **Honest data split** — IB daily bars for indicators, one day per row. A delayed print may update that day’s close and is the draft reference price. iTick is not part of the daily window. IB also executes and syncs positions inside Athena slots.

## Non-goals (MVP and near-term)

- Strategy marketplace / dozens of parallel strategies
- Cloud as primary trading runtime
- Fully unattended live trading on day one
- Replacing fanit-cli as your human portfolio UI immediately
- Hard dependency on Nautilus Trader for MVP
- Full WeChat / Rednote / TikTok scraping on day one (hooks only; ToS-safe sources first)
- Extra IB market data subscriptions until the sleeve is profitable enough to justify them
- Implied-vol tags. Chain mids may be stored. They are not a reason to open
- A portfolio optimizer. Caps and the read-only book are the combination view

## Human-in-the-loop philosophy

Athena proposes; you dispose — until Settings explicitly raise privilege.

| Layer | Machine | Human |
|-------|---------|-------|
| Scan / tags / phases | Auto every ~5 minutes | Review summaries |
| Opening ticket | Code, from the book and the numbers | Approve / Reject / Amend. Narrator may only rephrase or pick a listed strike |
| Risk limits | Enforce always | Tune via Settings |
| Privilege level | Applies rules | Change only via protected Settings flow |
| Ad-hoc Mobile IB trades | Detected on next sync | Expected in reserved slots; Athena reconciles |

Default privilege: **`all_orders`** — every open, roll, and close needs Approve (bot and/or ephemeral web link).

You can later set `opens_only`, `auto_within_risk`, or `paper_only` in Settings. Raising privilege is itself a protected action (see [04-settings-and-risk.md](04-settings-and-risk.md)).

## Success metrics

**Operational**

- IB disconnect during Mobile slots recovers automatically when Athena’s slot returns
- Main loop completes health → refresh → decide → (await approve) in under 60s of compute (wait for human is async)
- Zero unapproved live orders at default privilege
- Daily local backup success; offsite sync lag within configured SLA

**Trading process (not “guarantee PnL”)**

- Every live action has: signal reason, tags/phase, risk check result, approver identity, timestamp
- Impulse path reduced: no new live order without Athena draft + approve (except Mobile slot ad-hoc, which sync reconciles)
- Sleeve metrics tracked: premium collected, win rate by tactic, max drawdown, days in each NFR phase

**Strategy refinement**

- Settings + rule versions are commentable; you can improvise parameters without rewriting core code for every tweak
- Paper path proves the cycle before live capital

## Relationship to current workflow

Today: WorkBuddy reviews → you order in IB → you update fanit-cli.

Athena target: Athena drafts from news + tags + positions → you Approve on phone → Athena executes in IB slot → Athena ledger updates → optional later push to fanit.

## Defaults (changeable in review)

| Item | Default |
|------|---------|
| System of record | Athena engine DB |
| fanit | Remains manual ledger until sync adapter (**sync timing: later**) |
| IB accounts | Whatever you configure in env/Settings; multi-account schema ready |
| Execution | `ib_insync` + orchestrator |
| Store | Postgres/TimescaleDB locally |
| Backup offsite | MinIO (S3 API) locally; remote endpoint via same env later |
| Mode path | `paper` → `approve-gated-live` on LIVE account → higher privilege only if you opt in |

## Risks (product)

- Single IB API session contention with Mobile App — mitigated by sync slots and reconnect policy
- News false positives driving bad strangles — mitigated by the wheel, realized-vol tags, and Approve
- Over-automation temptation — privilege defaults stay strict; Settings changes audited
