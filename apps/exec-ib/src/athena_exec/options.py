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


def choose_strikes(strikes: list[float], underlying: Decimal) -> tuple[Decimal, Decimal] | None:
    """Put about 10% below the price, call about 10% above."""
    listed = sorted({Decimal(str(strike)) for strike in strikes})
    put_target = underlying * Decimal("0.90")
    call_target = underlying * Decimal("1.10")
    puts = [strike for strike in listed if strike <= put_target]
    calls = [strike for strike in listed if strike >= call_target]
    if not puts or not calls:
        return None
    return puts[-1], calls[0]
