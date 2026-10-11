# 步 5 — 选股与排序下沉到代码

**主计划**：[00-plan-order.md](00-plan-order.md) ｜ **状态**：**选择逻辑已落地（2026-10-11）**。D37 (c)、D38 (b) 在代码里。D19–D22 按本册推荐落地：Black–Scholes delta、可空流动性列（broker 未返回前不写入，`liquidity` 仍是 `unknown`）、top 3、备兑底价用持股 `avg_price`。无行权价高于成本时，审计记 `cc_no_strike_above_basis`。D39 的 `Volatility reading` 卡片仍属步 9，这些旋钮暂在 Strategy 卡片。发现带保留的是距现价 **以内** 的行权价（最多 8 档、靠近现价），不是 25% 以外的深虚值。
**依赖**：步 4（需要 `RV20` 算 `σ_ref` 与 delta） ｜ **阻塞**：步 6（状态机消费同一份候选）

> **2026-10-10 增补**：新增 §2.7（`strangle_otm_pct = 10` 丢掉全部 10% 以内的行权价，实测三标的）与 §3.8（**D37** 把行权价带从固定百分比改为按波动率缩放的 delta 目标；**D38** `iv_rv_multiple`）。同时**作废**本册早前 §6 的"不改 `strangle_otm_pct` 的语义"一条。触发来源：操作者给出实务口径 —— 30–45 DTE strangle 在 `MRVL` 上是上下各 7–10%、在 `AAPL`/`NVDA` 上约 5%。
>
> **2026-10-10 二轮裁定**：**D37 → (c)**、**D38 → (b)**。§3.8 的做法与参数**即刻生效**；界面落点由 **D39 (b)** 定在 [09](09-risk-control-ui.md) 的 `Volatility reading` 卡片。
>
> **2026-10-11 落地**：选择与排序已在代码里。验收见 §5 的「状态」列。纸面账户上的 MRVL / QCOM / T 各 1 股只用于试下单，**不是**本步验收数据。

---

## 1. 目标

把"哪一条腿、什么价"从**模型的直觉**搬到**代码的算术**，只把结果的前 N 名连数字一起喂给模型：

一句话判据：**任何一条进入 prompt 的 listed line，都带着 `dte / otm_pct / delta / annualized_yield / breakeven` 五个数；没有数的行不允许进入 prompt。**

副产品（同等重要）：**同一份候选要同时供 Engine A / Engine B / 步 6 的状态机使用**，不允许任何一条路径自己重算行权价。

---

## 2. 现状取证

> 本节是 **2026-10-10 改前** 的记录。当前行为以 §3 与 §5 为准。

### 2.1 "one expiry one shot" 的根因在写入端，不在读端

`agents._priced_lines` 读法：

```sql
SELECT expiry, contract_right, strike, limit_price FROM option_choices
WHERE symbol = %s AND limit_price > 0
ORDER BY expiry, contract_right, strike
```

而 `session._store_choices` 写入时：

```python
for expiry in band:                      # band = expiry_band(...)，最多 3 个到期
    ...
    limit_price = None
    if target is not None and expiry == target:     # ← 只有 target 那一个到期取 ask
        limit_price = broker.option_ask(...)
```

⇒ **除 target 到期之外，band 内其余到期的 `limit_price` 一律为 `NULL`**，被 `limit_price > 0` 过滤掉。这就是"单个到期、一击不中就没得换"的**真正原因**：不是模型不会挑，是**系统只给了一个到期报价**。

### 2.2 实测：7 个标的中只有 2 个有可用的 line

| symbol | `option_choices` 行数 | 其中 `limit_price > 0` | `_priced_lines` 实际返回 |
| --- | --- | --- | --- |
| NVDA | 24 | **0** | 回退 `_listed(plan)` → **1 put ＋ 1 call** |
| QCOM | 24 | **0** | 同上 |
| T | 24 | **0** | 同上 |
| 1810 | 8 | **0** | 同上 |
| 700 | 8 | **0** | 同上 |
| NOK | 24 | 6 | 6 条 line，**全部在 `2026-11-13` 一个到期上** |
| SKHY | 24 | 8 | 8 条 line，**全部在 `2026-11-13` 一个到期上** |

到期分布：`2026-11-06 / 11-13 / 11-20` 三个到期被列出，但**只有 11-13（target）有价**。

⇒ 连带后果：无价标的的行为**完全由 `option_plans` 那一行静态数据决定** —— 而 `option_plans` 是 `cache_symbol_chain` 用 `choose_strikes(..., otm_pct=strangle_otm_pct)` 取"距价 10% 的第一档"，即固定距离、固定一档。**这就是"one expiry one shot"的全部内容。**（该"固定距离"本身的缺陷见 §2.7。）

### 2.3 候选行里没有任何可供排序的数字

