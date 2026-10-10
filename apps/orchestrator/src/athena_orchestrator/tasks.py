"""Background fill tasks. The console polls them, so a page change keeps the card."""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from athena_exec.broker import IbBroker
from athena_settings import load_active
from athena_settings.model import SettingsPayload
from athena_settings.repository import save_settings
from athena_settings.resolve import deepseek_token, scoring_model
from athena_store import connect
from athena_strategy.pool import clean_symbol, lookup_company, store_profile

log = logging.getLogger("athena.pool")

_LOCK = threading.Lock()
_IB_LOCK = threading.Lock()
_TASKS: dict[str, dict] = {}
_KEEP_SECONDS = 45
_PROBE_CLIENT = 2


def list_tasks() -> list[dict]:
    now = time.time()
    with _LOCK:
        stale = [
            task_id
            for task_id, task in _TASKS.items()
            if task["status"] != "running" and now - task["finished"] > _KEEP_SECONDS
        ]
        for task_id in stale:
            del _TASKS[task_id]
        return [_public(task) for task in _TASKS.values()]


_OPS_RUNNING = {"health": "Health", "backup": "Backup", "halt": "Halt", "resume": "Resume"}
_OPS_DONE = {"health": "Health", "backup": "Backup", "halt": "Halted", "resume": "Resumed"}


def begin_ops(database_url: str, account: str, actor: str, allowlist: set[str], command: str) -> dict:
    """Queue health, backup, halt, or resume. The page does not wait."""
    if command not in _OPS_RUNNING:
        raise ValueError("unknown ops command")
    if not database_url:
        raise ValueError("The store is not configured.")
    with _LOCK:
        for task in _TASKS.values():
            if task.get("kind") == command and task["status"] == "running":
                return _public(task)
        task = {
            "id": uuid.uuid4().hex[:12],
            "symbol": "",
            "verb": command,
            "title": _OPS_RUNNING[command],
            "status": "running",
            "detail": "Working",
            "finished": 0.0,
            "kind": command,
        }
        _TASKS[task["id"]] = task
        public = _public(task)
    threading.Thread(
        target=_run_ops,
        args=(public["id"], database_url, account, actor, allowlist, command),
        name=f"athena-ops-{command}",
        daemon=True,
    ).start()
    return public


def _run_ops(task_id: str, database_url: str, account: str, actor: str, allowlist: set[str], command: str) -> None:
    from athena_orchestrator.cockpit import run_ops

    try:
        with connect(database_url) as conn:
            message = run_ops(conn, account, actor, allowlist, command)
        _finish(task_id, "done", message, _OPS_DONE[command])
    except Exception as exc:
        log.info("ops failed command=%s error=%s", command, exc.__class__.__name__)
        text = str(exc).strip() or "The step did not finish."
        _finish(task_id, "error", text, "Not done")


def begin_positions(database_url: str, account: str) -> dict:
    """Read the IB book on the read-only client. Does not place an order."""
    if not database_url:
        raise ValueError("The store is not configured.")
    with _LOCK:
        for task in _TASKS.values():
            if task.get("kind") == "positions" and task["status"] == "running":
                return _public(task)
        task = {
            "id": uuid.uuid4().hex[:12],
            "symbol": "",
            "verb": "positions",
            "title": "Positions",
            "status": "running",
            "detail": "Reading IB",
            "finished": 0.0,
            "kind": "positions",
        }
        _TASKS[task["id"]] = task
        public = _public(task)
    threading.Thread(
        target=_run_positions,
        args=(public["id"], database_url, account),
        name="athena-positions",
        daemon=True,
    ).start()
    return public


