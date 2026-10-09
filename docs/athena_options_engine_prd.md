
================================================================================
          智能期权策略交易系统 — 产品策划书 (PRD)
          版本: v1.0 | 日期: 2026-09-15
          目标: Cursor IDE 全栈开发指引
================================================================================

【状态说明 / STATUS】
  本文档为历史输入稿 (cloud-unattended / 多策略产品叙事)。
  现行提案与实现意图以 docs/athena/ 为准:
    → docs/athena/README.md
  冲突时以 docs/athena 锁定决策为准 (HITL、单策略 NFR、Windows 本地、
  iTick 实时 + IB 执行/时段、Settings 特权等级等)。

================================================================================

【文档约定】
  ▶ 表示核心模块
  → 表示数据流向
  ⚡ 表示关键决策点
  📌 表示Cursor开发提示


================================================================================
1. 系统总览
================================================================================

1.1 系统名称
    Athena Options Engine (AOE)
    —— 基于事件驱动架构的智能期权策略执行系统

1.2 核心目标
    构建一个全自动化的期权策略交易系统，实现：
    • 股票自动标签分类（Tagging Engine）
    • 多策略智能切换（CSP / CC / Strangle）
    • 5分钟级信号检测与仓位刷新
    • 事件驱动型新闻前置套利（News Front-Running Cycle）
    • 全流程无人值守执行

1.3 技术栈
    ┌─────────────────────────────────────────────────────────────┐
    │  决策层  │  DSH (DeepSeek Harness) — AI Agent 信号生成        │
    │  数据层  │  iTickDB — 时序数据存储（Tick/Bar/ Greeks）        │
    │  网关层  │  IB Gateway — Interactive Brokers 交易网关         │
    │  执行层  │  Nautilus Trader — Rust核心+Python策略编排          │
    │  调度层  │  APScheduler / Celery Beat — 5分钟定时任务          │
    │  监控层  │  Grafana + 自定义Dashboard                         │
    └─────────────────────────────────────────────────────────────┘


================================================================================
2. 系统架构图
================================================================================

                              ┌──────────────┐
                              │   外部数据源   │
                              │  Yahoo/WSJ/   │
                              │  Fed日历/财报  │
                              └──────┬───────┘
                                     │
                                     ▼
┌─────────────┐    ┌─────────────┐   ┌─────────────┐    ┌─────────────┐
│  DSH Agent  │◄───│  News Feed  │   │  iTickDB    │◄───│  IB Gateway │
│  Harness    │    │  Parser     │   │  (时序库)    │    │  (行情接口)  │
└──────┬──────┘    └─────────────┘   └──────┬──────┘    └─────────────┘
       │                                     │
       │  ┌──────────────────────────────────┘
       │  │
       ▼  ▼
┌──────────────────────────────────────────────────────────────────────┐
│                        Strategy Orchestrator                         │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐ │
│  │ Tag Engine  │  │ Signal      │  │ Position    │  │ Risk        │ │
│  │ (股票标签)   │  │ Detector    │  │ Manager     │  │ Guard       │ │
│  │             │  │ (信号检测)   │  │ (仓位管理)   │  │ (风控闸)     │ │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘ │
│         │                │                │                │        │
│         └────────────────┴────────────────┴────────────────┘        │
│                              │                                       │
│                              ▼                                       │
│  ┌─────────────────────────────────────────────────────────────────┐│
│  │              Strategy Selector (策略选择器)                       ││
│  │   ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐          ││
│  │   │  CSP    │  │   CC    │  │Strangle │  │  Hold   │          ││
│  │   │ 策略    │  │ 策略    │  │ 策略    │  │ (等待)  │          ││
│  │   └────┬────┘  └────┬────┘  └────┬────┘  └────┬────┘          ││
│  │        └─────────────┴─────────────┴─────────────┘              ││
│  └─────────────────────────────────────────────────────────────────┘│
└──────────────────────────────────┬───────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│                    Nautilus Trader 执行层                             │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐ │
│  │   Strategy  │  │   Risk      │  │   Exec      │  │   Cache     │ │
│  │   (Python)  │  │   Engine    │  │   Engine    │  │   (Redis)   │ │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘ │
│         │                │                │                │        │
│         └────────────────┴────────────────┴────────────────┘        │
│                              │                                       │
│                              ▼                                       │
│  ┌─────────────────────────────────────────────────────────────────┐│
│  │              Nautilus Kernel (Rust Core)                         ││
│  │   MessageBus → DataEngine → ExecEngine → Portfolio → Matching   ││
│  └─────────────────────────────────────────────────────────────────┘│
└──────────────────────────────────┬───────────────────────────────────┘
                                   │
                                   ▼
                          ┌──────────────┐
                          │  IB Gateway  │
                          │  (TWS/API)   │
                          └──────┬───────┘
                                 │
                                 ▼
                          ┌──────────────┐
                          │  Interactive │
                          │   Brokers    │
                          └──────────────┘


================================================================================
3. 模块详细设计
================================================================================

3.1 DSH Agent Harness (决策智能体层)
──────────────────────────────────────────────────────────────────────────────
📌 Cursor提示: 此模块为Python项目，独立repo或子目录 /agents/

【职责】
  • 接收外部新闻/事件输入，生成交易信号
  • 管理股票标签生命周期
  • 决策策略类型（CSP/CC/Strangle/Hold）
  • 与Strategy Orchestrator通过gRPC/REST通信

