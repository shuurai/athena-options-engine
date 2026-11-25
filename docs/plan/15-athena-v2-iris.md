# 15 — Athena v2 / Iris（信号、策略与行动的可观测架构）

**状态**：Planning / 待裁定；本册只合并现状证据、目标契约与实施顺序，不授权实现。
**证据截点**：2026-11-24，代码基线 `56739ce`；数据库只读快照见 §2.3。
> **状态推进（2026-11-25）**：§17 授权的 **V2-P1 静态 Iris ＋ Orchestration 独立双列已交付并验证**，交付记录见 §18；V2-P2–P7 与 V2-Q1–Q15 仍待裁定。上行「不授权实现」原句保留为 `C-20261124-11` 规划批次的历史状态，不改写。
> **状态推进（2026-11-25，第二笔）**：**V2-Q Round A 已裁定 8 条**（Q1/Q2/Q3/Q4/Q5/Q6/Q13/Q15），记录见 §19；Q7–Q12、Q14 共 7 条仍待裁定。上行「V2-Q1–Q15 仍待裁定」是第一笔当时的状态，保留不改写。
> **状态推进（2026-11-25，第三笔）**：操作者确认 §19 后，**V2-P2 / V2-P3 落地前契约已写入 §20**（`C-20261125-3`）。§20 本身零代码、零迁移、不预占迁移号，仍需操作者批准后才进入实现批次；Q7–Q12 / Q14 仍未裁定。
> **状态推进（2026-11-25，第四笔）**：操作者逐项确认 §20.8 五条实现口径后，**§20 获批**，记录见 §21（`C-20261125-4`）。其中第 1 条由 `America/New_York` **显式窄修订为 UTC 日界**（Q5 identity 公式本身不变）；§6.2 / §19 / §20 历史 verbatim 行一律保留，只追加状态推进。**§20 获批 ≠ 授权实现**：P2 / P3 仍须各自独立实现批次、独立 commit / rollback；Q7–Q12 / Q14 仍未裁定。
> **状态推进（2026-11-25，第五笔）**：操作者指令「stop asking me with choices … just give me one sequential order and get them done」，据此**一次性裁定剩余 7 条 Q7–Q12 / Q14**（§22，`C-20261125-5`），Q14 的 `eval-v1` 在看任何 holdout 结果前先验冻结。**V2-Q1–Q15 全部 15 条已裁定完毕**。此后按固定顺序实施 P2 → P3 → P4 → P5 → P6 → P7，每批独立验证与提交；P5 数据不足则 `no-go` 且不自动进入 P6。上行第三／四笔「Q7–Q12 / Q14 仍未裁定」是当时状态，保留不改写。
> **状态推进（2026-11-25，第六笔）**：固定顺序的 **Step 2 / V2-P2 已实现、验证并独立交付**，记录见 §23（`C-20261125-6`）。`strategy_events` 窄表（migration `056`）、post-commit 幂等 emit、独立 `/api/iris/events` SSE、cursor replay → live 交接、`observation_gap`、真值动画与 60s reconciler 全部落地；全量 `879/879`（2 env skips）、`27/27` mutation traps、真实 PostgreSQL apply + 事务内 rollback、wheel 52 迁移内含 `056`。**P3–P7 仍未实现**；Q1–Q15 已裁定完毕不代表实现完毕。上行「每批独立验证与提交」是本批遵循的口径。
> **状态推进（2026-11-25，第七笔）**：固定顺序的 **Step 3 / V2-P3 第一批（§6.3 N1 additive + N2 dual-write）已实现、验证并独立交付**，记录见 §24（`C-20261125-7`）。migration `057` 建 `news_articles / news_article_symbols / news_legacy_event_map` 三表；identity v1 按 §21.3 **UTC 日界**落地，解析入口 tz-aware → `.astimezone(UTC)` 校验、naive 不伪装 UTC；三处初始 writer + DSH sentiment + dismiss 双写；dismiss 明确 `association_id` 且一对多拒绝；三表加入 `backup.py TABLES`，`strategy_events` 排除。全量 `919/919`、`20/20` mutation traps、真实 PG apply + rollback、wheel 53 迁移。**§6.3 N3 canonical read cutover 与 N4 legacy retirement 仍是受六项对账硬门禁约束的后续阶段**，本批未切换 reader、未删 `news_events`；P4–P7 未开始。
> **状态推进（2026-11-25，第八笔）**：固定顺序的 **Step 3 收尾 / V2-P3 第二批（N1 backfill ＋ §6.3 六项对账）已实现、验证并独立交付**，记录见 §25（`C-20261125-8`）。新增 `news_backfill.py`（幂等、可重导、`ingest_fallback` 诚实标注）与 `news_reconcile.py`（`six_item_gate()`，把差异机械分为 registered / unexplained）。真实库事务内 dry-run：**2,529 legacy rows → 1,873 articles / 2,529 associations / 2,529 map**，六项 **全 PASS、unexplained = 0、`blocks_n3 = False`**，二次 backfill `maps_inserted = 0`，`ROLLBACK` 后 legacy 仍 `2529`、canonical 归零。全量 `959/959`、`40/40` mutation traps、wheel 含两个新模块。**本批只证明「六项对账通过」，不等于 N3 已切换**：reader 仍读 `news_events`、`news_events` 未删（N4 另需裁定），且 live 库的 canonical 表仍是空的（对账是 dry-run，不是持久回填）。上行第七笔「N3 / N4 仍是受六项对账硬门禁约束的后续阶段」依然成立，本批只解除了门禁本身；§24.6「未跑 news backfill」一句由本节取代，原句保留不改写。
> **状态推进（2026-11-25，第九笔）**：固定顺序的 **Step 3 / V2-P3 第三批（N3 canonical read model）已实现、验证并独立交付，记录见 §27（`C-20261125-9`）**。新增 `news_read.py`：以 `WITH news_events AS (...)` CTE 把 `057` 三表投影为 legacy 九列，17 处非测试 reader 全部经 `render()` / `score_sql()` 包装、SQL 主体逐字节不变；R1 保留 ET 日界、R2 只对 canonical `news_score` 加 `ORDER BY ts, sentiment` tie-break。**本批是 DARK LAUNCH，`SOURCE = LEGACY`**：§26.2 两项数据面前置条件未满足——§25 六项对账是事务内 dry-run（`ROLLBACK` 后 live canonical 仍为 `0/0/0`），§26.4 禁止启动 engine 观察运行期 N2 双写收敛；直接切 `CANONICAL` 会让 17 处 reader 全部读到空表。故零行为变化、零持久写入。真实 PG **17/17 逐处对账 `parity diffs = 0`**、六项门禁 `unexplained=0 / blocks_n3=False`、全量 `1006/1006`、`19/19` mutation traps、wheel 含新模块且 53 迁移。**N3 真正激活（持久 backfill ＋ `SOURCE = CANONICAL` ＋ 运行期收敛确认）须操作者单独 go/no-go**；N4 另需裁定；P4–P7 未开始。上行第八笔「live 库 canonical 仍是空的」依然成立，原句保留不改写。
> **状态推进（2026-11-25，第十笔）**：操作者裁定 **N3 激活 defer**（Task #123 open），固定顺序进入 **Step 4 / V2-P4**。落地前契约 §28（`C-20261125-10`，R1–R7）与交付记录 §29（`C-20261125-11`）先后入库：四个 pure 模块（ET 会话日历、六类日线缺口审计、ISO 周聚合、Strategy 3 shadow 信号子集）＋ Iris 只读接线，§22.3 第 1–6/14 项全部落地、第 7–13 项按 R6 留给 P5/P6。**零迁移、零持久写入、零 drafts / orders**；live `market_bars` 为空 ⇒ 数据面诚实 `unknown / no_data`，数据到位即自动产出真实值。全量 `1056/1056`、`27/27` mutation traps、真实 PG 事务内只读 + `ROLLBACK`、wheel 含四新模块。**P4 shadow 不等于 Strategy 3 可交易**：工具 / 入场窗口 / TP / NAV / 连止均未实现，`instrument_availability=deferred_p5`；P5（`eval-v1` 执行）以真实历史数据可用性为硬门禁。上行第九笔「N3 激活须操作者单独 go/no-go」依然成立。
> **状态推进（2026-11-25，第十一笔）**：固定顺序 **Step 5 / V2-P5（`eval-v1` 执行）已按 §22.5 硬门禁实测并裁定**，记录见 §30（`C-20261125-12`）。只读实测：live 库 `market_bars=0`、`sim_bars=0`、`calendar_marks` 54 条仅 FED（无 EARNINGS）、`iv_marks/chain_marks=0`、`drafts/orders=0` ⇒ §22.4 最低证据量四项**全部不达标**（52W 数据覆盖率 0%、holdout 完成周 0、有效 candidate 0、覆盖 symbol 0），bootstrap 区间不可计算。**裁定：`insufficient_evidence / no-go / 继续 shadow`**——这正是 §22.5 预言的默认路径；`eval-v1` 阈值零回改，holdout 未被触碰（无从触碰）。**P6 不自动启动**（仅 P5=go 才进入）；P7 不受 P5 no-go 阻塞，按固定顺序继续。本批 doc-only：零代码、零迁移、零持久写入。再运行前置条件见 §30.5。上行第十笔「P4 shadow 不等于 Strategy 3 可交易」依然成立。
> **状态推进（2026-11-25，第十二笔）**：固定顺序 **Step 6 跳过（P6 不启动，仅 P5=go 进入）→ Step 7 / V2-P7 已实现、验证并独立交付**，记录见 §32（`C-20261125-14`）。函数级死代码清理（6 个函数 + 1 处冗余调用，共 150 行：`_contract_line` / `pages._secret` / `cockpit._money` / `note_step` / `note_cycle` / `_audit_result_tag` / `_advisor_actions_table` ＋ `_decimal_or_none` ＋ 不可达包装 `_portfolio_analysis_page`）＋ 文档收口；实现期纠正一处契约口径（`.advisor-reason` CSS 为 live 代码，保留）。**固定顺序 `Q7–Q12/Q14 → P2 → P3 → P4 → P5 → (P6 跳过) → P7` 全部走完**。全量 1055/1055（−1 个仅钉住已删包装的测试）、零迁移（仍 53）、零 news SQL 变更、零 `SOURCE` 变更。**仍 open 的独立裁定：N3 激活 go/no-go（Task #123）、N4、§30.5 五项数据批次**。上行第十一笔「P6 不自动启动」已由本批执行为「跳过」。
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

---

## 22. V2-Q Round B 裁定记录（Q7–Q12 / Q14，2026-11-25，`C-20261125-5`）

### 22.1 状态与编号纪律

操作者指令：「stop asking me with choices on what to do first … just give me one sequential order and get them done」。本批据此**一次性裁定剩余 7 条**，不再逐条追问。裁定全部采用 §13「推荐」列与 §8.3–§8.5 已写明的口径，**7 条零偏离、零跳过**，不引入本册之外的新方向。

- `V2-Q*` 仍是册内标识，**不是全局 D 决策**；D1–D111 / 112 节点 / 54 条裁定不变；不预占迁移号；不改 §1。
- **本批零代码、零迁移、零 DB 写入、零 engine 重启**，只写裁定文档与台账。
- **Q14 必须在查看 holdout 结果前冻结**（§8.5、§19.4）。本批在**尚无任何历史 / holdout 结果**时冻结 §22.4 协议，这正是反 look-ahead 纪律要求的时点；§19.4「现在裁定等于凭空猜阈值」的顾虑由「先验冻结、事后不得回改」化解，不构成推迟理由。
- 裁定 ≠ 实现授权。P4–P7 仍须按 §10.1 门禁各自独立批次、独立 commit / rollback。

### 22.2 七条裁定

| V2-Q | 问题 | 裁定 | 来源 |
|---|---|---|---|
| **Q7** | 52W 完整性是否引入正式交易日历 | ✅ **引入**。以交易所交易日（ET 会话日）为唯一权威日历；先测量缺口再实现；缺口分六类 `internal_gap` / `long_tail_gap` / `ipo_short_history` / `halt` / `zero_volume` / `corporate_action`，各带 `reason_code`；Iris 不得把 <252 行直接显示为 `complete`，须显示 `partial` / `unknown` 并给出缺口计数 | §13 推荐「是；先测量缺口」、§8.4 |
| **Q8** | 周线 corporate action / 复权口径 | ✅ **唯一口径：复权后收盘序列**（split / dividend adjusted），全文一致，不混用 raw 与 adjusted；每条 shadow 输出带 `adjustment_basis`；周聚合 open=首个完成日、high/low=周内极值、close=最后完成日、volume=求和；未完成本周绝不产生 candidate；零 look-ahead；fixture 覆盖普通周 / 假日短周 / 跨月跨年 / 缺周 / 未完成周 / 拆分 / 分红 / 复权源缺失 | §13 推荐、§8.4 |
| **Q9** | §8.3 十四项语义 | ✅ **逐项冻结**，见 §22.3；十四项全部给出唯一可复算定义，不留「由 LLM 判断」或「待定」 | §13 推荐、§8.3 |
| **Q10** | 首选工具与 DTE / strikes / width | ✅ **capped-risk put spread** 为唯一默认；DTE / strikes / width 按 §22.3 第 8 项冻结为**shadow 评估用先验值**，不得在 P5 go 之前用于任何 ticket；short stock / long put 保留为候选但须 §11.1 显式风险扩展，本册不默认启用 | §13 推荐、§11.1 |
| **Q11** | exit 是否每次独立 Approve | ✅ **每次独立 Approve**，沿用现有 `decide()` + `all_orders` gate；TP1 / TP2 / SL / Time Stop 各自一张 exit draft；同时触发按 §22.3 第 11 项优先级串行，但审批粒度不合并；任何自动退出属另开 privilege 裁定，本册不授权 | §13 推荐、§11.5、V2-I5 |
| **Q12** | manual / liquidate 是否纳入 pre-trade drafts | ✅ **不改旁路**。仅要求在 Iris 显式标注 `source=manual\|liquidate\|external`，并从策略胜率统计中剔除；改变现有 `EXTERNAL` / `MANUAL` 插入即 approved 的行为属另开安全裁定，本册不授权 | §13 推荐、§11.6、§20.1 |
| **Q14** | 评估协议 / 最低证据量 / go-no-go | ✅ **§22.4 协议 `eval-v1` 先验冻结**：时间顺序切分、survivorship、成本 / 滑点 / borrow / bid-ask / 部分成交模型、简单基准、反例留存、最低候选数与覆盖率、go / no-go 阈值全部在看 holdout **之前**定死；`insufficient_evidence` 一律 `no-go`；holdout 后不得回改阈值再称同一次验证 | §13 推荐、§8.5 |

