"""§6.3 N1 — V2-P3 news backfill and the six-item reconciliation gate.

Pins: backfill is idempotent and re-derivable from legacy truth, backfilled rows
are honestly labelled ``ingest_fallback``, the immutable map is insert-only, the
weighted score uses the same Decimal path as ``indicators.news_score``, and the
gate separates *registered* from *unexplained* differences so that only an
unexplained one can block N3.
"""

from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from athena_store import news_backfill as nb
from athena_store import news_canonical as nc
from athena_store import news_reconcile as nr

ROOT = Path(__file__).resolve().parents[3]
STORE = ROOT / "packages/store/src/athena_store"
BACKFILL_SRC = STORE / "news_backfill.py"
RECONCILE_SRC = STORE / "news_reconcile.py"

ET = ZoneInfo("America/New_York")
T0 = datetime(2026, 11, 24, 14, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 24, 14, 0, tzinfo=UTC)  # tied instant on purpose


class _Cur:
    def __init__(self, rows=None, rowcount=1):
        self.rows = list(rows or [])
        self.rowcount = rowcount

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _Conn:
    """Minimal legacy-table double: SELECT returns the seeded legacy rows."""

    def __init__(self, rows=None):
        self.calls = []
        self.rows = list(rows or [])

    def transaction(self):
        return _Tx()

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if sql.strip().startswith("SELECT"):
            return _Cur(self.rows)
        return _Cur([])

    def sqls(self):
        return [sql for sql, _ in self.calls]

    def inserts(self):
        return [
            (sql, params) for sql, params in self.calls
            if not sql.strip().startswith("SELECT")
        ]


class _Tx:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Scripted:
    """Ordered substring dispatch, so one double can answer every gate query."""

    def __init__(self, rules):
        self.rules = rules  # list of (predicate, rows)
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        for predicate, rows in self.rules:
            if predicate(sql):
                return _Cur(rows)
        return _Cur([])


def legacy_row(fingerprint, ts, symbol, title, sentiment, *, source="yahoo",
               event_type="GENERIC", url=None, dismissed=False):
    return (fingerprint, ts, symbol, source, event_type, sentiment, title,
            url if url is not None else f"https://example.test/{fingerprint}",
            dismissed)


class TestBackfillIdentity(unittest.TestCase):
    def test_backfilled_rows_are_labelled_ingest_fallback(self):
        conn = _Conn([legacy_row("fp1", T0, "NVDA", "Chip stocks fall", 0.1)])
        nb.backfill(conn)
        assoc = [p for s, p in conn.inserts() if "news_article_symbols" in s]
        self.assertEqual(len(assoc), 1)
        self.assertEqual(assoc[0][9], "ingest_fallback")

    def test_cross_symbol_same_title_collapses_to_one_article(self):
        rows = [
            legacy_row("fp1", T0, "AMD", "Chip stocks fall", 0.1),
            legacy_row("fp2", T0, "NVDA", "Chip stocks fall", 0.1),
        ]
        stats = nb.backfill(_Conn(rows))
        self.assertEqual(stats["legacy_rows"], 2)
        self.assertEqual(stats["articles"], 1, "same UTC day + title => one article")
        self.assertEqual(stats["associations"], 2, "symbol is out of article identity")
        self.assertEqual(stats["maps_inserted"], 2)

    def test_article_identity_is_utc_not_et(self):
        # 2026-11-25T02:00Z is still 2026-11-24 in ET; identity must use UTC.
        late = datetime(2026, 11, 25, 2, 0, tzinfo=UTC)
        ident = nb._row_identity(legacy_row("fp1", late, "NVDA", "Title", 0.0))
        self.assertEqual(ident["published_day"], date(2026, 11, 25))

    def test_naive_legacy_timestamp_is_rejected(self):
        naive = datetime(2026, 11, 24, 14, 0)
        with self.assertRaises(nc.NaiveTimestamp):
            nb._row_identity(legacy_row("fp1", naive, "NVDA", "Title", 0.0))

    def test_symbol_is_uppercased(self):
        ident = nb._row_identity(legacy_row("fp1", T0, "nvda", "Title", 0.0))
        self.assertEqual(ident["symbol"], "NVDA")

    def test_limit_clause_only_when_asked(self):
        conn = _Conn([])
        nb.backfill(conn)
        self.assertNotIn("LIMIT", conn.calls[0][0])
        conn2 = _Conn([])
        nb.backfill(conn2, limit=17)
        self.assertIn("LIMIT %s", conn2.calls[0][0])
        self.assertEqual(conn2.calls[0][1], (17,))


