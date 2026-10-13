"""Local developer API. Callers poll a task id. Nothing here places an order."""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import threading
import uuid
from datetime import date, datetime

from athena_exec.accounts import active_accounts
from athena_exec.broker import IbBroker, stock_venue
from athena_exec.clients import client_id
from athena_orchestrator.tasks import _IB_LOCK
from athena_settings import load_active
from athena_store import connect

log = logging.getLogger("athena.dev")

_LOCK = threading.Lock()
_TASKS: dict[str, dict] = {}
_BARS = {"1 min", "5 mins", "15 mins", "30 mins", "1 hour", "1 day"}
_DURATION = re.compile(r"^\d+ (S|D|W|M|Y)$")
_MAX = 20


def presented_key(headers) -> str:
    raw = headers.get("Authorization", "")
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return headers.get("X-Api-Key", "").strip()


def stored_key(conn, account: str) -> str:
    if not account:
        return ""
    try:
        key = load_active(conn, account).developer.api_key
    except RuntimeError:
        return ""
    return (key or "").strip()


def authorized(conn, account: str, headers) -> bool:
    stored = stored_key(conn, account)
    presented = presented_key(headers)
    if not stored or not presented:
        return False
    return hmac.compare_digest(stored, presented)


def handle(conn, method: str, path: str, query: dict, body: bytes, headers) -> tuple[int, str]:
    account = active_accounts()[0] if active_accounts() else ""
    if path == "/api/v1":
        return 200, _dump(_index())
    if not authorized(conn, account, headers):
        return 401, _dump({"ok": False, "status": "failed", "error": "API key is not valid."})
    if method == "GET" and path.startswith("/api/v1/tasks/"):
        return _task(path.removeprefix("/api/v1/tasks/"))
    if method != "POST":
        return 404, _dump({"ok": False, "status": "failed", "error": "Unknown API."})
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return 400, _dump({"ok": False, "status": "failed", "error": "Body must be JSON."})
    if not isinstance(payload, dict):
        return 400, _dump({"ok": False, "status": "failed", "error": "Body must be a JSON object."})
    database_url = os.environ.get("DATABASE_URL", "")
    if path == "/api/v1/bars":
        return _begin_bars(payload, "bars")
    if path == "/api/v1/day":
        return _begin_bars(payload, "day")
    if path == "/api/v1/option":
        return _begin_bars(payload, "option")
    if path == "/api/v1/company":
        return _begin_company(database_url, payload)
    return 404, _dump({"ok": False, "status": "failed", "error": "Unknown API."})


def _index() -> dict:
    return {
        "ok": True,
        "auth": "Authorization: Bearer <key>",
        "endpoints": [
            {"method": "POST", "path": "/api/v1/bars", "result": "task_id"},
            {"method": "POST", "path": "/api/v1/day", "result": "task_id"},
            {"method": "POST", "path": "/api/v1/option", "result": "task_id"},
            {"method": "POST", "path": "/api/v1/company", "result": "task_id"},
            {"method": "GET", "path": "/api/v1/tasks/{task_id}", "result": "running, done, or failed"},
        ],
    }


def _begin_bars(payload: dict, mode: str) -> tuple[int, str]:
    try:
        raw = _symbols(payload)
        if mode == "option":
            specs = [_spec({**item, "sec_type": "OPT"} if isinstance(item, dict) else item) for item in raw]
            if any(spec["sec_type"] != "OPT" for spec in specs):
                raise ValueError("POST /api/v1/option takes option objects with expiry, strike, and right.")
        else:
            specs = [_spec(item) for item in raw]
            if any(spec["sec_type"] == "OPT" for spec in specs):
                raise ValueError("Options use POST /api/v1/option.")
        bar = str(payload.get("bar") or "1 day").strip()
        if bar not in _BARS:
            raise ValueError("bar must be 1 min, 5 mins, 15 mins, 30 mins, 1 hour, or 1 day.")
        day = str(payload.get("day") or "").strip()
        if mode == "day" and not day:
            raise ValueError("day is required, as YYYY-MM-DD.")
        if mode == "bars" and day:
            raise ValueError("One session uses POST /api/v1/day.")
        if day:
            date.fromisoformat(day)
            duration = "10 D"
            end = day.replace("-", "") + " 23:59:59"
        else:
            duration = str(payload.get("duration") or _default_duration(bar)).strip()
            end = ""
        if not _DURATION.match(duration):
            raise ValueError("duration looks like 30 D, 1 W, 6 M, or 1 Y.")
    except ValueError as exc:
        return 400, _dump({"ok": False, "status": "failed", "error": str(exc)})
    task_id = _create("bars")
    threading.Thread(
        target=_run_bars,
        args=(task_id, specs, duration, bar, end, day),
        name="athena-dev-bars",
        daemon=True,
    ).start()
    return 202, _dump({"ok": True, "task_id": task_id, "status": "running"})


