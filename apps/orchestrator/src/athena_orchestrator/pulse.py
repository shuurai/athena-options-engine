"""Live IB prints for the cockpit watchlist. Read-only, and not the trading client."""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal

from athena_exec.broker import IbBroker
from athena_orchestrator.tasks import _IB_LOCK
from athena_store import connect

log = logging.getLogger("athena.pool")

_PROBE_CLIENT = 2
_LOCK = threading.Lock()
_URL = ""
_SYMBOLS: list[str] = []
_SEEN = 0.0
_STARTED = False
DASH = "—"


def configure(database_url: str) -> None:
    global _URL
    _URL = database_url


def note_view(symbols: list[str]) -> None:
    """The cockpit is open. Keep refreshing these names."""
    global _SEEN
    with _LOCK:
        _SYMBOLS[:] = list(symbols)
        _SEEN = time.time()
    _ensure()


def ib_move(conn, symbol: str) -> tuple[str, str, str]:
    """Latest IB price and the change versus the prior day's IB close."""
    rows = conn.execute(
        """
        SELECT ts, close FROM market_bars
        WHERE symbol = %s AND source IN ('ib-delayed', 'ib-daily')
        ORDER BY ts DESC
        LIMIT 40
        """,
        (symbol,),
    ).fetchall()
    if not rows:
        return DASH, DASH, ""
    last_ts, last = rows[0][0], Decimal(rows[0][1])
    previous = None
    for ts, close in rows[1:]:
        if ts.date() == last_ts.date() or Decimal(close) == 0:
            continue
        previous = Decimal(close)
        break
    price = f"{last:,.2f}"
    if previous is None:
        return price, DASH, ""
    change = (last - previous) / previous * Decimal(100)
    tone = "chg-up" if change > 0 else "chg-down" if change < 0 else ""
    return price, f"{change:+.2f}%", tone


def snapshot(conn, account: str) -> dict:
    from athena_orchestrator.cockpit import sleeve
    from athena_settings import load_active

    settings = load_active(conn, account) if account else None
    symbols = list(settings.watchlist) if settings else []
    note_view(symbols)
    facts = sleeve(conn, account) if account else {}
    return {
        "quotes": [
            {"symbol": symbol, "price": price, "change": change, "tone": tone}
            for symbol in symbols
            for price, change, tone in [ib_move(conn, symbol)]
        ],
        "sleeve": {
            "nav": facts.get("nav", DASH),
            "change": facts.get("change", DASH),
            "premium": facts.get("premium", DASH),
            "premium_meta": facts.get("premium_meta", ""),
            "cash": facts.get("cash", DASH),
            "cash_pct": facts.get("cash_pct", ""),
            "cash_meta": facts.get("cash_meta", ""),
            "pending": facts.get("pending", 0),
        },
    }


def _ensure() -> None:
    global _STARTED
    with _LOCK:
        if _STARTED or not _URL:
            return
        _STARTED = True
    threading.Thread(target=_refresh_loop, name="athena-pulse", daemon=True).start()


def _refresh_loop() -> None:
    while True:
        with _LOCK:
            fresh = time.time() - _SEEN < 90
            symbols = list(_SYMBOLS)
        if not fresh or not symbols:
            time.sleep(5)
            continue
        try:
            _refresh(symbols)
        except Exception as exc:
            log.info("pulse skipped error=%s", exc.__class__.__name__)
        time.sleep(20)


def _refresh(symbols: list[str]) -> None:
    with connect(_URL) as conn:
        need_history = [
            symbol
            for symbol in symbols
            if conn.execute(
                "SELECT 1 FROM market_bars WHERE symbol = %s AND source = 'ib-daily' LIMIT 1",
                (symbol,),
            ).fetchone()
            is None
        ]
    prices, history = _pull(symbols, need_history)
    if not prices and not history:
        return
    hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    with connect(_URL) as conn:
        for symbol, price in prices.items():
            conn.execute(
                """
                INSERT INTO market_bars (symbol, ts, open, high, low, close, volume, source)
                VALUES (%s, %s, %s, %s, %s, %s, 0, 'ib-delayed')
                ON CONFLICT (symbol, ts, source) DO UPDATE SET close = EXCLUDED.close
                """,
                (symbol, hour, price, price, price, price),
            )
        for symbol, bars in history.items():
            for bar in bars:
                day = bar.date.date() if isinstance(bar.date, datetime) else bar.date
                ts = datetime(day.year, day.month, day.day, tzinfo=UTC)
                conn.execute(
                    """
                    INSERT INTO market_bars (symbol, ts, open, high, low, close, volume, source)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'ib-daily')
                    ON CONFLICT (symbol, ts, source) DO UPDATE SET
                        open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                        close = EXCLUDED.close, volume = EXCLUDED.volume
                    """,
                    (symbol, ts, bar.open, bar.high, bar.low, bar.close, int(bar.volume or 0)),
                )
        conn.commit()
    log.info("pulse quotes=%s history=%s", len(prices), len(history))


def _pull(symbols: list[str], need_history: list[str]) -> tuple[dict, dict]:
    import asyncio

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    host = os.environ.get("IB_GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("IB_GATEWAY_PORT", "4002"))
    broker = IbBroker()
    prices: dict[str, Decimal] = {}
    history: dict[str, list] = {}
    with _IB_LOCK:
        if not broker.connect(host, port, _PROBE_CLIENT, True):
            log.info("pulse gateway down")
            return {}, {}
        try:
            for symbol in symbols:
                price = broker.last_price(symbol)
                if price is not None:
                    prices[symbol] = price
                if symbol in need_history:
                    bars = broker.daily_history(symbol)
                    if bars:
                        history[symbol] = bars
        finally:
            broker.disconnect()
    return prices, history