【子模块】

  3.1.1 News Feed Parser
  ─────────────────────
  输入: RSS/API (Yahoo Finance News, WSJ, Fed日历, 财报日历)
  输出: 结构化事件对象 {symbol, event_type, sentiment, timestamp, severity}

  事件类型枚举:
    • FED_RATE_DECISION    — 美联储利率决议
    • EARNINGS_RELEASE     — 财报发布
    • ANTITRUST_FINE       — 反垄断罚款
    • AI_REGULATION        — AI监管新闻
    • SUPPLY_CHAIN         — 供应链消息
    • GENERIC_NEGATIVE     — 通用负面新闻

  情感评分: -1.0 ~ +1.0 (由DSH LLM分析)
  严重等级: 1-5 (1=轻微, 5=市场冲击)

  3.1.2 Tag Engine (股票标签引擎)
  ───────────────────────────────
  📌 核心功能: 给股票打标签，驱动策略选择

  标签体系:
    ┌─────────────────┬──────────────────────────────────────────┐
    │ 标签名          │ 定义与触发条件                            │
    ├─────────────────┼──────────────────────────────────────────┤
    │ HIGH_IV         │ IV Rank > 80%, 适合卖期权收租             │
    │ LOW_IV          │ IV Rank < 20%, 避免卖期权                 │
    │ EARNINGS_NEAR   │ 距离财报 < 5天, 高波动预期                │
    │ FED_EVENT       │ 美联储事件前后3天, 新闻前置套利窗口       │
    │ SUPPORT_TOUCH   │ 价格触及关键支撑位                        │
    │ RESISTANCE_TOUCH│ 价格触及关键阻力位                        │
    │ OVERBOUGHT      │ RSI > 70, 适合卖CC                       │
    │ OVERSOLD        │ RSI < 30, 适合卖CSP                      │
    │ MAG7            │ 属于MAG7成分股                            │
    │ HIGH_BETA       │ Beta > 1.5, 波动放大器                    │
    │ AI_NARRATIVE    │ AI叙事核心标的(NVDA等)                    │
    │ NEWS_WASH       │ 检测到新闻前置打压模式                    │
    │ STRONG_FCF      │ 自由现金流强劲, 抗压性强                  │
    │ WEAK_MARGIN     │ 利润率低, 抗压性弱                        │
    └─────────────────┴──────────────────────────────────────────┘

  标签状态机:
    PENDING → ACTIVE → EXPIRED
    • PENDING: 条件接近触发, 预警状态
    • ACTIVE: 条件满足, 可被策略引用
    • EXPIRED: 条件不再满足或超时(默认7天)

  3.1.3 Signal Generator (信号生成器)
  ───────────────────────────────────
  📌 核心功能: 将标签组合转化为交易信号

  信号类型:
    • OPEN_CSP    — 开仓Cash Secured Put
    • OPEN_CC     — 开仓Covered Call
    • OPEN_STRANGLE— 开仓Strangle
    • ROLL        — 展期(roll up/down/out)
    • CLOSE       — 平仓
    • HOLD        — 持有不动

  信号强度: 1-10 (由标签组合权重计算)
  信号置信度: 0.0-1.0 (由DSH LLM评估)

  信号生成规则示例:
    IF tags.contains("FED_EVENT") AND tags.contains("NEWS_WASH") AND IV_RANK > 70
       → Signal: OPEN_STRANGLE, Strength: 8, Confidence: 0.85

    IF tags.contains("OVERSOLD") AND tags.contains("SUPPORT_TOUCH") AND IV_RANK > 60
       → Signal: OPEN_CSP, Strike: support_level, Strength: 7

    IF tags.contains("OVERBOUGHT") AND tags.contains("RESISTANCE_TOUCH") AND IV_RANK < 40
       → Signal: OPEN_CC, Strike: resistance_level, Strength: 6

  3.1.4 Strategy Advisor (策略顾问)
  ────────────────────────────────
  输入: Signal + 当前仓位 + 账户状态
  输出: 具体执行指令 {action, symbol, option_type, strike, expiry, qty, reason}

  约束检查:
    • 单票最大仓位 ≤ 20%
    • 单日最大交易次数 ≤ 10次
    • 现金预留 ≥ 30% (用于CSP保证金)
    • 同一标的不能同时存在CSP和CC (避免方向冲突)


3.2 iTickDB (时序数据层)
──────────────────────────────────────────────────────────────────────────────
📌 Cursor提示: 可用TimescaleDB/InfluxDB/QuestDB替代, 目录 /data/

【职责】
  • 存储Tick级行情数据
  • 存储Bar数据 (1m/5m/15m/1h/1d)
  • 存储期权链快照 (每5分钟)
  • 存储 Greeks 历史 (IV Rank, IV Percentile, Delta, Theta等)
  • 存储仓位快照

【数据模型】

  Table: market_data_ticks
  ─────────────────────────
  time(TIMESTAMPTZ) | symbol(TEXT) | price(DOUBLE) | size(INT) | exchange(TEXT)

  Table: market_data_bars
  ───────────────────────
  time(TIMESTAMPTZ) | symbol(TEXT) | open(DOUBLE) | high(DOUBLE) | low(DOUBLE)
  | close(DOUBLE) | volume(BIGINT) | timeframe(TEXT)

  Table: options_chain
  ────────────────────
  time(TIMESTAMPTZ) | underlying(TEXT) | expiry(DATE) | strike(DOUBLE)
  | option_type(TEXT) | bid(DOUBLE) | ask(DOUBLE) | last(DOUBLE)
  | volume(INT) | open_interest(INT) | iv(DOUBLE) | delta(DOUBLE)
  | theta(DOUBLE) | gamma(DOUBLE) | vega(DOUBLE)

  Table: iv_history
  ─────────────────
  time(TIMESTAMPTZ) | symbol(TEXT) | iv_current(DOUBLE) | iv_rank(DOUBLE)
  | iv_percentile(DOUBLE) | hv_20d(DOUBLE) | hv_50d(DOUBLE)

  Table: positions
  ────────────────
  time(TIMESTAMPTZ) | account_id(TEXT) | symbol(TEXT) | position_type(TEXT)
  | qty(INT) | avg_cost(DOUBLE) | market_value(DOUBLE) | unrealized_pnl(DOUBLE)

  Table: signals_history
  ──────────────────────
  time(TIMESTAMPTZ) | signal_id(UUID) | symbol(TEXT) | signal_type(TEXT)
  | strength(INT) | confidence(DOUBLE) | tags(JSONB) | executed(BOOLEAN)

