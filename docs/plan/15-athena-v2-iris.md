# 15 — Athena v2 / Iris（信号、策略与行动的可观测架构）

**状态**：Planning / 待裁定；本册只合并现状证据、目标契约与实施顺序，不授权实现。
**证据截点**：2026-11-24，代码基线 `56739ce`；数据库只读快照见 §2.3。
> **状态推进（2026-11-25）**：§17 授权的 **V2-P1 静态 Iris ＋ Orchestration 独立双列已交付并验证**，交付记录见 §18；V2-P2–P7 与 V2-Q1–Q15 仍待裁定。上行「不授权实现」原句保留为 `C-20261124-11` 规划批次的历史状态，不改写。
> **状态推进（2026-11-25，第二笔）**：**V2-Q Round A 已裁定 8 条**（Q1/Q2/Q3/Q4/Q5/Q6/Q13/Q15），记录见 §19；Q7–Q12、Q14 共 7 条仍待裁定。上行「V2-Q1–Q15 仍待裁定」是第一笔当时的状态，保留不改写。
> **状态推进（2026-11-25，第三笔）**：操作者确认 §19 后，**V2-P2 / V2-P3 落地前契约已写入 §20**（`C-20261125-3`）。§20 本身零代码、零迁移、不预占迁移号，仍需操作者批准后才进入实现批次；Q7–Q12 / Q14 仍未裁定。
> **状态推进（2026-11-25，第四笔）**：操作者逐项确认 §20.8 五条实现口径后，**§20 获批**，记录见 §21（`C-20261125-4`）。其中第 1 条由 `America/New_York` **显式窄修订为 UTC 日界**（Q5 identity 公式本身不变）；§6.2 / §19 / §20 历史 verbatim 行一律保留，只追加状态推进。**§20 获批 ≠ 授权实现**：P2 / P3 仍须各自独立实现批次、独立 commit / rollback；Q7–Q12 / Q14 仍未裁定。
**编号纪律**：本册的 `V2-I*`、`V2-S*`、`V2-P*`、`V2-Q*` 都是册内规划标识，**不是全局 D 决策，不占用迁移号，不改变 D1–D111 / 112 节点的裁定状态**。

> **一句话目标**：让 Athena 的每条策略行动都能被看懂——什么输入形成了什么因子，哪个策略据此产生了哪张 draft，谁在何时批准，订单最后到了哪里。
>
> **Iris 是 Athena 的眼睛，不是第二个交易大脑。** 它解释与观察系统，不另造信号、草案、审批或订单状态。

---

## 0. 本册回答什么

1. 当前引擎实际怎样从行情、新闻、IV、VIX 与持仓走到 draft、审批和 IB 执行。
2. 为什么当前的 NFR、agents、Advisor、Portfolio Analysis、Drafts 与 Decisions 看起来互相重叠。
3. Athena v2 怎样划定 Strategy 1、Strategy 2、Strategy 3，而不虚构“已经有三条策略”。
4. Iris 怎样以真实事件驱动架构图和动画，而不是永久循环的装饰性流量。
5. 新闻跨标的重复怎样修正，同时保留现有 NFR 行为和回滚能力。
6. `docs/strategy_sma20_50.md` 还缺哪些可执行定义，怎样先 shadow、再进入统一 draft / approval。
7. 每一阶段以什么可验证退出条件推进，以及明确不做什么。

---

## 1. 不可回退的目标边界（待逐条裁定）

以下为本册推荐的目标边界；在用户确认前均为 **proposed**，不是现行裁定。

| 规划 ID | 推荐边界 | 理由 |
| --- | --- | --- |
| **V2-I1** | **一个行动账本**：所有策略建议统一进入 `tickets / drafts / approvals / orders / fills`，Iris 只投影它们 | 禁止再出现 Advisor 一套行动、Drafts 另一套行动、Iris 第三套行动 |
| **V2-I2** | **策略与呈现分离**：Iris 的事件、动画、筛选和节点状态绝不参与交易判定 | 页面断流、浏览器隐藏或动画失败不得影响引擎 |
| **V2-I3** | **无事件即静止**：只有真实生产者边界发生时才高亮节点或连接；不从自由文本日志猜流量 | 避免把“看起来很活跃”误认为系统真的在工作 |
| **V2-I4** | **新策略先 shadow**：Strategy 3 必须先产出可重放信号与反例，再允许创建 pending drafts | 原文胜率与盈亏比是待验证假设，不是上线事实 |
| **V2-I5** | **标准策略路径必须人工批准**：open / trim / close / roll 都复用 `decide()` 与执行 gate | Athena v2 不借 Iris 或新策略绕开 `all_orders` |
| **V2-I6** | **Guardian 保持旁路监督**：Guardian 观察整个组合与风险，不是 Strategy 4，不直接产生交易动作 | 延续 D58–D111 已裁定的 Guardian 边界 |
| **V2-I7** | **日线与周线同一真源**：Strategy 3 的周线由完成的 `ib-daily` 聚合，不另造周数据源 | 避免价格口径再次分叉 |
| **V2-I8** | **缺失不是否定**：`no_data / filtered / no_signal / quiet / invalid / blocked` 必须在 Iris 上可区分 | 空白不能同时表示“没有新闻”“模型坏了”和“策略没信号” |
| **V2-I9** | **多账户显式作用域**：市场输入可共享，Advisor、策略运行、draft、审批与执行必须带 `account_id` | 共享行情不等于共享交易账本 |
| **V2-I10** | **操作员直下单路径显式可见**：Universe manual trade 与 Liquidate All 不得伪装成“经过策略审批” | 当前代码确有标准 draft 之外的直接下单旁路，见 §2.6 |

---

## 2. 当前实现：证据地图

### 2.1 实际主链，而不是设想主链

```text
IB daily history / delayed prints / VIX indexes / Yahoo & Finviz / calendars / option plans
        ↓
market_bars / news_events / calendar_marks / iv_marks / regime_labels
        ↓
pricing + indicators + price tags + news tags + classifiers
        ↓
symbol_phases（NFR 阶段、tags、数值上下文；tactic 固定 HOLD）
        ↓
┌───────────────────────────────┬────────────────────────────────┐
│ run_signaled                  │ run_advisor + manage_book      │
│ Location + News → tag set     │ held positions + guidance      │
│ → wheel.decide_opening        │ → advisor_signals              │
│ → Narrator open/strike gate   │ → manage ticket + stage_draft  │
│ → ticket + stage_draft        │ → CLOSE / TRIM drafts          │
│ → apply_ticket_block          │ + deterministic option CLOSE   │
└───────────────────────────────┴────────────────────────────────┘
        ↓
pending / blocked drafts → apply_cycle_budget（两者共同后续边界）
        ↓
pending drafts → Telegram / console / one-time control link
        ↓ decide(approve)
approved drafts + queued orders
        ↓ later Athena session + live_block_reason
submitted orders → IB → order_watch / fills / positions
```

证据：

- 主 cycle 与账户顺序：`apps/orchestrator/src/athena_orchestrator/paper.py:50-117`。
- 每账户 Advisor → Manage → opening：`paper.py:185-205`。
- 唯一生产 opening 路径：`packages/strategy-nfr/src/athena_strategy/wheel.py:131-133,455-490`。
- 标准 draft staging：`packages/strategy-nfr/src/athena_strategy/drafts.py:798-929`。
- 审批与 queued order：`drafts.py:1744-1924`。
- IB 提交 gate：`apps/exec-ib/src/athena_exec/gate.py:13-35`；`session.py:500-587`。

### 2.2 “52 周”到底是日线还是周线

**当前是日线。** `year_view()` 读取最近 **252 个已存储的日 session**，不是 52 根周线：

- 请求：`reqHistoricalData(..., "1 day", "TRADES", useRTH=True, ...)`，见 `apps/exec-ib/src/athena_exec/broker.py:332-384`。
- 视图：`daily_bars(conn, symbol, 252)`，见 `packages/marketdata/src/athena_marketdata/pricing.py:73-94`。
- 存储：`market_bars(source='ib-daily')`，唯一键 `(symbol, ts, source)`，见 `packages/store/src/athena_store/migrations/002_paper.sql:1-12`。
- 启动 backfill 最多拉约两年日线；52 周只是读取窗口，见 `apps/orchestrator/src/athena_orchestrator/backfill.py:130-166`。
- 不足 252 根时使用现有全部历史，并标 `history_short`，不是补出虚假交易日。

**已发现的完整性边界**：当前“完整”主要看是否达到 252 行，或短历史是否接近 IB head date；它不证明内部逐交易日无缺口。单次 catch-up 上限 60 天，长缺口可能留下更早的内部空洞，见 `backfill.py:298-332`。因此 Iris 必须显示 **bar count + earliest/latest + known gap state**，不能只显示一个绿色“52W ready”。

### 2.3 新闻重复：代码与真实数据库都已确认

当前 fingerprint：

```python
sha256(f"{symbol.upper()}|{title.lower()}")[:24]
```

证据：`packages/strategy-nfr/src/athena_strategy/news.py:499-517,530-552`；schema 主键为 `news_events.fingerprint`，且每行只有一个 `symbol`，见 `packages/store/src/athena_store/migrations/006_news_events.sql:1-12`。

因此：

- 同一文章对同一 symbol、同一标题可去重。
- **同一文章关联多个 symbol 时必然各存一行。**
- URL 不属于 identity；没有独立 article 表，也没有 article ↔ symbol 关联表。
- Yahoo / Finviz 本来就逐 symbol 抓取，见 `news.py:70-104`。
- 手工 URL intake 只让模型选一个 watched symbol，并非完整多标的关联，见 `news.py:129-159`。

2026-11-24 对本地 `athena-pg` 执行 `BEGIN READ ONLY` 查询并 `ROLLBACK`，实测：

| 指标 | 实测值 |
| --- | ---: |
| `news_events` 总行数 | 1,757 |
| `lower(title)` 不同标题数 | 1,350 |
| 跨 symbol 标题组 | 269 |
| 这些组包含的行数 | 676 |
| 单一标题最大 symbol 数 | 7 |
| 按“同日同标题应只有文章本体一份”口径可见的重复标题行 | 407 |

示例：同一标题 `stocks give back gains as anthropic dips below ipo price` 分别存到 `AAPL, AMD, AMZN, AVGO, GOOGL, META, NVDA` 七行。该统计是**标题级证据**，不是语义级文章聚类准确率声明。

### 2.4 NFR、news、IV 与 drafts 的真实关系

| 层 | 当前职责 | 当前不做什么 |
| --- | --- | --- |
| News intake | 逐 symbol 拉标题，关键词 sentiment，可用模型 enrich；形成 `NEWS_WASH` 与 `news_score` | 不直接下单 |
| NFR `symbol_phases` | 聚合 tags / indicators / calendar，计算 P1–P5 与 wheel state | `tactic` 固定 `HOLD`，本身不创建 draft |
| `/phase` 看板 | 同屏展示 phase、IVR、负面新闻和 pending draft 链接 | 同屏不表示 phase 是 opening gate |
| wheel | 读取 tag set、位置、持仓、option candidates，形成 opening ticket | 不使用 NFR phase 作为确定性开仓条件 |
| Advisor / Manage | 对持仓形成 signal 和 CLOSE / TRIM draft | 不负责新仓 opening |

关键误区：`news_score` 会进入 `symbol_phases.indicators` 和模型上下文，但当前没有确定性 wheel / phase 消费者；`NEWS_WASH + RV_HIGH` 才会进入 `signaled_strangle` candidate。

### 2.5 当前确实存在哪些 score

