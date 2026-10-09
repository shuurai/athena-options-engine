# 11 — Test tracking

Each development step has a check. **Pending means the event has not happened yet.** It does not stop the next step.

**Status values:** `passed` · `pending` · `deferred`  
**Last updated:** 2026-10-09

Build tasks stay in [10-implementation-tracker.md](10-implementation-tracker.md). This file only records proof.

## Account books

| Step | Check | Status | Proof |
|------|--------|--------|-------|
| Paper mode picks one account | `ATHENA_TRADING_MODE=paper` resolves `ATHENA_IB_PAPER_ACCOUNT_ID` | `passed` | Active book is `DUR253115` on Gateway port 4002. |
| Live mode picks a list | Comma-separated `ATHENA_IB_ACCOUNT_IDS` parses to one book per id | `passed` | A two-id parse returned both ids. Those rows were deleted. |
| Empty live list sends nothing | Live mode with a blank id list logs and skips orders | `passed` | Code path checked. Mode in `.env` stays `paper`. |
| One Approve does not fan out | An approval queues only that draft's account | `passed` | Queued row carried `DUR253115`. No second account row. |
| Switching mode hides the other book | Paper and live rows stay in the database | `passed` | Stamp of unlabeled rows did not delete them. |
| A real live-account order | Approve on an id from `ATHENA_IB_ACCOUNT_IDS` | `pending` | Live id list is empty. Mode stays `paper`. |

## News and phases

| Step | Check | Status | Proof |
|------|--------|--------|-------|
| Yahoo and Finviz intake | Headlines land in `news_events` | `passed` | 50 Yahoo and 42 Finviz rows stored. |
| NEWS_WASH | Three or more negative headlines in 3 days | `passed` | NVDA and T tagged `NEWS_WASH`. |
| Fed calendar | FOMC dates stored | `passed` | 54 dates stored. None inside 7 days, so no `FED_EVENT`. |
| Earnings window | Watchlist names on the Nasdaq calendar | `passed` | No watchlist name inside 7 days. |
| Phase walk | P1 HOLD through P5 OPEN_CC on one symbol | `passed` | Fixture symbol walked each pair, then the rows were deleted. |
| DSH sentiment | Model score replaces the headline heuristic | `deferred` | Engine runs without DSH. |

## Slots, quotes, and stocks

| Step | Check | Status | Proof |
|------|--------|--------|-------|
| Weekday clock | Mon–Fri windows, weekend idle | `passed` | Friday 10:00 and 13:00 are Athena. 12:30 and Saturday 13:00 are idle. |
| HK afternoon session | Process connects inside 13:00–16:00 HKT | `passed` | 13:03 HKT logged `ib_session=connected` for `DUR253115`. |
| Delayed HK quote | Digit symbols use SEHK and delayed data | `passed` | 700 close 419.2. 1810 close 25.36. Source `ib-delayed`. |
| HK stock order, then cancel | Far limit is accepted and leaves no open trade | `passed` | Paper BUY 100 of 700 at HK$1, and BUY 200 of 1810 at HK$1, then both cancelled. |
| A live HK stock fill | An order that trades and writes a fill | `pending` | Probes were far limits on purpose. |

## Option strangle

| Step | Check | Status | Proof |
|------|--------|--------|-------|
| Expiry and strike picker | Nearest expiry at least 21 DTE, put 10% below, call 10% above | `passed` | Unit case returned 2026-11-27, put 370, call 470. |
| Tencent contract | Multiplier from the chain, not a fixed 100 | `passed` | `TCH NOV26 370 P`, multiplier 100, qualified. |
| Xiaomi contract | A different multiplier still qualifies | `passed` | `MIU NOV26 22 P`, multiplier 1000, qualified. |
| Far option sell, then cancel | Paper SELL rests, then the book is empty | `passed` | Tencent put at 20 cancelled. Xiaomi put at 50 cancelled. `openTrades` empty. |
| Plan stored | Expiry, strikes, limits, class, multiplier | `passed` | 700: 370/470, limits 3.37 and 4.49, `TCH` × 100. 1810: 22/28, limits 1.00 and 0.82, `MIU` × 1000. |
| No contract-less strangle draft | `OPEN_STRANGLE` without both limits does not insert a draft | `passed` | See the IV section proof. |
| Approve sends both option legs | An approved priced strangle becomes two SELL orders | `pending` | 700 and 1810 are still P1 HOLD, so no draft was sent. |
| An option fill | A short option position, then roll or profit-take | `pending` | No option position is open. |
| US option plan | NVDA, QCOM, or T gets a priced plan | `passed` | NVDA 13 Nov 2026, 205 put and 255 call. Stock close 230.48 from daily history. Put print 1.71 from CBOE. Paper SELL at 20 cancelled. No open trade. |

