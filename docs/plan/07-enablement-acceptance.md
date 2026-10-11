# 步 7 — 启用、配置与验收

**主计划**：[00-plan-order.md](00-plan-order.md) ｜ **状态**：已落地（2026-10-11）
**依赖**：步 6 ｜ **阻塞**：无。活账户仍是 `test`。多日影子对照是操作步骤，不是代码改动

## 0. 落地结论

配置与开关在 `wheel.may_buy`、`slots.observe_allowed` / `draft_allowed`、`agents.run_observe`、`paper_payload`、迁移 `040`。活账户 `DUR253115` 仍是 `test` 档，没有切到 `paper`。

**已落地**

- 工作日 07:00 至午夜观察，周末不动。草案仍只在 Athena slot。非草案时段只跑 Location 与 News，审计是 `observed` / `outside_draft_slot`。
- `may_buy` 与意图分开。收入型标的默认不买股。该标的显式打开后，回调可以买。奇数股补满 100 股的缺口单不走这道门。
- `/risk` 有 `paper` 档。十项是 `25 / 20 / 8 / 5 / 40 / 21 / 20000 / 6 / 35 / 7`。点档会填这十项。没有对活账户保存。
- 新空头读 `min_rv_pctl_new_short`，量是该标的自己的 `RV20` 百分位。样本不足是 `no_data` / `rv_samples`。低于门槛是 `quiet` / `rv_percentile`。`paper` 档门槛是 40。39 拒绝、41 放行有测试。
- `ATHENA_DRY_RUN` 默认关。打开后本会开出的票记成 `observed`，摘要 `would ticket: …`，不写草案。
- 本步开关是 `ATHENA_MAY_BUY`（默认开）、`ATHENA_SLOT_SPLIT`（默认开）、`ATHENA_DRY_RUN`（默认关）。
- tracker 的 `P4-01` 不再把开仓 tactic 写成存在 `symbol_phases` 上。`P4-18` 与 `P4-18b` 已拆开。策略文档的冲突规则已改成覆盖、重复空头、以及 `strict_conflict`。

**裁定后与原稿不同、且已按裁定落地**

| 裁定 | 代码 |
| --- | --- |
| 缺省买股仍是旧行为 | 旧行为按**当时代码**：`long` 且意图不是 `income`，所以 `both` 也能买。不是原稿写的「必须 `accumulate`」 |
| `ATHENA_MAY_BUY=off` | 即使该标的写了 `may_buy`，也回到上面的旧判断 |
| 身份判定比十项 | `max_symbol, cash, halt, contracts, min_rv, min_dte, position, orders, margin, blackout`。把 `paper` 的每日单数从 6 改成 50，档名变为 `custom` |
| 换轴旋钮先放在现有风控表 | 标签是 Min RV percentile new short。不称 implied vol。与 `rv_high_pctl` 同卡仍是步 9 |

**仍是操作步骤，不是未做的代码**

- 多日影子对照没有在 `DUR253115` 上跑。夹具都在 `ZZTEST`。`paper` 档只在 `ZZTEST` 上保存过，保存后已把该账户的活动行恢复。
- 成本对照是一次调用数：观察写 Location 与 News，完整一轮再写 Narrator。没有多日 `ai_calls` 行数对比。
- 没有 `ATHENA_PRICE_SINGLE_SOURCE`。回滚测的是现有六个开关一起关掉后仍写一条审计。不是原稿的七个开关逐一对照改造前。
- 旧字段名在模型、风控、提示词、界面标签里已消失。迁移 `040` 与本套计划文档仍写出旧名，用来完成改名。Volatility reading 卡仍是步 9。

---

## 1. 目标

前六步把"看得对、不出错、说得出、挑得好、走得通"都做完了，但**系统仍然一件也不做**。本步解决最后三件"行政堵死"的事，并给出可执行验收：

| # | 目标 | 一句话判据 |
| --- | --- | --- |
| **E1** | **Long 在配置上可达** | 存在一组配置，使 `BUY` 草案能通过 `_drafts` 的 flags 门（当前 31 个标的**无一**可达） |
| **E2** | **观察与许可解耦** | 任一轮非草案时段，审计仍能看到"这个标的当时是什么状态、什么 tag"，而不是空白 |
| **E3** | **两条用例端到端可复现** | G1（回调 → `[BUY, CSP]`）与 G2（高位 → `[CC]`）在一套**确定性 fixture** 上跑通，且可一键回滚 |
| **E4** | **文档与代码同频** | tracker 的 `P4-01`、`P4-18` 与策略文档的冲突规则表述被更正为与代码一致 |

---

## 2. 现状取证

### 2.1 Long 被**两条不同的路**同时堵死（比"配置写错"更严重）

`_drafts` 的门：