| 名称 | 当前真实用途 | 不应误读为 |
| --- | --- | --- |
| `news_score` | 时间加权新闻情绪，存入 indicators、进入 audit/context | wheel 的综合开仓评分 |
| `_OPENING_RANK` | 当日 pending opening 的替换优先级 | 收益预测或信号质量分 |
| Advisor fallback `score` | `0.35 guidance + 0.30 forecast + 0.20 near-high + 0.15 premium captured`，与阈值比较产生 hold / trim / take_profit | 全引擎统一评分 |
| `rv_pctl` | 当前 RV20 在已积累 `rv_marks` 中的分位；新 short leg 硬门 | measured implied volatility |
| premium-ratio IVR | `(put_limit + call_limit) / underlying` 的历史 rank，主要展示 | 真 IV / true IVR |
| Midday Regime | 只读 observation，真实 label 可注入 LLM context | 确定性交易 gate |
| Prediction / forecast | shadow / advisory；Advisor 可读 `dir_conf` | 自动 size / approve |

证据：`packages/strategy-nfr/src/athena_strategy/indicators.py:145-240,262-296`；`wheel.py:33-40,792-845`；`advisor.py:167-210`；`iv.py:36-38,53-102`；`agents.py:444-477`。

**结论**：用户感觉“不清楚 scoring 怎样参与策略”是有代码依据的。当前不是一个统一 score → strategy 的架构，而是 **确定性 predicates + tags + LLM context + 局部 Advisor score**。Iris 应该如实画出这些不同语义，不能把所有数值都装进一个总分。

### 2.6 Portfolio Advisor、Drafts、Decisions 与执行边界

- Advisor 对已有持仓每天每 symbol 写一条 `advisor_signals`，唯一键 `(account_id, symbol, day)`，见 `advisor.py:213-251`。
- `manage_book()` 把 Advisor 的 close / take_profit / trim 与普通 short-option profit-take 统一转为 drafts；ManageExplainer 只能改 reason / counsel，不能改 tactic、size、strike 或 limit。
- `/portfolio-analysis` 只是读取 signals、positions、pending drafts 与 Run C；打开页面不会运行 Advisor。
- `/drafts` 是行动队列；`/audit` 的 Decisions 是 `ai_calls + decision_trails` 历史，不是另一个审批队列。

**当前重复感来自页面职责，而不是两套执行账本**：Portfolio Analysis 把“解释 signal”与“行动列表”又展示了一遍。v2 应保留 Advisor signal 作为 Strategy 2 的证据，但行动只链接到同一 draft。

**标准路径之外的两个现行旁路必须保留在架构事实里**：

1. Universe manual trade：确认后直接调用 IB，再写 synthetic `MANUAL approved draft + approval + submitted order`；不是下单前审批，见 `apps/orchestrator/src/athena_orchestrator/tasks.py:340-484`。
2. Liquidate All：paper mode 下经确认直接发送 closing orders，不等待 Approve，见 `tasks.py:487-500,574-631`。

本册不擅自改变这两条既有操作员路径；Iris 必须把它们画成 **operator-initiated direct paths**，不能并入“策略已批准后执行”的绿色链。

### 2.7 当前 UI 为什么无法解释系统

- `/orchestration` 的 SVG 是静态图；没有 JS 根据 `data-node-id` / `data-edge-id` 更新它。
- `/api/cycle` 只有 `Session / Bars / News / DSH / IV / Phases / Drafts / Backup` 粗粒度阶段。
- 价格与订单有 SSE；news、factor、strategy、approval 没有统一的结构化实时事件。
- `/api/logs` 是有界日志流，不是 durable event history，不能解析 message 来伪造 connector activity。
- `/portfolio-analysis` 当前左栏 Advisor report、右栏 Advisor actions，下方 Run C；没有输入 → 因子 → 策略 → 行动图。
- 最接近用户所说 income breakdown 视觉的是 Cockpit 原生 SVG net-cash bars；可借鉴暗色 chart frame、克制的状态色与 mono 标注，不应复用其数据语义。

---

## 3. Athena v2 的策略词汇

### 3.1 不再把 NFR 与 agents 错称两条策略

| 规划 ID | 对外名称 | 当前基础 | 输出 | 边界 |
| --- | --- | --- | --- | --- |
| **V2-S1** | **NFR Wheel · Opening** | 当前唯一生产 opening 路径；NFR phase + Location / News + deterministic wheel + Narrator | `BUY / OPEN_CSP / OPEN_CC` 及现有 `OPEN_STRANGLE` 兼容表示的 drafts | NFR 是上下文；agents 是感知、否决和解释，不另算 Strategy 2 |
| **V2-S2** | **Portfolio Advisor · Management** | 当前 Advisor + deterministic Manage | `HOLD` signal；达到条件时 `TRIM / CLOSE` drafts | 只管理已有持仓，不创建 opening |
| **V2-S3** | **SMA Hill Retest · Bearish** | `docs/strategy_sma20_50.md`，目前未实现 | shadow signal；裁定后才允许 bearish opening / exit drafts | 不复用 `WEEK_DIP` 名称，不藏进 NFR tactic，不自动交易 |

**Strategy 1 与 Strategy 2 是两类决策职责，不是当前已有两条 opening engines。** Strategy 3 是新产品边界提案；未裁定前，00 册 §9“单一 NFR 策略产品”的现行约束仍有效。

### 3.2 Guardian、Prediction、Trade prompt 的位置

- **Guardian**：图上方或侧边的组合监督带，观察所有策略与操作员路径；不放入 Strategy 节点队列。
- **Prediction**：未完成训练时显示 `shadow / not ready`；只能连到标有 advisory 的因子或 Advisor context，不连到 approval。
- **Trade prompt**：只属于 Universe Auto Set Price，不是 cycle agent，也不因 UI 挪列而变成执行节点。
- **Decisions**：所有 agent / factor / strategy 的历史审计入口；Iris 节点详情可深链 `/audit`，不复制其完整表格。

---

## 4. Iris 页面契约

### 4.1 信息架构

```text
┌───────────────────────────────────────────────────────────────────────────┐
│ Iris · selected account · data freshness · cycle state · Guardian tier   │
├───────────────────────────────────────────────────────────────────────────┤
│ INPUTS             FACTORS              STRATEGIES          ACTIONS      │
│                                                                   ┌─────┐ │
│ News ───────────→ sentiment / NEWS_WASH                        ┌─────┐ │
│ Calendar ───────→ FED_EVENT / EARNINGS_NEAR                    │     │ │
│ Daily bars ─────→ 52W position / RV20 / RV pctl                │same │ │
│ Daily bars ─────→ completed-week SMA20 / SMA50 factors         │IDs /│ │
│ Delayed ticks ──→ current price input                          │links│ │
│ Option plans ───→ Premium ratio rank                           │     │ │
│ VIX / VIX9D ────→ regime context                               └─────┘ │
│ Positions ──────→ premium captured / Advisor score ──→ V2-S2 Advisor    │
│ Guidance / forecast (advisory) ────────────────────────→ V2-S2 Advisor  │
│ 52W / RV / NEWS_WASH / price & event tags ─────────────→ V2-S1 Wheel    │
│ Completed-week SMA factors ────────────────────────────→ V2-S3 SMA      │
│ V2-S1 / V2-S2 / V2-S3（裁定后）─────────────────────────→ same drafts   │
│                                                                           │
│ Guardian side band: portfolio / position conformance, alerts, freshness  │
├───────────────────────────────────────────────────────────────────────────┤
│ draft: pending → approved | rejected | expired | blocked；amended 仍 pending │
│ approved draft → order queued | blocked | filled（仅 simulated 路径）       │
│ queued order → submitted | blocked | cancelled                           │
│ submitted order → filled | cancelled；partial = submitted + 已成交/余量   │
│ operator direct paths shown separately: Manual / Liquidate / External IB  │
└───────────────────────────────────────────────────────────────────────────┘
```

上图是关联对象的投影，不是把 draft 与 order 合并成一张状态表：approved draft 仍保持 approved，后续 queued / submitted / filled 属于 order；partial fill 由 order 与多笔 fills 聚合。rejected / expired / blocked pending draft 永远不得排队。

桌面推荐 **图区 3fr + draft rail 2fr**；窄屏改为图区在上、统一 draft 列表在下。图区内部保持 Inputs → Factors → Strategies → Actions 的固定阅读方向，不随数据缺失随机重排。

### 4.2 页面落点与旧页面收敛

1. 将当前 `/portfolio-analysis` 的产品名称改为 **Iris**；推荐新增 canonical `/iris`，旧路径保留兼容别名，避免既有链接和 D70 的历史引用瞬间失效。
2. Advisor report 不再是一张独立“行动表”；它成为选中 V2-S2 节点时的 evidence inspector。
3. 右侧行动列表直接复用 `/drafts` 查询与状态，不建 `iris_actions`。
4. 每张行动卡显示 `strategy_id / account / symbol / tactic / status / reason / created_at`，点击打开原有 draft 详情与审批入口。
5. Decisions 保留为历史检索；Iris 只提供按 `run_id / draft_id / symbol` 的深链。
6. Run C 保留为只读 regime evidence，可在 VIX / regime 节点查看，不变成 gate 或第四个策略。

### 4.3 节点与边的显示语义

| 状态 | 视觉 | 必须能回答的问题 |
| --- | --- | --- |
| `idle` | 静态细边 | 这个连接在架构上存在，但当前没有事件 |
| `running` | 节点边框高亮；有限一次 connector pulse | 哪个 run / symbol 正在执行 |
| `completed` | 短暂完成色，随后回 idle | 何时完成、产生了什么证据 |
| `filtered / quiet` | 灰蓝标记 | 有输入但无行动的具体原因 |
| `blocked` | 紫色风险标记 | 哪个 Risk Guard / budget 拒绝 |
| `failed / invalid` | 红色错误标记 | 是 provider / parse / store / execution 哪类失败 |
| `stale / no_data` | 虚线与缺失原因 | 最后成功时间、缺哪个输入 |
| `shadow` | 琥珀虚线 | 只观察，不可能审批或执行 |

**禁止**：全图永久流动、价格 tick 触发所有策略边、一个 `Drafts` cycle step 触发所有 agent 边、用随机粒子表示“AI 正在思考”。

### 4.4 交互与可追溯性

- 点击 input：显示来源、最后事件、覆盖 symbol、freshness 与缺失原因。
- 点击 factor：显示公式版本、输入窗口、当前 snapshot、生成 tags、下游真实消费者；只有存在不可变 run evidence 时才显示“draft 当时数值”，否则明确标 `historical_value_unknown`，不得用已被覆盖的最新 `symbol_phases.indicators` 冒充历史值。
- 点击 strategy：显示 run 状态、候选数、filtered / quiet / invalid / drafted 数量和证据。
- 点击 edge：只显示该 edge 最近真实事件，不显示伪造的“吞吐量”。
- 点击 action：定位同一个 draft；批准后图上的 approval / order 节点由真实状态更新。
- 账户切换只改变账户作用域；共享市场节点显示“shared”，账户节点与 draft rail 必须重新查询。
- 所有时间显示明确时区；graph snapshot 带 `as_of`，断流时显示 stale 而不是继续动画。

### 4.5 视觉与可访问性

- 复用 `design/14-strategy/chain.svg` 的稳定 `data-node-id / data-edge-id / data-source / data-target` 寻址模式。
- 复用 console tokens、暗色 `.chart-box`、细线、mono 数值与有限状态色；不引入重型图表库作为前置条件。
- connector 动画为一次性短时反馈；同一边事件排队与合并必须有上限。
- `prefers-reduced-motion` 下只改颜色 / 边框；页面隐藏、断流或事件过期时清理 active 状态。
- 颜色不能是唯一编码；所有状态同时有文字或图形标记。
- SVG 节点可键盘聚焦，读屏名称包含节点名、状态与最后时间。

---

## 5. 真实事件与动画数据契约

### 5.1 当前可立即使用与必须补齐的部分