### 22.3 Q9 十四项语义冻结（shadow 用，可复算）

| # | 语义空洞 | 冻结定义 |
|---|---|---|
| 1 | hill 6–10 周 | 含信号周；`hill_weeks` = `SMA20 > SMA50` 的连续完成周数，6 ≤ n ≤ 10；等值（`SMA20 == SMA50`）**中断**连续计数；必须有局部峰 `hill_peak = max(SMA20 − SMA50)` 且峰不在最后一周 |
| 2 | SMA20 slope | 用最近 4 个完成周的 `SMA20` 做最小二乘斜率，归一化为 `slope / SMA20`；`flat` 当且仅当 \|归一化斜率\| < 0.002 / 周；`declining` 为 < −0.002；带 `factor_version` |
| 3 | 触碰 SMA20 | 容差 0.5%：`low ≤ SMA20 × 1.005` 且 `close ≥ SMA20 × 0.995` 视为触碰；以 OHLC 相交为准，不只看 close；首次回踩重置：新信号周开始后，第一次触碰即固定 `retest_state`，同周内后续触碰不重算 |
| 4 | 蜡烛 / 量能真值表 | 条件 4 = `bearish_candle OR long_upper_shadow OR low_volume` 三者 **OR**；`low_volume` 同时是**额外硬过滤**（缩量必须满足，否则 `filtered`）；长上影 = 上影 ≥ 实体 2 倍且 ≥ 全周振幅 40%；量能基准 = 前 8 完成周平均 volume，窗口固定 8 |
| 5 | SPX / VIX 过滤 | SPX 强势 = SPX 收盘 > 其 SMA50 且 SMA50 斜率 ≥ 0；VIX < 15 **跳过**（不减半），15 ≤ VIX < 20 为 `caution` 且风险预算减半（**风险预算**，非名义仓位，§11.4），VIX ≥ 20 正常；边界值归低档 |
| 6 | 资格与财报 | 3–6 个月上涨 50%+ 是**偏好**不是硬资格；「日均成交量 > 5 亿美元」明确为**日均成交额**、窗口 20 个完成交易日；「财报前 5 天」用**交易日**、时间源为 `calendar_marks`，盘后财报归属**次日**交易日 |
| 7 | 入场窗口 | 信号周完成后次周 regular session 开盘满 30 分钟起，至该周收盘为有效窗口；急跌 = 开盘 30 分钟内跌幅 > 3% 则该周作废；order type = limit，TTL = 当日收盘；同一信号幂等 |
| 8 | 工具参数（Q10） | put spread：DTE 30–45 日历日；short strike 取 0.25 delta 最近档（无 delta 数据时以 −7% OTM 代理并标 `proxy`）；width = 标的价格 5% 四舍五入到最近 $5 档；max loss = width − credit；合约数由 1% NAV 风险反推并向下取整 |
| 9 | TP 的 SMA50 | 入场时**冻结** SMA50 值，持有期不随周更新；以**周收盘**确认，不看盘中；TP1 = 跌破冻结 SMA50 下方 3% 平 50%，TP2 = 下方 5% 平余仓；−2.99% / −3% / −5% 边界单列 fixture |
| 10 | 持仓时长 | 「2–4 周」与「最长 2 周」冲突裁定为**最长 2 个完成周**；第 2 周收盘全平（不减半） |
| 11 | 触发优先级 | 同周多触发：`SL > Time Stop > TP2 > TP1`；高优先级执行后低优先级当周不再生成；每次 exit 独立 Approve（Q11） |
| 12 | 1% NAV 风险 | 三种工具统一按**最大确定损失**计：put spread = width − credit；long put = 权利金；short stock = 按入场价 10% 名义止损估算并另记 borrow / 召回；一律计入组合预算，多笔单独合格但合计超限即 `blocked` |
| 13 | 连续 3 次止损 | 按**全策略**累计（非单 symbol）；自然月以 ET 日历月计；仅 `SL` 触发算 stop，TP / Time Stop 不算；当月第 3 次 SL 后停止新开至月末 |
| 14 | 周线复权 | 与 Q8 同一口径：复权后序列，`adjustment_basis` 随每条输出；复权源缺失该 symbol 该周标 `adjustment_unavailable` 且 `outcome=no_data`，绝不静默用未复权价 |

### 22.4 Q14 评估协议 `eval-v1`（先验冻结，零 holdout 数据）

| 项 | 冻结值 |
|---|---|
| 版本 / 时点 | `protocol_version = eval-v1`；冻结于 `2026-11-25`，**早于任何历史或 holdout 结果**；此后不得回改阈值 |
| 数据切分 | 按 `as_of_week` **严格时间先后**切分：最早 60% 完成周 = calibration，其后 40% = holdout；禁止随机打散、禁止跨边界调参 |
| Universe | 每个历史时点的成员快照；退市标的保留至退市周并显式标记，禁止 survivorship 剔除；universe 定义带版本 |
| 冻结模型 | 信号 / 候选 / 入场窗口 / 成本 / 滑点 / borrow / option bid-ask / 部分成交模型全部按 §22.3 与固定参数表冻结；任何改动须新开 `eval-v2` 并**重新跑全链**，不得复用旧 holdout |
| 基准 | 至少一个简单可复算基准：同期 SPY buy-and-hold 净成本后收益；运行前登记 |
| 反例留存 | 保留所有 `filtered` / `invalid` / `no_data` / 亏损 candidate 与完整 `reason_codes[]`，不得只展示盈利候选 |
| 最低证据量 | holdout 完成周 ≥ 30 且有效 candidate ≥ 20 且覆盖 symbol ≥ 5；52W 数据覆盖率 ≥ 80%；bootstrap 95% 区间可计算。任一不满足 ⇒ `insufficient_evidence / no-go`，**继续 shadow**，不是「通过」 |
| go 阈值 | 净成本后 holdout 收益相对基准的超额 **bootstrap 95% 下界 > 0**，且单笔最大损失 ≤ 1% NAV、组合预算无超限、live shadow 可执行；四条全满足才 go |
| no-go 记录 | 同时记录净成本结果、最大风险暴露、数据覆盖、样本外表现、反例清单与 live shadow 可执行性；原文 60–70% 胜率 / 2:1–3:1 盈亏比 / 每年 5–15 次**不作为预定答案** |
| live shadow | 只写 audit / Iris evidence；即使出现 candidate 也**零 drafts、零 orders** |

### 22.5 对后续阶段的影响与硬门禁

- **P4 现在可写落地前契约并实施**：Q7 / Q8 / Q9 子集已冻结；工具 / 订单 / exit 执行语义仍留 P5 / P6，但 hill / slope / retest / volume / calendar / 周线口径不得留白。
- **P5 现在可冻结协议并实施**：`eval-v1` 已冻结；但**真实历史数据可用性是硬门禁** —— 当前 smoke 库 drafts / orders 为空、IB Gateway `7497/4001/4002` 关闭、无 VIX 历史，若 §22.4 最低证据量不达标，结论必须写 `insufficient_evidence / no-go / 继续 shadow`，**不得判 go、不得事后调阈值**。
- **P6 仅在 P5 = go 时进入**；按当前数据条件默认路径是 no-go，故 P6 **不自动启动**，不因本批裁定而绕过。
- **P7** 依赖 P1 + P3 完成；P5 no-go 不阻塞其余清理。
- 两个启动阻塞（`load_active()` 早于 `apply_migrations()`、`var/backups` 996 快照 prune）仍属独立运维轮；7799 console 仍关闭，本批不重启、不重试被取消的 `dxnrJP`。

### 22.6 验证、回滚与关联

- 上游：`C-20261124-11`（§16）、`C-20261125-1`（§18）、`-2`（§19）、`-3`（§20）、`-4`（§21）。
- 本批验证：`unittest discover` 全量、`git diff --check`、台账机检、缺失索引 mutation trap、本地链接与来源路径检查；零代码改动故无真实库写入。实测见 `logs/2026-11-25.md` `C-20261125-5` 验证块。
- 回滚：`git revert <sha>`；只回滚 §22、头部第五笔状态推进、00 册索引、`CHANGELOG.md` 与台账条目；历史 verbatim 行不受影响。

---

## 23. V2-P2 交付记录（2026-11-25，`C-20261125-6`）

### 23.1 状态与授权边界

- 操作者指令「stop asking me with choices … just give me one sequential order and get them done」确立固定顺序 `Q7–Q12/Q14 → P2 → P3 → P4 → P5 → P6 → P7`。本批是其中 **Step 2 / V2-P2 的独立实现批次**，按 §20.7 独立 commit、独立 rollback，与 P3 分开。
- 授权来源：§20（已批准，`C-20261125-4`）、§21.2 五项实现口径、§19 的 Q4 / Q15、§22 的 Q12（operator direct 标注口径）。**Q7–Q12 / Q14 已在 §22 裁定完毕**，本批不重新裁定。
- 编号纪律：本批仍是册内 `V2-*`，**不分配全局 D 号**；`D1–D111 / 112 节点 / 54 条裁定` 不变；§1 / §6 零修订；§14 / §17 / §18 / §19 / §20 / §21 / §22 历史 verbatim 行一律保留，只追加状态推进。
- 运行时：本批**未重启 7799 console、未重试被取消的 `dxnrJP`、未启动任何 engine 进程**；IB Gateway `7497 / 4001 / 4002` 仍关闭。`athena-pg` 是本批唯一 schema 变更：`51 → 52` migrations，仅新增 `056_strategy_events.sql`。验证数据写入使用隔离账号 `ZZP2TEST` 并在事务内 `ROLLBACK`，业务表零写入。

### 23.2 实现清单

| 文件 | 变更 |
|---|---|
| `packages/store/src/athena_store/migrations/056_strategy_events.sql` | 新增 append-only 窄表 `strategy_events`，15 列、`event_id` 主键、3 个索引；注释内不含分号（`migrate.py` 按 `;` 切句，R7 既有坑） |
| `packages/store/src/athena_store/strategy_events.py` | 新增：21 项词表、`PAYLOAD_KEYS` 结构化白名单、确定性 `event_id()`、`validate_event()`、`build_event()`、`emit_after_commit()`、`emit_observation_gap()`、`replay()` / `UnknownCursor`、`observe_event()`、`reconcile_drafts()`、观察者注册表 `add_observer()` / `_notify()` |
| `packages/store/pyproject.toml` | force-include 新增 `056_strategy_events.sql`（wheel 实测 52 迁移） |
| `packages/strategy-nfr/src/athena_strategy/drafts.py` | 新增 `_observe()` / `_observe_orders()`；在 `expire_pending()` / `apply_ticket_block()` / `_insert_draft()` / `decide()` / `use_link()` 五处 **domain `commit()` 之后**幂等 emit；失败完全内含，零 domain 影响 |
| `apps/orchestrator/src/athena_orchestrator/iris_events.py` | 新增独立 live fan-out 注册表（`publish` / `subscribe` / `recent` / `attach` / `detach`），与 `runtime._TRAIL` / `/api/logs` 完全分离 |
| `apps/orchestrator/src/athena_orchestrator/iris_reconcile.py` | 新增 60s daemon，只从 `drafts` 持久化行补齐**可推导**事件；`run_once()` / `accounts()` 可单测 |
| `apps/orchestrator/src/athena_orchestrator/console.py` | 新增 `/api/iris/events` 路由、`_iris_frame()`、`_send_iris_events()`、`_write_iris_frame()`；CSS 有限单次 pulse + `prefers-reduced-motion`；客户端 `bootIrisEvents()` 真值动画；启动时 `iris_events.attach()` |
| `apps/orchestrator/src/athena_orchestrator/iris_view.py` | `GRAPH_VERSION = iris-p2-events-v1`、真值动画文案、`data-iris-live="idle"` 状态节点；模块仍为纯只读快照 |
| `apps/orchestrator/src/athena_orchestrator/pages.py` | `_iris_page()` chip `EVENTS P2`、lead「Connectors move only on real run events」 |
| `apps/orchestrator/src/athena_orchestrator/loop.py` | 启动 / 停止 `iris_reconcile` daemon（两处 shutdown 路径） |
| `apps/orchestrator/tests/test_iris_p1.py` | 同步被 P2 supersede 的 P1 断言（graph version、文案、CSS、chip） |
| `apps/orchestrator/tests/test_iris_p2.py` | **新增 69 项** P2 单测：identity / payload / Q15 / replay / reconciler / registry / frame / migration / console surface / 域接线 / §20.2.7 fixture |
| 8 处历史断言 | `test_d111_vix` / `test_guardian_r3` / `r4` / `r5` / `r6` / `r7` / `test_guidance_basket` / `test_settings_layout` 同步 `51 → 52` 与 `056` 归属，**同步而非删除**（§20.4，D108 先例） |

### 23.3 §20.2 契约逐条对照