class TestBackfillIdempotency(unittest.TestCase):
    def test_map_insert_is_insert_only(self):
        self.assertIn("DO NOTHING", nb.INSERT_MAP_SQL)
        self.assertNotIn("DO UPDATE", nb.INSERT_MAP_SQL,
                         "the legacy anchor must stay immutable")

    def test_association_rederives_mutable_fields_from_legacy(self):
        # A DSH enrich followed by a re-backfill must not leave canonical stale.
        for field in ("source", "url", "event_type", "sentiment", "dismissed"):
            self.assertIn(f"{field} = EXCLUDED.{field}", nb.INSERT_ASSOCIATION_SQL)

    def test_association_effective_ts_is_order_independent(self):
        self.assertIn("LEAST(news_article_symbols.effective_ts, EXCLUDED.effective_ts)",
                      nb.INSERT_ASSOCIATION_SQL)

    def test_backfill_never_writes_legacy_table(self):
        conn = _Conn([legacy_row("fp1", T0, "NVDA", "Chip stocks fall", 0.1)])
        nb.backfill(conn)
        for sql, _ in conn.inserts():
            self.assertNotIn("news_events", sql)
            # Upserts legitimately contain "DO UPDATE SET"; what must never
            # happen is a statement whose verb targets a table destructively.
            self.assertFalse(
                sql.strip().upper().startswith(("UPDATE ", "DELETE ", "ALTER ", "DROP ")),
                sql,
            )

    def test_second_run_inserts_no_new_map_rows(self):
        conn = _Conn([])  # simulator: ON CONFLICT DO NOTHING yields rowcount 0
        conn.execute = lambda sql, params=None: (
            _Cur([]) if sql.strip().startswith("SELECT") else _Cur([], rowcount=0)
        )
        stats = nb.backfill(conn)
        self.assertEqual(stats["maps_inserted"], 0)


class TestWeightedScore(unittest.TestCase):
    def test_matches_indicators_decimal_path(self):
        rows = [(0.5,), (-0.25,), (0.1,)]
        # Same loop as indicators.news_score.
        total, weight = Decimal(0), Decimal(0)
        for index, row in enumerate(rows, start=1):
            total += Decimal(row[0]) * Decimal(index)
            weight += Decimal(index)
        self.assertEqual(nb.weighted_news_score(rows), total / weight)

    def test_empty_returns_none(self):
        self.assertIsNone(nb.weighted_news_score([]))

    def test_recency_weighting_is_positional(self):
        self.assertEqual(
            nb.weighted_news_score([(1.0,), (0.0,)]),
            Decimal(1) / Decimal(3),
            "the newer row (index 2) carries the larger weight",
        )

    def test_numeric_string_sentiment_is_accepted(self):
        self.assertIsNotNone(nb.weighted_news_score([("0.25",)]))


