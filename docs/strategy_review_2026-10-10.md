# Athena strategy review — signaled entry

Date: 10 Oct 2026
Scope: the signaled-entry loop (`run_signaled` in `packages/strategy-nfr/src/athena_strategy/agents.py`), the seven
agent prompts (`packages/settings/src/athena_settings/prompts.py`), the tag sources (`iv.py`, `price.py`, `news.py`),
the risk gate (`packages/risk/src/athena_risk/__init__.py`), and the audit trail in the paper book (DUR253115).

Goal restated: detect signals that produce drafts of Long, CSP, and CC permutations — single or multiple.
CC only exists when a Long (shares) exists. Example shape: an orderly market dip (the June AI-risk pullback) should
produce Long + CSP at the low; a run to a high should produce CC against held shares (INTC at 95 → CSP, INTC at
122 → CC).

---

## 0. What the audit trail shows today

The live book has run the loop on NOK and SKHY since the morning cycles. Pattern of the last ~28 call sets:

- All seven agents answer `ok` every cycle. Latencies are sub-second for the tactic agents, 2–6 s for Consolidator
  (reasoning on, ~600–1,200 tokens).
- Location mostly answers `CHANNEL_RISING` or empty. News answers `GENERIC_POSITIVE`. CSP, Purchase, Strangle, and
  Call answer `QUIET` every cycle. Consolidator answers `QUIET` every cycle, with thinking like "No CSP, call,
  purchase, or strangle proposals are provided. Income intent drops share purchases."
- Decision trails: all `quiet`.
- Drafts in the book are all from 8–9 Oct and are `OPEN_STRANGLE` / `OPEN_CSP` stubs (P2 stub, manual), not from
  the signaled loop. No signaled `BUY` or `OPEN_CC` draft has ever been created.

Concrete NOK cycle (09:41 UTC): Location saw last 10.36 inside a 63-session channel 8.37–13.13 and emitted no
tags — correct, the price is mid-channel. The CSP agent then saw "Tags: none" and stayed quiet. Correct behavior,
but it also shows the loop only speaks at the edges.

Concrete SKHY cycle: last 186.68, channel 124.82–199.87, rising. `CHANNEL_RISING` only. Position 0.83 up the
channel — no rail, no 52-week extreme, no PULLBACK (SKHY is not MAG7/MEGA_CAP). Everything quiet. Correct.

IV marks: NVDA 23, NOK 14, SKHY 14, others 2. `ivr_min_samples` is 1 on the live profile, so NOK/SKHY already
rank; most of the watchlist cannot produce HIGH_IV/LOW_IV yet.

**Read:** the loop behaves as designed — it is simply very quiet, because the tag set is narrow and the tactics
only arm at the extremes. The June-dip and INTC-high cases are exactly the cases that never fire mid-channel.

---

## 1. Do the prompts carry enough information?

### What each node sees today

| Node | Sees | Does not see |
| --- | --- | --- |
| Location | last, 63/252-session rails, prior 63 rails | trend slope %, distance-to-rail as a number, RSI, volume |
| News | last 8 headlines | sentiment history, prior news_wash state, source mix |
| CSP | last, tags, intent, listed puts (expiry, strike, limit) | IV rank, delta of the listed puts, cash this would lock, earnings date, why tags fired |
| Purchase | last, intent, long, tags, position cap, shares | channel position, pullback depth, how much cash a buy uses vs reserve |
| Strangle | tags, listed puts and calls | IV rank value, margin impact |
| Call | last, shares, tags, open purchase proposal, listed calls | cost basis of held shares, distance of call strike vs basis, ex-dividend date |
| Consolidator | intent, long, shares, flags, tags, caps, min DTE, blackout, cash reserve, all four proposals | buying-power math per proposal, IV rank, existing open positions per symbol, P&L of the book |

### Gaps that matter for the stated goal

1. **No numeric trend quality.** Location emits booleans only. A 1%-from-the-rail test plus rising rails cannot
   distinguish "first orderly pullback in a strong trend" (the June case) from "channel about to break down".
   The CSP agent has no slope % or drawdown depth, so it cannot be told "buy this kind of dip".
2. **No CC-basis link.** The Call agent knows shares are held but not the average cost. A CC above the rail but
   below cost basis locks in a loss; a human would not sell it. This is the single most important missing field
   for the wheel.
3. **No position awareness in Consolidator.** It knows shares held but not open short options on the symbol. It
   can draft a second CSP while one is already working (only the per-tactic open-draft check stops duplicates —
   and that check blocks *legitimate* second legs of a multi-draft permutation).
4. **News is headline titles only, no magnitudes.** `news_wash` is a boolean. The consolidator cannot weigh
   "one negative blog" vs "cluster of downgrades". A weighted score exists in code (`enrich_sentiment`) but the
   signaled path does not pass it through.
