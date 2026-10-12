# 09 — Roadmap

## Principle

Docs → skeleton → paper HITL → one-account live (approve-all) → deepen NFR + news → multi-account / higher privilege only if earned.

No live money until Settings + Approve path + IB slots are proven in paper.

## Phase 0 — Proposal (this deliverable)

- [x] Locked decisions and proposal set under `docs/athena/`  
- [x] Configurable IB account, fanit sync later, MinIO/S3 env, Telegram connect guide  
- [ ] Create Telegram bot + allowlist when leaving stub  
- [ ] Slot calendar + Tailscale on Windows box 

## Phase 1 — Monorepo skeleton (1–2 weeks)

- Init repo tooling (Python uv/poetry workspace + optional pnpm for control-web)  
- `packages/settings` + `packages/store` schema/migrations  
- `apps/orchestrator` dry loop (health → sleep) with Settings load  
- `deploy/windows` service sketches  
- `.env.example`  

**Exit:** Orchestrator runs on Windows against empty DB; Settings version 1 active.

## Phase 2 — Data + paper HITL (2–4 weeks)

- iTick → store bars for small watchlist  
- IB slot manager (connect/disconnect schedule) + read-only position sync  
- NFR phase/tag stubs + draft generator (no live orders)  
- Risk Guard on drafts  
- Feishu + Telegram notify + Approve/Reject  
- control-web one-time Approve page + signed webhook  
- Local backup job  

**Exit:** End-to-end paper: draft → Approve → a real paper-account order → audit row.

## Phase 3 — Live approve-gated (2–3 weeks)

- `ib_insync` exec path for approved limits  
- Fill callbacks → positions  
- `/halt`, reject-streak halt, daily loss halt  
- S3-compatible backup sync  
- Ops agent: gateway/athena restart  

**Exit:** One small live trade on MVP account with Approve-all; Mobile slots do not corrupt state.

## Phase 4 — NFR depth + news Tier A (3–5 weeks)

- Full phase machine + strangle/CSP/CC tactic rules  
- Yahoo/Finviz/Fed/earnings intake + NEWS_WASH density  
- DSH optional enrichment  
- Position management drafts (roll / profit-take)  
- Grafana or simple status dashboard (optional)  

**Exit:** One full event cycle observed (paper or tiny live) with settings tweaks audited.

## Phase 5 — Hardening + multi-account (ongoing)

- Second/third IB account activation  
- Optional fanit fill export  
- Privilege experiments (`opens_only`) only after stable metrics  
- Tier B/C news with compliance review  
- Revisit Nautilus only if execution complexity demands it  

## Explicit deferrals

| Item | Until |
|------|--------|
| Nautilus Trader | Post stable HITL + slots |
| Unattended `auto_within_risk` | Proven track record + dual confirm |
| Social scrapers | Compliance review |
| Cloud trading host | Not planned as primary |
| Extra IB market data subs | Sleeve pays for them |

## Near-term decisions checklist

1. [x] IB account = any configured `ATHENA_IB_ACCOUNT_ID` (virtual/config entry)  
2. [x] fanit sync = later  
3. [x] Backup = MinIO local via S3 env vars; remote endpoint later  
4. [x] Bots stub OK; Telegram connect guide on control-web  
5. [x] Slot calendar = HK cash, US regular session in HKT, Mobile 19:00–21:30. Overnight US tail included
6. [x] Telegram bot and allowlist
7. Tailscale (or equivalent) on Windows box
8. Feishu app (optional after Telegram)
9. [x] Wheel, strike menu, and cycle budget. Implied-vol consumption stays unbuilt  

## Success of the roadmap

Not “many strategies shipped.” Success is: **one strategy**, tight Settings, reliable remote Approve, boring automation of sync/ops, and a sleeve you can trust enough to keep refining for years.
