# 步 4 — 指标与 tags 层

**主计划**：[00-plan-order.md](00-plan-order.md) ｜ **状态**：**D4a 已落地**（2026-10-11）。D16 (a)、D17 (a)、D18 已落地；`SPY` 日线已入库。**4b① 采集已落地**（迁移 `035`，实盘 slot 只追加 `chain_marks`）。**4b② 未做**。A10 仍属步 9。
**依赖**：步 1（数据口径）、步 3（审计可辨） ｜ **阻塞**：步 5、步 6 ｜ **外溢**：D4b 已拆为 **4b① 采集（挂步 1 之后）** 与 **4b② 消费（留步 5 之后）**，均不在关键路径

> **本册已于 2026-10-10 更正。** 早前版本推荐"用 ATM 跨式近似反推 IV 以消除反相"（旧 D4 选项 c）。**实测证伪**：`iv_proxy ≡ credit/underlying`（逐行恒等，误差 1e−21），`credit` 与 `expiry` 每个 symbol 各只有一个取值，故跨式近似退化为**正线性缩放**，`iv_rank` 逐位不变 —— 它修不好反相。更正后的结论是 **D4a：本步不引入 IV 口径**。详见 §3.1。
>
> **二轮裁定（2026-10-10）**：**D4a (d)**、**拆分确认**、**D4b (b) —— 只提前采集**。因此 §3.1.4 的 D4b 设计需按两段阅读：**采集段（迁移 `035` + 快照 job）挂到步 1 之后立即启动**（20 日静默期即刻计时，且只写自己的表、无下游读取方）；**消费段（解 BS → `IV_RICH`）留步 5 之后**。**D4a 与 4b① 已于 2026-10-11 落地。4b② 仍只是设计。**

---

## 1. 目标

三件事，缺一不可：

| # | 目标 | 一句话判据 |
| --- | --- | --- |
| **T1′** | **反相不再被使用** | `_panic()` 不再消费 `HIGH_IV`；全库不存在把 `1/price` 当波动率判据的代码路径；本步的波动率门由 `RV_HIGH/RV_LOW` 承担（对价格缩放不变） |
| **T1（移交 D4b）** | *IV 不再反相* | *原表述：固定 credit、只让价格变动时 vol tag 不翻转 —— **本步不可能达成**，因为本步没有 IV。仅当 `chain_marks` 累积出实测样本、`estimated_iv()` 有真实实现后才可验收* |
| **T2** | **六月式 dip 可表达** | 存在一个 tag 组合，只在"低位区已出现过、现已收复、趋势仍向上"时触发，且不依赖 `LOWER_RAIL` 或 `PULLBACK` |
| **T3** | **两套引擎说同一门语言** | Engine A（`symbol_phases.tags`）与 Engine B（`_context.base_tags` + Location）的 tag 词汇表来自**同一份注册表**；任何 tag 的产生条件、所属 owner、方向（看多/看空/中性）都在该表里声明 |

**T3 是前置**：现在 Engine A 用 `PRICE_OWNED | NEWS_OWNED | IV_OWNED`，Engine B 用 `stored ∪ iv_tags ∪ Location 输出`，两边各写各的字符串字面量。任何一次新增指标都会分叉。

---

## 2. 现状取证

### 2.1 IV 代理的分子是常数，所以代理 ∝ 1/price

`record_iv_marks`：

```python
plan = SELECT put_limit, call_limit FROM option_plans WHERE symbol = %s AND put_limit > 0 AND call_limit > 0
credit = plan[0] + plan[1]          # 静态行，永不变化
proxy  = credit / price[0]
```

`option_plans` 只有 7 个标的、每个一行、没有时间维度。因此**同一标的的所有历史 `iv_proxy` 共用同一个分子**。

**实测（一）：恒等式，而非近似。** `iv_marks` 全表逐行做反算 `iv_proxy × underlying`：

```sql
SELECT symbol, count(*), max(abs(iv_proxy - credit/underlying)) AS max_abs_dev,
       min(iv_proxy*underlying), max(iv_proxy*underlying)
FROM iv_marks GROUP BY symbol;
```

| symbol | 行数 | `max_abs_dev` | `iv_proxy × underlying` |
| --- | --- | --- | --- |
| NOK | 23 | 4.9e−21 | 0.73000000000000000000 … 0494 |
| NVDA | 23 | 4.7e−21 | 3.22999999999999999999 … 1039 |
| SKHY | 23 | 5.0e−21 | 8.95999999999999999999 … 0902 |
| QCOM | 2 | 3.2e−21 | 8.35000000000000000000 … 0429 |
| T | 2 | 4.8e−21 | 2.46000000000000000000 … 0336 |
| 1810 | 2 | 2.9e−21 | 1.79000000000000000000 … 0083 |
| 700 | 2 | 4.9e−21 | 10.59999999999999999999 … 1640 |

偏差量级 **1e−21**（numpy/numeric 的浮点噪声），即

```
iv_proxy  ≡  credit / underlying          （逐行恒等，不是"接近"）
```

**实测（二）：分子确实冻结。**

```sql
SELECT symbol, count(*), count(DISTINCT credit), count(DISTINCT expiry)
FROM option_plans GROUP BY symbol;      -- 每个 symbol：rows=1, credit=1, expiry=1
```