【写入策略】
  • Tick数据: 实时写入, 保留7天, 自动降采样为Bar
  • Bar数据: 永久保留
  • 期权链: 每5分钟快照, 保留90天
  • Greeks: 每15分钟计算, 永久保留


3.3 Strategy Orchestrator (策略编排层)
──────────────────────────────────────────────────────────────────────────────
📌 Cursor提示: Python项目, 目录 /orchestrator/

【职责】
  • 5分钟定时触发全量检查
  • 协调DSH信号与Nautilus执行
  • 仓位刷新与再平衡
  • 状态机管理 (INIT → SCAN → DECIDE → EXECUTE → WAIT)

【核心循环 — 5分钟节拍】

  Step 1: REFRESH (刷新)
  ───────────────────────
  • 从IB Gateway拉取最新仓位
  • 从iTickDB读取最新行情
  • 更新所有标签状态
  • 计算当前账户风险指标

  Step 2: SCAN (扫描)
  ───────────────────
  • 遍历Tag Watchlist (被标记的股票)
  • 检测新信号 (调用DSH Signal Generator)
  • 评估现有仓位是否需要调整
  • 检查事件日历 (Fed/财报)

  Step 3: DECIDE (决策)
  ─────────────────────
  • 信号去重 (同一标的5分钟内不重复发单)
  • 信号排序 (按strength × confidence)
  • 资金分配 (可用现金 / 保证金需求)
  • 冲突检测 (避免同时开CSP和CC)

  Step 4: EXECUTE (执行)
  ─────────────────────
  • 将决策转换为Nautilus Order对象
  • 通过Nautilus Risk Engine预检
  • 提交至Nautilus Exec Engine
  • 记录订单状态至iTickDB

  Step 5: WAIT (等待)
  ───────────────────
  • 监听订单回调 (fill/reject/cancel)
  • 更新仓位状态
  • 发送通知 (微信/钉钉/邮件)
  • 进入下一个5分钟循环

【状态机】

  ┌─────┐    refresh    ┌─────┐    scan      ┌─────┐
  │INIT │ ─────────────►│IDLE │ ───────────►│SCAN │
  └─────┘               └─────┘             └─────┘
                           ▲                   │
                           │    execute        │ decide
                           │                   ▼
                        ┌─────┐             ┌─────┐
                        │EXEC │ ◄────────── │DECIDE│
                        └─────┘             └─────┘
                           │
                           │ wait
                           ▼
                        ┌─────┐
                        │WAIT │ ─────────────► (回到IDLE)
                        └─────┘


3.4 Nautilus Trader 执行层
──────────────────────────────────────────────────────────────────────────────
📌 Cursor提示: 使用Nautilus Python API, 目录 /nautilus/

【架构对接】
  Nautilus Trader 提供三种运行环境:
    • BacktestEngine — 回测
    • TradingNode (Sandbox) — 模拟交易
    • TradingNode (Live) — 实盘

  本系统使用 TradingNode (Live) + IB Adapter

【配置要点】

  LiveNodeConfig:
    • trader_id: "athena_trader_01"
    • cache: Redis (用于故障恢复)
    • log_level: INFO
    • exec_engine: 启用部分成交处理
    • risk_engine: 启用预交易检查

  IB Adapter配置:
    • host: localhost (IB Gateway运行在同一机器)
    • port: 7497 (TWS) / 4001 (IB Gateway)
    • client_id: 1
    • account_id: 从环境变量读取

【策略实现模式】

  每个期权策略对应一个Nautilus Strategy子类:

    class CSPStrategy(Strategy):
        # Cash Secured Put 策略
        # 输入: underlying, target_strike, target_expiry
        # 输出: Sell Put Order

    class CCStrategy(Strategy):
        # Covered Call 策略
        # 输入: underlying (已有持仓), target_strike, target_expiry
        # 输出: Sell Call Order

    class StrangleStrategy(Strategy):
        # Strangle 策略
        # 输入: underlying, lower_strike, upper_strike, target_expiry
        # 输出: Sell Put + Sell Call Orders (OCO管理)

  📌 关键: Nautilus的Strategy.on_bar() 或 on_quote_tick() 作为触发入口
  但本系统采用外部Orchestrator驱动模式:
    Orchestrator调用 Strategy.submit_order() 而非策略自主决策

【订单管理】

  订单类型映射:
    ┌─────────────────┬─────────────────────────────────────┐
    │ 策略动作        │ Nautilus Order Type                 │
    ├─────────────────┼─────────────────────────────────────┤
    │ 开仓CSP         │ LimitOrder (Sell, Put)              │
    │ 开仓CC          │ LimitOrder (Sell, Call)             │
    │ 开仓Strangle    │ OrderList (OCO=False, 两条腿)        │
    │ 平仓            │ MarketOrder 或 LimitOrder (Buy)     │
    │ Roll            │ OrderList (OCO=True, 平旧开新)       │
    └─────────────────┴─────────────────────────────────────┘

  Time in Force:
    • 开仓: DAY (当日有效, 收盘前未成交自动取消)
    • 紧急平仓: IOC (立即成交否则取消)
    • 展期: GTC (取消前有效)


3.5 IB Gateway 对接层
──────────────────────────────────────────────────────────────────────────────
📌 Cursor提示: 使用ib_insync或Nautilus原生IB Adapter

【职责】
  • 提供实时行情 (Tick/Bar)
  • 提供期权链查询
  • 提供 Greeks 计算 (IV, Delta, Theta等)
  • 提供账户信息 (仓位/现金/保证金)
  • 提供订单路由

【连接管理】
  • 自动重连 (断线后30秒内恢复)
  • 心跳检测 (每60秒)
  • 多客户端隔离 (本系统client_id=1)

【数据流】
  IB Gateway → Nautilus DataEngine → iTickDB (持久化)
                        │
                        └─► Nautilus Strategy (实时决策)

【期权链查询】
  • 每5分钟查询一次被标记股票的期权链
  • 缓存至iTickDB
  • 供DSH计算IV Rank和选择Strike


