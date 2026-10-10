# 13 · AI audit

Review trail for one signaled-entry cycle. This page does not approve orders.

## Features

- One NVDA cycle: Location, News, CSP, Call, Purchase, Strangle, Consolidator
- Columns: time, agent, symbol, latency, estimated tokens, result
- Keyword filter and agent filter
- A row opens meta (model, provider, thinking, latency, tokens, status) and the full JSON response
- A failed News row shows `TimeoutError` and tokens 0. The token secret is never shown

## Fixtures

- Location tags: `CHANNEL_RISING`, `LOWER_RAIL`
- Consolidator thinking on, drafts `draft_a1` (share purchase) and `draft_b2` (cash-secured put)
