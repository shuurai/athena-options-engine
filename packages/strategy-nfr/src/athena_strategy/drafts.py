"""Create a paper draft, then Approve or Reject it into an audit row."""

from __future__ import annotations

import secrets
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import psycopg

from athena_exec.halt import active_reason, note_reject_streak
from athena_risk import allowed_open, block_reason
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
        VALUES (%s, %s, 'P2', 'NEWS_WASH,HIGH_IV', 'OPEN_STRANGLE')
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


def _tactic_on(settings: SettingsPayload, symbol: str, tactic: str) -> bool:
    flag = _TACTIC_FLAG.get(tactic)
    if flag is None:
        return True
    flags = settings.tactic_flags.get(symbol.upper())
    if flags is None:
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


def _guard_inputs(conn, account_id: str, symbol: str, expiry, opening: bool) -> dict:
    orders = conn.execute(
        """
        SELECT count(*) FROM orders
        WHERE account_id = %s AND status <> 'blocked' AND created_at::date = CURRENT_DATE
        """,
        (account_id,),
    ).fetchone()[0]
    found = {"orders_today": int(orders)}
    if not opening:
        return found
    nav = conn.execute(
        "SELECT last_nav FROM nav_marks WHERE account_id = %s ORDER BY day DESC LIMIT 1",
        (account_id,),
    ).fetchone()
    mark = conn.execute(
        "SELECT cash, margin FROM account_marks WHERE account_id = %s ORDER BY day DESC LIMIT 1",
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
    today = date.today()
    nearest = None
    if earnings:
        offsets = [(row[0] - today).days for row in earnings]
        nearest = min(offsets, key=lambda days: (abs(days), days))
    dte = None if expiry is None else (expiry - today).days
    found.update(
        {
            "nav": None if nav is None else nav[0],
            "symbol_value": _symbol_value(conn, account_id, symbol),
            "cash": None if mark is None else mark[0],
            "margin": None if mark is None else mark[1],
            "iv_rank": _stored_iv_rank(conn, account_id, symbol),
            "dte": dte,
            "earnings_in_days": nearest,
        }
    )
    return found


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
    from athena_strategy.iv import iv_rank

    rows = conn.execute(
        "SELECT iv_proxy FROM iv_marks WHERE symbol = %s ORDER BY day",
        (symbol,),
    ).fetchall()
    if not rows:
        return None
    samples = [row[0] for row in rows]
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
    """Contracts for a new open. One when the money cap is off or the price is missing."""
    limits = settings.risk
    if limits.max_position_per_symbol <= 0:
        return 1
    price = _latest_price(conn, symbol)
    if price is None:
        return 1
    mult = Decimal(multiplier or "100")
    if mult <= 0:
        mult = Decimal(100)
    return allowed_open(limits, price, mult, _exposure(conn, account_id, symbol, price))


def _latest_price(conn: psycopg.Connection, symbol: str) -> Decimal | None:
    row = conn.execute(
        """
        SELECT close FROM market_bars
        WHERE symbol = %s AND source IN ('ib-delayed', 'ib-daily') AND close > 0
        ORDER BY ts DESC
        LIMIT 1
        """,
        (symbol,),
    ).fetchone()
    if row is None:
        return None
    return Decimal(row[0])


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
    price = _latest_price(conn, symbol)
    if price is None:
        return {}
    mult = Decimal(multiplier or "100")
    if mult <= 0:
        mult = Decimal(100)
    return {"price": price, "multiplier": mult, "existing": _exposure(conn, account_id, symbol, price)}


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
) -> dict:
    """Insert a draft and report why. status is pending, blocked, or skipped."""
    if symbol.upper() in {item.upper() for item in settings.blocked_symbols}:
        return {"id": None, "status": "skipped", "detail": "symbol blocked", "contracts": contracts}
    if not _tactic_on(settings, symbol, tactic):
        flag = _TACTIC_FLAG.get(tactic, "tactic")
        return {"id": None, "status": "skipped", "detail": f"{flag} is off", "contracts": contracts}
    opening = tactic in {"OPEN_STRANGLE", "OPEN_CSP", "OPEN_CC", "BUY"}
    if opening and _has_short(conn, account_id, symbol):
        blocked = "conflicting short on this symbol"
    else:
        extra = _position_inputs(conn, account_id, symbol, multiplier) if opening else {}
        blocked = block_reason(
            contracts,
            settings.risk,
            opening=opening,
            needs_cash=tactic in {"OPEN_CSP", "BUY"},
            **_guard_inputs(conn, account_id, symbol, expiry, opening),
            **extra,
        )
    status = "blocked" if blocked else "pending"
    draft_id = "draft_" + secrets.token_hex(3)
    conn.execute(
        """
        INSERT INTO drafts (
            id, symbol, tactic, contracts, status, reason, phase, account_id,
            instrument, limit_price, expiry, put_strike, call_strike,
            put_limit, call_limit, trading_class, option_exchange, multiplier, order_action
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            draft_id, symbol, tactic, contracts, status, blocked or reason, phase, account_id,
            instrument, limit_price, expiry, put_strike, call_strike,
            put_limit, call_limit, trading_class, option_exchange, multiplier, order_action,
        ),
    )
    conn.commit()
    return {
        "id": draft_id,
        "status": status,
        "detail": blocked or f"pending, size {contracts}",
        "contracts": contracts,
    }


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
    _apply_listed(conn, symbol, tactic, expiry, put_strike, call_strike, fields, updates)
    if not updates:
        return
    assignments = ", ".join(f"{key} = %s" for key in updates)
    conn.execute(
        f"UPDATE drafts SET {assignments} WHERE id = %s",
        (*updates.values(), draft_id),
    )


def _apply_listed(conn, symbol: str, tactic: str, expiry, put_strike, call_strike, fields: dict, updates: dict) -> None:
    moving = any(fields.get(key) is not None for key in ("expiry", "put_strike", "call_strike"))
    if not moving:
        return
    if tactic not in {"OPEN_STRANGLE", "OPEN_CSP", "OPEN_CC"}:
        raise AmendError("strike and expiry stay on this draft")
    final_expiry = fields["expiry"] if fields.get("expiry") is not None else expiry
    final_put = fields["put_strike"] if fields.get("put_strike") is not None else put_strike
    final_call = fields["call_strike"] if fields.get("call_strike") is not None else call_strike
    if final_put is not None and not _listed(conn, symbol, final_expiry, "P", final_put):
        raise AmendError("that put is not listed for this expiry")
    if final_call is not None and not _listed(conn, symbol, final_expiry, "C", final_call):
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
    price = _latest_price(conn, symbol)
    if price is None:
        return cap
    mult = Decimal(multiplier or "100")
    if mult <= 0:
        mult = Decimal(100)
    return allowed_open(settings.risk, price, mult, _exposure(conn, account_id, symbol, price))


def _listed(conn, symbol: str, expiry, right: str, strike) -> bool:
    if expiry is None or strike is None:
        return False
    row = conn.execute(
        """
        SELECT 1 FROM option_choices
        WHERE symbol = %s AND expiry = %s AND contract_right = %s AND strike = %s
        """,
        (symbol, expiry, right, strike),
    ).fetchone()
    return row is not None


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
        choice_rows = conn.execute(
            """
            SELECT expiry, contract_right, strike FROM option_choices
            WHERE symbol = %s
            ORDER BY expiry, contract_right, strike
            """,
            (symbol,),
        ).fetchall()
        for choice_expiry, right, strike in choice_rows:
            if choice_expiry not in expiries:
                expiries.append(choice_expiry)
            if right == "P" and tactic != "OPEN_CC":
                puts.append((choice_expiry, strike))
            if right == "C" and tactic != "OPEN_CSP":
                calls.append((choice_expiry, strike))
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
    _insert_order(conn, draft_id, account_id, symbol, tactic, "BUY", contracts, limit_price, status, block_reason, None, None, "", "", "", multiplier or "100")


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
    conn.execute(
        """
        INSERT INTO orders (
            id, draft_id, account_id, symbol, tactic, action, contracts, limit_price,
            status, block_reason, expiry, strike, contract_right, trading_class, option_exchange, multiplier,
            ib_order_id
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
        ),
    )
