# 04 — Settings and Risk

## Why Settings are first-class

Athena’s long-term edge is **refining one strategy**. That only works if risk limits, approval privilege, strategy knobs, IB slots, and notify prefs live in one **versioned, auditable Settings store** — not scattered env vars or hard-coded constants.

Env / OS secrets: API keys, DB URLs, bot tokens.  
Everything parametric: Settings.

## Settings package responsibilities

`packages/settings`:

- JSON Schema / Pydantic models for all knobs  
- DB table `settings_versions` (id, created_at, author, note, payload, active flag)  
- Load active bundle into orchestrator each loop (or on change notify)  
- Diff + audit log on every change  
- Protect privilege elevation with stronger confirmation  

## Approval privilege levels

| Level | Behavior |
|-------|----------|
| **`all_orders`** (default) | Every open, roll, and close requires Approve |
| **`opens_only`** | Opens need Approve; rolls/closes/risk exits may auto-run if risk passes |
| **`auto_within_risk`** | Auto-execute when risk guards pass; escalate exceptions / oversize / conflicts |
| **`paper_only`** | Not a separate mode. Paper already sends a real order on the paper account after Approve. Privilege stays `all_orders`. |

### Changing privilege

- Lowering privilege (more automation): still audited; recommend dual confirm when leaving `all_orders` or `paper_only`.  
- Raising automation (`opens_only` → `auto_within_risk`): **protected** — bot confirm + ephemeral web confirm (or two allowlisted users).  
- Emergency: `/halt` forces trading pause regardless of privilege.

## Settings domains

### 1. Risk

| Key | Example default | Role |
|-----|-----------------|------|
| `max_symbol_pct_nav` | 20% | Cap per underlying |
| `max_short_opt_margin_pct` | 50% | Seller margin budget |
| `cash_reserve_pct` | 30% | Post-trade excess liquidity floor vs NAV (IB ExcessLiquidity; TotalCashValue fallback) |
| `max_contracts_per_symbol` | 1–3 | Size |
| `max_position_per_symbol` | 0 | Money cap per symbol, in the ticker currency. Zero is off. One option contract is the latest price times the multiplier. A total within 10% still fits. When one contract is already above the cap, the contract limit is used. |
| `max_orders_per_day` | 10 | Activity cap |
| `daily_loss_halt_pct` | 5% | Pause trading |
| `min_rv_pctl_new_short` | 40–50 | A new short needs this percentile of the symbol's own `RV20`. Fewer than 20 samples does not pass. |
| `margin_rate` | 20 | Percent of notional used when a new short’s margin is estimated |
| `strict_conflict` | off | On means any existing short blocks every new open. Off allows a short put and a covered call on the same name |
| `max_bucket_pct_nav` | 35 | One classifier bucket, as a percent of NAV |
| `max_assign_pct_nav` | 40 | Assignment exposure cap. The proxy uses `assign_delta` (0.30) |
| `min_cash_after_assign_pct` | 10 | Cash that must remain after that assignment |
| `min_dte_new` | 7 | Avoid gamma junk |
| `earnings_blackout_days` | 7 | No new around earnings (unless NFR exception flag) |
| `reject_streak_halt` | 3 | Pause after IB rejects |

Named profiles fill those limits. They do not change privilege.

| Profile | Name size | Cash floor | Contracts | Short floor | DTE floor |
|---------|-----------|------------|-----------|---------------|-----------|
| defensive | 10% | 40% | 1 | 60 | 21 |
| standard | 20% | 30% | 3 | 50 | 7 |
| income | 25% | 25% | 2 | 45 | 14 |
| aggressive | 35% | 15% | 4 | 30 | 7 |
| paper | 25% | 20% | 5 | 40 | 21 |
| test | 80% | 5% | 12 | 0 | 1 |

The selectable cards are defensive, standard, income, aggressive, and paper. **paper** is the acceptance book: Approve still applies, the short floor is 40, and the position cap is 20,000 in the ticker currency. **test** can still be stored on an old row. It is not a card. A card fills the ten exposure fields. Bucket, assignment, and `strict_conflict` are not part of that identity. Any other mix is `custom`.

**test**, when a row still has it, is the same Approve path and the same Athena slot. It does not send an order while the slot is idle. It also drops the sample minimum to 1, so a rank can exist before 20 marks. Switching back to standard restores the standard knobs and the sample minimum of 20.

