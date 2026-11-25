"""Daily gap audit over completed ib-daily sessions (V2-P4, Q7 / §28.3 R2).

Six gap categories with reason codes (V2-Q7): ``internal_gap``,
``long_tail_gap``, ``ipo_short_history``, ``halt``, ``zero_volume`` and
``corporate_action``. Detection is honest about what bars alone can prove:

* missing expected sessions strictly between the first and last stored
  session are ``internal_gap``, unless the caller declared a halt for that
  day (``halt``);
* expected sessions before the first stored session are ``long_tail_gap``,
  unless the caller declared a listing date, in which case history before
  the listing is ``ipo_short_history`` and is not counted as missing data;
* a stored session with zero volume is ``zero_volume``;
* corporate actions are only reported when the caller declares them —
  detecting splits from price levels alone would be speculation.

The Iris state machine (R2): ``unknown`` when nothing can be measured (no
sessions), ``partial`` when any gap is detected or history is short,
``complete`` only when the 252-session window is fully populated and clean.
A missing detector must never display ``complete`` (§7.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from athena_strategy.trading_calendar import sessions_back

STATE_UNKNOWN = "unknown"
STATE_PARTIAL = "partial"
STATE_COMPLETE = "complete"

INTERNAL_GAP = "internal_gap"
LONG_TAIL_GAP = "long_tail_gap"
IPO_SHORT_HISTORY = "ipo_short_history"
HALT = "halt"
ZERO_VOLUME = "zero_volume"
CORPORATE_ACTION = "corporate_action"

GAP_WINDOW = 252


@dataclass(frozen=True)
class Gap:
    day: date
    category: str
    reason_code: str


@dataclass(frozen=True)
class GapAudit:
    symbol: str
    window_start: date | None
    window_end: date | None
    expected: int
    actual: int
    gaps: tuple[Gap, ...]
    state: str
    history_short: bool

    @property
    def category_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for gap in self.gaps:
            counts[gap.category] = counts.get(gap.category, 0) + 1
        return counts

    def has_blocking_gaps(self, *, through: date | None = None) -> bool:
        """Missing-session gaps (halt included) up to ``through``."""
        for gap in self.gaps:
            if gap.category in (INTERNAL_GAP, LONG_TAIL_GAP, HALT):
                if through is None or gap.day <= through:
                    return True
        return False


def audit_daily(
    symbol: str,
    present: list[tuple[date, Decimal]],
    *,
    as_of: date,
    window: int = GAP_WINDOW,
    listed_since: date | None = None,
    halt_days: frozenset[date] = frozenset(),
    corporate_action_days: frozenset[date] = frozenset(),
) -> GapAudit:
    """Audit one symbol's completed daily sessions against the calendar.

    ``present`` is ``(session day, volume)`` pairs, oldest first. Only days
    at or before ``as_of`` are audited; the expected window is the last
    ``window`` calendar sessions ending at the newest stored session, so no
    future session is ever reported as missing.
    """
    days = [d for d, _vol in present if d <= as_of]
    if not days:
        return GapAudit(symbol, None, None, 0, 0, (), STATE_UNKNOWN, False)
    days = sorted(set(days))
    volumes = {d: v for d, v in present if d <= as_of}
    last = days[-1]
    expected = sessions_back(last, window)
    if listed_since is not None:
        expected = [d for d in expected if d >= listed_since]

    gaps: list[Gap] = []
    present_set = set(days)
    first = days[0]
    for day in expected:
        if day in present_set:
            continue
        if day < first:
            gaps.append(Gap(day, LONG_TAIL_GAP, "no_bar_before_first_session"))
        elif day in halt_days:
            gaps.append(Gap(day, HALT, "halt_declared"))
        else:
            gaps.append(Gap(day, INTERNAL_GAP, "expected_session_missing"))
    if listed_since is not None and len(expected) < window and expected:
        # History legitimately starts at the declared listing date, not at
        # the window start: short history is an IPO fact, not missing data.
        gaps.append(
            Gap(expected[0], IPO_SHORT_HISTORY, "sessions_before_listing_absent")
        )
    for day in days:
        if volumes.get(day, Decimal(0)) == 0:
            gaps.append(Gap(day, ZERO_VOLUME, "zero_volume_session"))
    for day in sorted(corporate_action_days):
        if day in present_set:
            gaps.append(Gap(day, CORPORATE_ACTION, "corporate_action_declared"))

    missing = sum(
        1 for g in gaps if g.category in (INTERNAL_GAP, LONG_TAIL_GAP, HALT)
    )
    zero_vol = sum(1 for g in gaps if g.category == ZERO_VOLUME)
    history_short = len(expected) < window
    if missing or zero_vol or history_short:
        state = STATE_PARTIAL
    else:
        state = STATE_COMPLETE
    return GapAudit(
        symbol=symbol,
        window_start=expected[0] if expected else None,
        window_end=last,
        expected=len(expected),
        actual=len(present_set & set(expected)),
        gaps=tuple(sorted(gaps, key=lambda g: (g.day, g.category))),
        state=state,
        history_short=history_short,
    )
