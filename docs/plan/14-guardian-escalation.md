# 14 — Guardian & Escalation Layer（风险监测层与任务告知）

**状态**：**Ratified 2026-11-21 ＋ 2026-11-22 第九轮**（D58–D65 八条 ＋ D66 单条补裁定 ＋ D67–D73 七条，全取推荐、零偏离；＋ D74–D79 六条：五条取推荐、**一条明确偏离 D75**、零跳过）。Side track. Not on the signal critical path (steps 1–9).
**R1 已交付并经 D66 修订、R2 已交付并验证（均 2026-11-21）；R3 已交付并验证（2026-11-22）；R4–R6 待办；R7 已裁定（D67–D73，Planning 阶段结束，实现仍待 R4）。** Every rule below is a **ratified** decision node; rulings recorded in §7（含第八、九轮）. Implementation contracts: §8 (R1) / §11 (R2) / §14 (R3); delivery records: §9 (R1) / §12 (R2) / §15 (R3); R7 contract + ratification: §13; **§16 VIX-view proposal is now ✅ RATIFIED (D74–D79): four read-only dimensions registered in §13.9; §1／§6 boundaries zero-revised.**

> **Origin (operator, 2026-11-21)**: during work/exam crunch weeks, execution function is consumed by work and the portfolio goes into autopilot — and autopilot only does one thing: nothing. Last week a black-swan-scale move took the book from fine to near-liquidation. The quantitative layer (per-position scoring) already works. What is missing is a layer that (a) compares every live position — including accidental ones — against the rule book, (b) tells the operator the one thing to do, at a fixed time, and (c) refuses to be ignored when it matters.

---

## 1. What this layer is (and is not)

| Is | Is not |
|---|---|
| A **conformance and escalation** layer: positions vs rules, portfolio vs margin, alerts vs acknowledgement | A new strategy. It generates **no drafts, no orders, no tactic changes** |
| Read-only against IB and the store, except its own alert/ack tables | A modification of Engine A / the wheel / the advisor |
| Mechanical: every tier is computable from numbers already in `broker.py` / the store | A judgement call. If a tier cannot be computed from data, the rule is wrong, not the tier |

Existing seams it reuses (verified 2026-11-21):

| Need | Already exists |
|---|---|
| Margin distance | `broker.py:641` `net_liquidation`, `:650` `ExcessLiquidity`, `:778` `MaintMarginReq` |
| Position sync | `tasks.py:91` `begin_positions` / `:120` `_run_positions` |
| Scheduled execution | `loop.py` orchestrator cycle with `_cycle_sleep`; `pulse.py` 5-second loop |
| DST-safe session windows | `slots.py` pattern (`zoneinfo`, e.g. Xetra 09:00–17:30 `Europe/Berlin`) |
| Interactive ack button | `telegram.py:806` inline keyboard (Approve/Reject) + `:486` `_handle_callback` router |
| Colour-tier board precedent | `/book` heatmap page |

---

## 2. The five tiers（盈利 / 健康 / 平衡 / 危险 / 暴雷）

Per-position **and** portfolio-level. The portfolio tier is the worst of its inputs. Every threshold below is **D58, ratified as proposed 2026-11-21** — `EL/NL ≥ 20 / 10 / 5%`、`MaintMarginReq/NL > 90%`、临近窗 5 pts / 5 DTE，**不收紧也不放松**。

| Tier | Colour | Meaning | Mechanical definition (proposal) |
|---|---|---|---|
| **盈利** Profit | blue | Conforming **and** in the money | 健康 + day or unrealized P&L > 0 |
| **健康** Healthy | green | Every rule holds | No breach, no proximity flag; `ExcessLiquidity / NetLiq ≥ 20%` |
| **平衡** Balanced | yellow | Within rules, drifting | Any proximity flag: bucket % within 5 pts of cap, short option DTE within 5 of `min_dte`, |delta| past `assign_delta`, EL/NL 10–20% |
| **危险** Danger | red | A rule is breached or margin is thin | Any hard breach: **an unacked advisor `close` / `take_profit` decision (D66, mirrored by R2 via `pending_action`)**, uncovered short call, concentration over cap, EL/NL < 10%, projected-EL after assignment < 0 |
| **暴雷** Blowup | black | Liquidation distance is the only number that matters | EL/NL < 5%, or `MaintMarginReq / NetLiq > 90%`, or IB margin-call flag. **Tier black fires immediately, on any day, at any hour** |

Two deliberate properties:

1. **Accidental positions can never be 健康** (D63). A synced IB position with no universe row / no tactic flags is graded on conformance only, and carries the standing flag `unmanaged` until it is either adopted (added to universe) or closed. This is how "我小不小心买的" positions become visible instead of invisible.
2. **盈利 is above 健康, not beside it.** A day can be green but not blue; blue requires green first. This stops a profitable-but-broken book from reading as fine.
3. **No position-level stop line (D66).** R1 invents none, and carries no `stop_line` / `trim_line` / `market_price`. The only "act now" input is `pending_action`, supplied by the caller from the advisor's own staged decision. Operator price lines (`NVDA < 118`) belong solely to R5. One decision, one ack path.

---

## 3. The daily ritual (what the operator actually experiences)

**Fixed time (D60 ✅ 已裁定).** Two runs anchored to the US session, computed from `America/New_York` so DST is automatic:

- **Run A — pre-open brief**: 60 min before US open (HKT ~21:30 summer / ~22:30 winter). Contents: portfolio tier, per-position tier, the **one** required action (or "nothing required"), market state line.
- **Run B — at-open check**: open + 5 min. Only re-grades; sends only if the tier changed or a required action appeared.

Both runs sit between HK close (16:00 HKT) and US open — the window the operator named.

**The panel (`/guard`, D59 ✅ 已裁定为独立新页).** One page, five colours, designed to be read in 30 seconds:

```
PORTFOLIO  ● 危险   EL/NL 8.2%   1 breach, 2 proximity

⬛ 暴雷   — none
🟥 危险   NVDA  short call uncovered by 100 sh (assignment Fri)
          ACTION: buy 100 NVDA or roll the call  →  [Resolve] [Skip+reason]
🟨 平衡   MRVL  bucket TECH at 33% (cap 35%)
🟩 健康   24 positions
🟦 盈利   +1.8% day, +4.1% unrealized
```

**Forced reply (D62 ✅ 已裁定为逐条三态闭环).** Every 危险/暴雷 item is an *open alert* in `guardian_alerts`. It closes only one of three ways:

1. **Resolved** — operator clicks Resolve on the panel (or the Telegram button). Optional note.
2. **Skip + reason** — operator writes why. The alert sleeps for N hours (proposal 24), then reopens if the breach is still real.
3. **Auto-clear** — the breach stops being true (price recovers, position closed). Recorded as `auto_cleared`, never silently deleted.

**Escalation (D61 ✅ 已裁定 5 分钟、仅 red+black).** Open alert at tier ≥ 危险 → Telegram every **5 minutes** until acked. 暴雷 also fires outside the schedule, during any hour the US market is open, via the margin poll (常开机器 D64 / 心跳 D65). Healthy/balanced states never nag — the panel is the whole message.

---

## 4. Iterations

