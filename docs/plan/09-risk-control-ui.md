# 步 9 — Risk control 界面：D26、D4a、D36 与行权价带的落地

**主计划**：[00-plan-order.md](00-plan-order.md) ｜ **状态**：**D26 / D4a / D36 / D37 / D38 / D39 均已裁定**；界面细节待决（D33 / D34 / D35）
**依赖**：步 4（口径结论）、步 5（行权价带口径）、步 7（`paper` 档与验收） ｜ **阻塞**：M3 验收需要它先落地（否则 `paper` 档无法被选中、反相无法被看见、D36 的旧名会随验收档上线）

> **2026-10-10 更正**：本册早前版本把 D4 记作"采用 straddle 近似"。**该口径已被实测证伪**（见 [04-indicator-tags.md](04-indicator-tags.md) §3.1.1：`credit` 与 `expiry` 每 symbol 各只有一个取值 ⇒ 跨式近似退化为正线性缩放 ⇒ `iv_rank` 逐位不变）。更正后 D4 拆为 **D4a（本步：不引入 IV，只做反相去伪 + 留接口）** 与 **D4b（新增独立步：采集实测 ATM 链溢价 → 真 IV）**。本册因此从"IV 口径的配置页"改为"**反相的证据页**" —— 见 §3.2。
>
> **2026-10-10 二轮增补（D36–D39 裁定）**：本页新增两件事 —— ① **`min_iv_rank_new_short` → `min_rv_pctl_new_short`（D36，换轴改名）**，它是本册唯一一处 `risk` 块字段跨到波动率卡片；② **行权价带的 5 个字段 + `band_gap` 证据列（D37/D38/D39）**，见 §3.2(b) 与 §3.2(d)。

---

## 0. 本次裁定（两轮）

| 轮次 | 决策 | 裁定 | 落点 |
| --- | --- | --- | --- |
| 一 | **D26** | **按 [07-enablement-acceptance.md](07-enablement-acceptance.md) §3.3 的建议值新增 `paper` 档**，并可在 Risk control 页选中 | 本册 §3.1 |
| 一 | **D4a** | **本步不引入 IV 口径**。`HIGH_IV/LOW_IV` 从 `_panic()` 摘除、不再产生；波动率门改由 `RV_HIGH/RV_LOW` 承担；`estimated_iv()` 恒返回 `None`。页面上**只保留反相的证据显示**（sanity 列），不再提供"换一个算式就修好"的假选项 | 本册 §3.2 |
| 一 | **D4b** | 真 IV 的获取路线（`chain_marks` 采集 → 解 BS → 20 样本）**采集合规后另行启动**；UI 只留一个**置灰不可选**的 `chain_bs` 占位 | 本册 §3.2(b) / §6 |
| 二 | **D4a/D4b 拆分** | 确认拆分（D4a 本步，D4b① 采集挂步 1 之后、D4b② 消费留步 5 之后） | 主计划 §3 |
| 二 | **D36** | `min_iv_rank_new_short` → **`min_rv_pctl_new_short`**，读 `rv_pctl`，值保持 **40**；**放本页的 `Volatility reading` 卡片** | 本册 §3.1 / §3.2(b) |
| 二 | **D37** | 行权价带改为**按波动率缩放的 delta 目标**；`strangle_otm_pct` → **`chain_band_pct`** | 本册 §3.2(b) |
| 二 | **D38** | `iv_rv_multiple_hi/mid/lo` 进 Settings（1.05 / 1.35 / 1.80），**标注"假设，非测量"** | 本册 §3.2(b) |
| 二 | **D39** | 行权价带的 5 个字段**并入 `Volatility reading` 卡片**（不单开卡片） | 本册 §3.2(b) / §3.4 |

> 这些事落在**同一页**（`/risk`），因此合并为一册，避免同一份 UI 被几处分头描述后走样。

---

## 1. 目标

| # | 目标 | 一句话判据 |
| --- | --- | --- |
| **U1** | `paper` 档可被一键选中且**十个字段全部落库** | 点 `paper` 卡片 → 保存 → 新版本的 `risk` 十项与 §3.1 表逐项一致 |
| **U2** | 档位身份**不误判** | 十项中任一项与预设不同 → `profile` 标为 `custom`，不得仍显示 `paper` |
| **U3** | **口径可见**（可改性移交给 D4b） | 页面上能看到"当前用什么口径、样本够不够、每标的读到的值与得出的 tag"。**本步不提供"换成 straddle 即可修正"的选项**，因为那个选项是假的（§3.2(b)） |
| **U4** | **反相可被当场发现** | 读回表含一列自动校验：口径若随价格反向，UI 直接标红。**当前活数据下应为 7/7 标红** —— 这是**预期正确**的读数，不是故障 |
| **U5** | **波动率门与位置门不再混为一谈**（D36） | 页面上 `min_rv_pctl_new_short`（问"在动吗"）与 `LOWER_RAIL`（问"在哪"）**分处不同判据**；`min_iv_rank_new_short` 这个旧名在页面上**零出现** |
| **U6** | **行权价带的缺口可见**（D37/D39） | 读回表第二组列显示 `target_otm%` / `listed_otm%` / **`band_gap`**。**上线当天七个标的的 `band_gap` 应全为正** —— 这是**通过条件**，D37 落地后归零 |

