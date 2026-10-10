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
| `cash_reserve_pct` | 30% | CSP collateral buffer |
| `max_contracts_per_symbol` | 1–3 | Size |
| `max_position_per_symbol` | 0 | Money cap per symbol, in the ticker currency. Zero is off. One option contract is the latest price times the multiplier. A total within 10% still fits. When one contract is already above the cap, the contract limit is used. |
| `max_orders_per_day` | 10 | Activity cap |
| `daily_loss_halt_pct` | 5% | Pause trading |
| `min_iv_rank_new_short` | 20–50 | Block cheap premium sells |
| `min_dte_new` | 7 | Avoid gamma junk |
| `earnings_blackout_days` | 7 | No new around earnings (unless NFR exception flag) |
| `reject_streak_halt` | 3 | Pause after IB rejects |

Risk Guard runs **always**, independent of privilege.

Named profiles fill those limits. They do not change privilege.

| Profile | Name size | Cash floor | Contracts | IV rank floor | DTE floor |
|---------|-----------|------------|-----------|---------------|-----------|
| defensive | 10% | 40% | 1 | 60 | 21 |
| standard | 20% | 30% | 3 | 50 | 7 |
| income | 25% | 25% | 2 | 45 | 14 |
| aggressive | 35% | 15% | 4 | 30 | 7 |
| test | 80% | 5% | 12 | 0 | 1 |

**test** is for the paper account. It is the same Approve path and the same Athena slot. It does not send an order while the slot is idle. It also drops the IV rank sample minimum to 1, so a rank can exist before 20 marks. Switching back to standard restores the standard knobs and the sample minimum of 20. With no calendar event, names stay P1 HOLD.

### 2. Strategy (NFR params)

| Key | Role |
|-----|------|
| `phase_windows` | Days for P1–P5 relative to T |
| `news_wash_count` / `news_wash_days` | Density trigger |
| `ivr_high` / `ivr_low` | Tag thresholds |
| `strangle_otm_pct` | Put/call distance |
| `csp_buffer_pct` / `cc_buffer_pct` | Strike buffers |
| `dte_open_band` | e.g. 30–45 |
| `profit_take_pct` | e.g. 50% of max premium |
| `roll_dte` | e.g. 21 |
| `rsi_ob` / `rsi_os` | 70 / 30 |
| `support_lookback_days` | 20 |

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
| `athena_slots` | Cron/windows when Athena may hold API connection |
| `mobile_slots` | Windows reserved for Mobile IB App (Athena disconnects / idle) |
| `reconnect_backoff_sec` | After Mobile disconnect |
| `client_id` | Stable Athena client id |
| `account_ids` | List; `active_account_id` for MVP |

See [06-data-and-ib.md](06-data-and-ib.md).

### 5. Universe

| Key | Role |
|-----|------|
| `watchlist` | Symbols + tags overrides |
| `blocked_symbols` | Hard deny |
| `allow_csp_adr` | Default false (delisting caution) |

### 6. Backup / ops

| Key | Role |
|-----|------|
| `local_backup_cron` | e.g. hourly + daily |
| `s3_sync_enabled` | bool |
| `s3_prefix` | athena/prod/… |
| `retain_local_days` | Retention |

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
