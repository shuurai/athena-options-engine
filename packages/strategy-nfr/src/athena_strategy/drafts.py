"""Create a paper draft, then Approve or Reject it into an audit row."""

from __future__ import annotations

import secrets
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import psycopg

from athena_exec.halt import active_reason, note_reject_streak
from athena_risk import allowed_open, block_reason, block_ticket_reason, book_amount
from athena_settings import load_active
from athena_settings.model import SettingsPayload


class AmendError(ValueError):
    """The link is still good. The numbers are not."""

PAPER_SYMBOL = "NVDA"
PAPER_TACTIC = "OPEN_STRANGLE"


def ensure_paper_draft(conn: psycopg.Connection, settings: SettingsPayload, account_id: str) -> str | None:
    """Insert one pending NVDA strangle when none is waiting. Returns the id."""
    if active_reason(conn, account_id):
        return None
    existing = conn.execute(
        """
        SELECT id FROM drafts
        WHERE symbol = %s AND account_id = %s AND status = 'pending'
        """,
        (PAPER_SYMBOL, account_id),
    ).fetchone()
    if existing is not None:
        return None
    conn.execute(
        """
        INSERT INTO symbol_phases (account_id, symbol, phase, tags, tactic)
        VALUES (%s, %s, 'P2', 'NEWS_WASH', 'OPEN_STRANGLE')
        ON CONFLICT (account_id, symbol) DO UPDATE
        SET phase = EXCLUDED.phase, tags = EXCLUDED.tags, tactic = EXCLUDED.tactic
        """,
        (account_id, PAPER_SYMBOL),
    )
    draft_id = propose(
        conn,
        settings,
        PAPER_SYMBOL,
        PAPER_TACTIC,
        1,
        "P2 stub.",
        "P2",
        account_id,
    )
    return draft_id if draft_id else None


def has_open_draft(conn: psycopg.Connection, account_id: str, symbol: str, tactic: str) -> bool:
    """True when this tactic is already pending or approved and not filled or cancelled."""
    row = conn.execute(
        """
        SELECT 1 FROM drafts
        WHERE symbol = %s AND account_id = %s AND tactic = %s
          AND status IN ('pending', 'approved')
          AND NOT EXISTS (
            SELECT 1 FROM orders
            WHERE orders.draft_id = drafts.id
              AND orders.status IN ('filled', 'cancelled')
          )
        LIMIT 1
        """,
        (symbol, account_id, tactic),
    ).fetchone()
    return row is not None


def expire_pending(conn: psycopg.Connection, account_id: str, ttl_minutes: int) -> list[tuple]:
    """Mark pending drafts past the TTL as expired. Approved drafts stay."""
    rows = conn.execute(
        """
        UPDATE drafts
        SET status = 'expired'
        WHERE account_id = %s
          AND status = 'pending'
          AND created_at < now() - make_interval(mins => %s)
        RETURNING id, symbol, tactic, account_id
        """,
        (account_id, ttl_minutes),
    ).fetchall()
    conn.commit()
    return list(rows)


def draft_facts(conn: psycopg.Connection, draft_id: str) -> list[str]:
    """Expiry, strikes, and limits for one draft. Empty when the draft is missing."""
    row = conn.execute(
        """
        SELECT symbol, tactic, contracts, phase, reason, account_id,
               expiry, put_strike, call_strike, put_limit, call_limit,
               limit_price, order_action, trading_class, multiplier
        FROM drafts WHERE id = %s
        """,
        (draft_id,),
    ).fetchone()
    if row is None:
        return []
    (
        symbol, tactic, contracts, phase, reason, account_id,
        expiry, put_strike, call_strike, put_limit, call_limit,
        limit_price, order_action, trading_class, multiplier,
    ) = row
    lines = [
        f"{symbol} {tactic}",
        account_id,
        f"{contracts} contract" if contracts == 1 else f"{contracts} contracts",
        _risk_line(conn, account_id, int(contracts), symbol, tactic, multiplier or "100"),
        f"Phase {phase}",
    ]
    tags = _tag_line(conn, account_id, symbol)
    if tags:
        lines.append(tags)
    if expiry is not None:
        lines.append(f"Expiry {expiry.isoformat()}")
    if trading_class:
        lines.append(f"Class {trading_class} × {multiplier}")
    if order_action == "CANCEL":
        lines.append("Cancels the working sells.")
    if put_strike is not None:
        lines.append(f"Put {_num(put_strike)} limit {_num(put_limit)}")
    if call_strike is not None:
        lines.append(f"Call {_num(call_strike)} limit {_num(call_limit)}")
    if put_strike is None and call_strike is None and limit_price is not None:
        lines.append(f"Limit {_num(limit_price)}")
    if reason:
        lines.append(reason)
    return lines


_TACTIC_FLAG = {
    "OPEN_CSP": "csp",
    "OPEN_CC": "cc",
    "OPEN_STRANGLE": "strangle",
}


def _tactic_on(settings: SettingsPayload, symbol: str, tactic: str, reason: str = "") -> bool:
    """A leg is allowed when its own toggle is on. A strangle leg follows the strangle toggle."""
    flags = settings.tactic_flags.get(symbol.upper())
    if flags is None:
        return True
    if reason == "signaled_strangle" and flags.strangle and tactic in {"OPEN_CSP", "OPEN_CC"}:
        return True
    if tactic == "BUY":
        from athena_strategy.wheel import may_buy

        return may_buy(flags)
    flag = _TACTIC_FLAG.get(tactic)
    if flag is None:
        return True
    return bool(getattr(flags, flag))


def _has_short(conn: psycopg.Connection, account_id: str, symbol: str) -> bool:
    working = conn.execute(
        """
        SELECT 1 FROM orders
        WHERE account_id = %s AND symbol = %s AND status = 'submitted' AND action = 'SELL'
        LIMIT 1
        """,
        (account_id, symbol),
    ).fetchone()
    if working is not None:
        return True
    row = conn.execute(
        """
        SELECT quantity FROM positions
        WHERE account_id = %s AND symbol = %s AND sec_type = 'OPT'
        ORDER BY snapshot_at DESC, id DESC
        LIMIT 1
        """,
        (account_id, symbol),
    ).fetchone()
    return row is not None and row[0] < 0


def _opening_block(
    conn,
    settings: SettingsPayload,
    account_id: str,
    symbol: str,
    tactic: str,
    contracts: int,
    multiplier: str,
    simulated: bool,
) -> str | None:
    if settings.risk.strict_conflict and _has_short(conn, account_id, symbol):
        return "conflicting short on this symbol"
    if tactic == "OPEN_CC":
        cover = _cover_reason(conn, account_id, symbol, contracts, multiplier, simulated)
        if cover:
            return cover
    return _conflict_reason(conn, account_id, symbol, tactic, simulated)


