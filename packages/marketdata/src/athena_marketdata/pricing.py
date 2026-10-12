"""The only production reader of market_bars.

Rails, drawdown, and RV20 use completed ib-daily bars. An ib-delayed print
does not replace that close and does not add an unfinished day. The delayed
print stays the limit price.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import psycopg


@dataclass(frozen=True)
class Bar:
    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


def daily_bars(conn: psycopg.Connection, symbol: str, n: int) -> list[Bar]:
    """Completed ib-daily bars, oldest first, at most n days."""
    if n <= 0:
        return []
    rows = conn.execute(
        """
        SELECT ts, open, high, low, close, volume, source
        FROM market_bars
        WHERE symbol = %s
          AND source IN ('ib-daily', 'ib-delayed')
          AND close > 0
        ORDER BY ts
        """,
        (symbol.upper(),),
    ).fetchall()
    merged = _merge(rows)
    return merged[-n:]


def last_daily_close(conn: psycopg.Connection, symbol: str) -> Decimal | None:
    """Close of the newest completed daily bar. Not the delayed print."""
    bars = daily_bars(conn, symbol, 1)
    if not bars:
        return None
    return bars[-1].close


def last_intraday_price(conn: psycopg.Connection, symbol: str) -> Decimal | None:
    """Newest ib-delayed close. None when that source has no row."""
    found = _latest_intraday(conn, symbol)
    if found is None:
        return None
    return found[1]


def last_tradeable_price(conn: psycopg.Connection, symbol: str) -> Decimal | None:
    """Intraday print, otherwise the daily close. Not for rails or IV."""
    price = last_intraday_price(conn, symbol)
    if price is not None:
        return price
    return last_daily_close(conn, symbol)


def year_band(conn: psycopg.Connection, symbol: str) -> str:
    """52-week high/low once 252 daily sessions exist. Otherwise -/-."""
    return year_view(conn, symbol)[0]


def year_view(conn: psycopg.Connection, symbol: str) -> tuple[str, str, str]:
    """High/low, then today's percent versus the high and versus the low.

    A short history returns (-/-, "", ""). A missing price returns the range
    and a single -/- for the percent line.
    """
    bars = daily_bars(conn, symbol, 252)
    if len(bars) < 252:
        return "-/-", "", ""
    high = max(bar.high for bar in bars)
    low = min(bar.low for bar in bars)
    band = f"{high:.2f}/{low:.2f}"
    last = last_tradeable_price(conn, symbol)
    if last is None or last <= 0 or high <= 0 or low <= 0:
        return band, "-/-", ""
    return band, _versus(last, high), _versus(last, low)


def _versus(last: Decimal, level: Decimal) -> str:
    change = (last - level) / level * Decimal(100)
    return f"{change:+.2f}%"


def quote_pair(conn: psycopg.Connection, symbol: str) -> tuple[Decimal | None, Decimal | None]:
    """Latest tradeable price and the prior day's close."""
    intra = _latest_intraday(conn, symbol)
    bars = daily_bars(conn, symbol, 8)
    if intra is not None:
        last_ts, last = intra
        day = last_ts.date() if isinstance(last_ts, datetime) else last_ts
        previous = None
        for bar in reversed(bars):
            if bar.day >= day or bar.close == 0:
                continue
            previous = bar.close
            break
        return last, previous
    if not bars:
        return None, None
    previous = None
    for bar in reversed(bars[:-1]):
        if bar.close == 0:
            continue
        previous = bar.close
        break
    return bars[-1].close, previous


def has_ib_daily(conn: psycopg.Connection, symbol: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM market_bars
        WHERE symbol = %s AND source = 'ib-daily'
        LIMIT 1
        """,
        (symbol.upper(),),
    ).fetchone()
    return row is not None


def has_daily_on(conn: psycopg.Connection, symbol: str, day: date) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM market_bars
        WHERE symbol = %s AND source = 'ib-daily' AND ts::date = %s
        LIMIT 1
        """,
        (symbol.upper(), day),
    ).fetchone()
    return row is not None


def has_intraday(conn: psycopg.Connection, symbol: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM market_bars
        WHERE symbol = %s AND source = 'ib-delayed'
        LIMIT 1
        """,
        (symbol.upper(),),
    ).fetchone()
    return row is not None


def ib_daily_count(conn: psycopg.Connection, symbol: str) -> int:
    row = conn.execute(
        """
        SELECT count(DISTINCT ts::date) FROM market_bars
        WHERE symbol = %s AND source = 'ib-daily'
        """,
        (symbol.upper(),),
    ).fetchone()
    return int(row[0] if row else 0)


def set_history_short(conn: psycopg.Connection, symbol: str, short: bool) -> None:
    conn.execute(
        """
        INSERT INTO symbol_facts (symbol, name, history_short)
        VALUES (%s, '', %s)
        ON CONFLICT (symbol) DO UPDATE
        SET history_short = EXCLUDED.history_short,
            updated_at = now()
        """,
        (symbol.upper(), short),
    )


def store_ib_daily(conn: psycopg.Connection, symbol: str, bars: list[tuple]) -> int:
    """Write IB daily OHLC. Same symbol, day, and source updates in place."""
    written = 0
    symbol = symbol.upper()
    for bar in bars:
        day = bar[0]
        if isinstance(day, datetime):
            day = day.date()
        ts = datetime(day.year, day.month, day.day, tzinfo=UTC)
        conn.execute(
            """
            INSERT INTO market_bars (symbol, ts, open, high, low, close, volume, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'ib-daily')
            ON CONFLICT (symbol, ts, source) DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                volume = EXCLUDED.volume
            """,
            (symbol, ts, bar[1], bar[2], bar[3], bar[4], bar[5]),
        )
        written += 1
    return written


def _latest_intraday(conn: psycopg.Connection, symbol: str):
    row = conn.execute(
        """
        SELECT ts, close FROM market_bars
        WHERE symbol = %s AND source = 'ib-delayed' AND close > 0
        ORDER BY ts DESC
        LIMIT 1
        """,
        (symbol.upper(),),
    ).fetchone()
    if row is None:
        return None
    return row[0], Decimal(row[1])


def _merge(rows: list[tuple]) -> list[Bar]:
    grouped: dict[date, list[tuple]] = {}
    for ts, open_, high, low, close, volume, source in rows:
        day = ts.date() if isinstance(ts, datetime) else ts
        grouped.setdefault(day, []).append((ts, open_, high, low, close, volume, source))
    bars: list[Bar] = []
    for day in sorted(grouped):
        group = grouped[day]
        daily = [row for row in group if row[6] == "ib-daily"]
        if not daily:
            continue
        base = max(daily, key=lambda row: row[0])
        bars.append(
            Bar(
                day=day,
                open=Decimal(base[1]),
                high=Decimal(base[2]),
                low=Decimal(base[3]),
                close=Decimal(base[4]),
                volume=Decimal(base[5]),
            )
        )
    return bars
