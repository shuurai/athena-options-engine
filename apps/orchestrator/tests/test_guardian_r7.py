"""R7 midday regime (booklet 14, §13.4/§13.9/§24/§26; D67–D109).

Covers: the two-row post-D109 matrix and its closed vocabularies, D106 per-row
required axes, D73 unknown-with-reason, D105 volume removal, the VIX state
derivations, D111 dual-source reads with legacy `ib-vix` compatibility,
`regime_labels` writes, D107 prompt-injection gating, the read-only cards,
migration 055, the loop/settings wiring that keeps Run C out of
``EXECUTED_RUNS``, and the D109 10Y retirement invariants (§26).
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from psycopg.types.json import Jsonb

from athena_marketdata import vixfeed
from athena_orchestrator import guardian_regime as gr
from athena_orchestrator import guardian_schedule as gs
from athena_strategy import agents as ag
from athena_strategy import regime as rg

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _FakeStore:
    """Routes by SQL shape, because build_axes fans out to six readers."""

    def __init__(self, intraday=None, prior=None, news=None, vix=None, label=None):
        self.intraday = intraday or {}
        self.prior = prior or {}
        self.news = news or []
        self.vix = vix or {}
        self.label = label
        self.calls: list[tuple[str, tuple]] = []
        self.commits = 0

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.calls.append((flat, params))
        if "INSERT INTO regime_labels" in flat:
            return _Cursor([])
        if "INSERT INTO market_bars" in flat:
            return _Cursor([])
        if "SELECT label, close_bias, inputs" in flat:   # agents._regime_line
            if not self.label:
                return _Cursor([])
            day, run, label, bias, _ev, inputs, _ts = self.label
            return _Cursor([(label, bias, inputs)])
        if "FROM regime_labels" in flat:
            return _Cursor([self.label] if self.label else [])
        if "FROM news_events" in flat:
            return _Cursor([(kind,) for kind in self.news])
        if "::date < %s" in flat:                       # _PRIOR_CLOSE_SQL
            level = self.prior.get(params[0])
            return _Cursor([(level,)] if level is not None else [])
        if "source IN (%s, %s)" in flat:               # vixfeed.vix_closes
            rows = []
            for item in self.vix.get(params[0], []):
                day, level = item[:2]
                source = item[2] if len(item) > 2 else params[1]
                if source in params[1:3]:
                    rows.append((day, level, source))
            return _Cursor(rows)
        if "source = 'ib-delayed'" in flat:             # _INTRADAY_SQL
            return _Cursor([(None, value) for value in self.intraday.get(params[0], [])])
        return _Cursor([])

    def commit(self):
        self.commits += 1


def _series(levels, start=date(2026, 11, 16)):
    return [(start + timedelta(days=index), level) for index, level in enumerate(levels)]


def _slot(day=date(2026, 11, 24), run="C"):
    return gs.RunSlot(run=run, trade_day=day, moment=gs.run_moments(day)[run])


# ---------------------------------------------------------------------------
# Pure matrix: closed vocabularies
# ---------------------------------------------------------------------------


class TestVocabulary(unittest.TestCase):
    def test_labels_match_the_d109_vocabulary(self):
        """§26.4: 7 -> 4; three rates-dependent labels retired."""
        self.assertEqual(rg.LABELS, (
            "distribution_into_strength", "regime_shift", "dead_cat",
            "insufficient_data",
        ))

    def test_close_bias_is_the_ratified_four(self):
        self.assertEqual(rg.CLOSE_BIAS, (
            "risk_off_into_close", "bounce_likely_fades", "neutral", "insufficient_data",
        ))

    def test_every_row_label_is_known(self):
        for label, bias, _required in rg.ROWS:
            self.assertIn(label, rg.LABELS)
            self.assertIn(bias, rg.CLOSE_BIAS)
            self.assertNotEqual(label, "insufficient_data")

    def test_every_row_has_a_required_axis(self):
        """A row with no required axes would fire unconditionally."""
        for label, _bias, required in rg.ROWS:
            self.assertTrue(required, label)


# ---------------------------------------------------------------------------
# Pure matrix: each row reachable, and only with its full required set
# ---------------------------------------------------------------------------


class TestMatrixRows(unittest.TestCase):
    def test_dead_cat(self):
        axes = rg.Axes(stock="up", news="narrative_only", vix_state="falling")
        self.assertEqual(rg.evaluate(axes).label, "dead_cat")

    def test_first_matching_row_wins(self):
        """distribution_into_strength is ordered first and wins over dead_cat."""
        axes = rg.Axes(stock="up", stock_reversed=True, news="narrative_only",
                       vix_state="falling")
        result = rg.evaluate(axes)
        self.assertEqual(result.label, "distribution_into_strength")
        self.assertEqual(result.close_bias, "bounce_likely_fades")

    def test_regime_shift_is_unreachable_in_this_batch(self):
        """No axes combination produces it: known gap, not a silent bug (§24.6)."""
        combos = [rg.Axes()] + [
            rg.Axes(stock=stock, gold=gold, vix_state=vix,
                    news=news, stock_reversed=rev)
            for stock in ("up", "down", "flat")
            for gold in ("up", "down", None)
            for vix in ("high_rising", "early_high_fell_above_30", "falling", "quiet", None)
            for news in ("narrative_only", "regime_shift", "none")
            for rev in (True, False, None)
        ]
        self.assertNotIn("regime_shift", {rg.evaluate(c).label for c in combos})


# ---------------------------------------------------------------------------
# D106: insufficient_data is per candidate row, not global
# ---------------------------------------------------------------------------


class TestPerRowRequiredAxes(unittest.TestCase):
    def test_distribution_fires_on_two_axes_only(self):
        """§13.4 marks gold/VIX as '-' for this row, so they must not block it."""
        axes = rg.Axes(stock_reversed=True, news="narrative_only")
        self.assertIsNone(axes.gold)
        self.assertIsNone(axes.vix_state)
        self.assertEqual(rg.evaluate(axes).label, "distribution_into_strength")

    def test_required_axis_unknown_blocks_only_that_row(self):
        axes = rg.Axes(stock_reversed=True)  # news unknown
        result = rg.evaluate(axes)
        self.assertEqual(result.label, "insufficient_data")
        row = next(r for r in result.evidence["rows"]
                   if r["label"] == "distribution_into_strength")
        self.assertFalse(row["matched"])
        self.assertEqual(row["blocked_by"], ["news"])

    def test_mismatch_is_not_reported_as_blocked(self):
        """A known-but-wrong axis fails the row without claiming "unknown"."""
        axes = rg.Axes(stock_reversed=False, news="narrative_only")
        result = rg.evaluate(axes)
        row = next(r for r in result.evidence["rows"]
                   if r["label"] == "distribution_into_strength")
        self.assertFalse(row["matched"])
        self.assertEqual(row["blocked_by"], [])

    def test_flat_and_quiet_are_measured_not_unknown(self):
        axes = rg.Axes(stock="flat", gold="flat", vix_state="quiet")
        self.assertNotIn("stock n/a (no intraday print for the regime index)",
                         rg._reasons(axes))


# ---------------------------------------------------------------------------
# D73 / D105: gaps carry a fixed in-place reason
# ---------------------------------------------------------------------------


class TestReasonsAndVolume(unittest.TestCase):
    def test_empty_axes_lists_every_gap_reason(self):
        result = rg.evaluate(rg.Axes())
        self.assertEqual(result.label, "insufficient_data")
        self.assertIn(rg.REASONS["stock"], result.evidence["reasons"])
        self.assertIn(rg.REASONS["vix_term"], result.evidence["reasons"])
        self.assertNotIn("rates", rg.REASONS)

    def test_reasons_are_stable_and_deduplicated(self):
        reasons = rg._reasons(rg.Axes())
        self.assertEqual(reasons, list(dict.fromkeys(reasons)))
        self.assertEqual(reasons, rg._reasons(rg.Axes()))

    def test_volume_is_never_a_required_axis(self):
        """D105: removed from §13.4 because upsert_delayed() stores volume=0."""
        for _label, _bias, required in rg.ROWS:
            self.assertNotIn("volume", required)

    def test_inputs_record_volume_as_none_with_no_invented_number(self):
        inputs = rg.evaluate(rg.Axes(stock="up")).inputs
        self.assertIsNone(inputs["volume"])
        self.assertTrue(inputs["thresholds_unratified"])

    def test_vix_hits_counts_only_known_dimensions(self):
        """D77/D109: 0-3 display-only counter, never a gate."""
        self.assertEqual(rg._hit_count(rg.Axes()), 0)
        axes = rg.Axes(vix_slope="flat", vix_term="contango",
                       single_name_iv="normal")
        self.assertEqual(rg._hit_count(axes), 3)
        self.assertEqual(rg.evaluate(axes).inputs["vix_hits"], 3)


# ---------------------------------------------------------------------------
# Regime value object
# ---------------------------------------------------------------------------


class TestRegimeObject(unittest.TestCase):
    def test_is_real_false_only_for_insufficient_data(self):
        self.assertFalse(rg.evaluate(rg.Axes()).is_real)
        self.assertTrue(rg.evaluate(rg.Axes(stock_reversed=True,
                                            news="narrative_only")).is_real)

    def test_line_names_the_label_and_bias(self):
        line = rg.evaluate(rg.Axes(stock_reversed=True, news="narrative_only")).line()
        self.assertIn("distribution_into_strength", line)
        self.assertIn("bounce_likely_fades", line)
        self.assertIn("Midday Regime", line)
        self.assertNotIn("午間", line)

    def test_line_survives_string_percentages_from_jsonb(self):
        regime = rg.Regime(label="dead_cat", close_bias="bounce_likely_fades",
                           inputs={"vix_level": "21.5"}, evidence={})
        line = regime.line()
        self.assertIn("VIX 21.50", line)
        self.assertNotIn("10Y", line)

    def test_line_falls_back_when_nothing_is_known(self):
        self.assertIn("no live inputs", rg.evaluate(rg.Axes()).line())

    def test_evaluate_rejects_non_axes(self):
        with self.assertRaises(TypeError):
            rg.evaluate({"stock": "up"})


# ---------------------------------------------------------------------------
# VIX derivations (pure)
# ---------------------------------------------------------------------------


class TestVixState(unittest.TestCase):
    def test_high_rising(self):
        self.assertEqual(vixfeed.vix_state(_series([21.0, 21.5, 22.0, 23.0, 24.0])),
                         "high_rising")

    def test_early_high_fell_above_30(self):
        self.assertEqual(
            vixfeed.vix_state(_series([28.0, 34.0, 33.0, 32.0, 31.0])),
            "early_high_fell_above_30",
        )

    def test_falling(self):
        self.assertEqual(vixfeed.vix_state(_series([24.0, 23.0, 22.0, 21.0, 20.0])),
                         "falling")

    def test_quiet_is_measured_not_none(self):
        self.assertEqual(vixfeed.vix_state(_series([15.0, 14.8, 14.9, 15.0, 15.1])),
                         "quiet")

    def test_a_climb_below_the_high_level_is_not_called_quiet(self):
        """+15% to 19.9 is rising, not quiet; no row covers it, so it is a sentinel."""
        self.assertEqual(vixfeed.vix_state(_series([17.2, 18.0, 19.0, 19.5, 19.9])),
                         "rising_not_high")

    def test_none_with_too_few_points(self):
        self.assertIsNone(vixfeed.vix_state(_series([18.0])))
        self.assertIsNone(vixfeed.vix_state([]))

    def test_slope_rising_from_low_requires_a_low_start(self):
        self.assertEqual(
            vixfeed.slope_state(_series([14.0, 14.2, 14.5, 15.0, 16.0])),
            "rising_from_low",
        )
        # first close sits far above the window low -> not "from low"
        self.assertEqual(
            vixfeed.slope_state(_series([30.0, 25.0, 26.0, 27.0, 32.0])),
            "flat",
        )

    def test_term_states(self):
        self.assertEqual(
            vixfeed.term_state({"VIX": 18.0, "VIX9D": 19.0, "VIX3M": 17.0, "VIX1Y": 16.5}),
            "front_end_hot",
        )
        self.assertEqual(
            vixfeed.term_state({"VIX": 16.0, "VIX3M": 17.0, "VIX1Y": 18.0}), "contango",
        )
        self.assertEqual(
            vixfeed.term_state({"VIX": 22.0, "VIX3M": 20.0}), "inversion",
        )
        self.assertEqual(vixfeed.term_state({"VIX": 18.0, "VIX3M": 18.1}), "flat")

    def test_term_is_none_without_three_month(self):
        self.assertIsNone(vixfeed.term_state({"VIX": 18.0}))


# ---------------------------------------------------------------------------
# D104: ib-vix is inert for every existing reader
# ---------------------------------------------------------------------------


class TestVixStorage(unittest.TestCase):
    def test_upsert_uses_ib_vix_source_and_zero_volume(self):
        store = _FakeStore()
        written = vixfeed.store_vix_daily(
            store, "vix", [{"date": "2026-11-24", "close": 18.5}],
        )
        self.assertEqual(written, 1)
        sql, params = store.calls[0]
        self.assertIn("INSERT INTO market_bars", sql)
        self.assertIn("ON CONFLICT (symbol, ts, source) DO UPDATE", sql)
        self.assertEqual(params[0], "VIX")          # upper-cased
        self.assertEqual(params[5], 18.5)
        self.assertEqual(params[6], "ib-vix")

    def test_rows_without_a_usable_close_are_skipped(self):
        store = _FakeStore()
        written = vixfeed.store_vix_daily(store, "VIX", [
            {"date": "2026-11-24", "close": None},
            {"date": "2026-11-24", "close": 0},
            {"date": "not-a-date", "close": 18.0},
        ])
        self.assertEqual(written, 0)
        self.assertEqual(store.calls, [])

    def test_blank_symbol_writes_nothing(self):
        store = _FakeStore()
        self.assertEqual(vixfeed.store_vix_daily(store, "  ", [{"close": 1}]), 0)
        self.assertEqual(store.calls, [])

    def test_vix_closes_reads_daily_and_legacy_sources(self):
        store = _FakeStore(vix={"VIX": _series([18.0, 19.0])})
        vixfeed.vix_closes(store, "VIX", 5)
        sql, params = store.calls[0]
        self.assertIn("source IN (%s, %s)", sql)
        self.assertEqual(params, ("VIX", "ib-daily", "ib-vix"))

    def test_vix_closes_keeps_only_the_newest_n(self):
        store = _FakeStore(vix={"VIX": _series([1.0, 2.0, 3.0, 4.0, 5.0])})
        self.assertEqual([lvl for _d, lvl in vixfeed.vix_closes(store, "VIX", 2)],
                         [4.0, 5.0])

    def test_latest_levels_reads_every_declared_series(self):
        store = _FakeStore(vix={
            "VIX": _series([18.0]), "VIX9D": _series([19.0]),
            "VIX3M": _series([17.5]), "VIX1Y": _series([16.0]),
        })
        self.assertEqual(vixfeed.latest_levels(store), {
            "VIX": 18.0, "VIX9D": 19.0, "VIX3M": 17.5, "VIX1Y": 16.0,
        })

    def test_daily_bars_still_excludes_ib_vix(self):
        """D104: rails / RV20 / the 52-week band never see VIX rows."""
        text = (ROOT / "packages/marketdata/src/athena_marketdata/pricing.py").read_text()
        body = text.split("def daily_bars", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("source IN ('ib-daily', 'ib-delayed')", body)
        self.assertNotIn("ib-vix", body)


# ---------------------------------------------------------------------------
# Axis assembly from the store
# ---------------------------------------------------------------------------


class TestBuildAxes(unittest.TestCase):
    def test_direction_and_reversal(self):
        store = _FakeStore(intraday={"SPY": [99.0, 101.0]}, prior={"SPY": 100.0})
        axes = gr.build_axes(store, date(2026, 11, 24))
        self.assertEqual(axes.stock, "up")
        self.assertTrue(axes.stock_reversed)  # early red -> now green

    def test_sub_deadband_move_is_flat_not_invented(self):
        store = _FakeStore(intraday={"SPY": [100.001]}, prior={"SPY": 100.0})
        self.assertEqual(gr.build_axes(store, date(2026, 11, 24)).stock, "flat")

    def test_single_print_cannot_claim_a_reversal(self):
        store = _FakeStore(intraday={"SPY": [101.0]}, prior={"SPY": 100.0})
        self.assertIsNone(gr.build_axes(store, date(2026, 11, 24)).stock_reversed)

    def test_no_intraday_print_leaves_stock_unknown(self):
        store = _FakeStore(prior={"SPY": 100.0})
        axes = gr.build_axes(store, date(2026, 11, 24))
        self.assertIsNone(axes.stock)
        self.assertIsNone(axes.gold)

    def test_gold_uses_gld(self):
        store = _FakeStore(intraday={"GLD": [190.0]}, prior={"GLD": 200.0})
        self.assertEqual(gr.build_axes(store, date(2026, 11, 24)).gold, "down")

    def test_unmeasured_axes_stay_none(self):
        axes = gr.build_axes(_FakeStore(), date(2026, 11, 24))
        self.assertIsNone(axes.single_name_iv)
        self.assertFalse(hasattr(axes, "rates"))
        self.assertFalse(hasattr(axes, "rates_divergence"))

    def test_news_maps_earnings_to_narrative_only(self):
        store = _FakeStore(news=["EARNINGS_RELEASE", "GENERIC"])
        self.assertEqual(gr.build_axes(store, date(2026, 11, 24)).news, "narrative_only")

    def test_fed_decision_is_not_downgraded_to_narrative_only(self):
        store = _FakeStore(news=["EARNINGS_RELEASE", "FED_RATE_DECISION"])
        self.assertEqual(gr.build_axes(store, date(2026, 11, 24)).news, "none")

    def test_quiet_day_is_none_not_narrative(self):
        self.assertEqual(gr.build_axes(_FakeStore(), date(2026, 11, 24)).news, "none")

    def test_vix_dimensions_feed_the_axes(self):
        store = _FakeStore(vix={
            "VIX": _series([21.0, 21.5, 22.0, 23.0, 24.0]),
            "VIX9D": _series([25.0]), "VIX3M": _series([23.0]),
            "VIX1Y": _series([22.0]),
        })
        axes = gr.build_axes(store, date(2026, 11, 24))
        self.assertEqual(axes.vix_state, "high_rising")
        self.assertEqual(axes.vix_slope, "rising_from_low")  # 21 -> 24 is +14% off the low
        self.assertEqual(axes.vix_term, "front_end_hot")
        self.assertEqual(axes.numbers["vix_level"], 24.0)

    def test_observe_is_pure_evaluate_over_axes(self):
        store = _FakeStore(intraday={"SPY": [101.0]}, prior={"SPY": 100.0},
                           news=["EARNINGS_RELEASE"])
        self.assertEqual(gr.observe(store, date(2026, 11, 24)).label,
                         "insufficient_data")


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestWriteLabel(unittest.TestCase):
    def setUp(self):
        self.store = _FakeStore()
        self.regime = rg.evaluate(rg.Axes(stock_reversed=True, news="narrative_only"))

    def test_sql_targets_regime_labels(self):
        gr.write_label(self.store, "U1", date(2026, 11, 24), self.regime)
        sql, _params = self.store.calls[0]
        self.assertIn("INSERT INTO regime_labels", sql)
        self.assertIn("ON CONFLICT (account_id, trade_day, run) DO UPDATE", sql)

    def test_params_carry_account_day_run_label_bias(self):
        gr.write_label(self.store, "U1", date(2026, 11, 24), self.regime)
        params = self.store.calls[0][1]
        self.assertEqual(params[0], "U1")
        self.assertEqual(params[1], date(2026, 11, 24))
        self.assertEqual(params[2], "C")
        self.assertEqual(params[3], "distribution_into_strength")
        self.assertEqual(params[4], "bounce_likely_fades")

    def test_jsonb_columns_use_the_adapter(self):
        gr.write_label(self.store, "U1", date(2026, 11, 24), self.regime)
        params = self.store.calls[0][1]
        self.assertIsInstance(params[5], Jsonb)
        self.assertIsInstance(params[6], Jsonb)

    def test_evidence_records_when_it_was_observed(self):
        gr.write_label(self.store, "U1", date(2026, 11, 24), self.regime)
        evidence = self.store.calls[0][1][5].obj
        self.assertIn("observed_at", evidence)
        self.assertEqual(evidence["matched_rule"], "distribution_into_strength")

    def test_run_once_writes_exactly_one_row_and_commits(self):
        store = _FakeStore(intraday={"SPY": [99.0, 101.0]}, prior={"SPY": 100.0},
                           news=["EARNINGS_RELEASE"])
        regime = gr.run_once(store, "U1", _slot())
        self.assertEqual(regime.label, "distribution_into_strength")
        writes = [c for c in store.calls if "INSERT INTO regime_labels" in c[0]]
        self.assertEqual(len(writes), 1)
        self.assertEqual(store.commits, 1)


class TestLatestLabel(unittest.TestCase):
    def _row(self, label="dead_cat"):
        return (date(2026, 11, 24), "C", label, "bounce_likely_fades",
                {"matched_rule": label}, {"vix_level": 18.0},
                datetime(2026, 11, 24, 17, 30, tzinfo=UTC))

    def test_blank_account_returns_none_without_sql(self):
        store = _FakeStore()
        self.assertIsNone(gr.latest_label(store, ""))
        self.assertEqual(store.calls, [])

    def test_missing_row_returns_none(self):
        self.assertIsNone(gr.latest_label(_FakeStore(), "U1"))

    def test_row_is_mapped_with_is_real(self):
        found = gr.latest_label(_FakeStore(label=self._row()), "U1")
        self.assertEqual(found["label"], "dead_cat")
        self.assertTrue(found["is_real"])

    def test_insufficient_is_not_real(self):
        found = gr.latest_label(_FakeStore(label=self._row("insufficient_data")), "U1")
        self.assertFalse(found["is_real"])


# ---------------------------------------------------------------------------
# D107: prompt injection only for a real label
# ---------------------------------------------------------------------------


class TestPromptLine(unittest.TestCase):
    def _row(self, label):
        return (date(2026, 11, 24), "C", label, "neutral", {},
                {"vix_level": 18.0}, datetime(2026, 11, 24, 17, 30, tzinfo=UTC))

    def test_real_label_becomes_one_line(self):
        line = gr.prompt_line(_FakeStore(label=self._row("dead_cat")), "U1")
        self.assertIn("dead_cat", line)
        self.assertEqual(line.count("\n"), 0)

    def test_insufficient_data_is_not_injected(self):
        self.assertIsNone(
            gr.prompt_line(_FakeStore(label=self._row("insufficient_data")), "U1"),
        )

    def test_no_row_is_not_injected(self):
        self.assertIsNone(gr.prompt_line(_FakeStore(), "U1"))

    def test_store_failure_degrades_to_none(self):
        class _Broken:
            def execute(self, *_a, **_k):
                raise RuntimeError("no table")

        self.assertIsNone(gr.prompt_line(_Broken(), "U1"))

    def test_agents_context_carries_the_same_gate(self):
        """agents reads the table directly so strategy never imports the web layer."""
        row = (date(2026, 11, 24), "C", "dead_cat", "bounce_likely_fades", {},
               {"vix_level": 18.0}, datetime(2026, 11, 24, 17, 30, tzinfo=UTC))
        self.assertIn("dead_cat", ag._regime_line(_FakeStore(label=row), "U1"))
        blind = (date(2026, 11, 24), "C", "insufficient_data", "insufficient_data", {},
                 {}, datetime(2026, 11, 24, 17, 30, tzinfo=UTC))
        self.assertEqual(ag._regime_line(_FakeStore(label=blind), "U1"), "")

    def test_agents_regime_line_never_raises(self):
        class _Broken:
            def execute(self, *_a, **_k):
                raise RuntimeError("no table")

        self.assertEqual(ag._regime_line(_Broken(), "U1"), "")


class TestFillPromptRegime(unittest.TestCase):
    def test_regime_is_appended_when_the_template_lacks_a_slot(self):
        out = ag.fill_prompt("Base prompt", {"regime": "Midday Regime: VIX 18.00.",
                                             "numbers": ""})
        self.assertTrue(out.endswith("Midday Regime: VIX 18.00."))
        self.assertTrue(out.startswith("Base prompt"))

    def test_empty_regime_appends_nothing(self):
        self.assertEqual(ag.fill_prompt("Base prompt", {"regime": ""}), "Base prompt")

    def test_template_placeholder_is_substituted_not_appended(self):
        out = ag.fill_prompt("A {{regime}} B", {"regime": "R"})
        self.assertEqual(out, "A R B")

    def test_numbers_channel_is_unchanged(self):
        out = ag.fill_prompt("Base", {"numbers": "rv 22", "regime": ""})
        self.assertIn("Numbers: rv 22", out)


# ---------------------------------------------------------------------------
# Read-only cards
# ---------------------------------------------------------------------------


class TestRenderCard(unittest.TestCase):
    def _label(self, label="dead_cat", inputs=None):
        return (date(2026, 11, 24), "C", label, "bounce_likely_fades",
                {"matched_rule": label},
                inputs if inputs is not None else {"vix_hits": 1},
                datetime(2026, 11, 24, 17, 30, tzinfo=UTC))

    def test_no_row_renders_nothing(self):
        self.assertEqual(gr.render_card(_FakeStore(), "U1"), "")

    def test_card_names_the_label_and_bias(self):
        html = gr.render_card(_FakeStore(label=self._label()), "U1")
        self.assertIn("dead_cat", html)
        self.assertIn("bounce_likely_fades", html)

    def test_unknown_dimensions_carry_their_reason(self):
        html = gr.render_card(_FakeStore(label=self._label()), "U1")
        self.assertIn("unknown", html)
        self.assertIn("single_name_iv", html)

    def test_known_dimension_is_printed_not_marked_unknown(self):
        html = gr.render_card(
            _FakeStore(label=self._label(inputs={"vix_slope": "flat", "vix_hits": 1})),
            "U1",
        )
        self.assertIn("flat", html)

    def test_vix_hits_are_displayed_as_n_of_3(self):
        """D109/§26.4: denominator follows the three registered dimensions."""
        html = gr.render_card(_FakeStore(label=self._label()), "U1")
        self.assertIn("1/3", html)
        self.assertNotIn("1/4", html)

    def test_store_failure_renders_nothing(self):
        class _Broken:
            def execute(self, *_a, **_k):
                raise RuntimeError("no table")

        self.assertEqual(gr.render_card(_Broken(), "U1"), "")

    def test_dynamic_cells_are_escaped(self):
        html = gr.render_card(
            _FakeStore(label=self._label(inputs={"vix_slope": "<script>x</script>"})), "U1",
        )
        self.assertNotIn("<script>x</script>", html)
        self.assertIn("&lt;script&gt;", html)


# ---------------------------------------------------------------------------
# VIX settings card
# ---------------------------------------------------------------------------


class TestVixCard(unittest.TestCase):
    def test_card_offers_the_manual_sync_action(self):
        from athena_orchestrator import pages as pg

        html = pg._vix_card(_FakeStore(), "U1")
        self.assertIn('name="action" value="sync_vix"', html)
        self.assertIn("Sync VIX Data", html)

    def test_card_lists_every_declared_series(self):
        from athena_orchestrator import pages as pg

        html = pg._vix_card(_FakeStore(), "U1")
        for symbol in vixfeed.VIX_SYMBOLS:
            self.assertIn(f"<td>{symbol}</td>", html)

    def test_card_survives_a_broken_store(self):
        from athena_orchestrator import pages as pg

        class _Broken:
            def execute(self, *_a, **_k):
                raise RuntimeError("no table")

        self.assertIn("Sync VIX Data", pg._vix_card(_Broken(), "U1"))


# ---------------------------------------------------------------------------
# Migration 055 and section 13.5 columns
# ---------------------------------------------------------------------------


class TestMigration055(unittest.TestCase):
    def setUp(self):
        self.path = (ROOT / "packages/store/src/athena_store/migrations"
                     / "055_regime_labels.sql")

    def test_file_exists_and_is_the_only_055(self):
        self.assertTrue(self.path.exists())
        siblings = sorted(p.name for p in self.path.parent.glob("055_*.sql"))
        self.assertEqual(siblings, ["055_regime_labels.sql"])

    def test_columns_match_section_13_5(self):
        text = self.path.read_text()
        for column in ("account_id", "trade_day", "run", "label", "close_bias",
                       "evidence", "inputs", "created_at"):
            self.assertIn(column, text)

    def test_unique_key_is_account_day_run(self):
        self.assertIn("UNIQUE (account_id, trade_day, run)", self.path.read_text())

    def test_run_defaults_to_c(self):
        self.assertIn("run text NOT NULL DEFAULT 'C'", self.path.read_text())

    def test_jsonb_defaults_are_empty_objects(self):
        text = self.path.read_text()
        self.assertEqual(text.count("'{}'::jsonb"), 2)

    def test_comment_has_no_statement_terminator(self):
        """migrate() splits on ';', so a stray one in a comment breaks the file."""
        for line in self.path.read_text().splitlines():
            if line.lstrip().startswith("--"):
                self.assertNotIn(";", line, line)

    def test_registered_in_force_include(self):
        text = (ROOT / "packages/store/pyproject.toml").read_text()
        self.assertIn("055_regime_labels.sql", text)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


class TestWiring(unittest.TestCase):
    def test_run_c_stays_out_of_executed_runs(self):
        self.assertEqual(gs.EXECUTED_RUNS, ("A", "B"))

    def test_next_run_can_target_c_only(self):
        slot = gs.next_run(datetime(2026, 11, 24, 10, 0, tzinfo=UTC), runs=("C",))
        self.assertEqual(slot.run, "C")

    def test_regime_run_constant_is_c(self):
        self.assertEqual(gr.RUN, "C")

    def test_loop_starts_and_stops_the_regime_daemon(self):
        text = (ROOT / "apps/orchestrator/src/athena_orchestrator/loop.py").read_text()
        self.assertIn("start_regime_scheduler(database_url)", text)
        self.assertEqual(text.count("stop_regime_scheduler()"), 2)

    def test_settings_dispatch_handles_sync_vix_before_knob_validation(self):
        text = (ROOT / "apps/orchestrator/src/athena_orchestrator/pages.py").read_text()
        body = text.split("def _post_settings", 1)[1]
        self.assertIn('action == "sync_vix"', body)
        self.assertLess(body.index('action == "sync_vix"'), body.index("ranges = {"))

    def test_vix_card_is_outside_the_main_settings_form(self):
        """/settings nests a form inside a form otherwise — invalid HTML."""
        text = (ROOT / "apps/orchestrator/src/athena_orchestrator/pages.py").read_text()
        body = text.split("def _settings(", 1)[1].split("def _prediction_page", 1)[0]
        self.assertLess(body.index("</form>"), body.index("_vix_card(conn, account)"))

    def test_iris_page_appends_the_regime_card(self):
        text = (ROOT / "apps/orchestrator/src/athena_orchestrator/pages.py").read_text()
        body = text.split("def _iris_page", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("_regime_card(conn, account)", body)
        self.assertIn('f"{regime}"', body)  # actually interpolated, not just fetched

    def test_broker_builds_an_index_contract(self):
        """Indices are not stocks: _qualify_stock only builds Stock/Forex."""
        text = (ROOT / "apps/exec-ib/src/athena_exec/broker.py").read_text()
        helper = text.split("def _index_contract", 1)[1].split("\ndef ", 1)[0]
        body = text.split("def index_history", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("Index(", helper)
        self.assertIn('"CBOE"', helper)
        self.assertIn("_index_contract(", body)
        self.assertIn('"1 day"', body)

    def test_sync_vix_worker_uses_the_readonly_broker_path(self):
        text = (ROOT / "apps/orchestrator/src/athena_orchestrator/pages.py").read_text()
        body = text.split("def _sync_vix", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("IbBroker()", body)
        self.assertIn("broker.disconnect()", body)
        self.assertIn("configured_data_symbols", body)
        self.assertIn("_stored_or_pull", body)
        self.assertIn('client_id("probe")', body)
        self.assertIn("_IB_LOCK", body)
        self.assertIn("_VIX_SYNC_LOCK.acquire(blocking=False)", body)
        self.assertNotIn("store_vix_daily", body)

    def test_regime_daemon_never_touches_executed_runs(self):
        text = (ROOT / "apps/orchestrator/src/athena_orchestrator/guardian_regime.py").read_text()
        self.assertIn('runs=(RUN,)', text)
        self.assertNotIn("runs=EXECUTED_RUNS", text)


# ---------------------------------------------------------------------------
# D109: 10Y / treasury-yield retirement (§26)
# ---------------------------------------------------------------------------


class TestD109Retirement(unittest.TestCase):
    """Every retired invariant is pinned so a future re-introduction fails."""

    RETIRED = ("liquidity_squeeze", "short_cover_bounce", "normal_risk_off")

    def test_labels_are_the_four_d109_values(self):
        self.assertEqual(rg.LABELS, (
            "distribution_into_strength", "regime_shift", "dead_cat",
            "insufficient_data",
        ))

    def test_three_retired_labels_are_gone_from_vocabulary(self):
        for label in self.RETIRED:
            self.assertNotIn(label, rg.LABELS)

    def test_three_retired_labels_are_gone_from_rows(self):
        produced = {label for label, _bias, _req in rg.ROWS}
        for label in self.RETIRED:
            self.assertNotIn(label, produced)

    def test_matrix_has_exactly_two_rows(self):
        self.assertEqual([label for label, _b, _r in rg.ROWS],
                         ["distribution_into_strength", "dead_cat"])

    def test_no_row_requires_rates(self):
        for _label, _bias, required in rg.ROWS:
            self.assertNotIn("rates", required)

    def test_axes_has_no_rates_or_divergence_field(self):
        fields = set(rg.Axes.__dataclass_fields__)
        self.assertNotIn("rates", fields)
        self.assertNotIn("rates_divergence", fields)

    def test_reasons_has_no_rates_key(self):
        self.assertNotIn("rates", rg.REASONS)
        for reason in rg.REASONS.values():
            self.assertNotIn("10Y", reason)
            self.assertNotIn("treasury", reason)

    def test_reasons_loop_and_card_mapping_have_no_rates_entry(self):
        """Both the pure in-place reasons and the card mapping must be clean."""
        text = (ROOT / "packages/strategy-nfr/src/athena_strategy/regime.py").read_text()
        loop = text.split("def _reasons", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn('"rates"', loop)
        card = (ROOT / "apps/orchestrator/src/athena_orchestrator/guardian_regime.py").read_text()
        mapping = card.split("def _reason_for", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn("rates_divergence", mapping)
        self.assertNotIn('"rates"', mapping)

    def test_vix_dimensions_are_three(self):
        self.assertEqual(rg.VIX_DIMENSIONS,
                         ("vix_slope", "vix_term", "single_name_iv"))
        self.assertEqual(len(rg.VIX_DIMENSIONS), 3)

    def test_hit_count_caps_at_three(self):
        axes = rg.Axes(vix_slope="flat", vix_term="contango",
                       single_name_iv="normal")
        self.assertEqual(rg._hit_count(axes), 3)
        self.assertEqual(rg.evaluate(axes).inputs["vix_hits"], 3)

    def test_inputs_have_no_rates_keys(self):
        inputs = rg.evaluate(rg.Axes()).inputs
        self.assertNotIn("rates_10y", inputs)
        self.assertNotIn("rates_divergence", inputs)

    def test_line_ignores_legacy_rates_10y_key(self):
        """§26.6: historical regime_labels rows are neither rewritten nor deleted."""
        legacy = rg.Regime(
            label="dead_cat", close_bias="bounce_likely_fades",
            inputs={"vix_level": "21.5", "rates_10y": "4.25",
                    "rates_divergence": "aligned", "vix_hits": 4},
            evidence={},
        )
        line = legacy.line()
        self.assertIn("VIX 21.50", line)
        self.assertNotIn("10Y", line)
        self.assertNotIn("4.25", line)

    def test_build_axes_never_assigns_rates(self):
        axes = gr.build_axes(_FakeStore(), date(2026, 11, 24))
        self.assertFalse(hasattr(axes, "rates"))
        self.assertFalse(hasattr(axes, "rates_divergence"))

    def test_dimension_labels_have_three_rows(self):
        self.assertEqual(len(gr._DIMENSION_LABELS), 3)
        self.assertNotIn("股债背离", [t for _k, t in gr._DIMENSION_LABELS])

    def test_card_denominator_is_three(self):
        html = gr.render_card(
            _FakeStore(label=self._label(inputs={"vix_hits": 2})), "U1",
        )
        self.assertIn("2/3", html)
        self.assertNotIn("/4", html)

    def test_card_ignores_legacy_rates_keys(self):
        html = gr.render_card(
            _FakeStore(label=self._label(inputs={
                "vix_hits": 2, "rates_10y": "4.25", "rates_divergence": "aligned",
            })), "U1",
        )
        self.assertNotIn("4.25", html)
        self.assertNotIn("股债背离", html)
        self.assertNotIn("10Y", html)

    def test_close_bias_vocabulary_is_unchanged(self):
        """§26.4: CLOSE_BIAS stays four values; two currently have no producer."""
        self.assertEqual(rg.CLOSE_BIAS, (
            "risk_off_into_close", "bounce_likely_fades", "neutral",
            "insufficient_data",
        ))

    def test_no_new_market_data_source_or_migration(self):
        """§26.5: zero migration, zero new source, §6 untouched.

        Research notes in docstrings legitimately name DGS10/TYVIX; what must
        not appear is a *source literal* or a quote/subscription call.
        """
        migrations = list((ROOT / "packages/store/src/athena_store/migrations").glob("0*.sql"))
        # V2-P2/P3 C-20261125-6/7: 056 strategy_events, 057 news_canonical.
        self.assertEqual(len(migrations), 53)
        self.assertTrue((ROOT / "packages/store/src/athena_store/migrations"
                         / "055_regime_labels.sql").exists())
        self.assertEqual(vixfeed.SOURCE, "ib-vix")
        for path in (ROOT / "packages/strategy-nfr/src/athena_strategy/regime.py",
                     ROOT / "apps/orchestrator/src/athena_orchestrator/guardian_regime.py",
                     ROOT / "apps/orchestrator/src/athena_orchestrator/pages.py",
                     ROOT / "packages/marketdata/src/athena_marketdata/vixfeed.py"):
            text = path.read_text()
            self.assertNotIn("'ib-tnx'", text)
            self.assertNotIn('"ib-tnx"', text)
            self.assertNotIn("reqMktData", text)
        pricing = (ROOT / "packages/marketdata/src/athena_marketdata/pricing.py").read_text()
        self.assertIn("source IN ('ib-daily', 'ib-delayed')", pricing)
        self.assertNotIn("ib-tnx", pricing)

    def test_vixfeed_and_broker_are_untouched_by_d109(self):
        """index_history() is the VIX seam, not a 10Y source; it stays."""
        text = (ROOT / "apps/exec-ib/src/athena_exec/broker.py").read_text()
        self.assertEqual(text.count("Index("), 1)
        self.assertIn("def index_history", text)
        self.assertIn('"CBOE"', text)

    def test_regime_shift_is_still_a_declared_gap(self):
        """§26.6: not 10Y logic, so it survives the retirement."""
        self.assertIn("regime_shift", rg.LABELS)
        self.assertNotIn("regime_shift", {l for l, _b, _r in rg.ROWS})

    def _label(self, label="dead_cat", inputs=None):
        return (date(2026, 11, 24), "C", label, "bounce_likely_fades",
                {"matched_rule": label},
                inputs if inputs is not None else {"vix_hits": 1},
                datetime(2026, 11, 24, 17, 30, tzinfo=UTC))


if __name__ == "__main__":
    unittest.main()
