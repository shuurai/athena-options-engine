"""V2-P3 canonical news identity and dual-write helpers (§6.1 / §6.2 / §20.3).

Identity v1 is UTC calendar day + ``title.lower()``. The UTC day boundary is
the explicit narrow amendment in §21.3: no ``provider_tz`` or
``published_local_day`` column is introduced. Every source timestamp must be
tz-aware and is converted with ``astimezone(UTC)``; naive values are rejected
instead of being silently labelled as UTC.

``news_events`` stays the legacy truth source during N1/N2. Canonical rows are
written in the SAME transaction as the legacy row, but a canonical failure is
contained to its own savepoint so it can never abort the legacy insert.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, date, datetime
from typing import Any

log = logging.getLogger(__name__)

IDENTITY_VERSION = "v1"
DAY_PRECISION = frozenset({"provider", "ingest_fallback"})
LEGACY_STATUS = frozenset({"inserted", "conflict_existing"})


class NaiveTimestamp(ValueError):
    """A source timestamp had no tzinfo and must not be guessed as UTC."""


class AmbiguousAssociation(ValueError):
    """One legacy fingerprint maps to several associations; refuse batch dismiss."""


def to_utc(when: datetime) -> datetime:
    """Normalize one source timestamp to a UTC instant (§21.3 parse guard)."""
    if not isinstance(when, datetime):
        raise TypeError("timestamp must be a datetime")
    if when.tzinfo is None:
        raise NaiveTimestamp("naive source timestamp must not be assumed to be UTC")
    return when.astimezone(UTC)


def normalize_title(title: str) -> str:
    """v1 deliberately reuses the legacy normalization: no NFKC, no whitespace fold."""
    return title.lower()


def published_day(when: datetime) -> date:
    return to_utc(when).date()


def article_key(day: date, normalized_title: str) -> str:
    raw = f"{IDENTITY_VERSION}|{day.isoformat()}|{normalized_title}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def article_id(day: date, normalized_title: str) -> str:
    return "art_" + article_key(day, normalized_title)[:32]


def association_id(art_id: str, symbol: str) -> str:
    raw = f"v1|{art_id}|{symbol.upper()}"
    return "nas_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


INSERT_ARTICLE_SQL = """
INSERT INTO news_articles (
    article_id, identity_version, identity_hash, normalized_title,
    canonical_title, published_day, first_seen_at, last_seen_at
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (article_id) DO UPDATE SET
    last_seen_at = GREATEST(news_articles.last_seen_at, EXCLUDED.last_seen_at)
"""

INSERT_ASSOCIATION_SQL = """
INSERT INTO news_article_symbols (
    association_id, article_id, symbol, source, url, event_type, sentiment,
    dismissed, effective_ts, day_precision, original_ts, ts
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (article_id, symbol) DO UPDATE SET
    effective_ts = LEAST(news_article_symbols.effective_ts, EXCLUDED.effective_ts),
    dismissed = news_article_symbols.dismissed
"""

INSERT_MAP_SQL = """
INSERT INTO news_legacy_event_map (
    legacy_fingerprint, article_id, association_id, legacy_ts,
    legacy_write_status, source, url, event_type, sentiment, title, symbol
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (legacy_fingerprint, article_id) DO UPDATE SET
    legacy_write_status = EXCLUDED.legacy_write_status
"""


def build_identity(
    *, title: str, when: datetime, symbol: str,
    day_precision: str = "provider", original_ts: datetime | None = None,
) -> dict[str, Any]:
    """Pure identity derivation. No DB access."""
    if day_precision not in DAY_PRECISION:
        raise ValueError(f"unknown day_precision: {day_precision!r}")
    effective = to_utc(when)
    day = effective.date()
    normalized = normalize_title(title)
    art_id = article_id(day, normalized)
    return {
        "article_id": art_id,
        "identity_version": IDENTITY_VERSION,
        "identity_hash": article_key(day, normalized),
        "normalized_title": normalized,
        "canonical_title": title,
        "published_day": day,
        "effective_ts": effective,
        "day_precision": day_precision,
        "original_ts": to_utc(original_ts) if original_ts is not None else effective,
        "association_id": association_id(art_id, symbol),
        "symbol": symbol.upper(),
    }


def dual_write(
    conn,
    *,
    fingerprint: str,
    ts: datetime,
    symbol: str,
    title: str,
    source: str,
    url: str,
    event_type: str,
    sentiment: float,
    legacy_status: str,
    day_precision: str = "provider",
    original_ts: datetime | None = None,
) -> dict[str, Any] | None:
    """N2: write canonical article + association + immutable legacy map.

    Runs inside its own savepoint so a canonical failure cannot abort the
    legacy ``news_events`` write. The canonical insert does NOT depend on
    whether the legacy ``ON CONFLICT DO NOTHING`` inserted a row.
    """
    if legacy_status not in LEGACY_STATUS:
        raise ValueError(f"unknown legacy_write_status: {legacy_status!r}")
    ident = build_identity(
        title=title, when=ts, symbol=symbol,
        day_precision=day_precision, original_ts=original_ts,
    )
    effective = ident["effective_ts"]
    try:
        with conn.transaction():
            conn.execute(INSERT_ARTICLE_SQL, (
                ident["article_id"], ident["identity_version"],
                ident["identity_hash"], ident["normalized_title"],
                ident["canonical_title"], ident["published_day"],
                effective, effective,
            ))
            conn.execute(INSERT_ASSOCIATION_SQL, (
                ident["association_id"], ident["article_id"], ident["symbol"],
                source, url, event_type, sentiment, False, effective,
                ident["day_precision"], ident["original_ts"], effective,
            ))
            conn.execute(INSERT_MAP_SQL, (
                fingerprint, ident["article_id"], ident["association_id"],
                effective, legacy_status, source, url, event_type, sentiment,
                title, ident["symbol"],
            ))
    except Exception:
        log.warning("canonical dual-write failed fingerprint=%s", fingerprint)
        return None
    return ident


def resolve_association_id(conn, fingerprint: str) -> str | None:
    """Compat entry for dismiss. Ambiguous one-to-many is refused (§20.3.4)."""
    rows = conn.execute(
        "SELECT DISTINCT association_id FROM news_legacy_event_map "
        "WHERE legacy_fingerprint = %s", (fingerprint,),
    ).fetchall()
    if not rows:
        return None  # pre-N1 legacy row: dismiss only legacy, no canonical row
    if len(rows) > 1:
        raise AmbiguousAssociation(
            f"fingerprint {fingerprint} maps to {len(rows)} associations; "
            "pass an explicit association_id"
        )
    return str(rows[0][0])


def mark_dismissed(conn, association_id_: str) -> int:
    result = conn.execute(
        "UPDATE news_article_symbols SET dismissed = true "
        "WHERE association_id = %s AND dismissed = false", (association_id_,),
    )
    return result.rowcount


def mark_sentiment(conn, fingerprint: str, sentiment: float) -> int:
    result = conn.execute(
        "UPDATE news_article_symbols AS a SET sentiment = %s "
        "FROM news_legacy_event_map AS m "
        "WHERE m.legacy_fingerprint = %s AND m.association_id = a.association_id",
        (sentiment, fingerprint),
    )
    return result.rowcount


def collision_report(conn) -> list[dict[str, Any]]:
    """Same UTC day + same normalized title but different URLs (§6.2)."""
    rows = conn.execute(
        "SELECT a.published_day, a.normalized_title, count(DISTINCT s.url) AS urls, "
        "       count(DISTINCT s.association_id) AS associations "
        "FROM news_articles a JOIN news_article_symbols s ON s.article_id = a.article_id "
        "GROUP BY a.published_day, a.normalized_title "
        "HAVING count(DISTINCT s.url) > 1 "
        "ORDER BY a.published_day, a.normalized_title"
    ).fetchall()
    return [
        {"published_day": str(r[0]), "normalized_title": r[1],
         "urls": int(r[2]), "associations": int(r[3])}
        for r in rows
    ]


def reconcile(conn) -> dict[str, int]:
    """Six N1/N2 checks (§6.3). Returns counts; all diffs must be explained."""
    def scalar(sql: str) -> int:
        row = conn.execute(sql).fetchone()
        return int(row[0]) if row else 0

    legacy = scalar("SELECT count(*) FROM news_events")
    mapped = scalar("SELECT count(*) FROM news_legacy_event_map")
    distinct_map = scalar(
        "SELECT count(*) FROM (SELECT DISTINCT legacy_fingerprint, article_id "
        "FROM news_legacy_event_map) t"
    )
    articles = scalar("SELECT count(*) FROM news_articles")
    associations = scalar("SELECT count(*) FROM news_article_symbols")
    orphans = scalar(
        "SELECT count(*) FROM news_article_symbols s "
        "WHERE NOT EXISTS (SELECT 1 FROM news_articles a WHERE a.article_id = s.article_id)"
    )
    distinct_titles = scalar(
        "SELECT count(*) FROM (SELECT DISTINCT published_day, normalized_title "
        "FROM news_articles) t"
    )
    return {
        "legacy_rows": legacy,
        "map_rows": mapped,
        "distinct_map": distinct_map,
        "articles": articles,
        "associations": associations,
        "orphan_associations": orphans,
        "distinct_article_identities": distinct_titles,
    }
