"""IV rank from stored strangle premium. The proxy is not a volatility tag."""

from __future__ import annotations

import logging
import math
from datetime import date
from decimal import Decimal

import psycopg

from athena_settings.model import SettingsPayload

log = logging.getLogger("athena.strategy")

PREMIUM_METHOD = "premium_ratio"
RV_METHOD = "close_log_252"
RANK_FLOOR = 20
INVERT_CORR = 0.90


def iv_rank(samples: list[Decimal], current: Decimal, minimum: int) -> int | None:
    """(current - low) / (high - low), as a percent. None until the window is full."""
    if minimum < RANK_FLOOR:
        minimum = RANK_FLOOR
    if len(samples) < minimum:
        return None
    low = min(samples)
    high = max(samples)
    if high == low:
        return None
    rank = (current - low) / (high - low) * Decimal(100)
    return int(rank)


def estimated_iv(conn: psycopg.Connection, symbol: str, today: date) -> Decimal | None:
    """No measured IV in this step. Callers must not invent one."""
    return None


def iv_samples(conn: psycopg.Connection, symbol: str, method: str = PREMIUM_METHOD) -> list[Decimal]:
    rows = conn.execute(
        """
        SELECT iv_proxy FROM iv_marks
        WHERE symbol = %s AND method = %s
        ORDER BY day
        """,
        (symbol, method),
    ).fetchall()
    return [row[0] for row in rows]


def record_iv_marks(conn: psycopg.Connection, symbols: list[str], today: date) -> int:
    """One premium-ratio point per symbol per day. Returns rows written."""
    written = 0
    for symbol in symbols:
        plan = conn.execute(
            """
            SELECT put_limit, call_limit FROM option_plans
            WHERE symbol = %s AND put_limit > 0 AND call_limit > 0
            """,
            (symbol,),
        ).fetchone()
        if plan is None:
            continue
        from athena_strategy.simbars import in_replay, last_close

        if in_replay(conn, symbol):
            close = last_close(conn, symbol)
            price = None if close is None else (close,)
        else:
            from athena_marketdata.pricing import last_daily_close

            close = last_daily_close(conn, symbol)
            price = None if close is None else (close,)
        if price is None or price[0] is None or price[0] <= 0:
            continue
        credit = plan[0] + plan[1]
        proxy = credit / price[0]
        conn.execute(
            """
            INSERT INTO iv_marks (symbol, day, credit, underlying, iv_proxy, method, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (symbol, day) DO UPDATE SET
                credit = EXCLUDED.credit,
                underlying = EXCLUDED.underlying,
                iv_proxy = EXCLUDED.iv_proxy,
                method = EXCLUDED.method,
                updated_at = now()
            """,
            (symbol, today, credit, price[0], proxy, PREMIUM_METHOD),
        )
        written += 1
        log.info("iv_mark symbol=%s proxy=%s", symbol, proxy)
    if written:
        conn.commit()
    return written


def tags_from_iv(conn: psycopg.Connection, symbol: str, settings: SettingsPayload) -> set[str]:
    """Legacy premium-ratio tags are not produced."""
    return set()


def sanity_label(pairs: list[tuple[Decimal, Decimal]]) -> str:
    """Whether the premium ratio simply tracks 1/price. A red column is the known state."""
    window = [(proxy, underlying) for proxy, underlying in pairs[-30:] if underlying > 0]
    if len(window) < 5:
        return "pending"
    corr = _pearson([proxy for proxy, _ in window], [Decimal(1) / underlying for _, underlying in window])
    if corr is None:
        return "ok"
    if abs(corr) >= INVERT_CORR:
        return "INVERTED?"
    return "ok"


def _pearson(left: list[Decimal], right: list[Decimal]) -> float | None:
    count = len(left)
    if count < 2 or count != len(right):
        return None
    xs = [float(item) for item in left]
    ys = [float(item) for item in right]
    mean_x = sum(xs) / count
    mean_y = sum(ys) / count
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x <= 0 or var_y <= 0:
        return None
    return cov / math.sqrt(var_x * var_y)
