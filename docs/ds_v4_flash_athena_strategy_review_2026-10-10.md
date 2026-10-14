# Athena strategy review — signal → Long / CSP / CC permutations

**Model:** deepseek-v4.1-flash (`ds_v4_flash_` prefix)
**Date:** 10 Oct 2026
**Reviewer stance:** adversarial. Every claim below is tied to a file, a test, or a row I read in
the live paper book (`PAPER1`).
**Companion:** [strategy_review_2026-10-10.md](strategy_review_2026-10-10.md) — a prior review of the
same surface. §7 lists where I agree, and the three places I think it is wrong.

**Scope actually read**

| Surface | Path |
| --- | --- |
| Signaled LLM loop | `packages/strategy-nfr/src/athena_strategy/agents.py` |
| Deterministic phase engine | `packages/strategy-nfr/src/athena_strategy/phases.py` |
| Standing prompts | `packages/settings/src/athena_settings/prompts.py` |
| Stored prompts (live) | `settings_versions.payload.agents` for `PAPER1` |
| Tag sources | `iv.py`, `price.py`, `news.py`, `classifiers.py` |
| Draft / risk gate | `drafts.py`, `packages/risk/src/athena_risk/__init__.py` |
| Cycle wiring | `apps/orchestrator/src/athena_orchestrator/paper.py`, `loop.py`, `sim.py` |
| Slot clock | `apps/exec-ib/src/athena_exec/slots.py` |
| Audit rows | `ai_calls`, `decision_trails`, `drafts`, `symbol_phases`, `iv_marks`, `market_bars`, `positions`, `settings_versions` |
| Intent | `docs/athena/03-strategy-news-frontrun.md`, `10-implementation-tracker.md` |
| Tests | `apps/orchestrator/tests/test_rules.py` |

**Goal restated.** Detect signals that produce drafts of **Long, CSP, CC** — single or multiple.
CC only exists when a Long (shares) exists. Shape: an orderly market dip (the June AI-risk pullback)
should produce **Long + CSP** at the low; a run to a high should produce **CC** against held shares
(INTC 95 → CSP, INTC 122 → CC).

---

## 0. What is actually implemented

A greenfield uv monorepo, Python 3.12, Windows-first by intent, running today on the Mac against
local Postgres plus MinIO. 25 tables, 6 apps/packages, one live paper account.

One cycle every 5 minutes (`paper.py::run_paper_cycle`), in this order:

1. IB session + mock orders
2. `write_watchlist_bars` (iTick)
3. `ingest_news` (Yahoo RSS + Finviz + FOMC calendar + Nasdaq earnings) then `enrich_sentiment` (DSH)
4. `record_iv_marks`
5. **`evaluate_watchlist`** — deterministic NFR phase engine, every watchlist symbol
   → and, **only if** `slot_state(now, slots) == "athena"`, **`run_signaled`** — the 7 LLM agents
6. `manage_book` — roll / profit-take drafts
7. local snapshot + S3 sync

**There are two engines, and they are wired in series.**

### Engine A — deterministic NFR phase machine (`evaluate_watchlist` → `evaluate_symbol`)

- Tags = `tags_from_events` ∪ `tags_from_iv` ∪ `tags_from_prices` (news density, IV rank, RSI /
  support / resistance / volume spike), unioned with the stored set.
- `choose_phase` moves P1…P5 off `calendar_offset` (days to nearest Fed / earnings) plus `_panic()`
  (`NEWS_WASH ∧ HIGH_IV`, or `PRICE_WEAK ∧ VOLUME_SPIKE`).
- `choose_tactic` maps phase + tags → `OPEN_STRANGLE` / `OPEN_CSP` / `OPEN_CC` / `CLOSE` / `HOLD`.
- **Then it throws the opens away:**

```python
if tactic in {"OPEN_STRANGLE", "OPEN_CSP", "OPEN_CC"}:
    tactic = "HOLD"
```

This is deliberate. `test_evaluate_symbol_stores_open_as_hold` asserts it. So Engine A is a
**book-keeper**: it fills `symbol_phases` (phase, tags) and can only ever draft `CLOSE`.
Its `choose_tactic` OPEN_* branch is dead output — tested, but never used to draft.

