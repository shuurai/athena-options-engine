"""Process facts the console can read. The loop updates them."""

from __future__ import annotations

import json
import logging
import queue
import socket
import threading
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

started_at = datetime.now(timezone.utc)
cycle = 0
telegram_polling = False
link_code = ""
link_expires_at: datetime | None = None

STEPS = ("Session", "Bars", "News", "DSH", "IV", "Phases", "Drafts", "Backup")
_LOCK = threading.Lock()
_loop_seconds = 300
_state = "waiting"
_next_at: datetime | None = None
_step = 0
_lines = [""] * len(STEPS)
_marks = ["pending"] * len(STEPS)
_activity: deque[str] = deque(maxlen=12)
_saver: Callable[[dict], None] | None = None
_loader: Callable[[], dict | None] | None = None
log = logging.getLogger("athena")


def note_cycle(count: int) -> None:
    global cycle
    with _LOCK:
        cycle = count
    persist()


def note_loop(seconds: int) -> None:
    global _loop_seconds
    with _LOCK:
        _loop_seconds = max(1, seconds)
    persist()


def begin(count: int) -> None:
    """A cycle has started. The first step is Session."""
    global cycle, _state, _step, _next_at
    with _LOCK:
        cycle = count
        _state = "running"
        _step = 0
        _next_at = None
        for index in range(len(STEPS)):
            _lines[index] = ""
            _marks[index] = "pending"
        _marks[0] = "running"
        _activity.clear()
    persist()


def begin_running(count: int) -> None:
    begin(count)


def mark(index: int, status: str, detail: str = "") -> None:
    """Record one step as running, done, or skipped, with its real line."""
    global _step
    if index < 0 or index >= len(STEPS):
        return
    with _LOCK:
        if status == "running":
            for earlier in range(index):
                if _marks[earlier] == "running":
                    _marks[earlier] = "done"
        _marks[index] = status
        if detail:
            _lines[index] = detail
        if index > _step:
            _step = index


def note_step(index: int, skipped: bool = False) -> None:
    mark(index, "skipped" if skipped else "running")


def finish(index: int, detail: str) -> None:
    mark(index, "done", detail)


def wait(seconds: int | None = None) -> None:
    """The cycle finished. Finished lines stay. The bar fills until the next one."""
    global _state, _next_at
    with _LOCK:
        for index, status in enumerate(_marks):
            if status == "running":
                _marks[index] = "done"
        _state = "waiting"
        span = _loop_seconds if seconds is None else max(0, seconds)
        _next_at = datetime.now(timezone.utc) + timedelta(seconds=span)
    persist()


def begin_waiting(seconds: int | None = None) -> None:
    wait(seconds)


def snapshot() -> dict:
    with _LOCK:
        return {
            "cycle": cycle,
            "state": _state,
            "loop_seconds": _loop_seconds,
            "step": _step,
            "next_at": None if _next_at is None else _next_at.isoformat(),
            "marks": list(_marks),
            "lines": list(_lines),
        }


def apply_snapshot(data: dict) -> None:
    """Restore cycle strip state from a cached payload."""
    global cycle, _state, _loop_seconds, _step, _next_at
    marks = data.get("marks") or []
    lines = data.get("lines") or []
    next_raw = data.get("next_at")
    nxt = None
    if next_raw:
        nxt = datetime.fromisoformat(str(next_raw))
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=timezone.utc)
    with _LOCK:
        cycle = int(data.get("cycle") or 0)
        _state = str(data.get("state") or "waiting")
        _loop_seconds = max(1, int(data.get("loop_seconds") or 300))
        _step = max(0, min(int(data.get("step") or 0), len(STEPS) - 1))
        _next_at = nxt
        for index in range(len(STEPS)):
            _marks[index] = marks[index] if index < len(marks) else "pending"
            _lines[index] = lines[index] if index < len(lines) else ""
        _activity.clear()


def bind_progress(
    saver: Callable[[dict], None] | None,
    loader: Callable[[], dict | None] | None,
) -> None:
    """Hook used by tests. Production binds Postgres through bind_database."""
    global _saver, _loader
    _saver = saver
    _loader = loader


def bind_database(database_url: str) -> None:
    """Persist cycle progress in Postgres so a restart can resume."""

    def save(payload: dict) -> None:
        from athena_store import connect

        with connect(database_url) as conn:
            conn.execute(
                """
                INSERT INTO cycle_progress (id, payload, updated_at)
                VALUES (1, %s::jsonb, now())
                ON CONFLICT (id) DO UPDATE
                SET payload = EXCLUDED.payload, updated_at = now()
                """,
                (json.dumps(payload),),
            )
            conn.commit()

    def load() -> dict | None:
        from athena_store import connect

        with connect(database_url) as conn:
            row = conn.execute("SELECT payload FROM cycle_progress WHERE id = 1").fetchone()
        if row is None or row[0] is None:
            return None
        payload = row[0]
        return payload if isinstance(payload, dict) else json.loads(payload)

    bind_progress(save, load)