def _cover_reason(
    conn, account_id: str, symbol: str, contracts: int, multiplier: str, simulated: bool,
) -> str | None:
    mult = _chain_multiplier(multiplier)
    shares = _share_quantity(conn, account_id, symbol, simulated)
    used = _held_call_contracts(conn, account_id, symbol, simulated)
    remaining = int(shares // mult) - used
    if contracts > remaining:
        need = (used + contracts) * mult
        return f"I1: cc needs {_whole(need)} covered shares, held {_whole(shares)}"
    return None


def _conflict_reason(conn, account_id: str, symbol: str, tactic: str, simulated: bool) -> str | None:
    short_put, _short_call, any_leg = _option_flags(conn, account_id, symbol, simulated)
    if tactic == "OPEN_CSP" and short_put:
        return "I3: duplicate short put"
    if tactic == "OPEN_STRANGLE" and any_leg:
        return "I3: option leg already open on this symbol"
    return None


def _share_quantity(conn, account_id: str, symbol: str, simulated: bool) -> Decimal:
    row = conn.execute(
        """
        SELECT quantity FROM positions
        WHERE account_id = %s AND symbol = %s AND sec_type = 'STK' AND simulated = %s
        ORDER BY snapshot_at DESC, id DESC
        LIMIT 1
        """,
        (account_id, symbol.upper(), simulated),
    ).fetchone()
    if row is None or row[0] is None or row[0] <= 0:
        return Decimal(0)
    return Decimal(row[0])


def _held_call_contracts(conn, account_id: str, symbol: str, simulated: bool) -> int:
    rows = conn.execute(
        """
        SELECT DISTINCT ON (expiry, contract_right, strike) quantity, contract_right
        FROM positions
        WHERE account_id = %s AND symbol = %s AND sec_type = 'OPT' AND simulated = %s
        ORDER BY expiry, contract_right, strike, snapshot_at DESC
        """,
        (account_id, symbol.upper(), simulated),
    ).fetchall()
    held = 0
    for quantity, right in rows:
        if right == "C" and quantity is not None and quantity < 0:
            held += int(abs(quantity))
    working = conn.execute(
        """
        SELECT coalesce(sum(contracts), 0) FROM orders
        WHERE account_id = %s AND symbol = %s AND simulated = %s
          AND status IN ('submitted', 'queued') AND action = 'SELL' AND contract_right = 'C'
        """,
        (account_id, symbol.upper(), simulated),
    ).fetchone()[0]
    pending = conn.execute(
        """
        SELECT coalesce(sum(contracts), 0) FROM drafts
        WHERE account_id = %s AND symbol = %s AND simulated = %s
          AND status = 'pending' AND tactic = 'OPEN_CC'
        """,
        (account_id, symbol.upper(), simulated),
    ).fetchone()[0]
    return held + int(working) + int(pending)


def _option_flags(conn, account_id: str, symbol: str, simulated: bool) -> tuple[bool, bool, bool]:
    rows = conn.execute(
        """
        SELECT DISTINCT ON (expiry, contract_right, strike) quantity, contract_right
        FROM positions
        WHERE account_id = %s AND symbol = %s AND sec_type = 'OPT' AND simulated = %s
        ORDER BY expiry, contract_right, strike, snapshot_at DESC
        """,
        (account_id, symbol.upper(), simulated),
    ).fetchall()
    short_put = False
    short_call = False
    any_leg = False
    for quantity, right in rows:
        if quantity is None or quantity == 0:
            continue
        any_leg = True
        if quantity < 0 and right == "P":
            short_put = True
        if quantity < 0 and right == "C":
            short_call = True
    orders = conn.execute(
        """
        SELECT contract_right FROM orders
        WHERE account_id = %s AND symbol = %s AND simulated = %s
          AND status IN ('submitted', 'queued') AND action = 'SELL'
        """,
        (account_id, symbol.upper(), simulated),
    ).fetchall()
    for (right,) in orders:
        any_leg = True
        if right == "P":
            short_put = True
        if right == "C":
            short_call = True
    drafts = conn.execute(
        """
        SELECT tactic FROM drafts
        WHERE account_id = %s AND symbol = %s AND simulated = %s
          AND status IN ('pending', 'approved')
          AND tactic IN ('OPEN_CSP', 'OPEN_CC', 'OPEN_STRANGLE')
        """,
        (account_id, symbol.upper(), simulated),
    ).fetchall()
    for (tactic,) in drafts:
        any_leg = True
        if tactic in {"OPEN_CSP", "OPEN_STRANGLE"}:
            short_put = True
        if tactic in {"OPEN_CC", "OPEN_STRANGLE"}:
            short_call = True
    return short_put, short_call, any_leg


def _chain_multiplier(multiplier: str) -> Decimal:
    try:
        mult = Decimal(multiplier or "100")
    except Exception:
        return Decimal(100)
    if mult <= 0:
        return Decimal(100)
    return mult


def _whole(value: Decimal) -> str:
    if value == value.to_integral_value():
        return str(int(value))
    return format(value, "f")


def apply_ticket_block(
    conn, settings: SettingsPayload, account_id: str, symbol: str, draft_ids: list[str],
) -> str | None:
    """Block every still-pending leg when the ticket together breaches a cap."""
    if not draft_ids:
        return None
    rows = conn.execute(
        """
        SELECT id, tactic, contracts, limit_price, put_strike, call_strike, multiplier
        FROM drafts
        WHERE id = ANY(%s) AND status = 'pending'
        """,
        (draft_ids,),
    ).fetchall()
    if not rows:
        return None
    from athena_exec.broker import stock_venue

    simulated = _symbol_in_replay(conn, account_id, symbol)
    price = last_tradeable_price(conn, symbol)
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
    orders = conn.execute(
        """
        SELECT count(*) FROM orders
        WHERE account_id = %s AND upper(symbol) = %s
          AND status <> 'blocked' AND created_at::date = CURRENT_DATE
        """,
        (account_id, symbol.upper()),
    ).fetchone()[0]
    rates = {}
    if mark is not None and mark[3]:
        rates = {str(key): Decimal(str(value)) for key, value in mark[3].items()}
    reason = block_ticket_reason(
        [_ticket_leg(row) for row in rows],
        settings.risk,
        {
            "nav": None if nav is None else nav[0],
            "cash": None if mark is None else mark[0],
            "excess": None if mark is None or len(mark) < 5 else mark[4],
            "margin": None if mark is None else mark[1],
            "base": "" if mark is None or not mark[2] else mark[2],
            "rates": rates,
            "orders_today": int(orders),
            "price": price,
            "currency": stock_venue(symbol)[1],
        },
        _namespaced_exposure(conn, account_id, symbol, price, simulated) if price is not None else Decimal(0),
    )
    if reason is None:
        return None
    conn.execute(
        """
        UPDATE drafts SET status = 'blocked', reason = %s
        WHERE id = ANY(%s) AND status = 'pending'
        """,
        (reason, [row[0] for row in rows]),
    )
    conn.commit()
    note = "ticket cash need would breach reserve" if "cash_reserve" in reason else ""
    for row in rows:
        draft_id, tactic, contracts, limit_price, put_strike, call_strike, multiplier = row
        need = draft_cash_need(
            tactic, int(contracts), limit_price=limit_price, put_strike=put_strike, multiplier=multiplier or "100",
        )
        _note_risk_block(
            conn, account_id, draft_id, symbol, tactic, int(contracts), reason,
            limit_price=limit_price, put_strike=put_strike, call_strike=call_strike,
            multiplier=multiplier or "100", cash_need=need, note=note,
            orders_today=int(orders),
            max_orders_per_day=settings.risk.max_orders_per_day,
        )
    return reason


def _ticket_leg(row) -> dict:
    _draft_id, tactic, contracts, limit_price, put_strike, call_strike, multiplier = row
    return {
        "tactic": tactic,
        "contracts": int(contracts),
        "limit": None if limit_price is None else Decimal(limit_price),
        "put_strike": None if put_strike is None else Decimal(put_strike),
        "call_strike": None if call_strike is None else Decimal(call_strike),
        "multiplier": _chain_multiplier(multiplier),
        "orders": 2 if tactic == "OPEN_STRANGLE" else 1,
    }


def _namespaced_exposure(conn, account_id: str, symbol: str, price: Decimal, simulated: bool) -> Decimal:
    rows = conn.execute(
        """
        SELECT DISTINCT ON (sec_type, expiry, contract_right, strike)
            quantity, sec_type, multiplier
        FROM positions
        WHERE account_id = %s AND symbol = %s AND simulated = %s
        ORDER BY sec_type, expiry, contract_right, strike, snapshot_at DESC
        """,
        (account_id, symbol.upper(), simulated),
    ).fetchall()
    total = Decimal(0)
    for quantity, sec_type, multiplier in rows:
        qty = abs(Decimal(quantity))
        if sec_type == "OPT":
            total += qty * Decimal(multiplier or 100) * price
        else:
            total += qty * price
    return total


def _guard_inputs(conn, account_id: str, symbol: str, expiry, opening: bool) -> dict:
    orders = conn.execute(
        """
        SELECT count(*) FROM orders
        WHERE account_id = %s AND upper(symbol) = %s
          AND status <> 'blocked' AND created_at::date = CURRENT_DATE
        """,
        (account_id, symbol.upper()),
    ).fetchone()[0]
    found = {"orders_today": int(orders)}
    if not opening:
        return found
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
    earnings = conn.execute(
        """
        SELECT event_date FROM calendar_marks
        WHERE kind = 'EARNINGS' AND symbol = %s
        ORDER BY event_date
        """,
        (symbol,),
    ).fetchall()
    from athena_strategy.simbars import replay_cursor

    today = replay_cursor(conn, symbol) or date.today()
    if isinstance(expiry, str) and expiry:
        try:
            expiry = date.fromisoformat(expiry[:10])
        except ValueError:
            expiry = None
    nearest = None
    if earnings:
        offsets = [(row[0] - today).days for row in earnings]
        nearest = min(offsets, key=lambda days: (abs(days), days))
    dte = None if expiry is None else (expiry - today).days
    symbol_value, fx_reason = _symbol_value_in_book(conn, account_id, symbol, mark)
    found.update(
        {
            "nav": None if nav is None else nav[0],
            "symbol_value": symbol_value,
            "cash": None if mark is None else mark[0],
            "margin": None if mark is None else mark[1],
            "excess": None if mark is None or len(mark) < 5 else mark[4],
            "dte": dte,
            "earnings_in_days": nearest,
        }
    )
    if fx_reason:
        found["fx_reason"] = fx_reason
    return found


def _symbol_value_in_book(conn, account_id: str, symbol: str, mark) -> tuple[Decimal | None, str | None]:
    """Existing symbol notional in the book currency."""
    raw = _symbol_value(conn, account_id, symbol)
    if raw is None:
        return None, None
    value = Decimal(raw)
    base = "" if mark is None or not mark[2] else str(mark[2])
    if not base:
        return value, None
    from athena_exec.broker import stock_venue

    source = stock_venue(symbol)[1]
    rates = {}
    if mark[3]:
        rates = {str(key): Decimal(str(item)) for key, item in mark[3].items()}
    return book_amount(value, source, base, rates)


def _symbol_value(conn, account_id: str, symbol: str):
    row = conn.execute(
        """
        SELECT quantity, avg_price FROM positions
        WHERE account_id = %s AND symbol = %s
        ORDER BY snapshot_at DESC, id DESC
        LIMIT 1
        """,
        (account_id, symbol),
    ).fetchone()
    if row is None:
        return None
    return abs(row[0] * row[1])


def _stored_iv_rank(conn, account_id: str, symbol: str) -> int | None:
    """Premium-ratio rank only. Not passed into the order gate."""
    from athena_strategy.iv import PREMIUM_METHOD, iv_rank, iv_samples

    samples = iv_samples(conn, symbol, PREMIUM_METHOD)
    if not samples:
        return None
    minimum = load_active(conn, account_id).strategy.ivr_min_samples
    return iv_rank(samples, samples[-1], minimum)


def _tag_line(conn: psycopg.Connection, account_id: str, symbol: str) -> str:
    row = conn.execute(
        "SELECT tags FROM symbol_phases WHERE account_id = %s AND symbol = %s",
        (account_id, symbol),
    ).fetchone()
    if row is None or not row[0]:
        return ""
    return f"Tags {row[0]}"


def open_quantity(
    conn: psycopg.Connection,
    settings: SettingsPayload,
    account_id: str,
    symbol: str,
    multiplier: str,
) -> int:
    """Contracts for a new open. One when the money cap is off.

    A missing price is not one contract. The position cap cannot be checked.
    Position money is always in the account book currency (FX applied).
    """
    limits = settings.risk
    if limits.max_position_per_symbol <= 0:
        return 1
    price = last_tradeable_price(conn, symbol)
    if price is None or price <= 0:
        return 0
    unit, existing, err = position_in_book(conn, account_id, symbol, price, multiplier)
    if err or unit is None or existing is None:
        return 0
    return allowed_open(limits, unit, Decimal(1), existing)


def position_in_book(
    conn: psycopg.Connection,
    account_id: str,
    symbol: str,
    price: Decimal,
    multiplier: str,
) -> tuple[Decimal | None, Decimal | None, str | None]:
    """One-unit cost and existing exposure in book currency.

    Returns (unit, existing, error). error is set when an FX rate is missing.
    With no book base yet, local ticker amounts are used as-is.
    """
    from athena_exec.broker import stock_venue

    mult = Decimal(multiplier or "100")
    if mult <= 0:
        mult = Decimal(100)
    local_one = price * mult
    local_existing = _exposure(conn, account_id, symbol, price)
    return book_position_units(
        local_one,
        local_existing,
        stock_venue(symbol)[1],
        *_account_fx(conn, account_id),
    )


def book_position_units(
    local_one: Decimal,
    local_existing: Decimal,
    currency: str,
    base: str,
    rates: dict,
) -> tuple[Decimal | None, Decimal | None, str | None]:
    """Convert local one-unit and existing notionals into the book currency."""
    if not base:
        return local_one, local_existing, None
    one, gap = book_amount(local_one, currency, base, rates)
    if gap:
        return None, None, gap
    if one is None:
        return None, None, f"missing fx rate {currency}"
    existing, gap2 = book_amount(local_existing, currency, base, rates)
    if gap2:
        return None, None, gap2
    if existing is None:
        return None, None, f"missing fx rate {currency}"
    return one, existing, None


def _account_fx(conn, account_id: str) -> tuple[str, dict]:
    mark = conn.execute(
        """
        SELECT base_currency, fx_rates
        FROM account_marks WHERE account_id = %s ORDER BY day DESC LIMIT 1
        """,
        (account_id,),
    ).fetchone()
    if mark is None:
        return "", {}
    base = "" if not mark[0] else str(mark[0])
    rates = {}
    if mark[1]:
        rates = {str(key): Decimal(str(value)) for key, value in mark[1].items()}
    return base, rates


def _symbol_in_replay(conn: psycopg.Connection, account_id: str, symbol: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM sim_runs
        WHERE account_id = %s AND status IN ('active', 'halted')
          AND position(',' || %s || ',' in ',' || symbols || ',') > 0
        LIMIT 1
        """,
        (account_id, symbol.upper()),
    ).fetchone()
    return row is not None


def last_tradeable_price(conn: psycopg.Connection, symbol: str) -> Decimal | None:
    """Replay close, otherwise the live intraday print or the daily close.

    Live rails and IV marks do not use this. They use the merged daily series.
    """
    from athena_marketdata.pricing import last_tradeable_price as market_price
    from athena_strategy.simbars import in_replay, last_close

    if in_replay(conn, symbol):
        close = last_close(conn, symbol)
        if close is None or close <= 0:
            return None
        return Decimal(close)
    return market_price(conn, symbol)


def _exposure(conn: psycopg.Connection, account_id: str, symbol: str, price: Decimal) -> Decimal:
    """Share value plus option contracts times price times multiplier."""
    rows = conn.execute(
        """
        SELECT DISTINCT ON (sec_type, expiry, contract_right, strike)
            quantity, sec_type, multiplier
        FROM positions
        WHERE account_id = %s AND symbol = %s
        ORDER BY sec_type, expiry, contract_right, strike, snapshot_at DESC
        """,
        (account_id, symbol),
    ).fetchall()
    total = Decimal(0)
    for quantity, sec_type, multiplier in rows:
        qty = abs(Decimal(quantity))
        if sec_type == "OPT":
            total += qty * Decimal(multiplier or 100) * price
        else:
            total += qty * price
    return total


def _position_inputs(conn, account_id: str, symbol: str, multiplier: str) -> dict:
    price = last_tradeable_price(conn, symbol)
    if price is None or price <= 0:
        if load_active(conn, account_id).risk.max_position_per_symbol > 0:
            return {"mark_reason": "missing mark"}
        return {}
    if load_active(conn, account_id).risk.max_position_per_symbol <= 0:
        mult = Decimal(multiplier or "100")
        if mult <= 0:
            mult = Decimal(100)
        return {"price": price, "multiplier": mult, "existing": _exposure(conn, account_id, symbol, price)}
    unit, existing, err = position_in_book(conn, account_id, symbol, price, multiplier)
    if err:
        return {"mark_reason": err}
    if unit is None or existing is None:
        return {"mark_reason": "missing fx rate"}
    return {"price": unit, "multiplier": Decimal(1), "existing": existing}


def _risk_line(
    conn: psycopg.Connection,
    account_id: str,
    contracts: int,
    symbol: str = "",
    tactic: str = "",
    multiplier: str = "100",
) -> str:
    opening = tactic in {"OPEN_STRANGLE", "OPEN_CSP", "OPEN_CC", "BUY"}
    extra = _position_inputs(conn, account_id, symbol, multiplier) if opening else {}
    reason = block_reason(contracts, load_active(conn, account_id).risk, opening=opening, **extra)
    if reason is None:
        return "Risk pass"
    return f"Risk fail. {reason}"


def _num(value: object) -> str:
    if value is None:
        return "none"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def propose(
    conn: psycopg.Connection,
    settings: SettingsPayload,
    symbol: str,
    tactic: str,
    contracts: int,
    reason: str,
    phase: str,
    account_id: str,
    instrument: str | None = None,
    limit_price=None,
    expiry=None,
    put_strike=None,
    call_strike=None,
    put_limit=None,
    call_limit=None,
    trading_class: str = "",
    option_exchange: str = "",
    multiplier: str = "100",
    order_action: str = "",
) -> str | None:
    """Insert a draft. A guard failure is stored as blocked and returns None."""
    staged = stage_draft(
        conn, settings, symbol, tactic, contracts, reason, phase, account_id,
        instrument=instrument, limit_price=limit_price, expiry=expiry,
        put_strike=put_strike, call_strike=call_strike, put_limit=put_limit, call_limit=call_limit,
        trading_class=trading_class, option_exchange=option_exchange, multiplier=multiplier,
        order_action=order_action,
    )
    if staged["status"] != "pending":
        return None
    return staged["id"]


def stage_draft(
    conn: psycopg.Connection,
    settings: SettingsPayload,
    symbol: str,
    tactic: str,
    contracts: int,
    reason: str,
    phase: str,
    account_id: str,
    instrument: str | None = None,
    limit_price=None,
    expiry=None,
    put_strike=None,
    call_strike=None,
    put_limit=None,
    call_limit=None,
    trading_class: str = "",
    option_exchange: str = "",
    multiplier: str = "100",
    order_action: str = "",
    ticket_id: str | None = None,
    leg_index: int | None = None,
    forced_block: str | None = None,
    enforce_contract_cap: bool = True,
) -> dict:
    """Insert a draft and report why. status is pending, blocked, or skipped."""
    if forced_block:
        return _insert_draft(
            conn, symbol, tactic, contracts, "blocked", forced_block, phase, account_id,
            instrument, limit_price, expiry, put_strike, call_strike, put_limit, call_limit,
            trading_class, option_exchange, multiplier, order_action,
            _symbol_in_replay(conn, account_id, symbol), ticket_id, leg_index,
        )
    if symbol.upper() in {item.upper() for item in settings.blocked_symbols}:
        return {"id": None, "status": "skipped", "detail": "symbol blocked", "contracts": contracts, "tactic": tactic}
    if not _tactic_on(settings, symbol, tactic, reason):
        flag = _TACTIC_FLAG.get(tactic, "tactic")
        return {"id": None, "status": "skipped", "detail": f"{flag} is off", "contracts": contracts}
    from athena_strategy.screener import outside_table

    off_table = outside_table(
        conn, settings, account_id, symbol, tactic, expiry, put_strike, call_strike, None, date.today(),
    )
    if off_table:
        return {"id": None, "status": "skipped", "detail": off_table, "contracts": contracts}
    opening = tactic in {"OPEN_STRANGLE", "OPEN_CSP", "OPEN_CC", "BUY"}
    simulated = _symbol_in_replay(conn, account_id, symbol)
    blocked = (
        _opening_block(conn, settings, account_id, symbol, tactic, contracts, multiplier, simulated) if opening else None
    )
    guard: dict = {}
    if blocked is None:
        extra = _position_inputs(conn, account_id, symbol, multiplier) if opening else {}
        guard = _guard_inputs(conn, account_id, symbol, expiry, opening)
        blocked = extra.pop("mark_reason", None) or guard.pop("fx_reason", None)
        if blocked is None:
            need = None
            if tactic in {"OPEN_CSP", "BUY"}:
                need = draft_cash_need(
                    tactic,
                    contracts,
                    limit_price=limit_price,
                    put_strike=put_strike,
                    multiplier=multiplier or "100",
                )
            blocked = block_reason(
                contracts,
                settings.risk,
                opening=opening,
                needs_cash=tactic in {"OPEN_CSP", "BUY"},
                enforce_contract_cap=enforce_contract_cap,
                cash_need=need,
                **guard,
                **extra,
            )
    # Freeze the day-order numbers that fed block_reason (not a later re-fetch).
    orders_snap = guard.get("orders_today")
    max_orders_snap = settings.risk.max_orders_per_day
    if blocked and is_day_orders_reason(blocked) and orders_snap is None:
        orders_snap = _orders_today(conn, account_id, symbol)
    status = "blocked" if blocked else "pending"
    return _insert_draft(
        conn, symbol, tactic, contracts, status, blocked or reason, phase, account_id,
        instrument, limit_price, expiry, put_strike, call_strike, put_limit, call_limit,
        trading_class, option_exchange, multiplier, order_action, simulated, ticket_id, leg_index,
        orders_today=orders_snap if blocked else None,
        max_orders_per_day=max_orders_snap if blocked else None,
    )


def _insert_draft(
    conn, symbol, tactic, contracts, status, reason, phase, account_id,
    instrument, limit_price, expiry, put_strike, call_strike, put_limit, call_limit,
    trading_class, option_exchange, multiplier, order_action, simulated, ticket_id, leg_index,
    *,
    orders_today=None,
    max_orders_per_day=None,
) -> dict:
    draft_id = "draft_" + secrets.token_hex(3)
    conn.execute(
        """
        INSERT INTO drafts (
            id, symbol, tactic, contracts, status, reason, phase, account_id,
            instrument, limit_price, expiry, put_strike, call_strike,
            put_limit, call_limit, trading_class, option_exchange, multiplier, order_action,
            simulated, ticket_id, leg_index
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            draft_id, symbol, tactic, contracts, status, reason, phase, account_id,
            instrument, limit_price, expiry, put_strike, call_strike,
            put_limit, call_limit, trading_class, option_exchange, multiplier, order_action,
            simulated, ticket_id, leg_index,
        ),
    )
    conn.commit()
    if status == "blocked":
        _note_risk_block(
            conn, account_id, draft_id, symbol, tactic, contracts, reason,
            limit_price=limit_price, put_strike=put_strike, call_strike=call_strike,
            multiplier=multiplier,
            orders_today=orders_today,
            max_orders_per_day=max_orders_per_day,
        )
    return {
        "id": draft_id,
        "status": status,
        "detail": reason if status != "pending" else f"pending, size {contracts}",
        "contracts": contracts,
        "tactic": tactic,
    }


def draft_cash_need(
    tactic: str,
    contracts: int,
    *,
    limit_price=None,
    put_strike=None,
    multiplier: str = "100",
) -> Decimal | None:
    """BUY/CSP/strangle cash notional when the marks are present."""
    qty = Decimal(contracts)
    mult = _chain_multiplier(multiplier)
    if tactic == "BUY":
        if limit_price is None:
            return None
        return Decimal(limit_price) * qty
    if tactic in {"OPEN_CSP", "OPEN_STRANGLE"}:
        if put_strike is None:
            return None
        return Decimal(put_strike) * mult * qty
    return Decimal(0)


def cash_reserve_math(
    cash=None,
    nav=None,
    cash_reserve_pct=None,
    *,
    excess=None,
    cash_need=None,
) -> dict:
    """Percent, floor, and shortfall for a cash-reserve comparison.

    Uses ExcessLiquidity when present (margin buffer); otherwise TotalCashValue.
    Percent is post-trade when cash_need is set.
    """
    from athena_risk import reserve_buffer

    out: dict = {
        "cash_pct": None,
        "reserve_floor": None,
        "shortfall": None,
        "buffer": None,
    }
    buffer = reserve_buffer(
        None if excess is None else Decimal(excess),
        None if cash is None else Decimal(cash),
    )
    if buffer is None or nav is None or Decimal(nav) <= 0:
        return out
    need = Decimal(0) if cash_need is None else Decimal(cash_need)
    post = buffer - need
    nav_d = Decimal(nav)
    out["buffer"] = buffer
    out["cash_pct"] = (post / nav_d * Decimal(100)).quantize(Decimal("0.01"))
    if cash_reserve_pct is None:
        return out
    reserve = Decimal(cash_reserve_pct)
    floor = nav_d * reserve / Decimal(100)
    out["reserve_floor"] = floor
    out["shortfall"] = max(Decimal(0), floor - post)
    return out


def risk_block_steps(
    *,
    draft_id: str,
    symbol: str,
    tactic: str,
    contracts: int,
    reason: str,
    cash=None,
    nav=None,
    cash_reserve_pct=None,
    cash_need=None,
    excess=None,
    limit=None,
    put_strike=None,
    call_strike=None,
    note: str = "",
    orders_today=None,
    max_orders_per_day=None,
    max_contracts_per_symbol=None,
    max_position_per_symbol=None,
    max_symbol_pct_nav=None,
    symbol_value=None,
    margin=None,
    max_short_opt_margin_pct=None,
    rv_pctl=None,
    min_rv_pctl_new_short=None,
    dte=None,
    min_dte_new=None,
    earnings_in_days=None,
    earnings_blackout_days=None,
) -> dict:
    """Structured trail payload for a risk-blocked draft."""
    math = cash_reserve_math(
        cash, nav, cash_reserve_pct, excess=excess, cash_need=cash_need,
    )
    cash_pct = math["cash_pct"]
    reserve_floor = math["reserve_floor"]
    shortfall = math["shortfall"]
    if not note and (reason or "") == "cash is below cash_reserve_pct":
        note = "post-trade excess liquidity would breach cash reserve"
    detail = format_risk_block_detail(
        tactic=tactic,
        contracts=contracts,
        cash=cash,
        nav=nav,
        cash_pct=cash_pct,
        cash_reserve_pct=cash_reserve_pct,
        reserve_floor=reserve_floor,
        shortfall=shortfall,
        cash_need=cash_need,
        excess=excess,
        buffer=math.get("buffer"),
        reason=reason,
        note=note,
        orders_today=orders_today,
        max_orders_per_day=max_orders_per_day,
        max_contracts_per_symbol=max_contracts_per_symbol,
        max_position_per_symbol=max_position_per_symbol,
        max_symbol_pct_nav=max_symbol_pct_nav,
        symbol_value=symbol_value,
        margin=margin,
        max_short_opt_margin_pct=max_short_opt_margin_pct,
        rv_pctl=rv_pctl,
        min_rv_pctl_new_short=min_rv_pctl_new_short,
        dte=dte,
        min_dte_new=min_dte_new,
        earnings_in_days=earnings_in_days,
        earnings_blackout_days=earnings_blackout_days,
    )
    short = tactic[5:] if str(tactic).startswith("OPEN_") else tactic
    item = {
        "tactic": short,
        "outcome": "blocked",
        "detail": detail,
        "draft_id": draft_id or "",
        "contracts": int(contracts),
    }
    return {
        "reason": reason,
        "draft_id": draft_id or "",
        "symbol": symbol,
        "tactic": tactic,
        "contracts": int(contracts),
        "limit": None if limit is None else _num(limit),
        "put_strike": None if put_strike is None else _num(put_strike),
        "call_strike": None if call_strike is None else _num(call_strike),
        "cash": None if cash is None else _num(cash),
        "excess": None if excess is None else _num(excess),
        "nav": None if nav is None else _num(nav),
        "cash_pct": None if cash_pct is None else _num(cash_pct),
        "cash_reserve_pct": None if cash_reserve_pct is None else _num(cash_reserve_pct),
        "reserve_floor": None if reserve_floor is None else _num(reserve_floor),
        "shortfall": None if shortfall is None else _num(shortfall),
        "cash_need": None if cash_need is None else _num(cash_need),
        "orders_today": None if orders_today is None else int(orders_today),
        "max_orders_per_day": None if max_orders_per_day is None else int(max_orders_per_day),
        "max_contracts_per_symbol": None if max_contracts_per_symbol is None else int(max_contracts_per_symbol),
        "max_position_per_symbol": None if max_position_per_symbol is None else _num(max_position_per_symbol),
        "max_symbol_pct_nav": None if max_symbol_pct_nav is None else _num(max_symbol_pct_nav),
        "symbol_value": None if symbol_value is None else _num(symbol_value),
        "margin": None if margin is None else _num(margin),
        "max_short_opt_margin_pct": None if max_short_opt_margin_pct is None else _num(max_short_opt_margin_pct),
        "rv_pctl": None if rv_pctl is None else int(rv_pctl),
        "min_rv_pctl_new_short": None if min_rv_pctl_new_short is None else int(min_rv_pctl_new_short),
        "dte": None if dte is None else int(dte),
        "min_dte_new": None if min_dte_new is None else int(min_dte_new),
        "earnings_in_days": None if earnings_in_days is None else int(earnings_in_days),
        "earnings_blackout_days": None if earnings_blackout_days is None else int(earnings_blackout_days),
        "note": note,
        "items": [item],
    }


def format_risk_block_detail(
    *,
    tactic: str,
    contracts: int,
    cash=None,
    nav=None,
    cash_pct=None,
    cash_reserve_pct=None,
    reserve_floor=None,
    shortfall=None,
    cash_need=None,
    excess=None,
    buffer=None,
    reason: str = "",
    note: str = "",
    orders_today=None,
    max_orders_per_day=None,
    max_contracts_per_symbol=None,
    max_position_per_symbol=None,
    max_symbol_pct_nav=None,
    symbol_value=None,
    margin=None,
    max_short_opt_margin_pct=None,
    rv_pctl=None,
    min_rv_pctl_new_short=None,
    dte=None,
    min_dte_new=None,
    earnings_in_days=None,
    earnings_blackout_days=None,
) -> str:
    """Human one-liner for Audit / Recent decisions / History."""
    cash = _as_decimal(cash)
    excess = _as_decimal(excess)
    buffer = _as_decimal(buffer)
    nav = _as_decimal(nav)
    cash_pct = _as_decimal(cash_pct)
    cash_reserve_pct = _as_decimal(cash_reserve_pct)
    reserve_floor = _as_decimal(reserve_floor)
    shortfall = _as_decimal(shortfall)
    cash_need = _as_decimal(cash_need)
    symbol_value = _as_decimal(symbol_value)
    margin = _as_decimal(margin)
    max_symbol_pct_nav = _as_decimal(max_symbol_pct_nav)
    max_short_opt_margin_pct = _as_decimal(max_short_opt_margin_pct)
    max_position_per_symbol = _as_decimal(max_position_per_symbol)
    short = tactic[5:] if str(tactic).startswith("OPEN_") else (tactic or "—")
    parts = [f"{short} {int(contracts)}"]
    key = " ".join((reason or "").replace("_", " ").split()).lower()
    compared = False
    metric = "EL" if excess is not None else "cash"
    shown = buffer if buffer is not None else (excess if excess is not None else cash)
    post = None if shown is None else (shown if cash_need is None else shown - cash_need)

    if "orders" in key and ("max" in key or "exceed" in key or "day" in key):
        if orders_today is not None and max_orders_per_day is not None:
            used = int(orders_today)
            cap = int(max_orders_per_day)
            # Never invent an impossible inequality (e.g. live 2 ≥ max 8).
            if used >= cap:
                parts.append(f"orders today {used} ≥ max {cap}/day")
                compared = True
    elif "contracts exceed" in key or "max contracts" in key:
        if max_contracts_per_symbol is not None:
            parts.append(f"contracts {int(contracts)} > max {int(max_contracts_per_symbol)}")
            compared = True
    elif "position exceed" in key:
        bits = [f"size {int(contracts)}"]
        if max_position_per_symbol is not None:
            bits.append(f"cap {_money_plain(max_position_per_symbol)}")
        parts.append(" · ".join(bits) if len(bits) > 1 else bits[0])
        compared = True
    elif "symbol exceed" in key:
        if symbol_value is not None and nav is not None and nav > 0 and max_symbol_pct_nav is not None:
            pct = symbol_value / nav * Decimal(100)
            parts.append(f"symbol {_num(pct)}% NAV > max {_num(max_symbol_pct_nav)}%")
            compared = True
    elif is_cash_reserve_reason(reason) or ("cash" in key and "reserve" in key):
        if cash_pct is not None and cash_reserve_pct is not None:
            cash_bit = f"{metric} {_num(cash_pct)}%"
            if post is not None:
                cash_bit = f"{cash_bit} ({_money_plain(post)})"
            parts.append(f"{cash_bit} < reserve {_num(cash_reserve_pct)}%")
            if reserve_floor is not None and nav is not None:
                parts.append(f"floor {_money_plain(reserve_floor)} on NAV {_money_plain(nav)}")
            if shortfall is not None:
                parts.append(f"short {_money_plain(shortfall)}")
            compared = True
        elif cash_pct is not None:
            bit = f"{metric} {_num(cash_pct)}% NAV"
            if post is not None:
                bit = f"{bit} ({_money_plain(post)})"
            parts.append(bit)
            compared = True
    elif "margin exceed" in key or "cycle margin" in key:
        if margin is not None and nav is not None and nav > 0 and max_short_opt_margin_pct is not None:
            pct = margin / nav * Decimal(100)
            parts.append(f"margin {_num(pct)}% NAV > max {_num(max_short_opt_margin_pct)}%")
            compared = True
    elif "rv" in key and ("percentile" in key or "pctl" in key or "below" in key):
        if rv_pctl is not None and min_rv_pctl_new_short is not None:
            parts.append(f"rv pctl {int(rv_pctl)} < min {int(min_rv_pctl_new_short)}")
            compared = True
    elif "dte" in key and "below" in key:
        if dte is not None and min_dte_new is not None:
            parts.append(f"dte {int(dte)} < min {int(min_dte_new)}")
            compared = True
    elif "earnings" in key:
        if earnings_in_days is not None and earnings_blackout_days is not None:
            parts.append(
                f"earnings in {int(earnings_in_days)}d ≤ blackout {int(earnings_blackout_days)}d"
            )
            compared = True

    if cash_need is not None and cash_need > 0:
        parts.append(f"need ~${_money_plain(cash_need)}")
        compared = True
    if not compared and note:
        parts.append(note)
    elif not compared and reason:
        parts.append(reason)
    return " · ".join(parts)


def _money_plain(value) -> str:
    amount = Decimal(value)
    if amount == amount.to_integral_value():
        return f"{int(amount):,}"
    return f"{amount:,.2f}"


def _account_cash_marks(
    conn, account_id: str,
) -> tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None]:
    """cash, nav, cash_reserve_pct, excess_liquidity for the active book."""
    nav_row = conn.execute(
        "SELECT last_nav FROM nav_marks WHERE account_id = %s ORDER BY day DESC LIMIT 1",
        (account_id,),
    ).fetchone()
    mark = conn.execute(
        """
        SELECT cash, excess_liquidity
        FROM account_marks WHERE account_id = %s ORDER BY day DESC LIMIT 1
        """,
        (account_id,),
    ).fetchone()
    reserve = None
    try:
        reserve = Decimal(load_active(conn, account_id).risk.cash_reserve_pct)
    except Exception:
        reserve = None
    cash = None if mark is None or mark[0] is None else Decimal(mark[0])
    excess = None if mark is None or len(mark) < 2 or mark[1] is None else Decimal(mark[1])
    nav = None if nav_row is None else Decimal(nav_row[0])
    return cash, nav, reserve, excess


