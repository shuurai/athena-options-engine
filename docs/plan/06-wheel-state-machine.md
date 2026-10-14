# 步 6 — 轮动状态机 + ticket

**主计划**：[00-plan-order.md](00-plan-order.md) ｜ **状态**：已落地（2026-10-11）
**依赖**：步 2（不变量）、步 4（指标）、步 5（候选） ｜ **阻塞**：无。证明在 `ZZTEST`。活账户 `PAPER1` 仍是 `test` 档

## 0. 落地结论

内核已在 `wheel.py`。默认 `ATHENA_STATE_MACHINE=on`。`off` 回到旧的七个 agent。验收在 `apps/orchestrator/tests/test_wheel.py`，账本只用 `ZZTEST` / `ZZT`，不是纸面账户 `PAPER1`。

**已落地**

- 状态由持仓和已提交的卖单现算。`symbol_phases.tactic` 恒为 `HOLD`。`state` 与 `leg_counts` 只作快照。
- 一张开仓 ticket 带有序腿。同一标的同一天至多一张开仓 ticket。
- 模型只剩 Location、News、Narrator。Narrator 只能解释，或在已列出的行权价里换一条。表外行权价丢掉该腿，审计记 `narrator_overruled`。
- Engine A 不再写 `OPEN_CSP` / `OPEN_CC` / `OPEN_STRANGLE`。`phases.py` 里这三个名字为零。
- 管理按标的合成一张 ticket，至多两腿，原因 `manage_roll` 或 `manage_close`。数值门槛仍是 `roll_dte` 与 `profit_take_pct`。
- G1、G2、裸 call 静默、`[BUY, OPEN_CC]` 整张挡住、现金储备击穿、一轮三次模型调用，都有 ZZTEST 夹具。

**裁定后与原稿不同、且已按裁定落地**

| 裁定 | 代码 |
| --- | --- |
| 没有持续的 `CALLED_AWAY`。零股加一张裸 call 是 `NAKED_CALL` | `(0, 0, 1)` 为 `NAKED_CALL`。恰好 `(0, 1 put, 1 call)` 仍是 `STRANGLE` |
| 1–99 股、无空头，按持股对待，但 covered call 仍拒绝 | 别名 `ODD_LOT`。`OPEN_CC` 原因 `cc_needs_shares` |
| 1–99 股加一张空 put | 别名 `ODD_PUT`。可买股，不可再开 put，covered call 仍要满 100 股 |
| 收入型标的在回调上可以单独开一张现金担保 put | 原因 `dip_csp`。不要 `RV_HIGH`，也不买股 |
| `RV_HIGH` 只在“允许买股，并且旁边再挂一张 put”时需要 | 原因 `dip_long`，腿是 `[BUY, OPEN_CSP]` |
| 奇数股已经到了卖 call 的位置时，草案改为买满 100 股的缺口 | 原因 `cover_gap`。股数是 `100 − 持股`。不附带 `OPEN_CC`。草案保持 pending，等 Approve。期权张数上限不套在这张股票单上 |
| 已有 strangle 信号、标的允许 strangle、且持股至少 100 | 原因 `signaled_strangle`。一张票两腿，先 `OPEN_CSP` 后 `OPEN_CC`。用现成的 `strangle_put` 与 `strangle_call` 名单。不新开选股器。不足 100 股，或任一侧没有名单，不开这张票 |
| `NAKED_CALL` 与 `MULTI_CONFLICT` 不自动平仓 | 本轮不开仓，审计记 `dropped`。平仓仍只来自到期或止盈门槛 |
| 回调买股的股数不改公式 | 仍是 `open_quantity`。仓位上限打开时股数跟着公式走，测过大于 1。上限关掉时仍是 1。只有 `cover_gap` 按缺口定量 |

**仍按原稿留着、但不是未做的代码**

- 当前触发能走到的五格各有一张草案：`dip_long`、`dip_csp`、`high_cc`、`cover_gap`、`signaled_strangle`。迁移表里其余合法格只断言原因码，没有各跑一张草案。
- 不写 tactic `OPEN_STRANGLE`。有持股的双卖是上面那张两腿票，call 腿走覆盖检查。
- 原稿迁移号 `031` / `032` 没有使用，留作历史。实际是 `038_tickets.sql` 与 `039_phase_state.sql`，打包清单里已有，另有 `040`。

