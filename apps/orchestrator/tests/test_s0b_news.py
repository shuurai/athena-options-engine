"""S0b (booklet 16 §16.2 / §16.5) — news stays an information lens, no AI.

Pins the two things S0b changes:

* ``ingest_news`` no longer runs AI sentiment enrichment or the ingest-side
  wash-tag loop (a static mutation trap: reintroducing any AI news-analysis
  symbol must fail);
* ``ingest_url`` stays operator CRUD but is deterministic — it stores the page
  headline for the watched ticker the page actually mentions, with no model,
  provider or token in its signature.
"""

from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from unittest.mock import patch

from athena_strategy import news

ROOT = Path(__file__).resolve().parents[3]
NEWS_SRC = ROOT / "packages/strategy-nfr/src/athena_strategy/news.py"


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
    def __init__(self):
        self.calls: list[tuple] = []
        self.commits = 0

    def transaction(self):
        return _Tx()

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if sql.strip().startswith("SELECT"):
            return _Cur([])
        if "RETURNING" in sql:
            return _Cur([("fp",)])
        return _Cur([])

    def commit(self):
        self.commits += 1

    def sqls(self):
        return [sql for sql, _ in self.calls]


def _page(title: str, body: str) -> str:
    return f"<html><head><title>{title}</title></head><body><p>{body}</p></body></html>"


class TestNoAiNewsAnalysis(unittest.TestCase):
    def test_news_module_calls_no_ai_news_analysis(self):
        text = NEWS_SRC.read_text(encoding="utf-8")
        for gone in ("enrich_sentiment", "read_article", "parse_scores",
                     "scoring_model", "deepseek_token", "NEWS_TYPE_CODES"):
            with self.subTest(symbol=gone):
                self.assertNotIn(gone, text)

    def test_ingest_url_signature_has_no_ai_arguments(self):
        params = inspect.signature(news.ingest_url).parameters
        self.assertEqual(list(params), ["conn", "url", "symbols"])

    def test_canonical_dual_write_sites_are_unchanged(self):
        text = NEWS_SRC.read_text(encoding="utf-8")
        self.assertEqual(text.count("nc.dual_write("), 2)


class TestIngestUrlIsDeterministicCrud(unittest.TestCase):
    def test_stores_headline_for_the_watched_ticker_on_the_page(self):
        html = _page("NVDA slips on guidance",
                     "Nvidia shares fell after the company cut its outlook. " * 3)
        conn = _Conn()
        with (
            patch("athena_strategy.news.public_url", return_value="https://example.com/a"),
            patch("athena_strategy.news._fetch_public", return_value=html),
        ):
            note = news.ingest_url(conn, "https://example.com/a", ["NVDA", "AMD"])
        self.assertTrue(note.startswith("Stored NVDA."), note)
        self.assertTrue(any("INSERT INTO news_events" in sql for sql in conn.sqls()))
        self.assertEqual(conn.commits, 1)

    def test_refuses_a_page_that_mentions_no_watched_ticker(self):
        html = _page("Markets today",
                     "A broad look at the session and its macro drivers. " * 3)
        with (
            patch("athena_strategy.news.public_url", return_value="https://example.com/a"),
            patch("athena_strategy.news._fetch_public", return_value=html),
            self.assertRaises(ValueError),
        ):
            news.ingest_url(_Conn(), "https://example.com/a", ["NVDA"])

    def test_refuses_a_page_without_article_text(self):
        with (
            patch("athena_strategy.news.public_url", return_value="https://example.com/a"),
            patch("athena_strategy.news._fetch_public", return_value="<html><body></body></html>"),
            self.assertRaises(ValueError),
        ):
            news.ingest_url(_Conn(), "https://example.com/a", ["NVDA"])


if __name__ == "__main__":
    unittest.main()
