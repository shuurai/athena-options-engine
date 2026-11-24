"""VIX manual sync + pure state derivation (booklet 14, §13.9; D103/D104/D108).

VIX is an INDEX level with **no producer anywhere in this repo**: no CBOE feed,
no IB index subscription, and nothing in ``UNIVERSE_SEED`` (§24.2 #6). D102/D103
and D108 therefore make it a **manual, operator-triggered** sync into
``market_bars`` with ``source='ib-vix'``.

Why the existing table: ``market_bars.source`` is plain text, so a new source
needs no migration (D104). Why it stays inert: every existing reader filters
``source IN ('ib-daily', 'ib-delayed')`` — see ``pricing.daily_bars`` — so VIX
rows never leak into rails, RV20 or the 52-week band.

The thresholds below are **待校準** (§13.9): they are recorded inputs, not
ratified constants, and they are never acceptance criteria.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

import psycopg

log = logging.getLogger("athena.marketdata.vix")

SOURCE = "ib-vix"
# D103: the operator asked for the index plus its term structure.
VIX_SYMBOLS = ("VIX", "VIX9D", "VIX3M", "VIX1Y")

# 待校準 (unratified inputs, D72 evidence only).
SLOPE_WINDOW = 5
SLOPE_MOVE_PCT = 5.0
HIGH_LEVEL = 20.0        # "高位" for §13.4's 高位再升
EARLY_HIGH_LEVEL = 30.0  # §13.4's 早高回落但 >30 threshold


def _as_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _as_day(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def store_vix_daily(conn: psycopg.Connection, symbol: str, candles) -> int:
    """Upsert daily VIX candles as ``source='ib-vix'``. Returns rows written.

    ``candles`` are the dicts from ``IbBroker.historical()`` (``date``/``open``/
    ``high``/``low``/``close``). A row with no usable close is skipped rather
    than stored as zero — a fabricated level is worse than a gap (D68).
    """
    written = 0
    name = str(symbol or "").strip().upper()
    if not name:
        return 0
    for candle in candles or []:
        day = _as_day(candle.get("date"))
        close = _as_float(candle.get("close"))
        if day is None or close is None:
            continue
        ts = datetime(day.year, day.month, day.day, tzinfo=UTC)
        conn.execute(
            """
            INSERT INTO market_bars (symbol, ts, open, high, low, close, volume, source)
            VALUES (%s, %s, %s, %s, %s, %s, 0, %s)
            ON CONFLICT (symbol, ts, source) DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close
            """,
            (
                name, ts,
                _as_float(candle.get("open")) or close,
                _as_float(candle.get("high")) or close,
                _as_float(candle.get("low")) or close,
                close, SOURCE,
            ),
        )
        written += 1
    return written


def vix_closes(conn: psycopg.Connection, symbol: str, n: int = 30) -> list[tuple[date, float]]:
    """Newest ``n`` closes for one VIX series, oldest first. Missing -> []."""
    if n <= 0:
        return []
    rows = conn.execute(
        """
        SELECT ts::date, close
        FROM market_bars
        WHERE symbol = %s AND source = %s AND close > 0
        ORDER BY ts
        """,
        (str(symbol or "").strip().upper(), SOURCE),
    ).fetchall()
    series = [(row[0], _as_float(row[1])) for row in rows]
    return [(day, level) for day, level in series if day is not None and level is not None][-n:]


def latest_levels(conn: psycopg.Connection) -> dict[str, float]:
    """Latest close per VIX symbol. Missing symbols are simply absent."""
    levels: dict[str, float] = {}
    for symbol in VIX_SYMBOLS:
        series = vix_closes(conn, symbol, 1)
        if series:
            levels[symbol] = series[-1][1]
    return levels


def slope_state(closes) -> str | None:
    """``rising_from_low`` / ``flat`` / ``falling``, or None with too few points.

    待校準: a >5% move across the last 5 sessions counts as a slope; a rise only
    counts as ``rising_from_low`` when the window's first close is in its lower
    third, which is what §13.9's "低档连升" describes.
    """
    points = [level for _day, level in (closes or [])][-SLOPE_WINDOW:]
    if len(points) < 3:
        return None
    first, last = points[0], points[-1]
    if first <= 0:
        return None
    change_pct = (last - first) / first * 100.0
    if abs(change_pct) < SLOPE_MOVE_PCT:
        return "flat"
    if change_pct > 0:
        low, high = min(points), max(points)
        span = high - low
        at_low = span <= 0 or (first - low) <= span / 3.0
        return "rising_from_low" if at_low else "flat"
    return "falling"


def vix_state(closes) -> str | None:
    """§13.4's ``VIX 水位``: high_rising / early_high_fell_above_30 / falling, plus sentinels.

    The three values that appear in §13.4's rows are returned here. Two further
    values are **measured** but describe no row, so they block every dependent
    row exactly like a mismatch while carrying no gap reason (D73):

    * ``rising_not_high`` — up ≥5% but still below the high level
    * ``quiet`` — no meaningful move either way

    Calling a +15% climb "quiet" would be dishonest, which is why the two are
    separate.
    """
    points = [level for _day, level in (closes or [])][-SLOPE_WINDOW:]
    if len(points) < 3:
        return None
    first, latest, peak = points[0], points[-1], max(points)
    if latest > EARLY_HIGH_LEVEL and latest < peak:
        return "early_high_fell_above_30"
    change_pct = (latest - first) / first * 100.0 if first > 0 else 0.0
    if latest >= HIGH_LEVEL and change_pct >= -SLOPE_MOVE_PCT:
        return "high_rising"
    if change_pct <= -SLOPE_MOVE_PCT:
        return "falling"
    if change_pct >= SLOPE_MOVE_PCT:
        return "rising_not_high"
    return "quiet"


def term_state(levels) -> str | None:
    """``front_end_hot`` / ``contango`` / ``flat`` / ``inversion``, or None.

    Needs VIX and VIX3M at minimum; with only one series there is no term
    structure to compare, so the dimension stays unknown (D68).
    """
    spot = _as_float((levels or {}).get("VIX"))
    three_month = _as_float((levels or {}).get("VIX3M"))
    if spot is None or three_month is None:
        return None
    nine_day = _as_float((levels or {}).get("VIX9D"))
    if nine_day is not None and nine_day > spot > three_month:
        return "front_end_hot"
    if spot > three_month:
        return "inversion"
    one_year = _as_float((levels or {}).get("VIX1Y"))
    if one_year is not None and spot < three_month < one_year:
        return "contango"
    return "flat"
