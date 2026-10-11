# 步 1 — 价格与 bar 单一口径

**主计划**：[00-plan-order.md](00-plan-order.md) ｜ **状态**：**已落地**（2026-10-11）。D2 (a)、D3 按推荐落地。D1 落地为一日一根：`ib-daily` 与同日 `ib-delayed` 合并，delayed 只更新当日 close，不替换 daily 的 high/low；`seed` 不进窗口。
**依赖**：无 ｜ **阻塞**：2、3、4、5、6、7

---

## 1. 目标

让全库对"某标的当前的价格 / 最近的日线序列"只有**一套**定义，并让这套定义物理上无法被绕过。

一句话判据：**任何计算"通道高低轨、RSI、IV、回撤、落点"的代码，都只能从同一个模块拿到同一份日线序列；`grep` 全库，`market_bars` 的直接查询只允许出现在那一个模块里。**

---

## 2. 现状取证

### 2.1 全库存在 6 套互不相同的"当前价格"规则

| # | 位置 | 规则 | 后果 |
| --- | --- | --- | --- |
| 1 | `agents._bars` | `ORDER BY ts DESC LIMIT 273`，**无 source 过滤** | 合成 `seed` bar 与小时级 `ib-delayed` 混入"63 个交易日"窗口 |
| 2 | `agents._last_close` | `ORDER BY ts DESC LIMIT 1`，**无 source 过滤** | "最新价"可能是一条小时级盘中重复值，甚至是一条假 bar |
| 3 | `iv.record_iv_marks`（活路径） | `ORDER BY ts DESC LIMIT 1`，**无 source 过滤** | IV 代理的分母是盘中重复价，不是收盘价 |
| 4 | `simbars.load_daily` | `source = 'ib-daily'` | 唯一正确的一处（价格 tag 与 IV 走这条） |
| 5 | `drafts._latest_price` | `source IN ('ib-delayed','ib-daily')` | 又一种口径；用于仓位规模计算 |
| 6 | `marketdata.write_watchlist_bars` | 去重时用 `source <> 'seed'` | 写入端第 3 种口径 |

⇒ 同一个标的，**通道轨道、价格 tag、IV、下单规模**可能分别建立在四条不同的序列上。这是评审 §3.11 的第一条，且比原文描述更严重。

### 2.2 合成 `seed` bar 正在污染真实窗口（实测）

`write_watchlist_bars` 在 iTick 无 key / 拉取失败时，会写入 `source='seed'` 的**常数值假 bar**（`SEED_CLOSE` 或兜底 `Decimal("100")`）。

```
symbol | source | n  | f                   | l                   | distinct_closes | close
-------+--------+----+---------------------+---------------------+-----------------+-------
NVDA   | seed   | 15 | 2026-10-08 16:00:00 | 2026-10-09 17:00:00 |               1 | 218.42
QCOM   | seed   | 15 | 2026-10-08 16:00:00 | 2026-10-09 17:00:00 |               1 | 169.05
T      | seed   | 15 | 2026-10-08 16:00:00 | 2026-10-09 17:00:00 |               1 |  27.18
```

15 条 / 标的，跨 2 个自然日，**小时级时间戳**，取值恒为常数。受害标的恰好是 NVDA、QCOM、T。

### 2.3 量化影响：NVDA 的"63 交易日低点"错了约 10%

`agents._bars` 实际取回的最后 63 行构成：

| 口径 | 行数 | ib-daily | ib-delayed | seed | 窗口高 | 窗口低 |
| --- | --- | --- | --- | --- | --- | --- |
| `_bars` 现口径（63 个最新行） | 63 | 29 | 19 | 15 | 243.37 | **208.93** |
| 仅 `ib-daily`（61 根，全量） | 61 | 61 | 0 | 0 | 243.37 | **190.01** |

⇒ 34 行非日线把 34 根真实日线挤出窗口，**低点从 190.01 变成 208.93**。`LOWER_RAIL` 的判据是"最新价距轨道 1% 以内"，这 18.92 的差距足以在"不触发"与"触发"之间翻转。NVDA 是 `_pullback` 的合格标的（`MAG7,MEGA_CAP`），因此这条错误直接落在目标用例的路径上。

### 2.4 历史长度不足

| 事实 | 数值 |
| --- | --- |
| 每标的 `ib-daily` 根数 | 60–61（NVDA/QCOM/T 因另有非日线行，总行数到 95） |
| 折算交易日 | 约 3.3 个月 |
| `_rail(bars, 0, 252)` 实际含义 | 全部 bar 的最大值，即约 3.3 个月高点 |
| 审计实证 | `Location NOK` 同时报出 `63-session high 21 sessions ago: 17.45` 与 `252-session high: 17.45` —— 两者相等 |