5. **Tags arrive as a comma string.** The tactic agents parse prose, not structure. Passing a small JSON block
   (`{"CHANNEL_RISING": {"high": ..., "low": ...}}`) would cut the quiet/misfire ambiguity and make reasons
   auditable.
6. **Proposals block loses the "why".** `_proposal_block` serializes each tactic's JSON, but CSP/Call/Purchase
   prompts do not require a `reason` tied to a tag, so the consolidator cannot rank competing proposals by
   anything except limit price.

### Verdict

Enough to be safe, not enough to be selective. The pipeline is information-starved at exactly the two nodes that
decide: CSP (no trend/dip quality) and Call (no basis). Consolidator is asked to "choose drafts" from proposals
that carry a price and no thesis.

---

## 2. Is the strategy sound? Major issues

### Sound parts

- **Separation of duties.** Taggers (Location/News) and proposers (CSP/Purchase/Strangle/Call) and one chooser
  (Consolidator). Matches the industry pattern (§3).
- **Deterministic gates after the LLM.** `stage_draft` → `block_reason` enforces contract cap, position cap,
  cash reserve, margin, IV rank floor, DTE floor, earnings blackout, daily order cap. The model cannot exceed
  them. Approve is still human.
- **CC sequencing is structurally right.** Purchase runs before Call; Call is told to stay quiet until the
  purchase fills; `_drafts` also drops BUY unless Long+Accumulate. CC cannot fire without shares.
- **Replayability.** Every call stores the request body and response; decision trails store the context.

### Major issues, in order

1. **The signal set is a rail-touch detector, not a dip detector.** LOWER_RAIL fires within 1% of the 63-session
   low. In a real June-style flush the price slices through the old low and the tag fires *during* the panic —
   precisely when a human waits one or two sessions for stabilization. There is no "dip into rising trend,
   washed out, now recovering" state. PULLBACK is a step toward this but is gated to MAG7/MEGA_CAP and still
   requires being in the bottom 35% *today* — it cannot express "was in the bottom 35% within the last 3
   sessions and has now reclaimed". **This is the gap that makes the strategy silent exactly when you want it.**
2. **Phase engine and signaled engine disagree silently.** `choose_tactic` (P1–P5) can return OPEN_CSP / OPEN_CC
   on RSI+support/resistance, but `evaluate_symbol` rewrites those to HOLD; opens come only from `run_signaled`.
   The two systems use different tag vocabularies (OVERSOLD/SUPPORT_TOUCH vs LOWER_RAIL/PULLBACK) and neither
   sees the other's work. Result: dead code, two sources of truth, and audit rows that are hard to reconcile.
3. **Permutations are not representable.** The goal says "single or multiple" — Long + CSP on the same dip.
   Today `has_open_draft` blocks a second pending draft of the same tactic (fine), but the Consolidator has no
   concept of a *combined* ticket: it can emit BUY and CSP in one `drafts` array, and each is staged
   independently with independent cash checks. Two drafts that are individually inside cash reserve can together
   breach it — the gate is per-draft, not per-batch.
4. **CSP on a dip has no assignment plan.** The wheel math (if assigned, basis = strike − premium, then CC above
   basis) is nowhere in prompts or code. So the CSP agent picks a strike with no view of "would we want to own
   it here", which is the entire point of selling a put on a June dip.
5. **One expiry, one shot per cycle.** `_priced_lines` lists only the target expiry's shortlist. If that expiry
   is unattractive (wide spread, bad DTE), the tactic is quiet — there is no "next expiry" fallback, and the
   prompt cannot ask for one.
6. **Quiet is the same color as broken.** A symbol with no option chain, no bars, or a failed News call produces
   the same `QUIET` trail as a genuine no-signal. The audit cannot distinguish "checked, nothing to do" from
   "could not check". (The request column now exists — good — but the decision trail summary is still `QUIET`.)

---

## 3. Industry standard — similar tools, and what to copy

