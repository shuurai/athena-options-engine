# 12 — Short-horizon forecast R&D (bid / ask / stop / TP)

**Status:** Prepared in Athena (default **off**) — OpenAI-protocol LLM serve assumed done  
**Date:** 2026-10-14  
**Privilege:** Advisory only — never replaces risk rails or Approve  
**Serve:** OpenAI `/v1/chat/completions` (Settings → Prediction)  
**Train box:** Local RTX 5090 (offline; optional vs LLM serve)  
**Related:** [07-news-intake.md](07-news-intake.md), [02-architecture.md](02-architecture.md), [09-roadmap.md](09-roadmap.md), [04-settings-and-risk.md](04-settings-and-risk.md)  
**Upstream method template:** [shuurai2000/shingan-finrisk-labels](https://huggingface.co/datasets/shuurai2000/shingan-finrisk-labels) (Shingan / 心眼)

---

## North star

Predict the next **H-bar return distribution** for a symbol, conditioned on news/events and cross-asset context, then set **bid/ask** and **stop / take-profit** from calibrated quantiles — with hard risk gates and human Approve still in front of live money.

| Knob | Choice |
|------|--------|
| Primary horizon H | 1–5 sessions (daily first; intraday later) |
| Target type | Quantile / distribution — not a point forecast |
| Athena privilege | Advisory / shadow → paper HITL |
| Hardware | Local 5090 for train; light sidecar for serve |

---

## Why this sits next to Athena (not inside DECIDE)

Athena already owns strategy, risk, and HITL. This work is a **sidecar advisor**, not a new decision authority.

| Keep as code / settings | Add as advisory model service |
|-------------------------|-------------------------------|
| Cash reserve, day-order caps, contract/position caps, DTE, earnings blackout | Suggested limit vs mid / last |
| Phase machine, draft → Approve → EXEC | Suggested stop and take-profit levels |
| News → tags / NEWS_WASH (first-class) | Confidence / regime flag in UI Detail |
| | Shadow mode first; never auto-size risk |

The model **must never** raise `max_contracts`, bypass cash reserve, or skip Approve.

---

## Bridge from Shingan FinRisk → short-horizon α

[Shingan FinRisk labels](https://huggingface.co/datasets/shuurai2000/shingan-finrisk-labels) is the right **methodology** template (as-of discipline, purge/embargo, dual-track GBDT + text, rare-event metrics) but the wrong **prediction problem** for bid/ask.

| Dimension | Shingan FinRisk (now) | This R&D (target) |
|-----------|----------------------|-------------------|
| Horizon | 30d tail / ~1y credit-style | Intraday → 1–5 sessions (H bars) |
| Label | Binary rare risk (e.g. tail −30%) | Return quantile / volatility / TP–SL hit |
| Text | Counts + separate QLoRA on filings/news | Event embeddings + `news_events` join |
| Prices | Single-name + VIX / credit proxies | Equity + ETF + FX + commodity (+ crypto later) |
| Output use | Risk screen / ranking | Limit offset, stop, take-profit proposals |
| Eval | PR-AUC, KS, calibration | IC, pinball loss, fill rate, slippage, R-multiple |
| Reuse | Feature pipeline + as-of + split hygiene | Same hygiene; **new labels & heads** |

**Reuse, do not retrain the same head.** Keep Shingan as a parallel risk track (widen stops / shrink size when tail-risk score is high). Short-horizon pricing needs a new label book and a distributional head.

---

## Problem formulation

### Predict (at decision time `t`, for symbol `s`)

| Symbol | Meaning | Maps to trading knob |
|--------|---------|----------------------|
| `q_p(r[t→t+H])` | Return quantiles `p ∈ {0.05, 0.25, 0.5, 0.75, 0.95}` | Stop / TP / mid target |
| `sigma_H` | Horizon volatility (realized or implied proxy) | Spread width / confidence |
| `P(hit TP before SL)` | Path-aware optional head | R:R and whether to quote at all |
| `dir_conf` | Calibrated `P(r > 0)` | Buy vs sell aggressiveness |

### From quantiles → bid / ask / stop / TP (example policy)

1. **Limit offset** from mid ∝ `clamp(q_0.5 − mid_return_proxy, ±k · sigma_H)`, with liquidity/spread floors from IB bid–ask when available.
2. **Stop** ≈ entry + `q_0.05` (long) / entry + `q_0.95` (short), floored by ATR / max-loss settings.
3. **Take-profit** ≈ entry + `q_0.75` (long), with optional trailing once MFE exceeds a threshold.
4. All clipped by Settings risk; model never widens caps.

---

## Data streams

### Stream A — Text / events

- Athena `news_events` (Tier A): Yahoo / Finviz / Fed / earnings / manual URL — see [07-news-intake.md](07-news-intake.md)
- Company majors: 8-K / earnings transcripts / guidance (as-of by filing time)
- Macro calendar: FOMC, CPI, NFP as event tokens
- Features: embedding of last K events + type / sentiment / credibility aggregates
- **As-of rule (same as Shingan):** no post-`t` text ever enters features

### Stream B — Multi-asset prices

- Target: Athena watchlist bars (`ib-daily` / delayed; later intraday)
- Context basket: SPY/QQQ, sector ETF, USD index / major FX, gold/oil
- Optional later: BTC/ETH as risk-on proxy (separate exchange clock — do not naively merge sessions)
- Features: returns, RV, ATR, beta, relative strength, lead–lag to basket
- **Start** daily `H ∈ {1, 3, 5}`; only then promote to hourly/minute

---

## Model architecture (5090-sized, staged)

| Stage | Model | Why | 5090 fit |
|-------|--------|-----|----------|
| 0 — Baseline | LightGBM / CatBoost on tabular (price + news aggregates) | Fast IC / pinball baseline; easy purge CV | CPU fine; GPU optional |
| 1 — Dual track | Tabular GBDT + frozen text encoder (QLoRA or small BERT/FinBERT) | Reuse Shingan pattern; prove text adds IC | QLoRA ~7B comfortable on 32GB |
| 2 — Sequence | Temporal fusion / PatchTST / small Transformer on multi-asset windows | Lead–lag without LLM cost at inference | Train locally; export ONNX / TorchScript |
| 3 — Optional fusion | Late fusion: text emb + price seq → quantile heads | Only if stages 1–2 show real lift | Prefer distillation for serve |

**Do not start with end-to-end LLM price prediction.** LLMs are weak at calibrated short-horizon prices. Use them for event structuring (Athena DSH already does) and embeddings; put the money head on tabular / sequence models.

---

## Label book and evaluation

### Train labels

- `r_H = close[t+H] / close[t] − 1` (and open-to-open variants)
- Quantile regression / pinball loss on `q_p`
- Aux: realized vol over H; max adverse excursion proxy
- Split: time-ordered + purge / embargo (Shingan hygiene)

### Go / no-go metrics

- Rank IC vs naive (momentum / RV) on purged test
- Pinball + calibration of `q_0.05` / `q_0.95`
- Shadow: fill rate and slippage vs current limit heuristic
- **Kill** if IC ≤ 0 after costs for two consecutive test windows

---

## Phased R&D plan

| Phase | Weeks | Deliverable | Exit gate |
|-------|-------|-------------|-----------|
| P0 Spec & dataset | 1–2 | Label book, universe, as-of joins, Athena export of bars + `news_events` | Leakage tests pass; one CSV/Parquet train set |
| P1 Tabular baseline | 2–3 | GBDT quantile model; notebook + metrics | IC / pinball beat naive on holdout |
| P2 Text lift | 2–3 | Event embeddings + dual-track; ablate news on/off | Significant lift **or** drop text |
| P3 Cross-asset | 2 | Basket features / small sequence model | Lift on semis / high-beta Athena names |
| P4 Shadow in Athena | 2–3 | Inference sidecar; write `suggested_limit` / stop / tp onto draft Detail | Operators trust numbers; no live auto |
| P5 Paper HITL | ongoing | Approve still required; compare fills vs shadow | Slippage / R-multiple beat control cohort |

---

## Athena integration (prepared)

**Live surface (default off):**

| Touchpoint | Behavior |
|------------|----------|
| Settings → **Prediction** | `enabled` (false), OpenAI `base_url`, token, model, max tokens, stop lines, temperature, timeout, reasoning |
| Orchestration → **Prediction** prompt | System + execution templates; JSON-only bid/ask/stop/TP/risks for H∈{1,3,5} |
| Wheel cycle | When enabled: call forecast after tags; write `ai_calls` + `forecast_marks` + `decision_trails.steps.forecast` |
| Risk / Approve / limits | Unchanged — shadow only in this prepare; no auto `limit_price` |

**Canonical shadow JSON** (strict parse in `athena_strategy.forecast`):

`symbol`, `as_of`, `model`, `horizons.{1,3,5}.{bid,ask,stop,take_profit,dir_conf}`, `risks[{code,note}]`.

Offline train packages remain optional:

- `packages/forecast` — dataset / train (not required when the LLM serve already exists)
- Sidecar may be any OpenAI-compatible URL pointed at from Settings

---

## Risks and non-goals

### Hard risks

- Lookahead via news timestamps / adjusted closes
- Regime break → silent IC collapse (needs live monitoring)
- Options: underlying forecast ≠ option mid; consume `chain_marks` later
- Crypto clock / weekend gap if mixed with equity sessions wrongly

### Explicit non-goals (v1)

- HFT / sub-second market making
- Unattended `auto_within_risk` from the model alone
- Replacing wheel phase / tactic selection
- Training on scraped Tier-C social without compliance review

---

## First two weeks (concrete)

1. Freeze `H ∈ {1, 3, 5}` sessions and universe = Athena watchlist + ~8 context symbols.
2. Build as-of panel: bars + `news_events` aggregates (reuse Athena Postgres).
3. Train LightGBM multi-quantile; report IC and pinball vs mom/RV baseline.
4. Design draft Detail JSON schema for `suggested_limit` / `stop` / `tp` (shadow only).
5. Promote text dual-track **only if** P1 clears the gate.

---

## Bottom line

Treat this as a **calibrated short-horizon distributional forecaster** that advises Athena quotes and exits. Steal Shingan’s data hygiene and dual-track discipline; replace its rare-event risk head with quantile return heads; prove lift in shadow before any privilege change.

---

## Sources

- Athena: [07-news-intake.md](07-news-intake.md), [02-architecture.md](02-architecture.md), [09-roadmap.md](09-roadmap.md)
- Hugging Face: [shuurai2000/shingan-finrisk-labels](https://huggingface.co/datasets/shuurai2000/shingan-finrisk-labels)
