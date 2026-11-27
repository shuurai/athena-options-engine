# 16 — Athena SMA: Single-Strategy Simplification Proposal

**Status**: PROPOSAL / awaiting operator ruling. This booklet plans only; it authorizes **zero implementation, zero migrations, zero deletions** until the operator approves §11 decision points and the phased order in §12.
**Evidence cut**: 2026-11-28, code baseline `21fb6de` (branch `strategy/sma20-sma50-crossover`), booklets 00–15 as current truth.
**Operator directive (2026-11-28)**: Athena has become too complicated — too many strategies, too many distractions. Keep the foundation (Iris, event stream, drafts/approvals, IBKR plumbing, news lens, togglable configs, account controls), remove everything strategy-related that is not the weekly SMA strategy, and rebuild around one machine: **case library → frame comparison → forecast → trade request → approval**.
**Supersedes (upon approval)**: booklet 15's Strategy 3 frozen subset (§22.3 / `s3-shadow-v1`) and its `eval-v1` go/no-go gate. They remain as historical verbatim records; the strategy defined here is a **new contract**, not an amendment smuggled into the old one. N3 canonical news read model (activated `C-20261126-1`) is **kept** — news stays as an information lens only.

---

## 1. Goal and non-goals

### 1.1 One sentence

Turn Athena into a single-purpose tool: **monitor the universe, detect weekly-SMA hill/retest cases, compare each live case frame-by-frame against the finished case library, forecast path and risk, and emit one trade request per signal into the existing approval flow.**

### 1.2 Goals

1. One strategy only: weekly SMA20/SMA50 hill → SMA20 retest → SMA50 target (§2).
2. A **case library** built from history (IBKR weekly data, 2 years per symbol where available) plus every live case as it completes.
3. **Frame-exact comparison**: live case at case-day N is compared against library cases truncated to their first N frames, with library futures rendered as ghost frames (§6).
4. A visual, animated console: strategy panel → per-stock case detail → comparison canvas (§9).
5. Daily run at end of US market day; intraday event stream kept for live day → week-bar → comparison updates (§8).
6. Risk/exposure computed exactly for this strategy (heat, MAE-derived stop, tag caps, beta) and nothing else (§7).
7. Per-account strategy toggle across the 3 accounts; operator intends to use account 3 (§7.4).
8. AI agent retained **strictly** as a verbal recommender on SMA20 entry signals (§5.3).

### 1.3 Non-goals (explicit removal, §4)

- NFR wheel strategies, Strategy 1/2/3 v2 semantics, options tactics (CSP/CC/strangle/rolls), IV-rank/DTE/option-chain strategy logic, Guardian escalation strategy coupling, advisor trade recommendations, simulation engines unrelated to this strategy.
- AI news analysis / AI-based trade recommendations other than §5.3.
- Unrelated risk configuration surface (§7.5).

### 1.4 Kept foundations (do not remove)

- IBKR gateway/client plumbing, pacing, market-data storage (`market_bars`, `calendar_marks`), migrations discipline.
- Canonical news read model + news pages (information lens only; no strategy coupling).
- `strategy_events` + SSE + Iris event-driven graph/animation (P2 deliverable) — reused as the live update fabric.
- Unified drafts / approvals / orders, account isolation, `decide()` gates, Telegram notifications.
- Settings toggle framework, account-level controls, backup/migration/ledger discipline.

---

## 2. Strategy contract (machine rules, `sma-weekly-v1`)

All parameters below are frozen at approval time; changing any of them later requires a new version (`sma-weekly-v2`), never a silent edit.

### 2.1 Inputs and bars

