# Athena Options Engine — Proposal Index

**Status:** Paper and live use the same order path. `paper` trades `ATHENA_IB_PAPER_ACCOUNT_ID`. `live` trades each id in `ATHENA_IB_ACCOUNT_IDS`. Each account keeps its own book.  
**Last updated:** 2026-10-09  
**Supersedes for implementation intent:** [../athena_options_engine_prd.md](../athena_options_engine_prd.md) (historical input)  
**Next:** One small approved live order once Gateway and `ATHENA_IB_ACCOUNT_ID` are set. Task list: [10-implementation-tracker.md](10-implementation-tracker.md). Roadmap: [09-roadmap.md](09-roadmap.md).

Athena is a **Windows-first**, human-in-the-loop options engine that refines **one** strategy for the next 5–10 years: the **News Front-Running Cycle**. CSP / CC / Strangle / Hold / Roll / Close are tactics inside that strategy, not separate products.

Develop on Mac. Run on a dedicated Windows trading box. Not cloud-primary. Local backup first; S3-compatible blob sync as secondary.

---

## Locked decisions

| Topic | Decision |
|-------|----------|
| Strategy | News Front-Running Cycle only |
| Tactics | CSP, CC, Strangle, Hold, Roll, Close |
| Default human gate | Every open / roll / close needs Approve |
| Privilege | Configurable in Settings (`all_orders` default → can raise/lower) |
| Settings | First-class versioned store (risk, prefs, strategy params, IB slots, notify) |
| IB account | **Any account you configure** (`ATHENA_IB_ACCOUNT_ID` / Settings) — no hard-coded IBKR id |
| fanit sync | Later (after stable live fills) |
| Codebase | Greenfield monorepo in this repo; reuse fanit **concepts**, not as live SoR |
| Realtime data | iTick |
| IB role | Historical pulls, trade execution, scheduled sync slots (Mobile IB App windows) |
| Control UX | Feishu bot + Telegram bot + ephemeral one-time web control pages |
| Backup | Local first → AWS-compatible S3 API |
| Execution MVP | Python orchestrator + `ib_insync` (Nautilus deferred) |
| SoR | Athena owns engine state; fanit remains human portfolio ledger until optional sync |

---

## Document index

| Doc | Purpose |
|-----|---------|
| [01-proposal.md](01-proposal.md) | Goals, non-goals, HITL philosophy, success metrics |
| [02-architecture.md](02-architecture.md) | Monorepo layout, Windows process map, data/control planes |
| [03-strategy-news-frontrun.md](03-strategy-news-frontrun.md) | Phase machine, tactic mapping, watchlist rules |
| [04-settings-and-risk.md](04-settings-and-risk.md) | Settings surface, privilege levels, risk params |
| [05-human-control.md](05-human-control.md) | Feishu/Telegram, ephemeral web controls, audit |
| [06-data-and-ib.md](06-data-and-ib.md) | iTick vs IB, reconnect, sync slots, multi-account |
| [07-news-intake.md](07-news-intake.md) | News sources, legit vs adversarial signals, phased intake |
| [08-ops-backup-remote.md](08-ops-backup-remote.md) | Local/S3 backup, remote ops (restart Gateway/services) |
| [09-roadmap.md](09-roadmap.md) | Phased build plan |
| [10-implementation-tracker.md](10-implementation-tracker.md) | Task checklist with status and exit proof |
| [11-test-tracking.md](11-test-tracking.md) | What was proven, and which checks are still pending |

---

## Decided config

| Item | Decision |
|------|----------|
| **IB account** | **Configurable.** Athena uses whatever `ATHENA_IB_ACCOUNT_ID` (and optional multi-account list) you set. Treat IB accounts as virtual/config entries — not tied to a single hard-coded fanit id. Example seed from fanit: Relaxed `U3726383-BOT` — change freely. |
| **fanit sync** | **Later** — after stable live fills; Athena is SoR until then. |
| **Bots** | **Stub OK** for now. Telegram: connection page must include a simple connect guide (see [05-human-control.md](05-human-control.md)). Feishu stub until app created. |
| **Backup offsite** | **MinIO locally** first (S3 API). Real remote S3 later via same env vars ([08](08-ops-backup-remote.md), [`.env.example`](../../.env.example)). |

**Still needed when wiring:** Telegram bot token + allowlist user id; Feishu app credentials; optional remote S3 endpoint.

## Project progress

| Phase | Status |
|-------|--------|
| 0 Proposal docs (`docs/athena/*`) | **Done** |
| 1 Monorepo skeleton (orchestrator, settings, store) | **Done** — see [10-implementation-tracker.md](10-implementation-tracker.md) |
| 2 Paper HITL + iTick seed bars + slot clock + one-time Approve | **Done** — see [10-implementation-tracker.md](10-implementation-tracker.md) |
| 3 Live approve-gated exec | **Gate done.** No live order until Gateway and the account id are set. |
| 4 NFR + news depth | **In progress** — see [10-implementation-tracker.md](10-implementation-tracker.md) |
| Console web | **Local pages are up** on port 8788. Vercel waits on Tailscale. See [10-implementation-tracker.md](10-implementation-tracker.md). |

---

## Relationship to other tools

- **fanit-cli / income-tracker:** Portfolio ledger + Guru/DSH patterns. Athena does not replace it on day one. Reuse: option lifecycle concepts, approve tiers, iTick client ideas, DSH headless worker pattern.
- **WorkBuddy automations** (daily market, MPF): Stay as research overlays; Athena consumes structured signals/news, not those runbooks as-is.
- **Original PRD:** Useful for tag/rule/5-minute loop detail; discard cloud-unattended and “multi-strategy product” framing.