```python
if tactic == "BUY" and (not flags.long or flags.intent != "accumulate"):
    why = "Long is off" if not flags.long else "intent is not Accumulate"
```

活配置（`settings_versions` 最新版）实测：

| 分组 | 标的数 | `tactic_flags` 内容 | `BUY` 被拦原因 |
| --- | --- | --- | --- |
| 显式配置 | **7**（1810, AVGO, GLD, INTC, MRVL, NOK, NVDA） | `long=true, intent="income"` | **`intent is not Accumulate`** |
| 未配置 | **24**（其余 watchlist） | 无条目 → `TacticFlags()` 默认 `long=False` | **`Long is off`** |

⇒ **watchlist 全部 31 个标的都无法产生 Long 草案**，但原因分两类。这意味着：只把 7 个标的的 `intent` 改成 `accumulate` 依然不够 —— 另外 24 个的 `long` 默认是 `False`，会被另一条分支拦下。评审 §3.10 只说了前一半。

### 2.2 活风控档是 `test`，多处门形同虚设

| 参数 | 活值 | 后果 |
| --- | --- | --- |
| `profile` | `test` | 非生产档 |
| `min_dte_new` | **1** | `block_reason` 的 DTE 门几乎永不触发（步 5 的 `dte` 成了纯展示值） |
| `min_iv_rank_new_short`（**待改名 `min_rv_pctl_new_short`**） | **0** | IV rank 门失效（也间接掩盖了步 4 的 IV 反相问题） |
| `max_position_per_symbol` | **0** | 市值上限关闭 ⇒ `open_quantity` 恒返回 `1` |
| `max_symbol_pct_nav` | 80 | 单标的可占 NAV 80% |
| `cash_reserve_pct` | 5 | 现金预留 5% |
| `max_contracts_per_symbol` | 12 | 与"上限关闭"叠加后，唯一实际生效的 cap 是 12 |
| `daily_loss_halt_pct` | 20 | 日亏 20% 才熔断 |
| `max_short_opt_margin_pct` | 50 | 空头保证金上限 |

⇒ 这不是"配置错了"，而是**验收用的档位本身不具备验收价值**：一个 12 张合约、80% NAV、无市值上限的档位，无法用来判断"是否过度开仓"。

### 2.3 slot 语义不对等，导致审计无法回答"看过没有"

```python
draft_ids = evaluate_watchlist(conn, settings, account)          # 无条件
if slot_state(now, settings.ib) == "athena":
    draft_ids.extend(run_signaled(conn, settings, account))       # 有时段门
```

- Engine A：**任何时段**都跑。
- Engine B：**只在 Athena slot** 跑。

⇒ 在 Mobile slot（19:00–21:30）与 idle 时段（12:00–13:00、16:00–21:30、23:55–09:30），**信令引擎完全不观察**，`ai_calls` / `decision_trails` 里没有那一轮的任何行。步 3 定义的四态（`drafted`/`quiet`/`no_data`/`invalid`）**没有一格**能表示"这一轮我们没看"。

设计与代码的意图也不一致：tracker 的 "Locked for Phase 1" 写的是

> Live quotes: iTick. IB: history, positions, orders, **inside Athena slots**.

即"**IB 依赖的动作**限时段"，而"看报价、读新闻"不限时段。当前实现把整个信令引擎一起关了。

### 2.4 规模与成本（用于给 E2 定优先级）

| 量 | 值 |
| --- | --- |
| 周期 | `cycle_minutes = 5` |
| Athena slot 总时长 | 2.5h ＋ 3h ＋ 2.417h ＝ **7.917h** → 约 **95 轮/日** |
| watchlist | **31** 标的 |
| 每标的每轮调用（现状） | **7**（RUN_ORDER） |
| 最坏日调用量 | 95 × 31 × 7 ≈ **20,600 次/日** |

步 3 的预过滤（`no_chain` 标的降到 2 次）与步 6 的收缩（7 → 3）叠加后，量级降到原来的约 **1/3**。但在 E2 把观察时段扩展到全天之后，"轮数"会上升，**必须**先有步 3 与步 6 的降本，否则扩大观察窗口会直接放大账单。这是本步排在最后的原因。

### 2.5 三处文档漂移

