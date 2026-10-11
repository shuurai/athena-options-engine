# 步 2 — 安全不变量与硬门

**主计划**：[00-plan-order.md](00-plan-order.md) ｜ **状态**：**已落地**（2026-10-11）。D5 允许 short put 与 covered call 并存，`strict_conflict` 可收回旧的一刀切。D6 拒绝并写明缺多少股。D12 (a)，`margin_rate` 默认 20%。
**依赖**：步 1 ｜ **阻塞**：步 6（第一次真正放开合法开仓）

---

## 1. 目标

把三条本来"靠提示词祈祷"的规则变成**代码铁律**，且都必须发生在 LLM 之后、`drafts` 落库之前：

| 不变量 | 一句话 |
| --- | --- |
| **I1 持股覆盖** | 一张 CC 的合约数不得超出手上股票能覆盖的合约数（`contracts ≤ floor(shares/100)` 减去已占用） |
| **I2 批量风险** | 一张 ticket 的所有腿**合并**后仍须满足现金/市值/保证金/下单数上限，而不只是每一腿各自满足 |
| **I3 状态感知冲突** | 只禁止真正非法的组合（裸卖 call、双空 call、双空 put、strangle 叠仓）；**允许**轮动常态（short put ＋ 持股 ＋ short call） |

> **I2 的作用域声明（务必与步 8 对齐）**：I2 只覆盖**一张 ticket 内部**。同一 cycle 内**跨 ticket** 的合并检查是 **I4**，定义在 [08-portfolio-diagnostics.md](08-portfolio-diagnostics.md) §3.1。原因：`stage_draft` 求值时读 `account_marks` 的**最新快照**，而它在同轮内不随 drafts 变化（`stage_draft` 不落 `orders`），因此同轮先后 stage 的 N 张 ticket 各自看到**同一个未扣减的 `cash`** —— 两张各自合规、合并透支的情况 I2 拦不住。本步只做 I1/I2/I3，I4 留待步 8。

---

## 2. 现状取证

### 2.1 `OPEN_CC` 完全没有持股前置校验（最高危）

链路逐点核对：

| 位置 | 现有检查 | 是否检查持股 |
| --- | --- | --- |
| `agents._drafts` | `if tactic == "BUY" and (not flags.long or flags.intent != "accumulate")` | **否** —— 只对 `BUY` 校验 flags，`OPEN_CC` 无任何前置 |
| `agents._propose_one`（OPEN_CC 分支） | 只要求 Call agent 不 quiet、有 expiry/strike/limit | **否** |
| `drafts.stage_draft` | `_tactic_on`（开关）、`_has_short`（冲突空头） | **否** —— `_has_short` 查的是"有没有空头"，不是"有没有多头" |
| `drafts._queue_order`（OPEN_CC 分支） | 只校验 expiry/strike/limit 齐全 | **否**，随后直接 `_insert_order(action="SELL", right="C")` |

⇒ 唯一防线是 Call 系统提示词里那句 "Propose only when shares are already held"，即**靠模型自觉**。模型一旦误判，系统会**落库并提交一张裸卖 call**。

### 2.2 合约数不受持股约束

`_propose_one` 中 CC 的 `contracts` 来自 `open_quantity(...)`，而 `open_quantity` 的依据是 `risk.max_position_per_symbol` 与价格 —— **与 `shares` 无关**。

实测账面（`simulated = false`）：

```
symbol | sec_type | quantity | avg_price
-------+----------+----------+----------
MRVL   | STK      |        1 | 275.960003
QCOM   | STK      |        1 | 176.450003
T      | STK      |        1 |  22.465403
```

Call agent 看到的正是 `Shares held: 1`，可读成"有持仓"；而 1 股对应可覆盖合约数为 **0**。若提案，`contracts` 会是 1 —— **1 张合约 = 100 股，超出持仓 100 倍**。

### 2.3 `_has_short` 一刀切，禁掉了轮动

`stage_draft` 中：

```
opening = tactic in {"OPEN_STRANGLE","OPEN_CSP","OPEN_CC","BUY"}
if opening and _has_short(conn, account_id, symbol): blocked = "conflicting short on this symbol"
```

`_has_short` 为真当且仅当：存在 `status='submitted'` 的 `SELL` 单，**或**最新 `OPT` 持仓 `quantity < 0`。

推论：
- 一张 CSP 成交（short put）后，同标的的 `OPEN_CC` → **被拒**。
- 一张 CC 挂出后，同标的的 `OPEN_CSP` → **被拒**。
- strangle 与任何其他腿同标的 → 一律被拒。