- **Weekly bars** from IBKR (`whatToShow=TRADES`, weekly, `duration=2 Y` where available; fewer accepted with an explicit `history_short` flag).
- SMA on **weekly closes**; a week counts only when **completed** (last expected session done). In-progress week-to-date values are shown on screens as *preview*, never used for state transitions.
- Adjustment: adjusted prices only; every case/frame records `adjustment_basis`; missing adjustment ⇒ `no_data`, never silently raw (inherited from booklet 15 Q8/Q14).
- **Daily bars** (1 year) pulled alongside for: frame granularity (§6), gap/stop realism, and the live day → week-bar build (§8).

### 2.2 State machine (per symbol, no "failure" terminal state)

| State | Enter when (all conditions) | Meaning |
|---|---|---|
| `IDLE` | SMA20 ≤ SMA50, or SMA20 slope ≤ 0 | Watching, no case |
| `WATCH` | SMA20 > SMA50 **and** SMA20 slope > 0 (3-week linear regression) **and** price above SMA20 | Hill forming |
| `SETUP` | First touch from above: weekly low ≤ SMA20 × 1.01 (1% tolerance) | **Case opens**, frames begin |
| `TRIGGERED` | That week closes below SMA20 (bearish confirm) | Entry signal; simulated entry = next week open |
| `IN_TRADE` | Entered, not closed | Updated every trading day (frame) |
| `RE_TEST` | Sub-phase: second rally that fails to reach SMA20, then turns down | Same case, tagged path |
| `GRIND` | Sub-phase: price grinds around SMA20, hill re-forms later | Same case, tagged path |
| `SUCCESS` | Weekly low ≤ SMA50 | Case completes → library |
| `EXTENDED` | > 26 weeks to reach SMA50 | SUCCESS + `LONG` duration label |

- `RE_TEST` and `GRIND` are **sub-phases, not new cases and not failures**. The library records the *path shape* of every case.
- There is no `STOPPED` terminal in the case taxonomy. Risk control (stop-loss) is a **trade-level** decision (§7), separate from case path recording. A case that stops the trade out continues to be tracked to its SMA50 outcome for library purposes, tagged `STOPPED_OUT_THEN_SUCCESS` if applicable — the library learns from the full path, the P&L ledger records what the risk rule did. (Decision point D3.)

### 2.3 Labels

- Duration: `SHORT(<8w)` / `MID(8–16w)` / `LONG(16–26w)` / `EXTENDED(>26w)`.
- Path: `CLEAN_DROP` / `RE_TEST_DROP` / `GRIND_THEN_DROP` / `SLOW_GRIND`.
- Hill: `SHARP_HILL` / `WIDE_HILL` / `DOUBLE_HILL`.
- Environment: `HIGH_VIX(>25)` / `LOW_VIX` / `MARKET_CRASH` / `SECTOR_ROTATION`.
- Narrative (existing symbol tags reused): `AI_SEMIS` / `AI_SOFTWARE` / `MEGA_CAP` / `EV` / `DEFENSE`.
- Touch style: `PIERCE` (one bearish weekly candle) / `STEPS` (multi-week grind down) / `GAP` / `UPPER_WICKS`.

### 2.4 Universe suitability (which symbols "fit" this strategy)

A symbol is `suitable` only when all hard gates pass; each verdict carries reasons and is recomputed on the daily run:

| Gate | Rule (proposed default) | Reason surfaced when failing |
|---|---|---|
| History | ≥ 60 completed weekly bars (2y target, 1.2y minimum) | `history_short` |
| Liquidity | 20-session average dollar volume ≥ $500M | `thin_liquidity` |
| Price | ≥ $5 | `penny` |
| Data health | Gap audit clean on the SMA window (reuse P4 `gap_audit`) | `internal_gap` / `halt` / … |
| Shortability (only if short-stock instrument chosen, D1) | IBKR borrow available; fee recorded | `no_borrow` / `borrow_expensive` |
| Strategy evidence | ≥ 1 historical hill in the pulled window | `no_prior_hill` |

Suitability is a **displayed property**, not a hidden filter: unsuitable symbols remain visible with their reason codes.

---

## 3. Data layer (IBKR + cache)

### 3.1 Pull spec