### Engine B — signaled LLM loop (`run_signaled`)

Seven agents per symbol, in order `Location → News → CSP → Purchase → Strangle → Call → Consolidator`.
Taggers propose nothing; four tactic agents each propose at most one leg; the Consolidator (thinking
on) picks drafts. `_drafts` then stages each pick through the deterministic risk gate.

- **Opens can only come from Engine B.** Verify: `evaluate_symbol` erases its own opens; `_drafts` is
  the only other caller of `stage_draft` for opens.
- Engine B runs live **only inside an Athena HKT slot** (`slot_state == "athena"`), Mon–Fri
  09:30–12:00, 13:00–16:00, 21:30–23:55. In a replay it runs unconditionally.

**Blast radius:** the whole open path (Long / CSP / CC) is a weekday-slot-gated LLM loop, and it has
never produced a draft. See §1.

### What is genuinely solid

- Taggers / proposers / one chooser separation; deterministic gates after the LLM; human Approve.
- Every call stores `request` and `response`; every decision stores a `steps` JSON. Replayable.
- `manage_book` is real: ROLL at `roll_dte` (21) and CLOSE at `profit_take_pct` (50%) are computed,
  not guessed.
- Risk gate is honest about missing marks ("Missing marks are skipped, not invented").
- Universe classifiers are populated for all 30 symbols, so `PULLBACK` is reachable in principle.

---

## 1. Audit trail — five looks

Read on 2026-10-10 from `athena-pg` (the live paper DB).

### Look 1 — `ai_calls`: the signaled loop has only ever run on two symbols

| agent | calls | errors | avg latency | tokens |
| --- | --- | --- | --- | --- |
| Location | 28 | 0 | 906 ms | 7,406 |
| News | 28 | 0 | 800 ms | 8,498 |
| CSP | 28 | 0 | 716 ms | 5,891 |
| Purchase | 28 | 0 | 714 ms | 4,491 |
| Strangle | 28 | 0 | 744 ms | 6,843 |
| Call | 28 | 0 | 683 ms | 6,283 |
| Consolidator | 28 | 0 | 3,323 ms | 25,024 |
| Universe | 1 | 0 | 935 ms | 236 |

Grouped by symbol: **NOK 98, SKHY 98 (both `simulated = true`), META 1 (live).**
98 = 14 cycles × 7 agents. Last call `09:42:03Z` (17:42 HKT).

**Read:** 28 per agent is not a 31-symbol live sweep. It is a 2-symbol replay, 14 days, and it
matches `sim_symbols = ["NOK","SKHY"]` exactly. **No live signaled call is on record.** The entire
LLM loop has been exercised only inside a September paper replay, and only on two names.

### Look 2 — `decision_trails`: 28 rows, all identical in outcome

```
source=consolidator  outcome=quiet  n=28   (last 2026-10-10 09:42:03Z)
```

That is the complete decision history. There are no `book` rows, no `kept`, no `drafted`, no
`blocked`. Engine A does not write decision trails at all.

### Look 3 — `drafts`: 18 rows, none from the signal path

Every draft is dated 08–09 Oct, before the signaled loop ran:

| symbol | tactic | status | reason text |
| --- | --- | --- | --- |
| NVDA | OPEN_STRANGLE ×9 | approved / rejected / blocked | `P2 stub. Paper only.` / `P2 OPEN_STRANGLE.` |
| T | OPEN_STRANGLE | approved | `paper` |
| T | OPEN_CSP | approved | `paper account probe` |
| QCOM | OPEN_STRANGLE | rejected | `paper` |
| 700 | OPEN_STRANGLE | approved | `Mock progress. Sell 380.0 put and 470.0 call…` |
| 700 | CLOSE | approved | `P4 CLOSE. Cancel the working sells.` |

**Zero signaled drafts. Zero `BUY`, ever. Zero `OPEN_CC`, ever.** The `T` CSP is a manual probe; the
`700` strangle is a mock-signal fixture; the NVDA rows are the `ensure_paper_draft` stub.

### Look 4 — `symbol_phases`: 30 rows, all `HOLD`

