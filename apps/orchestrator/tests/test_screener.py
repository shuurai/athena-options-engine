"""Strike ranking is code arithmetic. No broker and no database."""

import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from athena_exec.options import bs_delta, discovery_params, rv_multiple, target_otm_pct
from athena_settings.model import SettingsPayload, standard_payload
from athena_settings.model import TacticFlags
from athena_strategy.agents import _trail_context
from athena_strategy.screener import (
    Choice,
    basis_block,
    build_line,
    prompt_sides,
    rank_choices,
    render_table,
    stock_basis,
)

TODAY = date(2026, 10, 1)
NEAR = date(2026, 11, 5)
FAR = date(2026, 11, 20)


def _settings():
    return standard_payload()


def _puts(expiry: date, strikes: range, ask: Decimal | None = Decimal("2")) -> list[Choice]:
    return [Choice(expiry, "P", Decimal(strike), ask) for strike in strikes]


class ScreenerMathTest(unittest.TestCase):
    def test_a_null_ask_is_dropped_and_a_missing_sigma_keeps_the_row(self) -> None:
        settings = _settings()
        spot = Decimal("100")
        blank = build_line(
            Choice(NEAR, "P", Decimal("92"), None),
            symbol="ZZT",
            spot=spot,
            today=TODAY,
            sigma=None,
            source="otm_band",
            multiplier=Decimal(100),
            avg_price=None,
            earnings_in=None,
        )
        self.assertIsNone(blank)
        rows = rank_choices(
            _puts(NEAR, range(90, 96)),
            symbol="ZZT",
            spot=spot,
            today=TODAY,
            settings=settings,
            purpose="csp",
            sigma=None,
            source="otm_band",
            target_otm=Decimal("8"),
        )
        self.assertTrue(rows)
        self.assertIsNone(rows[0].delta)
        self.assertEqual(rows[0].delta_source, "otm_band")

    def test_black_scholes_delta_matches_the_atm_band(self) -> None:
        spot = Decimal("100")
        call = bs_delta(spot, spot, 35, 0.45, "C")
        put = bs_delta(spot, spot, 35, 0.45, "P")
        deep = bs_delta(spot, Decimal("80"), 35, 0.45, "P")
        self.assertGreater(call, Decimal("0.50"))
        self.assertLess(call, Decimal("0.56"))
        self.assertGreater(put, Decimal("-0.50"))
        self.assertLess(put, Decimal("-0.44"))
        # The booklet's −0.10 for an 0.8×spot put is approximate. The r=0 formula is about −0.047.
        self.assertGreater(deep, Decimal("-0.06"))
        self.assertLess(deep, Decimal("-0.03"))

    def test_an_empty_target_expiry_falls_through(self) -> None:
        settings = _settings()
        spot = Decimal("100")
        sigma = 0.45
        width, _note = target_otm_pct(Decimal("45"), settings.strategy, 35)
        choices = [
            Choice(NEAR, "P", Decimal("99"), Decimal("3")),
            Choice(NEAR, "P", Decimal("50"), Decimal("0.2")),
            Choice(FAR, "P", Decimal("91"), Decimal("2")),
        ]
        rows = rank_choices(
            choices,
            symbol="ZZT",
            spot=spot,
            today=TODAY,
            settings=settings,
            purpose="csp",
            sigma=sigma,
            source="rv_mult",
            target_otm=width,
        )
        self.assertTrue(rows)
        self.assertEqual(rows[0].expiry, FAR)

    def test_two_runs_keep_the_same_order_and_keys(self) -> None:
        settings = _settings()
        spot = Decimal("100")
        width, _note = target_otm_pct(Decimal("52"), settings.strategy, 34)
        sigma = 0.546
        choices = _puts(NEAR, range(85, 98))
        calls = [Choice(NEAR, "C", Decimal(strike), Decimal("1.5")) for strike in range(102, 115)]
        first = rank_choices(
            choices, symbol="ZZT", spot=spot, today=TODAY, settings=settings,
            purpose="csp", sigma=sigma, source="rv_mult", target_otm=width,
        )
        second = rank_choices(
            choices, symbol="ZZT", spot=spot, today=TODAY, settings=settings,
            purpose="csp", sigma=sigma, source="rv_mult", target_otm=width,
        )
        self.assertEqual([row.rank_key for row in first], [row.rank_key for row in second])
        self.assertEqual([row.strike for row in first], [row.strike for row in second])
        covered = rank_choices(
            calls, symbol="ZZT", spot=spot, today=TODAY, settings=settings,
            purpose="cc", sigma=sigma, source="rv_mult", target_otm=width, avg_price=Decimal("90"),
        )
        strangle = rank_choices(
            choices + calls, symbol="ZZT", spot=spot, today=TODAY, settings=settings,
            purpose="strangle_put", sigma=sigma, source="rv_mult", target_otm=width,
        )
        self.assertEqual(first[0].rank_key[0], abs(abs(first[0].delta) - Decimal(str(settings.strategy.csp_delta_target))))
        self.assertEqual(covered[0].rank_key[0], covered[0].strike)
        self.assertEqual(strangle[0].rank_key[0], abs(abs(strangle[0].delta) - Decimal(str(settings.strategy.strangle_delta_target))))

    def test_each_prompt_row_has_the_five_figures(self) -> None:
        settings = _settings()
        rows = rank_choices(
            _puts(NEAR, range(90, 96)),
            symbol="ZZT",
            spot=Decimal("100"),
            today=TODAY,
            settings=settings,
            purpose="csp",
            sigma=None,
            source="otm_band",
            target_otm=Decimal("8"),
        )
        text = render_table(rows, "puts")
        for line in text.splitlines()[2:-1]:
            self.assertIn(str((NEAR - TODAY).days), line)
            self.assertIn("%", line)
            self.assertIn("unknown", line)
            self.assertRegex(line, r"\d")

    def test_liquidity_stays_unknown_without_a_quote_field(self) -> None:
        line = build_line(
            Choice(NEAR, "P", Decimal("92"), Decimal("2")),
            symbol="ZZT",
            spot=Decimal("100"),
            today=TODAY,
            sigma=None,
            source="otm_band",
            multiplier=Decimal(100),
            avg_price=None,
            earnings_in=None,
        )
        self.assertIsNotNone(line)
        self.assertEqual(line.liquidity, "unknown")

    def _priced(self, bid: Decimal | None, limit: int = 20):
        return build_line(
            Choice(NEAR, "P", Decimal("92"), Decimal("2"), bid),
            symbol="ZZT",
            spot=Decimal("100"),
            today=TODAY,
            sigma=None,
            source="otm_band",
            multiplier=Decimal(100),
            avg_price=None,
            earnings_in=None,
            max_spread_pct=limit,
        )

    def test_a_tight_bid_stays_quoted(self) -> None:
        line = self._priced(Decimal("1.9"))
        self.assertIsNotNone(line)
        self.assertEqual(line.liquidity, "quoted")

    def test_a_wide_spread_leaves_the_menu(self) -> None:
        self.assertIsNone(self._priced(Decimal("1")))
        kept = self._priced(Decimal("1"), 0)
        self.assertIsNotNone(kept)
        self.assertEqual(kept.liquidity, "quoted")

    def test_rv_tiers_pick_different_put_distances(self) -> None:
        settings = _settings()
        spot = Decimal("100")
        expiry = date(2026, 11, 4)
        bands = {
            Decimal("52"): (Decimal("6.5"), Decimal("9.5")),
            Decimal("25"): (Decimal("4"), Decimal("6")),
            Decimal("16"): (Decimal("3.5"), Decimal("6")),
        }
        picked = []
        for rv, (low, high) in bands.items():
            width, note = target_otm_pct(rv, settings.strategy, 34)
            self.assertIsNone(note)
            from athena_exec.options import rv_multiple

            sigma = float(rv_multiple(rv, settings.strategy) * rv / Decimal(100))
            rows = rank_choices(
                _puts(expiry, range(70, 100)),
                symbol="ZZT",
                spot=spot,
                today=TODAY,
                settings=settings,
                purpose="csp",
                sigma=sigma,
                source="rv_mult",
                target_otm=width,
            )
            self.assertGreaterEqual(rows[0].otm_pct, low)
            self.assertLessEqual(rows[0].otm_pct, high)
            picked.append(rows[0].otm_pct)
        self.assertEqual(len(set(picked)), 3)

    def test_booklet_widths_match_within_three_tenths(self) -> None:
        settings = _settings()
        expected = (
            (Decimal("51.95"), 30, Decimal("7.8")),
            (Decimal("51.95"), 45, Decimal("9.6")),
            (Decimal("25.28"), 30, Decimal("4.9")),
            (Decimal("25.28"), 45, Decimal("6.0")),
            (Decimal("16.41"), 30, Decimal("4.2")),
            (Decimal("16.41"), 45, Decimal("5.2")),
        )
        for rv, dte, booklet in expected:
            width, note = target_otm_pct(rv, settings.strategy, dte)
            self.assertIsNone(note)
            self.assertLess(abs(width - booklet), Decimal("0.3"))

    def test_a_quiet_name_clamps_to_the_floor(self) -> None:
        settings = _settings()
        width, note = target_otm_pct(Decimal("8"), settings.strategy, 34)
        self.assertEqual(width, Decimal(settings.strategy.otm_floor_pct))
        self.assertEqual(note, "floor")

    def test_settings_read_the_band_and_the_old_cut_is_gone(self) -> None:
        settings = _settings()
        self.assertEqual(settings.strategy.chain_band_pct, 25)
        self.assertEqual(discovery_params(settings.strategy), (8, 25))
        width, note = target_otm_pct(Decimal("8"), settings.strategy, 34)
        self.assertEqual(width, Decimal(settings.strategy.otm_floor_pct))
        self.assertEqual(note, "floor")
        ceiling, high_note = target_otm_pct(Decimal("200"), settings.strategy, 45)
        self.assertEqual(ceiling, Decimal(settings.strategy.otm_ceiling_pct))
        self.assertEqual(high_note, "ceiling")

    def test_a_stored_row_keeps_its_ceiling_until_risk_saves_one(self) -> None:
        raw = standard_payload().model_dump()
        raw["strategy"]["otm_ceiling_pct"] = 20
        raw["risk"].pop("otm_ceiling_pct")
        raw["risk"].pop("otm_floor_pct")
        loaded = SettingsPayload.model_validate(raw)
        self.assertEqual(loaded.risk.otm_ceiling_pct, 20)
        self.assertEqual(loaded.risk.otm_floor_pct, raw["strategy"]["otm_floor_pct"])

    def test_a_high_rv_follows_vol_when_the_ceiling_is_open(self) -> None:
        settings = _settings()
        clamped, note = target_otm_pct(Decimal("200"), settings, 90)
        self.assertEqual(note, "ceiling")
        self.assertEqual(clamped, Decimal(settings.risk.otm_ceiling_pct))
        opened = settings.model_copy(update={"risk": settings.risk.model_copy(update={"otm_ceiling_pct": 0})})
        width, open_note = target_otm_pct(Decimal("200"), opened, 90)
        self.assertIsNone(open_note)
        self.assertGreater(width, Decimal("40"))

    def test_the_rv_band_and_the_wing_factor_come_from_risk(self) -> None:
        settings = _settings()
        self.assertEqual(rv_multiple(Decimal("32"), settings), Decimal("1.35"))
        high_band = settings.model_copy(
            update={"risk": settings.risk.model_copy(update={"rv_band_high_pct": 30})}
        )
        self.assertEqual(rv_multiple(Decimal("32"), high_band), Decimal("1.05"))
        wider = settings.model_copy(update={"risk": settings.risk.model_copy(update={"wing_z": 1.0})})
        base, base_note = target_otm_pct(Decimal("30"), settings, 35)
        doubled, wide_note = target_otm_pct(Decimal("30"), wider, 35)
        self.assertIsNone(base_note)
        self.assertIsNone(wide_note)
        self.assertLess(abs(doubled - base * 2), Decimal("0.05"))
        old = "strangle_" + "otm_pct"
        root = Path(__file__).resolve().parents[3]
        hits = []
        for path in root.rglob("*.py"):
            if "site-packages" in path.parts or ".venv" in path.parts:
                continue
            if old in path.read_text(encoding="utf-8", errors="ignore"):
                hits.append(str(path))
        self.assertEqual(hits, [])

    def test_a_covered_call_below_cost_is_named(self) -> None:
        spot = Decimal("100")
        choices = [Choice(NEAR, "C", Decimal("105"), Decimal("1.5"))]
        self.assertEqual(basis_block(choices, spot, Decimal("140")), "cc_no_strike_above_basis")
        self.assertIsNone(basis_block(choices, spot, Decimal("90")))
        rows = rank_choices(
            choices,
            symbol="ZZT",
            spot=spot,
            today=TODAY,
            settings=_settings(),
            purpose="cc",
            sigma=0.45,
            source="rv_mult",
            target_otm=Decimal("8"),
            avg_price=Decimal("140"),
        )
        self.assertEqual(rows, [])
        trail = _trail_context(
            {"cc_block": "cc_no_strike_above_basis", "delta_source": "rv_mult"},
            TacticFlags(),
            "P1",
            {},
        )
        self.assertEqual(trail["cc_block"], "cc_no_strike_above_basis")
        self.assertEqual(trail["delta_source"], "rv_mult")

    def test_a_strangle_table_appears_when_its_strikes_differ(self) -> None:
        expiry = date(2026, 11, 4)
        rows = []
        for strike in range(80, 100):
            rows.append((expiry, "P", Decimal(strike), Decimal("2"), None, None, None))
        for strike in range(101, 121):
            rows.append((expiry, "C", Decimal(strike), Decimal("2"), None, None, None))

        class _Cursor:
            def fetchall(self):
                return rows

        class _Conn:
            def execute(self, sql, params=None):
                return _Cursor()

        class _Reading:
            rv20 = Decimal("52")
            earnings_in = None

        puts, calls, _note, block, _source = prompt_sides(
            _Conn(), _settings(), "", "ZZT", _Reading(), None, Decimal("100"), TODAY,
        )
        self.assertIsNone(block)
        self.assertIn("Listed puts", puts)
        self.assertIn("Listed strangle puts", puts)
        self.assertIn("Listed calls", calls)
        self.assertIn("Listed strangle calls", calls)

    def test_a_replay_cost_does_not_set_the_live_floor(self) -> None:
        class _Cursor:
            def __init__(self, row) -> None:
                self.row = row

            def fetchone(self):
                return self.row

        class _Conn:
            def __init__(self, replaying: bool) -> None:
                self.replaying = replaying
                self.simulated = None

            def execute(self, sql, params=None):
                if "sim_runs" in sql:
                    return _Cursor((1,) if self.replaying else None)
                self.simulated = params[-1]
                if params[-1]:
                    return _Cursor((Decimal("10"), Decimal("100")))
                return _Cursor((Decimal("99"), Decimal("100")))

        live = _Conn(False)
        replay = _Conn(True)
        self.assertEqual(stock_basis(live, "ZZTEST", "zzt"), Decimal("99"))
        self.assertFalse(live.simulated)
        self.assertEqual(stock_basis(replay, "ZZTEST", "zzt"), Decimal("10"))
        self.assertTrue(replay.simulated)


if __name__ == "__main__":
    unittest.main()
