# 14 — Guardian & Escalation Layer（风险监测层与任务告知）

**状态**：**Ratified 2026-11-21**（D58–D65，八条全取推荐，无偏离）。Side track. Not on the signal critical path (steps 1–9).
No code in this booklet is built yet. Every rule below is a **ratified** decision node; **R1 is unblocked**. Rulings recorded in §7.

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

**Sequencing logic**: R1→R2→R3→R4 is the spine (each needs the previous). R5 needs R4's escalation path. R6 is infrastructure and can run in parallel with R3–R5 — but note that **R4 on an unreliable machine is a false sense of safety**, so R6 should not trail far behind.

---

## 5. Decisions — 全部已裁定（2026-11-21）

**九条全取推荐，零偏离。** 前八条分两轮、每条带具体案例逐条问过（「EL/NL 8.2% 怎么显示」「考试中被 uncovered call 连发几条」「22:40 进程死了多久收到消息」），裁定记录见 §7；**D66** 是 R1 交付后的单条补裁定，见 §7 第七轮。

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

---

## 6. Explicitly out of scope

- No drafting, ordering, or tactic changes. The guardian **tells**; the wheel **does**.
- No new market data sources. Everything grades from IB account summary + existing store.
- The 5-min nag is the maximum intrusion. No email, no second channel, no phone call.
- R6 covers the *machine*, not a networking overhaul. Tunnel behaviour is unchanged.
- **R6 主机裁定为家用专用 Mac mini（D64）**，不是 Windows 共用机、不是云 VPS。IB Gateway 同机，`launchd` 自恢复，需在 `C-20261013-34` 的「每 Gateway 登录独立 client id」模式下给它单独的 client id。
- **D66**：不设持仓级 `stop_line`；不新增 Settings 字段、不加迁移、不改 `advisor_signals` / `manage_book` / `drafts`。价格触发线只归 R5。

---

## 7. 裁定记录（2026-11-21，第六轮 ＋ 第七轮补裁定）

操作者指令：「每個問題問我一遍，我來回答」。分两轮、每条带具体案例逐条问，**8 条全取推荐、零偏离、无跳过**。

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

**主计划**：[00-plan-order.md](00-plan-order.md) ｜ **状态**：**已裁定（2026-11-21，D58–D66 共 9 条全取推荐）**；**R1 已交付并经 D66 修订（2026-11-21）**；R2 起待办
