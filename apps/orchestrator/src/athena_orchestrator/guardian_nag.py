"""R4 Guardian nag + dead-man (booklet 14, section 17 / D80-D83).

Layering, same split as R1/R2/R3:

* pure:      ``due_alerts``, ``nag_text``, ``duplicate_live_keys``,
             ``deadman_findings``, ``expected_slots`` (zero I/O)
* schedule:  ``guardian_schedule`` (pure, DST-safe ET)
* state:     R2's ``guardian_alerts`` + its partial unique index. D83 puts
             uniqueness in the *state layer*, NOT in a process-memory table.
* I/O here:  one dedicated daemon thread at a fixed 300s cadence (D82),
             touch the heartbeat, detect missed/failed runs, read due alerts,
             and hand texts to ``athena_bots`` for delivery.

D80: process death cannot be reported from inside the dead process — the only
Telegram sender is the in-process daemon. R4 therefore does the observable
parts: failed-run rows, a 5-minute heartbeat, and post-restart catch-up.
Realtime "down while dead" alerting belongs to R6's external watchdog.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Mapping, Sequence

from athena_orchestrator import guardian_view as gv
from athena_orchestrator.guardian_schedule import (
    ET,
    EXECUTED_RUNS,
    RunSlot,
    is_us_session,
    is_us_weekday,
    run_moments,
)

__all__ = [
    "NAG_INTERVAL",
    "NAG_TIERS",
    "DEADMAN_SOURCE",
    "NagRow",
    "TickPlan",
    "due_alerts",
    "nag_text",
    "duplicate_live_keys",
    "duplicate_report",
    "expected_slots",
    "deadman_findings",
    "deadman_breach",
    "deadman_text",
    "nag_tick",
    "install_guardian_callbacks",
    "start_guardian_nag",
    "stop_guardian_nag",
]

log = logging.getLogger("athena")

# D82: hard-wired interval, never read from Settings.
NAG_INTERVAL = 300
# D61: only red + black. Balanced/healthy/profit never nag.
NAG_TIERS = frozenset({"danger", "blowup"})
# §17.5 source registration: 'grade' (R2) / 'trigger' (R5) / 'deadman' (R4).
DEADMAN_SOURCE = "deadman"
_LIVE_STATES = frozenset({"open", "skipped"})
# D65: 10 minutes, not 5 — a short network blip must not cry "down".
DEADMAN_GRACE = timedelta(minutes=10)

_STOP = threading.Event()
_THREAD: threading.Thread | None = None


@dataclass(frozen=True)
class NagRow:
    id: int
    account_id: str
    symbol: str
    breach: str
    tier: str
    state: str


@dataclass(frozen=True)
class TickPlan:
    nags: tuple[tuple[int, str], ...] = ()
    monitor: str = ""
    deadman: int = 0
    signature: tuple[tuple[str, str, str, int], ...] = ()


# ---------------------------------------------------------------------------
# Pure core (zero I/O)
# ---------------------------------------------------------------------------


def due_alerts(rows: Sequence[Mapping]) -> list[NagRow]:
    """D61 + D62 + D83: only ``open`` red/black rows are due.

    No in-memory dedup table. ``skipped`` is an ack (24h sleep), ``resolved``
    stays confirmed, ``auto_cleared`` is terminal — none of them nag.
    Uniqueness of live rows is guaranteed by ``guardian_alerts_live``.
    """
    out: list[NagRow] = []
    for row in rows:
        if str(row.get("state") or "") != "open":
            continue
        if str(row.get("tier") or "") not in NAG_TIERS:
            continue
        out.append(NagRow(
            id=int(row["id"]),
            account_id=str(row["account_id"]),
            symbol=str(row["symbol"]),
            breach=str(row["breach"]),
            tier=str(row["tier"]),
            state=str(row["state"]),
        ))
    return out


def nag_text(row: NagRow) -> str:
    """One message per alert, with its own inline Resolve (D62 / D83)."""
    scope = "PORTFOLIO" if row.symbol == gv.ACCOUNT_SCOPE else row.symbol
    return "\n".join([
        "GUARDIAN — action needed",
        f"Account {row.account_id}",
        f"Scope {scope}",
        f"Breach {row.breach}",
        f"Tier {row.tier}",
        "Resolve on this message to stop the 5-minute nag.",
    ])


def duplicate_live_keys(rows: Sequence[Mapping]) -> tuple[tuple[str, str, str, int], ...]:
    """D83 monitoring: pure equivalent of GROUP BY ... HAVING count(*) > 1.

    The partial unique index makes this permanently empty in a healthy DB;
    the function exists so the invariant has a testable, observable guard.
    """
    counts: dict[tuple[str, str, str], int] = {}
    for row in rows:
        if str(row.get("state") or "") not in _LIVE_STATES:
            continue
        key = (str(row["account_id"]), str(row["symbol"]), str(row["breach"]))
        counts[key] = counts.get(key, 0) + 1
    return tuple(sorted((*key, n) for key, n in counts.items() if n > 1))


def duplicate_report(dups: Sequence[tuple[str, str, str, int]]) -> str:
    """One monitoring message (D71 single-push, never part of the nag loop)."""
    if not dups:
        return ""
    lines = ["GUARDIAN MONITOR — duplicate live alert rows detected (D83)"]
    lines.extend(f"{a} / {s} / {b} x{n}" for a, s, b, n in dups)
    lines.append("guardian_alerts_live partial unique index may be missing.")
    return "\n".join(lines)


def expected_slots(now: datetime) -> list[RunSlot]:
    """A/B slots for the current ET trading day only. Pure and DST-safe.

    Honest gap (section 14.5): no US holiday calendar, so a holiday is an
    ordinary weekday. We deliberately do NOT look back over older days:
    ``054`` only started recording with R3, so older days have no rows by
    construction and would look like phantom outages. Catch-up is scoped to
    today's A/B slots (D80 "post-restart catch-up").
    """
    day = now.astimezone(ET).date()
    if not is_us_weekday(day):
        return []
    moments = run_moments(day)
    return [RunSlot(run, day, moments[run]) for run in EXECUTED_RUNS]


def deadman_findings(
    accounts: Sequence[str],
    slots: Sequence[RunSlot],
    runs: Sequence[Mapping],
    now: datetime,
    grace: timedelta = DEADMAN_GRACE,
) -> list[dict]:
    """D80: pure detection of missed / failed scheduled runs.

    A slot past its moment with no row => ``missed``; a row whose status is
    not ``ok`` => ``failed``. A successful row clears the finding. ``missed``
    needs the 10-minute D65 grace so an in-flight run is never reported down;
    ``failed`` is a written row and needs no grace.
    """
    index = {
        (str(r["account_id"]), str(r["trade_day"]), str(r["run"])): r
        for r in runs
    }
    out: list[dict] = []
    for account in accounts:
        for slot in slots:
            if slot.moment > now:
                continue
            row = index.get((account, str(slot.trade_day), slot.run))
            if row is not None and str(row.get("status") or "") == "ok":
                continue
            kind = "missed" if row is None else "failed"
            if kind == "missed" and now - slot.moment < grace:
                continue
            detail = "" if row is None else str(row.get("detail") or "")
            out.append({
                "account_id": account,
                "trade_day": slot.trade_day,
                "run": slot.run,
                "kind": kind,
                "detail": detail,
            })
    return out


def deadman_breach(finding: Mapping) -> str:
    """One stable key per (account, day, run) — D83 action/alert uniqueness.

    The kind (missed/failed) lives in ``reason``, never in the key: a slot that
    is first missing and later written as a failed row must still be ONE alert,
    not two. Uniqueness is enforced by ``guardian_alerts_live`` plus the
    ``WHERE NOT EXISTS`` guard below, both in the state layer.
    """
    return f"guardian_run_down:{finding['trade_day']}:{finding['run']}"


def deadman_text(finding: Mapping) -> str:
    scope = "PORTFOLIO"
    return "\n".join([
        "GUARDIAN — scheduled run " + str(finding["kind"]),
        f"Account {finding['account_id']}",
        f"Scope {scope}",
        f"Day {finding['trade_day']}  Run {finding['run']}",
        (f"Detail {finding['detail']}" if finding.get("detail") else "Detail no grade row written"),
        "Resolve on this message to stop the 5-minute nag.",
    ])


# ---------------------------------------------------------------------------
# Thin SQL
# ---------------------------------------------------------------------------

_HEARTBEAT_SQL = "UPDATE cycle_progress SET updated_at = now() WHERE id = 1"

_SELECT_RUNS_SQL = """
SELECT account_id, trade_day, run, status, detail
FROM guardian_runs
WHERE trade_day >= %s
"""

_DUE_SQL = """
SELECT id, account_id, symbol, breach, tier, state
FROM guardian_alerts
WHERE state = 'open' AND tier IN ('danger', 'blowup')
ORDER BY id
"""

_LIVE_SQL = """
SELECT account_id, symbol, breach, state
FROM guardian_alerts
WHERE state IN ('open', 'skipped')
ORDER BY id
"""

# D83: uniqueness lives in the state layer. The NOT EXISTS clause spans the
# whole history (including terminal rows), so a resolved slot is never
# re-reported after a process restart. No in-memory dedup table.
# One alert per (account, day, run): that triple is UNIQUE in guardian_runs.
_DEADMAN_INSERT_SQL = """
INSERT INTO guardian_alerts
    (account_id, symbol, breach, tier, state, source, reason, created_at, updated_at)