All 30 are `P1 / HOLD`. Spot check `T`:

```
T   P1   HIGH_IV,NEWS_WASH,OVERSOLD,PRICE_WEAK,VOLUME_SPIKE   HOLD
```

`T` sits on the textbook P2 setup — `_panic()` is literally `NEWS_WASH ∧ HIGH_IV` — so
`choose_tactic("P2", {HIGH_IV, NEWS_WASH})` returns `OPEN_STRANGLE`, and then the rewrite stores
`HOLD`. The engine detects the trade and then deletes it. Also note the tracker (§7) claims the
opposite.

### Look 5 — the artifacts inside the calls

**Consolidator, SKHY** (`result = QUIET`):

```
Symbol: SKHY / Intent: income / Long: off / Shares held: 0 / Flags: csp=on cc=on strangle=on
Tags: CHANNEL_RISING
Contract cap: 12 / Position cap: off / Min DTE: 1 / Earnings blackout: 7 days / Cash reserve: 5%
Proposals:  CSP: none / Call: none / Purchase: none / Strangle: none
```
→ `{"drafts": [], "thinking": "No CSP, call, purchase, or strangle proposals are provided…"}`

**Location, NOK:** `Last 10.14`, channel `8.37–13.13`, prior-63 high **17.45**, `252-session high
17.45`, `252-session low 4.71` → no tags. Note the prior-63 high (17.45) equals the "52-week" high.

**CSP, NOK:** `Tags: none`; two listed puts only (`8.5 limit 0.19`, `9 limit 0.32`).

**Call, SKHY:** `Shares held: 0`, `Tags: CHANNEL_RISING` → quiet.

**Read:** every agent behaved as instructed. The loop is not malfunctioning — it is **inert by
construction**: it opens only at rails, and the book has never been at a rail while the model was
allowed to run.

---

## 2. Q1 — Do the prompts carry enough information, settings, news, and upstream output?

**Verdict: enough to be safe, not enough to be selective — and the deficit is concentrated in the two
nodes that decide (CSP and Call), plus the one node that chooses (Consolidator).**

### What each node receives today (from code + the real request bodies)

| Node | Receives | Does **not** receive |
| --- | --- | --- |
| Location | last, 63/252 rails, prior-63 rails | trend slope, rail position as a number, RSI, volume |
| News | last 8 headline titles | sentiment history, magnitude, source mix, prior wash state |
| CSP | last, tags, intent, 2–4 listed puts (expiry + strike + limit) | IV rank value, delta, DTE, distance-OTM, annualized yield, assignment math, cash locked, earnings countdown |
| Purchase | last, intent, long, tags, position cap, shares held | range position, dip depth, how much cash a buy consumes vs reserve |
| Strangle | tags, listed puts + calls | IV rank value, margin impact, RV |
| Call | last, shares held, tags, open purchase proposal, listed calls | **cost basis**, strike vs basis, ex-div date, contract coverage |
| Consolidator | intent, long, shares, flags, tags, caps, min DTE, blackout, cash reserve, 4 proposals | per-proposal buying power, IV rank, **existing open short options per symbol**, book P&L, batch math |

### Gaps that block the stated goal

1. **No cost basis at Call.** This is the single most important missing field for the wheel. A CC
   struck above the rail but below basis locks a realised loss; no human would sell it. The Call
   agent sees `Shares held: 0` and nothing else about the position. The wheel rule
   `strike ≥ basis` exists nowhere in prompts or code.
2. **No contract coverage.** `_shares` returns the raw share count — the live book holds **1 share
   each** of MRVL, QCOM, T. Nothing caps a CC's contracts by `shares / 100`. See §3.2.
3. **No numeric trend quality.** Location emits booleans. `LOWER_RAIL` is a 1%-of-rail test, so it
   cannot separate "first orderly pullback in a strong trend" from "channel rolling over".
4. **No screener numbers.** The listed lines are raw `expiry / right / strike / limit`. There is no
   delta proxy, no distance-OTM, no DTE, no yield, no liquidity — so the model picks a strike by
   vibes. The industry answer is to compute these before the model sees the chain (§4).