`count(DISTINCT credit) = 1`、`count(DISTINCT expiry) = 1`（7 个标的全表 7 行）。因 `expiry` 唯一 ⇒ **`√(2π/T)` 对每个 symbol 也是常数**。

**实测（三）：相关系数封顶。**

```sql
SELECT symbol, corr(iv_proxy, 1.0/underlying) FROM iv_marks GROUP BY symbol;
```

有 ≥2 行的 symbol 全部给出 **`corr = 1.000000`**（精确 1，非 0.99）。

⇒ 结论链条（**每一环都是实测，不是推断**）：

1. `iv_proxy = credit / underlying`，逐行恒等（误差 1e−21）；
2. `credit` 对每个 symbol 是**单一常数**，`T` 也是**单一常数**；
3. 因此 `iv_proxy` **纯粹是 `1/underlying`**，且 `corr = 1.000000`；
4. 于是 T 的 proxy 上行 22% 而**没有任何一条信息说明波动率变了** —— 唯一变的是价格跌了 18%。

⇒ `iv_rank` 实际是"价格的反向百分位"。`HIGH_IV` 的真实语义是"**价格低**"，与 `LOWER_RAIL` 高度重合。而 `phases._panic()` 要求 `NEWS_WASH ∧ HIGH_IV` —— 在恐慌日这两个 tag 常同时出现，等于**同一个信号被数了两次**。

### 2.2 粒度退化使 rank 只能取 0 或 100

`iv_rank` 在 `len(samples) < minimum` 或 `high == low` 时返回 `None`。活配置 `ivr_min_samples = 1`：

| 样本数 | `high == low`? | 结果 |
| --- | --- | --- |
| 1 | 是 | `None` → 无 tag |
| 2 | 否 | rank 只能是 **0** 或 **100** |
| ≥3 | 否 | 仍然只有"最新样本是不是极值"这一位信息 |

实测与此吻合：`QCOM → LOW_IV`、`T → HIGH_IV`，中间态从未出现。

### 2.3 六月式 dip 的两个判据在错误时刻互斥

| 判据 | 定义 | 恐慌后立刻取证的结果 |
| --- | --- | --- |
| `LOWER_RAIL` | 最新价距 63 日低点 ≤1% | 放量下砸那天**会**触发 —— 但触发的是"正在跌"，不是"跌完了" |
| `PULLBACK` | `CHANNEL_RISING` ∧ 分类含 `MAG7`/`MEGA_CAP` ∧ `rail_pos ≤ 0.35` | 砸完以后，**前 63 日的高轨更高了**，`CHANNEL_RISING`（现轨 > 21 日前轨）往往**不成立** |

⇒ 想要的信号（"低区出现过、现已收复、趋势未坏"）**不存在任何 tag**。策略恰好在它被设计出来的时刻沉默。

### 2.4 已有但没被使用的 tag

`price.py` 产出 `OVERBOUGHT / OVERSOLD / SUPPORT_TOUCH / RESISTANCE_TOUCH / PRICE_WEAK / VOLUME_SPIKE`，经 `base_tags` **确实**到达模型，但：

| prompt | 认得的 tag | 丢弃的 tag |
| --- | --- | --- |
| `CSP` | `LOWER_RAIL`, `LOW_52W`, `PULLBACK` | `OVERSOLD`, `SUPPORT_TOUCH`, `PRICE_WEAK`, `VOLUME_SPIKE` |
| `Purchase` | `LOWER_RAIL`, `LOW_52W` | 同上 |
| `Call` | `UPPER_RAIL`, `HIGH_52W` | `OVERBOUGHT`, `RESISTANCE_TOUCH` |
| `Strangle` | `NEWS_WASH`, `HIGH_IV` | 其余全部 |

评审 §7 的第 1 条更正即指此处：**这是 prompt 词汇表问题，不是数据接线问题。**

### 2.5 轨道窗口与量能数据的实测边界

| 事实 | 数值 |
| --- | --- |
| 每标的 `ib-daily` 根数 | 60–61（≈3.3 个月） |
| `_rail(bars, 0, 252)` 实际含义 | 全部可用 bar 的最大值 |
| 审计实证 | `Location NOK` 同时报 `63-session high` 与 `252-session high` 均为 **17.45** |
| `market_bars` 有量的行 | `ib-daily` **1743/1743** 行 volume > 0（max 474,998,400） |
| 无量行 | `ib-delayed` 308/308、`pool-sync` 2/2、`seed` 46/46 全部 volume = 0 |

⇒ ① `HIGH_52W/LOW_52W` 目前是"约 3 个月高低点"的别名，与 `UPPER_RAIL/LOWER_RAIL` 近乎重复；
⇒ ② **量能类与 ATR、已实现波动率全部可算**，但**必须先完成步 1 的 source 收敛** —— 否则 `_bars` 混入的 34 行零成交量 bar 会把任何量能均值稀释。

---

## 3. 变更逻辑 spec

### 3.1 IV 口径：**三个选项在本步全部不可达** —— D4 拆成 D4a / D4b

> **本节更正本册早前版本。** 早前版本推荐选项 (c)"用 Brenner–Subrahmanyam 跨式近似反推 ATM IV，分子与分母同除 S，消除 1/price 反相"。**该推荐经实测证伪**。下面的 (a)(b)(c) 表保留作对照，但结论改为：本步**不引入任何 IV 口径**，只做"去伪 + 留接口"。

