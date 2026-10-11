"""RSI and nearby highs and lows. Too few daily bars leaves the tags off."""

from __future__ import annotations

import logging
from decimal import Decimal

import psycopg

from athena_settings.model import SettingsPayload
from athena_strategy.tags import OVERBOUGHT, OVERSOLD, PRICE_WEAK, RESISTANCE_TOUCH, SUPPORT_TOUCH, VOLUME_SPIKE

log = logging.getLogger("athena.strategy")


def rsi(closes: list[Decimal], period: int) -> Decimal | None:
    """Wilder RSI. None until there are period + 1 closes."""
    if len(closes) < period + 1:
        return None
    gains: list[Decimal] = []
    losses: list[Decimal] = []
    for previous, close in zip(closes, closes[1:]):
        change = close - previous
        gains.append(change if change > 0 else Decimal(0))
        losses.append(-change if change < 0 else Decimal(0))
    average_gain = sum(gains[:period]) / Decimal(period)
    average_loss = sum(losses[:period]) / Decimal(period)
    for gain, loss in zip(gains[period:], losses[period:]):
        average_gain = (average_gain * Decimal(period - 1) + gain) / Decimal(period)
        average_loss = (average_loss * Decimal(period - 1) + loss) / Decimal(period)
    if average_loss == 0:
        return Decimal(100)
    strength = average_gain / average_loss
    return Decimal(100) - (Decimal(100) / (Decimal(1) + strength))


def price_tags(
    highs: list[Decimal],
    lows: list[Decimal],
    closes: list[Decimal],
    *,
    period: int,
    overbought: int,
    oversold: int,
    lookback: int,
    touch_pct: int,
    volumes: list[Decimal] | None = None,
    volume_multiple: int = 2,
) -> set[str]:
    """Price location, a weaker close, and a volume spike."""
    tags: set[str] = set()
    value = rsi(closes, period)
    if value is not None:
        if value >= overbought:
            tags.add(OVERBOUGHT)
        elif value <= oversold:
            tags.add(OVERSOLD)
    if len(lows) >= lookback and len(highs) >= lookback and closes:
        window_low = min(lows[-lookback:])
        window_high = max(highs[-lookback:])
        close = closes[-1]
        band = Decimal(touch_pct) / Decimal(100)
        if window_low > 0 and abs(close - window_low) / window_low <= band:
            tags.add(SUPPORT_TOUCH)
        if window_high > 0 and abs(close - window_high) / window_high <= band:
            tags.add(RESISTANCE_TOUCH)
    if len(closes) > lookback and closes[-1] < closes[-1 - lookback]:
        tags.add(PRICE_WEAK)
    if volumes is not None and len(volumes) > lookback:
        prior = volumes[-(lookback + 1):-1]
        average = sum(prior) / Decimal(len(prior))
        if average > 0 and volumes[-1] >= average * Decimal(volume_multiple):
            tags.add(VOLUME_SPIKE)
    return tags


def tags_from_prices(conn: psycopg.Connection, symbol: str, settings: SettingsPayload) -> set[str]:
    from athena_strategy.simbars import load_daily

    rows = load_daily(conn, symbol)
    strategy = settings.strategy
    needed = max(strategy.rsi_period + 1, strategy.level_lookback)
    if len(rows) < needed:
        log.info("price tags pending symbol=%s bars=%s", symbol, len(rows))
        return set()
    highs = [row[0] for row in rows]
    lows = [row[1] for row in rows]
    closes = [row[2] for row in rows]
    volumes = [row[3] for row in rows]
    tags = price_tags(
        highs,
        lows,
        closes,
        period=strategy.rsi_period,
        overbought=strategy.rsi_overbought,
        oversold=strategy.rsi_oversold,
        lookback=strategy.level_lookback,
        touch_pct=strategy.touch_pct,
        volumes=volumes,
        volume_multiple=strategy.volume_spike_multiple,
    )
    if tags:
        log.info("price tags symbol=%s tags=%s", symbol, ",".join(sorted(tags)))
    return tags
