"""A Gateway login that keeps order status current.

The cycle keeps the session id. This login stays up on the next id and writes what IB reports.
"""

from __future__ import annotations

import logging
import os
import queue
import secrets
import threading
import time
from datetime import date
from decimal import Decimal

from athena_store import connect

log = logging.getLogger("athena.exec")

_CANCELLED = {"Cancelled", "ApiCancelled", "Inactive"}
_LOCK = threading.Lock()
_LISTENERS: dict[str, list[queue.Queue]] = {}


def order_client_id() -> int:
    """The watcher login. One past the session id, unless IB_ORDER_CLIENT_ID is set."""
    from athena_exec.clients import client_id

    return client_id("orders")


def publish(account_id: str, order_id: str) -> None:
    """Tell open Orders pages that one row changed. A slow page drops the extra."""
    if not account_id or not order_id:
        return
    with _LOCK:
        listeners = list(_LISTENERS.get(account_id, ()))
    for listener in listeners:
        try:
            listener.put_nowait(order_id)
        except queue.Full:
            continue


def subscribe(account_id: str) -> queue.Queue:
    listener: queue.Queue = queue.Queue(maxsize=100)
    with _LOCK:
        _LISTENERS.setdefault(account_id, []).append(listener)
    return listener


def unsubscribe(account_id: str, listener: queue.Queue) -> None:
    with _LOCK:
        found = _LISTENERS.get(account_id, [])
        if listener in found:
            found.remove(listener)
        if not found:
            _LISTENERS.pop(account_id, None)


def apply_live_order(conn, ib_order_id: int, status: str, filled, remaining, avg_fill) -> str | None:
    """Write one IB update. Unknown order ids are ignored. Returns the account when a row changed."""
    if not ib_order_id:
        return None
    row = conn.execute(
        """
        SELECT id, account_id, status, ib_status, filled_qty, remaining_qty, avg_fill
        FROM orders
        WHERE ib_order_id = %s
        """,
        (ib_order_id,),
    ).fetchone()
    if row is None:
        return None
    order_id, account_id, current, live_now, filled_now, remaining_now, avg_now = row
    live = (status or "").strip()
    filled_qty = _number(filled)
    remaining_qty = _number(remaining)
    average = _price(avg_fill)
    nxt = _next_status(current, live)
    stored_live = live or (live_now or "")
    if (
        nxt == current
        and stored_live == (live_now or "")
        and _same(filled_qty, filled_now)
        and _same(remaining_qty, remaining_now)
        and _same(average, avg_now)
    ):
        return None
    conn.execute(
        """
        UPDATE orders
        SET ib_status = CASE WHEN %s = '' THEN ib_status ELSE %s END,
            filled_qty = COALESCE(%s, filled_qty),
            remaining_qty = COALESCE(%s, remaining_qty),
            avg_fill = COALESCE(%s, avg_fill),
            status = %s,
            updated_at = now()
        WHERE id = %s
        """,
        (live, live, filled_qty, remaining_qty, average, nxt, order_id),
    )
    conn.commit()
    publish(account_id, order_id)
    return account_id


def sync_ib_order(conn, trade) -> str | None:
    """Store one order IB reports for an account. An Athena row with the same order is updated."""
    view = _order_view(trade)
    if view is None:
        return None
    row = _find_order(conn, view)
    if row is None:
        return _insert_ib_order(conn, view)
    order_id, account_id, current, live_now, filled_now, remaining_now, avg_now, perm_now = row
    live = view["status"]
    nxt = _next_status(current, live)
    stored_live = live or (live_now or "")
    perm_id = view["perm_id"] or perm_now
    if (
        nxt == current
        and stored_live == (live_now or "")
        and _same(view["filled"], filled_now)
        and _same(view["remaining"], remaining_now)
        and _same(view["average"], avg_now)
        and _same(perm_id, perm_now)
    ):
        return None
    conn.execute(
        """
        UPDATE orders
        SET ib_status = CASE WHEN %s = '' THEN ib_status ELSE %s END,
            filled_qty = COALESCE(%s, filled_qty),
            remaining_qty = COALESCE(%s, remaining_qty),
            avg_fill = COALESCE(%s, avg_fill),
            status = %s,
            perm_id = COALESCE(%s, perm_id),
            updated_at = now()
        WHERE id = %s
        """,
        (live, live, view["filled"], view["remaining"], view["average"], nxt, perm_id, order_id),
    )
    conn.commit()
    publish(account_id, order_id)
    return account_id


