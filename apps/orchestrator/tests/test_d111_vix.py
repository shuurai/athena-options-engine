"""D111: data-only VIX history plus English, consistent Guardian UI (§31).

The tests use no live IB session and no live database writes. Trading watchlists,
orders, drafts, option chains, streaming quotes, and notification paths stay out
of scope; dynamic Advisor/draft business reasons remain escaped, not translated.
"""
from __future__ import annotations

import re
import unittest
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from athena_exec import broker as broker_module
from athena_exec.broker import IbBroker
from athena_marketdata import vixfeed
from athena_marketdata.pricing import store_ib_daily
from athena_orchestrator import backfill as bf
from athena_orchestrator import guardian_regime as gr
from athena_orchestrator import guardian_runner as runs
from athena_orchestrator import guardian_view as gv
from athena_orchestrator import pages
from athena_risk import GuardianReport, Tier
from athena_settings.model import (
    DATA_SYMBOLS_SEED,
    UNIVERSE_SEED,
    SettingsPayload,
    standard_payload,
)
from athena_strategy import regime as rg

ROOT = Path(__file__).resolve().parents[3]
CJK = re.compile(r"[\u3400-\u9fff]")


class _Cursor:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _RecordingConn:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        return _Cursor(self.rows)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _Event:
    def __iadd__(self, listener):
        return self

    def __isub__(self, listener):
        return self


class _HistoryIB:
    def __init__(self):
        self.errorEvent = _Event()
        self.head_calls = []
        self.history_calls = []

    def reqHeadTimeStamp(self, *args):
        self.head_calls.append(args)
        return datetime(2020, 1, 2, tzinfo=UTC)

    def reqHistoricalData(self, *args):
        self.history_calls.append(args)
        return []


def _empty_report():
    return GuardianReport(
        portfolio_tier=Tier.HEALTHY,
        account_tier=Tier.HEALTHY,
        positions=(),
        account_flags=(),
        el_nl_pct=None,
        maint_nl_pct=None,
        breach_count=0,
        proximity_count=0,
        unmanaged_count=0,
    )


class TestDataSymbols(unittest.TestCase):
    def test_default_is_exactly_the_four_vix_series(self):
        self.assertEqual(DATA_SYMBOLS_SEED, ("VIX", "VIX9D", "VIX3M", "VIX1Y"))
        self.assertEqual(standard_payload().data_symbols, list(DATA_SYMBOLS_SEED))

    def test_old_settings_gain_the_default_without_a_migration(self):
        raw = standard_payload().model_dump()
        raw.pop("data_symbols")
        loaded = SettingsPayload.model_validate(raw)
        self.assertEqual(loaded.data_symbols, list(DATA_SYMBOLS_SEED))

    def test_data_symbols_do_not_contaminate_trading_universe(self):
        settings = standard_payload()
        self.assertTrue(set(DATA_SYMBOLS_SEED).isdisjoint(settings.watchlist))
        self.assertTrue(set(DATA_SYMBOLS_SEED).isdisjoint(UNIVERSE_SEED))
        self.assertEqual(len(UNIVERSE_SEED), 39)

    def test_history_merge_is_ordered_normalized_and_non_mutating(self):
        watch = ["NVDA", " spy "]
        data = [" vix ", "VIX9D", "SPY", "", "VIX9D"]
        merged = bf.history_symbols([watch], ["SPY", "GLD"], [data, ["VIX3M", "VIX1Y"]])
        self.assertEqual(merged, ["NVDA", "SPY", "GLD", "VIX", "VIX9D", "VIX3M", "VIX1Y"])
        self.assertEqual(watch, ["NVDA", " spy "])
        self.assertEqual(data, [" vix ", "VIX9D", "SPY", "", "VIX9D"])

    def test_configured_data_symbols_is_an_ordered_account_union(self):
        with patch.object(bf, "_data_books", return_value=[
            ("U2", ["vix", "VIX3M"]), ("U1", ["VIX", "VIX9D", "VIX1Y"]),
        ]):
            self.assertEqual(
                bf.configured_data_symbols("postgresql://unused"),
                ["VIX", "VIX3M", "VIX9D", "VIX1Y"],
            )

    def test_startup_backfill_pulls_watchlist_regime_and_data_symbols(self):
        broker = Mock()
        broker.connect.return_value = True
        conn = MagicMock()
        conn.__enter__.return_value = conn
        pulled = []

        def pull(_conn, _broker, symbol):
            pulled.append(symbol)
            return 300, "stored"

        with (
            patch.object(bf, "_books", return_value=[("U1", ["SPY"], "SPY")]),
            patch.object(bf, "_data_books", return_value=[("U1", list(DATA_SYMBOLS_SEED))]),
            patch.object(bf, "IbBroker", return_value=broker),
            patch.object(bf, "connect", return_value=conn),
            patch.object(bf, "_stored_or_pull", side_effect=pull),
            patch.object(bf, "set_history_short"),
            patch.object(bf.time, "sleep"),
        ):
            bf._run("postgresql://unused")

        self.assertEqual(pulled, ["SPY", *DATA_SYMBOLS_SEED])
        broker.connect.assert_called_once()
        self.assertTrue(broker.connect.call_args.args[-1])
        broker.disconnect.assert_called_once_with()


