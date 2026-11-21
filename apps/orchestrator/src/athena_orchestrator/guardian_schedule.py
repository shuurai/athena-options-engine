"""R3 ET-anchored guardian schedule (booklet 14, section 14). Pure, zero I/O.

Wall-clock targets are expressed in ``America/New_York`` local time and
converted to UTC *per trading day* with zoneinfo, so DST follows the IANA rule
rather than a hard-coded switch date. In particular this does NOT reuse the
one-off 2026-11-02 HKT-window rewrite in ``athena_exec.slots``, which would
silently drift after that season.

Ratified inputs: D60 (A = open-60m, B = open+5m, NY-anchored, two runs),
D71 (C = single 12:30 ET run, derived here but executed by R7).

Honest gap (section 14.5): no US market-holiday calendar exists in the repo and
section 6 forbids adding a data source for one, so only weekdays are filtered.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

__all__ = [
    "ET",
    "HKT",
    "UTC",
    "US_OPEN",
    "RUNS",
    "EXECUTED_RUNS",
    "RunSlot",
    "run_wall_times",
    "run_moments",
    "is_us_weekday",
    "next_run",
    "hk_clock",
    "hk_stamp",
]

ET = ZoneInfo("America/New_York")
HKT = ZoneInfo("Asia/Hong_Kong")
UTC = timezone.utc

US_OPEN = time(9, 30)
RUN_A_OFFSET = timedelta(minutes=-60)
RUN_B_OFFSET = timedelta(minutes=5)
RUN_C_WALL = time(12, 30)

RUNS = ("A", "B", "C")
# R3 scope. C is derived (run_moments) but not executed: R7 will pass
# runs=("C",) to next_run() and write its own regime_labels (D71).
EXECUTED_RUNS = ("A", "B")


@dataclass(frozen=True)
class RunSlot:
    run: str
    trade_day: date
    moment: datetime  # UTC-aware


def _wall(day: date, clock: time) -> datetime:
    return datetime.combine(day, clock).replace(tzinfo=ET)


def run_wall_times() -> dict[str, time]:
    """ET wall clock per run, derived from US_OPEN (D60) and D71's 12:30."""
    naive_open = datetime.combine(date(2000, 1, 1), US_OPEN)
    return {
        "A": (naive_open + RUN_A_OFFSET).time(),
        "B": (naive_open + RUN_B_OFFSET).time(),
        "C": RUN_C_WALL,
    }


def run_moments(day: date) -> dict[str, datetime]:
    """UTC instants of A/B/C for one ET trading day, DST-safe."""
    return {
        run: _wall(day, clock).astimezone(UTC)
        for run, clock in run_wall_times().items()
    }


def is_us_weekday(day: date) -> bool:
    """Mon-Fri in ET. US market holidays are NOT filtered (section 14.5)."""
    return day.weekday() < 5


def next_run(now: datetime, runs=EXECUTED_RUNS) -> RunSlot | None:
    """The next future fire strictly after ``now``. No backfill on restart."""
    utc_now = now.astimezone(UTC)
    et_day = utc_now.astimezone(ET).date()
    wanted = tuple(run for run in RUNS if run in runs)  # chronological order
    for shift in range(0, 8):
        day = et_day + timedelta(days=shift)
        if not is_us_weekday(day):
            continue
        moments = run_moments(day)
        for run in wanted:
            moment = moments[run]
            if moment > utc_now:
                return RunSlot(run, day, moment)
    return None


def hk_clock(moment: datetime) -> str:
    """HKT wall clock 'HH:MM' for display and the DST acceptance cases."""
    return moment.astimezone(HKT).strftime("%H:%M")


def hk_stamp(moment: datetime) -> str:
    return moment.astimezone(HKT).strftime("%Y-%m-%d %H:%M HKT")