def start_order_watch(database_url: str) -> None:
    threading.Thread(
        target=_run,
        args=(database_url,),
        name="order-watch",
        daemon=True,
    ).start()


def _run(database_url: str) -> None:
    import asyncio

    from athena_exec.broker import IbBroker

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    host = os.environ.get("IB_GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("IB_GATEWAY_PORT", "4002"))
    client_id = order_client_id()
    log.info("order watch client_id=%s", client_id)
    while True:
        broker = IbBroker()
        try:
            if not broker.connect(host, port, client_id, False):
                time.sleep(5)
                continue
            _listen(broker, database_url)
        except Exception as exc:
            log.info("order watch stopped: %s", exc)
        finally:
            broker.disconnect()
        time.sleep(5)


def _listen(broker, database_url: str) -> None:
    ib = broker._ib
    if ib is None:
        return

    def on_status(trade) -> None:
        _sync(database_url, trade)

    def on_fill(trade, _fill) -> None:
        _sync(database_url, trade)

    ib.orderStatusEvent += on_status
    ib.execDetailsEvent += on_fill
    last_completed = 0.0
    while broker.connected():
        trades: list = []
        try:
            trades.extend(ib.reqAllOpenOrders() or [])
        except Exception as exc:
            log.info("open orders: %s", exc)
        if time.time() - last_completed >= 30:
            try:
                trades.extend(ib.reqCompletedOrders(False) or [])
            except Exception as exc:
                log.info("completed orders: %s", exc)
            last_completed = time.time()
        seen: set = set()
        for trade in trades:
            key = _trade_key(trade)
            if key in seen:
                continue
            seen.add(key)
            _sync(database_url, trade)
        ib.sleep(2)


def _sync(database_url: str, trade) -> None:
    try:
        with connect(database_url) as conn:
            sync_ib_order(conn, trade)
    except Exception as exc:
        log.info("order watch write error=%s", exc.__class__.__name__)


def _order_view(trade) -> dict | None:
    order = getattr(trade, "order", None)
    contract = getattr(trade, "contract", None)
    state = getattr(trade, "orderStatus", None)
    if order is None or contract is None or state is None:
        return None
    account = str(getattr(order, "account", "") or "").strip()
    symbol = str(getattr(contract, "symbol", "") or "").strip().upper()
    action = str(getattr(order, "action", "") or "").strip().upper()
    order_id = int(getattr(order, "orderId", 0) or 0)
    perm_id = int(getattr(order, "permId", 0) or 0)
    shares = _shares(getattr(order, "totalQuantity", None))
    if not account or not symbol or action not in {"BUY", "SELL"} or shares is None:
        return None
    if not order_id and not perm_id:
        return None
    sec_type = str(getattr(contract, "secType", "") or "STK").upper()
    order_type = str(getattr(order, "orderType", "") or "LMT").upper()
    expiry = _expiry(getattr(contract, "lastTradeDateOrContractMonth", None)) if sec_type == "OPT" else None
    strike = _price(getattr(contract, "strike", None)) if sec_type == "OPT" else None
    return {
        "account": account,
        "symbol": symbol,
        "action": action,
        "shares": shares,
        "ib_order_id": order_id or None,
        "perm_id": perm_id or None,
        "order_type": order_type,
        "limit": _price(getattr(order, "lmtPrice", None)),
        "stop": _price(getattr(order, "auxPrice", None)) if "STP" in order_type else None,
        "status": str(getattr(state, "status", "") or "").strip(),
        "filled": _number(getattr(state, "filled", None)),
        "remaining": _number(getattr(state, "remaining", None)),
        "average": _price(getattr(state, "avgFillPrice", None)),
        "sec_type": "OPT" if sec_type == "OPT" else "STK",
        "expiry": expiry,
        "strike": strike,
        "right": str(getattr(contract, "right", "") or "") if sec_type == "OPT" else "",
        "trading_class": str(getattr(contract, "tradingClass", "") or "") if sec_type == "OPT" else "",
        "exchange": str(getattr(contract, "exchange", "") or "") if sec_type == "OPT" else "",
        "multiplier": str(getattr(contract, "multiplier", "") or ("100" if sec_type == "OPT" else "1")),
    }


def _find_order(conn, view: dict):
    perm_id = view["perm_id"]
    if perm_id:
        found = conn.execute(
            """
            SELECT id, account_id, status, ib_status, filled_qty, remaining_qty, avg_fill, perm_id
            FROM orders
            WHERE account_id = %s AND perm_id = %s
            """,
            (view["account"], perm_id),
        ).fetchone()
        if found is not None:
            return found
    if not view["ib_order_id"]:
        return None
    incoming_done = view["status"] == "Filled" or view["status"] in _CANCELLED
    rows = conn.execute(
        """
        SELECT id, account_id, status, ib_status, filled_qty, remaining_qty, avg_fill, perm_id
        FROM orders
        WHERE account_id = %s AND ib_order_id = %s AND symbol = %s
        ORDER BY created_at DESC, id DESC
        """,
        (view["account"], view["ib_order_id"], view["symbol"]),
    ).fetchall()
    for row in rows:
        row_perm = row[7]
        if perm_id and row_perm and int(row_perm) != perm_id:
            continue
        if row[2] in {"filled", "cancelled", "blocked"} and not incoming_done:
            continue
        return row
    return None


def _insert_ib_order(conn, view: dict) -> str:
    draft_id = "draft_" + secrets.token_hex(3)
    order_id = "ord_" + secrets.token_hex(3)
    reason = f"IB {view['action']} {view['shares']} {view['symbol']}"
    conn.execute(
        """
        INSERT INTO drafts (
            id, symbol, tactic, contracts, status, reason, phase, account_id,
            instrument, limit_price, multiplier, order_action
        )
        VALUES (%s, %s, 'EXTERNAL', %s, 'approved', %s, '', %s, %s, %s, %s, %s)
        """,
        (
            draft_id, view["symbol"], view["shares"], reason, view["account"],
            view["sec_type"], view["limit"], view["multiplier"], view["action"],
        ),
    )
    conn.execute(
        """
        INSERT INTO orders (
            id, draft_id, account_id, symbol, tactic, action, contracts, limit_price,
            status, ib_order_id, multiplier, order_type, stop_price,
            expiry, strike, contract_right, trading_class, option_exchange,
            ib_status, filled_qty, remaining_qty, avg_fill, updated_at, perm_id
        )
        VALUES (
            %s, %s, %s, %s, 'EXTERNAL', %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, now(), %s
        )
        """,
        (
            order_id, draft_id, view["account"], view["symbol"], view["action"], view["shares"], view["limit"],
            _next_status("submitted", view["status"]), view["ib_order_id"], view["multiplier"],
            view["order_type"], view["stop"],
            view["expiry"], view["strike"], view["right"], view["trading_class"], view["exchange"],
            view["status"], view["filled"], view["remaining"], view["average"], view["perm_id"],
        ),
    )
    conn.commit()
    publish(view["account"], order_id)
    log.info("ib order synced symbol=%s account=%s ib=%s", view["symbol"], view["account"], view["ib_order_id"])
    return view["account"]


def _trade_key(trade) -> tuple:
    order = getattr(trade, "order", None)
    contract = getattr(trade, "contract", None)
    perm_id = int(getattr(order, "permId", 0) or 0) if order is not None else 0
    order_id = int(getattr(order, "orderId", 0) or 0) if order is not None else 0
    symbol = str(getattr(contract, "symbol", "") or "") if contract is not None else ""
    account = str(getattr(order, "account", "") or "") if order is not None else ""
    if perm_id:
        return ("perm", perm_id, account)
    return ("order", order_id, symbol, account)


def _shares(value) -> int | None:
    number = _number(value)
    if number is None or number < 1:
        return None
    return int(number)


def _expiry(text: str | None) -> date | None:
    raw = (text or "").strip()
    if len(raw) >= 8 and raw[:8].isdigit():
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    return None


def _next_status(current: str, live: str) -> str:
    if live == "Filled":
        return "filled"
    if live in _CANCELLED:
        return "cancelled"
    if current in {"filled", "cancelled", "blocked"}:
        return current
    if live and current == "queued":
        return "submitted"
    return current


def _number(value):
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    if not number.is_finite():
        return None
    return number


def _price(value):
    number = _number(value)
    if number is None or number <= 0:
        return None
    return number


def _same(left, right) -> bool:
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    return Decimal(left) == Decimal(right)