| 事件 | 当前可用来源 | Iris 用法 |
| --- | --- | --- |
| 价格变化 | `/api/pulse/ticks`、 `/api/v1/events/prices` | 只驱动对应 Price input |
| cycle 粗粒度阶段 | `/api/cycle` 每秒轮询 | 显示 News / Phases / Drafts 等阶段运行状态 |
| 订单变化 | `/api/orders/events` + `order_events` | 驱动 Orders / Execution 节点 |
| news persisted | 当前只有 DB 写入与日志 | 需要结构化事件或 cursor read model |
| factor updated | 当前只有 `symbol_phases.indicators` / `rv_marks` 写入 | 需要在实际计算边界发事件 |
| strategy started/completed | 当前只有 decision trails / ai_calls / 日志 | 需要 run identity 与结构化结果 |
| draft staged / changed | durable draft，无专用 SSE | domain commit 后幂等 emit + snapshot reconciler；覆盖 blocked / expired / amended / replaced |
| approval decided | drafts / approvals / orders，无专用 SSE | 需要规范化事件 |

### 5.2 推荐：窄领域事件，不建“万能总线”

推荐新增工作名 **`strategy_events`** 的 append-only observation read model；最终表名与迁移号在实现前裁定。本册不预占 `056` 或任何后续号。

建议 envelope：

| 字段 | 含义 |
| --- | --- |
| `event_seq` | 单调序号，用于 SSE reconnect 与增量追赶 |
| `event_id` | 全局唯一、幂等 |
| `occurred_at` | UTC 时间 |
| `account_id` | 账户事件必填；共享市场事件可空 |
| `run_id / cycle_id` | 把 input → factor → strategy → draft 串成同一次追踪 |
| `symbol` | 标的级事件填写；组合级可空 |
| `strategy_id` | `nfr_wheel / portfolio_advisor / sma_hill_retest`；非策略事件可空 |
| `node_id / edge_id` | 必须来自版本化 graph registry，不接受任意前端文本 |
| `kind` | `input_observed / factor_updated / strategy_started / strategy_completed / draft_staged / draft_changed / approval_decided / order_changed` |
| `status` | `started / completed / filtered / quiet / invalid / blocked / failed`；domain 的 expired / amended / replaced / cancelled 等由 `draft_changed` 或 `order_changed` 的结构化状态字段表达 |
| `subject_type / subject_id` | 关联 `news / phase / decision / draft / approval / order`，不复制其状态 |
| `payload` | allowlist 的小型数值证据；`draft_changed` 至少含 `from_status / to_status / reason_code`，amend 另含版本或变更摘要；不存 token、完整 prompt 或大段新闻正文 |

**事务与失败规则（推荐，需 V2-Q15 裁定）**：domain write 是唯一真源，Iris event 写失败不得回滚或阻断持仓、风险、draft、approval、order 等 domain commit。生产者在真实 domain commit 边界后做幂等 observation emit；周期性 reconciler 从持久化 domain rows 补齐可推导的缺失事件，无法补出的区间明确显示 `observation_gap`。`event_id` 由 domain subject + transition + version 确定性生成，使重试与 replay 幂等。若将来选择 transactional outbox，也必须另行裁定可用性取舍，不能在本册里暗改 V2-I2。

### 5.3 读取与重连

- `GET /api/iris/graph`：版本化静态 graph + 当前 snapshot。
- `GET /api/iris/events?after=<event_seq>`：SSE 增量流，带 heartbeat。
- 浏览器保存最后 `event_seq`，重连先补有界历史，再恢复 live。
- 超过 replay 窗口时返回 resync marker，前端重新拉 snapshot，不静默丢事件。
- 不解析 `/api/logs` message 生成 edge；不把 queue drop 当作 domain failure。
- graph registry 改结构必须 bump `graph_version`，避免前端把事件投到错误节点。

---

## 6. 新闻去重目标设计

### 6.1 目标模型

```text
news_articles（一篇文章本体）
  article_id / identity_version / identity_hash / normalized_title
  canonical_title / published_day / first_seen_at / last_seen_at
        1
        │
        N
news_article_symbols（文章与标的的 canonical 关联及当前可变状态）
  association_id / article_id / symbol
  source / url / event_type / sentiment / dismissed / ts
  UNIQUE (article_id, symbol)

news_legacy_event_map（逐 legacy 行的不可变映射与 dual-write 状态）
  legacy_fingerprint / article_id / association_id
  legacy_ts / legacy_write_status
  PRIMARY KEY (legacy_fingerprint, article_id)
```

文章本体只存一次；每个 article + symbol 只有一条 canonical association。legacy map 独立保存每个旧 fingerprint 到 association 的映射，**不把 `legacy_fingerprint` 塞进 association 主键或要求其唯一**，因此未来同 symbol 同标题的不同日 article 不会被旧 fingerprint 的冲突语义卡死。NFR 在 canonical read 后按 association 计算；legacy map 只用于迁移、兼容与回滚，不是第二个新闻真源。

### 6.2 identity 推荐

推荐 v1 使用 **published day + 规范化标题** 作为主 identity，URL 作为证据与展示字段：

```text
normalized_title = title.lower()（v1 故意沿用 legacy 归一化，不新增 NFKC / 空白折叠碰撞）
effective_ts = provider-published timestamp when actually supplied;
               otherwise the stored ingestion timestamp, marked as fallback
published_day = effective_ts converted to America/New_York calendar day
article_key = sha256("v1|" + published_day + "|" + normalized_title)
```

> **窄修订（2026-11-25，§21，C-20261125-4）**：上行 `published_day = effective_ts converted to America/New_York calendar day` 经操作者逐项裁定改为 **`published_day = effective_ts converted to UTC calendar day`**（实现时用 `ts AT TIME ZONE 'UTC'` 取日期，避免 DB session `TimeZone` 漂移）。原行保留为 `C-20261124-11` 规划批次的历史 verbatim，不改写；下方「跨 ET 日」相应读作「跨 UTC 日」。Q5 裁定的 identity 公式其余部分（published day + normalized title、`title.lower()`、URL 只作证据、symbol 移出 identity、不做语义合并、`identity_version`）全部不变。实现时**不新增** `provider_tz` / `published_local_day` 等时区或证据列，只在解析入口加一道「tz-aware → `.astimezone(UTC)`」校验。

- 每条 association 保存 `effective_ts / day_precision = provider | ingest_fallback / original_ts`；legacy 没有真实发布时间的行不得伪装成 provider 发布时间。
- 同一 article 的多个 association 时间不一致时，canonical 展示时间取确定性最小 `effective_ts` 并保留全部原值；跨 ET 日的同标题不自动合并，进入 collision report。
- symbol 必须移出 article identity。
- 不用 URL 作为唯一主键：同一文章经 Yahoo / Finviz 或不同 symbol 页面可能有不同 URL。
- 不做“AI 主题相似所以合并”；同主题不同文章必须保留为不同 article。
- 标题改写、跨日发布与转载不自动语义合并；先保证可解释、可重放，再评估额外匹配层。
- 同日同标题但 URL 明显不同的行进入 collision report；identity v1 不声称能识别这种误合并，N3 前必须抽样审查并记录接受或修订。
- 所有 identity 规则带 `identity_version`，将来修订可重建 mapping，而不静默改写历史。

### 6.3 分阶段迁移，避免双真源

| 阶段 | 行为 | 回滚 |
| --- | --- | --- |
| N1 additive | 新表 + 幂等 backfill + mapping；旧 writer / reader 不动 | 删新表即可，旧数据不动 |
| N2 same-transaction dual write | canonical 写入不依赖 legacy insert 是否成功；每次初始 insert 分别在同一 transaction 写 canonical 与 legacy，并记录 `legacy_write_status=inserted/conflict_existing`。`dsh.py` sentiment、`dismiss_event()` 及所有后续可变字段 writer 同样在各自 transaction 双写并持续对账 | 切回旧 reader；legacy 保留旧行为，canonical rows 不删除 |
| N3 canonical read | NFR / News / Iris 读 canonical；legacy 停止新增前保留只读对照 | 切回 legacy reader |
| N4 legacy retirement | 只有长期对账为零且另行裁定后才冻结或删除旧表 | 本册不预设删除 |

backfill 必须保留每个旧 `fingerprint` 的 legacy map，保证 dismiss、draft reason、历史深链不会失锚。上线前用真实库 copy 或事务内 dry-run 验证：① 初始 backfill 的 map 数与 legacy 行数一致，每个 legacy row 恰好映射一次；② association 数等于不同 `(article_id, symbol)` 数，且无 orphan；③ article 数只按已登记 identity 规则减少；④ 初始 insert、DSH enrich、dismiss 后的 source / url / dismissed / sentiment / event_type 无未解释差异；⑤ 在一切新 writer 暂停的冻结数据集上，逐 symbol `NEWS_WASH` 完全一致、`news_score` 使用同一 Decimal 路径重算一致；⑥ ET 日界、provider 时间缺失、同 symbol 同标题跨日、legacy conflict、同日同标题不同 URL 与手工 URL 均有 fixture 和 collision report。

N2 遇到 legacy `ON CONFLICT DO NOTHING` 时，canonical 仍按自身 identity 幂等写入，map 记录 `conflict_existing`；不得因为旧 fingerprint 冲突而丢掉新 article。dismiss 在兼容入口必须解析到明确的 `association_id`，若一个 fingerprint 映射多个 association 且无法确定目标则拒绝操作并要求新 UI 传 association id，禁止一次 dismiss 全部。N3 前必须验证 canonical dismiss 后切回 legacy 的逐 symbol 行为一致；运行期间新产生的、legacy 因旧 identity 无法表达的事件差异要单独登记为预期差异，不能混入“对账通过”。任何未登记差异都阻止 N3。

---

## 7. 数据与因子层的 v2 收口

### 7.1 日线质量先于新策略

Iris 的 52W 节点至少展示：

- `source=ib-daily`、252-session window、实际 count；
- earliest / latest session；
- `history_short`；
- latest catch-up 状态；
- 已检测的内部缺口数量与首末日期；未实现 gap detector 前显示 `unknown`，不能显示 `complete`。

Strategy 3 shadow 前必须增加可执行的完整性检查。是否使用交易所日历、如何处理 halt 与 IPO，由 V2-Q 决策确定；不能用“252 行”替代“连续完整”。

### 7.2 周线构造

推荐从同一 `ib-daily` 序列按 `America/New_York` 聚合 ISO 周：

- open = 当周首个完成日 open；high / low = 周内极值；close = 最后完成日 close；volume = 周内求和。
- 假日短周保留；当前未完成周不参与确认信号。
- SMA20 / SMA50 使用完成周 close 的算术均值。
- 数据不足必须输出 `no_data`，不能输出 `no_signal`。
- 复权、公司行动与缺周处理见待决项；裁定前不宣称可直接回测。

### 7.3 IV、VIX 与 Prediction 的诚实标签

- premium-ratio IVR 在 Iris 中命名 **Premium ratio rank**，不得显示为 measured IV。
- `estimated_iv() = None` 与 `chain_marks` 无生产 reader 的现状必须可见。
- VIX / VIX9D / VIX3M / VIX1Y 是 data-only inputs，永不进入 tradable watchlist。
- Run C regime 是只读上下文；Midday Regime 与 `REGIME_RISK_OFF` 是不同节点。
- Prediction 未就绪前只显示 shadow / unavailable；不得为了图形完整而虚构 prediction factor。

---

## 8. Strategy 3 — SMA Hill Retest 契约草案

以下来自 `docs/strategy_sma20_50.md`；原文绩效数字均视为待验证假设。

### 8.1 已明确的策略意图

- 类型：趋势末端 bearish / 均值回归 / 周线。
- 结构：SMA20 在 SMA50 上方运行约 6–10 周形成“hill”，随后斜率衰减，价格回踩或跌破 SMA20，但尚未死叉。
- 入场：信号周完成后，次周 regular session 开盘至少 30 分钟后再评估；不追盘中急跌。
- 工具：short stock / long put / put spread，原文优先 put spread。
- 风险：单笔风险 ≤ 1% NAV；连续 3 次止损后当月停止新开。
- 止盈：跌破 SMA50 后，到其下方 3% 平 50%，下方 5% 平余仓。
- 失效：周收盘高于 SMA20 3%，或 SMA20 重新上拐且价格站回 SMA20。
- 过滤：SPX 强势、VIX 低波、放量回踩、财报前 5 天、流动性不足。

