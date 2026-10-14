"""Cycle budget. One walk after the account has drafted, using the same ticket check."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from athena_exec.broker import stock_venue
from athena_exec.options import bs_delta
from athena_marketdata.pricing import last_tradeable_price
from athena_risk import CycleBook, _to_book, bucket_names, budget_enabled, walk_cycle
from athena_settings.model import SettingsPayload
from athena_strategy.drafts import (
    _chain_multiplier,
    cash_reserve_math,
    draft_cash_need,
    format_risk_block_detail,
    risk_block_steps,
)
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
    from athena_strategy.agents import write_decision, write_risk_block

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
            steps, risk_legs = _budget_block_steps(ticket, reason, book, settings.risk)
            write_decision(
                conn, account_id, ticket["symbol"], "budget", "dropped",
                f"{summary} {reason}", steps,
            )
            used = 0
            if book.orders is not None:
                used = int(book.orders.get(str(ticket["symbol"]).upper(), 0))
            for leg in risk_legs:
                write_risk_block(
                    conn, account_id, ticket["symbol"], reason,
                    risk_block_steps(
                        draft_id=leg["draft_id"],
                        symbol=ticket["symbol"],
                        tactic=leg["tactic"],
                        contracts=leg["contracts"],
                        reason=reason,
                        cash=book.cash,
                        nav=book.nav,
                        excess=book.excess,
                        cash_reserve_pct=settings.risk.cash_reserve_pct,
                        cash_need=leg.get("cash_need"),
                        note=leg.get("note") or "",
                        orders_today=used,
                        max_orders_per_day=settings.risk.max_orders_per_day,
                        max_contracts_per_symbol=settings.risk.max_contracts_per_symbol,
                        max_position_per_symbol=settings.risk.max_position_per_symbol,
                        max_symbol_pct_nav=settings.risk.max_symbol_pct_nav,
                        margin=book.margin,
                        max_short_opt_margin_pct=settings.risk.max_short_opt_margin_pct,
                    ),
                )
        else:
            write_decision(
                conn, account_id, ticket["symbol"], "budget", "drafted",
                f"{summary} kept", _budget_kept_steps(ticket),
            )
    conn.commit()
    still = {row[0] for row in conn.execute(
        "SELECT id FROM drafts WHERE id = ANY(%s) AND status = 'pending'",
        (draft_ids,),
    ).fetchall()}
    return [item for item in draft_ids if item in still]


def _budget_kept_steps(ticket: dict) -> dict:
    items = []
    for draft_id, leg in zip(ticket.get("draft_ids") or [], ticket.get("legs") or []):
        short = leg["tactic"][5:] if str(leg["tactic"]).startswith("OPEN_") else leg["tactic"]
        items.append({
            "tactic": short,
            "outcome": "kept",
            "detail": f"{short} {int(leg['contracts'])} kept",
            "draft_id": draft_id,
            "contracts": int(leg["contracts"]),
        })
    return {"items": items}


def _budget_block_steps(ticket: dict, reason: str, book, risk) -> tuple[dict, list[dict]]:
    cash_reserve_pct = risk.cash_reserve_pct
    used = 0
    if book.orders is not None:
        used = int(book.orders.get(str(ticket["symbol"]).upper(), 0))
    items = []
    risk_legs = []
    first_math = cash_reserve_math(
        book.cash, book.nav, cash_reserve_pct, excess=book.excess,
    )
    for draft_id, leg in zip(ticket.get("draft_ids") or [], ticket.get("legs") or []):
        need = draft_cash_need(
            leg["tactic"], int(leg["contracts"]),
            limit_price=leg.get("limit"), put_strike=leg.get("put_strike"),
            multiplier=str(leg.get("multiplier") or "100"),
        )
        math = cash_reserve_math(
            book.cash, book.nav, cash_reserve_pct, excess=book.excess, cash_need=need,
        )
        note = ""
        if "cash" in (reason or "").lower() and "reserve" in (reason or "").lower():
            note = "cycle cash reserve"
        detail = format_risk_block_detail(
            tactic=leg["tactic"],
            contracts=int(leg["contracts"]),
            cash=book.cash,
            nav=book.nav,
            cash_pct=math["cash_pct"],
            cash_reserve_pct=cash_reserve_pct,
            reserve_floor=math["reserve_floor"],
            shortfall=math["shortfall"],
            cash_need=need,
            excess=book.excess,
            buffer=math.get("buffer"),
            reason=reason,
            note=note,
            orders_today=used,
            max_orders_per_day=risk.max_orders_per_day,
            max_contracts_per_symbol=risk.max_contracts_per_symbol,
            max_position_per_symbol=risk.max_position_per_symbol,
            max_symbol_pct_nav=risk.max_symbol_pct_nav,
            margin=book.margin,
            max_short_opt_margin_pct=risk.max_short_opt_margin_pct,
        )
        short = leg["tactic"][5:] if str(leg["tactic"]).startswith("OPEN_") else leg["tactic"]
        items.append({
            "tactic": short,
            "outcome": "blocked",
            "detail": detail,
            "draft_id": draft_id,
            "contracts": int(leg["contracts"]),
        })
        risk_legs.append({
            "draft_id": draft_id,
            "tactic": leg["tactic"],
            "contracts": int(leg["contracts"]),
            "cash_need": need,
            "note": note,
        })
    cash_pct = first_math["cash_pct"]
    return {
        "reason": reason,
        "cash": None if book.cash is None else str(book.cash),
        "excess": None if book.excess is None else str(book.excess),
        "nav": None if book.nav is None else str(book.nav),
        "cash_pct": None if cash_pct is None else str(cash_pct),
        "cash_reserve_pct": str(cash_reserve_pct),
        "reserve_floor": None if first_math["reserve_floor"] is None else str(first_math["reserve_floor"]),
        "shortfall": None if first_math["shortfall"] is None else str(first_math["shortfall"]),
        "orders_today": used,
        "max_orders_per_day": risk.max_orders_per_day,
        "items": items,
    }, risk_legs


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
    del account_id
    row = conn.execute(
        "SELECT classifiers FROM symbol_facts WHERE symbol = %s",
        (symbol.upper(),),
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
        SELECT cash, margin, base_currency, fx_rates, excess_liquidity
        FROM account_marks WHERE account_id = %s ORDER BY day DESC LIMIT 1
        """,
        (account_id,),
    ).fetchone()
    order_rows = conn.execute(
        """
        SELECT upper(symbol), count(*) FROM orders
        WHERE account_id = %s AND status <> 'blocked' AND created_at::date = CURRENT_DATE
        GROUP BY upper(symbol)
        """,
        (account_id,),
    ).fetchall()
    orders = {str(symbol): int(count) for symbol, count in order_rows if symbol}
    rates = {}
    if mark is not None and mark[3]:
        rates = {str(key): Decimal(str(value)) for key, value in mark[3].items()}
    base = "" if mark is None or not mark[2] else mark[2]
    book = CycleBook(
        nav=None if nav is None else Decimal(nav[0]),
        cash=None if mark is None else Decimal(mark[0]),
        margin=None if mark is None or mark[1] is None else Decimal(mark[1]),
        orders=orders,
        base=base,
        rates=rates,
        excess=None if mark is None or len(mark) < 5 or mark[4] is None else Decimal(mark[4]),
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
        names = bucket_names(_classifiers(conn, account_id, symbol))
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