**Comparable tools.** Retail/semipro options-income platforms are the closest match:
[Option Income Tools](https://optionincometools.com/) and its
[backtester](https://optionincometools.com/backtest/),
[Days to Expiry](https://www.daystoexpiry.com/) ([wheel backtest](https://www.daystoexpiry.com/wheel-backtest)),
and [Option Wheel Logic](https://optionwheellogic.com/). They converge on the same feature set:

- **Screener first, agent second.** They rank 50–300 tickers by IV rank, delta-target strike, premium yield
  (premium ÷ capital at risk, annualized), liquidity, and earnings proximity — deterministically — and only then
  surface candidates. Athena asks an LLM to do this ranking one symbol at a time with no yield or delta numbers
  in the prompt. Copy the screener: compute per-listed-line metrics (delta proxy, annualized yield, DTE, distance
  OTM) in code, pass the top 2–3 lines with numbers to the tactic agents.
- **Wheel-cycle state machine.** Days to Expiry and Option Wheel Logic model the symbol as
  cash → CSP → assigned → shares → CC → called away → cash, and track roll-adjusted cost basis across the whole
  cycle. Athena has the pieces (intent, long, shares) but no explicit wheel state per symbol and no basis
  tracking after assignment. This is the "strategy core" adjustment: make the wheel the object, not the
  individual draft. The June-dip case is the cash→CSP(+BUY) transition; the INTC-122 case is the shares→CC
  transition. Both should be transitions in one machine, not two independent quiet/loud agents.
- **Assignment probability and earnings proximity as first-class numbers**, not tags.
  Option Wheel Logic flags earnings risk per position; Athena has the blackout gate but the agents never see
  "earnings in 3 days" as a number.

**LLM-in-the-loop practice.** Production multi-agent trading systems — the AgenTrade case study, the
zero-trust trading desk, the crewless-capital policy engine, and Kosturski's guardrails write-up — agree on the
same shape Athena already half-follows:

- LLMs produce typed proposals; deterministic code owns eligibility, sizing, and execution. Athena does this at
  `block_reason`. Keep it.
- **Fail-closed and HOLD as a first-class outcome.** Athena is good here (quiet is normal).
- **Where they are stricter than Athena:** (a) typed handoffs with schema validation and bounded retries —
  Athena parses with a forgiving regex (`parse_json`) and treats unparseable output as `{}` → quiet, which
  hides model failures; (b) pre-filters so the model is never invoked for ineligible symbols (halted, stale
  data, no chain) — Athena calls all seven agents on every watchlist symbol every cycle, which costs tokens and
  pollutes the audit with uninformative QUIETs; (c) numeric claims must cite a computed value — Athena's
  proposals cite nothing, so "the model picked 8.5" cannot be checked against a delta or yield.

**Bottom-line adjustment:** move selection and ranking into code (screener), keep the LLM for the two things it
is actually good at — reading the news cluster and writing the human-readable thesis on the draft — and make
the wheel the state object.

---

## 4. Additional indicators for better tags

Ordered by value for the stated goal:

1. **Drawdown-and-reclaim (highest value, fills the June gap).**
   `DIP_BOUGHT`: price was in the bottom 35% of the 63-session channel within the last N sessions (N≈5) and the
   last close is back above that zone, while CHANNEL_RISING holds. This is "the dip happened and buyers
   returned" — the exact moment to Long + CSP. Cheap to compute in `_context` from the same bars; no LLM needed.
2. **Trend slope %.** `(rail_now - rail_21ago) / rail_21ago` for both rails. Emit `TREND_STRONG` /
   `TREND_WEAK` at thresholds, and pass the number to Location/CSP. Distinguishes a dip in a healthy trend from
   a rollover.
3. **Realized volatility and IV−RV spread.** You already store an IV proxy. Adding 20-session realized vol from
   daily bars lets you tag `IV_RICH` (proxy >> RV — the time to sell premium) and `IV_CHEAP` (don't sell). This
   is a better gate than a static ivr_high for the strangle and it is what the wheel platforms rank on.
4. **Distance-to-rail as a number in the prompt**, not just the boolean. `rail_pos` (0–1) is already computed
   for PULLBACK; give it to every tactic so "near lower rail but not touching" can still propose with a wider
   strike.
5. **Volume dry-up on the dip.** `VOLUME_SPIKE` exists for panic; the complement — below-average volume on the
   pullback day vs the spike on the wash day — is the classic "selling exhausted" tell for the dip-buy.
6. **Earnings/event countdown as a field** (`earnings_in_days: 3`) passed to all tactics, so the model can
   prefer the post-earnings expiry instead of being silently gated.
7. **RSI already exists** (`price.py`) but is not wired into the signaled loop. Either wire OVERSOLD/OVERBOUGHT
   into `_tag_set` for the signaled agents or delete the duplication with the phase engine (issue 2.2).
8. Lower priority: 52-week position percentile (distinct from the 1% touch), ATR for strike width (a 2-ATR
   OTM put is a more honest "income" strike than a fixed % OTM), and a simple market-regime tag from an index
   (e.g. SPY above/below its 63-session mid) so single-name dips in a falling market get smaller or no drafts.

---

## 5. Recommended sequence

1. Add `DIP_BOUGHT`, rail position, and trend slope to `_context`/`_tag_set` (deterministic, no prompt risk).
2. Pass cost basis to Call and open short-option state to Consolidator; add a batch-level cash check for
   multi-draft tickets.
3. Add the screener numbers (annualized yield, distance OTM, DTE) to the listed lines the tactic agents see.
4. Replace the per-tactic boolean prompts with the wheel state machine: one state per symbol, transitions
   cash→CSP(+BUY), shares→CC, with the consolidator choosing among legal transitions.
5. Tighten the audit: schema-validated replies with one retry, distinct "no data" vs "no signal" outcomes, and
   pre-filters so quiet symbols do not burn calls.
