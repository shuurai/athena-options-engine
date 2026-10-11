"""Deterministic indicators. Missing data is unknown, never a fake zero."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from athena_marketdata.pricing import Bar, daily_bars
from athena_settings.model import SettingsPayload, StrategySettings
from athena_strategy.iv import RV_METHOD, iv_rank
from athena_strategy.tags import (
    ATR_WIDE,
    DIP_BOUGHT,
    DRAWDOWN_OK,
    RECLAIM,
    REGIME_RISK_OFF,
    RV_HIGH,
    RV_LOW,
    TREND_STRONG,
    TREND_WEAK,
    VOLUME_DRY,
)

_ET = ZoneInfo("America/New_York")
_HUNDRED = Decimal(100)


def indicators_enabled() -> bool:
    flag = os.environ.get("ATHENA_INDICATORS", "on").strip().lower()
    return flag not in {"0", "off", "false", "no"}


@dataclass
class Indicators:
    tags: set[str] = field(default_factory=set)
    rail_pos: Decimal | None = None
    trend_slope_pct: Decimal | None = None
    drawdown_pct: Decimal | None = None
    atr: Decimal | None = None
    atr_pct: Decimal | None = None
    rv20: Decimal | None = None
    rv_pctl: int | None = None
    volume_x: Decimal | None = None
    range_pctl: int | None = None
    earnings_in: int | None = None
    news_score: Decimal | None = None
    regime_unknown: bool = True

    def line(self) -> str:
        """Prompt numbers. There is no iv field."""
        return " ".join(
            (
                f"rail_pos={_pos(self.rail_pos)}",
                f"trend={_signed(self.trend_slope_pct)}",
                f"dd={_signed(self.drawdown_pct)}",
                f"atr={_plain(self.atr)}",
                f"atr_pct={_percent(self.atr_pct)}",
                f"rv20={_plain(self.rv20)}",
                f"rv_pctl={_whole(self.rv_pctl)}",
                f"volume_x={_ratio(self.volume_x)}",
                f"range_pctl={_whole(self.range_pctl)}",
                f"earnings_in={_whole(self.earnings_in)}",
            )
        )

    def payload(self) -> dict:
        return {
            "rail_pos": _json(self.rail_pos),
            "trend_slope_pct": _json(self.trend_slope_pct),
            "drawdown_pct": _json(self.drawdown_pct),
            "atr": _json(self.atr),
            "atr_pct": _json(self.atr_pct),
            "rv20": _json(self.rv20),
            "rv_pctl": self.rv_pctl,
            "volume_x": _json(self.volume_x),
            "range_pctl": self.range_pctl,
            "earnings_in": self.earnings_in,
            "news_score": _json(self.news_score),
            "regime_unknown": self.regime_unknown,
            "tags": sorted(self.tags),
        }


def compute(
    bars: list[Bar],
    strategy: StrategySettings,
    *,
    rv_samples: list[Decimal] | None = None,
    regime_bars: list[Bar] | None = None,
    earnings_in: int | None = None,
    news_score: Decimal | None = None,
) -> Indicators:
    """Tags and numbers from one daily series. Callers pass stored RV samples."""
    reading = Indicators(earnings_in=earnings_in, news_score=news_score)
    _rails(reading, bars, strategy)
    _reclaim(reading, bars, strategy)
    if _volume_ok(bars):
        _volume(reading, bars, strategy)
        _atr(reading, bars, strategy)
        _rv(reading, bars, strategy, rv_samples or [])
    _range(reading, bars, strategy)
    _regime(reading, regime_bars, strategy)
    return reading


def read_indicators(conn: psycopg.Connection, settings: SettingsPayload, symbol: str) -> Indicators:
    """Load bars, record today's RV20, and return one reading."""
    strategy = settings.strategy
    need = max(
        strategy.range_min_sessions,
        strategy.rail_lookback + strategy.trend_lag_sessions + strategy.dip_window_sessions,
        strategy.rv_window + 1,
        strategy.atr_window + 1,
        strategy.volume_avg_sessions + 1,
        strategy.reclaim_window + 1,
    )
    bars = load_bars(conn, symbol, need)
    today = as_of(conn, symbol)
    if symbol.upper() == strategy.regime_index.upper():
        regime = bars
    else:
        regime = load_bars(conn, strategy.regime_index, strategy.regime_lookback)
    earned = earnings_in_days(conn, symbol, today)
    score = news_score(conn, settings, symbol, today)
    draft = compute(bars, strategy, regime_bars=regime, earnings_in=earned, news_score=score)
    if draft.rv20 is None:
        return draft
    record_rv_mark(conn, symbol, today, draft.rv20)
    samples = rv_samples(conn, symbol)
    return compute(
        bars,
        strategy,
        rv_samples=samples,
        regime_bars=regime,
        earnings_in=earned,
        news_score=score,
    )