---

## 1. 目标

把"标的"从**四个互不相干的模型调用**改造成**一个状态对象**，把"单个或多个"从"模型恰好同时说了两句话"改成**一张有顺序、有批量风险的 ticket**。

| # | 目标 | 一句话判据 |
| --- | --- | --- |
| **W1** | 状态唯一 | 任意 (account, symbol, simulated) 在任意时刻只有一个可复算的状态；它由持仓与在途订单**推导**得出，不由任何 agent 声明 |
| **W2** | 迁移合法 | 每个状态下的合法动作由一张**显式迁移表**决定；表外的动作一律不产生草案，并给出可枚举的原因码 |
| **W3** | 排列是一等公民 | 一次"多个"＝一条 `tickets` 记录 ＋ N 条有序腿；批量风险按 ticket 计（步 2 的 I2 以此为键） |
| **W4** | 模型只提名 | 模型可以提名腿与理由，**不能**决定合法性、尺寸或顺序；代码可以否决模型，模型不能否决代码 |

---

## 2. 现状取证

### 2.1 四个 agent 之间没有共享状态

`RUN_ORDER` 里 `CSP → Purchase → Strangle → Call` 四个 tactic agent **各自**看 `Tags`、各自答 `quiet`，彼此**看不到对方的答案**（只有 `Call` 能看到 `purchase` 的文本，用于"是否已提出买入"）。而 `news_wash` 的含义是"负面叙事簇" —— 它既不表达"仓位处于轮动哪一步"，也不表达"上一步是否已完成"。

⇒ 用户给的两个用例（`CASH→SHORT_PUT(+BUY)`、`SHARES→SHORT_CALL`）在代码里**没有共同的载体**，所以两者都不触发。

### 2.2 去重粒度是 (symbol, tactic)，不是状态

`has_open_draft(conn, account_id, symbol, tactic)` —— 只回答"这个标的这个 tactic 是否已有草案"。它无法回答"这个标的是否正处在需要先平仓的状态"，也无法回答"BUY 与 CSP 是否属于同一批"。

### 2.3 strangle 已经是两条腿，但没有任何东西把它们绑在一起

`_queue_order` 的 strangle 分支落**两条** `orders`（`queued`），各自独立。`orders` 表里**没有** ticket/leg 分组列。审计里它们是两条互不相识的挂单。

### 2.4 `manage_book` 与开仓腿互不知情

```sql
SELECT DISTINCT ON (symbol, expiry, contract_right, strike) ... FROM positions
```

一个同时持有空 put 与空 call 的标的会产出**两行**，各自独立跑 `management_tactic`，可能在同一轮**同时**对两条腿出 ROLL/CLOSE 草案，**没有任何汇总检查**。而 `management_tactic` 的判据只有 `dte` 与 `captured%`，与"轮动到哪一步"无关。

### 2.5 `symbol_phases.tactic` 只存 HOLD

Engine A 的 `evaluate_symbol` 把 `OPEN_*` 抹成 `HOLD`（且被测试 `test_evaluate_symbol_stores_open_as_hold` 断言）。实测 `symbol_phases` 30 行全为 `HOLD`。⇒ 库里**不存在**任何"检测到却不做"的痕迹，本步必须新建状态列，而不是复用 `tactic`。

### 2.6 可推导状态的数据已经齐了

| 需要 | 来源 | 是否已有 |
| --- | --- | --- |
| 持股 | `positions.sec_type='STK'` 最新快照 `quantity` | ✅ |
| 空 put / 空 call | `positions` 里 `sec_type='OPT'` 且 `quantity<0`，按 `contract_right` 分 | ✅ |
| 在途（未成交）腿 | `orders.status='submitted'` ＋ `action='SELL'` | ✅ |
| 成本价 | `positions.avg_price`（STK） | ✅ |
| 到期 | `positions.expiry` | ✅ |
| 命名空间 | `positions.simulated` / `orders.simulated` / `drafts.simulated` | ✅ |
| 账户熔断 | `athena_exec.halt.active_reason` | ✅ |

⇒ **不需要新数据源**；需要的是把这些字段收敛成**一个可用 SQL 表达的函数**。

---

## 3. 变更逻辑 spec

### 3.1 状态是三元组，不是枚举（W1）

把"状态"定义为一个**可从库复算的三元组**，而不是一个会漏 case 的枚举：

