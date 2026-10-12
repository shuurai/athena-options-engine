"""Draft size checks. Does not send orders."""

import os
from dataclasses import dataclass, field
from decimal import Decimal

from athena_settings.model import RiskLimits

_POSITION_SLACK = Decimal("1.1")
UNCLASSIFIED_BUCKET = "UNCLASSIFIED"


def bucket_names(raw) -> list[str]:
    """A name with no classifier shares one bucket, so the cap still applies."""
    names = [str(item).strip() for item in (raw or []) if str(item).strip()]
    return names or [UNCLASSIFIED_BUCKET]


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
    rv_pctl: int | None = None,
    dte: int | None = None,
    earnings_in_days: int | None = None,
    orders_today: int | None = None,
    opening: bool = True,
    needs_cash: bool = False,
    price: Decimal | None = None,
    multiplier: Decimal | None = None,
    existing: Decimal | None = None,
    enforce_contract_cap: bool = True,
) -> str | None:
    """None when the draft fits. Missing marks are skipped, not invented."""
    if contracts < 1:
        return "contracts must be at least 1"
    if enforce_contract_cap and contracts > limits.max_contracts_per_symbol:
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
    if rv_pctl is not None and rv_pctl < limits.min_rv_pctl_new_short:
        return "rv percentile is below min_rv_pctl_new_short"
    if dte is not None and dte < limits.min_dte_new:
        return "dte is below min_dte_new"
    if earnings_in_days is not None and abs(earnings_in_days) <= limits.earnings_blackout_days:
        return "earnings blackout"
    return None


def budget_enabled() -> bool:
    flag = os.environ.get("ATHENA_PORTFOLIO_BUDGET", "on").strip().lower()
    return flag not in {"0", "off", "false", "no"}


def panel_enabled() -> bool:
    flag = os.environ.get("ATHENA_PANEL", "on").strip().lower()
    return flag not in {"0", "off", "false", "no"}


def cycle_rank(reason: str) -> int:
    """Close and roll first, then a dip with shares, then a covered call."""
    if reason in {"manage_close", "manage_roll", "CLOSE", "ROLL"}:
        return 0
    if reason == "dip_long":
        return 1
    if reason == "high_cc":
        return 2
    return 3


@dataclass
class CycleBook:
    """One pass. Cash and margin are the running remainder, in the book currency."""

    nav: Decimal | None
    cash: Decimal | None
    margin: Decimal | None
    orders: int | None
    currency: str = ""
    base: str = ""
    rates: dict = field(default_factory=dict)
    buckets: dict = field(default_factory=dict)
    assignment: Decimal = Decimal(0)
    secured: Decimal = Decimal(0)
    symbols: dict = field(default_factory=dict)

    def clone(self) -> "CycleBook":
        return CycleBook(
            self.nav,
            self.cash,
            self.margin,
            self.orders,
            self.currency,
            self.base,
            dict(self.rates),
            dict(self.buckets),
            self.assignment,
            self.secured,
            dict(self.symbols),
        )

    def take(self, other: "CycleBook") -> None:
        self.cash = other.cash
        self.margin = other.margin
        self.orders = other.orders
        self.buckets = dict(other.buckets)
        self.assignment = other.assignment
        self.secured = other.secured
        self.symbols = dict(other.symbols)


def walk_cycle(tickets: list[dict], book: CycleBook, limits: RiskLimits) -> list[tuple[dict, str | None, int]]:
    """Sort by action, then judge each ticket against the running book.

    A ticket that fits is absorbed. A ticket that fails is blocked alone.
    Rank order stays: closes, then a dip with shares, then a covered call.
    Symbol order inside a rank decides who is judged first.
    """
    ordered = sorted(tickets, key=lambda ticket: (cycle_rank(str(ticket.get("reason") or "")), str(ticket.get("symbol") or "")))
    judged: list[tuple[dict, str | None, int]] = []
    order = 1
    for ticket in ordered:
        failed = _cycle_reason(ticket, limits, book)
        judged.append((ticket, failed, order))
        order += 1
        if failed is None:
            _absorb(book, ticket, limits)
    return judged


