# 13 — Portfolio advisor

**Status:** Implemented 2026-10-16. Off until Settings → Advisor is on.  
**Decisions:** [../plan/13-portfolio-advisor.md](../plan/13-portfolio-advisor.md) (D51–D57).

The advisor answers one question for each held name: is this a good time to add, or should the book take profit. It writes a signal. Manage may turn that signal into a pending draft. Approve stays human. The advisor does not call the Consolidator and does not change a ticket’s legality.

## Cycle

When trading is on for the account, one cycle is:

1. Evaluate the watchlist (openings stay with the wheel).
2. If `advisor.enabled`, `run_advisor` writes one `advisor_signals` row per held symbol for that day.
3. `manage_book` reads those rows and may stage CLOSE or TRIM drafts.
4. Signaled entry runs as before.
5. The cycle budget still walks the drafts. A draft stays `pending` until Approve.

Replay uses the same order on the cursor day.

## What it reads

| Input | Source | Used for |
|-------|--------|----------|
| Last price and prior close | Universe IB print (`quote_pair`: newest `ib-delayed` close, otherwise the daily close, and the prior session close) | Guidance price and day change |
| Held spot | `last_tradeable_price` | The position fact pack. No print means the symbol is `hold` / `no spot` |
| 52-week high | Last 252 daily bars | “Near high” when spot is within 2% of that high |
| Forecast | `forecast_marks` for the account and day | Average `dir_conf` across horizons. Missing forecast does not invent a number |
| Headlines | Newest `news_events` row with `source = yahoo` for that symbol. If none is stored, one Yahoo RSS title | Text IB does not have. Not a price |
| FOMC | `calendar_marks` kind `FED`, within 14 days | Guidance note |
| Earnings | `calendar_marks` kind `EARNINGS` for the guidance names and for the held name, within 14 days | Guidance note and the position facts |
| News tags | `NEWS_WASH`, `FED_EVENT`, `EARNINGS_NEAR` | Passed to the model as events |

A missing IB print is skipped. A missing headline is skipped. Yahoo is not asked for a last price.

Guidance names default to `SPY`, `QQQ`, `IWM`, `EEM`, `SMH`, `NVDA` (D110, 2026-11-24). Settings can change the list. All six are risk-on proxies: `dir_conf` is calibrated P(r>0) (`prompts.py`), so a hedge ETF rising would be mis-averaged as a risk-on `strong` signal. A name that is not on the universe has no IB print until the universe update stores one, and `forecast_marks` are produced only for names in `settings.watchlist`.

Basket tone from the average `dir_conf`:

| Average | Label |
|---------|--------|
| none | `unknown` |
| below 0.45 | `weak` |
| below 0.60 | `neutral` |
| otherwise | `strong` |

## Verdict

`AdvisorExplainer` returns JSON only:

```json
{"action":"hold","confidence":0.0,"reason":""}
```

`action` is `hold`, `take_profit`, `trim`, or `close`. Anything else becomes `hold`. Confidence is capped at `max_conf` (default 0.95). The model does not place orders.

If the model does not answer, a fixed score is used:

| Fact | Points |
|------|--------|
| Basket label `weak` | 0.35 |
| This name’s `dir_conf` below 0.45 | 0.30 |
| Spot within 2% of the 52-week high | 0.20 |
| A short option has captured at least 40% of premium | 0.15 |

Score at or above close confidence becomes `take_profit`. Score at or above trim confidence becomes `trim`. Otherwise `hold`.

An acting signal:

| Action | Acts when confidence is |
|--------|-------------------------|
| `close` | at or above close confidence (default 0.65) |
| `take_profit` or `trim` | at or above trim confidence (default 0.55) |
| `hold` | never |

These two thresholds are the only weight. There is no separate risk-weight knob.

## Drafts

Phase is `ADVISOR`. The option contract cap is not applied. One pending or approved draft is allowed per symbol, tactic, and instrument, so a stock close and an option close on the same name can both exist.

| Book | `take_profit` or `close` | `trim` |
|------|--------------------------|--------|
| Short option | CLOSE the full contract count, even when captured premium is below `profit_take_pct` | TRIM half the contracts, at least 1 |
| Long stock | CLOSE the full share count | TRIM half the shares, at least 1 |

A short option needs a stored option plan and a positive mark. The order action is BUY. A stock draft sells at the IB print. The reason starts with `Advisor:` and includes the fact pack.

`profit_take_relief_pct` is stored and shown on Settings. It does not change the draft. The acting signal stages the draft directly. The normal profit-take gate still closes a short option on its own when captured premium is high enough and the advisor did not already stage that CLOSE.

## Settings

The Advisor card is in the middle column of `/settings`, after Market data. Notify is in the third column. The settings page scrolls as one screen. It is off until Enabled is on.

| Key | Default | Role |
|-----|---------|------|
| `advisor.enabled` | false | The cycle writes signals and may stage drafts |
| `advisor.guidance_symbols` | SPY, QQQ, IWM, EEM, SMH, NVDA | Comma-separated risk-on tickers |
| `advisor.close_conf_threshold` | 0.65 | Floor for `close`. Must sit between trim and max |
| `advisor.trim_conf_threshold` | 0.55 | Floor for `trim` and `take_profit` |
| `advisor.max_conf` | 0.95 | Cap on stored confidence |
| `advisor.profit_take_relief_pct` | 15 | Stored only. Not applied |

Trim confidence must be at or below close confidence, and close confidence at or below max confidence. Each confidence is from 0 to 1. Relief is a whole number from 0 to 80.

## Pages

`/iris` sits under Core logic; `/portfolio-analysis` is a backward-compatible alias. The P1 page is a read-only architecture graph with a right-side action rail that reuses existing `/drafts`. Advisor report content is Strategy 2 evidence, not a second action list; the former two-column Advisor actions table is retired from the page.

The report, when the advisor is on, shows RISK OFF, CAUTION, or RISK ON from the highest non-hold confidence, the guidance list, and a Semiconductor/AI versus Other count. Off, it says to turn the advisor on under Settings → Advisor. Pending draft ids still link to `/drafts`; legacy drafts show `insufficient_provenance` because no strategy / factor / run identity is persisted.

`/orchestration` keeps the signaled-entry chain. Its prompt cards use two independent columns with Trade in the right column, avoiding shared row-track gaps. A side path is Guidance → Advisor → Manage, joining Approve. `Narrator`, `ManageExplainer`, and `AdvisorExplainer` are editable prompt cards on that page. The standing text lives in `packages/settings`.

## Store

`advisor_signals` primary key is `(account_id, symbol, day)`. Columns: `action`, `confidence`, `reason`, `payload`, `created_at`. A later pass the same day replaces the row.
