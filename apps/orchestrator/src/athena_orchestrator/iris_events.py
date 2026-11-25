"""In-process live fan-out for Iris observation events (V2-P2).

Deliberately separate from ``runtime._TRAIL`` / ``/api/logs`` (§21.2): log
lines and structured Iris frames never share a stream. This is NOT persisted
replay — reconnect always re-reads ``strategy_events`` from Postgres.
"""

from __future__ import annotations

import queue
import threading
from collections import deque

_LOCK = threading.Lock()
_RECENT: deque[dict] = deque(maxlen=200)
_LISTENERS: list[queue.Queue] = []


def publish(event: dict) -> None:
    """Fan out one structured event frame. Never raises."""
    with _LOCK:
        _RECENT.append(event)
        listeners = list(_LISTENERS)
    for listener in listeners:
        try:
            listener.put_nowait(event)
        except queue.Full:
            pass


def recent() -> list[dict]:
    with _LOCK:
        return list(_RECENT)


def subscribe() -> queue.Queue:
    listener: queue.Queue = queue.Queue(maxsize=256)
    with _LOCK:
        _LISTENERS.append(listener)
    return listener


def unsubscribe(listener: queue.Queue) -> None:
    with _LOCK:
        if listener in _LISTENERS:
            _LISTENERS.remove(listener)


def listener_count() -> int:
    with _LOCK:
        return len(_LISTENERS)


_ATTACHED = False


def attach() -> None:
    """Register ``publish`` as a live observer of post-commit event writes.

    Idempotent. The store module owns the observer list, so the domain layer
    never has to import the orchestrator (§21.2: separate live registry).
    """
    global _ATTACHED
    from athena_store import strategy_events

    with _LOCK:
        if _ATTACHED:
            return
        _ATTACHED = True
    strategy_events.add_observer(publish)


def detach() -> None:
    """Test seam: unregister the observer and drop the attached flag."""
    global _ATTACHED
    from athena_store import strategy_events

    with _LOCK:
        _ATTACHED = False
    strategy_events.remove_observer(publish)
