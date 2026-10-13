"""Pure rules. No database and no IB."""

import os
import unittest
from pathlib import Path
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from athena_exec.accounts import active_accounts, stamp_legacy_accounts, trading_mode
from athena_exec.gate import live_block_reason
from athena_exec.options import choose_expiry, choose_strikes, expiry_band, strike_shortlist
from athena_exec.slots import cash_open, draft_allowed, observe_allowed, slot_state
from athena_marketdata.pricing import Bar
from athena_orchestrator.money import DASH, cash_rows, convert, ledger, marked, report_currency, show
from athena_orchestrator.pages import PROFILES, _audit_result_tag, _audit_status_tag, _audit_tr, _position_nlv_stat, sim_targets
from athena_orchestrator.runtime import begin, cycle_view, finish, mark, note_cycle, note_loop, wait
from athena_orchestrator.sim import _add_months, duration_for
from athena_risk import CycleBook, allowed_open, block_reason, block_ticket_reason, book_amount
from athena_settings.model import AISettings, AgentPrompt, IBSlots, RiskLimits, SettingsPayload, TacticFlags, standard_payload
from athena_settings.prompts import default_agents, range_names, resolved_agents
from athena_settings.repository import SettingsVersion, format_settings, save_settings, set_knob
from athena_settings.resolve import (
    allowlist_ids,
    bot_username,
    control_flag,
    deepseek_token,
    itick_key,
    live_allowlist,
    live_settings,
    pick,
    scoring_model,
)
from athena_strategy.dsh import request_body
from athena_strategy.schemas import parse_response
from athena_strategy.screener import _listed
from athena_strategy.agents import (
    _bars,
    _context,
    _drafts,
    _headlines,
    _label,
    _last_close,
    _number,
    _plan,
    _position_cap,
    _prior_rail,
    _pullback,
    _proposal_block,
    _proposal_text,
    _propose_one,
    _rail,
    _result_line,
    _run_symbol,
    _shares,
    _show,
    _staged_step,
    _tag_set,
    _trail_context,
    _trail_outcome,
    fill_prompt,
    parse_json,
    run_legacy_signaled,
)
from athena_strategy.classifiers import clean_classifiers
from athena_strategy.indicators import compute, realized_vol
from athena_strategy.iv import estimated_iv, iv_rank
from athena_strategy.manage import management_tactic
from athena_strategy.legacy_tactics import choose_tactic
from athena_strategy.phases import choose_phase, evaluate_symbol
from athena_strategy.pool import clean_symbol, lookup_company, parse_chart, parse_finviz
from athena_strategy.price import price_tags, rsi
from athena_strategy.tags import DIP_BOUGHT, LOWER_RAIL, NEWS_WASH, PRICE_WEAK, PULLBACK, TAG_REGISTRY, VOLUME_SPIKE


def _legacy(prefix: str) -> str:
    return next(
        spec.name
        for spec in TAG_REGISTRY.values()
        if spec.status == "legacy_price_proxy" and spec.name.startswith(prefix)
    )
HKT = ZoneInfo("Asia/Hong_Kong")
TODAY = date(2026, 10, 10)


def _limits(**updates) -> RiskLimits:
    base = dict(
        profile="test",
        max_symbol_pct_nav=20,
        cash_reserve_pct=30,
        daily_loss_halt_pct=5,
        max_contracts_per_symbol=12,
        max_position_per_symbol=0,
        min_rv_pctl_new_short=50,
        min_dte_new=7,
        max_short_opt_margin_pct=50,
        earnings_blackout_days=7,
        max_orders_per_day=10,
    )
    base.update(updates)
    return RiskLimits(**base)


def _open(**kwargs):
    return dict(
        slot="athena",
        draft_status="approved",
        halted=False,
        account_id="PAPER",
        instrument="STK",
        limit_price=Decimal("10"),
    ) | kwargs


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return None if not self.rows else self.rows[0]

    def fetchall(self):
        return list(self.rows)


class _Conn:
    def __init__(self, rows=None):
        self.rows = [] if rows is None else rows
        self.calls = []
        self.committed = False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return _Cursor(self.rows)

    def commit(self):
        self.committed = True


class GateTest(unittest.TestCase):
    def test_approved_stock_and_option_open_the_gate(self) -> None:
        self.assertIsNone(live_block_reason(**_open()))
        self.assertIsNone(live_block_reason(**_open(instrument="OPT", limit_price=Decimal("1.25"))))

    def test_each_block_names_its_reason(self) -> None:
        self.assertEqual(live_block_reason(**_open(draft_status="pending")), "draft is not approved")
        self.assertEqual(live_block_reason(**_open(halted=True)), "trading is halted")
        self.assertEqual(live_block_reason(**_open(slot="idle")), "outside athena slot")
        self.assertEqual(live_block_reason(**_open(account_id="")), "account id is empty")
        self.assertEqual(live_block_reason(**_open(instrument="BAG")), "draft has no stock limit contract")
        self.assertEqual(live_block_reason(**_open(limit_price=Decimal("0"))), "draft has no limit price")
        self.assertEqual(live_block_reason(**_open(limit_price=None)), "draft has no limit price")