3.6 Risk Guard (风控闸)
──────────────────────────────────────────────────────────────────────────────
📌 Cursor提示: 在Nautilus RiskEngine基础上增加自定义规则

【预交易检查 (Pre-Trade)】
  每笔订单执行前必须通过:

  1. 仓位限制
     • 单标的最大敞口 ≤ 账户净值20%
     • 期权卖方总保证金 ≤ 账户净值50%
     • 现金预留 ≥ 账户净值30%

  2. 策略冲突
     • 同一标的不能同时存在CSP和CC
     • 同一标的同一方向5分钟内不重复下单
     • 已持有Put的标的不能再卖Put (除非Roll)

  3. 市场状态
     • 标的停牌 → 禁止交易
     • IV Rank < 20% → 禁止新开卖方仓位
     • 距离到期 < 3天 → 禁止新开仓位

  4. 账户状态
     • 可用现金 < 0 → 禁止开仓
     • 日内亏损 > 账户净值5% → 暂停交易(冷却1小时)
     • 连续3笔订单被拒绝 → 暂停交易(人工介入)

【后交易检查 (Post-Trade)】
  • 订单成交后更新仓位
  • 计算实时希腊值敞口 (Delta, Theta, Vega)
  • 触发预警 (如Delta敞口 > 阈值)


================================================================================
4. 数据流详细设计
================================================================================

4.1 实时行情流
──────────────────────────────────────────────────────────────────────────────

  IB Gateway ──► Nautilus DataEngine ──► iTickDB (ticks表)
                    │
                    ├─► Nautilus Cache (内存, 供策略查询)
                    │
                    └─► DSH Agent (通过API轮询, 5分钟)

  数据频率:
    • Tick: 实时流 (用于价格触发)
    • 1m Bar: Nautilus聚合, 写入iTickDB
    • 5m Bar: 策略主时间框架

4.2 期权链流
──────────────────────────────────────────────────────────────────────────────

  IB Gateway ──► 期权链查询API ──► iTickDB (options_chain表)
                    │
                    └─► DSH Tag Engine (计算IV Rank/Percentile)

  查询频率: 每5分钟一次 (被标记的股票)
  存储策略: 保留最近90天, 自动分区

4.3 信号流
──────────────────────────────────────────────────────────────────────────────

  DSH Signal Generator ──► Strategy Orchestrator ──► 信号队列(Redis)
                              │
                              ├─► 信号去重
                              ├─► 信号排序
                              ├─► 资金分配
                              └─► 生成Nautilus Order

  信号格式 (JSON):
  {
    "signal_id": "uuid",
    "timestamp": "2026-09-15T09:30:00Z",
    "symbol": "NVDA",
    "action": "OPEN_CSP",
    "strategy": "CSP",
    "underlying_price": 218.38,
    "strike": 210.00,
    "expiry": "2026-10-16",
    "option_type": "PUT",
    "qty": 1,
    "strength": 8,
    "confidence": 0.85,
    "tags": ["FED_EVENT", "NEWS_WASH", "HIGH_IV"],
    "reason": "检测到新闻前置打压+IV膨胀+事件落地窗口",
    "max_premium": 5.35,
    "target_dte": 36
  }

4.4 订单流
──────────────────────────────────────────────────────────────────────────────

  Orchestrator ──► Nautilus Strategy.submit_order() ──► Nautilus ExecEngine
                                                          │
                                                          ├─► RiskEngine (预检)
                                                          │
                                                          └─► IB Gateway ──► IB

  回调流:
  IB ──► IB Gateway ──► Nautilus ExecEngine ──► Strategy.on_order_filled()
                                                      │
                                                      ├─► 更新Portfolio
                                                      ├─► 写入iTickDB (positions表)
                                                      └─► 发送通知

4.5 仓位刷新流
──────────────────────────────────────────────────────────────────────────────

  定时触发 (每5分钟):
    IB Gateway ──► 查询账户仓位 ──► iTickDB (positions表, 快照)
                        │
                        └─► Strategy Orchestrator (比对目标仓位vs实际仓位)

  异常检测:
    • 实际仓位 ≠ 预期仓位 → 报警
    • 保证金使用率 > 70% → 报警
    • 现金余额 < 阈值 → 报警


================================================================================
5. 策略引擎详细设计
================================================================================

5.1 策略选择矩阵
──────────────────────────────────────────────────────────────────────────────

  这是系统的核心决策逻辑, 由DSH Agent根据标签组合自动判断:

                    │  低IV (IVR<30)  │  中IV (30-70)   │  高IV (IVR>70)
  ──────────────────┼─────────────────┼─────────────────┼─────────────────
  股价高位+阻力     │   卖CC (弱)     │   卖CC (中)     │   卖CC (强)     │
  股价中位+震荡     │   Hold          │   Hold/Strangle │   Strangle (强) │
  股价低位+支撑     │   卖CSP (弱)    │   卖CSP (中)    │   卖CSP (强)    │
  新闻前置打压期    │   Strangle      │   Strangle (强) │   Strangle (最强)│
  事件落地反弹期    │   卖CSP         │   卖CSP (强)    │   卖CSP (中)    │
  事件落地回落期    │   卖CC          │   卖CC (强)     │   卖CC (中)     │

  📌 注: "强/中/弱" 表示信号强度和仓位大小

