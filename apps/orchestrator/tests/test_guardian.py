"""Guardian R1: pure conformance grading. No database, no IB, no clock, no env."""

import unittest
from decimal import Decimal

from athena_risk import (
    AccountMarks,
    GuardianPosition,
    GuardianRules,
    Tier,
    grade,
    render_report,
)
from athena_settings.model import RiskLimits, standard_payload


def account(nav="100000", excess="25000", mmr="10000", call=False):
    return AccountMarks(
        net_liquidation=Decimal(nav),
        excess_liquidity=None if excess is None else Decimal(excess),
        maint_margin_req=None if mmr is None else Decimal(mmr),
        margin_call=call,
    )


def stock(symbol="NVDA", qty=100, value="5000", pnl=None, managed=True, **kw):
    return GuardianPosition(
        symbol=symbol,
        quantity=Decimal(qty),
        sec_type="STK",
        market_value=None if value is None else Decimal(value),
        unrealized_pnl=None if pnl is None else Decimal(pnl),
        managed=managed,
        **kw,
    )


class TestAccountTiers(unittest.TestCase):
    """D58: EL/NL 20/10/5% and MaintMargin/NL 90%."""

    def test_healthy_at_20pct(self):
        r = grade([], account(excess="20000"))
        self.assertEqual(r.account_tier, Tier.HEALTHY)
        self.assertEqual(r.el_nl_pct, Decimal(20))
        self.assertNotIn("el_watch", r.account_flags)

    def test_balanced_in_10_to_20_band(self):
        r = grade([], account(excess="19999"))
        self.assertEqual(r.account_tier, Tier.BALANCED)
        self.assertIn("el_watch", r.account_flags)

    def test_danger_below_10pct(self):
        r = grade([], account(excess="9999"))
        self.assertEqual(r.account_tier, Tier.DANGER)
        self.assertIn("el_thin", r.account_flags)

    def test_danger_at_8_2pct_the_ratified_case(self):
        r = grade([], account(excess="8200"))
        self.assertEqual(r.account_tier, Tier.DANGER)
        self.assertEqual(r.portfolio_tier, Tier.DANGER)

    def test_blowup_below_5pct(self):
        r = grade([], account(excess="4999"))
        self.assertEqual(r.account_tier, Tier.BLOWUP)
        self.assertIn("el_critical", r.account_flags)

    def test_exact_5pct_is_danger_not_blowup(self):
        r = grade([], account(excess="5000"))
        self.assertEqual(r.account_tier, Tier.DANGER)

    def test_maint_margin_over_90_is_blowup(self):
        r = grade([], account(excess="25000", mmr="90001"))
        self.assertEqual(r.account_tier, Tier.BLOWUP)
        self.assertIn("maint_margin_critical", r.account_flags)

    def test_maint_margin_at_90_is_not_blowup(self):
        r = grade([], account(excess="25000", mmr="90000"))
        self.assertNotIn("maint_margin_critical", r.account_flags)

    def test_margin_call_flag_is_blowup(self):
        r = grade([], account(excess="25000", call=True))
        self.assertEqual(r.account_tier, Tier.BLOWUP)
        self.assertIn("margin_call", r.account_flags)

    def test_blowup_wins_over_healthy_everything_else(self):
        r = grade([stock(pnl="999")], account(excess="1000", call=True))
        self.assertEqual(r.portfolio_tier, Tier.BLOWUP)


class TestMissingData(unittest.TestCase):
    """A missing number is a visible skip, never a green light."""

    def test_missing_nav_is_unknown_not_healthy(self):
        r = grade([], AccountMarks(net_liquidation=None, excess_liquidity=Decimal(25000)))
        self.assertEqual(r.account_tier, Tier.BALANCED)
        self.assertIn("unknown_margin", r.account_flags)
        self.assertIsNone(r.el_nl_pct)

    def test_missing_excess_is_unknown_not_healthy(self):
        r = grade([], account(excess=None))
        self.assertEqual(r.account_tier, Tier.BALANCED)
        self.assertIn("unknown_margin", r.account_flags)

    def test_zero_nav_is_unknown(self):
        r = grade([], account(nav="0", excess="100"))
        self.assertIn("unknown_margin", r.account_flags)

    def test_negative_nav_is_unknown(self):
        r = grade([], account(nav="-5", excess="100"))
        self.assertIn("unknown_margin", r.account_flags)

    def test_missing_market_value_skips_concentration_but_is_visible(self):
        r = grade([stock(value=None)], account())
        g = r.positions[0]
        self.assertIn("missing_market_value", g.flags)
        self.assertNotIn("symbol_over_cap", g.flags)

    def test_empty_book_grades_account_only(self):
        r = grade([], account())
        self.assertEqual(r.positions, ())
        self.assertEqual(r.portfolio_tier, Tier.HEALTHY)
        self.assertEqual(r.breach_count, 0)


