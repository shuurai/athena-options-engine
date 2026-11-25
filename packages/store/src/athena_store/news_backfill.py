"""V2-P3 N1: idempotent backfill of legacy news rows into the canonical model.

§6.3 N1 is additive: ``news_events`` is neither modified nor dropped. Every
legacy fingerprint gets exactly one immutable map row.

Two honesty rules drive the shape of this module:

1. Backfilled associations are labelled ``ingest_fallback``. The legacy table
   has no provenance column, so claiming a provider timestamp would be a lie.
   The raw inherited instant is still preserved in ``original_ts``.
2. During N1/N2 the legacy table is the truth source, so a re-run **re-derives**
   (overwrites) the mutable association columns from legacy. Without that, a
   DSH sentiment enrich followed by a re-backfill would leave the canonical row
   stale and break §6.3 item ④. The immutable map row is never rewritten —
   it is the audit anchor that keeps dismiss, draft reasons and deep links from
   losing their referent.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from athena_store import news_canonical as nc

log = logging.getLogger(__name__)

BACKFILL_STATUS = "legacy_backfill"

SELECT_LEGACY_SQL = """
SELECT fingerprint, ts, symbol, source, event_type, sentiment, title, url, dismissed
FROM news_events
ORDER BY fingerprint
"""

INSERT_ARTICLE_SQL = """
INSERT INTO news_articles (
    article_id, identity_version, identity_hash, normalized_title,
    canonical_title, published_day, first_seen_at, last_seen_at
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (article_id) DO UPDATE SET
    first_seen_at = LEAST(news_articles.first_seen_at, EXCLUDED.first_seen_at),
    last_seen_at = GREATEST(news_articles.last_seen_at, EXCLUDED.last_seen_at)
"""

# ``effective_ts`` uses LEAST so the result does not depend on row order.
# The mutable business columns are re-derived from legacy (see module docstring).
INSERT_ASSOCIATION_SQL = """
INSERT INTO news_article_symbols (
    association_id, article_id, symbol, source, url, event_type, sentiment,
    dismissed, effective_ts, day_precision, original_ts, ts
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (article_id, symbol) DO UPDATE SET
    effective_ts = LEAST(news_article_symbols.effective_ts, EXCLUDED.effective_ts),
    source = EXCLUDED.source,
    url = EXCLUDED.url,
    event_type = EXCLUDED.event_type,
    sentiment = EXCLUDED.sentiment,
    dismissed = EXCLUDED.dismissed
"""

INSERT_MAP_SQL = """
INSERT INTO news_legacy_event_map (
    legacy_fingerprint, article_id, association_id, legacy_ts,
    legacy_write_status, source, url, event_type, sentiment, title, symbol
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (legacy_fingerprint, article_id) DO NOTHING
"""

# Canonical read equivalents. Filters are pushed into SQL, never applied in
# Python after fetching the whole table.
NEWS_WASH_LEGACY_SQL = """
SELECT count(*) FROM news_events
WHERE symbol = %s AND sentiment < %s AND ts >= %s AND dismissed = false
"""
NEWS_WASH_CANONICAL_SQL = """
SELECT count(*) FROM news_article_symbols
WHERE symbol = %s AND sentiment < %s AND effective_ts >= %s AND dismissed = false
"""
NEWS_SCORE_LEGACY_SQL = """
SELECT sentiment FROM news_events
WHERE symbol = %s AND ts >= %s AND dismissed = false AND sentiment IS NOT NULL
ORDER BY ts
"""
NEWS_SCORE_CANONICAL_SQL = """
SELECT sentiment FROM news_article_symbols
WHERE symbol = %s AND effective_ts >= %s AND dismissed = false
  AND sentiment IS NOT NULL
ORDER BY effective_ts
"""

# Deterministic tie-breaks used only by the reconciliation proof. Rows sharing
# one timestamp have no defined order under ``ORDER BY ts``, which makes the
# recency-weighted mean non-reproducible in the legacy reader itself.
NEWS_SCORE_LEGACY_STABLE_SQL = """
SELECT sentiment FROM news_events
WHERE symbol = %s AND ts >= %s AND dismissed = false AND sentiment IS NOT NULL
ORDER BY ts, sentiment
"""
NEWS_SCORE_CANONICAL_STABLE_SQL = """
SELECT sentiment FROM news_article_symbols
WHERE symbol = %s AND effective_ts >= %s AND dismissed = false
  AND sentiment IS NOT NULL
ORDER BY effective_ts, sentiment
"""
NEWS_SCORE_LEGACY_MULTISET_SQL = """
SELECT ts, sentiment::text FROM news_events
WHERE symbol = %s AND ts >= %s AND dismissed = false AND sentiment IS NOT NULL
ORDER BY ts, sentiment
"""
NEWS_SCORE_CANONICAL_MULTISET_SQL = """
SELECT effective_ts, sentiment::text FROM news_article_symbols
WHERE symbol = %s AND effective_ts >= %s AND dismissed = false
  AND sentiment IS NOT NULL
ORDER BY effective_ts, sentiment
"""


def _row_identity(row: tuple) -> dict[str, Any]:
    fingerprint, ts, symbol, source, event_type, sentiment, title, url, dismissed = row
    # Legacy ts is a stored UTC instant. No provenance flag exists, so the
    # honest label is ingest_fallback; the raw value is preserved in original_ts.
    effective = nc.to_utc(ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts)))
    day = effective.date()
    normalized = nc.normalize_title(title or "")
    art_id = nc.article_id(day, normalized)
    return {
        "fingerprint": str(fingerprint),
        "effective": effective,
        "article_id": art_id,
        "identity_hash": nc.article_key(day, normalized),
        "normalized_title": normalized,
        "canonical_title": title or "",
        "published_day": day,
        "symbol": str(symbol or "").upper(),
        "source": source or "",
        "url": url or "",
        "event_type": event_type or "",
        "sentiment": sentiment,
        "dismissed": bool(dismissed),
        "association_id": nc.association_id(art_id, str(symbol or "").upper()),
    }


def backfill(conn, *, limit: int | None = None) -> dict[str, int]:
    """Idempotent N1 backfill. Safe to re-run; never writes ``news_events``."""
    sql = SELECT_LEGACY_SQL + (" LIMIT %s" if limit else "")
    rows = conn.execute(sql, (limit,) if limit else ()).fetchall()
    articles: set[str] = set()
    associations: set[str] = set()
    maps = 0
    for row in rows:
        i = _row_identity(row)
        conn.execute(INSERT_ARTICLE_SQL, (
            i["article_id"], nc.IDENTITY_VERSION, i["identity_hash"],
            i["normalized_title"], i["canonical_title"], i["published_day"],
            i["effective"], i["effective"],
        ))
        articles.add(i["article_id"])
        conn.execute(INSERT_ASSOCIATION_SQL, (
            i["association_id"], i["article_id"], i["symbol"], i["source"],
            i["url"], i["event_type"], i["sentiment"], i["dismissed"],
            i["effective"], "ingest_fallback", i["effective"], i["effective"],
        ))
        associations.add(i["association_id"])
        result = conn.execute(INSERT_MAP_SQL, (
            i["fingerprint"], i["article_id"], i["association_id"],
            i["effective"], BACKFILL_STATUS, i["source"], i["url"],
            i["event_type"], i["sentiment"], i["canonical_title"], i["symbol"],
        ))
        maps += getattr(result, "rowcount", 0) or 0
    return {
        "legacy_rows": len(rows),
        "articles": len(articles),
        "associations": len(associations),
        "maps_inserted": maps,
    }


def weighted_news_score(rows: list[tuple]) -> Decimal | None:
    """Byte-for-byte the same Decimal path as ``indicators.news_score``."""
    if not rows:
        return None
    total = Decimal(0)
    weight = Decimal(0)
    for index, row in enumerate(rows, start=1):
        step = Decimal(index)
        total += Decimal(row[0]) * step
        weight += step
    if weight == 0:
        return None
    return total / weight
