# 10 — Implementation gap review

**主计划**：[00-plan-order.md](00-plan-order.md) ｜ **规格**：[../athena/03-strategy-news-frontrun.md](../athena/03-strategy-news-frontrun.md)
**状态**：Done. This review is closed. The holes below are built, or left because they already match the spec.

The landed path matches the strategy spec: code writes one opening ticket, phases do not, `RV_HIGH` is the vol gate, and `estimated_iv()` stays empty. Two dates stay noted and are not open work in this booklet: a `chain_bs` rank after 20 same-method rows, and the March 2027 return to US daylight time. Known design choices that already match the spec are listed at the end so they are not reopened by accident.

## Holes that drop a legal order

### Keep the share buy

Built. A paired `dip_long` is `BUY` then `OPEN_CSP` in [wheel.py](../../packages/strategy-nfr/src/athena_strategy/wheel.py) `decide_opening` and `execute_opening`. The short-vol floor and an off-menu narrator strike remove the put. The buy stays.

**What the pass writes**

- The put is removed. The buy is the only draft, on one ticket, reason `dip_long`. `state_after` is projected from that buy alone.
- Two `decision_trails` rows, same symbol, source `wheel`. The buy row is `drafted` / `dip_long`. The put row has no draft and no ticket.
- A removed put uses these words. `quiet` stays the word for a pass that had nothing to draft (`not_a_dip` and the other miss reasons).
  - RV below `min_rv_pctl_new_short`: `dropped` / `rv_percentile`.
  - RV samples missing: `no_data` / `rv_samples`. The floor was not judged.
  - Off-menu narrator strike: `dropped` / `narrator_overruled`.
- A short-only ticket (`dip_csp`, `high_cc`, `signaled_strangle`) is still one row and no draft. Low RV is `dropped` / `rv_percentile`. Missing samples stay `no_data` / `rv_samples`. An off-menu strike with no share leg stays one `dropped` / `narrator_overruled` row.
- A bare `BUY` (`cover_gap`, or `dip_long` with no put) does not pass through the short-vol floor.

**Acceptance**

- Paired `dip_long`, RV percentile under the floor: one pending `BUY` draft, no `OPEN_CSP` draft. Trails are (`drafted`, `dip_long`) and (`dropped`, `rv_percentile`).
- Same ticket, no RV samples: the same buy draft. The put trail is (`no_data`, `rv_samples`).
- Same ticket, narrator strike not on the put menu: the same buy draft. The put trail is (`dropped`, `narrator_overruled`).
- Income name, short put only, percentile 39: no draft. One trail (`dropped`, `rv_percentile`). Percentile 41 still opens the put. This replaces the assertion in `test_rv_percentile_39_stays_quiet_and_41_opens` in [test_wheel.py](../../apps/orchestrator/tests/test_wheel.py).
- Income name, no RV history: still (`no_data`, `rv_samples`) and no draft (`test_a_short_without_rv_history_is_not_a_draft`).
- Income name, off-menu strike: still one `narrator_overruled` row and no draft (`test_an_off_list_strike_is_overruled`).
- Audit result badges stay capitals with underscores: `DIP_LONG`, `RV_PERCENTILE`, `RV_SAMPLES`, `NARRATOR_OVERRULED`.

**The legacy switch is not a cycle path.** Built. `ATHENA_STATE_MACHINE` stays on. `run_signaled` writes the wheel ticket. Setting the flag to off does not restore the seven-agent pass, so a flat book cannot queue an uncovered short call from that pass.

**An open and a roll do not land on the same name in one pass.** Built. The cycle drafts `manage_book` before the opening ticket. `decide_opening` stays quiet with `manage_pending` while a `manage_roll` or `manage_close` draft is still pending for that name.

## Holes in the money checks

**Assignment cash is counted once.** Built. A short put’s strike is the cash set aside and the assignment add. The floor test uses that strike once: cash already set aside in this pass is added back, then the strike and the existing assignment are subtracted. A put that still leaves `min_cash_after_assign_pct` stays pending.

