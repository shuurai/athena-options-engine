"""Replay, approve, and phase chains on account ZZTEST.

Skipped when DATABASE_URL is unset or Postgres is down. These tests never
read or write the paper account and never call placeOrder.
"""

import json
import os
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock, patch

from athena_exec.gate import live_block_reason
from athena_exec.halt import halt as book_halt
from athena_exec.session import _submit_queued
from athena_settings.model import TacticFlags, standard_payload
from athena_store.migrate import apply_migrations, connect
from athena_strategy.agents import run_legacy_signaled
from athena_strategy.drafts import decide
from athena_strategy.iv import estimated_iv, record_iv_marks, tags_from_iv
from athena_strategy.manage import manage_book, management_tactic
from athena_strategy.phases import evaluate_symbol
from athena_strategy.tags import OVERSOLD, SUPPORT_TOUCH

ACCOUNT = "ZZTEST"
RUN = "sim_zztest"
SYMBOL = "ZZT"


def _ib_trade(order_id: int, perm_id: int, symbol: str, action: str, qty: int, status: str, limit: str):
    from types import SimpleNamespace

    return SimpleNamespace(
        order=SimpleNamespace(
            orderId=order_id,
            permId=perm_id,
            account=ACCOUNT,
            action=action,
            totalQuantity=qty,
            orderType="LMT",
            lmtPrice=limit,
            auxPrice=0,
        ),
        contract=SimpleNamespace(symbol=symbol, secType="STK", multiplier="1"),
        orderStatus=SimpleNamespace(status=status, filled=0, remaining=qty, avgFillPrice=0),
    )


def _url() -> str:
    return os.environ.get("DATABASE_URL", "").strip()


def setUpModule() -> None:
    url = _url()
    if not url:
        raise unittest.SkipTest("DATABASE_URL is not set")
    try:
        conn = connect(url)
        conn.close()
    except Exception as exc:
        raise unittest.SkipTest(f"Postgres is not available: {exc}")


class WorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(_url())
        apply_migrations(self.conn)
        self.paper = patch("athena_orchestrator.sim.trading_mode", return_value="paper")
        self.paper.start()
        self._wipe()

    def tearDown(self) -> None:
        self.conn.rollback()
        self._wipe()
        self.paper.stop()
        self.conn.close()

    def _wipe(self) -> None:
        drafts = "SELECT id FROM drafts WHERE account_id = %s"
        self.conn.execute(f"DELETE FROM control_links WHERE draft_id IN ({drafts})", (ACCOUNT,))
        self.conn.execute(f"DELETE FROM approvals WHERE draft_id IN ({drafts})", (ACCOUNT,))
        self.conn.execute(f"DELETE FROM orders WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute(f"DELETE FROM fills WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM drafts WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM tickets WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM positions WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM sim_runs WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM symbol_phases WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM halts WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM decision_trails WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM ai_calls WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM nav_marks WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM account_marks WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM option_plans WHERE symbol = %s", (SYMBOL,))
        self.conn.execute("DELETE FROM option_choices WHERE symbol = %s", (SYMBOL,))
        self.conn.execute("DELETE FROM market_bars WHERE symbol = %s", (SYMBOL,))
        self.conn.execute("DELETE FROM iv_marks WHERE symbol = %s", (SYMBOL,))
        self.conn.execute("DELETE FROM rv_marks WHERE symbol = %s", (SYMBOL,))
        self.conn.execute("DELETE FROM chain_marks WHERE symbol = %s", (SYMBOL,))
        self.conn.execute("DELETE FROM symbol_facts WHERE symbol = %s", (SYMBOL,))
        self.conn.commit()

    def _run(self, status: str = "active", symbols: str = "AAA,BBB", cursor=None, run_id: str = RUN) -> None:
        self.conn.execute(
            """
            INSERT INTO sim_runs (id, account_id, symbols, start_date, end_date, cursor_date, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (run_id, ACCOUNT, symbols, date(2020, 1, 2), date(2020, 1, 10), cursor, status),
        )
        self.conn.commit()

    def _bar(self, symbol: str, day: date, run_id: str = RUN) -> None:
        self.conn.execute(
            """
            INSERT INTO sim_bars (run_id, symbol, ts, open, high, low, close, volume)
            VALUES (%s, %s, %s, 10, 12, 9, 11, 100)
            """,
            (run_id, symbol, datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc)),
        )
        self.conn.commit()

    def _draft(self, draft_id: str, simulated: bool) -> None:
        self.conn.execute(
            """
            INSERT INTO drafts (
                id, symbol, tactic, contracts, status, reason, phase,
                account_id, instrument, limit_price, simulated
            )
            VALUES (%s, %s, 'BUY', 1, 'pending', 'test', 'P1', %s, 'STK', 12.5, %s)
            """,
            (draft_id, SYMBOL, ACCOUNT, simulated),
        )
        self.conn.commit()

    def _cursor(self) -> date | None:
        row = self.conn.execute("SELECT cursor_date, status FROM sim_runs WHERE id = %s", (RUN,)).fetchone()
        return None if row is None else row

    def test_a_pending_draft_is_ready_for_review(self) -> None:
        from athena_orchestrator.console import review_ids

        self._draft("draft_ok", False)
        self.conn.execute(
            """
            INSERT INTO drafts (
                id, symbol, tactic, contracts, status, reason, phase,
                account_id, instrument, limit_price, simulated
            )
            VALUES ('draft_no', %s, 'BUY', 1, 'blocked', 'cap', 'P1', %s, 'STK', 12.5, false)
            """,
            (SYMBOL, ACCOUNT),
        )
        self.conn.commit()
        self.assertEqual(review_ids(self.conn, ACCOUNT), ["draft_ok"])
        self.assertEqual(review_ids(self.conn, ""), [])

    def test_replay_plays_halts_and_resumes(self) -> None:
        from athena_orchestrator.sim import cycle_gate, halt, progress, resume

        self._run()
        self._bar("AAA", date(2020, 1, 2))
        self._bar("AAA", date(2020, 1, 3))
        self._bar("BBB", date(2020, 1, 2))
        self._bar("BBB", date(2020, 1, 3))
        mode, day = cycle_gate(self.conn, ACCOUNT)
        self.assertEqual((mode, day), ("play", date(2020, 1, 2)))
        info = progress(self.conn, ACCOUNT, series=True)
        self.assertEqual(info["day"], 1)
        self.assertEqual(info["total"], 2)
        self.assertEqual(info["left"], 1)
        self.assertIn("AAA", info["series"])
        halt(self.conn, ACCOUNT)
        cursor, status = self._cursor()
        self.assertEqual(cursor, date(2020, 1, 2))
        self.assertEqual(status, "halted")
        self.assertEqual(cycle_gate(self.conn, ACCOUNT), ("wait", None))
        self.assertEqual(self._cursor()[0], date(2020, 1, 2))
        self.assertEqual(resume(self.conn, ACCOUNT), "Simulation resumed.")
        self.assertEqual(self._cursor()[1], "active")

    def test_resume_waits_until_every_symbol_has_bars(self) -> None:
        from athena_orchestrator.sim import resume

        self._run(status="halted")
        self._bar("AAA", date(2020, 1, 2))
        with self.assertRaises(ValueError) as caught:
            resume(self.conn, ACCOUNT)
        self.assertIn("Download stopped early", str(caught.exception))
        self.assertEqual(self._cursor()[1], "halted")

    def test_reset_keeps_a_real_position(self) -> None:
        from athena_orchestrator.sim import reset

        self._run()
        self._bar("AAA", date(2020, 1, 2))
        self.conn.execute(
            """
            INSERT INTO positions (account_id, symbol, quantity, avg_price, simulated)
            VALUES (%s, %s, 1, 10, false)
            """,
            (ACCOUNT, SYMBOL),
        )
        self.conn.execute(
            """
            INSERT INTO positions (account_id, symbol, quantity, avg_price, simulated)
            VALUES (%s, %s, 2, 11, true)
            """,
            (ACCOUNT, SYMBOL),
        )
        self.conn.commit()
        self.assertIn("cleared", reset(self.conn, ACCOUNT).lower())
        real = self.conn.execute(
            "SELECT quantity FROM positions WHERE account_id = %s AND simulated = false",
            (ACCOUNT,),
        ).fetchone()
        simulated = self.conn.execute(
            "SELECT 1 FROM positions WHERE account_id = %s AND simulated = true",
            (ACCOUNT,),
        ).fetchone()
        runs = self.conn.execute("SELECT 1 FROM sim_runs WHERE account_id = %s", (ACCOUNT,)).fetchone()
        self.assertEqual(Decimal(real[0]), Decimal("1"))
        self.assertIsNone(simulated)
        self.assertIsNone(runs)

    def test_reset_drops_simulated_audit_and_keeps_a_live_call(self) -> None:
        from athena_orchestrator.sim import reset
        from athena_strategy.agents import record_call, write_decision

        self._run(symbols="ZZT", cursor=date(2020, 1, 2))
        result = {"text": "{}", "error": "", "latency_ms": 1, "tokens": 0, "request": {}}
        record_call(self.conn, ACCOUNT, "CSP", "ZZT", "deepseek-flash", "DIRECT", "off", result, {"quiet": True})
        write_decision(self.conn, ACCOUNT, "ZZT", "consolidator", "quiet", "Quiet", {"items": []})
        record_call(self.conn, ACCOUNT, "Universe", "META", "deepseek-flash", "DIRECT", "off", result, {})
        flagged = self.conn.execute(
            "SELECT count(*) FROM ai_calls WHERE account_id = %s AND simulated",
            (ACCOUNT,),
        ).fetchone()[0]
        self.assertEqual(flagged, 1)
        reset(self.conn, ACCOUNT)
        left = self.conn.execute(
            "SELECT symbol, simulated FROM ai_calls WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchall()
        decisions = self.conn.execute(
            "SELECT 1 FROM decision_trails WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchone()
        self.assertEqual(left, [("META", False)])
        self.assertIsNone(decisions)

    def test_download_activate_and_flush_stay_on_this_account(self) -> None:
        from athena_orchestrator.sim import (
            activate,
            busy,
            delete_simulated_positions,
            fail,
            flush,
            latest_close,
            prepare,
            real_positions,
            still_downloading,
            stop,
            store_bars,
            symbols_for,
            view,
        )

        run_id, _end, duration = prepare(self.conn, ACCOUNT, date(2026, 9, 1), 1, ["NVDA"])
        self.assertTrue(duration.endswith(" Y"))
        self.assertTrue(busy(self.conn, ACCOUNT))
        self.assertTrue(still_downloading(self.conn, run_id))
        self.assertEqual(store_bars(self.conn, run_id, "NVDA", [(date(2026, 9, 2), 1, 2, 0.5, 0, 1)]), 0)
        self.assertEqual(store_bars(self.conn, run_id, "NVDA", [(date(2026, 9, 2), 10, 12, 9, 11, 5)]), 1)
        self.assertEqual(latest_close(self.conn, run_id, "NVDA"), Decimal("11"))
        activate(self.conn, run_id)
        self.assertEqual(view(self.conn, ACCOUNT)["status"], "active")
        self.assertEqual(symbols_for(self.conn, ACCOUNT), ["NVDA"])
        self.assertIn("stopped", stop(self.conn, ACCOUNT).lower())
        fail(self.conn, "missing")
        self.conn.execute(
            """
            INSERT INTO positions (account_id, symbol, quantity, avg_price, simulated)
            VALUES (%s, %s, 3, 9, true)
            """,
            (ACCOUNT, SYMBOL),
        )
        self.conn.execute(
            """
            INSERT INTO positions (account_id, symbol, quantity, avg_price, sec_type, simulated)
            VALUES (%s, %s, 4, 8, 'STK', false)
            """,
            (ACCOUNT, SYMBOL),
        )
        self.conn.commit()
        self.assertEqual(delete_simulated_positions(self.conn, ACCOUNT), 1)
        held = real_positions(self.conn, ACCOUNT)
        self.assertEqual(held[0][0], SYMBOL)
        with self.assertRaises(ValueError):
            flush(self.conn, ACCOUNT, "no")
        self.assertIn("cleared", flush(self.conn, ACCOUNT, "I CONFIRM").lower())
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM positions WHERE account_id = %s", (ACCOUNT,)).fetchone()
        )

    def test_simulated_approve_does_not_call_the_broker(self) -> None:
        self._draft("zz_sim", True)
        with patch("athena_exec.broker.IbBroker.place_limit") as stock, patch(
            "athena_exec.broker.IbBroker.place_option_limit"
        ) as option:
            decide(self.conn, "zz_sim", "approve", "tester", {"tester"})
            stock.assert_not_called()
            option.assert_not_called()
        order = self.conn.execute(
            "SELECT status, simulated, limit_price FROM orders WHERE draft_id = %s",
            ("zz_sim",),
        ).fetchone()
        fill = self.conn.execute(
            "SELECT simulated, price FROM fills WHERE draft_id = %s",
            ("zz_sim",),
        ).fetchone()
        position = self.conn.execute(
            """
            SELECT quantity, simulated FROM positions
            WHERE account_id = %s AND symbol = %s AND simulated = true
            """,
            (ACCOUNT, SYMBOL),
        ).fetchone()
        self.assertEqual(order[0], "filled")
        self.assertTrue(order[1])
        self.assertEqual(order[2], Decimal("12.5"))
        self.assertTrue(fill[0])
        self.assertEqual(Decimal(position[0]), Decimal("1"))

    def test_a_live_draft_outside_the_slot_is_not_sent(self) -> None:
        self._draft("zz_live", False)
        decide(self.conn, "zz_live", "approve", "tester", {"tester"})
        reason = live_block_reason(
            slot="idle",
            draft_status="approved",
            halted=False,
            account_id=ACCOUNT,
            instrument="STK",
            limit_price=Decimal("12.5"),
        )
        self.assertEqual(reason, "outside athena slot")
        broker = Mock()
        _submit_queued(self.conn, broker, "idle", ACCOUNT)
        broker.place_limit.assert_not_called()
        broker.place_option_limit.assert_not_called()
        status = self.conn.execute("SELECT status FROM orders WHERE draft_id = %s", ("zz_live",)).fetchone()
        self.assertEqual(status[0], "queued")

    def test_a_halted_account_is_a_soft_block(self) -> None:
        self._draft("zz_halt", False)
        decide(self.conn, "zz_halt", "approve", "tester", {"tester"})
        book_halt(self.conn, ACCOUNT, "test halt")
        reason = live_block_reason(
            slot="athena",
            draft_status="approved",
            halted=True,
            account_id=ACCOUNT,
            instrument="STK",
            limit_price=Decimal("12.5"),
        )
        self.assertEqual(reason, "trading is halted")
        broker = Mock()
        _submit_queued(self.conn, broker, "athena", ACCOUNT)
        broker.place_limit.assert_not_called()
        status = self.conn.execute("SELECT status FROM orders WHERE draft_id = %s", ("zz_halt",)).fetchone()
        self.assertEqual(status[0], "queued")

    def test_approve_queues_the_stock_or_option_side(self) -> None:
        self.conn.execute(
            """
            INSERT INTO drafts (
                id, symbol, tactic, contracts, status, reason, phase, account_id,
                instrument, limit_price, order_action, simulated
            )
            VALUES
                ('zz_sell', %s, 'CLOSE', 10, 'pending', 'close', 'MANAGE', %s, 'STK', 12.5, 'SELL', false),
                ('zz_buy', %s, 'BUY', 1, 'pending', 'open', 'P1', %s, 'STK', 12.5, '', false)
            """,
            (SYMBOL, ACCOUNT, SYMBOL, ACCOUNT),
        )
        self.conn.execute(
            """
            INSERT INTO drafts (
                id, symbol, tactic, contracts, status, reason, phase, account_id,
                instrument, expiry, put_strike, put_limit, multiplier, simulated
            )
            VALUES (
                'zz_put', %s, 'OPEN_CSP', 1, 'pending', 'open', 'P1', %s,
                'OPT', %s, 90, 1.25, '100', false
            )
            """,
            (SYMBOL, ACCOUNT, date.today() + timedelta(days=30)),
        )
        self.conn.commit()
        pending = self.conn.execute(
            "SELECT count(*) FROM orders WHERE draft_id = 'zz_put'",
        ).fetchone()
        self.assertEqual(pending[0], 0)
        for draft_id in ("zz_sell", "zz_buy", "zz_put"):
            decide(self.conn, draft_id, "approve", "tester", {"tester"})
        rows = self.conn.execute(
            "SELECT draft_id, action, status FROM orders WHERE account_id = %s ORDER BY draft_id",
            (ACCOUNT,),
        ).fetchall()
        self.assertEqual(
            rows,
            [
                ("zz_buy", "BUY", "queued"),
                ("zz_put", "SELL", "queued"),
                ("zz_sell", "SELL", "queued"),
            ],
        )

    def test_an_unmarked_stock_uses_the_stored_close(self) -> None:
        from athena_orchestrator.pages import _position_rows, _position_tr

        expiry = date(2026, 12, 18)
        self.conn.execute(
            """
            INSERT INTO positions (account_id, symbol, quantity, avg_price, sec_type, multiplier, simulated)
            VALUES (%s, %s, 10, 12, 'STK', 1, false)
            """,
            (ACCOUNT, SYMBOL),
        )
        self.conn.execute(
            """
            INSERT INTO positions (
                account_id, symbol, quantity, avg_price, sec_type, expiry, contract_right,
                strike, multiplier, simulated
            )
            VALUES (%s, %s, -1, 1.5, 'OPT', %s, 'P', 90, 100, false)
            """,
            (ACCOUNT, SYMBOL, expiry),
        )
        self.conn.execute(
            """
            INSERT INTO market_bars (symbol, ts, open, high, low, close, volume, source)
            VALUES (%s, %s, 14, 16, 13, 15, 1000, 'ib-daily')
            """,
            (SYMBOL, datetime(2026, 10, 13, tzinfo=timezone.utc)),
        )
        self.conn.commit()
        rows = _position_rows(self.conn, ACCOUNT)
        stock = next(row for row in rows if row["sec_type"] == "STK")
        option = next(row for row in rows if row["sec_type"] == "OPT")
        self.assertEqual(stock["mark"], "15.00")
        self.assertTrue(stock["stored_close"])
        self.assertEqual(stock["value"], "150.00")
        self.assertEqual(stock["pnl"], Decimal("30"))
        self.assertIn(">close<", _position_tr(stock))
        self.assertEqual(option["mark"], "")
        self.assertEqual(option["value"], "")
        self.assertIsNone(option["pnl"])
        self.assertNotIn(">close<", _position_tr(option))

    def test_liquidate_sends_the_closes(self) -> None:
        expiry = date(2026, 12, 18)
        self.conn.execute(
            """
            INSERT INTO positions (
                account_id, symbol, quantity, avg_price, sec_type, market_price, multiplier, simulated
            )
            VALUES
                (%s, %s, 10, 12, 'STK', 12.5, 1, false),
                (%s, %s, 1, 11, 'STK', 11, 1, true)
            """,
            (ACCOUNT, SYMBOL, ACCOUNT, SYMBOL),
        )
        self.conn.execute(
            """
            INSERT INTO positions (
                account_id, symbol, quantity, avg_price, sec_type, expiry, contract_right,
                strike, market_price, multiplier, simulated
            )
            VALUES (%s, %s, -1, 1, 'OPT', %s, 'P', 90, 1.2, 100, false)
            """,
            (ACCOUNT, SYMBOL, expiry),
        )
        self.conn.commit()
        broker = Mock()
        broker.connect.return_value = True
        broker.last_price.return_value = Decimal("12.5")
        broker.option_chain.return_value = ([], [], "ZZT", "SMART", "100")
        with patch("athena_orchestrator.tasks.IbBroker", return_value=broker):
            from athena_orchestrator.tasks import _run_liquidate

            _run_liquidate("zz-liq", os.environ["DATABASE_URL"], ACCOUNT)
        broker.place_limit.assert_called_once_with(ACCOUNT, SYMBOL, "SELL", 10, Decimal("12.5"))
        broker.place_option_limit.assert_called_once_with(
            ACCOUNT, SYMBOL, expiry, Decimal("90"), "P", "ZZT", "SMART", "BUY", 1, Decimal("1.2"), Decimal("100"),
        )
        drafts = self.conn.execute(
            "SELECT 1 FROM drafts WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchall()
        simulated = self.conn.execute(
            "SELECT count(*) FROM positions WHERE account_id = %s AND simulated = true",
            (ACCOUNT,),
        ).fetchone()
        self.assertEqual(drafts, [])
        self.assertEqual(simulated[0], 0)

    def test_a_confirmed_trade_is_stored_after_ib_accepts(self) -> None:
        from athena_orchestrator.tasks import _run_trade

        broker = Mock()
        broker.connect.return_value = False
        with patch("athena_orchestrator.tasks.IbBroker", return_value=broker):
            _run_trade("zz-trade-off", os.environ["DATABASE_URL"], ACCOUNT, "tester", SYMBOL, "BUY", 15, "LMT", Decimal("11.2"), None)
        quiet = self.conn.execute("SELECT count(*) FROM drafts WHERE account_id = %s", (ACCOUNT,)).fetchone()
        self.assertEqual(quiet[0], 0)
        broker.connect.return_value = True
        broker.place_limit.return_value = 77
        with patch("athena_orchestrator.tasks.IbBroker", return_value=broker):
            _run_trade("zz-trade", os.environ["DATABASE_URL"], ACCOUNT, "tester", SYMBOL, "BUY", 15, "LMT", Decimal("11.2"), None)
        broker.place_limit.assert_called_once_with(ACCOUNT, SYMBOL, "BUY", 15, Decimal("11.2"))
        broker.place_market.assert_not_called()
        row = self.conn.execute(
            """
            SELECT d.tactic, d.status, o.action, o.contracts, o.status, o.ib_order_id, o.order_type, o.multiplier, a.decision
            FROM drafts d
            JOIN orders o ON o.draft_id = d.id
            JOIN approvals a ON a.draft_id = d.id
            WHERE d.account_id = %s
            """,
            (ACCOUNT,),
        ).fetchone()
        self.assertEqual(row, ("MANUAL", "approved", "BUY", 15, "submitted", 77, "LMT", "1", "approve"))

    def test_auto_set_price_reads_todays_bar_and_news(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from athena_strategy.agents import suggest_trade_price

        day = datetime.now(ZoneInfo("America/New_York")).date()
        ts = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        self.conn.execute(
            """
            INSERT INTO market_bars (symbol, ts, open, high, low, close, volume, source)
            VALUES (%s, %s, 10, 11, 9, 10.5, 1000, 'ib-daily')
            """,
            (SYMBOL, ts),
        )
        self.conn.execute(
            """
            INSERT INTO news_events (fingerprint, ts, symbol, source, event_type, sentiment, title)
            VALUES ('zz-trade-news', now(), %s, 'test', 'GENERIC', 0, 'ZZT trade headline')
            """,
            (SYMBOL,),
        )
        self.conn.commit()
        seen = {}

        def fake_chat(system, user, model, provider, token, thinking):
            seen["user"] = user
            return {
                "text": '{"limit": 12.5, "stop": 0, "reason": "near the low"}',
                "tokens": 3,
                "error": "",
                "latency_ms": 1,
                "request": {},
            }

        try:
            with patch("athena_strategy.agents.chat", side_effect=fake_chat):
                result = suggest_trade_price(self.conn, standard_payload(), ACCOUNT, SYMBOL, "10.25", "BUY", "LMT")
        finally:
            self.conn.execute("DELETE FROM news_events WHERE symbol = %s", (SYMBOL,))
            self.conn.commit()
        self.assertTrue(result["ok"])
        self.assertEqual(result["limit"], "12.5")
        self.assertEqual(result["stop"], "")
        self.assertIn("ZZT trade headline", seen["user"])
        self.assertIn("Today open: 10", seen["user"])
        self.assertIn("Last: 10.25", seen["user"])
        self.assertIn("3-month high: 11 (1 of 63 sessions)", seen["user"])
        self.assertIn("52-week low: 9 (1 of 252 sessions)", seen["user"])
        self.assertIn("Prior close: none", seen["user"])

    def test_auto_set_price_uses_the_3_month_and_52_week_ranges(self) -> None:
        from athena_settings.model import AgentPrompt
        from athena_strategy.agents import suggest_trade_price

        start = date(2026, 1, 1)
        for offset in range(80):
            day = start + timedelta(days=offset)
            if offset == 0:
                low, high, close = "8", "200", "150"
            elif offset == 78:
                low, high, close = "15", "20", "16"
            else:
                low, high, close = "15", "20", "17"
            self._print(datetime(day.year, day.month, day.day, tzinfo=timezone.utc), "ib-daily", low, high, close)
        seen = {}

        def fake_chat(system, user, model, provider, token, thinking):
            seen["user"] = user
            seen["system"] = system
            return {
                "text": '{"limit": 18, "stop": 0, "reason": "inside the 3-month range"}',
                "tokens": 3,
                "error": "",
                "latency_ms": 1,
                "request": {},
            }

        with patch("athena_strategy.agents.chat", side_effect=fake_chat):
            result = suggest_trade_price(self.conn, standard_payload(), ACCOUNT, SYMBOL, "18", "BUY", "LMT")
            first = dict(seen)
            old = standard_payload().model_copy(update={
                "agents": {
                    "Trade": AgentPrompt(
                        system="You suggest a price.",
                        execution="Symbol: {{symbol}}\nLast: {{last}}\n",
                    ),
                },
            })
            old_result = suggest_trade_price(self.conn, old, ACCOUNT, SYMBOL, "18", "BUY", "LMT")
        self.assertTrue(result["ok"])
        self.assertIn("3-month high: 20 (63 of 63 sessions)", first["user"])
        self.assertIn("3-month low: 15 (63 of 63 sessions)", first["user"])
        self.assertIn("52-week high: 200 (80 of 252 sessions)", first["user"])
        self.assertIn("52-week low: 8 (80 of 252 sessions)", first["user"])
        self.assertIn("Last versus 3-month high: -10.00%", first["user"])
        self.assertIn("Last versus 3-month low: +20.00%", first["user"])
        self.assertIn("Last in the 3-month range: 60% up from the low", first["user"])
        self.assertIn("Last versus 52-week low: +125.00%", first["user"])
        self.assertIn("Last in the 52-week range: 5% up from the low", first["user"])
        self.assertIn("Prior close: 16", first["user"])
        self.assertIn("not from today's bar alone", first["system"])
        self.assertTrue(old_result["ok"])
        self.assertIn("Do not set the price from today alone.", seen["user"])
        self.assertIn("52-week high: 200 (80 of 252 sessions)", seen["user"])

    def test_the_orders_page_pages_the_selected_account(self) -> None:
        from athena_orchestrator.pages import _orders_page

        self.conn.execute(
            """
            INSERT INTO drafts (
                id, symbol, tactic, contracts, status, reason, phase, account_id, instrument, simulated
            )
            VALUES ('draft_zzorders', %s, 'MANUAL', 1, 'approved', 'seed', '', %s, 'STK', false)
            """,
            (SYMBOL, ACCOUNT),
        )
        start = datetime(2026, 10, 1, tzinfo=timezone.utc)
        for index in range(51):
            symbol = "DONE" if index == 50 else "OLDEST" if index == 0 else SYMBOL
            status = "filled" if index == 50 else "submitted"
            self.conn.execute(
                """
                INSERT INTO orders (
                    id, draft_id, account_id, symbol, tactic, action, contracts, status, created_at
                )
                VALUES (%s, 'draft_zzorders', %s, %s, 'MANUAL', 'BUY', 1, %s, %s)
                """,
                (f"ord_zz{index}", ACCOUNT, symbol, status, start + timedelta(seconds=index)),
            )
        self.conn.execute(
            """
            INSERT INTO drafts (
                id, symbol, tactic, contracts, status, reason, phase, account_id, instrument, simulated
            )
            VALUES ('draft_zzother', 'ZZOTHER', 'MANUAL', 1, 'approved', 'seed', '', 'ZZOTHER', 'STK', false)
            """
        )
        self.conn.execute(
            """
            INSERT INTO orders (
                id, draft_id, account_id, symbol, tactic, action, contracts, status
            )
            VALUES ('ord_zzother', 'draft_zzother', 'ZZOTHER', 'ZZOTHER', 'MANUAL', 'BUY', 1, 'submitted')
            """
        )
        self.conn.commit()
        try:
            page = _orders_page(self.conn, ACCOUNT, {})
            self.assertIn('id="orders-feed"', page["body"])
            self.assertIn("DONE", page["body"])
            self.assertIn(">FILLED<", page["body"])
            self.assertIn('class="tag">MANUAL<', page["body"])
            self.assertIn('class="tag green">BUY<', page["body"])
            self.assertNotIn("OLDEST", page["body"])
            self.assertNotIn("ZZOTHER", page["body"])
            nxt = _orders_page(self.conn, ACCOUNT, {"partial": ["rows"], "offset": ["50"]})
            body = json.loads(nxt["body"])
            self.assertIn("OLDEST", body["html"])
            self.assertFalse(body["more"])
            filled = _orders_page(self.conn, ACCOUNT, {"status": ["filled"]})
            self.assertIn("DONE", filled["body"])
            self.assertNotIn(">ZZT<", filled["body"])
            working = _orders_page(self.conn, ACCOUNT, {"status": ["working"]})
            self.assertNotIn("DONE", working["body"])
            self.assertIn(SYMBOL, working["body"])
        finally:
            self.conn.execute("DELETE FROM orders WHERE account_id = 'ZZOTHER'")
            self.conn.execute("DELETE FROM drafts WHERE account_id = 'ZZOTHER'")
            self.conn.commit()

    def test_a_live_ib_status_updates_only_a_known_order(self) -> None:
        from queue import Empty

        from athena_exec.order_watch import apply_live_order, subscribe, unsubscribe

        self.conn.execute(
            """
            INSERT INTO drafts (
                id, symbol, tactic, contracts, status, reason, phase, account_id, instrument, simulated
            )
            VALUES ('draft_zzlive', %s, 'MANUAL', 3, 'approved', 'seed', '', %s, 'STK', false)
            """,
            (SYMBOL, ACCOUNT),
        )
        self.conn.execute(
            """
            INSERT INTO orders (
                id, draft_id, account_id, symbol, tactic, action, contracts, status, ib_order_id
            )
            VALUES ('ord_zzlive', 'draft_zzlive', %s, %s, 'MANUAL', 'BUY', 3, 'submitted', 9001)
            """,
            (ACCOUNT, SYMBOL),
        )
        self.conn.commit()
        listener = subscribe(ACCOUNT)
        try:
            account = apply_live_order(self.conn, 9001, "Submitted", 1, 2, "10.5")
            self.assertEqual(account, ACCOUNT)
            stored = self.conn.execute(
                """
                SELECT status, ib_status, filled_qty, remaining_qty, avg_fill
                FROM orders WHERE id = 'ord_zzlive'
                """
            ).fetchone()
            self.assertEqual(stored, ("submitted", "Submitted", Decimal("1"), Decimal("2"), Decimal("10.5")))
            self.assertEqual(listener.get_nowait(), "ord_zzlive")
            self.assertIsNone(apply_live_order(self.conn, 9001, "Submitted", 1, 2, "10.5"))
            self.assertIsNone(apply_live_order(self.conn, 424242, "Filled", 1, 0, "1"))
            with self.assertRaises(Empty):
                listener.get_nowait()
        finally:
            unsubscribe(ACCOUNT, listener)

    def test_an_ib_account_order_is_stored(self) -> None:
        from athena_exec.order_watch import sync_ib_order
        from athena_orchestrator.pages import _orders_page

        trade = _ib_trade(77, 88001, "ZZT", "BUY", 30, "Submitted", "151.5")
        self.assertEqual(sync_ib_order(self.conn, trade), ACCOUNT)
        page = _orders_page(self.conn, ACCOUNT, {"status": ["working"]})
        self.assertIn("ZZT", page["body"])
        self.assertIn("EXTERNAL", page["body"])
        trade.orderStatus.filled = 10
        self.assertEqual(sync_ib_order(self.conn, trade), ACCOUNT)
        self.assertIsNone(sync_ib_order(self.conn, trade))
        stored = self.conn.execute(
            "SELECT count(*), max(filled_qty) FROM orders WHERE account_id = %s AND perm_id = 88001",
            (ACCOUNT,),
        ).fetchone()
        self.assertEqual(stored, (1, Decimal("10")))
        other = _ib_trade(77, 88002, "ZZX", "SELL", 5, "Submitted", "2")
        sync_ib_order(self.conn, other)
        rows = self.conn.execute(
            "SELECT symbol, tactic FROM orders WHERE account_id = %s AND ib_order_id = 77 ORDER BY symbol",
            (ACCOUNT,),
        ).fetchall()
        self.assertEqual(rows, [("ZZT", "EXTERNAL"), ("ZZX", "EXTERNAL")])
        self.conn.execute(
            """
            INSERT INTO drafts (
                id, symbol, tactic, contracts, status, reason, phase, account_id, instrument
            )
            VALUES ('draft_zzkeep', %s, 'MANUAL', 4, 'approved', 'seed', '', %s, 'STK')
            """,
            (SYMBOL, ACCOUNT),
        )
        self.conn.execute(
            """
            INSERT INTO orders (
                id, draft_id, account_id, symbol, tactic, action, contracts, status, ib_order_id
            )
            VALUES ('ord_zzkeep', 'draft_zzkeep', %s, %s, 'MANUAL', 'BUY', 4, 'submitted', 78)
            """,
            (ACCOUNT, SYMBOL),
        )
        self.conn.commit()
        sync_ib_order(self.conn, _ib_trade(78, 88003, SYMBOL, "BUY", 4, "Submitted", "9"))
        kept = self.conn.execute(
            "SELECT tactic, perm_id FROM orders WHERE id = 'ord_zzkeep'"
        ).fetchone()
        self.assertEqual(kept, ("MANUAL", 88003))

    def test_mock_send_order_stays_a_pending_draft(self) -> None:
        from athena_strategy.mock import prepare_mock_orders

        self.conn.execute(
            """
            INSERT INTO option_plans (
                symbol, expiry, put_strike, call_strike, put_limit, call_limit,
                trading_class, option_exchange
            )
            VALUES (%s, %s, 90, 110, 1, 1, 'ZZT', 'SMART')
            """,
            (SYMBOL, date.today() + timedelta(days=30)),
        )
        self.conn.execute(
            """
            INSERT INTO mock_signals (symbol, tags, send_order, active)
            VALUES (%s, 'NEWS_WASH', true, true)
            ON CONFLICT (symbol) DO UPDATE SET send_order = true, active = true
            """,
            (SYMBOL,),
        )
        self.conn.commit()
        try:
            prepare_mock_orders(self.conn, standard_payload().model_copy(update={"watchlist": [SYMBOL]}), ACCOUNT)
            draft = self.conn.execute(
                "SELECT status FROM drafts WHERE account_id = %s",
                (ACCOUNT,),
            ).fetchone()
            orders = self.conn.execute(
                "SELECT 1 FROM orders WHERE account_id = %s",
                (ACCOUNT,),
            ).fetchall()
            flag = self.conn.execute(
                "SELECT send_order FROM mock_signals WHERE symbol = %s",
                (SYMBOL,),
            ).fetchone()
            self.assertEqual(draft[0], "pending")
            self.assertEqual(orders, [])
            self.assertFalse(flag[0])
        finally:
            self.conn.execute("DELETE FROM mock_signals WHERE symbol = %s", (SYMBOL,))
            self.conn.commit()

    def test_open_csp_is_stored_as_hold_and_a_short_option_rolls(self) -> None:
        self.conn.execute(
            """
            INSERT INTO symbol_phases (account_id, symbol, phase, tags, tactic)
            VALUES (%s, %s, 'P4', '', 'HOLD')
            """,
            (ACCOUNT, SYMBOL),
        )
        evaluate_symbol(
            self.conn,
            standard_payload(),
            SYMBOL,
            ACCOUNT,
            tags={OVERSOLD, SUPPORT_TOUCH},
            event_offset=2,
        )
        stored = self.conn.execute(
            "SELECT phase, tactic FROM symbol_phases WHERE account_id = %s AND symbol = %s",
            (ACCOUNT, SYMBOL),
        ).fetchone()
        self.assertEqual(stored, ("P4", "HOLD"))
        self.assertIsNone(
            management_tactic(
                sec_type="OPT",
                quantity=Decimal("-1"),
                avg_price=Decimal("2"),
                market_price=Decimal("1.5"),
                expiry=date(2026, 10, 20),
                today=date(2026, 10, 10),
                roll_dte=21,
                profit_take_pct=50,
            )
        )
        self.conn.execute(
            """
            INSERT INTO option_plans (
                symbol, expiry, put_strike, call_strike, put_limit, call_limit,
                trading_class, option_exchange, multiplier
            )
            VALUES (%s, '2026-11-20', 10, 12, 1, 1, 'ZZT', 'CBOE', '100')
            """,
            (SYMBOL,),
        )
        self.conn.execute(
            """
            INSERT INTO positions (
                account_id, symbol, quantity, avg_price, sec_type, expiry,
                contract_right, strike, market_price, simulated
            )
            VALUES (%s, %s, -1, 2, 'OPT', '2026-10-20', 'P', 10, 1.5, false)
            """,
            (ACCOUNT, SYMBOL),
        )
        self.conn.execute(
            """
            INSERT INTO market_bars (symbol, ts, open, high, low, close, volume, source)
            VALUES (%s, '2026-10-01 12:00+00', 10, 11, 9, 10, 1, 'ib-daily')
            """,
            (SYMBOL,),
        )
        self.conn.commit()
        created = manage_book(self.conn, standard_payload(), ACCOUNT, date(2026, 10, 10))
        self.assertEqual(created, [])
        draft = self.conn.execute(
            "SELECT tactic FROM drafts WHERE account_id = %s AND tactic = 'ROLL'",
            (ACCOUNT,),
        ).fetchone()
        self.assertIsNone(draft)
        self.assertEqual(record_iv_marks(self.conn, [SYMBOL], date(2026, 10, 10)), 1)
        self.conn.execute(
            """
            INSERT INTO iv_marks (symbol, day, credit, underlying, iv_proxy)
            VALUES (%s, '2020-01-01', 1, 10, 0.1)
            ON CONFLICT (symbol, day) DO NOTHING
            """,
            (SYMBOL,),
        )
        self.conn.commit()
        settings = standard_payload()
        self.assertEqual(tags_from_iv(self.conn, SYMBOL, settings), set())
        self.assertIsNone(estimated_iv(self.conn, SYMBOL, date(2026, 10, 10)))
        method = self.conn.execute(
            "SELECT method FROM iv_marks WHERE symbol = %s AND day = %s",
            (SYMBOL, date(2026, 10, 10)),
        ).fetchone()
        self.assertEqual(method[0], "premium_ratio")

    def test_method_windows_do_not_mix(self) -> None:
        from athena_strategy.iv import iv_rank, iv_samples

        self.conn.execute(
            """
            INSERT INTO iv_marks (symbol, day, credit, underlying, iv_proxy, method)
            VALUES (%s, '2020-01-01', 1, 10, 0.5, 'premium_ratio')
            """,
            (SYMBOL,),
        )
        for offset in range(20):
            day = date(2021, 1, 1) + timedelta(days=offset)
            self.conn.execute(
                """
                INSERT INTO iv_marks (symbol, day, credit, underlying, iv_proxy, method)
                VALUES (%s, %s, 1, 10, %s, 'chain_bs')
                """,
                (SYMBOL, day, Decimal(offset + 1)),
            )
        self.conn.commit()
        premium = iv_samples(self.conn, SYMBOL, "premium_ratio")
        chain = iv_samples(self.conn, SYMBOL, "chain_bs")
        self.assertEqual(premium, [Decimal("0.5")])
        self.assertEqual(len(chain), 20)
        self.assertNotIn(Decimal("0.5"), chain)
        self.assertIsNone(iv_rank(premium, premium[-1], 20))
        self.assertEqual(iv_rank(chain, chain[-1], 20), 100)
        from athena_strategy.drafts import _stored_iv_rank

        with patch("athena_strategy.drafts.load_active") as load:
            load.return_value.strategy.ivr_min_samples = 20
            self.assertIsNone(_stored_iv_rank(self.conn, ACCOUNT, SYMBOL))

    def test_phase_indicators_match_the_reading(self) -> None:
        from athena_strategy.agents import _trail_context
        from athena_strategy.indicators import read_indicators

        for index in range(30):
            day = date(2024, 1, 1) + timedelta(days=index)
            close = 100 + index
            self.conn.execute(
                """
                INSERT INTO market_bars (symbol, ts, open, high, low, close, volume, source)
                VALUES (%s, %s, %s, %s, %s, %s, 1000, 'ib-daily')
                """,
                (
                    SYMBOL,
                    datetime(day.year, day.month, day.day, tzinfo=timezone.utc),
                    close,
                    close + 1,
                    close - 1,
                    close,
                ),
            )
        self.conn.commit()
        settings = standard_payload()
        reading = read_indicators(self.conn, settings, SYMBOL)
        evaluate_symbol(self.conn, settings, SYMBOL, ACCOUNT)
        stored = self.conn.execute(
            "SELECT indicators FROM symbol_phases WHERE account_id = %s AND symbol = %s",
            (ACCOUNT, SYMBOL),
        ).fetchone()
        trail = _trail_context(
            {"tags": "none", "flags": "", "numbers": reading.line(), "indicators": reading.payload()},
            TacticFlags(),
            "P1",
            {},
        )
        self.assertEqual(stored[0]["rail_pos"], reading.payload()["rail_pos"])
        self.assertEqual(trail["indicators"]["trend_slope_pct"], stored[0]["trend_slope_pct"])
        self.assertIsNone(stored[0]["rv_pctl"])
        self.assertNotIn("iv=", reading.line())
        count = self.conn.execute("SELECT count(*) FROM rv_marks WHERE symbol = %s", (SYMBOL,)).fetchone()
        self.assertEqual(count[0], 1)
        self.assertIsNone(estimated_iv(self.conn, SYMBOL, date(2026, 10, 10)))

    def test_simulation_handlers(self) -> None:
        from athena_orchestrator.console import _post_tasks
        from athena_orchestrator.sim import progress

        self._run()
        self._bar("AAA", date(2020, 1, 2))
        self._bar("BBB", date(2020, 1, 2))
        info = progress(self.conn, ACCOUNT)
        self.assertEqual(info["status"], "active")
        self.assertEqual(info["total"], 1)
        with patch("athena_orchestrator.console._account", return_value=ACCOUNT):
            code, body, kind = _post_tasks(self.conn, {"action": ["sim-halt"]}, "op", _url())
            self.assertEqual((code, kind), (200, "json"))
            self.assertTrue(json.loads(body)["ok"])
            self.assertEqual(self._cursor()[1], "halted")
            code, body, kind = _post_tasks(self.conn, {"action": ["sim-resume"]}, "op", _url())
            self.assertEqual(code, 200)
            self.assertTrue(json.loads(body)["ok"])
            self.assertEqual(self._cursor()[1], "active")
            code, body, kind = _post_tasks(self.conn, {"action": ["sim-reset"]}, "op", _url())
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM sim_runs WHERE id = %s", (RUN,)).fetchone())

    def test_merged_daily_bars_keep_the_session_low(self) -> None:
        from athena_marketdata.pricing import daily_bars, last_daily_close, last_tradeable_price

        day = datetime(2026, 1, 5, tzinfo=timezone.utc)
        later = datetime(2026, 1, 5, 15, tzinfo=timezone.utc)
        morning = datetime(2026, 1, 6, 10, tzinfo=timezone.utc)
        afternoon = datetime(2026, 1, 6, 15, tzinfo=timezone.utc)
        self._print(day, "ib-daily", "190.01", "195", "192")
        self._print(later, "ib-delayed", "200", "200", "200")
        self._print(morning, "ib-delayed", "180", "180", "180")
        self._print(afternoon, "ib-delayed", "181", "181", "181")
        self._print(day, "seed", "100", "100", "100")
        bars = daily_bars(self.conn, SYMBOL, 63)
        self.assertEqual([bar.day for bar in bars], [date(2026, 1, 5)])
        self.assertEqual(bars[0].low, Decimal("190.01"))
        self.assertEqual(bars[0].high, Decimal("195"))
        self.assertEqual(bars[0].close, Decimal("192"))
        self.assertEqual(last_daily_close(self.conn, SYMBOL), Decimal("192"))
        self.assertEqual(last_tradeable_price(self.conn, SYMBOL), Decimal("181"))

    def test_missing_itick_quote_writes_nothing(self) -> None:
        from athena_marketdata import write_watchlist_bars

        written = write_watchlist_bars(self.conn, [SYMBOL], "http://127.0.0.1", "")
        self.assertEqual(written, 0)
        count = self.conn.execute(
            "SELECT count(*) FROM market_bars WHERE symbol = %s",
            (SYMBOL,),
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_short_symbol_uses_range_tags(self) -> None:
        from athena_settings.model import TacticFlags
        from athena_strategy.agents import _context

        self._print(datetime(2026, 1, 5, tzinfo=timezone.utc), "ib-daily", "10", "12", "11")
        context = _context(self.conn, standard_payload(), ACCOUNT, SYMBOL, TacticFlags())
        self.assertEqual(context["range_high_tag"], "HIGH_RANGE")
        self.assertEqual(context["range_low_tag"], "LOW_RANGE")
        self.assertEqual(context["range_sessions"], "1")
        from athena_marketdata.pricing import year_band

        self.assertEqual(year_band(self.conn, SYMBOL), "-/-")

    def test_year_band_uses_252_sessions(self) -> None:
        from datetime import timedelta

        from athena_marketdata.pricing import year_band

        start = date(2024, 1, 1)
        for offset in range(252):
            day = start + timedelta(days=offset)
            low = "140.12" if offset == 10 else "150.00"
            high = "234.23" if offset == 200 else "160.00"
            self._print(datetime(day.year, day.month, day.day, tzinfo=timezone.utc), "ib-daily", low, high, "155.00")
        self.assertEqual(year_band(self.conn, SYMBOL), "234.23/140.12")
        from athena_marketdata.pricing import year_view

        _band, high_pct, low_pct = year_view(self.conn, SYMBOL)
        last = Decimal("155")
        self.assertEqual(high_pct, f"{(last - Decimal('234.23')) / Decimal('234.23') * 100:+.2f}%")
        self.assertEqual(low_pct, f"{(last - Decimal('140.12')) / Decimal('140.12') * 100:+.2f}%")

    def test_two_year_pull_marks_a_short_book(self) -> None:
        from datetime import timedelta

        from athena_marketdata.pricing import set_history_short
        from athena_orchestrator.backfill import pull_symbol
        from athena_store import apply_migrations

        apply_migrations(self.conn)

        class _Broker:
            def head_day(self, symbol):
                return date(2023, 1, 1)

            def history_span(self, symbol, duration, end=""):
                start = date(2024, 1, 1) if end else date(2025, 1, 1)
                return [
                    (
                        start + timedelta(days=offset),
                        Decimal("1"),
                        Decimal("2"),
                        Decimal("0.5"),
                        Decimal("1"),
                        Decimal("3"),
                    )
                    for offset in range(10)
                ]

        count = pull_symbol(self.conn, _Broker(), SYMBOL)
        set_history_short(self.conn, SYMBOL, count < 252)
        self.conn.commit()
        self.assertEqual(count, 20)
        row = self.conn.execute(
            "SELECT history_short, history_from FROM symbol_facts WHERE symbol = %s",
            (SYMBOL,),
        ).fetchone()
        self.assertTrue(row[0])
        self.assertEqual(row[1], date(2023, 1, 1))

    def test_young_name_skips_second_year_before_head(self) -> None:
        from datetime import timedelta

        from athena_orchestrator.backfill import pull_symbol, second_span
        from athena_store import apply_migrations

        apply_migrations(self.conn)
        head = date(2025, 6, 1)
        self.assertIsNone(second_span(head, head))
        self.assertEqual(second_span(date(2025, 8, 1), head), "61 D")
        self.assertEqual(second_span(date(2025, 8, 1), None), "1 Y")

        calls: list[tuple[str, str]] = []

        class _Broker:
            def head_day(self, symbol):
                return head

            def history_span(self, symbol, duration, end=""):
                calls.append((duration, end))
                start = head
                return [
                    (
                        start + timedelta(days=offset),
                        Decimal("1"),
                        Decimal("2"),
                        Decimal("0.5"),
                        Decimal("1"),
                        Decimal("3"),
                    )
                    for offset in range(5)
                ]

        count = pull_symbol(self.conn, _Broker(), SYMBOL)
        self.conn.commit()
        self.assertEqual(count, 5)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "1 Y")
        stored = self.conn.execute(
            "SELECT history_from FROM symbol_facts WHERE symbol = %s",
            (SYMBOL,),
        ).fetchone()
        self.assertEqual(stored[0], head)

    def test_complete_history_skips_full_pull_and_catchup(self) -> None:
        from datetime import timedelta
        from unittest.mock import patch

        from athena_orchestrator.backfill import _stored_or_pull
        from athena_store import apply_migrations

        apply_migrations(self.conn)
        today = date(2026, 10, 14)
        start = today - timedelta(days=400)
        for offset in range(260):
            day = start + timedelta(days=offset)
            self._print(
                datetime(day.year, day.month, day.day, tzinfo=timezone.utc),
                "ib-daily", "1", "2", "1.5",
            )
        calls: list[tuple[str, str]] = []

        class _Broker:
            def head_day(self, symbol):
                raise AssertionError("full pull must not ask head")

            def history_span(self, symbol, duration, end=""):
                calls.append((duration, end))
                return [
                    (today, Decimal("1"), Decimal("2"), Decimal("0.5"), Decimal("1.5"), Decimal("3")),
                ]

        frozen = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        with patch("athena_orchestrator.backfill.datetime") as clock:
            clock.now.return_value = frozen
            count, mode = _stored_or_pull(self.conn, _Broker(), SYMBOL)
        self.assertEqual(mode, "catchup")
        self.assertGreaterEqual(count, 260)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][0].endswith(" D"))
        self.assertNotEqual(calls[0][0], "1 Y")

        # Catch-up already wrote today's bar — next pass must be a no-op.
        calls.clear()
        with patch("athena_orchestrator.backfill.datetime") as clock:
            clock.now.return_value = frozen
            count, mode = _stored_or_pull(self.conn, _Broker(), SYMBOL)
        self.assertEqual(mode, "ok")
        self.assertEqual(calls, [])
        self.assertGreaterEqual(count, 260)

    def test_ipo_complete_short_book_skips_full_pull(self) -> None:
        from datetime import timedelta
        from unittest.mock import patch

        from athena_marketdata.pricing import set_history_from
        from athena_orchestrator.backfill import _stored_or_pull
        from athena_store import apply_migrations

        apply_migrations(self.conn)
        head = date(2026, 7, 10)
        set_history_from(self.conn, SYMBOL, head)
        for offset in range(68):
            day = head + timedelta(days=offset)
            self._print(
                datetime(day.year, day.month, day.day, tzinfo=timezone.utc),
                "ib-daily", "1", "2", "1.5",
            )
        self.conn.commit()
        calls: list[str] = []

        class _Broker:
            def head_day(self, symbol):
                raise AssertionError("IPO-complete must not full pull")

            def history_span(self, symbol, duration, end=""):
                calls.append(duration)
                return []

        with patch("athena_orchestrator.backfill.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
            count, mode = _stored_or_pull(self.conn, _Broker(), SYMBOL)
        self.assertEqual(count, 68)
        self.assertIn(mode, {"ok", "catchup"})
        self.assertNotIn("1 Y", calls)

    def test_incomplete_history_still_full_pulls(self) -> None:
        from datetime import timedelta

        from athena_orchestrator.backfill import _stored_or_pull
        from athena_store import apply_migrations

        apply_migrations(self.conn)
        for offset in range(10):
            day = date(2026, 9, 1) + timedelta(days=offset)
            self._print(
                datetime(day.year, day.month, day.day, tzinfo=timezone.utc),
                "ib-daily", "1", "2", "1.5",
            )
        calls: list[str] = []

        class _Broker:
            def head_day(self, symbol):
                return date(2020, 1, 1)

            def history_span(self, symbol, duration, end=""):
                calls.append(duration)
                start = date(2025, 1, 1) if end else date(2026, 1, 1)
                return [
                    (
                        start + timedelta(days=i),
                        Decimal("1"), Decimal("2"), Decimal("0.5"), Decimal("1"), Decimal("3"),
                    )
                    for i in range(5)
                ]

        count, mode = _stored_or_pull(self.conn, _Broker(), SYMBOL)
        self.assertEqual(mode, "stored")
        self.assertIn("1 Y", calls)
        self.assertGreaterEqual(count, 10)

    def _print(self, ts: datetime, source: str, low: str, high: str, close: str) -> None:
        self.conn.execute(
            """
            INSERT INTO market_bars (symbol, ts, open, high, low, close, volume, source)
            VALUES (%s, %s, %s, %s, %s, %s, 0, %s)
            """,
            (SYMBOL, ts, close, high, low, close, source),
        )
        self.conn.commit()

    def test_covered_call_needs_free_shares(self) -> None:
        from datetime import timedelta

        from athena_strategy.drafts import stage_draft

        expiry = date.today() + timedelta(days=40)
        settings = standard_payload()

        def open_call(contracts: int, multiplier: str = "100"):
            return stage_draft(
                self.conn, settings, SYMBOL, "OPEN_CC", contracts, "call", "P1", ACCOUNT,
                instrument="OPT", limit_price=Decimal("1"), expiry=expiry,
                call_strike=Decimal("20"), call_limit=Decimal("1"), multiplier=multiplier,
            )

        naked = open_call(1)
        self.assertEqual(naked["status"], "blocked")
        self.assertIn("covered shares, held 0", naked["detail"])
        self._shares(50)
        odd = open_call(1)
        self.assertEqual(odd["status"], "blocked")
        self.assertIn("held 50", odd["detail"])
        self._clear_gate()
        self._shares(100)
        two = open_call(2)
        self.assertEqual(two["status"], "blocked")
        self.assertIn("needs 200", two["detail"])
        self._clear_gate()
        self._shares(100)
        self._working_call()
        used = open_call(1)
        self.assertEqual(used["status"], "blocked")
        self.assertIn("needs 200", used["detail"])
        self._clear_gate()
        self._shares(100)
        first = open_call(1)
        self.assertEqual(first["status"], "pending")
        second = open_call(1)
        self.assertEqual(second["status"], "blocked")
        self.assertIn("needs 200", second["detail"])
        self._clear_gate()
        self._shares(1000)
        covered = open_call(1, "1000")
        self.assertEqual(covered["status"], "pending")
        self._clear_gate()
        self._shares(100)
        short = open_call(1, "1000")
        self.assertEqual(short["status"], "blocked")
        self.assertIn("needs 1000", short["detail"])

    def test_wheel_allows_a_covered_call_beside_a_short_put(self) -> None:
        from datetime import timedelta

        from athena_strategy.drafts import stage_draft

        expiry = date.today() + timedelta(days=40)
        settings = standard_payload()
        self._option(-1, "P")
        put = stage_draft(
            self.conn, settings, SYMBOL, "OPEN_CSP", 1, "put", "P1", ACCOUNT,
            instrument="OPT", limit_price=Decimal("1"), expiry=expiry,
            put_strike=Decimal("20"), put_limit=Decimal("1"),
        )
        self.assertEqual(put["status"], "blocked")
        self.assertIn("I3: duplicate short put", put["detail"])
        stacked = stage_draft(
            self.conn, settings, SYMBOL, "OPEN_STRANGLE", 1, "strangle", "P1", ACCOUNT,
            instrument="OPT", limit_price=Decimal("2"), expiry=expiry,
            put_strike=Decimal("20"), call_strike=Decimal("30"),
            put_limit=Decimal("1"), call_limit=Decimal("1"),
        )
        self.assertEqual(stacked["status"], "blocked")
        self.assertIn("I3: option leg already open", stacked["detail"])
        self._clear_gate()
        self._shares(100)
        self._option(-1, "P")
        covered = stage_draft(
            self.conn, settings, SYMBOL, "OPEN_CC", 1, "call", "P1", ACCOUNT,
            instrument="OPT", limit_price=Decimal("1"), expiry=expiry,
            call_strike=Decimal("30"), call_limit=Decimal("1"),
        )
        self.assertEqual(covered["status"], "pending")
        self._clear_gate()
        flat = stage_draft(
            self.conn, settings, SYMBOL, "OPEN_CSP", 1, "put", "P1", ACCOUNT,
            instrument="OPT", limit_price=Decimal("1"), expiry=expiry,
            put_strike=Decimal("20"), put_limit=Decimal("1"),
        )
        self.assertEqual(flat["status"], "pending")
        self._clear_gate()
        self._shares(100)
        one = stage_draft(
            self.conn, settings, SYMBOL, "OPEN_CC", 1, "call", "P1", ACCOUNT,
            instrument="OPT", limit_price=Decimal("1"), expiry=expiry,
            call_strike=Decimal("30"), call_limit=Decimal("1"),
        )
        self.assertEqual(one["status"], "pending")
        self._clear_gate()
        self._option(-1, "C")
        beside = stage_draft(
            self.conn, settings, SYMBOL, "OPEN_CSP", 1, "put", "P1", ACCOUNT,
            instrument="OPT", limit_price=Decimal("1"), expiry=expiry,
            put_strike=Decimal("20"), put_limit=Decimal("1"),
        )
        self.assertEqual(beside["status"], "pending")
        self._clear_gate()
        self._shares(100)
        self._option(-1, "P")
        strict = standard_payload().model_copy(
            update={"risk": standard_payload().risk.model_copy(update={"strict_conflict": True})}
        )
        banned = stage_draft(
            self.conn, strict, SYMBOL, "OPEN_CC", 1, "call", "P1", ACCOUNT,
            instrument="OPT", limit_price=Decimal("1"), expiry=expiry,
            call_strike=Decimal("30"), call_limit=Decimal("1"),
        )
        self.assertEqual(banned["status"], "blocked")
        self.assertIn("conflicting short", banned["detail"])

    def test_ticket_budget_blocks_both_legs(self) -> None:
        from athena_settings.model import RiskLimits, TacticFlags
        from athena_strategy.agents import _drafts

        self._book(nav="10000", cash="8000", price="10")
        cash_settings = standard_payload().model_copy(update={"risk": RiskLimits(
            profile="test", max_symbol_pct_nav=80, cash_reserve_pct=30, daily_loss_halt_pct=0,
            max_contracts_per_symbol=12, max_position_per_symbol=0, min_rv_pctl_new_short=0,
            min_dte_new=0, max_short_opt_margin_pct=80, earnings_blackout_days=0, margin_rate=20,
        )})
        flags = TacticFlags(long=True, intent="both")
        answers = {
            "CSP": {"expiry": "2026-12-18", "strike": 40, "limit": 1},
            "Consolidator": {"drafts": [
                {"tactic": "BUY", "limit": 2000, "reason": "rail"},
                {"tactic": "CSP", "limit": 1, "reason": "put"},
            ]},
        }
        self.assertEqual(_drafts(self.conn, cash_settings, ACCOUNT, SYMBOL, flags, answers, {"tags": "none"}), [])
        reasons = self.conn.execute(
            "SELECT status, reason FROM drafts WHERE account_id = %s ORDER BY tactic",
            (ACCOUNT,),
        ).fetchall()
        self.assertEqual(len(reasons), 2)
        self.assertTrue(all(status == "blocked" and reason.startswith("I2: ticket breaches cash_reserve_pct") for status, reason in reasons))
        self._clear_gate()
        self._book(nav="10000", cash="9000", price="20")
        symbol_settings = cash_settings.model_copy(update={"risk": cash_settings.risk.model_copy(update={
            "max_symbol_pct_nav": 20, "cash_reserve_pct": 5,
        })})
        symbol_answers = {
            "CSP": {"expiry": "2026-12-18", "strike": 10, "limit": 1},
            "Consolidator": {"drafts": [
                {"tactic": "BUY", "limit": 1500, "reason": "rail"},
                {"tactic": "CSP", "limit": 1, "reason": "put"},
            ]},
        }
        self.assertEqual(_drafts(self.conn, symbol_settings, ACCOUNT, SYMBOL, flags, symbol_answers, {"tags": "none"}), [])
        symbol_reasons = self.conn.execute(
            "SELECT reason FROM drafts WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchall()
        self.assertEqual(len(symbol_reasons), 2)
        self.assertTrue(all(row[0].startswith("I2: ticket breaches max_symbol_pct_nav") for row in symbol_reasons))
        self._clear_gate()
        self._book(nav="100000", cash="80000", price="10")
        calm = cash_settings.model_copy(update={"risk": cash_settings.risk.model_copy(update={
            "max_symbol_pct_nav": 50, "cash_reserve_pct": 5,
        })})
        calm_answers = {
            "CSP": {"expiry": "2026-12-18", "strike": 10, "limit": 1},
            "Consolidator": {"drafts": [
                {"tactic": "BUY", "limit": 10, "reason": "rail"},
                {"tactic": "CSP", "limit": 1, "reason": "put"},
            ]},
        }
        created = _drafts(self.conn, calm, ACCOUNT, SYMBOL, flags, calm_answers, {"tags": "none"})
        self.assertEqual(len(created), 2)
        pending = self.conn.execute(
            "SELECT status FROM drafts WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchall()
        self.assertTrue(all(row[0] == "pending" for row in pending))

    def _shares(self, quantity: int) -> None:
        self.conn.execute("DELETE FROM positions WHERE account_id = %s AND sec_type = 'STK'", (ACCOUNT,))
        self.conn.execute(
            """
            INSERT INTO positions (account_id, symbol, quantity, avg_price, sec_type, simulated)
            VALUES (%s, %s, %s, 10, 'STK', false)
            """,
            (ACCOUNT, SYMBOL, quantity),
        )
        self.conn.commit()

    def _option(self, quantity: int, right: str) -> None:
        self.conn.execute(
            """
            INSERT INTO positions (
                account_id, symbol, quantity, avg_price, sec_type, contract_right, strike, simulated
            )
            VALUES (%s, %s, %s, 1, 'OPT', %s, 20, false)
            """,
            (ACCOUNT, SYMBOL, quantity, right),
        )
        self.conn.commit()

    def _working_call(self) -> None:
        self.conn.execute(
            """
            INSERT INTO drafts (
                id, symbol, tactic, contracts, status, reason, phase, account_id, instrument, simulated
            )
            VALUES ('draft_zzseed', %s, 'OPEN_CC', 1, 'approved', 'seed', 'P1', %s, 'OPT', false)
            """,
            (SYMBOL, ACCOUNT),
        )
        self.conn.execute(
            """
            INSERT INTO orders (
                id, draft_id, account_id, symbol, tactic, action, contracts, status, contract_right, simulated
            )
            VALUES ('ord_zzseed', 'draft_zzseed', %s, %s, 'OPEN_CC', 'SELL', 1, 'submitted', 'C', false)
            """,
            (ACCOUNT, SYMBOL),
        )
        self.conn.commit()

    def _book(self, nav: str, cash: str, price: str) -> None:
        from psycopg.types.json import Jsonb

        self.conn.execute("DELETE FROM nav_marks WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM account_marks WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM market_bars WHERE symbol = %s", (SYMBOL,))
        self.conn.execute(
            """
            INSERT INTO nav_marks (account_id, day, start_nav, last_nav)
            VALUES (%s, %s, %s, %s)
            """,
            (ACCOUNT, date.today(), nav, nav),
        )
        self.conn.execute(
            """
            INSERT INTO account_marks (account_id, day, cash, margin, base_currency, fx_rates)
            VALUES (%s, %s, %s, 0, 'HKD', %s)
            """,
            (ACCOUNT, date.today(), cash, Jsonb({"USD": "1"})),
        )
        self.conn.execute(
            """
            INSERT INTO market_bars (symbol, ts, open, high, low, close, volume, source)
            VALUES (%s, %s, %s, %s, %s, %s, 1, 'ib-daily')
            """,
            (SYMBOL, datetime(2026, 10, 1, 12, tzinfo=timezone.utc), price, price, price, price),
        )
        self.conn.commit()

    def _sessions(self, count: int) -> None:
        start = date(2024, 1, 2)
        for offset in range(count):
            day = start + timedelta(days=offset)
            self.conn.execute(
                """
                INSERT INTO market_bars (symbol, ts, open, high, low, close, volume, source)
                VALUES (%s, %s, 10, 11, 9, 10, 100, 'ib-daily')
                """,
                (SYMBOL, datetime(day.year, day.month, day.day, 21, tzinfo=timezone.utc)),
            )
        self.conn.commit()

    def _chain(self) -> None:
        self.conn.execute(
            """
            INSERT INTO option_plans (
                symbol, expiry, put_strike, call_strike, put_limit, call_limit,
                trading_class, option_exchange
            )
            VALUES (%s, '2026-12-18', 10, 12, 1, 1, 'ZZT', 'SMART')
            """,
            (SYMBOL,),
        )
        self.conn.commit()

    def _audit_settings(self, **flags) -> object:
        return standard_payload().model_copy(
            update={
                "watchlist": [SYMBOL],
                "tactic_flags": {SYMBOL: TacticFlags(long=True, intent="accumulate", **flags)},
            }
        )

    def _pass(self, replies: list[str], settings=None) -> list:
        queue = list(replies)

        def _chat(_system, user, _model, _provider, _token, _thinking):
            text = queue.pop(0) if queue else '{"quiet":true}'
            return {"text": text, "error": "", "latency_ms": 1, "tokens": 1, "request": {"user": user}}

        apply_migrations(self.conn)
        with patch("athena_strategy.agents.chat", side_effect=_chat):
            return run_legacy_signaled(self.conn, settings or self._audit_settings(), ACCOUNT)

    def _trail(self) -> tuple:
        return self.conn.execute(
            "SELECT outcome, summary FROM decision_trails WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchone()

    def _calls(self) -> list:
        return self.conn.execute(
            "SELECT agent, status FROM ai_calls WHERE account_id = %s ORDER BY id",
            (ACCOUNT,),
        ).fetchall()

    def test_audit_states_cover_drafted_quiet_no_chain_and_invalid(self) -> None:
        quiet = [
            '{"tags":["LOWER_RAIL"]}',
            '{"news_wash":false}',
            '{"quiet":true}',
            '{"quiet":true}',
            '{"quiet":true}',
            '{"quiet":true}',
            '{"thinking":"","drafts":[]}',
        ]
        self._sessions(63)
        self._chain()
        drafted = quiet[:-1] + ['{"thinking":"rail","drafts":[{"tactic":"BUY","limit":10,"reason":"lower rail"}]}']
        self._pass(drafted)
        self.assertEqual(self._trail()[0], "drafted")
        self.assertTrue(self._calls())
        self._clear_gate()
        self.conn.execute("DELETE FROM ai_calls WHERE account_id = %s", (ACCOUNT,))
        self.conn.commit()
        self._pass(quiet)
        self.assertEqual(self._trail(), ("quiet", "no drafts"))
        self.conn.execute("DELETE FROM option_plans WHERE symbol = %s", (SYMBOL,))
        self.conn.execute("DELETE FROM decision_trails WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM ai_calls WHERE account_id = %s", (ACCOUNT,))
        self.conn.commit()
        bare = standard_payload().model_copy(
            update={"watchlist": [SYMBOL], "tactic_flags": {SYMBOL: TacticFlags()}}
        )
        self._pass(quiet[:2], bare)
        self.assertEqual(self._trail(), ("no_data", "no_chain"))
        self.assertEqual([row[0] for row in self._calls()], ["Location", "News"])
        self.assertTrue(all(row[1] == "ok" for row in self._calls()))
        self.conn.execute("DELETE FROM decision_trails WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM ai_calls WHERE account_id = %s", (ACCOUNT,))
        self.conn.commit()
        self._chain()
        self._pass(["not json", "still not"])
        self.assertEqual(self._trail()[0], "invalid")
        self.assertIn("Location", self._trail()[1])
        self.assertIn("parse", self._trail()[1])

    def test_one_invalid_reply_is_retried_once(self) -> None:
        self._sessions(63)
        self._chain()
        replies = [
            "not json",
            '{"tags":[]}',
            '{"news_wash":false}',
            '{"quiet":true}',
            '{"quiet":true}',
            '{"quiet":true}',
            '{"quiet":true}',
            '{"thinking":"","drafts":[]}',
        ]
        self._pass(replies)
        rows = self._calls()
        location = [row for row in rows if row[0] == "Location"]
        self.assertEqual(location, [("Location", "invalid"), ("Location", "ok")])
        self.assertEqual(self._trail()[0], "quiet")
        request = self.conn.execute(
            "SELECT request FROM ai_calls WHERE account_id = %s AND agent = 'Location' ORDER BY id DESC LIMIT 1",
            (ACCOUNT,),
        ).fetchone()
        self.assertIn("Reply with JSON only", request[0]["user"])

    def test_two_invalid_replies_stop_the_pass(self) -> None:
        self._sessions(63)
        self._chain()
        self._pass(["not json", "still not", '{"tags":[]}'])
        rows = self._calls()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row[1] == "invalid" for row in rows))
        self.assertEqual(self._trail()[0], "invalid")

    def test_contradictory_quiet_reply_is_invalid(self) -> None:
        self._sessions(63)
        self._chain()
        bad = '{"quiet": true, "strike": 9}'
        self._pass(['{"tags":[]}', '{"news_wash":false}', bad, bad])
        self.assertEqual(self._trail(), ("invalid", "CSP schema"))
        self.assertEqual(len(self._calls()), 4)

    def test_short_history_does_not_call_the_model(self) -> None:
        self._sessions(10)
        self._chain()
        calls = {"n": 0}

        def _chat(*_args, **_kwargs):
            calls["n"] += 1
            return {"text": '{"tags":[]}', "error": "", "latency_ms": 1, "tokens": 0, "request": {}}

        apply_migrations(self.conn)
        with patch("athena_strategy.agents.chat", side_effect=_chat):
            run_legacy_signaled(self.conn, self._audit_settings(), ACCOUNT)
        self.assertEqual(calls["n"], 0)
        self.assertEqual(self._trail(), ("no_data", "no_bars"))
        self.assertEqual(self._calls(), [])

    def test_halt_blocked_and_options_off_skip_tactics(self) -> None:
        self._sessions(63)
        self._chain()
        apply_migrations(self.conn)
        book_halt(self.conn, ACCOUNT, "test halt")
        self.conn.commit()
        with patch("athena_strategy.agents.chat") as chat:
            run_legacy_signaled(self.conn, self._audit_settings(), ACCOUNT)
        self.assertEqual(chat.call_count, 0)
        self.assertEqual(self._trail(), ("no_data", "halted"))
        self.conn.execute("DELETE FROM halts WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM decision_trails WHERE account_id = %s", (ACCOUNT,))
        self.conn.commit()
        blocked = standard_payload().model_copy(update={"watchlist": [SYMBOL], "blocked_symbols": [SYMBOL]})
        with patch("athena_strategy.agents.chat") as chat:
            run_legacy_signaled(self.conn, blocked, ACCOUNT)
        self.assertEqual(chat.call_count, 0)
        self.assertEqual(self._trail(), ("no_data", "symbol blocked"))
        self.conn.execute("DELETE FROM decision_trails WHERE account_id = %s", (ACCOUNT,))
        self.conn.commit()
        off = standard_payload().model_copy(
            update={
                "watchlist": [SYMBOL],
                "tactic_flags": {SYMBOL: TacticFlags(csp=False, cc=False, strangle=False)},
            }
        )
        with patch("athena_strategy.agents.chat", return_value={
            "text": '{"tags":[],"news_wash":false}',
            "error": "",
            "latency_ms": 1,
            "tokens": 1,
            "request": {},
        }) as chat:
            run_legacy_signaled(self.conn, off, ACCOUNT)
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(self._trail(), ("no_data", "options off"))
        self.assertTrue(all(row[1] == "ok" for row in self._calls()))

    def test_a_cycle_calls_only_the_toggles_that_are_on(self) -> None:
        self._sessions(63)
        self._chain()
        only_csp = standard_payload().model_copy(
            update={
                "watchlist": [SYMBOL],
                "tactic_flags": {
                    SYMBOL: TacticFlags(csp=True, cc=False, strangle=False, long=False, may_buy=False),
                },
            }
        )
        self._pass(
            [
                '{"tags":[]}',
                '{"news_wash":false}',
                '{"quiet":true}',
                '{"thinking":"","drafts":[]}',
            ],
            only_csp,
        )
        self.assertEqual(
            [row[0] for row in self._calls()],
            ["Location", "News", "CSP", "Consolidator"],
        )
        self.conn.execute("DELETE FROM ai_calls WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM decision_trails WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM option_plans WHERE symbol = %s", (SYMBOL,))
        self.conn.commit()
        only_long = standard_payload().model_copy(
            update={
                "watchlist": [SYMBOL],
                "tactic_flags": {
                    SYMBOL: TacticFlags(csp=False, cc=False, strangle=False, long=True, intent="accumulate"),
                },
            }
        )
        self._pass(
            [
                '{"tags":[]}',
                '{"news_wash":false}',
                '{"quiet":true}',
                '{"thinking":"","drafts":[]}',
            ],
            only_long,
        )
        self.assertEqual(
            [row[0] for row in self._calls()],
            ["Location", "News", "Purchase", "Consolidator"],
        )

    def test_chain_snapshot_appends_and_skips_a_missing_bid(self) -> None:
        from athena_exec.session import cache_symbol_chain, record_chain_snapshot
        from athena_strategy.iv import estimated_iv

        apply_migrations(self.conn)
        today = date(2026, 10, 11)
        settings = standard_payload()
        iv_before = self.conn.execute("SELECT count(*) FROM iv_marks").fetchone()[0]

        class _Chain:
            def __init__(self) -> None:
                self.missing = False

            def option_chain(self, symbol):
                return (["20261120"], [], "ZZT", "SMART", "100")

            def expiry_strikes(self, symbol, expiry, trading_class, exchange):
                return [90.0, 100.0, 110.0]

            def option_mid(self, symbol, expiry, strike, right, trading_class, exchange, multiplier="100"):
                if self.missing and right == "P":
                    return None
                return Decimal("1.50") if right == "P" else Decimal("1.20")

            def option_ask(self, symbol, expiry, strike, right, trading_class, exchange, multiplier="100"):
                return Decimal("1")

        broker = _Chain()
        self.assertTrue(record_chain_snapshot(self.conn, broker, settings, SYMBOL, Decimal("105"), today))
        self.assertTrue(record_chain_snapshot(self.conn, broker, settings, SYMBOL, Decimal("105"), today))
        rows = self.conn.execute(
            """
            SELECT expiry, dte, k_call, k_put, moneyness_pct, mid_call, mid_put
            FROM chain_marks WHERE symbol = %s ORDER BY id
            """,
            (SYMBOL,),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0], None)
        self.assertEqual(rows[0][0], date(2026, 11, 20))
        self.assertEqual(rows[0][2], Decimal("110"))
        self.assertEqual(rows[0][3], Decimal("100"))
        self.assertEqual(rows[0][5], Decimal("1.20"))
        self.assertEqual(rows[0][6], Decimal("1.50"))
        self.assertGreater(rows[0][4], 0)
        broker.missing = True
        self.assertFalse(record_chain_snapshot(self.conn, broker, settings, SYMBOL, Decimal("105"), today))
        count = self.conn.execute(
            "SELECT count(*) FROM chain_marks WHERE symbol = %s",
            (SYMBOL,),
        ).fetchone()[0]
        self.assertEqual(count, 2)
        self.assertIsNone(estimated_iv(self.conn, SYMBOL, today))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM iv_marks").fetchone()[0], iv_before)
        cache_symbol_chain(self.conn, broker, settings, SYMBOL, Decimal("105"), today)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM chain_marks WHERE symbol = %s", (SYMBOL,)).fetchone()[0],
            2,
        )

    def test_every_expiry_in_the_band_stores_an_ask(self) -> None:
        from athena_exec.session import _store_choices

        apply_migrations(self.conn)
        today = date(2026, 10, 11)
        settings = standard_payload()
        expiries = ["20261106", "20261120", "20261204"]
        asked: list[str] = []

        class _Band:
            def expiry_strikes(self, symbol, expiry, trading_class, exchange):
                return [80.0, 90.0, 110.0, 120.0]

            def option_ask(self, symbol, expiry, strike, right, trading_class, exchange, multiplier="100"):
                asked.append(expiry)
                return Decimal("1.25")

        _store_choices(
            self.conn, _Band(), SYMBOL, expiries, today, Decimal("100"),
            "ZZT", "SMART", "100", settings.strategy, None,
        )
        rows = self.conn.execute(
            """
            SELECT expiry, count(*) FROM option_choices
            WHERE symbol = %s AND limit_price > 0
            GROUP BY expiry ORDER BY expiry
            """,
            (SYMBOL,),
        ).fetchall()
        self.assertEqual([row[0] for row in rows], [date(2026, 11, 6), date(2026, 11, 20), date(2026, 12, 4)])
        self.assertTrue(all(row[1] > 0 for row in rows))
        self.assertEqual(set(asked), {date(2026, 11, 6), date(2026, 11, 20), date(2026, 12, 4)})

    def test_a_chain_quote_stores_the_bid(self) -> None:
        from athena_exec.session import _store_choices

        apply_migrations(self.conn)
        today = date(2026, 10, 11)
        settings = standard_payload()

        class _Quote:
            def expiry_strikes(self, symbol, expiry, trading_class, exchange):
                return [90.0, 110.0]

            def option_quote(self, symbol, expiry, strike, right, trading_class, exchange, multiplier="100"):
                return Decimal("2"), Decimal("1.9")

        _store_choices(
            self.conn, _Quote(), SYMBOL, ["20261120"], today, Decimal("100"),
            "ZZT", "SMART", "100", settings.strategy, None,
        )
        row = self.conn.execute(
            """
            SELECT limit_price, bid, open_interest, volume
            FROM option_choices WHERE symbol = %s LIMIT 1
            """,
            (SYMBOL,),
        ).fetchone()
        self.assertEqual(row, (Decimal("2"), Decimal("1.9"), None, None))

    def test_universe_toggle_returns_the_row(self) -> None:
        import json

        from athena_orchestrator.pages import _post_universe, _universe
        from athena_settings.model import SettingsPayload, TacticFlags, standard_payload
        from athena_settings.repository import load_active, save_settings

        try:
            before = load_active(self.conn, ACCOUNT)
        except RuntimeError:
            from athena_settings.repository import seed_standard_if_empty

            seed_standard_if_empty(self.conn, ACCOUNT)
            before = load_active(self.conn, ACCOUNT)
        original = SettingsPayload.model_validate(
            {key: before.model_dump()[key] for key in SettingsPayload.model_fields}
        )
        staged = standard_payload().model_copy(
            update={
                "watchlist": [SYMBOL],
                "tactic_flags": {SYMBOL: TacticFlags(csp=True, cc=True, strangle=True)},
            }
        )
        try:
            save_settings(self.conn, ACCOUNT, staged, "test", "universe row")
            result = _post_universe(
                self.conn,
                ACCOUNT,
                {"symbol": [SYMBOL], "action": ["toggle"], "field": ["csp"], "partial": ["row"]},
                "test",
            )
            self.assertEqual(result["kind"], "json")
            data = json.loads(result["body"])
            self.assertTrue(data["ok"])
            self.assertIn(f'data-symbol="{SYMBOL}"', data["html"])
            self.assertIn('class="pool-spin sim-spin"', data["html"])
            self.assertIn('aria-label="CSP"', data["html"])
            self.assertNotIn('class="pool-tgl on" type="submit" aria-label="CSP"', data["html"])
            fetched = json.loads(
                _universe(self.conn, ACCOUNT, {"partial": ["symbol"], "symbol": [SYMBOL]})["body"]
            )
            self.assertEqual(fetched["html"], data["html"])
            self.assertNotIn("<html", result["body"])
        finally:
            try:
                save_settings(self.conn, ACCOUNT, original, "test", "restore universe row")
            except ValueError:
                pass

    def test_settings_show_the_quote_cap(self) -> None:
        from athena_orchestrator.pages import _settings

        page = _settings(self.conn, ACCOUNT, {})
        self.assertIn('name="concurrent_subscriptions"', page["body"])
        self.assertIn("Concurrent subscriptions<button", page["body"])

    def test_seed_defaults_adds_the_paper_universe(self) -> None:
        from athena_orchestrator.pages import _post_universe, _universe
        from athena_settings.model import UNIVERSE_SEED, SettingsPayload, standard_payload
        from athena_settings.repository import load_active, save_settings

        try:
            before = load_active(self.conn, ACCOUNT)
        except RuntimeError:
            from athena_settings.repository import seed_standard_if_empty

            seed_standard_if_empty(self.conn, ACCOUNT)
            before = load_active(self.conn, ACCOUNT)
        original = SettingsPayload.model_validate(
            {key: before.model_dump()[key] for key in SettingsPayload.model_fields}
        )
        staged = standard_payload().model_copy(update={"watchlist": [SYMBOL]})
        try:
            save_settings(self.conn, ACCOUNT, staged, "test", "universe seed setup")
            page = _universe(self.conn, ACCOUNT, {})
            self.assertIn('aria-label="Market for ', page["body"])
            self.assertIn(
                'data-cmd="seed-defaults">seed defaults</button>'
                '<button class="more" type="button" data-cmd="add-symbol">',
                page["body"],
            )
            result = _post_universe(self.conn, ACCOUNT, {"action": ["seed"]}, "test")
            self.assertIn("Added 37 seed symbols.", result["body"])
            saved = load_active(self.conn, ACCOUNT)
            self.assertEqual(saved.watchlist[0], SYMBOL)
            self.assertEqual(saved.watchlist[1:], list(UNIVERSE_SEED))
            self.assertEqual(saved.tactic_flags["KULR"].market, "us")
            self.assertEqual(saved.tactic_flags["MRAM"].market, "us")
            again = _post_universe(self.conn, ACCOUNT, {"action": ["seed"]}, "test")
            self.assertIn("The pool already has the seed symbols.", again["body"])
            self.assertEqual(load_active(self.conn, ACCOUNT).watchlist, saved.watchlist)
        finally:
            try:
                save_settings(self.conn, ACCOUNT, original, "test", "restore universe seed")
            except ValueError:
                pass

    def test_chain_list_skips_names_with_options_off(self) -> None:
        from athena_exec.session import chain_names
        from athena_settings.model import TacticFlags
        from athena_strategy.iv import estimated_iv

        settings = standard_payload().model_copy(
            update={
                "watchlist": ["SPY", "QQQ", "NVDA"],
                "tactic_flags": {
                    "SPY": TacticFlags(csp=False, cc=False, strangle=False, long=False),
                    "QQQ": TacticFlags(csp=False, cc=True, strangle=False),
                },
            }
        )
        self.assertEqual(chain_names(settings), ["QQQ", "NVDA"])
        self.assertIsNone(estimated_iv(self.conn, "SPY", date(2026, 10, 11)))

    def test_history_list_adds_spy_without_touching_the_watchlist(self) -> None:
        from athena_orchestrator.backfill import history_symbols, listing_symbol, watchlist_after_listing

        watch = ["NVDA", "0992"]
        symbols = history_symbols([watch], ["spy"])
        self.assertEqual(symbols, ["NVDA", "0992", "SPY"])
        self.assertEqual(watch, ["NVDA", "0992"])
        self.assertEqual(history_symbols([["SPY"]], ["SPY"]), ["SPY"])

        class _Qualify:
            def qualifies_stock(self, symbol):
                return symbol == "992"

        self.assertEqual(listing_symbol(_Qualify(), "0992"), "992")
        self.assertEqual(listing_symbol(_Qualify(), "NVDA"), "NVDA")
        self.assertEqual(watchlist_after_listing(["NVDA", "0992", "992"], "0992", "992"), ["NVDA", "992"])

        class _Keep:
            def qualifies_stock(self, symbol):
                return symbol == "0992"

        self.assertEqual(listing_symbol(_Keep(), "0992"), "0992")

    def _clear_gate(self) -> None:
        self.conn.execute("DELETE FROM orders WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM drafts WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM positions WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM decision_trails WHERE account_id = %s", (ACCOUNT,))
        self.conn.commit()

