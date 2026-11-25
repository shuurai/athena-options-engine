"""V2-P2 reconciler: backfill only derivable observation events (§20.2.4).

Domain rows are the single source of truth. This daemon reconstructs
``draft_created`` and terminal draft transitions that a failed post-commit
emit left out. It never invents ``filtered`` / ``quiet`` / ``factor_computed``
events; intervals that cannot be reconstructed stay honest
``observation_gap`` territory and are never filled with decorative motion.
"""

from __future__ import annotations

import logging
import threading

from athena_store import strategy_events

log = logging.getLogger(__name__)

_THREAD: threading.Thread | None = None
_STOP = threading.Event()
_LOCK = threading.Lock()
INTERVAL_SECONDS = 60


def run_once(conn, *, account_id: str) -> dict[str, int]:
    """One reconciliation pass. Pure DB, safe inside a caller-owned transaction."""
    return strategy_events.reconcile_drafts(conn, account_id=account_id)


def accounts(conn) -> list[str]:
    """Every account that actually has draft rows. No settings/wheel reads."""
    rows = conn.execute(
        "SELECT DISTINCT account_id FROM drafts "
        "WHERE account_id <> '' ORDER BY account_id"
    ).fetchall()
    return [str(row[0]) for row in rows]


def _scheduler_loop(database_url: str) -> None:
    from athena_store import connect

    while not _STOP.wait(INTERVAL_SECONDS):
        try:
            with connect(database_url) as conn:
                for account in accounts(conn):
                    result = run_once(conn, account_id=account)
                    if result.get("gaps"):
                        log.warning(
                            "iris reconcile gaps account=%s gaps=%s",
                            account, result["gaps"],
                        )
        except Exception:
            log.exception("iris reconcile pass failed")


def start_iris_reconciler(database_url: str) -> None:
    """Start (or restart) the reconciler daemon. Safe to call more than once."""
    global _THREAD
    _STOP.clear()
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return
        _THREAD = threading.Thread(
            target=_scheduler_loop, args=(database_url,),
            name="iris-p2-reconcile", daemon=True,
        )
        _THREAD.start()


def stop_iris_reconciler() -> None:
    """Test seam: stop the daemon without waiting for the next interval."""
    _STOP.set()
