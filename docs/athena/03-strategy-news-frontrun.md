# 03 — Strategy: News Front-Running Cycle

## One strategy

Athena runs **exactly one** strategy product: the **News Front-Running Cycle (NFR)**.

CSP, CC, Strangle, Hold, Roll, and Close are **tactics**. Phases P1–P5 stay as news-cycle context. They do not pick an open.

Code picks the open. `wheel.decide_opening` is the only opening decision. It reads the book, the indicator numbers, and the symbol flags, then writes at most one ticket. Location and News may add tags. Narrator may explain the ticket or swap one listed strike. A strike that is not on the menu drops the leg and the audit says `narrator_overruled`. Nothing is sent until Approve.

The wheel is the only opening path. `ATHENA_STATE_MACHINE` cannot turn it off.

Refine this cycle for 5–10 years. Improvisation = Settings versions + rule comments, not new strategy packages.

## Thesis (plain language)

Around known macro/event windows (Fed, major regulation, earnings clusters), markets often see:

1. Early negative narrative / “wash” pressure  
2. Realized volatility rising with the selloff  
3. Event print  
4. Repair after the event  
5. Narrative return / chase  

Athena’s job: **detect phase**, show a short strike menu with numbers, draft size within risk, and wait for your Approve (default). A phase label does not by itself create a draft.

## Phase machine

| Phase | Window (typical) | Detection (inputs) | Typical intent |
|-------|------------------|--------------------|----------------|
| **P1 Warning** | T−7 … T−3 | Rising negative news density; calendar event approaching | Watchlist; Hold |
| **P2 Panic** | T−3 … T−1 | Price weak, volume spike, NEWS_WASH active | Consider **Strangle** |
| **P3 Event** | T day | Calendar match / official release | Hold; no new aggression |
| **P4 Repair** | T+1 … T+3 | Price stabilizes after the event | Close or reduce; CSP or CC by location |
| **P5 Euphoria** | T+5 … T+10 | Price into the top of the channel | Prefer **CC** only when the shares cover it |

Windows are **Settings parameters**, not hard-coded forever. A phase name does not choose the ticket. The wheel does.

```mermaid
stateDiagram-v2
  [*] --> P1_Warning
  P1_Warning --> P2_Panic: newsDensity
  P2_Panic --> P3_Event: eventDay
  P3_Event --> P4_Repair: priceStabilizes
  P4_Repair --> P5_Euphoria: narrativeReturn
  P5_Euphoria --> P1_Warning: nextCycleOrIdle
  P2_Panic --> P1_Warning: falseAlarm
  P4_Repair --> HoldIdle: cycleDone
  HoldIdle --> P1_Warning: newEventCluster
```

## Price

Rails, RSI, realized volatility, and the indicator tags read **one bar per calendar day**.

- Sources in that window are `ib-daily` and `ib-delayed` only. `seed` and `pool-sync` are gone. An `itick` print, if one is stored, is not a daily bar.
- A later `ib-delayed` print on the same day updates that day’s **close**. The high and the low stay on the `ib-daily` bar.
- `last_tradeable_price` is the newest delayed print, otherwise the daily close. It is the draft reference price. It is not an input to a rail or a volatility rank.
- A name with fewer than 252 `ib-daily` sessions is `history_short`. Its range tags are `HIGH_RANGE` and `LOW_RANGE`. The 52-week label is used only after 252 sessions exist.

## Tags (drivers, not strategies)

Tags are a registry. An unknown tag is logged and kept on the audit row. Indicator numbers are stored on `symbol_phases.indicators` and shown beside the tags. The model sees the numbers. It does not see an implied-vol figure.

| Tag or number | Meaning |
|---------------|---------|
| `RV_HIGH` / `RV_LOW` | `RV20` at or above the 80th percentile of its own history, or at or below the 20th. Needs 20 samples of method `close_log_252`. This is the volatility gate. |
| `RV20` | 20-day close log-return, annualized. A level, not a rank. |
| `rail_pos` | Where the last close sits in the 63-session high–low channel, from 0 to 1. |
| `TREND_STRONG` / `TREND_WEAK` | Both channel edges slope the same way versus 21 sessions ago, past the slope minimum. |
| `DRAWDOWN_OK`, `drawdown_pct` | Close is between 5% and 15% under the 63-session high. |
| `DIP_BOUGHT` | The channel was at or below 0.35 inside the last 5 sessions, has reclaimed 0.45, and the trend is strong. |
| `RECLAIM` | Close is above the recent average, or above the prior bar’s high. |
| `VOLUME_DRY` / `VOLUME_SPIKE` | Latest volume versus the prior 20 sessions. |
| `ATR`, `ATR_WIDE` | 14-session average true range. Wide means ATR is at least 4% of price. |
| `EARNINGS_NEAR` | Inside the earnings blackout. |
| `NEWS_WASH` | Elevated negative news density. |
| `REGIME_RISK_OFF` | `SPY` close is under its own 63-session midpoint. No `SPY` bars records `regime_unknown` and does not stop the name. |
| `HIGH_RANGE` / `LOW_RANGE` | Range tags when history is shorter than 252 sessions. |