def is_cash_reserve_reason(reason: str) -> bool:
    key = " ".join((reason or "").replace("_", " ").split()).lower()
    return "cash reserve" in key or "cash is below cash reserve pct" in key or "cash_reserve" in (reason or "").lower()


def is_day_orders_reason(reason: str) -> bool:
    key = " ".join((reason or "").replace("_", " ").split()).lower()
    return ("orders" in key and ("max" in key or "exceed" in key or "day" in key)) or key == "cycle orders"


def _orders_today(conn, account_id: str, symbol: str) -> int:
    row = conn.execute(
        """
        SELECT count(*) FROM orders
        WHERE account_id = %s AND upper(symbol) = %s
          AND status <> 'blocked' AND created_at::date = CURRENT_DATE
        """,
        (account_id, symbol.upper()),
    ).fetchone()
    return int(row[0]) if row else 0


def _risk_block_context(conn, account_id: str, symbol: str) -> dict:
    """Marks and limits for a rich risk-block Detail line."""
    settings = load_active(conn, account_id)
    risk = settings.risk
    cash, nav, reserve, excess = _account_cash_marks(conn, account_id)
    mark = conn.execute(
        """
        SELECT margin FROM account_marks
        WHERE account_id = %s ORDER BY day DESC LIMIT 1
        """,
        (account_id,),
    ).fetchone()
    opening = True
    guard = _guard_inputs(conn, account_id, symbol, None, opening)
    return {
        "cash": cash,
        "nav": nav,
        "excess": excess if excess is not None else guard.get("excess"),
        "cash_reserve_pct": reserve,
        "margin": None if mark is None or mark[0] is None else Decimal(mark[0]),
        "orders_today": guard.get("orders_today"),
        "max_orders_per_day": risk.max_orders_per_day,
        "max_contracts_per_symbol": risk.max_contracts_per_symbol,
        "max_position_per_symbol": risk.max_position_per_symbol,
        "max_symbol_pct_nav": risk.max_symbol_pct_nav,
        "max_short_opt_margin_pct": risk.max_short_opt_margin_pct,
        "min_rv_pctl_new_short": risk.min_rv_pctl_new_short,
        "min_dte_new": risk.min_dte_new,
        "earnings_blackout_days": risk.earnings_blackout_days,
        "symbol_value": guard.get("symbol_value"),
        "dte": guard.get("dte"),
        "earnings_in_days": guard.get("earnings_in_days"),
        "rv_pctl": None,
    }


