"""Process facts the console can read. The loop updates them."""

from __future__ import annotations

import logging
import socket
import threading
from collections import deque
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


def note_cycle(count: int) -> None:
    global cycle
    cycle = count


def note_loop(seconds: int) -> None:
    global _loop_seconds
    with _LOCK:
        _loop_seconds = max(1, seconds)


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


def begin_waiting(seconds: int | None = None) -> None:
    wait(seconds)


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
        progress = min(1.0, (loop - remaining) / loop)
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


def note_telegram(polling: bool) -> None:
    global telegram_polling
    telegram_polling = polling