#### 3.1.1 (c) 不成立 —— 它是恒等变换，不是修复

`iv_rank = (cur − min) / (max − min)`。对窗口内样本做**正线性缩放** `x ↦ a·x (a>0)`：

```
(a·cur − a·min) / (a·max − a·min) = (cur − min) / (max − min)     ← 精确恒等
```

而跨式近似与旧式的关系，由 §2.1 实测（二）`credit` 每 symbol 唯一、`expiry` 每 symbol 唯一，得 `√(2π/T)×100` 是**每 symbol 的常数** `a`：

```
iv_proxy_new ≡ a · iv_proxy_old        ⇒   iv_rank_new ≡ iv_rank_old   （逐行 bit 相同）
```

**"修好了反相"是假的**：新式只把 0.0135 换成 11.1%，`iv_rank` 一位都不动。

#### 3.1.2 (c) 的公式前提也不成立

Brenner–Subrahmanyam 是 **ATM 跨式**恒等式 `C + P ≈ S·σ·√(T/2π)`。实测 `option_plans` 七条腿（见下表）**全部是 ~11% OTM 宽跨式，无一条 ATM**。套用会系统性**低估** IV，且低估幅度随 moneyness 变化 —— 而 moneyness 本身随价格移动（价格跌 ⇒ OTM 更远 ⇒ 低估更深），构成**二阶的再反相**。

| symbol | put 距平值 | call 距平值 | dte |
| --- | --- | --- | --- |
| NVDA | −10.59% | +11.22% | 34 |
| QCOM | −11.68% | +11.11% | 34 |
| T | −12.08% | +10.46% | 34 |
| NOK | −13.13% | +11.00% | 34 |
| SKHY | −11.26% | +10.93% | 34 |
| 700 | −10.55% | +10.64% | 48 |
| 1810 | −11.40% | +11.71% | 48 |

#### 3.1.3 三个选项的真实状态

| 选项 | 状态 | 硬理由（实测） |
| --- | --- | --- |
| (a) `RV × 倍数` | **不可用，且反相被搬家** | RV 是**已实现**波动。更关键：任何形如 `credit / (S·σ·const)` 的"溢价相对期望波动"表达式，因分子 `credit` 冻结，仍 ∝ 1/S —— 反相只是从分子搬到分母 |
| (b) 接 IB 链真 IV | **当前物理上取不到** | `option_choices` 只有 `limit_price`（**我们自己的挂单价，不是市场 mark**），**无 bid/ask/mid、无 greeks、无 IV 列**；24 行中仅 NOK 有 6 行、SKHY 有 8 行有价，`1810/700/NVDA/QCOM/T` 为 **0/24** |
| (c) 跨式近似 | **恒等变换，非修复** | 见 3.1.1（rank bit 相同）与 3.1.2（ATM 前提不成立） |

#### 3.1.4 根因是**分子冻结**；修复必须以"刷新分子"为前提

$$iv\_proxy = \frac{\text{冻结的 } credit}{S}$$

只要分子是 `option_plans` 那一行静态 `put_limit + call_limit`，`iv_proxy` **只能是 `1/S` 的单调函数**；任何单调重标定（乘 `√(2π/T)`、取对数、开方）都**不改变序关系**。要得到与价格无关的波动率量，**分子必须随市场刷新**。

于是 D4 拆成两个决策：

| # | 决策 | 内容 | 本步范围 |
| --- | --- | --- | --- |
| **D4a**（本步落地） | **去伪 + 留接口**，不引入 IV 口径 | ① `HIGH_IV/LOW_IV` **从 `_panic()` 移除**，止住"同一信号数两次"（§2.1 末段）；② 两 tag 在注册表标 `legacy_price_proxy`、**不再产生**，读层保留别名；③ 新增 `RV_HIGH/RV_LOW`（由 `RV20` 自身百分位定，**无分子、与价格无关**）作为本步唯一波动率门；④ `estimated_iv()` 恒返回 `None` | ✅ |
| **D4b**（新增独立一步，**已裁定拆两段**） | **实测 ATM 溢价序列 → 真 IV** | 见下 | ❌ 本步只留接口 |

**D4b 最小设计（供后续排期，本册不实施）**：

```
每个 cycle，对每个有链的 symbol：
  1. 取链上最接近 ATM 的 call 与 put 的 mid (bid+ask)/2
  2. 记 (symbol, ts, expiry, dte, k_call, k_put, moneyness, mid_call, mid_put)
     → 新表 chain_marks（只追加）
  3. iv_from_chain := 解 BS：给定 S, K, T, mid → σ（牛顿法 3 次迭代足够）
  4. 仅当 |moneyness| ≤ iv_atm_max_moneyness_pct（默认 2%）才写 iv_marks，
     并把当时的 moneyness 一并落库（供审计判定"当时到底是不是 ATM"）
```

**D4b 排期已裁定为两段（2026-10-10 二轮）**：

| 段 | 做什么 | 依赖 | 状态 |
| --- | --- | --- | --- |
| **4b① 采集** | 上面第 1–2 步：迁移 `035` + 快照 job。**只写 `chain_marks`，不碰 `iv_marks`，无下游读取方** | 步 1 | **已落地（2026-10-11）。** 实盘 slot 追加；回放不写 |
| **4b② 消费** | 上面第 3–4 步：解 BS → `estimated_iv()` 返真值 → 定义 `IV_RICH`/`IV_CHEAP` | 4b①（样本 ≥ 20）、步 5 | 留步 5 之后 |