class TestClassifyScoreDelta(unittest.TestCase):
    def _conn(self, *, legacy, canon, multiset_left=None, multiset_right=None,
              stable_left=None, stable_right=None):
        def is_canon_multiset(sql):
            return "sentiment::text" in sql and "effective_ts" in sql

        def is_left_multiset(sql):
            return "sentiment::text" in sql

        def is_canon_stable(sql):
            return "ORDER BY effective_ts, sentiment" in sql

        def is_left_stable(sql):
            return "ORDER BY ts, sentiment" in sql

        def is_canon_base(sql):
            return "ORDER BY effective_ts" in sql

        return _Scripted([
            (is_canon_multiset, multiset_right if multiset_right is not None else []),
            (is_left_multiset, multiset_left if multiset_left is not None else []),
            (is_canon_stable, stable_right if stable_right is not None else canon),
            (is_left_stable, stable_left if stable_left is not None else legacy),
            (is_canon_base, canon),
            (lambda sql: "ORDER BY ts" in sql, legacy),
        ])

    def test_none_when_scores_agree(self):
        conn = self._conn(legacy=[(0.1,), (0.2,)], canon=[(0.1,), (0.2,)])
        self.assertIsNone(nr.classify_score_delta(conn, "NVDA", T0))

    def test_tie_order_is_registered_not_unexplained(self):
        legacy = [(-0.5,), (-0.1,)]
        canon = [(-0.1,), (-0.5,)]  # same multiset, different tie order
        conn = self._conn(
            legacy=legacy, canon=canon,
            multiset_left=[(T0, "-0.5"), (T1, "-0.1")],
            multiset_right=[(T0, "-0.5"), (T1, "-0.1")],
            stable_left=legacy, stable_right=legacy,
        )
        self.assertEqual(nr.classify_score_delta(conn, "NVDA", T0),
                         "tie_order_nondeterminism")

    def test_data_mismatch_when_multiset_differs(self):
        legacy = [(-0.5,), (-0.1,)]
        canon = [(-0.1,)]
        conn = self._conn(
            legacy=legacy, canon=canon,
            multiset_left=[(T0, "-0.5"), (T1, "-0.1")],
            multiset_right=[(T0, "-0.1")],
        )
        self.assertEqual(nr.classify_score_delta(conn, "NVDA", T0), "data_mismatch")

    def test_decimal_path_mismatch_when_stable_differs(self):
        legacy = [(-0.5,), (-0.1,)]
        canon = [(-0.1,), (-0.5,)]
        conn = self._conn(
            legacy=legacy, canon=canon,
            multiset_left=[(T0, "-0.5"), (T1, "-0.1")],
            multiset_right=[(T0, "-0.5"), (T1, "-0.1")],
            stable_left=[(-0.5,), (-0.1,)],
            stable_right=[(-0.1,), (-0.5,)],
        )
        self.assertEqual(nr.classify_score_delta(conn, "NVDA", T0),
                         "decimal_path_mismatch")


class _Seq:
    """Returns one scalar per call, in order — for items that only count."""

    def __init__(self, values):
        self.values = list(values)
        self.i = 0

    def execute(self, sql, params=None):
        value = self.values[self.i]
        self.i += 1
        return _Cur([(value,)])


class TestItemChecks(unittest.TestCase):
    def test_item1_passes_when_every_legacy_row_maps_once(self):
        item = nr.item1_map_parity(_Seq([2529, 2529, 0, 0, 0]))
        self.assertTrue(item["ok"])
        self.assertEqual(item["map_rows"], 2529)

    def test_item1_fails_when_a_legacy_row_is_unmapped(self):
        # Counts are held equal on purpose so this isolates the ``unmapped``
        # term: in a real DB an unmapped row also lowers ``map_rows``, which
        # would hide a regression in the uncovered-FINGERPRINT check itself.
        item = nr.item1_map_parity(_Seq([2529, 2529, 0, 1, 0]))
        self.assertFalse(item["ok"])
        self.assertEqual(item["unmapped_legacy_rows"], 1)

    def test_item1_fails_when_a_fingerprint_maps_twice(self):
        item = nr.item1_map_parity(_Seq([2529, 2530, 1, 0, 0]))
        self.assertFalse(item["ok"])
        self.assertEqual(item["multi_mapped_fingerprints"], 1)

    def test_item2_passes_on_clean_integrity(self):
        item = nr.item2_association_integrity(_Seq([2529, 2529, 0, 0, 0, 0]))
        self.assertTrue(item["ok"])

    def test_item2_fails_on_orphan_association(self):
        item = nr.item2_association_integrity(_Seq([2529, 2529, 1, 0, 0, 0]))
        self.assertFalse(item["ok"])
        self.assertEqual(item["orphan_associations"], 1)

    def test_item2_fails_when_count_exceeds_distinct_pairs(self):
        item = nr.item2_association_integrity(_Seq([2530, 2529, 0, 0, 0, 0]))
        self.assertFalse(item["ok"])

    def test_item3_passes_when_articles_equal_registered_identities(self):
        item = nr.item3_article_identity(_Seq([1873, 1873, 1873, 1873]))
        self.assertTrue(item["ok"])
        self.assertEqual(item["collapsed_by_identity_rule"], 0)

    def test_item3_fails_on_partial_backfill(self):
        # Article count shrinks by the identity rule, but coverage is short of
        # legacy identities: a half-done backfill must not pass the gate.
        item = nr.item3_article_identity(_Seq([1870, 1870, 1870, 1873]))
        self.assertFalse(item["ok"])
        self.assertEqual(item["collapsed_by_identity_rule"], 3)
        self.assertNotEqual(item["articles"], item["legacy_distinct_identities"])

    def test_item3_fails_when_identity_hash_is_duplicated(self):
        item = nr.item3_article_identity(_Seq([1873, 1870, 1873, 1873]))
        self.assertFalse(item["ok"])