⇒ 现在 prompt 里写的 "The 52-week range is the 252-session highest high and lowest low" 是一句**假话**，而 `HIGH_52W / LOW_52W` 这个 tag 名会直接进入模型判断。

---

## 3. 变更逻辑 spec

### 3.1 引入唯一价格契约模块

新增 `packages/marketdata/src/athena_marketdata/pricing.py`，成为**全库唯一**允许直接查询 `market_bars` 的模块。三个函数，语义互不重叠、命名即契约：

| 函数 | 语义 | 实现约束 |
| --- | --- | --- |
| `daily_bars(conn, symbol, n) -> list[Bar]` | 最近 `n` 个**交易日**，按时间升序 | 固定 `source='ib-daily'`；固定 `WHERE ts::date` 去重（同一天多行只留最新一行）；返回带 `ts` 的对象，而非裸三元组 |
| `last_daily_close(conn, symbol) -> Decimal \| None` | 最近一个交易日的收盘 | 等价于 `daily_bars(conn, symbol, 1)[-1].close`；**禁止**在别处用 `LIMIT 1` 自己取 |
| `last_intraday_price(conn, symbol) -> Decimal \| None` | 最近一条盘中报价（`ib-delayed`），**仅供下单参考价** | 命名空间独立，**任何 rail / RSI / IV / 回撤计算都不许调用它** |

同时**删除** `simbars.load_daily` 的重叠实现，改为委托 `pricing.daily_bars`（回放分支保留在 `simbars`，但对上暴露同一签名）。

### 3.2 六处调用点逐一归位

| 现位置 | 改为 | 说明 |
| --- | --- | --- |
| `agents._bars` | `pricing.daily_bars(conn, symbol, 273)` | 回放分支仍走 `simbars` |
| `agents._last_close` | `pricing.last_daily_close` | **不再回落到盘中价** |
| `iv.record_iv_marks` 活路径 | `pricing.last_daily_close` | 分母必须是收盘价 |
| `simbars.load_daily` | 委托 `pricing.daily_bars` | 消除重复实现 |
| `drafts._latest_price` | 重命名为 `last_tradeable_price`，内部 `last_intraday_price` 优先、否则 `last_daily_close` | **显式命名**它为"下单参考价"，并加注释禁止参与轨道/IV |
| `marketdata.write_watchlist_bars` | 删除 seed 写入分支 | 见 3.3 |

### 3.3 清除合成 `seed` bar

- **迁移 `027_drop_seed_bars.sql`**：`DELETE FROM market_bars WHERE source = 'seed';`（向后兼容，只删数据不改表结构）。
- `write_watchlist_bars` 删除 `SEED_CLOSE` 兜底分支；无 key / 拉取失败时**什么都不写**，只记一条 `log.info`（"宁可没有数据，也不要假数据"）。
- `SEED_CLOSE` 常量降级为测试夹具，移出生产路径。
- **不变量**：写入端只允许写 `ib-daily`（日线）与 `ib-delayed`（盘中）两个 source。

### 3.4 补足历史长度

- 在既有 IB 拉取路径上把 duration 从当前窗口扩展为 **≥252 个交易日**（IB `DurationStr` 用 `2 Y` 起，取回后按交易日截断到 252）。
- 拉取结果**只入 `ib-daily`**，并且：
  - 同一天已有行 → 以新拉取覆盖（`ON CONFLICT` 更新 close/high/low/volume）；
  - 拉取条数不足 252 → 记 `log.warning`，并把该标的标记为 `history_short`。
- **tag 名必须与真实窗口一致**：若达不到 252，则 `HIGH_52W / LOW_52W` 在 prompt 与代码中统一改名（建议 `HIGH_RANGE / LOW_RANGE`，并把窗口长度作为数值一并给模型）。**不允许**在窗口不足时继续使用"52 周"这一措辞。

### 3.5 收敛后的唯一读法（伪代码契约）

```
日线序列        := pricing.daily_bars(conn, symbol, n)          # 仅 ib-daily，按交易日去重
通道高/低轨     := max/min(daily_bars(...63).high/low)
区间高/低轨     := max/min(daily_bars(...window).high/low)      # window = 实际可用交易日数
盘中价          := pricing.last_intraday_price(conn, symbol)    # 仅用于下单参考
```

任何"用一天的 bar 去参与 63 日窗口"的做法，在契约层面即为违约。

---

## 4. 决策点