5.2 新闻前置套利周期 (News Front-Running Cycle)
──────────────────────────────────────────────────────────────────────────────

  这是用户识别的核心模式, 系统需自动检测:

  Phase 1: 预警期 (T-7 ~ T-3)
  ─────────────────────────────
  信号: 负面新闻集中释放 (AI有害/罚款/监管)
  检测: News Feed Parser 识别负面新闻密度 > 阈值
  标签: NEWS_WASH = ACTIVE
  动作: 将标的加入Watchlist, 开始监控IV

  Phase 2: 恐慌期 (T-3 ~ T-1)
  ─────────────────────────────
  信号: 股价下跌 + IV飙升 + 散户抛售
  检测: 价格跌破5日均线 + IV Rank > 80 + 成交量 > 均值2倍
  标签: HIGH_IV = ACTIVE, OVERSOLD = ACTIVE
  动作: ⚡ 卖Strangle (收租, 赌波动率回归)

  Phase 3: 落地期 (T日)
  ────────────────────
  信号: 事件公布 (加息/数据/决议)
  检测: 事件日历匹配
  标签: FED_EVENT = ACTIVE
  动作: 持有Strangle, 等待IV Crush

  Phase 4: 修复期 (T+1 ~ T+3)
  ────────────────────────────
  信号: IV崩溃 + 股价反弹/企稳
  检测: IV Rank < 50 (IV Crush确认)
  标签: HIGH_IV = EXPIRED
  动作: 平仓Strangle获利, 根据股价位置转CSP或CC

  Phase 5: 狂热期 (T+5 ~ T+10)
  ─────────────────────────────
  信号: 叙事回归 + 股价冲高 + IV回落
  检测: 价格接近阻力 + IV Rank < 40
  标签: OVERBOUGHT = ACTIVE, LOW_IV = ACTIVE
  动作: ⚡ 卖CC (收租, 赌涨不动)

5.3 CSP策略参数
──────────────────────────────────────────────────────────────────────────────

  触发条件:
    • 标签包含 OVERSOLD 或 SUPPORT_TOUCH
    • IV Rank > 50 (确保premium足够)
    • 距离到期 > 7天 (避免Gamma风险)
    • 现金充足 (strike × 100 × qty ≤ 可用现金)

  Strike选择算法:
    1. 获取最近支撑位 (从iTickDB读取20日低点)
    2. 在支撑位下方选择最接近的期权strike
    3. 确保 downside buffer ≥ 5%
    4. 优先选择高流动性strike (open interest > 1000)

  Expiry选择:
    • 默认: 30-45 DTE (时间价值衰减最快区间)
    • 事件后: 15-30 DTE (短期收租)

  仓位大小:
    • 单票最大1-3个contract
    • 总保证金 ≤ 账户净值50%

5.4 CC策略参数
──────────────────────────────────────────────────────────────────────────────

  触发条件:
    • 标签包含 OVERBOUGHT 或 RESISTANCE_TOUCH
    • 持有正股 (qty ≥ 100)
    • IV Rank > 30 (确保premium有意义)

  Strike选择算法:
    1. 获取最近阻力位 (从iTickDB读取20日高点)
    2. 在阻力位上方选择最接近的期权strike
    3. 确保 upside buffer ≥ 3%
    4. 优先选择高流动性strike

  目标价逻辑 (如用户提到的$120目标价):
    • 如果用户设定了目标价, 优先使用用户目标价作为strike
    • 系统计算: 如果被call走, 总收益 = premium + capital gain
    • 确保总收益 ≥ 年化15%

5.5 Strangle策略参数
──────────────────────────────────────────────────────────────────────────────

  触发条件:
    • 标签包含 FED_EVENT 或 NEWS_WASH
    • IV Rank > 70 (IV膨胀期)
    • 事件前3天内
    • 股价处于中位 (非极端高位或低位)

  Strike选择:
    • Put Strike: 当前价 × 0.93 (7% downside)
    • Call Strike: 当前价 × 1.07 (7% upside)
    • 选择最接近的标准strike

  Expiry:
    • 事件后7-14天 (确保覆盖事件波动)

  管理:
    • 事件后1-2天平仓 (利用IV Crush)
    • 如果一边被行权, 另一边自动平仓 (OCO)


================================================================================
6. 标签系统详细设计
================================================================================

6.1 标签生命周期
──────────────────────────────────────────────────────────────────────────────

  标签不是静态的, 而是动态计算的状态:

  ┌─────────────┐     条件满足      ┌─────────────┐     条件失效/超时    ┌─────────────┐
  │   PENDING   │ ───────────────► │   ACTIVE    │ ─────────────────► │   EXPIRED   │
  │   (预警)    │                  │   (生效)    │                    │   (过期)    │
  └─────────────┘                  └─────────────┘                    └─────────────┘
       ▲                                                                  │
       │                                                                  │
       └──────────────────────────────────────────────────────────────────┘
                              条件再次满足 (重新激活)

6.2 标签计算触发器
──────────────────────────────────────────────────────────────────────────────

  每5分钟计算一次:

    HIGH_IV / LOW_IV:
      • 从iTickDB读取IV Rank
      • IVR > 80 → HIGH_IV = ACTIVE
      • IVR < 20 → LOW_IV = ACTIVE

    EARNINGS_NEAR:
      • 从财报日历API读取日期
      • 距离财报 < 5天 → ACTIVE

    FED_EVENT:
      • 从Fed日历读取
      • 事件前后3天 → ACTIVE

    SUPPORT_TOUCH / RESISTANCE_TOUCH:
      • 从iTickDB读取20日高低点
      • 价格距支撑/阻力 < 2% → ACTIVE

    OVERBOUGHT / OVERSOLD:
      • 计算14日RSI
      • RSI > 70 → OVERBOUGHT
      • RSI < 30 → OVERSOLD

    NEWS_WASH:
      • News Feed Parser检测负面新闻密度
      • 3天内 > 5条负面新闻 → ACTIVE

6.3 标签组合与策略映射
──────────────────────────────────────────────────────────────────────────────

  系统维护一个规则库 (Rule Engine):

  Rule #1: FED_EVENT + NEWS_WASH + HIGH_IV → OPEN_STRANGLE (Priority: 10)
  Rule #2: OVERSOLD + SUPPORT_TOUCH + HIGH_IV → OPEN_CSP (Priority: 9)
  Rule #3: OVERBOUGHT + RESISTANCE_TOUCH + HIGH_IV → OPEN_CC (Priority: 9)
  Rule #4: FED_EVENT + IV_CRUSH_DETECTED → CLOSE_STRANGLE (Priority: 10)
  Rule #5: EARNINGS_NEAR + HIGH_IV → OPEN_STRANGLE (Priority: 8)
  Rule #6: LOW_IV + NO_TAGS → HOLD (Priority: 1)

  📌 优先级: 数字越大越优先执行
  📌 冲突解决: 同一标的多条规则触发, 取优先级最高者


