# 11 — Test tracking

Each development step has a check. **Pending means the event has not happened yet.** It does not stop the next step.

**Status values:** `passed` · `pending` · `deferred`  
**Last updated:** 2026-10-11

Rows below are the proof recorded on that day. The live rules for the wheel, price, cover, audit outcomes, realized-vol tags, the strike menu, the cycle budget, and the slot clock are in [03-strategy-news-frontrun.md](03-strategy-news-frontrun.md) and [06-data-and-ib.md](06-data-and-ib.md). A row that still says `HIGH_IV`, `source=seed`, or a market line on `/book` is history, not the current rule.

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
| DSH sentiment | Model score replaces the headline heuristic | `passed` | Two stored headlines were rescored. NVDA keyword −0.6 became −0.2. Xiaomi keyword 0.4 became 0.8. The browser app on port 3080 was not called. |

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
| An option fill | A short option position, then roll or profit-take | `pending` | No option position is open. A fill would send a Telegram note with the price. |
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
| Repair cancels unfilled sells | P4 CLOSE drafts a cancel while the sells are still working | `passed` | `700` is P4 CLOSE. The cancel draft was approved. IB 70 and IB 72 are cancelled. |
| Phase follows the event window | P1 is T−7 to T−3, P2 is T−3 to T−1, P3 is event day, P4 is T+1 to T+3, P5 is T+5 to T+10 | `passed` | Unit check on those offsets. No watchlist name has a Fed or earnings date inside the window, so the live phase is P1. The Tencent mock is off. |

## Control and settings

| Step | Check | Status | Proof |
|------|--------|--------|-------|
| One-time approve page | Second POST is 410 | `passed` | Token path records one decision. |
| Mobile layout | Full-width buttons, system font, viewport | `passed` | Headless screenshots at 390×844 and 1280×800. |
| Settings audit | A knob writes a version and a diff | `passed` | `roll_dte` 21 → 14 → 21. Privilege change refused. |
| Draft TTL | A pending draft older than the setting expires | `passed` | Fixture `ZZTTL`: a 2-hour pending draft became `expired`. A new pending draft and an old approved draft stayed. Rows deleted. Default TTL is 60 minutes. |
| Fill notice | A new execution tells Telegram the price | `passed` | Fixture `ZZFILL` returned `SELL ZZFILL OPEN_CSP` at 1.25. A status-only Filled notice has no price. Rows deleted. No chat message, because nothing live has filled. |
| Slots and backup commands | `/slots` shows the windows. `/backup now` writes a snapshot | `passed` | Paper account is in the Athena slot. Windows are the weekday HKT hours. Backup `athena-20261009T065323Z.json` synced. `/status` reports no open position. |
| Draft card shows the contract | Strike, expiry, and limit are on the card and the page | `passed` | Fixture `draft_zzcard` listed Tencent put 380 at 3.5 and call 470 at 5.1. The page HTML included those lines. The fixture was deleted. |
| Amend on the approve page | Quantity and limits can change. Tactic stays | `passed` | Fixture `draft_zzamd` queued 2 contracts at put 4.5 and call 6.5. The put strike stayed 380. Nine contracts raised an error. Rows deleted. |
| Listed strike and expiry | Approve can pick another listed expiry and strike | `passed` | Band nearest 35 days skipped a 7-day expiry. Fixture `draft_zzlist` moved to 2026-12-30 put 360 call 490. Put 300 was refused. Rows deleted. |
| Reject reason | `/reject <id> [reason]` stores the words | `passed` | Fixture `draft_zzrej` stored `too wide`. An empty reason still rejected `draft_zzrej2`. No order was queued. Rows deleted. |
| Draft list buttons | `/drafts` shows Approve and Reject on each pending draft | `passed` | Fixtures `draft_zzbtn` and `draft_zzrisk` each had Approve and Reject. Rows deleted. No chat message. |
| Risk line | The draft card states pass or fail | `passed` | `draft_zzbtn` said Risk pass. `draft_zzrisk` said Risk fail because nine contracts exceed the max. Rows deleted. |
| Price weak and volume spike | Those tags can move a name toward P2 | `passed` | A 20-day decline plus double volume tagged `PRICE_WEAK` and `VOLUME_SPIKE` and chose P2. The strangle still needs news wash and high IV. |
| Halt is per account | One account halted does not block the other | `passed` | Fixture accounts. Rows deleted. |

## Console web

The phone approve page is already proven. These checks are the operator console. None of them block the engine rows above.

| Step | Check | Status | Proof |
|------|--------|--------|-------|
| Console shell | Nav reaches each console page. Connect is absent | `passed` | All nine pages returned 200 on port 8788. Connect is not a nav item. Port 8787 returned 404 for `/risk`. |
| Cockpit | Mode, account, slot, drafts, and watchlist phase are the live book | `passed` | Account `DUR253115`. Latest NAV says no mark is stored. NVDA is on the watchlist. |
| Draft queue | Approve and Reject on a pending draft, including a reject reason | `passed` | Fixture `draft_zzcon` was rejected with `console check`. The row was deleted. |
| Phase board | Watchlist phase, tactic, and tags match `symbol_phases` | `passed` | The NFR board listed the stored phase rows. |
| News page | Watchlist headlines show the stored sentiment | `passed` | The news page listed stored headlines and scores. |
| Settings and risk | A profile fills limits. Privilege stays unchanged | `passed` | Defensive was applied, then Standard was restored. Privilege stayed `all_orders`. Max contracts is 3. |
| Accounts page | Active book is shown. The other book stays in the database | `passed` | The page shows the paper account and an empty live list. It does not switch mode. |
| History page | Fills and approvals for the active account | `passed` | The history page listed stored fills and approvals. |
| Universe page | Watchlist matches settings | `passed` | The universe page listed the settings watchlist. |
| Vercel host | Public console calls the box | `deferred` | Waits on Tailscale. |