Risk Guard runs on every draft, independent of privilege:

- A covered call needs `floor(shares / 100)` contracts still free after short calls already held or submitted. The block text names the shares required and the shares held.
- A second short put is blocked. A strangle is blocked when an option leg is already open. A short put beside a covered call is allowed unless `strict_conflict` is on.
- A ticket’s cash need is the share buys plus each short put’s strike times its multiplier. The reserve gate uses excess liquidity (or TotalCashValue when EL is missing): block when `(buffer − need) / NAV` is under `cash_reserve_pct`. Every leg on the ticket is blocked. Missing marks are skipped.

### 2. Strategy (NFR params)

| Key | Role |
|-----|------|
| `phase_windows` | Days for P1–P5 relative to T |
| `news_wash_count` / `news_wash_days` | Density trigger |
| `ivr_high` / `ivr_low` | 80 / 20. Kept for old rows. They do not produce `HIGH_IV` or `LOW_IV` |
| `ivr_min_samples` | 20. A rank stays off until this many samples exist |
| `rv_window` | 20 sessions for `RV20` |
| `rv_high_pctl` / `rv_low_pctl` | 80 / 20. These produce `RV_HIGH` and `RV_LOW` |
| `chain_band_pct` | 25. How wide a strike menu to fetch, around the price |
| `screener_top_n` | 3. Fewer rows are shown as they are. None are invented |
| `csp_delta_target` / `csp_delta_lo` / `csp_delta_hi` | 0.25 / 0.10 / 0.40 |
| `cc_delta_target` | 0.30. The call strike must also clear the share cost |
| `strangle_delta_target` | 0.28 |
| `otm_floor_pct` / `otm_ceiling_pct` | Risk limits. 3 / 40 on a new book. The distance target is clamped here, and the clamp is audited. A ceiling of 0 removes the cap so the width follows realized vol. A stored row keeps the ceiling it already saved |
| `rv_band_high_pct` / `rv_band_mid_pct` | Risk limits. 45 / 25. These pick which assumed multiple sizes the wing |
| `wing_z` | Risk limit. 0.50. Scales the distance. An assumption, not a measurement |
| `iv_rv_multiple_hi` / `_mid` / `_lo` | 1.05 / 1.35 / 1.80 by the RV band. An assumption for picking a strike. Not implied volatility |
| `dte_min` / `dte_target` / `dte_max` | 21 / 35 / 60 |
| `profit_take_pct` | 50. Percent of premium that drafts a close |
| `roll_dte` | 21 |
| `rsi_overbought` / `rsi_oversold` | 70 / 30 |
| `level_lookback` | 20 sessions for support and resistance |
| `drawdown_band_high_pct` / `drawdown_band_low_pct` | 15 / 5 |
| `dip_deep_pos` / `dip_reclaim_pos` | 0.35 / 0.45 inside `dip_window_sessions` (5) |
| `regime_index` | `SPY`, over `regime_lookback` sessions (63) |
| `cc_rail_pos` | 0.80. The close must sit at least here before a covered call |
| `observe_slots` | `always`. Weekdays from 07:00 HKT through midnight. `athena` and `athena+mobile` follow the slot clock, so the Saturday morning US tail can be watched |
| `draft_slots` | `athena`. A ticket is written only inside an Athena window |

The old name `strangle_otm_pct` is not read. The old name `min_iv_rank_new_short` is not shown. The live field is `min_rv_pctl_new_short`.

The Volatility reading shows two badges, not inputs. `premium_ratio` is the mark written today: planned put credit plus call credit, divided by the stock price. `chain_bs` is the future chain method. It cannot be selected, and nothing reads `chain_marks` yet.

### 2b. Prediction (short-horizon forecast)

OpenAI-protocol advisory forecast. **Off by default.** See [12-short-horizon-forecast-rnd.md](12-short-horizon-forecast-rnd.md).