class TestProfitAboveHealthy(unittest.TestCase):
    """§2 property 2: blue requires green first."""

    def test_profitable_healthy_book_is_profit(self):
        r = grade([stock(pnl="500")], account())
        self.assertEqual(r.portfolio_tier, Tier.PROFIT)
        self.assertEqual(r.positions[0].tier, Tier.PROFIT)

    def test_flat_book_is_healthy(self):
        r = grade([stock(pnl="0")], account())
        self.assertEqual(r.portfolio_tier, Tier.HEALTHY)

    def test_losing_book_is_healthy_not_profit(self):
        r = grade([stock(pnl="-500")], account())
        self.assertEqual(r.portfolio_tier, Tier.HEALTHY)

    def test_profitable_position_in_thin_margin_book_stays_danger(self):
        """The exact failure mode blue-must-require-green exists to prevent."""
        r = grade([stock(pnl="9999")], account(excess="8200"))
        self.assertEqual(r.positions[0].tier, Tier.PROFIT)
        self.assertEqual(r.portfolio_tier, Tier.DANGER)

    def test_day_pnl_takes_precedence_over_unrealized(self):
        p = GuardianPosition(
            symbol="NVDA", quantity=Decimal(100), sec_type="STK",
            market_value=Decimal(5000), day_pnl=Decimal(-10),
            unrealized_pnl=Decimal(9999),
        )
        r = grade([p], account())
        self.assertEqual(r.positions[0].tier, Tier.HEALTHY)

    def test_no_pnl_at_all_is_healthy_not_profit(self):
        r = grade([stock(pnl=None)], account())
        self.assertEqual(r.positions[0].tier, Tier.HEALTHY)


class TestUnmanagedD63(unittest.TestCase):
    """Accidental positions can never read healthy/profit."""

    def test_unmanaged_capped_at_balanced(self):
        r = grade([stock(managed=False, pnl="999")], account())
        g = r.positions[0]
        self.assertEqual(g.tier, Tier.BALANCED)
        self.assertTrue(g.unmanaged)
        self.assertIn("unmanaged", g.flags)

    def test_unmanaged_still_elevates_on_real_breach(self):
        p = stock(managed=False, value="30000")  # 30% > 20% symbol cap
        r = grade([p], account())
        self.assertEqual(r.positions[0].tier, Tier.DANGER)
        self.assertIn("symbol_over_cap", r.positions[0].flags)

    def test_unmanaged_count(self):
        r = grade([stock(), stock(symbol="MRVL", managed=False)], account())
        self.assertEqual(r.unmanaged_count, 1)

    def test_unmanaged_never_profit_even_if_pnl_positive(self):
        r = grade([stock(managed=False, pnl="100")], account())
        self.assertNotEqual(r.positions[0].tier, Tier.PROFIT)
        self.assertNotEqual(r.positions[0].tier, Tier.HEALTHY)


