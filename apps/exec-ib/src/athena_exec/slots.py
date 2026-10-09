"""Athena vs Mobile windows in Asia/Hong_Kong."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

from athena_settings.model import IBSlots

HKT = ZoneInfo("Asia/Hong_Kong")


def slot_state(now: datetime, slots: IBSlots) -> str:
    """Return mobile, athena, or idle. Mobile wins when both match.

    The windows run Monday through Friday in Hong Kong. Saturday and Sunday stay idle.
    """
    local = now.astimezone(HKT)
    if local.weekday() >= 5:
        return "idle"
    if _inside(local, slots.mobile_slots):
        return "mobile"
    if _inside(local, slots.athena_slots):
        return "athena"
    return "idle"


def _inside(now: datetime, spec: str) -> bool:
    text = spec.replace("HKT", " ").strip()
    clock = now.timetz().replace(tzinfo=None)
    for part in text.split(","):
        piece = part.strip()
        if not piece or "-" not in piece:
            continue
        start_s, end_s = piece.split("-", 1)
        start = time.fromisoformat(start_s.strip())
        end = time.fromisoformat(end_s.strip())
        if start <= clock < end:
            return True
    return False