def store_reading(conn: psycopg.Connection, account_id: str, symbol: str, reading: Indicators) -> None:
    conn.execute(
        """
        INSERT INTO symbol_phases (account_id, symbol, phase, tags, tactic, indicators)
        VALUES (%s, %s, 'P1', '', 'HOLD', %s)
        ON CONFLICT (account_id, symbol) DO UPDATE
        SET indicators = EXCLUDED.indicators, updated_at = now()
        """,
        (account_id, symbol.upper(), Jsonb(reading.payload())),
    )


def load_bars(conn: psycopg.Connection, symbol: str, n: int) -> list[Bar]:
    from athena_strategy.simbars import in_replay, load_daily

    if in_replay(conn, symbol):
        found = []
        for row in load_daily(conn, symbol)[-n:]:
            found.append(
                Bar(
                    day=date.min,
                    open=row[2],
                    high=row[0],
                    low=row[1],
                    close=row[2],
                    volume=row[3] if len(row) > 3 else Decimal(0),
                )
            )
        return found
    return daily_bars(conn, symbol, n)


def as_of(conn: psycopg.Connection, symbol: str) -> date:
    from athena_strategy.simbars import replay_cursor

    return replay_cursor(conn, symbol) or date.today()


def record_rv_mark(conn: psycopg.Connection, symbol: str, day: date, value: Decimal) -> None:
    """One RV20 row per symbol per day. Older days are not rewritten."""
    conn.execute(
        """
        INSERT INTO rv_marks (symbol, day, rv20, method)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (symbol, day) DO UPDATE SET
            rv20 = EXCLUDED.rv20,
            method = EXCLUDED.method
        """,
        (symbol.upper(), day, value, RV_METHOD),
    )


def rv_samples(conn: psycopg.Connection, symbol: str) -> list[Decimal]:
    rows = conn.execute(
        """
        SELECT rv20 FROM rv_marks
        WHERE symbol = %s AND method = %s
        ORDER BY day
        """,
        (symbol.upper(), RV_METHOD),
    ).fetchall()
    return [row[0] for row in rows]


def earnings_in_days(conn: psycopg.Connection, symbol: str, today: date) -> int | None:
    rows = conn.execute(
        """
        SELECT event_date FROM calendar_marks
        WHERE kind = 'EARNINGS' AND symbol = %s
        """,
        (symbol.upper(),),
    ).fetchall()
    offsets = []
    for row in rows:
        if isinstance(row[0], datetime):
            offsets.append((row[0].date() - today).days)
        elif isinstance(row[0], date):
            offsets.append((row[0] - today).days)
    if not offsets:
        return None
    return min(offsets, key=lambda days: (abs(days), days))


def news_score(conn: psycopg.Connection, settings: SettingsPayload, symbol: str, today: date) -> Decimal | None:
    """Recency-weighted mean of stored headline sentiment. Newer rows count more."""
    start = datetime.combine(today - timedelta(days=settings.news.wash_window_days), datetime.min.time(), _ET)
    rows = conn.execute(
        """
        SELECT sentiment FROM news_events
        WHERE symbol = %s AND ts >= %s AND dismissed = false AND sentiment IS NOT NULL
        ORDER BY ts
        """,
        (symbol.upper(), start),
    ).fetchall()
    if not rows:
        return None
    total = Decimal(0)
    weight = Decimal(0)
    for index, row in enumerate(rows, start=1):
        step = Decimal(index)
        total += Decimal(row[0]) * step
        weight += step
    if weight == 0:
        return None
    return total / weight