### 8.2 shadow 计算输出

每根完成周对每个候选 symbol 输出一条可重放结果：

```text
as_of_week / symbol / weekly_bar_count
sma20 / sma50 / sma20_slope_state
hill_weeks / hill_peak / retest_state / candle_state / volume_state
spx_filter / vix_filter / earnings_filter / liquidity_filter
prior_rally / instrument_availability
outcome = no_data | no_signal | filtered | candidate | invalid
reason_codes[] / input_hash / factor_version
```

该输出先进入 audit / Iris，不进入 drafts。原文的 60–70% 胜率、2:1–3:1 盈亏比和每年 5–15 次机会不得写成验收必须达到的答案。

### 8.3 必须先裁定的语义空洞

1. hill 6–10 周是否含信号周，等值是否中断，是否必须有局部峰。
2. SMA20 slope 的差分或回归公式、观察窗口与“走平”容差。
3. “触碰 SMA20”的容差、OHLC 相交与首次回踩重置规则。
4. 长上影比例、缩量 / 放量基准和窗口；条件 4 的“阴线 / 长上影 / 缩量”到底是 OR，还是缩量同时属于额外硬过滤，必须进入同一真值表。
5. SPX “强势区”、VIX 15 / 20 边界与 15–20 区间；VIX < 15 到底减半还是跳过。
6. 3–6 个月上涨 50%+ 是硬资格还是偏好；“日均成交量 > 5 亿美元”应明确为日均成交额及窗口；“财报周 / 财报前 5 天”采用自然日还是交易日、使用哪个时间源和盘后财报归属日。
7. 次周开盘 30 分钟后的有效入场窗口、急跌定义、order type 与 TTL。
8. 工具选择优先级、put spread DTE / strikes / width / liquidity；short stock 借券与召回风险。
9. TP 的 SMA50 是入场冻结值还是每周动态值；盘中还是周收盘确认。
10. 原文“持仓 2–4 周”与“最长 2 周”冲突；两周结束到底全平还是减半。
11. TP / SL / Time Stop 同时触发时的优先级，以及每次 exit 是否独立 Approve。
12. 三种工具各自怎样计算 1% NAV 风险；跳空、保证金、合约取整和组合预算怎样计入。
13. 连续 3 次止损按单 symbol 还是全策略、自然月时区、哪些 exit 算 stop。
14. 周线复权与 corporate actions 口径。

### 8.4 进入 drafts 前的最低验证

- 日线内部缺口、长尾缺口、IPO / 短历史、halt、零 volume、corporate action 与复权边界；Iris 不得把 252 行直接显示为 complete。
- 周线 OHLCV 聚合 fixture：普通周、假日短周、跨月 / 跨年、缺周、未完成本周；open 取首个完成日、high / low 取周内极值、close 取最后完成日、volume 求和。
- 19/20 与 49/50 根周线的数据不足边界。
- 未完成本周绝不产生 candidate；所有历史 fixture 无 look-ahead。
- hill 5/6/10/11 周边界、slope 容差、回踩容差、蜡烛与量能真值表。
- 五个核心入场条件逐一单点失败；SPX / VIX / earnings / liquidity 各边界。
- 次周开盘未满 30 分钟不得产生可批准 draft；同一信号幂等。
- −2.99% / −3% / −5% TP、跳空越过两档、TP1 后 SL / Time Stop。
- 1% NAV 恰好与超限；多笔单独合格但组合超限必须 blocked。
- 未批准不得提交；reject / expire / duplicate approve 均可辨且幂等。
- 合成 fixture → 历史 shadow（含成本、滑点与反例）→ live shadow → 独立裁定后才允许 pending draft。

### 8.5 历史与 live shadow 评估协议（V2-Q14 前置）

在查看 holdout 结果前，必须冻结一份带版本的 evaluation protocol：

1. 明确 universe 在每个历史时点的成员，处理退市与 survivorship；按时间先后划分 calibration / holdout，禁止随机打散周序列。
2. 冻结信号、候选、入场窗口、成本、滑点、borrow、option bid/ask 与部分成交模型；holdout 后不得回改阈值再称同一次验证。
3. 至少一个简单、可复算的基准在运行前登记；同时保留所有反例，不只展示盈利候选。
4. 最低候选数、覆盖率和不确定性要求由 V2-Q14 裁定；样本不足时结论是 `insufficient_evidence / no-go`，不是“通过”。
5. go / no-go 记录同时包含净成本结果、最大风险暴露、数据覆盖、样本外表现、反例与 live shadow 可执行性；原文胜率不作为预定答案。
6. live shadow 只写 audit / Iris evidence；即使出现 candidate，也保持零 drafts、零 orders。

---

## 9. `/orchestration` 近端 UI 修正

当前 `Trade` 在 `.agent-grid` 外；Consolidator 被提取到顶部后，grid 只剩 9 项，所以第 5 行右列空位，而 Trade 被排到整个 grid 后，见 `apps/orchestrator/src/athena_orchestrator/pages.py:4018-4074`。

**最低修正**：把 Trade markup 移入 `.agent-grid`，成为第 10 个 grid item，落在 `AdvisorExplainer` 右侧。无需改业务 prompt 或 `RUN_ORDER`。

**完整满足“列内不留空”的推荐形态**：两列独立 vertical stacks，而不是共享 row track：

- 左：Location / CSP / Purchase / Narrator / AdvisorExplainer。
- 右：News / Strangle / Call / ManageExplainer / Trade。
- 展开任一卡片只推动本列后续卡片，不在相邻列制造行高空白。
- 移动端恢复可解释的单列阅读顺序；不得复制两份表单。
- Consolidator 仍在顶部 cycle-split 右栏，Trade 仍是 manual Auto Set Price prompt。

**验收**：DOM 断言 Trade 位于右列；逐张展开卡片检查相邻列位置不变；900px 以下无横向溢出；保存 prompt 的 POST 语义与账号作用域不变。

---

## 10. 分阶段实施顺序与退出条件

排序只按依赖、风险与可验证性；工期是执行结果，不是排序依据。每阶段都需先确认对应契约再实现。

| 阶段 | 范围 | 依赖 | 退出条件 |
| --- | --- | --- | --- |
| **V2-P0** | 本册裁定：策略边界、Iris 范围、V2-Q 清单 | 用户确认本册 | 所有 P1 前置问题有明确裁定；未授权项仍为零代码 |
| **V2-P1** | **truthful static Iris**：改名 / alias、graph registry、现有表 snapshot、统一 draft rail、Guardian side band；同时完成 Orchestration 独立双列 | P0 | 旧 Advisor 行动表在页面上退役；任一 pending draft 都有统一行动入口，有持久化证据时可反查 strategy / factor / input，legacy 缺证据必须显示 `insufficient_provenance` 而非猜测；零新行动表；Trade 右列、展开隔离、移动端与 POST 回归全绿 |
| **V2-P2** | 结构化 run identity + `strategy_events` + SSE replay；真实事件动画 | P1 + V2-Q15 | news / factor / strategy / draft / approval / order 各有真实 fixture，BUY / CSP / CC / `OPEN_STRANGLE` 均可投影；rejected / expired / amended / replaced / blocked / partial-fill / cancel 状态正确；重复、乱序、错误 account / symbol / edge、graph version mismatch、event-write failure、replay→live 交接均经故障注入；无事件绝不动画，event 故障不改 domain commit |
| **V2-P3** | 新闻 article + association + legacy map additive backfill；初始 insert、DSH enrich、dismiss 等全部可变 writer dual-write 对账；canonical read | 可与 P2 并行，但不得与 NFR 行为变更混批 | 满足 §6.3 的逐项零未解释差异、legacy conflict 与 dismiss 回滚 fixture、collision 审查；跨 symbol 文章只一份；legacy 可回滚 |
| **V2-P4** | 日线 gap audit、周线聚合、SMA factor registry、Strategy 3 shadow | P1 + P2 + V2-Q7 / Q8 + V2-Q9 的**信号计算子集** | §8.4 数据与信号 fixture 全绿；Iris 可重放 candidate / filtered / no_data；零 drafts。工具、订单与 exit 执行语义可留 P5 / P6，但 hill / slope / retest / volume / calendar / 周线口径不得留白 |
| **V2-P5** | 按预先冻结的评估协议做 Strategy 3 历史与 live shadow；工具和风险契约裁定 | P4 + V2-Q14 + 其余相关 V2-Q | 在看结果前冻结时间顺序的 calibration / holdout 区间、候选定义、最低证据量、成本 / 滑点 / borrow / option spread 模型、至少一个简单基准和 go / no-go 阈值；报告反例与不确定性。样本不足或数据覆盖不合格即 no-go / 继续 shadow，不得事后调阈值；明确记录 go / no-go |
| **V2-P6** | Strategy 3 接入统一 ticket / draft / approval / budget / order gate | P5 go + 新工具风险扩展已验证 | pending draft 全链端到端；未批准零 IB order；风险与回滚测试全绿 |
| **V2-P7** | 已完成范围的 legacy news reader 与旧页面死代码清理、文档收口 | P1 + P3；若 P5 为 go 则另含 P6，no-go 不阻塞其余清理 | 单一行动入口与适用范围内的单一 news read model；旧链接仍可达；无未登记状态表 |

### 10.1 每批都必须通过的公共门禁

1. 对应纯函数与集成单测；关键新规则至少有会在破坏时失败的 mutation test。
2. 真实 PostgreSQL 迁移 / rollback 或事务内 dry-run；不只跑 fake connection。
3. draft → approve → queued → submitted / blocked 的账户隔离与权限回归。
4. Iris 断流、空数据、stale、reduced-motion、窄屏与键盘检查。
5. 不重启正在运行的 console，除非该批明确包含安全重启与回滚步骤。
6. 同步 00 册索引、CHANGELOG、日志与 workspace memory；不夹带 `.cursor/`、`docs/partnerships/` 等无关未跟踪内容。

---

## 11. 安全、权限与审计不变量

1. Strategy 3 的新工具不能默认落入现有 STK / OPT 风险公式就视为安全；short stock、long put、put spread 的风险与 contract selection 必须显式扩展。
2. 所有新策略 draft 必须带 `strategy_id`、factor version、input evidence 和 run id；不能只存一段 LLM reason。
3. LLM 只能解释或在已列出的合法候选中做受限选择；不得自行增加 leg、修改 size、绕过 Risk Guard。
4. 低 VIX 的“减半”若被裁定，必须明确是风险预算减半还是名义仓位减半，不能留给 LLM。
5. exit draft 的审批粒度必须明确；若希望某类风险退出自动执行，属于另一轮 privilege 变更，不在本册默认授权。
6. manual / liquidate / external IB orders 在 Iris 中有明确 source；统计“策略胜率”时不得混入。
7. graph events 不存 prompt secret、API token、完整账户凭据或大规模正文；payload 采用 allowlist。
8. `news dismissed` 的现有按 symbol 语义在迁移中保持，除非另行裁定改为全局文章 dismiss。

---

## 12. 建议修改面（实现时按阶段裁剪）

