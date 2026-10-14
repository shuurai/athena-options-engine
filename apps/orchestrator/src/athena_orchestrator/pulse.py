"""Live IB prints for the cockpit watchlist. Read-only, and not the trading client.

Prices stay on their own Gateway login. A changed print is published at once.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal

from athena_exec.broker import IbBroker
from athena_exec.clients import client_id
from athena_exec.slots import HKT, cash_open, symbol_market
from athena_marketdata.pricing import has_ib_daily
from athena_store import connect

log = logging.getLogger("athena.pool")

_LOCK = threading.Lock()
_TICK_LOCK = threading.Lock()
_TICKS: list[queue.Queue] = []
_URL = ""
_SYMBOLS: list[str] = []
_ACCOUNT = ""
_SEEN = 0.0
_STARTED = False
DASH = "—"


def note_print(seen: dict[str, Decimal], symbol: str, price: Decimal) -> bool:
    """True when this price is new. The same price does not count again."""
    key = symbol.upper()
    if seen.get(key) == price:
        return False
    seen[key] = price
    return True


def _tick_currency(market: str) -> str:
    """Native quote currency for an Athena market code."""
    return {"hk": "HKD", "ca": "CAD"}.get((market or "").lower(), "USD") if market else ""


def format_tick(
    symbol: str,
    price: Decimal,
    previous: Decimal | None,
    *,
    market: str = "",
    currency: str = "",
) -> dict:
    """Price text and the percent move versus the prior close.

    Additive fields (`type`, `market`, `last`, `change_pct`, `currency`, `ts`) are for
    Skill clients on `/api/v1/events/prices`. Console UI keeps using `price` / `change` / `tone`.
    """
    text = f"{price:,.2f}"
    ccy = currency or _tick_currency(market)
    base = {
        "type": "price",
        "symbol": symbol,
        "market": market or "",
        "price": text,
        "last": float(price),
        "currency": ccy,
        "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    if previous is None or previous == 0:
        return {**base, "change": DASH, "change_pct": None, "tone": ""}
    change = (price - previous) / previous * Decimal(100)
    tone = "chg-up" if change > 0 else "chg-down" if change < 0 else ""
    return {
        **base,
        "change": f"{change:+.2f}%",
        "change_pct": float(change),
        "tone": tone,
    }


def publish_tick(payload: dict) -> None:
    """Tell open cockpits that one price changed. A slow page drops the extra."""
    with _TICK_LOCK:
        listeners = list(_TICKS)
    for listener in listeners:
        try:
            listener.put_nowait(payload)
        except queue.Full:
            continue


def subscribe_ticks() -> queue.Queue:
    listener: queue.Queue = queue.Queue(maxsize=200)
    with _TICK_LOCK:
        _TICKS.append(listener)
    return listener


def unsubscribe_ticks(listener: queue.Queue) -> None:
    with _TICK_LOCK:
        if listener in _TICKS:
            _TICKS.remove(listener)


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


def held_stock_symbols(conn, account: str) -> list[str]:
    """Open stock names on this book. Used so positions keep a live print."""
    if not account:
        return []
    rows = conn.execute(
        """
        SELECT DISTINCT symbol
        FROM positions
        WHERE account_id = %s AND COALESCE(sec_type, 'STK') = 'STK' AND quantity <> 0
        ORDER BY symbol
        """,
        (account,),
    ).fetchall()
    return [row[0] for row in rows if row[0]]


def working_order_symbols(conn, account: str) -> list[str]:
    """Underlyings with a working order. Used so /orders can refresh vs last."""
    if not account:
        return []
    rows = conn.execute(
        """
        SELECT DISTINCT symbol
        FROM orders
        WHERE account_id = %s
          AND status NOT IN ('blocked', 'filled', 'cancelled')
          AND COALESCE(ib_status, '') NOT IN ('Filled', 'Cancelled', 'ApiCancelled', 'Inactive')
          AND expiry IS NULL AND strike IS NULL AND COALESCE(contract_right, '') = ''
        ORDER BY symbol
        """,
        (account,),
    ).fetchall()
    return [row[0] for row in rows if row[0]]


def watch_symbols(conn, account: str) -> list[str]:
    """Full universe pool (enabled or not), open stocks, and working stock orders."""
    from athena_settings import load_active

    names: list[str] = []
    seen: set[str] = set()
    if account:
        try:
            settings = load_active(conn, account)
        except RuntimeError:
            settings = None
        if settings is not None:
            for symbol in settings.watchlist:
                key = symbol.upper()
                if key in seen:
                    continue
                seen.add(key)
                names.append(symbol)
        for symbol in held_stock_symbols(conn, account):
            key = symbol.upper()
            if key in seen:
                continue
            seen.add(key)
            names.append(symbol)
        for symbol in working_order_symbols(conn, account):
            key = symbol.upper()
            if key in seen:
                continue
            seen.add(key)
            names.append(symbol)
    return names


def snapshot(conn, account: str) -> dict:
    from athena_orchestrator.cockpit import sleeve

    symbols = watch_symbols(conn, account)
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


def _watching() -> list[str]:
    with _LOCK:
        fresh = time.time() - _SEEN < 90
        symbols = list(_SYMBOLS)
    if not fresh:
        return []
    return symbols


def _refresh_loop() -> None:
    while True:
        symbols = _watching()
        if not symbols:
            time.sleep(5)
            continue
        try:
            _stream(symbols)
        except Exception as exc:
            log.info("pulse skipped error=%s", exc.__class__.__name__)
        time.sleep(5)


def _stream(symbols: list[str]) -> None:
    """Stay on the pulse login and publish a tick only when a price changes."""
    import asyncio

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    _apply_subscription_limit()
    host = os.environ.get("IB_GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("IB_GATEWAY_PORT", "4002"))
    login = client_id("pulse")
    broker = IbBroker()
    if not broker.connect(host, port, login, True):
        log.info("pulse gateway down client_id=%s", login)
        return
    ib = broker._ib
    held: dict = {}
    seen: dict[str, Decimal] = {}
    prior: dict[str, Decimal | None] = {}
    names: dict[str, str] = {}
    venues: dict[str, str] = {}
    hooked = False

    def on_tickers(tickers) -> None:
        _take_tickers(tickers, seen, prior, names, venues)

    try:
        ib.reqMarketDataType(3)
        ib.pendingTickersEvent += on_tickers
        hooked = True
        synced = ""
        filled = 0.0
        tried = 0.0
        while True:
            current = _watching()
            if not current or not broker.connected():
                return
            # Re-read markets every pass so Universe Market us/hk/ca changes apply
            # without waiting for a pulse reconnect (otherwise CA names stay SMART/USD).
            markets = _book_markets()
            broker.set_markets(markets)
            live = _live_names(current, markets)
            key = ",".join(
                f"{symbol.upper()}:{markets.get(symbol.upper(), '')}" for symbol in live
            )
            now = time.time()
            if key != synced or now - tried > 30:
                if key != synced:
                    # Drop lines whose venue changed so they re-qualify on the new market.
                    for code in list(held):
                        try:
                            ib.cancelMktData(held[code])
                        except Exception:
                            pass
                        held.pop(code, None)
                names.clear()
                names.update({symbol.upper(): symbol for symbol in live})
                venues.clear()
                venues.update(
                    {symbol.upper(): markets.get(symbol.upper(), "") for symbol in live}
                )
                _seed(live, seen, prior)
                _sync_lines(ib, broker, names, held)
                ib.reqMarketDataType(3)
                if key != synced:
                    log.info("pulse listening client_id=%s names=%s", login, len(live))
                synced = key
                tried = now
            if now - filled > 60:
                filled = now
                try:
                    _fill_history(broker, current)
                except Exception as exc:
                    log.info("pulse history skipped error=%s", exc.__class__.__name__)
            ib.sleep(1)
    finally:
        if hooked:
            try:
                ib.pendingTickersEvent -= on_tickers
            except Exception:
                pass
        for contract in held.values():
            try:
                ib.cancelMktData(contract)
            except Exception:
                pass
        broker.disconnect()


def _live_names(symbols: list[str], markets: dict[str, str]) -> list[str]:
    """Open names that are not inside a replay. A replay row keeps its stored price."""
    from athena_strategy.simbars import in_replay

    now = datetime.now(HKT)
    if not _URL:
        return []
    found = []
    with connect(_URL) as conn:
        for symbol in symbols:
            if in_replay(conn, symbol):
                continue
            if cash_open(symbol, now, markets.get(symbol.upper(), "")):
                found.append(symbol)
    return found


def _seed(symbols: list[str], seen: dict[str, Decimal], prior: dict[str, Decimal | None]) -> None:
    """Remember the stored print so the first identical tick does not move the row."""
    from athena_marketdata.pricing import quote_pair

    if not symbols or not _URL:
        return
    with connect(_URL) as conn:
        for symbol in symbols:
            last, previous = quote_pair(conn, symbol)
            key = symbol.upper()
            prior[key] = previous
            if key not in seen and last is not None:
                seen[key] = last


def _sync_lines(ib, broker, names: dict[str, str], held: dict) -> None:
    for key in list(held):
        if key in names:
            continue
        try:
            ib.cancelMktData(held[key])
        except Exception:
            pass
        held.pop(key, None)
    for key, symbol in names.items():
        if key in held:
            continue
        if broker.unknown_stock(symbol):
            continue
        try:
            contract = broker._qualify_stock(symbol)
            if contract is None:
                continue
            ib.reqMktData(contract, "", False, False)
        except Exception as exc:
            log.info("pulse subscribe skipped symbol=%s error=%s", symbol, exc.__class__.__name__)
            continue
        held[key] = contract


def _take_tickers(
    tickers,
    seen: dict[str, Decimal],
    prior: dict,
    names: dict[str, str],
    venues: dict[str, str] | None = None,
) -> None:
    from athena_exec.broker import _ticker_price

    books = venues or {}
    changed = []
    for ticker in tickers:
        contract = getattr(ticker, "contract", None)
        key = (getattr(contract, "symbol", "") or "").upper()
        if key not in names:
            continue
        price = _ticker_price(ticker)
        if price is None or not note_print(seen, key, price):
            continue
        shown = names[key]
        market = books.get(key, "")
        changed.append(
            (shown, price, format_tick(shown, price, prior.get(key), market=market))
        )
    if not changed:
        return
    try:
        _store_prints([(symbol, price) for symbol, price, _payload in changed])
    except Exception as exc:
        log.info("pulse store skipped error=%s", exc.__class__.__name__)
    for _symbol, _price, payload in changed:
        publish_tick(payload)


def _store_prints(rows: list[tuple[str, Decimal]]) -> None:
    from athena_marketdata.pricing import upsert_delayed

    hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    with connect(_URL) as conn:
        for symbol, price in sorted(rows, key=lambda item: item[0].upper()):
            upsert_delayed(conn, symbol, price, hour)


def _fill_history(broker, symbols: list[str]) -> None:
    if not symbols or not _URL:
        return
    with connect(_URL) as conn:
        missing = [symbol for symbol in symbols if not has_ib_daily(conn, symbol)]
    if not missing:
        return
    bars = broker.daily_history(missing[0])
    if not bars:
        return
    with connect(_URL) as conn:
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
                (missing[0], ts, bar.open, bar.high, bar.low, bar.close, int(bar.volume or 0)),
            )
        conn.commit()
    log.info("pulse history symbol=%s bars=%s", missing[0], len(bars))


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