---

## 2. 现状取证（`apps/orchestrator/src/athena_orchestrator/pages.py`）

### 2.1 档位只有 6 项，且身份判定只比这 6 项

```python
PROFILES = {                      # (max_symbol, cash, halt, contracts, min_ivr, min_dte)
    "defensive":  (10, 40, 3, 1, 60, 21),
    "standard":   (20, 30, 5, 3, 50, 7),
    "income":     (25, 25, 5, 2, 45, 14),
    "aggressive": (35, 15, 8, 4, 30, 7),
    "test":       (80, 5, 20, 12, 0, 1),
}
```

而 `RiskLimits` 有 **10** 个字段。**不在 PROFILES 里的 4 项**：`max_position_per_symbol`、`max_short_opt_margin_pct`、`earnings_blackout_days`、`max_orders_per_day`。

后果有两层：

1. 点档位卡片只填 6 个字段（`data-limits` 只带 6 个数），其余 4 项**保持表单里的现值** —— 也就是"同一个档位名，底下可以是不同的 cap"。
2. 保存时的身份判定只比 6 项：

```python
profile = next((name for name, preset in PROFILES.items() if preset == values), "custom")
```

⇒ 一个 `max_orders_per_day = 50`、`max_short_opt_margin_pct = 95` 的配置，只要 6 项对上，仍会被标成 `paper`/`standard`。**档位名变成了一个不可信的标签。**

### 2.2 `max_orders_per_day` 在界面上完全不可编辑

`_post_risk` 里它直接继承旧值：

```python
max_orders_per_day=current.risk.max_orders_per_day,
```

⇒ D26 的 `paper` 档需要 `6`，但界面**没有这项输入**。不补字段，`paper` 档无法被完整应用。

### 2.3 `ivr_min_samples` 被档位**隐式覆写**（这是 D4a 的钩子）

```python
if profile == "test":
    payload.strategy.ivr_min_samples = 1
elif current.risk.profile == "test":
    payload.strategy.ivr_min_samples = 20
```

⇒ 一个**不在本页显示**的指标参数，被档位静默改写：`test` 档强制 `1`，离开 `test` 档强制 `20`。

这正是评审 §3.4 那条"活配置 `ivr_min_samples = 1` 使 rank 只能取 0 或 100"的**来源**：它不是在 `standard_payload()` 里写错的，而是在**换档时被这行代码写进去的**。**只改口径而不动这三行，等于换了个算法继续被同一个开关按住** —— 而本步已确认"换个算法"这条捷径不存在（§3.2(b)），所以这三行是**唯一必须动的**。

### 2.4 IV 相关参数在界面上一个都没有

| 参数 | 是否可编辑 | 是否可见 |
| --- | --- | --- |
| `strategy.ivr_min_samples` | ❌（被 §2.3 覆写） | ❌ |
| `strategy.ivr_high` / `ivr_low` | ❌ | ❌ |
| IV 口径本身（`iv_marks.method`） | ❌ | ❌ |
| 每标的当前 `iv_proxy` / rank / 得出的 tag | ❌ | ❌ |

⇒ **整条 IV 链路对操作者是不透明的**。这也解释了为什么 `HIGH_IV` 语义反相能长期存活：没有任何界面会把它显示出来。

### 2.5 可复用的既有形态（好消息）

- 档位卡片：`<button class="profile" data-limits="a,b,c…">` ＋ `PROFILE_COPY[name]` 说明文案 —— 加一项即可，无需新组件。
- 数字字段：既有 `_limit(label, name, value, help)` 三元组模式（label / form name / 帮助文本），十个字段已经是这个形态。
- 保存路径：`save_settings(conn, account, payload, f"console:{actor}", f"risk {profile}")` → 返回 `new_id`，**已经是 append-only 的版本写入**，无需改造。
- 版本历史：`_version_rows(conn, account)` ＋ `risk-versions` 面板已存在。

---

## 3. 变更逻辑 spec

### 3.1 D26 — `paper` 档落到 `/risk`

#### (a) 模型层（`packages/settings/src/athena_settings/model.py`）

```python
RiskProfile = Literal["defensive", "standard", "income", "aggressive", "paper", "test", "custom"]
```

