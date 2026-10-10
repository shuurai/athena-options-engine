"""Display money in the chosen currency. Ticker prices stay in the market currency."""

from __future__ import annotations

from decimal import Decimal

from athena_settings import load_active

CURRENCIES = ("HKD", "USD", "AUD", "CNY", "JPY", "CAD")
DASH = "—"


def report_currency(conn, account: str) -> str:
    if not account:
        return "HKD"
    code = load_active(conn, account).global_currency
    return code if code in CURRENCIES else "HKD"


def ledger(conn, account: str) -> tuple[str, dict[str, Decimal], dict[str, Decimal]]:
    """Latest base currency, per-currency cash, and base units per one unit."""
    row = conn.execute(
        """
        SELECT base_currency, cash_balances, fx_rates
        FROM account_marks
        WHERE account_id = %s
        ORDER BY day DESC
        LIMIT 1
        """,
        (account,),
    ).fetchone()
    if row is None:
        return "", {}, {}
    base = row[0] or "HKD"
    return base, _decimals(row[1]), _decimals(row[2])


def convert(
    amount: Decimal,
    source: str,
    target: str,
    base: str,
    rates: dict[str, Decimal],
) -> Decimal | None:
    """Move an amount into the target currency. A missing IB rate stays blank."""
    if source == target:
        return amount
    src = _rate(source, base, rates)
    dst = _rate(target, base, rates)
    if src is None or dst is None:
        return None
    return amount * src / dst


def show(amount, currency: str) -> str:
    if amount is None:
        return DASH
    number = Decimal(amount)
    if currency == "JPY":
        text = f"{number:,.0f}"
    else:
        text = f"{number:,.2f}"
    return f"{text} {currency}"


def marked(amount, currency: str) -> str:
    """Money with its symbol in front. The 3-letter code stays on the card badge."""
    if amount is None:
        return DASH
    number = Decimal(amount)
    if currency == "JPY":
        text = f"{abs(number):,.0f}"
    else:
        text = f"{abs(number):,.2f}"
    sign = "−" if number < 0 else ""
    return f"{sign}{_symbol(currency)}{text}"


def _symbol(currency: str) -> str:
    if currency in {"CNY", "JPY"}:
        return "¥"
    return "$"


def cash_rows(balances: dict[str, Decimal]) -> list[tuple[str, str]]:
    """Native cash, including a negative margin balance. Unused currencies stay off."""
    order = {code: index for index, code in enumerate(CURRENCIES)}
    rows = []
    for code, amount in sorted(balances.items(), key=lambda item: (order.get(item[0], 99), item[0])):
        rows.append((code, _native(amount)))
    return rows


def _rate(code: str, base: str, rates: dict[str, Decimal]) -> Decimal | None:
    if code == base:
        return Decimal(1)
    if code == "CNY" and "CNH" in rates:
        code = "CNH"
    rate = rates.get(code)
    if rate is None or rate == 0:
        return None
    return rate


def _decimals(raw) -> dict[str, Decimal]:
    if not raw:
        return {}
    return {str(key): Decimal(str(value)) for key, value in raw.items()}


def _native(amount: Decimal) -> str:
    text = f"{abs(amount):,.2f}"
    if amount < 0:
        return f"−{text}"
    return text