| 领域 | 现有落点 | v2 计划修改面 |
| --- | --- | --- |
| Iris route / view | `apps/orchestrator/src/athena_orchestrator/pages.py` | 新 Iris builder；旧 `/portfolio-analysis` alias；复用 draft 查询 |
| 样式与动画 | `console.py`、`design/shared/console.css`、新 Iris SVG registry | graph 状态、有限动画、responsive / reduced-motion |
| graph / event API | `console.py`、`runtime.py` | graph snapshot、SSE replay、run identity；不解析自由日志 |
| domain events | news / indicators / agents / drafts / order watch 的真实写入边界 | 幂等 observation event 与 domain snapshot reconciler；不改策略决策，event 故障不回滚 domain |
| news model | `news.py` + `dsh.py` + store migrations | articles / associations、backfill、初始 insert 与 sentiment enrichment dual-write、对账、canonical readers |
| weekly factors | `pricing.py` 或独立纯函数模块 + `indicators.py` registry | completed-week aggregation、SMA factor evidence |
| Strategy 3 | 新独立策略模块；不可塞入 `legacy_tactics.py` | shadow evaluator → gated draft adapter |
| 统一行动 | `drafts.py / manage.py / wheel.py` | 仅增加 strategy provenance 与新类型必需适配，不建第二队列 |
| Orchestration | `pages.py` + 必要 CSS / tests | Trade 右列、独立 stacks、移动端顺序 |
| 文档 | 本册、00 册、相关 Athena 产品文档 | 裁定后同步策略数量、路由、事件与风险契约 |

---

## 13. 待裁定问题清单（未编号，不阻塞现状研究）

| 规划 ID | 问题 | 推荐 |
| --- | --- | --- |
| **V2-Q1** | Iris 是否直接取代当前 Portfolio Analysis 页面 | 是；`/iris` canonical，旧路径 alias |
| **V2-Q2** | Strategy 1 / 2 命名是否按 Opening / Management 划分 | 是；不要把 NFR 与 agents 拆成两条假策略 |
| **V2-Q3** | 是否接受 V2-I1–V2-I10 作为目标不变量 | 接受，尤其统一 drafts 与 truthful animation |
| **V2-Q4** | 结构化 observation 是否采用 append-only `strategy_events` | 采用窄事件 read model，不建万能 event bus |
| **V2-Q5** | 新闻 identity v1 是否按 published day + normalized title | 是；URL 留证据，不做语义强合并 |
| **V2-Q6** | legacy `news_events` 保留多久 | 至少跨 N1–N3 对账期；删除另行裁定 |
| **V2-Q7** | 52W 完整性是否引入正式交易日历 | 推荐是；先测量缺口，再选日历实现 |
| **V2-Q8** | 周线 corporate action / 复权口径 | 实现前必须给定唯一口径与 fixture |
| **V2-Q9** | §8.3 的 Strategy 3 十四项语义 | 逐项裁定后才能离开 shadow |
| **V2-Q10** | Strategy 3 首选工具 | 推荐 capped-risk put spread，但 DTE / strikes / width 未裁定前不得实现 |
| **V2-Q11** | exit 是否每次独立 Approve | 默认沿用 `all_orders`；任何自动退出另开权限裁定 |
| **V2-Q12** | manual trade / liquidate 是否未来也纳入 pre-trade drafts | 本册只要求显式展示；改变现有旁路另开安全裁定 |
| **V2-Q13** | 确认 Orchestration 采用独立双列及移动端阅读顺序 | 推荐独立列；这是彻底消除展开卡片造成相邻列空白的必要条件 |
| **V2-Q14** | Strategy 3 历史 / live shadow 的评估协议、最低证据量与 go / no-go 阈值 | 必须在查看 holdout 结果前冻结；样本不足不得判 go |
| **V2-Q15** | observation event 故障与 domain commit 的隔离策略 | 推荐 event 故障不回滚 domain；幂等 emit + snapshot reconciler + 明示 observation gap |

---

## 14. 明确不做

- 不在本批实现 Iris、迁移新闻、修改策略、创建订单或重启 console。
- 不把已规划的 Strategy 3 写成当前已上线能力。
- 不承诺原文胜率、盈亏比、频率或最大回撤。
- 不新建 Iris 专用行动表、审批表、订单表或 Telegram channel。
- 不用 LLM 判断“这两篇新闻看起来像”就直接合并。
- 不用 log message、随机定时器或永久循环模拟实时事件。
- 不把 premium ratio 称为 measured IV，不把 Prediction shadow 称为可交易模型。
- 不把 Guardian 变成自动交易层，不借止损名义绕开人工审批。
- 不在这份规划里预占全局 D 号或迁移号，不改写 D1–D111 历史裁定。

---

## 15. 回滚原则

- Iris 每个阶段必须可关闭新路由或切回旧 view，不影响 domain writers。
- event stream 可停止；交易与审批仍由原表和原路径工作。
- news N1–N3 全程保留 legacy rows 与 fingerprint mapping；canonical read 可切回。
- Strategy 3 默认 shadow；任何 draft 化开关都必须可独立关闭，并阻止新开但不阻止已批准风险退出。
- Orchestration 列调整只改呈现，回滚不得影响 prompt 保存与账户设置。
- 每阶段单独登记变更台账、验证证据和回滚点；不把 schema、新闻行为、策略开启与 UI 大改塞成不可拆的一批。

---

## 16. 本批交付状态

本册当前交付的是 **现状研究 + v2 目标计划 + 待裁定契约**。下一步不是直接开发，而是先确认 §1、§3、§4、§10 与 §13；确认后再为对应阶段建立全局 D 决策、迁移号和实现台账。

> **状态推进（2026-11-25）**：操作者随后下达 `Please start the implementation for me.` 本册据此进入 **V2-P1 实现批次**。该指令只授权下述 P1 范围，不等于逐项批准 V2-Q1–Q15、P2–P7、新闻迁移、事件流或 Strategy 3 交易化。§14 的「不在本批实现 Iris」专指 `C-20261124-11` 规划批次，历史句保留不改写。

---

## 17. V2-P1 实现前置契约（2026-11-25）

### 17.1 本批授权范围

1. **命名与路由**：新增 canonical `/iris`；`/portfolio-analysis` 保留 GET alias；导航显示 `Iris`；两个路径都高亮同一导航项。
2. **静态架构图**：服务端生成版本化 graph registry，使用稳定 `data-node-id / data-edge-id / data-source / data-target`；本批不含 JS、SSE、polling 或 connector animation。
3. **只读 snapshot**：只读取现有 `market_bars / news_events / iv_marks / symbol_phases / advisor_signals / regime_labels / drafts / approvals / orders / order_events / guardian_alerts` 等现有表或纯 helper；禁止调用 Guardian `sync()`、VIX sync、Advisor `run_advisor()`、draft `decide()`、任何 `commit()` 或 IB 连接。
4. **统一 draft rail**：右侧行动列表只读 `_pending_drafts` 一类已有 pending draft 查询，链接回 `/drafts`；不创建 `iris_actions`、不复制审批按钮、不新建状态字段。
5. **旧行动面收敛**：旧 `Advisor actions` 表从页面退役；Advisor report 保留为 Strategy 2 evidence card。Run C regime card 保留为只读 Guardian side band。
6. **legacy provenance**：当前 drafts 没有 `strategy_id / factor_version / input evidence / run_id`；静态图与 rail 对任何试图反查的 legacy action 显示 `insufficient_provenance`，不按 tactic 猜测血缘。
7. **Orchestration**：两列独立 stack，Trade 为右列最后一张卡；展开卡片只推动本列；移动端恢复原阅读顺序；POST action、hidden agent、settings save 与账号作用域不变。
8. **Guardian**：只呈现现有 read-only grade / alert / regime 信息；Guardian 不画成 Strategy 4，不产生 actions。manual / liquidate / external order 以 operator direct path 文字标注，不接入策略审批绿链。

### 17.2 明确不做

- 不实现 `strategy_events`、SSE replay、动画、自由日志解析、事件写失败策略或 graph live status。
- 不改新闻 schema，不跑 news backfill，不处理 DSH / dismiss dual-write。
- 不实现周线 SMA、Strategy 3 shadow、历史评估或 bearish 交易工具。
- 不改 watchlist、settings schema、migrations、drafts / approvals / orders 状态机、engine cycle、Telegram 或运行中的 7799 进程。
- 不在本批分配全局 D 号，不改写 D1–D111 / 112 节点 / 54 条裁定。

### 17.3 验收与回滚

- 新增 P1 单测覆盖：Iris title / route / alias、nav active、静态 graph 节点与边、无动画标记、统一 draft rail、legacy provenance、Run C 只读、无写入副作用、Trade 右列、双列独立、移动端顺序、POST 语义、旧文案同步。
- 更新受影响 legacy tests 的页面名称与 Run C 文案；保留 D111 英文固定 UI、表格、footnote、动态业务 reason escape 与窄屏 CSS 回归。
- 运行新增 focused suite、全量 `unittest discover`、`compileall`、`uv lock --check`、`git diff --check`、台账机检；做 targeted mutation traps 并逐字节还原。
- 真实 PostgreSQL 只做只读或事务内 rollback 验证；不因为 UI 批次重启操作员正在使用的 console。
- 回滚：按本批 commit `git revert`；Iris 路由可单独停用并保留 `/portfolio-analysis` 原入口；Orchestration 列调整仅回滚渲染，不动 prompt settings。

---

## 18. V2-P1 交付记录（2026-11-25, `C-20261125-1`）

| 项 | 内容 |
|---|---|
| 状态 | ✅ **已交付并验证**。仅 §17 授权的静态 Iris 与 Orchestration 独立双列；P2–P7、V2-Q1–Q15、新闻迁移、事件流、Strategy 3 交易化均**未启动** |
| 命名与路由 | canonical `/iris`；`/portfolio-analysis` 保留为 GET alias（`_portfolio_analysis_page()` 退化成薄包装）；导航只显示一条 `Iris`，两个路径共用同一 active 高亮（`console.IRIS_ALIASES`） |
| 静态图 | 新 `iris_view.py`：版本化 `GRAPH_VERSION = "iris-p1-static-v1"`；22 个节点 / 23 条边，稳定 `data-node-id / data-edge-id / data-source / data-target / data-flow`；状态只用 `no_data / partial / ready / unknown / shadow / configured / idle / operator`；shadow / direct 边虚线；无 JS、SSE、polling、connector animation |
| 只读 snapshot | 只发 `SELECT`，读 13 张现有表；显式 `daily_gap_state = "unknown"`、`run_evidence = "unavailable"`；零 `commit()` / `rollback()`、零 Guardian `sync()`、零 VIX sync、零 Advisor `run_advisor()`、零 draft `decide()`、零 IB 连接 |
| 统一行动面 | 右侧 rail 复用 `_pending_drafts()`，链接回 `/drafts`；legacy draft 一律 `insufficient_provenance`，不按 tactic 猜血缘；旧 `Advisor actions` 表从页面退役，Advisor report 降为 Strategy 2 evidence；Run C 与 Guardian 为只读 side band，Guardian 不画成 Strategy 4；manual / liquidate / external 标为 operator direct path |
| Orchestration | `.agent-grid` → `.agent-columns` 两个独立 flex stack；Trade 为右列最后一张卡；卡片带 `data-agent-name / data-agent-order / --agent-order`，窄屏 `order: var(--agent-order, 99)` 恢复原阅读顺序；POST `action="/orchestration"`、hidden `agent`、settings save 与账号作用域一字未改 |
| 零改动边界 | 零迁移（仍 51）、零 `uv.lock` 变动、零 Settings 字段、零新数据源、零 §1／§6 修订、零全局 D 号（D1–D111 / 112 节点 / 54 裁定不变）、零运行引擎重启 |
| 实测发现 | **真实 PostgreSQL 才暴露的 bug**：`LIKE 'VIX%'` 中的裸 `%` 被 psycopg 当成占位符，抛 `ProgrammingError: only '%s', '%b', '%t' are allowed as placeholders`。fake conn 单测无法捕获；已改为 `LIKE 'VIX%%'` 并新增 `test_like_wildcard_is_escaped_for_psycopg` 钉死。这是本批唯一的生产代码缺陷修复 |
| 遗留缺口（诚实登记） | ① 无结构化 run event ⇒ 因子节点恒 `unknown`、graph 无 live status；② Strategy 3 / weekly SMA 恒 `shadow`，其边为虚线；③ `market_bars` 无 ib-daily 时 52W 显示 `no_data`，内部 gap 仍 `unknown`；④ legacy draft 无 `strategy_id / factor_version / input evidence / run_id`，只能 `insufficient_provenance`；⑤ `/skill` 页与文档中的 `8788` 示例 URL 属既有 display-only 不一致，本批不动；⑥ 运行中的 7799 console 仍是改动前代码，D111 自动 VIX backfill 与本批 UI 需未来安全重启才生效 |
| 回滚 | `git revert <sha>`；只回滚渲染与新增模块，不动 prompt settings、drafts / approvals / orders 数据。Iris 路由可单独停用并保留 `/portfolio-analysis` 原入口 |

