"""R3 scheduled guardian runs (booklet 14, section 14). Thin I/O adapter.

Layering (same split as R2's guardian_view, section 12):

* pure:      ``report_payload``, ``render_run_history``, ``RunGrade``
* schedule:  ``guardian_schedule`` (pure, zero I/O)
* grading:   ``athena_risk.guardian`` (R1, pure, zero I/O)
* reconcile: ``guardian_view.sync`` (R2, reused verbatim)
* I/O here:  fetch inputs, call sync, persist one grade row, run the daemon

R3 executes A and B only. Run C is derived by guardian_schedule but deliberately
left to R7 (D71); no guardian_runs row and no push are produced for it here.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html import escape
from typing import Mapping, Sequence

from psycopg.types.json import Jsonb

from athena_orchestrator import guardian_view as gv
from athena_orchestrator.guardian_schedule import (
    EXECUTED_RUNS,
    RunSlot,
    hk_clock,
    next_run,
)

__all__ = [
    "RunGrade",
    "report_payload",
    "write_run",
    "load_runs",
    "render_run_history",
    "run_once",
    "start_guardian_scheduler",
    "stop_guardian_scheduler",
]

log = logging.getLogger("athena")

_STOP = threading.Event()
_THREAD: threading.Thread | None = None

# Idempotency is enforced by the unique key in 054_guardian_runs.sql. A
# re-fire in the same (account, day, run) slot is swallowed, never rewritten.
_WRITE_RUN_SQL = """
INSERT INTO guardian_runs
    (account_id, trade_day, run, scheduled_at, ran_at,
     portfolio_tier, account_tier, breach_count, proximity_count, unmanaged_count,
     el_nl_pct, maint_nl_pct, account_flags, positions, status, detail)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (account_id, trade_day, run) DO NOTHING
"""

_LOAD_RUNS_SQL = """
SELECT trade_day, run, scheduled_at, ran_at, portfolio_tier,
       breach_count, proximity_count, unmanaged_count, status, detail
