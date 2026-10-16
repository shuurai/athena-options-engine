# 13 — Portfolio advisor

**主计划**：[00-plan-order.md](00-plan-order.md)
**状态**：**Implemented**（2026-10-16）。Side track. Not on the signal critical path.
**依赖**：held positions; news calendar marks; human Approve

> **性质**：提案与验收口径。顾问只写信号和 pending 草案，不改 tactic 以外的下单权。Approve 仍是人。

---

## 0. Decisions (locked)

| ID | Decision | Ruling |
| --- | --- | --- |
| **D51** | Scope | Both long stock and short options. Drafts stay pending until Approve. |
| **D52** | Guidance | Fixed basket: QQQ, NVDA, SOXX, AVGO, MRVL, MU, TSM. Editable in Settings. |
| **D53** | Macro feed | Yahoo last price for the basket, plus Fed and earnings dates already stored in `calendar_marks`. No separate CPI feed. |
| **D54** | Option draft | An acting signal stages CLOSE (`take_profit` or `close`) or TRIM (`trim`) even when captured premium is below the profit-take gate. |
| **D55** | Stock size | `take_profit` and `close` sell the full share quantity. `trim` sells half, at least one share. The option contract cap does not apply. |
| **D56** | Weight | Confidence thresholds are the only weight. No extra risk-weight knob. |
| **D57** | Prompts | `AdvisorExplainer`, `Narrator`, and `ManageExplainer` are editable on Orchestration. |

---

## 1. Goals

| # | Goal | Pass when |
| --- | --- | --- |
| **G1** | Advisor can be turned on | Settings has an Advisor card. Off until enabled. |
| **G2** | Short option gets a draft | A `take_profit` signal stages CLOSE below the normal profit-take gate. |
| **G3** | Stock size matches the book | A 100-share `trim` stages 50 shares. `close` stages 100. |
| **G4** | Page shows drafts | Portfolio Analysis links the draft id and lists a pending BUY that is not already a position. |
| **G5** | Cycle picture | Orchestration diagram shows Guidance → Advisor → Manage joining Approve. |

---

## 2. Design

- Cycle order: evaluate watchlist, then `run_advisor` when enabled, then `manage_book`, then signaled entry. Advisor does not call the Consolidator.
- `advisor_signals` stores one row per account, symbol, and day.
- Manage reads that row. Option drafts use phase `ADVISOR`. Stock drafts use the same phase.
- Guidance notes include the Yahoo quote and nearby FOMC / earnings dates. A Yahoo miss is skipped.