### 18.1 实测验证块

```
新增 focused suite   apps.orchestrator.tests.test_iris_p1          63/63 OK
受影响 legacy suites test_guardian_r3 + r7 + test_d111_vix        211/211 OK
全量回归             unittest discover -p "test_*.py"              810/810 OK (skipped=2)
                     (基线 747 + 63；无失败、无新增 skip)
mutation traps       28/28 TRAPPED，逐字节 sha256 还原一致
                     (含针对 LIKE 'VIX%%' 修复的专项 trap)
compileall           OK
uv lock --check      Resolved 29 packages (无变动)
git diff --check     0
台账机检             logs 13 个 / 条目 123 条 / 索引 123 行 ✓
真实 PostgreSQL      athena-pg 51 migrations；BEGIN READ ONLY
                     snapshot 25 keys → 22 nodes / 23 edges
                     6 张表前后 count 不变，ROLLBACK 零写入
                     pages._iris_page(DUR253115) 20037 bytes，零 CJK
                     header / graph / Strategy 2 evidence / Guardian band / rail 全渲染
```

### 18.2 关联

- 台账 `C-20261125-1`（`logs/2026-11-25.md`）＋ `CHANGELOG.md` 索引行。
- 上游契约 §17；上游规划批次 `C-20261124-11`（§16）。
- 页面契约 `docs/athena/13-portfolio-advisor.md`；导航与 CSS `console.py`；页面 `pages.py`。
- 下一阶段仍为 **用户裁定 V2-Q1–Q15**，不是自动进入 P2。

---

## 19. V2-Q Round A 裁定记录（2026-11-25, `C-20261125-2`）

### 19.1 本批性质

操作者在 P1 交付后选择「Start V2-Q Round A」。本批**只裁定、不实现**：把近端 8 条 `V2-Q` 从「待裁定」推进到「已裁定」，为 V2-P2 / V2-P3 各自撰写落地前契约提供前置依据。按本册编号纪律，`V2-Q*` 是**册内规划标识，不是全局 D 决策**；D1–D111 / 112 节点 / 54 条裁定一字不动，不预占迁移号，不改写 §1 / §6 边界。

裁定方式：4 条由 P1 已交付代码反证确认（ratified-by-delivery），4 条经一次英文 `AskUserQuestion` 逐条裁定并附实测案例。**8 条全取推荐，0 偏离**。

### 19.2 八条裁定

| V2-Q | 问题 | 裁定 | 来源 |
|---|---|---|---|
| **Q1** | Iris 是否直接取代 Portfolio Analysis 页面 | ✅ **是**：`/iris` canonical，`/portfolio-analysis` 保留 alias | P1 交付 `7bbbf40` 反证 |
| **Q2** | Strategy 1 / 2 是否按 Opening / Management 划分 | ✅ **是**：V2-S1 NFR Wheel · Opening、V2-S2 Advisor · Management；不把 NFR 与 agents 拆成两条假策略 | 同上 |
| **Q3** | 是否接受 V2-I1–V2-I10 目标不变量 | ✅ **接受**，尤其统一 drafts 账本与 truthful animation | 同上（P1 零新行动表、零装饰动画） |
| **Q13** | Orchestration 独立双列及移动端阅读顺序 | ✅ **采用**：两列独立 stack，`--agent-order` 恢复单列顺序，展开卡片只推动本列 | 同上 |
| **Q4** | 结构化 observation 的存储形态 | ✅ **append-only `strategy_events` 窄 read model**：一行一个真实 domain transition，`event_id` 由 subject + transition + version 确定性生成。否决通用 event bus / outbox，否决「不建表、纯推导」 | AskUserQuestion 推荐项 |
| **Q15** | observation event 故障与 domain commit 的隔离 | ✅ **domain commit 唯一真源，event 故障绝不回滚**：commit 之后幂等 observation emit；周期性 reconciler 从持久化 domain rows 补齐可推导事件；补不出的区间明示 `observation_gap`。否决「同事务原子写」与「fire-and-forget 无 reconciler」 | AskUserQuestion 推荐项 |
| **Q5** | 新闻 identity v1 | ✅ **published day + normalized title**：URL 只作证据，不做语义强合并；沿用 `title.lower()` 口径，不引入 NFKC / 空白归一化。否决「仅 URL」与「语义相似度」 | AskUserQuestion 推荐项 |
| **Q6** | legacy `news_events` 保留多久 | ✅ **至少跨 N1–N3 对账期**；任何删除另开裁定。否决「验证后即删」与「无限期不裁定」（后者等价于把删除决策永久悬空） | AskUserQuestion 推荐项 |

统计：8 条 / 4 条交付反证 + 4 条实质裁定 / 8 全取推荐、0 偏离。全局 D 号零新增。

### 19.3 四条实质裁定的落地含义

- **Q4 → P2 形态冻结**：`strategy_events` 是窄 read model，不是万能 event bus。`draft_created / draft_rejected / draft_expired / draft_blocked / draft_amended / order_queued / order_submitted / order_filled / partial_fill / cancel` 与 `filtered / quiet / invalid` 各自需要真实 fixture；replay → live 交接、乱序、重复、错误 account / symbol / edge、graph version mismatch 都要故障注入。§8.5 的「推荐，需 Q15 裁定」自本批起升为已裁定。
- **Q15 → 交易安全边界**：event 写失败不得阻断或回滚 draft / approval / order 的 domain commit（V2-I2）。`observation_gap` 是诚实状态，不得用装饰性 connector 动画填补无证据区间；reconciler 只能补可从 domain rows **推导**的事件，不得臆造。
- **Q5 → P3 identity 冻结**：canonical article + per-symbol association + 独立 `news_legacy_event_map` 三件套不变；identity v1 不做语义强合并；canonical 写入不依赖 legacy conflict；dismiss 必须解析明确 `association_id`，禁止一对多 fingerprint 全部 dismiss。跨 symbol 同文只存一份。
- **Q6 → P3 验收门槛**：`news_legacy_event_map` 条数 == legacy 行数；initial insert、DSH sentiment enrich、dismiss 三处可变 writer 的 dual-write 对账全绿之前，不删任何 legacy 行，保留完整回滚路径。

案例锚点（裁定当场给出，作为后续 fixture 的验收样例）：

1. NVDA CSP 10:31 drafted → 10:34 rejected ⇒ replay 显示两次独立 connector pulse，不是一次。
2. `'NVDA beats Q3 estimates'` 同日落在 NVDA / SMH / QQQ ⇒ 1 篇 canonical article + 3 条 association，不是 3 行。
3. CC draft 落库后 `strategy_events` INSERT 超时 200ms ⇒ draft 仍 pending 且可批准，Iris 该区间显示 `observation_gap`。
4. 活库实测 2,529 行 news、269 个跨 symbol 标题组 ⇒ map count 必须等于 legacy 行数才算对账通过。

### 19.4 明确不做 / 仍未裁定

- **裁定 ≠ 实现授权**。P2 / P3 仍需各自撰写落地前契约（建议 §20）并经操作者确认后才可动代码；本批零代码、零迁移（仍 51）、零 Settings 字段、零 `uv.lock` 变动、零运行库写入、零引擎重启、零 §1 / §6 修订。
- **仍未裁定 7 条**：Q7（52W 是否引入正式交易日历）、Q8（周线 corporate action / 复权口径）、Q9（§8.3 Strategy 3 十四项语义）、Q10（首选工具与 DTE / strikes / width）、Q11（exit 是否每次独立 Approve）、Q12（manual trade / liquidate 是否纳入 pre-trade drafts）、Q14（历史 / live shadow 评估协议、最低证据量与 go / no-go 阈值）。对应 P4–P6；本轮不问的理由：Q9 / Q10 / Q14 需要先有数据与 fixture 才有意义，且 §8.5 明确 Q14 必须在看 holdout 结果**之前**冻结，现在裁定等于凭空猜阈值。
- 两个已知启动阻塞（`run_loop()` 的 `load_active()` 早于 `apply_migrations()`；`var/backups` 996 快照触发 prune 批量删除）属独立运维轮，不在本批。
- §14 的「不在本批实现 Iris」、`C-20261124-11` 的历史句、§17 与 §18 的交付状态一律保留原文，不因本批改写。

### 19.5 验收与回滚

| 项 | 结果 |
|---|---|
| 编号纪律 | V2-Q 册内标识；D1–D111 / 112 节点 / 54 裁定不变；零新 D 号；不预占迁移号 |
| 文档一致性 | 15 册 §19 + 头部状态推进；00 册 15 号索引；`docs/athena/13` 不动；历史 verbatim 不改写 |
| 台账 | `CHANGELOG.md` 索引行 + `logs/2026-11-25.md` `C-20261125-2`；机检 124/124 |
| 静态门禁 | `unittest discover` 810/810（2 env skips）、`git diff --check` 0、台账机检、缺失索引 mutation trapped |
| 运行时 | 零代码改动 ⇒ 无需重启 7799；本批未启动任何 engine 进程，未写任何业务表 |
| 回滚 | `git revert <sha>`；只回滚裁定文档与台账，不动任何运行时文件、prompt settings 或交易数据 |

### 19.6 关联

- 上游：`C-20261124-11`（§16 规划批次）、`C-20261125-1`（§17/§18 P1 交付）。
- 下游：§20 建议作为 V2-P2 / V2-P3 落地前契约（**待操作者确认后才写、之后才实现**）。
- 并行未决：Q7–Q12、Q14（P4–P6）；两个启动阻塞运维轮。

---

## 20. V2-P2 / V2-P3 落地前契约（2026-11-25, `C-20261125-3`）

### 20.0 本批性质

操作者已确认 §19 为合并后的裁定源。按 doc-first 纪律，本节**在实现之前**把 V2-P2（结构化事件 / SSE / 真实动画）与 V2-P3（新闻 canonical 模型）各自的实现边界、实测接缝、fixture、迁移与回滚写成契约。

- **本批零代码、零迁移、零 Settings、零运行库写入、零引擎重启。**
- **§20 本身不授权实现**：P2 / P3 仍需操作者批准本节，才进入各自实现批次；两者可并行但**不得与 NFR 行为变更混批**（§10 相位表）。
- 依据 §19 已裁定的 Q3 / Q4 / Q15（→ P2）与 Q5 / Q6（→ P3）；§6.2 / §6.3 的 identity 与 N1–N4 分阶段迁移**已被 Q5 / Q6 追认，原文不改写**，本节只补实测接缝与交付门禁。
- 本节不产生全局 D 号，不预占迁移号，不改 §1 / §6，不改 D1–D111 / 112 节点 / 54 裁定。

### 20.1 只读实测接缝（2026-11-25）

**P2 — 事件与传输**