`_priced_lines` 组装的是**自由文本**：

```python
line = f"{expiry} {'put' if right == 'P' else 'call'} {_show(strike)} limit {_show(limit_price)}"
```

没有 DTE、没有距价百分比、没有 delta、没有年化收益、没有保本点。模型看到的是"哪一个到期、哪一档、什么价"三件事，**排序只能靠猜**。

### 2.4 `option_choices` 里没有流动性字段

| 列 | 是否存在 |
| --- | --- |
| `symbol, expiry, contract_right, strike, limit_price` | ✅ |
| `bid` / `ask` 分开 | ❌（只有 `limit_price`，即 ask） |
| `open_interest` / `volume` | ❌ |
| `iv` | ❌ |

⇒ 评审 §4 提到的"spread/liquidity"在当前表结构下**算不出来**。要么加列（D20），要么显式标注 `liquidity=unknown`（**不允许**用零或猜测填充）。

### 2.5 第 7 套价格口径：它决定模型能看到哪些行权价

`session._cache_strangles` 在拉链之前先取价：

```sql
SELECT close FROM market_bars WHERE symbol = %s AND source = 'ib-delayed' ORDER BY ts DESC LIMIT 1
```

三个问题：
1. 这是**第 7 处**"当前价格"读法，步 1 的清单里**没有列出它**（步 1 §2.1 只列了 6 处）。它必须并入步 1 的 `pricing` 契约。
2. 没有 `close > 0` 护栏，也没有日线回退。
3. 代价不只是算错：`if price is None: continue` —— **拿不到盘中价的标的直接不拉链**，于是该标的在步 3 的预过滤里被判成 `no_chain`。一个"只是今天没盘中行"的标的会被永久降级。

实测（同一时刻）：`NVDA` ib-delayed 229.33 / ib-daily 229.28 / **seed 218.42**。`strike_shortlist(..., otm_pct=10)` 以这个价为锚取 `put ≤ 0.9×price` 的最近 4 档 —— 锚点偏 5%，整个可选的 strike 集合就整体平移。

### 2.6 `drafts.open_quantity` 只看钱，不看风险

```python
if limits.max_position_per_symbol <= 0: return 1
return allowed_open(limits, price, mult, _exposure(...))
```

合约数由市值上限决定，与 strike 距价、delta、ATR **无关**。一个 30% OTM 的 put 与一个 5% OTM 的 put 在同一个市值上限下拿到同样的张数。

### 2.7 `strangle_otm_pct = 10` 把**所有** 10% 以内的行权价丢掉（新发现，2026-10-10）

`options.strike_shortlist` 的判据是**硬阈值**，不是"以 10% 为中心取带"：

```python
band = [s for s in listed if s <= underlying * (Decimal(1) - distance)]   # distance = otm_pct/100
return band[-width:]                                                     # 取最靠近该边界的 4 档
```

⇒ `otm_pct=10` 的真实语义是"**至少** 10% 价外，且取其中最浅的 4 档"，等于把 **`0.9·S ~ S` 之间的全部行权价直接丢弃**。`option_choices` 只存过滤后的结果，所以库里看到的阶梯**天然全部在 10% 之外**：

| symbol | spot | put 阶梯最浅一档 | 距价 | call 阶梯最浅一档 | 距价 |
| --- | --- | --- | --- | --- | --- |
| NVDA | 229.28 | 205 | **−10.59%** | 255 | +11.22% |
| QCOM | 175.50 | 155 | **−11.68%** | 195 | +11.11% |
| T | 22.18 | 19.5 | **−12.08%** | 24.5 | +10.46% |

**没有任何一档落在 10% 以内**（NVDA 的 205 与现价之间是空的）。

**这与交易口径直接冲突。** 一个 30–45 DTE 的 strangle 在实务上是按"大约 30 delta（≈ 0.5σ√T）"定的，于是宽度**随标的波动率变化**：

```
width% ≈ z × σ × √(dte/365) × 100        z ≈ 0.5  ⇒  ≈ 28–29 delta
```

反过来解 σ 即为"市场对该档的定价所隐含的年化波动"。用操作者给的经验值（`MRVL` 30–45 DTE 上下各 **7–10%**；`AAPL`/`NVDA` 各约 **5%**）与库内实测 `RV20` 对照：

| symbol | `RV20`（20 日，实测） | 按操作者口径反推的 `σ_ref` = width/(0.5√T) | 隐含 **IV / RV** 倍数 |
| --- | --- | --- | --- |
| MRVL | 51.95% | 55.8% | **1.07** |
| NVDA | 25.28% | 34.9% | **1.38** |
| AAPL | 16.41% | 34.9% | **2.13** |

以及"按 `z=0.5` 从 `RV20` 正推的宽度"：