class RiskTest(unittest.TestCase):
    def test_money_cap_off_uses_the_contract_cap(self) -> None:
        self.assertEqual(allowed_open(_limits(), Decimal("100"), Decimal("100")), 12)

    def test_one_contract_above_the_slack_uses_the_contract_cap(self) -> None:
        limits = _limits(max_position_per_symbol=1000)
        self.assertEqual(allowed_open(limits, Decimal("20"), Decimal("100")), 12)

    def test_no_room_returns_zero(self) -> None:
        limits = _limits(max_position_per_symbol=1000)
        self.assertEqual(allowed_open(limits, Decimal("8"), Decimal("100"), existing=Decimal("1100")), 0)

    def test_room_counts_whole_contracts(self) -> None:
        limits = _limits(max_position_per_symbol=1000, max_contracts_per_symbol=12)
        self.assertEqual(allowed_open(limits, Decimal("2"), Decimal("100")), 5)

    def test_block_reason_names_each_cap(self) -> None:
        limits = _limits(max_position_per_symbol=1000)
        self.assertEqual(block_reason(0, limits), "contracts must be at least 1")
        self.assertEqual(block_reason(13, limits), "contracts exceed max_contracts_per_symbol")
        self.assertEqual(block_reason(9, limits, orders_today=10), "orders exceed max_orders_per_day")
        self.assertEqual(
            block_reason(1, limits, nav=Decimal("100"), symbol_value=Decimal("30")),
            "symbol exceeds max_symbol_pct_nav",
        )
        self.assertEqual(
            block_reason(1, limits, nav=Decimal("100"), cash=Decimal("10"), needs_cash=True),
            "cash is below cash_reserve_pct",
        )
        self.assertEqual(
            block_reason(1, limits, nav=Decimal("100"), margin=Decimal("60")),
            "margin exceeds max_short_opt_margin_pct",
        )
        self.assertEqual(block_reason(1, limits, rv_pctl=10), "rv percentile is below min_rv_pctl_new_short")
        self.assertEqual(block_reason(1, limits, dte=3), "dte is below min_dte_new")
        self.assertEqual(block_reason(1, limits, earnings_in_days=2), "earnings blackout")
        self.assertIsNone(block_reason(1, limits, rv_pctl=None, dte=None, nav=None))
        self.assertIsNone(block_reason(1, limits, opening=False, rv_pctl=1, dte=1))

    def test_ticket_margin_records_the_estimate(self) -> None:
        leg = {
            "tactic": "OPEN_CSP",
            "contracts": 1,
            "limit": Decimal("1"),
            "put_strike": Decimal("100"),
            "call_strike": None,
            "multiplier": Decimal("100"),
            "orders": 1,
        }
        marks = {
            "nav": Decimal("10000"),
            "cash": Decimal("10000"),
            "margin": Decimal("0"),
            "base": "USD",
            "rates": {},
            "orders_today": 0,
            "price": Decimal("100"),
            "currency": "USD",
        }
        reason = block_ticket_reason(
            [leg],
            _limits(max_short_opt_margin_pct=10, margin_rate=20, cash_reserve_pct=0, max_symbol_pct_nav=100),
            marks,
            Decimal(0),
        )
        self.assertIn("I2: ticket breaches max_short_opt_margin_pct", reason)
        self.assertIn("estimate 2000.00", reason)
        self.assertIn("cap 1000.00", reason)
        wide = _limits(max_orders_per_day=10, cash_reserve_pct=0, max_symbol_pct_nav=100, max_short_opt_margin_pct=100)
        strangle = {
            "tactic": "OPEN_STRANGLE",
            "contracts": 1,
            "limit": Decimal("1"),
            "put_strike": Decimal("1"),
            "call_strike": Decimal("1"),
            "multiplier": Decimal("100"),
            "orders": 2,
        }
        booked = dict(marks, nav=Decimal("1000000"), cash=Decimal("1000000"), orders_today=9, price=Decimal("1"))
        self.assertEqual(
            block_ticket_reason([strangle], wide, booked, Decimal(0)),
            "I2: ticket breaches max_orders_per_day",
        )

    def test_a_missing_rate_or_mark_blocks_the_ticket(self) -> None:
        limits = _limits(
            cash_reserve_pct=0,
            max_symbol_pct_nav=100,
            max_short_opt_margin_pct=100,
            max_bucket_pct_nav=100,
            max_assign_pct_nav=100,
        )
        leg = {
            "tactic": "OPEN_CSP",
            "contracts": 1,
            "limit": Decimal("1"),
            "put_strike": Decimal("1"),
            "call_strike": None,
            "multiplier": Decimal("1"),
            "orders": 1,
            "price": Decimal("10"),
            "delta": None,
        }
        book = CycleBook(
            nav=Decimal("100000"),
            cash=Decimal("100000"),
            margin=Decimal("0"),
            orders=0,
            base="HKD",
            currency="HKD",
        )
        self.assertEqual(
            block_ticket_reason([leg], limits, {}, book=book, symbol="ZZT", currency="USD"),
            "missing fx rate USD",
        )
        blank, reason = book_amount(Decimal("30"), "USD", "HKD", {})
        self.assertIsNone(blank)
        self.assertEqual(reason, "missing fx rate USD")
        converted, reason = book_amount(Decimal("30"), "USD", "HKD", {"USD": Decimal("7.8")})
        self.assertEqual(converted, Decimal("234"))
        self.assertIsNone(reason)
        priced = CycleBook(
            nav=Decimal("1000"),
            cash=Decimal("1000"),
            margin=Decimal("0"),
            orders=0,
            base="HKD",
            currency="HKD",
            rates={"USD": Decimal("7.8")},
        )
        cap = _limits(
            cash_reserve_pct=0,
            max_symbol_pct_nav=20,
            max_short_opt_margin_pct=100,
            max_bucket_pct_nav=100,
            max_assign_pct_nav=100,
        )
        sized = {**leg, "price": Decimal("30")}
        self.assertEqual(
            block_ticket_reason([sized], cap, {}, book=priced, symbol="ZZT", currency="USD"),
            "cycle symbol cap",
        )
        same = CycleBook(
            nav=Decimal("1000"),
            cash=Decimal("1000"),
            margin=Decimal("0"),
            orders=0,
            base="USD",
            currency="USD",
        )
        self.assertIsNone(
            block_ticket_reason([sized], cap, {}, book=same, symbol="ZZT", currency="USD"),
        )
        unmarked = {**leg, "price": None}
        usd = CycleBook(
            nav=Decimal("100000"),
            cash=Decimal("100000"),
            margin=Decimal("0"),
            orders=0,
            base="USD",
            currency="USD",
        )
        self.assertEqual(
            block_ticket_reason([unmarked], limits, {}, book=usd, symbol="ZZT", currency="USD"),
            "missing mark",
        )

    def test_position_cap_blocks_when_the_open_does_not_fit(self) -> None:
        limits = _limits(max_position_per_symbol=1000)
        reason = block_reason(
            6,
            limits,
            price=Decimal("2"),
            multiplier=Decimal("100"),
        )
        self.assertEqual(reason, "position exceeds max_position_per_symbol")


class OptionsAndSlotsTest(unittest.TestCase):
    def test_expiry_is_nearest_35_and_at_least_21(self) -> None:
        expiries = ["20261020", "20261114", "20261218", "bad"]
        picked = choose_expiry(expiries, TODAY)
        self.assertEqual(picked, date(2026, 11, 14))
        self.assertIsNone(choose_expiry(["20261015"], TODAY))
        band = expiry_band(expiries, TODAY)
        self.assertEqual(band[0], date(2026, 11, 14))

    def test_puts_are_at_or_below_and_calls_at_or_above(self) -> None:
        strikes = [150, 160, 170, 180, 190, 200, 210, 220]
        underlying = Decimal("180")
        puts = strike_shortlist(strikes, underlying, "P", width=4, otm_pct=10, inside=False)
        calls = strike_shortlist(strikes, underlying, "C", width=4, otm_pct=10, inside=False)
        self.assertTrue(puts)
        self.assertTrue(all(strike <= underlying * Decimal("0.9") for strike in puts))
        self.assertTrue(all(strike >= underlying * Decimal("1.1") for strike in calls))
        near = strike_shortlist(strikes, underlying, "P", width=4, otm_pct=25)
        self.assertIn(Decimal("170"), near)
        self.assertNotIn(Decimal("100"), near)
        pair = choose_strikes(strikes, underlying)
        self.assertIsNotNone(pair)
        self.assertLessEqual(pair[0], underlying * Decimal("0.9"))
        self.assertGreaterEqual(pair[1], underlying * Decimal("1.1"))
        self.assertIsNone(choose_strikes([180], underlying))

    def test_a_third_quote_waits_for_a_free_line(self) -> None:
        import threading
        import time

        from athena_exec.broker import set_subscription_limit, subscription

        self.assertEqual(IBSlots().concurrent_subscriptions, 2)
        set_subscription_limit(2)
        inside: list[int] = []
        release = threading.Event()

        def occupy() -> None:
            with subscription():
                inside.append(1)
                release.wait(2)

        threads = [threading.Thread(target=occupy) for _ in range(2)]
        try:
            for thread in threads:
                thread.start()
            deadline = time.time() + 1
            while len(inside) < 2 and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(inside), 2)
            third_in = threading.Event()

            def third() -> None:
                with subscription():
                    third_in.set()

            extra = threading.Thread(target=third)
            extra.start()
            time.sleep(0.15)
            self.assertFalse(third_in.is_set())
            release.set()
            extra.join(2)
            self.assertTrue(third_in.is_set())
        finally:
            release.set()
            for thread in threads:
                thread.join(2)
            set_subscription_limit(2)

    def test_a_closed_market_skips_the_live_quote(self) -> None:
        morning = datetime(2026, 10, 13, 10, 10, tzinfo=HKT)
        self.assertFalse(cash_open("QCOM", morning))
        self.assertTrue(cash_open("700", morning))
        lunch = datetime(2026, 10, 13, 12, 30, tzinfo=HKT)
        self.assertFalse(cash_open("QCOM", lunch))
        self.assertFalse(cash_open("700", lunch))
        evening = datetime(2026, 10, 13, 22, 0, tzinfo=HKT)
        self.assertTrue(cash_open("QCOM", evening))
        self.assertFalse(cash_open("700", evening))
        saturday_tail = datetime(2026, 10, 17, 2, 0, tzinfo=HKT)
        self.assertTrue(cash_open("QCOM", saturday_tail))
        self.assertFalse(cash_open("700", saturday_tail))
        saturday = datetime(2026, 10, 17, 10, 0, tzinfo=HKT)
        self.assertFalse(cash_open("QCOM", saturday))
        self.assertFalse(cash_open("700", saturday))
        self.assertFalse(cash_open("RY", morning, "ca"))
        self.assertTrue(cash_open("RY", evening, "ca"))

    def test_one_empty_history_quiets_the_symbol(self) -> None:
        from athena_exec.broker import IbBroker

        broker = IbBroker()
        self.assertFalse(broker.history_quiet("T"))
        broker.note_history_gap("T", 162)
        self.assertTrue(broker.history_quiet("T"))
        broker.note_history_gap("QCOM", 200)
        self.assertFalse(broker.history_quiet("QCOM"))

    def test_slot_clock(self) -> None:
        slots = IBSlots()
        monday = date(2026, 10, 5)
        self.assertEqual(slot_state(datetime(monday.year, monday.month, monday.day, 10, 0, tzinfo=HKT), slots), "athena")
        self.assertEqual(slot_state(datetime(2026, 10, 5, 20, 0, tzinfo=HKT), slots), "mobile")
        self.assertEqual(slot_state(datetime(2026, 10, 5, 12, 30, tzinfo=HKT), slots), "idle")
        self.assertEqual(slot_state(datetime(2026, 10, 10, 10, 0, tzinfo=HKT), slots), "idle")
        self.assertEqual(slot_state(datetime(2026, 10, 5, 22, 0, tzinfo=HKT), slots), "athena")
        self.assertEqual(slot_state(datetime(2026, 10, 6, 2, 0, tzinfo=HKT), slots), "athena")
        self.assertEqual(slot_state(datetime(2026, 10, 5, 2, 0, tzinfo=HKT), slots), "idle")
        self.assertEqual(slot_state(datetime(2026, 10, 10, 2, 0, tzinfo=HKT), slots), "athena")
        self.assertEqual(slot_state(datetime(2026, 10, 11, 2, 0, tzinfo=HKT), slots), "idle")
        self.assertEqual(slot_state(datetime(2026, 10, 31, 2, 0, tzinfo=HKT), slots), "athena")
        self.assertEqual(slot_state(datetime(2026, 10, 31, 4, 30, tzinfo=HKT), slots), "idle")
        self.assertEqual(slot_state(datetime(2026, 11, 1, 2, 0, tzinfo=HKT), slots), "idle")
        self.assertEqual(slot_state(datetime(2026, 11, 2, 22, 0, tzinfo=HKT), slots), "idle")
        self.assertEqual(slot_state(datetime(2026, 11, 2, 22, 30, tzinfo=HKT), slots), "athena")
        self.assertEqual(slot_state(datetime(2026, 11, 3, 4, 30, tzinfo=HKT), slots), "athena")
        self.assertEqual(slot_state(datetime(2026, 11, 3, 5, 0, tzinfo=HKT), slots), "idle")
        self.assertEqual(slot_state(datetime(2026, 11, 2, 10, 0, tzinfo=HKT), slots), "athena")
        custom = IBSlots(athena_slots="HKT 09:30-12:00, 13:00-16:00, 21:00-03:00")
        self.assertEqual(slot_state(datetime(2026, 11, 2, 21, 45, tzinfo=HKT), custom), "athena")
        afternoon = datetime(2026, 10, 5, 17, 0, tzinfo=HKT)
        self.assertTrue(observe_allowed(afternoon, slots, "always"))
        self.assertFalse(draft_allowed(afternoon, slots, "athena"))
        self.assertFalse(observe_allowed(datetime(2026, 10, 10, 12, 0, tzinfo=HKT), slots, "always"))
        paper = PROFILES["paper"]
        self.assertEqual(paper, (25, 20, 8, 5, 40, 21, 20000, 6, 35, 7))
        self.assertEqual(next(name for name, preset in PROFILES.items() if preset == paper), "paper")
        changed = paper[:7] + (50,) + paper[8:]
        self.assertIsNone(next((name for name, preset in PROFILES.items() if preset == changed), None))


