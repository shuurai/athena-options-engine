"""A Gateway login that keeps order status current.

The cycle keeps the session id. This login stays up on the next id and writes what IB reports.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
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
        state = trade.orderStatus
        _record(
            database_url,
            int(trade.order.orderId or 0),
            state.status or "",
            state.filled,
            state.remaining,
            state.avgFillPrice,
        )

    def on_fill(trade, fill) -> None:
        execution = fill.execution
        _record(
            database_url,
            int(execution.orderId or 0),
            "",
            execution.cumQty,
            None,
            execution.avgPrice,
        )

    ib.orderStatusEvent += on_status
    ib.execDetailsEvent += on_fill
    while broker.connected():
        ib.reqAllOpenOrders()
        ib.sleep(2)


def _record(database_url: str, ib_order_id: int, status: str, filled, remaining, avg_fill) -> None:
    try:
        with connect(database_url) as conn:
            apply_live_order(conn, ib_order_id, status, filled, remaining, avg_fill)
    except Exception as exc:
        log.info("order watch write ib=%s error=%s", ib_order_id, exc)


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