- Weekly: 2 years per suitable symbol (universe-wide), `endDateTime` after close so the current week is complete only when it actually is.
- Daily: 1 year, same symbols, for frames and the live day→week build.
- Context: VIX weekly/daily, SPY weekly, sector ETF weekly (existing symbol facts map sector), earnings dates into `calendar_marks` (`EARNINGS` kind — currently absent, **new data dependency**).
- Pacing: single long-lived connection + request queue, ≥3 s between historical requests, exponential backoff on failure; pacing violations are the #1 IBKR ban cause.
- Incremental updates: unique key `(symbol, timeframe, date)`; daily run pulls only the tail.

### 3.2 Stored fields (per bar)

Raw OHLCV + derived: SMA10/20/50, SMA20 slope (3w), `(SMA20−SMA50)/SMA50`, distance-to-SMA20 %, distance-to-SMA50 %, ATR14(weekly), weekly return. Context: VIX close, SPY weekly return, sector ETF weekly return, earnings-week flag, dividend-week flag.

### 3.3 Reuse vs new

- **Reuse**: `market_bars` (ib-daily store exists), `trading_calendar.py` (ET sessions), `gap_audit.py`, `weekly.py` (ISO aggregation, completed-week rule) from booklet 15 P4 — these are strategy-agnostic plumbing and stay.
- **New**: weekly bar persistence for the pulled window (today `market_bars` holds daily only), `EARNINGS` calendar marks ingestion, case/frame/features tables (§10).

---

## 4. Simplification plan (do this FIRST, before any new strategy code)

Phase order is deliberate: **remove → stable base → then build**. No new strategy code ships on top of the old strategy pile.

### 4.1 Removal inventory (each line = one independently committable batch, contract-first)

| Area | Action | Notes |
|---|---|---|
| NFR wheel strategies (Strategy 1/2 v1 wheel phases, tactics CSP/CC/strangle/roll) | Remove strategy logic + UI | Keep generic position/draft views |
| Booklet 15 Strategy 3 shadow (`strategy3.py`, `s3-shadow-v1` Iris nodes) | Remove | Superseded by `sma-weekly-v1`; history kept in git |
| Options strategy surface: IV rank, DTE targets, option plans/choices, chain-driven drafts | Remove | `iv_marks`/`chain_marks` tables stay dormant (no migration deletion in simplification batches) |
| Advisor trade recommendations / mock signals | Remove | News lens stays |
| Guardian strategy coupling (escalation into strategies) | Strip to risk-monitor only if kept at all (D6) | Guardian as pure monitor is optional |
| AI news analysis / DSH sentiment strategy inputs | Remove strategy coupling | News display stays; DSH becomes unused → remove |
| Simulation engines unrelated to SMA cases | Remove | SMA case simulation is new code |
| Unrelated risk settings (IV/DTE/strangle/tactic flags) | Remove from settings UI + payload | Migration for payload cleanup only with its own contract |

### 4.2 What remains after simplification (the "base")

Ticker monitoring (prices, news lens), positions/orders/drafts/approvals, account controls, settings toggles, Iris shell with event animation, backups, Telegram. **Zero strategies.** This base is the verified checkpoint before SMA work starts.

### 4.3 Removal safety

- Each removal batch: full test suite + honest test-count delta, no behavior claims without evidence, ledger entry, independent rollback.
- No table drops in simplification batches; orphaned tables are listed in a debt register and dropped (if ever) in a separate migration batch.

---

## 5. New strategy components

### 5.1 Case engine

- **BacktestScanner**: scans the pulled 2-year window per suitable symbol, generates historical cases with full frame sequences and labels. This seeds the library before any live signal is trusted.
- **LiveScanner**: runs at end of US market day: (a) advance open cases (new frame, sub-phase detection, SUCCESS check); (b) detect new `WATCH`/`SETUP`/`TRIGGERED` across the universe; (c) write completed cases into the library.
- Case = directory of record: `meta.json` (labels, versions, params), frame series (DB), features vector series (DB), charts rendered on demand from stored parameters (no PNG hoarding).