```python
@dataclass(frozen=True)
class WheelState:
    symbol: str
    shares: Decimal                      # STK 最新快照 quantity（可为 0）
    short_puts: int                      # 空 put 的合约数（持仓 + submitted 挂单），≥0
    short_calls: int                     # 空 call 的合约数，≥0
    short_put_strikes: tuple[Decimal, ...]   # 审计用，不参与判定
    short_call_strikes: tuple[Decimal, ...]
    cost_basis: Decimal | None           # STK avg_price
    block: str | None                    # "halted" | "no_data" | None
```

派生量：

```
bucket := 0 if shares == 0 else (ODD_LOT if shares < 100 else COVERABLE)
coverable_contracts := floor(shares / 100)
```

**别名**（仅供审计阅读与文档引用，判定一律基于三元组）：

| 别名 | 三元组 | 含义 |
| --- | --- | --- |
| `FLAT` | `(0, 0, 0)` | 空仓，轮动起点 |
| `LONG` | `(COVERABLE, 0, 0)` | 持股待收 |
| `SHORT_PUT` | `(0, n, 0)` | 已收租，等指派或到期 |
| `ASSIGNED` | `(COVERABLE, n, 0)` | 指派完成（等同"持股 ＋ 空 put"） |
| `WHEELING` | `(COVERABLE, n, m)` | 轮动常态：持股 ＋ 空 put ＋ 空 call |
| `LONG_CALL` | `(COVERABLE, 0, m)` | 持股只挂了 call |
| `STRANGLE` | `(0, n, m)` | 无持股的双卖（strangle） |
| `CALLED_AWAY` | `(0, 0, m)` 且无权益 | 已被叫走（瞬时） |
| ⚠️ `NAKED_CALL` | `(bucket ≠ COVERABLE, 0, m>0)` 且 `m > coverable` | **告警态**：不受覆盖的 call（步 2 的 I1 本应拦住） |
| ⚠️ `MULTI_CONFLICT` | `n > 1` 或 `m > 1`（同向叠仓） | **告警态** |

**三条设计选择**：

1. **`ASSIGNED` 不是独立状态，而是"持股 ＋ 空 put"** —— 指派是一个**事件**，不是持续状态。要留痕就记事件（`assigned_at`），不要在状态枚举里加一格。
2. **告警态必须是一等公民** —— `NAKED_CALL` 与 `MULTI_CONFLICT` 若是"非法输入"而不是"已知状态"，代码就会在遇到它们时静默走默认分支。它们必须被**命名、被计数、被报警**。
3. **`bucket` 三分而非二分** —— 1 股（当前账面 MRVL/QCOM/T 各 1 股）与 100 股在"能否卖 call"上完全不同，而在"能否加仓"上又相同。二分会丢信息。

### 3.2 迁移表（W2）

**与步 2 的分工，必须先说清**：

| 检查 | 问题 | 归属 |
| --- | --- | --- |
| **腿合法性** | "这条腿能不能与现有腿共存？"（裸卖 call、双空 put、叠 strangle） | **步 2 的 I3** |
| **迁移合法性** | "这条腿是否把标推向它该去的方向，且此刻是它的时机？" | **本步** |

两者**都必须通过**。步 2 是"不出错"，步 6 是"走对路"。下表是迁移表（行 = 三元组，列 = 拟做动作）：

