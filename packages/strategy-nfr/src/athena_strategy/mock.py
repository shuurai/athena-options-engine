"""Stand-in tags so the phase machine can move before live signals exist."""

from __future__ import annotations

import logging
from decimal import Decimal

import psycopg

from athena_settings.model import SettingsPayload
from athena_strategy.drafts import has_open_draft, propose

log = logging.getLogger("athena.strategy")


def mock_input(conn: psycopg.Connection, symbol: str) -> tuple[set[str], int | None] | None:
    """Active stand-in tags for one symbol. None when this name is not mocked."""
    row = conn.execute(
        """
        SELECT tags, event_offset FROM mock_signals
        WHERE symbol = %s AND active
        """,
        (symbol,),
    ).fetchone()
    if row is None:
        return None
    tags = {part for part in (row[0] or "").split(",") if part}
    log.info("mock symbol=%s tags=%s offset=%s", symbol, ",".join(sorted(tags)), row[1])
    return tags, row[1]


def prepare_mock_orders(conn: psycopg.Connection, settings: SettingsPayload, account_id: str) -> None:
    """Leave one pending strangle draft when send_order is on, then turn that flag off."""
    rows = conn.execute(
        """
        SELECT symbol FROM mock_signals
        WHERE active AND send_order
        """
    ).fetchall()
    for (symbol,) in rows:
        if symbol not in settings.watchlist:
            continue
        if has_open_draft(conn, account_id, symbol, "OPEN_STRANGLE"):
            _clear_send(conn, symbol)
            continue
        plan = conn.execute(
            """
            SELECT expiry, put_strike, call_strike, put_limit, call_limit,
                   trading_class, option_exchange, multiplier
            FROM option_plans
            WHERE symbol = %s AND put_limit > 0 AND call_limit > 0
            """,
            (symbol,),
        ).fetchone()
        if plan is None:
            log.info("mock order waits for a priced plan symbol=%s", symbol)
            continue
        expiry, put_strike, call_strike, put_limit, call_limit, trading_class, exchange, multiplier = plan
        put_far = _far(put_limit)
        call_far = _far(call_limit)
        draft_id = propose(
            conn,
            settings,
            symbol,
            "OPEN_STRANGLE",
            1,
            f"P2 OPEN_STRANGLE. Mock progress. Sell {put_strike} put and {call_strike} call far from the print.",
            "P2",
            account_id,
            instrument="OPT",
            limit_price=put_far + call_far,
            expiry=expiry,
            put_strike=put_strike,
            call_strike=call_strike,
            put_limit=put_far,
            call_limit=call_far,
            trading_class=trading_class,
            option_exchange=exchange,
            multiplier=multiplier,
        )
        if not draft_id:
            continue
        _clear_send(conn, symbol)
        conn.commit()
        log.info("mock draft waiting symbol=%s draft=%s", symbol, draft_id)


def _far(limit: Decimal) -> Decimal:
    """A sell this far above the print rests instead of crossing."""
    return max(limit * 5, Decimal("20"))


def _clear_send(conn: psycopg.Connection, symbol: str) -> None:
    conn.execute("UPDATE mock_signals SET send_order = false WHERE symbol = %s", (symbol,))