| §20.2 | 落地 | 证据 |
|---|---|---|
| 1 新表 | `056_strategy_events.sql` 窄表、append-only、`event_id` 由 subject + transition + version 确定性生成；无万能 event bus、无可变状态列 | 真实 PG 15 列 / 4 索引；`INSERT_SQL` 唯一写入路径 |
| 2 词表 | 21 项 `EVENT_TYPES` 与 §20.2.2 逐字一致；`status / from_status / to_status / reason_code` 显式 | `test_event_types_match_the_ratified_vocabulary` |
| 3 emit 边界（Q15） | domain `commit()` **之后**调用；`ON CONFLICT (event_id) DO NOTHING` 幂等；`_observe()` 捕获一切异常；失败只记 `observation_gap`，绝不回滚或阻断 `drafts / approvals / orders / positions` | `test_event_write_failure_does_not_change_a_committed_domain_row`、`test_observe_swallows_event_write_failures`、真实 PG 重复插入 count 不变 |
| 4 reconciler | 60s daemon 只补 `draft_created` 与 6 类终态；**不臆造** `filtered / quiet / factor_computed`，补不出留 `observation_gap` | `iris_reconcile.py` docstring、`test_does_not_invent_events_for_unknown_status` |
| 5 SSE | 独立 `/api/iris/events`；`Last-Event-ID` / `cursor`；先 replay 再 live；`sent` 去重；未知 / 跨账号 cursor → `observation_gap`；每 5s 从 `strategy_events` 复读持久行以跨进程闭合 replay→live 交接；15s keepalive；`prefers-reduced-motion`、graph version mismatch、wrong account / symbol / edge 不动画 | `test_iris_p2.py` `TestConsoleSurface`、真实 PG `UNKNOWN_CURSOR TRAPPED` / `CROSS_ACCOUNT_CURSOR TRAPPED` |
| 6 operator direct | `EXTERNAL` / `MANUAL` 不投影为策略链路；且这两类 draft 由 `tasks.py` / `order_watch.py` 直接写库，本就不经过 `drafts.py` emit 锚点 | `test_operator_direct_tactics_are_not_a_strategy_chain` |
| 7 fixture | BUY / `OPEN_CSP` / `OPEN_CC` / `OPEN_STRANGLE` 四 tactic；rejected / expired / amended / replaced / blocked / partial-fill / cancel 七状态类；event-write failure；replay→live 交接；「无事件不动画」 | `TestP2Fixture`（`TACTICS` / `STATUSES` 表驱动） |

### 23.4 §20.4 / §21.2 迁移、打包与备份口径

- 按 §20.8 第 5 条「实现时取当时空号」，本批取 `056`；同批加入 `.sql` + `pyproject.toml` force-include + 同步 8 处断言。**不预占 `057`**（留给 P3）。
- wheel 实测：`uv build --package athena-store` → `MIGRATION_COUNT_IN_WHEEL = 52`、`056_IN_WHEEL = True`、末三条 `054 / 055 / 056`。
- 真实 PG `apply_migrations()` 只返回 `['056_strategy_events.sql']`；`schema_migrations` 末位为 `056`。
- **§21.2 第 4 条遵守**：`strategy_events` **不加入** `backup.py TABLES`（append-only 观察读模型，丢失由 reconciler + `observation_gap` 覆盖）；P3 三张新闻表留到 P3 批次加入；`052_order_events` / `055_regime_labels` 既有缺口仍归独立运维轮。
- **§21.2 第 2 条遵守**：`payload` 只有结构化标量；自由文本 `reason` 留在 `drafts.reason`；`validate_event()` 显式拒绝 `reason` / `reason_text` 键，`_iris_frame()` 二次拦截。
- **§21.2 第 3 条遵守**：不复用 `/api/logs` 的 `subscribe_trail()` / `trail_recent()`；`/api/logs`、`/api/pulse/ticks` 未改。

### 23.5 验证（实测）

```
apps.orchestrator.tests.test_iris_p2                 69/69 OK
unittest discover -p "test_*.py"                    879/879 OK (skipped=2)
                                                    基线 810 + 新增 69
mutation traps                                      27/27 TRAPPED, sha256 逐字节还原
compileall                                          OK
uv lock --check                                      Resolved 29 packages (uv.lock 零变动)
git diff --check                                     0
node scripts/check-changelog.mjs                     logs 13 / 条目 128 / 索引 128 ✓
LOCAL_LINK_AND_SOURCE_ERRORS                         0
隔离临时夹具 missing-index mutation                 MISSING_INDEX_MUTATION_TRAPPED

真实 PostgreSQL (athena-pg, 52 migrations)
  apply_migrations() -> ['056_strategy_events.sql']
  strategy_events: 15 columns / 4 indexes (pkey + occurred + account + subject)
  BEGIN (implicit) -> insert 4 events -> replay all = 4
  after cursor (2nd) = 2 rows ['d2','d3']
  duplicate INSERT (ON CONFLICT DO NOTHING) -> count stays 4
  UNKNOWN_CURSOR TRAPPED / CROSS_ACCOUNT_CURSOR TRAPPED
  ROLLBACK -> COUNT_AFTER_ROLLBACK = 0
wheel: athena_store-0.0.0-py3-none-any.whl -> 52 migrations, 056 present
```

### 23.6 明确不做 / 遗留

- **零 P3**：未建新闻三表、未跑 news backfill、未改任何 writer / reader、未落地 §21.3 的 UTC 解析入口校验（属 P3）。
- **零 P4–P6**：未实现周线 SMA、Strategy 3 shadow、`eval-v1` 执行、exit 语义、工具接入。
- **零业务写入**：验证用 `ZZP2TEST` 隔离账号且事务内 `ROLLBACK`；未写 `drafts / approvals / orders / positions`。
- **P2 边界**：`news_ingested / factor_computed / strategy_started / strategy_completed / filtered / quiet / invalid / draft_replaced` 当前无 domain 生产者，`strategy_events` 中这些词表项保持为空；这不是臆造，未覆盖区间一律 `observation_gap`。`draft_replaced` 在当前代码中无对应状态路径，reconciler 只在出现该状态时补。
- **无持久化 live replay 队列**（§21.2）：重连从 cursor 复读 `strategy_events`；未知 cursor 显式 `observation_gap`。
- 两个启动阻塞（`run_loop()` 的 `load_active()` 早于 `apply_migrations()`；`var/backups` 996 快照 prune）仍属独立运维轮。
- 7799 console 仍载入改动前代码；D111 自动 VIX backfill 与本批 SSE 需未来安全重启窗口。**本批不重启、不重试 `dxnrJP`**。

### 23.7 回滚

- `git revert <sha>`：只回滚本批新增 / 修改文件、§23、头部第六笔状态推进、00 册 15 号索引、`CHANGELOG.md` 与 `C-20261125-6` 台账条目；历史 verbatim 行不受影响。
- `056_strategy_events.sql` 是 additive；按 §20.7 可**单独停用 `/api/iris/events` 端点并保留空表**（append-only、无读取方即无害），也可单独 `DROP TABLE strategy_events` 并从 `schema_migrations` 撤号，不影响任何业务表。
- `strategy_events` 未入 `backup.py TABLES`，restore 后该表缺失由 reconciler + `observation_gap` 覆盖，符合 §21.2 裁定。
- P2 / P3 分开提交、分开回滚，避免一次 revert 同时抽掉事件与新闻两条链。

### 23.8 关联

- 上游：`C-20261124-11`（§16 规划）、`C-20261125-1`（§17/§18 P1）、`-2`（§19 Round A）、`-3`（§20 契约）、`-4`（§21 批准与五项口径）、`-5`（§22 Round B / `eval-v1`）。
- 下游：V2-P3（`§20.3`，下一批，取当时空号 `057`）、P4 / P5 / P6 / P7 按固定顺序继续；P5 数据不足则 `no-go` 且不自动进入 P6（§22.5）。

---

## 24. V2-P3 第一批交付记录（N1 additive + N2 dual-write，2026-11-25，`C-20261125-7`）

### 24.1 状态与授权边界

- 固定顺序 `Q7–Q12/Q14 → P2 → P3 → P4 → P5 → P6 → P7` 的 **Step 3 / V2-P3 第一批**。§6.3 明确把 P3 拆成 N1–N4 四个**分阶段、各有回滚**的阶段，本批实现 **N1（additive）＋ N2（same-transaction dual write）**，**不含 N3 canonical read cutover、不含 N4 legacy retirement**。按 §20.7 与 P2 分开 commit / rollback。
- 授权来源：§20.3、§21.2 第 4 条（`backup.py TABLES`）、§21.3（UTC 日界窄修订 + 解析入口额外检查）、Q5 / Q6。**Q7–Q12 / Q14 已在 §22 裁定完毕**，本批不重新裁定；不分配全局 D 号，`D1–D111 / 112 节点 / 54 条裁定`、§1 不变；§6.2 / §19 / §20 / §21 / §22 历史 verbatim 行保留。
- 运行时：**未重启 7799、未重试被取消的 `dxnrJP`、未启动任何 engine**；IB Gateway `7497/4001/4002` 仍关闭。`athena-pg` 本批唯一 schema 变更：`52 → 53` migrations，仅新增 `057_news_canonical.sql`。真实库验证全部在事务内 `ROLLBACK`，使用 `ZZP3*` 隔离 symbol，业务表零持久写入。

### 24.2 实现清单

| 文件 | 变更 |
|---|---|
| `migrations/057_news_canonical.sql` | 新增三表：`news_articles`（`UNIQUE (identity_version, identity_hash)`）、`news_article_symbols`（`UNIQUE (article_id, symbol)`）、`news_legacy_event_map`（`PRIMARY KEY (legacy_fingerprint, article_id)`）+ 4 索引；**不改 / 不删 `news_events`** |
| `packages/store/src/athena_store/news_canonical.py` | 新增 identity v1（UTC 日界、`title.lower()`、`article_key`、symbol 移出 identity）、`to_utc()` / `NaiveTimestamp`、`dual_write()`（savepoint 内含）、`resolve_association_id()` / `AmbiguousAssociation`、`mark_dismissed()` / `mark_sentiment()`、`collision_report()`、`reconcile()` 六项 |
| `packages/store/src/athena_store/backup.py` | `TABLES` 加入三张新闻表；**`strategy_events` 不加入**（§21.2 第 4 条） |
| `packages/store/pyproject.toml` | force-include 新增 `057_news_canonical.sql`（wheel 实测 53） |
| `packages/strategy-nfr/src/athena_strategy/news.py` | `parse_yahoo_rss()` 去掉 `parsed.replace(tzinfo=UTC)`，naive pubDate 回退 ingestion instant；`_insert_event()` / `_save_headlines()` / `store_manual()` 三处归一到 UTC 并 `dual_write`；`dismiss_event()` 支持显式 `association_id`、一对多拒绝 |
| `packages/strategy-nfr/src/athena_strategy/dsh.py` | sentiment enrich 双写 canonical association（失败内含、不影响 legacy UPDATE） |
| `apps/orchestrator/tests/test_news_p3.py` | **新增 40 项** P3 单测：identity / UTC 日界 / dual-write / dismiss / reconcile / collision / migration / wiring / §20.3.5 fixture |
| 8 处历史断言 | `52 → 53`、`05*.sql` 列表加 `057`；**同步而非删除** |

### 24.3 §20.3 契约逐条对照

| §20.3 | 落地 | 证据 |
|---|---|---|
| 1 三表 additive | `057` 三表按 §6.1 模型；`news_events` 结构与主键零改动、未删除；legacy map 不并入 association 主键 | `test_migration_creates_three_tables_and_keeps_legacy`、真实 PG 9/13/12 列 |
| 2 identity v1 | `normalized_title = title.lower()`（无 NFKC／空白折叠）、`published_day = UTC calendar day`、`article_key = sha256("v1|" + day + "|" + normalized_title)`、`identity_version = v1`；`effective_ts` 区分 `provider` / `ingest_fallback`、保留 `original_ts`；URL 只作证据；symbol 移出 identity | `TestIdentity`、ET `23:30 → 2026-11-26` fixture |
| 3 dual-write 三处 | `_insert_event` / `_save_headlines`（含 `store_manual`）/ `ingest_url` 走 `_insert_event`；每处记录 `inserted` / `conflict_existing`；canonical 写入**不依赖** legacy `ON CONFLICT DO NOTHING` 是否插入；DSH sentiment 双写 | `test_conflict_existing_does_not_skip_canonical`、`TestMigrationAndWiring` |
| 4 dismiss 语义 | 兼容入口解析明确 `association_id`；一个 fingerprint 映射多个 association ⇒ `AmbiguousAssociation` 且**在写 legacy 前拒绝**；禁止批量 dismiss | `TestDismiss`、真实 PG `AMBIGUOUS: TRAPPED` |
| 5 readers 对账 | `reconcile()` 提供七项可查询计数（覆盖 §6.3 六项）、`collision_report()` 同日同标题不同 URL；真实 PG 事务内 dry-run 已跑。**N3 切换前的冻结数据集逐 symbol `NEWS_WASH` / `news_score` 同 Decimal 路径重算留作 N3 批次硬门禁** | 真实 PG `RECONCILE` / `COLLISIONS` / `ORPHANS: 0` |
| 6 legacy 保留（Q6） | `news_events` 原表与全部 reader 均未改动；map 数按每 legacy row 逐行累计设计 | §24.5 遗留、真实 PG rollback 后 `map_rows = 0` 而 legacy `2529` 不变 |
| 7 不与 NFR 行为变更混批 | 未改 wheel gate / tag 阈值 / `NEWS_WASH` 语义；只新增 canonical 写入与解析入口校验 | 全量 919/919 零回归 |

### 24.4 §21.2 / §21.3 口径遵守