并在 `standard_payload()` 同级新增 `paper_payload()`，字段值即下表。**`test` 一行不改。**

| 字段 | `test`（现） | **`paper`（裁定值）** |
| --- | --- | --- |
| `profile` | `test` | **`paper`** |
| `max_symbol_pct_nav` | 80 | **25** |
| `cash_reserve_pct` | 5 | **20** |
| `daily_loss_halt_pct` | 20 | **8** |
| `max_contracts_per_symbol` | 12 | **5** |
| `min_iv_rank_new_short` → **`min_rv_pctl_new_short`**（**D36 已裁定**） | 0 | **40**（值不变，**轴换**：`iv_rank` → `rv_pctl`） |
| `min_dte_new` | 1 | **21** |
| `max_position_per_symbol` | 0（cap off） | **20000** |
| `max_short_opt_margin_pct` | 50 | **35** |
| `earnings_blackout_days` | 7 | **7** |
| `max_orders_per_day` | 10 | **6** |

#### (b) 页面层（`pages.py`）三处改动

1. **`PROFILES` 从 6 元组改为全 10 项**（键名与 `RiskLimits` 字段一一对应）：

```python
PROFILES = {
    "defensive":  {...10 fields...},
    "standard":   {...},
    "income":     {...},
    "aggressive": {...},
    "paper":      {"max_symbol_pct_nav": 25, "cash_reserve_pct": 20, "daily_loss_halt_pct": 8,
                   "max_contracts_per_symbol": 5, "min_rv_pctl_new_short": 40, "min_dte_new": 21,
                   "max_position_per_symbol": 20000, "max_short_opt_margin_pct": 35,
                   "earnings_blackout_days": 7, "max_orders_per_day": 6},
    "test":       {...},
}
```

> 其余 5 档的**新增 4 项必须按现行活值补齐**（`standard` 用 `standard_payload()` 的那 4 项；`test` 用活配置实测值），不得留空 —— 留空会让"点一次卡片"就悄悄改掉 cap，正是 §2.1 的病。

2. **`data-limits` 携带 10 个值**，并把 `_post_risk` 的 profile 判定改为比 10 项：

```python
profile = next((name for name, preset in PROFILES.items()
                if all(values[k] == preset[k] for k in preset)), "custom")
```

3. **补 `Max orders / day` 输入字段**（§2.2）：

```python
_limit('Max orders / day', 'orders', risk.max_orders_per_day,
       'Most orders one day may create, across all symbols on this account. Zero turns this off.')
```

并把 `_post_risk` 的 `max_orders_per_day=current.risk.max_orders_per_day` 改为读表单值。

4. **`PROFILE_COPY["paper"]`** 建议文案：

> `paper` — "The acceptance profile. Real gates: 21-day DTE floor, 40 IV floor, a live name cap. Use this to prove the engine, not to trade size."

### 3.2 D4a — **反相的证据页**落到 `/risk`

#### (a) 删掉隐式覆写

`_post_risk` 中这三行**删除**：

```python
if profile == "test":
    payload.strategy.ivr_min_samples = 1
elif current.risk.profile == "test":
    payload.strategy.ivr_min_samples = 20
```

改由 §3.2(b) 的显式字段承担；`paper`/`standard` 等档位**各自携带**自己的 `ivr_min_samples`（进 10 项之外的"策略块"，见 (c)）。

> **这一条是 D4a 能否成立的前提**。不删它，操作者在界面上把 `ivr_min_samples` 改成 20，下一次换档又被打回 1。

#### (b) 新增卡片 `Volatility reading`（**不是** `IV reading`）

> 卡片名刻意不叫 IV：本步**没有 IV**。命名若叫 IV，操作者会以为调这里能得到 IV 判断 —— 而它实际给的是"标的自己在动多少"。

