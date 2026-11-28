"""§6.1 / §6.2 / §20.3 / §21.3 — V2-P3 canonical news identity and dual-write.

Pins: UTC calendar-day identity (explicit narrow amendment of §6.2 / Q5),
naive timestamps are never silently assumed UTC, symbol is out of article
identity, legacy map is not part of the association key, dual-write is
savepoint-contained, dismiss refuses a one-to-many mapping, and the three
canonical tables join ``backup.py TABLES`` while ``strategy_events`` does not.
"""

from __future__ import annotations

import hashlib
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from athena_store import news_canonical as nc
from athena_strategy import news

ROOT = Path(__file__).resolve().parents[3]
STORE = ROOT / "packages/store/src/athena_store"
NEWS_SRC = ROOT / "packages/strategy-nfr/src/athena_strategy/news.py"
MIG = STORE / "migrations/057_news_canonical.sql"
BACKUP_SRC = STORE / "backup.py"
PYPROJECT = ROOT / "packages/store/pyproject.toml"

ET = ZoneInfo("America/New_York")


class _Cur:
    def __init__(self, rows=None, rowcount=1):
        self.rows = list(rows or [])
        self.rowcount = rowcount

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _Tx:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Conn:
    def __init__(self, *, rows=None, fail_sql=None):
        self.calls: list[tuple] = []
        self.commits = 0
        self.rows = rows or []
        self.fail_sql = fail_sql

    def transaction(self):
        return _Tx()

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if self.fail_sql and self.fail_sql in sql:
            raise RuntimeError("injected canonical write failure")
        if sql.strip().startswith("SELECT"):
            return _Cur(self.rows)
        if "RETURNING" in sql:
            return _Cur([("fp",)])
        return _Cur([])

    def commit(self):
        self.commits += 1

    def sqls(self):
        return [sql for sql, _ in self.calls]


def _ts(*args, **kwargs) -> datetime:
    return datetime(*args, tzinfo=UTC, **kwargs)


class TestIdentity(unittest.TestCase):
    def test_identity_version_is_v1(self):
        self.assertEqual(nc.IDENTITY_VERSION, "v1")

    def test_to_utc_rejects_naive(self):
        with self.assertRaises(nc.NaiveTimestamp):
            nc.to_utc(datetime(2026, 11, 25, 12, 0))
        with self.assertRaises(TypeError):
            nc.to_utc("2026-11-25")

    def test_to_utc_converts_et_offset(self):
        local = datetime(2026, 11, 25, 23, 30, tzinfo=ET)  # 04:30 UTC next day
        out = nc.to_utc(local)
        self.assertEqual(out.tzinfo, UTC)
        self.assertEqual(out.isoformat(), "2026-11-26T04:30:00+00:00")

    def test_published_day_is_utc_not_et(self):
        local = datetime(2026, 11, 25, 23, 30, tzinfo=ET)
        self.assertEqual(nc.published_day(local), date(2026, 11, 26))
        self.assertNotEqual(nc.published_day(local), local.date())

    def test_normalize_title_is_lower_only(self):
        self.assertEqual(nc.normalize_title("  Hello   WORLD  "), "  hello   world  ")
        self.assertEqual(nc.normalize_title("ＥＴＦ"), "ｅｔｆ")  # no NFKC folding

    def test_article_key_formula(self):
        day = date(2026, 11, 26)
        title = "some title"
        expected = hashlib.sha256(b"v1|2026-11-26|some title").hexdigest()
        self.assertEqual(nc.article_key(day, title), expected)
        self.assertEqual(nc.article_id(day, title), "art_" + expected[:32])

    def test_symbol_is_out_of_article_identity(self):
        day, title = date(2026, 11, 26), "same headline"
        a = nc.article_id(day, title)
        self.assertEqual(a, nc.article_id(day, title))
        self.assertNotEqual(
            nc.association_id(a, "NVDA"), nc.association_id(a, "AMD"))

    def test_cross_utc_day_same_title_is_a_different_article(self):
        self.assertNotEqual(
            nc.article_id(date(2026, 11, 25), "headline"),
            nc.article_id(date(2026, 11, 26), "headline"),
        )

    def test_build_identity_is_pure_and_complete(self):
        ident = nc.build_identity(
            title="Headline", when=_ts(2026, 11, 25, 12), symbol="nvda",
            day_precision="provider",
        )
        self.assertEqual(ident["symbol"], "NVDA")
        self.assertEqual(ident["published_day"], date(2026, 11, 25))
        self.assertEqual(ident["day_precision"], "provider")
        self.assertEqual(ident["identity_version"], "v1")
        self.assertIn("association_id", ident)
        self.assertIn("identity_hash", ident)

    def test_ingest_fallback_is_explicit(self):
        ident = nc.build_identity(
            title="Manual", when=_ts(2026, 11, 25, 12), symbol="NVDA",
            day_precision="ingest_fallback",
        )
        self.assertEqual(ident["day_precision"], "ingest_fallback")
        with self.assertRaises(ValueError):
            nc.build_identity(title="x", when=_ts(2026, 11, 25), symbol="NVDA",
                              day_precision="guess")