| # | Name | Scope | Depends on | Acceptance |
|---|---|---|---|---|
| **R1** | Conformance core | Pure function: `(positions, account summary, rules) → per-position tier + flags + portfolio tier`. Includes `unmanaged` flag for accidental positions. Read-only, no UI, no alerts. Log output only. | none | Run against the latest live snapshot; tiers match a hand-check of 5 positions; `unmanaged` positions flagged |
| **R2** | `/guard` panel + alert store | `guardian_alerts` table (alert, tier, breach, created, state, ack). Panel with 5-colour board, Resolve / Skip+reason. | R1 | Open panel, see live tiers; resolve an alert; skip writes reason; auto-clear on next run |
| **R3** | Scheduler | Run A / Run B anchored to `America/New_York`; reuses `loop.py` task runner pattern; writes each run's grade to the store so the panel always shows the latest | R2 | Two runs land at the right HKT wall-clock across a DST boundary (test with both offsets) |
| **R4** | Telegram escalation | Nag loop: tier ≥ 危险 & unacked → resend every 5 min with inline Resolve button (reuses `_handle_callback`). Dead-man: if a scheduled run fails, that failure itself becomes a 危险 alert | R3 | Force a red state in paper; confirm 5-min cadence; ack from phone stops it; kill the orchestrator, get the "guardian is down" message |
| **R5** | Trigger lines | Operator-defined lines (`NVDA < 118`, `VIX > 38`, `MRVL > 145` …) as a first-class alert source feeding the same store and escalation path. This is option **C** from the earlier conversation, folded in. **D66：R5 是唯一的价格型触发线来源**，R1/R2 不设持仓级 `stop_line` | R4 | Set a line in Settings; cross it in paper; alert fires within one poll interval |
| **R6** | Dedicated host | Always-on machine running orchestrator + exec-ib + IB Gateway under a watchdog (launchd/systemd auto-restart), with a heartbeat: if no heartbeat for 10 min during US hours, Telegram fires | R4 | Unplug test: kill the box, get the heartbeat alert; reboot, everything self-recovers without a shell |
| **R7** | Midday regime label + close bias (**✅ 已裁定 D67–D73**) | **Run C** at 12:30 ET; joint matrix (stocks × 10Y yield × gold × VIX level × volume + news tags) → closed `label` vocabulary + qualitative `close_bias` (**not** a price forecast); append-only `regime_labels` with replayable `evidence`; fed into the existing `Indicators` / `agents.py` context as **read-only annotation, no gating**. 4.5 of 5 inputs have no producer today ⇒ missing input records `unknown` **with an inline reason**, never invented. Seven rulings D67–D73, see §13 | R3 + R4 (and separated from R5's static trigger lines) | Every fired rule leaves `evidence`; label reproducible from stored inputs; 5 worked cases calibrate the matrix |

**Sequencing logic**: R1→R2→R3→R4 is the spine (each needs the previous). R5 needs R4's escalation path. R6 is infrastructure and can run in parallel with R3–R5 — but note that **R4 on an unreliable machine is a false sense of safety**, so R6 should not trail far behind. **R7 is numbered after R6 but depends on R3 (ET-anchored scheduler) + R4 (delivery), so it cannot jump the spine; it must also be separated semantically from R5's static trigger lines (§13.7).**

---

## 5. Decisions — 全部已裁定（2026-11-21 ＋ 2026-11-22 第九轮）

**二十二条全部裁定：二十一条取推荐、一条明确偏离（Q2／D75）、零跳过。** 前八条分两轮、每条带具体案例逐条问过（「EL/NL 8.2% 怎么显示」「考试中被 uncovered call 连发几条」「22:40 进程死了多久收到消息」），裁定记录见 §7；**D66** 是 R1 交付后的单条补裁定，见 §7 第七轮；**D67–D73** 是 R7 企划的第八轮裁定（Q1–Q7 编号化），见 §7 第八轮与 §13.6；**D74–D79** 是 R7 VIX 预警视角补充企划的第九轮裁定（Q1–Q6 编号化，其中 Q6 由 Q2 的偏离答案拆出），见 §7 第九轮与 §16.5。

| # | Decision | Options | 裁定 | 依据 |
|---|---|---|---|---|
| **D58** | Tier thresholds (§2 table): EL/NL boundaries 20/10/5%, MaintMargin/NetLiq 90%, proximity windows (5 pts bucket, 5 DTE) | (a) as proposed (b) tighter (c) looser | **✅ (a) as proposed** | 与 D32 已裁定的预算值重合，guardian 与 wheel 不会互相打架。明确否决 (c)：8.2% 那种情况在 (c) 下仍是黄色且静默，正是刚过去那一周 |
| **D59** | Panel location | (a) new `/guard` page (b) extend `/book` heatmap (c) both, with a link | **✅ (a) new `/guard` page** | `/book` 是只读诊断、`/guard` 是带 ack 状态的动作面；混在一起两个都糊。裁决：不必加 (c) 的跳转徽章，单独入口反而逼你每天开一次 |
| **D60** | Run times | (a) open−60min and open+5min, NY-anchored (b) fixed HKT clock times (c) also add a post-HK-close run | **✅ (a) NY-anchored, 2 runs** | DST 不可能漂；(b) 在冬季会让「开盘后复查」提前到开盘前 55 分钟、拿到误导数据；(c) 被否：HK 收市到美股盘前之间无可动作变化，只多一次打扰 |
| **D61** | Escalation rule | (a) 5-min nag, tier ≥ 危险 only, Telegram (b) also nag 平衡 (c) 15-min interval | **✅ (a) 5 min, red+black only** | 黄色也催会训练你忽略所有消息；5 分钟是「刻意令人受不了」，只在规则真被破时才用。明确否决 15 分钟：真正的保证金事件里 4 条/小时容易被睡过去 |
| **D62** | Ack semantics | (a) per-alert Resolve / Skip+reason / auto-clear (b) one daily blanket ack (c) per-alert but Resolve only | **✅ (a) per-alert, 3 close paths** |  blanket ack 会让第二条 breach 藏在第一条后面 —— 正是本层要修的失效模式；否决「只能 Resolve」：休市或考试时无法行动会被无限催。Skip 24h 后若 breach 仍在则重开 |
| **D63** | Accidental positions | (a) `unmanaged` flag, max tier 平衡, listed until adopted or closed (b) ignore them (c) auto-tier like managed ones | **✅ (a) `unmanaged`, max yellow** | 无 universe 行的同步持仓只按相符性打分、永远不能变绿，直到收养或平仓。手机随手买的 50 股会立刻出现在板上。否决 (b)：复现盲点；否决 (c)：赢钱的无规则仓位会读成「健康」 |
| **D64** | Host for R6 | (a) dedicated Mac mini at home (b) existing Windows box with auto-login (c) cloud VPS | **✅ (a) dedicated Mac mini** | IB Gateway 同机、无 RDP 脆弱性、断电后 launchd 自恢复、不与 ComfyUI/5090 抢资源。代价：一台机器 ＋ 独立 Gateway client id（`C-20261013-34` 已有此模式）。否决 (c)：IB 2FA ＋ 住宅 IP 期待让 VPS 登录脆弱 |
| **D65** | Heartbeat rule | (a) 10-min missed heartbeat during US hours → Telegram (b) only at scheduled runs (c) 5-min missed beat | **✅ (a) 10-min → Telegram** | 定时跑挂掉本身也变成危险告警。否决 (b)：22:40 死亡直到次日盘前都不可见，恰是你最需要它的时段；否决 (c)：短网络抖动会造出假「down」并训练你忽略 |
| **D66** | Stop line 的处置（R1 交付后补裁定） | (a) 删除 `stop_line` / `trim_line`，改为镜像顾问 `pending_action` (b) 保留到 R5 再喂 (c) 现在就建 `positions.stop_price` ＋ Settings 旋钮 ＋ 迁移 | **✅ (a) 删除并镜像顾问；价格线只归 R5** | 库里三个 `stop` 全是假朋友（LLM 停止词 / STP LMT 委托属性 / 已被顾问消费的预测价），`profit_take_pct` 也不是价格线。第三条产线会与 `advisor_signals → drafts` 形成两套 ack、与 D22 `avg_price` 基准冲突，并重复 D54 刚删掉的「假旋钮」。否决 (b)：R2 告警表要背一条永不触发的死分支；否决 (c)：重复 R5、多一个生产者与迁移 |
| **D67** | 午间 Regime Label 的权限（R7 Q1） | (a) 只读注解、绝不改既有门控 (b) gate：🔴/🟠 抑制 `wheel.py` 新开仓 (c) 直接给动作指令 | **✅ (a) 只读注解** | 12:30 ET 判定 `liquidity_squeeze` 红时，顾问仍有一条 pending NVDA CSP 草案等批准 —— 标签只入库、只展示，顾问／轮动／草案一切照旧，仍由操作者自己点批准。保住 §1「no orders / no tactic changes」与 D66 的单 ack 路径。否决 (b)(c)：改既有行为或另造第二套决策面 |
| **D68** | 4.5 个输入无生产者怎么处理（R7 Q2） | (a) 降級 `unknown`、不臆造 (b) 新增轻量行情源 (c) 操作者手工粘贴 → LLM 分类 | **✅ (a) 降級 `unknown`**；操作者加注「要告诉 user 为什么 VIX 缺」⇒ 拆为 **D73** | 「VIX 34（早高 40 回落）、10Y 4.94%↓、金跌」这行里库内全无生产者；照抄会变成永远靠猜的标签。缺维度记 `unknown`、标签降 `insufficient_data`，只用真有的延迟 tick ＋ 消息标签。否决 (b)：需显式修订 §6「No new market data sources」并新建生产者；否决 (c)：依赖操作者 12:30 ET 在桌前，正是 Guardian 要修的失效模式 |
| **D69** | 标签落点与粒度（R7 Q3） | (a) 组合级存一次、注入 `agents.py` 全局上下文 (b) 逐标的加 `Indicators.session_regime` (c) 两者都做 | **✅ (a) 组合级存一次** | 一个市场级 `short_cover_bounce` 若写进 ~30 个标的的 `symbol_phases.indicators` 会复制 N 份，且与 `Indicators.regime_unknown`（`SPY` 日线**跨日** regime）撞名。存一行 `regime_labels`、在 `agents.py:_context()` 注入一行全局上下文，另起新名 `session_regime`。否决 (b)(c)：N 份复制／双真源 |
| **D70** | 面板位置（R7 Q4） | (a) 扩 `/portfolio-analysis` 现有标签卡 (b) `/guard` 加只读卡 (c) 新页 `/regime` | **✅ (a) 扩 `/portfolio-analysis`** | 22:00 HKT 只有 30 秒；`/portfolio-analysis` 已有 `RISK OFF / CAUTION / RISK ON` 展示态卡。沿用 D59 的独立页思路但不必再开第三页，且 `/guard` 保持纯 ack 动作面（D62 语义不动）。否决 (b)(c)：把时序混进状态机／增加导航负担 |
| **D71** | Run C 与推送（R7 Q5） | (a) 单次 12:30 ET 跑 (b) 12:30 ＋ 15:30 ET 覆核两跑 (c) 沿用 5 分钟 cycle 不做 ET 锚定 | **✅ (a) 单次 12:30 ET** | D60 的 Run A／Run B 不动；Run C 由 R3 的 `America/New_York` 锚定派生（DST 安全），推送复用 R4 的 Telegram 通道但**不进 5 分钟 nag 循环**。否决 (b)：多一次打扰；否决 (c)：「12:30 ET」变成近似值并跨 DST 漂，正是 D60 否过的 |
| **D72** | 验收口径（R7 Q6） | (a) 可重放 `evidence`、标签可由存下输入复现、5 案例校准 (b) 以「还原机构意图 80%」为验收 (c) 先 shadow-only 暂缓验收 | **✅ (a) 可重放 `evidence`**；**明確否決 (b)** | n=5、同一段 regime、无标注无反例，「80% 機構意圖」不可证伪且声称了不可观测量。每条命中留 `evidence`、标签可从 `inputs` 重放；启用任何 gate 前须有跨 regime 标注样本与反例。否决 (c)：把验收无限期推迟 |
| **D73** | 缺失原因如何呈现（R7 Q7，由 Q2 附注拆出） | (a) 每个 `unknown` 维度就地标注 (b) 单列一行 coverage footer (c) 只在面板卡显示 | **✅ (a) 就地标注** | 操作者在 Q2 加注「we should advise user why VIX is missing」，`insufficient_data` 不得是静默空白。形如 `VIX n/a (no intraday-high feed in store)`，区分「库里没有生产者」与「今天没抓到」，让操作者知道该不该信这个标签。否决 (b)(c)：理由离一眼之外／Telegram 上完全看不见 |
| **D74** | 5 条 VIX 预警视角放哪（§16 Q1） | (a) 扩 R7 §13.4 只读注解维度 (b) 归 R5 静态触发线 (c) 另开 R8 | **✅ (a) 扩 R7 §13.4** | 案例：12:30 ET 正常日，R7 是已冻结契约、R5 管 `VIX > 38` 静态线。扩维度只需登记词表，不动 D67–D73 七条结论、不加迁移。否决 (b)：改 R5 范围且要等 R4 催办路径；否决 (c)：第二套决策面、第二个契约与迁移 |
| **D75** | 5/5 输入无生产者怎么办（§16 Q2） | (a) 降級 `unknown` ＋ 就地原因 (b) 新增行情源 (c) 操作者粘贴 | **✅ (b) 新增行情源／用 IBKR 建**（偏离推荐） | 操作者明确不选 (a)：「add new feeds or build using IBKR」。意图是要把感測器真正装上，而不是永远报缺。**但见 D79**：本批次不改 §6，四个维度暂仍 `unknown` ＋ 就地原因，数据源另起裁定轮。否决 (c)：依赖 12:30 ET 人在桌前，正是 Guardian 要修的失效模式 |
| **D76** | 黄／橙／红灯的动作指令（§16 Q3） | (a) 剥掉动作、只留只读状态标签 (b) 纯文字建议 (c) 实现 gating | **✅ (a) 只读状态标签** | 案例：12:30 ET 标 `liquidity_squeeze`，同时一条 NVDA CSP 草案还等你批准 —— 工具只给 `pre_warning / elevated / critical`，顾问／轮动／草案一切照旧，单一 ack 路径保住（D67／§1）。操作者附注「symbols are for an example」⇒ NVDA/MRVL/INTC 只是举例，**不得硬编码进代码或阈值**（与 §13.8 同纪律）。否决 (b)：工具被看成在下指令；否决 (c)：第二套决策面且须改 §1 |
| **D77** | 0–5 分与推送（§16 Q4） | (a) 展示用命中维度数 (b) ≥2 分推 Telegram (c) 不做计分 | **✅ (a) 展示用命中维度数** | 案例：12:30 ET 亮 2 个维度，`/guard` 已有一条硬违规 open —— 命中数只显示在 `/portfolio-analysis` 现有卡（D70），不新推送、不进 nag、`/guard` 保持纯 ack 面。否决 (b)：须改 D71 单次跑且造新催办路径；否决 (c)：丢掉「亮了几个」的一眼可读性 |
| **D78** | 11/13 案例与验收（§16 Q5） | (a) 未核实工作案例 (b) 以「11/13 应亮 4–5 条」为验收 (c) shadow-only 暂缓 | **✅ (a) 未核实工作案例**；**明確否決 (b)** | n=1、后见之明挑选、无标注无反例，与 D72 否决的「80% 機構意圖」同类。仅记为第 6 个校准案例；验收仍是可重放 `evidence` ＋ 既有 5 案例，不声称准确率、不硬编码数字。否决 (c)：把验收无限期推迟 |
| **D79** | Q2 偏离的排期与 §6 边界（§16 Q6，由 Q2 拆出） | (a) 另起「R7 数据源」裁定轮显式修订 §6 (b) 本批次改 §6、IBKR 优先 (c) 本批次改 §6、5 个源全接 | **✅ (a) 另起裁定轮** | §13.7 明写「新行情源须另开一轮裁定显式修订 §6，不得在实现里顺手改掉」。实测 `quote_fields()` 只建 STK/CASH、`gate.py:31` 硬限 `{STK, OPT}`、`reqMktData` 的 generic tick list 为空 ⇒ IBKR 侧目前无 IND 合约路径、无期权 IV 度量；CBOE 期限结构与 10Y 收益率多半仍需 vendor feed。**本批次 §6 保持原文、零修订**；四维度暂 `unknown` ＋ 就地原因，下一轮再定 feeds／entitlements／迁移。否决 (b)(c)：把未经核实的 entitlement 与 vendor 决策压进本批次 |

---

## 6. Explicitly out of scope

- No drafting, ordering, or tactic changes. The guardian **tells**; the wheel **does**.
- No new market data sources. Everything grades from IB account summary + existing store.
- The 5-min nag is the maximum intrusion. No email, no second channel, no phone call.
- R6 covers the *machine*, not a networking overhaul. Tunnel behaviour is unchanged.
- **R6 主机裁定为家用专用 Mac mini（D64）**，不是 Windows 共用机、不是云 VPS。IB Gateway 同机，`launchd` 自恢复，需在 `C-20261013-34` 的「每 Gateway 登录独立 client id」模式下给它单独的 client id。
- **D66**：不设持仓级 `stop_line`；不新增 Settings 字段、不加迁移、不改 `advisor_signals` / `manage_book` / `drafts`。价格触发线只归 R5。
- **R7（§13，已裁定 D67–D73）**：七条全取 (a) ⇒ **本节「No new market data sources」与 §1「no drafts, no orders, no tactic changes」两条边界一条都不用改**。D67 只读注解不 gate、D68 降級 `unknown` 不新增源、D70 不新开页、D71 不进 nag。若日后要启用 gate 或新行情源，仍须先显式修订这两条边界（§13.7）。
- **R7 VIX 补充企划（§16，第九轮 D74–D79）**：**D75 明确表达了「新增行情源／用 IBKR 建」的意图，但 D79 裁定把它排到独立的「R7 数据源」裁定轮** ⇒ 本批次**本节「No new market data sources」与 §1 仍保持原文、零修订、零代码、零迁移**。四个新维度暂以 `unknown` ＋ 就地原因为 interim（D68/D73 纪律）。下一轮须显式修订本节后才能建生产者；不得在实现里顺手接 feed、IND 合约或期权 IV。D74 只读注解、D76 剥动作、D77 不推送、D78 不作验收 ⇒ §1 亦无需改。

---

## 7. 裁定记录（2026-11-21，第六轮 ＋ 第七轮补裁定 ＋ 第八轮 R7 ＋ 第九轮 R7 VIX）

操作者指令：「每個問題問我一遍，我來回答」。分两轮、每条带具体案例逐条问，**8 条全取推荐、零偏离、无跳过**（第六轮 D58–D65 的原始记录；第七／八／九轮见下文各节）。

| 轮 | 决策 | 案例（问的是什么） | 裁定 |
|---|---|---|---|
| 1 | **D58** | `EL/NL = 8.2%` 显示红并催办；收紧档两天前就红、放松档仍是黄色静默 | ✅ 按提议 20/10/5% |
| 1 | **D59** | 22:00 HKT 只有 30 秒，`/guard` 全屏五色板 vs 塞进 `/book` 那 20 列诊断表 | ✅ 独立新页 `/guard` |
| 1 | **D60** | 夏季 21:30／冬季 22:30 开盘，NY 锚定给出 20:30/21:35 或 21:30/22:35；固定 HKT 会让冬季「开盘后复查」提前到开盘前 55 分钟 | ✅ NY 锚定、两跑 |
| 1 | **D61** | 考试中 14:00 uncovered NVDA call 转红，5 分钟一条 = 首小时 12 条，直到点 Resolve | ✅ 5 分钟、仅 red+black |
| 2 | **D62** | 同日两条 breach（NVDA uncovered ＋ MRVL 37% 超 35% 上限），blanket ack 一点全清会漏掉第二条 | ✅ 逐条、三态闭环 |
| 2 | **D63** | 手机随手买 50 股忘了，无 universe 行、无 tactic flags、无打分 | ✅ `unmanaged`、最高黄色、永不绿 |
| 2 | **D64** | Gateway 必须同机且断电自恢复；Windows 共用机有 5090/ComfyUI 抢资源；VPS 卡 IB 2FA ＋ 住宅 IP | ✅ 家用专用 Mac mini |
| 2 | **D65** | 22:40 进程死，10 分钟无心跳 → 22:50 收到「guardian is down」；否则要到次日盘前约 23 小时后 | ✅ 10 分钟 → Telegram |

**裁定后的状态变化**：

- 决策编号扩到 **D1–D65（66 个节点，`D4` 拆两段）**，其中 **D1–D57 早已裁定**、**D58–D65 本轮裁定**。**决策层面零待决**。（同日第七轮补 **D66** 后为 **D1–D66 / 67 节点**，见下。）
- **R1（conformance core）解除阻塞**，可以开工 —— 它是纯函数、只读、不触碰 Engine A / 轮动 / 顾问。
- 本轮**仍是计划与裁定，零代码改动**。R1–R6 的实现属于"准备实行"的后续动作，须按 `dev-change-ledger` 逐条建台账。

### 第七轮（2026-11-21，R1 交付后单条补裁定 D66）

操作者追问：「stop line 最好怎么做，不要太复杂、不要和外层框架与 guard 打架」。核代码后发现三个 `stop` 全是假朋友，且 R1 初版的 `stop_line` / `trim_line` 分支**没有任何生产者** —— 与 D54 刚删掉的 `profit_take_relief_pct` 是同一类「写着会做事、实际没来源」的旋钮。

| 决策 | 案例 | 选项 | 裁定 |
|---|---|---|---|
| **D66** | 持 200 股 NVDA、顾问已挂一条 pending `CLOSE` 草案：单独 `stop_line` 会同时产生「一条草案」＋「一条 guardian 红告警」，两套 ack 状态；镜像方案只出一行、批准草案即同时关闭 | (a) 删除并镜像顾问 (b) 保留到 R5 再喂 (c) 现在就加 `positions.stop_price` ＋ Settings 旋钮 ＋ 迁移 | **✅ (a) 删除并镜像顾问** |

**明确否决**：(b) 让 `breach_stop_line` 成为 R2 告警表里永不触发的死分支；(c) 重复 R5 的职责、与 D22 `avg_price` 基准冲突、并要求一个生产者 ＋ UI ＋ 迁移。**不新增 Settings 字段、不加迁移、不改 `advisor_signals` / `manage_book` / `drafts`；R1 保持零 I/O 纯函数。** 裁定后决策编号扩到 **D1–D66（67 个节点）**，仍零待决。

### 第八轮（2026-11-21，R7 企划 Q1–Q7 → D67–D73）

操作者先给出 2026/11 对照表与 5 条午间规则要求「加到计划、可以放 R7」，确认合并后的 §13 文档后回 `Yes`，按「每個問題問我一遍」分两轮逐条问（Q1–Q4 ／ Q5–Q7），**七条全取推荐、零偏离、无跳过**。Q2 的回答带一句附注「but we should advise user why VIX is missing」，该附注**不代拍板**，拆成 Q7 单独再问，落为 D73。

| 轮 | 决策 | 案例（问的是什么） | 裁定 |
|---|---|---|---|
| 1 | **D67** | 12:30 ET 判定 `liquidity_squeeze` 红，同时顾问挂着 pending NVDA CSP 草案等你批准 —— 标签该有多大权限 | ✅ (a) 只读注解，不 gate |
| 1 | **D68** | 「VIX 34（早高 40 回落）、10Y 4.94%↓、金跌」这行里 4.5 个输入库内全无生产者 | ✅ (a) 降級 `unknown`、不臆造（＋附注拆出 D73） |
| 1 | **D69** | 一个市场级 `short_cover_bounce` 要喂给 ~30 个标的，而 `Indicators` 已有跨日 `regime_unknown` | ✅ (a) 组合级存一次、注入 `agents.py` 上下文 |
| 1 | **D70** | 22:00 HKT 只有 30 秒，`/portfolio-analysis` 已有展示态卡、`/guard` 是 ack 面 | ✅ (a) 扩 `/portfolio-analysis`，不新开页 |
| 2 | **D71** | D60 已有 Run A／Run B，午间 12:30 ET 是第三跑 | ✅ (a) 单次 12:30 ET，R3 派生、R4 通道、不进 nag |
| 2 | **D72** | 「價格行為＋債金背離＋消息標籤可還原 80% 機構意圖」能不能当验收 | ✅ (a) 可重放 `evidence`；明確否決 (b) 80% |
| 2 | **D73** | `insufficient_data` 不能是静默空白，怎么告诉操作者 VIX 为什么缺 | ✅ (a) 每个 `unknown` 维度就地标注固定原因 |

**裁定后的状态变化**：

- 决策编号扩到 **D1–D73（74 个节点，`D4` 拆两段）**（第八轮当时的计数；第九轮后为 D1–D79 / 80 个节点，见 §7 第九轮），**决策层面仍零待决**；Q1–Q6 的「裁定前不编号」约定已兑现为 D67–D72，Q2 附注拆出的 Q7 落为 D73。
- **七条全取 (a) ⇒ §1「no orders / no tactic changes」与 §6「No new market data sources」一条都不用改**；R7 保持只读注解、不新增数据源、不 gate、不新开页、不进 nag 循环。
- R7 从 **Planning／待裁定** 转为 **已裁定、契约冻结**；实现仍依赖 R3 ＋ R4（§13.7），**本轮零代码改动**。
- 逐条依据见 §5 决策表与 §13.6；本轮台账 `C-20261121-7`。

### 第九轮（2026-11-22，R7 VIX 预警视角 Q1–Q6 → D74–D79）

操作者先确认 §16 合并文档后回 `Yes`，按「每個問題問我一遍」分两轮逐条问（Q1–Q4 ／ Q5 ＋ 由 Q2 偏离答案拆出的 Q6），**六条中五条取推荐、一条明确偏离、零跳过**。Q2 没有选推荐的 (a)，而是手写「add new feeds or build using IBKR」；助手没有替操作者把 §6 边界顺手改掉，而是把这份意图拆成 Q6 单独再问一次，落为 **D79**。

| 轮 | 决策 | 案例（问的是什么） | 裁定 |
|---|---|---|---|
| 1 | **D74** | 12:30 ET 正常日，R7 已冻结、R5 管 `VIX > 38` 静态线 —— 5 条 VIX 视角该放哪 | ✅ (a) 扩 R7 §13.4 只读维度 |
| 1 | **D75** | `VIX1D` 与 `estimated_iv()` 都没值，工具该报缺还是该装感測器 | ⚠️ **(b) 新增行情源／用 IBKR 建**（偏离推荐） |
| 1 | **D76** | 12:30 ET 标 `liquidity_squeeze`，同时 NVDA CSP 草案还等你批准 —— 工具发不发「減一半」 | ✅ (a) 只读状态标签；附注「symbols are for an example」 |
| 1 | **D77** | 亮 2 个维度时 `/guard` 已有一条硬违规 open —— 要不要 ≥2 分推 Telegram | ✅ (a) 展示用命中维度数 |
| 2 | **D78** | 「11/13 亮 4–5 条 → 应该橙灯 → 当週減一半」能不能当验收 | ✅ (a) 未核实工作案例；明確否決 (b) |
| 2 | **D79** | Q2 要新增行情源，但 §13.7 要求另起裁定轮显式改 §6；且实测 IBKR 侧无 IND 合约路径、无期权 IV 度量 | ✅ (a) 另起「R7 数据源」裁定轮；本批次 §6 零修订 |

**裁定后的状态变化**：

- 决策编号扩到 **D1–D79（80 个节点，`D4` 拆两段）**，**决策层面仍零待决**；§16.5 的「裁定前不编号」约定已兑现为 D74–D78，Q2 偏离拆出的 Q6 落为 D79。
- **§1／§6 两条边界仍保持原文、零修订**：D74 只读注解、D76 剥动作、D77 不推送、D78 不作验收；D75 的「新增行情源」意图被 **D79 排到独立裁定轮**，本批次不改 §6、不接 feed、不加迁移、零代码。
- §16 从 **⚠️ 企划／待裁定** 转为 **✅ 已裁定**；四个 VIX 维度以 **interim `unknown` ＋ 就地原因** 登记进 §13.9（D74），等下一轮显式修订 §6 后才建生产者。
- **下一轮（尚未开工）**：「R7 数据源」裁定轮，须显式修订 §6，并决定 CBOE VIX 期限结构／10Y 收益率的 vendor feed 与 IBKR entitlement、`gate.py` 的 `{STK, OPT}` 硬限、`reqMktData` generic tick list、真 IV Rank 时序表与迁移号（不得占用 `055`，见 §14.2）。
- 逐条依据见 §5 决策表与 §16.5；本轮台账 `C-20261122-3`。**本轮零代码改动。**

---

## 8. R1 实现口径（2026-11-21，落地前的契约）

R1 是纯函数、只读、零 I/O：不连库、不连 IB、不写表、不发消息、不建 UI。落在 `packages/risk/src/athena_risk/guardian.py`，经 `athena_risk` 顶层导出；输入为调用方组装好的快照（`GuardianPosition` / `AccountMarks` / `GuardianRules`），输出为分级结果 ＋ 一段可写日志的文本。

**签名**：`grade(positions, marks, rules) -> GuardianReport`，其中 `GuardianReport` 含 `portfolio_tier` / `account_tier` / 每仓 `PositionGrade(symbol, tier, flags, unmanaged)` / `el_nl_pct` / 三个计数。组合档位 = `max(account_tier, 所有仓位档位)`。

**档位次序**（好 → 坏）：`盈利 0 < 健康 1 < 平衡 2 < 危险 3 < 暴雷 4`。盈利在健康**之上**，蓝色要求先绿再盈利（§2 第 2 条）。`D63`：`unmanaged` 仓最高只能到平衡，但真实 breach 仍可升到危险 —— 顺序是「breach 先判，unmanaged 再压」。

**阈值绑定**：D58 的五个阈值（EL/NL 20/10/5%、MaintMargin/NL 90%、5pt/5DTE 临近窗）是 `GuardianRules` 的** ratified 常量**；与 D32 重叠的四个值（`max_bucket_pct_nav` / `max_symbol_pct_nav` / `min_dte_new` / `assign_delta`）由 `GuardianRules.from_limits(limits)` 从活 `RiskLimits` 取，避免 guardian 与 wheel 各读一套。**本轮不改 Settings、不加字段。**

**四条实现口径（诚实标注，非新决策）**：

1. **止损线（D66 补裁定，见 §7 第七轮）**：R1 **不设** `stop_line` / `trim_line` / `market_price`，也不做任何价格穿越算术。理由：库里三个叫 `stop` 的东西全是假朋友 —— `PredictionConfig.stop: list[str]`（`model.py:145`）是 LLM 生成停止词、`orders.stop_price`（迁移 `041`）是 STP LMT 委托属性、`forecast_marks.horizons.*.stop` 是 LLM 短周期预测价且已被 `advisor.py` 消费；`profit_take_pct` 是权利金捕获百分比、不是价格线。真正的「需要马上做」由**调用方**以 `pending_action` 传入（取 `advisor_signals.action` 或 pending `drafts.tactic`，归一化为小写）：`close` / `take_profit` ⇒ `advisor_close_pending`（危险，触发 D61 催办）；`trim` ⇒ `advisor_trim_pending`（平衡）；`hold` / 空 / 未知值 ⇒ 跳过、不臆造。**R1 仍是零 I/O 纯函数**，读 `today_signals()` 是 R2 的职责。操作者自定的价格线（`NVDA < 118`）**只归 R5**，直接进 `guardian_alerts`，不回写 `GuardianPosition`。
2. **DTE 临近窗**：按字面实现 `abs(dte - min_dte_new) <= 5`，仅对空头期权。若实盘数据表明「已跌破 floor」才该提示，留到 R2 依操作者反馈再扩。
3. **缺数据**：`net_liquidation` / `excess_liquidity` 任一缺失或 NAV ≤ 0 ⇒ 不算 EL/NL、不算集中度，记 `unknown_margin` 旗并把账户档位压到**平衡**（不谎报健康、也不误报危险）。缺 `market_value` 则该仓跳过集中度检查。所有跳过都要在 `flags` 里可见。
4. **覆盖率**：短 call 由同标的持股覆盖，`shares >= abs(qty) * multiplier` 才算覆盖；`multiplier` 缺省沿用 `budget.py` 既有约定（OPT 100 / STK 1），不是新发明。空 put 只做 `assignment_risk` 临近判定与 projected-EL 推演，不做覆盖率检查（CSP 是现金担保）。

**边界**：无迁移、无 `guardian_alerts` 表（属 R2）、无 Telegram、无调度、无页面。R1 的输出只有 `GuardianReport` 与 `render_report()` 文本。

---

## 9. R1 交付记录（2026-11-21）

| 项 | 内容 |
|---|---|
| 状态 | ✅ 已实现并验证。`C-20261121-3` |
| 代码 | 新增 `packages/risk/src/athena_risk/guardian.py`（纯函数、零 I/O）；`athena_risk/__init__.py` 顶层导出 8 个符号；新增 `apps/orchestrator/tests/test_guardian.py`（初版 51 例 → **D66 修订后 53 例**） |
| 接口 | `grade(positions, marks, rules) -> GuardianReport` ＋ `render_report()`；`GuardianRules.from_limits(limits)` 绑定 D32 重叠值；`GuardianPosition.pending_action`（D66，调用方传入、可缺省） |
| 验证 | 初版单测 51/51、全量 229/229；**D66 修订后 53/53、全量 231/231**（`skipped=2` 为既有）；5 仓位手核冒烟与 §3 面板示例一致；两轮变异测试（阈值 5→0、禁用 `pending_action` 映射）均确认测试会失败且已还原；台账机检正反例均符合预期 |
| 边界 | **未做**：无迁移、无 `guardian_alerts` 表、无 `/guard` 页、无 Telegram、无调度、无常开机器、未改 Settings、未改 `advisor_signals` / `manage_book` / `drafts`。R2–R6 仍待办 |
| 实现中发现 | ① 初版因「无存储止损线」把 `stop_line` 设为可选、缺省跳过 —— **同日 D66 补裁定已删除该分支**（见 §7 第七轮、§10），改为 `pending_action` 镜像顾问决策；② 初版测试暴露字符串价格被按字典序比较（`"9" <= "10"` 为 False），已统一强制 Decimal 转换；D66 删除价格穿越算术后该转换仍服务于其它字段；③ 「单个 33% 仓位」会同时撞 20% 单标的 cap，桶临近窗用例改为两个 16.5% 仓位 |

---

## 10. D66 修订记录（2026-11-21，R1 交付后同日）

| 项 | 内容 |
|---|---|
| 状态 | ✅ 已实现并验证。`C-20261121-4` |
| 触发 | 操作者问「stop line 最好怎么做，不要太复杂、不要和外层框架与 guard 打架」。核代码确认三个 `stop` 全是假朋友，且 R1 的 `stop_line` / `trim_line` 分支**无任何生产者** —— 与 D54 刚删掉的假旋钮同类 |
| 删除 | `GuardianPosition` 的 `market_price` / `stop_line` / `trim_line` 三字段；`BREACH_FLAGS` 的 `breach_stop_line` / `breach_trim_line` 两死旗标；`_crossed_stop` / `_crossed_trim` 两判定；对应 6 例单测 |
| 新增 | `GuardianPosition.pending_action: str \\| None`（调用方从 `advisor_signals.action` 或 pending `drafts.tactic` 传入，R1 不连库）；`advisor_close_pending`（危险，`close` / `take_profit`）、`advisor_trim_pending`（平衡，`trim`）两旗标；8 例单测（含「旗标已删除」「输入契约无价格线字段」两条守门用例） |
| 映射依据 | `manage.py:216-219` 的置信度阈值与 `manage.py:239` 的 `_advisor_tactic`（`trim` → TRIM，其余 → CLOSE）。guardian 与 wheel 用同一套动作语义，不产生第二套判定 |
| 验证 | guardian 53/53；全量 231/231（`skipped=2` 既有）；5 仓位冒烟中 `pending_action='close'` 的 NVDA 单独出一行红 `advisor_close_pending`；变异测试禁用映射后 `test_pending_close_is_danger` 如期 FAIL、trap 还原、`cmp` 一致、全量重跑通过 |
| 边界 | **未改**：Settings、迁移、`advisor_signals` / `manage_book` / `drafts` / `advisor.py`、R2–R6、任何外层框架或 guard。**R2 契约**（本轮只记录）：快照构造方读 `today_signals()` 填 `pending_action`，红行链接 pending 草案，批准草案即同时关闭告警（D62 的 auto-clear），不设第二套 ack。**R5 契约**：操作者价格线直接进 `guardian_alerts` |

---

## 11. R2 实现口径（2026-11-21，落地前的契约）

R2 = **`guardian_alerts` 告警表 ＋ `/guard` 五色面板 ＋ Resolve / Skip+reason 三态闭环**。依赖 R1 的 `grade()`；**不含**调度（R3）、Telegram 催办（R4）、价格触发线（R5）、常开机器（R6）。全部口径由 D58–D66 已裁定项推导，**不是新决策**；凡库里无生产者的数据一律留空并记旗标，不臆造（延续 D66）。

### 11.1 告警粒度与状态机（D61 / D62）

| 项 | 口径 |
|---|---|
| 告警键 | `(account_id, symbol, breach)`。`symbol` 为持仓标的；账户级 breach 用保留哨兵 `*ACCOUNT*`（不可能是真代码）。`tier` 是**可更新列**，重分级只 UPDATE，不产生第二条 |
| 哪些旗标开告警 | **仅硬违规**：`BREACH_FLAGS`（危险）∪ `BLOWUP_FLAGS`（暴雷）。临近窗/平衡档**永不开告警、永不催办**（D61）。平衡与盈利只在板上显示 |
| 状态 | `open`（待确认）／`skipped`（带 `skip_until` 休眠）／`resolved`（操作者点 Resolve）／`auto_cleared`（breach 不再成立）。**终态行永不删除**（D62「never silently deleted」） |
| 「本次告警」 | 每个 `(account, symbol, breach)` 取**最新一行**为当前 occurrence；旧行是历史。reconcile 只按当前行的状态决定下一步，不扫全历史 |
| 唯一约束 | 部分唯一索引只在 `state IN ('open','skipped')` 上生效 ⇒ 每个键最多一条活告警，终态行可累积成历史 |
| Skip 重开 | 休眠期 `24h`（D62 提议值）内不开新告警；`skip_until` 过期且 breach 仍在 ⇒ 同一行回到 `open`。breach 已消失 ⇒ `auto_cleared` |
| Resolve | **本次**确认，保持 `resolved`：breach 仍在也**不**在下一跑重开（否则点一次 Resolve、页面一刷新就立刻弹回来，等于没确认）。breach 后来消失 ⇒ 转 `auto_cleared`，`ack_actor` / `ack_note` / `resolved_at` 保留 |
| 复发 | 只有当前行已是 `auto_cleared`（即 breach 真消失过）而 breach 又出现时才 **INSERT 一条新 `open`**。这就是「拒绝被忽略」的正确支点：不是靠重复弹同一条，而是靠「消失后复发」重新武装 |

**状态转移表**（`desired` = 本次 `grade()` 里的硬违规键集合）：

| 当前行状态 | breach 仍在 desired | breach 已不在 desired |
|---|---|---|
| `open` | 保持（tier 变了就 UPDATE tier） | → `auto_cleared` |
| `skipped` | 休眠中保持；`skip_until` 已过 → `open` | → `auto_cleared` |
| `resolved` | 保持（不 nag、不重开） | → `auto_cleared` |
| `auto_cleared` | **INSERT 新 `open`**（复发重新武装） | 保持 |

**R2 的「一次 run」= `/guard` 页载入**（R3 会把同一个 `sync()` 接进定时跑批）。纯函数 `reconcile(report, alerts, now)` 产出 `ReconcilePlan`，薄 SQL 层 `apply_plan()` 执行 —— 与 R1 的纯核心同构，R3 复用时不需要碰 Web 层。

### 11.2 `pending_action` 生产者与「批准草案即关闭」（D66 §10）

D66 要求**不设第二套 ack**：顾问已挂的草案是唯一动作面，批准它必须同时关掉 guardian 红告警。落地方式**不改 `drafts.decide()` / `advisor_signals` / `manage_book`**，而是让 `pending_action` 由「当日顾问信号 ∩ 仍 pending 的草案」共同决定：

```
signals = today_signals(conn, account, HKT_today)      # advisor_signals.action
pending = {d.symbol: d for d in pending_drafts}         # drafts.status = 'pending'
for each position:
    pending_action = signals[symbol].action if symbol in pending else None
    (无信号时按草案 tactic 兜底：CLOSE→close，TRIM→trim)
```

- 红行 `advisor_close_pending` 链接该 symbol 的 pending 草案 id（`/drafts`）。
- **批准/拒绝草案 ⇒ 草案不再 pending ⇒ 下次 `sync()` 时 `pending_action` 消失 ⇒ breach 自动 `auto_cleared`**。零 hook、零第二套 ack、不碰任何外层批准链路。
- `advisor_trim_pending` 属平衡档，只显示、不开告警。

### 11.3 快照构造与**诚实留白**

新模块 `apps/orchestrator/src/athena_orchestrator/guardian_view.py`（不 import `pages`，避免环；`pages` 传入 `_position_rows` / `_pending_drafts` / `settings`）。`athena_risk.guardian` 保持零 I/O 不变。

| 字段 | 来源 | 缺失时 |
|---|---|---|
| `GuardianPosition` 主体 | `pages._position_rows(conn, account)`（已含 qty / sec_type / right / strike / dte / market / pnl / currency / multiplier） | 跳过该仓 |
| `market_value` / `unrealized_pnl` | 用 `money.ledger()` ＋ `money.convert()` 折算到账户基准币 | 无汇率 ⇒ `None` ⇒ R1 记 `missing_market_value` |
| `classifiers` | `symbol_facts.classifiers` 逗号串 | 空 ⇒ R1 落 `UNCLASSIFIED` 桶 |
| `managed`（D63） | `symbol.upper() in settings.watchlist` | 不在 ⇒ `unmanaged`、最高黄色 |
| `GuardianRules` | `GuardianRules.from_limits(settings.risk)` 绑定 D32 四个重叠值 | — |
| `AccountMarks` | `nav_marks.last_nav` / `account_marks.margin` / `.excess_liquidity` / `.base_currency`（各取最新一行） | 缺 ⇒ `unknown_margin`、账户压到平衡 |
| **`delta`** | **本轮无来源**：migrations 全库无 delta 列，`chain_marks` 也只有 mid/strike，`budget._put_delta` 是按需 BS 计算、非快照可读 | **`delta=None`** ⇒ `assignment_risk` / `projected_el_negative` 在 R2 静默不触发。**明记为 R2 已知边界**，不虚构 delta（D66 纪律）；delta 源归后续迭代 |
| **`margin_call`** | **无持久化来源**（IB 即时旗标未入库） | **`False`** ⇒ D58 的「IB margin-call 立即黑档」在 R2 只能由 EL<5% / MaintMargin>90% 触发；即时旗标归 R4/R6 的保证金轮询 |

### 11.4 落地面

| 面 | 改动 |
|---|---|
| 迁移 | `053_guardian_alerts.sql`：`id / account_id / symbol / breach / tier / state / source / reason / ack_actor / ack_note / created_at / updated_at / skip_until / resolved_at` ＋ 部分唯一索引 ＋ open 索引。`source` 缺省 `'grade'`，**预留给 R5 价格线**（`'trigger'`），R2 不写它 |
| 页 | `pages.build()` 注册 `"/guard"`；`pages.submit()` 加 `decision=resolve\|skip`（skip 必须有 reason）；`_html()` 返回同形 dict |
| 导航 | `NAV_GROUPS` 的 Core logic 加 `("/guard", "Guardian")`；`_nav_badges()` 加 open 红/黑告警数（**带 try/except**：迁移未跑时不得拖垮全站） |
| 样式 | `_EXTRA_CSS` 加 `.tag.black`（暴雷）与少量 `.guard-*`；沿用既有 `.card` / `.tag` / `.btn` |
| 包接线 | `apps/orchestrator/pyproject.toml` 补 `athena-risk` 直接依赖（源码早已 import）；同步 `uv.lock`；`packages/store/pyproject.toml` 的 `force-include` 补 045–053（原列表停在 044，属落地时发现的打包缺口） |
| 测试 | 新增 `apps/orchestrator/tests/test_guardian_r2.py`：纯 `reconcile` 状态机、快照映射（含 `pending_action` 门控）、HTML 转义、POST SQL 参数与 account 作用域、fake conn |

### 11.5 边界（R2 明确不做）

不改 `advisor_signals` / `manage_book` / `drafts.decide()` / `advisor.py`；不加 Settings 字段；不设持仓级 `stop_line`（D66）；不发 Telegram、不建调度、不建心跳、不接价格线；不新增市场数据源。`guardian_alerts` 是本层唯一可写表（§1）。

---

## 12. R2 交付记录（2026-11-21）

| 项 | 内容 |
|---|---|
| 状态 | ✅ **已实现并验证**。`C-20261121-5` |
| 代码 | 新增 `053_guardian_alerts.sql`、`apps/orchestrator/src/athena_orchestrator/guardian_view.py`、`apps/orchestrator/tests/test_guardian_r2.py`；修改 `pages.py` / `console.py` / 两份 `pyproject.toml` / `uv.lock`。契约见 §11 |
| 接口 | `sync()`（`build_snapshot()` → `reconcile()` → `apply_plan()` → `load_alerts()`）供 R3 直接复用；`resolve()` / `skip()` / `open_alert_count()`；`desired()` / `ReconcilePlan` 为纯核心，与 R1 同构 |
| 验证 | ① R2 单测 **60/60**；② 全量 **291/291**（`skipped=2` 为既有）；③ `uv lock --check`（29 packages）、`compileall`、`from athena_orchestrator import main`、`git diff --check` 全绿；④ 真实 Postgres 一次性容器：`apply_migrations()` applied 49、`053` 落 14 列 ＋ `guardian_alerts_live` 部分唯一索引 ＋ `guardian_alerts_open` ＋ pkey，`_statements()` 切 3 条有效 statement；⑤ **live path `LIVE_PATH_OK`**：RUN1 两条 open（`advisor_close_pending` / `el_thin`）→ Resolve 后 RUN2 保持 `resolved` → 批准 D1 草案后仅前者 `auto_cleared`；⑥ skip 24h 休眠、25h 后回 `open`；⑦ **`LIVE_PAGE_OK`**：`pages.build` GET 200（五色带 ＋ ack form）、`pages.submit` POST 200 ＋ flash toast；⑧ 两轮变异测试均如期 FAIL，trap 还原 `cmp=0`、全量恢复 291/291；⑨ 台账机检 **97/97 `EXIT=0`** |
| 边界 | **未做**：调度（R3）、Telegram 催办（R4）、价格触发线（R5）、常开机器／心跳（R6）、新市场数据源；不改 `drafts.decide()` / `advisor.py` / Settings。**已知留白**：`delta=None` ⇒ `assignment_risk` / `projected_el_negative` 不触发；`margin_call=False` ⇒ 即时 IB margin-call 黑档归 R4/R6（R2 仍可由 EL<5% 或 MaintMargin>90% 触发）。详见 §11.3 |
| 提交 | 本批次独立提交（`Ship R2 Guardian alerts board with resolve, skip, and auto-clear.`）；hash 以 `git log -1 --format=%h` 为准，**不写入本文档** —— 文档与代码同一批次提交，自指不可能，沿用 §9 R1 惯例 |

---

## 13. R7 契约：午間 Regime Label 與收盤定性前瞻（2026-11-21，✅ 已裁定 D67–D73）

> **来源**：操作者 2026-11-21 提供 2026/11 的「時間點 × 消息 × 暗池／大機構 × 聯儲」對照表與 5 條午間判斷規則，要求把「當天發生什麼」的判斷 ＋「當天結尾」的預知提供給既有打分表，並指定放在 **R7**。
> **状态**：**✅ 已裁定（第八轮，D67–D73 七条全取推荐、零偏离）。仍是契约、不是实现 —— 零代码、零迁移、零 Settings 字段。** Q1–Q7 已编号并同步 `00` 册：**D1–D73 / 74 个节点零待决**（第八轮当时的计数）。裁定记录见 §7 第八轮与 §13.6；逐条依据见 §5。**七条全取 (a) ⇒ §1／§6 两条边界均未修订。** **2026-11-22 第九轮**：§13.9 按 D74 补登记四个 VIX 只读维度，D67–D73 七条结论与 §1／§6 两条边界一字未改；决策编号现为 **D1–D79 / 80 个节点零待决**。

### 13.1 思路判断

**方向成立，但不能照抄这张表落地。** 值得做的部分是「把当天已发生的事压成一个可复核的标签，并喂给既有量化层」；这正是 §1 里「量化层已工作、缺的是判断层」的延伸。但按代码实测有 4 个硬问题：

| # | 问题 | 判断 |
|---|---|---|
| 1 | **5 个输入里 4.5 个没有生产者**（§13.3） | 「不用接暗池真数据」这句成立，但**不等于不用接数据**。VIX 日内高、10Y、金、費半、NVDA 分鐘斜率全库皆无；只有 `pulse.py` 的延迟流式报价 ＋ 小时 `ib-delayed` 快照可用，且不存 tick 序列／日内高。照抄规则会得到一个永远靠猜的标签 |
| 2 | **与 §1／§6 边界直接冲突** | §1 明写「A new strategy… no drafts, no orders, no tactic changes」、§6 明写「No new market data sources」。R7 既要新数据源，又要喂打分表（可能被 `wheel.py` 当开门控），还提出「只減不買／不加倉」这类动作指令。**裁定后已消解**：D67 只读注解、D68 降級 `unknown` 不新增源、D70 不新开页 ⇒ 两条边界一条都不用改（见 §13.7） |
| 3 | **规则形态不对：5 条独立 if-then 抓不到真正的信号** | 操作者自己的结语「債金不同步的反彈，都是門，不是底」是**联合条件**，但表里 5 条是各自独立的单信号判定。且「股跌債也跌」讲的是**债券价格跌＝收益率升**，输入却定义成「10Y 收益率」，符号会反。「金跌」在股跌与股弹两种情形下含义相反（11/20 是流動性擠兌、11/21 是空頭回補），单看黄金无解 |
| 4 | **样本与验收口径不可证伪** | 5 个日期是叙事挑選的同一段 regime（n≈3 週），「還原 80% 機構意圖」既无标注也无反例。本仓 `01/04/09` 册的「證偽／sanity／band_gap」惯例不接受这种验收。且「機構意圖」是不可观测量，标签不该声称还原它 |

**建议的形态**：定义成「**午间可观测状态分类器**」—— 输出是价格／利率／黄金／波动率／消息标签的**联合状态**，不是机构意图、不是价格预测；规则用确定性真值表（可复核、可留痕），LLM 只在自由文本消息分类这一处出现且必须带 `unknown` 兜底。

### 13.2 与既有裁定／迭代的冲突（已由 D67–D73 消解）

| 既有口径 | 冲突点 | 裁定后的处置 |
|---|---|---|
| §1「no orders / no tactic changes」 | 「只減不買／不加倉」若由工具强制执行，就是新增门控 | **✅ D67**：只读注解、绝不改既有门控 ⇒ §1 不修订 |
| §6「No new market data sources」 | 5 个输入全部需要新源 | **✅ D68**：降級 `unknown`、不新增源 ⇒ §6 不修订；**D73** 就地标注缺失原因 |
| D60「两跑、NY 锚定」 | 午间 12:30 ET 是**第三跑** | **✅ D71**：不替换 Run A/B；新增 **Run C**，由 R3 的 NY 锚定派生，单次 12:30 ET |
| R5「`VIX > 38` 触发线」 | VIX 规则重叠 | R5 = 操作者设的**静态阈值触发线**（`source='trigger'`）；R7 = **时间窗内相对状态**（spot vs 早盤高）。语义不同，不合并、不复用同一行 |
| D62 / `guardian_alerts` | 标签是时序，不是待确认告警 | **✅ D69**：不进 ack 状态机、不加 `source='regime'`（`'trigger'` 已预留给 R5）；独立 append-only `regime_labels` 表 |
| ~~R3~~ / R4 未交付 | R7 没有定时跑、没有推送通道 | **✅ R3 已交付（§14／§15，ET 锚定调度与 Run C 派生已就位）**；R7 仍依赖 **R4 的推送通道**，排期在 R4 之后；不能独立开工（§13.7 不变） |
| 面板 / `/portfolio-analysis` | 新标签要落在哪 | **✅ D70**：扩 `/portfolio-analysis` 现有标签卡；不新开页、`/guard` 保持纯 ack 面 |
| 验收口径 | 「80% 機構意圖」不可证伪 | **✅ D72**：可重放 `evidence`、无准确率声明；明確否決 80% |

### 13.3 数据可得性实测（2026-11-21，只读核实，非推测）

| 输入 | 库内生产者 | 状态 |
|---|---|---|
| VIX 现值 ＋ 早盤高 | 无。全库 `VIX` 只出现在计划文档的 R5 示例 | ❌ |
| 10Y 美债收益率 / `^TNX` / 债券价 | 无（`docs/athena/12-short-horizon-forecast-rnd.md` 仅 R&D 设想） | ❌ |
| 黄金 GLD / GC / XAU | 无专用源；`GLD` 只是 `UNIVERSE_SEED` 里的字符串；D52 指导篮子提到 `GLD`/`TLT`/`SOXX` 但代码默认 `guidance_symbols` 并不含它们 | ❌ |
| 費半 / SOX / SMH / SOXX | 无独立生产者，只是可放进 watchlist 的普通 ticker | ❌ |
| NVDA 早盤 vs 午盤斜率 | 无。`market_bars` 只有日线与小时快照（同日合并成一根日线）；唯一 slope 是 `indicators.py` 的 21 session 跨日 `trend_slope_pct` | ❌ |
| 盘中即时价 | **有但有限**：`pulse.py` 延迟流式 tick，仅按小时 `upsert_delayed()` 落库，不存 tick 序列、不存日内高；订阅限于 watchlist ＋ 持仓股 ＋ 在途股票单 | ⚠️ |
| 消息／事件标签 | **有**：`news.py` 的 Yahoo RSS / Finviz / FOMC 日历 / Nasdaq 财报日历 / 手工 URL；`news_events.event_type` 半结构化。但 `score_title()` 只产出 5 个码，`AI_REGULATION` / `ANTITRUST_FINE` / `NARRATIVE_ATTACK` / `SUPPLY_CHAIN` 定义了却从不产生；**无 Fed 官员讲话正文、无 SEC／监管抓取** | ⚠️ |
| LLM 分类能力 | 有：`agents.py` 的 News agent（schema 校验）、`dsh.read_article()` / `enrich_sentiment()`、`forecast_marks` 的 `risks[].code` | ✅ 可复用 |

**结论（D68 / D73 已裁定）**：延续 D66「无生产者的数据一律留空并记旗标、不臆造」的纪律 —— 缺任一输入 ⇒ 该维度记 `unknown`，标签最多降到 `insufficient_data`，**不允许用叙述里的数字补位**。**D73**：每个 `unknown` 维度必须就地附一条固定原因（如 `VIX n/a (no intraday-high feed in store)`），区分「库里没有生产者」与「今天没抓到」，同一行推送与面板卡都带 —— `insufficient_data` 不得是静默空白。`VIX > 30 / > 35` 只是操作者给的候选阈值，标注**待校準**，不进验收。

### 13.4 建议的规则形态：三轴联合矩阵

> **2026-11-22 第九轮补登记**：`VIX 水位` 一栏已按 **D74** 扩为四个只读维度，清单与封闭词表见 **§13.9**；本矩阵其余列与 D67–D73 结论不变。

把 5 条独立规则改成「**股 × 債(收益率) × 金**」方向矩阵 ＋ VIX 水位 ＋ 量能 ＋ 消息标签，一次判定一个标签：

| 股 | 10Y 收益率 | 金 | VIX 水位 | 量能 | 消息标签 | 候选 label | 燈 |
|---|---|---|---|---|---|---|---|
| 跌 | 升（債價跌） | 跌 | 高位再升 | 放量 | 聯儲／監管／SEC | `liquidity_squeeze` | 🔴 |
| 彈 | 降（債價升） | 跌 | 早高回落但 >30 | 縮量 | 只有 IPO／單股財報／空頭回補 | `short_cover_bounce` | 🟠 |
| 跌 | 降（債價升） | 升 | 回落 | 正常 | 無重大標籤 | `normal_risk_off` | 🟡 |
| 早紅→午綠 | — | — | — | 午間放量 | 個股做空／融資質疑 | `distribution_into_strength` | 🟠 |
| 任一軸 `unknown` | — | — | — | — | — | `insufficient_data` | ⚪ |

- **方向统一**：一律以「10Y 收益率升／降」表述，避免「股跌債也跌」的債價／收益率符号反转。
- **消息标签**只做二分类：`narrative_only`（IPO 路演／單股財報／空頭回補 ⇒ `dead_cat` 候选）与 `regime_shift`（聯儲官員講話 ＋ 監管/SEC ＋ 債市異動**同时**出现）。「同时」是硬条件，缺一不算。
- **收盤前瞻**是**定性** `close_bias ∈ {risk_off_into_close, bounce_likely_fades, neutral, insufficient_data}` ＋ 一句原因，**不是价格预测**。理由：库里已有 `forecast_marks` 的数值预测（默认关闭、shadow，输出 `bid/ask/stop/take_profit/dir_conf`，horizon 单位是 sessions），再叠一个价格预言机会重复 D54 删掉的「假旋钮」；`02` 册也早已否定 MPT 式最优化。
- **不声称「还原机构意图」**：标签只描述可观测状态。操作者原话可存进风控档案：「午間的反彈，先看債和金，再看消息；債金不同步的反彈，都是門，不是底。」

### 13.5 输出与落点（已裁定 D69 / D70 / D71 / D73）

- **一行推送**（12:30 ET 示例形）：`🟠 11/21 午間：VIX 34（早高 40 回落）、NVDA 抽 +3% 無量、10Y 4.94%↓、金跌 →【空頭回補反抽】。close_bias: bounce_likely_fades。`
- **降級形（D73）**：`⚪ … VIX n/a (no intraday-high feed in store)、10Y n/a (no ^TNX feed)、金 n/a (no gold feed) →【insufficient_data】。close_bias: insufficient_data。` —— 每个 `unknown` 就地给原因，推送与面板卡同带。
- **标签词表**（封闭，禁止 `risks[].code` 那种自由串）：`liquidity_squeeze / short_cover_bounce / distribution_into_strength / normal_risk_off / regime_shift / dead_cat / insufficient_data`。
- **存贮**：独立 append-only `regime_labels(account_id, day, run, label, close_bias, evidence jsonb, inputs jsonb, created_at)`，一跑一行；`evidence` 存「哪个输入 → 哪条规则命中 → 得出什么」，用于复核。**不复用 `guardian_alerts`**（它是 `open/skipped/resolved/auto_cleared` ack 去重语义，部分唯一索引会挡掉同键历史）。**号段口径已由 R3 修正（§14.2）**：`054` 已归 `054_guardian_runs.sql`，R7 的 `regime_labels` 顺延到 `055`；此处仍仅为预留，未落地、未 apply。该修正只动号段登记，D67–D73 七条裁定与 §1／§6 边界均不变。
- **喂给打分表（✅ D69 组合级存一次）**：全库无字面叫「打分表」的表；最贴近的是 `indicators.py` 的 `Indicators`（14 字段、落 `symbol_phases.indicators`，被 `phases.evaluate_symbol()` / `agents.py` prompt / `wheel.py` 开门控 / `screener.py` 消费），另有 `advisor.py:_rule_fallback()` 字面 `score = 0.0` 写 `advisor_signals`。注意 `Indicators` 已有 `regime_unknown`（基于 `regime_index = "SPY"` 日线的**跨日** regime）—— R7 是**当日盘中**状态，**必须换名字**（`session_regime`），不可覆盖。市场级标签是组合级，逐标的写进 JSONB 会复制 N 份 ⇒ **裁定存一行 `regime_labels`、在 `agents.py:_context()` 注入一行全局上下文**；`wheel.py` 不做逐标的读取（D67 不 gate）。
- **面板（✅ D70）**：`pages.py:1698-1699` 的 `/portfolio-analysis` 已有一张展示态 `RISK OFF / CAUTION / RISK ON` 卡（由 `advisor_signals` 推导、无时序）。**裁定在此扩一张「午間 Regime」卡**，不新开页；`/guard` 保持纯 ack 动作面（D62 语义不动）。

### 13.6 第八轮裁定记录（Q1–Q7 → D67–D73，2026-11-21）

**七条全取推荐、零偏离、无跳过。** 分两轮逐条问（Q1–Q4 ／ Q5–Q7），每条带具体案例；Q2 的回答附一句「but we should advise user why VIX is missing」，该附注拆成 Q7 单独再问，落为 D73。裁定记录亦见 §7 第八轮，逐条依据见 §5。

| 问 | 决策 | 问题 | 选项 | 裁定 |
|---|---|---|---|---|
| **Q1** | **D67** | 标签的权限：只读注解，还是可压制加仓？ | (a) 只读注解、绝不改既有门控 (b) 可 gate：🔴/🟠 时抑制 `wheel.py` 新开仓 (c) 直接给动作指令 | **✅ (a)** |
| **Q2** | **D68** | 4.5 个输入无生产者怎么处理 | (a) 降級 `unknown`、不臆造 (b) 新增轻量行情源 (c) 操作者粘贴 → LLM 分类 | **✅ (a)**；附注拆出 **Q7 → D73** |
| **Q3** | **D69** | 落点与粒度 | (a) 组合级存 `regime_labels`、注入 `agents.py` 全局上下文 (b) 逐标的加 `Indicators.session_regime` (c) 两者都做 | **✅ (a)** |
| **Q4** | **D70** | 面板位置 | (a) 扩 `/portfolio-analysis` 现有标签卡 (b) `/guard` 加只读卡 (c) 新页 `/regime` | **✅ (a)** |
| **Q5** | **D71** | Run C 与推送 | (a) 单次 12:30 ET 跑 (b) 12:30 ＋ 15:30 ET 覆核两跑 (c) 沿用 5 分钟 cycle 不做 ET 锚定 | **✅ (a)** |
| **Q6** | **D72** | 验收口径 | (a) 可重放 `evidence`、标签可由存下输入复现 (b) 以「还原机构意图 80%」为验收 (c) 先 shadow-only 暂缓验收 | **✅ (a)**；**明確否決 (b)** |
| **Q7** | **D73** | 缺失原因如何呈现（由 Q2 附注拆出） | (a) 每个 `unknown` 维度就地标注 (b) 单列 coverage footer (c) 只在面板卡显示 | **✅ (a)** |

**明确否决**：Q1 (b)(c) 改行为／造第二套决策面；Q2 (b)(c) 新数据源／人在回路；Q3 (b)(c) N 份复制／双真源；Q4 (b)(c) 混入 ack 面／第三页；Q5 (b)(c) 多一次打扰／跨 DST 漂移；Q6 (b) 不可证伪、(c) 无限期推迟验收；Q7 (b)(c) 理由离一眼之外／Telegram 看不见。**七条全取 (a) 的净效果**：§1／§6 边界零修订、零新增数据源、零 gate、零新页、零 nag 循环。

### 13.7 边界与依赖

- **依赖**：R3（ET 锚定调度／Run C）＋ R4（推送通道）＋ R2（面板），并需与 R5 的 `VIX > 38` 静态触发线划清语义。**R7 不能独立开工**，排期在 R3/R4 之后；放在 R7 只是编号，不代表可越过脊柱。
- **本轮（第八轮裁定）不做**：零代码、零迁移、零 Settings 字段、不改 `advisor.py` / `wheel.py` / `drafts` / `guardian.py`、不接暗池数据、不产生订单、不新建价格预测模型。**R7 实现仍待 R4 落地后另起台账**（R3 已于 2026-11-22 交付，见 §14／§15；ET 锚定调度与 Run C 派生已就位）。
- **边界修订状态**：D67–D73 全取 (a) ⇒ **§1「no drafts, no orders, no tactic changes」与 §6「No new market data sources」均保持原文、零修订**。若日后要启用 gate（Q1-b）或新行情源（Q2-b），须**另开一轮裁定显式修订这两条**，不得在实现里顺手改掉。
- **核实校正**：§8 第 1 条写「`forecast_marks.horizons.*.stop` … 已被 `advisor.py` 消费」不够精确 —— 实测 `advisor.py` 不读取 `stop`，只把整份 `payload` 带进 `advisor_signals`；`stop` 随包带入但不参与任何判定。**D66 结论不变**（它仍是假朋友）。此处只记口径校正，不改写历史契约。
- **案例库性质**：§13.8 的 2026/11 对照表由操作者提供、**未经独立核实**，仅作规则校准的工作案例；不得写成数据源、不得作为回测基准、不得把其中具体人名／机构／数字硬编码进代码或阈值。

### 13.8 工作案例（操作者提供，未核实，仅供校准）

| 日期 | 盤面 | 消息 | 債／金 | 操作者给的判定 |
|---|---|---|---|---|
| 11/4–11/5 | 納指 -5.5~6.36%、費半 -12% | 中期選舉 → AI 監管預期 | 10Y 單週 +30bp | 被動去槓桿 |
| 11/9–11/11 | 反彈 3.4%、VIX 19.9→19.3 | AI 需求敘事重燃 | CPI 超預期但被蓋住 | 機構換倉 |
| 11/19 | 高開 +1% → 收 -2.25% | 個股做空報告 | 金同跌 | 借高開出貨 |
| 11/20 | 納指 -5.28%、VIX 盤中 40.03 | SEC／監管、作證 | 股債金同跌 | 流動性擠兌（最危險） |
| 11/21 盤後 | 超賣反抽 +2~4%、無量 | 無實質利好 | 債穩、金跌 | 空頭回補反抽 |

> 樣本 n=5 且集中在同一段 regime，僅用於把 §13.4 的矩陣逐格走一遍；不作為準確率證據。**第九轮 D78 追加第 6 例（11/13 VIX 低檔連升／短端先行／單股 IV）**，同样未核实、仅供校准，不作验收、不作回测基准。

### 13.9 VIX 预警维度补登记（2026-11-22，第九轮 D74／D75／D76／D77／D79）

> **性质**：本小节是 **D74 的登记**，不是新裁定。D67–D73 七条结论、§1／§6 两条边界**一字未改**；补登记只读维度清单、封闭词表与 interim 缺值口径，供 R7 实现时直接用。来源企划见 §16。

**四个只读可观测维度**（§13.4 的 `VIX 水位` 一栏扩为四栏，仍是同一张联合矩阵、同一次 12:30 ET Run C 判定）：

| 维度 | 描述可观测状态 | 封闭取值 | interim 缺值原因（D73／D79） |
|---|---|---|---|
| `vix_slope` | VIX 低档连升／斜率 | `rising_from_low / flat / falling` | `VIX n/a (no VIX daily-close feed in store)` |
| `vix_term` | 短端 vs 30 天 vs 3M vs 1Y 的相对关系 | `front_end_hot / contango / flat / inversion` | `VIX1D / VIX9D / VIX3M / VIX1Y n/a (no VIX-term feed in store)` |
| `single_name_iv` | 核心持仓的 IV 状态 | `elevated / normal` | `single_name_iv n/a (no measured IV; estimated_iv() returns None; iv_rank is a price percentile)` |
| `rates_divergence` | 10Y 周变动 × 股指方向 | `rates_tightening / aligned` | `10Y n/a (no treasury-yield feed in store)` |

- **每维必带 `unknown`**，且 `unknown` 就地附固定原因；任一 `unknown` ⇒ 整体最多 `insufficient_data`（D68/D73）。**区分「库里没有生产者」与「今天没抓到」**：本批次全部属前者，原因是没有 feed，不是当天抓取失败。
- **命中维度数**（D77）：0–4 的**展示用**计数，落在 §13.5 已裁定的 `/portfolio-analysis` 同一张卡；**不是推送门槛、不是动作等级**，`/guard` 保持纯 ack 面（D59/D62）。
- **只读状态标签**（D76）：`pre_warning / elevated / critical` 仅作状态命名，**不附动作指令**（「減倉／清一半／只賣」一律不进工具）、不 gate `wheel.py`、不产生订单或草案、不进 5 分钟 nag（D67／§1／D71）。操作者附注：NVDA/MRVL/INTC 只是举例，**不得硬编码进代码或阈值**（§13.8 同纪律）。
- **`evidence.inputs`**（D72）：阈值 `<20`、`+20%`、`IV Rank >90`、`+20bp`、`>4.5`、`0.936`、`VIX>25` 一律标 **待校準**，只作输入记录，**不进 Settings、不进验收、不硬编码**。
- **`label` 词表不变**：仍为 §13.5 的 7 个封闭值；四个维度是 `inputs` 的观测状态，不新增 `label` 值、不改 `close_bias` 词表、不新增 `source`（`'trigger'` 仍留给 R5）。
- **存贮不变**：仍是一行 `regime_labels`（§13.5，号段 `055` 预留）；四个维度与命中数进 `inputs jsonb` / `evidence jsonb`，**不加列、不加迁移**。
- **D75／D79 的 interim**：操作者要「新增行情源／用 IBKR 建」的意图已记录，但 §6 未修订、下一轮才定 feeds ⇒ **本批次四个维度全部按上表 `unknown` 呈现**，不得用叙述里的 11/13 数字补位。下一轮显式修订 §6 后，才把对应维度从 `unknown` 切到真值；届时须同步更新本表原因字符串。

---

## 14. R3 实现口径（2026-11-22，落地前的契约）

R3 = **ET 锚定的定时巡检 ＋ 每跑一次把 grade 写进 store**。它不改分级规则（R1）、不改告警状态机（R2）、不改门控／草案／订单（§1／§6）。全部口径由 **D60**（两跑、NY 锚定）、**D71**（Run C 12:30 ET、由 R3 派生）、**§4 R3 行**、**§12 R2 接口**推导，**不是新决策、不新增 D 编号**；D1–D73 / 74 个节点零待决保持不变（R3 交付当时的计数；第九轮后为 D1–D79 / 80 个节点，见 §7 第九轮）。

### 14.1 时间与 DST（D60）

统一以 **`America/New_York` 墙上时钟**定义目标，再用 `zoneinfo` 按该交易日换算 UTC；**不复用** `athena_exec/slots.py` 的 `US_STANDARD_FROM = date(2026, 11, 2)` 硬编码切日 —— 那是一次性替换 `21:30-04:00` HKT 窗口的逻辑，不是 IANA DST 规则，跨年会失效。`US_OPEN = 09:30 ET`：

| 跑 | 定义 | ET 墙上钟 |
|---|---|---|
| **A** | `US_OPEN − 60min` | 08:30 |
| **B** | `US_OPEN + 5min` | 09:35 |
| **C**（R7，D71） | 单次固定 | 12:30 |

换算后的 HKT 墙上钟即 §4 的验收口径，与 D60 问询案例逐格一致：

| 偏移 | A UTC | **A HKT** | B UTC | **B HKT** | C UTC | C HKT |
|---|---|---|---|---|---|---|
| EDT（UTC−4，如 2026-07-01） | 12:30 | **20:30** | 13:35 | **21:35** | 16:30 | 00:30 (+1d) |
| EST（UTC−5，如 2026-12-01） | 13:30 | **21:30** | 14:35 | **22:35** | 17:30 | 01:30 (+1d) |

D60 案例原文「NY 锚定给出 20:30/21:35 或 21:30/22:35」即上表 A/B 两列；固定 HKT 时钟会在冬季把「开盘后复查」提前到开盘前 55 分钟，正是 D60 否决 (b) 的原因。

### 14.2 迁移号口径修正（只改号段，不动任何裁定）

已提交的 §13.5 与 00 册 §12 曾写「R7 `regime_labels` 号段自 `054` 起顺延，仅预留未落地」。R3 是**第一个真正落地的后续迁移**，按 §12「号段被占用则顺延、不插号」的既有纪律：`054` 归 **`054_guardian_runs.sql`**，R7 的 `regime_labels` 顺延到 **`055`**。这是号段登记的技术修正，**D67–D73 七条裁定、§1／§6 两条边界、R7 契约冻结状态一律不变**；R7 实现时再从 `055` 起，不回填历史。

### 14.3 存贮：`guardian_runs`（append-only）

不复用 `guardian_alerts` —— 它是 `open/skipped/resolved/auto_cleared` 的 ack 去重状态机，且 `guardian_alerts_live` 部分唯一索引会挡掉同键历史（§13.5 为 `regime_labels` 写过同一条理由）。新增独立表，一跑一行、按账户：

| 列 | 用途 |
|---|---|
| `account_id / trade_day / run` | 唯一键 `(account_id, trade_day, run)` ⇒ 同一跑重复触发由 `ON CONFLICT DO NOTHING` 幂等吞掉 |
| `scheduled_at / ran_at` | ET 派生的计划时刻与实际执行时刻（timestamptz，UTC 存） |
| `portfolio_tier / account_tier` | 五档分级字符串，面板直接读 |
| `breach_count / proximity_count / unmanaged_count` | R1 `GuardianReport` 三个计数 |
| `el_nl_pct / maint_nl_pct` | 可空 numeric；R2 §11.3 的诚实留白延续到持久化，`None` 不臆造 |
| `account_flags / positions` | jsonb：账户旗标数组、逐标的 `{symbol, tier, flags, unmanaged}`，可重放本次 grade |
| `status / detail` | `ok` 为主；失败／降级留痕（R4 的 dead-man 会消费，R3 不实现告警） |

`/guard` GET 仍调 `sync()` 出「当前实时」五色板；R3 另加一张只读「Scheduled runs」卡展示最近 6 行，让操作者看得见「上次定时跑是什么时候、结论是什么」。表不存在时静默降级为空卡（与 `_nav_badges()` 的 try/except 同构），**不因迁移未跑而全站报错**。

### 14.4 调度形态与接缝

- 新增纯模块 `guardian_schedule.py`（零 I/O）：`run_moments(day)`、`next_run(now, runs)`、`hk_clock()`；`RUNS = ("A","B","C")`、`EXECUTED_RUNS = ("A", "B")`。
- 新增薄适配 `guardian_runner.py`：复用 `pages._guard_inputs()`（lazy import，不复制 ~200 行 SQL）→ 原样调 `guardian_view.sync()` → 把 `GuardianReport` 序列化写一行 → `conn.commit()`。
- 在 `loop.py` 起一条 **daemon 线程**，由 `next_run()` 算出下一次 fire 并用 `Event.wait()` 精确等待；**不改** 5 分钟 `cycle()` / `_cycle_sleep()` / `_bootstrap_cycle()` 语义，不引入 cron / APScheduler。`ops restart` 时 `stop_guardian_scheduler()` 与 `stop_tunnel()` 并列调用。
- **Run C 只派生不执行**：`run_moments()` 已给出 12:30 ET 的 UTC 时刻（DST 安全），但 R3 的 `EXECUTED_RUNS` 不含 `C`，不写 `guardian_runs` 行、不推送。R7 只需接一个 handler 参数 `runs=("C",)` 并写自己的 `regime_labels`，**不必重做调度**。D71 的「由 R3 的 NY 锚定派生」即由此兑现。

### 14.5 交易日口径与诚实留白

全仓**无** US market holiday / trading-day helper（只读核实），且 §6「No new market data sources」不允许为此新增源 ⇒ R3 **只按 ET 工作日（Mon–Fri）触发**，周末跳过。**US 法定假日不在过滤范围**：假日会多一次无害的 grade 行（无订单、无草案、无改动，§1 边界不动），不臆造一张硬编码假日表。这是已知留白，与 D66「无生产者一律留空并记旗标」同纪律；若日后要有真假日历，须显式修订 §6 并另起裁定。

### 14.6 幂等、重启与并发

- **幂等**：`guardian_runs` 唯一键 ＋ `ON CONFLICT DO NOTHING`；`sync()` 的 `reconcile()` 本身幂等（D62），`guardian_alerts_live` 部分唯一索引兜住并发重复 INSERT。
- **重启不回补**：`next_run()` 永远算「下一个未来时刻」；进程 10:00 ET 起来就等明天 A，不补今天 A/B。已在库里的行不重写。
- **并发**：daemon 线程与 `/guard` GET 可能同时调 `sync()`；二者都是 `load_alerts → reconcile → apply_plan`，冲突由部分唯一索引 ＋ `ON CONFLICT DO NOTHING` 收敛，不新增锁。

### 14.7 验收（§4 R3 行，逐条可测）

1. EDT / EST 两种偏移下 A/B 的 HKT 墙上钟分别为 **20:30/21:35** 与 **21:30/22:35**；Run C 派生为 00:30/01:30 (+1d)。
2. `next_run()` 在 A 前 → 今日 A；A 与 B 之间 → 今日 B；B 之后 → 下一工作日 A；周五 B 之后跳过周末 → 周一 A。
3. 同一 `(account, day, run)` 写两次只留一行；`sync()` 被调用、`ON CONFLICT` 在 SQL 里。
4. `/guard` 只读卡渲染、所有动态文本转义；表缺失时降级为空卡且 GET 仍 200。
5. 真实 Postgres：`054` 迁移可 apply、表／唯一键／索引齐全、`_statements()` 切分正确。
6. 两轮变异测试：改坏 DST 偏移或去掉 `ON CONFLICT` 必须 FAIL，还原后 `cmp=0`、全量回归通过。

### 14.8 明确不做（R3 边界）

不实现 Telegram 催办／dead-man（R4）、不实现 `VIX > 38` 等静态触发线（R5）、不实现常开机器／心跳（R6）、不实现 `regime_labels` 联合矩阵／`close_bias`／`evidence`／面板标签卡（R7）；不新增 Settings 字段、不改 `drafts.decide()` / `advisor.py` / `wheel.py` / `guardian.py`、不接新行情源、不产生订单、不改 5 分钟 cycle 语义。

---

## 15. R3 交付记录（2026-11-22）

| 项 | 内容 |
|---|---|
| 状态 | ✅ **已实现并验证**。`C-20261122-1` |
| 代码 | 新增 `054_guardian_runs.sql`、`guardian_schedule.py`、`guardian_runner.py`、`test_guardian_r3.py`；修改 `guardian_view.py` / `pages.py` / `loop.py` / `packages/store/pyproject.toml` / `00` 册 / `CHANGELOG.md` / `logs/2026-11-22.md`。契约见 §14 |
| 接口 | `guardian_schedule.run_moments()` / `next_run()` / `hk_clock()`（纯）；`guardian_runner.run_once()` / `write_run()` / `load_runs()` / `render_run_history()` / `start_guardian_scheduler()` / `stop_guardian_scheduler()`；`render_board(..., extra_cards="")` 向后兼容。**R2 `sync()` 原样复用，未改一行** |
| 验证 | ① R3 单测 **71/71**；② 全量 **362/362**（`skipped=2`）；③ `uv lock --check`（29 packages）、`compileall`、import smoke、`git diff --check` 全绿；④ 台账机检 **100/100 `EXIT=0`**；⑤ **真实 Postgres 一次性容器** `athena-r3-verify-55440`：`apply_migrations()` applied 50 且含 `054`、17 列 ＋ `guardian_runs_latest` ＋ 唯一约束齐全、`_statements()` 切 2 条；同一 `(U1, day, A)` 连跑两次只留 1 行、再跑 B 得 2 行；`load_runs()` 回 `20:30/21:35 HKT`；带卡 GET body 仍以 `<p class="note">` 开头 ⇒ `R3_LIVE_ALL_OK`；⑥ **两轮变异测试均如期 FAIL**：固定 UTC−4 偏移 ⇒ 24 例 `failures=5`；删 `ON CONFLICT` ⇒ 8 例 `failures=1` `EXIT=1`；两次 `cp` 还原 `cmp=0`、全量恢复 362/362；⑦ 容器与临时文件已清理 |
| 边界 | **未做**：Telegram 催办／dead-man（R4）、静态触发线（R5）、常开机器／心跳（R6）、`regime_labels` 矩阵／`close_bias`／`evidence`／标签卡（R7）；不改 R1 分级、R2 状态机、`drafts.decide()` / `advisor.py` / `wheel.py` / Settings、5 分钟 cycle 语义、任何订单／门控／行情源。**已知留白**：无 US market holiday helper ⇒ 只按 ET 工作日触发，假日多一次无害 grade 行（§14.5）；重启不回补错过的跑（§14.6）；Run C 只派生、不写行、不推送（§14.4） |
| 提交 | 本批次独立提交；hash 以 `git log -1 --format=%h` 为准，**不写入本文档** —— 文档与代码同一批次提交，自指不可能，沿用 §9／§12 惯例 |

---

## 16. R7 补充企划：VIX 预警视角（2026-11-22，✅ 第九轮已裁定 D74–D79）

> **来源**：操作者 2026-11-22 贴出 11/13（上上週五）的「VIX 斜率 ＋ 期限結構 ＋ 單股 IV」三件套回顾与 5 条预警规则，要求「update R7 with a few more VIX views」。
> **状态**：**✅ 已裁定（2026-11-22 第九轮，Q1–Q6 → D74–D79）。六条中五条取推荐、一条明确偏离（Q2／D75）、零跳过。** 裁定后按 Q1 裁 (a) 把四个只读 VIX 维度**登记进 §13 的 §13.9**（§13.4 加 pointer），**D67–D73 七条结论、§1／§6 两条边界一字未改**。决策编号扩到 **D1–D79 / 80 个节点零待决**。裁定记录见 §7 第九轮与 §16.5，边界同步见 §16.6。**仍是零代码、零迁移、零 Settings 字段、零运行时改动**；企划原文见 `C-20261122-2`，本批入库见 `C-20261122-3`。
> **相关**：§13.3 数据可得性、§13.4 三轴联合矩阵、**§13.9 VIX 维度补登记**、§13.5 输出与落点、§13.7 边界、R5 静态触发线、`C-20261122-2`（企划）／`C-20261122-3`（裁定）。

### 16.1 思路判断：方向成立，但「照贴」会撞 4 条已裁定边界

**值得做的部分**：把 R7 的 VIX 从 §13.4 单一「VIX 水位」一栏，扩成**多维可观测状态**（斜率／期限结构／单股 IV／股债背离），并用封闭词表 ＋ 命中维度数呈现。这与 §13.4「确定性真值表、可复核、可留痕」同构，也正好补上 §13.3 已承认的资料缺口。

**但按 §13 已裁定口径与 2026-11-22 代码实测，原提案有 4 个硬冲突**：

| # | 冲突 | 依据 | 判断 |
|---|---|---|---|
| 1 | **5 条规则的输入全部没有生产者**（§16.2） | §13.3 实测 ＋ 本节复核 | VIX 日线收盘序列、VIX1D／VIX9D／VIX3M／VIX1Y、真单股 IV Rank、10Y 周变动、股指方向 —— 全库皆无。照贴会得到「永远 `insufficient_data`」或「靠猜」的规则，与 D66「无生产者不臆造」同病 |
| 2 | **黄／橙／红灯带动作指令** | 「減交易倉、不加倉」「當週把高 beta 清一半」「只賣」「把 NVDA/MRVL/INTC 減一半」 | **D67** 明裁「只讀註解、絕不改既有門控」，Q1 (c)「直接給動作指令」已明確否決；§1「no tactic changes」。動作指令只能留在操作者自己的風控檔案，**不能進工具** |
| 3 | **「貼進工具設定頁的觸發卡」＝ 新 Settings 閾值 ＋ 新告警推送** | 「alert crossing up / once per bar close」「≥2 分就推黃燈」「TradingView compare 面板／IBKR SMS」 | 閾值進 Settings 會重複 **D66** 剛否決的「假旋鈕」（寫著會做事、實際沒來源）；新推送通道撞 **D71**（單次 12:30 ET、不進 5 分鐘 nag）；靜態閾值觸發線的職責屬 **R5**（`source='trigger'`，§13.2 已劃界） |
| 4 | **11/13 單一樣本當驗收** | 「11/13 亮了 4–5 條 → 應該是橙燈 → 當週減一半，不是等 11/20 VIX 40 才動」 | 與 **D72** 明確否決的「還原 80% 機構意圖」同類：n=1、敘事挑選、無標註無反例、後見之明。§13.8 已把操作者對照表標為「未核實、僅供校準」，同一紀律適用 |

**结论**：可以「加 VIX 视角」，但只能以 **§13 既有边界内的只读注解维度** 加入 —— 每个新维度缺值即 `unknown` ＋ 就地原因（D68/D73）、封闭词表、不 gate、不推送、不改 Settings、不把 11/13 当验收。第九轮已裁定：**放进 R7 §13.4 只读注解维度**（Q1 → D74），四个维度与封闭词表登记于 §13.9；「新增行情源／用 IBKR 建」的意图由 D75 记录、D79 排到独立裁定轮。逐条见 §16.5。

### 16.2 数据可得性复核（2026-11-22，只读、非推测）

| 提案规则 | 需要的输入 | 库内生产者（实测） | 状态 |
|---|---|---|---|
| ① VIX 低档连升 | VIX 连续 2 日收盘 ＋ 起点 <20 | 全库 `.py` 中 `VIX` **零命中**；`market_bars` 只有日线／小时快照且同日并成一根日线，无 VIX 标的 | ❌ |
| ② 短端先行 | VIX1D 日涨幅、VIX9D/VIX 比值 | `VIX1D / VIX9D / VIX3M / VIX1Y` 全库**零命中**（含 docs）；无任何期限结构序列 | ❌ |
| ③ 单股 IV 极端 | 核心持仓真 IV Rank ＋ 到期期权隐含单日振幅 | **假朋友警告**：`athena_strategy/iv.py` 的 `estimated_iv()` 明确 `return None`（"No measured IV in this step. Callers must not invent one."）；`iv_rank()` 是对 `iv_proxy = credit/price` 的百分位，D4a 已证伪为**价格的反向百分位**，不是波动率标签；D36 已把门控换轴到 `rv_pctl`。`chain_marks` 只存 ATM 链 mid 时序（4b① 采集已落地、4b② 消费未实施） | ❌ |
| ④ 债市背离 | 10Y 周变动 bp ＋ 股指同期未跌 | `10Y / TNX / treasury / yield_curve / bond_yield` 在 `.py` **零命中**；默认 `guidance_symbols = ["QQQ","NVDA","SOXX","AVGO","MRVL","MU","TSM"]`，无 `TLT`、无 `GLD`（与 §13.3 口径一致） | ❌ |
| ⑤ 期限结构变陡／contango | VIX1Y−VIX9D、VIX/VIX3M | 同 ②；无任何 VIX 期限结构数据 | ❌ |
| （辅助）盘中价、消息标签 | 延迟 tick、事件码 | **有但有限**：`pulse.py` 延迟流式 tick 仅按小时 `upsert_delayed()` 落库，订阅限 watchlist ＋ 持仓股 ＋ 在途股票单；`news.py` 定义 9 个 `NEWS_TYPE_CODES`，但 `score_title()` 只产出 5 个（`EARNINGS_RELEASE` / `FED_RATE_DECISION` / `GENERIC_NEGATIVE` / `GENERIC_POSITIVE` / `GENERIC`），`AI_REGULATION`／`ANTITRUST_FINE`／`NARRATIVE_ATTACK`／`SUPPLY_CHAIN` 定义了却从不产生；无 Fed 讲话正文、无 SEC 抓取 | ⚠️ |

**结论（延续 D68 / D73）**：5 条规则所需输入 **5/5 无生产者**。不能照抄阈值，也不能把叙述里的 11/13 数字补位。特别提醒：**③ 的 `iv_rank` 是同名异义的假朋友** —— 若把「NVDA IV Rank 93.3」直接接到库里的 `iv_rank()`，会得到一个看起来合理、实际语义是「价格低」的读数，正是 D66 处理的那一类失效模式。

### 16.3 合规形态：把 5 条改写成「维度」而不是「触发器」

- VIX 从 `VIX 水位` 一栏扩成 4 个**只读可观测维度**：`vix_slope`（低档连升）／`vix_term`（短端 vs 30 天 vs 3M vs 1Y 的相对关系）／`single_name_iv`（核心持仓 IV 状态）／`rates_divergence`（10Y 周变动 × 股指方向）。
- 每个维度用**封闭取值 ＋ `unknown` 兜底**；任一 `unknown` 就地附固定原因（D73），形如 `VIX1D n/a (no VIX-term feed in store)`、`single_name_iv n/a (no measured IV; estimated_iv() returns None)`，区分「库里没有生产者」与「今天没抓到」。
- 「0–5 分」改为**展示用的命中维度数**，落在 §13.5 已裁定的 `/portfolio-analysis` 同一张卡（D70）；**不是推送门槛、不是动作等级**；`/guard` 保持纯 ack 面（D59/D62）。
- 黄／橙／红灯词汇可保留为**只读状态标签**（如 `pre_warning / elevated / critical`），但**不附动作指令**、不改 `wheel.py`、不产生订单或草案、不进 5 分钟 nag。
- `evidence` 沿用 §13.5：哪个维度 → 哪条真值表规则命中 → 得出什么，可由存下 `inputs` 重放（D72）。
- 阈值（<20、+20%、IV Rank >90、+20bp、>4.5、0.936、VIX>25）一律标 **待校準**，只进 `evidence.inputs`，**不进 Settings、不进验收**（D66/D72）。

### 16.4 与既有裁定／迭代的对照

| 既有口径 | 新提案 | 合规处置 |
|---|---|---|
| **D67** 只读注解、绝不改门控 | 黄／橙／红灯带「減倉／清一半／只賣」 | 剥掉动作指令，只留只读状态标签；§1 不修订 |
| **D68 / D73** 降級 `unknown`、就地标注原因 | 5 条硬阈值直接判灯 | 每条降为维度 ＋ `unknown` ＋ 固定原因；`insufficient_data` 不是静默空白 |
| **D66** 不臆造、价格线归 R5、不设假旋钮 | 「貼進工具設定頁的觸發卡」 | 阈值不进 Settings；静态触发线仍归 R5（`source='trigger'`），R7 只做时间窗内相对状态 |
| **D71** 单次 12:30 ET、不进 nag | 「≥2 分就推黃燈」 | 不新增推送；仍挂 R7 单次 Run C，通道复用 R4 但不进 nag |
| **D72** 可重放 `evidence`、否決准确率声明 | 「11/13 亮 4–5 條 → 應該橙燈 → 當週減一半」 | 11/13 标为**未核实工作案例（第 6 例）**，不作验收、不作回测基准 |
| **§6** No new market data sources | 5 个新行情源 | 不加源；若要真接 VIX 期限结构／真 IV／10Y，须**另开一轮裁定显式修订 §6**（§13.7 同纪律）。**第九轮 Q2（D75）明确选 (b)「新增行情源／用 IBKR 建」偏离推荐；由 Q2 拆出的 Q6 → D79 裁定另起「R7 数据源」裁定轮 ⇒ 本批次 §6 保持原文、零修订**（§16.5） |
| **R3 §14** Run C 派生 | 需要 12:30 ET 之外的连续监测 | 不新增 run；R3 的 `next_run(runs=("C",))` 已预留，R7 接 handler 即可 |

### 16.5 第九轮裁定记录（Q1–Q6 → D74–D79，2026-11-22）

**已裁定、不再待决。** 沿用「每個問題問我一遍」惯例，分两轮逐条问（Q1–Q4 ／ Q5 ＋ 由 Q2 偏离答案拆出的 Q6），每条带具体案例；**六条中五条取推荐、一条明确偏离（Q2／D75）、零跳过**。裁定后编号 **D74–D79**，`00` 册计数同步到 **D1–D79 / 80 个节点零待决**。

| 问 | 编号 | 问题（带案例） | 选项 | 推荐 | 裁定 |
|---|---|---|---|---|---|
| **Q1** | **D74** | 这 5 条 VIX 视角放哪？例：R7 已冻结契约 §13 vs R5 静态触发线 vs 新迭代 | (a) 作为 R7 §13.4 的**只读注解维度**扩展，不改 D67–D73 七条结论 (b) 归 R5 静态触发线 (c) 另开 R8 | (a) | **✅ (a)**；四个维度登记进 §13.9 |
| **Q2** | **D75** | 5/5 输入无生产者怎么办？例：`VIX1D n/a` | (a) 降級 `unknown` ＋ 就地原因（D68/D73），不加源 (b) 新增 VIX 期限结构／真 IV／10Y 行情源，须修订 §6 (c) 操作者手工粘贴 | (a) | **⚠️ (b) 新增行情源／用 IBKR 建（明确偏离推荐）**；操作者手写「add new feeds or build using IBKR」。**但见 D79**：本批次不改 §6，四维度暂 `unknown` ＋ 就地原因 |
| **Q3** | **D76** | 黄／橙／红灯的动作指令怎么办？例：「當週把 NVDA/MRVL/INTC 減一半」 | (a) 剥掉动作、只留只读状态标签 (b) 保留为纯文字建议（无门控） (c) 实现 gating，须修订 §1 | (a) | **✅ (a)**；操作者附注「symbols are for an example」⇒ 举例标的**不得硬编码进代码或阈值** |
| **Q4** | **D77** | 0–5 分与推送怎么落？例：「≥2 分就推黃燈」 | (a) 展示用命中维度数，挂 `/portfolio-analysis` 现有卡，不新增推送 (b) ≥2 分触发新 Telegram 告警，须修订 D71 (c) 不做计分 | (a) | **✅ (a)**；不新推送、不进 5 分钟 nag、`/guard` 保持纯 ack 面 |
| **Q5** | **D78** | 11/13 案例与验收口径？例：「應該是橙燈，不是等 11/20 VIX 40」 | (a) 标为未核实工作案例（第 6 例），验收仍用可重放 `evidence` ＋ 5 案例校准 (b) 以「11/13 应亮 4–5 条」为验收 (c) 先 shadow-only 暂缓 | (a) | **✅ (a)**；**明確否決 (b)**（n=1、后见之明挑选、无标注无反例，与 D72 同类） |
| **Q6**（由 Q2 偏离拆出） | **D79** | Q2 要新增行情源，但 §13.7 要求另起裁定轮显式修订 §6；且实测 IBKR 侧无 IND 合约路径、无期权 IV 度量 | (a) 另起「R7 数据源」裁定轮 (b) 本批次改 §6、IBKR 优先 (c) 本批次改 §6、5 个源全接 | (a) | **✅ (a)**；本批次 **§6 零修订**，四维度暂 `unknown`，下一轮再定 feeds／entitlements／迁移号 |

**Q2 偏离的处置**：助手没有替操作者把 §6 边界顺手改掉，而是把「新增行情源／用 IBKR 建」拆成 Q6 单独再问一次，落为 D79。实测 `quote_fields()` 只建 STK/CASH、`gate.py:31` 硬限 `{STK, OPT}`、`reqMktData` generic tick list 为空 ⇒ IBKR 侧目前无 IND 合约路径、无期权 IV 度量；CBOE 期限结构与 10Y 收益率多半仍需 vendor feed。逐条依据亦见 §5 决策表与 §7 第九轮。

### 16.6 边界同步（第九轮 D74–D79）与依赖

- 本节**已裁定、仍是零代码**：零迁移、零 Settings 字段、零运行时改动、不改 R1–R3 交付状态、不新增数据源、不产生订单／草案／门控、不改 §13 冻结契约任何一条结论。
- **Q1 已裁 (a)**：R7 契约 §13.4 加 pointer、§13.9 一次性补登记四个维度与封闭词表（**D67–D73 七条结论不动**），`00` 册 §5 已裁定表／§6 欠账组／§11 分册索引／§12 号段注记已同步。(b)(c) 已否决，本节不再「移入别的迭代」。
- **§1／§6 两条边界本批次零修订**：D74 只读注解、D76 剥动作、D77 不推送、D78 不作验收、D79 排到独立裁定轮 ⇒ §1 无需改；D75 的「新增行情源」意图被 D79 排到下一轮 ⇒ §6 保持原文。
- **下一轮（尚未开工）**：「R7 数据源」裁定轮，须先**显式修订 §6**，再决定 CBOE VIX 期限结构／10Y 收益率的 vendor feed 与 IBKR entitlement、`gate.py` 的 `{STK, OPT}` 硬限、`reqMktData` generic tick list、真 IV Rank 时序表与迁移号。**`055` 仍为 R7 `regime_labels` 预留号段，不被本企划占用**（§14.2）。
- 依赖不变：R7 实现仍待 **R4**（推送通道）；R3 的 ET 锚定调度与 Run C 派生已就位（§14／§15）。
- 11/13 的 VIX 19.30／17.49→18.70→19.30、VIX1D +26.5%、NVDA IV Rank 93.3、10Y +24bp、VIX/VIX3M 0.936、VIX1Y−VIX9D 5.03 等数字**由操作者提供、未经独立核实**，仅作第 6 个校准工作案例；**不作验收、不作回测基准、不得硬编码进代码或阈值**（D78）。
- 操作者结语可存进风控档案：「預警不是 VIX 破 30，是『指數還在 19、但短端保險和單股 IV 已經先慌』。」—— 但**工具内不声称还原机构意图**（D72）。

---

**主计划**：[00-plan-order.md](00-plan-order.md) ｜ **状态**：**已裁定（2026-11-21，D58–D73 共 16 条全取推荐、零偏离；＋ 2026-11-22 第九轮 D74–D79 六条，五取推荐、一条明确偏离 D75、零跳过 ⇒ 共 22 条 / D1–D79 80 个节点零待决）**；**R1 已交付并经 D66 修订（2026-11-21）**；**R2 已交付并验证（2026-11-21，§11 契约／§12 交付记录）**；**R3 已交付并验证（2026-11-22，§14 契约／§15 交付记录）**；R4–R6 待办；**R7 已裁定、契约冻结（D67–D73，§13），实现待 R4**；**§16 R7 VIX 预警视角补充企划 ✅ 已裁定（D74–D79，§16.5），四个只读维度登记进 §13.9；§1／§6 边界零修订、零代码、零迁移；「R7 数据源」另起裁定轮（D79）**