`HIGH_IV` and `LOW_IV` stay as aliases so old rows can still be grouped. They are not produced, and they are not a reason to open. `estimated_iv()` returns nothing. There is no `IV_RICH` or `IV_CHEAP`.

Each live Athena slot appends one at-the-money call mid and put mid to `chain_marks`. Nothing reads that table yet. A replay does not write it.

## Strike menu

Code ranks the chain. The model sees at most three puts and three calls, each with DTE, distance, delta, annualized yield, and breakeven. A missing number is written `unknown`. The model may name a row in that list. A strike that is not in the list is skipped.

- Discovery uses `chain_band_pct` (default 25). That is the width fetched around the price, up to eight strikes on each side. It is not a cut that throws away everything inside 10%.
- Selection uses a delta target. Cash-secured put target 0.25, band 0.10–0.40. Covered call target 0.30, and the strike must be at least the share cost (`avg_price`). Strangle target 0.28.
- Delta is Black–Scholes with interest rate 0. The volatility input is `RV20` times a multiple: 1.05 when `RV20` is at least 45%, 1.35 from 25% up to 45%, 1.80 below 25%. That multiple is an assumption used to pick a strike. It is not implied volatility, and it does not create a rich or cheap tag.
- The distance target is half of that volatility times the square root of DTE over 365, then clamped between 3% and 20%. A clamp is written on the audit row.
- Annualized yield is ask divided by strike, times 365 over DTE. A covered call also shows yield on the share cost when a cost exists.
- Each expiry inside the DTE band can carry asks. The menu walks expiries by distance from the target DTE.
- Bid, open interest, volume, and a vendor IV column exist on `option_choices` and stay empty until the broker sends them. Liquidity on the menu is `unknown`.
- `ATHENA_SCREENER=off` restores the old free-text lines.

## Wheel

The state is recomputed from shares and short option counts. `symbol_phases.tactic` stays `HOLD`. `state` and `leg_counts` are a snapshot of that count, not a second source.

| Alias | Book |
|-------|------|
| `FLAT` | No shares, no short option |
| `ODD_LOT` | 1–99 shares, no short option |
| `LONG` | At least 100 shares, no short option |
| `SHORT_PUT` | One short put, no shares |
| `ODD_PUT` | 1–99 shares and one short put |
| `ASSIGNED` | At least 100 shares and one short put |
| `LONG_CALL` | At least 100 shares and one short call |
| `WHEELING` | At least 100 shares, one short put, and one short call |
| `STRANGLE` | No shares, one short put, and one short call |
| `NAKED_CALL` | A short call the shares do not cover |
| `MULTI_CONFLICT` | More than one short put or more than one short call, or any other mix |

`NAKED_CALL` and `MULTI_CONFLICT` write `dropped` and open nothing. Athena does not flatten them. A close still comes only from the roll or profit-take rule.

One name gets at most one opening ticket per day. The legs are ordered. A signaled strangle is put, then call. There is no tactic named `OPEN_STRANGLE`.

| Reason | When | Legs |
|--------|------|------|
| `signaled_strangle` | `NEWS_WASH` and `RV_HIGH`, the strangle flag is on, and at least 100 shares. Both menu sides exist. This wins over a covered call and a dip. | `OPEN_CSP`, then `OPEN_CC` |
| `high_cc` | Close is at or above `cc_rail_pos` (0.80), the call flag is on, and the shares cover the contracts. Strike is at least `avg_price`. This wins over a dip when both are true. | `OPEN_CC` |
| `cover_gap` | The close is in that high band, the call flag is on, and the shares are an odd lot. The order buys `100 − shares`. It does not attach a call. The contract cap does not apply to this stock order. | `BUY` |
| `dip_long` | A dip, and this name may buy shares. If `RV_HIGH` and a put is listed, the put is the second leg. Share size is `open_quantity`. | `BUY`, or `BUY` then `OPEN_CSP` |
| `dip_csp` | A dip, the put flag is on, and this name may not buy shares. `RV_HIGH` is not required. | `OPEN_CSP` |

