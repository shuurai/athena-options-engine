"""Deterministic NYSE/ET session calendar (V2-P4, §28.3 R1).

stdlib-only: weekdays minus the fixed and floating NYSE holidays, including
Good Friday via the Anonymous Gregorian Easter algorithm. No network, no new
dependency. A holiday falling on a Saturday is observed on the prior Friday;
one falling on a Sunday is observed on the following Monday (NYSE rule).
This is the authoritative expected-session source for the daily gap audit
(V2-Q7); it is deliberately conservative and fixture-pinned against known
2025-2026 holidays.
"""

from __future__ import annotations

from datetime import date, timedelta

CALENDAR_VERSION = "nyse-et-v1"

_MON, _TUE, _WED, _THU, _FRI, _SAT, _SUN = range(7)


def _easter(year: int) -> date:
    """Anonymous Gregorian algorithm (Gregorian calendar only)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        probe = date(year + 1, 1, 1)
    else:
        probe = date(year, month + 1, 1)
    last = probe - timedelta(days=1)
    offset = (last.weekday() - weekday) % 7
    return last - timedelta(days=offset)


def _observed(day: date) -> date:
    if day.weekday() == _SAT:
        return day - timedelta(days=1)
    if day.weekday() == _SUN:
        return day + timedelta(days=1)
    return day


def holidays(year: int) -> frozenset[date]:
    """Observed non-session dates that land inside ``year``.

    Rules anchored in adjacent years are included so a Saturday New Year
    observed on Dec 31 of the prior year is honoured on both sides of the
    boundary.
    """
    out: set[date] = set()
    for y in (year - 1, year, year + 1):
        gf = _easter(y) - timedelta(days=2)
        rules = [
            date(y, 1, 1),
            _nth_weekday(y, 1, _MON, 3),
            _nth_weekday(y, 2, _MON, 3),
            gf,
            _last_weekday(y, 5, _MON),
            date(y, 6, 19),
            date(y, 7, 4),
            _nth_weekday(y, 9, _MON, 1),
            _nth_weekday(y, 11, _THU, 4),
            date(y, 12, 25),
        ]
        if y < 2022:
            rules.remove(date(y, 6, 19))
        for rule in rules:
            observed = _observed(rule)
            if observed.year == year:
                out.add(observed)
    return frozenset(out)


def is_session(day: date) -> bool:
    """True when ``day`` is an expected NYSE/ET regular session."""
    if day.weekday() >= 5:
        return False
    return day not in holidays(day.year)


def sessions(start: date, end: date) -> list[date]:
    """Expected sessions in ``[start, end]``, oldest first."""
    if end < start:
        return []
    out: list[date] = []
    day = start
    while day <= end:
        if is_session(day):
            out.append(day)
        day += timedelta(days=1)
    return out


def sessions_back(end: date, n: int) -> list[date]:
    """The last ``n`` expected sessions ending at or before ``end``."""
    if n <= 0:
        return []
    out: list[date] = []
    day = end
    while len(out) < n:
        if is_session(day):
            out.append(day)
        day -= timedelta(days=1)
    out.reverse()
    return out


def next_sessions(day: date, n: int) -> list[date]:
    """The first ``n`` expected sessions strictly after ``day``."""
    out: list[date] = []
    probe = day
    while len(out) < n:
        probe += timedelta(days=1)
        if is_session(probe):
            out.append(probe)
    return out