## Console fidelity

CW-02 stays the first data shell. These checks are the design cockpit. A missing series must show a dash.

| Step | Check | Status | Proof |
|------|--------|--------|-------|
| Design shell | Nav, chips, cards, and the Strangle mark match the cockpit layout | `passed` | Nine pages returned 200 with the design shell and the Strangle mark. Connect is absent. Port 8787 `/risk` is 404. |
| Cockpit stats and charts | NLV, premium, cash reserve, and drafts come from stored rows | `passed` | NLV and premium show a dash. The fixture amount is absent. |
| Today's schedule | Slot windows and a saved settings version | `passed` | Mobile 19:00–21:30 is Reserved. Settings version 11 restored the original windows. Privilege stayed `all_orders`. |
| Health and ops | Five checks refresh. Backup, halt, resume, and restarts use the ops path | `passed` | Health is 4/5 because iTick is not configured. Backup synced a snapshot. Gateway restart reported an empty command. `ZZHALT` was halted, resumed, and deleted. |
| Watchlist pulse | Last, IV rank, phase, side, strike, DTE, and draft status | `passed` | NVDA is last 218.42, phase P1, IV rank dash, side FLAT. |
| Draft cards | Approve, amend, reject, control link, and recent decisions | `passed` | Fixture `draft_zzpage` amended to 2 contracts, then rejected with a reason. The card and the reason rendered. Rows deleted. |
| NFR board | Phase rail, IV series, phase counts, and the symbol table | `passed` | Port 8788 shows five phases, the NVDA series, rank dash, and P1 counts. |
| News intake | Stats, charts, manual ingest, and false-alarm exclusion | `passed` | ZZPAGE sentiment −0.6 counted once, then 0 after dismiss. Row deleted. |
| Universe pool | Add, remove, deny, and tactic toggles | `passed` | NVDA Strangle off blocked `propose`. Settings version 13 restored the original book. Privilege stayed `all_orders`. |
| History filters | Filtered rows, zero summary on an empty book, and CSV | `passed` | Premium and monthly net are 0. CSV starts with `when,kind`. |
| Account cards | Cards and a read-only view of the other book | `passed` | The active card has no switch. `/accounts?book=` opens drafts, positions, and halt read-only. |
| Risk editor | Editable limits, version history, and a privilege that does not change | `passed` | Limits are editable. `opens_only`, `auto_within_risk`, and `paper_only` do not write. Active version is 13. |
| Settings sections | Sections, saved knobs, and Telegram status | `passed` | Six sections render. Polling is on. The bot token is shown as set. |
| Live settings override | Saved secrets and the model beat the environment, and the audit hides the secret | `passed` | A proof key overrode the environment. The audit said set, not the key. A page save stored `deepseek-proof`, then the previous payload was restored. Privilege stayed all_orders. |
| Settings badges and link | Badges match the mock, and Link appears only when no user id is in force | `passed` | The page shows PAPER, SET, EMPTY, ON, and OFF. The linked id is shown. Link appears only when the id is cleared. |
| Add account dialog | Id, label, Cancel, and Add account. The Gateway book stays put | `passed` | The dialog title is Add IB account. Cancel closes it. Proof sleeve `ZZACCT` appeared beside Paper and was not active. Settings restore removed it. Mode stayed paper on `DUR253115`. |
| Account sync and trading | Both toggles click, persist, and the cycle follows them | `passed` | Both started on for the paper account. A click turned each off and showed a toast. Settings restore turned them on again. |

## Still specified

| Step | Check | Status | Proof |
|------|--------|--------|-------|
| Stored risk limits | NAV, cash, margin, IV rank, DTE, earnings blackout, and daily order cap can block a draft | `passed` | Each limit returned its own block reason. Nine contracts still exceeds the max. A missing mark does not block. |
| Strike settings | Distance and DTE band come from settings | `passed` | 20% out of the money on 100 returned put 80 and call 120. |
| Conflicting shorts | A second directional short on one name is refused | `passed` | `ZZCF` had a short option. The new CSP was blocked. Rows deleted. |
| Undrafted fill | A fill with no Athena order is stored and announced | `passed` | `exec-zz` became an `EXTERNAL` fill and a position. The second copy was ignored. Rows deleted. A live Telegram note waits for a real undrafted fill. |
| Cash and margin | An Athena slot stores both beside positions | `passed` | Test mark cash 1000 and margin 100 was written and deleted. |
| Manual headline | A pasted headline can change `NEWS_WASH` | `passed` | Three `ZZNEWS` headlines tagged `NEWS_WASH`. Rows deleted. |
| Tags on the card | The draft card lists the phase tags | `passed` | `draft_zztag` included `NEWS_WASH,HIGH_IV`. The row was deleted. |
| Settings by domain | `/settings get risk` prints the limits | `passed` | The text included `contracts 3` and `privilege all_orders`. |
| Blocked symbol | A denied name is not drafted | `passed` | `ZZBLK` produced no draft row. |
| Ops health | Health and a short log tail | `passed` | Health reported slot, backup, and Gateway. A two-line log returned the last line. |
| Backup retention | An old snapshot is removed and the latest stays | `passed` | A 30-day-old file was removed. The newer file stayed. |
| Slot reconnect | Retry only while the slot is Athena | `passed` | Athena retried until the third login. Mobile disconnected with no login attempt. |

## Still deferred

Tailscale on the Windows box, the Vercel console host, Feishu, fanit export, privilege `opens_only`, Tier B and C news, Nautilus, and a cloud trading host stay deferred. The local console is not deferred.