## IV rank

| Step | Check | Status | Proof |
|------|--------|--------|-------|
| Rank formula | None before 20 samples. 80 is HIGH_IV. 20 is LOW_IV | `passed` | Unit check on `iv_rank` and `iv_tags`. |
| Daily mark | One premium-ratio row per symbol per day | `passed` | Written for names that already have a priced plan. |
| HIGH_IV or LOW_IV on a live name | Tag appears only after 20 daily marks | `pending` | One day is stored. The tag stays off until the window fills. |
| Priced strangle draft | NEWS_WASH + HIGH_IV + a plan inserts an OPT draft | `passed` | Fixture only. The draft was deleted. No Telegram message. |

## Price levels, CSP, and covered calls

| Step | Check | Status | Proof |
|------|--------|--------|-------|
| RSI and touch tags | A rising series is overbought at the high. A falling series is oversold at the low. A short series stays untagged | `passed` | Unit check on `price_tags`. |
| Daily bars | IB daily history is stored as `ib-daily` | `passed` | 60 bars each for NVDA, QCOM, T, 700, and 1810. None is overbought, oversold, or within 1% of the 20-day high or low. |
| CSP draft has a put | P4 + OVERSOLD + SUPPORT_TOUCH drafts one SELL put, or nothing when the put has no price | `passed` | Fixture `ZZCSP` queued a 155 put, then the rows were rolled back. No Telegram message. |
| Covered-call draft has a call | P5 + OVERBOUGHT + long shares drafts one SELL call | `pending` | The account has no long shares, so the live path stays HOLD. |
| A live CSP or covered-call draft | The phase machine itself chooses the tactic | `pending` | Names are still P1 or P2. Tags alone do not open those phases. |

## Mock progress

Live IV rank and price tags are not extreme yet. `mock_signals` stands in for one symbol so the phase machine and the HK order path can move. `send_order` queues one far strangle, then turns itself off.

| Step | Check | Status | Proof |
|------|--------|--------|-------|
| Mock tags move 700 | `NEWS_WASH` + `HIGH_IV` sets P2 OPEN_STRANGLE | `passed` | Phase is P2 `OPEN_STRANGLE` with those tags. `send_order` is off, so it will not queue another. |
| Far HK strangle rests | Two paper sells are accepted and left working | `passed` | IB 70 sells the 380 put at 20. IB 72 sells the 470 call at 25.5. Both Submitted. No fill. |
| Working orders stay in the book | Each cycle reads IB status for submitted orders | `passed` | Both Tencent orders logged `order resting` as Submitted. They were not cancelled. |
| Mock steps to event day | `event_offset` 0 moves 700 to P3 HOLD | `passed` | Phase is P3 HOLD. No new draft. |
| Close or roll uses the option | A short option with 10 DTE drafts a BUY of that put | `passed` | Fixture `ZZMAN` only. The draft and the queued order were removed. |
| Repair cancels unfilled sells | P4 CLOSE drafts a cancel while the sells are still working | `passed` | `700` is P4 CLOSE. A pending cancel draft was sent. IB 70 and IB 72 stay Submitted until that draft is approved. |
| Phase follows the event window | P1 is T−7 to T−3, P2 is T−3 to T−1, P3 is event day, P4 is T+1 to T+3, P5 is T+5 to T+10 | `passed` | Unit check on those offsets. No watchlist name has a Fed or earnings date inside the window, so the live phase is P1. The Tencent mock is off. |

## Control and settings

| Step | Check | Status | Proof |
|------|--------|--------|-------|
| One-time approve page | Second POST is 410 | `passed` | Token path records one decision. |
| Mobile layout | Full-width buttons, system font, viewport | `passed` | Headless screenshots at 390×844 and 1280×800. |
| Settings audit | A knob writes a version and a diff | `passed` | `roll_dte` 21 → 14 → 21. Privilege change refused. |
| Draft TTL | A pending draft older than the setting expires | `passed` | Fixture `ZZTTL`: a 2-hour pending draft became `expired`. A new pending draft and an old approved draft stayed. Rows deleted. Default TTL is 60 minutes. |
| Halt is per account | One account halted does not block the other | `passed` | Fixture accounts. Rows deleted. |

## Still deferred

Tailscale on the Windows box, Feishu, DSH, fanit export, privilege `opens_only`, Tier B and C news, Nautilus, and a cloud trading host stay deferred. They are not pending checks for the current paper book.