| 状态 \ 动作 | `BUY` | `OPEN_CSP` | `OPEN_CC` | `OPEN_STRANGLE` | `CLOSE` | `ROLL` |
| --- | --- | --- | --- | --- | --- | --- |
| `FLAT` (0,0,0) | ✅ 需 `LONG` 目标 | ✅ | ⛔ `cc_needs_shares` | ✅ | ⛔ `nothing_to_close` | ⛔ |
| `LONG` (◆,0,0) | ✅ 可加仓 | ✅ | ✅ 需 `rail_pos ≥ cc_rail_pos` | ✅ | ⛔ | ⛔ |
| `SHORT_PUT` (0,n,0) | ✅（转 `ASSIGNED` 路径） | ⛔ `duplicate_short_put` | ⛔ `cc_needs_shares` | ⛔ `leg_already_open` | ✅ | ✅ |
| `ASSIGNED` (◆,n,0) | ✅ | ⛔ `duplicate_short_put` | ✅ 需 `rail_pos ≥ cc_rail_pos` | ⛔ `leg_already_open` | ✅ | ✅ |
| `LONG_CALL` (◆,0,m) | ✅ | ✅（这是解锁"多个"的关键格） | ⛔ `duplicate_short_call` | ⛔ `leg_already_open` | ✅ | ✅ |
| `WHEELING` (◆,n,m) | ✅ | ⛔ `duplicate_short_put` | ⛔ `duplicate_short_call` | ⛔ `leg_already_open` | ✅ | ✅ |
| `STRANGLE` (0,n,m) | ✅ | ⛔ `leg_already_open` | ⛔ `cc_needs_shares` | ⛔ `leg_already_open` | ✅ | ✅ |
| ⚠️ `NAKED_CALL` | ✅（补股） | ⛔ | ⛔ `already_naked` | ⛔ | ✅ 优先 | ✅ |
| ⚠️ `MULTI_CONFLICT` | ⛔ `manual_review` | ⛔ | ⛔ | ⛔ | ✅ 优先减仓 | ⛔ |

**原因码必须可枚举**（步 2 A3 已要求前缀纪律，本步沿用）：

```
cc_needs_shares | duplicate_short_put | duplicate_short_call | leg_already_open
nothing_to_close | manual_review | cc_below_cost | cc_rail_not_high
```

### 3.3 目标状态与触发条件（把用户给的形态写成代码判据）

这是"策略内核"的落点。全部用步 4 的 tag 与数值，全部确定性：

| 期望 | 确定性触发 | 产出的 ticket |
| --- | --- | --- |
| **回调时做 Long** | `TREND_STRONG` ∧ `drawdown_pct ∈ [−drawdown_band_high, −drawdown_band_low]` ∧ `RECLAIM` ∧ ¬`EARNINGS_NEAR` | `[BUY]` |
| **Long 旁挂 CSP** | 上述 Long 触发成立 ∧ **`RV_HIGH`**（步 4a 的波动率门；**不是** `IV_RICH` —— 后者需真 IV，属步 4b）∧ 步 5 的 CSP 候选非空 | `[BUY, OPEN_CSP]` |
| **六月式 dip** | 上述任一 ＋ `DIP_BOUGHT`（此时 `rail_pos` 已回到 0.45 以上，`LOWER_RAIL` 已消失） | 同上 |
| **高位卖 CC** | `shares ≥ contracts × 100` ∧ `strike ≥ cost_basis` ∧ (`rail_pos ≥ cc_rail_pos` ∨ `UPPER_RAIL`) ∧ ¬`EARNINGS_NEAR` | `[OPEN_CC]` |
| **收租平仓** | `management_tactic`（`dte ≤ roll_dte` 或 captured ≥ `profit_take_pct`） | `[CLOSE]` / `[ROLL]` |
| **不做** | 其余一切 | 不产生 ticket（记账为 `quiet`，原因码具名） |

**关键**：这张表是**唯一**的"什么时候开仓"的真源。Engine A 与 Engine B 都读它，不再各自拥有 `choose_tactic` 或各自的 agent 提示词。

### 3.4 ticket 实体化（W3）

迁移 `031_tickets.sql`（**加表 ＋ 加列，不删不改**）：

```sql
CREATE TABLE tickets (
    id           text PRIMARY KEY,
    account_id   text NOT NULL,
    symbol       text NOT NULL,
    state_before text NOT NULL,          -- 别名，审计可读
    state_after  text NOT NULL,          -- 预期到达的别名
    legs         integer NOT NULL,       -- 腿数，冗余便于统计
    reason       text NOT NULL,          -- 触发来源（"dip_long"、"high_cc"、"manage_roll"…）
    status       text NOT NULL,          -- pending | partial | done | expired
    simulated    boolean NOT NULL DEFAULT false,
    created_at   timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE drafts ADD COLUMN ticket_id text;   -- 可空，兼容历史行
ALTER TABLE drafts ADD COLUMN leg_index integer; -- 0-based，可空
CREATE INDEX tickets_symbol_idx ON tickets (account_id, symbol, simulated, created_at DESC);
```

**腿的顺序语义**（`leg_index` 的意义与限制）：