- **UTC 日界**：`published_day()` 用 `to_utc(when).date()`，不在 SQL 里用 session-local `::date`；DB 侧 `published_day date` 已是确定性日历日，避免 `TimeZone` 漂移。跨 UTC 日同标题不自动合并（fixture 已验证）。
- **解析入口唯一新增检查**：`to_utc()` 要求 tz-aware 并 `.astimezone(UTC)`；naive 抛 `NaiveTimestamp`，由 `_save_headlines` 回退到 ingestion instant 并标记 `day_precision="ingest_fallback"`，**绝不**静默 `replace(tzinfo=UTC)`。Yahoo RFC-822 offset 与 Finviz `tzinfo=ET` 均由 Python datetime 换算。三处 writer 全覆盖：`_insert_event`（`datetime.now(UTC)`，fallback）、`_save_headlines`（provider `when`）、`store_manual`（`datetime.now(UTC)` → 传 `ingest_fallback`）。
- **不新增时区列**：无 `provider_tz` / `published_local_day` / 其它证据列。
- **`backup.py TABLES`**：只加三张新闻表（现 23 张），`strategy_events` 排除；`052_order_events` / `055_regime_labels` 既有缺口仍归独立运维轮。
- **命名口径**：§21.2 第 4 条写作 `news_associations`，§6.1 与 §20.3 写作 `news_article_symbols`。本批按 **§6.1 模型定义与 §20.3 直接契约**采用 `news_article_symbols`，此处登记差异，不静默改写任何历史行。

### 24.5 验证（实测）

```
apps.orchestrator.tests.test_news_p3       40/40 OK
unittest discover -p "test_*.py"           919/919 OK (skipped=2)
                                             879 (P2 后) + 40 (新增 P3)
mutation traps                             20/20 TRAPPED, sha256 逐字节还原
compileall / uv lock --check / git diff     OK / 29 packages / 0
node scripts/check-changelog.mjs            logs 13 / 条目 129 / 索引 129 ✓
LOCAL_LINK_AND_SOURCE_ERRORS                0
隔离临时夹具 missing-index mutation        MISSING_INDEX_MUTATION_TRAPPED

真实 PostgreSQL (athena-pg, 53 migrations)
  apply_migrations() -> ['057_news_canonical.sql']
  news_articles 9 cols / news_article_symbols 13 cols / news_legacy_event_map 12 cols
  真实 news._insert_event() 双写 -> RECONCILE:
    legacy_rows=2531 map_rows=2 distinct_map=2 articles=1 associations=2
    orphan_associations=0 distinct_article_identities=1
  UTC_DAY_OF_ET_2330 = 2026-11-26
  RESOLVE_ONE = True / AMBIGUOUS = TRAPPED
  COLLISIONS = same UTC day + same title + 2 urls -> 1 report row
  ROLLBACK -> legacy_rows=2529, map/articles/associations = 0
wheel: athena_store-0.0.0-py3-none-any.whl -> 53 migrations, 057 present
```

### 24.6 明确不做 / 遗留（硬门禁）

- **N3 canonical read cutover 未执行**：NFR / News / Iris 仍读 `news_events`；未切 reader、未做「冻结数据集上逐 symbol `NEWS_WASH` 一致、`news_score` 同一 Decimal 路径重算一致」验证。按 §6.3，任何未登记差异都**阻止 N3**；本批 `reconcile()` / `collision_report()` 是 N3 的输入，不是 N3 的通过证明。
- **N4 legacy retirement 未执行**：`news_events` 保留且不删，删除须长期对账为零且另行裁定。
- **未跑 news backfill**：本批只做 additive 建表与 N2 双写，未把 2,529 行历史 `news_events` 回填进 canonical；§6.3 要求 backfill 的 map 数 == legacy 行数、每 legacy row 恰好映射一次，属 N3 前独立批次。
- **零 P4–P6**：周线 SMA、Strategy 3 shadow、`eval-v1` 执行、exit 语义、工具接入均未开始。
- 两个启动阻塞（`run_loop()` 的 `load_active()` 早于 `apply_migrations()`；`var/backups` 996 快照 prune）仍属独立运维轮。
- 7799 console 仍载入改动前代码；本批不重启、不重试 `dxnrJP`。

### 24.7 回滚

- `git revert <sha>`：只回滚 `057`、canonical 模块、三处 writer / dsh 双写、`backup.py TABLES`、§24、头部第七笔、00 册索引、`CHANGELOG.md` 与 `C-20261125-7`；历史 verbatim 行不受影响。
- **N1 回滚按 §6.3「删新表即可，旧数据不动」**：三张新表 additive，无外部 reader 依赖；`news_events` 完全不动。回滚后 writer 恢复纯 legacy 行为。
- `057` 已入 `backup.py TABLES`，restore 后 canonical 数据可保；但 N3 前 canonical 只作写入与对账，不是读取真源。
- P2 / P3 分开提交、分开回滚，一次 revert 不同时抽掉事件与新闻两条链。

### 24.8 关联

- 上游：`C-20261124-11`（§16 规划）、`C-20261125-1`～`-6`（P1 / Round A / §20 / §21 / Round B / P2）。
- 下游：**V2-P3 后续批次 = news backfill + 六项对账 + N3 canonical reader cutover**（N4 另需裁定）；随后 P4 / P5 / P6 / P7 按固定顺序继续；P5 数据不足则 `no-go` 且不自动进入 P6（§22.5）。

---

## 25. V2-P3 第二批交付记录（N1 backfill ＋ §6.3 六项对账，2026-11-25，`C-20261125-8`）

### 25.1 状态与授权边界

- 固定顺序 `Q7–Q12/Q14 → P2 → P3 → P4 → P5 → P6 → P7` 的 **Step 3 收尾**。本批实现 **N1 的历史数据回填**与 **§6.3 六项对账门禁**，**不含 N3 canonical read cutover、不含 N4 legacy retirement**。与 §24 分开 commit / rollback。
- 授权来源：**§6.3 六项**（本批逐条落地并留证）、Q6（legacy 至少跨 N1–N3 不删）、§20.3 第 5 / 6 条、§21.2 第 4 条。不分配全局 D 号，`D1–D111 / 112 节点 / 54 条裁定`、§1 不变；§6.2 / §19 / §20 / §21 / §22 / §24 历史 verbatim 行一律保留，只在本节追加。
- 运行时：**未重启 7799、未重试被取消的 `dxnrJP`、未启动任何 engine**；IB Gateway `7497/4001/4002` 仍关闭。本批**零 migration**（`athena-pg` 维持 `53`），`news_events` 零写入。真实库验证全部在事务内 `ROLLBACK`，业务表零持久写入。

### 25.2 实现清单

| 文件 | 变更 |
|---|---|
| `packages/store/src/athena_store/news_backfill.py` | 新增 `backfill(conn, limit=None)`：按 `fingerprint` 全序幂等回填；`_row_identity()` 复用 `nc.to_utc()` / `article_id()`；回填行一律标 `day_precision='ingest_fallback'`（legacy 无 provenance 列，不冒充 provider）；`INSERT_MAP_SQL` **只 insert 不 update**（审计锚点不可变）；`INSERT_ASSOCIATION_SQL` 对可变列 `source/url/event_type/sentiment/dismissed` 按 legacy **重导**、`effective_ts` 取 `LEAST`（与行序无关）；`weighted_news_score()` 与 `indicators.news_score` 同一 Decimal 循环；成对的 legacy / canonical reader SQL 及确定性 tie-break 变体 |
| `packages/store/src/athena_store/news_reconcile.py` | 新增 `six_item_gate(conn, *, as_of, window_days=3, sentiment_below=-0.3)`、六个 `item*` 函数、`classify_score_delta()`、`format_gate()`；**纯只读**（模块内零 `INSERT/UPDATE/DELETE/DROP/ALTER`）；输出 `registered` / `unexplained` 两桶，`blocks_n3 = bool(unexplained) or not all(ok)` |
| `apps/orchestrator/tests/test_news_p3_backfill.py` | **新增 40 项**：identity / UTC 日界 / naive 拒绝 / symbol 大写 / limit 门控 / 幂等 / 重导可变列 / map 不可变 / Decimal 路径 / tie 分类四分支 / 六项聚合 / 只读不变量 / reader SQL 与生产 `WHERE` 对齐 |

### 25.3 §6.3 六项逐条对照（真实库，事务内 dry-run）

| §6.3 | 判据 | 实测 |
|---|---|---|
| ① map 数与 legacy 行数一致、每 legacy row 恰好映射一次 | `legacy_rows == map_rows` 且每 `fingerprint` 恰一行 且无未映射 且无 orphan map | `2529 == 2529`，`multi_mapped=0`、`unmapped=0`、`orphan_map=0` |
| ② association 数 == 不同 `(article_id, symbol)` 数、无 orphan | 计数相等 + 无 orphan + map 无悬挂 `association_id` / `article_id` + 无无 map 的 association | `2529 == 2529`，`orphan=0`、`dangling_assoc=0`、`dangling_article=0`、`assoc_without_map=0` |
| ③ article 数只按已登记 identity 规则减少 | `articles == distinct identity_hash == distinct (published_day, normalized_title)`，且 `articles == legacy distinct (UTC day, lower(title))` | `1873 == 1873 == 1873 == 1873`，`collapsed=0`；相对 legacy 行数减少 `2529 − 1873 = 656` 全部来自 439 个跨 symbol 同 `(UTC 日, 标题)` 组 |
| ④ 初始 insert / DSH enrich / dismiss 后字段无未解释差异 | 不可变列经 **map** 对 legacy；可变列经 **association** 对 legacy | `source/url/event_type = 0/0/0`；`sentiment/dismissed = 0/0`；`duplicate (article,symbol) pairs = 0` |
| ⑤ 冻结数据集逐 symbol `NEWS_WASH` 一致、`news_score` 同一 Decimal 路径重算一致 | 37 个 symbol 逐一比对；分数差异必须可机械归因 | `NEWS_WASH` **37/37 零差异**；`news_score` 1 项差异（NVDA），归类 `tie_order_nondeterminism`（见 §25.4） |
| ⑥ ET 日界 / provider 缺失 / 跨日同标题 / legacy conflict / 同日同标题异 URL / 手工 URL 均有 fixture 与 collision report | 六族齐全并有实测计数 | `et_utc_boundary_rows=183`、`provider_ts_missing=2529`、`cross_day_same_title_groups=1`、`legacy_conflict_rows=0`、`collision_groups=16`、`manual_url_rows=0`（手工 URL 为合成 fixture，见 §25.6） |

**门禁结论：`blocks_n3 = False`（unexplained = 0，registered = 2）。**

### 25.4 ⑤ 的 NVDA 差异为何是「已登记」而不是「未解释」

这是本批唯一需要辩护的地方，必须机械证明而非口头断言。`indicators.news_score` 只按 `ts` 排序，**同一时间戳的行没有确定相对顺序**，而 recency 权重是位置权重（第 n 行权重 = n），因此同 ts 行的聚合结果不可复现。三项证据：

1. **无数据丢失**：两侧 `(ts, sentiment)` 多重集**完全相同**（`698 == 698`，逐元素相等）。
2. **Decimal 路径相同**：两侧同时改用 `(ts, sentiment)` 作 tie-break 后，分数**逐位相同**（`-0.09053301687633992072178429275`，同一字符串）。
3. **legacy 自身不一致**：同一 legacy 查询用 `ORDER BY ts` 与「`ORDER BY ts DESC` 再反转」得到**不同**结果（`-0.090555972305…` vs `-0.090558021897…`），证明差异源于 tie 顺序而非 canonical 建模。

NVDA 窗口内有 **26 个同 ts 组、最大 8 行同 ts**。叠加 §5 的既有结论——`news_score` **当前没有确定性 wheel / phase 消费者**（只有 `NEWS_WASH + RV_HIGH` 才进 `signaled_strangle` candidate）——该差异对任何决策路径的行为影响为零。因此归入 `registered`，`classify_score_delta()` 的三分支（`data_mismatch` / `decimal_path_mismatch` / `tie_order_nondeterminism`）保证：**多重集不同或确定性分数不同一律落进 `unexplained` 并阻止 N3**。本批不修改 `indicators.py`（§6.3 第 7 条：不与 NFR 行为变更混批）；N3 批次再决定 canonical reader 是否加确定性 tie-break。

### 25.5 collision report 抽样审查（§6.2 要求）

16 组「同 UTC 日 + 同 normalized title + 不同 URL」全部逐组看过真实 URL，结论：

- **同 URL 仅差追踪尾巴**：如 `.../alibaba-tencent-put-faith-chinese-124451275.html` 与 `...?tsrc=rss`（组 4/5/13/7/11/14）。
- **同一稿件多站转载**：`just-auto` / `yahoo`（组 1）、`thedailyupside` / `yahoo`（组 2/3）、`barrons` / `yahoo`（组 6）、`yahoo` / `proactiveinvestors`（组 8）、`stocktwits` / `yahoo`（组 9）、`yahoo` / `investors.com`（组 10）、`ibtimes` / `yahoo`（组 12）、`investorshub` / finviz 相对路径（组 15/16）。
- **无一组是「两篇不同报道被错误合并」**。按 Q5「URL 只作证据、不做语义强合并」的既定口径，**接受 identity v1 现状，不修订规则**；不引入 URL 归一化（会反向引入新的误合并风险，且属新裁定）。

### 25.6 §6.3 之外的工程决定

- **回填诚实标注**：legacy `ts` 是已存 UTC 瞬时，但表内无 provenance 列，故一律 `ingest_fallback`，原始值另存 `original_ts`。不把「猜测的 provider 时间」写成 provider。
- **回填可重导**：N1/N2 期 legacy 是真源，因此重跑时按 legacy **覆盖** association 的可变列。否则「回填 → DSH enrich → 再回填」会让 canonical 变旧并击穿 ④。`backfill()` 永不写 `news_events`（测试以 SQL 动词断言）。
- **map 不可变**：`ON CONFLICT DO NOTHING`，只做锚点，不随后续 enrich 漂移；④ 的可变列因此走 association 而非 map。
- **门禁只读**：`news_reconcile.py` 模块级零变更 SQL，用测试把 `INSERT/UPDATE/DELETE/DROP/TRUNCATE/ALTER` 全部扫掉，防止对账逻辑日后偷偷改写数据。
- **不新增迁移**：本批纯代码，`53` 不变，回滚无需 `DOWN`。

