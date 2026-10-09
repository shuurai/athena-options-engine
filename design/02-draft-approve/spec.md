# 02 · Draft approve queue

Default privilege `all_orders`: every open/roll/close sits here until Approve.

## Fixture draft A

- `draft_7f3a` · OPEN_STRANGLE · NVDA
- Phase P2 · tags NEWS_WASH + HIGH_IV + FED_EVENT
- Put 210 / Call 235 · Oct 17 2026 · qty 1 · limit mid−0.15
- Strength 8 · confidence 0.84
- Risk checks: all pass except warn on single-name 18% of sleeve after margin

## Fixture draft B

- `draft_9c12` · ROLL · QCOM put 165 → 162.5 out 21 DTE
- Reason: 50% max profit on short put