| symbol | `RV20` | 30 DTE 推出宽度 | 45 DTE 推出宽度 | 与操作者口径 |
| --- | --- | --- | --- | --- |
| MRVL | 51.95% | **7.45%** | **9.12%** | ✅ 命中 7–10% |
| NVDA | 25.28% | **3.62%** | **4.44%** | ✅ 接近 5% |
| AAPL | 16.41% | 2.35% | 2.88% | ⚠ 偏低（差在 IV/RV 倍数） |

三条推论：

1. **`z ≈ 0.5`（≈ 28–29 delta）是这套口径的锚**，且它把 `MRVL` 的 7–10% 与 `NVDA` 的 ~5% 同时解释掉 —— 两者宽度差 1.6×，`RV20` 差 2.05×，差值就是**低波名字的 IV/RV 溢价更高**（1.07 → 1.38 → 2.13，随 `RV20` 下降单调上升）。这是波动率风险溢价的已知形态，被操作者的两个数点复现。
2. **`AAPL` 的偏差不是噪声，是缺失的那一项**：`RV20 = 16.4%` 而实务宽度对应 `σ_ref ≈ 35%` ⇒ 隐含 `IV/RV ≈ 2`。旧的 `strangle_otm_pct = 10` 既不表达这层信息，也不表达名字差异 —— 它对 `AAPL` **宽了约 2 倍**、对 `MRVL` 宽了约 1.25 倍、对 `CNXC`（`RV20 = 78.3%`，正推 11.2%）反而**略窄**。**两个方向都错。**
3. **`RV20` 的绝对水平不可跨名字比较**（`AAPL` 16.4% vs `NOK` 70.9%），所以波动率门必须用**自身历史百分位**。这独立复验了步 4 的 `RV_HIGH/RV_LOW` 设计（04 册 §3.2）。同时提醒：当前 20 日窗口恰好是一段平静期（`MSFT` 22.6%、`GOOGL` 24.0%），**绝对水平阈值在平静期会把所有名字都判成"低波"**，百分位没有这个毛病。

**副作用（与步 4a 的关系）**：上述"`σ_ref`"是**假设**（由操作者经验 + `RV20` 构造），**不是测量到的 IV**。它足以支撑**行权价选择**（§3.8(b)，理由见 §3.8(e)），但**不能**支撑"溢价是否偏贵"的判断 —— 后者仍须等 D4b。这个区分是本步的关键，不要混。

---

## 3. 变更逻辑 spec

### 3.1 新增 `ScreenerLine`：候选行的唯一形态

新增 `packages/strategy-nfr/src/athena_strategy/screener.py`：

```python
@dataclass(frozen=True)
class ScreenerLine:
    symbol: str
    expiry: date
    right: str                 # "P" | "C"
    strike: Decimal
    ask: Decimal               # option_choices.limit_price
    spot: Decimal              # pricing.last_daily_close（步 1）
    dte: int
    otm_pct: Decimal           # 正 = 价外；put=(spot-strike)/spot，call=(strike-spot)/spot
    delta: Decimal | None      # 见 3.2；σ 未知时为 None
    moneyness: Decimal         # strike / spot
    premium_per_contract: Decimal   # ask × multiplier
    breakeven: Decimal         # put: strike − ask；call: strike + ask
    yield_on_strike_pct: Decimal    # 见 3.3
    yield_on_basis_pct: Decimal | None   # 仅 CC 且有成本价时
    earnings_after_expiry: bool | None
    liquidity: str             # "unknown" | 具体值（D20 落地后才可能不是 unknown）
    rank_key: tuple            # 3.4 的排序键，冗余地保留在行上便于审计
```

**生成者唯一**：`screener.candidates(...)`。**已落地**于 `screener.py`。Agent 与草稿都走它。步 6 的状态机尚未接上。

### 3.2 delta 的近似式（不引入 greeks 服务）

用 Black–Scholes 的 `d1` 与标准正态 CDF，`r = 0`：

```
σ      := σ_ref（见 §3.8(b)：`iv_rv_multiple(rv_class) × RV20`；**步 4b 之后**才换成真 IV）
T      := dte / 365
d1     := ( ln(spot / strike) + 0.5 σ² T ) / ( σ √T )
Φ(x)   := 0.5 × (1 + erf(x / √2))            # math.erf 即可
call Δ := Φ(d1)
put  Δ := Φ(d1) − 1                          # 负值；行上记绝对值并保留 right 区分
```

**σ 的来源优先级（步 4a 不产出 IV，故必须显式声明来源）**：

```
σ, source :=
  1. estimated_iv(conn, symbol, today)                 → source = "iv"       # 步 4b 之后才有值
  2. iv_rv_multiple(rv_class) × indicators.rv20 / 100  → source = "rv_mult"  # 步 4a 的实际路径（§3.8(b)）
  3. None                                              → source = None       # 无足够 bar
```