### 25.7 验证（实测）

```
apps.orchestrator.tests.test_news_p3_backfill   40/40 OK
apps.orchestrator.tests.test_news_p3            40/40 OK
unittest discover -p "test_*.py"               959/959 OK (skipped=2)
                                                 919 (P3-1 后) + 40 (本批)
mutation traps                                  20/20 TRAPPED, sha256 逐字节还原
compileall / uv lock --check / git diff --check  OK / 29 packages / clean
wheel athena_store-0.0.0-py3-none-any.whl       53 migrations (057 present)
                                                 news_backfill.py / news_reconcile.py / news_canonical.py 均在包内

真实 PostgreSQL (athena-pg, 53 migrations, 全程事务内 ROLLBACK)
  legacy rows (untouched)            2529
  backfill #1                        {legacy_rows: 2529, articles: 1873, associations: 2529, maps_inserted: 2529}
  backfill #2 (幂等)                 {legacy_rows: 2529, articles: 1873, associations: 2529, maps_inserted: 0}
  six_item_gate as_of=2026-11-25
    PASS 1_map_parity              legacy_rows=2529 map_rows=2529 multi=0 unmapped=0 orphan_map=0
    PASS 2_association_integrity   associations=2529 pairs=2529 orphan=0 dangling=0/0
    PASS 3_article_identity        articles=1873 identity=1873 published=1873 legacy=1873 collapsed=0
    PASS 4_field_parity            immutable 0/0/0  mutable 0/0  duplicate_pairs=0
    PASS 5_reader_parity           symbols=37  NEWS_WASH diffs=0  news_score diffs=1(registered)
    PASS 6_fixture_coverage        boundary=183 fallback=2529 cross_day=1 conflict=0 collisions=16 manual=0
    registered=2  unexplained=0  blocks_n3=False
  二次门禁 blocks_n3                False
  ROLLBACK -> articles/assoc/map/legacy   0 / 0 / 0 / 2529
```

### 25.8 明确不做 / 遗留（N3 仍是独立批次）

- **N3 canonical read cutover 未执行**：六项门禁已解除（`blocks_n3=False`），但 reader 切换是**另一件事**——`news.py tags_from_events()`、`indicators.news_score()`、`agents.py` 头条、`advisor.py _yahoo_notes()`、`pages.py` 新闻 UI、`guardian_regime.py _NEWS_SQL`、`iris_view.py` 计数共 21 处非测试 SQL 仍读 `news_events`，须逐处给出 canonical 等价路径与逐处对账后再切。
- **live 库 canonical 仍为空**：本批对账是**事务内 dry-run**，没有持久回填。N3 之前必须先做一次**真实持久 backfill** 并确认 N2 双写在运行期持续收敛，否则切 reader 会读到空表。
- **N4 legacy retirement 未执行**：`news_events` 保留不删，删除须长期对账为零且另行裁定（Q6）。
- **未改 `indicators.py` 排序**：NVDA tie 问题留待 N3 批次决定，避免与 NFR 行为变更混批（§6.3 第 7 条）。
- **零 P4–P6**：周线 SMA、Strategy 3 shadow、`eval-v1` 执行、exit 语义、工具接入均未开始。
- 两个启动阻塞（`run_loop()` 的 `load_active()` 早于 `apply_migrations()`；`var/backups` 996 快照 prune）仍属独立运维轮。

### 25.9 回滚

- `git revert <sha>`：只回滚两个新模块、40 项测试、§25、头部第八笔、00 册索引、`CHANGELOG.md` 与 `C-20261125-8`。**无 migration 可回滚**，`057` 三表与既有数据不受影响。
- 两个模块在无人调用时完全惰性：不 import 进 `loop.py`、不注册定时器，回滚后系统行为与 §24 交付态一致。
- `news_events` 与本批零交互，回滚不存在数据面风险。

### 25.10 关联

- 上游：`C-20261124-11`（§16 规划）、`C-20261125-1`～`-7`（P1 / Round A / §20 / §21 / Round B / P2 / P3-1）。
- 下游：**V2-P3 第三批 = 持久 backfill ＋ 21 处 reader 逐处 cutover（N3）**，随后 N4 另需裁定；P4 / P5 / P6 / P7 按固定顺序继续；P5 数据不足则 `no-go` 且不自动进入 P6（§22.5）。

---

## 26. V2-P3 第三批（N3）落地前契约（2026-11-25，两项裁定已确认）

本节是 **N3 canonical read cutover 的实现前契约**，只记录口径与边界，不含实现。§6.3 已把 N3 定义为「NFR / News / Iris 读 canonical；legacy 停止新增前保留只读对照」；§25 已证明六项对账门禁 `blocks_n3 = False`（unexplained = 0）。N3 仍需独立实现、独立验证、独立 commit / rollback。

### 26.1 两项裁定（操作者已确认）

| # | 问题 | 裁定 | 理由 |
|---|---|---|---|
| R1 | `guardian_regime.py _NEWS_SQL` 现按 ET 日 `(ts AT TIME ZONE 'America/New_York')::date` 分桶，而 canonical `published_day` 是 **UTC 日** | **保留 ET 分桶**：canonical reader 用 `association.effective_ts AT TIME ZONE 'America/New_York'` 决定 regime 日界；`published_day`（UTC）**只用于 identity，不用于 regime 分桶** | 切 reader 是存储替换，不得顺手改变 183 行跨 ET/UTC 日界的归属，从而改变 regime 标签；§6.3 第 7 条「不与 NFR 行为变更混批」 |
| R2 | `news_score` 只按 `ts` 排序，同 ts 行的位置权重不可复现（§25.4 NVDA） | **只给 canonical reader 加确定性 tie-break**：`ORDER BY effective_ts, sentiment`；`indicators.py` 的 legacy 路径**不改** | canonical 今后可复现；不触碰 NFR 代码；`news_score` 当前无确定性 wheel / phase 消费者，差异仅影响已登记的 epsilon 级数值；回滚仍回到 legacy 原行为 |

### 26.2 N3 前置条件（缺一不可）

1. **真实持久回填**：§25 的对账是事务内 dry-run，live 库 canonical 三表仍为空。N3 前必须持久执行 `backfill()` 并复跑 `six_item_gate()` 得 `blocks_n3=False`。
2. **N2 双写收敛确认**：确认运行期新 writer 的 dual-write 持续收敛，否则 reader 切换后会读到陈旧 canonical 行。
3. **逐处对账**：17 处非测试 reader 的 canonical 等价路径必须逐处给出 SQL 与对照结果；未登记的逐处差异阻止 N3。
4. **回滚路径**：每个 reader 保留一行可切回 `news_events` 的开关；一次 `git revert` 即恢复全部 legacy 读取，`news_events` 本身不动。

### 26.3 17 处 reader 清单（本批只读枚举，逐处对账用）

| 文件 | 处数 | 语义 |
|---|---|---|
| `apps/orchestrator/src/athena_orchestrator/pages.py` | 7 | 新闻计数 / `avg(sentiment)` / 列表 / `source, count(*)` 分布 / 明细 |
| `packages/strategy-nfr/src/athena_strategy/news.py` | 1 | `tags_from_events()` 的 `NEWS_WASH` 计数（`sentiment < x AND ts >= x AND dismissed=false`） |
| `packages/strategy-nfr/src/athena_strategy/indicators.py` | 1 | `news_score()`（R2：canonical 侧加 tie-break） |
| `packages/strategy-nfr/src/athena_strategy/agents.py` | 2 | 头条标题列表（`ORDER BY ts DESC LIMIT 8`） |
| `packages/strategy-nfr/src/athena_strategy/advisor.py` | 1 | `_yahoo_notes()` 新闻条目 |
| `apps/orchestrator/src/athena_orchestrator/guardian_regime.py` | 1 | `_NEWS_SQL`（R1：保留 ET 日界） |
| `apps/orchestrator/src/athena_orchestrator/iris_view.py` | 2 | 新闻计数、`max(ts)` 新鲜度 |
| `apps/orchestrator/src/athena_orchestrator/console.py` | 1 | 新闻计数 |
| `apps/bots/src/athena_bots/telegram.py` | 1 | 新闻条目 |

**不属于 N3**：`news.py` 三处 writer（`INSERT` ×2 / `dismiss UPDATE`）、`dsh.py` sentiment `UPDATE`、`backup.py TABLES`、`news_canonical.py` / `news_backfill.py` / `news_reconcile.py` 中的 legacy 对照与对账 SQL —— 这些保持读写 `news_events`，用于对照与回滚。

### 26.4 边界

- **N4 不在本批**：`news_events` 不删、不冻结；删除须长期对账为零且另行裁定（Q6）。
- **不新增迁移**：N3 优先以 canonical 读模块 / SQL 投影落地，避免再动 schema；如确需兼容视图，另开裁定与迁移号。
- **不重启 7799、不重试被取消的 `dxnrJP`、不启动 engine**；IB Gateway 保持关闭。
- 两项裁定由操作者于 `C-20261125-8` 交付后确认，历史 verbatim 行（含 §24.6 / §25.8）保留不改写。

---

## 27. V2-P3 第三批交付记录（N3 canonical read model · DARK LAUNCH，2026-11-25，`C-20261125-9`）

本节是 **N3 读侧改造的交付记录**。**本批是 dark launch，不是 cutover**：读模型与 17 处 reader 包装全部完成并逐处对账，但单一开关 `news_read.SOURCE` 保持 `LEGACY`，因此运行期零行为变化、零持久 DB 写入。§26.2 的两项数据面前置条件（真实持久回填、运行期 N2 双写收敛确认）尚未满足，真正激活须操作者单独 go/no-go（§27.11）。§26 落地前契约以 doc-only commit `9fff99c` 先行，作为本批附录一并记入 `C-20261125-9`，不另开索引行。

### 27.1 为什么是 dark launch

| 事实 | 证据 |
|---|---|
| §25 六项对账是**事务内 dry-run** | `ROLLBACK` 后 live `news_articles / news_article_symbols / news_legacy_event_map` = `0 / 0 / 0`，`news_events` = `2529` |
| §26.2 第 1 / 2 项未满足 | 未做持久 backfill；§26.4 禁止启动 engine，无法观察运行期 dual-write 收敛 |
| 直接切 `CANONICAL` 的后果 | 17 处 reader 全部改读空 CTE ⇒ 新闻计数、头条、`NEWS_WASH`、regime 标签全部静默变空 |
| 本批选择 | 交付并验证读模型与全部包装，**默认保持 `LEGACY`**；激活是 `SOURCE = CANONICAL` 一行 ＋ 持久 backfill ＋ 运行期收敛确认 |

历史 verbatim 行（§24.6 / §25.8「21 处 reader 仍读 `news_events`」、§26.2）保留不改写，只在此追加状态推进。

### 27.2 读模型：`news_read.py` 的 CTE 投影

新增 `packages/store/src/athena_store/news_read.py`，**零迁移、零 `pyproject.toml` 变动**（hatch 自动收 Python 模块；wheel 实测含该模块）。核心是命名 CTE：

```sql
WITH news_events AS (
    SELECT m.legacy_fingerprint AS fingerprint,
           s.effective_ts       AS ts,
           s.symbol             AS symbol,
           s.source             AS source,
           s.event_type         AS event_type,
           s.sentiment          AS sentiment,
           m.title              AS title,
           s.url                AS url,
           s.dismissed          AS dismissed
    FROM news_legacy_event_map AS m
    JOIN news_article_symbols AS s ON s.association_id = m.association_id
)
```

- **九列与 legacy 完全同形**：`fingerprint, ts, symbol, source, event_type, sentiment, title, url, dismissed`；每处 reader 的 `WHERE` / `ORDER BY` / `LIMIT` / `date_trunc('week', ts)` / `(ts AT TIME ZONE 'America/New_York')::date` 主体**逐字节不变**。
- **CTE 名 `news_events` shadow 物理表**：单条语句内把 `FROM news_events` 重定向到投影，不需要改任何 reader 的 SQL 字面量。
- **列溯源**：不可变锚点字段（深链、dismiss、draft reason）来自 `news_legacy_event_map`；可变业务字段（DSH enrich、dismiss）来自 `news_article_symbols`。`published_day`（UTC identity）**故意不暴露**给 reader。
- **基数**：一条 map 行 → 一行投影，与 legacy 表 1:1，故 `count` / `avg` / `max` / 分页列表可逐处对账。

### 27.3 两处裁定的落地

| 裁定 | 实现 | 验证 |
|---|---|---|
| **R1 保留 ET 分桶** | CTE 的 `ts = s.effective_ts` 是真实 `timestamptz`；`guardian_regime._NEWS_SQL` 字面量一字不改，仍 `(ts AT TIME ZONE 'America/New_York')::date = %s` | 真实库 **92 个 ET 日**逐日对照 `diffs = 0`；`effective_ts <> legacy ts` 行数 = 0；AST 精确提取 `_NEWS_SQL` 的只读断言（避免同文件 `market_bars` 的 ET 字符串掩盖 mutation） |
| **R2 只给 canonical `news_score` 加 tie-break** | `score_sql()` 仅对 `ORDER BY ts`（非 `DESC`）替换为 `ORDER BY ts, sentiment`；`render()` 默认不启用 `stable_order`；legacy 路径 byte-identical；`indicators.py` 的 Decimal 循环未改 | `ORDER BY ts DESC` 不受影响；`stable_order` 默认 `False`；canonical 与 legacy 各自自比对一致 |