> **为什么采集段可以提前而无风险**：它**只追加自己的表**，既不读也不写 `iv_marks`、`symbol_phases.indicators` 或任何 prompt 输入。提前的唯一后果是 `chain_marks` 多几行 —— 而这几行正是 20 日静默期所等的。反之若不提前，静默期会推迟到关键路径末端才开始计时。
>
> **不可回填**：`chain_marks` 的历史无法补（券商不提供过去的盘口），因此**晚一天启动就永久少一天样本**。这是本套计划里唯一一处"日历比工时更贵"的地方。

#### 3.1.5 本步（D4a）仍必须做的三件事

1. **加 `method` 列，防窗口混算**：迁移 `028_iv_method.sql`
   ```sql
   ALTER TABLE iv_marks ADD COLUMN method text NOT NULL DEFAULT 'premium_ratio';
   ALTER TABLE iv_marks ADD COLUMN moneyness_pct numeric;   -- D4b 前恒为 NULL
   ```
   本步**不写新 method**（因为不引入新口径）；该列是给 D4b 预留的地基。`tags_from_iv` 与 `drafts._stored_iv_rank` 的窗口查询**必须**加 `AND method = %s`。
   **理由**：D4b 上线后会把 0.09（旧比例）与 42.0（新年化 %）放进同一个 min/max，rank 会永久失真。**现在加列成本 1 行 SQL，D4b 之后再加要改所有读路径。**
2. **`ivr_min_samples` 提到 ≥ 20**（活配置当前为 1）。rank 需要 20 个同 method 样本才有意义 ⇒ `RV_HIGH/RV_LOW` 与将来的 `IV_RICH/IV_CHEAP` 都会先**静默 20 个交易日**；这是预期行为，必须在审计里表现为"样本不足"而非"无信号"（与步 3 的 `no_data` 契约对齐）。
3. **保留真 IV 接口点**：`iv.py` 暴露 `estimated_iv(conn, symbol, today) -> Decimal | None`，**D4a 的实现恒返回 `None`**（诚实：本步确实没有 IV）。所有下游只调用该函数、不直读 `iv_marks`；D4b 上线时整体替换该实现，**调用方零改动**。

> **本步验收口径的相应调整**：§1 的 T1"IV 不再反相"在本步**不可能达成**（本步根本没有 IV）。T1 更改为 **T1′ —— "反相不再被使用"**：`_panic()` 不再消费 `HIGH_IV`，且全库不存在把 `1/price` 当波动率判据的代码路径。T1 的原始表述移交给 D4b。

#### 3.1.6 **缺少 IV 到底伤到什么、没伤到什么**（2026-10-10 增补，更正早前的过度悲观）

早前版本暗示"σ 拿不到 ⇒ 步 5 的 delta 会静默失效"。**这个说法过重**。按用途拆开：

| 用途 | 是否需要真 IV | 依据 |
| --- | --- | --- |
| **挑哪一档**（行权价选择 / 排序） | **不需要** | delta 对 σ 的误差是**一阶小量**：`strike 偏移 ≈ z·σ√T`，而偏移本身只有 5–10%，故 σ 错 20% ⇒ 档位只错 **1–2 个百分点**（步 5 §3.8(e)）。用 `RV20 × 显式声明的倍数` 足够 |
| **这一档的价格贵不贵**（溢价充足性） | **需要** | 这是**唯一**真正依赖 IV 的地方。缺了它，`IV_RICH/IV_CHEAP` 无从定义 —— 而这恰好就是"卖保险收的租是否偏贵"这个判据，也是 D4b 存在的理由 |

⇒ **本步的缺口是"不能判断溢价贵不贵"，不是"不能选行权价"。** 两者不要混为一谈，否则会误以为整个选股层不可用。

**与步 5 的接口约定**：步 5 用 `σ_ref := iv_rv_multiple(rv_class) × RV20` 算 delta，其中 `iv_rv_multiple` 是**步 5 的 Settings 字段**（不是本步的），默认值由操作者的实务口径标定（30–45 DTE strangle：`MRVL` 上下各 7–10%、`AAPL`/`NVDA` 约 5%；反推 `MRVL` 1.07 / `NVDA` 1.38 / `AAPL` 2.13，按 `RV20` 三档取 1.05 / 1.35 / 1.80）。

**三条硬约束**：
1. `σ_ref` **不得**在任何界面、prompt、日志里被称作 IV；
2. `σ_ref` **不得**被用来构造 `IV_RICH/IV_CHEAP`；
3. `iv_rv_multiple` 必须在 Settings 里被标注为**假设**（assumption），并被 D4b 的实测值替换 —— 这是它与"测量"的唯一区别，也是它可接受的理由。

> **为什么 `σ_ref` 不属于本步**：本步（步 4）只产出**可测量**的量（`RV20`、`ATR`、轨道、回撤…）。`iv_rv_multiple` 是**操作者知识**，把它放进步 4 会让"指标层"看起来在做假设。放在步 5（消费侧）职责更清。

### 3.2 新 tag 与数值字段（全部确定性，无 LLM）