class PricePhaseTest(unittest.TestCase):
    def test_rsi_needs_period_plus_one(self) -> None:
        self.assertIsNone(rsi([Decimal("1"), Decimal("2")], 2))
        rising = [Decimal(index) for index in range(1, 6)]
        self.assertEqual(rsi(rising, 2), Decimal(100))

    def test_support_and_resistance_inside_the_band(self) -> None:
        lows = [Decimal("100")] * 5
        highs = [Decimal("110")] * 5
        support = price_tags(highs, lows, [Decimal("100")] * 5, period=14, overbought=70, oversold=30, lookback=5, touch_pct=1)
        self.assertIn("SUPPORT_TOUCH", support)
        resistance = price_tags(
            highs, lows, [Decimal("110")] * 5, period=14, overbought=70, oversold=30, lookback=5, touch_pct=1
        )
        self.assertIn("RESISTANCE_TOUCH", resistance)
        weak = price_tags(
            [Decimal("10")] * 6,
            [Decimal("9")] * 6,
            [Decimal("12"), Decimal("12"), Decimal("12"), Decimal("12"), Decimal("12"), Decimal("8")],
            period=14,
            overbought=70,
            oversold=30,
            lookback=5,
            touch_pct=1,
            volumes=[Decimal("1")] * 5 + [Decimal("10")],
        )
        self.assertIn("PRICE_WEAK", weak)
        self.assertIn("VOLUME_SPIKE", weak)

    def test_phase_steps_and_open_tactics(self) -> None:
        settings = standard_payload()
        self.assertEqual(choose_phase("P1", set(), 0, settings), "P3")
        self.assertEqual(choose_phase("P1", set(), -5, settings), "P1")
        self.assertEqual(choose_phase("P1", {NEWS_WASH, _legacy("H")}, -2, settings), "P1")
        self.assertEqual(choose_phase("P1", {_legacy("L")}, 2, settings), "P1")
        self.assertEqual(choose_phase("P4", {"OVERBOUGHT"}, 7, settings), "P5")
        self.assertEqual(choose_phase("P5", set(), -20, settings), "P1")
        self.assertEqual(choose_phase("P2", set(), None, settings), "P1")
        self.assertEqual(choose_phase("P1", {PRICE_WEAK, VOLUME_SPIKE}, None, settings), "P2")
        self.assertEqual(choose_tactic("P2", {_legacy("H"), NEWS_WASH}, False, False), "HOLD")
        self.assertEqual(choose_tactic("P4", {"OVERSOLD", "SUPPORT_TOUCH"}, False, False), "OPEN_CSP")
        self.assertEqual(choose_tactic("P5", {"OVERBOUGHT"}, True, False), "OPEN_CC")
        self.assertEqual(choose_tactic("P1", set(), False, False), "HOLD")

    def test_evaluate_symbol_stores_open_as_hold(self) -> None:
        conn = _Conn()
        with patch("athena_strategy.phases._load", return_value=("P4", set())):
            evaluate_symbol(
                conn,
                standard_payload(),
                "ZZT",
                "ZZTEST",
                tags={"OVERSOLD", "SUPPORT_TOUCH"},
                event_offset=2,
            )
        _sql, params = conn.calls[-1]
        self.assertEqual(params[2], "P4")
        self.assertEqual(params[4], "HOLD")
        self.assertTrue(conn.committed)


class ManageTest(unittest.TestCase):
    def test_short_dated_option_rolls(self) -> None:
        tactic, _reason = management_tactic(
            sec_type="OPT",
            quantity=Decimal("-1"),
            avg_price=Decimal("2"),
            market_price=Decimal("1.8"),
            expiry=date(2026, 10, 20),
            today=TODAY,
            roll_dte=21,
            profit_take_pct=50,
        )
        self.assertEqual(tactic, "ROLL")

    def test_captured_premium_closes_a_short(self) -> None:
        tactic, reason = management_tactic(
            sec_type="OPT",
            quantity=Decimal("-1"),
            avg_price=Decimal("2"),
            market_price=Decimal("0.5"),
            expiry=date(2026, 12, 18),
            today=TODAY,
            roll_dte=21,
            profit_take_pct=50,
        )
        self.assertEqual(tactic, "CLOSE")
        self.assertIn("75%", reason)

    def test_a_long_stock_is_neither(self) -> None:
        self.assertIsNone(
            management_tactic(
                sec_type="STK",
                quantity=Decimal("1"),
                avg_price=Decimal("20"),
                market_price=Decimal("10"),
                expiry=None,
                today=TODAY,
                roll_dte=21,
                profit_take_pct=50,
            )
        )
        self.assertIsNone(
            management_tactic(
                sec_type="OPT",
                quantity=Decimal("1"),
                avg_price=Decimal("2"),
                market_price=Decimal("3"),
                expiry=date(2026, 12, 18),
                today=TODAY,
                roll_dte=21,
                profit_take_pct=50,
            )
        )