| 字段 | 表单名 | 默认 | 帮助文本（要点） |
| --- | --- | --- | --- |
| **Method**（**只读**） | — | `premium_ratio` | 当前实际写入 `iv_marks` 的口径。**本步不可改** —— 见下方"为什么不给选项" |
| **`chain_bs`**（**置灰不可选**） | — | — | D4b 的口径占位。工具提示：*"待 `chain_marks` 采集出实测 ATM 链溢价后启用（见 04 册 D4b）"* |
| **Min samples for rank** | `ivr_min_samples` | 20 | rank 窗口的最小样本数。**小于 20 会让 rank 只能取 0 或 100** |
| **RV window** | `rv_window` | 20 | 已实现波动窗口 —— **本步真正的波动率门** |
| **RV HIGH at pctl ≥** | `rv_high_pctl` | 80 | `RV20` 自身百分位 → `RV_HIGH` |
| **RV LOW at pctl ≤** | `rv_low_pctl` | 20 | → `RV_LOW` |
| **Min RV pctl for new short** | **`min_rv_pctl_new_short`** | 40 | **D36 已裁定**：原 `min_iv_rank_new_short`，读的是反相的 `iv_rank`（语义其实是"价格低"），现换轴到 `rv_pctl`、值保持 40。语义 = **"标的确实在动"**，与步 6 的 `LOWER_RAIL`（问"位置"）正交。**旧名在本页不得出现** |
| **Chain band %**（原 `Strangle distance %`） | `chain_band_pct` | 25 | **候选发现**用的宽度，**不是**选中的行权价距离。旧名 `strangle_otm_pct` 的 `10` 曾把 10% 以内的行权价全部丢掉（步 5 §2.7），故更名并放宽 |
| **Strangle Δ target** | `strangle_delta_target` | 0.28 | 真正决定选哪一档的量（≈ z 0.5）。改它会同时改变所有标的的距价 —— 这正是"宽度随波动率变化"的实现方式 |
| **OTM floor / ceiling** | `otm_floor_pct` / `otm_ceiling_pct` | 3 / 20 | 对 Δ 目标算出的距价做夹取；防极低波名字卖 ATM、极高波名字卖废纸。夹取会留痕（`otm_clamped`） |
| **IV/RV multiple** | `iv_rv_multiple_hi/mid/lo` | 1.05 / 1.35 / 1.80 | 按 `RV20` 三档给。**标注为"假设，非测量"** —— 它只用于**算 delta 挑档**，**不得**用作 `IV_RICH` 依据，也不得在 prompt 里被称作 IV（04 册 §3.1.6 三条硬约束） |

> **上表 10 个字段全部是 D37/D38/D39/D36 的裁定落点**（✅ 均已裁定）。其中 `min_rv_pctl_new_short` 属 `risk` 块、其余属 `strategy` 块 —— **本卡片是刻意的跨块聚合**，理由见下方 (c) 的例外条款。

**为什么不给"换方法"的选项**：本册早前版本提供 `straddle_iv` 三选一。**该选项必须删除**，因为它是一个**假旋钮**：

```
iv_proxy_new ≡ a · iv_proxy_old    (a = √(2π/T)×100，每 symbol 常数)
⇒ iv_rank_new ≡ iv_rank_old        (rank 对正线性缩放不变 ⇒ 逐位相同)
```

把它放进选择器，等于向操作者承诺"改这里就能修正反相"，而实际上改动后 `sanity` 列**依然是红的、数值一个字都不变** —— 一个永远无法让读数变好的控件，比没有控件更糟。这与 §3.4 第 6 条"不放假旋钮"、以及 [04-indicator-tags.md](04-indicator-tags.md) §3.5 末注是同一条原则。

> **`iv_rich_min_pts` / `iv_cheap_max_pts` 刻意不出现在本页**，理由同上：D4a 没有读它们的代码。它们随 D4b 一起进本卡片（见 [04-indicator-tags.md](04-indicator-tags.md) §3.5 末注）。

#### (c) 档位与策略块的边界

`ivr_min_samples` 属于 `strategy`（不是 `risk`）。为避免"档位到底改了哪些块"再次变成暗箱：

- **档位卡片写 `risk` 块的资金/暴露类字段**；`ivr_min_samples` 由 IV 卡片独立维护；
- **例外（D36 + D39 裁定引入）**：`risk` 块里**语义属波动率口径**的字段 —— `min_rv_pctl_new_short` —— **不留在档位卡片，随 `Volatility reading` 卡片走**。理由：它与 `rv_high_pctl` / `rv_low_pctl` 是同一族判据，分开放会造出"两个看起来都是波动率门"的旋钮，这正是 D36 与 D39 要消除的；
  - **代价必须写清**：本卡片因此**跨了 `risk` 与 `strategy` 两个块**。保存提示语必须能表达这种跨块改动（见下条），且 `min_rv_pctl_new_short` 仍受档位机制管辖 —— 即**它是档位的一部分**，换档时会被重写。**界面上必须标注它属于当前档位**，否则操作者会以为它是自由字段。
- 但 `paper` 档需要 `ivr_min_samples` 等于 20，因此 `paper_payload()` 里一并设好作为**初值**，UI 之后不再自动改写；
- 保存提示语从 `risk {profile}` 改为列出实际变动的块，例：`risk paper + strategy iv_method`。

#### (d) 读回表（U3 ＋ U4 的核心）

页面下半部新增一张只读表，**每标的一行**，分为**上中下三组**（分组本身就在传达"哪个是本步生效的"）：

**第一组 —— 波动率门与行权价带（本步生效）**

