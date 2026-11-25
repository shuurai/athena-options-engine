"""V2-P4: gap audit, weekly aggregation, SMA registry and Strategy 3 shadow.

Covers §28 / §8.4: the deterministic ET calendar (R1), the six gap
categories (Q7), ISO-week aggregation (Q8), the SMA factor registry and the
frozen §22.3 signal semantics (items 1-6, 14). The shadow is pure: every
test here is also a zero-draft assertion by construction — the module has
no conn, no writer and no draft path.
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

from athena_strategy.gap_audit import (
    GapAudit,
    audit_daily,
    CORPORATE_ACTION,
    HALT,
    INTERNAL_GAP,
    IPO_SHORT_HISTORY,
    LONG_TAIL_GAP,
    STATE_COMPLETE,
    STATE_PARTIAL,
    STATE_UNKNOWN,
    ZERO_VOLUME,
)
from athena_strategy.strategy3 import (
    FACTOR_VERSION,
    ShadowRecord,
    _hill,
    _retest_state,
    shadow_history,
)
from athena_strategy.trading_calendar import (
    CALENDAR_VERSION,
    holidays,
    is_session,
    next_sessions,
    sessions,
    sessions_back,
)
from athena_strategy.weekly import WeekBar, weekly_bars
from athena_store import news_read  # noqa: F401  (keeps store import parity)


# ---------------------------------------------------------------- fixtures
START = date(2025, 6, 2)
N_WEEKS = 60
SIGNAL_K = 58  # ISO 2026-W29 in the engineered fixture


def _week_groups():
    all_sess = sessions(START, START + timedelta(days=7 * N_WEEKS))
    weeks: dict[tuple[int, int], list[date]] = {}
    for day in all_sess:
        iso = day.isocalendar()
        weeks.setdefault((iso[0], iso[1]), []).append(day)
    return sorted(weeks.items())


WEEK_KEYS = sorted(_week_groups())


def _base_closes() -> list[Decimal]:
    out = []
    for k in range(len(WEEK_KEYS)):
        if k < 25:
            out.append(Decimal(100) - Decimal(2) * k)  # decline 100 -> 50
        elif k < 38:
            out.append(Decimal(50) + Decimal(100) * (k - 25) / Decimal(13))
        elif k < 42:
            out.append(Decimal(150) - Decimal(5) * (k - 38))  # decay
        else:
            out.append(Decimal(130))  # plateau
    return out


def _build_daily(
    *,
    signal_k: int = SIGNAL_K,
    signal_volume: Decimal = Decimal(3_500_000),
    base_volume: Decimal = Decimal(6_000_000),
    signal_gap_up_open: bool = True,
    drop_session: date | None = None,
) -> list[tuple]:
    wc = _base_closes()
    daily = []
    prev_close = Decimal(100)
    for k, (key, sess) in enumerate(WEEK_KEYS):
        target = wc[k]
        n = len(sess)
        step = (target - prev_close) / max(1, n - 1)
        vol = signal_volume if k == signal_k else base_volume
        for i, day in enumerate(sess):
            if day == drop_session:
                continue
            close = prev_close + step * i if i < n - 1 else target
            if k == signal_k and i == 0 and signal_gap_up_open:
                open_ = prev_close * Decimal("1.06")  # bearish weekly candle
                high = open_ * Decimal("1.002")
                low = min(close, prev_close) * Decimal("0.995")
                daily.append((day, open_, high, low, close, vol))
            elif k == signal_k and i == n - 1:
                open_ = prev_close
                high = max(open_, close) * Decimal("1.002")
                low = close * Decimal("0.985")  # touches the SMA20 band
                daily.append((day, open_, high, low, close, vol))
            else:
                open_ = prev_close
                high = max(open_, close) * Decimal("1.002")
                low = min(open_, close) * Decimal("0.998")
                daily.append((day, open_, high, low, close, vol))
            prev_close = close
    return daily


def _spx(daily, *, rising=True):
    out = []
    for i, row in enumerate(daily):
        px = Decimal(5000) + Decimal(i) if rising else Decimal(9000) - Decimal(i) * 2
        out.append((row[0], px))
    return out


def _vix(daily, *, week_override=None):
    out = []
    for row in daily:
        px = Decimal(22)
        if week_override is not None:
            iso = row[0].isocalendar()
            if (iso[0], iso[1]) == WEEK_KEYS[SIGNAL_K][0]:
                px = week_override
        out.append((row[0], px))
    return out


def _replay(daily, **overrides):
    defaults = dict(
        as_of=daily[-1][0],
        adjustment_basis="adjusted",
        spx_closes=_spx(daily),
        vix_closes=_vix(daily),
        spx_available=True,
        vix_available=True,
        earnings_days=frozenset(),
        earnings_available=True,
    )
    defaults.update(overrides)
    return shadow_history("TST", daily, **defaults)


def _by_week(records):
    return {r.as_of_week: r for r in records}


# ----------------------------------------------------------------- tests
class TestTradingCalendar(unittest.TestCase):
    def test_known_2026_holidays(self):
        for day in (
            date(2026, 1, 1),   # New Year, Thursday
            date(2026, 1, 19),  # MLK
            date(2026, 2, 16),  # Washington
            date(2026, 4, 3),   # Good Friday (Easter Apr 5)
            date(2026, 5, 25),  # Memorial
            date(2026, 7, 3),   # July 4 observed (Saturday)
            date(2026, 9, 7),   # Labor
            date(2026, 11, 26),  # Thanksgiving
            date(2026, 12, 25),  # Christmas, Friday
        ):
            self.assertFalse(is_session(day), day)

    def test_observed_cross_year_saturday_new_year(self):
        # Jan 1 2028 is a Saturday -> observed Friday Dec 31 2027, and the
        # following Monday is a normal session again.
        self.assertFalse(is_session(date(2027, 12, 31)))
        self.assertTrue(is_session(date(2028, 1, 3)))

    def test_normal_weekdays_are_sessions(self):
        self.assertTrue(is_session(date(2026, 11, 24)))
        self.assertTrue(is_session(date(2026, 1, 2)))  # Jan 1 was a Thursday
        self.assertTrue(is_session(date(2026, 11, 27)))  # half-day is a session

    def test_sessions_back_returns_exact_window(self):
        self.assertEqual(len(sessions_back(date(2026, 11, 24), 252)), 252)
        window = sessions_back(date(2026, 11, 24), 5)
        self.assertEqual(window[-1], date(2026, 11, 24))

    def test_next_sessions_skips_holidays_and_weekends(self):
        nxt = next_sessions(date(2026, 11, 25), 2)
        self.assertEqual(nxt, [date(2026, 11, 27), date(2026, 11, 30)])

    def test_calendar_version_is_pinned(self):
        self.assertEqual(CALENDAR_VERSION, "nyse-et-v1")
        self.assertIn(date(2026, 4, 3), holidays(2026))


class TestGapAudit(unittest.TestCase):
    def _sessions(self, start=date(2026, 11, 16), end=date(2026, 11, 27)):
        return sessions(start, end)

    def test_internal_gap_detected(self):
        sess = self._sessions()
        missing = [d for d in sess if d.weekday() == 2][0]
        present = [(d, Decimal(1000)) for d in sess if d != missing]
        audit = audit_daily("TST", present, as_of=sess[-1], window=10)
        self.assertEqual(audit.category_counts.get(INTERNAL_GAP), 1)
        self.assertEqual(audit.state, STATE_PARTIAL)
        gap = [g for g in audit.gaps if g.category == INTERNAL_GAP][0]
        self.assertEqual(gap.day, missing)
        self.assertEqual(gap.reason_code, "expected_session_missing")

    def test_halt_declaration_overrides_internal_gap(self):
        sess = self._sessions()
        missing = [d for d in sess if d.weekday() == 2][0]
        present = [(d, Decimal(1000)) for d in sess if d != missing]
        audit = audit_daily(
            "TST", present, as_of=sess[-1], window=10,
            halt_days=frozenset([missing]),
        )
        self.assertEqual(audit.category_counts.get(HALT), 1)
        self.assertIsNone(audit.category_counts.get(INTERNAL_GAP))

    def test_zero_volume_session(self):
        sess = self._sessions()
        present = [
            (d, Decimal(0) if d == sess[-1] else Decimal(1000)) for d in sess
        ]
        audit = audit_daily("TST", present, as_of=sess[-1], window=10)
        self.assertEqual(audit.category_counts.get(ZERO_VOLUME), 1)
        self.assertEqual(audit.state, STATE_PARTIAL)

    def test_long_tail_gap_without_listing_date(self):
        sess = self._sessions()
        short = [(d, Decimal(1000)) for d in sess[-3:]]
        audit = audit_daily("TST", short, as_of=sess[-1], window=10)
        self.assertGreater(audit.category_counts.get(LONG_TAIL_GAP, 0), 0)
        self.assertEqual(audit.state, STATE_PARTIAL)

    def test_ipo_short_history_with_listing_date(self):
        sess = self._sessions()
        listed = sess[-3]
        short = [(d, Decimal(1000)) for d in sess[-3:]]
        audit = audit_daily(
            "TST", short, as_of=sess[-1], window=10, listed_since=listed,
        )
        self.assertEqual(audit.category_counts.get(IPO_SHORT_HISTORY), 1)
        self.assertIsNone(audit.category_counts.get(LONG_TAIL_GAP))
        self.assertTrue(audit.history_short)

    def test_corporate_action_declared_only(self):
        sess = self._sessions()
        present = [(d, Decimal(1000)) for d in sess]
        audit = audit_daily(
            "TST", present, as_of=sess[-1], window=9,
            corporate_action_days=frozenset([sess[2]]),
        )
        self.assertEqual(audit.category_counts.get(CORPORATE_ACTION), 1)
        # a declared corporate action is a data-quality flag, not missing data
        self.assertEqual(audit.state, STATE_COMPLETE)

    def test_complete_state_on_clean_window(self):
        sess = self._sessions()
        present = [(d, Decimal(1000)) for d in sess]
        audit = audit_daily("TST", present, as_of=sess[-1], window=len(sess))
        self.assertEqual(audit.state, STATE_COMPLETE)
        self.assertEqual(audit.expected, audit.actual)

    def test_unknown_on_empty(self):
        audit = audit_daily("TST", [], as_of=date(2026, 11, 27), window=10)
        self.assertEqual(audit.state, STATE_UNKNOWN)
        self.assertIsNone(audit.window_start)

    def test_no_future_session_reported_missing(self):
        sess = self._sessions()
        present = [(d, Decimal(1000)) for d in sess[:5]]
        audit = audit_daily(
            "TST", present, as_of=sess[4], window=10,
        )
        # expected window ends at the newest stored session: nothing after it
        self.assertEqual(audit.window_end, sess[4])
        self.assertEqual(audit.category_counts.get(INTERNAL_GAP, 0), 0)

    def test_blocking_gap_window_filter(self):
        sess = self._sessions()
        missing = [d for d in sess if d.weekday() == 2][0]
        present = [(d, Decimal(1000)) for d in sess if d != missing]
        audit = audit_daily("TST", present, as_of=sess[-1], window=len(sess) - 1)
        self.assertTrue(audit.has_blocking_gaps(through=sess[-1]))
        self.assertFalse(audit.has_blocking_gaps(through=missing - timedelta(days=1)))


class TestWeeklyBars(unittest.TestCase):
    def _bars(self, drop=None, as_of=None):
        days = sessions(date(2026, 11, 2), date(2026, 11, 20))
        bars = []
        for i, day in enumerate(days):
            if day == drop:
                continue
            px = Decimal(100 + i)
            bars.append((day, px, px + 1, px - 1, px, Decimal(1000)))
        return weekly_bars(bars, as_of=as_of or date(2026, 11, 23))

    def test_normal_week_aggregation_rules(self):
        weeks = self._bars()
        self.assertEqual(len(weeks), 3)
        first = weeks[0]
        self.assertEqual(first.sessions, 5)
        self.assertEqual(first.open, Decimal(100))   # first session's open
        self.assertEqual(first.close, Decimal(104))  # last session's close
        self.assertEqual(first.high, Decimal(105))   # weekly extreme
        self.assertEqual(first.low, Decimal(99))
        self.assertEqual(first.volume, Decimal(5000))  # weekly sum
        self.assertTrue(first.complete)

    def test_holiday_short_week_is_kept_and_complete(self):
        # week of Nov 23 2026 has Thanksgiving (Nov 26) -> 4 sessions
        days = sessions(date(2026, 11, 23), date(2026, 11, 27))
        self.assertEqual(len(days), 4)
        bars = [
            (d, Decimal(10), Decimal(11), Decimal(9), Decimal(10), Decimal(100))
            for d in days
        ]
        weeks = weekly_bars(bars, as_of=date(2026, 11, 30))
        self.assertEqual(len(weeks), 1)
        self.assertEqual(weeks[0].sessions, 4)
        self.assertEqual(weeks[0].expected_sessions, 4)
        self.assertTrue(weeks[0].complete)

    def test_missing_session_makes_week_incomplete(self):
        friday = date(2026, 11, 6)
        weeks = self._bars(drop=friday)
        target = [w for w in weeks if w.week_start == date(2026, 11, 2)][0]
        self.assertFalse(target.complete)

    def test_in_progress_week_never_completes(self):
        weeks = self._bars(as_of=date(2026, 11, 18))
        self.assertFalse(weeks[-1].complete)
        # no look-ahead: bars after as_of contribute nothing to the week
        self.assertEqual(weeks[-1].week_start, date(2026, 11, 16))
        self.assertEqual(weeks[-1].sessions, 3)  # Mon-Wed only
        self.assertEqual(weeks[-1].volume, Decimal(3000))
        self.assertEqual(weeks[-1].close, Decimal(112))  # Wednesday close

    def test_adjustment_basis_passes_through_unchanged(self):
        weeks = self._bars()
        self.assertIsNone(weeks[0].adjustment_basis)
        bars = [
            (d, Decimal(10), Decimal(11), Decimal(9), Decimal(10), Decimal(100))
            for d in sessions(date(2026, 11, 2), date(2026, 11, 6))
        ]
        weeks2 = weekly_bars(bars, as_of=date(2026, 11, 9), adjustment_basis="adjusted")
        self.assertEqual(weeks2[0].adjustment_basis, "adjusted")


class TestShadowTruthTable(unittest.TestCase):
    """§22.3 items 1-6 + 14, replayed on the engineered fixture."""

    @classmethod
    def setUpClass(cls):
        cls.daily = _build_daily()
        cls.records = _replay(cls.daily)
        cls.by_week = _by_week(cls.records)
        cls.signal_week = WEEK_KEYS[SIGNAL_K][0]
        cls.week_key = f"{WEEK_KEYS[SIGNAL_K][0][0]}-W{WEEK_KEYS[SIGNAL_K][0][1]:02d}"

    def test_engineered_fixture_produces_exactly_one_candidate(self):
        candidates = [r for r in self.records if r.outcome == "candidate"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].as_of_week, self.week_key)

    def test_candidate_record_fields(self):
        rec = self.by_week[self.week_key]
        self.assertIsInstance(rec, ShadowRecord)
        self.assertEqual(rec.symbol, "TST")
        self.assertEqual(rec.factor_version, FACTOR_VERSION)
        self.assertEqual(rec.adjustment_basis, "adjusted")
        self.assertEqual(rec.instrument_availability, "deferred_p5")
        self.assertEqual(rec.hill_weeks, 10)          # item 1: 6 <= n <= 10
        self.assertGreater(rec.hill_peak, Decimal(0))  # max(SMA20 - SMA50)
        self.assertGreater(rec.sma20, rec.sma50)       # hill intact, no death cross
        self.assertEqual(rec.sma20_slope_state, "declining")  # item 2
        self.assertEqual(rec.retest_state, "below")   # item 3: pullback
        self.assertEqual(rec.candle_state, "bearish")  # item 4 condition
        self.assertEqual(rec.volume_state, "low")     # item 4 hard filter
        self.assertEqual(rec.spx_filter, "strong")    # item 5
        self.assertEqual(rec.vix_filter, "ok")
        self.assertEqual(rec.earnings_filter, "clear")  # item 6
        self.assertEqual(rec.liquidity_filter, "ok")
        # the +50% rally ended more than 3 months before the signal week, so
        # the preference reads unmet — and the week is still a candidate:
        # prior_rally is never a gate (§22.3 item 6).
        self.assertEqual(rec.prior_rally, "unmet")
        self.assertEqual(rec.outcome, "candidate")
        self.assertEqual(rec.reason_codes, ())
        self.assertTrue(rec.input_hash)
        self.assertGreaterEqual(rec.weekly_bar_count, 50)

    def test_history_short_boundary_49_and_50(self):
        shorts = [r for r in self.records if r.outcome == "no_data"]
        self.assertTrue(all("history_short" in r.reason_codes for r in shorts[:49]))
        # the 50th complete week leaves the history_short gate
        first_real = self.records[49]
        self.assertNotIn("history_short", first_real.reason_codes)

    def test_hill_too_long_after_the_signal_week(self):
        later = [
            r for r in self.records
            if r.as_of_week > self.week_key and "hill_too_long" in r.reason_codes
        ]
        self.assertTrue(later)

    def test_slope_not_declining_blocks(self):
        blocked = [
            r for r in self.records
            if "slope_not_declining" in r.reason_codes
        ]
        self.assertTrue(blocked)
        self.assertTrue(all(r.outcome == "no_signal" for r in blocked))

    def test_retest_state_fixed_at_first_pullback(self):
        # §22.3 item 3: the first pullback week after the peak fixes the
        # state; later weeks do not recompute it.
        pullbacks = [
            r for r in self.records if r.retest_state in ("touched", "below")
        ]
        self.assertTrue(pullbacks)
        first = pullbacks[0].retest_state
        for rec in pullbacks:
            self.assertEqual(rec.retest_state, first)

    def test_death_cross_when_sma20_falls_below_sma50(self):
        daily = _build_daily(signal_k=-1)  # no engineered signal week
        # append a long, deep decline so SMA20 crosses under SMA50
        extra = []
        px = daily[-1][4]
        day = daily[-1][0]
        for i in range(130):
            day += timedelta(days=1)
            if day.weekday() >= 5:
                continue
            px = px * Decimal("0.985")
            extra.append(
                (day, px, px * Decimal("1.01"), px * Decimal("0.99"), px,
                 Decimal(6_000_000))
            )
        records = _replay(daily + extra)
        crossed = [r for r in records if "death_cross" in r.reason_codes]
        self.assertTrue(crossed)

    def test_peak_in_last_week_blocked(self):
        # monotonic rise: the hill keeps growing, peak sits on the last week
        daily = []
        px = Decimal(100)
        day = date(2024, 1, 1)
        for i in range(450):
            if day.weekday() >= 5:
                day += timedelta(days=1)
                continue
            px = px * Decimal("1.002")
            daily.append((day, px, px * Decimal("1.01"), px * Decimal("0.99"),
                          px, Decimal(6_000_000)))
            day += timedelta(days=1)
        records = _replay(daily)
        peaks = [r for r in records if "peak_in_last_week" in r.reason_codes]
        self.assertTrue(peaks)

    def test_unit_hill_equality_interrupts_and_peak_index(self):
        rise = [Decimal(100) + Decimal(i) for i in range(12)]
        flat = [Decimal(112)] * 3
        series20 = rise + flat  # 15 weekly SMA20 values
        series50 = [Decimal(100)] * 15
        weeks, peak, peak_index = _hill(series20, series50)
        # the first element (100 == 100) breaks the run, so 14 count
        self.assertEqual(weeks, 14)
        self.assertEqual(peak_index, 12)  # end of the rise, not the last week
        self.assertEqual(peak, Decimal(12))
        # exact equality interrupts the run immediately
        weeks2, _p, _pi = _hill(
            [Decimal(101), Decimal(102), Decimal(103)],
            [Decimal(100), Decimal(100), Decimal(103)],
        )
        self.assertEqual(weeks2, 0)

    def test_unit_retest_touch_and_below(self):
        from athena_strategy.weekly import WeekBar as WB

        def wb(low, close):
            return WB(
                iso_week="2026-W01", week_start=date(2026, 1, 5),
                open=Decimal(10), high=Decimal(11), low=low,
                close=close, volume=Decimal(1), sessions=5,
                expected_sessions=5, complete=True, adjustment_basis=None,
            )

        sma20 = [Decimal(100)] * 4
        weeks = [wb(Decimal(99), Decimal(100))] * 4  # low <= 100.5, close >= 99.5
        self.assertEqual(_retest_state(weeks, 3, sma20, 0), "touched")
        weeks_below = [wb(Decimal(99), Decimal(98))] * 4  # close < 99.5
        self.assertEqual(_retest_state(weeks_below, 3, sma20, 0), "below")
        weeks_high = [wb(Decimal(105), Decimal(104))] * 4  # never touches
        self.assertEqual(_retest_state(weeks_high, 3, sma20, 0), "untouched")
        self.assertEqual(_retest_state(weeks, 3, sma20, None), "untouched")

    def test_neutral_candle_filters(self):
        daily = _build_daily(signal_gap_up_open=False)
        rec = _by_week(_replay(daily))[self.week_key]
        self.assertEqual(rec.outcome, "filtered")
        self.assertIn("candle_condition_unmet", rec.reason_codes)
        self.assertEqual(rec.candle_state, "neutral")

    def test_volume_not_low_filters(self):
        daily = _build_daily(signal_volume=Decimal(6_000_000))
        rec = _by_week(_replay(daily))[self.week_key]
        self.assertIn("volume_not_low", rec.reason_codes)
        self.assertEqual(rec.outcome, "filtered")

    def test_spx_weak_filters(self):
        rec = _by_week(_replay(self.daily, spx_closes=_spx(self.daily, rising=False)))[
            self.week_key
        ]
        self.assertIn("spx_weak", rec.reason_codes)
        self.assertEqual(rec.spx_filter, "weak")

    def test_vix_boundaries_skip_caution_ok(self):
        skip = _by_week(_replay(self.daily, vix_closes=_vix(self.daily, week_override=Decimal(14))))[
            self.week_key
        ]
        self.assertEqual(skip.vix_filter, "skip")
        self.assertIn("vix_skip", skip.reason_codes)
        caution = _by_week(_replay(self.daily, vix_closes=_vix(self.daily, week_override=Decimal(17))))[
            self.week_key
        ]
        self.assertEqual(caution.vix_filter, "caution")
        self.assertEqual(caution.outcome, "candidate")  # caution halves P5 budget, not the signal
        boundary = _by_week(_replay(self.daily, vix_closes=_vix(self.daily, week_override=Decimal(20))))[
            self.week_key
        ]
        self.assertEqual(boundary.vix_filter, "caution")  # boundary goes low
        above = _by_week(_replay(self.daily, vix_closes=_vix(self.daily, week_override=Decimal(21))))[
            self.week_key
        ]
        self.assertEqual(above.vix_filter, "ok")

    def test_earnings_near_filters(self):
        from athena_strategy.trading_calendar import sessions as cal_sessions

        week_days = [d for d in self.daily if d[0].isocalendar()[:2] == WEEK_KEYS[SIGNAL_K][0]]
        week_last = week_days[-1][0]
        near_day = next_sessions(week_last, 1)[0]
        rec = _by_week(
            _replay(self.daily, earnings_days=frozenset([near_day]))
        )[self.week_key]
        self.assertEqual(rec.earnings_filter, "near")
        self.assertIn("earnings_near", rec.reason_codes)

    def test_liquidity_thin_filters(self):
        daily = _build_daily(
            base_volume=Decimal(100_000), signal_volume=Decimal(30_000)
        )
        rec = _by_week(_replay(daily))[self.week_key]
        self.assertEqual(rec.liquidity_filter, "thin")
        self.assertIn("liquidity_thin", rec.reason_codes)
        self.assertEqual(rec.volume_state, "low")

    def test_adjustment_missing_is_no_data_everywhere(self):
        records = _replay(self.daily, adjustment_basis=None)
        self.assertTrue(records)
        for rec in records:
            self.assertEqual(rec.outcome, "no_data")
            self.assertIn("adjustment_unavailable", rec.reason_codes)

    def test_unavailable_filter_inputs_are_no_data_not_negation(self):
        rec = _by_week(_replay(self.daily, spx_available=False))[self.week_key]
        self.assertEqual(rec.outcome, "no_data")
        self.assertIn("spx_unavailable", rec.reason_codes)
        rec2 = _by_week(_replay(self.daily, vix_available=False))[self.week_key]
        self.assertIn("vix_unavailable", rec2.reason_codes)
        rec3 = _by_week(_replay(self.daily, earnings_available=False))[self.week_key]
        self.assertIn("earnings_calendar_unavailable", rec3.reason_codes)
        for rec_x in (rec, rec2, rec3):
            self.assertEqual(rec_x.outcome, "no_data")

    def test_gap_inside_sma_window_blocks_with_reason(self):
        # remove one mid-week session eight weeks before the signal week
        gap_day = [
            d for d in sessions(START, START + timedelta(days=7 * N_WEEKS))
            if d.isocalendar()[:2] == WEEK_KEYS[50][0] and d.weekday() == 2
        ][0]
        daily = _build_daily(drop_session=gap_day)
        records = _replay(daily)
        blocked = [r for r in records if "daily_gap_in_window" in r.reason_codes]
        self.assertTrue(blocked)
        self.assertTrue(all(r.outcome == "no_data" for r in blocked))

    def test_volume_baseline_is_prior_8_weeks(self):
        from athena_strategy.strategy3 import _volume_state

        def week(volume):
            return WeekBar(
                iso_week="2026-W01", week_start=date(2026, 1, 5),
                open=Decimal(10), high=Decimal(11), low=Decimal(9),
                close=Decimal(10), volume=volume, sessions=5,
                expected_sessions=5, complete=True, adjustment_basis=None,
            )

        weeks = [week(Decimal(1000))] * 7 + [week(Decimal(100)), week(Decimal(500))]
        # baseline = prior 8 completed weeks' average = (7*1000 + 100)/8
        self.assertEqual(_volume_state(weeks, 8), "low")  # 500 < 887.5
        weeks_high = [week(Decimal(1000))] * 7 + [week(Decimal(100)), week(Decimal(1200))]
        self.assertEqual(_volume_state(weeks_high, 8), "normal")  # 1200 > 887.5
        self.assertEqual(_volume_state(weeks[:3], 2), "unavailable")  # i < 8

    def test_input_hash_is_deterministic_and_week_unique(self):
        again = _replay(self.daily)
        base = _by_week(self.records)[self.week_key]
        twin = _by_week(again)[self.week_key]
        self.assertEqual(base.input_hash, twin.input_hash)
        other = [
            r for r in self.records
            if r.as_of_week != self.week_key and r.outcome != "no_data"
        ][0]
        self.assertNotEqual(base.input_hash, other.input_hash)

    def test_no_look_ahead_replay_is_stable(self):
        signal_day = [
            row[0] for row in self.daily
            if row[0].isocalendar()[:2] == WEEK_KEYS[SIGNAL_K][0]
        ][-1]
        upto = _replay(self.daily, as_of=signal_day)
        base = _by_week(self.records)[self.week_key]
        self.assertEqual(_by_week(upto)[self.week_key], base)

    def test_invalid_bar_data_is_invalid_not_crash(self):
        broken = list(self.daily)
        bad = list(broken[10])
        broken[10] = (bad[0], bad[1], bad[2], bad[3], Decimal(0), bad[5])
        records = _replay(broken)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].outcome, "invalid")
        self.assertIn("bad_bar_data", records[0].reason_codes)

    def test_every_record_is_shadow_only(self):
        for rec in self.records:
            self.assertEqual(rec.instrument_availability, "deferred_p5")
            self.assertNotEqual(rec.outcome, "draft")


class TestIrisP4Wiring(unittest.TestCase):
    """Read-only snapshot additions: gap state, SMA, Strategy 3 summary."""

    class _Cur:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return list(self.rows)

        def fetchone(self):
            return self.rows[0] if self.rows else None

    class _RouteConn:
        """Routes by SQL needle; everything unmatched returns no rows."""

        def __init__(self, routes):
            self.routes = routes
            self.calls = []
            self.commits = 0
            self.rollbacks = 0

        def execute(self, sql, params=None):
            flat = " ".join(sql.split())
            self.calls.append((flat, params))
            for needle, rows in self.routes:
                if needle in flat:
                    return TestIrisP4Wiring._Cur(rows)
            return TestIrisP4Wiring._Cur([])

    def _bar_rows(self, n_sessions=30):
        days = sessions(date(2026, 9, 1), date(2026, 10, 30))[:n_sessions]
        rows = []
        for i, day in enumerate(days):
            px = Decimal(100 + i)
            rows.append(("TST", day, px, px + 1, px - 1, px, Decimal(1000)))
        return rows

    def _conn(self, rows):
        return self._RouteConn(
            [
                ("symbol = ANY", []),
                ("symbol = 'VIX'", []),
                ("kind = 'EARNINGS'", []),
                ("ORDER BY symbol, ts", rows),
            ]
        )

    def test_snapshot_computes_gap_state_from_bars(self):
        from apps.orchestrator.src.athena_orchestrator import iris_view

        conn = self._conn(self._bar_rows())
        snap = iris_view.snapshot(conn, "U1", SimpleNamespace(watchlist=["TST"]))
        self.assertEqual(snap["daily_gap_state"], STATE_PARTIAL)
        self.assertGreater(snap["daily_gap_count"], 0)
        self.assertEqual(snap["daily_gap_symbols"], 1)
        self.assertEqual(snap["strategy3_scanned"], 1)
        self.assertEqual(snap["strategy3_candidates"], 0)
        self.assertNotEqual(snap["strategy3_week"], "unknown")

    def test_snapshot_empty_stays_unknown(self):
        from apps.orchestrator.src.athena_orchestrator import iris_view

        snap = iris_view.snapshot(self._conn([]), "U1", SimpleNamespace(watchlist=[]))
        self.assertEqual(snap["daily_gap_state"], "unknown")
        self.assertEqual(snap["daily_gap_count"], 0)
        self.assertEqual(snap["strategy3_week"], "unknown")
        self.assertEqual(snap["strategy3_candidates"], 0)

    def test_snapshot_readonly_and_known_tables_only(self):
        import re

        from apps.orchestrator.src.athena_orchestrator import iris_view
        from apps.orchestrator.tests.test_iris_p1 import _EXISTING_TABLES

        conn = self._conn(self._bar_rows())
        iris_view.snapshot(conn, "U1", SimpleNamespace(watchlist=["TST"]))
        self.assertEqual(conn.commits, 0)
        self.assertEqual(conn.rollbacks, 0)
        tables = set()
        for sql, _params in conn.calls:
            body = sql.upper()
            self.assertTrue(body.lstrip().startswith("SELECT"), sql[:80])
            tables.update(re.findall(r"(?i)\bFROM\s+([a-z_][a-z0-9_]*)", sql))
        self.assertTrue(tables <= _EXISTING_TABLES, tables - _EXISTING_TABLES)

    def test_strategy3_node_stays_shadow_and_names_zero_drafts(self):
        from apps.orchestrator.src.athena_orchestrator import iris_view

        snap = iris_view.snapshot(self._conn([]), "U1", SimpleNamespace(watchlist=[]))
        states = iris_view._node_states(snap)
        self.assertEqual(states["strategy_3"][0], "shadow")
        self.assertIn("zero drafts", states["strategy_3"][1])
        self.assertEqual(states["weekly_sma"][0], "shadow")

    def test_header_note_switches_with_gap_state(self):
        from apps.orchestrator.src.athena_orchestrator import iris_view

        empty = iris_view.snapshot(self._conn([]), "U1", SimpleNamespace(watchlist=[]))
        self.assertIn("internal gaps are unknown", iris_view.render_header(empty))
        known = dict(empty)
        known.update(
            daily_gap_state=STATE_PARTIAL, daily_gap_count=3, daily_gap_symbols=2
        )
        header = iris_view.render_header(known)
        self.assertIn("gap audit found 3 gap(s)", header)
        self.assertIn("2 symbol(s)", header)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