def _run_positions(task_id: str, database_url: str, account: str) -> None:
    import asyncio

    from athena_exec.session import sync_account_positions

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    host = os.environ.get("IB_GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("IB_GATEWAY_PORT", "4002"))
    broker = IbBroker()
    try:
        with _IB_LOCK:
            if not broker.connect(host, port, _PROBE_CLIENT, True):
                raise RuntimeError("IB Gateway did not connect.")
            try:
                rows = broker.positions()
            finally:
                broker.disconnect()
        with connect(database_url) as conn:
            count = sync_account_positions(conn, account, rows)
        detail = "No open position." if count == 0 else f"{count} open"
        _finish(task_id, "done", detail, "Positions")
    except Exception as exc:
        log.info("positions sync failed error=%s", exc.__class__.__name__)
        _finish(task_id, "error", "IB did not return the book.", "Not done")


def begin_fill(conn, database_url: str, account: str, actor: str, symbol: str, mode: str) -> dict:
    """Queue a fill. Add also puts the ticker on the watchlist first."""
    symbol = clean_symbol(symbol)
    current = load_active(conn, account)
    if mode == "add":
        if symbol in {item.upper() for item in current.watchlist}:
            raise ValueError(f"{symbol} is already in the pool.")
        payload = SettingsPayload.model_validate(current.model_dump())
        payload.watchlist.append(symbol)
        save_settings(conn, account, payload, f"console:{actor}", f"universe add {symbol}")
        verb = "Added"
    elif mode == "sync":
        if symbol not in {item.upper() for item in current.watchlist}:
            raise ValueError(f"{symbol} is not in the pool.")
        verb = "Synced"
    else:
        raise ValueError("Unknown action")
    with _LOCK:
        for task in _TASKS.values():
            if task["symbol"] == symbol and task["status"] == "running":
                return _public(task)
        task = {
            "id": uuid.uuid4().hex[:12],
            "symbol": symbol,
            "verb": verb,
            "title": f"Filling {symbol}",
            "status": "running",
            "detail": "Looking up the company",
            "finished": 0.0,
            "kind": "fill",
        }
        _TASKS[task["id"]] = task
        public = _public(task)
    threading.Thread(
        target=_run_fill,
        args=(public["id"], database_url, account, symbol, verb),
        name=f"athena-fill-{symbol}",
        daemon=True,
    ).start()
    return public


def _run_fill(task_id: str, database_url: str, account: str, symbol: str, verb: str) -> None:
    try:
        _update(task_id, "Looking up the company")
        facts = lookup_company(symbol)
        _update(task_id, "Reading the IB quote")
        with connect(database_url) as conn:
            quote_note, problem = _write_ib_quote(conn, symbol)
            _update(task_id, "Choosing classifiers")
            current = load_active(conn, account)
            note = store_profile(
                conn,
                account,
                symbol,
                facts,
                scoring_model(current),
                verb,
                quote_note,
                current.ai.provider,
                deepseek_token(current),
            )
        if problem:
            note = f"{note} {problem}"
        _finish(task_id, "done", note, f"Filled {symbol}")
    except Exception as exc:
        log.info("pool fill failed symbol=%s error=%s", symbol, exc.__class__.__name__)
        _finish(task_id, "error", "The fill did not finish.", f"{symbol} was not filled")


def _write_ib_quote(conn, symbol: str) -> tuple[str, str]:
    """Latest delayed quote and daily bars. Read-only, and not the orchestrator client."""
    price, bars, problem = _pull_ib(symbol)
    if problem:
        return "", problem
    wrote = False
    hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    if price is not None:
        conn.execute(
            """
            INSERT INTO market_bars (symbol, ts, open, high, low, close, volume, source)
            VALUES (%s, %s, %s, %s, %s, %s, 0, 'ib-delayed')
            ON CONFLICT (symbol, ts, source) DO UPDATE SET close = EXCLUDED.close
            """,
            (symbol, hour, price, price, price, price),
        )
        wrote = True
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
        wrote = True
    if wrote:
        conn.commit()
        log.info("pool ib symbol=%s price=%s bars=%s", symbol, price if price is not None else "-", len(bars))
        return "Price from IB.", ""
    return "", "IB had no price."


def _pull_ib(symbol: str) -> tuple[Decimal | None, list, str]:
    import asyncio

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    host = os.environ.get("IB_GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("IB_GATEWAY_PORT", "4002"))
    broker = IbBroker()
    with _IB_LOCK:
        if not broker.connect(host, port, _PROBE_CLIENT, True):
            return None, [], "IB Gateway did not connect."
        try:
            price = broker.last_price(symbol)
            bars = broker.daily_history(symbol)
        finally:
            broker.disconnect()
    if price is None and not bars:
        return None, [], "IB had no price."
    return price, bars, ""


def _update(task_id: str, detail: str) -> None:
    with _LOCK:
        task = _TASKS.get(task_id)
        if task and task["status"] == "running":
            task["detail"] = detail


def _finish(task_id: str, status: str, detail: str, title: str) -> None:
    with _LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            return
        task["status"] = status
        task["detail"] = detail
        task["title"] = title
        task["finished"] = time.time()


def begin_simulate(database_url: str, account: str, start: str, months: str) -> dict:
    """Download daily history, snapshot the chain, then arm the replay."""
    from datetime import date

    from athena_orchestrator.sim import prepare

    try:
        day = date.fromisoformat(start)
        count = int(months)
    except ValueError as exc:
        raise ValueError("Choose a start date and a duration.") from exc
    with connect(database_url) as conn:
        settings = load_active(conn, account)
        names = [symbol.strip().upper() for symbol in settings.sim_symbols if symbol.strip()]
        run_id, _end, duration = prepare(conn, account, day, count, names)
    task, fresh = _start("simulate", "Simulation", "Downloading history")
    if fresh:
        threading.Thread(
            target=_run_simulate,
            args=(task["id"], database_url, account, run_id, names, duration),
            name="athena-sim",
            daemon=True,
        ).start()
    return task


def begin_liquidate(database_url: str, account: str) -> dict:
    from athena_exec.accounts import trading_mode

    if trading_mode() != "paper":
        raise ValueError("Liquidate runs in paper mode.")
    task, fresh = _start("liquidate", "Liquidate", "Closing the book")
    if fresh:
        threading.Thread(
            target=_run_liquidate,
            args=(task["id"], database_url, account),
            name="athena-liquidate",
            daemon=True,
        ).start()
    return task


def _start(kind: str, title: str, detail: str) -> tuple[dict, bool]:
    with _LOCK:
        for task in _TASKS.values():
            if task.get("kind") == kind and task["status"] == "running":
                return _public(task), False
        task = {
            "id": uuid.uuid4().hex[:12],
            "symbol": "",
            "verb": kind,
            "title": title,
            "status": "running",
            "detail": detail,
            "finished": 0.0,
            "kind": kind,
        }
        _TASKS[task["id"]] = task
        return _public(task), True


def _run_simulate(task_id: str, database_url: str, account: str, run_id: str, symbols: list[str], duration: str) -> None:
    import asyncio
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from athena_exec.session import cache_symbol_chain
    from athena_orchestrator.sim import activate, fail, latest_close, still_downloading, store_bars

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    host = os.environ.get("IB_GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("IB_GATEWAY_PORT", "4002"))
    broker = IbBroker()
    written = 0
    try:
        with _IB_LOCK:
            if not broker.connect(host, port, _PROBE_CLIENT, True):
                raise RuntimeError("IB Gateway did not connect.")
            try:
                with connect(database_url) as conn:
                    settings = load_active(conn, account)
                    today = datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
                    for symbol in symbols:
                        if not still_downloading(conn, run_id):
                            _finish(task_id, "done", "Stopped", "Simulation")
                            return
                        _update(task_id, f"Downloading {symbol}")
                        bars = broker.history_span(symbol, duration)
                        written += store_bars(conn, run_id, symbol, bars)
                        price = latest_close(conn, run_id, symbol)
                        if price is None:
                            continue
                        _update(task_id, f"Chain {symbol}")
                        try:
                            cache_symbol_chain(conn, broker, settings, symbol, price, today)
                        except Exception as exc:
                            log.info("chain skipped symbol=%s error=%s", symbol, exc)
            finally:
                broker.disconnect()
        with connect(database_url) as conn:
            activate(conn, run_id)
    except Exception as exc:
        log.info("simulation failed run=%s error=%s", run_id, exc)
        with connect(database_url) as conn:
            fail(conn, run_id)
        _finish(task_id, "error", str(exc), "Simulation")
        return
    _finish(task_id, "done", f"{written} daily bars stored", "Simulation")


def _run_liquidate(task_id: str, database_url: str, account: str) -> None:
    import asyncio

    from athena_orchestrator.sim import delete_simulated_positions, real_positions

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    with connect(database_url) as conn:
        removed = delete_simulated_positions(conn, account)
        rows = [row for row in real_positions(conn, account) if row[1] is not None and row[1] != 0]
    if not rows:
        _finish(task_id, "done", f"Deleted {removed} simulated positions.", "Liquidate")
        return
    host = os.environ.get("IB_GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("IB_GATEWAY_PORT", "4002"))
    broker = IbBroker()
    closed: list[str] = []
    errors: list[str] = []
    with _IB_LOCK:
        if not broker.connect(host, port, _PROBE_CLIENT, False):
            _finish(task_id, "error", "IB Gateway did not connect.", "Liquidate")
            return
        try:
            for symbol, quantity, sec_type, expiry, right, strike, mark, multiplier in rows:
                qty = abs(int(quantity))
                if qty <= 0:
                    continue
                action = "SELL" if quantity > 0 else "BUY"
                try:
                    if sec_type == "OPT":
                        limit = mark
                        if limit is None or limit <= 0:
                            raise RuntimeError("no option price")
                        chain = broker.option_chain(symbol)
                        if chain is None or expiry is None or strike is None:
                            raise RuntimeError("no option chain")
                        _expiries, _strikes, trading_class, exchange, chain_mult = chain
                        broker.place_option_limit(
                            account, symbol, expiry, strike, right or "C", trading_class, exchange,
                            action, qty, limit, multiplier or chain_mult or "100",
                        )
                    else:
                        limit = broker.last_price(symbol) or mark
                        if limit is None or limit <= 0:
                            raise RuntimeError("no stock price")
                        broker.place_limit(account, symbol, action, qty, limit)
                    closed.append(symbol)
                except Exception as exc:
                    errors.append(f"{symbol}: {exc}")
        finally:
            broker.disconnect()
    detail = f"Deleted {removed} simulated positions. Limit close sent for {', '.join(closed) or 'none'}."
    if errors:
        detail += " " + "; ".join(errors)
    _finish(task_id, "error" if errors and not closed else "done", detail, "Liquidate")


def _public(task: dict) -> dict:
    return {
        "id": task["id"],
        "symbol": task["symbol"],
        "title": task["title"],
        "status": task["status"],
        "detail": task["detail"],
        "kind": task.get("kind", "fill"),
    }