| 接缝 | 实测位置 | 结论 |
|---|---|---|
| SSE 传输已存在 | `console.py:934 _open_stream()`、`:941 _open_api_stream()`；端点 `/api/pulse/ticks`、`/api/logs`、`/api/orders/events`（`console.py:862–870`） | 可复用 HTTP 头 + `queue.Queue` 订阅循环，**但都是进程内队列，无持久化 replay** |
| 订阅模式 | `pulse.py:96 subscribe_ticks()`、`runtime.py:346 trail_recent()` / `:351 subscribe_trail()`、`order_watch.py:46 subscribe(account)` | `trail_recent()` 只在进程内存里，重启即失；不能当作 `strategy_events` replay |
| 周期轮询 | `console.py:862 /api/cycle` → `runtime.cycle_view()`；前端 `console.py:3925 fetch("/api/cycle")` 每秒 | 是粗粒度阶段状态，**不是事件流**，不得解析日志冒充事件 |
| 已有订单事件表 | `052_order_events.sql`：`order_id / account_id / ib_status / status / filled_qty / remaining_qty / avg_fill / created_at`，`ON DELETE CASCADE` | 是**订单维度**的 append-only 日志，不是通用 strategy event model；P2 不得改写它，也不得拿它充当 `strategy_events` |
| domain commit 边界 | `drafts.py:899 INSERT INTO drafts`；`:1767 approved` / `:1773 rejected` / `:1778 INSERT INTO approvals` / `:1892 INSERT INTO orders`，`conn.commit()` 在 `:1783`；`expire_pending()` `:82` + commit | Q15 的「commit 之后幂等 emit」必须锚定这些**已存在的 commit 点**，不得在 transaction 内同步写事件 |
| 其它状态写入 | `budget.py:42` / `wheel.py:775,881` blocked；`drafts.py:1599` 通用 `UPDATE drafts SET ...`（amend）；`manage.py:199` reason 更新；`session.py:531/545/566/575/617/639/757/773` 订单状态；`order_watch.py:96/336` 订单更新 | 事件词表必须覆盖这些路径，否则动画会漏帧并撒谎 |
| operator direct paths | `order_watch.py:431 _insert_ib_order()`（`tactic='EXTERNAL'`、插入即 `status='approved'`）；`tasks.py:450–467`（`tactic='MANUAL'`、插入即 approved，并同时写 approvals + orders） | 这类 draft **没有 pending 阶段**，不得投影成策略 `draft_created`；§17.1(8) 已要求标成 operator direct path |
| graph 版本 | `iris_view.py:14 GRAPH_VERSION`、`:36 NODES` / `:61 EDGES` | replay 事件携带 graph_version，不匹配时不得动画，触发 mismatch 分支 |

**P3 — 新闻读写**

| 接缝 | 实测位置 | 结论 |
|---|---|---|
| 旧表结构 | `006_news_events.sql`：`fingerprint text PRIMARY KEY`，`ts / symbol / source / event_type / sentiment / title / url`；`017_news_dismissed.sql` 追加 `dismissed boolean` | 无 article 表、无 association 表、无 `account_id`；`url` 字段已存在（Q5 的证据字段可落地） |
| 旧 identity | `news.py:508` 与 `:540`：`sha256(f"{symbol.upper()}\|{title.lower()}")[:24]` | **symbol 在 identity 内 ⇒ 跨 symbol 重复是结构性、必然的**，配置改不掉 |
| 三处初始写入 | `news.py:511 _insert_event()`（`ts=datetime.now(UTC)`）；`news.py:543 _save_headlines()`（`ts=provider when`）；`news.py:122 store_manual()` 复用 `_save_headlines`（`ts=now(UTC)`） | **三种 `ts` 语义**：provider 发布时间 / ingestion 时间 / ingestion 时间。§6.2 的 `effective_ts + day_precision` 与 ET 日界必须逐 writer 落地，不得把 fallback 伪装成 provider 时间 |
| 幂等锚点 | 两处 `INSERT ... ON CONFLICT (fingerprint) DO NOTHING` | canonical 必须按自身 identity 幂等写入，且不依赖 legacy 是否冲突（§6.3 N2 的 `conflict_existing`） |
| sentiment writer | `dsh.py:188 UPDATE news_events SET sentiment=%s WHERE fingerprint=%s` + `conn.commit()`；`enrich_sentiment()` `dsh.py:163` 接收 `_save_headlines` 返回的 `(fingerprint, symbol, title)` | DSH 是 **commit 之后的第二次写**，dual-write 对账必须覆盖；`fresh` 列表按旧 fingerprint 组织，mapping 必须能反查 association |
| dismiss writer | `news.py:107 dismiss_event(fingerprint)`：`UPDATE ... SET dismissed=true ... RETURNING fingerprint` + `conn.commit()` | 入口只收**单个 fingerprint**；Q5/§6.3 要求解析明确 `association_id`，一对多时必须拒绝并要求新 UI 传 id，禁止一次 dismiss 全部 |
| reader 面 | 21 处非测试 SQL，分布在 `pages.py`(7)、`iris_view.py`(2)、`news.py`(4)、`agents.py`(2)、`telegram.py`、`console.py`、`guardian_regime.py`、`advisor.py`、`indicators.py`、`dsh.py`；其中 `news.py:226 tags_from_events()` 过滤 `sentiment < x AND ts >= x AND dismissed=false` | N3 canonical read 必须逐处对账；`NEWS_WASH` 与 `news_score` 的 Decimal 路径一致是 §6.3 既定验收项 |
| 备份契约 | `backup.py:8–27 TABLES` 只列 20 张表，**连 `052_order_events` / `055_regime_labels` 都不在其中** | 既有遗留缺口。P3 若依赖 backup/restore 回滚，必须显式处理新表，否则 restore 会丢 canonical 数据；本批不扩大备份契约，列为实现批次必答项 |

**真实 PostgreSQL 只读快照（`athena-pg`，51 migrations，`BEGIN READ ONLY` → `ROLLBACK`）**

```text
news_events                 2,529 rows
distinct lower(title)       1,872
distinct (UTC day, lower)   1,873     ← day 几乎不额外拆分，仅 1 标题跨两日
cross-symbol title groups     440
rows in those groups        1,097
rows with url               2,529 / 2,529
distinct sources                2
ts range          2026-05-08 .. 2026-11-25
dismissed                         0
strategy_events table       absent
drafts / orders                 empty (smoke DB)
```

⇒ 按 Q5 identity 预计收敛到约 **1,873 篇 canonical article + 2,529 条 association**，减少约 **656 行**重复。该数字是**当前快照的推算，不是验收阈值**；实现批次须在事务内 dry-run 重算。`distinct (UTC day, lower)` 用 UTC 日界计算，§6.2 规定 **America/New_York** 日界，两者可能不同 —— 必须列为 fixture。

### 20.2 V2-P2 契约

1. **新表**：独立 migration 建 `strategy_events`，append-only 窄 read model（Q4）。建议字段：`event_id text PRIMARY KEY`（由 domain subject + transition + version 确定性生成）、`run_id / account_id / subject_type / subject_id / status / from_status / to_status / reason_code / symbol / edge_id / graph_version / occurred_at / payload jsonb / created_at`。**不建万能 event bus、不建订阅注册表、不给事件加可变状态列**。
2. **词表**：`news_ingested / factor_computed / strategy_started / strategy_completed / filtered / quiet / invalid / draft_created / draft_approved / draft_rejected / draft_expired / draft_blocked / draft_amended / draft_replaced / order_queued / order_submitted / order_blocked / order_cancelled / order_filled / partial_fill / observation_gap`。§8.5 的 `status` 与结构化 `from_status / to_status / reason_code` 必须显式，不得从自由文本推断。
3. **emit 边界（Q15）**：domain commit **完成后**幂等 emit；`event_id` 确定性 ⇒ 重复 emit / retry / replay 收敛为同一行。事件写失败只记 `observation_gap`，**绝不**回滚或阻断 `drafts / approvals / orders / positions` 的 commit。不得把 emit 放进 domain transaction。
4. **reconciler**：周期性从持久化 domain rows 补齐**可推导**的缺失事件；补不出的区间显式 `observation_gap`。禁止臆造、禁止用装饰动画填补。
5. **SSE**：新增独立端点（复用 `_open_stream()` 的 header 与 `queue.Queue` 循环骨架），先按 cursor 从 `strategy_events` replay，再交接 live；`Last-Event-ID` / cursor、断线重连、重复、乱序、错误 account / symbol / edge、graph_version mismatch 全部故障注入。**无事件时零动画**。`/api/cycle` 轮询与 `/api/logs` 自由日志不参与事件判定。
6. **operator direct**：`EXTERNAL` / `MANUAL` 插入即 approved，不得投影为策略链路；只显示 §17 已标注的 operator direct path。
7. **fixture 必含**：BUY / CSP / CC / `OPEN_STRANGLE` 四种 tactic；rejected / expired / amended / replaced / blocked / partial-fill / cancel 七类状态；event-write failure；replay→live 交接；「无事件不动画」。

### 20.3 V2-P3 契约

1. **三表**：按 §6.3 已画出的 `news_articles` / `news_article_symbols` / `news_legacy_event_map`，additive，**不改 `news_events` 结构与主键**；legacy map 不并入 association 主键。
2. **identity v1**：严格按 §6.2 —— `normalized_title = title.lower()`（不做 NFKC / 空白折叠）、`effective_ts` 区分 provider 与 `ingest_fallback`、`published_day = America/New_York` 日界、`article_key = sha256("v1|" + published_day + "|" + normalized_title)`、带 `identity_version`。URL 只作证据，不做语义合并。本节**不改写 §6.2**。
3. **dual-write 三处**（§6.3 N2）：初始 insert（两个 writer + manual）、`dsh.py` sentiment enrich、`dismiss_event()`。每处记录 `legacy_write_status`；canonical 写入不依赖 legacy `ON CONFLICT DO NOTHING` 结果。
4. **dismiss 语义**：兼容入口解析明确 `association_id`；一个 fingerprint 映射多个 association 且目标不明确 ⇒ **拒绝操作**，要求 UI 传 id；禁止批量 dismiss。
5. **readers 对账**：§6.3 六项在真实库 copy 或事务内 dry-run 逐项验证；`NEWS_WASH` 逐 symbol 一致、`news_score` 同一 Decimal 路径重算一致；所有差异必须登记，未登记差异阻止 N3。
6. **legacy 保留（Q6）**：至少跨 N1–N3 对账期不删 `news_events`；map 数 == legacy 行数、每 legacy row 恰好映射一次、association 无 orphan。删除另开裁定。
7. **不与 NFR 行为变更混批**：P3 改的是新闻存储/read model，不得顺手改 wheel gate、tag 阈值或 `NEWS_WASH` 语义。

### 20.4 迁移与打包决策

- 当前 `packages/store/src/athena_store/migrations/` 有 **51** 个 `0*.sql`，末位 `055_regime_labels.sql`；下一空号是 `056`。**本批不预占、不创建**；实现批次取当时的下一个空号，P2 与 P3 各一张。
- `packages/store/pyproject.toml:12–` 的 `[tool.hatch.build.targets.wheel.force-include]` 是**逐文件显式清单**（现 51 条，末条 `055_regime_labels.sql`）。新 migration 必须同时加入该清单，否则 wheel 不含该文件（R7 已实测此坑）。
- `apply_migrations()`（`migrate.py:26`）按文件名排序、用 `schema_migrations.version` 去重、`conn.commit()` 一次；新表必须在真实 postgres 实例上验证 apply + wheel 内含数量。
- **至少 6 处测试会随新迁移变化**：`test_d111_vix.py:422`、`test_guardian_r6.py:540`、`test_guardian_r7.py:912` 断言 `len(migrations) == 51`；`test_guardian_r3.py:516–518`、`test_guardian_r4.py:579`、`test_guardian_r5.py:639` 断言末位仍是 `055_regime_labels.sql`。实现批次必须**同步这些断言，而不是删除它们**（D108 收窄而非删除的先例）。
- 启动顺序缺陷（`run_loop()` → `_cycle_sleep()` → `load_active()` 早于 `apply_migrations()`）与 `var/backups` 996 快照 prune 属独立运维轮；若实现批次要跑 smoke，仍用 `ATHENA_LOCAL_BACKUP_DIR=var/backups-smoke` 与预加迁移的 workaround。

### 20.5 明确不做