class AgentsTest(unittest.TestCase):
    def test_fill_and_parse(self) -> None:
        self.assertEqual(fill_prompt("{{symbol}} {{last}}", {"symbol": "NVDA", "last": "10"}), "NVDA 10")
        self.assertEqual(parse_json('note {"a": 1} tail'), {"a": 1})
        self.assertEqual(parse_json("not json"), {})
        self.assertEqual(parse_json("[1, 2]"), {})

    def test_rails_use_63_sessions_and_the_prior_window(self) -> None:
        bars = [
            Bar(
                day=date.min,
                open=Decimal(index + 5),
                high=Decimal(index + 10),
                low=Decimal(index),
                close=Decimal(index + 5),
                volume=Decimal(0),
            )
            for index in range(80)
        ]
        self.assertEqual(_rail(bars, "high", 63), Decimal(89))
        self.assertEqual(_rail(bars, "low", 63, high=False), Decimal(17))
        self.assertEqual(_prior_rail(bars, "high"), max(row.high for row in bars[:-21][-63:]))
        self.assertIsNone(_prior_rail(bars[:10], "high"))
        self.assertIsNone(_rail([], "high", 63))

    def test_short_history_renames_the_range(self) -> None:
        short = range_names(40)
        self.assertEqual(short["range_high_tag"], "HIGH_RANGE")
        self.assertEqual(short["range_low_tag"], "LOW_RANGE")
        self.assertEqual(short["range_sessions"], "40")
        location = default_agents()["Location"]
        text = fill_prompt(location.system + "\n" + location.execution, short)
        self.assertNotIn("52-week", text)
        self.assertNotIn("252-session", text)
        self.assertIn("HIGH_RANGE", text)
        long = fill_prompt(location.system, range_names(300))
        self.assertIn("52-week", long)
        self.assertIn("HIGH_52W", long)

    def test_helpers_and_result_lines(self) -> None:
        self.assertEqual(_show(Decimal("2.50")), "2.5")
        self.assertEqual(_show(None), "unknown")
        self.assertIsNone(_number("0"))
        self.assertIsNone(_number("nope"))
        self.assertEqual(_number("1.5"), Decimal("1.5"))
        self.assertEqual(_label("OPEN_CSP"), "CSP")
        self.assertEqual(_listed(None), ("none", "none"))
        plan = {
            "expiry": "20261120",
            "put_strike": Decimal("170"),
            "call_strike": Decimal("190"),
            "put_limit": Decimal("2.5"),
            "call_limit": Decimal("1"),
        }
        puts, calls = _listed(plan)
        self.assertIn("put 170", puts)
        self.assertIn("call 190", calls)
        self.assertEqual(_proposal_text({"quiet": True}), "none")
        self.assertEqual(_proposal_text({"error": "down"}), "none")
        block = _proposal_block({"CSP": {"quiet": False, "strike": 170}})
        self.assertIn("CSP:", block)
        self.assertEqual(_result_line("Location", {"tags": ["LOWER_RAIL"]}, ""), "LOWER_RAIL")
        self.assertEqual(_result_line("News", {"news_wash": True}, ""), "News wash")
        self.assertEqual(_result_line("CSP", {"quiet": False, "strike": 170, "limit": 2}, ""), "170 limit 2")
        self.assertEqual(_result_line("Purchase", {"quiet": False, "limit": 10}, ""), "Buy 10")
        self.assertEqual(_result_line("Strangle", {"quiet": False, "put_strike": 1, "call_strike": 2}, ""), "Put 1 call 2")
        self.assertEqual(_result_line("Universe", {"classifiers": "SEMIS,AI_NARRATIVE"}, ""), "SEMIS, AI_NARRATIVE")
        self.assertEqual(_result_line("Consolidator", {"drafts": [{"id": "draft_a"}]}, ""), "draft_a")
        self.assertEqual(_result_line("News", {}, "timeout"), "timeout")
        self.assertEqual(_result_line("CSP", {"quiet": True}, ""), "QUIET")
        self.assertEqual(_result_line("Location", {}, ""), "QUIET")
        self.assertEqual(_result_line("Consolidator", {}, ""), "QUIET")
        _outcome, summary = _trail_outcome([])
        self.assertEqual(summary, "QUIET")
        quiet_row = _audit_tr({
            "time": "10-10 15:00:00",
            "agent": "CSP",
            "symbol": "NVDA",
            "status": "OK",
            "thinking": "off",
            "model": "deepseek-flash",
            "latency": "1.0s",
            "tokens": "0",
            "result": "QUIET",
            "simulated": False,
        })
        self.assertIn("tag green", quiet_row)
        self.assertIn(">QUIET<", _audit_result_tag("Quiet", "ok"))
        self.assertIn(">NEWS_WASH<", _audit_result_tag("News wash", "ok"))
        self.assertIn(">INVALID_SCHEMA<", _audit_result_tag("invalid schema", "invalid"))
        self.assertIn(">CHANNEL_RISING,UPPER_RAIL<", _audit_result_tag("CHANNEL_RISING, UPPER_RAIL", "ok"))
        self.assertIn("tag red", _audit_status_tag("invalid"))
        self.assertIn("tag red", _audit_status_tag("error"))
        self.assertIn(">OK<", _audit_status_tag("ok"))
        tags = _tag_set({"base_tags": _legacy("L")}, {"Location": {"tags": ["LOWER_RAIL"]}, "News": {"news_wash": True}})
        self.assertEqual(tags, ["LOWER_RAIL", "NEWS_WASH"])
        self.assertEqual(_position_cap(0), "off")
        self.assertEqual(_position_cap(12), "12")
        kept = _staged_step("CSP", {"status": "pending", "detail": "ok", "id": "d1"})
        self.assertEqual(kept["outcome"], "kept")
        blocked = _staged_step("CSP", {"status": "blocked", "detail": "cap", "id": "d2"})
        self.assertEqual(blocked["outcome"], "blocked")
        dropped = _staged_step("CSP", {"status": "skipped", "detail": "off", "id": None})
        self.assertEqual(dropped["outcome"], "dropped")
        outcome, summary = _trail_outcome([kept])
        self.assertEqual(outcome, "drafted")
        quiet, _summary = _trail_outcome([{"tactic": "", "outcome": "dropped", "detail": "no drafts"}])
        self.assertEqual(quiet, "quiet")
        blocked_leg, blocked_summary = _trail_outcome(
            [{"tactic": "CC", "outcome": "blocked", "detail": "I1: cc needs 100 covered shares, held 0"}]
        )
        self.assertEqual(blocked_leg, "quiet")
        self.assertIn("I1:", blocked_summary)
        self.assertIn("CSP kept", summary)

    def test_draft_drops_and_a_kept_buy(self) -> None:
        settings = standard_payload()
        flags = TacticFlags(long=False, intent="income")
        with patch("athena_strategy.agents._load", return_value=("P1", set())), patch(
            "athena_strategy.agents.write_decision"
        ):
            empty = _drafts(None, settings, "ZZTEST", "ZZT", flags, {"Consolidator": {"error": "down"}}, {"tags": "none"})
            self.assertEqual(empty, [])
            quiet = _drafts(None, settings, "ZZTEST", "ZZT", flags, {"Consolidator": {"drafts": []}}, {"tags": "none"})
            self.assertEqual(quiet, [])
            unknown = _drafts(
                None,
                settings,
                "ZZTEST",
                "ZZT",
                flags,
                {"Consolidator": {"drafts": ["nope", {"tactic": "NOPE"}, {"tactic": "BUY", "limit": 10}]}},
                {"tags": "none"},
            )
            self.assertEqual(unknown, [])
        self.assertEqual(_propose_one(None, settings, "ZZTEST", "ZZT", "P1", "BUY", None, "buy", {})["outcome"], "dropped")
        self.assertEqual(
            _propose_one(None, settings, "ZZTEST", "ZZT", "P1", "OPEN_CSP", Decimal("1"), "csp", {"CSP": {"quiet": True}})[
                "detail"
            ],
            "CSP agent was quiet",
        )
        self.assertIn(
            "missing expiry",
            _propose_one(None, settings, "ZZTEST", "ZZT", "P1", "OPEN_STRANGLE", Decimal("1"), "st", {"Strangle": {}})[
                "detail"
            ],
        )

    def test_both_keeps_a_long_and_a_strangle(self) -> None:
        settings = standard_payload()
        answers = {
            "Consolidator": {
                "drafts": [
                    {"tactic": "BUY", "limit": 10, "reason": "rail"},
                    {"tactic": "STRANGLE", "limit": 1.2, "reason": "wash"},
                ]
            }
        }

        def propose(*args, **_kwargs):
            tactic = args[5]
            return {"tactic": tactic, "outcome": "kept", "detail": "ok", "draft_id": tactic}

        with patch("athena_strategy.agents._load", return_value=("P1", set())), patch(
            "athena_strategy.agents.write_decision"
        ), patch("athena_strategy.agents.has_open_draft", return_value=False), patch(
            "athena_strategy.agents._propose_one", side_effect=propose
        ), patch("athena_strategy.agents.apply_ticket_block", return_value=None):
            both = _drafts(
                None, settings, "ZZTEST", "ZZT", TacticFlags(long=True, intent="both"), answers, {"tags": "none"}
            )
            income = _drafts(
                None, settings, "ZZTEST", "ZZT", TacticFlags(long=True, intent="income"), answers, {"tags": "none"}
            )
            accumulate = _drafts(
                None,
                settings,
                "ZZTEST",
                "ZZT",
                TacticFlags(long=True, intent="accumulate"),
                answers,
                {"tags": "none"},
            )
        self.assertEqual(both, ["BUY", "OPEN_STRANGLE"])
        self.assertEqual(income, ["OPEN_STRANGLE"])
        self.assertEqual(accumulate, ["BUY", "OPEN_STRANGLE"])
        purchase = default_agents()["Purchase"].system
        consolidator = default_agents()["Consolidator"].system
        self.assertIn("may_buy is on", purchase)
        self.assertIn("Stay quiet when may_buy is off", purchase)
        self.assertIn("A short strangle may be drafted", consolidator)
        self.assertIn("a share purchase may be drafted in the same pass", consolidator)

    def test_signaled_pass_does_not_call_the_model_when_patched(self) -> None:
        settings = standard_payload().model_copy(update={"watchlist": ["ZZT", "BLOCK"], "blocked_symbols": ["BLOCK"]})
        with patch("athena_strategy.agents.write_decision") as written, patch(
            "athena_strategy.agents._run_symbol", side_effect=RuntimeError("boom")
        ), patch("athena_strategy.agents.eligible", return_value=("full", "")):
            created = run_legacy_signaled(None, settings, "ZZTEST")
        self.assertEqual(created, [])
        self.assertEqual(written.call_count, 2)
        texts = [
            '{"tags":[]}',
            '{"news_wash":false}',
            '{"quiet":true}',
            '{"quiet":true}',
            '{"quiet":true}',
            '{"quiet":true}',
            '{"thinking":"","drafts":[]}',
        ]
        cursor = {"n": 0}

        def reply(*_args, **_kwargs):
            text = texts[cursor["n"]]
            cursor["n"] += 1
            return {"text": text, "error": "", "latency_ms": 1, "tokens": 0, "request": {}}

        with patch("athena_strategy.agents._context", return_value={"base_tags": "", "tags": "none"}), patch(
            "athena_strategy.agents.chat", side_effect=reply
        ) as called, patch("athena_strategy.agents._record"), patch(
            "athena_strategy.agents._drafts", return_value=["draft_z"]
        ):
            ids = _run_symbol(None, settings, "ZZTEST", "ZZT")
        self.assertEqual(ids, ["draft_z"])
        self.assertGreaterEqual(called.call_count, 7)

    def test_reply_schema_splits_parse_and_shape(self) -> None:
        payload, status, stage = parse_response("Location", '{"tags":["LOWER_RAIL"]}')
        self.assertEqual(status, "ok")
        self.assertEqual(stage, "")
        self.assertEqual(payload["tags"], ["LOWER_RAIL"])
        payload, status, stage = parse_response("Location", '{"tags":["LOWER_RAIL","252_LOW"]}')
        self.assertEqual(status, "ok")
        self.assertEqual(payload["tags"], ["LOWER_RAIL", "LOW_52W"])
        payload, status, _stage = parse_response(
            "Location", '{"tags":["CHANNEL_RISING","UPPER_RAIL","RANGE_HIGH"]}'
        )
        self.assertEqual(status, "ok")
        self.assertEqual(payload["tags"], ["CHANNEL_RISING", "UPPER_RAIL", "HIGH_RANGE"])
        payload, status, _stage = parse_response("Location", '{"tags":["RANGE_LOW"]}')
        self.assertEqual(status, "ok")
        self.assertEqual(payload["tags"], ["LOW_RANGE"])
        _payload, status, stage = parse_response("Location", '{"tags":["NOT_A_TAG"]}')
        self.assertEqual((status, stage), ("invalid", "schema"))
        _payload, status, stage = parse_response("Location", "not json")
        self.assertEqual((status, stage), ("invalid", "parse"))
        _payload, status, stage = parse_response("CSP", '{"quiet": true, "strike": 9}')
        self.assertEqual((status, stage), ("invalid", "schema"))
        payload, status, _stage = parse_response("Consolidator", '{"thinking":"","drafts":[]}')
        self.assertEqual(status, "ok")
        self.assertEqual(payload["drafts"], [])
        _payload, status, stage = parse_response("News", '{"news_wash":false,"sentiment":2}')
        self.assertEqual((status, stage), ("invalid", "schema"))

    def test_context_reads_rails_from_bars(self) -> None:
        bars = [
            Bar(
                day=date.min,
                open=Decimal("10"),
                high=Decimal("11"),
                low=Decimal("9"),
                close=Decimal("10"),
                volume=Decimal(0),
            )
        ] * 30
        conn = _Conn(rows=[(Decimal("11"), Decimal("9"), Decimal("10"))])
        with patch("athena_strategy.simbars.in_replay", return_value=False), patch(
            "athena_strategy.agents._bars", return_value=bars
        ), patch("athena_strategy.agents._last_close", return_value=Decimal("10")), patch(
            "athena_strategy.agents._shares", return_value=0
        ), patch("athena_strategy.agents._plan", return_value=None), patch(
            "athena_strategy.agents._load", return_value=("P1", {_legacy("L")})
        ), patch("athena_strategy.screener.prompt_sides", return_value=("none", "none", None, None, None)), patch(
            "athena_strategy.agents._classifiers", return_value=""
        ), patch("athena_strategy.agents.tags_from_iv", return_value=set()), patch(
            "athena_strategy.agents._reading", return_value=None
        ), patch("athena_strategy.agents._headlines", return_value="none"
        ), patch("athena_marketdata.pricing.ib_daily_count", return_value=30):
            context = _context(conn, standard_payload(), "ZZTEST", "zzt", TacticFlags())
        self.assertEqual(context["symbol"], "ZZT")
        self.assertEqual(context["last"], "10")
        self.assertEqual(context["high_63"], "11")
        self.assertEqual(context["intent"], "income")
        self.assertEqual(context["long"], "off")
        self.assertEqual(context["position_cap"], "off")
        self.assertEqual(context["contract_cap"], "3")
        self.assertEqual(context["min_dte"], "7")
        self.assertEqual(context["cash_reserve"], "30")
        self.assertEqual(context["range_high_tag"], "HIGH_RANGE")
        day = datetime(2026, 1, 5)
        with patch("athena_strategy.simbars.in_replay", return_value=False):
            rows = _bars(
                _Conn(rows=[(day, Decimal("2"), Decimal("3"), Decimal("1"), Decimal("2"), Decimal("8"), "ib-daily")]),
                "ZZT",
            )
            self.assertEqual(
                _last_close(
                    _Conn(rows=[(day, Decimal("4"), Decimal("4"), Decimal("4"), Decimal("4"), Decimal("1"), "ib-daily")]),
                    "ZZT",
                ),
                Decimal("4"),
            )
            self.assertEqual(_shares(_Conn(rows=[(Decimal("2"),)]), "ZZTEST", "ZZT"), Decimal("2"))
        self.assertEqual(rows[0].high, Decimal("3"))
        self.assertEqual(rows[0].low, Decimal("1"))
        self.assertEqual(rows[0].close, Decimal("2"))
        self.assertEqual(_headlines(_Conn(rows=[]), "ZZT"), "none")
        self.assertEqual(_headlines(_Conn(rows=[("Headline",)]), "ZZT"), "- Headline")
        planned = _plan(
            _Conn(rows=[(date(2026, 11, 20), Decimal("1"), Decimal("2"), Decimal("0.4"), Decimal("0.3"), "NVDA", "CBOE", "100")]),
            "ZZT",
        )
        self.assertEqual(planned["trading_class"], "NVDA")

    def test_news_reply_owns_the_wash_tag(self) -> None:
        legacy = _legacy("L")
        kept = _tag_set(
            {"base_tags": "NEWS_WASH," + legacy},
            {"News": {"error": "timeout"}},
        )
        self.assertIn("NEWS_WASH", kept)
        self.assertNotIn(legacy, kept)
        cleared = _tag_set(
            {"base_tags": "NEWS_WASH," + legacy},
            {"News": {"news_wash": False, "sentiment": 0, "event_type": "GENERIC"}},
        )
        self.assertNotIn("NEWS_WASH", cleared)
        self.assertNotIn(legacy, cleared)

    def test_pullback_is_only_a_mega_cap_dip(self) -> None:
        dip = {
            "base_tags": _legacy("L"),
            "classifiers": "MAG7,MEGA_CAP",
            "last": "100",
            "high_63": "130",
            "low_63": "90",
        }
        rising = {"Location": {"tags": ["CHANNEL_RISING"]}, "News": {"news_wash": False}}
        self.assertIn("PULLBACK", _tag_set(dip, rising))
        other = {**dip, "classifiers": "SEMIS"}
        self.assertNotIn("PULLBACK", _tag_set(other, rising))
        blank = {**dip, "classifiers": ""}
        self.assertNotIn("PULLBACK", _tag_set(blank, rising))
        high = {**dip, "last": "237", "high_63": "243", "low_63": "190"}
        self.assertNotIn("PULLBACK", _tag_set(high, rising))
        stale = {**high, "base_tags": "PULLBACK," + _legacy("L")}
        self.assertNotIn("PULLBACK", _tag_set(stale, rising))
        self.assertFalse(_pullback(high, ["CHANNEL_RISING"]))


