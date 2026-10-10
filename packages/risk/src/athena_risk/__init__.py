"""Draft size checks. Does not send orders."""

from decimal import Decimal

from athena_settings.model import RiskLimits

_POSITION_SLACK = Decimal("1.1")


def allowed_open(
    limits: RiskLimits,
    price: Decimal,
    multiplier: Decimal,
    existing: Decimal = Decimal(0),
) -> int:
    """How many new contracts fit both size caps.

    One contract is price times the chain multiplier. A total within 10% of the
    money cap still fits. When one contract is already above that, the contract
    cap is the only limit.
    """
    cap = max(0, limits.max_contracts_per_symbol)
    one = price * multiplier
    if limits.max_position_per_symbol <= 0 or one <= 0:
        return cap
    ceiling = Decimal(limits.max_position_per_symbol) * _POSITION_SLACK
    if one > ceiling:
        return cap
    room = ceiling - existing
    if room < one:
        return 0
    return min(cap, int(room // one))


def block_reason(
    contracts: int,
    limits: RiskLimits,
    *,
    nav: Decimal | None = None,
    symbol_value: Decimal | None = None,
    cash: Decimal | None = None,
    margin: Decimal | None = None,
    iv_rank: int | None = None,
    dte: int | None = None,
    earnings_in_days: int | None = None,
    orders_today: int | None = None,
    opening: bool = True,
    needs_cash: bool = False,
    price: Decimal | None = None,
    multiplier: Decimal | None = None,
    existing: Decimal | None = None,
) -> str | None:
    """None when the draft fits. Missing marks are skipped, not invented."""
    if contracts < 1:
        return "contracts must be at least 1"
    if contracts > limits.max_contracts_per_symbol:
        return "contracts exceed max_contracts_per_symbol"
    if (
        opening
        and limits.max_position_per_symbol > 0
        and price is not None
        and price > 0
        and multiplier is not None
        and multiplier > 0
        and contracts > allowed_open(limits, price, multiplier, existing or Decimal(0))
    ):
        return "position exceeds max_position_per_symbol"
    if orders_today is not None and orders_today >= limits.max_orders_per_day:
        return "orders exceed max_orders_per_day"
    if not opening:
        return None
    if nav is not None and nav > 0 and symbol_value is not None:
        if symbol_value / nav * Decimal(100) > limits.max_symbol_pct_nav:
            return "symbol exceeds max_symbol_pct_nav"
    if needs_cash and nav is not None and nav > 0 and cash is not None:
        if cash / nav * Decimal(100) < limits.cash_reserve_pct:
            return "cash is below cash_reserve_pct"
    if nav is not None and nav > 0 and margin is not None:
        if margin / nav * Decimal(100) > limits.max_short_opt_margin_pct:
            return "margin exceeds max_short_opt_margin_pct"
    if iv_rank is not None and iv_rank < limits.min_iv_rank_new_short:
        return "iv rank is below min_iv_rank_new_short"
    if dte is not None and dte < limits.min_dte_new:
        return "dte is below min_dte_new"
    if earnings_in_days is not None and abs(earnings_in_days) <= limits.earnings_blackout_days:
        return "earnings blackout"
    return None