def cash_comparison_view(
    *,
    tactic: str,
    contracts,
    reason: str,
    trail: dict | None = None,
    cash=None,
    nav=None,
    cash_reserve_pct=None,
    cash_need=None,
    excess=None,
    orders_today=None,
    max_orders_per_day=None,
    max_contracts_per_symbol=None,
    max_position_per_symbol=None,
    max_symbol_pct_nav=None,
    symbol_value=None,
    margin=None,
    max_short_opt_margin_pct=None,
    rv_pctl=None,
    min_rv_pctl_new_short=None,
    dte=None,
    min_dte_new=None,
    earnings_in_days=None,
    earnings_blackout_days=None,
) -> dict:
    """Modal/detail fields for a risk block. Fills math when marks exist.

    Empty money fields are "" so the UI can map them to a dash.
    """
    payload = dict(trail or {})

    def _pick(override, key):
        if override is not None:
            return override
        return payload.get(key)

    cash_v = cash if cash is not None else _as_decimal(payload.get("cash"))
    nav_v = nav if nav is not None else _as_decimal(payload.get("nav"))
    excess_v = excess if excess is not None else _as_decimal(payload.get("excess"))
    reserve_v = cash_reserve_pct if cash_reserve_pct is not None else _as_decimal(payload.get("cash_reserve_pct"))
    need_v = cash_need if cash_need is not None else _as_decimal(payload.get("cash_need"))
    orders_v = orders_today if orders_today is not None else _pick(None, "orders_today")
    max_orders_v = max_orders_per_day if max_orders_per_day is not None else _pick(None, "max_orders_per_day")
    max_contracts_v = (
        max_contracts_per_symbol if max_contracts_per_symbol is not None
        else _pick(None, "max_contracts_per_symbol")
    )
    max_position_v = (
        max_position_per_symbol if max_position_per_symbol is not None
        else _as_decimal(payload.get("max_position_per_symbol"))
    )
    max_symbol_v = (
        max_symbol_pct_nav if max_symbol_pct_nav is not None
        else _as_decimal(payload.get("max_symbol_pct_nav"))
    )
    symbol_value_v = symbol_value if symbol_value is not None else _as_decimal(payload.get("symbol_value"))
    margin_v = margin if margin is not None else _as_decimal(payload.get("margin"))
    max_margin_v = (
        max_short_opt_margin_pct if max_short_opt_margin_pct is not None
        else _as_decimal(payload.get("max_short_opt_margin_pct"))
    )
    rv_v = rv_pctl if rv_pctl is not None else _pick(None, "rv_pctl")
    min_rv_v = min_rv_pctl_new_short if min_rv_pctl_new_short is not None else _pick(None, "min_rv_pctl_new_short")
    dte_v = dte if dte is not None else _pick(None, "dte")
    min_dte_v = min_dte_new if min_dte_new is not None else _pick(None, "min_dte_new")
    earn_v = earnings_in_days if earnings_in_days is not None else _pick(None, "earnings_in_days")
    blackout_v = (
        earnings_blackout_days if earnings_blackout_days is not None
        else _pick(None, "earnings_blackout_days")
    )
    math = cash_reserve_math(cash_v, nav_v, reserve_v, excess=excess_v, cash_need=need_v)
    qty = 0 if contracts is None else int(contracts)
    detail = ""
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    if items and isinstance(items[0], dict):
        detail = items[0].get("detail") or ""
    frozen_day_orders = (
        is_day_orders_reason(reason)
        and payload.get("orders_today") is not None
        and payload.get("max_orders_per_day") is not None
        and bool(detail)
    )
    rebuilt = format_risk_block_detail(
        tactic=tactic,
        contracts=qty,
        cash=cash_v,
        nav=nav_v,
        cash_pct=math["cash_pct"],
        cash_reserve_pct=reserve_v,
        reserve_floor=math["reserve_floor"],
        shortfall=math["shortfall"],
        cash_need=need_v,
        excess=excess_v,
        buffer=math.get("buffer"),
        reason=reason,
        note=str(payload.get("note") or ""),
        orders_today=orders_v,
        max_orders_per_day=max_orders_v,
        max_contracts_per_symbol=max_contracts_v,
        max_position_per_symbol=max_position_v,
        max_symbol_pct_nav=max_symbol_v,
        symbol_value=symbol_value_v,
        margin=margin_v,
        max_short_opt_margin_pct=max_margin_v,
        rv_pctl=rv_v,
        min_rv_pctl_new_short=min_rv_v,
        dte=dte_v,
        min_dte_new=min_dte_v,
        earnings_in_days=earn_v,
        earnings_blackout_days=blackout_v,
    )
    # Keep the frozen day-orders Detail from the trail; do not rebuild over it
    # with live counts that no longer match the block.
    if frozen_day_orders:
        pass
    elif rebuilt and rebuilt != f"{(tactic[5:] if str(tactic).startswith('OPEN_') else (tactic or '—'))} {qty} · {reason}":
        detail = rebuilt
    elif not detail:
        detail = rebuilt or reason or ""
    # Prefer trail-frozen day-order fields when present.
    if payload.get("orders_today") is not None and orders_today is None:
        orders_v = payload.get("orders_today")
    if payload.get("max_orders_per_day") is not None and max_orders_per_day is None:
        max_orders_v = payload.get("max_orders_per_day")
    return {
        "cash": "" if cash_v is None else _num(cash_v),
        "excess": "" if excess_v is None else _num(excess_v),
        "nav": "" if nav_v is None else _num(nav_v),
        "cash_pct": "" if math["cash_pct"] is None else _num(math["cash_pct"]),
        "cash_reserve_pct": "" if reserve_v is None else _num(reserve_v),
        "reserve_floor": "" if math["reserve_floor"] is None else _num(math["reserve_floor"]),
        "shortfall": "" if math["shortfall"] is None else _num(math["shortfall"]),
        "cash_need": "" if need_v is None else _num(need_v),
        "orders_today": "" if orders_v is None else str(int(orders_v)),
        "max_orders_per_day": "" if max_orders_v is None else str(int(max_orders_v)),
        "detail": detail,
    }