class TestDualWrite(unittest.TestCase):
    def test_writes_article_association_and_map_in_order(self):
        conn = _Conn()
        ident = nc.dual_write(
            conn, fingerprint="fp1", ts=_ts(2026, 11, 25, 12), symbol="NVDA",
            title="Headline", source="yahoo", url="https://a", event_type="GENERIC",
            sentiment=0.1, legacy_status="inserted",
        )
        self.assertIsNotNone(ident)
        inserts = [sql for sql in conn.sqls() if "INSERT INTO" in sql]
        self.assertEqual(len(inserts), 3)
        self.assertIn("INSERT INTO news_articles", inserts[0])
        self.assertIn("INSERT INTO news_article_symbols", inserts[1])
        self.assertIn("INSERT INTO news_legacy_event_map", inserts[2])
        self.assertIn("ON CONFLICT (article_id) DO UPDATE", inserts[0])
        self.assertIn("ON CONFLICT (article_id, symbol) DO UPDATE", inserts[1])
        self.assertIn("ON CONFLICT (legacy_fingerprint, article_id)", inserts[2])

    def test_conflict_existing_does_not_skip_canonical(self):
        conn = _Conn()
        nc.dual_write(
            conn, fingerprint="fp1", ts=_ts(2026, 11, 25, 12), symbol="NVDA",
            title="Headline", source="yahoo", url="https://a", event_type="GENERIC",
            sentiment=0.1, legacy_status="conflict_existing",
        )
        params = [p for sql, p in conn.calls if "news_legacy_event_map" in sql][0]
        self.assertEqual(params[4], "conflict_existing")

    def test_canonical_failure_is_contained(self):
        conn = _Conn(fail_sql="INSERT INTO news_articles")
        self.assertIsNone(nc.dual_write(
            conn, fingerprint="fp1", ts=_ts(2026, 11, 25, 12), symbol="NVDA",
            title="Headline", source="yahoo", url="", event_type="GENERIC",
            sentiment=0.0, legacy_status="inserted",
        ))

    def test_rejects_unknown_legacy_status(self):
        conn = _Conn()
        with self.assertRaises(ValueError):
            nc.dual_write(
                conn, fingerprint="fp1", ts=_ts(2026, 11, 25, 12), symbol="NVDA",
                title="x", source="y", url="", event_type="GENERIC",
                sentiment=0.0, legacy_status="invented",
            )

    def test_uses_a_savepoint_so_legacy_insert_survives(self):
        class NoTx(_Conn):
            def transaction(self):
                raise AttributeError("no savepoint support")

        self.assertIsNone(nc.dual_write(
            NoTx(), fingerprint="fp1", ts=_ts(2026, 11, 25, 12), symbol="NVDA",
            title="x", source="y", url="", event_type="GENERIC",
            sentiment=0.0, legacy_status="inserted",
        ))


