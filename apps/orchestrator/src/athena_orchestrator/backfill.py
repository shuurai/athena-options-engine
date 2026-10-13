"""One paced pass that stores two years of IB daily bars. Read-only, on the probe client id."""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime

from athena_exec.accounts import active_accounts
from athena_exec.broker import IbBroker
from athena_marketdata.pricing import ib_daily_count, set_history_short, store_ib_daily
from athena_exec.clients import client_id
from athena_orchestrator.tasks import _IB_LOCK
from athena_settings import load_active
from athena_settings.model import SettingsPayload
from athena_settings.prompts import default_agents
from athena_settings.repository import save_settings
from athena_store import connect

log = logging.getLogger("athena.history")

_RANGE = 252
_RANGE_AGENTS = ("Location", "CSP", "Call", "Purchase", "Consolidator")


def start_history_backfill(database_url: str) -> None:
    threading.Thread(
        target=_run,
        args=(database_url,),
        daemon=True,
        name="history-backfill",
    ).start()


def history_symbols(watchlists: list[list[str]], regime_indexes: list[str]) -> list[str]:
    """Watchlist names, then the regime index when it is not already listed."""
    names: list[str] = []
    for group in watchlists:
        for symbol in group:
            if symbol not in names:
                names.append(symbol)
    for raw in regime_indexes:
        index = raw.strip().upper()
        if index and index not in names:
            names.append(index)
    return names


def listing_symbol(broker, symbol: str) -> str:
    """0992 is not the SEHK code when only 992 qualifies."""
    if symbol != "0992":
        return symbol
    if broker.qualifies_stock("0992"):
        return "0992"
    if broker.qualifies_stock("992"):
        log.info("listing 0992 qualifies as 992")
        return "992"
    return "0992"


def watchlist_after_listing(names: list[str], old: str, new: str) -> list[str]:
    found: list[str] = []
    for item in names:
        picked = new if item == old else item
        if picked not in found:
            found.append(picked)
    return found


def rename_watch_symbol(conn, account_id: str, old: str, new: str) -> None:
    """Replace one watchlist entry. Other settings stay."""
    version = load_active(conn, account_id)
    data = version.model_dump()
    payload = SettingsPayload.model_validate(
        {key: data[key] for key in SettingsPayload.model_fields}
    )
    if old not in payload.watchlist:
        return
    names = watchlist_after_listing(list(payload.watchlist), old, new)
    if names == list(payload.watchlist):
        return
    save_settings(
        conn,
        account_id,
        payload.model_copy(update={"watchlist": names}),
        "athena",
        f"listing {old} to {new}",
    )


def pull_symbol(conn, broker, symbol: str) -> int:
    """Two documented 1 Y daily requests. Returns the stored ib-daily day count."""
    first = broker.history_span(symbol, "1 Y", "")
    store_ib_daily(conn, symbol, first)
    if first:
        time.sleep(1)
        oldest = min(_as_day(bar[0]) for bar in first)
        second = broker.history_span(symbol, "1 Y", f"{oldest:%Y%m%d}-00:00:00")
        store_ib_daily(conn, symbol, second)
    return ib_daily_count(conn, symbol)


def align_range_prompts(conn, account_id: str) -> None:
    """Stored prompts win, so a short history replaces the 52-week sentences.

    Reasoning and every other setting stay as they are.
    """
    version = load_active(conn, account_id)
    data = version.model_dump()
    payload = SettingsPayload.model_validate(
        {key: data[key] for key in SettingsPayload.model_fields}
    )
    standing = default_agents()
    agents = dict(payload.agents)
    changed = False
    for name in _RANGE_AGENTS:
        current = agents.get(name)
        updated = standing[name]
        if current is not None:
            updated = updated.model_copy(update={"reasoning": current.reasoning})
        if current is None or current.system != updated.system or current.execution != updated.execution:
            agents[name] = updated
            changed = True
    if not changed:
        return
    try:
        save_settings(
            conn,
            account_id,
            payload.model_copy(update={"agents": agents}),
            "athena",
            "Standing prompts for range tags and intent",
        )
    except ValueError as exc:
        log.info("range prompts unchanged account=%s error=%s", account_id, exc)


def _run(database_url: str) -> None:
    import asyncio

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    host = os.environ.get("IB_GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("IB_GATEWAY_PORT", "4002"))
    books = _books(database_url)
    if not books:
        return
    symbols = history_symbols(
        [names for _account, names, _index in books],
        [index for _account, _names, index in books],
    )
    broker = IbBroker()
    short: list[str] = []
    with _IB_LOCK:
        if not broker.connect(host, port, client_id("probe"), True):
            log.info("history backfill skipped gateway")
            _mark(database_url, books, symbols)
            return
        try:
            with connect(database_url) as conn:
                for symbol in symbols:
                    resolved = listing_symbol(broker, symbol)
                    if resolved != symbol:
                        for account, names, _index in books:
                            if symbol not in names:
                                continue
                            rename_watch_symbol(conn, account, symbol, resolved)
                            names[:] = watchlist_after_listing(names, symbol, resolved)
                        symbol = resolved
                    try:
                        count = _stored_or_pull(conn, broker, symbol)
                    except Exception as exc:
                        log.info(
                            "history backfill symbol=%s error=%s",
                            symbol,
                            exc.__class__.__name__,
                        )
                        count = ib_daily_count(conn, symbol)
                    is_short = count < _RANGE
                    set_history_short(conn, symbol, is_short)
                    conn.commit()
                    if is_short:
                        short.append(symbol)
                        log.warning("history short symbol=%s bars=%s", symbol, count)
                    else:
                        log.info("history stored symbol=%s bars=%s", symbol, count)
                    time.sleep(1)
        finally:
            broker.disconnect()
    if not short:
        return
    with connect(database_url) as conn:
        for account, names, _index in books:
            if any(symbol in short for symbol in names):
                align_range_prompts(conn, account)


def _mark(database_url: str, books: list[tuple[str, list[str], str]], symbols: list[str]) -> None:
    short: list[str] = []
    with connect(database_url) as conn:
        for symbol in symbols:
            count = ib_daily_count(conn, symbol)
            is_short = count < _RANGE
            set_history_short(conn, symbol, is_short)
            if is_short:
                short.append(symbol)
                log.warning("history short symbol=%s bars=%s", symbol, count)
        conn.commit()
    if not short:
        return
    with connect(database_url) as conn:
        for account, names, _index in books:
            if any(symbol in short for symbol in names):
                align_range_prompts(conn, account)


def _stored_or_pull(conn, broker, symbol: str) -> int:
    count = ib_daily_count(conn, symbol)
    if count >= _RANGE:
        return count
    return pull_symbol(conn, broker, symbol)


def _books(database_url: str) -> list[tuple[str, list[str], str]]:
    found = []
    with connect(database_url) as conn:
        for account in active_accounts():
            try:
                settings = load_active(conn, account)
            except RuntimeError:
                continue
            found.append((account, list(settings.watchlist), settings.strategy.regime_index))
    return found


def _as_day(value):
    if isinstance(value, datetime):
        return value.date()
    return value