def _as_decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _note_risk_block(
    conn,
    account_id: str,
    draft_id: str,
    symbol: str,
    tactic: str,
    contracts: int,
    reason: str,
    *,
    limit_price=None,
    put_strike=None,
    call_strike=None,
    multiplier: str = "100",
    cash_need: Decimal | None = None,
    note: str = "",
    orders_today=None,
    max_orders_per_day=None,
) -> None:
    from athena_strategy.agents import write_risk_block

    ctx = _risk_block_context(conn, account_id, symbol)
    need = cash_need
    if need is None:
        need = draft_cash_need(
            tactic, contracts, limit_price=limit_price, put_strike=put_strike, multiplier=multiplier,
        )
    # Prefer the snapshot from block_reason / ticket guard over a re-fetched context.
    frozen_orders = ctx.get("orders_today") if orders_today is None else int(orders_today)
    frozen_max = ctx.get("max_orders_per_day") if max_orders_per_day is None else int(max_orders_per_day)
    steps = risk_block_steps(
        draft_id=draft_id,
        symbol=symbol,
        tactic=tactic,
        contracts=contracts,
        reason=reason,
        cash=ctx.get("cash"),
        nav=ctx.get("nav"),
        excess=ctx.get("excess"),
        cash_reserve_pct=ctx.get("cash_reserve_pct"),
        cash_need=need,
        limit=limit_price,
        put_strike=put_strike,
        call_strike=call_strike,
        note=note,
        orders_today=frozen_orders,
        max_orders_per_day=frozen_max,
        max_contracts_per_symbol=ctx.get("max_contracts_per_symbol"),
        max_position_per_symbol=ctx.get("max_position_per_symbol"),
        max_symbol_pct_nav=ctx.get("max_symbol_pct_nav"),
        symbol_value=ctx.get("symbol_value"),
        margin=ctx.get("margin"),
        max_short_opt_margin_pct=ctx.get("max_short_opt_margin_pct"),
        rv_pctl=ctx.get("rv_pctl"),
        min_rv_pctl_new_short=ctx.get("min_rv_pctl_new_short"),
        dte=ctx.get("dte"),
        min_dte_new=ctx.get("min_dte_new"),
        earnings_in_days=ctx.get("earnings_in_days"),
        earnings_blackout_days=ctx.get("earnings_blackout_days"),
    )
    write_risk_block(conn, account_id, symbol, reason, steps)