class TestDismiss(unittest.TestCase):
    def test_no_mapping_returns_none(self):
        self.assertIsNone(nc.resolve_association_id(_Conn(rows=[]), "fp"))

    def test_one_mapping_returns_the_association(self):
        self.assertEqual(
            nc.resolve_association_id(_Conn(rows=[("nas_1",)]), "fp"), "nas_1")

    def test_one_to_many_is_refused(self):
        with self.assertRaises(nc.AmbiguousAssociation):
            nc.resolve_association_id(_Conn(rows=[("nas_1",), ("nas_2",)]), "fp")

    def test_explicit_association_id_bypasses_resolution(self):
        conn = _Conn(rows=[("nas_1",), ("nas_2",)])
        with patch("athena_store.news_canonical.resolve_association_id") as resolve:
            self.assertTrue(news.dismiss_event(conn, "fp", association_id="nas_2"))
            resolve.assert_not_called()
        self.assertTrue(any("UPDATE news_article_symbols" in sql for sql in conn.sqls()))

    def test_ambiguous_dismiss_propagates_without_touching_legacy(self):
        conn = _Conn(rows=[("nas_1",), ("nas_2",)])
        with self.assertRaises(nc.AmbiguousAssociation):
            news.dismiss_event(conn, "fp")
        self.assertFalse(any("UPDATE news_events" in sql for sql in conn.sqls()))

    def test_dismiss_without_canonical_row_still_updates_legacy(self):
        conn = _Conn(rows=[])
        self.assertTrue(news.dismiss_event(conn, "fp"))
        self.assertTrue(any("UPDATE news_events" in sql for sql in conn.sqls()))
        self.assertEqual(conn.commits, 1)

    def test_mark_dismissed_and_sentiment_are_targeted(self):
        conn = _Conn()
        nc.mark_dismissed(conn, "nas_1")
        nc.mark_sentiment(conn, "fp1", 0.5)
        joined = " ".join(conn.sqls())
        self.assertIn("WHERE association_id = %s AND dismissed = false", joined)
        self.assertIn("news_legacy_event_map AS m", joined)


class TestReconcile(unittest.TestCase):
    def test_reconcile_returns_all_six_checks(self):
        conn = _Conn(rows=[(0,)])
        out = nc.reconcile(conn)
        for key in ("legacy_rows", "map_rows", "distinct_map", "articles",
                    "associations", "orphan_associations",
                    "distinct_article_identities"):
            self.assertIn(key, out)
        self.assertEqual(len(conn.calls), 7)

    def test_collision_report_groups_same_day_title_by_url(self):
        conn = _Conn(rows=[(date(2026, 11, 25), "headline", 2, 3)])
        rows = nc.collision_report(conn)
        self.assertEqual(rows[0]["urls"], 2)
        self.assertEqual(rows[0]["associations"], 3)
        self.assertIn("HAVING count(DISTINCT s.url) > 1", conn.sqls()[0])


class TestMigrationAndWiring(unittest.TestCase):
    def test_migration_creates_three_tables_and_keeps_legacy(self):
        text = MIG.read_text(encoding="utf-8")
        for table in ("news_articles", "news_article_symbols", "news_legacy_event_map"):
            self.assertIn(f"CREATE TABLE {table}", text)
        self.assertIn("UNIQUE (identity_version, identity_hash)", text)
        self.assertIn("UNIQUE (article_id, symbol)", text)
        self.assertIn("PRIMARY KEY (legacy_fingerprint, article_id)", text)
        for forbidden in ("DROP TABLE", "ALTER TABLE news_events",
                          "DELETE FROM news_events", "UPDATE news_events"):
            self.assertNotIn(forbidden, text)

    def test_no_semicolon_inside_comments(self):
        for line in MIG.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("--"):
                self.assertNotIn(";", line, line)

    def test_wheel_registers_057(self):
        self.assertIn("057_news_canonical.sql", PYPROJECT.read_text(encoding="utf-8"))

    def test_backup_tables_add_exactly_the_three_news_tables(self):
        from athena_store.backup import TABLES

        self.assertIn("news_events", TABLES)
        for table in ("news_articles", "news_article_symbols", "news_legacy_event_map"):
            self.assertIn(table, TABLES)
        self.assertNotIn("strategy_events", TABLES)
        self.assertEqual(len(TABLES), 23)

    def test_news_writers_dual_write(self):
        text = NEWS_SRC.read_text(encoding="utf-8")
        self.assertIn("from athena_store import news_canonical as nc", text)
        self.assertEqual(text.count("nc.dual_write("), 2)
        self.assertIn('day_precision="ingest_fallback"', text)
        self.assertNotIn('parsed.replace(tzinfo=UTC)', text)
        self.assertIn("nc.to_utc(now)", text)

    def test_yahoo_naive_pubdate_falls_back_to_ingestion(self):
        xml = ("<rss><channel><item><title>Headline</title>"
               "<link>https://a</link><pubDate>Wed, 25 Nov 2026 12:00:00</pubDate>"
               "</item></channel></rss>")
        now = _ts(2026, 11, 25, 13)
        rows = news.parse_yahoo_rss(xml, now)
        self.assertEqual(rows[0][0], now)
        self.assertEqual(rows[0][0].tzinfo, UTC)

    def test_yahoo_offset_pubdate_is_converted_to_utc(self):
        xml = ("<rss><channel><item><title>Headline</title>"
               "<link>https://a</link><pubDate>Wed, 25 Nov 2026 23:30:00 -0500</pubDate>"
               "</item></channel></rss>")
        rows = news.parse_yahoo_rss(xml, _ts(2026, 11, 25, 13))
        self.assertEqual(rows[0][0].isoformat(), "2026-11-26T04:30:00+00:00")

    def test_dismiss_signature_accepts_explicit_association_id(self):
        text = NEWS_SRC.read_text(encoding="utf-8")
        self.assertIn("association_id: str | None = None", text)
        self.assertIn("nc.resolve_association_id(conn, fingerprint)", text)
        self.assertIn("nc.mark_dismissed(conn, association_id)", text)