### 5.2 Similarity engine (frame-exact, §6)

Runs on the daily run and on-demand from the console. Produces Top-10 with per-case score breakdown, `match_confidence`, and the forecast aggregate (§5.4). Minimum library size gate: similarity outputs stay `insufficient_library` until ≥ 50 completed cases (recommendation; D5).

### 5.3 AI agent (kept, strictly bounded)

- Input: the current case's computed facts (state, similarity Top-10 stats, forecast distribution, risk numbers, red flags) — **never** raw freedom to invent levels.
- Output: a short verbal recommendation on the SMA20 entry signal: what the shape resembles, what the matched cases did, what the risk numbers say, what to watch next. No numeric invention; all numbers quoted from the engine.
- Forbidden: any other strategy advice, news-based trading calls, price targets not derived from the case aggregate.

### 5.4 Forecast

- Primary: from Top-10 matched cases' futures — distribution of time-to-SMA50 (median + IQR), expected RE_TEST/GRIND counts, MAE distribution, R-multiple distribution.
- Secondary (screen fan lines for the next days/weeks): linear extension of SMA slopes + residual bootstrap on the symbol's own weekly returns (5/50/95 percentiles). Displayed as **fan**, never a single line.
- Derived outputs: expected holding period, take-profit zone (SMA50 path, using only values known at each frame), suggested stop from matched-case MAE distribution, expected R weighted by similarity, confidence (`LOW/MID/HIGH` from match count + dispersion).

---

## 6. Frame comparison contract (the core visual asset)

### 6.1 Frame definition

- A **frame = one trading day of a case**, starting at `SETUP` (case-day 0). Each frame stores daily OHLCV + the weekly-SMA context known at that day + normalized feature vector.
- `T0` anchor for alignment = `TRIGGERED` day when it exists; pre-trigger frames numbered negatively. (If the operator prefers anchoring at SETUP, say so — D2.)
- Cases run on **trading-day indices** (case-day N), not calendar dates and not week numbers. "Today is the 4th day of the case" ⇒ live case has 4 frames.

### 6.2 Normalization (cross-symbol comparability)

Per-frame vector, all relative — no dollar amounts:

```
(close − SMA20)/ATR,  (SMA20 − SMA50)/SMA50,
SMA20_slope_3w/ATR,   SMA50_slope_3w/ATR,
volume/SMA20_volume,  (high − low)/ATR,
close_position_in_range,  RSI14
```

### 6.3 Matching

- Compare the live case's frames `0..N` against each library case's frames `0..N` (**overlap only; library futures never influence the match**).
- Per-frame cosine distance; weighted mean with recency weights (recent frames matter more; trigger-adjacent frames double-weighted).
- Optional ±2-frame sliding window to absorb small rhythm offsets (replaces DTW; simpler, day-exact by default).
- Score breakdown exposed per case; `match_confidence` = separation between Top-1 and the rest.

### 6.4 Rendering (ghost-car comparison)

- **Top**: the live case, N frames, full color.
- **Bottom**: Top-10 similar cases, each rendered with **exactly the same N-frame window and the same aspect ratio/scale** as the live card (identical axes normalization so shapes are visually comparable).
- Each library card additionally shows its **future frames (N+1 … completion)** as **dashed/ghosted** lines — the "ghost car" ahead. Ghost opacity fixed (e.g. 35%), visually distinct from the solid past.
- Cards animate on new frames via the existing SSE event fabric; no fake motion — movement only on real events.
- Clicking a library card opens that historical case's full path with its labels and outcome stats.

---

## 7. Risk, exposure, accounts (kept but cut to this strategy)

### 7.1 Per-signal math