def issue_link(conn: psycopg.Connection, draft_id: str, ttl_minutes: int) -> str:
    token = secrets.token_urlsafe(18)
    expires = datetime.now(UTC) + timedelta(minutes=ttl_minutes)
    conn.execute(
        """
        INSERT INTO control_links (token, draft_id, expires_at)
        VALUES (%s, %s, %s)
        """,
        (token, draft_id, expires),
    )
    conn.commit()
    return token


def apply_amend(conn: psycopg.Connection, draft_id: str, fields: dict) -> None:
    """Change quantity, limits, and a listed strike or expiry. The tactic stays."""
    row = conn.execute(
        """
        SELECT account_id, symbol, tactic, status, expiry, put_limit, call_limit,
               limit_price, put_strike, call_strike, multiplier
        FROM drafts WHERE id = %s
        """,
        (draft_id,),
    ).fetchone()
    if row is None:
        raise ValueError("unknown draft")
    account_id, symbol, tactic, status, expiry, put_limit, call_limit, limit_price, put_strike, call_strike, multiplier = row
    if status != "pending":
        raise ValueError("draft is not pending")
    settings = load_active(conn, account_id)
    updates: dict[str, object] = {}
    if fields.get("contracts") is not None:
        new_contracts = int(fields["contracts"])
        opening = tactic in {"OPEN_STRANGLE", "OPEN_CSP", "OPEN_CC", "BUY"}
        extra = _position_inputs(conn, account_id, symbol, multiplier or "100") if opening else {}
        reason = extra.pop("mark_reason", None)
        if reason is None:
            reason = block_reason(new_contracts, settings.risk, opening=opening, **extra)
        if reason:
            raise AmendError(reason)
        updates["contracts"] = new_contracts
    leg_limits = {
        "put_limit": put_limit,
        "call_limit": call_limit,
    }
    for key, current in leg_limits.items():
        if fields.get(key) is None:
            continue
        if current is None:
            raise AmendError("this draft has no " + key.replace("_", " "))
        value = Decimal(fields[key])
        if value <= 0:
            raise AmendError("limit must be above 0")
        updates[key] = value
    if fields.get("limit_price") is not None and put_strike is None and call_strike is None:
        value = Decimal(fields["limit_price"])
        if value <= 0:
            raise AmendError("limit must be above 0")
        if limit_price is None:
            raise AmendError("this draft has no limit")
        updates["limit_price"] = value
    _apply_listed(conn, settings, account_id, symbol, tactic, expiry, put_strike, call_strike, fields, updates)
    if not updates:
        return
    assignments = ", ".join(f"{key} = %s" for key in updates)
    conn.execute(
        f"UPDATE drafts SET {assignments} WHERE id = %s",
        (*updates.values(), draft_id),
    )