而"short put ＋ 持股 ＋ short call"正是轮动的**常态状态**。评审 §3.3 已定性：该规则与"单个或多个"目标直接冲突。

### 2.4 批量风险不存在

`_drafts` 对 Consolidator 返回的 `drafts` 数组**逐个**调用 `_propose_one`，每腿各自走一遍 `block_reason`（现金储备、`max_symbol_pct_nav`、`max_short_opt_margin_pct` 均为**单腿**口径）。两条腿各自合规、合起来透支现金储备的情况**无人拦**。

---

## 3. 变更逻辑 spec

### 3.1 I1 — 持股覆盖硬门

**位置**：`drafts.stage_draft` 的前置校验区（`opening` 判定之后、`block_reason` 之前）。
**为什么放这里**：`_propose_one`（LLM 路径）、`phases.evaluate_symbol`、`manage_book` 全部经此一处，是唯一收口点。

**计算规则**：

```
coverable_shares   := 同账户 + 同标的 + sec_type='STK' + 同 simulated 命名空间的最新持仓 quantity（>0 才计）
coverable_contracts:= floor(coverable_shares / 100)
held_calls         := |同标的 OPT 持仓中 contract_right='C' 且 quantity<0 的合约数之和|
                    + |status='submitted' 且 action='SELL' 且 contract_right='C' 的挂单合约数|
remaining          := coverable_contracts − held_calls
若 tactic == 'OPEN_CC' 且 contracts > remaining  →  blocked
```

**必须同时成立的两个细节**：
1. **命名空间一致**：`shares` 必须取自与本次 draft 相同的 `simulated` 值。当前 `_shares` 已按 `simulated` 过滤，但 CC 分支未校验，需显式对齐，避免回放持仓与实盘持仓串味。
2. **已占用要扣减**：不能只看"有没有 100 股"，否则同一批股票可以被两轮 CC 重复使用。

**边界用例表**：

| shares | contracts | 已挂/已空 call | 结果 | 原因文本（建议） |
| --- | --- | --- | --- | --- |
| 0 | 1 | 0 | blocked | `cc needs 100 covered shares, held 0` |
| 50 | 1 | 0 | blocked | `cc needs 100 covered shares, held 50` |
| 100 | 1 | 0 | pending | — |
| 100 | 2 | 0 | blocked | `cc needs 200 covered shares, held 100` |
| 200 | 1 | 1 | pending | 剩余 1 张可覆盖 |
| 200 | 2 | 1 | blocked | `cc needs 300 covered shares, held 200` |
| 100 | 1 | 1 | blocked | `cc needs 200 covered shares, held 100` |

### 3.2 I2 — ticket 级批量风险

**新增函数**：`packages/risk/src/athena_risk/__init__.py` → `block_ticket_reason(legs, limits, marks, existing)`。
**调用时机**：`_drafts` 内，逐腿 `_propose_one` **之后**再跑一次汇总复核；aggregate 不过 → 整张 ticket 的每一腿都改判 `blocked`，detail 指向 ticket 级原因（例：`ticket breaches cash_reserve_pct`）。

**汇总口径**（对 ticket 内所有腿求和，再叠加已有敞口）：

| 维度 | 汇总算式 | 对应上限 |
| --- | --- | --- |
| 现金需求 | `Σ(BUY: limit × qty)` ＋ `Σ(CSP: strike × multiplier × contracts)` | `cash_reserve_pct`（用 `account_marks.cash` 与 `nav_marks.last_nav`） |
| 标的值 | `existing_symbol_value` ＋ `Σ(腿名义价值)` | `max_symbol_pct_nav` |
| 空头保证金 | `existing_margin` ＋ `Σ(空头腿保证金估计)` | `max_short_opt_margin_pct` |
| 当日下单数 | `orders_today` ＋ `len(ticket)` | `max_orders_per_day` |

**关键设计选择**：现有 `account_marks.margin` 是**已发生**的保证金，而 ticket 里的是**拟开**的，因此需要一个保证金估计式。见决策 **D12**。

### 3.3 I3 — 状态感知冲突规则

**替换** `_has_short` 的二元判断，改为 `conflict_reason(positions, working_orders, tactic)`，返回 `None` 或原因文本。

**先定义"当前状态"**（同标的、同 `simulated`）：