| 位置 | 现文本 | 与代码的冲突 |
| --- | --- | --- |
| `10-implementation-tracker.md` **L82 (P4-01)** | "One symbol walks P1 HOLD, P2 OPEN_STRANGLE, P3 HOLD, P4 CLOSE, **P4 OPEN_CSP, P5 OPEN_CC**. Each pair is stored on `symbol_phases`." | 代码把 `OPEN_*` 抹成 `HOLD`（有测试断言），DB 里 `symbol_phases` 30 行**全为 HOLD** |
| `10-implementation-tracker.md` **L150 (P4-18)** | "No conflicting directional shorts … Fixture `ZZCF` already had a short option. A new CSP was stored as blocked." | 该表述只在"既有空头是 short put"时成立；步 2 的 I3 之后，"已有 short call → 新 CSP 允许"（这正是轮动） |
| `03-strategy-news-frontrun.md` **L76** | "Conflict rule (Settings-enforced): do not hold conflicting directional short options on the same underlying unless explicitly allowed" | 步 2 把"一刀切"改成状态感知矩阵；本文与代码不再一致 |

---

## 3. 变更逻辑 spec

### 3.1 E1 — 把 `may_buy` 与 `intent` 解耦（D9）

**为什么不是"改配置"**：`intent` 目前同时承担两个语义 —— "这是收益型还是累积型标的"与"允不允许买股"。前者是策略意图，后者是权限。把权限挂在意图上，就出现"要买股必须先声明自己是累积型"的耦合。改配置能解一时，改语义才不再复发。

**改动**：

```python
class TacticFlags(BaseModel):
    csp: bool = True
    cc: bool = True
    strangle: bool = True
    intent: Literal["income", "accumulate"] = "income"
    long: bool = False
    may_buy: bool | None = None          # None = 旧行为（向后兼容）

    @property
    def buy_allowed(self) -> bool:
        if self.may_buy is not None:
            return self.may_buy
        return self.long and self.intent == "accumulate"     # 旧 payload 行为不变
```

- `_drafts` 的门改为 `if tactic == "BUY" and not flags.buy_allowed:`，原因文本相应改为 `may_buy is off`。
- `Purchase` 提示词把"Propose only when Long is on, intent is Accumulate"改为"Propose only when `may_buy` is on"；`{{long}}` 与 `{{intent}}` 继续传，作为上下文。
- **向后兼容是硬要求**：`may_buy` 缺省为 `None` 时**逐字复现旧行为**。这样本步的迁移是"改一个 JSON 字段"，不是"改逻辑"。
- **最小可达配置**（用于 E1 验收）：对想要 Long 的标的显式写 `{"may_buy": true}`；其余标的**不动**，继续不可买。

### 3.2 E2 — 观察与许可解耦（D8）

新增两个 settings 项：

```
StrategySettings.observe_slots = "always"      # always | athena | athena+mobile
StrategySettings.draft_slots   = "athena"      # 与现状一致，可调
```

`run_signaled` 拆成两段：

```
observe_symbols(conn, settings, account)        # 按 observe_slots；算 tag / 状态 / 指标 / 候选
                                                # 写 symbol_phases（phase, state, indicators, tags）
                                                # 不产生 drafts、不调用 Narrator
draft_from_observation(...)                     # 按 draft_slots；由 §步6 的迁移表产生 ticket
```

**关键设计**：`observe_symbols` 的成本上限是 `Location + News = 2 次调用/标的`，因为确定性指标不需要模型。这样"全天观察"的边际成本是可接受的：

| | 轮数 | 每标的调用 | 日调用 |
| --- | --- | --- | --- |
| 现状（仅 Athena slot 全文） | 95 | 7 | ≈20,600 |
| 观察全天（07:00–24:00 ≈ 204 轮） | 204 | **2** | ≈12,650 |
| 其中草案时段（95 轮） | 95 | 2 ＋ 1（Narrator） | ≈8,835 |

⇒ **扩大观察窗口的同时总成本下降**，前提是步 3 与步 6 已完成。

**审计第五态**：`observe_symbols` 在非草案时段的输出必须落一行 `decision_trails`，新增 outcome：

| outcome | 含义 | 触发 |
| --- | --- | --- |
| **`observed`** | 已评估、未进入草案阶段（时段未许可） | `observe` 跑了、`draft_slots` 不允许 |

⇒ **步 3 的 `outcome` 枚举从四态变五态**（§5 A5 要求同步更新 [03-audit-discernibility.md](03-audit-discernibility.md) 与 D14 的 CHECK 约束）。这是本步对步 3 的唯一反向修改，必须显式记录。

**`observe_slots` 的取值范围必须受限**：`"always"` 指的是 **HKT 07:00–24:00**（可配置的窗口），不是 24×7。理由：凌晨的时段没有新报价与新头条，纯烧钱；且 `slot_state` 已有周末 idle 语义，`observe` 沿用同一周末开关。

### 3.3 配置基线：新增 `paper` 档（D26）

本步**不改** `test` 档（它是开发用档），而是新增一个专供验收/paper 运行的档位：