| 列 | 来源 | 说明 |
| --- | --- | --- |
| `symbol` | `settings.watchlist` | |
| `rv20` | `indicators.rv20` | 20 日已实现波动（年化 %）—— 本步的波动率读数 |
| `rv_pctl` | `indicators.rv_pctl` | `RV20` 在自身历史中的百分位；`pending (n/20)` 若样本不足 |
| `tag` | `tags_from_rv(...)` | `RV_HIGH` / `RV_LOW` / `—` |
| `σ_ref` | `iv_rv_multiple × rv20` | **标注为"假设"**，不是 IV |
| **`target_otm%`** | 步 5 §3.8(b) | 该标的在目标 DTE 下**应当**的距价宽度 |
| **`listed_otm%`** | `option_choices` 阶梯最浅一档 | 当前**实际能选到**的最浅距价 |
| **`band_gap`** | `listed_otm% − target_otm%` | **本册的第二张"证据列"**：正值越大，说明可选档位比想要的**远**多少。实测（用 D38 的三档倍数）：`NVDA` **+5.38pp**（listed 10.59 vs target 5.21）、`T` +2.93、`QCOM` +2.76、`700` +2.70、`SKHY` +2.13、`1810` +2.05、`NOK` +1.77 —— **七个标的全部为正**，即阶梯无一例外地偏远，且**波动率越低偏得越多**（`NVDA` 最差）。mega-cap 只能卖深虚值的缺陷，在页面上直接可见 |

> **`band_gap` 与 `sanity` 是同一类设计**：`sanity` 把"口径反相"变成可见的红色，`band_gap` 把"阶梯太远"变成可见的正数。两者都是**只读、无需新数据、由已有字段相减得到**的证据，且都在缺陷修好后自动归零。D37 落地后 `band_gap` 应趋近 0；**A 与 D37 的关系**：`band_gap` 全为正 且 随 `RV20` 单调递减 —— 这是同一条规律的两种呈现。

**第二组 —— IV 诊断（legacy，只读；本步不消费，仅作反相证据）**

| 列 | 来源 | 说明 |
| --- | --- | --- |
| `method` | `iv_marks.method` | 该标的**实际**用的是哪套口径（当前恒为 `premium_ratio`） |
| `samples` | `count(*) FROM iv_marks WHERE method = <active>` | 与 `ivr_min_samples` 对比，不足即为 `pending` |
| `iv_proxy` | 最新一行 | **纯 `1/price` 比值**（本步如实标注，不假装是 IV） |
| `underlying` | 最新一行 | 与上一列并列，便于肉眼判断反向关系 |
| `rank` | `iv_rank(...)` | 空则显示 `pending (n/20)` |
| **`sanity`** | **自动校验（见下）** | `ok` / ⚠ `INVERTED?` |

**`sanity` 列的定义（这是本册最有价值的一列）**：

```
取该标的最近 min(samples, 30) 天的 (iv_proxy, underlying) 序列，
计算 corr( iv_proxy , 1 / underlying )：

  |corr| ≥ 0.90  →  ⚠ INVERTED?      # 代理纯随价格反向 ⇒ 该口径不表达波动率
  samples < 5    →  pending          # 样本不足，不下结论
  否则           →  ok
```

**为什么用这个判据、以及今天它应当读什么**：

评审 §3.4 的全部内容就是"代理 ∝ 1/price"。**实测：活库中该相关系数为 `1.000000`（7/7 标的）**，且 `max|iv_proxy − credit/underlying| ≤ 5e−21` —— 也就是说

> **本步上线当天，这张表的 `sanity` 列应当全红（7/7 `INVERTED?`）。这是正确的读数，不是故障。**

它把"这个口径不表达波动率"从一句论断变成**页面上可复现的证据**。由此得到的验收是**双向**的：

| 时点 | `sanity` 应为 | 含义 |
| --- | --- | --- |
| D4a 上线当天 | **7/7 ⚠ `INVERTED?`** | 面板**当场证明**旧口径反相 —— 这正是本步要留下的证据 |
| D4b 上线并累积 20 个 `chain_bs` 样本后 | **转为 `ok`** | 真 IV 生效。**这个由红转绿本身就是 D4b 的验收**（对应 04 册 A10） |

> **本册早前版本在此处写"换成 `straddle_iv` 后应当显著下降"——该句已删除**，因为跨式近似是正线性缩放，`corr` 会**保持 1.000000 不变**，红色不会转绿。

#### (e) 切换预览（U5）

**本步**可改的只有阈值（`rv_high_pctl` / `rv_low_pctl` / `rv_window` / `ivr_min_samples`），保存前弹出：

```
rv_high_pctl: 80 → 70
  RV_HIGH 标的数:   3 → 9
  RV_LOW  标的数:   4 → 0
  受影响标的:      12 个
  [Confirm and save]  [Cancel]
```

