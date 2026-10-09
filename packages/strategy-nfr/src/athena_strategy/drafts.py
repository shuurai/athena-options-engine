"""Create a paper draft, then Approve or Reject it into an audit row."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import psycopg

from athena_exec.halt import active_reason, note_reject_streak
from athena_risk import block_reason
from athena_settings.model import SettingsPayload

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
    """Insert a draft. Over-size drafts are stored as blocked and return None."""
    blocked = block_reason(contracts, settings.risk)
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
    return None if blocked else draft_id


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


def use_link(
    conn: psycopg.Connection,
    token: str,
    decision: str,
    actor: str,
    allowlist: set[str],
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
        INSERT INTO approvals (draft_id, actor, decision, account_id)
        VALUES (%s, %s, %s, %s)
        """,
        (draft_id, actor, decision, account_id),
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