A dip needs `TREND_STRONG`, `RECLAIM`, and a drawdown between the 5% and 15% band. Earnings nearby blocks a dip and a new call.

Share purchases follow `may_buy` when that flag is set. When it is empty, a buy is allowed only if Long is on and the intent is not Income. Income can still open `dip_csp`.

### Cover and conflict

These run before a draft can be pending. They use the same account and the same simulated book as the draft.

- A covered call is blocked unless the contracts fit in `floor(shares / 100)` after short calls already held or submitted. The reason names the shares required and the shares held. One share cannot cover a call.
- A second short put on the same name is blocked.
- A strangle is blocked when that name already has an option leg.
- A short put and a covered call may both exist. That is the wheel. `strict_conflict` on restores the old rule that any short blocks every new open. Default is off.
- After each leg is legal, the ticket is checked as one cash need: share buys at their limits, plus each short put’s strike times the multiplier. If that need breaks `cash_reserve_pct`, every leg on the ticket is blocked. Missing cash or NAV marks skip that check. They are not invented. New short margin uses `margin_rate` (default 20%).

## Position management

Management is one ticket per name, at most two legs, reason `manage_roll` or `manage_close`.

- DTE at or under `roll_dte` drafts a roll.
- Premium captured at or above `profit_take_pct` drafts a close.

Both stay pending until Approve. The cycle drafts that ticket before a new open. A name with a pending roll or close does not also receive an opening ticket in that pass. A fill Athena did not draft is stored as external and reconciled on the next sync.

## Cycle budget

After the drafts for one pass exist, `ATHENA_PORTFOLIO_BUDGET` (default on) walks them once. The order is closes and rolls, then `dip_long`, then `high_cc`, then every other reason. A priority band is kept or blocked as a whole, so symbol order does not pick a winner.

The walk checks cash reserve, estimated short margin (`margin_rate`), the daily order cap, `max_bucket_pct_nav` (35), assignment exposure (`max_assign_pct_nav` 40, using a delta proxy and `assign_delta` 0.30), and cash left after that assignment (`min_cash_after_assign_pct` 10). A new short put’s strike is counted once in that cash-after-assignment test. A block reason is written on every leg of the ticket.

## Book

`/book` is read only. It plots `RV20` against premium yield. It does not estimate a return and it does not draw a market line. The position map groups open stock and option rows by intent. A name with no flag is Income. Tile size is absolute market value. Color is open profit or loss, grey when the change is under 0.05%, capped at ±25%.

## Universe / watchlist

Each name has intent `income`, `both`, or `accumulate`, plus the CSP, CC, and strangle flags. Intent is also the outer group on the book map. A deny-list symbol produces no draft.

## Parameterization (owned by Settings)

Examples (full list in [04-settings-and-risk.md](04-settings-and-risk.md)):

- Phase window days relative to event T  
- News density thresholds for NEWS_WASH  
- `RV20` window and the high / low percentiles  
- Chain band, delta targets, and the 3%–20% distance clamp  
- The three RV multiples, labeled as an assumption  
- DTE band, profit-take %, roll DTE  
- Max contracts per name, cash reserve, bucket cap, and assignment cap
- Observe window and draft window
- Per-name intent and `may_buy`  

## Improvisation workflow

1. Comment on a draft via bot (“widen put strike”, “skip this Fed”)  
2. Or open control link → amend fields → Approve  
3. Or change Settings version (strategy params) with audit note  
4. Athena does **not** require a new strategy package for tweaks  

## What we deliberately do not build

- Parallel “CSP strategy product” vs “CC strategy product” codebases
- A model that can approve its own open, or veto a ticket the wheel already refused
- Implied-vol tags. `chain_marks` is collected. `estimated_iv()` stays empty until a later consumption step. The RV multiple is not called IV
- A portfolio optimizer. No mean-variance, Sharpe, risk parity, or covariance matrix
- Auto-trading every headline without the wheel and Approve
- Weeklies-only scalping as core (optional later param, not MVP focus)  
