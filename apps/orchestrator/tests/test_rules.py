"""Pure rules. No database and no IB."""

import logging
import os
import unittest
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
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
from athena_orchestrator.runtime import (
    begin,
    cycle_view,
    finish,
    install_activity_log,
    mark,
    note_activity,
    note_cycle,
    note_loop,
    wait,
)
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

    def test_position_cap_uses_book_currency_fx(self) -> None:
        from athena_strategy.drafts import book_position_units

        # USD name on an HKD book: $100 × 1 share → HK$780 at 7.8
        unit, existing, err = book_position_units(
            Decimal("100"), Decimal("0"), "USD", "HKD", {"USD": Decimal("7.8")},
        )
        self.assertIsNone(err)
        self.assertEqual(unit, Decimal("780"))
        self.assertEqual(existing, Decimal("0"))
        limits = _limits(max_position_per_symbol=7800, max_contracts_per_symbol=100)
        # 7800 × 1.1 slack / 780 ≈ 11 shares in book HKD
        self.assertEqual(allowed_open(limits, unit, Decimal(1), existing), 11)

        # Same local $100 without FX would wrongly allow far more against an HKD-sized cap
        self.assertGreater(allowed_open(limits, Decimal("100"), Decimal(1), Decimal(0)), 11)

        missing, _ex, gap = book_position_units(
            Decimal("100"), Decimal("0"), "USD", "HKD", {},
        )
        self.assertIsNone(missing)
        self.assertEqual(gap, "missing fx rate USD")

        same, same_ex, same_err = book_position_units(
            Decimal("50"), Decimal("100"), "HKD", "HKD", {},
        )
        self.assertIsNone(same_err)
        self.assertEqual(same, Decimal("50"))
        self.assertEqual(same_ex, Decimal("100"))

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
            orders={},
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
            orders={},
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
            orders={},
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
            orders={},
            base="USD",
            currency="USD",
        )
        self.assertEqual(
            block_ticket_reason([unmarked], limits, {}, book=usd, symbol="ZZT", currency="USD"),
            "missing mark",
        )

    def test_max_orders_per_day_is_per_symbol(self) -> None:
        from athena_risk import walk_cycle

        limits = _limits(max_orders_per_day=2, cash_reserve_pct=0, max_symbol_pct_nav=100, max_short_opt_margin_pct=100)
        # Account already has two NVDA orders today; AMD still has room.
        self.assertEqual(
            block_reason(1, limits, orders_today=2),
            "orders exceed max_orders_per_day",
        )
        self.assertIsNone(block_reason(1, limits, orders_today=1))
        book = CycleBook(
            nav=Decimal("1000000"),
            cash=Decimal("1000000"),
            margin=Decimal("0"),
            orders={"NVDA": 2, "AMD": 0},
            base="USD",
            currency="USD",
        )
        leg = {
            "tactic": "OPEN_CSP",
            "contracts": 1,
            "limit": Decimal("1"),
            "put_strike": Decimal("100"),
            "call_strike": None,
            "multiplier": Decimal("100"),
            "orders": 1,
            "price": Decimal("100"),
        }
        judged = walk_cycle(
            [
                {"symbol": "NVDA", "reason": "open", "currency": "USD", "legs": [leg], "draft_ids": ["a"]},
                {"symbol": "AMD", "reason": "open", "currency": "USD", "legs": [leg], "draft_ids": ["b"]},
            ],
            book,
            limits,
        )
        by_symbol = {ticket["symbol"]: reason for ticket, reason, _order in judged}
        self.assertEqual(by_symbol["NVDA"], "cycle orders")
        self.assertIsNone(by_symbol["AMD"])
        self.assertEqual(book.orders["AMD"], 1)
        self.assertEqual(book.orders["NVDA"], 2)

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

    def test_positions_wait_for_that_accounts_prices(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import Mock

        from athena_exec.broker import IbBroker

        contract = SimpleNamespace(
            conId=7, symbol="ZZT", multiplier="100", secType="OPT", strike=90,
            right="P", currency="USD", lastTradeDateOrContractMonth="20261218",
        )
        book = SimpleNamespace(
            contract=contract, position=-1, averageCost=150, marketPrice=0,
            marketValue=-150, unrealizedPNL=40, realizedPNL=0, account="U1",
        )
        held = SimpleNamespace(account="U1", contract=contract, position=-1, avgCost=150)
        ib = Mock()
        ib.portfolio.return_value = [book]
        ib.positions.return_value = [held]
        broker = IbBroker()
        broker._ib = ib
        rows = broker.positions("U1")
        ib.reqAccountUpdates.assert_called_once_with("U1")
        ib.portfolio.assert_called_with("U1")
        self.assertEqual(rows[0].market_price, Decimal("1.50"))
        self.assertEqual(rows[0].market_value, Decimal("-150"))
        self.assertEqual(rows[0].unrealized_pnl, Decimal("40"))
        self.assertEqual(rows[0].avg_price, Decimal("1.50"))
        ib.reqAccountUpdates.side_effect = TimeoutError()
        ib.portfolio.return_value = []
        late = broker.positions("U1")
        self.assertEqual(late[0].quantity, Decimal("-1"))
        self.assertIsNone(late[0].market_price)

    def test_a_stock_order_can_be_market_limit_or_stop(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import Mock

        from athena_exec.broker import IbBroker
        from athena_settings.prompts import AGENT_ORDER, RUN_ORDER, TRADE_NAME, default_agents

        self.assertIn(TRADE_NAME, default_agents())
        self.assertNotIn(TRADE_NAME, RUN_ORDER)
        self.assertNotIn(TRADE_NAME, AGENT_ORDER)
        broker = IbBroker()
        broker.readonly = False
        ib = Mock()
        trade = SimpleNamespace(order=SimpleNamespace(orderId=44), orderStatus=SimpleNamespace(status="Submitted"), log=[])
        ib.qualifyContracts.return_value = [SimpleNamespace()]
        ib.placeOrder.return_value = trade
        broker._ib = ib
        self.assertEqual(broker.place_market("U1", "ZZT", "BUY", 10), 44)
        market = ib.placeOrder.call_args[0][1]
        self.assertEqual(market.orderType, "MKT")
        self.assertEqual(market.tif, "DAY")
        self.assertEqual(broker.place_limit("U1", "ZZT", "BUY", 1, Decimal("9"), "GTC"), 44)
        self.assertEqual(ib.placeOrder.call_args[0][1].tif, "GTC")
        self.assertEqual(broker.place_stop_limit("U1", "ZZT", "SELL", 4, Decimal("12.4"), Decimal("12.1")), 44)
        stopped = ib.placeOrder.call_args[0][1]
        self.assertEqual(stopped.orderType, "STP LMT")
        self.assertEqual(stopped.lmtPrice, 12.4)
        self.assertEqual(stopped.auxPrice, 12.1)

    def test_one_empty_history_quiets_the_symbol(self) -> None:
        from athena_exec.broker import IbBroker

        broker = IbBroker()
        self.assertFalse(broker.history_quiet("T"))
        broker.note_history_gap("T", 162)
        self.assertTrue(broker.history_quiet("T"))
        broker.note_history_gap("QCOM", 200)
        self.assertFalse(broker.history_quiet("QCOM"))

    def test_history_162_and_head_day_parsing(self) -> None:
        from athena_exec.broker import _as_head_day, _history_162_kind
        from athena_orchestrator.backfill import second_span

        self.assertEqual(_history_162_kind("HMDS query returned no data"), "no_data")
        self.assertEqual(_history_162_kind("No market data permissions for ISLAND STK"), "permission")
        self.assertEqual(_history_162_kind("something else"), "other")
        self.assertEqual(_as_head_day(datetime(2024, 3, 15, 9, 30, tzinfo=timezone.utc)), date(2024, 3, 15))
        self.assertEqual(_as_head_day("20240315"), date(2024, 3, 15))
        self.assertEqual(_as_head_day("2024-03-15"), date(2024, 3, 15))
        self.assertIsNone(_as_head_day(""))
        self.assertIsNone(second_span(date(2024, 3, 15), date(2024, 3, 15)))
        self.assertEqual(second_span(date(2024, 6, 15), date(2024, 3, 15)), "92 D")
        self.assertEqual(second_span(date(2024, 6, 15), None), "1 Y")

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
        from athena_orchestrator.pages import GRADE_KNOBS, matched_profile

        self.assertEqual(list(PROFILES), ["defensive", "standard", "harvest", "aggressive", "paper"])
        for name, knobs in GRADE_KNOBS.items():
            self.assertEqual(PROFILES[name][5], knobs["dte_min"])
            self.assertEqual(matched_profile(PROFILES[name], {key: str(value) for key, value in knobs.items()}), name)
        self.assertEqual(matched_profile(PROFILES["defensive"], {}), "custom")
        self.assertEqual(matched_profile(PROFILES["paper"], {}), "paper")
        self.assertNotIn(7, {preset[5] for name, preset in PROFILES.items() if name != "paper"})

    def test_a_deadlocked_bar_write_is_tried_again(self) -> None:
        from psycopg.errors import DeadlockDetected

        from athena_marketdata.pricing import run_bar_write

        class Conn:
            def __init__(self) -> None:
                self.writes = 0
                self.commits = 0
                self.rollbacks = 0

            def commit(self) -> None:
                self.commits += 1

            def rollback(self) -> None:
                self.rollbacks += 1

        conn = Conn()

        def write() -> None:
            conn.writes += 1
            if conn.writes == 1:
                raise DeadlockDetected()

        self.assertTrue(run_bar_write(conn, write))
        self.assertEqual(conn.writes, 2)
        self.assertEqual(conn.commits, 1)
        self.assertEqual(conn.rollbacks, 1)

    def test_filled_is_only_an_athena_order(self) -> None:
        from athena_bots.telegram import fill_text

        own = fill_text(
            "U1", "SKHY", "MANUAL", "BUY", "draft_1", "151.5",
            shares=10, multiplier="1", commission="1.25", commission_currency="USD",
        )
        self.assertTrue(own.startswith("Filled BUY SKHY MANUAL"))
        self.assertIn("10 @ 151.50", own)
        self.assertIn("Cost $1,515", own)
        self.assertIn("Fee 1.25 USD", own)
        self.assertIn("Draft draft_1", own)
        external = fill_text("U1", "SKHY", "EXTERNAL", "BOT", "", "152.12", 100)
        self.assertEqual(
            external,
            "External buy SKHY\n100 @ 152.12\nCost $15,212\nAccount U1",
        )
        self.assertNotIn("Filled", external)
        sold = fill_text("U1", "SKHY", "EXTERNAL", "SLD", "", "10", 5)
        self.assertTrue(sold.startswith("External sell SKHY"))
        self.assertIn("Credit $50", sold)
        option = fill_text(
            "U1", "NVDA", "OPEN_CSP", "SELL", "draft_p", "2.15",
            shares=1, multiplier="100", commission="0.65", commission_currency="USD", sec_type="OPT",
        )
        self.assertIn("1 @ 2.15", option)
        self.assertIn("Credit $215", option)
        self.assertIn("Multiplier 100", option)
        self.assertIn("Fee 0.65 USD", option)

    def test_a_strike_from_another_class_is_not_quoted(self) -> None:
        from athena_exec.broker import _class_strikes

        class Contract:
            def __init__(self, strike, trading_class):
                self.strike = strike
                self.tradingClass = trading_class

        class Detail:
            def __init__(self, contract):
                self.contract = contract

        strikes = _class_strikes([
            Detail(Contract(200.0, "NVDA")),
            Detail(Contract(203.0, "NVDA1")),
            Detail(Contract(205.0, "NVDA")),
        ], "NVDA")
        self.assertEqual(strikes, [200.0, 205.0])


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
    def test_a_short_dated_option_does_not_roll(self) -> None:
        self.assertIsNone(
            management_tactic(
                sec_type="OPT",
                quantity=Decimal("-1"),
                avg_price=Decimal("2"),
                market_price=Decimal("1.8"),
                expiry=date(2026, 10, 20),
                today=TODAY,
                roll_dte=21,
                profit_take_pct=50,
            )
        )

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
        budget = _audit_result_tag("cycle_order=5 prob_source=delta_proxy cycle margin", "dropped")
        self.assertIn(">CYCLE MARGIN<", budget)
        self.assertIn(">ORDER 5<", budget)
        self.assertNotIn("DELTA", budget)
        self.assertNotIn("PROB_SOURCE", budget)
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
        payload, status, _stage = parse_response(
            "Narrator", '{"open":true,"sentence":"ok","strike":null}'
        )
        self.assertEqual(status, "ok")
        self.assertTrue(payload["open"])
        payload, status, _stage = parse_response(
            "Narrator", '{"open":false,"sentence":"quiet","strike":null}'
        )
        self.assertEqual(status, "ok")
        self.assertFalse(payload["open"])
        _payload, status, stage = parse_response(
            "Narrator", '{"sentence":"missing open","strike":null}'
        )
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
        self.assertEqual(waiting["activity"], [])

    def test_a_running_step_keeps_recent_log_lines(self) -> None:
        import logging

        begin(5)
        note_activity("news yahoo NVDA")
        install_activity_log()
        news = logging.getLogger("athena.news")
        news.setLevel(logging.INFO)
        news.info("news yahoo QCOM")
        broker = logging.getLogger("ib_insync.wrapper")
        broker.setLevel(logging.INFO)
        broker.info("position: skipped")
        running = cycle_view()
        self.assertEqual(running["state"], "running")
        self.assertTrue(any(line.endswith("news yahoo NVDA") for line in running["activity"]))
        self.assertTrue(any(line.endswith("news yahoo QCOM") for line in running["activity"]))
        self.assertFalse(any("position:" in line for line in running["activity"]))
        wait(0)
        self.assertEqual(cycle_view()["activity"], [])


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


class OrdersPageTest(unittest.TestCase):
    def test_orders_sits_in_core_logic(self) -> None:
        from athena_orchestrator.console import NAV_GROUPS

        core = dict(NAV_GROUPS)["Core logic"]
        self.assertIn(("/orders", "Orders"), core)
        self.assertLess(core.index(("/positions", "Positions")), core.index(("/orders", "Orders")))

    def test_the_order_watch_uses_the_next_client_id(self) -> None:
        from athena_exec.clients import client_id
        from athena_exec.order_watch import order_client_id

        with patch.dict("os.environ", {"IB_CLIENT_ID": "2"}, clear=False):
            os.environ.pop("IB_ORDER_CLIENT_ID", None)
            self.assertEqual(client_id("session"), 2)
            self.assertEqual(client_id("orders"), 3)
            self.assertEqual(order_client_id(), 3)
            self.assertEqual(client_id("probe"), 4)
            self.assertEqual(client_id("pulse"), 5)
            self.assertEqual(client_id("telegram"), 22)
        with patch.dict("os.environ", {"IB_ORDER_CLIENT_ID": "9", "IB_CLIENT_ID": "2"}, clear=False):
            self.assertEqual(order_client_id(), 9)
            self.assertEqual(client_id("probe"), 4)


class PulseStreamTest(unittest.TestCase):
    def test_a_changed_print_moves_and_the_same_print_does_not(self) -> None:
        from athena_orchestrator.pulse import format_tick, note_print

        seen: dict[str, Decimal] = {}
        self.assertTrue(note_print(seen, "skhy", Decimal("10.50")))
        self.assertFalse(note_print(seen, "SKHY", Decimal("10.5")))
        up = format_tick("SKHY", Decimal("10.50"), Decimal("10"))
        self.assertEqual(up["price"], "10.50")
        self.assertEqual(up["change"], "+5.00%")
        self.assertEqual(up["tone"], "chg-up")
        down = format_tick("SKHY", Decimal("9"), Decimal("10"))
        self.assertEqual(down["tone"], "chg-down")
        self.assertEqual(down["change"], "-10.00%")
        flat = format_tick("SKHY", Decimal("10"), None)
        self.assertEqual(flat["change"], "—")
        self.assertEqual(flat["tone"], "")

    def test_the_pulse_ivr_line_starts_at_the_left(self) -> None:
        from athena_orchestrator.console import _watch_html

        html = _watch_html([{
            "symbol": "SKHY",
            "price": "10.00",
            "change": "—",
            "tone": "",
            "band": "—",
            "high_pct": "",
            "low_pct": "",
            "iv": [12, 40],
            "rank": "12",
            "phase_class": "",
            "phase": "—",
            "tags": [],
            "side": [],
            "strikes": [],
            "dte": "—",
            "status": [],
        }])
        self.assertIn('data-fit="left"', html)
        self.assertIn("SKHY", html)

    def test_pulse_option_legs_are_expiry_right_strike_badges(self) -> None:
        from athena_orchestrator.cockpit import _watch_row, option_badge
        from athena_orchestrator.console import _watch_html

        expiry = date(2026, 11, 20)
        short_put = option_badge(expiry, "P", Decimal("210"), -1)
        long_call = option_badge(expiry, "C", Decimal("250"), 2)
        self.assertEqual(short_put, {"t": "20'NOV P 210", "cls": "red"})
        self.assertEqual(long_call, {"t": "20'NOV C 250", "cls": "green"})
        self.assertIsNone(option_badge(expiry, "P", Decimal("210"), 0))

        positions = [
            (Decimal("-1"), "OPT", expiry, "P", Decimal("210"), None),
            (Decimal("2"), "OPT", expiry, "C", Decimal("250"), None),
        ]
        row = _watch_row(
            "NVDA", None, "180.00", "—", "", "—", "", "", [], None,
            positions, (expiry, Decimal("200"), Decimal("220")), False, date(2026, 10, 14),
        )
        self.assertEqual(
            row["strikes"],
            [
                {"t": "20'NOV C 250", "cls": "green"},
                {"t": "20'NOV P 210", "cls": "red"},
            ],
        )
        html = _watch_html([row])
        self.assertIn('class="tag red">20&#x27;NOV P 210</span>', html)
        self.assertIn('class="tag green">20&#x27;NOV C 250</span>', html)
        # Plan strikes alone do not fill the option column.
        plan_only = _watch_row(
            "FLAT", None, "10.00", "—", "", "—", "", "", [], None,
            [], (expiry, Decimal("9"), Decimal("11")), False, date(2026, 10, 14),
        )
        self.assertEqual(plan_only["strikes"], [])
        self.assertEqual(plan_only["dte"], str((expiry - date(2026, 10, 14)).days))

    def test_the_cockpit_log_column_spans_both_chart_rows(self) -> None:
        from athena_orchestrator.console import _cockpit_body

        class IB:
            athena_slots = ""
            mobile_slots = ""

        class Settings:
            ib = IB()

        facts = {
            "currency": "HKD", "account": "ZZTEST", "nav": "—", "change": "—",
            "premium": "—", "premium_meta": "", "cash_pct": "", "cash": "—",
            "cash_meta": "", "pending": 0, "ttl": "", "nlv_labels": [], "months": [],
        }
        health = {"state": "ok", "ok": 5, "checks": []}
        html = _cockpit_body(facts, [], health, [], Settings(), None, "", "")
        self.assertIn('class="grid share cockpit-board"', html)
        self.assertIn('class="card log-trail"', html)
        self.assertIn('id="log-roll"', html)
        board = html.split('class="grid share cockpit-board"', 1)[1].split('<div class="card fill">', 1)[0]
        self.assertLess(board.find("sleeve nlv"), board.find("log-trail"))
        self.assertLess(board.find("log-trail"), board.find("ib session schedule"))
        self.assertLess(board.find("monthly premium"), board.find("System health"))

    def test_the_log_trail_drops_lines_that_no_longer_fit(self) -> None:
        from athena_orchestrator.runtime import _TRAIL, install_log_trail, trail_recent

        install_log_trail()
        _TRAIL.clear()
        logger = logging.getLogger("cockpit.test")
        logger.setLevel(logging.INFO)
        for index in range(150):
            logger.info("line %s", index)
        logger.warning("careful")
        recent = [line for line in trail_recent() if line["name"] == "cockpit.test"]
        self.assertLessEqual(len(trail_recent()), 120)
        self.assertEqual(recent[-1]["level"], "WARNING")
        self.assertEqual(recent[-1]["message"], "careful")
        self.assertNotIn("line 0", [line["message"] for line in recent])

    def test_a_sent_order_keeps_one_result_toast(self) -> None:
        from athena_orchestrator.console import _CLIENT_JS

        self.assertNotIn('toast("Sending"', _CLIENT_JS)
        self.assertIn('task.kind === "trade" && task.status !== "running"', _CLIENT_JS)
        self.assertIn("watchedTasks.has(id)", _CLIENT_JS)
        self.assertIn('class="status-tgl on" data-trade-gtc', _CLIENT_JS)
        self.assertIn('tif: gtcOn() ? "GTC" : "DAY"', _CLIENT_JS)
        from athena_orchestrator.tasks import trade_tif

        self.assertEqual(trade_tif(""), "GTC")
        self.assertEqual(trade_tif("day"), "DAY")

    def test_a_reset_connection_is_logged_and_released(self) -> None:
        from athena_orchestrator.httpd import QuietHandler

        class Closed(QuietHandler):
            def handle_one_request(self) -> None:
                self.path = "/api/logs"
                raise ConnectionResetError(54, "Connection reset by peer")

        probe = Closed.__new__(Closed)
        probe.client_address = ("127.0.0.1", 50964)
        with self.assertLogs("athena.http", level="INFO") as caught:
            probe.handle()
        self.assertTrue(probe.close_connection)
        self.assertIn("client closed 127.0.0.1 /api/logs", caught.output[0])

        class Broken(QuietHandler):
            def handle_one_request(self) -> None:
                raise RuntimeError("bad request")

        with self.assertRaises(RuntimeError):
            Broken.__new__(Broken).handle()


class PositionNlvTest(unittest.TestCase):
    def test_one_currency_sums_signed_market_values(self) -> None:
        card = _position_nlv_stat([
            _book_row(Decimal("22937")),
            _book_row(Decimal("-310")),
        ])
        self.assertIn("$22,627.00", card)
        self.assertIn("USD", card)

    def test_mixed_currencies_convert_into_the_settings_currency(self) -> None:
        card = _position_nlv_stat(
            [
                _book_row(Decimal("100"), "USD"),
                _book_row(Decimal("780"), "HKD"),
            ],
            "HKD",
            "HKD",
            {"USD": Decimal("7.8")},
        )
        self.assertIn("$1,560.00", card)
        self.assertIn("HKD", card)
        self.assertNotIn("mixed", card)

    def test_open_pnl_row_shows_native_currency(self) -> None:
        from athena_orchestrator.pages import _position_tr

        html = _position_tr({
            "symbol": "NVDA",
            "qty": Decimal("10"),
            "avg": Decimal("100"),
            "sec_type": "STK",
            "right": "",
            "expiry": "",
            "dte": "",
            "strike": "",
            "mark": "110.00",
            "stored_close": False,
            "market": Decimal("1100"),
            "value": "1,100.00",
            "pnl": Decimal("100"),
            "currency": "USD",
            "mult": "1",
            "multiplier": "",
            "realized": "",
            "synced": "",
            "simulated": False,
        })
        self.assertIn('class="mono pos-pnl chg-up">+100.00 USD</td>', html)
        self.assertIn('class="mono pos-mark"', html)
        self.assertIn('data-mult="1"', html)
        short = _position_tr({
            "symbol": "0700",
            "qty": Decimal("-100"),
            "avg": Decimal("300"),
            "sec_type": "STK",
            "right": "",
            "expiry": "",
            "dte": "",
            "strike": "",
            "mark": "310.00",
            "stored_close": False,
            "market": Decimal("-31000"),
            "value": "-31,000.00",
            "pnl": Decimal("-1000"),
            "currency": "HKD",
            "mult": "1",
            "multiplier": "",
            "realized": "",
            "synced": "",
            "simulated": False,
        })
        self.assertIn('class="mono pos-pnl chg-down">-1,000.00 HKD</td>', short)

    def test_pulse_watch_includes_held_stocks(self) -> None:
        from athena_orchestrator.pulse import held_stock_symbols, watch_symbols, working_order_symbols
        from athena_settings.model import standard_payload

        class Conn:
            def __init__(self):
                self.calls = []

            def execute(self, sql, params=None):
                self.calls.append((sql, params))
                class Rows:
                    def fetchall(self_inner):
                        if "FROM positions" in sql:
                            return [("NVDA",), ("AAPL",)]
                        if "FROM orders" in sql:
                            return [("AMD",)]
                        return []
                return Rows()

        conn = Conn()
        self.assertEqual(held_stock_symbols(conn, "U1"), ["NVDA", "AAPL"])
        self.assertEqual(working_order_symbols(conn, "U1"), ["AMD"])
        settings = standard_payload().model_copy(
            update={"watchlist": ["TSLA", "META"], "blocked_symbols": ["META"]},
        )
        with patch("athena_settings.load_active", return_value=settings):
            # Disabled pool names stay on the pulse list so universe rows can update.
            self.assertEqual(watch_symbols(conn, "U1"), ["TSLA", "META", "NVDA", "AAPL", "AMD"])

    def test_universe_pool_row_has_live_price_hooks(self) -> None:
        from athena_orchestrator.console import _CLIENT_JS
        from athena_orchestrator.pages import _pool_row
        from athena_settings.model import standard_payload

        settings = standard_payload().model_copy(update={"blocked_symbols": ["ZZT"]})
        with patch("athena_orchestrator.pages._closes", return_value=("10.00", "+1.00%", "chg-up")), patch(
            "athena_orchestrator.pages._year_band", return_value=(DASH, DASH, DASH)
        ), patch(
            "athena_orchestrator.pages._phase_row", return_value=("", "", "")
        ), patch(
            "athena_orchestrator.pages._pool_profile", return_value=("", None, "", "")
        ), patch(
            "athena_orchestrator.pages._rank_text", return_value=DASH
        ):
            html = _pool_row(object(), "ZZTEST", settings, "ZZT")
        self.assertIn('class="mono pool-px"', html)
        self.assertIn('class="mono pool-chg chg-up"', html)
        self.assertIn('data-symbol="ZZT"', html)
        self.assertIn('style="opacity:.45"', html)
        self.assertIn("function paintPoolTick", _CLIENT_JS)
        self.assertIn('getElementById("pool-table")', _CLIENT_JS)

    def test_order_gap_and_cost_from_limit_and_last(self) -> None:
        from athena_orchestrator.pages import _order_cost, _order_gap

        text, tone = _order_gap(Decimal("105"), Decimal("100"))
        self.assertEqual(text, "+5.00%")
        self.assertEqual(tone, "chg-up")
        below, down = _order_gap(Decimal("95"), Decimal("100"))
        self.assertEqual(below, "-5.00%")
        self.assertEqual(down, "chg-down")
        self.assertEqual(_order_gap(None, Decimal("100")), (DASH, ""))
        self.assertEqual(_order_cost(10, Decimal("12.5"), None, "1"), "125.00")
        self.assertEqual(_order_cost(2, Decimal("1.5"), None, "100"), "300.00")
        self.assertEqual(_order_cost(10, None, Decimal("20"), "1"), "200.00")

    def test_order_row_has_live_price_hooks(self) -> None:
        from athena_orchestrator.console import _CLIENT_JS
        from athena_orchestrator.pages import _order_tr, _orders_table

        row = {
            "id": "ord_zz",
            "account": "U1",
            "when": "2026-10-14 12:00",
            "symbol": "ZZT",
            "action": "BUY",
            "qty": 10,
            "filled": None,
            "order_type": "LMT",
            "limit": Decimal("100"),
            "stop": None,
            "status": "Submitted",
            "bucket": "working",
            "ib": "1",
            "tactic": "MANUAL",
            "kind": "stk",
            "mult": "1",
        }
        with patch("athena_orchestrator.pages._closes", return_value=("98.00", "-1.00%", "chg-down")):
            html = _order_tr(object(), row)
        self.assertIn('class="mono order-gap chg-up"', html)
        self.assertIn("+2.04%", html)
        self.assertIn('class="mono order-cost">1,000.00</td>', html)
        self.assertIn('data-symbol="ZZT"', html)
        self.assertIn('data-kind="stk"', html)
        table = _orders_table("")
        self.assertIn(">vs last<", table)
        self.assertIn(">Est. cost<", table)
        self.assertIn("function paintOrderTick", _CLIENT_JS)
        self.assertIn('getElementById("orders-feed")', _CLIENT_JS)

    def test_open_pnl_converts_and_colors_the_subtitle(self) -> None:
        from athena_orchestrator.pages import _position_pnl_stat

        card = _position_pnl_stat(
            [
                {"pnl": Decimal("10"), "currency": "USD"},
                {"pnl": Decimal("-7.8"), "currency": "HKD"},
            ],
            "HKD",
            "HKD",
            {"USD": Decimal("7.8")},
        )
        self.assertIn("+70.20", card)
        self.assertIn('class="value up"', card)
        self.assertIn('class="meta up"', card)
        self.assertNotIn("mixed", card)
        down = _position_pnl_stat(
            [{"pnl": Decimal("-5"), "currency": "HKD"}],
            "HKD",
            "HKD",
            {},
        )
        self.assertIn("-5.00", down)
        self.assertIn('class="value down"', down)
        self.assertIn('class="meta down"', down)

    def test_a_missing_fx_rate_stays_blank(self) -> None:
        card = _position_nlv_stat(
            [_book_row(Decimal("10"), "USD"), _book_row(Decimal("10"), "HKD")],
            "HKD",
            "HKD",
            {},
        )
        self.assertIn(DASH, card)
        self.assertIn("no HKD rate", card)
        self.assertNotIn("mixed", card)

    def test_a_flat_book_and_a_missing_mark_stay_blank(self) -> None:
        self.assertIn(DASH, _position_nlv_stat([]))
        card = _position_nlv_stat([_book_row(None), _book_row(Decimal("10"))])
        self.assertIn("$10.00", card)
        self.assertIn("1 unmarked", card)


class ViewAndCycleTest(unittest.TestCase):
    def tearDown(self) -> None:
        from athena_orchestrator.view import bind_view

        bind_view(None)
        wait(0)

    def test_the_pulse_keeps_enabled_names_for_this_account(self) -> None:
        from athena_orchestrator.pulse import enabled_symbols
        from athena_settings.model import standard_payload

        settings = standard_payload().model_copy(update={
            "watchlist": ["AAA", "BBB", "CCC"],
            "blocked_symbols": ["BBB"],
        })
        self.assertEqual(enabled_symbols(settings), ["AAA", "CCC"])

    def test_a_cycle_copy_keeps_only_enabled_names(self) -> None:
        from athena_settings.model import cycle_book, open_symbols, standard_payload

        settings = standard_payload().model_copy(update={
            "watchlist": ["AAA", "BBB"],
            "blocked_symbols": ["BBB"],
        })
        scoped = cycle_book(settings)
        self.assertEqual(scoped.watchlist, ["AAA"])
        self.assertEqual(settings.watchlist, ["AAA", "BBB"])
        self.assertIsNot(scoped, settings)
        self.assertEqual(open_symbols(settings, ["AAA", "BBB", "CCC"]), ["AAA", "CCC"])

    def test_the_view_menu_switches_listed_accounts(self) -> None:
        from unittest.mock import patch

        from athena_orchestrator.console import _nav_entries, _view_switch
        from athena_orchestrator.view import bind_view

        with patch("athena_orchestrator.console.trading_mode", return_value="live"):
            groups = dict(_nav_entries())
        self.assertEqual(next(iter(groups)), "View")
        self.assertEqual(groups["View"], ())
        self.assertIn(("/accounts", "Accounts"), groups["Configs"])
        self.assertNotIn(("/accounts", "Accounts"), groups["Core logic"])
        self.assertNotIn(("/simulation", "Simulation"), groups["Configs"])
        self.assertNotIn(("/simulation", "Simulation"), groups["Core logic"])
        with patch("athena_orchestrator.console.trading_mode", return_value="paper"):
            paper = dict(_nav_entries())
        self.assertIn(("/simulation", "Simulation"), paper["Configs"])
        with (
            patch("athena_exec.accounts.active_accounts", return_value=["U1", "U2"]),
            patch("athena_orchestrator.view.active_accounts", return_value=["U1", "U2"]),
        ):
            bind_view("athena_view=U2")
            html = _view_switch(None)
        self.assertIn('data-view-pick', html)
        self.assertIn('value="U2" selected', html)
        self.assertIn(">U1<", html)

    def test_the_view_cookie_selects_a_listed_account(self) -> None:
        from unittest.mock import patch

        from athena_orchestrator.view import bind_view, current_view

        with patch("athena_orchestrator.view.active_accounts", return_value=["A", "B"]):
            bind_view(None)
            self.assertEqual(current_view(), "A")
            bind_view("athena_view=B")
            self.assertEqual(current_view(), "B")
            bind_view("other=1; athena_view=NOPE")
            self.assertEqual(current_view(), "A")

    def test_view_this_book_replaces_the_current_button(self) -> None:
        from athena_orchestrator.pages import _account_card

        facts = {"nlv": "1", "cash": "1", "positions": 0}
        other = _account_card("Live", "LIVE", "U2", False, facts, "p")
        self.assertIn("View this book", other)
        self.assertIn('data-cmd="view-account"', other)
        self.assertNotIn("Current", other)
        viewing = _account_card("Live", "LIVE", "U1", True, facts, "p")
        self.assertIn("VIEWING", viewing)
        self.assertIn(">Viewing<", viewing)

    def test_a_session_switch_is_stored_on_that_account(self) -> None:
        from unittest.mock import patch

        from athena_orchestrator.pages import _controls_for

        with (
            patch("athena_orchestrator.pages.active_accounts", return_value=["A", "B"]),
            patch("athena_orchestrator.pages.load_active", side_effect=lambda _conn, owner: owner),
        ):
            self.assertEqual(_controls_for(None, "A", "B"), ("B", "B"))
            self.assertEqual(_controls_for(None, "A", "EXTRA"), ("A", "A"))

    def test_a_replay_leaves_the_other_account_on_the_live_session(self) -> None:
        from datetime import date
        from unittest.mock import MagicMock, patch

        from athena_orchestrator.paper import extra_sync_ids, run_paper_cycle
        from athena_settings.model import AccountEntry, AccountSwitches, standard_payload
        from athena_settings.repository import SettingsVersion

        def version() -> SettingsVersion:
            data = standard_payload().model_dump()
            data.update(id=1, author="t", note="t")
            return SettingsVersion.model_validate(data)

        book_a = version()
        book_a.extra_accounts = [AccountEntry(account_id="B", label="B")]
        book_a.account_controls = {"B": AccountSwitches(sync=True)}
        book_b = version()
        self.assertEqual(extra_sync_ids(book_a, "A", {"A", "B"}), [])
        self.assertEqual(extra_sync_ids(book_a, "A", {"A"}), ["B"])

        def load(_conn, account: str):
            return book_a if account == "A" else book_b

        def gate(_conn, account: str):
            if account == "A":
                return "live", None
            return "play", date(2026, 1, 2)

        snap = MagicMock()
        snap.name = "athena.json"
        seen: list[tuple] = []

        def spy(index: int, status: str, detail: str = "") -> None:
            seen.append((index, status, detail))
            mark(index, status, detail)

        with (
            patch("athena_orchestrator.paper.active_accounts", return_value=["A", "B"]),
            patch("athena_orchestrator.paper.mark", side_effect=spy),
            patch("athena_orchestrator.paper.seed_standard_if_empty"),
            patch("athena_orchestrator.paper.load_active", side_effect=load),
            patch("athena_orchestrator.paper.cycle_gate", side_effect=gate),
            patch("athena_orchestrator.paper.run_session", return_value=[]) as session,
            patch("athena_orchestrator.paper.prepare_mock_orders"),
            patch("athena_orchestrator.paper.write_watchlist_bars", return_value=0),
            patch("athena_orchestrator.paper.ingest_news", return_value=0),
            patch("athena_orchestrator.paper.record_iv_marks", return_value=0),
            patch("athena_orchestrator.paper.expire_pending", return_value=[]),
            patch("athena_orchestrator.paper.evaluate_watchlist", return_value=[]),
            patch("athena_orchestrator.paper.manage_book", return_value=[]),
            patch("athena_orchestrator.paper.run_signaled", return_value=[]),
            patch("athena_orchestrator.paper.run_observe"),
            patch("athena_orchestrator.paper.apply_cycle_budget", side_effect=lambda _c, _s, _a, ids: ids),
            patch("athena_orchestrator.paper.symbols_for", return_value=["NVDA"]),
            patch("athena_orchestrator.paper.write_snapshot", return_value=snap),
            patch("athena_orchestrator.paper.prune_snapshots", return_value=0),
            patch("athena_orchestrator.paper.sync_snapshot", return_value=None),
        ):
            run_paper_cycle(MagicMock())
        self.assertEqual(session.call_count, 1)
        self.assertEqual(session.call_args.args[2], "A")
        self.assertEqual(session.call_args.kwargs["also_sync"], [])
        self.assertIn((0, "running", "session A"), seen)
        self.assertNotIn((0, "running", "session B"), seen)
        self.assertIn((5, "running", "phases A"), seen)
        self.assertIn((5, "running", "phases B"), seen)


class _FakeMarksConn:
    """Minimal conn for cash-reserve backfill in decision rows."""

    def execute(self, sql, params=None):
        self.sql = " ".join(sql.split())
        self.params = params
        return self

    def fetchone(self):
        if "nav_marks" in getattr(self, "sql", ""):
            return (Decimal("1000"),)
        if "account_marks" in getattr(self, "sql", ""):
            return (Decimal("100"),)
        if "settings" in getattr(self, "sql", "") or "settings_versions" in getattr(self, "sql", ""):
            return None
        return None


class BlockedDraftsTest(unittest.TestCase):
    def test_blocked_rows_stay_off_approve(self) -> None:
        from datetime import UTC, datetime

        from athena_orchestrator.pages import _decision_trs

        html = _decision_trs([
            (datetime(2026, 10, 13, 8, 3, tzinfo=UTC), "draft_a", "SKHY", "ROLL", "blocked", "cycle margin"),
            (datetime(2026, 10, 13, 8, 4, tzinfo=UTC), "draft_b", "INTC", "OPEN_CC", "approve", "ada"),
        ])
        self.assertIn("SKHY", html)
        self.assertIn("cycle margin", html)
        self.assertIn(">BLOCKED<", html)
        self.assertIn(">APPROVED<", html)
        self.assertIn("CC", html)
        self.assertNotIn("Approve", html)
        self.assertNotIn("Blocked ·", html)
        self.assertEqual(_decision_trs([]), "")

    def test_the_drafts_badge_counts_pending(self) -> None:
        from unittest.mock import patch

        from athena_orchestrator.console import _nav_badges

        seen = []

        class Conn:
            def execute(self, sql, params=None):
                seen.append(" ".join(sql.split()))
                return self

            def fetchone(self):
                return (0,)

        with (
            patch("athena_orchestrator.console._account", return_value="U1"),
            patch("athena_orchestrator.sim.progress", return_value={"status": "idle", "total": 0, "day": 0}),
        ):
            badges = _nav_badges(Conn())
        draft_sql = next(sql for sql in seen if "FROM drafts" in sql)
        self.assertIn("status = 'pending'", draft_sql)
        self.assertNotIn("created_at", draft_sql)
        self.assertNotIn("/drafts", badges)

    def test_the_audit_heading_opens_result_types(self) -> None:
        from athena_orchestrator.pages import _audit, _result_legend

        page = _audit(None, "", {})
        self.assertIn('data-panel="audit-legend"', page["body"])
        self.assertIn("Result types", page["body"])
        self.assertIn('class="audit-table-head"', page["body"])
        head, table = page["body"].split('id="audit-table"', 1)
        self.assertIn('data-cmd="refresh-list"', head)
        self.assertNotIn('data-cmd="refresh-list"', table)
        self.assertEqual(page["actions"], "")
        legend = _result_legend()
        self.assertIn('id="audit-legend"', legend)
        self.assertIn("CYCLE MARGIN", legend)
        self.assertIn("ORDER 5", legend)
        self.assertIn("DELTA PROXY", legend)
        self.assertIn("short-option margin", legend)

    def test_blocked_rows_load_one_page(self) -> None:
        import json
        from datetime import UTC, datetime

        from athena_orchestrator.pages import _PAGE, _decision_slice, _drafts

        stamp = datetime(2026, 10, 13, 8, 3, tzinfo=UTC)
        stored = [(stamp, "draft_a", "SKHY", "ROLL", "blocked", "cycle margin")] * (_PAGE + 1)

        class Conn:
            def execute(self, sql, params=None):
                self.sql = sql
                self.params = params
                return self

            def fetchall(self):
                return stored

        conn = Conn()
        rows, more, nxt = _decision_slice(conn, "U1", 0)
        self.assertIn("status = 'blocked'", " ".join(conn.sql.split()))
        self.assertEqual(conn.params[10], _PAGE + 1)
        self.assertEqual(len(rows), _PAGE)
        self.assertTrue(more)
        self.assertEqual(nxt, _PAGE)
        page = _drafts(conn, "U1", {"partial": ["rows"], "offset": ["50"]})
        payload = json.loads(page["body"])
        self.assertIn("SKHY", payload["html"])
        self.assertIn(">BLOCKED<", payload["html"])
        self.assertIn('data-cmd="decision-row"', payload["html"])
        self.assertTrue(payload["more"])
        self.assertNotIn("Approve", payload["html"])
        self.assertNotIn("Blocked ·", payload["html"])

    def test_decision_filters_narrow_the_list(self) -> None:
        from athena_orchestrator.pages import _decision_choice, _decision_slice, _drafts

        self.assertEqual(_decision_choice({"status": ["nope"], "q": ["  SKHY  "]}), ("all", "SKHY"))
        self.assertEqual(_decision_choice({"status": ["blocked"]}), ("blocked", ""))

        class Conn:
            def execute(self, sql, params=None):
                self.sql = " ".join(sql.split())
                self.params = params
                return self

            def fetchall(self):
                return []

        conn = Conn()
        _decision_slice(conn, "U1", 0, "blocked", "margin%")
        self.assertIn("decision = %s", conn.sql)
        self.assertEqual(conn.params[2], "blocked")
        self.assertIn("%margin\\%%", conn.params)
        page = _drafts(None, "", {})
        self.assertIn('data-status="blocked"', page["body"])
        self.assertIn('id="decision-keyword"', page["body"])
        self.assertIn('class="on"', page["body"])
        self.assertIn(">Approved<", page["body"])

    def test_history_filters_keep_fills_and_a_symbol(self) -> None:
        from athena_orchestrator.pages import _history, _history_visible

        filled = {"status": "filled", "symbol": "SKHY", "tactic": "OPEN_CSP", "reason": "dip", "actor": "ada"}
        rejected = {"status": "rejected", "symbol": "NVDA", "tactic": "ROLL", "reason": "no", "actor": "ada"}
        blocked = {"status": "blocked", "symbol": "INTC", "tactic": "BUY", "reason": "cash is below cash_reserve_pct", "actor": ""}
        self.assertTrue(_history_visible(filled, "filled", ""))
        self.assertFalse(_history_visible(rejected, "filled", ""))
        self.assertTrue(_history_visible(blocked, "blocked", ""))
        self.assertFalse(_history_visible(filled, "blocked", ""))
        self.assertTrue(_history_visible(filled, "all", "skhy"))
        self.assertFalse(_history_visible(filled, "all", "nvda"))
        page = _history(None, "", {})
        self.assertIn("Open a row for size, cash, fees", page["lead"])
        self.assertIn('data-status="filled"', page["body"])
        self.assertIn('data-status="approved"', page["body"])
        self.assertIn('data-status="rejected"', page["body"])
        self.assertIn('data-status="blocked"', page["body"])
        self.assertIn("No fills, approvals, or blocks yet.", page["body"])
        self.assertIn('id="history-keyword"', page["body"])
        self.assertIn(">Detail<", page["body"])

    def test_history_detail_summarizes_fill_cost_and_fee(self) -> None:
        from athena_orchestrator.console import _CLIENT_JS
        from athena_orchestrator.pages import _history_detail, _history_item, _history_tr

        when = datetime(2026, 10, 14, 4, 0, tzinfo=timezone.utc)
        item = _history_item(
            when, "ZZT", "MANUAL", "P2", "filled", Decimal("-1000"), "ada", "buy dip",
            side="BUY", qty=10, price=Decimal("100"), multiplier="1",
            commission=Decimal("1.25"), commission_currency="USD", exec_id="exec1", sec_type="STK",
        )
        self.assertEqual(item["detail"], "BUY 10 @ 100.00 · cost $1,000 · fee 1.25 USD")
        self.assertEqual(_history_detail({"status": "approved", "reason": "ok", "actor": "ada"}), "ok")
        html = _history_tr(item)
        self.assertIn('class="mono hist-detail"', html)
        self.assertIn("data-json=", html)
        self.assertIn("fee 1.25 USD", html)
        self.assertIn('cmd === "history-row"', _CLIENT_JS)
        self.assertIn('cmd === "decision-row"', _CLIENT_JS)
        self.assertIn("Notional", _CLIENT_JS)
        self.assertIn("Cash need", _CLIENT_JS)

    def test_history_includes_blocked_drafts(self) -> None:
        from datetime import UTC, datetime

        from athena_orchestrator.pages import _history_detail, _history_item, _history_tr

        when = datetime(2026, 10, 14, 5, 0, tzinfo=UTC)
        item = _history_item(
            when, "INTC", "BUY", "P2", "blocked", None, "", "cash is below cash_reserve_pct",
            qty=12, draft_id="draft_ab12",
            cash="910", nav="5000", cash_pct="18.2", cash_reserve_pct="30",
            cash_need="1200", reserve_floor="1500", shortfall="590",
        )
        self.assertIn("cash 18.2% (910)", item["detail"])
        self.assertIn("< reserve 30%", item["detail"])
        self.assertIn("floor 1,500", item["detail"])
        self.assertIn("short 590", item["detail"])
        self.assertIn("BUY 12", _history_detail(item))
        html = _history_tr(item)
        self.assertIn(">BLOCKED<", html)
        self.assertIn("draft_ab12", html)
        self.assertIn("tag amber", html)
        self.assertIn("reserve_floor", html)

    def test_decision_row_json_has_contracts(self) -> None:
        from datetime import UTC, datetime

        from athena_orchestrator.pages import _decision_trs

        html = _decision_trs([
            (
                datetime(2026, 10, 14, 8, 0, tzinfo=UTC),
                "draft_x1", "INTC", "BUY", "blocked", "cash is below cash_reserve_pct",
                12, "P2", "cash is below cash_reserve_pct",
                {"cash": "1000", "nav": "5000", "cash_pct": "20", "cash_reserve_pct": "30", "cash_need": "1200",
                 "items": [{"detail": "BUY 12 · cash 20% NAV (below 30%) · need ~$1,200"}]},
            ),
        ])
        self.assertIn('data-cmd="decision-row"', html)
        self.assertIn("· 12", html)
        self.assertIn("cash_need", html)
        self.assertIn("1200", html)

    def test_risk_block_trail_and_cash_reserve_badge(self) -> None:
        from athena_orchestrator.console import _CLIENT_JS
        from athena_orchestrator.pages import _audit_badges, _audit_status_label, _budget_label, _decision_trs
        from athena_strategy.drafts import cash_comparison_view, cash_reserve_math, format_risk_block_detail, risk_block_steps

        steps = risk_block_steps(
            draft_id="draft_r1",
            symbol="INTC",
            tactic="BUY",
            contracts=12,
            reason="cash is below cash_reserve_pct",
            cash=Decimal("910"),
            nav=Decimal("5000"),
            cash_reserve_pct=Decimal("30"),
            cash_need=Decimal("1200"),
        )
        self.assertEqual(steps["reason"], "cash is below cash_reserve_pct")
        self.assertEqual(steps["contracts"], 12)
        self.assertIn("items", steps)
        self.assertIn("cash", steps)
        self.assertEqual(steps["reserve_floor"], "1500")
        self.assertEqual(steps["shortfall"], "590")
        self.assertIn("floor 1,500", steps["items"][0]["detail"])
        self.assertIn("short 590", steps["items"][0]["detail"])
        math = cash_reserve_math(Decimal("910"), Decimal("5000"), Decimal("30"))
        self.assertEqual(math["reserve_floor"], Decimal("1500"))
        self.assertEqual(math["shortfall"], Decimal("590"))
        detail = format_risk_block_detail(
            tactic="BUY", contracts=12, cash=Decimal("910"), nav=Decimal("5000"),
            cash_pct=Decimal("18.2"), cash_reserve_pct=Decimal("30"),
            reserve_floor=Decimal("1500"), shortfall=Decimal("590"),
            cash_need=Decimal("1200"), reason="cash is below cash_reserve_pct",
        )
        self.assertIn("BUY 12", detail)
        self.assertIn("cash 18.2% (910)", detail)
        self.assertIn("< reserve 30%", detail)
        self.assertIn("floor 1,500 on NAV 5,000", detail)
        self.assertIn("short 590", detail)
        self.assertIn("1,200", detail)
        thin = cash_comparison_view(
            tactic="BUY", contracts=1, reason="cash is below cash_reserve_pct",
            trail={}, cash=Decimal("100"), nav=Decimal("1000"), cash_reserve_pct=Decimal("30"),
        )
        self.assertEqual(thin["reserve_floor"], "300")
        self.assertEqual(thin["shortfall"], "200")
        self.assertIn("cash 10% (100)", thin["detail"])
        from datetime import UTC, datetime
        from unittest.mock import patch

        with patch(
            "athena_strategy.drafts._account_cash_marks",
            return_value=(Decimal("100"), Decimal("1000"), Decimal("30")),
        ):
            html = _decision_trs([
                (
                    datetime(2026, 10, 14, 8, 0, tzinfo=UTC),
                    "draft_thin", "NVDA", "BUY", "blocked", "cash is below cash_reserve_pct",
                    1, "P1", "cash is below cash_reserve_pct", {},
                ),
            ], _FakeMarksConn(), "U1")
        self.assertIn("reserve_floor", html)
        self.assertIn("300", html)
        self.assertIn("shortfall", html)
        self.assertIn("Reserve floor", _CLIENT_JS)
        self.assertIn("Shortfall", _CLIENT_JS)
        self.assertIn("Orders today", _CLIENT_JS)
        self.assertIn("Max orders/day", _CLIENT_JS)
        self.assertEqual(_budget_label("cash is below cash_reserve_pct"), "CASH RESERVE")
        self.assertEqual(_audit_status_label("blocked"), "BLOCKED")
        badges = _audit_badges("cash is below cash_reserve_pct", "blocked")
        self.assertEqual(badges[0]["text"], "CASH RESERVE")
        self.assertEqual(badges[0]["kind"], "red")

    def test_day_orders_block_detail_shows_counts(self) -> None:
        from athena_orchestrator.pages import _budget_label
        from athena_strategy.drafts import cash_comparison_view, format_risk_block_detail, risk_block_steps

        detail = format_risk_block_detail(
            tactic="CLOSE", contracts=1, reason="orders exceed max_orders_per_day",
            orders_today=10, max_orders_per_day=10,
        )
        self.assertEqual(detail, "CLOSE 1 · orders today 10 ≥ max 10/day")
        steps = risk_block_steps(
            draft_id="draft_ord",
            symbol="SKHY",
            tactic="CLOSE",
            contracts=1,
            reason="orders exceed max_orders_per_day",
            orders_today=10,
            max_orders_per_day=6,
        )
        self.assertEqual(steps["orders_today"], 10)
        self.assertEqual(steps["max_orders_per_day"], 6)
        self.assertIn("orders today 10 ≥ max 6/day", steps["items"][0]["detail"])
        view = cash_comparison_view(
            tactic="CLOSE", contracts=1, reason="orders exceed max_orders_per_day",
            trail={}, orders_today=3, max_orders_per_day=2,
        )
        self.assertEqual(view["orders_today"], "3")
        self.assertEqual(view["max_orders_per_day"], "2")
        self.assertIn("orders today 3 ≥ max 2/day", view["detail"])
        self.assertEqual(_budget_label("orders exceed max_orders_per_day"), "DAY ORDERS")
        contracts = format_risk_block_detail(
            tactic="BUY", contracts=13, reason="contracts exceed max_contracts_per_symbol",
            max_contracts_per_symbol=12,
        )
        self.assertIn("contracts 13 > max 12", contracts)

    def test_fill_commission_ignores_ib_unset(self) -> None:
        from types import SimpleNamespace

        from athena_exec.broker import _fill_commission

        self.assertIsNone(_fill_commission(None))
        self.assertEqual(_fill_commission(SimpleNamespace(commission=1.25)), Decimal("1.25"))
        self.assertIsNone(_fill_commission(SimpleNamespace(commission=1e308)))