### 27.4 17 处 reader 清单与 `21 → 17` 口径对齐

§25.8 写「21 处非测试 SQL 仍读 `news_events`」，§26.3 收窄为 **17 处 reader**。差异是机械口径，不是遗漏：`apps/` ＋ `packages/strategy-nfr/` 中共有 **21 条**非测试 SQL 引用 `news_events`，其中 **17 条是 SELECT reader、4 条是 writer**（`news.py` 的 `UPDATE` ×1 ＋ `INSERT` ×2、`dsh.py` 的 sentiment `UPDATE` ×1）。writer 属 N3 范围外，继续写 legacy。`packages/store/` 内 `news_backfill.py` / `news_reconcile.py` / `news_canonical.py` 的 legacy 对照 SQL 亦保持不动，用于对账与回滚。

| 文件 | 处数 | 包装方式 |
|---|---|---|
| `apps/orchestrator/src/athena_orchestrator/pages.py` | 7 | `news_read.render(...)` |
| `apps/orchestrator/src/athena_orchestrator/iris_view.py` | 2 | `news_read.render(...)` |
| `packages/strategy-nfr/src/athena_strategy/agents.py` | 2 | `news_read.render(...)` |
| `apps/bots/src/athena_bots/telegram.py` | 1 | `news_read.render(...)` |
| `apps/orchestrator/src/athena_orchestrator/console.py` | 1 | `news_read.render(...)` |
| `apps/orchestrator/src/athena_orchestrator/guardian_regime.py` | 1 | call site `news_read.render(_NEWS_SQL)`（module-level 常量不变） |
| `packages/strategy-nfr/src/athena_strategy/advisor.py` | 1 | `news_read.render(...)` |
| `packages/strategy-nfr/src/athena_strategy/indicators.py` | 1 | `news_read.score_sql(...)`（R2） |
| `packages/strategy-nfr/src/athena_strategy/news.py` | 1 | `news_read.render(...)`，仅 `tags_from_events()` |
| **合计** | **17** | 16 处 inline literal ＋ 1 处 call site |

**writers 未被包装**：`news.py` 三处与 `dsh.py` 一处保持原样。

### 27.5 单一开关与回滚路径（§26.2 第 4 项）

```python
SOURCE = LEGACY   # dark launch；激活改 CANONICAL
```

- `render(sql, *, stable_order=False, source=None)`：`source=` 让每处 reader 可单独切回；省略则用全局 `SOURCE`。
- `LEGACY` 分支**原样返回 sql**，不做任何字符串变换；`CANONICAL` 分支加 CTE（＋可选 tie-break）。
- 未知 `source` 抛 `ValueError`，不静默降级。
- **一次 `git revert <sha>`** 即移除 `news_read.py` 与全部包装、恢复 17 处 legacy 读取；`news_events` 与 `057` 三表零改动。单行开关或 `source=LEGACY` 亦可即时回退。

### 27.6 测试

| 套件 | 结果 |
|---|---|
| `apps/orchestrator/tests/test_news_p3_n3.py`（新增 47 项） | **47/47 OK** |
| `apps/orchestrator/tests/test_iris_p1.py` | **63/63 OK** |
| 全量 `unittest discover -s apps/orchestrator/tests -t . -p "test_*.py"` | **1006/1006 OK**（skipped=2；基线 959 ＋ 47） |

`test_iris_p1.py` 的只读断言同步收紧：只允许 `news_read.CANONICAL_CTE` 这一个**精确** CTE 前缀（`_readonly_body()`），仍拒绝任意 `WITH` 与所有写动词；`_EXISTING_TABLES` 加入 `057` 三表。新增 `test_p1_readonly_contract_survives_a_canonical_flip()` 证明切到 `CANONICAL` 后只读契约仍成立，且 `WITH evil AS (DELETE ...)` 仍被拒。

47 项 N3 专项覆盖：`SOURCE = LEGACY` dark launch、legacy byte-identical / canonical CTE、R2 tie-break 与 `DESC` 免疫、九列投影、`published_day` 不暴露、R1 ET 分桶（AST 精确提取 `_NEWS_SQL`）、17 处 wiring 计数、writers 未包装、无 `058` migration、回滚路径、P1 helper 仍拒绝任意 `WITH`。

### 27.7 真实 PostgreSQL 逐处对账（事务内 dry-run，`ROLLBACK`）

`athena-pg` 维持 **53** migrations，本批零迁移。在**一个事务**内依次执行 `backfill()` → `six_item_gate()` → 17 处 legacy-vs-canonical 逐处 parity → `ROLLBACK`：

```
before                       news_events 2529 | canonical 0 / 0 / 0
backfill #1                  legacy_rows=2529 articles=1873 associations=2529 maps_inserted=2529
backfill #2                  maps_inserted=0（幂等）
six_item_gate as_of=2026-05-08   6 items PASS | registered=3 unexplained=0 blocks_n3=False
17-site parity               PASS all 17 sites | parity diffs: 0
before ROLLBACK              1873 / 2529 / 2529
after ROLLBACK               news_events 2529 | canonical 0 / 0 / 0
DRY_RUN_OK
```

补充证据：`effective_ts <> legacy ts` 行数 = **0**；**92** 个 ET 日逐日对照 `diffs = 0`；`telegram` / `agents_headlines` / `pages_news_rows` 三个 `LIMIT` 边界 chosen-set equality 全 `True`；`as_of=2026-11-25` 门禁 `unexplained=0 / blocks_n3=False`；16 组同日同标题异 URL 为转载／追踪尾巴（§25.6 已审）。

**`registered` 计数在 2–4 间波动是合法的**：`indicators.news_score()` 按 `ORDER BY ts` 取行，同 `ts` 行的位置权重不确定（§25.4 已用 multiset equality、stable tie-break equality、legacy self-inconsistency 三项机械证明）。四次重复 dry-run 均得 `unexplained=0 / blocks_n3=False`；只有 `unexplained` 或 `ok=False` 才阻止 N3。

### 27.8 变异测试

`/tmp/n3_mutation.py` **19/19 TRAPPED**，每个 trap 逐字节还原（sha256 一致）。覆盖：dark launch 被翻转、CTE 丢 `url` / 用 `published_day` 当 `ts` / 暴露 `published_day`、`render` 不加 CTE、`score_sql` 不加 tie-break、tie-break 误伤 `DESC`、默认启用 stable order、未知 source 静默、legacy 路径被改写、可变字段取自 map、JOIN 丢 map、CTE 读物理表、reader 未包装、guardian ET→UTC、indicators 用 `render` 而非 `score_sql`、writer 被包装、P1 允许任意 `WITH`、多出 `058` 迁移。

### 27.9 验证链汇总

```
compileall                                       OK
源码 control / ESC bytes                          0
test_iris_p1                                      63/63 OK
test_news_p3_n3                                   47/47 OK
unittest discover（全量）                         1006/1006 OK (skipped=2)
uv lock --check                                   29 packages
git diff --check                                  clean
变更台账机检                                     131/131
LOCAL_LINK_AND_SOURCE_ERRORS                      0（32 份文档）
wheel                                             athena_store-0.0.0-py3-none-any.whl
                                                  news_read.py / news_canonical.py /
                                                  news_backfill.py / news_reconcile.py
                                                  53 migrations, 057 present, 058 absent
pyproject.toml force-include                      零改动（hatch 自动收 Python 模块）
真实 PostgreSQL                                   news_events 2529 | canonical 0/0/0 | 53 migrations
运行时监听                                        7799 / 7497 / 4001 / 4002 全无
```

### 27.10 边界与明确不做

- **未做持久 backfill**：live canonical 三表仍为 `0 / 0 / 0`；所有真实 PG 验证均事务内 `ROLLBACK`，业务表零持久写入。
- **未把 `SOURCE` 切到 `CANONICAL`**：17 处 reader 运行期仍读 `news_events`，行为与 §25 交付态一致。
- **N4 不在本批**：`news_events` 不删、不冻结；删除须长期对账为零且另行裁定（Q6）。
- **不新增迁移**：53 个不变，无 `058`；`packages/store/pyproject.toml` 零改动。
- **不重启 7799、不重试被取消的 `dxnrJP`、不启动 engine**；IB Gateway `7497/4001/4002` 保持关闭。
- **未改 `indicators.py` legacy 排序**（§6.3 第 7 条「不与 NFR 行为变更混批」）；零 P4–P7。
- 两个启动阻塞（`run_loop()` 的 `load_active()` 早于 `apply_migrations()`；`var/backups` 996 快照 prune）仍属独立运维轮。

### 27.11 激活 go/no-go（须操作者回答）

本批把 N3 的**读侧能力**完整交付，但 §26.2 四项前置条件中第 3 / 4 项（逐处对账、回滚路径）已满足，第 1 / 2 项仍缺：

1. **真实持久回填**：持久执行 `backfill()`，在 live 已持久状态上复跑 `six_item_gate()` 得 `blocks_n3=False`。
2. **运行期 N2 双写收敛确认**：需启动 engine 观察新 writer 的 dual-write 持续收敛；§26.4 明确禁止在本批启动。
3. `SOURCE = CANONICAL`（一行）＋ 全量回归。

前两步是**数据面写入 / 运行期变更**，超出「零持久写入、零行为变化」的 dark launch 边界，因此**必须由操作者显式授权后另起一轮**，不由本批擅自执行。回滚随时可用（§27.5）。

### 27.12 回滚

- `git revert <sha>`：移除 `news_read.py`、17 处包装、两个测试文件的改动、§27、头部第九笔、00 册索引、`CHANGELOG.md` 与 `C-20261125-9`。
- **无 migration 可回滚**；`057` 三表、`news_events` 与既有数据不受影响。
- 单行 `SOURCE = LEGACY` 或逐处 `source=LEGACY` 即时回退；`git revert` 后系统行为与 §25 交付态一致。

### 27.13 关联

- 上游：`C-20261124-11`（§16 规划）、`C-20261125-1`～`-8`（P1 / Round A / §20 / §21 / Round B / P2 / P3-1 / P3-2）、§26 落地前契约（`9fff99c`）。
- 下游：**N3 激活 go/no-go**（§27.11）→ N4 legacy retirement（Q6 另行裁定）→ P4 → P5 → P6 → P7；P5 数据不足则 `no-go` 且不自动进入 P6（§22.5）。

---

## 28. V2-P4 落地前契约（2026-11-25，`C-20261125-10`）

本节是 **P4（日线 gap audit、周线聚合、SMA factor registry、Strategy 3 shadow 信号子集）的实现前契约**。依据：§10 `V2-P4` 行、§7.1 / §7.2、§8.2 / §8.4、§22.2 Q7 / Q8、§22.3 第 1–6 / 14 项。操作者已指示「go with your recommendation」，下列 R1–R7 按推荐口径冻结，实现批次照此执行；任何偏离须新开裁定。

### 28.1 只读实测接缝（2026-11-25）

| 事实 | 证据 |
|---|---|
| **live `market_bars` 为空（0 行）** | `127.0.0.1:55432`：`market_bars=0`、`iv_marks=0`、`regime_labels=1`、`drafts=0`；`calendar_marks` 仅 54 条 `kind='FED'`，**无任何 earnings marks** |
| 无复权数据源 | `market_bars` 仅 `(symbol, ts, open, high, low, close, volume, source)`，无 adjusted 列；未发现 `ADJUSTED_LAST` 生产者 |
| Iris 52W 节点现状 | `iris_view.snapshot()` 硬编码 `daily_gap_state: "unknown"`；P1 测试在两处 pin 该字面量 |
| 包依赖 | `apps/orchestrator` 已依赖 `athena-strategy-nfr`，pure 模块可被 Iris 直接复用；`strategy-nfr` 已有 `simbars.py` 等纯模块先例 |
| §22.5 硬门禁 | 「真实历史数据可用性是硬门禁」——IB Gateway 关闭、无 VIX 历史、smoke 库空 ⇒ P4 的数据面在 live 库上必然全为 `no_data` / `unknown` |

**结论**：P4 的全部价值在 **pure 计算模块 ＋ §8.4 fixtures ＋ Iris 诚实显示**；live 数据面按 §22.3 第 14 项与 V2-I8 诚实输出 `no_data` / `unknown`，绝不为图形完整虚构数据。

### 28.2 范围

1. **日线 gap audit（Q7）**：pure 缺口检测器，六类缺口 `internal_gap` / `long_tail_gap` / `ipo_short_history` / `halt` / `zero_volume` / `corporate_action`，各带 `reason_code`；Iris 52W 节点用真实计算值替换硬编码 `unknown`。
2. **周线聚合（Q8）**：从 `ib-daily` 日线按 `America/New_York` ISO 周聚合；open=首个完成日、high/low=周内极值、close=最后完成日、volume=求和；假日短周保留；未完成本周绝不参与确认。
3. **SMA factor registry**：完成周 close 的 SMA20 / SMA50，带 `factor_version`。
4. **Strategy 3 shadow 信号子集（Q9 第 1–6 / 14 项）**：每完成周每 symbol 输出 §8.2 记录（outcome / reason_codes / input_hash / factor_version）。
5. **Iris 重放（只读）**：snapshot 增量只读；deterministic 重算即可重放 candidate / filtered / no_data。

### 28.3 R1–R7 实现口径（推荐已冻结）