```
Notional = entry × qty
Risk     = |entry − stop| × qty        # 1R, stop from matched-case MAE distribution
Heat     = Risk / NAV
BetaNot  = Notional × β(SPY)
qty      = floor((NAV × target_heat) / |entry − stop|)
```

### 7.2 Portfolio caps (proposed defaults, all settings-togglable)

- Per-signal heat default 1% NAV (D4); tag caps: sector ≤ 8–10%, theme ≤ 6–8%, total heat ≤ 20–25%, beta-weighted ≤ 30%.
- Red flags on every request: earnings within N days, VIX > 30, same-tag exposure already present, borrow fee high, gap risk.
- Forecast discounts: `E[R] × VIX factor × earnings factor` (VIX>30 → 0.5, earnings week → 0.7).
- Score for ranking: `E[R] × confidence / Heat × fit`.

### 7.3 TradeRequest (reuses existing drafts/approvals)

Symbol, direction, qty (risk-derived), entry method (GTC limit at pullback level vs market — D1/D3), stop price + type, TP zone (SMA50), Top-3 match summary + conditional stats + expected R + expected duration, before/after portfolio heat/beta, red-flag list. Approve/amend/reject stays exactly as today (every exit its own approval, per prior ruling).

### 7.4 Accounts

- Strategy toggle per account (3 accounts), any subset enabled; operator default: **account 3 only** once available.
- Per-account NAV and heat budgets; account isolation rules unchanged.

### 7.5 Removed risk surface

IV-rank minimums, DTE bands, strangle/tactic parameters, option-margin percentages, roll rules, profit-take percentages of option tactics, and every other non-SMA risk knob.

---

## 8. Daily operating loop and realtime

- **Main run**: after US close (e.g. 17:00 ET, ET-calendar anchored, DST-safe via existing scheduler patterns): pull tails → rebuild week bar → gap audit → advance cases → new signals → similarity → forecast → risk → TradeRequests → notifications.
- **Intraday (kept)**: existing event stream updates the current day → week-to-date bar → preview frame on screens, with forecast fan lines for the coming days. Preview values are visually marked as preview; they never trigger state transitions.
- **Weekly report**: open-case progress, new signals, Top-10 matches, portfolio heat dashboard, completed-case review with conditional-stat drift.

---

## 9. Console / visual spec

1. **Strategy panel** (new Iris page section): one card per symbol currently in `WATCH/SETUP/TRIGGERED/IN_TRADE` — state chip, case-day, predicted time-to-SMA50 (median + IQR), TP zone, risk/heat, expected R, confidence, red flags. Animated state transitions on real events only.
2. **Case detail** (click a card): comparison canvas per §6.4 (live on top, 10 ghost cards below, same ratio), frame scrubber, per-frame numbers drawer, similarity score breakdown, forecast fan chart.
3. **Library browser**: completed cases filterable by labels (path/duration/hill/environment/narrative), each with its frame film and outcome stats.
4. **Suitability list**: every universe symbol with its gate verdicts and reason codes.
5. Empty/honest states everywhere: `insufficient_library`, `no_data`, `adjustment_unavailable`, `no_borrow` — never fabricated values.

---

## 10. Data model (new tables only after ruling; migrations numbered at implementation time, no pre-allocation)

| Table | Contents |
|---|---|
| `sma_cases` | case id, symbol, setup/trigger/close dates, state, labels[], versions (`sma-weekly-v1`, factor version), adjustment_basis, outcome stats |
| `sma_case_frames` | case id, case-day index, daily OHLCV, weekly-SMA context snapshot, normalized vector, preview flag |
| `sma_case_features` | precomputed hill/touch/retest/grind descriptors per case |
| `sma_matches` | daily snapshot: live case → Top-N library case ids, scores, confidence |
| `sma_signals` | TradeRequest payloads linked to existing drafts/orders |
| `sma_universe_verdicts` | daily suitability gates per symbol |

Reuse: `market_bars`, `calendar_marks`, `strategy_events`, drafts/approvals/orders, settings/accounts. News tables untouched.