**D4b 启用后**才可能出现口径切换，此处先把预览格式定下来（使 D4b 不必重新设计 UI）：

```
method: premium_ratio → chain_bs
  tag 变化:                7 个 (3 → IV_RICH, 4 → —)
  将转入样本不足(pending):   7 个      # 换口径后历史样本不可复用, 需重新累积 20 个交易日
  预计静默期:              20 个交易日
  sanity 列:               7/7 ⚠INVERTED? → 预期转 ok
  [Confirm and save]  [Cancel]
```

**必须显式显示"预计静默期"**：这是步 4 的 A9（换口径后 20 天无波动率 tag）在操作面上的呈现。若不显示，操作者会在换口径后误判为"系统坏了"。

> **本步不可能出现"改方法就修好"的预览**，因为不存在这样的方法（§3.2(b)）。本步预览的作用是让**阈值**改动的影响可预见，而非暗示口径问题可在界面上解决。

### 3.3 预留位（避免本页被反复改）

| 预留 | 归属决策 | 位置 | 说明 |
| --- | --- | --- | --- |
| `max_bucket_pct_nav` / `max_assign_pct_nav` / `min_cash_after_assign_pct` | 步 8 的 **D32** | 新建"Portfolio budget"卡片，**先只放字段位与帮助文本，取值待 D32 裁定** | I5/I6 是 risk limits，天然属于本页 |
| `chain_band_pct` / `strangle_delta_target` / `otm_floor_pct` / `otm_ceiling_pct` / `iv_rv_multiple_*` | 步 5 的 **D37 / D38** | **放本页的 `Volatility reading` 卡片**（§3.2(b)） | 它们是 `strategy` 块字段，但**语义是波动率口径**，与 `rv_*` 同族。放在同一张卡片可避免"两个看起来都是波动率门"的旋钮（07 册 D36 注亦要求如此）。**旧字段名 `strangle_otm_pct` 的更名同步在本页生效** |
| `may_buy`（每标的一行） | 步 7 的 **D9** | **不放本页** —— 它是 per-symbol 策略标志，应落在 Universe/持仓页与 `csp/cc/strangle/intent` 同列 | 本册只登记归属，避免两处重复定义 |
| `ATHENA_*` 灰度开关 | 步 7 §3.5 | 不放本页 | 它们是**运维开关**（env），不是配置版本；放进配置页会让人误以为可以按版本回滚 |

### 3.4 设置完整性规则（本页必须遵守）

1. **只用 `save_settings`**：不得直写 `settings_versions`。每次保存 = 一个新版本，`note` 必须列出实际变动的块（§3.2(c)）。
2. **字段与模型一一对应**：`PROFILES` 的键集合必须**等于** `RiskLimits` 的字段集合；建议加一条单测断言 `set(PROFILES["x"]) == set(RiskLimits.model_fields)`，防止将来加了字段忘了补档位（§2.1 的根因）。
3. **不静默覆写**：任何"选 A 档就顺带改了 B 块参数"的行为一律删除（§2.3 教训）；改别的块必须在预览里列出。
4. **`test` 档保持可达**：它是开发档，不能被 `paper` 取代；两个卡片并存。
5. **privilege 不受本页影响**：保存后仍为 `all_orders`（现有行为，须保持）。
6. **不放假旋钮**：任何"看得到、改得动、但改了不产生任何行为差异"的控件一律不得上页。判据：**该字段必须有代码读它**。`straddle_iv`（恒等变换，§3.2(b)）与 `iv_rich_min_pts`/`iv_cheap_max_pts`（D4a 无读取方，[04-indicator-tags.md](04-indicator-tags.md) §3.5）因此被排除。前置阶段的字段只允许以**置灰 + 路线说明**（`chain_bs`）呈现，不允许伪装成可调。

---

## 4. 决策点

| # | 待决 | 选项 | 推荐 | 说明 |
| --- | --- | --- | --- | --- |
| **D33** | `PROFILES` 的结构 | (a) 改为全 10 项 dict（§3.1(b)）(b) 保留 6 元组，另存 4 项在别处 | **(a)** | (b) 保留了"同名不同 cap"的病；且 D32 还要再加 3 个字段，(a) 才能自然容纳 |
| **D34** | 口径在 UI 的权限 | (a) 可选（含假选项 `straddle_iv`）(b) **只读显示当前口径 + 置灰的 `chain_bs` 占位** | **(b)** | (a) 已被推翻：跨式近似是恒等变换，选了红色**不会**变绿，等于提供假旋钮（§3.2(b)）。口径的真切换属 D4b；(b) 既显示事实又留路线 |
| **D35** | 是否现在就放 Portfolio budget 卡片 | (a) 放字段位、取值待 D32 (b) 等 D32 裁定后再加 | **(a)** | 位子留好成本极低；不留则会为了 3 个字段再改一次页面结构 |
| **D39** | 行权价带的字段放在哪张卡片 | (a) 单开"Strangle band"卡片 (b) **并入 `Volatility reading`** (c) 放回原 `strangle_otm_pct` 的位置 | **✅ 已裁定 (b)** | 它们是 `strategy` 块的字段但**语义属波动率口径**；(a) 会造成"两个波动率门"的错觉（07 册 D36 注明确要求合并）；(c) 保留了旧名字所带的错误暗示 |