5. **Tags arrive as a comma string.** Fine for a human; for a parser it is prose. A structured
   payload (`{"CHANNEL_RISING":{"high":…,"low":…}}`) would make "why did this fire" auditable and
   remove the misfire ambiguity.
6. **`reason` is not required and never used for ranking.** `_proposal_block` serialises each tactic's
   JSON, but CSP/Call/Purchase do not have to justify their pick against a tag, so the Consolidator
   can only rank by limit price.
7. **Settings reach the model, but thinly.** It gets `contract_cap`, `min_dte`, `cash_reserve`,
   `earnings_blackout` as scalars. It does not get `max_symbol_pct_nav`, `max_short_opt_margin_pct`,
   NAV, cash, or margin — the numbers it would need to size a ticket.

### One correction to the prior review

The prior review (§1.2 and §4.7) says price tags are "not wired into the signaled loop". **That is
wrong.** `_context` builds `base_tags = stored ∪ iv_tags`, and `stored` is whatever Engine A last
wrote to `symbol_phases` — which includes `OVERSOLD`, `SUPPORT_TOUCH`, `RESISTANCE_TOUCH`,
`PRICE_WEAK`, `VOLUME_SPIKE`. `_tag_set` keeps them. So the signaled agents **do** see the price
tags; the CSP/Purchase/Call prompts simply **instruct the model to honour a different vocabulary**
(`LOWER_RAIL` / `LOW_52W` / `PULLBACK`) and discard them. The defect is prompt vocabulary
divergence, not missing data — which is a cheaper fix than the prior review implies.

---

## 3. Q2 — Is the strategy sound? Major issues

### Sound

- Separation of duties; deterministic gates after the LLM; human Approve; full replayability.
- CC sequencing is structurally right: Purchase runs before Call, Call is told not to fire until the
  fill, and `_drafts` drops `BUY` unless Long ∧ Accumulate.
- Vol-premium mechanics are correctly parameterised: 50% profit take, 21-DTE roll.

### Issues, highest severity first

#### 3.1 Nothing can open. Two independent causes.

(a) Engine A erases its own opens (`→ HOLD`, tested). (b) Engine B is slot-gated, and in its only
recorded execution (the NOK/SKHY replay) it was quiet on every cycle. Net effect, verified in
`drafts`: **the "detect signals → draft Long/CSP/CC" capability has never produced a single draft.**
Whatever else is fixed, this is the acceptance test: a dip must produce a draft, and today it cannot.

#### 3.2 "CC only when a Long exists" is a prompt, not a rule — and it is live-relevant

No code requires shares for `OPEN_CC`:

- `_drafts` checks flags only for `BUY`. `OPEN_CC` has no shares precondition.
- `stage_draft` blocks *conflicting shorts* (`_has_short`), not a missing long.
- `_queue_order` will happily emit a `SELL` call for `OPEN_CC`.

The only guard is the Call system prompt sentence "Propose only when shares are already held" —
LLM goodwill. And the model is shown `Shares held: 1` for MRVL/QCOM/T (the live book holds one share
each), which reads as "a long exists". **If the Call agent proposes, the system stages and submits a
naked short call.** This is the highest-severity gap in the codebase, and it is exactly the invariant
the user stated as a hard requirement.

#### 3.3 The conflict rule forbids the permutations the goal asks for

`stage_draft` blocks **any** open when `_has_short()` is true, and `_has_short` is true whenever a
SELL order is working or the latest OPT position is short. Consequence:

- A CSP is filled (short put on the book) → an `OPEN_CC` on the same symbol is **blocked**.
- A CC is working → an `OPEN_CSP` on the same symbol is **blocked**.
- A strangle + anything else on the same symbol is blocked.

The tracker records this as a feature (`P4-18 No conflicting directional shorts`). But the user's
requirement is literally "single **or multiple**", and a short put + long shares + short call is the
*normal* wheel state. The rule as written makes the wheel unrepresentable.

#### 3.4 The IV tag is degenerate and semantically inverted

`record_iv_marks` writes `iv_proxy = (put_limit + call_limit) / close`, where both limits come from
the **static** `option_plans` row that never changes. So `iv_proxy ∝ 1 / price`, and `iv_rank` is
therefore an **inverse-price percentile**, not a volatility percentile.