================================================================================
7. 5分钟主循环设计
================================================================================

7.1 循环流程图
──────────────────────────────────────────────────────────────────────────────

  ┌────────────────────────────────────────────────────────────────────────────┐
  │                         5分钟主循环 (Main Loop)                             │
  │  Cron: */5 * * * *  或 APScheduler interval=300s                           │
  └────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
  ┌────────────────────────────────────────────────────────────────────────────┐
  │ Step 0: HEALTH CHECK                                                       │
  │ • IB Gateway连接状态                                                       │
  │ • Nautilus TradingNode状态                                                 │
  │ • iTickDB连接状态                                                          │
  │ • DSH Agent可用性                                                          │
  │ ⚡ 任一失败 → 报警 + 跳过本次循环                                          │
  └────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
  ┌────────────────────────────────────────────────────────────────────────────┐
  │ Step 1: DATA REFRESH                                                       │
  │ • 从IB拉取最新仓位 → 写入iTickDB positions表                               │
  │ • 从IB拉取最新账户信息 (现金/保证金/净值)                                   │
  │ • 从iTickDB读取最新行情 (5m Bar)                                           │
  │ • 从IB查询期权链 → 写入iTickDB options_chain表                             │
  │ • 计算 Greeks (IV Rank, Delta, Theta等)                                    │
  └────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
  ┌────────────────────────────────────────────────────────────────────────────┐
  │ Step 2: TAG UPDATE                                                         │
  │ • 遍历Watchlist (被标记的股票列表)                                          │
  │ • 计算每个标签的最新状态 (ACTIVE/PENDING/EXPIRED)                           │
  │ • 更新标签历史记录                                                          │
  │ • 检测标签状态变化 (新增/过期)                                              │
  └────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
  ┌────────────────────────────────────────────────────────────────────────────┐
  │ Step 3: SIGNAL SCAN                                                        │
  │ • 调用DSH Signal Generator生成信号                                         │
  │ • 遍历所有规则 (Rule Engine)                                                │
  │ • 收集所有触发的信号                                                        │
  │ • 信号去重 (同一标的5分钟内不重复)                                          │
  │ • 信号排序 (strength × confidence)                                          │
  └────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
  ┌────────────────────────────────────────────────────────────────────────────┐
  │ Step 4: PORTFOLIO CHECK                                                    │
  │ • 检查现有仓位是否需要调整:                                                 │
  │   - 期权到期 < 3天 → 考虑Roll或平仓                                        │
  │   - 已实现利润 > 50% premium → 考虑提前关闭                                │
  │   - 股价突破strike → 评估是否被行权                                        │
  │ • 计算组合希腊值敞口 (Net Delta, Theta, Vega)                               │
  │ • 检查风险限额                                                              │
  └────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
  ┌────────────────────────────────────────────────────────────────────────────┐
  │ Step 5: DECISION                                                           │
  │ • 合并信号 + 仓位调整需求                                                   │
  │ • 资金分配 (可用现金 / 保证金需求)                                          │
  │ • 冲突检测与解决                                                            │
  │ • 生成最终执行列表 (Action List)                                            │
  └────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
  ┌────────────────────────────────────────────────────────────────────────────┐
  │ Step 6: EXECUTE                                                            │
  │ • 遍历Action List                                                           │
  │ • 每个Action通过Risk Guard预检                                              │
  │ • 生成Nautilus Order对象                                                    │
  │ • 提交至Nautilus ExecEngine                                                 │
  │ • 记录订单状态 (PENDING → SUBMITTED)                                        │
  └────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
  ┌────────────────────────────────────────────────────────────────────────────┐
  │ Step 7: WAIT & NOTIFY                                                      │
  │ • 等待订单回调 (异步, 不阻塞主循环)                                         │
  │ • 订单成交 → 更新仓位 → 发送通知                                            │
  │ • 订单拒绝 → 记录原因 → 报警                                                │
  │ • 订单超时(5分钟未成交) → 自动取消 → 记录日志                               │
  └────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
                              ┌──────────────┐
                              │   SLEEP 5min │
                              │   (等待下次) │
                              └──────────────┘

7.2 并发设计
──────────────────────────────────────────────────────────────────────────────

  主循环是顺序执行的, 但以下操作可并行:

  • 数据刷新 (Step 1): 多标的并行查询IB
  • 标签更新 (Step 2): 多标的并行计算
  • 订单执行 (Step 6): 多个独立标的同时下单

  使用Python asyncio + ThreadPoolExecutor实现


================================================================================
8. 与Nautilus Trader的集成点
================================================================================

8.1 集成架构
──────────────────────────────────────────────────────────────────────────────

  本系统不修改Nautilus源码, 而是通过其Python API进行集成:

  ┌────────────────────────────────────────────────────────────────────────────┐
  │                        Athena Orchestrator (Python)                        │
  │                                                                            │
  │  ┌─────────────────────────────────────────────────────────────────────┐  │
  │  │              Nautilus Python API (nautilus_trader包)                 │  │
  │  │  • TradingNode                                                       │  │
  │  │  • Strategy                                                          │  │
  │  │  • OrderFactory                                                      │  │
  │  │  • Portfolio                                                         │  │
  │  └─────────────────────────────────────────────────────────────────────┘  │
  │                                    │                                       │
  │                                    │ PyO3 Bindings                         │
  │                                    ▼                                       │
  │  ┌─────────────────────────────────────────────────────────────────────┐  │
  │  │              Nautilus Rust Core (nautilus_pyso3.so)                  │  │
  │  │  • NautilusKernel                                                    │  │
  │  │  • MessageBus                                                        │  │
  │  │  • ExecEngine                                                        │  │
  │  │  • DataEngine                                                        │  │
  │  └─────────────────────────────────────────────────────────────────────┘  │
  │                                    │                                       │
  │                                    │ IB Adapter                            │
  │                                    ▼                                       │
  │  ┌─────────────────────────────────────────────────────────────────────┐  │
  │  │              IB Gateway (TWS / IB Gateway独立进程)                   │  │
  │  └─────────────────────────────────────────────────────────────────────┘  │
  └────────────────────────────────────────────────────────────────────────────┘