class TestHistoryContracts(unittest.TestCase):
    def test_index_contract_matches_the_ibkr_request(self):
        contract = broker_module._index_contract(" vix ")
        self.assertEqual(contract.secType, "IND")
        self.assertEqual(contract.symbol, "VIX")
        self.assertEqual(contract.exchange, "CBOE")
        self.assertEqual(contract.currency, "USD")

    def test_every_data_symbol_uses_ind_and_never_stock_qualification(self):
        broker = IbBroker()
        for symbol in DATA_SYMBOLS_SEED:
            with self.subTest(symbol=symbol):
                with (
                    patch.object(broker_module, "_index_contract", side_effect=lambda name: f"IND:{name}") as index,
                    patch.object(broker, "_qualify_stock") as stock,
                ):
                    self.assertEqual(broker._qualify_history(f" {symbol.lower()} "), f"IND:{symbol}")
                    index.assert_called_once_with(symbol)
                    stock.assert_not_called()

    def test_ordinary_history_names_keep_stock_routing(self):
        broker = IbBroker()
        with (
            patch.object(broker_module, "_index_contract") as index,
            patch.object(broker, "_qualify_stock", return_value="STK:SPY") as stock,
        ):
            self.assertEqual(broker._qualify_history(" spy "), "STK:SPY")
            stock.assert_called_once_with("SPY")
            index.assert_not_called()

    def test_head_and_span_share_the_history_resolver_and_request_shape(self):
        broker = IbBroker()
        broker._ib = _HistoryIB()
        contract = SimpleNamespace(symbol="VIX", secType="IND")
        with patch.object(broker, "_qualify_history", return_value=contract) as resolve:
            self.assertEqual(broker.head_day("VIX"), date(2020, 1, 2))
            self.assertEqual(broker.history_span("VIX", "5 D"), [])
        self.assertEqual(resolve.call_args_list, [unittest.mock.call("VIX"), unittest.mock.call("VIX")])
        self.assertEqual(broker._ib.head_calls[0], (contract, "TRADES", True, 2))
        self.assertEqual(
            broker._ib.history_calls[0],
            (contract, "", "5 D", "1 day", "TRADES", True, 1),
        )

    def test_index_history_reuses_the_same_contract_and_daily_bar_size(self):
        broker = IbBroker()
        with patch.object(broker, "historical", return_value=[]) as historical:
            self.assertEqual(broker.index_history("vix9d", "6 M"), [])
        contract, duration, bar_size = historical.call_args.args
        self.assertEqual((contract.secType, contract.symbol, contract.exchange, contract.currency),
                         ("IND", "VIX9D", "CBOE", "USD"))
        self.assertEqual((duration, bar_size), ("6 M", "1 day"))

    def test_new_vix_rows_use_the_normal_ib_daily_writer(self):
        conn = _RecordingConn()
        bar = (date(2026, 11, 23), Decimal("18"), Decimal("19"), Decimal("17"),
               Decimal("18.5"), Decimal(0))
        self.assertEqual(store_ib_daily(conn, "vix", [bar]), 1)
        sql, params = conn.calls[0]
        self.assertIn("'ib-daily'", sql)
        self.assertNotIn("ib-vix", sql)
        self.assertEqual(params[0], "VIX")
        self.assertEqual(params[5], Decimal("18.5"))


class TestDualSourceReads(unittest.TestCase):
    def test_daily_row_wins_a_duplicate_day_regardless_of_row_order(self):
        day = date(2026, 11, 23)
        daily = (day, 20.0, "ib-daily")
        legacy = (day, 18.0, "ib-vix")
        for rows in ([legacy, daily], [daily, legacy]):
            with self.subTest(rows=rows):
                conn = _RecordingConn([*rows, (date(2026, 11, 24), 21.0, "ib-vix")])
                self.assertEqual(vixfeed.vix_closes(conn, "VIX", 5), [
                    (day, 20.0), (date(2026, 11, 24), 21.0),
                ])

    def test_legacy_rows_remain_a_fallback(self):
        day = date(2026, 11, 23)
        conn = _RecordingConn([(day, "18.5", "ib-vix")])
        self.assertEqual(vixfeed.vix_closes(conn, "VIX", 1), [(day, 18.5)])

    def test_limit_is_applied_after_duplicate_days_are_removed(self):
        day = date(2026, 11, 23)
        conn = _RecordingConn([
            (day, 18.0, "ib-vix"), (day, 20.0, "ib-daily"),
            (date(2026, 11, 24), 21.0, "ib-daily"),
            (date(2026, 11, 25), 22.0, "ib-daily"),
        ])
        self.assertEqual(vixfeed.vix_closes(conn, "VIX", 2), [
            (date(2026, 11, 24), 21.0), (date(2026, 11, 25), 22.0),
        ])

    def test_sql_selects_both_sources_and_positive_closes(self):
        conn = _RecordingConn()
        vixfeed.vix_closes(conn, " vix ", 5)
        sql, params = conn.calls[0]
        self.assertIn("source IN (%s, %s)", sql)
        self.assertIn("close > 0", sql)
        self.assertEqual(params, ("VIX", "ib-daily", "ib-vix"))


