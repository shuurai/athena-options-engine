# 步 3 — 审计可辨性

**主计划**：[00-plan-order.md](00-plan-order.md) ｜ **状态**：**已落地**（2026-10-11）。D13 重试 1 次。D14 (a)，迁移 `033`，取值含 `observed`。D15 预过滤下限取 **63** 根，没有绑到步 4 的 252 日窗口。
**依赖**：步 1 ｜ **可与步 2 并行** ｜ **阻塞**：步 4（验收靠本步）

---

## 1. 目标

让审计能回答"**发生了什么**"，而不只是"结果是什么"。落地为**五态可辨**：

| 状态 | 含义 | 一句话判据 |
| --- | --- | --- |
| `drafted` | 有合法草案产生 | `steps` 中存在 `kept` |
| `quiet` | **有数据、模型正常作答、但没有提案** | 全部腿 `dropped`，且原因来自业务规则 |
| `no_data` | **数据不足以判断**（无日线 / 无期权链 / 停牌 / 被屏蔽） | 未调用任何模型 |
| `invalid` | **模型输出不可用**（不可解析或不合 schema，重试后仍失败） | `ai_calls.status = 'invalid'` |
| `observed` | **已评估、未进入草案阶段**（时段未许可草案） | `draft_slots` 不允许，只跑了观察 |

> **`observed` 由步 7 引入**（[07-enablement-acceptance.md](07-enablement-acceptance.md) §3.2）：它把"看过了、只是这一刻不许开"与"真没信号"分开。本步的四态是本表的**前四行**；`observed` 的落库契约见 §3.4（由步 7 一并交付），D14 的 CHECK 约束必须包含它。

目标判据：**给定任意一条 decision_trail，仅凭库内字段就能唯一归类到上述五态之一；不需要读日志文件。**

---

## 2. 现状取证

### 2.1 解析失败被静默降级为"没信号"

```python
def parse_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text or "", re.S)
    if match is None: return {}
    try: payload = json.loads(match.group(0))
    except json.JSONDecodeError: return {}
    return payload if isinstance(payload, dict) else {}
```

三个降级点全部返回 `{}`：
- 回复里根本没有花括号；
- 花括号内不是合法 JSON（例：模型多写了一个尾随逗号、或被截断）；
- 顶层不是对象（例：模型返回了数组）。

而 `_drafts` 中：

```python
items = choice.get("drafts") or []
if not isinstance(items, list) or not items:
    trail["items"] = [_step("", "dropped", "no drafts")]
    write_decision(conn, account_id, symbol, "consolidator", "quiet", "QUIET", trail)
```

⇒ **模型崩了、模型拒答、模型截断、以及真的没有信号，四条路径全部写成 `outcome='quiet', summary='QUIET'`，不可区分。**

### 2.2 唯一的可辨路径是异常

`run_signaled` 捕获异常后写 `outcome='dropped'`、`summary=<异常类名>`。也就是说：**只有抛异常才留下痕迹；返回垃圾不留下痕迹。**

### 2.3 `no_data` 场景被记为业务"没信号"

无期权链的标的：四个 tactic agent 看到的 listed lines 是 `none`，只能答 quiet → 记为 `quiet`。但真相是"**无法评估**"，不是"评估后放弃"。混在一起后，无法回答"这个标的到底被评估过没有"。

### 2.4 无谓调用与审计噪声（实测）

```
agent        | calls | avg latency | tokens
Consolidator |    28 |     3323 ms | 25024
News         |    28 |      800 ms |  8498
Location     |    28 |      906 ms |  7406
```

2 个标的 × 14 轮 = 每 agent 28 次调用，合计约 **75k tokens**。全部 28 条 decision_trail 都是 `consolidator/quiet`。一个 31 标的 × 每日 3 个时段 × 5 分钟周期的工作量下，噪声行会把有信息量的行淹没。

### 2.5 schema 无约束（好消息）

`ai_calls.status/result`、`decision_trails.outcome/summary` 均为自由文本，**无 CHECK 约束、无枚举表**（见 `021_ai_calls.sql`、`022_decision_trails.sql`）。⇒ 本步**不需要迁移**即可扩展状态取值；是否补 CHECK 约束见 D14。

---

## 3. 变更逻辑 spec

### 3.1 为每个 agent 定义回复 schema（数据化，不写死在 if 里）

新增 `packages/strategy-nfr/src/athena_strategy/schemas.py`，用一张表描述每个 agent 的合法回复形状：

