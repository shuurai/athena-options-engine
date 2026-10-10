"""Paper replay. One stored trading day per cycle. No simulated order goes to IB."""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import psycopg

from athena_exec.accounts import trading_mode

log = logging.getLogger("athena.sim")

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_LABELS = {
    "downloading": "Downloading",
    "active": "Running",
    "halted": "Halted",
    "done": "Done",
    "failed": "Failed",
    "stopped": "Stopped",
}


def view(conn: psycopg.Connection, account: str) -> dict | None:
    row = conn.execute(
        """
        SELECT status, start_date, end_date, cursor_date, symbols
        FROM sim_runs
        WHERE account_id = %s AND status IN ('downloading', 'active', 'halted')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (account,),
    ).fetchone()
    if row is None:
        return None
    status, start, end, cursor, symbols = row
    return {
        "status": status,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "cursor": "" if cursor is None else cursor.isoformat(),
        "symbols": symbols,
    }


def busy(conn: psycopg.Connection, account: str) -> bool:
    return view(conn, account) is not None


def symbols_for(conn: psycopg.Connection, account: str) -> list[str]:
    row = conn.execute(
        """
        SELECT symbols FROM sim_runs
        WHERE account_id = %s AND status = 'active'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (account,),
    ).fetchone()
    if row is None:
        return []
    return [part for part in row[0].split(",") if part]


def cycle_gate(conn: psycopg.Connection, account: str) -> tuple[str, date | None]:
    """live, wait (download), play (one day), or done (this cycle stays off the live book)."""
    row = conn.execute(
        """
        SELECT id, start_date, end_date, cursor_date, status
        FROM sim_runs
        WHERE account_id = %s AND status IN ('downloading', 'active', 'halted')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (account,),
    ).fetchone()
    if row is None:
        return "live", None
    run_id, start, end, cursor, status = row
    if status in {"downloading", "halted"}:
        return "wait", None
    found = conn.execute(
        """
        SELECT MIN(ts::date) FROM sim_bars
        WHERE run_id = %s AND ts::date >= %s AND ts::date <= %s
          AND (%s::date IS NULL OR ts::date > %s::date)
        """,
        (run_id, start, end, cursor, cursor),
    ).fetchone()
    day = None if found is None else found[0]
    if day is None:
        conn.execute("UPDATE sim_runs SET status = 'done' WHERE id = %s", (run_id,))
        conn.commit()
        log.info("simulation finished run=%s", run_id)
        return "done", None
    conn.execute(
        "UPDATE sim_runs SET cursor_date = %s WHERE id = %s",
        (day, run_id),
    )
    conn.commit()
    log.info("simulation day=%s run=%s", day, run_id)
    return "play", day


def stop(conn: psycopg.Connection, account: str) -> str:
    cur = conn.execute(
        """
        UPDATE sim_runs SET status = 'stopped'
        WHERE account_id = %s AND status IN ('downloading', 'active')
        """,
        (account,),
    )
    conn.commit()
    if cur.rowcount:
        return "Simulation stopped. The next cycle is live."
    return "No simulation is running."


def halt(conn: psycopg.Connection, account: str) -> str:
    _require_paper()
    cur = conn.execute(
        """
        UPDATE sim_runs SET status = 'halted'
        WHERE account_id = %s AND status IN ('downloading', 'active')
        """,
        (account,),
    )
    conn.commit()
    if cur.rowcount:
        log.info("simulation halted account=%s", account)
        return "Simulation halted."
    return "No simulation is running."


def resume(conn: psycopg.Connection, account: str) -> str:
    _require_paper()
    row = _open_run(conn, account)
    if row is None or row[1] != "halted":
        raise ValueError("No simulation is halted.")
    run_id, _status, _start, _end, _cursor, symbols = row
    names = [part for part in symbols.split(",") if part]
    if not _bars_ready(conn, run_id, names):
        raise ValueError("Download stopped early. Reset and start again.")
    conn.execute("UPDATE sim_runs SET status = 'active' WHERE id = %s", (run_id,))
    conn.commit()
    log.info("simulation resumed run=%s", run_id)
    return "Simulation resumed."


def reset(conn: psycopg.Connection, account: str) -> str:
    """Drop this account's replays and simulated book. Real rows stay."""
    _require_paper()
    if not account:
        raise ValueError("No account is configured.")
    drafts = "SELECT id FROM drafts WHERE account_id = %s AND simulated"
    conn.execute(
        f"DELETE FROM control_links WHERE draft_id IN ({drafts})",
        (account,),
    )
    conn.execute(
        f"DELETE FROM approvals WHERE draft_id IN ({drafts})",
        (account,),
    )
    conn.execute(
        f"""
        DELETE FROM orders
        WHERE account_id = %s AND (simulated OR draft_id IN ({drafts}))
        """,
        (account, account),
    )
    conn.execute(
        f"""
        DELETE FROM fills
        WHERE account_id = %s AND (simulated OR draft_id IN ({drafts}))
        """,
        (account, account),
    )
    conn.execute(
        "DELETE FROM drafts WHERE account_id = %s AND simulated",
        (account,),
    )
    conn.execute(
        "DELETE FROM positions WHERE account_id = %s AND simulated",
        (account,),
    )
    conn.execute(
        "DELETE FROM ai_calls WHERE account_id = %s AND simulated",
        (account,),
    )
    conn.execute(
        "DELETE FROM decision_trails WHERE account_id = %s AND simulated",
        (account,),
    )
    conn.execute("DELETE FROM sim_runs WHERE account_id = %s", (account,))
    conn.commit()
    log.info("simulation reset account=%s", account)
    return "Simulation cleared. The next cycle is live."


def progress(conn: psycopg.Connection, account: str, series: bool = False) -> dict:
    """The run on the Simulation page. In-progress wins over an older finished run."""
    empty = {
        "run_id": "",
        "status": "",
        "label": "None",
        "start": "",
        "end": "",
        "cursor": "",
        "start_label": "",
        "end_label": "",
        "cursor_label": "",
        "day": 0,
        "total": 0,
        "left": 0,
        "bars": 0,
        "resume": False,
        "note": "",
        "symbols": [],
        "dates": [],
        "series": {},
    }
    if not account:
        return empty
    row = _open_run(conn, account)
    if row is None:
        row = conn.execute(
            """
            SELECT id, status, start_date, end_date, cursor_date, symbols
            FROM sim_runs
            WHERE account_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (account,),
        ).fetchone()
    if row is None:
        return empty
    run_id, status, start, end, cursor, symbols = row
    names = [part for part in symbols.split(",") if part]
    days = [
        item[0]
        for item in conn.execute(
            """
            SELECT DISTINCT ts::date FROM sim_bars
            WHERE run_id = %s AND ts::date >= %s AND ts::date <= %s
            ORDER BY 1
            """,
            (run_id, start, end),
        ).fetchall()
    ]
    if cursor is None:
        day_n, left = 0, len(days)
    else:
        day_n = sum(1 for item in days if item <= cursor)
        left = sum(1 for item in days if item > cursor)
    bar_count = conn.execute(
        "SELECT count(*) FROM sim_bars WHERE run_id = %s",
        (run_id,),
    ).fetchone()[0]
    ready = status == "halted" and _bars_ready(conn, run_id, names)
    payload = {
        "run_id": run_id,
        "status": status,
        "label": _LABELS.get(status, status),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "cursor": "" if cursor is None else cursor.isoformat(),
        "start_label": _written(start),
        "end_label": _written(end),
        "cursor_label": "" if cursor is None else _written(cursor),
        "day": day_n,
        "total": len(days),
        "left": left,
        "bars": int(bar_count),
        "resume": ready,
        "note": "Download stopped early. Reset and start again." if status == "halted" and not ready else "",
        "symbols": names,
        "dates": [item.isoformat() for item in days],
        "series": {},
    }
    if series:
        payload["series"] = _aligned(conn, run_id, start, end, names, days)
    return payload


def _open_run(conn: psycopg.Connection, account: str):
    return conn.execute(
        """
        SELECT id, status, start_date, end_date, cursor_date, symbols
        FROM sim_runs
        WHERE account_id = %s AND status IN ('downloading', 'active', 'halted')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (account,),
    ).fetchone()


def _bars_ready(conn: psycopg.Connection, run_id: str, names: list[str]) -> bool:
    if not names:
        return False
    found = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT symbol FROM sim_bars WHERE run_id = %s",
            (run_id,),
        ).fetchall()
    }
    return all(name in found for name in names)