- `leg_index` **只表达"同时提交的一组腿"**，不表达"成交后再到期"。原因：`OPEN_CC` 的持股前置（步 2 的 I1）在**求值时刻**检查 `shares`，而不是"BUY 成交后的 shares"。因此：
- **`[BUY, OPEN_CC]` 是不合法的 ticket** —— I1 在求值时会看到"当前无股"而拦掉 CC。CC 必须落在**后续一轮**（BUY 成交之后）。这正是现有 Call 提示词那句话（"If a share purchase is proposed and not yet filled, stay quiet"）的正确性来源，本步把它从提示词搬进代码。
- `[BUY, OPEN_CSP]` 合法（CSP 不依赖持股）。
- `[OPEN_STRANGLE]`＝1 条 ticket ＋ 2 条腿（`put` 在前、`call` 在后）。
- **腿数上限 = 2**（见 D25）。

**批量风险**：`block_ticket_reason`（步 2 的 I2）的键从"同批次"改为 `ticket_id`。任一腿 `blocked` → 整张 ticket 的其余腿一并改判 `blocked`，原因指向 ticket 级原因。

### 3.5 LLM 角色收缩（W4）

现在 7 个 agent 里，**5 个在"做决定"**（CSP/Purchase/Strangle/Call/Consolidator）。收缩为**2 个"读"＋1 个"写"**：

| 现 agent | 改后角色 | 说明 |
| --- | --- | --- |
| `Location` | **保留**（标签） | 仍是 tag 来源之一；与步 4 的确定性指标并存 |
| `News` | **保留**（标签 ＋ `news_score`） | 事件阅读与情绪分；`news_score` 进 `Numbers` 块 |
| `CSP` | **删除**（作为决策者） | 由 §3.3 的确定性触发 ＋ 步 5 的候选代替 |
| `Purchase` | **删除**（作为决策者） | 同上 |
| `Strangle` | **删除**（作为决策者） | 同上 |
| `Call` | **删除**（作为决策者） | 同上 |
| `Consolidator` | **改名 `Narrator`，职责收窄** | 只做两件事：① 用一句话解释"为什么这轮是这个 ticket"（写进 `drafts.reason` 与 `decision_trails.thinking`）；② 在代码给出的 top-N 候选里**选一条**（可选动作，不选则用 `screener_pick`）。**不得**新增表外腿、不得改变尺寸、不得否决合法性 |

**收益（可测）**：每标的每轮调用数从 **7 → 3**，按评审实测的 28 次/agent 推算，2 标的 14 轮的总调用从 ~196 降到 ~84；`ai_calls` 的噪声降一半以上。

**与 D11 的一致性**：模型提名、代码定夺。`Narrator` 的输出若与代码结论冲突（例如它要一条表外的行权价），**以代码为准**，并在审计里记 `narrator_overruled`。

### 3.6 Engine A 的 `choose_tactic` 退场（D10）

- **删除** `choose_tactic`，同时删除 `evaluate_symbol` 里 `if tactic in {"OPEN_STRANGLE","OPEN_CSP","OPEN_CC"}: tactic = "HOLD"` 这段**自我抹除**，以及 `OPEN_TACTICS` 的开仓分支（`OPEN_STRANGLE`/`OPEN_CSP`/`OPEN_CC` 三段）。
- Engine A 的职责收窄为：**写 phase ＋ 写状态**（`symbol_phases.phase`、`symbol_phases.state`、`symbol_phases.indicators`）。它**不再**产生草案。
- `choose_tactic` 的调用者（`evaluate_symbol`）与既有测试 `test_evaluate_symbol_stores_open_as_hold` 一并更新为"断言不再有 `tactic` 列写入（或列保留但恒为 `HOLD`）"。
- `_put_leg` / `_call_leg` / `_strangle_plan` 三处 Engine A 专用取价函数删除（改由步 5 的 `screener` 供数）。

### 3.7 `manage_book` 并入同一状态机

- `manage_book` 保留（它处理的是"已有腿的处置"，与开仓的时机不同），但**产出改为 ticket**（`reason="manage_roll"` / `"manage_close"`）。
- 同一标的在**同一轮**只允许一张 `ROLL`/`CLOSE` ticket：把 `DISTINCT ON (symbol, ...)` 的行先按 `symbol` 归组，**一次决策覆盖该标的的全部 opt 腿**，避免 2.4 里"两条腿各自决定"的漏洞。
- 处置优先级：`NAKED_CALL` / `MULTI_CONFLICT` 告警态 → **先减仓，且本轮不出任何开仓 ticket**。