**每一行 `ScreenerLine` 必须带 `delta_source ∈ {"iv","rv_mult",None}`**，并在 prompt 与审计里如实显示。

**为什么这仍然算诚实**：`σ_ref` 里唯一被"假设"的是 `iv_rv_multiple` 这一项（由操作者经验值 + `RV20` 标定，见 §3.8(b)），它**不是一个假的 IV** —— 它是"该名字的隐含波动约为其已实现的 X 倍"这一**显式声明**，进 Settings、可审计、可被 D4b 的实测替换。相比之下，任何从冻结 `credit` 反推的量都会退化成 `1/price`（04 册 §3.1.4），那才是不可用的。

**δ 选择对 σ 误差不敏感**（§3.8(e)）：σ 错 20% ⇒ 目标档位偏移约 1–2 个百分点，仍落在同一档邻近位置。故 `rv_mult` 足以支撑选股。

**退化行为（必须显式）**：
- `σ is None`（bar 不足、或标的无 chain）→ `delta = None`，**不是 0**；
- `delta is None` 时排序**不得**用 delta，改用"距价带" `|otm_pct − target_otm|`，并在行上标 `delta_source="otm_band"`；
- 任一数值缺失时，prompt 里该格写 `unknown`，不得省略该列（缺列会让模型自己编）。

**已落地。** `delta_source` 在每一行上，并写进 prompt 的 `src` 列。审计记下本轮选中行的 `delta_source`。`estimated_iv()` 仍是 `None`，所以现在的来源是 `rv_mult` 或 `otm_band`。

**验证锚点**：`T=35/365、σ=0.45` 时，`strike=spot` 的 call Δ ≈ 0.53、put Δ ≈ −0.47。`strike = 0.8×spot` 的 put 按上面的 `r = 0` 公式约为 **−0.047**（早前写的 −0.10 是粗估，验收改跟公式）。**A3 的 fixture 直接注入 σ=0.45，不经 `estimated_iv()`**，因此步 4a 期间该断言仍可运行。

### 3.3 收益率的两个口径（不问"哪一个对"，两个都给）

```
yield_on_strike_pct := ask / (strike × multiplier) × 365 / dte × 100
```

- **CSP**：口径即"以行权价全额备兑"的真实收租率 —— 这是业界（Option Income Tools 等）的标准口径，可直接横向比。
- **CC**：额外给 `yield_on_basis_pct := ask / (avg_price × multiplier) × 365 / dte × 100`，`avg_price` 取该标的 STK 持仓成本；无持仓则 `None`。
- 两个口径都进 prompt。**理由**：同一个 CC 在"行权价基准"与"成本基准"下的吸引力可能相反 —— 让模型看见分歧，而不是替它选一个。

**已落地。** 年化收益按 `ask / strike × 365 / dte × 100`，与上面示例行的 21.4 一致。`premium_per_contract` 另记 `ask × multiplier`。有持股成本时，call 行再给 `basis%` 和 `avg`。

### 3.4 排序规则（确定性、可复算）

| 用途 | 过滤 | 排序键（升序） | 取 |
| --- | --- | --- | --- |
| **CSP 候选** | `|Δ| ∈ [csp_delta_lo, csp_delta_hi]`；`limit_price > 0` | `(|Δ| − csp_delta_target)`, 再 `− yield_on_strike_pct` | top N |
| **CC 候选** | `strike ≥ max(cost_basis, spot)`（成本价口径见 D22）；`limit_price > 0` | `strike`, 再 `(|Δ| − cc_delta_target)` | top N |
| **Strangle 双腿** | put 腿用 CSP 的 delta 带，排序目标是 `strangle_delta_target`；call 腿 `strike ≥ spot` 且用同一目标 | 各自同表 | 各 top N。与 CSP / CC 表的行权价不同时，prompt 另附 `strangle puts` / `strangle calls` |
| **确定性默认腿**（供步 6 兜底） | 同上 | 同上 | **top 1** |

默认参数（全部进 Settings）：`csp_delta_target=0.25`、`csp_delta_lo=0.10`、`csp_delta_hi=0.40`、`cc_delta_target=0.30`、`strangle_delta_target=0.28`（§3.8，对应 z≈0.5）。**已落地**，含 Strategy 卡片上的旋钮。`screener_pick` 即 top 1。

> `strangle_delta_target=0.28` 不是另设一套：它与 `csp_delta_target=0.25` / `cc_delta_target=0.30` 同处一个语言里。**delta 是这里的通用货币，距价百分比只是它的显示形式** —— 这正是 §3.8 要把 `strangle_otm_pct` 从选择参数降级为发现参数的原因。

### 3.5 到期回退：把 band 真正用起来

**两端都要改**：