| # | 口径 |
|---|---|
| **R1 交易日历** | 打包**确定性 NYSE/ET 会话日历**（stdlib-only：工作日 + 节假日规则含 Good Friday，无网络、无新依赖）；expected sessions 由该日历给出；用 2025–2026 已知假期 fixture 验证 |
| **R2 Iris gap 状态机** | `unknown`（0 会话或日历覆盖不足，不可测量）/ `partial`（<252 会话或检出 ≥1 缺口）/ `complete`（窗口内 252/252 且零缺口）；替换硬编码字面量；pin 旧字面量的 P1 测试同步收紧而非删除 |
| **R3 复权诚实** | shadow 模块显式接受 `adjustment_basis`；live 侧无复权生产者 ⇒ `adjustment_basis=None` ⇒ **所有 live 输出 `outcome=no_data` + `adjustment_unavailable`**（§22.3 第 14 项），绝不静默用未复权价；declared-adjusted fixtures 走通 candidate 路径 |
| **R4 过滤器可得性** | SPX / VIX / earnings 输入缺失 ⇒ `outcome=no_data` + 专属 reason code（`spx_unavailable` / `vix_unavailable` / `earnings_calendar_unavailable`），缺失不是否定（V2-I8）；SPX 用**周线**口径（收盘 > 其周线 SMA50 且斜率 ≥ 0）；VIX 取信号周收盘分档（<15 跳过、15–20 `caution`、≥20 正常，边界归低档） |
| **R5 outcome 真值序** | 优先级 `no_data` > `invalid` > `filtered` > `no_signal` > `candidate`；每个非 candidate 携带 `reason_codes[]`；只消费完成周、严格无 look-ahead；`hill_peak` 不在最后一周（§22.3 第 1 项） |
| **R6 P4/P5 边界** | §22.3 第 7–13 项（入场窗口、工具参数、TP、持仓、优先级、NAV、连止）**一律不实现**；`instrument_availability` 输出 `deferred_p5`；VIX `caution` 只作标记、不减半预算（预算属 P5）；**零 drafts、零 orders、零预算消耗** |
| **R7 零持久化** | **零迁移（无 `058`）**、shadow 逐次 on-read 重算、重放 = 确定性重算 + `input_hash`；live DB 零写入；数据出现时同一代码自动产出真实值 |

### 28.4 验收门禁（实现批次）

- §8.4 fixture 清单：普通周 / 假日短周 / 跨月跨年 / 缺周 / 未完成周 / 19-20 与 49-50 根边界 / hill 5/6/10/11 / slope 容差 / 回踩容差 / 蜡烛量能真值表 / 五条件逐一点失败 / SPX / VIX / earnings / liquidity 边界 / 六类缺口各一 / 日历假期边界。
- `unittest discover` 全量绿；新增规则有破坏即失败的 mutation test；真实 PG 事务内 dry-run（Iris 只读，`ROLLBACK` 零写入）。
- §10.1 公共门禁全过；同步 00 册索引、CHANGELOG、日志、memory；`.cursor/`、`docs/partnerships/` 不夹带。

### 28.5 明确不做

- 零迁移、零持久写入、零 engine 启动、零 7799 重启、零 IB Gateway 变更。
- 不实现 §22.3 第 7–13 项；不做 eval 协议执行（P5）；不写 drafts / orders。
- 不动 NFR 行为、不动 N3 开关（`SOURCE = LEGACY` 维持，Task #123 仍 open）。

---

## 29. V2-P4 交付记录（gap audit ＋ 周线 SMA ＋ Strategy 3 shadow，2026-11-25，`C-20261125-11`）

### 29.1 状态与范围

固定顺序 **Step 4 / V2-P4 已实现、验证并独立交付**。四个 pure 模块 ＋ Iris 只读接线，**零迁移、零持久写入、零 drafts / orders、零 NFR 行为变更**。R1–R7 按 §28.3 落地；实现中只修正契约内口径，未引入新裁定。

### 29.2 新模块（`packages/strategy-nfr/src/athena_strategy/`）

| 模块 | 内容 |
|---|---|
| `trading_calendar.py` | R1：确定性 NYSE/ET 会话日历（stdlib-only），fixed/floating 假日 ＋ Good Friday（Anonymous Gregorian Easter），Saturday→前周五 / Sunday→后周一 observed，跨年 Jan 1 边界两侧都正确；`sessions / sessions_back / next_sessions / is_session` |
| `gap_audit.py` | Q7：六类缺口 `internal_gap / long_tail_gap / ipo_short_history / halt / zero_volume / corporate_action` 各带 reason_code；expected 窗口止于最新存储会话，**未来会话永不算缺失**；`listed_since` 声明后 tail 归 IPO 而非数据缺口；`has_blocking_gaps(through=)` 供 SMA 窗口拦截 |
| `weekly.py` | Q8：ISO 周聚合（open=首个完成日 / high-low=周内极值 / close=最后完成日 / volume=求和）；假日短周保留；`complete` = 周内全部 expected 会话已存在且最后 expected 会话已完成；**进行中本周永不确认**；`adjustment_basis` 原样透传 |
| `strategy3.py` | Q9 第 1–6/14 项：hill（6–10 周、相等中断、峰不在最后一周）、4 周最小二乘 slope 归一化（|x|<0.002 flat、<−0.002 declining）、0.5% OHLC 触碰带、蜡烛/量能真值表（低量硬过滤、基准=前 8 完成周）、SPX 周线口径、VIX <15 skip / ≤20 caution / >20 ok（边界归低档）、财报前 5 交易日（calendar_marks `EARNINGS`）、20 日均成交额 ≥ $5 亿、prior_rally 仅偏好不设门；outcome 真值序 `no_data > invalid > filtered > no_signal > candidate`；每条记录带 `input_hash` / `factor_version=s3-shadow-v1` / `instrument_availability=deferred_p5` |

### 29.3 实现中修掉的两个真实缺陷

1. **SMA 未成窗时以 `Decimal(0)` 占位毒化 hill 计数**：早期周 SMA50 尚不可算，占位 0 使 `SMA20 > 0` 恒真，hill 连续计数被虚增到 40 周。改为显式 `None`，`_hill` / `_slope_state` / `_retest_state` 遇 `None` 即中断/不可用。
2. **`weekly_bar_count` 泄漏未来**：每条记录原带「全历史完成周总数」，重放旧周时记录含未来信息（look-ahead）。改为该周当时的完成周数 `i+1`，`test_no_look_ahead_replay_is_stable` 钉死。

### 29.4 Iris 只读接线（`iris_view.py`）

- `_p4_recompute()`：单查询读全部 `ib-daily` 会话 ＋ SPX（仅 `SPX/^SPX/I:SPX`，**明确不把 SPY 当代理**）＋ `VIX` ＋ `calendar_marks kind='EARNINGS'`；逐 watchlist symbol 跑 gap audit 与 shadow（live 侧 `adjustment_basis=None` ⇒ 全部诚实 `no_data`）。
- snapshot 新键：`daily_gap_state / daily_gap_count / daily_gap_symbols / strategy3_week / strategy3_candidates / strategy3_scanned`；52W 节点、weekly_sma 与 strategy_3 节点文案换真实计算值；header 注记在可测量时切换为「gap audit found N gap(s)」。
- 节点状态保持 `shadow`（P1 断言不变），`zero drafts (P5/P6 pending)` 文案明示零草案。

### 29.5 测试与变异

`apps/orchestrator/tests/test_strategy3_p4.py` **50 项**：日历 2026 假日/observed/跨年、六类缺口逐一、周聚合规则/假日短周/缺周/进行中周/no-look-ahead、49/50 边界、工程化 fixture 精确产出 **1 个 candidate**（2026-W29：hill 10 / declining / below / bearish / low volume / SPX strong / VIX ok / liquidity ok）并在 W30 出 `hill_too_long`、W25 出 `slope_not_declining`、49 周出 `history_short`；`death_cross` / `peak_in_last_week` / equality 中断 / touch-below-untouched 单元断言；蜡烛中性、低量缺失、SPX weak、VIX skip/caution/20 边界/21 ok、财报 near、thin、adjustment 缺失、SPX/VIX/earnings unavailable、SMA 窗口内缺口、input_hash 确定性与周唯一、无效 bar → `invalid`；Iris 接线（gap 计算、空库 unknown、只读＋表白名单、节点文案、header 切换）。

mutation traps `/tmp/p4_mutation.py` **27/27 TRAPPED**、逐字节 sha256 还原：覆盖日历假日规则、252 窗口 off-by-one、六类缺口分类、状态机、周聚合四规则、look-ahead、slope/touch/VIX/hill/earnings/liquidity/adjustment/过滤分支/量能基准/history_short 门/bar 校验、Iris 硬编码 unknown、gap 计数、zero-drafts 文案、recompute 整体跳过。初跑 24/27：`gap_blocking_gaps` anchor 不符、look-ahead 与量能基准窗口缺测试覆盖 —— 补齐断言后 27/27。

### 29.6 验证链汇总

```
compileall                                       OK
test_strategy3_p4                                 50/50 OK
test_iris_p1                                      63/63 OK
unittest discover（全量）                        1056/1056 OK (skipped=2)
mutation traps                                   27/27 TRAPPED, sha256 一致, 逐字节还原
uv lock --check                                   29 packages
git diff --check                                  clean
变更台账机检                                     133/133
LOCAL_LINK_AND_SOURCE_ERRORS                      0（32 份文档）
wheel (athena-strategy-nfr)                       trading_calendar / gap_audit / weekly / strategy3 全部在包内
真实 PostgreSQL (事务内 ROLLBACK)                 live market_bars=0 → gap unknown / strategy3 unknown；
                                                  market_bars 与 calendar_marks 行数前后不变
运行时监听                                        7799 / 7497 / 4001 / 4002 全无
```

### 29.7 边界与明确不做

- **零迁移（仍 53）、零持久写入**：live DB 只有只读 `SELECT` ＋ `ROLLBACK`；`uv.lock` 零变动、零新依赖。
- **§22.3 第 7–13 项未实现**（入场窗口、工具参数、TP、持仓、优先级、NAV、连止）——`instrument_availability=deferred_p5`；eval 协议执行、成本/滑点模型归 P5。
- live 数据面诚实空态：`market_bars` 空 ⇒ gap `unknown`、Strategy 3 `unknown`；数据出现时同一代码自动产出真实值，无需再改。
- 不重启 7799、不重试 `dxnrJP`、不启动 engine；IB Gateway 关闭；N3 开关不动（`SOURCE = LEGACY`，Task #123 open）。

### 29.8 关联

- 上游：`C-20261125-10`（§28 契约，R1–R7）、§22（Q7/Q8/Q9/Q14）、`C-20261125-9`（N3 dark launch；操作者裁定 defer 激活、进入 P4）。
- 下游：**V2-P5 = 冻结协议 `eval-v1` 的历史与 live shadow 执行**（P4 产物即其输入；真实历史数据可用性是硬门禁，不足则 `insufficient_evidence / no-go / 继续 shadow`）；P6 仅在 P5 go 后进入；P7 按固定顺序收尾。

## 30. V2-P5 交付记录（`eval-v1` 执行 · 数据可用性硬门禁 · no-go 裁定，2026-11-25，`C-20261125-12`）

### 30.1 状态与范围

固定顺序 **Step 5 / V2-P5 已执行并裁定**。本批是 §22.4 冻结协议 `eval-v1` 的**首次执行记录**，执行对象是协议自身的硬门禁：真实历史数据可用性（§22.5）。**本批 doc-only：零代码、零迁移、零持久写入、零 engine 启动**。无 holdout 数据可看，故「holdout 后不得回改阈值」的纪律未被触碰——阈值原封不动，`protocol_version` 仍为 `eval-v1`。

### 30.2 数据可用性只读实测（live DB `athena-pg` @ 127.0.0.1:55432，2026-11-25）

| 表 | 行数 | 对 `eval-v1` 的意义 |
|---|---|---|
| `market_bars` | **0** | 无任何日线 bar：无 52W 覆盖、无周线聚合输入、无 SMA/hill/slope 因子输入 |
| `sim_bars` | **0** | 无模拟 bar（且模拟数据本就不满足「真实历史数据」） |
| `calendar_marks` | 54（`kind='FED'` 仅此一种） | **无 EARNINGS marks** ⇒ 财报过滤（§22.3 第 6 项）不可执行 |
| `iv_marks` / `chain_marks` | **0** / **0** | 无 IV、无期权链 ⇒ 成本/滑点/bid-ask 模型（§22.4 冻结模型）无校准输入 |
| `drafts` / `orders` / `positions` | **0** / **0** / **0** | 无 live shadow 可执行性证据 |
| `news_events` | 2,529 | 仅 legacy 新闻回填；与价格历史评估无关 |

IB Gateway（7497/4001/4002）关闭，无外部数据源在拉取；7799 关闭。

### 30.3 §22.4 最低证据量逐项对照

| 协议要求 | 实测 | 达标？ |
|---|---|---|
| holdout 完成周 ≥ 30 | 0（无任何完成周） | ❌ |
| 有效 candidate ≥ 20 | 0 | ❌ |
| 覆盖 symbol ≥ 5 | 0 | ❌ |
| 52W 数据覆盖率 ≥ 80% | **0%** | ❌ |
| bootstrap 95% 区间可计算 | 不可计算（零样本） | ❌ |

五项全不达标，无一项接近边界——不存在任何解读空间。

### 30.4 裁定

**`insufficient_evidence / no-go / 继续 shadow`**。依据：

- §22.5 硬门禁原文：「真实历史数据可用性是硬门禁……若 §22.4 最低证据量不达标，结论必须写 `insufficient_evidence / no-go / 继续 shadow`，不得判 go、不得事后调阈值」。
- §22.4 no-go 记录要求逐项留档：净成本结果 = **不可计算**（零样本）；最大风险暴露 = **零**（无任何仓位或草案产生）；数据覆盖 = **0%**（§30.2/§30.3）；样本外表现 = **不存在**（holdout 未被构造，纪律未被触碰）；反例清单 = **不适用**（无 candidate 可保留）；live shadow 可执行性 = **不满足**（Iris shadow 节点在零数据下诚实显示 `unknown`，P4 §29 已交付）。
- 原文「60–70% 胜率 / 2:1–3:1 盈亏比 / 每年 5–15 次」按 §22.4 不作为预定答案——本裁定不引用它们。
- `eval-v1` 阈值**零回改**；未来任何重跑都必须以 `protocol_version = eval-v1` 原样执行；若需改口径须新开 `eval-v2` 并重新跑全链、不得复用旧 holdout。

