# 13 — Portfolio advisor

**主计划**：[00-plan-order.md](00-plan-order.md)
**状态**：**Implemented**（2026-10-16），**D51–D57 已于 2026-11-05 追认**（5 条按现行，**2 条附修正**：D52 换篮子、D54 删假旋钮）。Side track. Not on the signal critical path.
**依赖**：held positions; news calendar marks; human Approve

> **性质**：提案与验收口径。顾问只写信号和 pending 草案，不改 tactic 以外的下单权。Approve 仍是人。

---

## 0. Decisions (locked)

| ID | Decision | Ruling |
| --- | --- | --- |
| **D51** | Scope | Both long stock and short options. Drafts stay pending until Approve. |
| **D52** | Guidance | Fixed basket: QQQ, NVDA, SOXX, AVGO, MRVL, MU, TSM. Editable in Settings. |
| **D53** | Macro feed | Price from the universe IB print. Yahoo supplies headlines only. Fed and earnings dates come from `calendar_marks`. No separate CPI feed. |
| **D54** | Option draft | An acting signal stages CLOSE (`take_profit` or `close`) or TRIM (`trim`) even when captured premium is below the profit-take gate. |
| **D55** | Stock size | `take_profit` and `close` sell the full share quantity. `trim` sells half, at least one share. The option contract cap does not apply. |
| **D56** | Weight | Confidence thresholds are the only weight. No extra risk-weight knob. |
| **D57** | Prompts | `AdvisorExplainer`, `Narrator`, and `ManageExplainer` are editable on Orchestration. |

**裁定状态（2026-11-05，追认 7 条）**：

| 决策 | 裁定 | 说明 |
| --- | --- | --- |
| D51 | 追认 | 股票与空头期权都管：持仓可被建议 CLOSE / TRIM，也可被提议 BUY 一个尚未持有的标的，一律停在你 Approve |
| **D52** | **改** | 篮子由 `QQQ, NVDA, SOXX, AVGO, MRVL, MU, TSM` 换成 **`QQQ, SPY, IWM, TLT, GLD, SOXX, NVDA`** —— 宽基三、利率一、商品一、芯片二。原篮子名为"市场指引"、实为**芯片周期指引**（7 个里 6 个半导体） |
| D53 | 追认 | 价格只认 universe 的 IB 报价；Yahoo 只给标题；Fed / 财报日取 `calendar_marks`；不单开 CPI 源 |
| **D54** | **追认 ＋ 删旋钮** | 语义不变：顾问发信号即下 CLOSE / TRIM 草案，不等普通止盈门槛。但 `profit_take_relief_pct`（默认 15）在策略层**无任何读取**，UI 却称"从止盈阈值扣除、不低于 20" —— **假旋钮**，与 D34 已推翻的同类问题相同 |
| D55 | 追认 | `take_profit` / `close` 全量卖；`trim` = `max(1, held // 2)`。**边界已明确**：持 1 股时 trim 算出 1 股，实际是清仓但标 `TRIM` —— 这是"减到不剩"的语义，不是缺陷 |
| D56 | 追认 | 只有置信度阈值（`take_profit_conf` / `trim_conf`），不另乘风险权重 |
| D57 | 追认 | 三个提示词可在 Orchestration 编辑；**改错没有回滚** |

**D52 的附带收益**：`SPY` 进篮子后开始拉 SPY 日线，顺带解掉 D18 的「`market_bars` 里 SPY 为 0 行 ⇒ `REGIME_RISK_OFF` 永不触发」问题 —— 不必再单独做一次补拉。

**D52 / D54 的实施欠账**（`model.py:157` 篮子的 `default_factory`、`model.py:161` 与 `pages.py:2931` 的假旋钮）记在主计划 §6 末。

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

The operator specification is [../athena/13-portfolio-advisor.md](../athena/13-portfolio-advisor.md). This booklet keeps the locked decisions. The specification keeps the inputs, thresholds, draft sizes, pages, and store.

- Cycle order: evaluate watchlist, then `run_advisor` when enabled, then `manage_book`, then signaled entry. Advisor does not call the Consolidator.
- `advisor_signals` stores one row per account, symbol, and day.
- Manage reads that row. Option and stock drafts use phase `ADVISOR`.
- Guidance prices use the same IB print as the universe row. Yahoo adds a headline when no stored Yahoo title exists. Nearby FOMC and earnings dates come from stored calendar marks. A missing print or headline is skipped.
- `profit_take_relief_pct` stays on the Settings card and is not applied. An acting signal stages the draft directly.