def _begin_company(database_url: str, payload: dict) -> tuple[int, str]:
    try:
        specs = [_spec(item) for item in _symbols(payload)]
    except ValueError as exc:
        return 400, _dump({"ok": False, "status": "failed", "error": str(exc)})
    task_id = _create("company")
    threading.Thread(
        target=_run_company,
        args=(task_id, database_url, specs),
        name="athena-dev-company",
        daemon=True,
    ).start()
    return 202, _dump({"ok": True, "task_id": task_id, "status": "running"})


def _symbols(payload: dict) -> list:
    raw = payload.get("symbols")
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(raw, list) or not raw:
        raise ValueError("symbols must list at least one ticker.")
    if len(raw) > _MAX:
        raise ValueError(f"symbols is limited to {_MAX}.")
    return raw


def _spec(item) -> dict:
    if isinstance(item, str):
        item = {"symbol": item}
    if not isinstance(item, dict):
        raise ValueError("Each symbol is a ticker or an object.")
    symbol = str(item.get("symbol") or "").strip().upper()
    if not symbol or any(char in symbol for char in " \n\r/\\"):
        raise ValueError("Each symbol is a ticker such as NVDA or 700.")
    sec_type = str(item.get("sec_type") or "STK").strip().upper()
    if sec_type not in {"STK", "OPT"}:
        raise ValueError("sec_type is STK or OPT. An ETF uses STK.")
    spec = {"symbol": symbol, "sec_type": sec_type, "expiry": "", "strike": None, "right": ""}
    if sec_type == "OPT":
        expiry = str(item.get("expiry") or "").strip().replace("-", "")
        if len(expiry) != 8 or not expiry.isdigit():
            raise ValueError(f"{symbol} needs an expiry as YYYYMMDD.")
        try:
            strike = float(item.get("strike"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{symbol} needs a strike.") from exc
        if strike <= 0:
            raise ValueError(f"{symbol} needs a strike above 0.")
        right = str(item.get("right") or "").strip().upper()
        if right in {"CALL", "C"}:
            right = "C"
        elif right in {"PUT", "P"}:
            right = "P"
        else:
            raise ValueError(f"{symbol} right is C or P.")
        spec.update(expiry=expiry, strike=strike, right=right)
    return spec


def _fold_days(candles: list[dict]) -> list[dict]:
    """This login has no option end-of-day chart. Hourly prints become one candle per session."""
    groups: dict[str, list[dict]] = {}
    for candle in candles:
        groups.setdefault(candle["date"], []).append(candle)
    folded = []
    for session, rows in groups.items():
        folded.append({
            "date": session,
            "time": session,
            "open": rows[0]["open"],
            "high": max(row["high"] for row in rows),
            "low": min(row["low"] for row in rows),
            "close": rows[-1]["close"],
            "volume": sum(row["volume"] for row in rows),
        })
    return folded


def _default_duration(bar: str) -> str:
    if bar == "1 day":
        return "30 D"
    if bar == "1 hour":
        return "5 D"
    return "2 D"


def _run_bars(task_id: str, specs: list[dict], duration: str, bar: str, end: str, day: str) -> None:
    def work(broker: IbBroker) -> list[dict]:
        rows = []
        for spec in specs:
            rows.append(_one_bar(broker, spec, duration, bar, end, day))
        return rows

    _finish_work(task_id, work)


def _one_bar(broker: IbBroker, spec: dict, duration: str, bar: str, end: str, day: str) -> dict:
    row = _blank(spec)
    try:
        fetch_bar = "1 hour" if spec["sec_type"] == "OPT" and bar == "1 day" else bar
        candles = broker.historical(_contract(spec), duration, fetch_bar, end)
        if fetch_bar != bar:
            candles = _fold_days(candles)
        if day:
            candles = [item for item in candles if item["date"] == day]
        if not candles:
            row["status"] = "failed"
            row["error"] = "No bar on that day." if day else "IB returned no bars."
            return row
        row["bars"] = candles
        newest = max(candles, key=lambda item: item.get("time") or item.get("date") or "")
        row["latest"] = newest["close"]
        return row
    except Exception as exc:
        log.info("bars failed symbol=%s error=%s", spec["symbol"], exc)
        row["status"] = "failed"
        row["error"] = str(exc)
        return row


def _run_company(task_id: str, database_url: str, specs: list[dict]) -> None:
    def work(broker: IbBroker) -> list[dict]:
        facts = _facts(database_url, [spec["symbol"] for spec in specs])
        rows = []
        for spec in specs:
            rows.append(_one_company(broker, spec, facts.get(spec["symbol"])))
        return rows

    _finish_work(task_id, work)


def _one_company(broker: IbBroker, spec: dict, facts: tuple | None) -> dict:
    row = _blank(spec)
    row.pop("bars", None)
    try:
        described = broker.describe(_contract(spec))
    except Exception as exc:
        log.info("company failed symbol=%s error=%s", spec["symbol"], exc)
        row["status"] = "failed"
        row["error"] = str(exc)
        return row
    name = described["name"]
    cap = None
    if facts is not None:
        if facts[0]:
            name = facts[0]
        if facts[1] is not None:
            cap = float(facts[1])
    row.update(
        name=name,
        market_cap=cap,
        exchange=described["exchange"],
        primary_exchange=described["primary_exchange"],
        currency=described["currency"],
        con_id=described["con_id"],
        trading_class=described["trading_class"],
    )
    return row


def _facts(database_url: str, symbols: list[str]) -> dict[str, tuple]:
    if not database_url:
        return {}
    with connect(database_url) as conn:
        rows = conn.execute(
            "SELECT symbol, name, market_cap FROM symbol_facts WHERE symbol = ANY(%s)",
            (symbols,),
        ).fetchall()
    return {symbol: (name, cap) for symbol, name, cap in rows}


def _finish_work(task_id: str, work) -> None:
    import asyncio

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    host = os.environ.get("IB_GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("IB_GATEWAY_PORT", "4002"))
    broker = IbBroker()
    try:
        with _IB_LOCK:
            if not broker.connect(host, port, client_id("probe"), True):
                raise RuntimeError("IB Gateway did not connect.")
            try:
                result = work(broker)
            finally:
                broker.disconnect()
    except Exception as exc:
        log.info("developer task failed id=%s error=%s", task_id, exc)
        _complete(task_id, "failed", None, str(exc))
        return
    _complete(task_id, "done", {"symbols": result}, "")


def _contract(spec: dict):
    from ib_insync import Option, Stock

    if spec["sec_type"] == "OPT":
        exchange, currency = stock_venue(spec["symbol"])
        if exchange == "SMART":
            exchange = "CBOE"
        return Option(
            spec["symbol"],
            spec["expiry"],
            spec["strike"],
            spec["right"],
            exchange,
            currency=currency,
        )
    exchange, currency = stock_venue(spec["symbol"])
    return Stock(spec["symbol"], exchange, currency)


def _blank(spec: dict) -> dict:
    return {
        "symbol": spec["symbol"],
        "sec_type": spec["sec_type"],
        "expiry": spec["expiry"],
        "strike": spec["strike"],
        "right": spec["right"],
        "status": "done",
        "bars": [],
        "error": "",
    }


def _create(kind: str) -> str:
    task_id = uuid.uuid4().hex[:12]
    with _LOCK:
        _TASKS[task_id] = {
            "id": task_id,
            "kind": kind,
            "status": "running",
            "result": None,
            "error": "",
            "created": datetime.now().isoformat(timespec="seconds"),
        }
    return task_id


def _complete(task_id: str, status: str, result, error: str) -> None:
    with _LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            return
        task["status"] = status
        task["result"] = result
        task["error"] = error


def _task(task_id: str) -> tuple[int, str]:
    with _LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            return 404, _dump({"ok": False, "status": "failed", "error": "Unknown task."})
        body = {
            "ok": True,
            "task_id": task["id"],
            "kind": task["kind"],
            "status": task["status"],
            "result": task["result"],
            "error": task["error"],
        }
    return 200, _dump(body)


def _dump(payload: dict) -> str:
    return json.dumps(payload)