class MoneyAndSettingsTest(unittest.TestCase):
    def test_convert_uses_the_stored_rate(self) -> None:
        rates = {"USD": Decimal("7.8")}
        self.assertEqual(convert(Decimal("10"), "USD", "HKD", "HKD", rates), Decimal("78"))
        self.assertEqual(convert(Decimal("10"), "HKD", "HKD", "HKD", rates), Decimal("10"))
        self.assertIsNone(convert(Decimal("10"), "EUR", "HKD", "HKD", rates))
        self.assertEqual(show(None, "EUR"), DASH)
        self.assertEqual(marked(Decimal("-12.5"), "USD"), "−$12.50")
        self.assertEqual(marked(Decimal("1000"), "JPY"), "¥1,000")
        rows = cash_rows({"USD": Decimal("-1.5"), "HKD": Decimal("10")})
        self.assertEqual(rows[0][0], "HKD")
        self.assertEqual(rows[1][1], "−1.50")

    def test_ledger_and_report_currency(self) -> None:
        conn = _Conn(rows=[("USD", {"USD": "3.5"}, {"USD": "1"})])
        base, cash, rates = ledger(conn, "ZZTEST")
        self.assertEqual(base, "USD")
        self.assertEqual(cash["USD"], Decimal("3.5"))
        self.assertEqual(rates["USD"], Decimal("1"))
        empty = _Conn()
        self.assertEqual(ledger(empty, "ZZTEST"), ("", {}, {}))
        self.assertEqual(report_currency(empty, ""), "HKD")

    def test_pick_inherits_env_and_empty_string_wins(self) -> None:
        with patch.dict(os.environ, {"ATHENA_TEST_TOKEN": "from-env"}):
            self.assertEqual(pick(None, "ATHENA_TEST_TOKEN"), "from-env")
            self.assertEqual(pick("", "ATHENA_TEST_TOKEN"), "")
            self.assertEqual(pick(" saved ", "ATHENA_TEST_TOKEN"), "saved")
        settings = standard_payload()
        self.assertEqual(scoring_model(settings), "deepseek-flash")
        renamed = settings.model_copy(update={"ai": settings.ai.model_copy(update={"model": "deepseek-chat"})})
        self.assertEqual(scoring_model(renamed), "deepseek-chat")
        with patch.dict(os.environ, {"TELEGRAM_BOT_USERNAME": "@athena_test"}):
            self.assertEqual(bot_username(settings), "athena_test")
        cleared = settings.model_copy(
            update={"notify": settings.notify.model_copy(update={"bot_username": ""})}
        )
        self.assertEqual(bot_username(cleared), "")
        self.assertEqual(itick_key(settings), pick(settings.itick.api_key, "ITICK_API_KEY"))
        self.assertEqual(deepseek_token(settings), "")
        listed = settings.model_copy(update={"notify": settings.notify.model_copy(update={"telegram_user_ids": "1, 2"})})
        self.assertEqual(allowlist_ids(listed), {"1", "2"})
        self.assertTrue(control_flag(settings, "MISSING", "trading", True))

    def test_provider_credential_and_reasoning(self) -> None:
        legacy = AISettings.model_validate({"provider": "DIRECT"})
        self.assertEqual(legacy.provider, "DEEPSEEK")
        self.assertEqual(AISettings.model_validate({"provider": "KIMI API"}).provider, "KIMI")
        base = standard_payload()
        with patch.dict(
            os.environ,
            {
                "DEEPSEEK_TOKEN": "deep",
                "OPENROUTER_API_KEY": "open",
                "ANTHROPIC_API_KEY": "claude",
                "MOONSHOT_API_KEY": "kimi",
            },
        ):
            expected = {
                "DEEPSEEK": "deep",
                "OPENROUTER": "open",
                "CLAUDE": "claude",
                "KIMI": "kimi",
                "DSH": "",
            }
            for provider, token in expected.items():
                probe = base.model_copy(update={"ai": base.ai.model_copy(update={"provider": provider})})
                self.assertEqual(deepseek_token(probe), token)
        cleared = base.model_copy(
            update={"ai": base.ai.model_copy(update={"provider": "DEEPSEEK", "deepseek_token": ""})}
        )
        self.assertEqual(deepseek_token(cleared), "")
        on = request_body("DEEPSEEK", "deepseek-flash", "sys", "user", True)
        self.assertEqual(on["thinking"], {"type": "enabled"})
        self.assertEqual(on["reasoning_effort"], "medium")
        off = request_body("DIRECT", "deepseek-flash", "sys", "user", False)
        self.assertEqual(off["thinking"], {"type": "disabled"})
        self.assertEqual(request_body("OPENROUTER", "openai/gpt-6-luna", "sys", "user", True)["reasoning"], {"effort": "medium"})
        self.assertEqual(request_body("OPENROUTER", "openai/gpt-6-luna", "sys", "user", False)["reasoning"], {"effort": "none"})
        self.assertEqual(request_body("KIMI", "kimi-k3", "sys", "user", True)["reasoning_effort"], "high")
        self.assertEqual(request_body("KIMI", "kimi-k2.6", "sys", "user", False)["thinking"], {"type": "disabled"})
        code_off = request_body("KIMI", "kimi-k2.7-code", "sys", "user", False)
        self.assertNotIn("thinking", code_off)
        claude = request_body("CLAUDE", "claude-opus-5-5", "sys", "user", True)
        self.assertEqual(claude["output_config"], {"effort": "medium"})
        self.assertNotIn("thinking", claude)
        self.assertTrue(default_agents()["Consolidator"].reasoning)
        self.assertFalse(default_agents()["Location"].reasoning)
        stored = standard_payload()
        stored.agents["Consolidator"] = AgentPrompt(system="Keep this.", execution="Run this.")
        self.assertTrue(resolved_agents(stored)["Consolidator"].reasoning)
        dsh = request_body("DSH", "deepseek-flash", "sys", "user", True)
        self.assertNotIn("thinking", dsh)

    def test_a_normal_save_cannot_change_privilege(self) -> None:
        current = standard_payload()
        version = SettingsVersion(id=1, author="seed", note="n", **current.model_dump())
        changed = current.model_copy(update={"privilege": "opens_only"})
        with patch("athena_settings.repository.load_active", return_value=version):
            with self.assertRaises(ValueError) as caught:
                save_settings(object(), "ZZTEST", changed, "tester", "try")
        self.assertEqual(str(caught.exception), "privilege stays all_orders")
        with self.assertRaises(ValueError) as knob:
            set_knob(object(), "ZZTEST", "privilege", "opens_only", "tester")
        self.assertEqual(str(knob.exception), "privilege stays all_orders")
        text = format_settings(version)
        self.assertIn("privilege all_orders", text)

    def test_accounts_follow_the_mode(self) -> None:
        with patch.dict(os.environ, {"ATHENA_TRADING_MODE": "approve_gated_live", "ATHENA_IB_ACCOUNT_IDS": "A, B"}):
            self.assertEqual(trading_mode(), "live")
            self.assertEqual(active_accounts(), ["A", "B"])
        with patch.dict(os.environ, {"ATHENA_TRADING_MODE": "paper", "ATHENA_IB_PAPER_ACCOUNT_ID": "PAPER1"}):
            self.assertEqual(active_accounts(), ["PAPER1"])
        with patch.dict(os.environ, {"ATHENA_TRADING_MODE": "paper", "ATHENA_IB_PAPER_ACCOUNT_ID": ""}):
            self.assertEqual(stamp_legacy_accounts(object()), 0)
        conn = _Conn()
        conn.rows = []

        class _Result:
            rowcount = 1

        class _Writer(_Conn):
            def execute(self, sql, params=None):
                self.calls.append((sql, params))
                return _Result()

        writer = _Writer()
        with patch.dict(os.environ, {"ATHENA_IB_PAPER_ACCOUNT_ID": "PAPER1"}):
            updated = stamp_legacy_accounts(writer)
        self.assertEqual(updated, 8)
        self.assertTrue(writer.committed)
        with patch("athena_settings.resolve.active_accounts", return_value=[]):
            self.assertIsNone(live_settings(object()))
        with patch.dict(os.environ, {"TELEGRAM_ALLOWLIST_USER_IDS": "9"}), patch(
            "athena_settings.resolve.live_settings", return_value=None
        ):
            self.assertEqual(live_allowlist(object()), {"9"})