**A name with no classifier skips the 35% bucket.** Built. An empty classifier list is the `UNCLASSIFIED` bucket in [athena_risk](../../packages/risk/src/athena_risk/__init__.py) `bucket_names`. Stock already held and a new ticket both count there, and `max_bucket_pct_nav` applies. A name that has a classifier stays in those buckets only. A small unclassified open under the cap stays pending.

**A missing FX rate or mark skips the check.** Built. A live book blocks the ticket. The reason is `missing fx rate USD` (the source currency) or `missing mark`. Symbol notional is converted into the book currency before the cap. A known breach, such as the bucket, is still named when that check can run. `open_quantity` does not treat a missing price as one contract when the position cap is on.

**A new covered call does not add share notional to the bucket.** Built. The shares are already in the bucket at the stock’s own average cost. A call can be opened only against stock already held, and a call that has not filled does not change that cost. `_bucket_amount` stays zero for `OPEN_CC`. Adding the covered shares again would count them twice.

**One bad ticket blocks its whole priority band.** Built. `walk_cycle` still sorts closes and rolls, then `dip_long`, then `high_cc`, then the rest, and symbol order inside a rank. Each ticket is judged on the running book. A failure blocks that ticket only. A ticket that fits is absorbed before the next one is judged.

## Strategy shortfalls

**True IV is still not a reason to trade.** Built for the wing cap. `estimated_iv()` stays empty, and the RV multiple is still an assumption. The floor, the ceiling, the two RV band edges, and the wing factor are risk limits. A new book ceilings at 40%. A stored row keeps the ceiling it already saved. A ceiling of 0 removes the cap, so a high `RV20` follows vol, and a clamp still writes `floor` or `ceiling` on the audit. `chain_bs` stays a badge. Ranking richness from `chain_bs` waits until 20 same-method rows exist.

**The menu cannot see a wide market.** Built. The chain pull stores the bid from the same delayed ticker as the ask. Open interest, volume, and `iv` stay empty. Spread is `(ask − bid) / mid`. `max_spread_pct` defaults to 20 on the risk limits, and zero keeps every quoted strike. A wider pair stays in `option_choices` and does not become a menu line. A missing bid stays, with liquidity `unknown`. A bid inside the limit is `quoted`.

**Rails move on today’s delayed close.** Built. `daily_bars` keeps completed `ib-daily` bars only. A delayed print does not replace that close and does not add an unfinished day, so `rail_pos`, drawdown, and `RV20` stay on the last completed session. `last_tradeable_price` is still the delayed print, and that is the limit price.

**US hours will be wrong after 1 Nov 2026.** Built for this Sunday. The stored leg stays `21:30–04:00`. Friday 30 Oct still ends Saturday 31 Oct at 04:00. From Monday 2 Nov 2026 that exact leg is read as `22:30–05:00`. HK cash and Mobile stay put. A stored window that is not that pair is left alone. The March 2027 return to daylight time is a later dated edit.

**A flat book cannot open the two-leg short.** Left. This matches the spec. `signaled_strangle` requires at least 100 shares. `FLAT` can only `dip_csp` or `dip_long`. A flat short strangle would need its own reason, and the cover rule would not apply to that put.

**A naked call cannot be repaired by a buy.** Left. This matches the spec. `NAKED_CALL` is dropped before `_choose`. The spec says open nothing. A later improvisation would be a cover ticket of `100 − shares` with no new short, the same shape as `cover_gap`.

**The first opening of the day blocks a better one.** Built. Rank is `signaled_strangle`, then `high_cc`, then `cover_gap`, then `dip_long` and `dip_csp` tied. A strictly higher reason replaces a ticket whose drafts are still pending, and those drafts are blocked with `replaced by {reason}`. An approved draft, or an order that is queued, submitted, or filled, keeps the day. A blocked ticket does not. An equal rank leaves the pending ticket.

## Already consistent, leave them

Odd-lot `cover_gap` buys the remainder and does not attach the call. `dip_long` may be shares only when `RV_HIGH` or a put is missing. `HIGH_IV` is not produced. Replay does not write `chain_marks`. Saturday 02:00 HKT can draft because it is still Friday’s US session, while default observe stays weekday-only. Profile cards fill the ten exposure fields and leave bucket and assignment caps as they were. Short history renames the range tag to `HIGH_RANGE` and still allows a draft.