8.2 关键集成代码结构 (伪代码/设计意图)
──────────────────────────────────────────────────────────────────────────────

  📌 Cursor提示: 以下不是真实代码, 是设计意图描述

  // 1. 初始化TradingNode
  config = LiveNodeConfig(
      trader_id="athena_01",
      cache=RedisCacheConfig(host="localhost", port=6379),
      log_level=INFO,
  )
  node = TradingNode(config)

  // 2. 注册IB Adapter
  ib_config = IBDataClientConfig(
      host="localhost",
      port=7497,
      client_id=1,
      account_id="U1234567",
  )
  node.add_data_client_factory("IB", IBLiveDataClientFactory(ib_config))
  node.add_exec_client_factory("IB", IBLiveExecClientFactory(ib_config))

  // 3. 注册策略
  // 注意: 本系统的策略是"被动驱动型"
  // 策略本身不做决策, 只接收Orchestrator的指令执行
  csp_strategy = CSPStrategy(config=csp_config)
  cc_strategy = CCStrategy(config=cc_config)
  strangle_strategy = StrangleStrategy(config=strangle_config)
  node.add_strategy(csp_strategy)
  node.add_strategy(cc_strategy)
  node.add_strategy(strangle_strategy)

  // 4. 启动
  node.build()
  node.run()

  // 5. Orchestrator驱动下单
  // Orchestrator通过API调用策略的submit_order方法
  csp_strategy.submit_order(
      order=LimitOrder(
          instrument_id=InstrumentId("NVDA  261016P00210000"),
          order_side=SELL,
          quantity=Quantity(1),
          price=Price(5.35),
          time_in_force=DAY,
      )
  )

8.3 事件回调处理
──────────────────────────────────────────────────────────────────────────────

  Nautilus通过事件回调通知订单状态:

  Strategy.on_order_filled(self, event):
    • 解析成交事件
    • 更新内部仓位记录
    • 写入iTickDB
    • 发送通知 (微信/钉钉)

  Strategy.on_order_rejected(self, event):
    • 记录拒绝原因
    • 触发报警
    • 暂停该标的交易(冷却)

  Strategy.on_position_opened(self, event):
    • 记录新开仓位
    • 计算目标管理参数 (profit target, stop loss)

  Strategy.on_position_closed(self, event):
    • 计算 realized P&L
    • 记录交易历史
    • 释放保证金


================================================================================
9. 监控与报警
================================================================================

9.1 监控指标
──────────────────────────────────────────────────────────────────────────────

  系统级:
    • IB Gateway连接状态 (up/down)
    • Nautilus TradingNode状态 (up/down)
    • iTickDB写入延迟 (ms)
    • 主循环执行时间 (应 < 60秒)

  交易级:
    • 当前仓位数量
    • 组合Delta / Theta / Vega
    • 保证金使用率 (%)
    • 当日已实现P&L
    • 当日未实现P&L
    • 订单成交率 (%)

  策略级:
    • 各策略开仓次数
    • 各策略胜率
    • 各策略平均收益
    • 信号命中率

9.2 报警规则
──────────────────────────────────────────────────────────────────────────────

  CRITICAL (立即通知):
    • IB Gateway断开 > 60秒
    • 日内亏损 > 账户净值5%
    • 订单连续3笔被拒绝
    • 保证金使用率 > 80%

  WARNING (5分钟内通知):
    • 主循环执行时间 > 120秒
    • iTickDB写入延迟 > 5秒
    • 单票仓位 > 20%
    • 期权到期 < 2天未处理

  INFO (每日汇总):
    • 当日交易汇总
    • 仓位变化汇总
    • 信号生成汇总

9.3 通知渠道
──────────────────────────────────────────────────────────────────────────────

  • 微信企业号 (实时交易通知)
  • 钉钉机器人 (系统报警)
  • 邮件 (每日汇总报告)
  • Grafana Dashboard (可视化监控)


================================================================================
10. 部署架构
================================================================================

10.1 物理部署
──────────────────────────────────────────────────────────────────────────────

  推荐方案: 单台云服务器 (AWS EC2 / 阿里云ECS)

  ┌────────────────────────────────────────────────────────────────────────────┐
  │                         云服务器 (Ubuntu 22.04)                            │
  │                                                                            │
  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐      │
  │  │ IB Gateway  │  │ Nautilus    │  │ Athena      │  │ Redis       │      │
  │  │ (Docker)    │  │ TradingNode │  │ Orchestrator│  │ (Cache)     │      │
  │  │             │  │ (Python)    │  │ (Python)    │  │             │      │
  │  │ Port: 4001  │  │             │  │             │  │ Port: 6379  │      │
  │  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘      │
  │                                                                            │
  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐                       │
  │  │ DSH Agent   │  │ iTickDB     │  │ Grafana     │                       │
  │  │ (Python)    │  │ (Docker)    │  │ (Docker)    │                       │
  │  │             │  │ Port: 5432  │  │ Port: 3000  │                       │
  │  └─────────────┘  └─────────────┘  └─────────────┘                       │
  │                                                                            │
  └────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    │ Internet
                                    ▼
                          ┌──────────────────┐
                          │ Interactive      │
                          │ Brokers (云端)   │
                          └──────────────────┘