def block_ticket_reason(
    legs: list[dict],
    limits: RiskLimits,
    marks: dict,
    existing: Decimal | None = None,
    book: CycleBook | None = None,
    symbol: str = "",
    classifiers: list[str] | None = None,
    currency: str = "",
) -> str | None:
    """None when the pending legs of one ticket fit together.

    Money is compared in the book currency. A missing FX rate or mark blocks
    the ticket and names what is missing. A cycle book checks the running
    remainder instead of the stored snapshot.
    """
    if not legs:
        return None
    if book is not None:
        return _cycle_reason(
            {
                "symbol": symbol,
                "currency": currency or book.currency,
                "classifiers": classifiers or [],
                "legs": legs,
            },
            limits,
            book,
        )
    nav = _decimal(marks.get("nav"))
    cash = _decimal(marks.get("cash"))
    margin = _decimal(marks.get("margin"))
    price = _decimal(marks.get("price"))
    currency = str(marks.get("currency") or "")
    base = str(marks.get("base") or "")
    rates = marks.get("rates") or {}
    held = existing or Decimal(0)

    need = Decimal(0)
    cash_known = True
    for leg in legs:
        part = _cash_need(leg)
        if part is None:
            cash_known = False
            break
        need += part
    missing = None
    if cash_known and need > 0 and cash is not None and nav is not None and nav > 0:
        booked, gap = _require_book(need, currency, base, rates)
        missing = gap or missing
        if booked is not None and (cash - booked) / nav * Decimal(100) < limits.cash_reserve_pct:
            return "I2: ticket breaches cash_reserve_pct"

    added = Decimal(0)
    symbol_known = True
    for leg in legs:
        part = _symbol_need(leg, price)
        if part is None:
            symbol_known = False
            break
        added += part
    if not symbol_known and _needs_mark(legs):
        missing = missing or "missing mark"
    elif symbol_known and nav is not None and nav > 0:
        booked, gap = _require_book(held + added, currency, base, rates)
        missing = missing or gap
        if booked is not None and booked / nav * Decimal(100) > limits.max_symbol_pct_nav:
            return "I2: ticket breaches max_symbol_pct_nav"

    estimate = Decimal(0)
    margin_known = margin is not None
    if margin_known:
        for leg in legs:
            part = _margin_notional(leg)
            if part is None:
                margin_known = False
                break
            estimate += part * Decimal(limits.margin_rate) / Decimal(100)
    if margin_known and nav is not None and nav > 0:
        booked, gap = _require_book(estimate, currency, base, rates)
        missing = missing or gap
        if booked is not None:
            cap = nav * Decimal(limits.max_short_opt_margin_pct) / Decimal(100)
            if (margin + booked) / nav * Decimal(100) > limits.max_short_opt_margin_pct:
                return (
                    "I2: ticket breaches max_short_opt_margin_pct "
                    f"(estimate {booked:.2f}, cap {cap:.2f})"
                )

    orders_today = marks.get("orders_today")
    if orders_today is not None:
        fresh = sum(int(leg.get("orders") or 1) for leg in legs)
        if int(orders_today) + fresh > limits.max_orders_per_day:
            return "I2: ticket breaches max_orders_per_day"
    return missing


def _cash_need(leg: dict) -> Decimal | None:
    contracts = Decimal(leg["contracts"])
    mult = leg["multiplier"]
    tactic = leg["tactic"]
    if tactic == "BUY":
        return None if leg.get("limit") is None else leg["limit"] * contracts
    if tactic == "OPEN_CSP":
        return None if leg.get("put_strike") is None else leg["put_strike"] * mult * contracts
    if tactic == "OPEN_STRANGLE":
        return None if leg.get("put_strike") is None else leg["put_strike"] * mult * contracts
    return Decimal(0)


def _symbol_need(leg: dict, price: Decimal | None) -> Decimal | None:
    contracts = Decimal(leg["contracts"])
    if leg["tactic"] == "BUY":
        return None if leg.get("limit") is None else leg["limit"] * contracts
    if price is None or price <= 0:
        return None
    return price * leg["multiplier"] * contracts


def _margin_notional(leg: dict) -> Decimal | None:
    contracts = Decimal(leg["contracts"])
    mult = leg["multiplier"]
    tactic = leg["tactic"]
    if tactic == "OPEN_CSP":
        return None if leg.get("put_strike") is None else leg["put_strike"] * mult * contracts
    if tactic == "OPEN_CC":
        return None if leg.get("call_strike") is None else leg["call_strike"] * mult * contracts
    if tactic == "OPEN_STRANGLE":
        put_strike = leg.get("put_strike")
        call_strike = leg.get("call_strike")
        if put_strike is None or call_strike is None:
            return None
        return (put_strike + call_strike) * mult * contracts
    return Decimal(0)


def _to_book(amount: Decimal, source: str, base: str, rates: dict) -> Decimal | None:
    booked, _gap = _require_book(amount, source, base, rates)
    return booked