1. **写入端**（`session._store_choices`）：**已落地。** 不再只给 target 到期取 ask。band 内每个到期都取。上限是 3 个到期 × 8 档 put × 8 档 call（发现宽度按 §3.8(a) 从 4 改为 8）。
2. **读取端**：**已落地。** `screener.candidates` 按 `|dte − target|` 逐到期评估；某到期在 delta 带内无可用行则落到下一个到期。

⇒ 这才是"next expiry fallback"的落地；评审 §3.11 的 "One expiry, one shot" 需**同时**修写入端与读取端，只修读取端无效（读端根本没得读）。

### 3.6 价格锚点归位（并入步 1）

`session._cache_strangles` 的价格读取**移交**给步 1 的 `pricing` 契约：

- 优先 `pricing.last_intraday_price`（`ib-delayed`，加 `close > 0` 护栏）；
- 盘中价缺失 → 回退 `pricing.last_daily_close`，**不再 `continue`**（拿不到盘中价不等于没有链条）；
- 同时把这一处补进 **步 1 §2.1 的清单**（口径总数 6 → **7**），并在步 1 的验收 A4（"`FROM market_bars` 只允许出现在 `pricing.py`"）里把它计入。

**已落地。** `session.py` 不再直接 `FROM market_bars`。拉链用 `pricing.last_tradeable_price`（盘中价，缺失则日线收盘）。

> 这一条是步 5 的**前置修正**：若不做，`no_chain` 的误判率不会因为选股改造而下降。

### 3.7 prompt 契约：从自由文本到带数字的表

| 现 prompt | 改后 |
| --- | --- |
| `Listed puts:\n{{puts}}` | `Listed puts (top N, code-ranked):\n expiry       dte  strike  ask   otm%   delta  yield%  be     liq\n 2026-11-13   34   145.0   2.56  6.3%   -0.28  21.4    142.44 unknown` |
| `Listed calls:\n{{calls}}` | 同构表 |

**硬约束**：
- 表里**每一行都必须带满 5 个数字**（`dte/otm/delta/yield/be`）；任一为 `unknown` 也要占位；
- 只喂 top N（D21），**不喂全量**；
- 同时给一行 `screener_pick`：代码选出的默认腿（`top 1`），标明"若无其他理由，采用此行"。这与 D11 不冲突：**代码提名默认腿，模型可以否决并说明理由；模型不能提出表外的行权价**。

**已落地。** 模板仍用 `{{puts}}` / `{{calls}}`，内容换成上表。表外行权价在 `stage_draft` 被跳过。`ATHENA_SCREENER=off` 恢复旧的自由文本。

### 3.8 行权价带：从"固定 10%"改为"按波动率缩放的 delta 目标"（D37 ✅ 已裁定 (c)）

> **裁定状态**：**D37 选 (c)、D38 选 (b)**（2026-10-10 二轮裁定）。本节下方的做法与参数即为**生效规格**，不再标注"待决"。UI 落点见 [09](09-risk-control-ui.md) §3.2(b)（D39 并入 `Volatility reading` 卡片）。

§2.7 的实测表明 `strangle_otm_pct = 10` 是一条**对所有名字都相同**的硬阈值，而实务口径是**大约固定的 delta**（≈ 0.5σ√T，即 ~28–29 delta），宽度随波动率变化。修法分三层，**全部是 Settings 与选择逻辑，不需要迁移**。

#### (a) 把"发现"与"选择"分开（这是根因）

`options.strike_shortlist` 现在**同时**做了两件事：从链条里挑候选、又用 `<= 0.9·S` 施加了策略判断 —— 于是策略参数藏在了取数函数里，且它是个硬切。改为职责单一：

| 函数 | 改后职责 | 参数 |
| --- | --- | --- |
| `strike_shortlist` | **只做发现**：给定链条，返回一个**宽阔**的候选集合，不做取舍 | `width: 4 → 8`、`otm_pct: 10 → chain_band_pct`（默认 **25**） |
| `choose_strikes` | 保留为**兜底**（无价标的仍需要一对腿），但改用下方 (b) 的目标宽度 | `otm_pct` 由 (b) 算出 |
| `screener.candidates` | **唯一的选择者**：按 `|Δ|` 排序取 top N（§3.4） | — |

⇒ 结果：`NVDA` 的候选集合从"只有 −10.59% 及以外"变成"约 **−3% ~ −25%** 连续覆盖"，`220/225` 这类档位**重新可见**。这是修掉"mega-cap 只能卖深虚值"的前提。

#### (b) 目标宽度按波动率缩放

```
σ_ref    := iv_rv_multiple(rv_class) × RV20            # RV20 来自步 4 的 Indicators
width_z  := 0.5                                        # ≈ 28–29 delta，见 §2.7
target_otm_pct := width_z × σ_ref × √(dte/365) × 100
```