```
shares      := STK 持仓 quantity > 0
short_put   := 存在 OPT 持仓 right='P' 且 quantity<0，或挂单 SELL put
short_call  := 存在 OPT 持仓 right='C' 且 quantity<0，或挂单 SELL call
```

**冲突矩阵**（行 = 当前状态，列 = 拟开腿；✅ 允许 ／ ⛔ 拒绝）：

| 当前状态 | `BUY` | `OPEN_CSP` | `OPEN_CC` | `OPEN_STRANGLE` |
| --- | --- | --- | --- | --- |
| FLAT（无股无期权） | ✅ | ✅ | ⛔ I1 | ✅ |
| ODD_LOT（0<shares<100） | ✅ | ✅ | ⛔ I1 | ✅ |
| SHARES（≥100） | ✅ | ✅ | ✅（受 I1） | ✅ |
| SHORT_PUT | ✅ | ⛔ 重复空 put | ✅（受 I1，需有股） | ⛔ 叠仓 |
| SHORT_PUT ＋ SHARES | ✅ | ⛔ 重复空 put | ✅（受 I1） | ⛔ 叠仓 |
| SHORT_CALL（有覆盖） | ✅ | ✅ | ✅（受 I1） | ⛔ 叠仓 |
| SHORT_CALL ＋ SHORT_PUT ＋ SHARES | ✅ | ⛔ 重复空 put | ✅（受 I1） | ⛔ 叠仓 |

⛔ 的判别理由统一为四类：`naked call（I1 未过）`、`duplicate short put`、`duplicate short call（超出覆盖）`、`option leg already open on this symbol`。

> **刻意保留的限制**：strangle 必须"独占"同标的的期权腿。理由是它本身已含两条腿，与之叠加会超出当前的风险口径；若将来要放开，需先扩展 I2 的 margin 模型。

### 3.4 对既有"已验收"记录的影响（必须显式处理）

tracker `P4-18` 的验收文本是："Fixture `ZZCF` already had a short option. A new CSP was stored as blocked." —— 在新规则下，这句话**只在既有空头是 short put 时成立**；若既有空头是 short call（例如一张 CC），新的 CSP 将**变为允许**（这正是轮动）。

⇒ 本步会**修订 P4-18 的验收定义**：拆成两条 fixture（"已有 short put → 新 CSP 拒绝"、"已有 short call → 新 CSP 允许"），并在 `10-implementation-tracker.md` 里记一条勘误。这属"放宽门禁"，必须配套负面用例（3.5）。

### 3.5 落地位置总览

```
_drafts (agents.py)
  ├─ 逐腿 _propose_one → stage_draft(I1, I3 前置) → block_reason(单腿)
  └─ 汇总 block_ticket_reason(I2) → 覆写整张 ticket 的结局
```

三层顺序不可颠倒：**先单腿合法（I1/I3）→ 再单腿风险 → 最后批量风险**。理由：批量检查需要一份"已经合法"的腿列表，否则原因文本会互相污染。

---

## 4. 决策点

| # | 待决 | 选项 | 推荐 | 落地 |
| --- | --- | --- | --- | --- |
| **D5** | 是否允许同标的同时存在 short put 与 short call | 允许 / 禁止 | **允许** | **已落地：允许。** `strict_conflict` 打开时恢复「有短仓就全拒」 |
| **D6** | 持股不满 100 股时的 CC | 拒绝并给原因 / 静默跳过 / 允许 odd-lot | **拒绝并给明确原因** | **已落地。** 原因形如 `I1: cc needs N covered shares, held M` |
| **D12** | ticket 内空头腿的保证金估计式 | (a) `strike × multiplier × contracts × margin_rate`，`margin_rate` 进 Settings（默认 20%）(b) 近似为 `symbol_pct_nav` 上限的一部分 (c) 暂不估，留空跳过 | **(a)** | **已落地 (a)。** `RiskLimits.margin_rate` 默认 20，审计带估计值与上限 |

---

## 5. 验收标准

### 5.1 负面用例（必须全部 blocked）

