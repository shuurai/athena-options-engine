"""S0a: P4 strategy-agnostic plumbing kept after removing Strategy 3 shadow.

The deterministic ET calendar, six-category gap audit and ISO-week aggregation
survive the simplification (booklet 16 §1.4 / §16 S0a). All shadow-signal and
Strategy-3 Iris-wiring tests were removed with the strategy itself.
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta
from decimal import Decimal

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
from athena_strategy.trading_calendar import (
    CALENDAR_VERSION,
    holidays,
    is_session,
    next_sessions,
    sessions,
    sessions_back,
)
from athena_strategy.weekly import WeekBar, weekly_bars


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
