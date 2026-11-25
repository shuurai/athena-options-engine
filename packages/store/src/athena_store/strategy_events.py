"""Append-only narrow observation read model for Iris (V2-P2, Q4/Q15).

Domain commits are the single source of truth. This module never writes inside a
domain transaction and never rolls one back: ``emit_after_commit`` is best
effort, post-commit, and idempotent via a deterministic ``event_id``. A failed
observation write therefore cannot change drafts / approvals / orders.

The payload is an allowlist of structured scalars. Free-text ``reason`` stays in
``drafts.reason`` (§21.2, §11.7): no prompt secret, token, credential or prose.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Any, Callable

GRAPH_VERSION = "iris-p2-events-v1"

# Optional live observers. The domain layer (athena-strategy-nfr) may import
# this module, but the console-only fan-out registry lives in the orchestrator.
# Registering it here avoids a strategy-nfr -> orchestrator dependency cycle.
_OBSERVERS: list[Callable[[dict], None]] = []
_OBSERVER_LOCK = threading.Lock()


class UnknownCursor(ValueError):
    """Raised when a replay cursor does not exist for the requested account."""


def add_observer(fn: Callable[[dict], None]) -> None:
    """Attach a live observer. Idempotent; callers must never be able to block emit."""
    with _OBSERVER_LOCK:
        if fn not in _OBSERVERS:
            _OBSERVERS.append(fn)


def remove_observer(fn: Callable[[dict], None]) -> None:
    with _OBSERVER_LOCK:
        if fn in _OBSERVERS:
            _OBSERVERS.remove(fn)


def clear_observers() -> None:
    """Test seam: drop every observer."""
    with _OBSERVER_LOCK:
        _OBSERVERS.clear()


def observer_count() -> int:
    with _OBSERVER_LOCK:
        return len(_OBSERVERS)


def _notify(event: dict[str, Any]) -> None:
    with _OBSERVER_LOCK:
        observers = tuple(_OBSERVERS)
    for fn in observers:
        try:
            fn(event)
        except Exception:
            pass  # a broken observer must never affect the observation write

EVENT_TYPES = frozenset(
    {
        "news_ingested",
        "factor_computed",
        "strategy_started",
        "strategy_completed",
        "filtered",
        "quiet",
        "invalid",
        "draft_created",
        "draft_approved",
        "draft_rejected",
        "draft_expired",
        "draft_blocked",
        "draft_amended",
        "draft_replaced",
        "order_queued",
        "order_submitted",
        "order_blocked",
        "order_cancelled",
        "order_filled",
        "partial_fill",
        "observation_gap",
    }
)

# Structured scalars only. No free-text reason, no prose, no credentials.
PAYLOAD_KEYS = frozenset(
    {
        "tactic",
        "contracts",
        "symbol",
        "edge_id",
        "reason_code",
        "from_status",
        "to_status",
        "run_id",
        "instrument",
        "limit_price",
        "draft_status",
        "order_status",
        "filled_qty",
        "remaining_qty",
        "filter",
        "factor_version",
    }
)
MAX_PAYLOAD_BYTES = 4096

INSERT_SQL = """
INSERT INTO strategy_events (
    event_id, run_id, account_id, subject_type, subject_id, status,
    from_status, to_status, reason_code, symbol, edge_id, graph_version,
    occurred_at, payload
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
ON CONFLICT (event_id) DO NOTHING
"""

_REPLAY_COLUMNS = """
SELECT event_id, run_id, account_id, subject_type, subject_id, status,
       from_status, to_status, reason_code, symbol, edge_id, graph_version,
       occurred_at, payload
FROM strategy_events
"""

CURSOR_SQL = """
SELECT occurred_at, event_id FROM strategy_events
WHERE event_id = %s AND account_id = %s
"""

REPLAY_SQL = _REPLAY_COLUMNS + """
WHERE account_id = %s
ORDER BY occurred_at, event_id
LIMIT %s
"""

REPLAY_AFTER_SQL = _REPLAY_COLUMNS + """
WHERE account_id = %s AND (occurred_at, event_id) > (%s::timestamptz, %s)
ORDER BY occurred_at, event_id
LIMIT %s
"""


def event_id(
    *,
    subject_type: str,
    subject_id: str,
    transition: str,
    run_id: str = "",
    version: str = GRAPH_VERSION,
) -> str:
    """Deterministic id: same subject + transition + version converges on one row."""
    raw = f"v1|{version}|{subject_type}|{subject_id}|{transition}|{run_id}"
    return "sev_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def validate_event(event: dict[str, Any]) -> None:
    """Reject anything that is not a structured, allowlisted observation."""
    if not isinstance(event, dict):
        raise ValueError("event must be a dict")
    status = event.get("status")
    if status not in EVENT_TYPES:
        raise ValueError(f"unknown event status: {status!r}")
    for key in ("account_id", "subject_type", "subject_id", "transition"):
        if not isinstance(event.get(key), str) or not event[key]:
            raise ValueError(f"{key} must be a non-empty string")
    payload = event.get("payload", {})
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")
    if "reason" in payload or "reason_text" in payload:
        raise ValueError("free-text reason is not allowed in payload")
    extra = set(payload) - PAYLOAD_KEYS
    if extra:
        raise ValueError(f"payload keys outside allowlist: {sorted(extra)}")
    encoded = json.dumps(payload, sort_keys=True, default=str)
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError("payload exceeds size cap")
    for key in ("from_status", "to_status", "reason_code", "symbol", "edge_id", "run_id"):
        value = event.get(key, "")
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
    occurred_at = event.get("occurred_at")
    if occurred_at is None:
        return
    if isinstance(occurred_at, str):
        datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
    elif not isinstance(occurred_at, datetime):
        raise ValueError("occurred_at must be datetime or ISO string")


def build_event(
    *,
    account_id: str,
    subject_type: str,
    subject_id: str,
    status: str,
    transition: str,
    from_status: str = "",
    to_status: str = "",
    reason_code: str = "",
    symbol: str = "",
    edge_id: str = "",
    run_id: str = "",
    payload: dict[str, Any] | None = None,
    occurred_at: datetime | str | None = None,
    version: str = GRAPH_VERSION,
) -> dict[str, Any]:
    event = {
        "event_id": event_id(
            subject_type=subject_type,
            subject_id=subject_id,
            transition=transition,
            run_id=run_id,
            version=version,
        ),
        "run_id": run_id,
        "account_id": account_id,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "status": status,
        "from_status": from_status,
        "to_status": to_status,
        "reason_code": reason_code,
        "symbol": symbol,
        "edge_id": edge_id,
        "graph_version": version,
        "occurred_at": occurred_at or _now(),
        "payload": payload or {},
        "transition": transition,
    }
    validate_event(event)
    return event


def _params(event: dict[str, Any]) -> tuple:
    return (
        event["event_id"],
        event["run_id"],
        event["account_id"],
        event["subject_type"],
        event["subject_id"],
        event["status"],
        event["from_status"],
        event["to_status"],
        event["reason_code"],
        event["symbol"],
        event["edge_id"],
        event["graph_version"],
        event["occurred_at"],
        json.dumps(event["payload"], sort_keys=True, default=str),
    )


def emit_after_commit(conn, event: dict[str, Any]) -> bool:
    """Best-effort observation write. Call ONLY after the domain ``commit()``.

    Never raises and never propagates a rollback into domain work. Returns
    False on any failure so the caller can record ``observation_gap``.
    """
    try:
        validate_event(event)
        conn.execute(INSERT_SQL, _params(event))
        conn.commit()
    except Exception:
        try:
            conn.rollback()  # clears only the failed post-commit transaction
        except Exception:
            pass
        return False
    _notify(event)
    return True


def emit_observation_gap(
    conn, *, account_id: str, subject_id: str, reason_code: str, run_id: str = ""
) -> bool:
    """Explicit, honest marker for an interval that cannot be reconstructed."""
    return emit_after_commit(
        conn,
        build_event(
            account_id=account_id,
            subject_type="observation",
            subject_id=subject_id,
            status="observation_gap",
            transition=reason_code,
            from_status="",
            to_status="",
            reason_code=reason_code,
            run_id=run_id,
        ),
    )


_REPLAY_KEYS = (
    "event_id", "run_id", "account_id", "subject_type", "subject_id", "status",
    "from_status", "to_status", "reason_code", "symbol", "edge_id",
    "graph_version", "occurred_at", "payload",
)


def _as_rows(rows) -> list[dict[str, Any]]:
    return [dict(zip(_REPLAY_KEYS, row, strict=True)) for row in rows]


def replay(
    conn, *, account_id: str, after_event_id: str = "", limit: int = 200
) -> list[dict[str, Any]]:
    """Cursor-based replay ordered by (occurred_at, event_id).

    An unknown or cross-account cursor raises ``UnknownCursor`` rather than
    silently returning zero rows: the caller must show ``observation_gap``
    instead of pretending the interval was empty.
    """
    if after_event_id:
        position = conn.execute(
            CURSOR_SQL, (after_event_id, account_id)
        ).fetchone()
        if position is None:
            raise UnknownCursor(f"cursor {after_event_id!r} not found for account")
        rows = conn.execute(
            REPLAY_AFTER_SQL, (account_id, position[0], position[1], limit)
        ).fetchall()
    else:
        rows = conn.execute(REPLAY_SQL, (account_id, limit)).fetchall()
    return _as_rows(rows)


def observe_event(
    conn,
    *,
    account_id: str,
    subject_type: str,
    subject_id: str,
    status: str,
    transition: str,
    symbol: str = "",
    tactic: str = "",
    from_status: str = "",
    to_status: str = "",
    reason_code: str = "",
    payload: dict[str, Any] | None = None,
    occurred_at: datetime | str | None = None,
) -> bool:
    """Q15 post-commit observation of one domain transition.

    Operator-direct tactics (Q12 / §20.2.6) are deliberately not projected as
    a strategy chain. A failed write records one honest ``observation_gap``;
    the domain commit is already durable and is never touched.
    """
    if subject_type == "draft" and str(tactic).upper() in {"EXTERNAL", "MANUAL"}:
        return False
    event = build_event(
        account_id=account_id,
        subject_type=subject_type,
        subject_id=subject_id,
        status=status,
        transition=transition,
        from_status=from_status,
        to_status=to_status,
        reason_code=reason_code or status,
        symbol=symbol,
        run_id="",
        payload=payload,
        occurred_at=occurred_at,
    )
    if emit_after_commit(conn, event):
        return True
    emit_observation_gap(
        conn, account_id=account_id, subject_id=subject_id,
        reason_code="event_write_failed",
    )
    return False


def event_count(conn, *, account_id: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM strategy_events WHERE account_id = %s", (account_id,)
    ).fetchone()
    return int(row[0]) if row else 0


def reconcile_drafts(conn, *, account_id: str) -> dict[str, int]:
    """Backfill only derivable transitions from persisted domain rows.

    Reconstructs ``draft_created`` and each terminal status from ``drafts``.
    It does NOT invent filtered/quiet/factor events; those remain gaps.
    """
    rows = conn.execute(
        "SELECT id, symbol, tactic, status, created_at, account_id "
        "FROM drafts WHERE account_id = %s ORDER BY created_at, id",
        (account_id,),
    ).fetchall()
    terminal = {
        "approved": "draft_approved",
        "rejected": "draft_rejected",
        "expired": "draft_expired",
        "blocked": "draft_blocked",
        "amended": "draft_amended",
        "replaced": "draft_replaced",
    }
    emitted = 0
    gaps = 0
    for draft_id, symbol, tactic, status, created_at, _account in rows:
        candidates = [("draft_created", "created")]
        if status in terminal:
            candidates.append((terminal[status], f"created->{status}"))
        for event_status, transition in candidates:
            ok = emit_after_commit(
                conn,
                build_event(
                    account_id=account_id,
                    subject_type="draft",
                    subject_id=draft_id,
                    status=event_status,
                    transition=transition,
                    from_status="pending" if event_status != "draft_created" else "",
                    to_status=status if event_status != "draft_created" else "pending",
                    symbol=symbol or "",
                    reason_code=event_status,
                    occurred_at=created_at,
                    payload={"tactic": tactic or "", "draft_status": status},
                ),
            )
            if ok:
                emitted += 1
            else:
                gaps += 1
    return {"emitted": emitted, "gaps": gaps, "drafts": len(rows)}