| agent | 必需字段 | 类型/取值域 | 可选字段 |
| --- | --- | --- | --- |
| `Location` | `tags` | `list[str]`，元素 ∈ tag registry | — |
| `News` | `news_wash` | `bool` | `event_type` ∈ `NEWS_TYPE_CODES`、`sentiment` ∈ `[-1,1]` |
| `CSP` | `quiet` | `bool` | 若 `quiet=false`：`expiry`(`YYYY-MM-DD`)、`strike`(`>0`)、`limit`(`>0`)、`reason`(`str`) |
| `Purchase` | `quiet` | `bool` | 若 `false`：`limit`(`>0`)、`reason` |
| `Strangle` | `quiet` | `bool` | 若 `false`：`expiry`、`put_strike`、`put_limit`、`call_strike`、`call_limit`、`reason` |
| `Call` | `quiet` | `bool` | 同 CSP 形状 |
| `Consolidator` | `thinking`,`drafts` | `str`、`list[dict]` | 每项 `tactic` ∈ `{BUY,CSP,CC,STRANGLE}`、`limit`(`>0`)、`reason` |

**校验规则**：
- `quiet=true` 时**禁止**附带 strike/limit（防"既 quiet 又给价"的自相矛盾）；
- `quiet=false` 时必需字段缺一即 `invalid`；
- 数值必须 `> 0`（复用现有 `_number` 的语义）。

### 3.2 解析与一次重试

`parse_json` 升级为 `parse_response(agent, text, *, retry_hook) -> tuple[dict | None, str]`，返回 `(payload, status)`，`status ∈ {ok, invalid}`：

```
1. 提取花括号块 → json.loads → 得到候选 dict
   （任一步失败 = 第一次 invalid）
2. 按 3.1 的 schema 校验
   （失败 = 第一次 invalid）
3. 第一次 invalid 时：调用 retry_hook()
   —— 把同一请求重新发一次，追加一条 user 级纠正指令：
      "Your previous reply was not valid JSON for the required schema.
       Reply with JSON only, matching: <schema summary>"
4. 再失败 → 最终 status = invalid，payload = None
```

**重试上限固定为 1 次**（见 D13）。理由：成本已测（Consolidator 单次 ~3.3s / ~900 tokens），重试两次会把单轮延迟推高到不可接受，且生产实践中"第二次仍不合法"通常意味着 prompt 或模型能力问题，而不是偶发。

### 3.3 预过滤：把"无法判断"挡在模型之前

新增 `eligible(conn, settings, symbol) -> tuple[str, str]`，返回 `(level, reason)`，`level ∈ {full, tag_only, none}`：

| 检查 | 不通过 → | 级别 |
| --- | --- | --- |
| 账户被 halt | `none`, `halted` | 不调用 |
| 标的在 `blocked_symbols` | `none`, `symbol blocked` | 不调用 |
| `daily_bars` 为空或不足最小根数 | `none`, `no_bars` | 不调用 |
| 无 `option_choices` 且无 `option_plans` | `tag_only`, `no_chain` | **只调用 Location / News** |
| 该标的三个期权开关全 off | `tag_only`, `options off` | 只调用 Location / News |
| 以上皆过 | `full` | 全部 7 个 |

**关键设计**：`no_chain` 只降级、不跳过 —— 因为 Location/News 的产物（tags、news_wash）本身有审计价值，且是后续指标层（步 4）的输入。真正"什么都判断不了"（无日线）才完全跳过。

预过滤命中时：写一条 `decision_trails(outcome='no_data', summary=<reason>, steps={...})`，**不写 `ai_calls`**。

### 3.4 四态的落库契约

| 状态 | `decision_trails.outcome` | `ai_calls.status` | 是否写 ai_calls |
| --- | --- | --- | --- |
| 有草案 | `drafted` | `ok` | 是 |
| 有数据、无提案 | `quiet` | `ok` | 是 |
| 数据不足 | `no_data` | — | **否** |
| 模型输出不可用 | `invalid` | `invalid` | 是（保留原始 response 便于复核） |
| 已评估、时段不许草案 | `observed` | `ok` | 是（仅 Location / News） |
| 异常（保留现有语义） | `dropped` | `error` | 是 |

`summary` 不再是固定的 `"QUIET"`：
- `no_data` → 原因码（如 `no_chain`）；
- `invalid` → 失败的 agent 名 + 失败阶段（`parse` / `schema`）；
- `quiet` → 各腿原因拼接（可含 `I1:`/`I2:`/`I3:` 前缀，与步 2 对齐）；
- `observed` → `outside_draft_slot`（步 7 交付）。

