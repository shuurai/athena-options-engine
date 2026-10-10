# 12 · Positions

IBKR book for the active account. One row per open contract. Stocks and options sit in the same table.

## Fixtures

These numbers are a design sample, not the paper account.

- NVDA long 100 shares, USD
- NVDA short 1 put, 200 strike, 2026-10-16, multiplier 100
- 700 short 1 call, 420 strike, 2026-10-29, HKD, multiplier 100
- Account id is masked

## Live page

`/positions` reads the latest stored snapshot. Sync from IB uses the read-only client and does not send an order. A contract missing from that snapshot is stored as flat and drops off the table. Empty marks stay a dash.
