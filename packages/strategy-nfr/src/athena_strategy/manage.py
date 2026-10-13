"""Profit-take drafts. They stay pending until Approve. Rolls are not drafted."""

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
    """CLOSE when a short option has captured enough premium. A long option stays put."""
    del expiry, today, roll_dte
    if sec_type != "OPT" or quantity >= 0:
        return None
    if avg_price > 0 and market_price is not None:
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
        SELECT DISTINCT ON (symbol, expiry, contract_right, strike)
            symbol, quantity, avg_price, sec_type, expiry, contract_right, strike, market_price
        FROM positions
        WHERE account_id = %s AND simulated = %s
        ORDER BY symbol, expiry, contract_right, strike, snapshot_at DESC
        """,
        (account_id, replaying is not None),
    ).fetchall()
    created: list[str] = []
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row[0], []).append(row)
    for symbol, legs in grouped.items():
        chosen = []
        for symbol_name, quantity, avg_price, sec_type, expiry, right, strike, market_price in legs:
            del symbol_name
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
            chosen.append((choice, quantity, sec_type, expiry, right, strike, market_price or avg_price))
        if not chosen:
            continue
        rolls = [item for item in chosen if item[0][0] == "ROLL"]
        closes = [item for item in chosen if item[0][0] != "ROLL"]
        use = (rolls + closes)[:2]
        ticket_reason = "manage_roll" if any(item[0][0] == "ROLL" for item in use) else "manage_close"
        from athena_strategy.wheel import insert_manage_ticket

        ticket_id = insert_manage_ticket(conn, account_id, symbol, ticket_reason, len(use))
        for index, (choice, quantity, sec_type, expiry, right, strike, mark) in enumerate(use):
            tactic, reason = choice
            if has_open_draft(conn, account_id, symbol, tactic):
                _book(conn, account_id, symbol, "dropped", f"{tactic} already open", tactic, "dropped", "draft already open")
                continue
            contracts = max(1, int(abs(quantity)))
            leg = _option_leg(conn, symbol, expiry, right, strike, mark) if sec_type == "OPT" else None
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
                ticket_id=ticket_id,
                leg_index=index,
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
