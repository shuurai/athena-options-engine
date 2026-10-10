"""Roll and profit-take drafts. They stay pending until Approve."""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

import psycopg

from athena_exec.halt import active_reason
from athena_settings.model import SettingsPayload
from athena_strategy.agents import write_decision
from athena_strategy.drafts import has_open_draft, stage_draft

log = logging.getLogger("athena.strategy")


def management_tactic(
    *,
    sec_type: str,
    quantity: Decimal,
    avg_price: Decimal,
    market_price: Decimal | None,
    expiry: date | None,
    today: date,
    roll_dte: int,
    profit_take_pct: int,
) -> tuple[str, str] | None:
    """ROLL when DTE is short. CLOSE when a short has captured enough premium."""
    if sec_type != "OPT" or quantity == 0:
        return None
    if expiry is not None:
        dte = (expiry - today).days
        if 0 <= dte <= roll_dte:
            return "ROLL", f"DTE {dte} is within {roll_dte}."
    if quantity < 0 and avg_price > 0 and market_price is not None:
        captured = (avg_price - market_price) / avg_price * 100
        if captured >= profit_take_pct:
            return "CLOSE", f"Premium captured {captured:.0f}%."
    return None


def manage_book(
    conn: psycopg.Connection,
    settings: SettingsPayload,
    account_id: str,
    today: date | None = None,
) -> list[str]:
    """Draft roll or profit-take for this account. One pending draft per symbol."""
    halt_reason = active_reason(conn, account_id)
    if halt_reason:
        write_decision(
            conn, account_id, "—", "book", "dropped", f"Halted. {halt_reason}",
            {"items": [_book_step("", "dropped", halt_reason)]},
        )
        return []
    day = today or date.today()
    rows = conn.execute(
        """
        SELECT DISTINCT ON (symbol, expiry, contract_right, strike)
            symbol, quantity, avg_price, sec_type, expiry, contract_right, strike, market_price
        FROM positions
        WHERE account_id = %s
        ORDER BY symbol, expiry, contract_right, strike, snapshot_at DESC
        """,
        (account_id,),
    ).fetchall()
    created: list[str] = []
    for symbol, quantity, avg_price, sec_type, expiry, right, strike, market_price in rows:
        choice = management_tactic(
            sec_type=sec_type,
            quantity=quantity,
            avg_price=avg_price,
            market_price=market_price,
            expiry=expiry,
            today=day,
            roll_dte=settings.strategy.roll_dte,
            profit_take_pct=settings.strategy.profit_take_pct,
        )
        if choice is None:
            continue
        tactic, reason = choice
        if has_open_draft(conn, account_id, symbol, tactic):
            _book(conn, account_id, symbol, "dropped", f"{tactic} already open", tactic, "dropped", "draft already open")
            continue
        contracts = max(1, int(abs(quantity)))
        leg = _option_leg(conn, symbol, expiry, right, strike, market_price or avg_price) if sec_type == "OPT" else None
        if sec_type == "OPT" and leg is None:
            log.info("manage waits for a contract symbol=%s tactic=%s", symbol, tactic)
            _book(conn, account_id, symbol, "dropped", f"{tactic} skipped", tactic, "dropped", "no contract")
            continue
        action = "BUY" if quantity < 0 else "SELL"
        staged = stage_draft(
            conn,
            settings,
            symbol,
            tactic,
            contracts,
            reason,
            "MANAGE",
            account_id,
            instrument=None if leg is None else "OPT",
            limit_price=None if leg is None else leg[2],
            expiry=None if leg is None else leg[0],
            put_strike=strike if leg and right == "P" else None,
            call_strike=strike if leg and right == "C" else None,
            put_limit=leg[2] if leg and right == "P" else None,
            call_limit=leg[2] if leg and right == "C" else None,
            trading_class="" if leg is None else leg[3],
            option_exchange="" if leg is None else leg[4],
            multiplier="100" if leg is None else leg[5],
            order_action="" if leg is None else action,
        )
        if staged["status"] == "pending":
            log.info("manage account=%s symbol=%s tactic=%s", account_id, symbol, tactic)
            created.append(staged["id"])
            _book(conn, account_id, symbol, "drafted", f"{tactic} kept", tactic, "kept", staged["detail"], staged["id"])
        elif staged["status"] == "blocked":
            _book(conn, account_id, symbol, "dropped", f"{tactic} blocked", tactic, "blocked", staged["detail"], staged["id"])
        else:
            _book(conn, account_id, symbol, "dropped", f"{tactic} dropped", tactic, "dropped", staged["detail"])
    return created


def _book(conn, account_id, symbol, outcome, summary, tactic, step_outcome, detail, draft_id=None) -> None:
    write_decision(
        conn, account_id, symbol, "book", outcome, summary,
        {"items": [_book_step(tactic, step_outcome, detail, draft_id or "")]},
    )


def _book_step(tactic: str, outcome: str, detail: str, draft_id: str = "") -> dict:
    return {"tactic": tactic, "outcome": outcome, "detail": detail, "draft_id": draft_id}


def _option_leg(conn: psycopg.Connection, symbol: str, expiry, right: str, strike, limit_price):
    """Class and exchange come from the stored plan. The position supplies the expiry."""
    if expiry is None or not right or strike is None or limit_price is None or limit_price <= 0:
        return None
    plan = conn.execute(
        """
        SELECT trading_class, option_exchange, multiplier
        FROM option_plans WHERE symbol = %s
        """,
        (symbol,),
    ).fetchone()
    if plan is None:
        return None
    return expiry, strike, limit_price, plan[0], plan[1], plan[2]
