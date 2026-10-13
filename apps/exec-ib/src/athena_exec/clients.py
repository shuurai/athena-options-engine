"""One client id per Gateway login.

The session id is IB_CLIENT_ID. The other logins sit at a fixed offset so a
cycle on id 2 does not block a probe or the order watch.
"""

from __future__ import annotations

import os

_OFFSETS = {
    "session": 0,
    "orders": 1,
    "probe": 2,
    "pulse": 3,
    "telegram": 20,
}


def client_id(role: str, fallback: int = 2) -> int:
    """The Gateway client id for this role. Orders may set IB_ORDER_CLIENT_ID."""
    if role == "orders":
        chosen = os.environ.get("IB_ORDER_CLIENT_ID", "").strip()
        if chosen:
            return int(chosen)
    if role not in _OFFSETS:
        raise KeyError(role)
    raw = os.environ.get("IB_CLIENT_ID", "").strip()
    base = int(raw) if raw else fallback
    return base + _OFFSETS[role]