| 参数 | `test`（现） | **建议 `paper`** | 理由 |
| --- | --- | --- | --- |
| `profile` | `test` | **`paper`**（新增取值） | 与 `test` 明确区分 |
| `min_dte_new` | 1 | **21** | 与 `strategy.dte_min = 21` 一致；短于 21 天的短期权不该被开出 |
| `min_iv_rank_new_short` → **`min_rv_pctl_new_short`** | 0 | **40** | 语义改为"`RV20` 自身历史百分位 ≥ 40"，即**标的确实在动**。详见下方 **D36**（已裁定，含改名） |
| `max_position_per_symbol` | 0 | **20000** | 打开市值上限，"1 张永远"的怪状态消失 |
| `max_symbol_pct_nav` | 80 | **25** | 单标的不该能占 80% NAV |
| `cash_reserve_pct` | 5 | **20** | 步 2 的 I2 需要真实的现金约束才能被验证 |
| `max_contracts_per_symbol` | 12 | **5** | 与市值上限共同生效 |
| `daily_loss_halt_pct` | 20 | **8** | 熔断要能在验收中真的被触发一次 |
| `max_short_opt_margin_pct` | 50 | **35** | 同上 |
| `max_orders_per_day` | 10 | **6** | 一日 6 单已足够覆盖"多个"用例 |

> **这些数字是建议，不是结论。** 见 D26 —— 它们决定验收跑出来的规模，最终取值由你定。上表的作用是让"用什么档位验收"这件事有明确答案，而不是拿 `test` 档验收。

> **⚠ D36 —— `min_iv_rank_new_short` 在 D4a 期间是反相的（✅ 已裁定 (a)：换轴 + 改名）**
>
> 这个门读的是 `iv_rank`，而 `iv_rank` 来自反相代理（[04](04-indicator-tags.md) §2.1：`corr(iv_proxy, 1/underlying) = 1.000000`）。于是 `≥ 40` 的真实含义是"**价格处于区间下部**"，不是"波动率溢价够高"。虽然它在效果上偶然接近"跌了才卖"，但：
> 1. **语义是错的**，且与步 4 已摘除的 `HIGH_IV` 是**同一个病**（同一份反相量换个位置继续当波动率用）；
> 2. **会被重复计数** —— 步 6 的触发表里 `LOWER_RAIL` / `drawdown_pct` 已经在做"价格低"这件事；
> 3. **提示词会说谎** —— 若 prompt 把 `min_iv_rank` 描述成"波动率门"，模型会据此推理。
>
> **裁定结果**（选项 (a)）：
>
> | 项 | 变更前 | 变更后 |
> | --- | --- | --- |
> | 字段名 | `min_iv_rank_new_short` | **`min_rv_pctl_new_short`** |
> | 读的量 | `iv_rank`（∝ 1/S，反相） | **`rv_pctl`** = `RV20` 在自身历史（同 method 窗口 ≥ 20）中的百分位 |
> | 语义 | "溢价够高"（实为"价格低"） | **"标的确实在动"** |
> | 数值 | 40 | **40**（不变） |
> | 与 `LOWER_RAIL` 的关系 | **重复计数** | **正交**：一个问"波动"，一个问"位置" |
>
> 附带要求：
> 1. **旧名必须零命中**（`min_iv_rank_new_short` 在仓库内不再作为可读字段出现；`iv_rank` 本身降级为只读诊断量，只在 [09](09-risk-control-ui.md) 的 legacy 证据组里显示）。
> 2. **与 `rv_high_pctl` 同卡**：本字段落在 [09](09-risk-control-ui.md) §3.2(b) 的 `Volatility reading` 卡片，与 `rv_window` / `rv_high_pctl` / `rv_low_pctl` 同组 —— 否则界面上会出现两个"看起来都是波动率门"的旋钮。
> 3. **prompt 措辞同步**：描述改为"标的历史波动率分位"，**不得**出现 `iv` / "implied" 字样（[04](04-indicator-tags.md) §3.1.6 第 2 条硬约束）。
> 4. **样本不足时的行为**：`rv_pctl` 需要 ≥ 20 个同 method 样本；不足时该门**必须表现为"样本不足"而非"通过"或"无信号"**（与步 3 的 `no_data` 契约、步 4 的 `ivr_min_samples` 一致）。

`paper` 档的实现方式：`RiskProfile` 的 `Literal` 加 `"paper"`，并在 `standard_payload` 同级加 `paper_payload()`；`test` 一行不改。

### 3.4 E3 — G1/G2 的确定性验收工装（D27）

**不用真实历史某一天做 fixture。** 理由：真实某天只有一个样本、不可复现、且会被步 1 的口径变更打乱。改为**合成序列 + 注入**：

