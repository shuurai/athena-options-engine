"""Pure rules. No database and no IB."""

import os
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from athena_exec.accounts import active_accounts, stamp_legacy_accounts, trading_mode
from athena_exec.gate import live_block_reason
from athena_exec.options import choose_expiry, choose_strikes, expiry_band, strike_shortlist
from athena_exec.slots import slot_state
from athena_orchestrator.money import DASH, cash_rows, convert, ledger, marked, report_currency, show
from athena_orchestrator.pages import _audit_result_tag, _audit_status_tag, _audit_tr, sim_targets
from athena_orchestrator.runtime import begin, cycle_view, finish, mark, note_cycle, note_loop, wait
from athena_orchestrator.sim import _add_months, duration_for
from athena_risk import allowed_open, block_reason
from athena_settings.model import AISettings, AgentPrompt, IBSlots, RiskLimits, SettingsPayload, TacticFlags, standard_payload
from athena_settings.prompts import default_agents, resolved_agents
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
from athena_strategy.agents import (
    _bars,
    _context,
    _drafts,
    _headlines,
    _label,
    _last_close,
    _listed,
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
    _trail_outcome,
    fill_prompt,
    parse_json,
    run_signaled,
)
from athena_strategy.classifiers import clean_classifiers
from athena_strategy.iv import iv_rank, iv_tags
from athena_strategy.manage import management_tactic
from athena_strategy.phases import choose_phase, choose_tactic, evaluate_symbol
from athena_strategy.pool import clean_symbol, lookup_company, parse_chart, parse_finviz
from athena_strategy.price import price_tags, rsi


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
        min_iv_rank_new_short=50,
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
        self.assertEqual(block_reason(1, limits, iv_rank=10), "iv rank is below min_iv_rank_new_short")
        self.assertEqual(block_reason(1, limits, dte=3), "dte is below min_dte_new")
        self.assertEqual(block_reason(1, limits, earnings_in_days=2), "earnings blackout")
        self.assertIsNone(block_reason(1, limits, iv_rank=None, dte=None, nav=None))
        self.assertIsNone(block_reason(1, limits, opening=False, iv_rank=1, dte=1))

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
        puts = strike_shortlist(strikes, underlying, "P")
        calls = strike_shortlist(strikes, underlying, "C")
        self.assertTrue(puts)
        self.assertTrue(all(strike <= underlying * Decimal("0.9") for strike in puts))
        self.assertTrue(all(strike >= underlying * Decimal("1.1") for strike in calls))
        pair = choose_strikes(strikes, underlying)
        self.assertIsNotNone(pair)
        self.assertLessEqual(pair[0], underlying * Decimal("0.9"))
        self.assertGreaterEqual(pair[1], underlying * Decimal("1.1"))
        self.assertIsNone(choose_strikes([180], underlying))

    def test_slot_clock(self) -> None:
        slots = IBSlots()
        monday = date(2026, 10, 5)
        self.assertEqual(slot_state(datetime(monday.year, monday.month, monday.day, 10, 0, tzinfo=HKT), slots), "athena")
        self.assertEqual(slot_state(datetime(2026, 10, 5, 20, 0, tzinfo=HKT), slots), "mobile")
        self.assertEqual(slot_state(datetime(2026, 10, 5, 12, 30, tzinfo=HKT), slots), "idle")
        self.assertEqual(slot_state(datetime(2026, 10, 10, 10, 0, tzinfo=HKT), slots), "idle")


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
        self.assertEqual(choose_phase("P1", {"NEWS_WASH", "HIGH_IV"}, -2, settings), "P2")
        self.assertEqual(choose_phase("P1", {"LOW_IV"}, 2, settings), "P4")
        self.assertEqual(choose_phase("P4", {"OVERBOUGHT"}, 7, settings), "P5")
        self.assertEqual(choose_phase("P5", set(), -20, settings), "P1")
        self.assertEqual(choose_phase("P2", set(), None, settings), "P1")
        self.assertEqual(choose_phase("P1", {"PRICE_WEAK", "VOLUME_SPIKE"}, None, settings), "P2")
        self.assertEqual(choose_tactic("P2", {"HIGH_IV", "NEWS_WASH"}, False, False), "OPEN_STRANGLE")
        self.assertEqual(choose_tactic("P4", {"OVERSOLD", "SUPPORT_TOUCH"}, False, False), "OPEN_CSP")
        self.assertEqual(choose_tactic("P5", {"OVERBOUGHT"}, True, False), "OPEN_CC")
        self.assertEqual(choose_tactic("P1", set(), False, False), "HOLD")

    def test_evaluate_symbol_stores_open_as_hold(self) -> None:
        conn = _Conn()
        evaluate_symbol(
            conn,
            standard_payload(),
            "ZZT",
            "ZZTEST",
            tags={"LOW_IV", "OVERSOLD", "SUPPORT_TOUCH"},
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
        bars = [(Decimal(index + 10), Decimal(index), Decimal(index + 5)) for index in range(80)]
        self.assertEqual(_rail(bars, 0, 63), Decimal(89))
        self.assertEqual(_rail(bars, 1, 63, high=False), Decimal(17))
        self.assertEqual(_prior_rail(bars, 0), max(row[0] for row in bars[:-21][-63:]))
        self.assertIsNone(_prior_rail(bars[:10], 0))
        self.assertIsNone(_rail([], 0, 63))

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
        self.assertIn("tag red", _audit_status_tag("error"))
        self.assertIn(">OK<", _audit_status_tag("ok"))
        tags = _tag_set({"base_tags": "LOW_IV"}, {"Location": {"tags": ["LOWER_RAIL"]}, "News": {"news_wash": True}})
        self.assertEqual(tags, ["LOW_IV", "LOWER_RAIL", "NEWS_WASH"])
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

    def test_signaled_pass_does_not_call_the_model_when_patched(self) -> None:
        settings = standard_payload().model_copy(update={"watchlist": ["ZZT", "BLOCK"], "blocked_symbols": ["BLOCK"]})
        with patch("athena_strategy.agents.write_decision") as written, patch(
            "athena_strategy.agents._run_symbol", side_effect=RuntimeError("boom")
        ):
            created = run_signaled(None, settings, "ZZTEST")
        self.assertEqual(created, [])
        self.assertEqual(written.call_count, 2)
        chat = {"text": '{"tags":[]}', "error": "", "latency_ms": 1, "tokens": 0, "request": {}}
        with patch("athena_strategy.agents._context", return_value={"base_tags": "", "tags": "none"}), patch(
            "athena_strategy.agents.chat", return_value=chat
        ) as called, patch("athena_strategy.agents._record"), patch(
            "athena_strategy.agents._drafts", return_value=["draft_z"]
        ):
            ids = _run_symbol(None, settings, "ZZTEST", "ZZT")
        self.assertEqual(ids, ["draft_z"])
        self.assertGreaterEqual(called.call_count, 7)

    def test_context_reads_rails_from_bars(self) -> None:
        bars = [(Decimal("11"), Decimal("9"), Decimal("10"))] * 30
        conn = _Conn(rows=[(Decimal("11"), Decimal("9"), Decimal("10"))])
        with patch("athena_strategy.simbars.in_replay", return_value=False), patch(
            "athena_strategy.agents._bars", return_value=bars
        ), patch("athena_strategy.agents._last_close", return_value=Decimal("10")), patch(
            "athena_strategy.agents._shares", return_value=0
        ), patch("athena_strategy.agents._plan", return_value=None), patch(
            "athena_strategy.agents._load", return_value=("P1", {"LOW_IV"})
        ), patch("athena_strategy.agents._priced_lines", return_value=("none", "none")), patch(
            "athena_strategy.agents._classifiers", return_value=""
        ), patch("athena_strategy.agents.tags_from_iv", return_value=set()), patch(
            "athena_strategy.agents._headlines", return_value="none"
        ):
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
        with patch("athena_strategy.simbars.in_replay", return_value=False):
            rows = _bars(_Conn(rows=[(Decimal("3"), Decimal("1"), Decimal("2"))]), "ZZT")
            self.assertEqual(_last_close(_Conn(rows=[(Decimal("4"),)]), "ZZT"), Decimal("4"))
            self.assertEqual(_shares(_Conn(rows=[(Decimal("2"),)]), "ZZTEST", "ZZT"), Decimal("2"))
        self.assertEqual(rows, [(Decimal("3"), Decimal("1"), Decimal("2"))])
        self.assertEqual(_headlines(_Conn(rows=[]), "ZZT"), "none")
        self.assertEqual(_headlines(_Conn(rows=[("Headline",)]), "ZZT"), "- Headline")
        planned = _plan(
            _Conn(rows=[(date(2026, 11, 20), Decimal("1"), Decimal("2"), Decimal("0.4"), Decimal("0.3"), "NVDA", "CBOE", "100")]),
            "ZZT",
        )
        self.assertEqual(planned["trading_class"], "NVDA")

    def test_news_reply_owns_the_wash_tag(self) -> None:
        kept = _tag_set(
            {"base_tags": "NEWS_WASH,LOW_IV"},
            {"News": {"error": "timeout"}},
        )
        self.assertIn("NEWS_WASH", kept)
        cleared = _tag_set(
            {"base_tags": "NEWS_WASH,LOW_IV"},
            {"News": {"news_wash": False, "sentiment": 0, "event_type": "GENERIC"}},
        )
        self.assertNotIn("NEWS_WASH", cleared)
        self.assertIn("LOW_IV", cleared)

    def test_pullback_is_only_a_mega_cap_dip(self) -> None:
        dip = {
            "base_tags": "LOW_IV",
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
        stale = {**high, "base_tags": "PULLBACK,LOW_IV"}
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
        self.assertEqual(iv_tags(90, 80, 20), {"HIGH_IV"})
        self.assertEqual(iv_tags(10, 80, 20), {"LOW_IV"})
        self.assertEqual(iv_tags(None, 80, 20), set())
        self.assertEqual(iv_tags(40, 80, 20), set())


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