def _aligned(conn, run_id: str, start: date, end: date, names: list[str], days: list[date]) -> dict:
    stored: dict[str, dict] = {name: {} for name in names}
    rows = conn.execute(
        """
        SELECT symbol, ts::date, open, high, low, close, volume
        FROM sim_bars
        WHERE run_id = %s AND ts::date >= %s AND ts::date <= %s
        ORDER BY ts
        """,
        (run_id, start, end),
    ).fetchall()
    for symbol, day, open_, high, low, close, volume in rows:
        if symbol not in stored:
            continue
        stored[symbol][day] = {
            "o": float(open_),
            "h": float(high),
            "l": float(low),
            "c": float(close),
            "v": float(volume or 0),
        }
    return {
        name: [stored[name].get(day) for day in days]
        for name in names
    }


def _written(day: date) -> str:
    return f"{day.day} {_MONTHS[day.month - 1]} {day.year}"


def _require_paper() -> None:
    if trading_mode() != "paper":
        raise ValueError("Simulation runs in paper mode.")


def prepare(
    conn: psycopg.Connection,
    account: str,
    start: date,
    months: int,
    symbols: list[str],
) -> tuple[str, date, str]:
    """Insert a downloading run. Returns run id, end date, and the IB duration."""
    if trading_mode() != "paper":
        raise ValueError("Simulation runs in paper mode.")
    if months not in {1, 2, 3}:
        raise ValueError("Duration is 1, 2, or 3 months.")
    if start > date.today():
        raise ValueError("Start date is in the future.")
    names: list[str] = []
    for symbol in symbols:
        text = symbol.strip().upper()
        if text and text not in names:
            names.append(text)
    if not names:
        raise ValueError("Add at least one simulation symbol.")
    if busy(conn, account):
        raise ValueError("A simulation is already running.")
    end = _add_months(start, months)
    if end > date.today():
        end = date.today()
    if start > end:
        raise ValueError("Start date is after the window.")
    run_id = "sim_" + secrets.token_hex(4)
    conn.execute(
        """
        INSERT INTO sim_runs (id, account_id, symbols, start_date, end_date, status)
        VALUES (%s, %s, %s, %s, %s, 'downloading')
        """,
        (run_id, account, ",".join(names), start, end),
    )
    conn.commit()
    return run_id, end, duration_for(start)