SELECT %s, %s, %s, 'danger', 'open', 'deadman', %s, %s, %s
WHERE NOT EXISTS (
    SELECT 1 FROM guardian_alerts
     WHERE account_id = %s AND symbol = %s AND breach = %s
)
ON CONFLICT DO NOTHING
"""


def touch_heartbeat(conn) -> None:
    """D80/D65: only ``updated_at`` moves. ``runtime.restore()`` reads payload."""
    conn.execute(_HEARTBEAT_SQL)


def load_recent_runs(conn, earliest: date) -> list[dict]:
    found = conn.execute(_SELECT_RUNS_SQL, (earliest,)).fetchall()
    return [{
        "account_id": str(r[0]),
        "trade_day": str(r[1]),
        "run": str(r[2]),
        "status": str(r[3]),
        "detail": str(r[4] or ""),
    } for r in found]


def load_due(conn) -> list[dict]:
    found = conn.execute(_DUE_SQL).fetchall()
    return [{
        "id": int(r[0]), "account_id": str(r[1]), "symbol": str(r[2]),
        "breach": str(r[3]), "tier": str(r[4]), "state": str(r[5]),
    } for r in found]


def load_duplicates(conn) -> tuple[tuple[str, str, str, int], ...]:
    """D83 monitor: the pure GROUP BY ... HAVING count(*)>1 over live rows."""
    found = conn.execute(_LIVE_SQL).fetchall()
    rows = [{
        "account_id": str(r[0]), "symbol": str(r[1]),
        "breach": str(r[2]), "state": str(r[3]),
    } for r in found]
    return duplicate_live_keys(rows)


def insert_deadman_alert(conn, finding: Mapping, now: datetime) -> None:
    breach = deadman_breach(finding)
    conn.execute(
        _DEADMAN_INSERT_SQL,
        (
            finding["account_id"], gv.ACCOUNT_SCOPE, breach,
            f"{finding['kind']}: {finding.get('detail') or ''}".strip(), now, now,
            finding["account_id"], gv.ACCOUNT_SCOPE, breach,
        ),
    )


# ---------------------------------------------------------------------------
# One tick (connection injected so unit tests use a fake)
# ---------------------------------------------------------------------------


def nag_tick(
    conn,
    *,
    now: datetime,
    accounts: Sequence[str],
    slots: Sequence[RunSlot],
    session: bool,
    previous_signature: Sequence[tuple[str, str, str, int]] = (),
) -> TickPlan:
    """One 300s tick. Pure decisions + thin SQL; no sending, no sleeping."""
    touch_heartbeat(conn)

    deadman = 0
    if session and accounts and slots:
        earliest = min(s.trade_day for s in slots)
        findings = deadman_findings(accounts, slots, load_recent_runs(conn, earliest), now)
        for finding in findings:
            insert_deadman_alert(conn, finding, now)
            deadman += 1

    rows = due_alerts(load_due(conn))
    nags = tuple((row.id, nag_text(row)) for row in rows)

    dups = load_duplicates(conn)
    signature = tuple(dups)
    monitor = duplicate_report(dups) if dups and signature != tuple(previous_signature) else ""

    return TickPlan(nags=nags, monitor=monitor, deadman=deadman, signature=signature)


# ---------------------------------------------------------------------------
# Callback injection (D81) — bots owns the registry, orchestrator owns the SQL
# ---------------------------------------------------------------------------


def install_guardian_callbacks(database_url: str) -> None:
    """Register ``guardres`` -> ``guardian_view.resolve`` with zero cycle."""
    from athena_bots import register_callback

    def _guardres(_database_url: str, actor: str, payload: str) -> str:
        # Resolved per call so the DB seam stays patchable; the bots
        # package never imports the orchestrator (D81, zero cycle).
        from athena_store import connect

        try:
            alert_id = int(payload)
        except ValueError:
            return "Unknown alert."
        with connect(database_url) as conn:
            row = conn.execute(
                "SELECT account_id FROM guardian_alerts WHERE id = %s", (alert_id,),
            ).fetchone()
            if row is None:
                return "Unknown alert."
            account = str(row[0])
            try:
                gv.resolve(conn, account, alert_id, actor, "telegram:guardres", datetime.now(timezone.utc))
            except ValueError as exc:
                return str(exc)
            conn.commit()
        return f"Resolved alert {alert_id}."

    register_callback("guardres", _guardres)


# ---------------------------------------------------------------------------
# Daemon thread
# ---------------------------------------------------------------------------


def _nag_loop(database_url: str) -> None:
    from athena_bots import notify_guardian, notify_guardian_once
    from athena_exec.accounts import active_accounts
    from athena_store import connect

    signature: tuple[tuple[str, str, str, int], ...] = ()
    while not _STOP.is_set():
        try:
            now = datetime.now(timezone.utc)
            with connect(database_url) as conn:
                plan = nag_tick(
                    conn,
                    now=now,
                    accounts=active_accounts(),
                    slots=expected_slots(now),
                    session=is_us_session(now),
                    previous_signature=signature,
                )
                conn.commit()
            for alert_id, text in plan.nags:
                notify_guardian(alert_id, text)
            if plan.monitor:
                notify_guardian_once(plan.monitor)
            signature = plan.signature
            if plan.nags or plan.deadman or plan.monitor:
                log.info(
                    "guardian nag tick nags=%s deadman=%s monitor=%s",
                    len(plan.nags), plan.deadman, bool(plan.monitor),
                )
        except Exception:
            log.exception("guardian nag tick failed")
        if _STOP.wait(NAG_INTERVAL):  # D82: fixed 300s, independent of the cycle setting
            break


def start_guardian_nag(database_url: str) -> None:
    """Start (or restart) the dedicated nag daemon. Safe to call twice."""
    global _THREAD
    _STOP.clear()
    if _THREAD is not None and _THREAD.is_alive():
        return
    _THREAD = threading.Thread(
        target=_nag_loop, args=(database_url,),
        name="guardian-r4", daemon=True,
    )
    _THREAD.start()


def stop_guardian_nag() -> None:
    _STOP.set()