class PoolAndIvTest(unittest.TestCase):
    def test_classifiers_and_symbols(self) -> None:
        self.assertEqual(clean_classifiers("semis, not_a_code, SEMIS; mag7"), "SEMIS,MAG7")
        self.assertEqual(clean_symbol(" nvda "), "NVDA")
        self.assertEqual(clean_symbol("700"), "700")
        with self.assertRaises(ValueError):
            clean_symbol("..")
        with self.assertRaises(ValueError):
            clean_symbol("HAS SPACE")

    def test_finviz_and_chart_parsers(self) -> None:
        html = (
            "<title>NVDA - NVIDIA Corp Stock Price</title>"
            ">Market Cap</div></td><td><b>5.5T</b></td>"
            ">Price</div></td><td><b>180.25</b></td>"
            ">Prev Close</div></td><td><b>179.10</b></td>"
            '<a href="screener.ashx?v=111&f=sec_technology">Technology</a>'
            '<a href="screener.ashx?v=111&f=ind_semis">Semiconductors</a>'
        )
        parsed = parse_finviz(html)
        self.assertEqual(parsed["name"], "NVIDIA Corp")
        self.assertEqual(parsed["market_cap"], Decimal("5500000000000"))
        self.assertEqual(parsed["price"], Decimal("180.25"))
        self.assertEqual(parsed["sector"], "Technology")
        chart = parse_chart(
            {"chart": {"result": [{"meta": {"longName": "Nokia", "regularMarketPrice": 4.2, "previousClose": 4.1}}]}}
        )
        self.assertEqual(chart["name"], "Nokia")
        self.assertIsNone(chart["market_cap"])
        self.assertEqual(parse_chart({})["name"], "")

    def test_lookup_uses_the_page_then_the_chart(self) -> None:
        page = "<title>NOK - Nokia Stock Price</title>>Price</div></td><td><b>4.20</b></td>"
        with patch("athena_strategy.pool._get", return_value=page):
            found = lookup_company("NOK")
        self.assertEqual(found["name"], "Nokia")
        body = '{"chart":{"result":[{"meta":{"shortName":"Tencent","regularMarketPrice":380}}]}}'
        with patch("athena_strategy.pool._get", return_value=body):
            hk = lookup_company("700")
        self.assertEqual(hk["name"], "Tencent")

    def test_iv_rank_needs_a_full_window(self) -> None:
        self.assertIsNone(iv_rank([Decimal("1")], Decimal("1"), 20))
        samples = [Decimal(index) for index in range(1, 21)]
        self.assertEqual(iv_rank(samples, Decimal("20"), 20), 100)
        self.assertIsNone(iv_rank([Decimal("2"), Decimal("2")], Decimal("2"), 2))
        self.assertIsNone(iv_rank([Decimal(index) for index in range(1, 20)], Decimal("19"), 1))
        self.assertIsNone(estimated_iv(object(), "T", date.today()))