```
fixture_june_dip():
  1. 造 252 根 ib-daily（NVDA）：前 200 根缓慢上行（使 CHANNEL_RISING 成立），
     随后 8 根下砸至区间 −11%（落在 DRAWDOWN_OK 带内），
     随后 3 根收复（rail_pos 从 0.12 回到 0.52、RECLAIM 成立），
     全程量能正常（保证 ATR / RV 可算）
  2. 注入 1 行 option_plans ＋ 该到期 ≥4 档 put/call 的 option_choices（带 limit_price）
  3. 注入 1 条负面新闻簇（NEWS_WASH）与 1 段**高已实现波动**的收益序列
     （使 RV20 落在自身历史高位 ⇒ `RV_HIGH`。**不得**注入"credit 序列"来凑 `IV_RICH`——步 4a 不产出 IV，见 04 册 §3.1.3）
  4. 断言 G1
fixture_intc_high():
  1. 同一序列，但注入 positions：STK 300 股 @ avg_price 95
  2. 价推至区间上沿（rail_pos ≥ 0.92）
  3. 断言 G2：出现 [CC]，且 strike ≥ 95
```

**辅助（不是替代）：live shadow**。`ATHENA_DRY_RUN=1` 时，`draft_from_observation` 只写 `decision_trails`（`outcome='observed'`，`summary` 里带"would ticket: …"），**不写 drafts**。用真实 watchlist 跑 N 个交易日，比对"本会开出什么"。两道验收都要，fixture 在前（可复现），shadow 在后（防 fixture 过拟合）。

### 3.5 灰度与回滚开关总表

| 开关 | 关闭后行为 | 默认 | 归属步 |
| --- | --- | --- | --- |
| `ATHENA_PRICE_SINGLE_SOURCE` | 恢复旧的多口径读法（保留 `_legacy` 函数） | on | 1 |
| `ATHENA_INDICATORS` | `_context` 退回旧 tags 串，不给 Numbers 块 | on | 4 |
| `ATHENA_SCREENER` | `_priced_lines` 恢复旧文本格式 | on | 5 |
| `ATHENA_STATE_MACHINE` | 恢复旧路径（`choose_tactic` ＋ 7 agent） | on | 6 |
| `ATHENA_MAY_BUY` | `buy_allowed` 退回 `long ∧ accumulate` | on | 7 |
| `ATHENA_SLOT_SPLIT` | `observe` 与 `draft` 合并回"仅 Athena slot 全文" | on | 7 |
| `ATHENA_DRY_RUN` | 只记 `observed`，不产生 drafts | off | 7 |

**迁移清单（全部向后兼容）**：

| 号 | 内容 | 归属步 | 回滚 |
| --- | --- | --- | --- |
| `027` | `DELETE FROM market_bars WHERE source='seed'` | 1 | 不可逆（seed 是假数据，删除即修复） |
| `028` | `iv_marks` 加 `method` | 4 | 无需回滚（加列） |
| `029` | `symbol_phases` 加 `indicators jsonb` | 4 | 无需回滚（加列） |
| `030` | `option_choices` 加 `bid/open_interest/volume/iv` | 5 | 无需回滚（可空加列） |
| `031` | 新建 `tickets`；`drafts` 加 `ticket_id/leg_index` | 6 | 无需回滚（加表/加列） |
| `032` | `symbol_phases` 加 `state/leg_counts` | 6 | 无需回滚（加列） |
| `033` | `decision_trails.outcome` 加 CHECK（含第五态 `observed`） | 3/7 | 约束可 drop |
| `040` | 已存设置里的 `risk.min_iv_rank_new_short` 改名为 `min_rv_pctl_new_short`。不改数值，不改哪一行是 active | 7 | 可把键名改回 |

> **注意顺序**：`027` 是唯一不可逆的一步，执行前须完成步 1 的 D2 拍板（已确认 seed 行仅为常数、小时级、零成交量的假数据）。

### 3.6 E4 — 文档勘误的**替换文本**

| 位置 | 替换为 |
| --- | --- |
| tracker L82 (P4-01) | `P4-01 · Full phase machine and CSP, CC, Strangle rules · done` → 说明改为："One symbol walks P1→P5 phases only. **Tactic selection is not stored** — `symbol_phases.tactic` was written as `HOLD` for every open tactic, asserted by `test_evaluate_symbol_stores_open_as_hold`. Opening is owned by the signaled path and (after step 6) by the wheel state machine." |
| tracker L150 (P4-18) | 拆成两行：`P4-18a 已有 short put → 新 CSP 拒绝 (duplicate_short_put)`；`P4-18b 已有 short call → 新 CSP 允许（轮动，受 I1/I2 约束）`。并注："原表述 'no conflicting directional shorts' 已由状态感知矩阵取代" |
| `03-strategy-news-frontrun.md` L76 | 冲突规则改为："Conflict rule: a new option leg is rejected only when it is *genuinely* illegal — an uncovered short call, a duplicate short put, a duplicate short call beyond share coverage, or a second option leg on a symbol that already carries a strangle. A short put together with long shares and a short call is the **normal wheel state** and is allowed." |

