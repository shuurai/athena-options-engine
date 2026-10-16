# 12 — Germany equity (IFX) — stocks only

**主计划**：[00-plan-order.md](00-plan-order.md)
**状态**：**Implemented**（2026-10-16）。Side track. Not on the original signal critical path.
**依赖**：IB Germany equity market data; Universe Market `de` on the name

> **性质**：提案与验收口径。提案先入 `docs/plan`，再实现。

---

## 0. Decisions (locked)

| ID | Decision | Ruling |
| --- | --- | --- |
| **D46** | Scope | Stocks only (quotes, history, BUY). No EUREX / European options wheel. |
| **D47** | Geography | Germany / Xetra via Universe Market `de` (IBIS, EUR). Acceptance name **IFX**. |
| **D48** | Contract | `Stock(code, "SMART", "EUR", primaryExchange="IBIS")` — same pattern as CA + TSE. |
| **D49** | Cash session | `Europe/Berlin` weekday 09:00–17:30 for `market=de`. |
| **D50** | Options | Option chain / ask / mid for `de` underlyings return empty / skip. |

Ops prerequisite: IB **European (Germany) equity** Level I (or delayed entitlement). Routing alone cannot invent a quote.

---

## 1. Goals

| # | Goal | Pass when |
| --- | --- | --- |
| **G1** | IFX routes to EUR/IBIS | `stock_contract("IFX", "de")` is SMART/EUR/IBIS, not SMART/USD |
| **G2** | Universe can set `de` | TacticFlags + UI accept `de` |
| **G3** | Berlin cash window | `cash_open` for `de` follows Xetra hours |
| **G4** | No EU options | Option path for `de` stays empty |
| **G5** | EUR in book FX | EUR listed in display / global currencies |

---

## 2. Design

- `symbol_market` / `TacticFlags.market`: add `de`
- `stock_venue` / `stock_contract`: SMART + EUR + primary IBIS
- `cash_open`: Berlin zone for `de`
- Option helpers: early return when kind is `de`
- README: Germany equity subscription row; options still unsupported in Europe

---

## 3. Out of scope

- Pan-EU venues (LSE, Euronext, …)
- EUREX options / CSP / CC / CLOSE on EU underlyings
- Hardcoding only IFX (any `market=de` letter ticker uses the same route)