def book_amount(amount: Decimal, source: str, base: str, rates: dict) -> tuple[Decimal | None, str | None]:
    """Amount in the book currency. The reason names a rate the book does not have."""
    return _require_book(amount, source, base, rates)


def _require_book(amount: Decimal, source: str, base: str, rates: dict) -> tuple[Decimal | None, str | None]:
    if not source or not base or source == base:
        if source and base and source == base:
            return amount, None
        return None, None
    rate = _fx(source, rates)
    if rate is None:
        return None, f"missing fx rate {source}"
    return amount * rate, None


def _needs_mark(legs: list[dict]) -> bool:
    for leg in legs:
        if leg.get("tactic") in {"BUY", "CLOSE", "ROLL"}:
            continue
        price = leg.get("price")
        if price is None or Decimal(price) <= 0:
            return True
    return False


def _fx(code: str, rates: dict) -> Decimal | None:
    if code == "CNY" and code not in rates and "CNH" in rates:
        code = "CNH"
    raw = rates.get(code)
    if raw is None:
        return None
    rate = Decimal(str(raw))
    if rate == 0:
        return None
    return rate


def _decimal(value) -> Decimal | None:
    if value is None:
        return None
    return Decimal(value)


def _cycle_reason(ticket: dict, limits: RiskLimits, book: CycleBook) -> str | None:
    """None when this ticket fits the running book. A missing rate or mark blocks it."""
    legs = ticket.get("legs") or []
    currency = str(ticket.get("currency") or book.currency or "")
    symbol = str(ticket.get("symbol") or "")
    names = bucket_names(ticket.get("classifiers"))
    missing = None
    cash_need = _sum_legs(legs, _cycle_cash)
    if cash_need is not None and cash_need > 0 and book.cash is not None and book.nav is not None and book.nav > 0:
        booked, gap = _require_book(cash_need, currency, book.base, book.rates)
        missing = gap or missing
        if booked is not None and (book.cash - booked) / book.nav * Decimal(100) < limits.cash_reserve_pct:
            return "cycle cash reserve"
    added = _sum_legs(legs, lambda leg: _cycle_symbol(leg))
    held = book.symbols.get(symbol, Decimal(0))
    if added is None and _needs_mark(legs):
        missing = missing or "missing mark"
    elif added is not None and book.nav is not None and book.nav > 0:
        booked, gap = _require_book(held + added, currency, book.base, book.rates)
        missing = missing or gap
        if booked is not None and booked / book.nav * Decimal(100) > limits.max_symbol_pct_nav:
            return "cycle symbol cap"
    margin_add = _sum_legs(legs, lambda leg: _cycle_margin(leg, limits))
    if margin_add is not None and book.margin is not None and book.nav is not None and book.nav > 0:
        booked, gap = _require_book(margin_add, currency, book.base, book.rates)
        missing = missing or gap
        if booked is not None and (book.margin + booked) / book.nav * Decimal(100) > limits.max_short_opt_margin_pct:
            return "cycle margin"
    if book.orders is not None:
        fresh = sum(int(leg.get("orders") or 1) for leg in legs)
        if int(book.orders) + fresh > limits.max_orders_per_day:
            return "cycle orders"
    bucket_add = _sum_legs(legs, _bucket_amount)
    if bucket_add is not None and bucket_add > 0 and book.nav is not None and book.nav > 0:
        booked, gap = _require_book(bucket_add, currency, book.base, book.rates)
        missing = missing or gap
        if booked is not None:
            for name in names:
                used = book.buckets.get(name, Decimal(0)) + booked
                pct = used / book.nav * Decimal(100)
                if pct > limits.max_bucket_pct_nav:
                    return f"cycle bucket {name} at {pct:.0f}%"
    assign_add = _sum_legs(legs, lambda leg: _assignment_amount(leg, limits))
    if assign_add is not None and book.nav is not None and book.nav > 0 and book.cash is not None:
        booked_assign, assign_gap = _require_book(assign_add, currency, book.base, book.rates)
        booked_cash, cash_gap = (
            (Decimal(0), None)
            if cash_need in (None, 0)
            else _require_book(cash_need or Decimal(0), currency, book.base, book.rates)
        )
        missing = missing or assign_gap or cash_gap
        if booked_assign is not None and booked_cash is not None:
            cash_after = book.cash + book.secured - booked_cash - book.assignment
            floor = book.nav * Decimal(str(limits.min_cash_after_assign_pct)) / Decimal(100)
            if cash_after < floor:
                return "assignment would breach min cash"
            assign_after = book.assignment + booked_assign
            if assign_after / book.nav * Decimal(100) > limits.max_assign_pct_nav:
                return "cycle assignment cap"
    return missing