def _apply_listed(conn, settings: SettingsPayload, account_id: str, symbol: str, tactic: str, expiry, put_strike, call_strike, fields: dict, updates: dict) -> None:
    moving = any(fields.get(key) is not None for key in ("expiry", "put_strike", "call_strike"))
    if not moving:
        return
    if tactic not in {"OPEN_STRANGLE", "OPEN_CSP", "OPEN_CC"}:
        raise AmendError("strike and expiry stay on this draft")
    final_expiry = fields["expiry"] if fields.get("expiry") is not None else expiry
    final_put = fields["put_strike"] if fields.get("put_strike") is not None else put_strike
    final_call = fields["call_strike"] if fields.get("call_strike") is not None else call_strike
    if final_put is not None and not _listed(conn, settings, account_id, symbol, tactic, final_expiry, "P", final_put):
        raise AmendError("that put is not listed for this expiry")
    if final_call is not None and not _listed(conn, settings, account_id, symbol, tactic, final_expiry, "C", final_call):
        raise AmendError("that call is not listed for this expiry")
    if final_expiry != expiry:
        updates["expiry"] = final_expiry
    if final_put != put_strike:
        updates["put_strike"] = final_put
    if final_call != call_strike:
        updates["call_strike"] = final_call


def _amend_cap(conn, settings: SettingsPayload, account_id: str, symbol: str, tactic: str, multiplier: str) -> int:
    cap = settings.risk.max_contracts_per_symbol
    if tactic not in {"OPEN_STRANGLE", "OPEN_CSP", "OPEN_CC"} or settings.risk.max_position_per_symbol <= 0:
        return cap
    price = last_tradeable_price(conn, symbol)
    if price is None:
        return cap
    unit, existing, err = position_in_book(conn, account_id, symbol, price, multiplier)
    if err or unit is None or existing is None:
        return 0
    return allowed_open(settings.risk, unit, Decimal(1), existing)


def _listed(conn, settings: SettingsPayload, account_id: str, symbol: str, tactic: str, expiry, right: str, strike) -> bool:
    if expiry is None or strike is None:
        return False
    from athena_strategy.screener import outside_table

    put_strike = strike if right == "P" else None
    call_strike = strike if right == "C" else None
    reason = outside_table(
        conn, settings, account_id, symbol, tactic, expiry, put_strike, call_strike, None, date.today(),
    )
    if reason:
        return False
    from athena_strategy.screener import has_choices, screener_enabled

    if screener_enabled() and has_choices(conn, symbol):
        return True
    from athena_strategy.screener import load_choices

    wanted = strike if isinstance(strike, Decimal) else Decimal(str(strike))
    wanted_day = expiry if isinstance(expiry, date) else date.fromisoformat(str(expiry)[:10])
    return any(
        choice.expiry == wanted_day and choice.right == right and choice.strike == wanted
        for choice in load_choices(conn, symbol)
    )


def amend_view(conn: psycopg.Connection, draft_id: str) -> dict | None:
    """Fields the approve page may edit. None when the draft is missing."""
    facts = draft_facts(conn, draft_id)
    if not facts:
        return None
    row = conn.execute(
        """
        SELECT account_id, symbol, tactic, contracts, expiry, put_limit, call_limit,
               limit_price, put_strike, call_strike, multiplier
        FROM drafts WHERE id = %s
        """,
        (draft_id,),
    ).fetchone()
    if row is None:
        return None
    account_id, symbol, tactic, contracts, expiry, put_limit, call_limit, limit_price, put_strike, call_strike, multiplier = row
    settings = load_active(conn, account_id)
    stock_limit = None if put_strike is not None or call_strike is not None else limit_price
    expiries: list[date] = []
    puts: list[tuple[date, Decimal]] = []
    calls: list[tuple[date, Decimal]] = []
    if tactic in {"OPEN_STRANGLE", "OPEN_CSP", "OPEN_CC"}:
        from athena_marketdata.pricing import last_tradeable_price
        from athena_strategy.screener import menu_rows

        puts, calls = menu_rows(conn, settings, account_id, symbol, tactic, last_tradeable_price(conn, symbol), date.today())
        for choice_expiry, _strike in puts + calls:
            if choice_expiry not in expiries:
                expiries.append(choice_expiry)
    return {
        "heading": facts[0],
        "facts": facts[1:],
        "contracts": int(contracts),
        "max_contracts": _amend_cap(conn, settings, account_id, symbol, tactic, multiplier or "100"),
        "put_limit": put_limit,
        "call_limit": call_limit,
        "limit_price": stock_limit,
        "expiry": expiry,
        "put_strike": put_strike,
        "call_strike": call_strike,
        "expiries": expiries,
        "puts": puts,
        "calls": calls,
    }


def use_link(
    conn: psycopg.Connection,
    token: str,
    decision: str,
    actor: str,
    allowlist: set[str],
    amend: dict | None = None,
) -> str:
    row = conn.execute(
        """
        SELECT draft_id, expires_at, used_at
        FROM control_links
        WHERE token = %s
        """,
        (token,),
    ).fetchone()
    if row is None:
        raise ValueError("unknown link")
    draft_id, expires_at, used_at = row
    if used_at is not None:
        raise ValueError("link already used")
    if expires_at < datetime.now(UTC):
        raise ValueError("link expired")
    if decision == "approve" and amend:
        apply_amend(conn, draft_id, amend)
    decide(conn, draft_id, decision, actor, allowlist)
    conn.execute(
        "UPDATE control_links SET used_at = now() WHERE token = %s",
        (token,),
    )
    conn.commit()
    return draft_id