class TestHardBreaches(unittest.TestCase):
    def test_uncovered_short_call(self):
        call = GuardianPosition(
            symbol="NVDA", quantity=Decimal(-2), sec_type="OPT", right="C",
            strike=Decimal(120), multiplier=Decimal(100), market_value=Decimal(-1000),
        )
        r = grade([call], account())
        self.assertEqual(r.positions[0].tier, Tier.DANGER)
        self.assertIn("uncovered_call", r.positions[0].flags)

    def test_covered_short_call_is_not_breached(self):
        call = GuardianPosition(
            symbol="NVDA", quantity=Decimal(-2), sec_type="OPT", right="C",
            strike=Decimal(120), multiplier=Decimal(100), market_value=Decimal(-1000),
        )
        r = grade([stock(qty=200), call], account())
        self.assertNotIn("uncovered_call", [f for g in r.positions for f in g.flags])

    def test_symbol_over_cap(self):
        r = grade([stock(value="30000")], account())
        self.assertEqual(r.positions[0].tier, Tier.DANGER)
        self.assertIn("symbol_over_cap", r.positions[0].flags)

    def test_symbol_at_cap_is_not_breached(self):
        r = grade([stock(value="20000")], account())
        self.assertNotIn("symbol_over_cap", r.positions[0].flags)

    def test_bucket_over_cap(self):
        p = stock(value="40000", classifiers=("TECH",))
        r = grade([p], account())
        self.assertIn("bucket_over_cap", r.positions[0].flags)
        self.assertEqual(r.positions[0].tier, Tier.DANGER)

    def test_bucket_proximity_window(self):
        # Two 16.5% names in one bucket = 33% bucket (cap 35, within 5 pts),
        # while neither single name breaches the 20% symbol cap.
        rows = [
            stock(symbol="NVDA", value="16500", classifiers=("TECH",)),
            stock(symbol="MRVL", value="16500", classifiers=("TECH",)),
        ]
        r = grade(rows, account())
        for g in r.positions:
            self.assertIn("prox_bucket", g.flags)
            self.assertNotIn("symbol_over_cap", g.flags)
            self.assertEqual(g.tier, Tier.BALANCED)

    def test_bucket_far_from_cap_is_healthy(self):
        p = stock(value="1000", classifiers=("TECH",))
        r = grade([p], account())
        self.assertNotIn("prox_bucket", r.positions[0].flags)
        self.assertNotIn("bucket_over_cap", r.positions[0].flags)

    # -- D66: no invented stop line, mirror the advisor's pending decision --
    def test_no_pending_action_is_healthy(self):
        r = grade([stock()], account())
        self.assertNotIn("advisor_close_pending", r.positions[0].flags)
        self.assertNotIn("advisor_trim_pending", r.positions[0].flags)
        self.assertEqual(r.positions[0].tier, Tier.HEALTHY)

    def test_pending_close_is_danger(self):
        r = grade([stock(pending_action="close")], account())
        self.assertIn("advisor_close_pending", r.positions[0].flags)
        self.assertEqual(r.positions[0].tier, Tier.DANGER)

    def test_pending_take_profit_is_danger(self):
        # manage.py:239 maps take_profit -> CLOSE, so it must read DANGER too.
        r = grade([stock(pending_action="take_profit")], account())
        self.assertIn("advisor_close_pending", r.positions[0].flags)
        self.assertEqual(r.positions[0].tier, Tier.DANGER)

    def test_pending_trim_is_balanced(self):
        r = grade([stock(pending_action="trim")], account())
        self.assertIn("advisor_trim_pending", r.positions[0].flags)
        self.assertEqual(r.positions[0].tier, Tier.BALANCED)

    def test_pending_action_is_normalized(self):
        r = grade([stock(pending_action="  CLOSE  ")], account())
        self.assertIn("advisor_close_pending", r.positions[0].flags)

    def test_pending_hold_and_unknown_values_are_ignored(self):
        for value in ("hold", "HOLD", "nonsense", "", None):
            with self.subTest(value=value):
                r = grade([stock(pending_action=value)], account())
                self.assertNotIn("advisor_close_pending", r.positions[0].flags)
                self.assertNotIn("advisor_trim_pending", r.positions[0].flags)

    def test_stop_line_flags_are_gone(self):
        """D66: the two dead flag codes must not survive in the vocabulary."""
        from athena_risk.guardian import BREACH_FLAGS, PROXIMITY_FLAGS
        self.assertNotIn("breach_stop_line", BREACH_FLAGS)
        self.assertNotIn("breach_trim_line", BREACH_FLAGS)
        self.assertIn("advisor_close_pending", BREACH_FLAGS)
        self.assertIn("advisor_trim_pending", PROXIMITY_FLAGS)

    def test_position_has_no_stop_line_fields(self):
        """D66: the invented line must not exist on the input contract."""
        from dataclasses import fields
        names = {f.name for f in fields(GuardianPosition)}
        self.assertNotIn("stop_line", names)
        self.assertNotIn("trim_line", names)
        self.assertNotIn("market_price", names)
        self.assertIn("pending_action", names)

    def test_short_put_projected_el_negative(self):
        put = GuardianPosition(
            symbol="NVDA", quantity=Decimal(-1), sec_type="OPT", right="P",
            strike=Decimal(100), multiplier=Decimal(100),
            delta=Decimal("-0.40"), market_value=Decimal(-1000),
        )
        r = grade([put], account(excess="5000"))  # 10000 notional > 5000 excess
        flags = [f for g in r.positions for f in g.flags]
        self.assertIn("projected_el_negative", flags)
        self.assertIn("assignment_risk", flags)

    def test_short_put_below_assign_delta_no_projection(self):
        put = GuardianPosition(
            symbol="NVDA", quantity=Decimal(-1), sec_type="OPT", right="P",
            strike=Decimal(100), multiplier=Decimal(100),
            delta=Decimal("-0.10"), market_value=Decimal(-1000),
        )
        r = grade([put], account(excess="5000"))
        flags = [f for g in r.positions for f in g.flags]
        self.assertNotIn("projected_el_negative", flags)
        self.assertNotIn("assignment_risk", flags)


