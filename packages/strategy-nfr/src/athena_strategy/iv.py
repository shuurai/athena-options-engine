"""IV rank from stored strangle premium. Too few days leaves the tag off."""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

import psycopg

from athena_settings.model import SettingsPayload

log = logging.getLogger("athena.strategy")

IV_OWNED = {"HIGH_IV", "LOW_IV"}


def iv_rank(samples: list[Decimal], current: Decimal, minimum: int) -> int | None:
    """(current - low) / (high - low), as a percent. None until the window is full."""
    if len(samples) < minimum:
        return None
    low = min(samples)
    high = max(samples)
    if high == low:
        return None
    rank = (current - low) / (high - low) * Decimal(100)
    return int(rank)


def iv_tags(rank: int | None, high: int, low: int) -> set[str]:
    if rank is None:
        return set()
    if rank >= high:
        return {"HIGH_IV"}
    if rank <= low:
        return {"LOW_IV"}
    return set()


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
            price = conn.execute(
                """
                SELECT close FROM market_bars
                WHERE symbol = %s
                ORDER BY ts DESC
                LIMIT 1
                """,
                (symbol,),
            ).fetchone()
        if price is None or price[0] is None or price[0] <= 0:
            continue
        credit = plan[0] + plan[1]
        proxy = credit / price[0]
        conn.execute(
            """
            INSERT INTO iv_marks (symbol, day, credit, underlying, iv_proxy, updated_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (symbol, day) DO UPDATE SET
                credit = EXCLUDED.credit,
                underlying = EXCLUDED.underlying,
                iv_proxy = EXCLUDED.iv_proxy,
                updated_at = now()
            """,
            (symbol, today, credit, price[0], proxy),
        )
        written += 1
        log.info("iv_mark symbol=%s proxy=%s", symbol, proxy)
    if written:
        conn.commit()
    return written


def tags_from_iv(conn: psycopg.Connection, symbol: str, settings: SettingsPayload) -> set[str]:
    rows = conn.execute(
        "SELECT iv_proxy FROM iv_marks WHERE symbol = %s ORDER BY day",
        (symbol,),
    ).fetchall()
    if not rows:
        return set()
    samples = [row[0] for row in rows]
    rank = iv_rank(samples, samples[-1], settings.strategy.ivr_min_samples)
    if rank is None:
        log.info("iv_rank pending symbol=%s samples=%s", symbol, len(samples))
        return set()
    log.info("iv_rank symbol=%s rank=%s", symbol, rank)
    return iv_tags(rank, settings.strategy.ivr_high, settings.strategy.ivr_low)
