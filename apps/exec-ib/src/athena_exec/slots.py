"""Athena vs Mobile windows in Asia/Hong_Kong."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

from athena_settings.model import IBSlots

HKT = ZoneInfo("Asia/Hong_Kong")


def observe_allowed(now: datetime, slots: IBSlots, mode: str) -> bool:
    """Watch window. always is 07:00 through midnight on a Hong Kong weekday."""
    local = now.astimezone(HKT)
    if mode == "athena":
        return slot_state(now, slots) == "athena"
    if mode == "athena+mobile":
        return slot_state(now, slots) in {"athena", "mobile"}
    if local.weekday() >= 5:
        return False
    clock = local.timetz().replace(tzinfo=None)
    return clock >= time(7, 0)


def draft_allowed(now: datetime, slots: IBSlots, mode: str) -> bool:
    """When a ticket may be written. athena keeps the existing session."""
    if mode == "always":
        return observe_allowed(now, slots, "always")
    return slot_state(now, slots) == "athena"


def slot_state(now: datetime, slots: IBSlots) -> str:
    """Return mobile, athena, or idle. Mobile wins when both match.

    A window that ends earlier than it starts crosses midnight. Its morning
    belongs to the previous evening, so Friday's US session stays open until
    Saturday 04:00 and Monday morning stays closed.
    """
    local = now.astimezone(HKT)
    if _inside(local, slots.mobile_slots):
        return "mobile"
    if _inside(local, slots.athena_slots):
        return "athena"
    return "idle"


def parse_windows(spec: str) -> list[tuple[time, time]]:
    """Start and end clocks. An end before the start is an overnight window."""
    text = spec.replace("HKT", " ").strip()
    found = []
    for part in text.split(","):
        piece = part.strip()
        if "-" not in piece:
            continue
        start_s, end_s = piece.split("-", 1)
        try:
            start = time.fromisoformat(start_s.strip())
            end = time.fromisoformat(end_s.strip())
        except ValueError:
            continue
        if start != end:
            found.append((start, end))
    return found


def covers(weekday: int, clock: time, start: time, end: time) -> bool:
    """True when this Hong Kong weekday and clock sit inside the window."""
    if start < end:
        return weekday < 5 and start <= clock < end
    if clock >= start:
        return weekday < 5
    if clock < end:
        return (weekday - 1) % 7 < 5
    return False


def _inside(now: datetime, spec: str) -> bool:
    clock = now.timetz().replace(tzinfo=None)
    weekday = now.weekday()
    return any(covers(weekday, clock, start, end) for start, end in parse_windows(spec))