**计算位置**：新增 `packages/strategy-nfr/src/athena_strategy/indicators.py`，输入 `daily_bars`（步 1 唯一口径）＋ `settings.strategy`，输出 `Indicators`（一个 dataclass）。

| tag | 定义 | 参数（进 Settings） | 用途 |
| --- | --- | --- | --- |
| `TREND_STRONG` / `TREND_WEAK` | 两条 63 日轨道相对 21 日前的斜率：`slope = (rail_now − rail_21ago) / rail_21ago`；**两条轨道同号**才给 tag | `trend_slope_min_pct`（默认 2） | 把"健康趋势里的回调"与"滚下来的"分开 |
| `trend_slope_pct`（数值） | 上式的百分数，**随 tag 一起进 prompt** | — | 布尔值不够用；CSP agent 需要这个数 |
| `rail_pos`（数值 0–1） | `(last − low_63) / (high_63 − low_63)`；已存在于 `_pullback` 内部，**提升到 `_context`** | — | "接近下轨但没触及 → 放宽行权价"而非二元沉默 |
| `DIP_BOUGHT` | 三个条件同时成立：① 最近 N 日内**曾**出现 `rail_pos ≤ 0.35`；② 最新 `rail_pos ≥ 0.45`（已收复）；③ `TREND_STRONG` | `dip_window_sessions`（默认 5）、`dip_deep_pos`（0.35）、`dip_reclaim_pos`（0.45） | **T2 的落点**。这是"跌过了、买盘回来了"，即 Long + CSP 的时刻 |
| `DRAWDOWN_OK` ＋ `drawdown_pct`（数值） | `drawdown = (last − high_63) / high_63`；落在 `[−high, −low]` 带内给 tag | `drawdown_band_high_pct`（15）、`drawdown_band_low_pct`（5） | 区分"有序回调"与"−30% 破位" |
| `VOLUME_DRY` | 最新一根量 < `volume_dry_multiple ×` 前 20 根均量 | `volume_dry_multiple`（0.7） | "卖压耗尽"，与既有 `VOLUME_SPIKE` 互补 |
| `ATR`（数值）＋ `ATR_WIDE` | ATR(14)：Wilder，`TR = max(H−L, |H−C₋₁|, |L−C₋₁|)`；`ATR_WIDE = ATR/last ≥ atr_wide_multiple` | `atr_window`（14）、`atr_wide_ratio_pct`（4） | "2×ATR 之外的 put 才是有诚意的收租价"；固定 % OTM 不是 |
| `RV_HIGH` / `RV_LOW` | `RV20` 在**自身历史**（同 method 窗口 ≥20）中的百分位：`≥ rv_high_pctl` → `RV_HIGH`，`≤ rv_low_pctl` → `RV_LOW` | `rv_high_pctl`（80）、`rv_low_pctl`（20） | **本步唯一的波动率门**。无分子、与价格无关，因此**天生不反相**（D4a） |
| `RV20`（数值） | 20 日收盘对数收益的样本标准差 × √252 × 100 | `rv_window`（20） | 波动率水平；`RV_HIGH/LOW` 的底量 |
| ~~`IV_RICH` / `IV_CHEAP`~~ | **本步不引入**（D4a）。原设计为 `iv_proxy − RV20`，但 `iv_proxy` 分子冻结 ⇒ 与 `RV20` **量纲不可比**（34 天溢价占股价比 vs 年化 σ），且 ∝ 1/S | — | **移交 D4b**：待 `chain_marks` 累积出 20 个同 method 实测样本后再定义 |
| `RANGE_PCTL`（数值） | 最新价在**自身历史**（≥252 日，依赖步 1 的 D3）中的百分位 | — | 与 1% 触碰解耦的"贵/便宜"标尺 |
| `REGIME_RISK_OFF` | 指数（默认 `SPY`，见 D18）收盘 vs 其自身 63 日中枢，低于中枢 → tag | `regime_index`、`regime_lookback`（63） | 个股 dip 在下跌市里应"少做或不做" |
| `RECLAIM` | 最新收盘 > 前 5 日均值 **或** > 前一根 bar 高点 | `reclaim_window`（5） | T2 的第三个条件，且是"买盘回来了"的直接证据 |
| `earnings_in_days`（数值字段，非 tag） | 已有 `calendar_offset` 的 offset，**暴露给每一个 tactic** | — | 让模型主动选"财报后的到期"，而不是被静默 gate 掉 |
| `news_score`（数值字段） | `enrich_sentiment` 的加权结果，替代布尔 `news_wash` | `wash_*` 已有 | 让 Consolidator 分清"一篇博客"与"一串下调" |

**`HIGH_IV` / `LOW_IV` 的退场策略**（见 D17）：
- 代码层**删除产生**（`iv_tags` 不再被调用），并从 `phases._panic()` 的判据中**移除**；
- 注册表里标注 `owner="iv"`、`status="legacy_price_proxy"`、**`deprecated=True`**，`direction` 记为 `neutral`（不再声称它表达波动率）；
- 读层保留**别名**（映射到自身，仅用于历史统计连续性），使历史 `symbol_phases.tags` 仍可被 `GROUP BY` 解析；
- prompt 层**不再出现**这两个词；本步的波动率词汇只有 `RV_HIGH`/`RV_LOW`。`IV_RICH`/`IV_CHEAP` 属 D4b，**本步不在 prompt 里出现**。