def _absorb(book: CycleBook, ticket: dict, limits: RiskLimits) -> None:
    legs = ticket.get("legs") or []
    currency = str(ticket.get("currency") or book.currency or "")
    symbol = str(ticket.get("symbol") or "")
    names = bucket_names(ticket.get("classifiers"))
    cash_need = _sum_legs(legs, _cycle_cash)
    if cash_need is not None and book.cash is not None:
        booked = _to_book(cash_need, currency, book.base, book.rates)
        if booked is not None:
            book.cash -= booked
    added = _sum_legs(legs, _cycle_symbol)
    if added is not None:
        booked = _to_book(added, currency, book.base, book.rates)
        if booked is not None:
            book.symbols[symbol] = book.symbols.get(symbol, Decimal(0)) + booked
    margin_add = _sum_legs(legs, lambda leg: _cycle_margin(leg, limits))
    if margin_add is not None and book.margin is not None:
        booked = _to_book(margin_add, currency, book.base, book.rates)
        if booked is not None:
            book.margin += booked
    if book.orders is not None:
        book.orders += sum(int(leg.get("orders") or 1) for leg in legs)
    bucket_add = _sum_legs(legs, _bucket_amount)
    if bucket_add is not None:
        booked = _to_book(bucket_add, currency, book.base, book.rates)
        if booked is not None:
            for name in names:
                book.buckets[name] = book.buckets.get(name, Decimal(0)) + booked
    assign_add = _sum_legs(legs, lambda leg: _assignment_amount(leg, limits))
    if assign_add is not None:
        booked = _to_book(assign_add, currency, book.base, book.rates)
        if booked is not None:
            book.assignment += booked
            book.secured += booked


def _sum_legs(legs: list[dict], one) -> Decimal | None:
    total = Decimal(0)
    for leg in legs:
        part = one(leg)
        if part is None:
            return None
        total += part
    return total


def _cycle_cash(leg: dict) -> Decimal | None:
    tactic = leg["tactic"]
    contracts = Decimal(leg["contracts"])
    mult = leg["multiplier"]
    if tactic in {"CLOSE", "ROLL"} and leg.get("put_strike") is not None:
        return -(leg["put_strike"] * mult * contracts)
    return _cash_need(leg)


def _cycle_symbol(leg: dict) -> Decimal | None:
    if leg["tactic"] in {"CLOSE", "ROLL"}:
        return Decimal(0)
    return _symbol_need(leg, _decimal(leg.get("price")))


def _cycle_margin(leg: dict, limits: RiskLimits) -> Decimal | None:
    tactic = leg["tactic"]
    contracts = Decimal(leg["contracts"])
    mult = leg["multiplier"]
    rate = Decimal(limits.margin_rate) / Decimal(100)
    if tactic in {"CLOSE", "ROLL"}:
        strike = leg.get("put_strike") if leg.get("put_strike") is not None else leg.get("call_strike")
        if strike is None:
            return Decimal(0)
        return -(strike * mult * contracts * rate)
    part = _margin_notional(leg)
    if part is None:
        return None
    return part * rate


def _bucket_amount(leg: dict) -> Decimal | None:
    tactic = leg["tactic"]
    contracts = Decimal(leg["contracts"])
    mult = leg["multiplier"]
    if tactic == "BUY":
        if leg.get("limit") is None:
            return None
        return leg["limit"] * contracts
    if tactic in {"OPEN_CSP", "OPEN_STRANGLE"} and leg.get("put_strike") is not None:
        return leg["put_strike"] * mult * contracts
    if tactic in {"CLOSE", "ROLL"} and leg.get("put_strike") is not None:
        return -(leg["put_strike"] * mult * contracts)
    return Decimal(0)


def _assignment_amount(leg: dict, limits: RiskLimits) -> Decimal | None:
    if leg.get("put_strike") is None:
        return Decimal(0)
    if leg["tactic"] not in {"OPEN_CSP", "OPEN_STRANGLE", "CLOSE", "ROLL"}:
        return Decimal(0)
    delta = leg.get("delta")
    if delta is None:
        return Decimal(0)
    if abs(Decimal(delta)) < Decimal(str(limits.assign_delta)):
        return Decimal(0)
    amount = leg["put_strike"] * leg["multiplier"] * Decimal(leg["contracts"])
    if leg["tactic"] in {"CLOSE", "ROLL"}:
        return -amount
    return amount