**已裁定汇总（本册相关）**：D26、D4a、D4b（排期：只提前采集）、**D36 (a)**、**D37 (c)**、**D38 (b)**、**D39 (b)**。**仍待决**：**D33**（`PROFILES` 结构）、**D34**（口径的 UI 权限）、**D35**（Portfolio budget 卡片时机）—— 三者都只影响本页的形状，不影响已裁定的语义。

---

## 5. 验收标准

| # | 验收 | 判据 |
| --- | --- | --- |
| **A1** | `paper` 可选且完整 | 点 `paper` → 保存 → 新版本 `risk` 十项与 §3.1 表**逐项相等**；`profile == "paper"` |
| **A2** | 身份不误判 | 手工把 `max_orders_per_day` 改成 50 再保存 → `profile == "custom"`（**不得**仍为 `paper`） |
| **A3** | orders 字段可编辑 | 页面上存在 `Max orders / day` 输入；保存后 `max_orders_per_day` 等于输入值 |
| **A4** | 档位字段集合完整 | 单测：`set(PROFILES[k]) == set(RiskLimits.model_fields)` 对全部 6 档成立 |
| **A5** | 隐式覆写已移除 | 在 `paper` 档下把 `ivr_min_samples` 改为 30 → 保存 → 再次打开页面仍为 30（不被任何档位改写）；`grep` 确认 `_post_risk` 中不再有 `ivr_min_samples = 1` / `= 20` |
| **A6** | 读回表反映真实状态 | 第一组 `rv20`/`rv_pctl`/`tag` 与 `Indicators` 一致；第二组每行的 `method` 与该标的 `iv_marks` 最新行一致；样本数与 `ivr_min_samples` 的对比正确显示 `pending` |
| **A7** | **反相校验双向有效** | ① **活数据**：本步上线当天，第二组 `sanity` 列显示 **7/7 ⚠ `INVERTED?`**（实测 `corr = 1.000000`）—— 这是**通过**条件，不是失败；② fixture：造一组与 `1/underlying` 无关的序列 → 显示 `ok`；③ **反例断言**：对同一组数据套用"跨式近似"重算 → `sanity` **仍为 ⚠**（证明该口径不是修复），此断言防止将来有人把它当解法 |
| **A8** | 预览准确 | 阈值预览的"受影响标的数/各 tag 计数变化"与保存后实际一致；D4b 格式的预览含"预计静默期 20 个交易日"与"sanity 预期转 ok" |
| **A9** | **D36 换轴在页面上生效** | ① 页面**不存在** `min_iv_rank_new_short` 这个标签（含帮助文本、tooltip、`name` 属性）；② 存在 `Min RV pctl for new short` ↔ `min_rv_pctl_new_short`，初值 40；③ 改它并保存 → `settings_versions` 新版本里 `risk.min_rv_pctl_new_short` 变化而 `iv_rank` 相关字段**不变** |
| **A10** | 保存仍 append-only | 连续保存两次 → `settings_versions` 新增 **2** 行，旧行未被修改；`note` 含实际变动的块名 |
| **A11** | **`band_gap` 证据列有效** | ① 上线当天：七个标的 `band_gap` **全为正**，且 `NVDA`（`RV20` 最低）最大（实测 +5.38pp）、`NOK`（最高）最小（+1.77pp）—— **这是通过条件**；② 把 `chain_band_pct` 从 25 调回 10 并复原 `strike_shortlist` 的硬切 → `band_gap` 不减反增（证明缺口来自选择规则而非发现宽度）；③ D37 落地后全部趋近 0 |
| **A12** | **同族字段不分裂** | `chain_band_pct` / `strangle_delta_target` / `iv_rv_multiple_*` / `otm_floor_pct` / `otm_ceiling_pct` / **`min_rv_pctl_new_short`** 与 `rv_window` / `rv_high_pctl` / `rv_low_pctl` **出现在同一张卡片**（§3.2(b)）；仓库内**不再有** `strangle_otm_pct` 作为可编辑标签出现 |
| **A13** | **跨块聚合有提示** | `Volatility reading` 卡片同时含 `risk` 块（`min_rv_pctl_new_short`）与 `strategy` 块字段；保存提示语逐块列出改动；换档后该字段值随之改变（证明它仍受档位管辖，不是自由字段） |
| **A14** | privilege 不变 | 每次保存后 `payload.privilege == "all_orders"` |