### 3.3 Tag 注册表：单一真源

新增 `packages/strategy-nfr/src/athena_strategy/tags.py`：

```python
@dataclass(frozen=True)
class TagSpec:
    name: str
    owner: str        # "location" | "news" | "iv" | "price" | "indicators" | "regime"
    direction: str    # "bull" | "bear" | "neutral"
    requires: tuple[str, ...]   # 依赖的其他 tag（用于拓扑与文档）
    aliases: tuple[str, ...] = ()

TAG_REGISTRY: dict[str, TagSpec] = {...}
```

**三条硬性使用规则**：
1. `phases.evaluate_symbol` 的 `stored - NEWS_OWNED - IV_OWNED - PRICE_OWNED` 改为按注册表 `owner` 过滤：`{t for t in stored if TAG_REGISTRY.get(t, EMPTY).owner != "location"}`（同理其余 owner），**消除四个手写常量集合**。
2. `agents._tag_set` 处理 `NEWS_WASH` / `PULLBACK` 的两段特判改为注册表驱动的通用逻辑（"凡 owner=news 的 tag 由 News 结果覆盖；凡 owner=indicators 的 tag 每轮重算"）。
3. **未知 tag 必须可见**：`_tag_set` 遇到不在注册表里的 tag 时记 `log.warning` 并在审计 `steps.unknown_tags` 里留痕，不再静默携带。

### 3.4 结构化 tags：给模型数字，不只给词

`_context` 目前把 tags 拼成一个逗号串。本步给 prompt 增加一个**数值块**：

| 现 prompt 片段 | 改后 |
| --- | --- |
| `Tags: {{tags}}` | `Tags: {{tags}}`（保留）<br>`Numbers: rail_pos=0.31 trend=+4.2% dd=-8.1% atr=6.4 atr_pct=3.1% rv20=38.2 rv_pctl=71 volume_x=0.62 range_pctl=58 earnings_in=11` |
| `Listed puts:` | 由步 5 替换为带数字的候选表 |

> **注意：本步的 `Numbers` 块里没有 `iv=`**。D4a 不产出 IV，因此也不给模型任何 IV 数字 —— 给出一个反相的数字比不给出更糟。`rv20` 与 `rv_pctl` 是它的诚实替代（"标的自己在动多少"，而非"保险卖得贵不贵"）。`iv=` 字段在 D4b 落地后加入。

落库侧：`symbol_phases` 增加一列 `indicators jsonb`（迁移 `029_phase_indicators.sql`，加列不删列），审计与回看时数值可复现，不必重算。

> **为什么不用纯数值替代 tag**：tag 是**审计的聚合键**（`GROUP BY tag` 出计数），数值是**模型判断的输入**。两者用途不同，都要留。

### 3.5 阈值全部进 Settings，写死即违约

`StrategySettings` 新增字段（均有默认值，旧 payload 不受影响）：

```
trend_slope_min_pct, dip_window_sessions, dip_deep_pos, dip_reclaim_pos,
drawdown_band_high_pct, drawdown_band_low_pct, volume_dry_multiple,
atr_window, atr_wide_ratio_pct, rv_window, rv_high_pctl, rv_low_pctl,
regime_index, regime_lookback, reclaim_window, rsi_period(已有)
```

理由：本步引入 14+ 个阈值，任何写死在函数里的数字都会在步 6 调参时变成不可审计的暗值。

> **D4b 预留（本步不写入 Settings）**：`iv_rich_min_pts`、`iv_cheap_max_pts`、`iv_atm_max_moneyness_pct`。**故意不提前加**——加了但没有代码读，会成为"看起来可调其实是空开关"的假旋钮，正是步 9 §3.4 要禁止的设置完整性问题。
>
> **也不在本步**：`iv_rv_multiple`（步 5 的字段，见 §3.1.6）—— 它是**操作者知识**而非可测量指标，放在指标层会让本步显得在假设。

### 3.6 与步 5 / 步 6 的接口

```
pricing.daily_bars ──► indicators.compute ──► Indicators ──┬─► tags.py 注册表 → tag 集合
                                                            ├─► _context 的 Numbers 块
                                                            └─► screener.metrics 的 σ 输入（步 5 的 delta_proxy）
```

`Indicators` 是**唯一**的指标产物对象；步 5 与步 6 都从它取数，不得各自重算。

---

## 4. 决策点