class IndicatorTest(unittest.TestCase):
    def _bars(self, closes: list[Decimal], volume: Decimal = Decimal(1000)) -> list[Bar]:
        bars = []
        for index, close in enumerate(closes):
            bars.append(
                Bar(
                    day=date(2024, 1, 1) + timedelta(days=index),
                    open=close,
                    high=close + Decimal(1),
                    low=close - Decimal(1),
                    close=close,
                    volume=volume,
                )
            )
        return bars

    def test_rv_is_unchanged_when_prices_scale(self) -> None:
        strategy = standard_payload().strategy
        closes = [Decimal(100) + Decimal(index) * Decimal("3") + Decimal(index % 4) for index in range(40)]
        left = realized_vol(closes, strategy.rv_window, strategy.trading_days_per_year)
        right = realized_vol([price * 2 for price in closes], strategy.rv_window, strategy.trading_days_per_year)
        self.assertEqual(left, right)
        fallen = []
        price = Decimal(100)
        for index in range(40):
            price = price * (Decimal("82") / Decimal(100) if index == 30 else Decimal("1005") / Decimal(1000))
            fallen.append(price)
        other = [item * 2 for item in fallen]
        self.assertEqual(
            realized_vol(fallen, strategy.rv_window, strategy.trading_days_per_year),
            realized_vol(other, strategy.rv_window, strategy.trading_days_per_year),
        )
        samples = [Decimal(index) for index in range(1, 21)]
        base = compute(self._bars(closes), strategy, rv_samples=samples)
        scaled = compute(self._bars([price * 2 for price in closes]), strategy, rv_samples=samples)
        self.assertEqual(base.rv20, scaled.rv20)
        self.assertEqual(base.rv_pctl, scaled.rv_pctl)
        self.assertEqual(base.tags & {spec.name for spec in TAG_REGISTRY.values() if spec.name.startswith("RV_")}, scaled.tags & {spec.name for spec in TAG_REGISTRY.values() if spec.name.startswith("RV_")})

    def test_june_dip_does_not_need_the_rail_tags(self) -> None:
        strategy = standard_payload().strategy
        closes = [Decimal(100) + Decimal(index) for index in range(100)]
        bars = self._bars(closes)
        window = bars[-strategy.rail_lookback:]
        high = max(bar.high for bar in window)
        low = min(bar.low for bar in window)
        span = high - low
        dip = bars[-3]
        deep = low + span * Decimal(str(strategy.dip_deep_pos)) / Decimal(2)
        bars[-3] = Bar(dip.day, deep, high - Decimal(1), low + Decimal(1), deep, dip.volume)
        last = low + span * (Decimal(str(strategy.dip_reclaim_pos)) + Decimal("0.1"))
        current = bars[-1]
        bars[-1] = Bar(current.day, last, high, low + Decimal(1), last, current.volume)
        reading = compute(bars, strategy)
        self.assertIn(DIP_BOUGHT, reading.tags)
        self.assertNotIn(PULLBACK, reading.tags)
        self.assertNotIn(LOWER_RAIL, reading.tags)
        self.assertGreater(reading.rail_pos, Decimal(str(strategy.dip_reclaim_pos)))

    def test_zero_volume_is_unknown(self) -> None:
        strategy = standard_payload().strategy
        closes = [Decimal(100) + Decimal(index) for index in range(40)]
        reading = compute(self._bars(closes, volume=Decimal(0)), strategy)
        self.assertIsNone(reading.atr)
        self.assertIsNone(reading.rv20)
        self.assertIsNone(reading.volume_x)
        self.assertNotIn("VOLUME_DRY", reading.tags)
        self.assertIn("atr=unknown", reading.line())
        self.assertIn("rv20=unknown", reading.line())
        self.assertNotIn("iv=", reading.line())

    def test_short_rv_history_has_no_vol_tag(self) -> None:
        strategy = standard_payload().strategy
        closes = [Decimal(100) + Decimal(index % 5) for index in range(40)]
        reading = compute(self._bars(closes), strategy, rv_samples=[Decimal(10)] * 5)
        self.assertIsNotNone(reading.rv20)
        self.assertIsNone(reading.rv_pctl)
        self.assertFalse(any(tag.startswith("RV_") for tag in reading.tags))

    def test_numbers_match_the_stored_payload(self) -> None:
        strategy = standard_payload().strategy
        closes = [Decimal(80) + Decimal(index) for index in range(30)]
        reading = compute(self._bars(closes), strategy)
        trail = _trail_context(
            {"tags": "none", "flags": "", "numbers": reading.line(), "indicators": reading.payload()},
            TacticFlags(),
            "P1",
            {},
        )
        self.assertEqual(trail["indicators"]["rail_pos"], reading.payload()["rail_pos"])
        self.assertEqual(trail["indicators"]["trend_slope_pct"], reading.payload()["trend_slope_pct"])
        self.assertNotIn("iv=", trail["numbers"])
        filled = fill_prompt("Tags: {{tags}}", {"tags": "DIP_BOUGHT", "numbers": reading.line()})
        self.assertIn("Numbers: ", filled)
        self.assertNotIn("iv=", filled)

    def test_indicator_flag_can_turn_the_layer_off(self) -> None:
        import os

        from athena_strategy.indicators import indicators_enabled

        with patch.dict(os.environ, {"ATHENA_INDICATORS": "off"}):
            self.assertFalse(indicators_enabled())
        with patch.dict(os.environ, {"ATHENA_INDICATORS": "on"}):
            self.assertTrue(indicators_enabled())

    def test_indicator_source_has_no_threshold_literals(self) -> None:
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        text = (root / "packages/strategy-nfr/src/athena_strategy/indicators.py").read_text()
        code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
        code = code.replace("rv20", "").replace("RV20", "")
        self.assertIsNone(re.search(r"0\.35|0\.45|0\.7|252|63|80|20", code))

    def test_legacy_names_are_not_quoted_outside_the_registry(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        hits = []
        for path in list((root / "packages").rglob("*.py")) + list((root / "apps").rglob("*.py")):
            if "tests" in path.parts or path.name == "tags.py":
                continue
            text = path.read_text()
            if '"HIGH_IV"' in text or "'HIGH_IV'" in text or '"LOW_IV"' in text or "'LOW_IV'" in text:
                hits.append(str(path))
        self.assertEqual(hits, [])


class SimMathAndRuntimeTest(unittest.TestCase):
    def test_duration_and_month_add(self) -> None:
        start = date(2026, 9, 10)
        span = (date.today() - (start - timedelta(days=400))).days + 2
        years = min(5, max(1, (max(span, 1) + 364) // 365))
        self.assertEqual(duration_for(start), f"{years} Y")
        self.assertEqual(_add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(_add_months(date(2024, 1, 31), 1), date(2024, 2, 29))
        self.assertEqual(_add_months(date(2026, 11, 10), 3), date(2027, 2, 10))
        self.assertEqual(
            sim_targets(["nvda", "MRVL", "nvda", "bad symbol"], ["NVDA", "MRVL"], ["AAPL"]),
            ["NVDA", "MRVL"],
        )
        self.assertEqual(sim_targets(["AAPL"], ["NVDA"], ["AAPL"]), ["AAPL"])

    def test_cycle_view_moves_from_running_to_waiting(self) -> None:
        note_cycle(3)
        note_loop(60)
        begin(4)
        running = cycle_view()
        self.assertEqual(running["state"], "running")
        self.assertEqual(running["cycle"], 4)
        self.assertEqual(running["step"], "Session")
        mark(1, "running", "bars")
        finish(1, "2 bars")
        wait(0)
        waiting = cycle_view()
        self.assertEqual(waiting["state"], "waiting")
        self.assertEqual(waiting["remaining"], 0)
        self.assertEqual(waiting["roll"][1]["detail"], "2 bars")


class PayloadTest(unittest.TestCase):
    def test_standard_payload_keeps_privilege(self) -> None:
        payload = standard_payload()
        self.assertEqual(payload.privilege, "all_orders")
        self.assertEqual(payload.risk.profile, "standard")
        self.assertIn("NVDA", payload.watchlist)
        with self.assertRaises(Exception):
            SettingsPayload.model_validate({"privilege": "nope", "risk": payload.risk.model_dump()})


def _book_row(market, currency: str = "USD") -> dict:
    return {"market": market, "currency": currency}


class BudgetSourceTest(unittest.TestCase):
    def test_the_decision_path_has_no_optimizer(self) -> None:
        roots = [
            Path("packages/risk/src"),
            Path("packages/strategy-nfr/src/athena_strategy/budget.py"),
            Path("packages/strategy-nfr/src/athena_strategy/wheel.py"),
            Path("packages/strategy-nfr/src/athena_strategy/drafts.py"),
        ]
        banned = ("efficient_frontier", "tangency", "sharpe", "mean_variance", "optimize")
        for root in roots:
            files = [root] if root.is_file() else list(root.rglob("*.py"))
            for path in files:
                text = path.read_text(encoding="utf-8")
                for word in banned:
                    self.assertNotIn(word, text, path.name)


class PositionNlvTest(unittest.TestCase):
    def test_one_currency_sums_signed_market_values(self) -> None:
        card = _position_nlv_stat([
            _book_row(Decimal("22937")),
            _book_row(Decimal("-310")),
        ])
        self.assertIn("$22,627.00", card)
        self.assertIn("USD", card)

    def test_mixed_currencies_stay_apart(self) -> None:
        card = _position_nlv_stat([
            _book_row(Decimal("22627"), "USD"),
            _book_row(Decimal("-940"), "HKD"),
        ])
        self.assertIn(">mixed<", card)
        self.assertIn("USD 22,627.00", card)
        self.assertIn("HKD -940.00", card)

    def test_a_flat_book_and_a_missing_mark_stay_blank(self) -> None:
        self.assertIn(DASH, _position_nlv_stat([]))
        card = _position_nlv_stat([_book_row(None), _book_row(Decimal("10"))])
        self.assertIn("$10.00", card)
        self.assertIn("1 unmarked", card)