def realized_vol(closes: list[Decimal], window: int, days_per_year: int) -> Decimal | None:
    """Annualized sample stdev of log returns, in percent. Scale-invariant."""
    if len(closes) < window + 1 or window < 2 or days_per_year < 1:
        return None
    returns = _log_returns(closes[-(window + 1):])
    if len(returns) < window:
        return None
    sigma = _stdev(returns[-window:])
    if sigma is None:
        return None
    return sigma * Decimal(days_per_year).sqrt() * _HUNDRED


def _rails(reading: Indicators, bars: list[Bar], strategy: StrategySettings) -> None:
    lookback = strategy.rail_lookback
    if len(bars) < lookback:
        return
    high, low = _span(bars[-lookback:])
    last = bars[-1].close
    if high > low and last is not None:
        reading.rail_pos = (last - low) / (high - low)
        reading.drawdown_pct = (last - high) / high * _HUNDRED
        deep = Decimal(str(strategy.drawdown_band_high_pct))
        shallow = Decimal(str(strategy.drawdown_band_low_pct))
        if -deep <= reading.drawdown_pct <= -shallow:
            reading.tags.add(DRAWDOWN_OK)
    lag = strategy.trend_lag_sessions
    if len(bars) < lookback + lag:
        return
    prior_high, prior_low = _span(bars[-(lookback + lag):-lag])
    high_slope = _slope(high, prior_high)
    low_slope = _slope(low, prior_low)
    if high_slope is None or low_slope is None:
        return
    reading.trend_slope_pct = (high_slope + low_slope) / Decimal(2) * _HUNDRED
    minimum = Decimal(str(strategy.trend_slope_min_pct))
    if high_slope * _HUNDRED >= minimum and low_slope * _HUNDRED >= minimum:
        reading.tags.add(TREND_STRONG)
    elif high_slope * _HUNDRED <= -minimum and low_slope * _HUNDRED <= -minimum:
        reading.tags.add(TREND_WEAK)
    if TREND_STRONG not in reading.tags or reading.rail_pos is None:
        return
    reclaim = Decimal(str(strategy.dip_reclaim_pos))
    if reading.rail_pos < reclaim:
        return
    if _was_deep(bars, strategy):
        reading.tags.add(DIP_BOUGHT)


def _was_deep(bars: list[Bar], strategy: StrategySettings) -> bool:
    deep = Decimal(str(strategy.dip_deep_pos))
    start = max(0, len(bars) - strategy.dip_window_sessions)
    for index in range(start, len(bars)):
        pos = _pos_at(bars, index, strategy.rail_lookback)
        if pos is not None and pos <= deep:
            return True
    return False


def _pos_at(bars: list[Bar], index: int, lookback: int) -> Decimal | None:
    begin = index - lookback + 1
    if begin < 0:
        return None
    high, low = _span(bars[begin:index + 1])
    last = bars[index].close
    if high <= low:
        return None
    return (last - low) / (high - low)


def _reclaim(reading: Indicators, bars: list[Bar], strategy: StrategySettings) -> None:
    window = strategy.reclaim_window
    if len(bars) < window + 1:
        return
    prior = bars[-(window + 1):-1]
    last = bars[-1].close
    mean = sum((bar.close for bar in prior), Decimal(0)) / Decimal(len(prior))
    if last > mean or last > bars[-2].high:
        reading.tags.add(RECLAIM)


def _volume(reading: Indicators, bars: list[Bar], strategy: StrategySettings) -> None:
    window = strategy.volume_avg_sessions
    if len(bars) < window + 1:
        return
    prior = bars[-(window + 1):-1]
    average = sum((bar.volume for bar in prior), Decimal(0)) / Decimal(len(prior))
    if average <= 0:
        return
    reading.volume_x = bars[-1].volume / average
    if reading.volume_x < Decimal(str(strategy.volume_dry_multiple)):
        reading.tags.add(VOLUME_DRY)