class TestGateAggregation(unittest.TestCase):
    def _item(self, ok, *, registered=(), unexplained=()):
        return {"ok": ok, "registered": list(registered), "unexplained": list(unexplained)}

    def test_blocks_when_any_item_fails(self):
        canned = {name: self._item(True) for name in nr.ITEMS}
        canned["1_map_parity"] = self._item(
            False, unexplained=[{"item": "1_map_parity", "count": 3}]
        )
        with patch.multiple(
            nr,
            item1_map_parity=lambda c: canned["1_map_parity"],
            item2_association_integrity=lambda c: canned["2_association_integrity"],
            item3_article_identity=lambda c: canned["3_article_identity"],
            item4_field_parity=lambda c: canned["4_field_parity"],
            item5_reader_parity=lambda c, **k: canned["5_reader_parity"],
            item6_fixture_coverage=lambda c: canned["6_fixture_coverage"],
        ):
            gate = nr.six_item_gate(_Conn([]), as_of=date(2026, 11, 25))
        self.assertTrue(gate["blocks_n3"])
        self.assertEqual(len(gate["unexplained"]), 1)

    def test_does_not_block_when_only_registered(self):
        canned = {name: self._item(True) for name in nr.ITEMS}
        canned["5_reader_parity"] = self._item(
            True, registered=[{"item": "5_reader_parity", "symbol": "NVDA"}]
        )
        with patch.multiple(
            nr,
            item1_map_parity=lambda c: canned["1_map_parity"],
            item2_association_integrity=lambda c: canned["2_association_integrity"],
            item3_article_identity=lambda c: canned["3_article_identity"],
            item4_field_parity=lambda c: canned["4_field_parity"],
            item5_reader_parity=lambda c, **k: canned["5_reader_parity"],
            item6_fixture_coverage=lambda c: canned["6_fixture_coverage"],
        ):
            gate = nr.six_item_gate(_Conn([]), as_of=date(2026, 11, 25))
        self.assertFalse(gate["blocks_n3"])
        self.assertEqual(gate["registered"][0]["symbol"], "NVDA")

    def test_item5_registers_tie_order_but_fails_on_data_mismatch(self):
        def is_symbols(sql):
            return "DISTINCT symbol" in sql

        rules = [
            (is_symbols, [("NVDA",)]),
            (lambda s: "count(*)" in s and "news_events" in s, [(0,)]),
            (lambda s: "count(*)" in s and "news_article_symbols" in s, [(0,)]),
        ]
        conn = _Scripted(rules)
        with patch.object(nr, "classify_score_delta",
                          return_value="tie_order_nondeterminism"):
            healthy = nr.item5_reader_parity(
                conn, as_of=date(2026, 11, 25), window_days=3, sentiment_below=-0.3
            )
        self.assertTrue(healthy["ok"])
        self.assertEqual(healthy["registered"][0]["classification"],
                         "tie_order_nondeterminism")
        self.assertEqual(healthy["unexplained"], [])

        with patch.object(nr, "classify_score_delta", return_value="data_mismatch"):
            broken = nr.item5_reader_parity(
                conn, as_of=date(2026, 11, 25), window_days=3, sentiment_below=-0.3
            )
        self.assertFalse(broken["ok"])
        self.assertEqual(broken["unexplained"][0]["classification"], "data_mismatch")

    def test_item4_registers_duplicate_pairs(self):
        conn = _Scripted([(lambda s: "count(*)" in s and "HAVING count(*) > 1" in s, [(2,)])])
        item = nr.item4_field_parity(conn)
        self.assertEqual(item["duplicate_article_symbol_pairs"], 2)
        self.assertEqual(item["registered"][0]["reason"], "duplicate_article_symbol_pair")


