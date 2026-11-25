"""Strategy 3 — SMA Hill Retest weekly shadow (V2-P4, Q9 signal subset).

Implements the frozen semantics of §22.3 items 1-6 and 14, plus §8.2's
replayable output shape. Items 7-13 (entry window, instrument parameters,
take-profit, holding duration, trigger priority, NAV sizing, the 3-stop
rule) are deliberately NOT implemented here — they belong to P5 / P6
(§28.3 R6); ``instrument_availability`` is therefore always
``deferred_p5`` and this module never creates a draft, order or budget.

Truth order (§28.3 R5): ``no_data`` > ``invalid`` > ``filtered`` >
``no_signal`` > ``candidate``. Missing filter inputs are never negation
(V2-I8): an unavailable SPX / VIX / earnings / adjustment source makes the
week ``no_data`` with a dedicated reason code, never a silent pass.

No look-ahead: a week is only ever evaluated from bars at or before that
week's last completed session, and the in-progress week is never a signal.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from athena_strategy.gap_audit import GapAudit, audit_daily
from athena_strategy.trading_calendar import is_session, next_sessions
from athena_strategy.weekly import WeekBar, weekly_bars

FACTOR_VERSION = "s3-shadow-v1"

OUTCOME_NO_DATA = "no_data"
OUTCOME_INVALID = "invalid"
OUTCOME_FILTERED = "filtered"
OUTCOME_NO_SIGNAL = "no_signal"
OUTCOME_CANDIDATE = "candidate"

HILL_MIN_WEEKS = 6
HILL_MAX_WEEKS = 10
SMA20_N = 20
SMA50_N = 50
SLOPE_WINDOW = 4
SLOPE_FLAT = Decimal("0.002")
SLOPE_DECLINING = Decimal("-0.002")
TOUCH_TOL = Decimal("0.005")
VOLUME_BASELINE_WEEKS = 8
LIQUIDITY_SESSIONS = 20
LIQUIDITY_MIN_DOLLAR_VOLUME = Decimal(500_000_000)
VIX_SKIP_BELOW = Decimal(15)
VIX_CAUTION_THROUGH = Decimal(20)
EARNINGS_WINDOW_SESSIONS = 5
PRIOR_RALLY_MIN = Decimal("1.5")
PRIOR_RALLY_LONG_SESSIONS = 126
PRIOR_RALLY_SHORT_SESSIONS = 63

DEFERRED_P5 = "deferred_p5"


@dataclass(frozen=True)
class ShadowRecord:
    """One replayable §8.2 result for one completed week of one symbol."""

    as_of_week: str
    symbol: str
    weekly_bar_count: int
    sma20: Decimal | None
    sma50: Decimal | None
    sma20_slope_state: str
    hill_weeks: int
    hill_peak: Decimal | None
    retest_state: str
    candle_state: str
    volume_state: str
    spx_filter: str
    vix_filter: str
    earnings_filter: str
    liquidity_filter: str
    prior_rally: str
    instrument_availability: str
    adjustment_basis: str | None
    outcome: str
    reason_codes: tuple[str, ...]
    input_hash: str
    factor_version: str


def _sma(values: list[Decimal], n: int) -> Decimal | None:
    if len(values) < n:
        return None
    window = values[-n:]
    return sum(window, Decimal(0)) / Decimal(n)


def _least_squares_slope(values: list[Decimal]) -> Decimal:
    """Slope per step over evenly spaced points; ≥2 points required."""
    n = len(values)
    if n < 2:
        raise ValueError("slope needs at least two points")
    mean_x = Decimal(n - 1) / Decimal(2)
    mean_y = sum(values, Decimal(0)) / Decimal(n)
    num = Decimal(0)
    den = Decimal(0)
    for x, y in enumerate(values):
        dx = Decimal(x) - mean_x
        num += dx * (y - mean_y)
        den += dx * dx
    return num / den


def _slope_state(sma20_series: list[Decimal | None]) -> str:
    """§22.3 item 2: 4-week least-squares slope of SMA20, normalised."""
    if len(sma20_series) < SMA50_N:
        return "unavailable"
    window = sma20_series[-SLOPE_WINDOW:]
    if any(v is None for v in window):
        return "unavailable"
    raw = _least_squares_slope(window)
    current = window[-1]
    if current == 0:
        return "unavailable"
    normalized = raw / current
    if normalized < SLOPE_DECLINING:
        return "declining"
    if abs(normalized) < SLOPE_FLAT:
        return "flat"
    return "rising"


def _candle_state(week: WeekBar) -> str:
    body = abs(week.close - week.open)
    upper = week.high - max(week.open, week.close)
    rng = week.high - week.low
    if week.close < week.open:
        return "bearish"
    if body > 0 and rng > 0 and upper >= 2 * body and upper >= Decimal("0.4") * rng:
        return "long_upper_shadow"
    return "neutral"


def _weekly_closes_from_daily(
    daily_closes: list[tuple[date, Decimal]],
) -> dict[str, Decimal]:
    """ISO-week -> last close of the week, from completed daily closes."""
    out: dict[str, Decimal] = {}
    for day, close in sorted(daily_closes):
        iso_year, iso_number, _ = day.isocalendar()
        out[f"{iso_year}-W{iso_number:02d}"] = close
    return out


def _vix_state(vix_close: Decimal | None) -> str:
    """§22.3 item 5: <15 skip, ≤20 caution, >20 ok; boundaries go low."""
    if vix_close is None:
        return "unavailable"
    if vix_close < VIX_SKIP_BELOW:
        return "skip"
    if vix_close <= VIX_CAUTION_THROUGH:
        return "caution"
    return "ok"


def _prior_rally(closes: list[Decimal]) -> str:
    """§22.3 item 6: +50% inside the last 3-6 months is a preference, not a gate."""
    if len(closes) < PRIOR_RALLY_LONG_SESSIONS:
        return "unavailable"
    window = closes[-PRIOR_RALLY_LONG_SESSIONS:]
    base = min(window)
    if base <= 0:
        return "unavailable"
    if max(window) / base >= PRIOR_RALLY_MIN:
        return "met"
    return "unmet"


def shadow_history(
    symbol: str,
    daily: list[tuple[date, Decimal, Decimal, Decimal, Decimal, Decimal]],
    *,
    as_of: date,
    adjustment_basis: str | None,
    spx_closes: list[tuple[date, Decimal]],
    vix_closes: list[tuple[date, Decimal]],
    spx_available: bool,
    vix_available: bool,
    earnings_days: frozenset[date] = frozenset(),
    earnings_available: bool = False,
    listed_since: date | None = None,
    halt_days: frozenset[date] = frozenset(),
    corporate_action_days: frozenset[date] = frozenset(),
) -> list[ShadowRecord]:
    """Replay §8.2 records for every completed week with a full SMA50 window.

    Zero drafts: this function is pure and writes nothing. ``adjustment_basis``
    is a declared caller fact — ``None`` means the source cannot prove the
    series is split/dividend adjusted, and every week then reports
    ``no_data`` + ``adjustment_unavailable`` (§22.3 item 14).
    """
    for row in daily:
        if row[4] <= 0 or row[2] < row[3]:
            return [
                ShadowRecord(
                    as_of_week="unknown",
                    symbol=symbol,
                    weekly_bar_count=0,
                    sma20=None,
                    sma50=None,
                    sma20_slope_state="unavailable",
                    hill_weeks=0,
                    hill_peak=None,
                    retest_state="unavailable",
                    candle_state="unavailable",
                    volume_state="unavailable",
                    spx_filter="unavailable",
                    vix_filter="unavailable",
                    earnings_filter="unavailable",
                    liquidity_filter="unavailable",
                    prior_rally="unavailable",
                    instrument_availability=DEFERRED_P5,
                    adjustment_basis=adjustment_basis,
                    outcome=OUTCOME_INVALID,
                    reason_codes=("bad_bar_data",),
                    input_hash="",
                    factor_version=FACTOR_VERSION,
                )
            ]

    audit = audit_daily(
        symbol,
        [(d, v) for d, _o, _h, _l, _c, v in daily],
        as_of=as_of,
        listed_since=listed_since,
        halt_days=halt_days,
        corporate_action_days=corporate_action_days,
    )
    weeks = weekly_bars(daily, as_of=as_of, adjustment_basis=adjustment_basis)
    complete_weeks = [w for w in weeks if w.complete]
    closes = [w.close for w in complete_weeks]
    sma20_series: list[Decimal | None] = []
    sma50_series: list[Decimal | None] = []
    for i in range(len(complete_weeks)):
        # None while the window is short — never stubbed with 0, which would
        # poison the hill run (SMA20 > 0 would count as a formed hill).
        sma20_series.append(_sma(closes[: i + 1], SMA20_N))
        sma50_series.append(_sma(closes[: i + 1], SMA50_N))

    spx_by_week = _weekly_closes_from_daily(spx_closes)
    vix_by_week = _weekly_closes_from_daily(vix_closes)
    daily_closes_for_rally = [row[4] for row in daily if row[0] <= as_of]

    records: list[ShadowRecord] = []
    for i, week in enumerate(complete_weeks):
        if i + 1 < SMA50_N:
            short_reasons = ["history_short"]
            if adjustment_basis is None:
                short_reasons.append("adjustment_unavailable")
            records.append(
                _record(
                    week,
                    symbol,
                    outcome=OUTCOME_NO_DATA,
                    reasons=tuple(short_reasons),
                    sma20=None,
                    sma50=None,
                    slope_state="unavailable",
                    hill_weeks=0,
                    hill_peak=None,
                    retest="unavailable",
                    candle="unavailable",
                    volume_state="unavailable",
                    spx="unavailable",
                    vix="unavailable",
                    earnings="unavailable",
                    liquidity="unavailable",
                    rally="unavailable",
                    adjustment_basis=adjustment_basis,
                    weekly_bar_count=i + 1,
                )
            )
            continue

        # ---- no_data gates (R4/R5: missing is never negation) ----------
        reasons: list[str] = []
        if adjustment_basis is None:
            reasons.append("adjustment_unavailable")
        window_start_week = complete_weeks[i + 1 - SMA50_N].week_start
        if _gap_in_window(audit, window_start_week, as_of):
            reasons.append("daily_gap_in_window")
        spx_state = _spx_state(week, spx_by_week, spx_available)
        vix_state = _vix_week_state(week, vix_by_week, vix_available)
        earnings_state = _earnings_state(week, earnings_days, earnings_available)
        liquidity_state = _liquidity_state(daily, week)
        if spx_state == "unavailable":
            reasons.append("spx_unavailable")
        if vix_state == "unavailable":
            reasons.append("vix_unavailable")
        if earnings_state == "unavailable":
            reasons.append("earnings_calendar_unavailable")
        if liquidity_state == "unavailable":
            reasons.append("liquidity_unavailable")
        if reasons:
            records.append(
                _record(
                    week,
                    symbol,
                    outcome=OUTCOME_NO_DATA,
                    reasons=tuple(reasons),
                    sma20=sma20_series[i],
                    sma50=sma50_series[i],
                    slope_state=_slope_state(sma20_series[: i + 1]),
                    hill_weeks=_hill_weeks(sma20_series[: i + 1], sma50_series[: i + 1]),
                    hill_peak=None,
                    retest="unavailable",
                    candle=_candle_state(week),
                    volume_state=_volume_state(complete_weeks, i),
                    spx=spx_state,
                    vix=vix_state,
                    earnings=earnings_state,
                    liquidity=liquidity_state,
                    rally=_prior_rally(daily_closes_for_rally),
                    adjustment_basis=adjustment_basis,
                    weekly_bar_count=i + 1,
                )
            )
            continue

        # ---- structural formation (no_signal when the setup is absent) --
        hill_weeks, peak, peak_index = _hill(sma20_series[: i + 1], sma50_series[: i + 1])
        slope_state = _slope_state(sma20_series[: i + 1])
        structural: list[str] = []
        if sma20_series[i] <= sma50_series[i]:
            structural.append("death_cross")
        if hill_weeks and hill_weeks < HILL_MIN_WEEKS:
            structural.append("hill_too_short")
        if hill_weeks > HILL_MAX_WEEKS:
            structural.append("hill_too_long")
        if hill_weeks and peak_index is not None and peak_index == i:
            structural.append("peak_in_last_week")
        if slope_state != "declining":
            structural.append("slope_not_declining")
        retest_state = _retest_state(
            complete_weeks, i, sma20_series[: i + 1], peak_index
        )
        if retest_state == "untouched":
            structural.append("no_retest")
        if structural:
            records.append(
                _record(
                    week,
                    symbol,
                    outcome=OUTCOME_NO_SIGNAL,
                    reasons=tuple(structural),
                    sma20=sma20_series[i],
                    sma50=sma50_series[i],
                    slope_state=slope_state,
                    hill_weeks=hill_weeks,
                    hill_peak=peak,
                    retest=retest_state,
                    candle=_candle_state(week),
                    volume_state=_volume_state(complete_weeks, i),
                    spx=spx_state,
                    vix=vix_state,
                    earnings=earnings_state,
                    liquidity=liquidity_state,
                    rally=_prior_rally(daily_closes_for_rally),
                    adjustment_basis=adjustment_basis,
                    weekly_bar_count=i + 1,
                )
            )
            continue

        # ---- entry-condition filters (filtered when the setup is real) --
        filters: list[str] = []
        candle = _candle_state(week)
        if candle == "neutral":
            filters.append("candle_condition_unmet")
        volume_state = _volume_state(complete_weeks, i)
        if volume_state != "low":
            filters.append("volume_not_low")
        if spx_state == "weak":
            filters.append("spx_weak")
        if vix_state == "skip":
            filters.append("vix_skip")
        if earnings_state == "near":
            filters.append("earnings_near")
        if liquidity_state == "thin":
            filters.append("liquidity_thin")
        if filters:
            records.append(
                _record(
                    week,
                    symbol,
                    outcome=OUTCOME_FILTERED,
                    reasons=tuple(filters),
                    sma20=sma20_series[i],
                    sma50=sma50_series[i],
                    slope_state=slope_state,
                    hill_weeks=hill_weeks,
                    hill_peak=peak,
                    retest=retest_state,
                    candle=candle,
                    volume_state=volume_state,
                    spx=spx_state,
                    vix=vix_state,
                    earnings=earnings_state,
                    liquidity=liquidity_state,
                    rally=_prior_rally(daily_closes_for_rally),
                    adjustment_basis=adjustment_basis,
                    weekly_bar_count=i + 1,
                )
            )
            continue

        records.append(
            _record(
                week,
                symbol,
                outcome=OUTCOME_CANDIDATE,
                reasons=(),
                sma20=sma20_series[i],
                sma50=sma50_series[i],
                slope_state=slope_state,
                hill_weeks=hill_weeks,
                hill_peak=peak,
                retest=retest_state,
                candle=candle,
                volume_state=volume_state,
                spx=spx_state,
                vix=vix_state,
                earnings=earnings_state,
                liquidity=liquidity_state,
                rally=_prior_rally(daily_closes_for_rally),
                adjustment_basis=adjustment_basis,
                weekly_bar_count=i + 1,
            )
        )
    return records


def _gap_in_window(audit: GapAudit, window_start: date, as_of: date) -> bool:
    """A missing-session gap inside the SMA50 lookback invalidates the math."""
    return audit.has_blocking_gaps(through=as_of) and any(
        gap.day >= window_start
        for gap in audit.gaps
        if gap.category in ("internal_gap", "long_tail_gap", "halt")
    )


def _hill(
    sma20_series: list[Decimal | None], sma50_series: list[Decimal | None]
) -> tuple[int, Decimal | None, int | None]:
    """§22.3 item 1: consecutive SMA20 > SMA50 weeks ending at the last index.

    Equality interrupts the count, and so does an unfinished SMA window —
    a week whose SMA50 cannot be computed yet is not a formed hill week.
    Returns ``(weeks, peak_value, peak_index)``; the peak must not sit on
    the last week to count as a formed hill.
    """
    weeks = 0
    run_values: list[Decimal] = []
    for a, b in zip(reversed(sma20_series), reversed(sma50_series)):
        if a is None or b is None or a <= b:
            break
        weeks += 1
        run_values.append(a - b)
    if weeks == 0:
        return 0, None, None
    run_values.reverse()
    peak_value = max(run_values)
    peak_index = len(sma20_series) - weeks + run_values.index(peak_value)
    return weeks, peak_value, peak_index


def _hill_weeks(sma20_series: list[Decimal], sma50_series: list[Decimal]) -> int:
    return _hill(sma20_series, sma50_series)[0]


def _retest_state(
    weeks: list[WeekBar],
    i: int,
    sma20_series: list[Decimal | None],
    peak_index: int | None,
) -> str:
    """§22.3 item 3: the first pullback after the hill peak fixes the state.

    Touch tolerance is 0.5% against each pullback week's own SMA20; the
    OHLC range intersects the band, not just the close.
    """
    if peak_index is None:
        return "untouched"
    for j in range(peak_index + 1, i + 1):
        sma20 = sma20_series[j]
        if sma20 is None:
            continue
        w = weeks[j]
        if w.low <= sma20 * (Decimal(1) + TOUCH_TOL) and w.close >= sma20 * (
            Decimal(1) - TOUCH_TOL
        ):
            return "touched"
        if w.close < sma20 * (Decimal(1) - TOUCH_TOL):
            return "below"
    return "untouched"


def _volume_state(weeks: list[WeekBar], i: int) -> str:
    """§22.3 item 4: baseline is the prior 8 completed weeks' average."""
    if i < VOLUME_BASELINE_WEEKS:
        return "unavailable"
    baseline = (
        sum((w.volume for w in weeks[i - VOLUME_BASELINE_WEEKS : i]), Decimal(0))
        / Decimal(VOLUME_BASELINE_WEEKS)
    )
    if weeks[i].volume < baseline:
        return "low"
    return "normal"