---

## 11. Decision points for the operator (recommended defaults; answer "use recommendations" to accept all)

| # | Question | Recommended default |
|---|---|---|
| D1 | Instrument: short stock (per the strategy discussion) vs capped-risk put spread? | **Short stock**, borrow fee + shortability gates on; put spread as a later version |
| D2 | Frame anchor: `TRIGGERED` day vs `SETUP` day? | **TRIGGERED** when it exists, pre-trigger frames negative; SETUP anchor fallback before trigger |
| D3 | Entry execution: next-day market vs GTC limit at pullback level? | **GTC limit at pullback level** (your habit), market fallback flag per request |
| D4 | Per-signal heat | **1% NAV**, stop from matched-case MAE distribution |
| D5 | Minimum library before similarity drives signals | **50 completed cases**; below that, signals render `insufficient_library` and no TradeRequest is emitted |
| D6 | Guardian: keep as pure risk monitor or remove? | **Keep as monitor only**, strip all strategy coupling |
| D7 | History depth: 2y as requested, or 2y now + optional 5y backfill later? | **2y now**, 5y as a separate data batch if library < D5 |
| D8 | Does the old `eval-v1`/`s3-shadow-v1` removal need its own sign-off? | Treated as **approved by this booklet's approval** (it is the explicit "forget the old frozen strategy 3" directive) |

---

## 12. Phased order (each phase = contract → implementation → verification → independent commit/rollback)

| Phase | Content | Exit gate |
|---|---|---|
| **S0** | Simplification: §4 removal batches until only the base remains | Base runs with zero strategies; full suite green; honest count deltas |
| **S1** | Data layer: IBKR weekly/daily pull for universe, persistence, earnings marks, suitability verdicts | Live DB has real bars; suitability list visible; read-only dry-run then persisted with backup |
| **S2** | Case engine: BacktestScanner + case/frame/features storage + chart rendering | Historical library built from real data; case film viewable |
| **S3** | LiveScanner daily run + case completion into library + strategy panel | End-of-day run advances real cases; panel shows live states |
| **S4** | Similarity + forecast + ghost-car comparison canvas | Frame-exact Top-10 with ghost futures; forecast aggregates from real matches; `insufficient_library` honesty below D5 |
| **S5** | Risk/exposure + TradeRequest into approvals + per-account toggles (account 3 first) | Full request→approval chain on real signal; zero orders without approval |
| **S6** | AI verbal recommender (§5.3) + weekly report | Verbal output strictly quoting engine numbers |

Hard rules carried forward: no look-ahead (entry = next open / futures never leak into matches), survivorship care in the universe pool, adjusted prices only, no orders without approval, independent rollback per batch, ledger + memory per batch.

---

## 13. Open risks / things you haven't listed (for discussion, not blocking)

1. **Library cold start**: 2 years × ~20–40 symbols yields few dozen cases; D5's 50-case gate may force the 5y backfill (D7) earlier than expected.
2. **Short-specific costs**: borrow recall/fee spikes and buy-ins are real path events; the case engine should record them when short stock is the instrument.
3. **Earnings marks are currently absent** in the live DB — S1 must build this ingestion or earnings red flags stay `unknown`.
4. **Preview discipline**: intraday week-to-date preview must never leak into SMA state transitions (a known failure mode of "live-feeling" tools).
5. **Similarity overfit**: with small libraries, Top-10 confidence will be noisy early; confidence must be displayed, not hidden.
6. **Operational**: IB Gateway scheduling and the two known startup blockers (load_active ordering, backup prune) remain a separate ops round; S1 needs a documented gateway runbook.
7. **Branch**: all SMA work happens on `strategy/sma20-sma50-crossover`; simplification batches S0 are proposed to run on the same branch to keep main stable until S0+S1 land.

---

*Product design reference; confirm implementation details against compliance and operational requirements before execution.*
