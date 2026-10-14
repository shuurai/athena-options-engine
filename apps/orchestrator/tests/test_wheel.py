"""Wheel state, one opening ticket, and the three-agent pass. ZZTEST only."""

import os
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from athena_orchestrator.pages import (
    FORM_LIMITS,
    PROFILE_COPY,
    PROFILE_LIMITS,
    PROFILES,
    _book_page,
    _heat_color,
    _position_heatmap,
    _squarify,
    _pool_row,
    _post_risk,
    _risk,
    _strategy,
    _volatility_page,
    band_gap,
    listed_otm_pct,
    method_switch_note,
    reading_shift,
    threshold_preview,
    vol_tag,
)
from athena_settings.model import TacticFlags, standard_payload
from athena_settings.prompts import default_agents
from athena_settings.repository import load_active, sync_standing_agents
from athena_strategy.iv import sanity_label
from athena_store.migrate import apply_migrations, connect
from athena_strategy.agents import run_observe, run_signaled
from athena_strategy.budget import apply_cycle_budget
from athena_strategy.indicators import indicators_enabled, read_indicators
from athena_strategy.manage import manage_book
from athena_strategy.screener import screener_enabled
from athena_strategy.tags import WEEK_DIP
from athena_strategy.wheel import (
    WheelState,
    alias_of,
    buy_access,
    commit_refused,
    decide_opening,
    dry_run_enabled,
    execute_opening,
    may_buy,
    refusal,
    slot_split_enabled,
    state_machine_enabled,
    transition_actions,
    transition_names,
    share_gap,
    wheel_state,
)

ACCOUNT = "ZZTEST"
SYMBOL = "ZZT"


def _channels(color: str) -> tuple[int, int, int]:
    raw = color.lstrip("#")
    return tuple(int(raw[index:index + 2], 16) for index in (0, 2, 4))


def _rect_overlap(left: tuple, right: tuple) -> float:
    ax, ay, aw, ah = left
    bx, by, bw, bh = right
    width = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    height = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    return width * height


def _heat_row(symbol: str, pnl: int, avg: int, qty: int, market: int) -> dict:
    return {
        "symbol": symbol,
        "qty": Decimal(qty),
        "avg": Decimal(avg),
        "sec_type": "STK",
        "right": "",
        "strike": "",
        "mark": "10.00",
        "market": Decimal(market),
        "pnl": Decimal(pnl),
        "multiplier": "",
    }


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


def _state(shares, puts=0, calls=0) -> WheelState:
    return WheelState(SYMBOL, Decimal(shares), puts, calls, (), (), Decimal("95") if shares else None)


class WheelAliasTest(unittest.TestCase):
    def test_each_alias_matches_its_counts(self) -> None:
        cases = {
            "FLAT": _state(0),
            "ODD_LOT": _state(1),
            "LONG": _state(100),
            "SHORT_PUT": _state(0, puts=1),
            "ODD_PUT": _state(40, puts=1),
            "ASSIGNED": _state(300, puts=1),
            "LONG_CALL": _state(100, calls=1),
            "WHEELING": _state(200, puts=1, calls=1),
            "STRANGLE": _state(0, puts=1, calls=1),
            "NAKED_CALL": _state(1, calls=1),
            "MULTI_CONFLICT": _state(0, puts=2),
        }
        for name, state in cases.items():
            self.assertEqual(alias_of(state), name)
        self.assertEqual(alias_of(_state(0, calls=1)), "NAKED_CALL")

    def test_every_transition_cell_has_a_code_or_is_open(self) -> None:
        refused = 0
        allowed = 0
        for name in transition_names():
            for action in transition_actions():
                code = refusal(name, action)
                if code is None:
                    allowed += 1
                else:
                    refused += 1
                    self.assertIsInstance(code, str)
                    self.assertTrue(code)
        self.assertEqual(allowed + refused, len(transition_names()) * len(transition_actions()))
        self.assertEqual(refusal("FLAT", "OPEN_CC"), "cc_needs_shares")
        self.assertEqual(refusal("ODD_LOT", "OPEN_CC"), "cc_needs_shares")
        self.assertIsNone(refusal("ODD_LOT", "OPEN_CSP"))
        self.assertEqual(refusal("ODD_PUT", "OPEN_CSP"), "duplicate_short_put")
        self.assertIsNone(refusal("ODD_PUT", "CLOSE"))
        self.assertEqual(refusal("SHORT_PUT", "OPEN_CSP"), "duplicate_short_put")
        self.assertEqual(refusal("NAKED_CALL", "OPEN_CC"), "already_naked")
        self.assertIsNone(refusal("NAKED_CALL", "BUY"))
        self.assertEqual(refusal("MULTI_CONFLICT", "BUY"), "manual_review")
        self.assertIsNone(refusal("MULTI_CONFLICT", "CLOSE"))
        self.assertIsNone(refusal("LONG", "OPEN_CC"))
        self.assertIsNone(refusal("ASSIGNED", "OPEN_CC"))
        self.assertEqual(refusal("WHEELING", "OPEN_CC"), "duplicate_short_call")
        self.assertEqual(refusal("STRANGLE", "OPEN_CSP"), "leg_already_open")
        self.assertEqual(refusal("FLAT", "CLOSE"), "nothing_to_close")
        self.assertIsNone(refusal("FLAT", "OPEN_STRANGLE"))
        self.assertIsNone(refusal("LONG", "OPEN_STRANGLE"))
        self.assertEqual(refusal("SHORT_PUT", "OPEN_STRANGLE"), "leg_already_open")
        self.assertIsNone(refusal("SHORT_PUT", "ROLL"))
        self.assertIsNone(refusal("NAKED_CALL", "CLOSE"))
        self.assertEqual(refusal("MULTI_CONFLICT", "ROLL"), "manual_review")

    def test_each_account_keeps_its_own_tactics(self) -> None:
        from athena_strategy.wheel import enabled_functions

        account_a = TacticFlags(csp=True, cc=False, strangle=False, long=False, may_buy=False)
        account_b = TacticFlags(csp=False, cc=True, strangle=False, long=False, may_buy=False)
        self.assertEqual(enabled_functions(account_a), frozenset({"CSP"}))
        self.assertEqual(enabled_functions(account_b), frozenset({"CC"}))
        self.assertNotIn("CC", enabled_functions(account_a))
        self.assertNotIn("CSP", enabled_functions(account_b))

    def test_income_does_not_buy_unless_may_buy_is_on(self) -> None:
        self.assertFalse(may_buy(TacticFlags(long=True, intent="income")))
        self.assertTrue(may_buy(TacticFlags(long=True, intent="income", may_buy=True)))
        self.assertTrue(may_buy(TacticFlags(long=True, intent="both")))
        self.assertFalse(may_buy(TacticFlags()))
        self.assertEqual(buy_access(TacticFlags(long=True, intent="both")), (True, "allowed"))
        self.assertEqual(buy_access(TacticFlags(long=True, intent="income")), (False, "income"))
        self.assertEqual(buy_access(TacticFlags()), (False, "long off"))
        self.assertEqual(buy_access(TacticFlags(long=True, intent="both", may_buy=False)), (False, "may_buy off"))
        self.assertEqual(buy_access(TacticFlags(long=True, intent="income", may_buy=True)), (True, "allowed"))

    def test_share_gap_is_the_shares_still_needed_for_one_lot(self) -> None:
        self.assertEqual(share_gap(Decimal(0)), 0)
        self.assertEqual(share_gap(Decimal(1)), 99)
        self.assertEqual(share_gap(Decimal(40)), 60)
        self.assertEqual(share_gap(Decimal("99.2")), 1)
        self.assertEqual(share_gap(Decimal(100)), 0)

    def test_engine_a_file_does_not_name_an_opening_tactic(self) -> None:
        text = Path("packages/strategy-nfr/src/athena_strategy/phases.py").read_text()
        for name in ("OPEN_CSP", "OPEN_CC", "OPEN_STRANGLE"):
            self.assertNotIn(name, text)