### 3.8 状态落库（审计用）

迁移 `032_phase_state.sql`：

```sql
ALTER TABLE symbol_phases ADD COLUMN state text;      -- 别名，审计可读
ALTER TABLE symbol_phases ADD COLUMN leg_counts text; -- "p2,c1,s300" 形式的紧凑快照
```

**只写不读**：所有判定从持仓现算（W1），落库仅用于事后复核"当时看到的状态是什么"。这与 `phase`/`tactic` 的现状一致（那两列也是审计快照）。

---

## 4. 决策点

| # | 待决 | 选项 | 推荐 | 落地 |
| --- | --- | --- | --- | --- |
| **D7** | ticket 实体化 | (a) 新表 `tickets` ＋ `drafts.ticket_id`（3.4）(b) 只用同批次 id 分组 | **(a)** | **已落地 (a)。** 表在 `038_tickets.sql`，不是原稿的 `031` |
| **D10** | Engine A 的 `choose_tactic` | (a) 删除（3.6）(b) 保留但继续改写为 HOLD | **(a)** | **已落地 (a)。** 开仓函数离开 `phases.py`。`tactic` 恒为 `HOLD`。旧选择留在 `legacy_tactics.py`，只给关闭开关的路径用 |
| **D11** | LLM 权限边界 | (a) 只能提名（3.5）(b) 可否决合法提案 | **(a)** | **已落地 (a)。** 表外行权价记 `narrator_overruled`。非法回复保留代码选出的第 1 档 |
| **D23** | 状态是否落库 | (a) 落 `symbol_phases.state` 快照（3.8）(b) 纯现算、不落库 | **(a)** | **已落地 (a)。** 列在 `039_phase_state.sql`，不是原稿的 `032`。判定仍现算 |
| **D24** | LLM 收缩程度 | (a) 收缩为 3 agent（Location/News/Narrator）(b) 保留 7 agent 只改职责 | **(a)** | **已落地 (a)。** 默认三次调用。`ATHENA_STATE_MACHINE=off` 才走旧的七个 agent |
| **D25** | ticket 腿数上限 | 2 / 3 / 不限 | **2** | **已落地。** 开仓与管理都最多两腿 |

---

## 5. 验收标准

### 5.1 端到端（主计划的 G1/G2）

| # | 验收 | 判据 | 落地 |
| --- | --- | --- | --- |
| **G1** | 能开 | 在一条刻意构造的"有序回调"序列上跑 paper 一轮：`tickets` 出现 1 行 `reason='dip_long'`，`drafts` 出现 `BUY` 与 `OPEN_CSP` 各 1 条，且二者 `ticket_id` 相同、`leg_index` 为 0/1 | **已落地（夹具）。** `fixture_june_dip`。`may_buy` 打开时同一张票是 `BUY` 加 `OPEN_CSP`。收入型且未开 `may_buy` 时没有 `BUY`。股数仍是 `open_quantity` |
| **G2** | 能收口 | 在 `ASSIGNED`（持股 ≥100 ＋ 空 put）且 `rail_pos ≥ cc_rail_pos` 的序列上：出现 1 张 `[OPEN_CC]` ticket，且 `call_strike ≥ positions.avg_price` | **已落地（夹具）。** `fixture_intc_high`。300 股、成本 95，一张 `OPEN_CC`，行权价不低于成本 |

### 5.2 状态机