`iv_rv_multiple` 按**波动率档**给默认值，取值来自 §2.7 的操作者口径反推：

| `RV20` 档 | `iv_rv_multiple` 默认 | 依据（§2.7） |
| --- | --- | --- |
| ≥ 45% | **1.05** | `MRVL` 反推 1.07 |
| 25% – 45% | **1.35** | `NVDA` 反推 1.38、`T` 1.26 |
| < 25% | **1.80** | `MSFT` 1.55、`AAPL` 2.13 之间 |

**校准验证（用操作者给的三个数点）**：

| symbol | `RV20` | × 倍数 ⇒ `σ_ref` | 30 DTE 宽度 | 45 DTE 宽度 | 操作者口径 |
| --- | --- | --- | --- | --- | --- |
| MRVL | 51.95% | 54.5% | **7.8%** | **9.6%** | 7–10% ✅ |
| NVDA | 25.28% | 34.1% | **4.9%** | **6.0%** | ~5% ✅ |
| AAPL | 16.41% | 29.5% | **4.2%** | **5.2%** | ~5% ✅ |

三个数点全部落位。**且这不是拟合**：`width_z` 由操作者自己的 `MRVL` 数字定住，倍数由三个数点的比值定住，之后再无自由参数。

#### (c) 护栏：把宽度夹在合理区间

```
otm_pct := clamp(target_otm_pct, otm_floor_pct (默认 3), otm_ceiling_pct (默认 20))
```

防两类病态：极低波名字（`AAPL` 平静期）算出 2% 以内的带，等于卖 ATM；极高波名字（`CNXC` 78%）算出 20% 以外的带，等于卖废纸。夹取**必须留痕**（审计里记 `otm_clamped: floor|ceiling`），不得静默。

#### (d) `strangle_otm_pct` 的处置

| 处置 | 说明 |
| --- | --- |
| **语义变更**（不是删除） | **已落地。** 字段改为 `chain_band_pct`（发现带宽，默认 25）。Strategy 卡片标签是 `Chain band %`。发现结果是距现价 **以内**、靠近现价的最多 8 档，不是 25% 以外的深虚值。Python 里不再读取旧字段名 |
| 保留为**只读回显** | **未做。** `target_otm%` 读回与 `band_gap` 在步 9 的 `Volatility reading` 卡片（D39） |

> **为什么不用"直接设 `strangle_otm_pct = 5`"绕过去**：那只对 `AAPL`/`NVDA` 成立，对 `MRVL` 会把宽度砍掉近一半、对 `CNXC` 会砍到三分之一。操作者给的两个例子（`MRVL` 宽、`AAPL`/`NVDA` 窄）**本身就说明**这个参数不能是全局常数。

#### (e) 为什么"IV 不确定"对本步影响有限（对操作者问题的直接回答）

delta 选择对 σ 的误差是**一阶小量**：`strike 偏移 ≈ z·σ√T`，σ 错 20% ⇒ 偏移错 20%，而**偏移本身只有 5–10%**，故绝对误差约 **1–2 个百分点**。而 `σ_ref` 的误差主要来自 `iv_rv_multiple` 这一项，其影响同阶。

⇒ **`RV20`-based 的 `σ_ref` 足够支撑"挑哪一档"**；真正的损失发生在"**这一档的价格贵不贵**"，那需要实时溢价分子，属 D4b。这条区分决定了步 4a 的缺口**不影响选股**、只影响 `IV_RICH`。§3.2 的 `delta_source` 降级链据此实现：`estimated_iv → rv20/iv_rv_multiple → None`。

#### (f) 验收（可执行）

| # | 验收 | 判据 | 状态 |
| --- | --- | --- | --- |
| A11 | mega-cap 的浅档重新可见 | 在 `RV20 ≈ 25%` 的 fixture 上，选中 put 的 `otm_pct ∈ [3%, 7%]` | **✅** 单测。另：2026-10-11 只读重拉 NVDA（现价 229.33），put 阶梯为 190–225，220 / 225 在列 |
| A12 | 宽度随波动率变化 | 同一 34 DTE，`RV20` 52% / 25% / 16% 的选中 put 分别落在 [6.5%, 9.5%] / [4%, 6%] / [3.5%, 6%]，三者不等 | **✅** 单测 |
| A13 | 三个数点复现 | MRVL / NVDA / AAPL 的 30 与 45 DTE 宽度与 (b) 表一致（±0.3pp） | **✅** 单测 |
| A14 | 夹取有痕 | `RV20 = 8%` 夹到 `otm_floor_pct`，审计含 `otm_clamped=floor` | **✅** 宽度单测；`otm_clamped` 写入 decision trail |
| A15 | 无假旋钮 | `chain_band_pct`、`otm_floor_pct`、`otm_ceiling_pct` 有代码读取；旧字段名在 Python 里零命中 | **✅** Python 零命中。本册 §2 仍保留旧名，作为改前记录 |

