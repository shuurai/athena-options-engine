# 07 — News Intake

## Role in Athena

News is a **first-class input** to the News Front-Running Cycle: it drives `NEWS_WASH`, event calendars, and phase transitions. It is not a decorative feed.

Two detection goals:

1. **Legitimate institutional / government / company news** — Fed, regulators, earnings, filings  
2. **Adversarial / short-driven narrative pressure** (“shorter’s strategy news”) — clustered negative spin that may precede or amplify washouts  

Both become structured `news_events` for the tag engine + DSH sentiment assist.

## Event object (target schema)

```json
{
  "event_id": "uuid",
  "timestamp": "ISO-8601",
  "symbols": ["NVDA"],
  "source": "yahoo|finviz|fed|ib|msn|manual|...",
  "event_type": "FED_RATE_DECISION|EARNINGS_RELEASE|ANTITRUST_FINE|AI_REGULATION|SUPPLY_CHAIN|GENERIC_NEGATIVE|GENERIC_POSITIVE|NARRATIVE_ATTACK",
  "sentiment": -1.0,
  "severity": 1,
  "credibility": 0.0,
  "raw_title": "...",
  "raw_url": "...",
  "llm_summary": "..."
}
```

- **sentiment:** −1.0 … +1.0 (DSH/LLM assisted)  
- **severity:** 1–5  
- **credibility:** 0–1 (source tier; social starts lower)

## Source tiers (phased)

### Tier A — MVP (ToS-friendly / API/RSS)

| Source | Use |
|--------|-----|
| Yahoo Finance news | Broad equity headlines |
| Finviz news/headlines | Dense header scan for watchlist |
| Fed / FOMC calendar | Macro T dates |
| Earnings calendars | EARNINGS_NEAR |
| IB news (if available on account) | Broker-side headlines |
| Manual / bot paste | You forward a link; parser ingests |

### Tier B — Expand when stable

| Source | Use |
|--------|-----|
| MSN / Edge finance surfaces | Retail narrative echo |
| Search engine alerts | Query packs per watchlist |
| Curated RSS (WSJ/FT if licensed) | High credibility |

### Tier C — Design hooks only (later; ToS / legal review)

| Source | Risk |
|--------|------|
| WeChat public accounts | Scraping/ToS; prefer official APIs or manual forward |
| Rednote (小红书) | Same |
| TikTok | Same |

**Policy:** Athena must support a `manual_ingest` and `user_forward` path so you can drop social posts into the pipeline without building banned scrapers on day one. Automated Tier C only after explicit compliance review.

## NEWS_WASH detection (sketch)

Settings-driven, e.g.:

- ≥ N negative events (sentiment < −0.3) on symbol in M days  
- Multiple low-credibility sources repeating same narrative → raise `NARRATIVE_ATTACK` weight  
- High-credibility gov/company release → separate path (may be P3 Event, not wash)

Wash alone does **not** auto-trade at default privilege; it moves phase/tags and may draft a Strangle for Approve.

## DSH role

- Optional enrichment: summarize cluster, score sentiment/severity, flag “coordinated negative narrative?”  
- Pattern: headless `dsh` worker (fanit/fanbao style) — analysis sidecar, **not** SoR  
- Hard rules + calendar still work if DSH is down  

## Dedup and storage

- Fingerprint by URL + normalized title  
- Store raw + structured; retain enough history for density windows  
- Link events to symbols via ticker map + LLM assist with human confirm on ambiguous names  

## Non-goals for news MVP

- Full multimedia OCR of every TikTok  
- Trading solely on a single unverified social post  
- Guaranteed “front-run” of news (illegal insider framing avoided; we mean **public** narrative cycle detection only)  