| # | 待决 | 选项 | 推荐 | 说明 |
| --- | --- | --- | --- | --- |
| **D4a**（本步） | **是否存在可用的 IV 口径** | (a) `RV × 倍数` (b) 接 IB 链 IV (c) 跨式近似反推 **(d) 本步不引入 IV，只去伪 + 留接口** | **✅ 已裁定 (d)** | **已落地 (d)。** `HIGH_IV` 不再进 `_panic()`，`estimated_iv()` 恒为 `None`，波动率门是 `RV_HIGH` / `RV_LOW` |
| **D4b**（独立排期，**已裁定拆两段**） | 真 IV 的获取与排期 | 每 cycle 采 ATM 链 mid → `chain_marks` → 解 BS 得 σ；排期 (a) 整步延后 (b) 只提前采集 (c) 采集+消费一起提前 | **✅ 已裁定 (b)**：**4b① 采集**挂步 1 之后立即启动（静默期即刻计时，只写自己的表、无下游读取方）；**4b② 消费**留步 5 之后 | **4b① 已落地。** 迁移 `035`。实盘 slot 追加 ATM mid，缺一边则跳过。`estimated_iv()` 仍为 `None`。**4b② 未做** |
| **D16** | 数值字段的承载 | (a) `symbol_phases.indicators jsonb`（加列）(b) 只进 prompt、不留库 | **(a)** | **已落地 (a)。** 迁移 `029` |
| **D17** | `HIGH_IV`/`LOW_IV` 退场 | (a) 保留别名映射 (b) 直接删除 | **(a)** | **已落地 (a)。** 注册表标 `legacy_price_proxy`，不再产生。历史字面量仍可分组 |
| **D18** | `REGIME_RISK_OFF` 的指数与缺数据行为 | 指数选 `SPY` 或 `QQQ`；缺指数数据时 fail-open 还是 fail-closed | **`SPY`；缺数据 → fail-open 但记 `regime_unknown`** | **已落地。** 指数为 `SPY`。没有 bar 时 `regime_unknown`，不因此停掉该标的 |

> **与步 8 共用一个缺口**：watchlist 里**没有任何宽基指数**（实测 `SPY/QQQ/IVV/VOO/VTI/IWM/DIA` 在 `market_bars` 中各 0 行）。因此本步的 `REGIME_RISK_OFF` 会常年返回 `regime_unknown`。该指数序列应**只拉一次、只存一处**，同时供本步与 [08-portfolio-diagnostics.md](08-portfolio-diagnostics.md) 的 CML 锚点使用 —— 不要各自引一个。

---

## 5. 验收标准

| # | 验收 | 判据 | 落地 |
| --- | --- | --- | --- |
| **A1** | tag 注册表是唯一真源 | 全库 grep 六个 tag 字面量（`LOWER_RAIL` 等）只允许出现在 `tags.py` 与 prompt 模板；`phases.py` 里不再有 `PRICE_OWNED/IV_OWNED/NEWS_OWNED` 的集合运算 | **已落地。** 拥有者集合改为 `tags.names_owned_by` |
| **A2** | **反相量不再进入判据**（T1′） | ① fixture：造一行 `symbol_phases.tags` 含 `HIGH_IV`、且当天 `NEWS_WASH=真` → `phases._panic()` **必须为假**（证明 `HIGH_IV` 已从判据移除）；② `grep -rn "HIGH_IV\|LOW_IV" packages/ apps/ --include=*.py` **只命中** `tags.py`（别名表）与历史读路径，**零命中** `phases.py` / `drafts.py` | **已落地。** `_panic` 只看 `PRICE_WEAK` 与 `VOLUME_SPIKE`。`test_legacy_names_are_not_quoted_outside_the_registry` |
| **A3** | **`RV_HIGH/RV_LOW` 对价格缩放不变**（真正的不反相检验） | fixture：取 T 的真实 60 日序列，构造 `S' = 2·S`（同一组百分比收益）→ 对数收益逐日相同 ⇒ `RV20` **逐位相同** ⇒ `RV_HIGH/LOW` 与 `rv_pctl` **逐位相同**。再取一条真实 −18% 下跌序列：`RV20` 的变化**只由波动结构决定**，不被价格水平驱动 | **已落地。** `test_rv_is_unchanged_when_prices_scale` |
| **A4** | 窗口不混算（为 D4b 预置） | 造 1 行 `method='premium_ratio'` 与 20 行 `method='chain_bs'` → 两条查询路径都只在自己 method 内取 min/max；`premium_ratio` 行**不参与** `chain_bs` 的 rank。另断言：**本步不写入任何非 `premium_ratio` 的 method**（`chain_bs` 只是 fixture，证明地基可用） | **已落地。** `test_method_windows_do_not_mix`。迁移 `028` 加了 `method` |
| **A5** | 六月可表达 | 构造一条合成序列（跌 → 深位 → 收复 → 轨道仍上行）：`DIP_BOUGHT` 为真；同时 `LOWER_RAIL` 与 `PULLBACK` **均为假**（反证它们覆盖不到） | **已落地。** `test_june_dip_does_not_need_the_rail_tags` |
| **A6** | 量能指标受步 1 保护 | 在只存在 `ib-delayed`（volume 全 0）的 fixture 上，`VOLUME_DRY`、`ATR`、`RV20` 均返回 `unknown` 而**不是 0 或 1** | **已落地。** `test_zero_volume_is_unknown` |
| **A7** | 数值进 prompt 且进库 | 一条 `decision_trails.steps` 中能找到与 `symbol_phases.indicators` 一致的 `rail_pos`/`trend_slope_pct`/`rv_pctl` 数值；**且 `Numbers` 串中不含 `iv=`**（D4a 不产出 IV） | **已落地。** `test_numbers_match_the_stored_payload`、`test_phase_indicators_match_the_reading` |
| **A8** | 阈值不写死 | `grep -E "0\.35\|0\.45\|0\.7\|252\|63\|80\|20" indicators.py` 不命中（除常量名与注释） | **已落地。** `test_indicator_source_has_no_threshold_literals`。阈值在 `StrategySettings` |
| **A9** | 静默期有痕、且不伪造 IV | 上线后前 20 个交易日：`iv_marks` **没有任何新 method 行**（未伪造 IV），`estimated_iv()` 恒返回 `None`，`RV_HIGH/LOW` 因样本不足返回 `unknown`，审计表现为**样本不足**而非 quiet | **已落地（代码）。** `estimated_iv()` 返回 `None`。样本不足时 `RV_HIGH/LOW` 为 unknown。`test_short_rv_history_has_no_vol_tag` |
| **A10** | **D4b 的前置缺口被显式记账** | `09-risk-control-ui.md` 的 sanity 列在活数据上**读出 `INVERTED?`（7/7 标的）** —— 即面板**当场证明**当前口径反相；D4b 落地后该列必须转为 `ok`。该转变本身是 D4b 的验收 | **未做。** 属步 9。Risk 页没有这列 |
| **A11** | **D4b① 采集段可独立启动且零副作用**（D4b 裁定 (b) 的验收） | ① 迁移 `035` 执行后 `chain_marks` 存在且**空**；② 采集 job 跑一轮 → 该表新增若干行，而 `iv_marks`、`symbol_phases.indicators`、`drafts`、`ai_calls` **行数全部不变**（证明无下游副作用）；③ 连续跑 3 个 cycle → 每次追加、**从不 UPDATE/DELETE**（只追加语义）；④ 采集开启后 `estimated_iv()` **仍返回 `None`**（采集 ≠ 消费） | **代码已落地。** `test_chain_snapshot_appends_and_skips_a_missing_bid` 覆盖追加、缺 bid 不写、`estimated_iv()` 仍为 `None`、`iv_marks` 不变。活库的连续 3 个 slot 还没跑完 |