同时新增一行 `P4-19`（或其他下一个空号）记录本次改造的验收结论，指向本套计划的 07 文档。

### 3.7 交付与运行手册

**你要做的三件事**（按顺序）：

1. **裁定 D1–D39**。未裁定的项不阻塞其余步，只阻塞它归属的那一步。
   - **本步已落地**：D8 (a)、D9 (b)、D26 (b)、D28、D36 (a)。D27 只落地了干跑开关。见 §0。
   - **已随更早的步落地**：D4a、D4b 的采集段、D37 (c)、D38 (b)。D39 与 Volatility reading 卡仍是步 9。
   - **其余待决**：D29–D35。
2. **选定验收档位**（D26）并确认 `paper` 档数字（含 D36 的字段改名）。数字已按 §3.3 放进 `paper` 档。活账户没有切过去。
3. **批准灰度顺序**：步 1 → **4b①（采集，与步 2/3 并行即可）** → 步 3 → 步 2 → 步 4 → 步 5 → 步 6 → 步 7 → 步 9（与主计划依赖图一致；步 2 与步 3 可互换，但都必须在步 6 之前；**4b② 与步 8 不在关键路径**）。

**上线后要看的三条查询**：

```sql
-- ① 本轮是否真的产生了排列（而不是只有 quiet）
SELECT created_at::date, outcome, count(*) FROM decision_trails GROUP BY 1,2 ORDER BY 1 DESC;

-- ② 每张 ticket 的腿是否齐全（"多个"是否真的落成两条腿）
SELECT t.id, t.symbol, t.state_before, t.state_after, t.legs, count(d.id) AS drafted
FROM tickets t LEFT JOIN drafts d ON d.ticket_id = t.id
GROUP BY 1,2,3,4,5 HAVING count(d.id) <> t.legs;

-- ③ 是否出现裸卖 call 告警态（应为 0）
SELECT * FROM symbol_phases WHERE state = 'NAKED_CALL';
```

---

## 4. 决策点

| # | 待决 | 选项 | 推荐 | 落地 |
| --- | --- | --- | --- | --- |
| **D8** | 开仓时段语义 | (a) 观察全天、草案仍限 Athena slot（3.2）(b) 完全放开时段 | **(a)** | **已落地 (a)。** 观察与草案拆开。`ATHENA_SLOT_SPLIT=off` 回到仅 Athena slot 全文 |
| **D9** | Long 可达方式 | (a) 逐标的设 `intent=accumulate` (b) 新增 `may_buy` 与 intent 正交（3.1） | **(b)** | **已落地 (b)，缺省与原稿不同。** 缺省是 `long` 且意图不是 `income`。显式 `may_buy` 覆盖。收入型默认仍不买 |
| **D26** | 验收使用的风控档 | (a) 继续用 `test` (b) 新增 `paper` 档（3.3 的数字）(c) 直接用 `standard` | **(b)** | **已落地 (b)。** 档可选。十项已在 `ZZTEST` 上保存并恢复。活账户仍是 `test` |
| **D27** | 验收工装 | (a) 只用合成 fixture (b) 只用 live shadow (c) 两者（fixture 先行） | **(c)** | **夹具已落地。** 多日影子对照仍是操作步骤 |
| **D28** | `observe_slots` 的取值域 | `always` 定义为 HKT 07:00–24:00 / 与 Athena slot 相同 / 24×7 | **HKT 07:00–24:00 且沿用周末 idle** | **已落地。** `always` 是工作日 07:00 至午夜。周末是 idle |
| **D36** | `min_iv_rank_new_short` 在 D4a 期间怎么办 | (a) 换轴到 `rv_pctl` 并改名 (b) 保留现状只改文档 (c) 临时置 0 | **✅ 已裁定 (a)** | **已落地 (a)，同卡未做。** 字段是 `min_rv_pctl_new_short`，读 `rv_pctl`，`paper` 档为 40。旋钮在现有风控表。Volatility reading 卡仍是步 9 |
| **D37** | 行权价带的口径 | (a) 固定 10% (b) 固定调小 (c) 按 `σ_ref × 0.5 × √T` 缩放 (d) 纯 delta 无护栏 | **✅ 已裁定 (c)** | **已随步 5 落地。** 不在本步 |
| **D38** | `iv_rv_multiple` 的取值与它算什么 | (a) 写死 (b) 进 Settings 三档 1.05/1.35/1.80 标"假设" (c) 全取 1.0 | **✅ 已裁定 (b)** | **已随步 5 落地。** 不在本步 |

