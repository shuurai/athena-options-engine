"""Pick one listed strangle. About 10% out of the money, near 35 days."""

from __future__ import annotations

from datetime import date
from decimal import Decimal


def choose_expiry(
    expiries: list[str],
    today: date,
    min_dte: int = 21,
    target_dte: int = 35,
) -> date | None:
    """Listed expiry nearest 35 days, and at least 21 days out."""
    choices: list[tuple[int, date]] = []
    for raw in expiries:
        if len(raw) < 8 or not raw[:8].isdigit():
            continue
        expiry = date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
        dte = (expiry - today).days
        if dte >= min_dte:
            choices.append((abs(dte - target_dte), expiry))
    if not choices:
        return None
    return min(choices)[1]


def expiry_band(
    expiries: list[str],
    today: date,
    min_dte: int = 21,
    max_dte: int = 60,
    target_dte: int = 35,
    limit: int = 3,
) -> list[date]:
    """A few listed expiries inside the open band, nearest the target first."""
    ranked: list[tuple[int, date]] = []
    for raw in expiries:
        if len(raw) < 8 or not raw[:8].isdigit():
            continue
        expiry = date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
        dte = (expiry - today).days
        if min_dte <= dte <= max_dte:
            ranked.append((abs(dte - target_dte), expiry))
    ranked.sort()
    return [expiry for _, expiry in ranked[:limit]]


def strike_shortlist(
    strikes: list[float],
    underlying: Decimal,
    side: str,
    width: int = 4,
    otm_pct: int = 10,
) -> list[Decimal]:
    """Puts at or below the distance. Calls at or above it."""
    listed = sorted({Decimal(str(strike)) for strike in strikes})
    distance = Decimal(otm_pct) / Decimal(100)
    if side == "P":
        band = [strike for strike in listed if strike <= underlying * (Decimal(1) - distance)]
        return band[-width:]
    band = [strike for strike in listed if strike >= underlying * (Decimal(1) + distance)]
    return band[:width]


def choose_strikes(strikes: list[float], underlying: Decimal, otm_pct: int = 10) -> tuple[Decimal, Decimal] | None:
    """Put and call at the configured distance from the price."""
    listed = sorted({Decimal(str(strike)) for strike in strikes})
    distance = Decimal(otm_pct) / Decimal(100)
    put_target = underlying * (Decimal(1) - distance)
    call_target = underlying * (Decimal(1) + distance)
    puts = [strike for strike in listed if strike <= put_target]
    calls = [strike for strike in listed if strike >= call_target]
    if not puts or not calls:
        return None
    return puts[-1], calls[0]
