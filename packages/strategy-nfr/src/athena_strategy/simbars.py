"""Daily bars for a paper replay. Live reads go through pricing.daily_bars."""

from __future__ import annotations

import psycopg


def replay_cursor(conn: psycopg.Connection, symbol: str):
    """The replay date when this symbol is inside an active run. None on the live book."""
    row = conn.execute(
        """
        SELECT cursor_date FROM sim_runs
        WHERE status IN ('active', 'halted') AND cursor_date IS NOT NULL
          AND position(',' || %s || ',' in ',' || symbols || ',') > 0
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (symbol.upper(),),
    ).fetchone()
    if row is None:
        return None
    return row[0]


def in_replay(conn: psycopg.Connection, symbol: str) -> bool:
    return replay_cursor(conn, symbol) is not None


def load_daily(conn: psycopg.Connection, symbol: str) -> list[tuple]:
    """(high, low, close, volume), oldest first, through the replay date."""
    symbol = symbol.upper()
    cursor = replay_cursor(conn, symbol)
    if cursor is None:
        from athena_marketdata.pricing import daily_bars

        return [
            (bar.high, bar.low, bar.close, bar.volume)
            for bar in daily_bars(conn, symbol, 3000)
        ]
    # Replay exemption: this query reads sim_bars, not market_bars.
    return list(
        conn.execute(
            """
            SELECT high, low, close, volume FROM sim_bars
            WHERE symbol = %s AND ts::date <= %s
              AND run_id = (
                  SELECT id FROM sim_runs
                  WHERE status IN ('active', 'halted') AND cursor_date IS NOT NULL
                    AND position(',' || %s || ',' in ',' || symbols || ',') > 0
                  ORDER BY created_at DESC
                  LIMIT 1
              )
            ORDER BY ts
            """,
            (symbol, cursor, symbol),
        ).fetchall()
    )


def last_close(conn: psycopg.Connection, symbol: str):
    rows = load_daily(conn, symbol)
    if not rows:
        return None
    return rows[-1][2]