def duration_for(start: date) -> str:
    """Window plus about 400 calendar days, so the channel exists on day one."""
    span = (date.today() - (start - timedelta(days=400))).days + 2
    years = min(5, max(1, (max(span, 1) + 364) // 365))
    return f"{years} Y"


def store_bars(conn: psycopg.Connection, run_id: str, symbol: str, bars: list[tuple]) -> int:
    written = 0
    for raw_date, open_, high, low, close, volume in bars:
        if close is None or Decimal(close) <= 0:
            continue
        conn.execute(
            """
            INSERT INTO sim_bars (run_id, symbol, ts, open, high, low, close, volume)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                run_id,
                symbol,
                _noon(raw_date),
                open_,
                high,
                low,
                close,
                volume or 0,
            ),
        )
        written += 1
    conn.commit()
    return written


def still_downloading(conn: psycopg.Connection, run_id: str) -> bool:
    row = conn.execute("SELECT status FROM sim_runs WHERE id = %s", (run_id,)).fetchone()
    return row is not None and row[0] == "downloading"


def activate(conn: psycopg.Connection, run_id: str) -> None:
    if not still_downloading(conn, run_id):
        return
    count = conn.execute(
        "SELECT count(*) FROM sim_bars WHERE run_id = %s",
        (run_id,),
    ).fetchone()
    if not count or count[0] == 0:
        conn.execute("UPDATE sim_runs SET status = 'failed' WHERE id = %s", (run_id,))
        conn.commit()
        raise RuntimeError("IB returned no daily bars.")
    conn.execute("UPDATE sim_runs SET status = 'active' WHERE id = %s", (run_id,))
    conn.commit()


def fail(conn: psycopg.Connection, run_id: str) -> None:
    conn.execute(
        "UPDATE sim_runs SET status = 'failed' WHERE id = %s AND status = 'downloading'",
        (run_id,),
    )
    conn.commit()


def latest_close(conn: psycopg.Connection, run_id: str, symbol: str):
    row = conn.execute(
        """
        SELECT close FROM sim_bars
        WHERE run_id = %s AND symbol = %s
        ORDER BY ts DESC
        LIMIT 1
        """,
        (run_id, symbol),
    ).fetchone()
    if row is None:
        return None
    return Decimal(row[0])


def delete_simulated_positions(conn: psycopg.Connection, account: str) -> int:
    cur = conn.execute(
        "DELETE FROM positions WHERE account_id = %s AND simulated = true",
        (account,),
    )
    conn.commit()
    return cur.rowcount


def real_positions(conn: psycopg.Connection, account: str) -> list[tuple]:
    return list(
        conn.execute(
            """
            SELECT DISTINCT ON (symbol, sec_type, expiry, contract_right, strike)
                   symbol, quantity, sec_type, expiry, contract_right, strike,
                   market_price, multiplier
            FROM positions
            WHERE account_id = %s AND simulated = false
            ORDER BY symbol, sec_type, expiry, contract_right, strike, snapshot_at DESC, id DESC
            """,
            (account,),
        ).fetchall()
    )


def flush(conn: psycopg.Connection, account: str, phrase: str) -> str:
    if phrase != "I CONFIRM":
        raise ValueError("Type I CONFIRM.")
    if not account:
        raise ValueError("No account is configured.")
    conn.execute(
        """
        DELETE FROM control_links
        WHERE draft_id IN (SELECT id FROM drafts WHERE account_id = %s)
        """,
        (account,),
    )
    for table in (
        "approvals",
        "orders",
        "fills",
        "drafts",
        "positions",
        "decision_trails",
        "ai_calls",
        "nav_marks",
        "account_marks",
        "sim_runs",
    ):
        conn.execute(f"DELETE FROM {table} WHERE account_id = %s", (account,))
    conn.commit()
    log.info("flush account=%s", account)
    return "This account's book, drafts, and audit were cleared."


def _add_months(start: date, months: int) -> date:
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, _month_length(year, month))
    return date(year, month, day)


def _month_length(year: int, month: int) -> int:
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    return (nxt - date(year, month, 1)).days


def _noon(value) -> datetime:
    if isinstance(value, datetime):
        value = value.date()
    return datetime(value.year, value.month, value.day, 12, tzinfo=UTC)