Worse, the live profile sets `ivr_min_samples = 1`. With one sample `high == low` → `None` → no tag.
With two samples the rank can only be exactly 0 or 100. Observed result: `QCOM → LOW_IV`,
`T → HIGH_IV`, nothing in between, ever. And `HIGH_IV` fires when price is *low* — so it is a noisy
duplicate of `LOWER_RAIL`, it double-counts in `_panic()`, and it will make the Strangle agent
behave backwards (sell vol precisely when the "vol" reading is an artefact of a price drop).

#### 3.5 The "52-week" rails are ~3.3-month rails

`market_bars` holds ~69 daily bars per symbol (ib-daily 1,743 rows / 29 symbols ≈ 60, plus
ib-delayed). So `_rail(bars, 0, 252)` returns the max over all ~69 bars. Proof in the audit:
`Location NOK` reports `63-session high 21 sessions ago: 17.45` **and** `252-session high: 17.45`
— identical. Therefore `HIGH_52W`/`LOW_52W` are near-duplicates of `UPPER_RAIL`/`LOWER_RAIL`, and
"CC at the 52-week high" currently means "CC at the ~3-month high". The strategy semantics silently
depend on a data-acquisition gap.

#### 3.6 The June-dip case is not representable

The dip the user cites is "small drop, de-risk, then recover". Nothing in the tag set expresses it:

- `LOWER_RAIL` needs within 1% of the 63-session low. In a real flush the price slices the old low
  and the tag fires *during* the panic.
- `PULLBACK` needs bottom 35% of the channel **today**, plus `CHANNEL_RISING`, plus MAG7/MEGA_CAP.
  But right after a flush the prior rails are higher, so `CHANNEL_RISING` typically does **not** hold
  — the two conditions are anti-correlated exactly when you want them.
- There is no "was in the lower zone within N sessions and has now reclaimed" state.

So the strategy is silent at the precise moment the user designed it for.

#### 3.7 Permutations are not a first-class object

`has_open_draft` correctly stops a duplicate leg. But `drafts` in one Consolidator reply are staged
independently, each with its own cash check. Two legs that individually fit
`cash_reserve_pct` / `max_symbol_pct_nav` / `max_short_opt_margin_pct` can jointly breach them. The
gate is per-draft, never per-ticket. Combined with 3.3, "multiple" is doubly blocked: by conflict
rule before staging, and by absent batch math during staging.

#### 3.8 Quiet is indistinguishable from broken

`parse_json` returns `{}` on unparseable model output, and `_drafts` treats `{}` as "no drafts" →
`outcome="quiet"`. A schema break, a truncated reply, or a refusal is recorded identically to a
genuine no-signal. The `status` column can say `error`, but the `decision_trails` row still says
`QUIET`. (A tolerant-regex parse with no retry, no validation, and a silent default is the single
most common failure mode in production LLM-agent systems.)

#### 3.9 Cost and audit noise

7 calls × 31 symbols × every Athena slot, with no pre-filter. `_run_symbol` runs the full
`RUN_ORDER` even when `tags = none` and the chain is empty. The recorded 2-symbol replay already
burned ~75k tokens. At 31 symbols × 3 windows × 5-min cycles this is a large, uninformative bill,
and it floods `ai_calls` with `QUIET` rows that hide the interesting ones.

#### 3.10 Config makes Long unreachable before prompts matter

Every `tactic_flags` entry has `long = true` but `intent = "income"`. The Purchase prompt requires
Long **and** Accumulate, and `_drafts` drops `BUY` unless Long ∧ Accumulate. So **no symbol can ever
produce a Long draft in the current settings** — the long leg of "Long + CSP" is administratively
impossible. Also live: risk profile `test` (cash reserve 5%, max contracts 12, `min_iv_rank_new_short
0`, `max_position_per_symbol 0` = money cap off).

#### 3.11 Smaller but real

- **Data-source divergence.** `agents._bars` reads `market_bars` with **no source filter** (includes
  `ib-delayed` intraday extremes), while `price.py` / `iv.py` use `source='ib-daily'`. The signaled
  rails and the tag rails can be computed from different series.