`steps` 增加两个键：`filter`（预过滤结果）、`err`（原始错误摘要，截断 500 字符）。

### 3.5 成本效果（可测）

预期：无链条标的从"7 次调用"降为"2 次调用"。以当前 31 标的 watchlist 估算，若其中 N 个无链条，则每轮节省 `5N` 次调用。验收时记录**改造前后同一时段的 `ai_calls` 行数与 tokens** 作为对照基线。

---

## 4. 决策点

| # | 待决 | 选项 | 推荐 | 落地 |
| --- | --- | --- | --- | --- |
| **D13** | 重试次数 | 0 / 1 / 2 | **1** | **已落地：1。** 传输错误不重试 |
| **D14** | 是否给 outcome/status 加 CHECK 约束 | (a) 加（需迁移 028）(b) 只在代码与文档层约定 | **(a)** | **已落地 (a)。** 迁移 `033`。`decision_trails.outcome` 含 `observed`；`ai_calls.status` 为 `ok` / `error` / `invalid` |
| **D15** | 预过滤的"最小根数"阈值 | 63 / 与步 4 的窗口绑定 | **与步 4 的最长窗口绑定** | **已落地为 63，未采推荐。** `agents._MIN_BARS = 63`。不足则 `no_data` / `no_bars`，零次模型调用。252 日窗口只用于 `HIGH_RANGE` 改名，不挡调用 |

---

## 5. 验收标准

| # | 验收 | 判据 | 落地 |
| --- | --- | --- | --- |
| A1 | 四态可分 | 构造 4 个 fixture（有草案 / 无提案 / 无链条 / 回复为 `not json`），在 `decision_trails` 与 `ai_calls` 中分别落在 4 个不同取值上。**第五态 `observed` 由步 7 的 A4 验证**（它依赖 `draft_slots`，本步尚无该概念） | **已落地。** `test_audit_states_cover_drafted_quiet_no_chain_and_invalid`。`observed` 仍属步 7 |
| A2 | 重试生效 | 令模型第一次返回 `not json`、第二次返回合法 JSON（mock），最终 `status='ok'` 且 `ai_calls` 有 2 行 | **已落地。** `test_one_invalid_reply_is_retried_once` |
| A3 | 重试上限 | 令两次都返回垃圾，`status='invalid'`，`ai_calls` 恰好 2 行（不是 3 行） | **已落地。** `test_two_invalid_replies_stop_the_pass` |
| A4 | 预过滤省调用 | 对一个无链条标的跑一轮：`ai_calls` 新增 **2** 行（Location/News），`decision_trails` 新增 1 行 `no_data` | **已落地。** 无链条：上表 A1 的 fixture，只有 Location 与 News。不足 63 根：`test_short_history_does_not_call_the_model`，零次调用 |
| A5 | 不再误判 | 全库 `SELECT outcome, count(*) FROM decision_trails GROUP BY 1`，不存在 `invalid` 与 `quiet` 混淆的样本（用 fixture 反证） | **已落地（fixture）。** 解析失败记 `invalid`，不再记 `quiet`。未另扫历史行 |
| A6 | 矛盾回复被拒 | `{"quiet":true,"strike":9}` → `invalid` | **已落地。** `test_contradictory_quiet_reply_is_invalid` |

---

## 6. 明确不做

- 不改 model / provider / 温度等调用参数。
- 不做并发或请求合并（顺序调用保持不变）。
- 不把 `tags` 为空视为异常 —— 空 tags 是合法的 `quiet`，不是 `no_data`。
- 不重构 `ai_calls` 的表结构（只扩展 `status` 取值；`response` 已足够承载原始输出）。

---

## 7. 风险与回滚

| 风险 | 说明 | 对策 |
| --- | --- | --- |
| 重试推高延迟 | Consolidator 单次已 ~3.3s | 重试只对 `invalid` 触发；正常路径零额外成本 |
| 预过滤过度收紧 | 误把有数据的标的判为 `no_chain` | `no_chain` 只降级不打回；A4 同时验证 Location/News 仍被调用 |
| 状态取值膨胀 | 未来可能出现第五态 | D14 选 (a) 后，新增取值必须走迁移，强制讨论 |
| 回滚 | 若重试引发异常或成本异常 | 逻辑回滚：`retry_limit=0` 的开关；无数据变更 |