def decide(
    conn: psycopg.Connection,
    draft_id: str,
    decision: str,
    actor: str,
    allowlist: set[str],
    reason: str = "",
) -> None:
    if actor not in allowlist:
        raise PermissionError("actor is not on the Telegram allowlist")
    if decision not in {"approve", "reject"}:
        raise ValueError("decision must be approve or reject")
    row = conn.execute(
        "SELECT symbol, tactic, contracts, status, account_id FROM drafts WHERE id = %s",
        (draft_id,),
    ).fetchone()
    if row is None:
        raise ValueError("unknown draft")
    symbol, tactic, contracts, status, account_id = row
    if status != "pending":
        raise ValueError("draft is not pending")
    if decision == "approve":
        conn.execute(
            "UPDATE drafts SET status = 'approved' WHERE id = %s",
            (draft_id,),
        )
        _queue_order(conn, draft_id, symbol, tactic, contracts, account_id)
    else:
        conn.execute(
            "UPDATE drafts SET status = 'rejected' WHERE id = %s",
            (draft_id,),
        )
    conn.execute(
        """
        INSERT INTO approvals (draft_id, actor, decision, account_id, reason)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (draft_id, actor, decision, account_id, reason.strip() if decision == "reject" else ""),
    )
    if decision == "reject":
        note_reject_streak(conn, account_id)
    conn.commit()


def _queue_order(
    conn: psycopg.Connection,
    draft_id: str,
    symbol: str,
    tactic: str,
    contracts: int,
    account_id: str,
) -> None:
    contract = conn.execute(
        """
        SELECT instrument, limit_price, expiry, put_strike, call_strike,
               put_limit, call_limit, trading_class, option_exchange, multiplier, order_action
        FROM drafts WHERE id = %s
        """,
        (draft_id,),
    ).fetchone()
    if contract is None:
        return
    instrument, limit_price, expiry, put_strike, call_strike, put_limit, call_limit, trading_class, option_exchange, multiplier, order_action = contract
    if instrument == "OPT" and tactic == "OPEN_STRANGLE":
        if not expiry or not put_strike or not call_strike or not put_limit or not call_limit:
            _insert_order(conn, draft_id, account_id, symbol, tactic, "SELL", contracts, limit_price, "blocked", "draft has no option contract", None, None, "", "", "", multiplier or "100")
            return
        _insert_order(conn, draft_id, account_id, symbol, tactic, "SELL", contracts, put_limit, "queued", None, expiry, put_strike, "P", trading_class, option_exchange, multiplier or "100")
        _insert_order(conn, draft_id, account_id, symbol, tactic, "SELL", contracts, call_limit, "queued", None, expiry, call_strike, "C", trading_class, option_exchange, multiplier or "100")
        return
    if instrument == "OPT" and tactic == "OPEN_CSP":
        if not expiry or not put_strike or not put_limit:
            _insert_order(conn, draft_id, account_id, symbol, tactic, "SELL", contracts, limit_price, "blocked", "draft has no option contract", None, None, "", "", "", multiplier or "100")
            return
        _insert_order(conn, draft_id, account_id, symbol, tactic, "SELL", contracts, put_limit, "queued", None, expiry, put_strike, "P", trading_class, option_exchange, multiplier or "100")
        return
    if instrument == "OPT" and tactic == "OPEN_CC":
        if not expiry or not call_strike or not call_limit:
            _insert_order(conn, draft_id, account_id, symbol, tactic, "SELL", contracts, limit_price, "blocked", "draft has no option contract", None, None, "", "", "", multiplier or "100")
            return
        _insert_order(conn, draft_id, account_id, symbol, tactic, "SELL", contracts, call_limit, "queued", None, expiry, call_strike, "C", trading_class, option_exchange, multiplier or "100")
        return
    if order_action == "CANCEL":
        sells = conn.execute(
            """
            SELECT ib_order_id, expiry, strike, contract_right, limit_price,
                   trading_class, option_exchange, multiplier, contracts
            FROM orders
            WHERE account_id = %s AND symbol = %s AND status = 'submitted' AND action = 'SELL'
              AND ib_order_id IS NOT NULL
            """,
            (account_id, symbol),
        ).fetchall()
        if not sells:
            _insert_order(conn, draft_id, account_id, symbol, tactic, "CANCEL", contracts, limit_price, "blocked", "no working order to cancel", None, None, "", "", "", multiplier or "100")
            return
        for ib_order_id, expiry, strike, right, leg_limit, trading_class, option_exchange, multiplier, qty in sells:
            _insert_order(
                conn, draft_id, account_id, symbol, tactic, "CANCEL", int(qty), leg_limit,
                "queued", None, expiry, strike, right, trading_class, option_exchange, multiplier or "100",
                int(ib_order_id),
            )
        return
    if instrument == "OPT" and tactic in {"CLOSE", "ROLL"}:
        strike = put_strike or call_strike
        leg_limit = put_limit or call_limit
        right = "P" if put_strike else "C"
        if not expiry or not strike or not leg_limit:
            _insert_order(conn, draft_id, account_id, symbol, tactic, order_action or "BUY", contracts, limit_price, "blocked", "draft has no option contract", None, None, "", "", "", multiplier or "100")
            return
        _insert_order(conn, draft_id, account_id, symbol, tactic, order_action or "BUY", contracts, leg_limit, "queued", None, expiry, strike, right, trading_class, option_exchange, multiplier or "100")
        return
    status = "queued"
    block_reason = None
    if instrument != "STK":
        status = "blocked"
        block_reason = "draft has no stock limit contract"
    elif limit_price is None or limit_price <= 0:
        status = "blocked"
        block_reason = "draft has no limit price"
    action = order_action if order_action in {"BUY", "SELL"} else "BUY"
    _insert_order(conn, draft_id, account_id, symbol, tactic, action, contracts, limit_price, status, block_reason, None, None, "", "", "", multiplier or "100")


def _insert_order(
    conn: psycopg.Connection,
    draft_id: str,
    account_id: str,
    symbol: str,
    tactic: str,
    action: str,
    contracts: int,
    limit_price,
    status: str,
    block_reason: str | None,
    expiry,
    strike,
    right: str,
    trading_class: str,
    option_exchange: str,
    multiplier: str,
    ib_order_id: int | None = None,
) -> None:
    simulated = _draft_simulated(conn, draft_id)
    if simulated and status == "queued" and action != "CANCEL":
        status = "filled"
    conn.execute(
        """
        INSERT INTO orders (
            id, draft_id, account_id, symbol, tactic, action, contracts, limit_price,
            status, block_reason, expiry, strike, contract_right, trading_class, option_exchange, multiplier,
            ib_order_id, simulated
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            "ord_" + secrets.token_hex(3),
            draft_id,
            account_id,
            symbol,
            tactic,
            action,
            contracts,
            limit_price,
            status,
            block_reason,
            expiry,
            strike,
            right,
            trading_class,
            option_exchange,
            multiplier,
            ib_order_id,
            simulated,
        ),
    )
    if simulated and status == "filled":
        _book_simulated(
            conn, draft_id, account_id, symbol, tactic, action, contracts,
            limit_price, expiry, strike, right, multiplier,
        )


def _draft_simulated(conn: psycopg.Connection, draft_id: str) -> bool:
    row = conn.execute("SELECT simulated FROM drafts WHERE id = %s", (draft_id,)).fetchone()
    return bool(row and row[0])


def _book_simulated(
    conn: psycopg.Connection,
    draft_id: str,
    account_id: str,
    symbol: str,
    tactic: str,
    action: str,
    contracts: int,
    limit_price,
    expiry,
    strike,
    right: str,
    multiplier: str,
) -> None:
    """A replay fill at the draft limit. Nothing is sent to IB."""
    if limit_price is None or Decimal(limit_price) <= 0 or contracts <= 0:
        return
    price = Decimal(limit_price)
    signed = Decimal(contracts) if action == "BUY" else Decimal(-contracts)
    sec_type = "OPT" if right else "STK"
    conn.execute(
        """
        INSERT INTO fills (
            draft_id, symbol, tactic, contracts, price, simulated, account_id, side, sec_type
        )
        VALUES (%s, %s, %s, %s, %s, true, %s, %s, %s)
        """,
        (draft_id, symbol, tactic, contracts, price, account_id, action or "", sec_type),
    )
    existing = conn.execute(
        """
        SELECT quantity, avg_price FROM positions
        WHERE account_id = %s AND symbol = %s AND simulated = true AND sec_type = %s
          AND contract_right = %s
          AND expiry IS NOT DISTINCT FROM %s
          AND strike IS NOT DISTINCT FROM %s
        ORDER BY snapshot_at DESC, id DESC
        LIMIT 1
        """,
        (account_id, symbol, sec_type, right or "", expiry, strike),
    ).fetchone()
    if existing is None:
        quantity = signed
        average = price
    else:
        old_qty = Decimal(existing[0])
        old_avg = Decimal(existing[1])
        quantity = old_qty + signed
        if old_qty == 0 or (old_qty > 0) != (signed > 0):
            average = price if quantity != 0 else old_avg
        else:
            average = (old_avg * abs(old_qty) + price * abs(signed)) / abs(quantity or signed)
    mult = Decimal(multiplier or "1") if sec_type == "OPT" else Decimal(1)
    conn.execute(
        """
        INSERT INTO positions (
            account_id, symbol, quantity, avg_price, sec_type, expiry,
            contract_right, strike, market_price, currency, multiplier,
            market_value, unrealized_pnl, simulated
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'USD', %s, %s, 0, true)
        """,
        (
            account_id, symbol, quantity, average, sec_type, expiry,
            right or "", strike, price, mult, quantity * price * mult,
        ),
    )