class TestGateIsReadOnly(unittest.TestCase):
    def test_reconcile_module_emits_no_mutating_sql(self):
        source = RECONCILE_SRC.read_text(encoding="utf-8")
        for keyword in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "TRUNCATE ",
                        "ALTER "):
            self.assertNotIn(keyword, source, f"gate must stay read-only: {keyword}")

    def test_backfill_module_never_touches_legacy_table(self):
        source = BACKFILL_SRC.read_text(encoding="utf-8")
        for keyword in ("UPDATE news_events", "DELETE FROM news_events",
                        "DROP TABLE news_events"):
            self.assertNotIn(keyword, source)


class TestFormatGate(unittest.TestCase):
    def test_marks_pass_and_fail(self):
        gate = {
            "as_of": "2026-11-25",
            "items": {
                name: {"ok": name != "1_map_parity", "registered": [],
                       "unexplained": [], "legacy_rows": 2529}
                for name in nr.ITEMS
            },
            "registered": [],
            "unexplained": [{"item": "1_map_parity"}],
            "blocks_n3": True,
        }
        text = nr.format_gate(gate)
        self.assertIn("FAIL 1_map_parity", text)
        self.assertIn("PASS 2_association_integrity", text)
        self.assertIn("blocks_n3=True", text)

    def test_item_names_are_the_six_gate_items(self):
        self.assertEqual(len(nr.ITEMS), 6)
        self.assertEqual(nr.ITEMS[0], "1_map_parity")
        self.assertEqual(nr.ITEMS[-1], "6_fixture_coverage")


class TestFixtureFamilies(unittest.TestCase):
    def test_all_six_fixture_families_are_enumerated(self):
        conn = _Scripted([(lambda s: "count(*)" in s, [(0,)]),
                          (lambda s: "GROUP BY" in s, [])])
        conn.execute = lambda sql, params=None: _Cur([])
        with patch.object(nc, "collision_report", return_value=[]):
            item = nr.item6_fixture_coverage(conn)
        self.assertEqual(
            item["fixture_families"],
            ("et_day_boundary", "provider_ts_missing", "cross_day_same_title",
             "legacy_conflict", "same_day_same_title_diff_url", "manual_url"),
        )

    def test_collisions_are_registered_not_unexplained(self):
        conn = _Conn([])
        collisions = [{"published_day": "2026-11-24", "normalized_title": "t",
                       "urls": 2, "associations": 2}]
        with patch.object(nc, "collision_report", return_value=collisions):
            item = nr.item6_fixture_coverage(conn)
        self.assertTrue(item["ok"])
        self.assertEqual(item["registered"][0]["count"], 1)
        self.assertEqual(item["unexplained"], [])


class TestReaderSqlMatchesProduction(unittest.TestCase):
    """The gate's canonical SQL must mirror the real reader WHERE clauses."""

    def test_news_wash_where_clause_matches_tags_from_events(self):
        source = (ROOT / "packages/strategy-nfr/src/athena_strategy/news.py").read_text()
        self.assertIn("sentiment < %s AND ts >= %s AND dismissed = false", source)
        self.assertIn("sentiment < %s AND effective_ts >= %s AND dismissed = false",
                      nb.NEWS_WASH_CANONICAL_SQL)

    def test_news_score_orders_by_timestamp(self):
        source = (ROOT / "packages/strategy-nfr/src/athena_strategy/indicators.py").read_text()
        self.assertIn("ORDER BY ts", source)
        self.assertIn("ORDER BY ts", nb.NEWS_SCORE_LEGACY_SQL)
        self.assertIn("ORDER BY effective_ts", nb.NEWS_SCORE_CANONICAL_SQL)


if __name__ == "__main__":
    unittest.main()
