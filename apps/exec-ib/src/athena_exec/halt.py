"""Halt new drafts. Resume is an allowlisted command."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

HKT = ZoneInfo("Asia/Hong_Kong")
REJECT_STREAK = 3


def active_reason(conn, account_id: str) -> str | None:
    row = conn.execute(
        """
        SELECT reason FROM halts
        WHERE active AND account_id = %s
        ORDER BY id DESC
        LIMIT 1
        """,
        (account_id,),
    ).fetchone()
    return None if row is None else row[0]


def halt(conn, account_id: str, reason: str) -> None:
    conn.execute(
        """
        INSERT INTO halts (account_id, reason, active)
        SELECT %s, %s, true
        WHERE NOT EXISTS (
            SELECT 1 FROM halts WHERE active AND account_id = %s
        )
        """,
        (account_id, reason, account_id),
    )
    conn.commit()


def resume(conn, account_id: str) -> bool:
    cur = conn.execute(
        """
        UPDATE halts SET active = false, cleared_at = now()
        WHERE active AND account_id = %s
        """,
        (account_id,),
    )
    conn.commit()
    return cur.rowcount > 0


def note_reject_streak(conn, account_id: str) -> str | None:
    rows = conn.execute(
        """
        SELECT decision FROM approvals
        WHERE account_id = %s
        ORDER BY id DESC
        LIMIT %s
        """,
        (account_id, REJECT_STREAK),
    ).fetchall()
    decisions = [row[0] for row in rows]
    if len(decisions) < REJECT_STREAK or any(item != "reject" for item in decisions):
        return None
    reason = "reject streak"
    halt(conn, account_id, reason)
    return reason


def note_nav(conn, account_id: str, nav: Decimal, halt_pct: int, now: datetime) -> str | None:
    """Halt when today's drop from the first mark reaches the settings percent."""
    if not account_id or nav <= 0 or halt_pct <= 0:
        return None
    day = now.astimezone(HKT).date()
    row = conn.execute(
        "SELECT start_nav FROM nav_marks WHERE account_id = %s AND day = %s",
        (account_id, day),
    ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO nav_marks (account_id, day, start_nav, last_nav)
            VALUES (%s, %s, %s, %s)
            """,
            (account_id, day, nav, nav),
        )
        conn.commit()
        return None
    start = Decimal(row[0])
    conn.execute(
        """
        UPDATE nav_marks SET last_nav = %s
        WHERE account_id = %s AND day = %s
        """,
        (nav, account_id, day),
    )
    conn.commit()
    if start <= 0:
        return None
    drop = (start - nav) / start * 100
    if drop < halt_pct:
        return None
    reason = "daily loss halt"
    halt(conn, account_id, reason)
    return reason