| Key | Default | Role |
|-----|---------|------|
| `prediction.enabled` | false | When on, the wheel may call the forecast endpoint (shadow only) |
| `prediction.base_url` | `http://127.0.0.1:8080/v1` | OpenAI root; Athena posts to `/chat/completions` |
| `prediction.api_token` | inherit / cleared | Bearer token (`ATHENA_PREDICTION_TOKEN` when unset) |
| `prediction.model` | `forecast` | Model id |
| `prediction.max_tokens` | 2048 | Completion budget |
| `prediction.stop` | `[]` | Generation stop sequences |
| `prediction.temperature` | 0 | Sampling temperature |
| `prediction.reasoning` | false | Optional reasoning / thinking flag |
| `prediction.timeout_seconds` | 30 | HTTP timeout |
| `prediction.horizons` | `[1, 3, 5]` | Sessions (locked for v1) |

Prompts live on Orchestration under **Prediction**. Output is stored on `forecast_marks` and `decision_trails.steps.forecast`. It does not size drafts or skip Approve.

### 3. Preference / UX

| Key | Role |
|-----|------|
| `notify_channels` | feishu / telegram / both |
| `notify_on` | draft, fill, reject, health, daily_summary |
| `draft_ttl_minutes` | Expire unapproved drafts |
| `language` | en / zh for bot copy |
| `timezone` | e.g. Asia/Hong_Kong |

### 4. IB session schedule

| Key | Role |
|-----|------|
| `athena_slots` | Default `HKT 09:30-12:00, 13:00-16:00, 21:30-04:00`. HK cash, then the US regular session in Hong Kong time. From Monday 2 Nov 2026 the stored `21:30-04:00` leg is read as `22:30-05:00` |
| `mobile_slots` | Default `HKT 19:00-21:30`. Athena disconnects |
| `reconnect_backoff_sec` | After Mobile disconnect |
| `client_id` | Stable Athena client id |
| `account_ids` | List; `active_account_id` for MVP |

See [06-data-and-ib.md](06-data-and-ib.md).

### 5. Universe

| Key | Role |
|-----|------|
| `watchlist` | Symbols Athena may draft |
| `blocked_symbols` | Hard deny |
| `tactic_flags` | Per name: `csp`, `cc`, `strangle`, `intent` (`income` / `both` / `accumulate`), `long`, and optional `may_buy` |
| `allow_csp_adr` | Default false (delisting caution) |

### 6. Backup / ops

| Key | Role |
|-----|------|
| `local_backup_cron` | e.g. hourly + daily |
| `s3_sync_enabled` | bool |
| `s3_prefix` | athena/prod/… |
| `retain_local_days` | Retention |

## Switches

These are environment flags, not settings versions. Default on, except dry run.

| Flag | Default | Off means |
|------|---------|-----------|
| `ATHENA_STATE_MACHINE` | always on | The flag does not restore the seven-agent pass |
| `ATHENA_SCREENER` | on | The strike menu is the old free-text lines |
| `ATHENA_INDICATORS` | on | The pass does not read the indicator numbers |
| `ATHENA_MAY_BUY` | on | An explicit `may_buy` is ignored. A buy needs Long on and an intent other than Income |
| `ATHENA_SLOT_SPLIT` | on | There is no observe-only pass. A signaled ticket is written only while the clock is in an Athena window |
| `ATHENA_PORTFOLIO_BUDGET` | on | The cycle walk does not block tickets |
| `ATHENA_PANEL` | on | `/book` says charts are off |
| `ATHENA_DRY_RUN` | off | On writes the trail as `observed` and does not leave a pending ticket |

## Validation rules

- Schema validate on write; reject unknown keys in strict mode  
- Cross-field checks (e.g. `cash_reserve_pct` + CSP size feasible)  
- Paper mode sends a real order only on the paper account, and only after Approve inside the Athena slot  

## Suggested UI surfaces

1. **Bot:** `/settings get risk` / `/settings set …` for small knobs  
2. **Ephemeral web:** full form for privilege change + multi-field edits  
3. **Local dashboard (later):** optional; not MVP-blocking  

## Audit record (every change)

```json
{
  "settings_version": 42,
  "author": "telegram:123",
  "note": "Raise IVR floor before FOMC",
  "diff": [{"path": "strategy.ivr_high", "from": 70, "to": 75}],
  "confirmations": ["telegram", "web_token:abc"]
}
```

## Interaction with fanit pricing rules

fanit `PRICING_RULES` / wheel docs are **reference defaults** when seeding Athena Settings — not a live dependency. Athena Settings become the runtime source of truth for the engine.