class TestManualSyncSafety(unittest.TestCase):
    def test_worker_uses_probe_lock_readonly_and_isolates_one_failure(self):
        broker = Mock()
        broker.connect.return_value = True
        conn = Mock()
        context = MagicMock()
        context.__enter__.return_value = conn
        ib_lock = MagicMock()
        calls = []

        def pull(_conn, _broker, symbol):
            calls.append(symbol)
            if symbol == "VIX9D":
                raise RuntimeError("no entitlement")
            return 300, "catchup"

        with (
            patch("athena_orchestrator.backfill.configured_data_symbols",
                  return_value=list(DATA_SYMBOLS_SEED)),
            patch("athena_orchestrator.backfill._stored_or_pull", side_effect=pull),
            patch("athena_exec.broker.IbBroker", return_value=broker),
            patch("athena_exec.clients.client_id", return_value=777) as client,
            patch("athena_orchestrator.tasks._IB_LOCK", ib_lock),
            patch("athena_store.connect", return_value=context),
            patch.dict(pages.os.environ, {"IB_GATEWAY_HOST": "ib.test", "IB_GATEWAY_PORT": "4002"}),
        ):
            pages._sync_vix("postgresql://unused")

        self.assertEqual(calls, list(DATA_SYMBOLS_SEED))
        client.assert_called_once_with("probe")
        ib_lock.__enter__.assert_called_once_with()
        broker.connect.assert_called_once_with("ib.test", 4002, 777, True)
        broker.disconnect.assert_called_once_with()
        self.assertEqual(conn.commit.call_count, 3)
        conn.rollback.assert_called_once_with()

    def test_duplicate_click_returns_without_touching_configuration_or_ib(self):
        self.assertTrue(pages._VIX_SYNC_LOCK.acquire(blocking=False))
        try:
            with patch("athena_orchestrator.backfill.configured_data_symbols") as configured:
                pages._sync_vix("postgresql://unused")
                configured.assert_not_called()
        finally:
            pages._VIX_SYNC_LOCK.release()

    def test_in_flight_lock_is_released_after_an_exception(self):
        with patch("athena_orchestrator.backfill.configured_data_symbols",
                   side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                pages._sync_vix("postgresql://unused")
        self.assertFalse(pages._VIX_SYNC_LOCK.locked())
        with patch("athena_orchestrator.backfill.configured_data_symbols", return_value=[]):
            pages._sync_vix("postgresql://unused")
        self.assertFalse(pages._VIX_SYNC_LOCK.locked())


class TestEnglishConsistentUI(unittest.TestCase):
    def test_guard_page_fixed_copy_is_english_and_accurate(self):
        snapshot = SimpleNamespace(report=_empty_report(), base_currency="USD")
        history = [{"trade_day": "2026-11-24", "run": "A", "hk": "21:30",
                    "portfolio_tier": "healthy", "breach_count": 0, "status": "ok"}]
        with (
            patch.object(pages, "_guard_inputs", return_value=(standard_payload(), [], [])),
            patch.object(gv, "sync", return_value=(snapshot, [])),
            patch.object(runs, "load_runs", return_value=history),
        ):
            built = pages._guard_page(_RecordingConn(), "U1", {})
        body = built["body"]
        chips = " ".join(text for _tone, text in built["chips"])
        self.assertIsNone(CJK.search(body + chips))
        self.assertIn("live grading", chips)
        self.assertNotIn("read only", chips)
        self.assertIn("Run C is the midday regime observation on Iris.", body)
        self.assertIn('<table class="data">', body)
        self.assertIn('<p class="slot-note">', body)
        self.assertNotIn("guard-history", body)

    def test_iris_fixed_copy_is_english_and_consistent(self):
        found = {
            "trade_day": date(2026, 11, 24), "run": "C", "label": "insufficient_data",
            "close_bias": "insufficient_data", "evidence": {},
            "inputs": {"vix_slope": "flat", "vix_hits": 1},
            "created_at": datetime(2026, 11, 24, 17, 30, tzinfo=UTC), "is_real": False,
        }
        with (
            patch.object(pages, "load_active", return_value=standard_payload()),
            patch.object(pages, "_position_rows", return_value=[]),
            patch.object(pages, "_pending_drafts", return_value=[]),
            patch("athena_strategy.advisor.today_signals", return_value={}),
            patch.object(gr, "latest_label", return_value=found),
        ):
            body = pages._iris_page(_RecordingConn(), "U1", {})["body"]
        self.assertIsNone(CJK.search(body))
        self.assertIn("Midday Regime · Run C", body)
        self.assertIn("VIX dimensions known 1/3", body)
        self.assertNotIn("VIX hits", body)
        self.assertIn("VIX slope", body)
        self.assertIn("Term structure", body)
        self.assertIn("Single-name IV", body)
        self.assertIn("VIX history syncs at engine start", body)
        self.assertIn('<table class="data">', body)
        self.assertIn('<p class="slot-note">', body)
        self.assertNotIn("guard-history", body)
        self.assertEqual(body.count("Midday Regime"), 1)

    def test_tier_labels_and_regime_line_are_english(self):
        self.assertEqual([meta[0] for _tier, meta in gv._TIER_META.items()],
                         ["Blowup", "Danger", "Balanced", "Healthy", "Profit"])
        line = rg.evaluate(rg.Axes(stock="up", gold="up", numbers={"vix_level": 18.0})).line()
        self.assertIsNone(CJK.search(line))
        self.assertIn("Midday Regime:", line)
        self.assertIn("Gold up", line)

    def test_dynamic_business_reason_is_preserved_and_escaped(self):
        pending = [{"id": "draft<1>", "symbol": "NVDA", "tactic": "BUY",
                    "reason": "继续持有 <script>alert(1)</script>"}]
        with patch.object(pages, "_pending_drafts", return_value=pending):
            html = pages._advisor_actions_table(
                _RecordingConn(), "U1", standard_payload(), [], {}, date(2026, 11, 24),
            )
        self.assertIn("继续持有", html)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("draft&lt;1&gt;", html)
        self.assertIn('<td class="advisor-reason">', html)
        self.assertNotIn('style="white-space:pre-wrap"', html)

    def test_shared_css_covers_empty_states_flex_and_narrow_layout(self):
        from athena_orchestrator.console import _EXTRA_CSS

        empty_css = _EXTRA_CSS.split(".guard-empty {", 1)[1].split("}", 1)[0]
        self.assertIn("font-size: 12.5px", empty_css)
        self.assertIn("line-height: 1.45", empty_css)
        self.assertIn(".stage > .regime-card { flex: none; }", _EXTRA_CSS)
        self.assertIn(".advisor-reason { white-space: pre-wrap; }", _EXTRA_CSS)
        self.assertIn("@media (max-width: 1100px)", _EXTRA_CSS)
        self.assertIn(".settings-cols.analysis { grid-template-columns: minmax(0, 1fr); }", _EXTRA_CSS)
        self.assertNotIn(".guard-history", _EXTRA_CSS)


class TestDecisionRecord(unittest.TestCase):
    def test_d111_contract_and_counts_are_recorded(self):
        booklet = (ROOT / "docs/plan/14-guardian-escalation.md").read_text(encoding="utf-8")
        order = (ROOT / "docs/plan/00-plan-order.md").read_text(encoding="utf-8")
        settings = (ROOT / "docs/athena/04-settings-and-risk.md").read_text(encoding="utf-8")
        self.assertIn("## 31. English console presentation", booklet)
        self.assertIn("D1–D111 / **112 个节点**", booklet)
        self.assertIn("D1–D111 / 112 个节点全部有裁定", order)
        self.assertIn("54 条：50 取推荐", order)
        self.assertIn("| `data_symbols` |", settings)

    def test_no_schema_migration_or_streaming_vix_path_was_added(self):
        migrations = list((ROOT / "packages/store/src/athena_store/migrations").glob("0*.sql"))
        # V2-P2/P3 C-20261125-6/7: 056 strategy_events, 057 news_canonical.
        self.assertEqual(len(migrations), 53)
        for path in (
            ROOT / "apps/orchestrator/src/athena_orchestrator/backfill.py",
            ROOT / "apps/orchestrator/src/athena_orchestrator/pages.py",
            ROOT / "packages/marketdata/src/athena_marketdata/vixfeed.py",
        ):
            text = path.read_text(encoding="utf-8")
            body = text.split("def _sync_vix", 1)[-1] if path.name == "pages.py" else text
            self.assertNotIn("reqMktData", body)
            self.assertNotIn("pendingTickersEvent", body)


if __name__ == "__main__":
    unittest.main()