### 3.9 合约数与风险挂钩（与步 2 的分工）

`drafts.open_quantity` 保持"钱的上限"职责不变；**本步不接管 sizing**。**已落地的部分**：delta 在候选表里，ATR 在步 4 的指标行里，两者都进 prompt。真正的 sizing 规则留待步 6。

---

## 4. 决策点

| # | 待决 | 选项 | 推荐 | 说明 |
| --- | --- | --- | --- | --- |
| **D19** | delta 的算法 | (a) 闭式 BS 近似（3.2）(b) 只用"距价分档"替代 delta (c) 引入 greeks 服务 | **✅ (a)** | (b) 无法回答"2×ATR 之外的 strike"，且 CC 的"行权价 ≥ 成本"要靠 strike 直接判，delta 是补充而非替代；(c) 超边界 |
| **D20** | 是否给 `option_choices` 加流动性列 | (a) 迁移加 `bid / open_interest / volume / iv`（可空）(b) 不加，`liquidity` 永远 `unknown` | **✅ (a)** | 列已加（迁移 `037`）。broker 未返回这些字段前不写入，行上仍是 `unknown` |
| **D21** | top N | 2 / 3 / 全部 | **✅ 3** | 不足 3 条时全给，不补造 |
| **D22** | CC 排序用的"成本价" | (a) STK 持仓 `avg_price`（含已实现滚动调整）(b) 原始买入价（不调整） | **✅ (a)** | 行上打印 `avg_price`。全部 call 低于成本时审计记 `cc_no_strike_above_basis` |
| **D37** | 行权价带的口径 | (a) 保留固定 `strangle_otm_pct = 10` (b) 固定但调小（如 5）(c) **按 `σ_ref × 0.5 × √T` 缩放，按 delta 目标选择** (d) 直接按目标 delta 选、不设任何带 | **✅ 已裁定 (c)** | 见 §2.7/§3.8。(a) 对 `AAPL` 宽 2 倍、对 `CNXC` 略窄，**两个方向都错**；(b) 只对 mega-cap 成立、把 `MRVL` 砍掉近一半；(d) 缺少护栏，极低/极高波名字会算出 2% 或 20% 以外的带 |
| **D38** | `iv_rv_multiple` 的取值与它算什么 | (a) 不进 Settings，写死在代码 (b) **进 Settings，按 `RV20` 三档给默认 1.05 / 1.35 / 1.80，明确标注为"假设"而非测量** (c) 全部取 1.0（等于忽略波动率风险溢价） | **✅ 已裁定 (b)** | (a) 违反"阈值全进 Settings"；(c) 会让低波名字的行权价系统性过窄 —— 正是 `AAPL` 那个 2.13 倍的偏差。**(b) 的关键约束**：该值只可用于**选档**，**不得**作为 `IV_RICH` 的依据，也不得在 prompt 里被称作 IV；D4b② 落地后由实测 IV 替换 |

**已裁定汇总（本册相关）**：**D37 (c)**、**D38 (b)**（2026-10-10）。**D19 (a)、D20 (a)、D21（3）、D22 (a) 已按推荐落地（2026-10-11）**。界面卡片仍待步 9 的 D39。

---

## 5. 验收标准

| # | 验收 | 判据 | 状态 |
| --- | --- | --- | --- |
| **A1** | band 内每个到期都有价 | 每个到期 `count(limit_price) > 0` | **✅** 2026-10-11 只读重拉 §2.2 的七个名字。NVDA、QCOM、T、NOK、SKHY 的 11-06 / 11-13 / 11-20 每个到期都有 ask。700 与 1810 在 DTE 带里只有 11-27，该到期有 ask。浅档已回到现价附近（例如 NVDA put 225、QCOM put 175、700 put 410） |
| **A2** | 无数字的行不进 prompt | 空 ask 被丢掉；`σ=None` 时 `delta=None` 且 `delta_source="otm_band"` | **✅** 单测 |
| **A3** | delta 近似可核 | ATM call ∈ (0.50, 0.56)、ATM put ∈ (−0.50, −0.44)；`0.8×spot` put ∈ (−0.06, −0.03) | **✅** 单测。深虚值 put 跟 `r=0` 公式（约 −0.047），不跟早前的 −0.10 粗估 |
| **A4** | 到期回退生效 | target 到期无带内行时用下一个到期 | **✅** 单测 |
| **A5** | 排序确定 | 同一 fixture 两次，`rank_key` 顺序相同 | **✅** 单测 |
| **A6** | 排序可解释 | CSP / CC / strangle 的 `rank_key` 与 §3.4 一致 | **✅** 单测 |
| **A7** | 数字齐全 | 每行有 dte、otm、delta、yield、breakeven；缺数写 `unknown` | **✅** 单测 |
| **A8** | 流动性不编造 | bid、open interest、volume 都空时 `liquidity == "unknown"` | **✅** 单测。迁移 `037` 加了可空列，写入端仍不填 |
| **A9** | 价格锚点归位 | `session.py` 不再直接查 `market_bars` | **✅** 拉链走 `last_tradeable_price` |
| **A10** | 单一真源 | `option_choices` 的读在 `screener.py`，写在 `session._store_choices` | **✅** 写入处有豁免注释。测试里的清理与计数查询除外 |

