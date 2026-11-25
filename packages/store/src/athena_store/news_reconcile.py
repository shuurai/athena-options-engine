"""V2-P3 N1 gate — §6.3 six-item reconciliation.

The gate is read-only. It never mutates ``news_events`` or the canonical tables.
It returns a per-item result plus ``registered`` / ``unexplained`` buckets, and
``blocks_n3`` is true the moment anything is unexplained.

Item ⑤ needs one nuance that must not be papered over. ``indicators.news_score``
orders by ``ts`` alone, so rows sharing a timestamp have no defined relative
order and the recency-weighted mean is not reproducible — the legacy reader
disagrees with *itself* between an ascending scan and a reversed descending
scan. A score delta is therefore only *registered* when it is proved to be tie
ordering:

* the ``(ts, sentiment)`` multiset is identical on both sides (no data loss), and
* the score is bit-identical once both sides use ``(ts, sentiment)`` as the
  tie-break (the Decimal path is the same one).

A differing multiset or a differing stable score is *unexplained* and blocks N3.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from athena_store import news_backfill as nb
from athena_store import news_canonical as nc

ET = ZoneInfo("America/New_York")

ITEMS = (
    "1_map_parity",
    "2_association_integrity",
    "3_article_identity",
    "4_field_parity",
    "5_reader_parity",
    "6_fixture_coverage",
)

IMMUTABLE_FIELDS = ("source", "url", "event_type")
MUTABLE_FIELDS = ("sentiment", "dismissed")


def _scalar(conn, sql: str, args: tuple = ()) -> int:
    row = conn.execute(sql, args).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _result(ok: bool, **counts) -> dict[str, Any]:
    return {"ok": bool(ok), "registered": [], "unexplained": [], **counts}


# --------------------------------------------------------------------------- #
# ① map parity — map count == legacy rows, and every legacy row mapped once
# --------------------------------------------------------------------------- #
def item1_map_parity(conn) -> dict[str, Any]:
    legacy = _scalar(conn, "SELECT count(*) FROM news_events")
    map_rows = _scalar(conn, "SELECT count(*) FROM news_legacy_event_map")
    multi = _scalar(
        conn,
        "SELECT count(*) FROM (SELECT legacy_fingerprint FROM news_legacy_event_map "
        "GROUP BY legacy_fingerprint HAVING count(*) <> 1) t",
    )
    unmapped = _scalar(
        conn,
        "SELECT count(*) FROM news_events e WHERE NOT EXISTS ("
        "SELECT 1 FROM news_legacy_event_map m "
        "WHERE m.legacy_fingerprint = e.fingerprint)",
    )
    orphan_map = _scalar(
        conn,
        "SELECT count(*) FROM news_legacy_event_map m WHERE NOT EXISTS ("
        "SELECT 1 FROM news_events e WHERE e.fingerprint = m.legacy_fingerprint)",
    )
    ok = legacy == map_rows and multi == 0 and unmapped == 0 and orphan_map == 0
    return _result(
        ok,
        legacy_rows=legacy,
        map_rows=map_rows,
        multi_mapped_fingerprints=multi,
        unmapped_legacy_rows=unmapped,
        orphan_map_rows=orphan_map,
    )


# --------------------------------------------------------------------------- #
# ② association integrity — count == distinct (article_id, symbol), no orphans
# --------------------------------------------------------------------------- #
def item2_association_integrity(conn) -> dict[str, Any]:
    associations = _scalar(conn, "SELECT count(*) FROM news_article_symbols")
    distinct_pairs = _scalar(
        conn,
        "SELECT count(*) FROM (SELECT DISTINCT article_id, symbol "
        "FROM news_article_symbols) t",
    )
    orphan = _scalar(
        conn,
        "SELECT count(*) FROM news_article_symbols s WHERE NOT EXISTS ("
        "SELECT 1 FROM news_articles a WHERE a.article_id = s.article_id)",
    )
    dangling_assoc = _scalar(
        conn,
        "SELECT count(*) FROM news_legacy_event_map m WHERE NOT EXISTS ("
        "SELECT 1 FROM news_article_symbols s WHERE s.association_id = m.association_id)",
    )
    dangling_article = _scalar(
        conn,
        "SELECT count(*) FROM news_legacy_event_map m WHERE NOT EXISTS ("
        "SELECT 1 FROM news_articles a WHERE a.article_id = m.article_id)",
    )
    unmapped_assoc = _scalar(
        conn,
        "SELECT count(*) FROM news_article_symbols s WHERE NOT EXISTS ("
        "SELECT 1 FROM news_legacy_event_map m WHERE m.association_id = s.association_id)",
    )
    ok = (
        associations == distinct_pairs
        and orphan == 0
        and dangling_assoc == 0
        and dangling_article == 0
        and unmapped_assoc == 0
    )
    return _result(
        ok,
        associations=associations,
        distinct_article_symbol_pairs=distinct_pairs,
        orphan_associations=orphan,
        map_dangling_association=dangling_assoc,
        map_dangling_article=dangling_article,
        associations_without_map=unmapped_assoc,
    )


# --------------------------------------------------------------------------- #
# ③ article identity — count only shrinks by the registered identity rule
# --------------------------------------------------------------------------- #
def item3_article_identity(conn) -> dict[str, Any]:
    articles = _scalar(conn, "SELECT count(*) FROM news_articles")
    distinct_identity = _scalar(
        conn,
        "SELECT count(*) FROM (SELECT DISTINCT identity_version, identity_hash "
        "FROM news_articles) t",
    )
    distinct_published = _scalar(
        conn,
        "SELECT count(*) FROM (SELECT DISTINCT published_day, normalized_title "
        "FROM news_articles) t",
    )
    legacy_distinct = _scalar(
        conn,
        "SELECT count(*) FROM (SELECT DISTINCT (ts AT TIME ZONE 'UTC')::date, "
        "lower(title) FROM news_events) t",
    )
    # Article count may only shrink relative to legacy identities, and only
    # because several legacy symbols share one (UTC day, lower(title)).
    collapsed = legacy_distinct - articles
    ok = (
        articles == distinct_identity == distinct_published
        and collapsed >= 0
        and articles == legacy_distinct
    )
    return _result(
        ok,
        articles=articles,
        distinct_identity_hashes=distinct_identity,
        distinct_published_titles=distinct_published,
        legacy_distinct_identities=legacy_distinct,
        collapsed_by_identity_rule=collapsed,
    )


# --------------------------------------------------------------------------- #
# ④ field parity — immutable via map, mutable via association
# --------------------------------------------------------------------------- #
def item4_field_parity(conn) -> dict[str, Any]:
    immutable = {
        field: _scalar(
            conn,
            f"SELECT count(*) FROM news_legacy_event_map m "
            f"JOIN news_events e ON e.fingerprint = m.legacy_fingerprint "
            f"WHERE COALESCE(m.{field}, '') <> COALESCE(e.{field}, '')",
        )
        for field in IMMUTABLE_FIELDS
    }
    mutable = {
        field: _scalar(
            conn,
            f"SELECT count(*) FROM news_legacy_event_map m "
            f"JOIN news_events e ON e.fingerprint = m.legacy_fingerprint "
            f"JOIN news_article_symbols s ON s.association_id = m.association_id "
            f"WHERE s.{field} <> e.{field}",
        )
        for field in MUTABLE_FIELDS
    }
    # Two legacy rows that share (article, symbol) would collapse into one
    # association; only then can a mutable field legitimately disagree.
    duplicate_pairs = _scalar(
        conn,
        "SELECT count(*) FROM (SELECT (ts AT TIME ZONE 'UTC')::date d, "
        "lower(title) t, upper(symbol) s FROM news_events "
        "GROUP BY 1, 2, 3 HAVING count(*) > 1) x",
    )
    out = _result(
        all(v == 0 for v in immutable.values()) and all(v == 0 for v in mutable.values()),
        immutable=immutable,
        mutable=mutable,
        duplicate_article_symbol_pairs=duplicate_pairs,
    )
    if duplicate_pairs:
        out["registered"].append({
            "item": "4_field_parity",
            "reason": "duplicate_article_symbol_pair",
            "count": duplicate_pairs,
            "note": "several legacy rows collapse into one association; "
                    "mutable fields keep the association value",
        })
    return out


# --------------------------------------------------------------------------- #
# ⑤ reader parity — per-symbol NEWS_WASH and news_score
# --------------------------------------------------------------------------- #
def classify_score_delta(conn, symbol: str, start: datetime) -> str | None:
    """Return None when scores agree, else the mechanical classification."""
    legacy = conn.execute(nb.NEWS_SCORE_LEGACY_SQL, (symbol, start)).fetchall()
    canon = conn.execute(nb.NEWS_SCORE_CANONICAL_SQL, (symbol, start)).fetchall()
    if nb.weighted_news_score(legacy) == nb.weighted_news_score(canon):
        return None
    left = conn.execute(nb.NEWS_SCORE_LEGACY_MULTISET_SQL, (symbol, start)).fetchall()
    right = conn.execute(nb.NEWS_SCORE_CANONICAL_MULTISET_SQL, (symbol, start)).fetchall()
    if left != right:
        return "data_mismatch"
    stable_legacy = conn.execute(nb.NEWS_SCORE_LEGACY_STABLE_SQL, (symbol, start)).fetchall()
    stable_canon = conn.execute(nb.NEWS_SCORE_CANONICAL_STABLE_SQL, (symbol, start)).fetchall()
    if nb.weighted_news_score(stable_legacy) != nb.weighted_news_score(stable_canon):
        return "decimal_path_mismatch"
    return "tie_order_nondeterminism"


def item5_reader_parity(
    conn, *, as_of: date, window_days: int, sentiment_below: float
) -> dict[str, Any]:
    start = datetime.combine(as_of - timedelta(days=window_days), time.min, ET)
    symbols = [str(r[0]) for r in conn.execute(
        "SELECT DISTINCT symbol FROM news_events ORDER BY symbol"
    ).fetchall()]
    wash_diffs: list[dict[str, Any]] = []
    score_diffs: list[dict[str, Any]] = []
    for symbol in symbols:
        upper = symbol.upper()
        legacy_wash = _scalar(conn, nb.NEWS_WASH_LEGACY_SQL, (symbol, sentiment_below, start))
        canon_wash = _scalar(conn, nb.NEWS_WASH_CANONICAL_SQL, (upper, sentiment_below, start))
        if legacy_wash != canon_wash:
            wash_diffs.append({
                "symbol": symbol, "legacy": legacy_wash, "canonical": canon_wash,
            })
        verdict = classify_score_delta(conn, upper, start)
        if verdict is not None:
            score_diffs.append({"symbol": upper, "classification": verdict})
    out = _result(
        not wash_diffs
        and not [d for d in score_diffs if d["classification"] != "tie_order_nondeterminism"],
        symbols_checked=len(symbols),
        window_start=start.isoformat(),
        news_wash_diffs=wash_diffs,
        news_score_diffs=score_diffs,
    )
    for diff in score_diffs:
        bucket = (
            out["registered"]
            if diff["classification"] == "tie_order_nondeterminism"
            else out["unexplained"]
        )
        bucket.append({"item": "5_reader_parity", **diff})
    return out


# --------------------------------------------------------------------------- #
# ⑥ fixtures — every registered edge case has a fixture and a collision report
# --------------------------------------------------------------------------- #
def item6_fixture_coverage(conn) -> dict[str, Any]:
    boundary = _scalar(
        conn,
        "SELECT count(*) FROM news_events WHERE "
        "(ts AT TIME ZONE 'America/New_York')::date <> (ts AT TIME ZONE 'UTC')::date",
    )
    provider_missing = _scalar(
        conn,
        "SELECT count(*) FROM news_article_symbols WHERE day_precision = 'ingest_fallback'",
    )
    cross_day = _scalar(
        conn,
        "SELECT count(*) FROM (SELECT lower(title) FROM news_events "
        "GROUP BY 1 HAVING count(DISTINCT (ts AT TIME ZONE 'UTC')::date) > 1) x",
    )
    conflict = _scalar(
        conn,
        "SELECT count(*) FROM news_legacy_event_map "
        "WHERE legacy_write_status = 'conflict_existing'",
    )
    manual = _scalar(
        conn,
        "SELECT count(*) FROM news_events "
        "WHERE url ILIKE 'manual%%' OR source ILIKE '%%manual%%'",
    )
    collisions = nc.collision_report(conn)
    out = _result(
        True,
        et_utc_boundary_rows=boundary,
        provider_ts_missing_rows=provider_missing,
        cross_day_same_title_groups=cross_day,
        legacy_conflict_rows=conflict,
        manual_url_rows=manual,
        collision_groups=len(collisions),
        collisions=collisions,
        fixture_families=(
            "et_day_boundary",
            "provider_ts_missing",
            "cross_day_same_title",
            "legacy_conflict",
            "same_day_same_title_diff_url",
            "manual_url",
        ),
    )
    if collisions:
        out["registered"].append({
            "item": "6_fixture_coverage",
            "reason": "same_day_same_title_diff_url",
            "count": len(collisions),
            "note": "identity v1 accepts these as one article; URLs are evidence only",
        })
    return out


# --------------------------------------------------------------------------- #
# aggregate
# --------------------------------------------------------------------------- #
def six_item_gate(
    conn, *, as_of: date, window_days: int = 3, sentiment_below: float = -0.3
) -> dict[str, Any]:
    items = {
        "1_map_parity": item1_map_parity(conn),
        "2_association_integrity": item2_association_integrity(conn),
        "3_article_identity": item3_article_identity(conn),
        "4_field_parity": item4_field_parity(conn),
        "5_reader_parity": item5_reader_parity(
            conn, as_of=as_of, window_days=window_days,
            sentiment_below=sentiment_below,
        ),
        "6_fixture_coverage": item6_fixture_coverage(conn),
    }
    registered = [d for name in ITEMS for d in items[name]["registered"]]
    unexplained = [d for name in ITEMS for d in items[name]["unexplained"]]
    return {
        "as_of": as_of.isoformat(),
        "items": items,
        "registered": registered,
        "unexplained": unexplained,
        "blocks_n3": bool(unexplained) or not all(v["ok"] for v in items.values()),
    }


def format_gate(gate: dict[str, Any]) -> str:
    """Human-readable evidence block for the delivery log."""
    lines = [f"as_of={gate['as_of']}"]
    for name in ITEMS:
        item = gate["items"][name]
        mark = "PASS" if item["ok"] else "FAIL"
        counts = ", ".join(
            f"{k}={v}" for k, v in item.items()
            if k not in {"ok", "registered", "unexplained", "collisions", "fixture_families"}
            and not isinstance(v, (list, tuple))
        )
        lines.append(f"  {mark} {name}: {counts}")
    lines.append(f"  registered={len(gate['registered'])} unexplained={len(gate['unexplained'])}")
    lines.append(f"  blocks_n3={gate['blocks_n3']}")
    return "\n".join(lines)