- **One expiry, one shot.** `_priced_lines` lists a single expiry's shortlist; if it is unattractive
  there is no "next expiry" fallback and the prompt cannot ask for one.
- **`_option_leg` fragility.** `manage_book` cannot draft a roll/close without an `option_plans` row.
- **Documentation drift.** Tracker `P4-01` claims the phase machine walks "P4 OPEN_CSP, P5 OPEN_CC"
  and stores those pairs in `symbol_phases`. Code and DB both say otherwise (all `HOLD`), and a test
  asserts the `HOLD` behaviour. The tracker row is stale.

---

## 4. Q3 — Industry standard, and how the core should be adjusted

### Comparable tools

Retail / semi-pro options-income platforms are the closest match and they converge on one shape:

| Tool | What it does that Athena does not |
| --- | --- |
| Option Income Tools (+ backtester) | Rank 50–300 tickers by IV rank, delta-target strike, **annualized premium yield**, liquidity, earnings proximity — deterministically — *before* surfacing candidates |
| Days to Expiry (wheel backtester) | Models the symbol as `cash → CSP → assigned → shares → CC → called away → cash` and tracks roll-adjusted basis across the cycle |
| Option Wheel Logic | Treats earnings risk per position as a first-class number; flags it on the position |

Systematic-index reality check: the CBOE put-write (PUT/BXM) and buy-write (BXM) indices are the
institutional version of this — mechanical, delta-targeted, monthly, no discretion. The academic
backbone is the volatility risk premium (Bakshi–Kapadia): you are paid for selling insurance, and
the edge lives in **strike selection relative to a level you would accept**, not in prediction.
The practical rule every one of these encodes:

> A CSP strike is a **limit buy order you are happy to have filled**. A CC strike is a **limit sell
> order above your own cost**. Neither is a directional bet.