def _atr(reading: Indicators, bars: list[Bar], strategy: StrategySettings) -> None:
    window = strategy.atr_window
    if len(bars) < window + 1:
        return
    true_ranges: list[Decimal] = []
    for previous, bar in zip(bars, bars[1:]):
        true_ranges.append(max(bar.high - bar.low, abs(bar.high - previous.close), abs(bar.low - previous.close)))
    if len(true_ranges) < window:
        return
    value = sum(true_ranges[:window]) / Decimal(window)
    for item in true_ranges[window:]:
        value = (value * Decimal(window - 1) + item) / Decimal(window)
    last = bars[-1].close
    if last <= 0:
        return
    reading.atr = value
    reading.atr_pct = value / last * _HUNDRED
    if reading.atr_pct >= Decimal(strategy.atr_wide_ratio_pct):
        reading.tags.add(ATR_WIDE)


def _rv(reading: Indicators, bars: list[Bar], strategy: StrategySettings, samples: list[Decimal]) -> None:
    closes = [bar.close for bar in bars if bar.close is not None and bar.close > 0]
    reading.rv20 = realized_vol(closes, strategy.rv_window, strategy.trading_days_per_year)
    if reading.rv20 is None or len(samples) == 0:
        return
    rank = iv_rank(samples, samples[-1], strategy.ivr_min_samples)
    reading.rv_pctl = rank
    if rank is None:
        return
    if rank >= strategy.rv_high_pctl:
        reading.tags.add(RV_HIGH)
    elif rank <= strategy.rv_low_pctl:
        reading.tags.add(RV_LOW)


def _range(reading: Indicators, bars: list[Bar], strategy: StrategySettings) -> None:
    if len(bars) < strategy.range_min_sessions:
        return
    last = bars[-1].close
    below = sum(1 for bar in bars if bar.close <= last)
    reading.range_pctl = int(Decimal(below) / Decimal(len(bars)) * _HUNDRED)


def _regime(reading: Indicators, bars: list[Bar] | None, strategy: StrategySettings) -> None:
    if not bars or len(bars) < strategy.regime_lookback:
        reading.regime_unknown = True
        return
    window = bars[-strategy.regime_lookback:]
    high, low = _span(window)
    reading.regime_unknown = False
    if high + low <= 0:
        return
    if window[-1].close < (high + low) / Decimal(2):
        reading.tags.add(REGIME_RISK_OFF)


def _volume_ok(bars: list[Bar]) -> bool:
    return any(bar.volume > 0 for bar in bars)


def _span(bars: list[Bar]) -> tuple[Decimal, Decimal]:
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    return max(highs), min(lows)


def _slope(now: Decimal, prior: Decimal) -> Decimal | None:
    if prior <= 0:
        return None
    return (now - prior) / prior


def _log_returns(closes: list[Decimal]) -> list[Decimal]:
    found = []
    for previous, close in zip(closes, closes[1:]):
        if previous <= 0 or close <= 0:
            continue
        found.append(Decimal(str(math.log(float(close / previous)))))
    return found


def _stdev(values: list[Decimal]) -> Decimal | None:
    count = len(values)
    if count < 2:
        return None
    mean = sum(values) / Decimal(count)
    variance = sum((item - mean) ** 2 for item in values) / Decimal(count - 1)
    return variance.sqrt()


def _json(value: Decimal | None):
    if value is None:
        return None
    return float(value)


def _missing(value) -> bool:
    return value is None


def _pos(value: Decimal | None) -> str:
    if _missing(value):
        return "unknown"
    return f"{value:.2f}"


def _plain(value: Decimal | None) -> str:
    if _missing(value):
        return "unknown"
    return f"{value:.1f}"


def _signed(value: Decimal | None) -> str:
    if _missing(value):
        return "unknown"
    return f"{value:+.1f}%"


def _percent(value: Decimal | None) -> str:
    if _missing(value):
        return "unknown"
    return f"{value:.1f}%"


def _ratio(value: Decimal | None) -> str:
    if _missing(value):
        return "unknown"
    return f"{value:.2f}"


def _whole(value: int | None) -> str:
    if value is None:
        return "unknown"
    return str(value)