FROM guardian_runs
WHERE account_id = %s
ORDER BY ran_at DESC, id DESC
LIMIT %s
"""


@dataclass(frozen=True)
class RunGrade:
    account_id: str
    trade_day: date
    run: str
    scheduled_at: datetime
    ran_at: datetime
    payload: dict
    status: str = "ok"
    detail: str = ""


# ---------------------------------------------------------------------------
# Pure: R1 report -> storable payload
# ---------------------------------------------------------------------------


def report_payload(report) -> dict:
    """Serialize GuardianReport. Missing numbers stay None, never invented."""
    return {
        "portfolio_tier": report.portfolio_tier.value,
        "account_tier": report.account_tier.value,
        "breach_count": int(report.breach_count),
        "proximity_count": int(report.proximity_count),
        "unmanaged_count": int(report.unmanaged_count),
        "el_nl_pct": report.el_nl_pct,
        "maint_nl_pct": report.maint_nl_pct,
        "account_flags": [str(flag) for flag in report.account_flags],
        "positions": [
            {
                "symbol": str(item.symbol),
                "tier": item.tier.value,
                "flags": [str(flag) for flag in item.flags],
                "unmanaged": bool(item.unmanaged),
            }
            for item in report.positions
        ],
    }


# ---------------------------------------------------------------------------
# Thin SQL
# ---------------------------------------------------------------------------


def write_run(conn, grade: RunGrade) -> None:
    p = grade.payload
    conn.execute(
        _WRITE_RUN_SQL,
        (
            grade.account_id, grade.trade_day, grade.run,
            grade.scheduled_at, grade.ran_at,
            p["portfolio_tier"], p["account_tier"],
            p["breach_count"], p["proximity_count"], p["unmanaged_count"],
            p["el_nl_pct"], p["maint_nl_pct"],
            Jsonb(p["account_flags"]), Jsonb(p["positions"]),
            grade.status, grade.detail,
        ),
    )


def load_runs(conn, account: str, limit: int = 6) -> list[dict]:
    """Recent scheduled grades for the /guard read-only card."""
    if not account:
        return []
    found = conn.execute(_LOAD_RUNS_SQL, (account, limit)).fetchall()
    rows: list[dict] = []
    for r in found:
        scheduled = r[2]
        hk = hk_clock(scheduled) if isinstance(scheduled, datetime) else ""
        rows.append({
            "trade_day": str(r[0]),
            "run": str(r[1]),
            "scheduled_at": scheduled,
            "hk": hk,
            "ran_at": r[3],
            "portfolio_tier": str(r[4]),
            "breach_count": int(r[5]),
            "proximity_count": int(r[6]),
            "unmanaged_count": int(r[7]),
            "status": str(r[8]),
            "detail": str(r[9] or ""),
        })
    return rows


# ---------------------------------------------------------------------------
# Pure HTML: read-only card appended into /guard's guard-stack
# ---------------------------------------------------------------------------


def render_run_history(rows: Sequence[Mapping]) -> str:
    """Pure. Every dynamic cell escaped. Empty list -> no card at all."""
    if not rows:
        return ""
    lines = []
    for row in rows:
        lines.append(
            "<tr>"
            f"<td>{escape(str(row.get('trade_day') or ''))}</td>"
            f"<td>{escape(str(row.get('run') or ''))}</td>"
            f"<td>{escape(str(row.get('hk') or ''))} HKT</td>"
            f"<td>{escape(str(row.get('portfolio_tier') or ''))}</td>"
            f"<td>{escape(str(row.get('breach_count') or 0))}</td>"
            f"<td>{escape(str(row.get('status') or ''))}</td>"
            "</tr>"
        )
    return (
        '<div class="card guard-runs"><h2>Scheduled runs</h2>'
        '<table class="guard-history"><thead><tr>'
        "<th>Day (ET)</th><th>Run</th><th>Fire (HKT)</th>"
        "<th>Portfolio</th><th>Breaches</th><th>Status</th>"
        "</tr></thead><tbody>" + "".join(lines) + "</tbody></table>"
        '<p class="guard-empty">Latest A / B grades. Run C (12:30 ET) is reserved for R7.</p>'
        "</div>"
    )


# ---------------------------------------------------------------------------
# One run + daemon thread
# ---------------------------------------------------------------------------


def _default_fetch(conn, account: str):
    """R2's /guard page already owns these three fetchers. Reuse, don't copy.

    Lazy: pages imports guardian_view, so a module-level import here would be a
    needless cycle. This is the only web-layer seam in R3 (section 14.4).
    """
    from athena_orchestrator.pages import _guard_inputs

    return _guard_inputs(conn, account)


def run_once(conn, account: str, slot: RunSlot, *, fetch=None, now: datetime | None = None):
    """One scheduled grade. Returns the Snapshot produced by R2's sync()."""
    moment = now or datetime.now(timezone.utc)
    inputs = fetch or _default_fetch
    settings, rows, drafts = inputs(conn, account)
    snapshot, _alerts = gv.sync(conn, account, rows, drafts, settings, now=moment)
    write_run(conn, RunGrade(
        account_id=account,
        trade_day=slot.trade_day,
        run=slot.run,
        scheduled_at=slot.moment,
        ran_at=moment,
        payload=report_payload(snapshot.report),
    ))
    conn.commit()
    return snapshot


def _scheduler_loop(database_url: str) -> None:
    from athena_exec.accounts import active_accounts
    from athena_store import connect

    while not _STOP.is_set():
        slot = next_run(datetime.now(timezone.utc), runs=EXECUTED_RUNS)
        if slot is None:
            if _STOP.wait(60):
                break
            continue
        delay = max(0.0, (slot.moment - datetime.now(timezone.utc)).total_seconds())
        if _STOP.wait(delay):
            break
        if _STOP.is_set():
            break
        for account in active_accounts():
            try:
                with connect(database_url) as conn:
                    run_once(conn, account, slot)
                log.info(
                    "guardian run ok account=%s run=%s day=%s hkt=%s",
                    account, slot.run, slot.trade_day, hk_clock(slot.moment),
                )
            except Exception:
                log.exception(
                    "guardian run failed account=%s run=%s", account, slot.run,
                )


def start_guardian_scheduler(database_url: str) -> None:
    """Start (or restart) the daemon. Safe to call more than once."""
    global _THREAD
    _STOP.clear()
    if _THREAD is not None and _THREAD.is_alive():
        return
    _THREAD = threading.Thread(
        target=_scheduler_loop, args=(database_url,),
        name="guardian-r3", daemon=True,
    )
    _THREAD.start()


def stop_guardian_scheduler() -> None:
    _STOP.set()