| # | 待决 | 选项 | 推荐 | 落地 |
| --- | --- | --- | --- | --- |
| **D1** | 日线唯一真源 | (a) 仅 `ib-daily` (b) `ib-daily + ib-delayed` 取最新 | **(a)** | **已落地，介于 (a)(b) 之间。** 一日一行。同日 `ib-delayed` 可更新 close；high/low 留 `ib-daily`。不是 (b) 的整行取最新。`pricing.daily_bars` |
| **D2** | `seed` bar 处置 | (a) 物理删除并禁止再写 (b) 保留但读路径排除 | **(a)** | **已落地 (a)。** 迁移 `027` 删除 `seed` 与 `pool-sync`。活库这两类 source 为 0 |
| **D3** | 历史长度目标 | ≥252 交易日 / ≥300 / vendor 上限 | **≥252**，不足则改名 tag | **已落地。** 不足 252 个 `ib-daily` 日记 `history_short`，tag 改为 `HIGH_RANGE` / `LOW_RANGE`。活库 31 个标的里 4 个不足并已标记 |

---

## 5. 验收标准

以下每条都要能作为可执行命令跑出来，不接受"看起来对"。

| # | 验收 | 判据 | 落地 |
| --- | --- | --- | --- |
| A1 | 无合成 bar | `SELECT count(*) FROM market_bars WHERE source='seed';` → `0` | **已落地。** 2026-10-11 活库该计数为 0 |
| A2 | source 白名单 | `SELECT DISTINCT source FROM market_bars;` → 仅 `ib-daily` / `ib-delayed`（`pool-sync` 若仍存在须单独说明并纳入契约） | **已落地。** 活库只有 `ib-daily`（13789）与 `ib-delayed`（428） |
| A3 | 窗口修正 | 对 NVDA 调用 `daily_bars(..., 63)`，其 `min(low)` 应为 **190.01**（不再是 208.93）；且返回行数 = 63 或实际可用交易日数，其中 **100% 为 `ib-daily`** | **已落地（不变量）。** `test_merged_daily_bars_keep_the_session_low`：同日 low 保持 190.01，seed 不进窗口，一日一行。190.01 是当时 NVDA 的快照，不是永久行情 |
| A4 | 读路径收敛 | 全库 grep `FROM market_bars`，命中只允许出现在 `pricing.py`（`simbars` 回放分支除外，需显式豁免注释） | **已落地。** 生产读路径只在 `pricing.py`。`simbars` 有豁免注释。写入仍在 `bars.py`、`session.py`、`pulse.py`、`tasks.py` |
| A5 | 历史长度 | `SELECT count(*) FROM market_bars WHERE symbol=%s AND source='ib-daily'` 对每个 watchlist 标的 **≥252**，或存在明确的 `history_short` 标记且 tag 已改名 | **已落地。** `0992` 不是 SEHK 代码，已改为 `992`（492 根日线）。仍短的是 `SKHY` / `SPCX` / `DRAM`，三个 `history_short=true`。`SPY` 日线在 watchlist 之外，供 regime 使用 |
| A6 | 命名一致性 | prompt 文本中不再出现 "52-week"/"252-session" 措辞，除非 A5 通过 | **已落地。** 历史不足时用 `HIGH_RANGE`。`test_short_history_renames_the_range` |

---

## 6. 明确不做

- 不引入新的行情供应商（仅在既有 IB 拉取上扩展窗口）。
- 不改 `market_bars` 表结构（只删行、不改列）；如需加 `history_short` 标记，倾向用现有 `symbol_facts` 承载，避免新表。
- 不动回放（`sim_bars`）的语义 —— 回放是另一条时间线，本步只要求它走同一函数签名。
- 不调整任何阈值参数（`touch_pct`、`rsi_*` 等），阈值调整属步 4。

---

## 7. 风险与回滚

| 风险 | 说明 | 对策 |
| --- | --- | --- |
| 删 seed 后某些标的"没数据" | 本来就没有真日线，删假 bar 后窗口为空 | 空窗口应表现为"无 tag"而非报错；在 A5 里同时验证"空窗口不抛异常" |
| 历史拉取受 vendor 上限限制 | IB 对单次 duration 有限制 | 接受分次拉取；若仍不足 252，走 D3 的改名分支，不硬凑 |
| 改口径导致 tag 分布突变 | 修正后 `LOWER_RAIL` 等 tag 的触发频率会变 | 这**是预期结果**；在验收时记录修正前后的 tag 计数对照，作为后续步骤的基线 |
| 回滚 | 若口径切换引发不可接受的行为变化 | 逻辑回滚：恢复旧查询（保留旧函数为 `_legacy`）；数据回滚：seed 行已删，**不可逆** —— 但 seed 是假数据，删除本身即修复 |

> **注**：数据删除不可逆，故 A1 的删除动作应在 D2 拍板后、且已确认 seed 行仅为假数据（取值常数、时间戳为小时级）的前提下执行。
