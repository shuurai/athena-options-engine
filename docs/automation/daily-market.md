NeoData金融搜索服务
生成当日全球市场日报（美股/港股/A股/新闻/科技/战争/世界专题），记录到 project
memory 与 Excel，支持周、月分析和历史查询。

**⚠️
本任务的完整规范（数据源路由、全部步骤命令、判定规则、卦象流程、troubleshooting）以
runbook 为准：**

`/Users/fdi777/WorkBuddy/Financial/docs/daily_market_report_workflow.md`

执行前先读 runbook。步骤概要（细节以 runbook 为准）：

0. **Pre-flight 幽灵交易 guardrail（必须最先执行）**

   `/Users/fdi777/.workbuddy/binaries/python/envs/default/bin/python3 /Users/fdi777/WorkBuddy/Financial/preflight_phantom_check.py`

   退出码 1 = 检测到 phantom trades → **HALT**，创建 HIGH alert Notice（按
   docs/notice_[templates.md](http://templates.md) 的 Alert
   模板），等用户确认清理后才继续。退出码 2 = 无法验证 → 继续但在日报注明。

1. 运行 `build_daily_report.py`（yfinance 日报，生成
   reports/Global_Markets_Daily_Report_YYYY-MM-DD.xlsx）

2. NeoData 新闻缺失/过期时用 WebSearch
   补充当日新闻（禁止把旧日期内容当当日新闻）

3. 运行 `value_screener.py --no-neodata --topN 5`

4. 运行 `daily_runner.py --screener-only --no-neodata` 注入 screener

5. `fanit-cli sync all`

6. `fanit-cli calendar list --days 7`（财报/FOMC/期权到期）

7. `fanit-cli trade list --instrument-type option --status active`（DTE≤14、ITM、到期簇）

8. `fanit-cli execution list --status pending`（距市价 5% 内的 GTC）

9. CQQQ/[3032.HK/IGV](http://3032.HK/IGV) 目标 5% 检查（IGV:
   $83.52/$88.16/$78.88）

9b. Cyclic Opportunity Detection（规则见 runbook §Step 9b；未触发记录 CYCLIC=0）

9c. 市场卦象：每日恰一个 T1 market 卦；满足任一触发条件（持仓±5%/Cyclic/GTC
5%内/7天内财报期权/重大地缘冲击）时至多一个 T2 focus
卦。种子由日期+模板+主题决定，同问题不得改写 context
重摇。卦签中文、不作确定性预测、与策略冲突以策略文档为准。用 `--verdict --excel`
重跑 T1 生成 ☯ 市场卦象 sheet。

10. **Notice 创建：kind 只用 news/alert/signal（fanit-cli v1.7.0+ 用 --kind 不用
    --type）；signal 必须 --action + --ticker。Markdown body 模板与 CLI 命令见
    `/Users/fdi777/WorkBuddy/Financial/docs/notice_templates.md`（必须遵守 body
    硬要求：标题分节、表格、币种符号、Sources & Data Quality 结尾）。创建前对
    active Notice 去重。每条需用户关注的 Notice 创建后立即用
    `iching_market.py attach --id <id>` 挂卦并写中文卦签。**

11. `present_files` 交付日报 xlsx（含 ☯ 卦象 sheet）+ Value_Screener xlsx

12. 当日摘要追加到 `.workbuddy/memory/YYYY-MM-DD.md`（市场走势、组合指标、Notice
    记录、CYCLIC、卦象 T1/T2 各一行含种子、数据限制）

**纪律**：价格一律以 fanit-cli sync 后数据为准；用户声明优先于 CLI 数据；China
tech P1-P3 HOLD NO trims；HK 票先答整手数`$` 字面值用单引号包
body。




  
  
  
  
  




   
   
   
   
   
   