| # | 验收 | 判据 | 落地 |
| --- | --- | --- | --- |
| **A1** | 状态可复算 | 对同一组 fixture 持仓，`wheel_state()` 连调两次结果**逐字段相同**；不依赖 dict/set 迭代顺序 | **已落地。** 连调相同。已提交的卖出 put 计入空头 |
| **A2** | 别名覆盖 | §3.1 的 8 个别名 ＋ 2 个告警态各有至少 1 个 fixture，且派生别名与三元组一致 | **已落地，并多了两个别名。** 原稿 8 个别名里没有 `CALLED_AWAY`。另有 `ODD_LOT`、`ODD_PUT`。`(0, 1, 1)` 是 `STRANGLE`，不是裸 call |
| **A3** | 迁移表逐格 | §3.2 表中**每一个 ⛔ 格**都有负面 fixture（至少得原因码），**每一个 ✅ 格**都有正面 fixture | **当前触发能走到的五格已有草案。** 禁止格都有原因码。其余合法格没有各跑一张草案 |
| **A4** | 告警态不静默 | 构造 `(1 股, c1)` → 状态为 `NAKED_CALL`，本轮**不产生任何开仓 ticket**，且写一条 `decision_trails(outcome='dropped', summary='NAKED_CALL')` | **已落地。** 不开仓。也不因此起草平仓 |
| **A5** | 非法组合不产生 ticket | `[BUY, OPEN_CC]` ticket → CC 腿 `blocked`，原因为 `cc_needs_shares`；BUY 腿是否保留见 ticket 策略（推荐整张 `blocked`） | **已落地。** 两腿都 `blocked`，原因都是 `cc_needs_shares` |
| **A6** | 单轮单 ticket | 同一 (account, symbol, simulated) 在**同一轮**内不会产生两张不同的开仓 ticket | **已落地，并且更严。** 同一天已有 `dip_long` / `dip_csp` / `high_cc` / `cover_gap` / `signaled_strangle` 则不再开 |

### 5.3 收缩与真源

| # | 验收 | 判据 | 落地 |
| --- | --- | --- | --- |
| **A7** | Engine A 不再开仓 | 全库 grep `OPEN_CSP\|OPEN_CC\|OPEN_STRANGLE` **不再命中** `phases.py`；`symbol_phases` 不再写入 `tactic` 的开仓值 | **已落地。** `phases.py` 零命中。`tactic` 恒为 `HOLD` |
| **A8** | 调用数下降 | 同一 fixture 一轮的 `ai_calls` 行数 = 标的数 × **3**（原为 ×7） | **已落地。** 一轮是 Location、News、Narrator |
| **A9** | 模型不能越权 | 构造 `Narrator` 返回一条表外 strike → 该腿被丢弃，审计出现 `narrator_overruled` | **已落地。** 唯一的期权腿被丢掉后，不写 ticket |
| **A10** | ticket 批量风险成键 | 一张 2 腿 ticket 合计击穿 `cash_reserve_pct` → 两腿**都** `blocked`，原因指向 ticket | **已落地。** 两腿都挡住，原因带 ticket id。审计结果是 `dropped`，不是 `blocked` |

---

## 6. 明确不做

- **不做组合级优化**（多标的之间的资金分配、相关性约束）。
- **不做一天多张 ticket**：同一标的每日至多一张新开仓 ticket（防止在震荡里反复进出）。
- **不放开自动下单**：`all_orders` ＋ 人工 Approve 不变；ticket 只是把"批准"的单位从一条腿变成一批腿。
- **不改 `management_tactic` 的数值判据**（`roll_dte`、`profit_take_pct`），只改它的产出形态与汇总粒度。
- **不删 `symbol_phases.tactic` 列**（历史数据要留），只停止写入开仓值。
- **不引入 learn-to-rank / 强化学习**：触发条件是手写阈值，全部可解释。

---

## 7. 风险与回滚

| 风险 | 说明 | 对策 |
| --- | --- | --- |
| 契约收缩一次幅度大 | 删 4 个 agent ＋ 删 `choose_tactic` ＋ 加 ticket，同一步内 | 依赖前置已绿（步 2/4/5）；且 `ATHENA_STATE_MACHINE=off` 可一键回到旧路径（旧代码保留一个版本） |
| ticket 让"批准"变重 | 一次 Approve 批准 2 条腿 | 这是**目的**（避免半批成交）；ticket 页需把两腿并排展示 —— 属前端，不在本套计划 |
| `Narrator` 退化 | 收缩后模型无事可做，可能编造 | A9 的越权测试 ＋ `narrator_overruled` 计数；若长期 0 越权，可再考虑关闭该 agent |
| 状态推导与持仓同步竞态 | `positions` 是快照表，同步与策略同轮 | 状态在**同一连接同一事务**内推导（现算）；`snapshot_at` 取最新 |
| 历史数据无 `ticket_id` | 18 行旧 drafts | 列可空；审计查询按 `ticket_id IS NULL` 归为"改造前" |
| 回滚 | 迁移 031/032 均为加表/加列 | 逻辑回滚：开关关闭 → 旧路径；数据回滚：无需删列（向后兼容），`tickets` 表可留空 |