def _spx_state(
    week: WeekBar,
    spx_by_week: dict[str, Decimal],
    spx_available: bool,
) -> str:
    """§22.3 item 5 / §28.3 R4: SPX weekly close vs its weekly SMA50 + slope."""
    if not spx_available:
        return "unavailable"
    closes = [
        spx_by_week[key]
        for key in sorted(spx_by_week)
        if key <= week.iso_week
    ]
    sma = _sma(closes, SMA50_N)
    if sma is None or not closes:
        return "unavailable"
    slope_window = closes[-SLOPE_WINDOW:] if len(closes) >= SLOPE_WINDOW else closes
    slope = _least_squares_slope(slope_window)
    if closes[-1] > sma and slope >= 0:
        return "strong"
    return "weak"


def _vix_week_state(
    week: WeekBar,
    vix_by_week: dict[str, Decimal],
    vix_available: bool,
) -> str:
    if not vix_available:
        return "unavailable"
    return _vix_state(vix_by_week.get(week.iso_week))


def _earnings_state(
    week: WeekBar,
    earnings_days: frozenset[date],
    earnings_available: bool,
) -> str:
    """§22.3 item 6: earnings within 5 ET sessions after the signal week.

    ``earnings_available`` is a caller-declared fact about the calendar
    source (V2-I8): an unconfirmed source is ``unavailable``, never a
    silent pass.
    """
    if not earnings_available:
        return "unavailable"
    week_last = week.week_start + timedelta(days=4)
    while not is_session(week_last):
        week_last -= timedelta(days=1)
    for day in next_sessions(week_last, EARNINGS_WINDOW_SESSIONS):
        if day in earnings_days:
            return "near"
    return "clear"


