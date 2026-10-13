"""Live IB prints for the cockpit watchlist. Read-only, and not the trading client."""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal

from athena_exec.broker import IbBroker
from athena_exec.slots import HKT, cash_open, symbol_market
from athena_marketdata.pricing import has_ib_daily
from athena_orchestrator.tasks import _IB_LOCK
from athena_store import connect

log = logging.getLogger("athena.pool")

_PROBE_CLIENT = 2
_LOCK = threading.Lock()
_URL = ""
_SYMBOLS: list[str] = []
_ACCOUNT = ""
_SEEN = 0.0
_STARTED = False
DASH = "—"


def configure(database_url: str) -> None:
    global _URL
    _URL = database_url


def enabled_symbols(settings) -> list[str]:
    """Watchlist names this account still screens. A blocked name stays in the pool."""
    from athena_settings import enabled_watchlist

    return enabled_watchlist(settings)


def note_view(symbols: list[str], account: str = "") -> None:
    """The cockpit is open. Keep refreshing these names for this account."""
    global _SEEN, _ACCOUNT
    with _LOCK:
        _SYMBOLS[:] = list(symbols)
        _ACCOUNT = account
        _SEEN = time.time()
    _ensure()


def ib_move(conn, symbol: str) -> tuple[str, str, str]:
    """Latest IB price and the change versus the prior day's IB close."""
    from athena_strategy.simbars import in_replay, load_daily

    if in_replay(conn, symbol):
        daily = load_daily(conn, symbol)
        if not daily:
            return DASH, DASH, ""
        last = Decimal(daily[-1][2])
        previous = None
        for row in reversed(daily[:-1]):
            close = Decimal(row[2])
            if close == 0 or close == last:
                continue
            previous = close
            break
        price = f"{last:,.2f}"
        if previous is None or previous == 0:
            return price, DASH, ""
        change = (last - previous) / previous * Decimal(100)
        tone = "chg-up" if change > 0 else "chg-down" if change < 0 else ""
        return price, f"{change:+.2f}%", tone
    from athena_marketdata.pricing import quote_pair

    last, previous = quote_pair(conn, symbol)
    if last is None:
        return DASH, DASH, ""
    price = f"{last:,.2f}"
    if previous is None or previous == 0:
        return price, DASH, ""
    change = (last - previous) / previous * Decimal(100)
    tone = "chg-up" if change > 0 else "chg-down" if change < 0 else ""
    return price, f"{change:+.2f}%", tone


def snapshot(conn, account: str) -> dict:
    from athena_orchestrator.cockpit import sleeve
    from athena_settings import load_active

    settings = load_active(conn, account) if account else None
    symbols = enabled_symbols(settings) if settings else []
    note_view(symbols, account)
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
            if not has_ib_daily(conn, symbol)
        ]
    prices, history = _pull(symbols, need_history, _book_markets())
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


def _book_markets() -> dict[str, str]:
    """Markets for the names on screen. A missing book leaves each name to be inferred."""
    from athena_exec.accounts import active_accounts
    from athena_settings.repository import load_active

    with _LOCK:
        account = _ACCOUNT
        symbols = list(_SYMBOLS)
    if not account:
        accounts = active_accounts()
        account = accounts[0] if accounts else ""
    if not _URL or not account or not symbols:
        return {}
    try:
        with connect(_URL) as conn:
            settings = load_active(conn, account)
    except Exception as exc:
        log.info("pulse markets skipped error=%s", exc.__class__.__name__)
        return {}
    found = {}
    for symbol in symbols:
        flags = settings.tactic_flags.get(symbol.upper())
        explicit = flags.market if flags is not None and flags.market else ""
        found[symbol.upper()] = symbol_market(symbol, explicit)
    return found


def _pull(symbols: list[str], need_history: list[str], markets: dict[str, str]) -> tuple[dict, dict]:
    import asyncio

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    _apply_subscription_limit()
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
            broker.set_markets(markets)
            now = datetime.now(HKT)
            for symbol in symbols:
                if cash_open(symbol, now, markets.get(symbol.upper(), "")):
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


def _apply_subscription_limit() -> None:
    """Use the active book's quote cap. A missing book keeps the default of 2."""
    from athena_exec.accounts import active_accounts
    from athena_exec.broker import set_subscription_limit
    from athena_settings.repository import load_active

    accounts = active_accounts()
    if not _URL or not accounts:
        return
    try:
        with connect(_URL) as conn:
            settings = load_active(conn, accounts[0])
    except Exception as exc:
        log.info("pulse limit skipped error=%s", exc.__class__.__name__)
        return
    set_subscription_limit(settings.ib.concurrent_subscriptions)