---

## 5. 验收标准

### 5.1 主判据（主计划的 G1/G2/G3）

| # | 验收 | 判据 | 落地 |
| --- | --- | --- | --- |
| **G1** | 能开 | `fixture_june_dip` 上跑一轮 paper：`tickets` 1 行（`reason='dip_long'`）＋ `drafts` 中 `BUY` 与 `OPEN_CSP` 各 1 条、同 `ticket_id`、`leg_index` 0/1 | **已落地（夹具）。** `may_buy` 打开时是 `BUY` 加 `OPEN_CSP`。同一回调、收入型且未开 `may_buy` 时没有 `BUY` |
| **G2** | 能收口 | `fixture_intc_high` 上跑一轮：`[OPEN_CC]` 1 张，`call_strike ≥ positions.avg_price` | **已落地（夹具）。** 一张 `OPEN_CC`，行权价不低于成本 |
| **G3** | 不会失控 | 四个负面 fixture（无持股 / 无合法状态 / 无数据 / 回复不可解析）**各自**不产生任何可提交的裸卖单，且审计**分别**落在 `dropped` / `no_data` / `invalid` / `observed` 上 | **已落地（夹具）。** 裸 call 是 `dropped`，无 RV 样本是 `no_data`，坏回复重试一次后是 `invalid`，干跑是 `observed` 且不写草案 |

### 5.2 E1（配置可达性）

| # | 验收 | 判据 | 落地 |
| --- | --- | --- | --- |
| **A1** | 旧 payload 行为不变 | 用当前 `settings_versions` 最新版跑 `fixture_june_dip` → **不**产生 `BUY`（证明向后兼容，不是"顺手放开"） | **已落地（夹具）。** `fixture_june_dip` 在收入型、`may_buy` 为空时没有 `BUY` |
| **A2** | `may_buy` 生效 | 同一 fixture，只把该标的加 `{"may_buy": true}` → `BUY` 通过 flags 门，进入正常风控 | **已落地（夹具）。** 收入型加上 `may_buy=true` 后，同一张票是 `BUY` 加 `OPEN_CSP` |
| **A3** | 可达性自查 | 一条命令输出"每个标的 `BUY` 是否可达 ＋ 原因"，不存在"原因未知"的标的 | **已落地（函数）。** `buy_access` 对每种旗标组合都返回原因：`may_buy off`、`long off`、`income`、`allowed`。轮动不读 Purchase 提示词 |

### 5.3 E2（观察与许可）

| # | 验收 | 判据 | 落地 |
| --- | --- | --- | --- |
| **A4** | 五态可分 | 构造 5 个 fixture，`decision_trails.outcome` 落在 5 个**互不相同**的取值上；`observed` 与 `quiet` 不混淆 | **已落地（夹具）。** `dropped`、`no_data`、`invalid`、`observed`、`quiet` 各有一条。低百分位是 `quiet`，干跑是 `observed` |
| **A5** | 步 3 文档同步 | `03-audit-discernibility.md` 的四态表已更新为五态，且 D14 的 CHECK 约束含 `observed` | **已落地。** 产品说明在 `docs/athena/05-human-control.md`。CHECK 在迁移 `033`，含 `observed` |
| **A6** | 成本不升 | 同一日，开启 `observe_slots=always` 前后 `ai_calls` 总行数**不增加**（步 3＋步 6 的降本应覆盖扩窗） | **已落地（调用数）。** `run_observe` 写 Location 与 News，结果 `observed`。完整一轮再写 Narrator。多日行数对比是操作步骤 |

### 5.4 E3（`paper` 档与 D36 换轴）
 
| # | 验收 | 判据 | 落地 |
| --- | --- | --- | --- |
| **A7** | `paper` 档可选 | `/risk` 的档位下拉含 `paper`，选中并保存后 `settings_versions` 最新版里十个 `RiskLimits` 字段**逐项等于** §3.3 表值（十项全比，不是六项） | **已落地（ZZTEST）。** 保存路径认出 `paper`，十项相符，随后恢复该账户原先的活动行。没有保存活账户 |
| **A8** | 档位身份判定是十项 | 把 `max_orders_per_day` 从 6 改成 50 → 档位名**必须**从 `paper` 变为 `custom`（这是 `PROFILES` 六项身份判定的反例断言） | **已落地。** 十项含 margin 与 blackout。改每日单数为 50 后不再匹配任何档 |
| **A9** | **D36 换轴生效** | ① `min_iv_rank_new_short` 在仓库内**零命中**（含 Settings 模型、`RiskLimits`、prompt 模板、界面标签）；② `min_rv_pctl_new_short = 40` 读 `rv_pctl`；③ 构造一个 `rv_pctl = 39` 的标的 → 该门**必须**拒绝新开空头，而 `rv_pctl = 41` 必须通过（证明读的是新轴而非旧轴） | **已落地，零命中有例外。** 模型、风控、提示词、界面已无旧名。迁移 `040` 与计划文档仍写出旧名。39 为 `quiet`，41 可开出 |
| **A10** | `rv_pctl` 样本不足的行为 | 同 method 样本 < 20 时，该门的结果落在 `no_data`（步 3 的五态之一），**不得**返回"通过"，也不得与 `quiet` 混淆 | **已落地。** 摘要是 `rv_samples`。低于门槛才是 `quiet` / `rv_percentile` |