10.2 Docker Compose 结构 (设计意图)
──────────────────────────────────────────────────────────────────────────────

  📌 Cursor提示: 每个服务一个Docker容器

  Services:
    • ib-gateway: 官方IB Gateway Docker镜像
    • itickdb: TimescaleDB/InfluxDB (时序数据库)
    • redis: 官方Redis镜像 (缓存+消息队列)
    • grafana: 官方Grafana镜像 (监控)
    • athena-orchestrator: 本系统主程序
    • dsh-agent: DeepSeek Harness Agent

  Networks:
    • athena-network: 内部通信

  Volumes:
    • itickdb-data: 时序数据持久化
    • grafana-data: 监控配置持久化
    • logs: 日志文件

10.3 高可用考虑
──────────────────────────────────────────────────────────────────────────────

  当前版本为MVP, 高可用性在V2考虑:
    • IB Gateway主备 (双实例+浮动IP)
    • iTickDB主从复制
    • Orchestrator状态持久化 (Redis + 本地文件)
    • 故障自动恢复 (Supervisor/systemd)


================================================================================
11. 开发路线图
================================================================================

Phase 1: MVP (4-6周)
────────────────────
• 搭建基础架构 (Docker + Nautilus + IB Gateway)
• 实现iTickDB数据模型
• 实现5分钟主循环骨架
• 实现CSP策略 (单一标的)
• 实现基础监控 (日志+通知)

Phase 2: 策略扩展 (3-4周)
─────────────────────────
• 实现CC策略
• 实现Strangle策略
• 实现Tag Engine
• 集成DSH Agent
• 实现Rule Engine

Phase 3: 智能化 (4-6周)
───────────────────────
• 实现新闻前置检测
• 实现事件日历集成
• 实现自动Roll管理
• 实现组合希腊值监控
• 实现Grafana Dashboard

Phase 4: 生产化 (持续)
───────────────────────
• 压力测试
• 故障恢复
• 性能优化
• 多账户支持
• 回测框架集成


================================================================================
12. Cursor开发指引
================================================================================

12.1 项目目录结构
──────────────────────────────────────────────────────────────────────────────

📌 Cursor提示: 按以下结构创建项目

athena-options-engine/
├── README.md
├── docker-compose.yml
├── .env.example
├── Makefile
│
├── agents/                          # DSH Agent层
│   ├── __init__.py
│   ├── dsh_client.py               # DSH API客户端
│   ├── news_parser.py              # 新闻解析器
│   ├── tag_engine.py               # 标签引擎
│   ├── signal_generator.py         # 信号生成器
│   └── strategy_advisor.py         # 策略顾问
│
├── data/                            # 数据层
│   ├── __init__.py
│   ├── itickdb_client.py           # iTickDB客户端
│   ├── schema.sql                  # 数据库Schema
│   ├── migrations/                 # 数据库迁移
│   └── scripts/                    # 数据维护脚本
│
├── orchestrator/                    # 策略编排层
│   ├── __init__.py
│   ├── main_loop.py                # 5分钟主循环
│   ├── position_manager.py         # 仓位管理
│   ├── risk_guard.py               # 风控闸
│   ├── rule_engine.py              # 规则引擎
│   └── state_machine.py            # 状态机
│
├── nautilus/                        # Nautilus集成层
│   ├── __init__.py
│   ├── node_manager.py             # TradingNode管理
│   ├── strategies/
│   │   ├── __init__.py
│   │   ├── base_strategy.py        # 基础策略类
│   │   ├── csp_strategy.py         # CSP策略
│   │   ├── cc_strategy.py          # CC策略
│   │   └── strangle_strategy.py    # Strangle策略
│   └── adapters/
│       └── ib_adapter.py           # IB适配器配置
│
├── gateway/                         # 网关层
│   ├── __init__.py
│   └── ib_gateway_manager.py       # IB Gateway管理
│
├── monitoring/                      # 监控层
│   ├── __init__.py
│   ├── metrics.py                  # 指标收集
│   ├── alerts.py                   # 报警系统
│   └── dashboards/                 # Grafana配置
│
├── config/                          # 配置
│   ├── __init__.py
│   ├── settings.py                 # 主配置
│   ├── symbols.yaml                # 标的配置
│   ├── tags.yaml                   # 标签规则
│   └── rules.yaml                  # 策略规则
│
├── tests/                           # 测试
│   ├── unit/
│   ├── integration/
│   └── fixtures/
│
└── docs/                            # 文档
    ├── architecture.md
    ├── deployment.md
    └── operations.md

12.2 关键技术选型
──────────────────────────────────────────────────────────────────────────────

  语言: Python 3.12+
  框架: Nautilus Trader (Rust核心 + Python API)
  数据库: TimescaleDB (PostgreSQL时序扩展)
  缓存: Redis
  调度: APScheduler
  配置: Pydantic Settings + YAML
  日志: structlog
  监控: Prometheus + Grafana
  通知: 微信企业号API / 钉钉Webhook
  测试: pytest + pytest-asyncio

12.3 环境变量 (.env)
──────────────────────────────────────────────────────────────────────────────

  IB_GATEWAY_HOST=localhost
  IB_GATEWAY_PORT=4001
  IB_ACCOUNT_ID=U1234567

  ITICKDB_HOST=localhost
  ITICKDB_PORT=5432
  ITICKDB_USER=athena
  ITICKDB_PASSWORD=xxx
  ITICKDB_NAME=athena_db

  REDIS_HOST=localhost
  REDIS_PORT=6379

  DSH_API_KEY=xxx
  DSH_API_URL=http://localhost:8000

  NOTIFIER_WECHAT_KEY=xxx
  NOTIFIER_DINGTALK_KEY=xxx

  LOG_LEVEL=INFO
  ENV=production


================================================================================
13. 风险与免责声明
================================================================================

  ⚠️ 本系统为自动化交易工具, 使用风险由使用者自行承担
  ⚠️ 期权交易具有高风险, 可能导致本金全部损失
  ⚠️ 系统故障、网络中断、API异常等情况可能导致意外损失
  ⚠️ 建议在模拟账户充分测试后再接入实盘
  ⚠️ 本策划书仅供技术开发参考, 不构成投资建议


================================================================================
                          策划书完
================================================================================
