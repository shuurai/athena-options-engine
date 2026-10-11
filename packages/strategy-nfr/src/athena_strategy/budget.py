"""Cycle budget. One walk after the account has drafted, using the same ticket check."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from athena_exec.broker import stock_venue
from athena_exec.options import bs_delta
from athena_marketdata.pricing import last_tradeable_price
from athena_risk import CycleBook, _to_book, budget_enabled, walk_cycle
from athena_settings.model import SettingsPayload
from athena_strategy.drafts import _chain_multiplier
from athena_strategy.indicators import compute, load_bars
from athena_strategy.screener import sigma_for


def apply_cycle_budget(conn, settings: SettingsPayload, account_id: str, draft_ids: list[str]) -> list[str]:
    """Block tickets the running book cannot hold. Returns the drafts still pending."""
    if not draft_ids:
        return []
    if not budget_enabled():
        return list(draft_ids)
    pending = _pending(conn, settings, account_id, draft_ids)
    if not pending:
        return []
    book = _baseline(conn, settings, account_id)
    walked = walk_cycle(pending, book, settings.risk)
    from athena_strategy.agents import write_decision

    for ticket, reason, order in walked:
        summary = f"cycle_order={order} prob_source=delta_proxy"
        if reason:
            conn.execute(
                """
                UPDATE drafts SET status = 'blocked', reason = %s
                WHERE id = ANY(%s) AND status = 'pending'
                """,
                (reason, ticket["draft_ids"]),
            )
            if ticket.get("id"):
                conn.execute(
                    "UPDATE tickets SET status = 'blocked' WHERE id = %s",
                    (ticket["id"],),
                )
            write_decision(
                conn, account_id, ticket["symbol"], "budget", "dropped",
                f"{summary} {reason}", {"items": []},
            )
        else:
            write_decision(
                conn, account_id, ticket["symbol"], "budget", "drafted",
                f"{summary} kept", {"items": []},
            )
    conn.commit()
    still = {row[0] for row in conn.execute(
        "SELECT id FROM drafts WHERE id = ANY(%s) AND status = 'pending'",
        (draft_ids,),
    ).fetchall()}
    return [item for item in draft_ids if item in still]


def _pending(conn, settings: SettingsPayload, account_id: str, draft_ids: list[str]) -> list[dict]:
    rows = conn.execute(
        """
        SELECT d.id, d.ticket_id, d.symbol, d.tactic, d.contracts, d.limit_price,
               d.put_strike, d.call_strike, d.multiplier, d.expiry,
               COALESCE(t.reason, d.reason)
        FROM drafts d
        LEFT JOIN tickets t ON t.id = d.ticket_id
        WHERE d.account_id = %s AND d.id = ANY(%s) AND d.status = 'pending'
        ORDER BY d.leg_index NULLS FIRST, d.id
        """,
        (account_id, draft_ids),
    ).fetchall()
    grouped: dict[str, dict] = {}
    for draft_id, ticket_id, symbol, tactic, contracts, limit_price, put_strike, call_strike, multiplier, expiry, reason in rows:
        key = ticket_id or draft_id
        ticket = grouped.get(key)
        if ticket is None:
            ticket = {
                "id": ticket_id,
                "symbol": symbol,
                "reason": reason or "",
                "currency": stock_venue(symbol)[1],
                "classifiers": _classifiers(conn, account_id, symbol),
                "draft_ids": [],
                "legs": [],
            }
            grouped[key] = ticket
        price = last_tradeable_price(conn, symbol)
        ticket["draft_ids"].append(draft_id)
        ticket["legs"].append({
            "tactic": tactic,
            "contracts": int(contracts),
            "limit": None if limit_price is None else Decimal(limit_price),
            "put_strike": None if put_strike is None else Decimal(put_strike),
            "call_strike": None if call_strike is None else Decimal(call_strike),
            "multiplier": _chain_multiplier(multiplier),
            "orders": 2 if tactic == "OPEN_STRANGLE" else 1,
            "price": price,
            "delta": _put_delta(conn, settings, symbol, put_strike, expiry, price) if put_strike is not None else None,
        })
    return list(grouped.values())


def _put_delta(conn, settings: SettingsPayload | None, symbol: str, strike, expiry, price) -> Decimal | None:
    if settings is None or strike is None or expiry is None or price is None:
        return None
    today = date.today()
    dte = (expiry - today).days
    rv20 = compute(load_bars(conn, symbol, settings.strategy.rv_window + 1), settings.strategy).rv20
    sigma, _source = sigma_for(conn, settings, symbol, today, rv20)
    if sigma is None:
        return None
    return bs_delta(price, Decimal(strike), dte, sigma, "P")


def _classifiers(conn, account_id: str, symbol: str) -> list[str]:
    row = conn.execute(
        """
        SELECT classifiers FROM symbol_phases
        WHERE account_id = %s AND symbol = %s
        """,
        (account_id, symbol.upper()),
    ).fetchone()
    if row is None or not row[0]:
        return []
    return [part.strip() for part in str(row[0]).split(",") if part.strip()]


def _baseline(conn, settings: SettingsPayload, account_id: str) -> CycleBook:
    nav = conn.execute(
        "SELECT last_nav FROM nav_marks WHERE account_id = %s ORDER BY day DESC LIMIT 1",
        (account_id,),
    ).fetchone()
    mark = conn.execute(
        """
        SELECT cash, margin, base_currency, fx_rates
        FROM account_marks WHERE account_id = %s ORDER BY day DESC LIMIT 1
        """,
        (account_id,),
    ).fetchone()
    orders = conn.execute(
        """
        SELECT count(*) FROM orders
        WHERE account_id = %s AND status <> 'blocked' AND created_at::date = CURRENT_DATE
        """,
        (account_id,),
    ).fetchone()[0]
    rates = {}
    if mark is not None and mark[3]:
        rates = {str(key): Decimal(str(value)) for key, value in mark[3].items()}
    base = "" if mark is None or not mark[2] else mark[2]
    book = CycleBook(
        nav=None if nav is None else Decimal(nav[0]),
        cash=None if mark is None else Decimal(mark[0]),
        margin=None if mark is None or mark[1] is None else Decimal(mark[1]),
        orders=int(orders),
        base=base,
        rates=rates,
    )
    replaying = conn.execute(
        """
        SELECT 1 FROM sim_runs
        WHERE account_id = %s AND status IN ('active', 'halted') AND cursor_date IS NOT NULL
        LIMIT 1
        """,
        (account_id,),
    ).fetchone()
    rows = conn.execute(
        """
        SELECT DISTINCT ON (symbol, sec_type, expiry, contract_right, strike)
            symbol, quantity, avg_price, sec_type, expiry, contract_right, strike, multiplier
        FROM positions
        WHERE account_id = %s AND simulated = %s AND quantity <> 0
        ORDER BY symbol, sec_type, expiry, contract_right, strike, snapshot_at DESC
        """,
        (account_id, replaying is not None),
    ).fetchall()
    for symbol, quantity, avg_price, sec_type, expiry, right, strike, multiplier in rows:
        qty = Decimal(quantity)
        mult = Decimal(multiplier or (100 if sec_type == "OPT" else 1))
        currency = stock_venue(symbol)[1]
        names = _classifiers(conn, account_id, symbol)
        amount = Decimal(0)
        if sec_type == "OPT" and right == "P" and qty < 0 and strike is not None:
            amount = abs(qty) * Decimal(strike) * mult
            price = last_tradeable_price(conn, symbol)
            delta = _put_delta(conn, settings, symbol, strike, expiry, price)
            if delta is not None and abs(delta) >= Decimal(str(settings.risk.assign_delta)):
                booked = _into(book, amount, currency)
                if booked is not None:
                    book.assignment += booked
        elif sec_type != "OPT" and avg_price is not None:
            amount = abs(qty) * Decimal(avg_price)
        if amount == 0:
            continue
        booked = _into(book, amount, currency)
        if booked is None:
            continue
        for name in names:
            book.buckets[name] = book.buckets.get(name, Decimal(0)) + booked
        if sec_type != "OPT":
            book.symbols[symbol] = book.symbols.get(symbol, Decimal(0)) + booked
    if book.base:
        book.currency = book.base
    return book


def _into(book: CycleBook, amount: Decimal, currency: str):
    if not book.base:
        book.base = currency
    return _to_book(amount, currency, book.base, book.rates)