class TestP3Fixtures(unittest.TestCase):
    """§20.3.5 / §21.3: UTC day, provider missing, cross-day, conflict, URL."""

    def test_utc_day_boundary_fixture(self):
        before = datetime(2026, 11, 25, 22, 0, tzinfo=ET)  # 03:00 UTC 26th
        after = before + timedelta(hours=4)                  # 07:00 UTC 26th
        self.assertEqual(nc.published_day(before), nc.published_day(after))
        self.assertEqual(nc.published_day(before), date(2026, 11, 26))

    def test_provider_missing_uses_ingest_fallback(self):
        ident = nc.build_identity(
            title="Manual paste", when=_ts(2026, 11, 25, 9), symbol="NVDA",
            day_precision="ingest_fallback",
        )
        self.assertEqual(ident["day_precision"], "ingest_fallback")
        self.assertEqual(ident["effective_ts"], ident["original_ts"])

    def test_cross_utc_day_same_title_not_merged(self):
        a = nc.build_identity(title="Same", when=_ts(2026, 11, 25, 23, 59),
                              symbol="NVDA")
        b = nc.build_identity(title="Same", when=_ts(2026, 11, 26, 0, 1),
                              symbol="NVDA")
        self.assertNotEqual(a["article_id"], b["article_id"])

    def test_same_day_same_title_different_urls_is_one_article(self):
        a = nc.build_identity(title="Same", when=_ts(2026, 11, 25, 10),
                              symbol="NVDA")
        b = nc.build_identity(title="Same", when=_ts(2026, 11, 25, 11),
                              symbol="NVDA")
        self.assertEqual(a["article_id"], b["article_id"])
        self.assertNotEqual(
            nc.association_id(a["article_id"], "NVDA"),
            nc.association_id(a["article_id"], "AMD"),
        )

    def test_legacy_conflict_still_writes_canonical(self):
        conn = _Conn()
        nc.dual_write(
            conn, fingerprint="fp", ts=_ts(2026, 11, 25, 10), symbol="NVDA",
            title="Headline", source="yahoo", url="https://a",
            event_type="GENERIC", sentiment=0.0,
            legacy_status="conflict_existing",
        )
        self.assertEqual(len([s for s in conn.sqls() if "INSERT INTO" in s]), 3)

    def test_naive_source_timestamp_falls_back_and_is_marked(self):
        conn = _Conn()
        with patch("athena_strategy.news.nc.dual_write") as dw:
            news._save_headlines(
                conn, "NVDA", "manual",
                [(datetime(2026, 11, 25, 12), "Headline", "")],
            )
        self.assertEqual(dw.call_args.kwargs["day_precision"], "ingest_fallback")
        self.assertEqual(dw.call_args.kwargs["ts"].tzinfo, UTC)

    def test_manual_url_fixture_is_a_fallback_timestamp(self):
        text = NEWS_SRC.read_text(encoding="utf-8")
        self.assertIn('day_precision="ingest_fallback",\n    )', text)
        self.assertIn('"manual", [(datetime.now(UTC), title, "")]', text)


if __name__ == "__main__":
    unittest.main()
