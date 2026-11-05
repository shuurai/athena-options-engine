# 12 — Germany equity (IFX) — stocks only

**主计划**：[00-plan-order.md](00-plan-order.md)
**状态**：**Implemented**（2026-10-16），**D46–D50 已于 2026-11-05 追认**（五项全部按现行，无附动作）。Side track. Not on the original signal critical path.
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

**核定作业（2026-11-05，追认时的代码核对）**：`broker.py:1050` 确为 `Stock(code, "SMART", "EUR", primaryExchange="IBIS")` —— 不写 `primaryExchange` 时，SMART 在 EUR 名下可能解到其他欧洲交易所并返回别的价，所以这个 primary 不是装饰。`slots.py:35-40` 对 `de` 取 `Europe/Berlin` 工作日 09:00–17:30，**DST 由时区自动偏移**。边界已明确：Xetra 17:30–17:35 的**收盘集合竞价不在可交易窗口内**。`market=de` 对任意 ticker 生效，不止 IFX。

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