---

## 6. 明确不做

- **不改 `privilege`**：本页仍只展示、不放开 `opens_only` / `auto_within_risk`。
- **不接真期权链 IV，也不假装能接**：`chain_bs` 只以**置灰不可选**呈现，并在提示里指向 D4b（需先建 `chain_marks` 采集器）。真正的链 IV 采集是 D4b 的范围，**不在本册实施**。
- **不提供 `straddle_iv` 选项**：它是恒等变换、修不好反相，放进 UI 会误导操作者（§3.2(b)）。
- **不显示 `iv_rich_min_pts` / `iv_cheap_max_pts`**：D4a 无代码读取它们；空开关同属设置完整性问题（§3.4 第 3 条）。
- **不保留 `min_iv_rank_new_short` 这个旧名**（D36 裁定 (a)）：它在页面上（标签、帮助文本、`name` 属性）与仓库内都必须**零命中**。`iv_rank` 本身降级为**只读诊断量**，只出现在第二组 legacy 证据列里。
- **不在两处重复放 `min_rv_pctl_new_short`**：它是 `risk` 块字段但**只出现在 `Volatility reading` 卡片**；档位卡片不再重复显示它（否则两处可改、谁赢不确定）。
- **不在界面做回测或试算**：预览只做"现值阈值改动的影响对比"，不做历史重放。
- **不改 `PROFILES` 既有 5 档的 6 项取值**（只为其补齐另外 4 项）。
- **不动 `_limit` 的组件与样式**（沿用既有模式，不引入新前端组件）。
- **不做权限分级**：本页仍由既有 console 访问控制覆盖。

---

## 7. 风险与回滚

| 风险 | 说明 | 对策 |
| --- | --- | --- |
| `paper` 档一下变紧 | 相对 `test`：DTE 1→21、IV 0→40、contracts 12→5、市值上限从关到 20000 | 这是**目的**（`test` 档的门几乎全是虚设，见 07 册 §2.2）。切换不加锁 —— 但 A1 只断言"能选中并落库"，不断言"会开仓"，避免把配置验收和功能验收混在一起 |
| 补 4 项时写错既有档位 | 5 个老档位要各补 4 项 | A4 的单测强制字段集合一致；且 `test` 档的 4 项取**活配置实测值**（`position=0`、`margin=50`、`blackout=7`、`orders=10`），保持现状不变 |
| 读回表成本 | 每标的要读 `iv_marks` 窗口 | 单账户 ≤31 标的 × 一次窗口查询；且 `sanity` 只取最近 30 行。若嫌慢，加 60 秒页面级缓存 —— **不改数据** |
| **面板长红被误当故障** | D4a 期间 `sanity` 列**会一直显示 7/7 ⚠`INVERTED?`**（因为口径确实反相且未被修复） | 表头加固定说明："**此列全红是当前已知状态，代表旧口径不表达波动率；转绿条件是 D4b 上线**"。A7① 把"全红"定为**通过**条件，防止后续有人为让它变绿而放宽阈值或伪造数据 |
| **有人用假修复让红灯转绿** | 最可能的做法是套用跨式近似，或把 `corr` 阈值从 0.90 放宽 | ① 阈值进常量＋注释；② A7③ 的反例断言（套跨式近似后 `sanity` 仍须为 ⚠）；③ §6 明确不提供该选项 |
| **跨块卡片让"档位改了哪些块"重新变成暗箱** | `Volatility reading` 卡片同时含 `risk`（`min_rv_pctl_new_short`）与 `strategy` 字段，是 D36/D39 裁定引入的**刻意例外** | ① 保存提示语逐块列出改动（`risk paper + strategy chain_band_pct`）；② 卡内标注 `min_rv_pctl_new_short` **属于当前档位**；③ A13 断言换档后它随之改变，证明它没有被"漂"成自由字段 |
| **D36 改名漏改 prompt 或 Settings 模型** | 只改界面标签而把 `min_iv_rank_new_short` 留在后端，会让"零命中"断言假通过 | A9① 的检索范围明确含 Settings 模型、`RiskLimits`、prompt 模板、界面标签四处；07 册 A9 做同样断言（两处互为独立检查） |
| 回滚 | 全部是页面与预设改动 | 逻辑回滚：删除 `paper` 键与 IV 卡片，恢复 `PROFILES` 为 6 元组；**无迁移**（`settings_versions` 是 JSON payload，新档位只是多一个取值）。已保存的 `paper` 版本会退化为 `custom`，不影响运行 |
