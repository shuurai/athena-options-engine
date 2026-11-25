"""ISO-week aggregation over completed ib-daily sessions (V2-P4, Q8 / §28.3 R3).

Aggregation rules frozen by V2-Q8: weeks anchor on the ISO week of each ET
session day; ``open`` = first completed session's open, ``high`` / ``low`` =
weekly extremes, ``close`` = last completed session's close, ``volume`` =
weekly sum. Holiday-short weeks are kept. A week is *complete* only when
every expected calendar session of that week (up to ``as_of``) is present
and the week has fully ended — the in-progress week never enters the
confirmation stream and never produces a candidate (§22.3 item 8 / Q8).

Adjustment honesty (Q8, §22.3 item 14): this module never pretends bars are
adjusted. ``adjustment_basis`` is a declared caller fact; the strategy layer
emits ``no_data`` + ``adjustment_unavailable`` when it is ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from athena_strategy.trading_calendar import is_session

_MONDAY = 0


@dataclass(frozen=True)
class WeekBar:
    iso_week: str
    week_start: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    sessions: int
    expected_sessions: int
    complete: bool
    adjustment_basis: str | None


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def weekly_bars(
    daily: list[tuple[date, Decimal, Decimal, Decimal, Decimal, Decimal]],
    *,
    as_of: date,
    adjustment_basis: str | None = None,
) -> list[WeekBar]:
    """Aggregate completed daily sessions into ISO weeks, oldest first.

    ``daily`` is ``(day, open, high, low, close, volume)`` tuples of
    completed ``ib-daily`` sessions. A week whose expected sessions contain
    a gap is aggregated but flagged ``complete=False`` so the signal layer
    refuses to confirm across it.
    """
    by_week: dict[date, list[tuple]] = {}
    for row in daily:
        day = row[0]
        if day > as_of:
            continue
        by_week.setdefault(_week_start(day), []).append(row)

    out: list[WeekBar] = []
    for start in sorted(by_week):
        rows = sorted(by_week[start], key=lambda r: r[0])
        week_end = start + timedelta(days=4)
        expected = [
            start + timedelta(days=i)
            for i in range(5)
            if is_session(start + timedelta(days=i))
        ]
        # Only expected sessions that already lie in the past can be missing.
        expected_past = [d for d in expected if d <= as_of]
        present_days = {r[0] for r in rows}
        missing = [d for d in expected_past if d not in present_days]
        # The week is confirmable once its last expected session has completed
        # (its bar exists). A mid-progress week still has future sessions.
        week_ended = bool(expected) and expected[-1] <= as_of
        complete = week_ended and not missing
        iso_year, iso_number, _ = rows[0][0].isocalendar()
        out.append(
            WeekBar(
                iso_week=f"{iso_year}-W{iso_number:02d}",
                week_start=start,
                open=rows[0][1],
                high=max(r[2] for r in rows),
                low=min(r[3] for r in rows),
                close=rows[-1][4],
                volume=sum((r[5] for r in rows), Decimal(0)),
                sessions=len(rows),
                expected_sessions=len(expected),
                complete=complete,
                adjustment_basis=adjustment_basis,
            )
        )
    return out
