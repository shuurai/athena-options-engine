Execute the MPF bi-weekly portfolio review for the Manulife Global Select MPF
aggressive strategy.

STRATEGY CONFIGURATION:

- Portfolio: HK$650,000

- Target allocation: Japan Equity 35%, Asia Pacific Equity 25%, China Value 20%,
  North American Equity 20%

- Thesis: Aggressive Asia AI/Hardware cycle (1-2 year horizon)

- Asia concentration: 80%

STEPS TO EXECUTE:

1. FETCH LATEST FUND DATA:

Use WebFetch to query MPFA fund pages for each fund and get latest
NAV/performance:

- Japan Equity:
  [https://mfp.mpfa.org.hk/tc/fund_details.html?cf_id=371](https://mfp.mpfa.org.hk/tc/fund_details.html?cf_id=371)

- Asia Pacific Equity:
  [https://mfp.mpfa.org.hk/tc/fund_details.html?cf_id=370](https://mfp.mpfa.org.hk/tc/fund_details.html?cf_id=370)

- China Value:
  [https://mfp.mpfa.org.hk/tc/fund_details.html?cf_id=375](https://mfp.mpfa.org.hk/tc/fund_details.html?cf_id=375)

- North American Equity:
  [https://mfp.mpfa.org.hk/tc/fund_details.html?cf_id=377](https://mfp.mpfa.org.hk/tc/fund_details.html?cf_id=377)

Extract: Fund name, NAV, 1-month return, 3-month return, 1-year return, YTD
return

2. CHECK MARKET CONDITIONS:

Use neodata-financial-search skill to check:

- Nikkei 225 level (Japan fund proxy)

- Hang Seng Index level (Asia Pacific/China proxy)

- NASDAQ level (North America proxy)

- USD/JPY rate (yen impact on Japan)

- Any major news affecting AI/semiconductor sector

3. EVALUATE DRIFT:

Compare each fund's actual weight vs target weight. If any fund has drifted >5%
from target (e.g., China was 20% but now 26%), flag for rebalancing.

4. CHECK REBALANCING TRIGGERS:

- China Value: CUT to 5% if 3-month drawdown > 25%

- Asia Pacific: Reduce to 15% if 1-year return > 60%

- North America: Reduce to 10% if NASDAQ drops > 15% in 1 month

- Japan: Reduce to 25% if Nikkei > 80,000

5. GENERATE REPORT:

Output a concise bi-weekly review in this format:

```
=== MPF Bi-Weekly Review: [DATE] ===

Portfolio: HK$650,000 | Strategy: Aggressive Asia AI/Hardware

FUND PERFORMANCE (Bi-Weekly):

| Fund | Target% | 1M% | 3M% | 1Y% | YTD% | Status |

|------|---------|-----|-----|-----|------|--------|

MARKET SNAPSHOT:

- Nikkei: [level] | HSI: [level] | NASDAQ: [level] | USD/JPY: [rate]

DRIFT ANALYSIS:

- Any fund >5% drift from target? [Yes/No]

- Rebalancing needed? [Yes/No]

REBALANCING ACTIONS:

[Specific actions if needed, or "No rebalancing this period"]

NEXT REVIEW: [Date + 2 weeks]
```

If any rebalancing trigger is hit, provide specific HK$ amounts to move between
funds.

IMPORTANT: Always output the report in the conversation. Do NOT write files
unless explicitly requested.