### 30.5 再运行的解除条件（re-run prerequisites）

`eval-v1` 重跑前必须满足的独立数据批次（均不属于本册已授权范围，须各自契约 + 实现 + 验证）：

1. **真实历史日线（复权后）导入**：至少覆盖 watchlist 全体 symbol 的 52W+ 窗口，含 `adjustment_basis` 来源声明（split/dividend adjusted）；导入批次须带确定性重放与缺口审计（P4 `gap_audit` 即其验收工具）。
2. **EARNINGS calendar marks**：历史财报日期（含盘后归属次日交易日的规则，§22.3 第 6 项）。
3. **SPX / VIX 历史周线**：SPX 收盘 + SMA50 输入、VIX 周收盘（§22.3 第 5 项）。
4. **期权链 / IV / borrow 数据源**：§22.4 冻结的成本、滑点、bid-ask、部分成交模型的校准输入；缺失时相应项按协议记 `no_data` 并计入证据量。
5. 导入完成后再跑 §22.4 全链（切分 → 基准 → 反例留存 → 最低证据量 → go 阈值），产出独立 go/no-go 记录。

### 30.6 对 P6 / P7 的影响

- **P6 不自动启动**：§22.5「P6 仅在 P5 = go 时进入」。本裁定 no-go，故 P6（工具 / 订单 / exit 执行语义）**不进入、不预占迁移号、不写契约**。
- **P7 不受阻塞**：P5 no-go 不阻塞其余收尾（§22.5 原文）；P7 按固定顺序在 P5 之后执行。

### 30.7 验证、回滚与边界

- 本批只读实测三条 `SELECT count(*)` 查询（§30.2），单连接、无事务写、无 `ROLLBACK` 需求；零迁移（仍 53、无 `058`）。
- 回滚：`git revert <sha>`；只回滚 §30、头部第十一笔、00 册索引、`CHANGELOG.md` 与台账条目；§22.4 协议冻结行是历史 verbatim，不受影响。
- 边界：N3 开关不动（`SOURCE = LEGACY`，Task #123 open）；7799 / IB Gateway 均关闭；不重试 `dxnrJP`；两个启动阻塞（`load_active()` 早于 `apply_migrations()`、`var/backups` 996 快照 prune）仍属独立运维轮。

### 30.8 关联

- 上游：§22.4 / §22.5（`C-20261125-5` 协议冻结与硬门禁）、§29（`C-20261125-11`，P4 shadow 产物与诚实空态）。
- 下游：**P7（固定顺序收尾）**；`eval-v1` 重跑等 §30.5 数据批次就绪后另开独立批次。

## 31. V2-P7 落地前契约（已完成范围死代码清理与文档收口，2026-11-25，`C-20261125-13`）

### 31.1 状态与范围

固定顺序 **Step 6（跳过）/ Step 7 / V2-P7**。P6 已由 §30.6 裁定**不启动**（P5 = no-go），故本批直接进入 P7。P7 范围 = §10.1「**已完成范围的** legacy news reader 与旧页面死代码清理、文档收口」；门禁 P1 + P3 已满足，P5 no-go 不阻塞。**本契约零代码、零迁移、零 DB 写入**；实现批次独立 commit / rollback。

### 31.2 只读调研结论（2026-11-25，全量引用扫描）

- **无整模块死亡**：27 个 orchestrator 模块全部可从入口点或测试到达；唯一 console script 是 `athena-orchestrator`，无 `python -m athena_*` 调用。
- **news 读路径无死亡重复品**：全部 17 处 SELECT reader 已统一经 `news_read.render()` / `score_sql()` 包装；写路径有意留在 legacy 表（N2 dual-write 生效中）；`news_backfill.py` / `news_reconcile.py` 是 N3 激活前提工具。**N3 仍为 DARK LAUNCH（`SOURCE = LEGACY`，Task #123 open），legacy 读路径全部保留，N4 retirement 另需裁定——本批不碰任何 news SQL。**
- **无未登记状态表**：代码引用的全部表都能在 53 个 migration 中找到来源（唯一例外 `schema_migrations` 由 `migrate.py` 自建自管）。
- **旧链接仍可达**：`/portfolio-analysis` 与 `/iris` 直映射同页、`/orchestration` ↔ `/strategy` 双别名、导航无孤儿链接。

### 31.3 清单 W1–W7（实现批次口径，逐项冻结）

| # | 动作 | 目标 | 测试同步 |
|---|---|---|---|
| W1 | 删除零引用死函数 | `pages._contract_line`、`pages._secret`、`cockpit._money`、`runtime.note_step` | 无测试引用，无需同步 |
| W2 | 删除生产死亡 setter | `runtime.note_cycle`（生产 cycle 一律经 `begin()` 设置） | `test_rules.py` 移除 import 与一处调用（该调用被紧随的 `begin(4)` 覆盖，行为不变） |
| W3 | 删除测试专用薄包装 | `pages._audit_result_tag`（= `_audit_result_html({"result":…, "status":…})` 逐字包装） | `test_rules.py` 5 处调用改写为直接构造 dict 调 `_audit_result_html`，断言语义逐字不变 |
| W4 | 删除旧 Advisor 行动表残件 | `pages._advisor_actions_table`（P1 已将其从页面退役，路由与调用零残留）＋ 仅被它引用的 `pages._decimal_or_none` ＋ 仅服务它的死 CSS `.advisor-reason { white-space: pre-wrap; }` | `test_d111_vix.py` 的 XSS 转义测试**改写到 live 路径 `_draft_card`**（patch 依赖，断言 `<script>` 转义 + `white-space:pre-wrap` 内联样式），CSS 断言移除；**覆盖不降级：live 渲染路径此前无专门转义测试，本批补上** |
| W5 | 删除不可达别名包装 | `pages._portfolio_analysis_page`（路由表直映射 `_iris_page`，包装函数零调用者；§18「alias 经由该包装」与路由实现的事实不一致由此消除，§18 原句 verbatim 保留、§32 加 supersession 说明） | 删除 P1 测试 `test_alias_helper_is_a_thin_backward_compatible_wrapper`（其存在仅为钉住包装本身；`test_route_map_points_both_paths_at_iris_page` 已覆盖别名语义） |
| W6 | **明确保留**（有记录意图，非死代码） | `pages.method_switch_note`（docstring 自述 future chain_bs switch 占位 + 测试钉住）；`iris_events.listener_count / detach`（测试 API 面）；全部 news legacy 读写路径（dark launch 生效中）；`news_backfill` / `news_reconcile`；`_pending_drafts` / `_draft_links`（live 多处引用） | 不动 |
| W7 | 文档收口 | 15 册头部第十二笔 + §32 交付记录；00 册 15 号索引终态（P2–P5 已交付、P6 未启动、P7 已交付）；CHANGELOG / logs / memory | — |

### 31.4 退出条件核验口径（P7 验收）

1. **单一行动入口**：draft rail 仍是唯一行动入口（P1 测试 `test_iris_page_has_exactly_one_action_rail` 等保持全绿）。
2. **适用范围内单一 news read model**：17 处 reader 全部经 `news_read` 包装（`test_news_p3_n3` AST 断言保持全绿，本批不改任何 reader SQL）。
3. **旧链接仍可达**：`/portfolio-analysis` 与 `/orchestration` 别名路由不变；导航无孤儿链接。
4. **无未登记状态表**：零新表、零迁移（仍 53、无 `058`）。
5. 全量 `unittest discover` 绿（测试总数变化如实记录）；台账机检、`git diff --check`、wheel 全绿；零持久写入、零 engine 启动、零 7799 重启。

### 31.5 边界

- 不碰 `news_events` / canonical 三表的任何 SQL；不切 `SOURCE`；不做 N4。
- 不删 `method_switch_note` 等有记录意图的占位；不降级任何测试覆盖（W4 覆盖反而增加）。
- 两个启动阻塞（`load_active()` 早于 `apply_migrations()`、`var/backups` 996 快照 prune）仍属独立运维轮。

## 32. V2-P7 交付记录（已完成范围死代码清理与文档收口，2026-11-25，`C-20261125-14`）

### 32.1 状态与范围

固定顺序 **Step 6 跳过（P6 不启动）→ Step 7 / V2-P7 已实现、验证并独立交付**。函数级死代码清理 ＋ 文档收口；**零迁移、零持久写入、零 news SQL 变更、零 `SOURCE` 变更**。§31 契约 W1–W7 全部按冻结口径落地，其中 W4 有一处实现期纠正（§32.3）。

### 32.2 已删除（6 个函数 ＋ 1 处冗余调用，共 150 行）

| 符号 | 判定依据 | 测试同步 |
|---|---|---|
| `pages._contract_line` | 全仓库零引用 | 无需 |
| `pages._secret` | 零引用（`_masked_secret` 是另一活函数，未动） | 无需 |
| `cockpit._money` | 零引用（bots 的 `_fill_money` 是另一活函数，未动） | 无需 |
| `runtime.note_step` | 零引用（`mark()`/`finish()` 保留） | 无需 |
| `runtime.note_cycle` | 生产死亡（cycle 一律经 `begin()` 设置；模块级 `cycle = 0` 初始化与 `apply_snapshot` 恢复路径完好） | `test_rules.py` 移除 import 与一处调用（该调用被紧随的 `begin(4)` 覆盖） |
| `pages._audit_result_tag` | 生产死亡薄包装（= `_audit_result_html({"result":…, "status":…})` 逐字） | `test_rules.py` 5 处调用改写为直接构造 dict，断言语义逐字不变 |
| `pages._advisor_actions_table` ＋ `pages._decimal_or_none` | P1 已把旧 Advisor 行动表从页面退役；两者互相引用、对外零调用者 | `test_d111_vix.py` XSS 转义测试**改写到 live `_draft_card`**（`<script>` 转义、`draft&lt;1&gt;` 转义、`white-space:pre-wrap` 内联样式）；live 渲染路径此前无专门转义测试，覆盖不降级反而增加 |
| `pages._portfolio_analysis_page` | 不可达薄包装（路由表直映射 `_iris_page`） | 删除其专属 P1 钉住测试；`test_route_map_points_both_paths_at_iris_page` 继续覆盖别名语义 |

### 32.3 实现期纠正：`.advisor-reason` CSS 不是死代码

§31 W4 原列「死 CSS `.advisor-reason { white-space: pre-wrap; }`」——实现期复核发现 **`iris_view.render_draft_rail()`（live Iris draft rail，P1 交付物）仍在发射该类**，CSS 是活代码。纠正：**保留 CSS 与其断言原样**（`console.py` 与 `test_d111_vix.py` 净零改动）；只删真正的死发射者 `pages._advisor_actions_table`。§31 W4 原句 verbatim 保留，本节为 supersession 说明。

### 32.4 明确保留（有记录意图，非死代码）

`pages.method_switch_note`（docstring 自述 future chain_bs switch 占位 + 测试钉住）；`iris_events.listener_count / detach`（测试 API 面）；全部 news legacy 读写路径（N3 dark launch 生效中，N4 retirement 另需裁定）；`news_backfill.py` / `news_reconcile.py`（N3 激活前提工具）；`_pending_drafts` / `_draft_links`（live 多处引用）。

### 32.5 验证链汇总

```
compileall                                       OK
unittest discover（全量）                        1055/1055 OK (skipped=2)
  （1056 → 1055：删除 1 个仅钉住已删包装的 P1 测试；test_news_p3_n3 AST
    reader 包装断言、test_iris_p1 单一行动入口与路由映射断言全部保持绿）
uv lock --check                                   29 packages（零新依赖）
git diff --check                                  clean
变更台账机检                                     136/136
删除符号残留引用                                  0（全仓库 grep 复核；仅测试名与 live iris_view 用法属预期）
wheel                                             不受影响（packages/ 零改动，git diff 仅 apps/）
迁移数                                            53（无 058）
运行时进程                                        none（7799 closed, Gateway closed, no engine launched）
```

### 32.6 退出条件核验（§31.4 逐项）

1. **单一行动入口** ✅ draft rail 唯一（`test_iris_page_has_exactly_one_action_rail` 绿）。
2. **适用范围内单一 news read model** ✅ 17 处 reader 全部经 `news_read` 包装（AST 断言绿，零 reader SQL 改动）。
3. **旧链接仍可达** ✅ `/portfolio-analysis` 直映射、`/orchestration` ↔ `/strategy` 双别名、导航无孤儿（路由映射测试绿）。
4. **无未登记状态表** ✅ 零新表、零迁移。
5. 全量绿 + 台账机检绿 + `git diff --check` clean ✅。

### 32.7 文档收口终态

- 15 册：P1–P5 全部有交付记录（§18/§23/§24/§25/§27/§29）、P5 no-go 裁定（§30）、P7 交付（本节）；P6 显式「不启动（仅 P5=go 进入）」。
- **固定顺序 `Q7–Q12/Q14 → P2 → P3 → P4 → P5 → (P6 跳过) → P7` 全部走完**。
- 仍 open 的独立裁定：**N3 激活 go/no-go**（Task #123：持久 backfill ＋ `SOURCE = CANONICAL` ＋ 运行期收敛确认）、N4 legacy retirement、§30.5 五项数据批次（`eval-v1` 重跑前提）、两个启动阻塞运维轮。

### 32.8 关联

- 上游：§31 契约（`C-20261125-13`）、§30（P5 no-go）、§29（P4）、§18（P1 别名设计，其「alias 经由该包装」句由 §32.2 W5 行 supersede，原句 verbatim 保留）。
- 下游：Task #123（N3 激活，操作者裁定）；§30.5 数据批次；N4。