### 5.5 E4（文档一致）

| # | 验收 | 判据 | 落地 |
| --- | --- | --- | --- |
| **A11** | tracker 无漂移 | `grep -n "OPEN_CSP\|OPEN_CC" docs/athena/10-implementation-tracker.md` **不再命中**开仓声明；P4-18 已拆为 a/b 两行 | **已落地。** tracker 里这两个开仓名零命中。拆行是 `P4-18` 与 `P4-18b`，不是 `P4-18a` |
| **A12** | 策略文档同频 | `03-strategy-news-frontrun.md` 的冲突规则表述与步 2 的 I3 矩阵一致（逐项对照） | **已落地。** 覆盖不足、重复空 put、以及 short put 与 covered call 可以并存 |
| **A13** | 台账闭合 | `node scripts/check-changelog.mjs` → EXIT=0，且本次七个文档各有索引行 | **已落地。** 本步收口记在 `C-20261011-1`。机检通过 |

### 5.6 回滚演练（必做）

| # | 演练 | 判据 | 落地 |
| --- | --- | --- | --- |
| **R1** | 逐个开关关闭 | 7 个开关**逐一**关闭后，行为与改造前一致（用改造前的 fixture 断言），且**不需要**回滚任何迁移 | **按现有开关落地。** 没有 `ATHENA_PRICE_SINGLE_SOURCE`，没有补这个旗标 |
| **R2** | 全关 | 7 个开关全关 → G3 的四个负面用例仍全部通过（证明"关掉新功能"不会退化成不安全） | **已落地（现有六个）。** `ATHENA_STATE_MACHINE`、`ATHENA_MAY_BUY`、`ATHENA_SLOT_SPLIT`、`ATHENA_DRY_RUN`、`ATHENA_SCREENER`、`ATHENA_INDICATORS` 一起关掉后，一轮仍写审计 |
| **R3** | 开一半 | 只开步 1＋步 3（`PRICE` ＋ `INDICATORS`），其余关 → 系统仍能正常跑完一轮并留下审计（证明步骤间无硬耦合） | **已并入 R2。** 策略开关关掉后仍有审计。没有单独的价格旗标 |

---

## 6. 明确不做

- **不改 `privilege`**：仍是 `all_orders` ＋ 人工 Approve。ticket 只改变"批准的单位"，不改变"必须批准"。
- **不接实盘**：验收全部在 paper 命名空间（`simulated` 维度）进行。
- **不做回测框架**：fixture 是**单序列断言工具**，不是历史回测引擎。
- **不改 `test` 档的数字**（只新增 `paper` 档）。
- **不动 fanit 同步、云端部署、Tailscale、Vercel**（tracker 已列为 deferred）。
- **不自动调参**：所有阈值是手写常量，调参是人的动作。

---

## 7. 风险与回滚

| 风险 | 说明 | 对策 |
| --- | --- | --- |
| 扩窗放大账单 | `observe_slots=always` 让轮数从 95 增到 ~204 | A6 硬性要求总调用数不升；否则先回退 `observe_slots=athena` |
| `paper` 档数字过严 | 验收跑不出任何 ticket，误判为"功能没做出来" | 先跑 `fixture`（与档位无关的具体断言），再跑 shadow；若 fixture 通过而 shadow 全空，说明档位过严而非功能缺失 |
| 向后兼容被破坏 | `may_buy` 若默认 `True` 会立刻放开 24 个标的的买股 | A1 是硬门：旧 payload 必须**仍然不可买** |
| 五态扩枚举引发审计断裂 | 历史 `decision_trails` 无 `observed` | CHECK 约束用 `NOT VALID` 建，再 `VALIDATE`；或直接接受历史行为新枚举的子集 |
| 回滚被迁移阻塞 | 六个迁移中有五个是加列 | R1/R2/R3 的演练就是"不靠迁移回滚"的证明；唯一不可逆的 `027` 删的是假数据 |
| 文档与代码再次漂移 | 本步修的三处正是上一轮的成果 | 把漂移检查写成 A7/A8 的可执行断言（grep），纳入每一步的验收习惯 |