LLM-in-the-loop practice (multi-agent trading write-ups, guardrail papers) adds three constraints
Athena half-satisfies: typed handoffs with schema validation + bounded retry (Athena has neither);
fail-closed with a first-class HOLD (Athena is good here); and numeric claims must cite a computed
value (Athena's proposals cite nothing, so "the model picked 8.5" is unverifiable).

### How the core should change

**1. Make the wheel the state object, not the draft.** One state per symbol:
`CASH → SHORT_PUT → ASSIGNED → SHARES → SHORT_CALL → CALLED_AWAY → CASH`, plus `LONG_ONLY` for
accumulation names. Legal transitions per state, derived in code. The June-dip case is the
`CASH → SHORT_PUT (+BUY)` transition; the INTC-122 case is `SHARES → SHORT_CALL`. Today those are two
independent quiet/loud agents with no shared state — which is why neither fires.

**2. Move screening and ranking into code.** For each listed line compute: delta proxy, DTE,
distance-OTM %, annualized yield (`premium / (strike × multiplier) × 365 / DTE`), spread/liquidity,
earnings-in-days. Pass the top 2–3 lines *with numbers*. Keep the LLM for the two things it is
actually good at: reading the news cluster, and writing the human-readable thesis on the draft.

**3. Lock the economics the user asked for — in code.**

| Desired | Deterministic trigger (code, not prompt) |
| --- | --- |
| **LONG** on a dip | `TREND_STRONG` (both rails rising) **and** drawdown from range high in `[5%, 15%]` (orderly, not a crash) **and** `RECLAIM` (close back above the 5-day mean / prior bar high) **and** not `EARNINGS_NEAR` |
| **CSP** beside the Long | the LONG trigger holds **and** IV ≥ its own median **and** strike ≤ a level you would accept as a buy, struck as a delta band (≈0.20–0.30), 30–45 DTE |
| **CC** on a high | `shares ≥ contracts × 100` **and** strike ≥ cost basis **and** price in the top decile of its range or `UPPER_RAIL` |
| **Multiple** | a *ticket* = an ordered leg list; one batch risk check over the ticket |

**4. Replace the IV proxy.** Use ATM IV from the chain, or a realised-vol multiple, with a real
rolling window (`ivr_min_samples ≥ 20`). Tag `IV_RICH` from the **IV − RV spread**, not from
premium ÷ price. This alone un-inverts `HIGH_IV`.

**5. Harden the invariants.** Add `shares ≥ contracts × 100` to `stage_draft` for `OPEN_CC` (hard
gate, not a prompt). Add a batch-level cash / margin / NAV check. Replace the blanket conflict rule
with a *state-aware* one that permits the legal wheel combinations (short put + long shares + short
call) and blocks only genuinely naked or directionally contradictory books.

**6. Fix the data before the tags.** You cannot compute a 52-week rail, a real IV rank, or a
regime filter from 69 bars. Extending history to ≥ 252 sessions is a precondition for most of §5.

**7. Reachable config.** Either set `intent = accumulate` where Long entries are wanted, or decouple
"may buy shares" from "income" so the long leg is not administratively dead.

**8. Audit hygiene.** Schema-validate replies with one retry; distinguish `NO_DATA` from
`NO_SIGNAL`; pre-filter ineligible symbols so the model is never called for them.

---

## 5. Q4 — Additional technical indicators for better `tags`

Ordered by value for *this* goal. All are computable in `_context` / `_tag_set` from bars already in
the DB — no LLM, no new vendor.

| # | Tag | Definition | Why it matters here |
| --- | --- | --- | --- |
| 1 | **`DIP_BOUGHT`** | price was in the bottom 35% of the 63-session range within the last N≈5 sessions **and** last close is back above that band **and** `TREND_STRONG` | Fills the June gap. This is "the dip happened and buyers returned" — the exact moment to Long + CSP |
| 2 | **`TREND_STRONG` / `TREND_WEAK`** + slope % | `(rail_now − rail_21ago) / rail_21ago`, both rails; pass the number to Location/CSP | Separates a dip in a healthy trend from a rollover. The number, not the boolean, is what the CSP agent needs |
| 3 | **`rail_pos` (0–1)** as a number to every tactic | already computed inside `_pullback`; promote it to `_context` | Enables "near the lower rail but not touching → wider strike" instead of binary silence |
| 4 | **`IV_RICH` / `IV_CHEAP`** via IV − RV | 20-session realised vol from daily closes vs the IV proxy; tag the spread | The right vol gate for the strangle and the CSP. Replaces the inverted `ivr` |
| 5 | **`DRAWDOWN_OK`** | drawdown % from the range high, banded (e.g. `−5…−15%` = orderly) | Distinguishes an orderly pullback from a −30% break |
| 6 | **`VOLUME_DRY`** | pullback-day volume < 0.7 × 20-day average while the wash day spiked | The classic "selling exhausted" tell for a dip-buy; complements the existing `VOLUME_SPIKE` |
| 7 | **wire `OVERSOLD` / `OVERBOUGHT`** | already in `price.py`; they arrive via `base_tags` but the tactic prompts ignore them | Cheapest single fix in this list — make them explicit prompt vocabulary |
| 8 | **`ATR_WIDE` / ATR value** | ATR(14) passed to the tactic agents | A 2×ATR-OTM put is an honest income strike; a fixed % OTM is not |
| 9 | **`earnings_in_days: n`** | the gate already computes the offset; expose it to every tactic | Lets the model pick a post-earnings expiry instead of being silently gated |
| 10 | **`RANGE_PCTL`** | percentile of price in its own history | Distinct from the 1% rail touch; needs #6 of §4 first |
| 11 | **`REGIME_RISK_OFF`** | index (SPY/QQQ) close vs its own 63-session mid | Makes single-name dips in a falling market draft smaller or nothing |
| 12 | **news magnitude score** | the weighted `enrich_sentiment` output instead of the boolean `news_wash` | Lets the Consolidator weigh "one blog" against "cluster of downgrades" |

---

## 6. Recommended sequence

1. **Safety first (days).** Hard gate `shares ≥ contracts × 100` for `OPEN_CC`; batch risk check;
   split `NO_DATA` from `NO_SIGNAL`; schema-validate replies with one retry.
2. **Fix tag semantics (days).** Extend bar history to ≥ 252 sessions; replace the IV proxy; add
   `DIP_BOUGHT`, `TREND_*`, `rail_pos`, `DRAWDOWN_OK`. Prompts then get better inputs for free.
3. **Screener in code (week).** Compute per-line delta / DTE / yield / distance-OTM; pass the top 2–3
   lines with numbers to the tactic agents.
4. **Wheel state machine (week+).** One state per symbol, legal transitions, ticket = ordered legs.
   This is the change that actually delivers "Long / CSP / CC, single or multiple".
5. **Config + acceptance.** Make Long reachable; prove the two acceptance cases end-to-end on the
   paper book: a June-shaped dip → `[BUY, CSP]` ticket; a run to the high → `[CC]` ticket.

---

## 7. Corrections to `strategy_review_2026-10-10.md`

**Agree:** the pipeline is information-starved at CSP and Call; the separations of duties and the
deterministic gate are the right shape; the wheel should become the state object; screener-first is
the correct industry pattern; the audit should separate "no data" from "no signal".

**Three corrections:**

1. **"RSI / price tags are not wired into the signaled loop."** They are — via `base_tags`
   (`stored ∪ iv_tags`, and `stored` is Engine A's `symbol_phases` output). The defect is that the
   tactic **prompts** honour a different vocabulary (`LOWER_RAIL`/`LOW_52W`/`PULLBACK`) and discard
   the richer tags. Prompt fix, not data plumbing.
2. **"The loop is simply very quiet because the tag set is narrow."** Understated. The audit shows
   the signaled loop has **never run live** and has **never produced a draft**; the only opens the
   system can produce are from the slot-gated LLM path, and Engine A deliberately erases its opens.
   The problem is not that it is quiet — it is that the open path is inert, plus §3.2 (naked-CC
   exposure) which that review does not mention at all.
3. **It treats the conflict rule as benign.** `_has_short` blocking every open while a short exists
   makes the wheel's normal state (short put + shares + short call) impossible. That is a direct
   contradiction of the stated "single or multiple" requirement and should have been a top-3 issue.

**One gap:** no mention that `iv_rank` is an **inverse-price** percentile (§3.4) or that the
"52-week" rails are ~3.3 months (§3.5). Both silently change what `HIGH_IV`, `HIGH_52W`, and
`LOW_52W` mean.

---

## Appendix — reproduction

```sql
-- who ran
SELECT agent, count(*), count(*) FILTER (WHERE status='error'), round(avg(latency_ms)), sum(tokens)
FROM ai_calls GROUP BY agent ORDER BY 2 DESC;
SELECT symbol, simulated, count(*) FROM ai_calls GROUP BY 1,2;
-- what was decided
SELECT source, outcome, count(*), max(logged_at) FROM decision_trails GROUP BY 1,2;
-- what was ever drafted
SELECT created_at::date, symbol, tactic, status, reason, simulated FROM drafts ORDER BY created_at;
-- engine A output
SELECT symbol, phase, tags, tactic FROM symbol_phases ORDER BY symbol;
-- the two data defects
SELECT source, count(*), count(DISTINCT symbol) FROM market_bars GROUP BY source;
SELECT symbol, count(*), min(day), max(day) FROM iv_marks GROUP BY symbol ORDER BY 2 DESC;
SELECT symbol, expiry, put_limit, call_limit FROM option_plans;   -- static row -> proxy ∝ 1/price
-- the live book (share coverage matters for CC)
SELECT symbol, sec_type, quantity, avg_price FROM positions ORDER BY symbol;
```

Code anchors: `agents.py` `_tag_set` / `_pullback` / `_context` / `_drafts`; `phases.py`
`choose_tactic` / `evaluate_symbol`; `drafts.py` `stage_draft` / `_has_short` / `_queue_order`;
`iv.py` `record_iv_marks`; `price.py` `price_tags`; `paper.py` `run_paper_cycle`; `slots.py`
`slot_state`. Test anchors: `test_evaluate_symbol_stores_open_as_hold`,
`test_pullback_is_only_a_mega_cap_dip`, `test_iv_rank_needs_a_full_window`,
`test_rails_use_63_sessions_and_the_prior_window`.
