"""Replay, approve, and phase chains on account ZZTEST.

Skipped when DATABASE_URL is unset or Postgres is down. These tests never
read or write the paper account and never call placeOrder.
"""

import json
import os
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import Mock, patch

from athena_exec.gate import live_block_reason
from athena_exec.halt import halt as book_halt
from athena_exec.session import _submit_queued
from athena_settings.model import standard_payload
from athena_store.migrate import connect
from athena_strategy.drafts import decide
from athena_strategy.iv import record_iv_marks, tags_from_iv
from athena_strategy.manage import manage_book, management_tactic
from athena_strategy.phases import evaluate_symbol

ACCOUNT = "ZZTEST"
RUN = "sim_zztest"
SYMBOL = "ZZT"


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
        self.conn.execute("DELETE FROM positions WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM sim_runs WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM symbol_phases WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM halts WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM decision_trails WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM ai_calls WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM nav_marks WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM account_marks WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM option_plans WHERE symbol = %s", (SYMBOL,))
        self.conn.execute("DELETE FROM market_bars WHERE symbol = %s", (SYMBOL,))
        self.conn.execute("DELETE FROM iv_marks WHERE symbol = %s", (SYMBOL,))
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

    def test_open_csp_is_stored_as_hold_and_a_short_option_rolls(self) -> None:
        evaluate_symbol(
            self.conn,
            standard_payload(),
            SYMBOL,
            ACCOUNT,
            tags={"LOW_IV", "OVERSOLD", "SUPPORT_TOUCH"},
            event_offset=2,
        )
        stored = self.conn.execute(
            "SELECT phase, tactic FROM symbol_phases WHERE account_id = %s AND symbol = %s",
            (ACCOUNT, SYMBOL),
        ).fetchone()
        self.assertEqual(stored, ("P4", "HOLD"))
        tactic, _reason = management_tactic(
            sec_type="OPT",
            quantity=Decimal("-1"),
            avg_price=Decimal("2"),
            market_price=Decimal("1.5"),
            expiry=date(2026, 10, 20),
            today=date(2026, 10, 10),
            roll_dte=21,
            profit_take_pct=50,
        )
        self.assertEqual(tactic, "ROLL")
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
        self.assertTrue(created)
        draft = self.conn.execute(
            "SELECT tactic FROM drafts WHERE account_id = %s AND tactic = 'ROLL'",
            (ACCOUNT,),
        ).fetchone()
        self.assertEqual(draft[0], "ROLL")
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
        settings = settings.model_copy(
            update={"strategy": settings.strategy.model_copy(update={"ivr_min_samples": 1})}
        )
        self.assertIn("HIGH_IV", tags_from_iv(self.conn, SYMBOL, settings))

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