def persist() -> None:
    if _saver is None:
        return
    try:
        _saver(snapshot())
    except Exception:
        log.exception("cycle progress save failed")


def restore() -> dict | None:
    """Load the last cycle strip into memory. None when nothing was cached."""
    if _loader is None:
        return None
    try:
        data = _loader()
    except Exception:
        log.exception("cycle progress load failed")
        return None
    if not data:
        return None
    apply_snapshot(data)
    return snapshot()


def wait_remaining() -> int:
    """Seconds left in the wait bar. Zero when running or the deadline passed."""
    now = datetime.now(timezone.utc)
    with _LOCK:
        if _state != "waiting" or _next_at is None:
            return 0
        return max(0, int((_next_at - now).total_seconds()))


def cycle_view() -> dict:
    now = datetime.now(timezone.utc)
    with _LOCK:
        state = _state
        step = _step
        skipped = _marks[step] == "skipped"
        loop = _loop_seconds
        nxt = _next_at
        count = cycle
        marks = list(_marks)
        lines = list(_lines)
        activity = list(_activity)
    if state == "waiting" and nxt is not None:
        remaining = max(0, int((nxt - now).total_seconds()))
        progress = min(1.0, (loop - remaining) / loop) if loop else 1.0
        label = ""
        visible = len(STEPS)
    else:
        remaining = 0
        done = sum(1 for status in marks[: step + 1] if status in {"done", "skipped"})
        progress = min(1.0, done / len(STEPS))
        label = STEPS[min(step, len(STEPS) - 1)]
        visible = step + 1
    roll = [
        {"name": STEPS[index], "status": marks[index], "detail": lines[index]}
        for index in range(visible)
        if marks[index] != "pending"
    ]
    delta = now - started_at
    hours, remainder = divmod(delta.seconds, 3600)
    minutes = remainder // 60
    uptime = f"{delta.days}D {hours:02d}H" if delta.days else f"{hours}H {minutes:02d}M"
    return {
        "state": state,
        "cycle": count,
        "loop": loop,
        "remaining": remaining,
        "progress": progress,
        "step": label,
        "index": step + 1,
        "skipped": skipped,
        "roll": roll,
        "activity": activity if state == "running" else [],
        "steps": len(STEPS),
        "uptime": uptime,
        "host": socket.gethostname(),
        "started": started_at.astimezone(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M"),
    }


def note_activity(text: str) -> None:
    """Keep one short line the cockpit can show while a step is still running."""
    clean = " ".join(text.split())
    if not clean:
        return
    if len(clean) > 160:
        clean = clean[:157] + "..."
    stamp = datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%H:%M:%S")
    with _LOCK:
        _activity.append(f"{stamp}  {clean}")


class _ActivityLog(logging.Handler):
    """Copy Athena log lines into the cycle strip. Broker position dumps stay out."""

    def emit(self, record: logging.LogRecord) -> None:
        name = record.name or ""
        if name != "athena" and not name.startswith("athena."):
            return
        if record.levelno < logging.INFO:
            return
        try:
            note_activity(record.getMessage())
        except Exception:
            return


def install_activity_log() -> None:
    root = logging.getLogger()
    if any(type(handler) is _ActivityLog for handler in root.handlers):
        return
    root.addHandler(_ActivityLog())


_TRAIL_LOCK = threading.Lock()
_TRAIL: deque[dict] = deque(maxlen=120)
_TRAIL_LISTENERS: list[queue.Queue] = []
_HKT = ZoneInfo("Asia/Hong_Kong")


class _TrailLog(logging.Handler):
    """Keep the newest terminal lines for the cockpit column."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.INFO:
            return
        try:
            message = record.getMessage().replace("\n", " ")[:300]
        except Exception:
            return
        line = {
            "time": datetime.now(_HKT).strftime("%H:%M:%S"),
            "level": record.levelname,
            "name": record.name or "",
            "message": message,
        }
        with _TRAIL_LOCK:
            _TRAIL.append(line)
            listeners = list(_TRAIL_LISTENERS)
        for listener in listeners:
            try:
                listener.put_nowait(line)
            except queue.Full:
                continue


def install_log_trail() -> None:
    root = logging.getLogger()
    if any(type(handler) is _TrailLog for handler in root.handlers):
        return
    handler = _TrailLog()
    handler.setLevel(logging.INFO)
    root.addHandler(handler)


def trail_recent() -> list[dict]:
    with _TRAIL_LOCK:
        return list(_TRAIL)


def subscribe_trail() -> queue.Queue:
    listener: queue.Queue = queue.Queue(maxsize=200)
    with _TRAIL_LOCK:
        _TRAIL_LISTENERS.append(listener)
    return listener


def unsubscribe_trail(listener: queue.Queue) -> None:
    with _TRAIL_LOCK:
        if listener in _TRAIL_LISTENERS:
            _TRAIL_LISTENERS.remove(listener)


def note_telegram(polling: bool) -> None:
    global telegram_polling
    telegram_polling = polling