未完成、且不属于本步代码的两项：**步 6** 尚未改用 `candidates()`；**D39** 的 `Volatility reading` 卡片与 `target_otm` 只读回显仍在步 9。`estimated_iv()` 仍返回 `None`。

---

## 6. 明确不做

- **不做组合/仓位优化**：本步输出的是**候选排序**，不是"应该买几张"。sizing 归步 6。
- **不接期权链供应商**：`iv` 与 `bid/ask` 若 broker 给不出，就留 `unknown`（D20 只加列不加源）。
- **不产出 IV**（与步 4a 一致）：`σ_ref` 是"`RV20` × 一个显式声明的倍数"，**不是 IV**，不得在任何界面、prompt 或日志里被称作 IV；`IV_RICH/IV_CHEAP` 属 D4b。
- **不删 `option_plans`**：它退化为"无 chain 时的最后兜底"，仍被 `_listed` 使用。
- **不引入回测**。
- **不改 `phases` 的相位机**：本步只改"列出与排序哪些行权价"，不碰 P1–P5。

> **对本册早前版本的一处更正**：原文写"**不改 `strangle_otm_pct` 的语义**"。**该条作废** —— §2.7 的实测证明该字段的现有语义（`<= 0.9·S` 硬切）正是"mega-cap 只能卖深虚值"的成因，必须改（D37）。改为：`strangle_otm_pct` 的更名与降级（→ `chain_band_pct`）**在本步范围内**。

---

## 7. 风险与回滚

| 风险 | 说明 | 对策 |
| --- | --- | --- |
| ask 调用量上升 | 写入端从每标的 8 次增到 ≤24 次；`chain_band_pct=25` 后 `width=8` 可能再增 | 有界（先 ×3，再 ×2）；若 broker 限流，退化为"只取 target±1 两个到期"并**同步收窄 `chain_band_pct`**（参数化，不写死） |
| delta 依赖 σ，σ 前 20 天不可用 | 步 4 的 `RV_HIGH/LOW` 有静默期，但 `RV20` 本身在 20 根 bar 即有值 | 两者不同：`RV20` 是**滚动统计**（20 根 bar 就能算），`iv_rank` 才需 20 个同 method 样本。故本步的 `σ_ref` **不受 D4a 静默期影响**；仅当 bar < 20 时走退化路径，标 `delta_source="otm_band"`，不让 prompt 出现 0 |
| **`iv_rv_multiple` 被当成 IV 使用** | 最危险的误用：把它乘回 `RV20` 后当"我们的 IV"去做 `IV_RICH` 判断 | ① 字段名不含 iv 以外的暗示（`rv_multiple` 系列）并在 Settings 帮助文本写明"**假设值，非测量**"；② D37/D38 明确禁止用作 `IV_RICH`；③ A15 的"无假旋钮"检查连同 prompt 契约一起断言该值不出现在任何 `iv=` 语境里 |
| **宽度变宽后风险敞口上升** | 从 −10.6% 收到 −5% 意味着被指派概率显著上升（`NVDA` put 从 ~0.14Δ 升到 ~0.28Δ） | 这是**有意**的（同一张合约的权利金约为 3 倍，见 §2.7 的对数：205 put 1.55 → 218 put ≈ 4.94）。但必须由**步 2 的 I1/I2 与步 8 的 I4/I5/I6**在组合层兜住，且 `paper` 档的 `max_symbol_pct_nav=25` / `max_contracts_per_symbol=5` 是这一变化的安全垫 —— **上线顺序上，步 2 必须先在位** |
| `strike ≥ 成本价` 过滤掉全部 CC 候选 | 深套持仓（如成本远高于现价）会永远选不出 CC | **已落地。** 审计与 call 侧文本记 `cc_no_strike_above_basis`。单测覆盖。纸面账户那三笔 1 股持仓不拿来验这一条 |
| top N 太窄 | 只剩 1 条可选时模型无空间 | N=3 且候选不足时返回全部（不补造） |
| 回滚 | 候选层是新增模块 | `ATHENA_SCREENER=off` 恢复旧的自由文本。`ATHENA_SCREENER_BAND=off` 让发现回到「至少 10% 价外、每侧 4 档」。旧字段名不恢复。流动性列可空，不必回滚 |
