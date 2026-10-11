"""Listed expiries, a wide strike menu, and a volatility-sized fallback pair."""

from __future__ import annotations

import math
import os
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


def wide_band() -> bool:
    """False restores the old 4-strike, 10% discovery cut."""
    flag = os.environ.get("ATHENA_SCREENER_BAND", "on").strip().lower()
    return flag not in {"0", "off", "false", "no"}


def discovery_params(strategy) -> tuple[int, int]:
    """How many strikes to fetch, and how far the menu reaches. Not the sold strike."""
    if not wide_band():
        return 4, 10
    return 8, int(strategy.chain_band_pct)


def rv_multiple(rv20_pct: Decimal, strategy) -> Decimal:
    """Assumed premium over realized vol. Not a measured implied vol."""
    if rv20_pct >= 45:
        return Decimal(str(strategy.iv_rv_multiple_hi))
    if rv20_pct >= 25:
        return Decimal(str(strategy.iv_rv_multiple_mid))
    return Decimal(str(strategy.iv_rv_multiple_lo))


def target_otm_pct(rv20_pct: Decimal, strategy, dte: int) -> tuple[Decimal, str | None]:
    """Percent out of the money for this name and expiry. The note is floor or ceiling."""
    sigma = rv_multiple(rv20_pct, strategy) * (rv20_pct / Decimal(100))
    years = Decimal(dte) / Decimal(365)
    raw = Decimal("0.5") * sigma * years.sqrt() * Decimal(100)
    floor = Decimal(strategy.otm_floor_pct)
    ceiling = Decimal(strategy.otm_ceiling_pct)
    if raw < floor:
        return floor, "floor"
    if raw > ceiling:
        return ceiling, "ceiling"
    return raw, None


def plan_otm_pct(strategy, rv20_pct: Decimal | None, dte: int) -> Decimal:
    """Distance for the single fallback pair when a chain has no asks."""
    if not wide_band():
        return Decimal(10)
    if rv20_pct is None or rv20_pct <= 0 or dte <= 0:
        return Decimal(strategy.otm_floor_pct)
    width, _note = target_otm_pct(rv20_pct, strategy, dte)
    return width


def bs_delta(spot: Decimal, strike: Decimal, dte: int, sigma: float, right: str) -> Decimal | None:
    """Black–Scholes delta with r = 0. Puts are negative. None when the inputs cannot move."""
    if dte <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return None
    years = dte / 365
    root = math.sqrt(years)
    d1 = (math.log(float(spot) / float(strike)) + 0.5 * sigma * sigma * years) / (sigma * root)
    call = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
    value = call if right == "C" else call - 1.0
    return Decimal(str(round(value, 6)))


def strike_shortlist(
    strikes: list[float],
    underlying: Decimal,
    side: str,
    width: int = 8,
    otm_pct: int = 25,
    inside: bool = True,
) -> list[Decimal]:
    """Strikes inside the discovery band, nearest the spot. inside=False is the old cut beyond that distance."""
    listed = sorted({Decimal(str(strike)) for strike in strikes})
    distance = Decimal(otm_pct) / Decimal(100)
    if side == "P":
        if inside:
            band = [strike for strike in listed if underlying * (Decimal(1) - distance) <= strike <= underlying]
        else:
            band = [strike for strike in listed if strike <= underlying * (Decimal(1) - distance)]
        return band[-width:]
    if inside:
        band = [strike for strike in listed if underlying <= strike <= underlying * (Decimal(1) + distance)]
    else:
        band = [strike for strike in listed if strike >= underlying * (Decimal(1) + distance)]
    return band[:width]


def atm_strikes(strikes: list[float], underlying: Decimal) -> tuple[Decimal, Decimal] | None:
    """Put at or below spot, call at or above it. Each is the closest listed strike."""
    if underlying <= 0:
        return None
    listed = sorted({Decimal(str(strike)) for strike in strikes if Decimal(str(strike)) > 0})
    puts = [strike for strike in listed if strike <= underlying]
    calls = [strike for strike in listed if strike >= underlying]
    if not puts or not calls:
        return None
    return puts[-1], calls[0]


def choose_strikes(strikes: list[float], underlying: Decimal, otm_pct: Decimal | int = 10) -> tuple[Decimal, Decimal] | None:
    """One put and one call at a distance. The screener, not this pair, picks the sold strike."""
    listed = sorted({Decimal(str(strike)) for strike in strikes})
    distance = Decimal(str(otm_pct)) / Decimal(100)
    put_target = underlying * (Decimal(1) - distance)
    call_target = underlying * (Decimal(1) + distance)
    puts = [strike for strike in listed if strike <= put_target]
    calls = [strike for strike in listed if strike >= call_target]
    if not puts or not calls:
        return None
    return puts[-1], calls[0]