| # | 构造 | 期望 | 落地 |
| --- | --- | --- | --- |
| N1 | shares=0，`OPEN_CC` 1 张 | `blocked`，原因含 `covered shares, held 0` | **已落地。** `test_covered_call_needs_free_shares` |
| N2 | shares=50，`OPEN_CC` 1 张 | `blocked` | **已落地。** 同上 |
| N3 | shares=100，`OPEN_CC` 2 张 | `blocked` | **已落地。** 同上 |
| N4 | shares=100 ＋ 已挂 SELL call 1 张，再 `OPEN_CC` 1 张 | `blocked` | **已落地。** 同上 |
| N5 | 已有 short put，再 `OPEN_CSP` | `blocked`，`duplicate short put` | **已落地。** `test_wheel_allows_a_covered_call_beside_a_short_put` |
| N6 | 已有 short put，再 `OPEN_STRANGLE` | `blocked`，`option leg already open` | **已落地。** 同上 |
| N7 | ticket = [BUY, CSP]，两腿各自在现金储备内，**合并后**击穿 `cash_reserve_pct` | **两腿都** `blocked`，原因指向 ticket | **已落地。** `test_ticket_budget_blocks_both_legs` |
| N8 | ticket = [BUY, CSP]，合并后击穿 `max_symbol_pct_nav` | 同上 | **已落地。** 同上 |

### 5.2 正面用例（必须 pending，证明没有过度收紧）

| # | 构造 | 期望 | 落地 |
| --- | --- | --- | --- |
| P1 | shares=100，`OPEN_CC` 1 张，价格在区间上沿 | `pending` | **已落地。** `test_covered_call_needs_free_shares`（持股覆盖，不另验区间上沿） |
| P2 | 已有 short put ＋ shares=100，`OPEN_CC` 1 张 | `pending`（轮动成立 —— 这是被修复的核心缺陷） | **已落地。** `test_wheel_allows_a_covered_call_beside_a_short_put` |
| P3 | FLAT，`OPEN_CSP` 1 张 | `pending` | **已落地。** 同上 |
| P4 | FLAT，ticket = [BUY, CSP]，合并后未击穿任何上限 | 两腿均 `pending` | **已落地。** `test_ticket_budget_blocks_both_legs` |

### 5.3 不变量断言

| # | 断言 | 落地 |
| --- | --- | --- |
| A1 | 对任意输入，**不存在** `tactic='OPEN_CC'` 且 `status IN ('pending','approved')` 且 `contracts > floor(持股可覆盖数/100)` 的行 | **已落地。** `_cover_reason` 在 `stage_draft` 之前拦截 |
| A2 | 同一标的、同一 `simulated` 空间内，不存在两张独立的 `OPEN_STRANGLE` 同时 active | **已落地。** 已有任意期权腿时 `OPEN_STRANGLE` 返回 `I3: option leg already open` |
| A3 | `decision_trails.steps` 中每条 `blocked` 的 `detail` 必须能唯一映射到 I1/I2/I3 之一（原因文本受枚举约束） | **已落地（本步三门）。** 原因前缀为 `I1:` / `I2:` / `I3:`。单腿的现金、仓位等旧原因仍用原来的句子，不带这三前缀 |

---

## 6. 明确不做

- 不改 `risk` 的**数值**（profile `test`、`cash_reserve_pct=5`、`max_contracts_per_symbol=12` 等一律不动；那是步 7 的配置议题）。
- 不实现真实保证金引擎；D12 选 (a) 即为"估计式"，并明确标注为估计。
- 不做仓位规模优化（`contracts` 的**最优**取值属步 5/6）。
- 不改 `manage_book` 的 roll/close 逻辑（`CLOSE`/`ROLL` 不走 `opening` 分支，本步不影响它们）。

---

## 7. 风险与回滚

| 风险 | 说明 | 对策 |
| --- | --- | --- |
| I3 是"放松门禁" | 从"有短仓就全拒"变成"按矩阵逐项判"，理论上放宽了限制 | 必须由 5.1 的 N5/N6 两个负面用例守住；放宽的只有轮动组合 |
| I2 依赖保证金估计 | 估计式偏小会低估风险 | D12 的 `margin_rate` 设为可调 Settings 项，并在审计里同时记录估计值与上限，便于事后校准 |
| 原因文本膨胀 | 三层检查会产生多种 block 原因，审计难以聚合 | 原因文本受枚举约束（A3），前缀固定为 `I1:`/`I2:`/`I3:`，便于 SQL 分组统计 |
| 与 `_has_short` 的既有调用者冲突 | `phases._has_short` 是另一份实现，用于 CLOSE 判定 | 本步只替换 `drafts._has_short`；`phases` 那一份保留到步 6 一并统一（届时 D10 会删除该分支） |
| 回滚 | 若放开轮动后出现意外组合 | 逻辑回滚：恢复 `_has_short` 一刀切（保留为开关 `ATHENA_STRICT_CONFLICT=1`）。**无 schema 变更**，回滚成本极低 |