class TestProximity(unittest.TestCase):
    def test_dte_window(self):
        p = GuardianPosition(
            symbol="NVDA", quantity=Decimal(-1), sec_type="OPT", right="P",
            strike=Decimal(100), multiplier=Decimal(100), dte=9,
            market_value=Decimal(-1000),
        )
        r = grade([p], account())  # min_dte_new default 7, |9-7|=2 <= 5
        self.assertIn("prox_dte", r.positions[0].flags)
        self.assertEqual(r.positions[0].tier, Tier.BALANCED)

    def test_dte_outside_window(self):
        p = GuardianPosition(
            symbol="NVDA", quantity=Decimal(-1), sec_type="OPT", right="P",
            strike=Decimal(100), multiplier=Decimal(100), dte=30,
            market_value=Decimal(-1000),
        )
        r = grade([p], account())
        self.assertNotIn("prox_dte", r.positions[0].flags)

    def test_stock_has_no_dte_check(self):
        r = grade([stock(dte=3)], account())
        self.assertNotIn("prox_dte", r.positions[0].flags)


class TestRulesBinding(unittest.TestCase):
    def test_from_limits_binds_overlapping_knobs(self):
        limits = RiskLimits(
            profile="custom", max_symbol_pct_nav=11, cash_reserve_pct=30,
            daily_loss_halt_pct=5, max_contracts_per_symbol=3,
            min_rv_pctl_new_short=50, min_dte_new=30,
            max_short_opt_margin_pct=50, earnings_blackout_days=7,
            max_bucket_pct_nav=9, assign_delta=0.16,
        )
        rules = GuardianRules.from_limits(limits)
        self.assertEqual(rules.max_bucket_pct_nav, Decimal(9))
        self.assertEqual(rules.max_symbol_pct_nav, Decimal(11))
        self.assertEqual(rules.min_dte_new, 30)
        self.assertEqual(rules.assign_delta, Decimal("0.16"))
        # D58 constants stay fixed and cannot be overridden by limits
        self.assertEqual(rules.el_blowup_pct, Decimal(5))

    def test_from_limits_standard_payload(self):
        rules = GuardianRules.from_limits(standard_payload().risk)
        self.assertEqual(rules.min_dte_new, 7)
        self.assertEqual(rules.max_symbol_pct_nav, Decimal(20))

    def test_custom_bucket_cap_is_used(self):
        rules = GuardianRules(max_bucket_pct_nav=Decimal(10))
        p = stock(value="15000", classifiers=("TECH",))
        r = grade([p], account(), rules)
        self.assertIn("bucket_over_cap", r.positions[0].flags)


class TestPurityAndRendering(unittest.TestCase):
    def test_grade_is_deterministic_and_input_untouched(self):
        positions = [stock(value="30000"), stock(symbol="MRVL", managed=False)]
        marks = account(excess="8200")
        a = grade(positions, marks)
        b = grade(positions, marks)
        self.assertEqual(a, b)
        self.assertEqual(len(positions), 2)
        self.assertEqual(positions[0].market_value, Decimal("30000"))

    def test_render_report_has_five_bands(self):
        r = grade([stock(pnl="100")], account())
        text = render_report(r)
        for label in ("盈利", "健康", "平衡", "危险", "暴雷"):
            self.assertIn(label, text)
        self.assertIn("PORTFOLIO", text)
        self.assertIn("EL/NL", text)

    def test_render_report_matches_ratified_mock_case(self):
        p = stock(value="30000")
        r = grade([p], account(excess="8200"))
        text = render_report(r)
        self.assertIn("PORTFOLIO 危险", text)
        self.assertIn("EL/NL 8.2%", text)
        self.assertIn("1 breach", text)

    def test_flags_sorted_and_unique(self):
        p = stock(value="40000", classifiers=("TECH",), managed=False, pnl="10")
        flags = grade([p], account()).positions[0].flags
        self.assertEqual(list(flags), sorted(set(flags)))


if __name__ == "__main__":
    unittest.main()