def _liquidity_state(
    daily: list[tuple[date, Decimal, Decimal, Decimal, Decimal, Decimal]],
    week: WeekBar,
) -> str:
    """§22.3 item 6: 20-session average dollar volume, close × volume."""
    through = week.week_start + timedelta(days=4)
    sessions = [(d, c, v) for d, _o, _h, _l, c, v in daily if d <= through]
    if len(sessions) < LIQUIDITY_SESSIONS:
        return "unavailable"
    last_n = sessions[-LIQUIDITY_SESSIONS:]
    avg = sum((c * v for _d, c, v in last_n), Decimal(0)) / Decimal(
        LIQUIDITY_SESSIONS
    )
    if avg >= LIQUIDITY_MIN_DOLLAR_VOLUME:
        return "ok"
    return "thin"


def _record(
    week: WeekBar,
    symbol: str,
    *,
    outcome: str,
    reasons: tuple[str, ...],
    sma20: Decimal | None,
    sma50: Decimal | None,
    slope_state: str,
    hill_weeks: int,
    hill_peak: Decimal | None,
    retest: str,
    candle: str,
    volume_state: str,
    spx: str,
    vix: str,
    earnings: str,
    liquidity: str,
    rally: str,
    adjustment_basis: str | None,
    weekly_bar_count: int,
) -> ShadowRecord:
    basis = {
        "adjustment_basis": adjustment_basis,
        "candle": candle,
        "earnings": earnings,
        "factor_version": FACTOR_VERSION,
        "hill_weeks": hill_weeks,
        "liquidity": liquidity,
        "outcome": outcome,
        "reasons": list(reasons),
        "retest": retest,
        "sma20": str(sma20),
        "sma50": str(sma50),
        "slope": slope_state,
        "spx": spx,
        "symbol": symbol,
        "vix": vix,
        "volume": volume_state,
        "week": week.iso_week,
        "weekly_bar_count": weekly_bar_count,
    }
    payload = json.dumps(basis, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return ShadowRecord(
        as_of_week=week.iso_week,
        symbol=symbol,
        weekly_bar_count=weekly_bar_count,
        sma20=sma20,
        sma50=sma50,
        sma20_slope_state=slope_state,
        hill_weeks=hill_weeks,
        hill_peak=hill_peak,
        retest_state=retest,
        candle_state=candle,
        volume_state=volume_state,
        spx_filter=spx,
        vix_filter=vix,
        earnings_filter=earnings,
        liquidity_filter=liquidity,
        prior_rally=rally,
        instrument_availability=DEFERRED_P5,
        adjustment_basis=adjustment_basis,
        outcome=outcome,
        reason_codes=reasons,
        input_hash=digest,
        factor_version=FACTOR_VERSION,
    )