- 不实现 `strategy_events` / SSE / 动画 / replay；不建新闻三表；不跑 news backfill；不改写任何 writer 或 reader。
- 不实现周线 SMA、Strategy 3 shadow、评估协议、工具或 exit 语义（P4–P6，Q7–Q12 / Q14 仍未裁定）。
- 不改 watchlist、Settings schema、drafts / approvals / orders 状态机、engine cycle、Telegram、NFR gate、`backup.py TABLES`。
- 不重启运行中的 7799 console（§10.1 门禁 5）；不启动任何 engine 进程；不写任何业务表。
- 不分配全局 D 号、不改 §1 / §6、不预留迁移号、不改写 §14 / §17 / §18 / §19 的历史 verbatim。

### 20.6 验收门禁（实现批次，不是本批）

1. 纯函数 + 集成单测；每个新规则至少一个破坏即失败的 mutation test，逐字节还原。
2. 真实 PostgreSQL 迁移 apply、事务内 dry-run / rollback；不只跑 fake conn（P1 的 `LIKE 'VIX%'` 教训：裸 `%` 必须写成 `%%`）。
3. P2：draft → approve → queued → submitted / blocked 账户隔离与权限回归；事件故障注入不得改变 domain commit。
4. P3：§6.3 六项对账 + dismiss 一对多拒绝 + collision report + ET 日界 / provider 缺失 / 跨日同标题 / 手工 URL fixture。
5. Iris：断流、空数据、stale、`prefers-reduced-motion`、窄屏、键盘检查；`graph_version` mismatch 不动画。
6. 全量 `unittest discover`、`compileall`、`uv lock --check`、`git diff --check`、台账机检、00 册索引 / CHANGELOG / 日志 / workspace memory 同步。
7. 不夹带 `.cursor/`、`docs/partnerships/` 等无关未跟踪内容。

### 20.7 回滚

- 按实现批次的 commit `git revert`；P2 可单独停用 SSE 端点并保留 `strategy_events` 表（append-only，无读取方即无害）；P3 按 §6.3 逐阶段回退（N3 → legacy reader，N2 → 切回旧 writer，N1 → 删新表，**legacy 数据不动**）。
- 若依赖 backup/restore，须先解决 `backup.py TABLES` 未含新表的问题，否则 restore 后 canonical 数据缺失。
- P2 / P3 分开提交、分开回滚，避免一次 revert 同时抽掉事件与新闻两条链。

### 20.8 批准 §20 时需一并确认的实现口径（非新裁定、非全局 D 号）

这些是**实现口径**，不改变 §19 的任何裁定；若操作者认为其中某条实为决策，需另行裁定而非默认通过：

1. `published_day` 时区口径确认沿用 §6.2 的 `America/New_York`（§20.1 快照按 UTC 日界推算，两者可能不同）。
2. `strategy_events` 的 `payload jsonb` 是否允许携带自由文本 `reason`（建议只允许结构化 `reason_code`，自由文本留在 draft.reason）。
3. P2 SSE 端点命名与是否复用 `/api/logs` 的 `trail_recent()` 订阅骨架（建议新端点，避免与日志流混淆）。
4. P3 是否把新表加入 `backup.py TABLES`（建议加入，但会扩大备份契约，需明确授权）。
5. P2 / P3 的迁移号是否按 `056` / `057` 顺序分配，或按实际实现先后取当时空号（建议后者，本节不预占）。

### 20.9 关联

- 上游：`C-20261124-11`（§16 规划）、`C-20261125-1`（§17/§18 P1 交付）、`C-20261125-2`（§19 Round A 裁定）。
- 本节是 **V2-P2 / V2-P3 实现批次的前置契约**；未获操作者批准前零代码。
- 并行未决：Q7–Q12 / Q14（P4–P6）、两个启动阻塞运维轮、D111 的 7799 安全重启与 IB Gateway 可用性。

---

## 21. §20 批准记录与 §20.8 实现口径裁定（2026-11-25，`C-20261125-4`）

### 21.1 状态与授权边界

- 操作者要求「Run each item with me one by one」，§20.8 五条逐项确认，**5 / 5 全部有明确结论、零跳过**。§20 由「待操作者批准」推进为 **已批准**；§20.9「未获操作者批准前零代码」是本批之前的历史状态，保留不改写，状态推进见本节与头部第四笔。
- **本批只有记录**：零代码、零迁移、零 DB 写入、零 engine 重启、不预占迁移号、不分配全局 D 号、不改 D1–D111 / 112 节点 / 54 条裁定、不改 §1。
- **§20 获批 ≠ 授权实现**。P2 / P3 仍须各自独立实现批次、独立 commit / 独立 rollback（§20.7）。Q7–Q12 / Q14 共 7 条仍未裁定，P4–P7 未授权。
- 本节的 `V2-*` 仍是册内标识；第 1 条的 UTC 修订是 §6.2 / Q5 的**窄修订**，不是新全局 D 号，也不是新 `V2-Q` 号。

### 21.2 §20.8 逐项裁定（5 / 5）

| # | 口径 | 裁定 | 落地含义 | 明确排除 |
|---|---|---|---|---|
| 1 | `published_day` 日界 | **UTC 日界**（操作者：「The date is stored in UTC right? Just stick to the date UTC」）。此为**显式窄修订**，见 §21.3 | `published_day` 取 `ts AT TIME ZONE 'UTC'`；解析入口保证来源时间按时区正确换算为 UTC 后再写入 | 不新增 `provider_tz` / `published_local_day` / 其它时区或证据列；不改 Q5 identity 公式主体 |
| 2 | `payload jsonb` 自由文本 `reason` | **只允许结构化 `reason_code`** | 事件行 `status` / `from_status` / `to_status` / `reason_code` 显式；`payload` 只放与 `reason_code` 对应的结构化标量；人类可读句子留在 `drafts.reason` | `payload` 不设 `reason` 键；不存第二份会漂移的自由文本；不从自由文本推断状态（§8.5） |
| 3 | P2 SSE 端点 | **新增 `/api/iris/events`** | 独立端点 + 独立订阅注册表，结构化 JSON 帧；复用 `_open_stream()` / 15s keepalive / `client_closed` 底层骨架 | 不复用 `/api/logs` 的 `trail_recent()` / `subscribe_trail()`；`/api/logs`、`/api/pulse/ticks` 不动；P2 仍无持久化 replay，重连一律显式 `observation_gap` |
| 4 | `backup.py TABLES` | **只加 P3 三张新闻表** | `news_articles` / `news_associations` / `news_legacy_event_map` 加入 `TABLES`，为 N3 后的 restore 保住 canonical 数据；本批明确授权扩大备份契约 | `strategy_events` **不加入**（append-only 观察读模型，丢失由 reconciler + `observation_gap` 覆盖）；`052_order_events` / `055_regime_labels` 既有缺口仍归独立运维轮，不在本批补 |
| 5 | 迁移号分配 | **实现时取当时空号**，本节不预占 | 先落地的批次取当时的下一空号；同批加入 `.sql` + `pyproject.toml` force-include + 同步 `len(migrations)` / 末位断言 | 不预分配 `056` / `057`；不保留连续号段；实现批次须**同步而非删除**至少 6 处断言（§20.4，D108 收窄先例） |

### 21.3 第 1 条是显式修订，不是默认通过

§20.8 规定「若操作者认为其中某条实为决策，需另行裁定而非默认通过」。第 1 条改变了 §6.2 / Q5 已裁定的日界，因此按**显式修订**登记，不按实现口径静默默认。历史 verbatim 行一律保留，下表是唯一权威覆盖表：

| 位置 | 历史原文关键词 | §21 覆盖 |
|---|---|---|
| §6.2 identity 代码块 | `published_day = effective_ts converted to America/New_York calendar day` | 读作 **`UTC calendar day`**；实现用 `ts AT TIME ZONE 'UTC'` 取日期，避免 DB session `TimeZone` 漂移 |
| §6.2 bullet「跨 ET 日」 | 跨 ET 日的同标题不自动合并，进入 collision report | 读作「跨 UTC 日」；机制不变，仍进 collision report |
| §20.1 三处初始写入行 | `§6.2 的 effective_ts + day_precision 与 ET 日界必须逐 writer 落地` | 日界改为 UTC；`effective_ts + day_precision` 区分 provider / `ingest_fallback` 的口径不变，fallback 仍不得伪装成 provider 时间 |
| §20.1 真实库快照结论 | `distinct (UTC day, lower)` 与 ET 日界「两者可能不同 —— 必须列为 fixture」 | **该跨时区 fixture 要求撤销**：identity 日界与快照日界现在同为 UTC，1,873 直接对齐。但该数字仍是**推算而非验收阈值**，事务内 dry-run 重算要求不变 |
| §20.2 identity v1 第 2 条 | `published_day = America/New_York 日界` | 读作 UTC 日界；其余全部不变 |
| §20.7 P3 fixture 清单 | `ET 日界 / provider 缺失 / 跨日同标题 / 手工 URL fixture` | ET 日界 fixture 改为 UTC；其余三项不变 |
| §20.8 第 1 条 | 确认沿用 §6.2 的 `America/New_York` | 裁定为 UTC；§20.8 原文保留，本节是批准记录 |

**Q5 裁定主体不变**：identity = published day + normalized title；`normalized_title = title.lower()`；不引入 NFKC / 空白折叠；symbol 移出 article identity；URL 只作证据、不做语义强合并；不做「AI 主题相似所以合并」；所有规则带 `identity_version`；§6.3 N1–N4 分阶段迁移与六项对账不变。

**解析入口额外检查（唯一新增要求）**：`006_news_events.sql` 的 `ts` 是 `timestamptz`，PG 存的已是 UTC instant，不保留来源时区。因此不新增任何时区列；只在解析入口保证每个 `when` 是 tz-aware 并以 `.astimezone(UTC)` 正确换算后写入，naive datetime 不得静默 `replace(tzinfo=UTC)`（当前 `parse_yahoo_rss()` 的 `parsed.replace(tzinfo=UTC)` 分支即需显式审视）。三处写入全部覆盖：`news.py:515 _insert_event()`（`datetime.now(UTC)`）、`:547 _save_headlines()`（provider `when`：Yahoo RFC-822 offset 或 `_finviz_stamp()` 的 `tzinfo=ET`，`ET = ZoneInfo("America/New_York")`）、`:124 store_manual()`（`datetime.now(UTC)`）。来源 offset / ET 由 Python datetime 换算成 UTC instant，**不需要存来源时区**。

### 21.4 未决与门禁

- Q7–Q12 / Q14 共 7 条仍待裁定（P4–P6、Strategy 3、评估协议）；Q14 须在看 holdout 结果**之前**冻结。
- 两个启动阻塞（`run_loop()` 的 `load_active()` 早于 `apply_migrations()`；`var/backups` 996 快照 prune）属独立运维轮。
- 7799 console 仍关闭、IB Gateway `7497 / 4001 / 4002` 仍关闭；D111 / P1 代码需未来安全重启窗口，本批不重启、不重试被取消的 `dxnrJP`。
- P2 / P3 实现批次仍须各自：事务内 dry-run、mutation trap、真实 PostgreSQL 只读验证、`git diff --check`、台账机检、独立 commit / rollback。§21 不替代任何一项。

### 21.5 验证、回滚与关联

- 上游：`C-20261124-11`（§16 规划）、`C-20261125-1`（§17/§18 P1 交付）、`C-20261125-2`（§19 Round A）、`C-20261125-3`（§20 契约）。
- 本批验证：`unittest discover` 全量、`git diff --check`、台账机检、缺失索引 mutation trap、本地链接与来源路径检查；零代码改动故无真实库写入。实测结果见 `logs/2026-11-25.md` `C-20261125-4` 验证块。
- 回滚：`git revert <sha>`；只回滚 §21、头部第四笔状态推进、§6.2 窄修订注记、00 册索引、`CHANGELOG.md` 与台账条目。历史 verbatim 行不受影响。