class WheelDbTest(unittest.TestCase):
    def setUp(self) -> None:
        self._machine = os.environ.get("ATHENA_STATE_MACHINE")
        os.environ["ATHENA_STATE_MACHINE"] = "on"
        self.conn = connect(_url())
        apply_migrations(self.conn)
        self._wipe()

    def tearDown(self) -> None:
        self._wipe()
        self.conn.close()
        if self._machine is None:
            os.environ.pop("ATHENA_STATE_MACHINE", None)
        else:
            os.environ["ATHENA_STATE_MACHINE"] = self._machine

    def _wipe(self) -> None:
        self.conn.rollback()
        self.conn.execute("DELETE FROM orders WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM drafts WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM tickets WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM positions WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM decision_trails WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM ai_calls WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM symbol_phases WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM nav_marks WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM account_marks WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM option_plans WHERE symbol = %s", (SYMBOL,))
        self.conn.execute("DELETE FROM option_choices WHERE symbol = %s", (SYMBOL,))
        self.conn.execute("DELETE FROM market_bars WHERE symbol = %s", (SYMBOL,))
        self.conn.execute("DELETE FROM rv_marks WHERE symbol = %s", (SYMBOL,))
        self.conn.commit()

    def _bars(self, closes: list[Decimal]) -> Decimal:
        end = date.today()
        for index, close in enumerate(closes):
            day = end - timedelta(days=len(closes) - 1 - index)
            self.conn.execute(
                """
                INSERT INTO market_bars (symbol, ts, open, high, low, close, volume, source)
                VALUES (%s, %s, %s, %s, %s, %s, 1000, 'ib-daily')
                """,
                (
                    SYMBOL,
                    datetime(day.year, day.month, day.day, 16, tzinfo=timezone.utc),
                    close,
                    close + 1,
                    close - 1,
                    close,
                ),
            )
        self.conn.commit()
        return closes[-1]

    def _dip_closes(self) -> list[Decimal]:
        """Rising channel, then a ~5-session pullback with a slight reclaim (week dip)."""
        closes = [Decimal("100") + Decimal(index) * Decimal("0.8") for index in range(80)]
        closes.extend([Decimal("190"), Decimal("188"), Decimal("182"), Decimal("178"), Decimal("180")])
        return closes

    def _high_closes(self) -> list[Decimal]:
        closes = []
        for index in range(100):
            if index < 30:
                closes.append(Decimal("50") + Decimal(index))
            else:
                closes.append(Decimal("80") + Decimal(index - 30) * Decimal("19") / Decimal("69"))
        closes[-1] = Decimal("98.6")
        return closes

    def _choices(self, spot: Decimal, right: str, distances: tuple[str, ...]) -> None:
        expiry = date.today() + timedelta(days=33)
        for distance in distances:
            strike = (spot * Decimal(distance)).quantize(Decimal("1"))
            self.conn.execute(
                """
                INSERT INTO option_choices (symbol, expiry, contract_right, strike, limit_price)
                VALUES (%s, %s, %s, %s, 2)
                ON CONFLICT (symbol, expiry, contract_right, strike) DO UPDATE
                SET limit_price = EXCLUDED.limit_price
                """,
                (SYMBOL, expiry, right, strike),
            )
        self.conn.execute(
            """
            INSERT INTO option_plans (
                symbol, expiry, put_strike, call_strike, put_limit, call_limit,
                trading_class, option_exchange, multiplier
            )
            VALUES (%s, %s, %s, %s, 2, 2, 'ZZT', 'SMART', '100')
            ON CONFLICT (symbol) DO UPDATE SET expiry = EXCLUDED.expiry
            """,
            (SYMBOL, expiry, spot - 5, spot + 5),
        )
        self.conn.commit()

    def _rv_history(self) -> None:
        today = date.today()
        for back in range(1, 20):
            self.conn.execute(
                """
                INSERT INTO rv_marks (symbol, day, rv20, method)
                VALUES (%s, %s, 0.1, 'close_log_252')
                ON CONFLICT (symbol, day) DO UPDATE SET rv20 = EXCLUDED.rv20
                """,
                (SYMBOL, today - timedelta(days=back)),
            )
        self.conn.commit()

    def _settings(self, **flags):
        return standard_payload().model_copy(
            update={"watchlist": [SYMBOL], "tactic_flags": {SYMBOL: TacticFlags(**flags)}}
        )

    def _stock(self, shares: int, cost: str = "95") -> None:
        self.conn.execute(
            """
            INSERT INTO positions (account_id, symbol, quantity, avg_price, sec_type, simulated)
            VALUES (%s, %s, %s, %s, 'STK', false)
            """,
            (ACCOUNT, SYMBOL, shares, Decimal(cost)),
        )
        self.conn.commit()

    def _short(self, right: str, strike: str = "90") -> None:
        self.conn.execute(
            """
            INSERT INTO positions (
                account_id, symbol, quantity, avg_price, sec_type, expiry,
                contract_right, strike, market_price, simulated
            )
            VALUES (%s, %s, -1, 2, 'OPT', %s, %s, %s, 1, false)
            """,
            (ACCOUNT, SYMBOL, date.today() + timedelta(days=20), right, Decimal(strike)),
        )
        self.conn.commit()

    def test_state_is_stable_and_counts_a_submitted_sell(self) -> None:
        self._stock(100, "10")
        self.conn.execute(
            """
            INSERT INTO drafts (id, symbol, tactic, contracts, status, reason, phase, account_id)
            VALUES ('draft_wheel', %s, 'OPEN_CSP', 1, 'approved', 'test', 'P1', %s)
            """,
            (SYMBOL, ACCOUNT),
        )
        self.conn.execute(
            """
            INSERT INTO orders (
                id, draft_id, account_id, symbol, tactic, action, contracts, status,
                contract_right, strike, simulated
            )
            VALUES ('ord_wheel', 'draft_wheel', %s, %s, 'OPEN_CSP', 'SELL', 1, 'submitted', 'P', 9, false)
            """,
            (ACCOUNT, SYMBOL),
        )
        self.conn.commit()
        first = wheel_state(self.conn, ACCOUNT, SYMBOL)
        second = wheel_state(self.conn, ACCOUNT, SYMBOL)
        self.assertEqual(first, second)
        self.assertEqual(alias_of(first), "ASSIGNED")
        self.assertEqual(first.short_puts, 1)

    def test_a_naked_call_writes_no_opening_ticket(self) -> None:
        self._stock(1, "10")
        self._short("C", "12")
        ids = execute_opening(self.conn, self._settings(), ACCOUNT, SYMBOL)
        self.assertEqual(ids, [])
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM tickets WHERE account_id = %s", (ACCOUNT,)).fetchone()
        )
        trail = self.conn.execute(
            "SELECT outcome, summary FROM decision_trails WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchone()
        self.assertEqual(trail, ("dropped", "NAKED_CALL"))

    def test_a_buy_plus_a_covered_call_blocks_both_legs(self) -> None:
        ticket = commit_refused(self.conn, self._settings(long=True, intent="both"), ACCOUNT, SYMBOL, ("BUY", "OPEN_CC"))
        rows = self.conn.execute(
            """
            SELECT tactic, status, reason, ticket_id, leg_index
            FROM drafts WHERE account_id = %s ORDER BY leg_index
            """,
            (ACCOUNT,),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row[1] == "blocked" and row[2] == "cc_needs_shares" and row[3] == ticket for row in rows))
        self.assertEqual([row[4] for row in rows], [0, 1])

    def test_dip_long_buys_shares_and_sells_a_put(self) -> None:
        spot = self._bars(self._dip_closes())
        self._choices(spot, "P", ("0.97", "0.95", "0.93"))
        self._rv_history()
        reading = read_indicators(self.conn, standard_payload(), SYMBOL)
        self.assertTrue(reading.week_dip, reading)
        self.assertIn("RV_HIGH", reading.tags, reading.tags)
        ids = execute_opening(
            self.conn, self._settings(long=True, intent="both"), ACCOUNT, SYMBOL,
        )
        self.assertEqual(len(ids), 2)
        ticket = self.conn.execute(
            "SELECT reason, state_before FROM tickets WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchone()
        self.assertEqual(ticket[0], "dip_long")
        self.assertEqual(ticket[1], "FLAT")
        legs = self.conn.execute(
            """
            SELECT tactic, leg_index, ticket_id, status
            FROM drafts WHERE account_id = %s ORDER BY leg_index
            """,
            (ACCOUNT,),
        ).fetchall()
        self.assertEqual([(row[0], row[1], row[3]) for row in legs], [("BUY", 0, "pending"), ("OPEN_CSP", 1, "pending")])
        self.assertEqual(legs[0][2], legs[1][2])
        again = execute_opening(self.conn, self._settings(long=True, intent="both"), ACCOUNT, SYMBOL)
        self.assertEqual(again, [])

    def test_income_dip_sells_a_put_without_a_share_purchase(self) -> None:
        spot = self._bars(self._dip_closes())
        self._choices(spot, "P", ("0.97", "0.95", "0.93"))
        self._rv_history()
        ids = execute_opening(self.conn, self._settings(long=False, intent="income"), ACCOUNT, SYMBOL)
        self.assertEqual(len(ids), 1)
        row = self.conn.execute(
            "SELECT tactic, reason FROM drafts WHERE id = %s",
            (ids[0],),
        ).fetchone()
        self.assertEqual(row[0], "OPEN_CSP")
        ticket = self.conn.execute(
            "SELECT reason FROM tickets WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchone()
        self.assertEqual(ticket[0], "dip_csp")
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM drafts WHERE account_id = %s AND tactic = 'BUY'",
                (ACCOUNT,),
            ).fetchone()
        )

    def test_a_week_dip_with_a_slight_reclaim_sells_a_csp(self) -> None:
        """Oct-13 style: WEEK_DIP candidate → dip_csp when code stages (AI gate is separate)."""
        from athena_strategy.wheel import _dip

        closes = [Decimal("200") + Decimal(index) * Decimal("0.2") for index in range(70)]
        closes.extend([
            Decimal("239"),
            Decimal("230"),
            Decimal("229"),
            Decimal("226"),
            Decimal("227"),
        ])
        spot = self._bars(closes)
        self._choices(spot, "P", ("0.97", "0.95", "0.93"))
        self._rv_history()
        reading = read_indicators(self.conn, standard_payload(), SYMBOL)
        self.assertTrue(reading.week_dip, reading)
        self.assertIn(WEEK_DIP, reading.tags, reading.tags)
        self.assertTrue(_dip(reading, set(), standard_payload()))
        opening = decide_opening(
            self.conn, self._settings(long=False, intent="income", csp=True), ACCOUNT, SYMBOL,
        )
        self.assertEqual(opening.reason, "dip_csp", opening)
        self.assertNotEqual(opening.quiet, "not_a_dip")
        ids = execute_opening(
            self.conn, self._settings(long=False, intent="income", csp=True), ACCOUNT, SYMBOL,
        )
        self.assertEqual(len(ids), 1)
        self.assertEqual(
            self.conn.execute("SELECT tactic FROM drafts WHERE id = %s", (ids[0],)).fetchone()[0],
            "OPEN_CSP",
        )

    def test_a_flat_week_is_not_a_dip(self) -> None:
        from athena_strategy.wheel import _dip

        closes = [Decimal("100") + Decimal(index) * Decimal("0.05") for index in range(80)]
        self._bars(closes)
        reading = read_indicators(self.conn, standard_payload(), SYMBOL)
        self.assertFalse(reading.week_dip)
        self.assertNotIn(WEEK_DIP, reading.tags)
        self.assertFalse(_dip(reading, set(), standard_payload()))
        opening = decide_opening(
            self.conn,
            self._settings(long=False, intent="income", csp=True, cc=False, strangle=False),
            ACCOUNT,
            SYMBOL,
        )
        self.assertEqual(opening.quiet, "not_a_dip")

    def test_multi_conflict_stays_refused_before_narrator(self) -> None:
        self._short("P", "9")
        self._short("P", "8")
        state = wheel_state(self.conn, ACCOUNT, SYMBOL)
        self.assertEqual(alias_of(state), "MULTI_CONFLICT")
        opening = decide_opening(
            self.conn, self._settings(long=False, intent="income", csp=True), ACCOUNT, SYMBOL,
        )
        self.assertEqual(opening.outcome, "dropped")
        self.assertEqual(opening.quiet, "MULTI_CONFLICT")
        ids = execute_opening(
            self.conn, self._settings(long=False, intent="income", csp=True), ACCOUNT, SYMBOL,
        )
        self.assertEqual(ids, [])

    def test_assigned_shares_sell_a_call_above_cost(self) -> None:
        spot = self._bars(self._high_closes())
        self._rv_history()
        self._stock(300, "95")
        self._short("P", "90")
        self._choices(spot, "C", ("1.03", "1.05", "1.08"))
        state = wheel_state(self.conn, ACCOUNT, SYMBOL)
        self.assertEqual(alias_of(state), "ASSIGNED")
        reading = read_indicators(self.conn, standard_payload(), SYMBOL)
        self.assertGreaterEqual(reading.rail_pos, Decimal("0.80"))
        ids = execute_opening(self.conn, self._settings(), ACCOUNT, SYMBOL)
        self.assertEqual(len(ids), 1)
        draft = self.conn.execute(
            "SELECT tactic, call_strike FROM drafts WHERE id = %s",
            (ids[0],),
        ).fetchone()
        self.assertEqual(draft[0], "OPEN_CC")
        self.assertGreaterEqual(draft[1], Decimal("95"))
        ticket = self.conn.execute(
            "SELECT reason, state_before, state_after FROM tickets WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchone()
        self.assertEqual(ticket, ("high_cc", "ASSIGNED", "WHEELING"))

    def test_odd_lot_at_the_rail_buys_the_gap_to_100(self) -> None:
        self._bars(self._high_closes())
        self._stock(1, "10")
        ids = execute_opening(self.conn, self._settings(may_buy=True), ACCOUNT, SYMBOL)
        self.assertEqual(len(ids), 1)
        draft = self.conn.execute(
            "SELECT tactic, contracts, status FROM drafts WHERE id = %s",
            (ids[0],),
        ).fetchone()
        self.assertEqual(draft, ("BUY", 99, "pending"))
        ticket = self.conn.execute(
            "SELECT reason, state_before, state_after FROM tickets WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchone()
        self.assertEqual(ticket, ("cover_gap", "ODD_LOT", "LONG"))
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM drafts WHERE account_id = %s AND tactic = 'OPEN_CC'",
                (ACCOUNT,),
            ).fetchone()
        )

    def test_an_odd_lot_does_not_buy_when_may_buy_is_off(self) -> None:
        self._bars(self._high_closes())
        self._stock(1, "10")
        ids = execute_opening(self.conn, self._settings(), ACCOUNT, SYMBOL)
        self.assertEqual(ids, [])

    def test_a_strangle_toggle_is_enough_without_csp_or_cc(self) -> None:
        spot = self._bars(self._dip_closes())
        self._rv_history()
        self._stock(100, "10")
        self._choices(spot, "P", ("0.97", "0.95", "0.93"))
        self._choices(spot, "C", ("1.12", "1.20", "1.30"))
        ids = execute_opening(
            self.conn,
            self._settings(csp=False, cc=False, strangle=True),
            ACCOUNT,
            SYMBOL,
            tags={"NEWS_WASH", "RV_HIGH"},
        )
        self.assertEqual(len(ids), 2)
        reason = self.conn.execute(
            "SELECT reason FROM tickets WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchone()
        self.assertEqual(reason, ("signaled_strangle",))

    def test_an_account_does_not_screen_a_tactic_it_has_off(self) -> None:
        from athena_strategy.screener import candidates as real_candidates

        spot = self._bars(self._dip_closes())
        self._rv_history()
        self._choices(spot, "P", ("0.97", "0.95", "0.93"))
        self._choices(spot, "C", ("1.12", "1.20", "1.30"))
        purposes = []

        def _spy(*args, **kwargs):
            purposes.append(kwargs.get("purpose"))
            return real_candidates(*args, **kwargs)

        with patch("athena_strategy.wheel.candidates", side_effect=_spy):
            execute_opening(
                self.conn,
                self._settings(csp=True, cc=False, strangle=False, long=False, may_buy=False),
                ACCOUNT,
                SYMBOL,
            )
        self.assertNotIn("cc", purposes)
        self.assertNotIn("strangle_put", purposes)
        self.assertNotIn("strangle_call", purposes)

    def test_strangle_off_does_not_open_both_legs(self) -> None:
        spot = self._bars(self._dip_closes())
        self._rv_history()
        self._stock(100, "10")
        self._choices(spot, "P", ("0.97", "0.95", "0.93"))
        self._choices(spot, "C", ("1.12", "1.20", "1.30"))
        execute_opening(
            self.conn,
            self._settings(strangle=False),
            ACCOUNT,
            SYMBOL,
            tags={"NEWS_WASH", "RV_HIGH"},
        )
        reason = self.conn.execute(
            "SELECT reason FROM tickets WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchone()
        self.assertNotEqual(reason, ("signaled_strangle",))

    def test_may_buy_lets_an_income_name_buy_the_dip(self) -> None:
        spot = self._bars(self._dip_closes())
        self._choices(spot, "P", ("0.97", "0.95", "0.93"))
        self._rv_history()
        ids = execute_opening(
            self.conn, self._settings(intent="income", may_buy=True), ACCOUNT, SYMBOL,
        )
        self.assertTrue(ids)
        tactics = self.conn.execute(
            "SELECT tactic FROM drafts WHERE account_id = %s ORDER BY leg_index",
            (ACCOUNT,),
        ).fetchall()
        self.assertEqual(tactics[0][0], "BUY")

    def test_a_short_without_rv_history_is_not_a_draft(self) -> None:
        spot = self._bars(self._dip_closes())
        self._choices(spot, "P", ("0.97", "0.95", "0.93"))
        ids = execute_opening(self.conn, self._settings(intent="income"), ACCOUNT, SYMBOL)
        self.assertEqual(ids, [])
        trail = self.conn.execute(
            "SELECT outcome, summary FROM decision_trails WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchone()
        self.assertEqual(trail, ("no_data", "rv_samples"))

    def test_rv_percentile_39_drops_the_put_and_41_opens(self) -> None:
        spot = self._bars(self._dip_closes())
        self._choices(spot, "P", ("0.97", "0.95", "0.93"))
        self._rv_history()
        base = self._settings(intent="income")
        settings = base.model_copy(update={
            "risk": base.risk.model_copy(update={"min_rv_pctl_new_short": 40}),
        })

        def gated(rank: int):
            seen = {"n": 0}

            def gate(conn, book, symbol):
                seen["n"] += 1
                reading = read_indicators(conn, book, symbol)
                if seen["n"] > 1:
                    return replace(reading, rv_pctl=rank)
                return reading

            return gate

        with patch("athena_strategy.wheel.read_indicators", side_effect=gated(39)):
            refused = execute_opening(self.conn, settings, ACCOUNT, SYMBOL)
        self.assertEqual(refused, [])
        quiet = self.conn.execute(
            "SELECT outcome, summary FROM decision_trails WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchone()
        self.assertEqual(quiet, ("dropped", "rv_percentile"))
        self.conn.execute("DELETE FROM decision_trails WHERE account_id = %s", (ACCOUNT,))
        self.conn.commit()
        with patch("athena_strategy.wheel.read_indicators", side_effect=gated(41)):
            opened = execute_opening(self.conn, settings, ACCOUNT, SYMBOL)
        self.assertTrue(opened)

    def test_odd_lot_stays_quiet_when_covered_calls_are_off(self) -> None:
        self._bars(self._high_closes())
        self._stock(1, "10")
        ids = execute_opening(self.conn, self._settings(cc=False), ACCOUNT, SYMBOL)
        self.assertEqual(ids, [])

    def test_an_off_list_strike_is_overruled(self) -> None:
        spot = self._bars(self._dip_closes())
        self._choices(spot, "P", ("0.97", "0.95", "0.93"))
        self._rv_history()
        ids = execute_opening(
            self.conn, self._settings(intent="income"), ACCOUNT, SYMBOL, narrator_strike=Decimal("1"),
        )
        self.assertEqual(ids, [])
        trail = self.conn.execute(
            "SELECT outcome, summary FROM decision_trails WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchone()
        self.assertEqual(trail, ("dropped", "narrator_overruled"))

    def test_a_low_rv_floor_keeps_the_share_buy(self) -> None:
        spot = self._bars(self._dip_closes())
        self._choices(spot, "P", ("0.97", "0.95", "0.93"))
        self._rv_history()
        base = self._settings(long=True, intent="both")
        settings = base.model_copy(update={
            "risk": base.risk.model_copy(update={"min_rv_pctl_new_short": 40}),
        })
        seen = {"n": 0}

        def gate(conn, book, symbol):
            seen["n"] += 1
            reading = read_indicators(conn, book, symbol)
            if seen["n"] > 1:
                return replace(reading, rv_pctl=39)
            return reading

        with patch("athena_strategy.wheel.read_indicators", side_effect=gate):
            ids = execute_opening(self.conn, settings, ACCOUNT, SYMBOL)
        self.assertEqual(len(ids), 1)
        drafts = self.conn.execute(
            "SELECT tactic, status FROM drafts WHERE account_id = %s ORDER BY leg_index",
            (ACCOUNT,),
        ).fetchall()
        self.assertEqual(drafts, [("BUY", "pending")])
        ticket = self.conn.execute(
            "SELECT reason, state_before, state_after, legs FROM tickets WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchone()
        buy_qty = self.conn.execute(
            "SELECT contracts FROM drafts WHERE account_id = %s AND tactic = 'BUY'",
            (ACCOUNT,),
        ).fetchone()[0]
        self.assertEqual(ticket[0], "dip_long")
        self.assertEqual(ticket[1], "FLAT")
        self.assertEqual(ticket[2], "LONG" if buy_qty >= 100 else "ODD_LOT")
        self.assertEqual(ticket[3], 1)
        trails = self.conn.execute(
            "SELECT outcome, summary FROM decision_trails WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchall()
        self.assertEqual(set(trails), {("drafted", "dip_long"), ("dropped", "rv_percentile")})

    def test_missing_rv_samples_keep_the_share_buy(self) -> None:
        spot = self._bars(self._dip_closes())
        self._choices(spot, "P", ("0.97", "0.95", "0.93"))
        self._rv_history()
        seen = {"n": 0}

        def gate(conn, book, symbol):
            seen["n"] += 1
            reading = read_indicators(conn, book, symbol)
            if seen["n"] > 1:
                return replace(reading, rv_pctl=None)
            return reading

        with patch("athena_strategy.wheel.read_indicators", side_effect=gate):
            ids = execute_opening(self.conn, self._settings(long=True, intent="both"), ACCOUNT, SYMBOL)
        self.assertEqual(len(ids), 1)
        drafts = self.conn.execute(
            "SELECT tactic FROM drafts WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchall()
        self.assertEqual(drafts, [("BUY",)])
        trails = self.conn.execute(
            "SELECT outcome, summary FROM decision_trails WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchall()
        self.assertEqual(set(trails), {("drafted", "dip_long"), ("no_data", "rv_samples")})

    def test_an_off_menu_strike_keeps_the_share_buy(self) -> None:
        spot = self._bars(self._dip_closes())
        self._choices(spot, "P", ("0.97", "0.95", "0.93"))
        self._rv_history()
        ids = execute_opening(
            self.conn, self._settings(long=True, intent="both"), ACCOUNT, SYMBOL,
            narrator_strike=Decimal("1"),
        )
        self.assertEqual(len(ids), 1)
        drafts = self.conn.execute(
            "SELECT tactic, status FROM drafts WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchall()
        self.assertEqual(drafts, [("BUY", "pending")])
        trails = self.conn.execute(
            "SELECT outcome, summary FROM decision_trails WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchall()
        self.assertEqual(set(trails), {("drafted", "dip_long"), ("dropped", "narrator_overruled")})

    def test_a_ticket_that_breaks_the_cash_reserve_blocks_both_legs(self) -> None:
        spot = self._bars(self._dip_closes())
        self._choices(spot, "P", ("0.97", "0.95", "0.93"))
        self._rv_history()
        self.conn.execute(
            """
            INSERT INTO nav_marks (account_id, day, start_nav, last_nav)
            VALUES (%s, %s, 100000, 100000)
            """,
            (ACCOUNT, date.today()),
        )
        self.conn.execute(
            """
            INSERT INTO account_marks (account_id, day, cash, margin, base_currency, fx_rates)
            VALUES (%s, %s, 40000, 0, 'USD', '{}')
            """,
            (ACCOUNT, date.today()),
        )
        self.conn.commit()
        ids = execute_opening(self.conn, self._settings(long=True, intent="both"), ACCOUNT, SYMBOL)
        self.assertEqual(ids, [])
        rows = self.conn.execute(
            "SELECT status, reason, ticket_id FROM drafts WHERE account_id = %s ORDER BY leg_index",
            (ACCOUNT,),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row[0] == "blocked" for row in rows))
        self.assertIn(rows[0][2], rows[0][1])
        self.assertIn("cash_reserve_pct", rows[0][1])
        self.assertEqual(rows[0][1], rows[1][1])

    def test_one_full_pass_calls_three_agents(self) -> None:
        spot = self._bars(self._dip_closes())
        self._choices(spot, "P", ("0.97", "0.95", "0.93"))
        self._rv_history()
        replies = [
            '{"tags":[]}',
            '{"news_wash":false}',
            '{"open":true,"sentence":"dip","strike":null}',
        ]

        def _chat(_system, _user, _model, _provider, _token, _thinking):
            text = replies.pop(0)
            return {"text": text, "error": "", "latency_ms": 1, "tokens": 1, "request": {"user": _user}}

        with patch("athena_strategy.agents.chat", side_effect=_chat):
            run_signaled(self.conn, self._settings(long=True, intent="both"), ACCOUNT)
        rows = self.conn.execute(
            "SELECT agent FROM ai_calls WHERE account_id = %s ORDER BY id",
            (ACCOUNT,),
        ).fetchall()
        self.assertEqual([row[0] for row in rows], ["Location", "News", "Narrator"])

    def test_narrator_quiet_refuses_a_week_dip_draft(self) -> None:
        spot = self._bars(self._dip_closes())
        self._choices(spot, "P", ("0.97", "0.95", "0.93"))
        self._rv_history()
        replies = [
            '{"tags":[]}',
            '{"news_wash":false}',
            '{"open":false,"sentence":"52w high and weak trend","strike":null}',
        ]

        def _chat(_system, _user, _model, _provider, _token, _thinking):
            text = replies.pop(0)
            return {"text": text, "error": "", "latency_ms": 1, "tokens": 1, "request": {"user": _user}}

        with patch("athena_strategy.agents.chat", side_effect=_chat):
            ids = run_signaled(
                self.conn, self._settings(long=False, intent="income", csp=True), ACCOUNT,
            )
        self.assertEqual(ids, [])
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM drafts WHERE account_id = %s", (ACCOUNT,)).fetchone()
        )
        trail = self.conn.execute(
            "SELECT outcome, summary FROM decision_trails WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchone()
        self.assertEqual(trail, ("quiet", "narrator_quiet"))

    def test_narrator_open_allows_a_week_dip_csp(self) -> None:
        spot = self._bars(self._dip_closes())
        self._choices(spot, "P", ("0.97", "0.95", "0.93"))
        self._rv_history()
        replies = [
            '{"tags":[]}',
            '{"news_wash":false}',
            '{"open":true,"sentence":"week dip CSP","strike":null}',
        ]
        narrator_users: list[str] = []

        def _chat(_system, _user, _model, _provider, _token, _thinking):
            if "Ticket:" in _user:
                narrator_users.append(_user)
            text = replies.pop(0)
            return {"text": text, "error": "", "latency_ms": 1, "tokens": 1, "request": {"user": _user}}

        with patch("athena_strategy.agents.chat", side_effect=_chat):
            ids = run_signaled(
                self.conn, self._settings(long=False, intent="income", csp=True), ACCOUNT,
            )
        self.assertEqual(len(ids), 1)
        self.assertEqual(
            self.conn.execute("SELECT tactic, reason FROM drafts WHERE id = %s", (ids[0],)).fetchone(),
            ("OPEN_CSP", "week dip CSP"),
        )
        self.assertEqual(len(narrator_users), 1)
        self.assertIn("Dip window sessions:", narrator_users[0])
        self.assertIn("Cash reserve pct:", narrator_users[0])
        self.assertIn("Min RV pctl new short:", narrator_users[0])

    def test_sync_standing_agents_writes_narrator_marker(self) -> None:
        previous = self._settings_id()
        try:
            # ZZTEST is skipped by sync; use a temporary live-style id.
            live = "UTESTWDIP"
            payload = standard_payload().model_copy(
                update={"agents": {"CSP": default_agents()["CSP"]}},
            )
            self.conn.execute(
                "UPDATE settings_versions SET active = false WHERE account_id = %s",
                (live,),
            )
            next_id = self.conn.execute("SELECT coalesce(max(id), 0) + 1 FROM settings_versions").fetchone()[0]
            from psycopg.types.json import Jsonb

            self.conn.execute(
                """
                INSERT INTO settings_versions (id, author, note, payload, active, account_id)
                VALUES (%s, 'test', 'sync fixture', %s, true, %s)
                """,
                (next_id, Jsonb(payload.model_dump()), live),
            )
            self.conn.commit()
            written = sync_standing_agents(self.conn, account_ids=[live])
            self.assertEqual(len(written), 1)
            self.assertEqual(written[0][0], live)
            saved = load_active(self.conn, live)
            self.assertIn("WEEK_DIP", saved.agents["Narrator"].system)
            self.assertIn("WEEK_DIP", saved.agents["CSP"].system)
            self.assertIn('"open":true', saved.agents["Narrator"].system)
        finally:
            self.conn.execute("DELETE FROM settings_versions WHERE account_id = %s", ("UTESTWDIP",))
            self.conn.execute("DELETE FROM settings_audits WHERE account_id = %s", ("UTESTWDIP",))
            self.conn.commit()
            self._restore_settings(previous)

    def fixture_june_dip(self) -> Decimal:
        spot = self._bars(self._dip_closes())
        self._choices(spot, "P", ("0.97", "0.95", "0.93"))
        self._rv_history()
        return spot

    def fixture_intc_high(self) -> Decimal:
        spot = self._bars(self._high_closes())
        self._rv_history()
        self._stock(300, "95")
        self._short("P", "90")
        self._choices(spot, "C", ("1.03", "1.05", "1.08"))
        return spot

    def test_a_strangle_signal_with_100_shares_drafts_put_then_call(self) -> None:
        spot = self._bars(self._dip_closes())
        self._rv_history()
        self._stock(100, "10")
        self._choices(spot, "P", ("0.97", "0.95", "0.93"))
        self._choices(spot, "C", ("1.12", "1.20", "1.30"))
        ids = execute_opening(
            self.conn, self._settings(), ACCOUNT, SYMBOL, tags={"NEWS_WASH", "RV_HIGH"},
        )
        self.assertEqual(len(ids), 2)
        ticket = self.conn.execute(
            "SELECT reason, legs FROM tickets WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchone()
        self.assertEqual(ticket, ("signaled_strangle", 2))
        legs = self.conn.execute(
            "SELECT tactic, leg_index, call_strike FROM drafts WHERE account_id = %s ORDER BY leg_index",
            (ACCOUNT,),
        ).fetchall()
        self.assertEqual([row[0] for row in legs], ["OPEN_CSP", "OPEN_CC"])
        self.assertEqual([row[1] for row in legs], [0, 1])
        self.assertGreaterEqual(legs[1][2], Decimal("10"))

    def _pending_dip(self) -> Decimal:
        spot = self._bars(self._dip_closes())
        self._rv_history()
        self._stock(100, "10")
        self._choices(spot, "P", ("0.97", "0.95", "0.93"))
        self._choices(spot, "C", ("1.12", "1.20", "1.30"))
        self._open_ticket("ticket_dip", "draft_dip", SYMBOL, "OPEN_CSP", "dip_csp", "9")
        return spot

    def test_a_pending_dip_yields_to_a_strangle(self) -> None:
        self._pending_dip()
        ids = execute_opening(
            self.conn, self._settings(), ACCOUNT, SYMBOL, tags={"NEWS_WASH", "RV_HIGH"},
        )
        old = self.conn.execute(
            "SELECT status, reason FROM drafts WHERE id = 'draft_dip'"
        ).fetchone()
        fresh = self.conn.execute(
            """
            SELECT reason, status FROM tickets
            WHERE account_id = %s AND symbol = %s AND reason = 'signaled_strangle'
            """,
            (ACCOUNT, SYMBOL),
        ).fetchone()
        self.assertEqual(old, ("blocked", "replaced by signaled_strangle"))
        self.assertEqual(fresh, ("signaled_strangle", "pending"))
        self.assertEqual(len(ids), 2)

    def test_an_equal_reason_leaves_the_pending_dip(self) -> None:
        self._pending_dip()
        ids = execute_opening(self.conn, self._settings(cc=False, strangle=False), ACCOUNT, SYMBOL)
        old = self.conn.execute(
            "SELECT status, reason FROM drafts WHERE id = 'draft_dip'"
        ).fetchone()
        extra = self.conn.execute(
            """
            SELECT count(*) FROM tickets
            WHERE account_id = %s AND symbol = %s AND id <> 'ticket_dip'
              AND reason = ANY(%s)
            """,
            (ACCOUNT, SYMBOL, ["dip_long", "dip_csp", "high_cc", "cover_gap", "signaled_strangle"]),
        ).fetchone()
        self.assertEqual(ids, [])
        self.assertEqual(old, ("pending", "dip_csp"))
        self.assertEqual(extra[0], 0)

    def test_an_approved_dip_stays_for_the_day(self) -> None:
        self._pending_dip()
        self.conn.execute("UPDATE drafts SET status = 'approved' WHERE id = 'draft_dip'")
        self.conn.commit()
        ids = execute_opening(
            self.conn, self._settings(), ACCOUNT, SYMBOL, tags={"NEWS_WASH", "RV_HIGH"},
        )
        old = self.conn.execute(
            "SELECT status FROM drafts WHERE id = 'draft_dip'"
        ).fetchone()
        fresh = self.conn.execute(
            """
            SELECT count(*) FROM tickets
            WHERE account_id = %s AND symbol = %s AND reason = 'signaled_strangle'
            """,
            (ACCOUNT, SYMBOL),
        ).fetchone()
        self.assertEqual(ids, [])
        self.assertEqual(old[0], "approved")
        self.assertEqual(fresh[0], 0)

    def test_a_strangle_signal_under_100_shares_does_not_sell_both_legs(self) -> None:
        spot = self._bars(self._high_closes())
        self._rv_history()
        self._stock(99, "10")
        self._choices(spot, "P", ("0.97", "0.95", "0.93"))
        self._choices(spot, "C", ("1.12", "1.16", "1.20"))
        execute_opening(self.conn, self._settings(), ACCOUNT, SYMBOL, tags={"NEWS_WASH", "RV_HIGH"})
        reason = self.conn.execute(
            "SELECT reason FROM tickets WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchone()
        self.assertNotEqual(reason, ("signaled_strangle",))
        tactics = self.conn.execute(
            "SELECT tactic FROM drafts WHERE account_id = %s ORDER BY leg_index",
            (ACCOUNT,),
        ).fetchall()
        self.assertNotIn(("OPEN_CSP",), tactics)

    def test_a_dip_buy_follows_the_position_cap(self) -> None:
        self.fixture_june_dip()
        base = self._settings(long=True, intent="both")
        settings = base.model_copy(update={
            "risk": base.risk.model_copy(update={"max_position_per_symbol": 20000}),
        })
        ids = execute_opening(self.conn, settings, ACCOUNT, SYMBOL)
        self.assertTrue(ids)
        contracts = self.conn.execute(
            "SELECT contracts FROM drafts WHERE account_id = %s AND tactic = 'BUY'",
            (ACCOUNT,),
        ).fetchone()
        self.assertGreater(contracts[0], 1)

    def test_a_short_put_near_expiry_rolls(self) -> None:
        self._choices(Decimal("100"), "P", ("0.90",))
        self.conn.execute(
            """
            INSERT INTO positions (
                account_id, symbol, quantity, avg_price, sec_type, expiry,
                contract_right, strike, market_price, simulated
            )
            VALUES (%s, %s, -1, 2, 'OPT', %s, 'P', 90, 2, false)
            """,
            (ACCOUNT, SYMBOL, date.today() + timedelta(days=5)),
        )
        self.conn.commit()
        ids = manage_book(self.conn, self._settings(), ACCOUNT)
        self.assertEqual(ids, [])
        row = self.conn.execute(
            "SELECT tactic FROM drafts WHERE account_id = %s AND tactic = 'ROLL'",
            (ACCOUNT,),
        ).fetchone()
        self.assertIsNone(row)

    def test_a_pending_close_blocks_a_new_open(self) -> None:
        self._choices(Decimal("100"), "P", ("0.90",))
        self.conn.execute(
            """
            INSERT INTO positions (
                account_id, symbol, quantity, avg_price, sec_type, expiry,
                contract_right, strike, market_price, simulated
            )
            VALUES (%s, %s, -1, 2, 'OPT', %s, 'P', 90, 0.4, false)
            """,
            (ACCOUNT, SYMBOL, date.today() + timedelta(days=5)),
        )
        self.conn.commit()
        self.assertTrue(manage_book(self.conn, self._settings(), ACCOUNT))
        opened = execute_opening(self.conn, self._settings(long=True, intent="both"), ACCOUNT, SYMBOL)
        self.assertEqual(opened, [])
        tactics = self.conn.execute(
            "SELECT tactic FROM drafts WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchall()
        self.assertEqual(tactics, [("CLOSE",)])
        trail = self.conn.execute(
            "SELECT outcome, summary FROM decision_trails WHERE account_id = %s AND source = 'wheel'",
            (ACCOUNT,),
        ).fetchone()
        self.assertEqual(trail, ("quiet", "manage_pending"))

    def test_a_short_put_that_captured_premium_closes(self) -> None:
        self._choices(Decimal("100"), "P", ("0.90",))
        self.conn.execute(
            """
            INSERT INTO positions (
                account_id, symbol, quantity, avg_price, sec_type, expiry,
                contract_right, strike, market_price, simulated
            )
            VALUES (%s, %s, -1, 2, 'OPT', %s, 'P', 90, 0.4, false)
            """,
            (ACCOUNT, SYMBOL, date.today() + timedelta(days=40)),
        )
        self.conn.commit()
        ids = manage_book(self.conn, self._settings(), ACCOUNT)
        self.assertTrue(ids)
        row = self.conn.execute(
            "SELECT reason FROM tickets WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchone()
        self.assertEqual(row[0], "manage_close")
        tactic = self.conn.execute(
            "SELECT tactic FROM drafts WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchone()
        self.assertEqual(tactic[0], "CLOSE")

    def test_fixture_june_dip_buys_when_may_buy_is_on(self) -> None:
        self.fixture_june_dip()
        ids = execute_opening(
            self.conn, self._settings(intent="income", may_buy=True), ACCOUNT, SYMBOL,
        )
        self.assertEqual(len(ids), 2)
        legs = self.conn.execute(
            "SELECT tactic, ticket_id FROM drafts WHERE account_id = %s ORDER BY leg_index",
            (ACCOUNT,),
        ).fetchall()
        self.assertEqual([row[0] for row in legs], ["BUY", "OPEN_CSP"])
        self.assertEqual(legs[0][1], legs[1][1])

    def test_fixture_june_dip_income_does_not_buy(self) -> None:
        self.fixture_june_dip()
        execute_opening(self.conn, self._settings(intent="income"), ACCOUNT, SYMBOL)
        bought = self.conn.execute(
            "SELECT 1 FROM drafts WHERE account_id = %s AND tactic = 'BUY'",
            (ACCOUNT,),
        ).fetchone()
        self.assertIsNone(bought)

    def test_fixture_intc_high_sells_a_covered_call(self) -> None:
        self.fixture_intc_high()
        ids = execute_opening(self.conn, self._settings(), ACCOUNT, SYMBOL)
        self.assertEqual(len(ids), 1)
        row = self.conn.execute(
            "SELECT tactic, call_strike FROM drafts WHERE id = %s",
            (ids[0],),
        ).fetchone()
        self.assertEqual(row[0], "OPEN_CC")
        self.assertGreaterEqual(row[1], Decimal("95"))

    def test_dry_run_records_the_ticket_and_writes_no_draft(self) -> None:
        self.fixture_june_dip()
        with patch.dict(os.environ, {"ATHENA_DRY_RUN": "on"}):
            ids = execute_opening(
                self.conn, self._settings(long=True, intent="both"), ACCOUNT, SYMBOL,
            )
        self.assertEqual(ids, [])
        trail = self.conn.execute(
            "SELECT outcome, summary FROM decision_trails WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchone()
        self.assertEqual(trail[0], "observed")
        self.assertTrue(str(trail[1]).startswith("would ticket:"))
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM drafts WHERE account_id = %s", (ACCOUNT,)).fetchone()
        )

    def test_a_bad_reply_is_invalid_after_one_retry(self) -> None:
        self._bars(self._dip_closes())

        def _chat(_system, _user, _model, _provider, _token, _thinking):
            return {"text": "nope", "error": "", "latency_ms": 1, "tokens": 1, "request": {"user": _user}}

        with patch("athena_strategy.agents.chat", side_effect=_chat):
            run_signaled(self.conn, self._settings(), ACCOUNT)
        calls = self.conn.execute(
            "SELECT count(*) FROM ai_calls WHERE account_id = %s AND status = 'invalid'",
            (ACCOUNT,),
        ).fetchone()
        self.assertGreaterEqual(calls[0], 2)
        trail = self.conn.execute(
            "SELECT outcome FROM decision_trails WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchone()
        self.assertEqual(trail[0], "invalid")

    def test_observe_writes_two_agents_and_not_a_draft(self) -> None:
        self._bars(self._dip_closes())
        replies = ['{"tags":[]}', '{"news_wash":false}']

        def _chat(_system, _user, _model, _provider, _token, _thinking):
            text = replies.pop(0)
            return {"text": text, "error": "", "latency_ms": 1, "tokens": 1, "request": {"user": _user}}

        with patch("athena_strategy.agents.chat", side_effect=_chat):
            run_observe(self.conn, self._settings(), ACCOUNT)
        agents = self.conn.execute(
            "SELECT agent FROM ai_calls WHERE account_id = %s ORDER BY id",
            (ACCOUNT,),
        ).fetchall()
        self.assertEqual([row[0] for row in agents], ["Location", "News"])
        trail = self.conn.execute(
            "SELECT outcome, summary FROM decision_trails WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchone()
        self.assertEqual(trail, ("observed", "outside_draft_slot"))
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM drafts WHERE account_id = %s", (ACCOUNT,)).fetchone()
        )

    def test_switches_off_still_write_a_trail(self) -> None:
        flags = {
            "ATHENA_STATE_MACHINE": "off",
            "ATHENA_MAY_BUY": "off",
            "ATHENA_SLOT_SPLIT": "off",
            "ATHENA_DRY_RUN": "off",
            "ATHENA_SCREENER": "off",
            "ATHENA_INDICATORS": "off",
        }
        with patch.dict(os.environ, flags):
            self.assertTrue(state_machine_enabled())
            self.assertFalse(slot_split_enabled())
            self.assertFalse(dry_run_enabled())
            self.assertFalse(screener_enabled())
            self.assertFalse(indicators_enabled())
            self.assertEqual(
                buy_access(TacticFlags(long=True, intent="income", may_buy=True)),
                (False, "income"),
            )
            run_signaled(self.conn, self._settings(), ACCOUNT)
        trail = self.conn.execute(
            "SELECT outcome FROM decision_trails WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchone()
        self.assertEqual(trail[0], "no_data")

    def test_paper_profile_saves_the_ten_fields_on_zztest(self) -> None:
        from athena_orchestrator.pages import _post_risk
        from athena_settings.repository import load_active

        previous = self.conn.execute(
            "SELECT id FROM settings_versions WHERE account_id = %s AND active",
            (ACCOUNT,),
        ).fetchone()
        self.assertIsNotNone(previous)
        current = load_active(self.conn, ACCOUNT)
        strategy = current.strategy
        form = {
            "max_symbol": ["25"], "cash": ["20"], "halt": ["8"], "contracts": ["5"],
            "min_rv": ["40"], "min_dte": ["21"], "position": ["20000"], "max_orders": ["6"],
            "margin": ["35"], "blackout": ["7"],
            "roll_dte": [str(strategy.roll_dte)],
            "profit_take_pct": [str(strategy.profit_take_pct)],
            "ivr_high": [str(strategy.ivr_high)],
            "chain_band_pct": [str(strategy.chain_band_pct)],
            "otm_floor_pct": [str(strategy.otm_floor_pct)],
            "otm_ceiling_pct": [str(strategy.otm_ceiling_pct)],
            "screener_top_n": [str(strategy.screener_top_n)],
            "dte_min": [str(strategy.dte_min)],
            "dte_target": [str(strategy.dte_target)],
            "dte_max": [str(strategy.dte_max)],
            "csp_delta_lo": [str(strategy.csp_delta_lo)],
            "csp_delta_target": [str(strategy.csp_delta_target)],
            "csp_delta_hi": [str(strategy.csp_delta_hi)],
            "cc_delta_target": [str(strategy.cc_delta_target)],
            "cc_rail_pos": [str(strategy.cc_rail_pos)],
            "strangle_delta_target": [str(strategy.strangle_delta_target)],
            "iv_rv_multiple_hi": [str(strategy.iv_rv_multiple_hi)],
            "iv_rv_multiple_mid": [str(strategy.iv_rv_multiple_mid)],
            "iv_rv_multiple_lo": [str(strategy.iv_rv_multiple_lo)],
        }
        try:
            _post_risk(self.conn, ACCOUNT, form, "test")
            risk = load_active(self.conn, ACCOUNT).risk
            self.assertEqual(risk.profile, "paper")
            self.assertEqual(
                (
                    risk.max_symbol_pct_nav, risk.cash_reserve_pct, risk.daily_loss_halt_pct,
                    risk.max_contracts_per_symbol, risk.min_rv_pctl_new_short, risk.min_dte_new,
                    risk.max_position_per_symbol, risk.max_orders_per_day,
                    risk.max_short_opt_margin_pct, risk.earnings_blackout_days,
                ),
                (25, 20, 8, 5, 40, 21, 20000, 6, 35, 7),
            )
        finally:
            self.conn.rollback()
            self.conn.execute(
                "UPDATE settings_versions SET active = false WHERE account_id = %s",
                (ACCOUNT,),
            )
            self.conn.execute(
                "UPDATE settings_versions SET active = true WHERE id = %s",
                (previous[0],),
            )
            self.conn.commit()

    def _marks(self, cash: str, nav: str, margin: str = "0") -> None:
        self.conn.execute(
            """
            INSERT INTO account_marks (account_id, day, cash, margin, base_currency)
            VALUES (%s, CURRENT_DATE, %s, %s, 'USD')
            """,
            (ACCOUNT, Decimal(cash), Decimal(margin)),
        )
        self.conn.execute(
            """
            INSERT INTO nav_marks (account_id, day, start_nav, last_nav)
            VALUES (%s, CURRENT_DATE, %s, %s)
            """,
            (ACCOUNT, Decimal(nav), Decimal(nav)),
        )
        self.conn.commit()

    def _open_ticket(self, ticket: str, draft: str, symbol: str, tactic: str, reason: str, strike: str) -> None:
        self.conn.execute(
            """
            INSERT INTO tickets (
                id, account_id, symbol, state_before, state_after, legs, reason, status, simulated
            )
            VALUES (%s, %s, %s, 'FLAT', 'FLAT', 1, %s, 'open', false)
            """,
            (ticket, ACCOUNT, symbol, reason),
        )
        self.conn.execute(
            """
            INSERT INTO drafts (
                id, symbol, tactic, contracts, status, reason, phase, account_id,
                instrument, put_strike, multiplier, order_action, simulated, ticket_id, leg_index,
                expiry
            )
            VALUES (
                %s, %s, %s, 1, 'pending', %s, 'P1', %s,
                'OPT', %s, '1', 'SELL', false, %s, 0, %s
            )
            """,
            (draft, symbol, tactic, reason, ACCOUNT, Decimal(strike), ticket, date.today() + timedelta(days=30)),
        )
        self.conn.commit()

    def _statuses(self) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT id, status, reason FROM drafts WHERE account_id = %s",
            (ACCOUNT,),
        ).fetchall()
        return {row[0]: (row[1], row[2]) for row in rows}

    def _peer_bar(self, symbol: str) -> None:
        self.conn.execute(
            """
            INSERT INTO market_bars (symbol, ts, open, high, low, close, volume, source)
            VALUES (%s, %s, 10, 11, 9, 10, 1000, 'ib-daily')
            """,
            (symbol, datetime.now(timezone.utc)),
        )
        self.conn.commit()

    def _cash_settings(self):
        settings = self._settings()
        return settings.model_copy(update={
            "risk": settings.risk.model_copy(update={"max_bucket_pct_nav": 100}),
        })

    def test_the_first_put_keeps_the_cash_and_the_second_is_blocked(self) -> None:
        self._marks("100", "100")
        self._bars([Decimal("10")])
        self._peer_bar("ZZU")
        self._open_ticket("tkt_a", "draft_a", SYMBOL, "OPEN_CSP", "dip_csp", "40")
        self._open_ticket("tkt_b", "draft_b", "ZZU", "OPEN_CSP", "dip_csp", "40")
        apply_cycle_budget(self.conn, self._cash_settings(), ACCOUNT, ["draft_a", "draft_b"])
        found = self._statuses()
        self.assertEqual(found["draft_a"][0], "pending")
        self.assertEqual(found["draft_b"][0], "blocked")
        self.assertIn("cycle cash reserve", found["draft_b"][1])
        self.conn.execute("DELETE FROM drafts WHERE symbol = 'ZZU'")
        self.conn.execute("DELETE FROM tickets WHERE symbol = 'ZZU'")
        self.conn.execute("DELETE FROM market_bars WHERE symbol = 'ZZU'")
        self.conn.commit()

    def test_a_later_put_still_fits_after_an_earlier_one_fails(self) -> None:
        self._marks("100", "100")
        self._bars([Decimal("10")])
        self._peer_bar("ZZU")
        self._open_ticket("tkt_a", "draft_a", SYMBOL, "OPEN_CSP", "dip_csp", "80")
        self._open_ticket("tkt_b", "draft_b", "ZZU", "OPEN_CSP", "dip_csp", "20")
        apply_cycle_budget(self.conn, self._cash_settings(), ACCOUNT, ["draft_a", "draft_b"])
        found = self._statuses()
        self.assertEqual(found["draft_a"][0], "blocked")
        self.assertIn("cycle cash reserve", found["draft_a"][1])
        self.assertEqual(found["draft_b"][0], "pending", found["draft_b"][1])
        self.conn.execute("DELETE FROM drafts WHERE symbol = 'ZZU'")
        self.conn.execute("DELETE FROM tickets WHERE symbol = 'ZZU'")
        self.conn.execute("DELETE FROM market_bars WHERE symbol = 'ZZU'")
        self.conn.commit()

    def test_close_before_a_dip_is_not_the_same_as_the_dip_first(self) -> None:
        self._marks("50", "100")
        self._bars([Decimal("10")])
        self.conn.execute(
            """
            INSERT INTO market_bars (symbol, ts, open, high, low, close, volume, source)
            VALUES ('ZZU', %s, 10, 11, 9, 10, 1000, 'ib-daily')
            """,
            (datetime.now(timezone.utc),),
        )
        self.conn.commit()
        settings = self._settings()
        settings = settings.model_copy(update={
            "risk": settings.risk.model_copy(update={"max_bucket_pct_nav": 100}),
        })
        self._open_ticket("tkt_c", "draft_c", SYMBOL, "CLOSE", "manage_close", "40")
        self._open_ticket("tkt_d", "draft_d", "ZZU", "OPEN_CSP", "dip_long", "40")
        apply_cycle_budget(self.conn, settings, ACCOUNT, ["draft_c", "draft_d"])
        first = self._statuses()
        self.assertEqual(first["draft_c"][0], "pending")
        self.assertEqual(first["draft_d"][0], "pending")
        self.conn.execute("DELETE FROM drafts WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM tickets WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM decision_trails WHERE account_id = %s", (ACCOUNT,))
        self.conn.commit()
        self._open_ticket("tkt_c", "draft_c", SYMBOL, "CLOSE", "manage_close", "40")
        self._open_ticket("tkt_d", "draft_d", "ZZU", "OPEN_CSP", "dip_long", "40")

        def _swapped(reason: str) -> int:
            if reason == "dip_long":
                return 0
            if reason == "manage_close":
                return 1
            return 3

        with patch("athena_risk.cycle_rank", side_effect=_swapped):
            apply_cycle_budget(self.conn, settings, ACCOUNT, ["draft_c", "draft_d"])
        second = self._statuses()
        self.assertNotEqual(
            (first["draft_c"][0], first["draft_d"][0]),
            (second["draft_c"][0], second["draft_d"][0]),
        )
        self.assertEqual(second["draft_d"][0], "blocked")
        self.assertEqual(second["draft_c"][0], "pending")
        self.conn.execute("DELETE FROM drafts WHERE symbol = 'ZZU'")
        self.conn.execute("DELETE FROM tickets WHERE symbol = 'ZZU'")
        self.conn.execute("DELETE FROM market_bars WHERE symbol = 'ZZU'")
        self.conn.commit()

    def test_a_full_bucket_blocks_the_next_ticket_in_that_bucket(self) -> None:
        self._marks("1000", "1000")
        self._stock(1, "400")
        self.conn.execute(
            """
            INSERT INTO symbol_phases (account_id, symbol, phase, tags, tactic, classifiers)
            VALUES (%s, %s, 'P1', '', 'HOLD', 'AI_NARRATIVE')
            ON CONFLICT (account_id, symbol) DO UPDATE SET classifiers = 'AI_NARRATIVE'
            """,
            (ACCOUNT, SYMBOL),
        )
        self.conn.commit()
        self._open_ticket("tkt_e", "draft_e", SYMBOL, "OPEN_CSP", "dip_csp", "10")
        apply_cycle_budget(self.conn, self._settings(), ACCOUNT, ["draft_e"])
        status, reason = self._statuses()["draft_e"]
        self.assertEqual(status, "blocked")
        self.assertIn("AI_NARRATIVE", reason)

    def test_a_name_with_no_classifier_uses_one_bucket(self) -> None:
        self._marks("1000", "1000")
        self._stock(1, "400")
        self._open_ticket("tkt_u", "draft_u", SYMBOL, "OPEN_CSP", "dip_csp", "10")
        apply_cycle_budget(self.conn, self._settings(), ACCOUNT, ["draft_u"])
        status, reason = self._statuses()["draft_u"]
        self.assertEqual(status, "blocked")
        self.assertIn("UNCLASSIFIED", reason)
        self.conn.execute("DELETE FROM drafts WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM tickets WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM positions WHERE account_id = %s", (ACCOUNT,))
        self.conn.commit()
        self._bars([Decimal("10")])
        self._open_ticket("tkt_v", "draft_v", SYMBOL, "OPEN_CSP", "dip_csp", "10")
        apply_cycle_budget(self.conn, self._settings(), ACCOUNT, ["draft_v"])
        status, reason = self._statuses()["draft_v"]
        self.assertEqual(status, "pending", reason)

    def test_assignment_that_spends_the_cash_floor_blocks_the_put(self) -> None:
        self._marks("100", "100")
        self._bars([Decimal(100 + (index % 5) - 2) for index in range(25)])
        self._open_ticket("tkt_f", "draft_f", SYMBOL, "OPEN_CSP", "dip_csp", "100")
        settings = self._settings()
        settings = settings.model_copy(update={
            "risk": settings.risk.model_copy(update={
                "cash_reserve_pct": 0,
                "max_symbol_pct_nav": 200,
                "max_short_opt_margin_pct": 100,
                "max_bucket_pct_nav": 100,
            }),
        })
        apply_cycle_budget(self.conn, settings, ACCOUNT, ["draft_f"])
        status, reason = self._statuses()["draft_f"]
        self.assertEqual(status, "blocked")
        self.assertIn("assignment would breach min cash", reason)

    def test_a_put_counts_its_strike_once_against_the_cash_floor(self) -> None:
        self._marks("100", "100")
        self._bars([Decimal(100 + (index % 5) - 2) for index in range(25)])
        self._open_ticket("tkt_g", "draft_g", SYMBOL, "OPEN_CSP", "dip_csp", "46")
        settings = self._settings()
        settings = settings.model_copy(update={
            "risk": settings.risk.model_copy(update={
                "cash_reserve_pct": 0,
                "max_symbol_pct_nav": 500,
                "max_short_opt_margin_pct": 100,
                "max_assign_pct_nav": 100,
                "max_bucket_pct_nav": 100,
            }),
        })
        apply_cycle_budget(self.conn, settings, ACCOUNT, ["draft_g"])
        status, reason = self._statuses()["draft_g"]
        self.assertEqual(status, "pending", reason)
        self.conn.execute("DELETE FROM drafts WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("DELETE FROM tickets WHERE account_id = %s", (ACCOUNT,))
        self.conn.commit()
        self._open_ticket("tkt_g", "draft_g", SYMBOL, "OPEN_CSP", "dip_csp", "25")
        self._open_ticket("tkt_h", "draft_h", SYMBOL, "OPEN_CSP", "dip_csp", "25")
        apply_cycle_budget(self.conn, settings, ACCOUNT, ["draft_g", "draft_h"])
        found = self._statuses()
        self.assertEqual(found["draft_g"][0], "pending", found["draft_g"][1])
        self.assertEqual(found["draft_h"][0], "pending", found["draft_h"][1])

    def test_book_page_labels_the_axes(self) -> None:
        page = _book_page(self.conn, ACCOUNT, {})
        body = page["body"]
        self.assertIn(">RV20</text>", body)
        self.assertIn(">premium yield</text>", body)
        self.assertNotIn("market_unknown", body)
        self.assertNotIn("tangency", body)
        self.assertNotIn("not estimated", body)
        self.assertIn("<h2>Positions</h2>", body)

    def test_position_heatmap_groups_by_intent_and_colors_pnl(self) -> None:
        laid = _squarify([(1, "a"), (1, "b")], 0, 0, 100, 100)
        self.assertEqual(len(laid), 2)
        self.assertLess(_rect_overlap(laid[0][:4], laid[1][:4]), 0.01)
        flat = _channels(_heat_color(0)[0])
        gain = _channels(_heat_color(10)[0])
        loss = _channels(_heat_color(-10)[0])
        self.assertEqual(_heat_color(0)[0], "#3a4350")
        self.assertEqual(_heat_color(None)[0], "#3a4350")
        self.assertGreater(gain[1], flat[1])
        self.assertGreater(loss[0], flat[0])
        body = _position_heatmap(
            [
                _heat_row("AAA", pnl=0, avg=10, qty=10, market=100),
                _heat_row("BBB", pnl=10, avg=10, qty=10, market=110),
                _heat_row("CCC", pnl=-10, avg=10, qty=10, market=90),
            ],
            {"AAA": "income", "BBB": "both", "CCC": "accumulate"},
        )
        self.assertIn(">Income</text>", body)
        self.assertIn(">Both</text>", body)
        self.assertIn(">Accumulate</text>", body)
        self.assertNotIn('preserveAspectRatio="none"', body)
        self.assertIn('id="book-heat-data"', body)
        self.assertIn('data-name="AAA"', body)
        self.assertIn(f'fill="{_heat_color(0)[0]}"', body)
        self.assertIn(f'fill="{_heat_color(10)[0]}"', body)
        self.assertIn(f'fill="{_heat_color(-10)[0]}"', body)

    def test_orders_off_the_paper_ten_are_custom(self) -> None:
        previous = self._settings_id()
        form = self._risk_form(load_active(self.conn, ACCOUNT).strategy)
        form["max_orders"] = ["50"]
        try:
            _post_risk(self.conn, ACCOUNT, form, "test")
            saved = load_active(self.conn, ACCOUNT)
            self.assertEqual(saved.risk.profile, "custom")
            self.assertEqual(saved.risk.max_orders_per_day, 50)
            self.assertEqual(saved.privilege, "all_orders")
        finally:
            self._restore_settings(previous)

    def test_sample_floor_survives_a_profile_save(self) -> None:
        from inspect import getsource

        previous = self._settings_id()
        current = load_active(self.conn, ACCOUNT)
        try:
            form = self._risk_form(current.strategy, risk=current.risk)
            form["ivr_min_samples"] = ["30"]
            form["max_orders"] = ["49" if current.risk.max_orders_per_day != 49 else "48"]
            _post_risk(self.conn, ACCOUNT, form, "test")
            self.assertEqual(load_active(self.conn, ACCOUNT).strategy.ivr_min_samples, 30)
            paper = self._risk_form(load_active(self.conn, ACCOUNT).strategy)
            paper.pop("ivr_min_samples", None)
            _post_risk(self.conn, ACCOUNT, paper, "test")
            self.assertEqual(load_active(self.conn, ACCOUNT).strategy.ivr_min_samples, 30)
            source = getsource(_post_risk)
            self.assertNotIn("ivr_min_samples = 1", source)
            self.assertNotIn("ivr_min_samples = 20", source)
        finally:
            self._restore_settings(previous)

    def test_a_risk_and_strategy_change_are_both_named(self) -> None:
        previous = self._settings_id()
        current = load_active(self.conn, ACCOUNT)
        high = 70 if current.strategy.rv_high_pctl != 70 else 75
        form = self._risk_form(current.strategy)
        form["cash"] = ["19" if current.risk.cash_reserve_pct != 19 else "18"]
        form["max_orders"] = ["50"]
        form["rv_high_pctl"] = [str(high)]
        try:
            _post_risk(self.conn, ACCOUNT, form, "test")
            saved = load_active(self.conn, ACCOUNT)
            self.assertEqual(saved.privilege, "all_orders")
            self.assertEqual(saved.strategy.rv_high_pctl, high)
            note = self.conn.execute(
                "SELECT note FROM settings_versions WHERE account_id = %s AND active",
                (ACCOUNT,),
            ).fetchone()[0]
            self.assertIn("risk", note)
            self.assertIn("strategy rv_high_pctl", note)
        finally:
            self._restore_settings(previous)

    def test_the_reading_card_holds_the_volatility_family(self) -> None:
        body = _risk(self.conn, ACCOUNT, {})["body"]
        card = body.split('id="volatility-reading"', 1)[1].split("<h2>Limits</h2>", 1)[0]
        for name in (
            "min_rv", "ivr_min_samples", "rv_window", "rv_high_pctl", "rv_low_pctl",
            "chain_band_pct", "strangle_delta_target",
            "iv_rv_multiple_hi", "iv_rv_multiple_mid", "iv_rv_multiple_lo",
        ):
            self.assertIn(f'name="{name}"', card)
        self.assertNotIn('name="otm_floor_pct"', card)
        limits = body.split("<h2>Limits</h2>", 1)[1].split("<h2>Approval privilege", 1)[0]
        for name in (
            "otm_floor_pct", "otm_ceiling_pct", "rv_band_high_pct", "rv_band_mid_pct", "wing_z",
            "max_spread_pct",
        ):
            self.assertIn(f'name="{name}"', limits)
        self.assertIn("Zero removes the cap so the width follows realized vol.", limits)
        self.assertIn("Zero keeps every quoted strike.", limits)
        self.assertIn('class="tag blue">premium_ratio</span>', card)
        self.assertIn('class="tag">chain_bs</span>', card)
        self.assertIn("The mark written today. This page cannot change it.", card)
        self.assertIn("Waits for measured chain marks. It cannot be selected.", card)
        self.assertNotIn("Realized vol is the gate", body)
        self.assertNotIn('class="slot-note"', card)
        self.assertNotIn('value="premium_ratio"', card)
        self.assertIn("follows the current profile", card)
        self.assertNotIn('name="chain_bs"', card)
        self.assertNotIn("min_iv_rank_new_short", body)
        self.assertNotIn("quiet sessions", body)
        self.assertNotIn('id="vol-evidence"', body)
        vol = _volatility_page(self.conn, ACCOUNT, {})
        self.assertEqual(vol["title"], "Volatility")
        self.assertIn("A full red sanity column is the known state", vol["body"])
        self.assertIn('id="vol-evidence"', vol["body"])
        page = _strategy(self.conn, ACCOUNT, {})
        self.assertEqual(page["title"], "Orchestration")
        self.assertIn('action="/orchestration"', page["body"])
        self.assertIn("orchestration blueprint", page["lead"])
        self.assertNotIn('name="min_rv"', body.split('id="volatility-reading"', 1)[0])
        strategy = body.split('id="strategy-card"', 1)[1].split("Volatility reading", 1)[0]
        self.assertNotIn('name="chain_band_pct"', strategy)
        self.assertIn('class="form-grid three"', strategy)
        self.assertLess(body.index('id="strategy-card"'), body.index('id="volatility-reading"'))
        self.assertLess(body.index('id="volatility-reading"'), body.index("<h2>Limits</h2>"))
        self.assertLess(body.index("<h2>Limits</h2>"), body.index("Approval privilege"))
        self.assertIn('class="card risk-span-5" id="strategy-card"', body)
        self.assertIn('class="card risk-span-2" id="volatility-reading"', body)
        self.assertIn('class="card risk-span-2"><h2>Limits</h2>', body)
        self.assertIn('class="risk-stack risk-span-1"', body)
        self.assertIn('<h2>Approval privilege', body)
        limits = body.split("<h2>Limits</h2>", 1)[1].split("Approval privilege", 1)[0]
        self.assertNotIn('name="strict_conflict"', limits)
        right = body.split('class="risk-stack risk-span-1"', 1)[1]
        self.assertLess(right.index("Approval privilege"), right.index("Strict conflict"))
        self.assertLess(right.index("Strict conflict"), right.index("Reset to Default"))
        self.assertIn('name="strict_conflict"', right)
        self.assertIn("data-reset-grade", right)
        self.assertIn("It does not tag a symbol.", strategy)
        self.assertIn('class="strategy-scroll"', body)
        self.assertIn('id="strategy-card"', body)
        self.assertNotIn('id="strategy-card" class="card span-2"', body)
        self.assertNotIn('class="card span-2" id="strategy-card"', body)
        self.assertNotIn('<div class="name">test</div>', body)
        self.assertEqual(body.count('<div class="name">'), 5)
        self.assertIn("Used only by Auto Set Price", page["body"])
        self.assertIn('value="Trade"', page["body"])
        row = _pool_row(self.conn, ACCOUNT, load_active(self.conn, ACCOUNT), SYMBOL)
        self.assertLess(row.index(">Edit</button>"), row.index(">Trade</button>"))
        self.assertIn('data-cmd="trade-symbol"', row)

    def _settings_id(self) -> int:
        row = self.conn.execute(
            "SELECT id FROM settings_versions WHERE account_id = %s AND active",
            (ACCOUNT,),
        ).fetchone()
        self.assertIsNotNone(row)
        return row[0]

    def _restore_settings(self, previous: int) -> None:
        self.conn.rollback()
        self.conn.execute("UPDATE settings_versions SET active = false WHERE account_id = %s", (ACCOUNT,))
        self.conn.execute("UPDATE settings_versions SET active = true WHERE id = %s", (previous,))
        self.conn.commit()

    def _risk_form(self, strategy, risk=None) -> dict:
        form = {
            "max_symbol": ["25"], "cash": ["20"], "halt": ["8"], "contracts": ["5"],
            "min_rv": ["40"], "min_dte": ["21"], "position": ["20000"], "max_orders": ["6"],
            "margin": ["35"], "blackout": ["7"],
            "roll_dte": [str(strategy.roll_dte)],
            "profit_take_pct": [str(strategy.profit_take_pct)],
            "ivr_high": [str(strategy.ivr_high)],
            "chain_band_pct": [str(strategy.chain_band_pct)],
            "otm_floor_pct": [str(strategy.otm_floor_pct)],
            "otm_ceiling_pct": [str(strategy.otm_ceiling_pct)],
            "screener_top_n": [str(strategy.screener_top_n)],
            "dte_min": [str(strategy.dte_min)],
            "dte_target": [str(strategy.dte_target)],
            "dte_max": [str(strategy.dte_max)],
            "csp_delta_lo": [str(strategy.csp_delta_lo)],
            "csp_delta_target": [str(strategy.csp_delta_target)],
            "csp_delta_hi": [str(strategy.csp_delta_hi)],
            "cc_delta_target": [str(strategy.cc_delta_target)],
            "cc_rail_pos": [str(strategy.cc_rail_pos)],
            "strangle_delta_target": [str(strategy.strangle_delta_target)],
            "iv_rv_multiple_hi": [str(strategy.iv_rv_multiple_hi)],
            "iv_rv_multiple_mid": [str(strategy.iv_rv_multiple_mid)],
            "iv_rv_multiple_lo": [str(strategy.iv_rv_multiple_lo)],
        }
        if risk is not None:
            form.update({
                "max_symbol": [str(risk.max_symbol_pct_nav)],
                "cash": [str(risk.cash_reserve_pct)],
                "halt": [str(risk.daily_loss_halt_pct)],
                "contracts": [str(risk.max_contracts_per_symbol)],
                "min_rv": [str(risk.min_rv_pctl_new_short)],
                "min_dte": [str(risk.min_dte_new)],
                "position": [str(risk.max_position_per_symbol)],
                "max_orders": [str(risk.max_orders_per_day)],
                "margin": [str(risk.max_short_opt_margin_pct)],
                "blackout": [str(risk.earnings_blackout_days)],
            })
        return form


class RiskReadingTest(unittest.TestCase):
    def test_profile_keys_are_the_ten_exposure_fields(self) -> None:
        from athena_settings.model import paper_payload

        self.assertEqual(len(PROFILE_LIMITS), 10)
        self.assertNotIn("max_bucket_pct_nav", PROFILE_LIMITS)
        self.assertNotIn("assign_delta", PROFILE_LIMITS)
        self.assertNotIn("test", PROFILES)
        self.assertEqual(list(PROFILES), ["defensive", "standard", "harvest", "aggressive", "paper"])
        for copy in PROFILE_COPY.values():
            self.assertLessEqual(len(copy.split()), 7)
        paper = paper_payload().risk
        self.assertEqual(PROFILES["paper"], tuple(getattr(paper, name) for name in PROFILE_LIMITS))
        script = Path("apps/orchestrator/src/athena_orchestrator/console.py").read_text(encoding="utf-8")
        self.assertIn(", ".join(f'"{name}"' for name in FORM_LIMITS), script)

    def test_a_price_inverse_stays_inverted_after_a_scale(self) -> None:
        pairs = [(Decimal(1) / Decimal(price), Decimal(price)) for price in (10, 11, 12, 13, 14, 15)]
        self.assertEqual(sanity_label(pairs), "INVERTED?")
        self.assertEqual(sanity_label([(proxy * 3, price) for proxy, price in pairs]), "INVERTED?")
        self.assertEqual(sanity_label(pairs[:4]), "pending")

    def test_an_unrelated_series_is_ok(self) -> None:
        prices = [Decimal(price) for price in (10, 12, 11, 15, 13, 18, 14, 20)]
        proxies = [Decimal(proxy) for proxy in (2, 9, 3, 8, 4, 1, 7, 5)]
        self.assertEqual(sanity_label(list(zip(proxies, prices))), "ok")

    def test_band_gap_is_listed_minus_target(self) -> None:
        listed = listed_otm_pct(Decimal(100), [("P", Decimal(90)), ("C", Decimal(110)), ("C", Decimal(120))])
        self.assertEqual(listed, Decimal(10))
        self.assertEqual(band_gap(listed, Decimal("4.62")), Decimal("5.38"))
        self.assertIsNone(listed_otm_pct(Decimal(100), []))
        self.assertIsNone(band_gap(None, Decimal(5)))

    def test_preview_counts_match_a_second_compute(self) -> None:
        from athena_marketdata.pricing import Bar
        from athena_settings.model import StrategySettings
        from athena_strategy.indicators import compute

        start = date(2026, 1, 1)
        bars = []
        for index in range(40):
            close = Decimal(100 + (index % 7))
            bars.append(Bar(start + timedelta(days=index), close, close + 1, close - 1, close, Decimal(1000)))
        samples = [Decimal(10 + index) for index in range(25)]
        strategy = StrategySettings()
        before, after = reading_shift(bars, samples, strategy, "rv_high_pctl", 101)
        again = vol_tag(compute(bars, strategy.model_copy(update={"rv_high_pctl": 101}), rv_samples=samples).tags)
        self.assertEqual(after, again)
        text = threshold_preview("rv_high_pctl", strategy.rv_high_pctl, 101, [before], [after])
        self.assertIn(f"RV_HIGH: {int(before == 'RV_HIGH')} → {int(after == 'RV_HIGH')}", text)
        self.assertIn("rv_high_pctl: 80 → 101", text)

    def test_method_switch_names_the_quiet_period(self) -> None:
        text = method_switch_note(7)
        self.assertIn("quiet sessions: 20", text)
        self.assertIn("sanity: expected ok", text)
        self.assertIn("premium_ratio → chain_bs", text)