---

## 6. 明确不做

- **不引入期权链数据源**（`option_choices` 的 bid/ask/OI 缺失留给步 5 D20 决定，本步只用既有 `limit_price`）。
- **不产出任何 IV 数字**（D4a）。`estimated_iv()` 返回 `None`，`Numbers` 块不含 `iv=`，`IV_RICH/IV_CHEAP` 不定义 —— 因为**当前数据里没有 IV**，伪造一个反相的 IV 比缺失更危险。
- **不做 LLM 打分或情绪合成**：所有新 tag 都是确定性算术。
- **不改 `phases` 的相位机 P1–P5 与窗口阈值**（`p1_from`…`p5_to` 不动）；本步只换它消费的 tag 与数字（含把 `HIGH_IV` 从 `_panic()` 摘除）。
- **不调 RSI / 触碰阈值**已存在的 `rsi_overbought` 等值，只把它们真正接进 prompt 词汇表。
- **不改 `option_plans` 的结构**（保持单行静态）。**恰恰因为它是单行静态，它不能作为 IV 的分子来源** —— 这是 D4a 的根因，见 §3.1.4。

---

## 7. 风险与回滚

| 风险 | 说明 | 对策 |
| --- | --- | --- |
| 本步之后**仍然没有 IV** | 波动率门由 `RV_HIGH/RV_LOW` 顶上，语义是"标的自己在动多少"，**不是**"保险卖得贵不贵" | 这是**有意接受的缺口**（D4a）。`IV_RICH` 这一判据在 D4b 之前不存在；prompt 里不得暗示模型有 IV 信息。缺口已登记在 §3.1.3 与 A10 |
| D4b② 长期不排期 | `chain_marks` 需要新采集器，可能一直不做 | **已裁定缓解**：采集段（4b①）挂步 1 之后立即启动，静默期不再等待排期；且 `estimated_iv()` 的 `None` 实现与 `iv_marks.method` / `moneyness_pct` 两列是**低成本地基**（§3.1.5 第 1 条），使消费段随时可插入而不必改读路径 |
| 采集段提前后被误当成"已经有 IV" | `chain_marks` 里出现真实盘口 mid 之后，可能有人直接拿它做判据，绕过 20 日静默与 BS 求解 | A11④ 断言采集开启后 `estimated_iv()` **仍返回 `None`**；唯一读 `chain_marks` 的代码是消费段（4b②）；`IV_RICH/IV_CHEAP` 在消费段落地前**不定义** |
| **误以为 (c) 已修好反相** | 这是最危险的一条：跨式近似产出的是 `a·(1/S)`，看起来"年化了很专业"，实际 rank 逐位不变 | §3.1.1 的恒等证明 + A2 的 grep 断言（`phases.py` 零命中）+ A3 的**缩放不变性**检验。**禁止**以任何 `credit` 比值作为 IV 来源 |
| 波动率门静默 20 天 | `RV_HIGH/LOW` 需 20 个样本 | 预期行为；A9 强制"留痕为样本不足"，且 `min_iv_rank_new_short=0` 的活配置不受影响 |
| 新 tag 一次涌入导致行为跳变 | 新增 11 个 tag 会改变触发频率 | 阈值全部进 Settings；上线首日以 `dry_run` 只记录不草案（步 7 的灰度开关），比对 tag 计数基线 |
| `regime` 缺数据 | 指数可能不在 watchlist | D18 选 fail-open ＋ `regime_unknown` 留痕 |
| 回滚 | 指标层是新增模块 | 逻辑回滚：`ATHENA_INDICATORS=off` → `_context` 退回旧 tags 串（旧代码路径保留一个版本）。**迁移 028/029 均为加列**，回滚无需删列 |
