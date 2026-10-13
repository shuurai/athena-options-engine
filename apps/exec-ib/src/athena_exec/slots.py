"""Athena vs Mobile windows in Asia/Hong_Kong."""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from athena_settings.model import IBSlots

HKT = ZoneInfo("Asia/Hong_Kong")

# The US regular session leaves daylight time on Sunday 1 Nov 2026.
# Friday 30 Oct still ends Saturday 31 Oct at 04:00. Monday 2 Nov is the first standard session.
US_STANDARD_FROM = date(2026, 11, 2)
US_DAYLIGHT = (time(21, 30), time(4, 0))
US_STANDARD = (time(22, 30), time(5, 0))
HK_CASH = ((time(9, 30), time(12, 0)), (time(13, 0), time(16, 0)))


def symbol_market(symbol: str, explicit: str = "") -> str:
    """us, hk, or ca. A blank value treats a numeric code as Hong Kong and the rest as US."""
    kind = (explicit or "").strip().lower()
    if kind in {"us", "hk", "ca"}:
        return kind
    if str(symbol).strip().isdigit():
        return "hk"
    return "us"


def cash_open(symbol: str, now: datetime, market: str = "") -> bool:
    """True during that name's own cash session.

    Hong Kong is the weekday morning and afternoon sessions.
    US and Canada use the US cash leg, including the Friday overnight tail.
    """
    local = now.astimezone(HKT)
    clock = local.timetz().replace(tzinfo=None)
    weekday = local.weekday()
    if symbol_market(symbol, market) == "hk":
        return any(covers(weekday, clock, start, end) for start, end in HK_CASH)
    window = US_STANDARD if local.date() >= US_STANDARD_FROM else US_DAYLIGHT
    return covers(weekday, clock, window[0], window[1])


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


def effective_windows(spec: str, day: date) -> list[tuple[time, time]]:
    """Stored windows, with the daylight US leg moved on the dated Monday.

    Only the exact 21:30-04:00 pair moves. Any other window stays as stored.
    """
    windows = parse_windows(spec)
    if day < US_STANDARD_FROM:
        return windows
    return [US_STANDARD if window == US_DAYLIGHT else window for window in windows]


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
    return any(covers(weekday, clock, start, end) for start, end in effective_windows(spec, now.date()))
