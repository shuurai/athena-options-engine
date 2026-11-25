"""D110 (2026-11-24): guidance basket replaced by six independent risk-on axes.

The operator picked Option A after a read-only review. These tests pin the
decision so a future edit cannot silently reintroduce a hedge ETF into a
mean-of-P(r>0) aggregator (sign inversion), or re-add the old 7-semi default.
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BASKET = ["SPY", "QQQ", "IWM", "EEM", "SMH", "NVDA"]
OLD_DEFAULT = ["QQQ", "NVDA", "SOXX", "AVGO", "MRVL", "MU", "TSM"]
D52_BASKET = ["QQQ", "SPY", "IWM", "TLT", "GLD", "SOXX", "NVDA"]
# Hedge / inverse / defensive proxies are forbidden: dir_conf = P(r>0), so a
# rising hedge would be averaged as a risk-on "strong" signal.
FORBIDDEN = ("GLD", "TLT", "VIXY", "UUP", "XLP", "HYG", "LQD", "RSP", "SHV", "BIL")


def _text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestGuidanceBasketDefault(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from athena_settings.model import AdvisorSettings, UNIVERSE_SEED

        cls.basket = AdvisorSettings().guidance_symbols
        cls.seed = list(UNIVERSE_SEED)

    def test_default_is_exactly_option_a(self):
        self.assertEqual(self.basket, BASKET)

    def test_default_is_not_the_old_seven_semi_basket(self):
        self.assertNotEqual(self.basket, OLD_DEFAULT)

    def test_default_is_not_the_superseded_d52_basket(self):
        self.assertNotEqual(self.basket, D52_BASKET)

    def test_no_duplicates(self):
        self.assertEqual(len(self.basket), len(set(self.basket)))

    def test_all_six_are_risk_on_and_no_hedge_proxy(self):
        for symbol in self.basket:
            self.assertNotIn(symbol, FORBIDDEN, f"{symbol} is a hedge/defensive proxy")

    def test_old_semi_duplicates_are_gone(self):
        for symbol in ("SOXX", "AVGO", "MRVL", "MU", "TSM"):
            self.assertNotIn(symbol, self.basket)

    def test_smh_is_the_semiconductor_axis(self):
        self.assertIn("SMH", self.basket)

    def test_spy_is_present_for_broad_market(self):
        self.assertIn("SPY", self.basket)


class TestUniverseSeedCoverage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from athena_settings.model import UNIVERSE_SEED, AdvisorSettings

        cls.seed = list(UNIVERSE_SEED)
        cls.basket = AdvisorSettings().guidance_symbols

    def test_every_basket_name_is_in_the_seed(self):
        missing = [s for s in self.basket if s not in self.seed]
        self.assertEqual(missing, [], f"guidance names with no seed entry: {missing}")

    def test_iwm_and_eem_were_added(self):
        self.assertIn("IWM", self.seed)
        self.assertIn("EEM", self.seed)

    def test_seed_length_is_39(self):
        self.assertEqual(len(self.seed), 39)

    def test_seed_has_no_duplicates(self):
        self.assertEqual(len(self.seed), len(set(self.seed)))

    def test_seed_keeps_original_37_in_order(self):
        original = [
            "NVDA", "QCOM", "T", "700", "1810", "MRVL", "NOK", "DRAM", "AAPL", "ORCL",
            "INTC", "SMH", "GLD", "GOOGL", "AMZN", "SKHY", "GLW", "NIO", "992", "MSFT",
            "SPCX", "AVGO", "ROKU", "AMD", "CNXC", "RDW", "RARE", "IGV", "BABA", "META",
            "TSLA", "SPY", "QQQ", "9988", "9888", "KULR", "MRAM",
        ]
        self.assertEqual(self.seed[:37], original)
        self.assertEqual(self.seed[37:], ["IWM", "EEM"])

    def test_new_names_resolve_to_us_market(self):
        from athena_exec.slots import symbol_market

        for symbol in ("IWM", "EEM"):
            self.assertEqual(symbol_market(symbol), "us")


class TestAggregatorUntouched(unittest.TestCase):
    """Option A is config-only. The mean-of-dir_conf logic must not change."""

    @classmethod
    def setUpClass(cls):
        cls.advisor = _text("packages/strategy-nfr/src/athena_strategy/advisor.py")

    def test_guidance_snapshot_still_averages_dir_conf(self):
        self.assertIn("total += conf", self.advisor)
        self.assertIn("count += 1", self.advisor)
        self.assertIn("avg = total / count if count else None", self.advisor)

    def test_thresholds_unchanged(self):
        self.assertIn('avg < 0.45', self.advisor)
        self.assertIn('avg < 0.6', self.advisor)

    def test_no_new_aggregation_branch(self):
        for forbidden in ("z-score", "zscore", "risk_off_bucket", "defensive_bucket", "ratio_pair"):
            self.assertNotIn(forbidden, self.advisor)

    def test_notes_generators_still_present(self):
        for fn in ("_ib_quotes", "_yahoo_notes", "_calendar_lines"):
            self.assertIn(fn, self.advisor)

    def test_dir_conf_definition_unchanged(self):
        prompts = _text("packages/settings/src/athena_settings/prompts.py")
        self.assertIn("dir_conf is calibrated P(r > 0) in [0, 1]", prompts)

    def test_forecast_still_written_per_watchlist_symbol(self):
        agents = _text("packages/strategy-nfr/src/athena_strategy/agents.py")
        self.assertGreaterEqual(agents.count("for symbol in settings.watchlist:"), 3)
        self.assertIn("_shadow_prediction", agents)


class TestStaleCopyRemoved(unittest.TestCase):
    def test_no_old_seven_semi_literal_in_python(self):
        for rel in (
            "packages/settings/src/athena_settings/model.py",
            "packages/settings/src/athena_settings/prompts.py",
            "apps/orchestrator/src/athena_orchestrator/pages.py",
        ):
            self.assertNotIn("QQQ, NVDA, SOXX, AVGO, MRVL, MU, TSM", _text(rel))

    def test_prompt_no_longer_hardcodes_qqq_semis(self):
        prompts = _text("packages/settings/src/athena_settings/prompts.py")
        self.assertNotIn("guidance basket weakness (QQQ / semis)", prompts)
        self.assertIn("guidance basket weakness (broad-market risk-on proxies)", prompts)

    def test_settings_placeholder_matches_basket(self):
        pages = _text("apps/orchestrator/src/athena_orchestrator/pages.py")
        self.assertIn('placeholder=\\"SPY, QQQ, IWM, EEM, SMH, NVDA\\">', pages)
        self.assertNotIn('placeholder=\\"QQQ, NVDA, SOXX\\">', pages)

    def test_athena_04_default_matches(self):
        doc = _text("docs/athena/04-settings-and-risk.md")
        self.assertIn("| `advisor.guidance_symbols` | SPY, QQQ, IWM, EEM, SMH, NVDA |", doc)
        self.assertNotIn("QQQ, NVDA, SOXX, AVGO, MRVL, MU, TSM", doc)

    def test_athena_13_default_matches(self):
        doc = _text("docs/athena/13-portfolio-advisor.md")
        self.assertIn("| `advisor.guidance_symbols` | SPY, QQQ, IWM, EEM, SMH, NVDA |", doc)
        self.assertIn("Guidance names default to `SPY`, `QQQ`, `IWM`, `EEM`, `SMH`, `NVDA`", doc)

    def test_settings_post_parser_accepts_the_new_basket(self):
        """The /settings POST parser round-trips exactly these six names."""
        from athena_orchestrator.pages import _posted_advisor

        form = {
            "advisor_close_conf": ["0.65"],
            "advisor_trim_conf": ["0.55"],
            "advisor_max_conf": ["0.95"],
            "guidance_symbols": ["SPY, QQQ, IWM, EEM, SMH, NVDA"],
        }
        payload, error = _posted_advisor(form, 15)
        self.assertEqual(error, "")
        self.assertEqual(payload["guidance_symbols"], BASKET)

    def test_settings_card_renders_the_new_basket(self):
        from athena_settings.model import standard_payload
        from athena_orchestrator.pages import _advisor_card

        html = _advisor_card(standard_payload())
        self.assertIn('value="SPY, QQQ, IWM, EEM, SMH, NVDA"', html)

    def test_seed_test_expects_39(self):
        tests = _text("apps/orchestrator/tests/test_workflows.py")
        self.assertIn('self.assertIn("Added 39 seed symbols.", result["body"])', tests)
        self.assertNotIn('self.assertIn("Added 37 seed symbols.", result["body"])', tests)


class TestDecisionRecord(unittest.TestCase):
    def test_14_book_has_section_30(self):
        doc = _text("docs/plan/14-guardian-escalation.md")
        self.assertIn("## 30. Guidance 篮子修正：D110 取代 D52", doc)
        self.assertIn("dir_conf` 语义", doc)
        self.assertIn("### 30.7 交付与验证（2026-11-24，`C-20261124-9`）", doc)

    def test_14_book_records_new_decision_node(self):
        doc = _text("docs/plan/14-guardian-escalation.md")
        self.assertIn("D1–D110", doc)  # D110's own historical record remains intact.
        self.assertIn("D1–D111", doc)
        self.assertIn("112 个节点", doc)

    def test_00_book_count_updated(self):
        doc = _text("docs/plan/00-plan-order.md")
        self.assertIn("D1–D111 / 112 个节点全部有裁定", doc)
        self.assertIn("54 条：50 取推荐", doc)

    def test_00_book_debt_item_3_closed(self):
        doc = _text("docs/plan/00-plan-order.md")
        self.assertIn("已解除（2026-11-24，D110", doc)
        self.assertIn("SPY, QQQ, IWM, EEM, SMH, NVDA", doc)

    def test_00_book_section_12_note(self):
        doc = _text("docs/plan/00-plan-order.md")
        self.assertIn("§30 Guidance 篮子修正（第十五轮 D110", doc)

    def test_13_book_has_section_3_and_d52_pointer(self):
        doc = _text("docs/plan/13-portfolio-advisor.md")
        self.assertIn("## 3. D110：Guidance 篮子取代", doc)
        self.assertIn("**（2026-11-24 由 D110 取代，见 §3）**", doc)

    def test_boundaries_zero_revised(self):
        doc = _text("docs/plan/14-guardian-escalation.md")
        self.assertIn("### 30.4 显式不做（边界）", doc)
        self.assertIn("本轮不扩展活库 watchlist", doc)
        self.assertIn("**不新建数据源／不订阅**", doc)
        # Must be the §30 delivery row, not one of the older §27–§29 rows.
        self.assertIn("**§1／§6 零修订**、**新增 1 个决策节点 D110**", doc)

    def test_no_new_migration(self):
        migrations = sorted((ROOT / "packages/store/src/athena_store/migrations").glob("*.sql"))
        # V2-P2 C-20261125-6: 056_strategy_events.sql is the first post-055 migration.
        self.assertEqual(len(migrations), 52)
        self.assertFalse((ROOT / "packages/store/src/athena_store/migrations/056_guidance.sql").exists())


if __name__ == "__main__":
    unittest.main()
