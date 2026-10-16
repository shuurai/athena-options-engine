"""Profit-take drafts. They stay pending until Approve. Rolls are not drafted."""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

import psycopg

from athena_exec.halt import active_reason
from athena_settings.model import SettingsPayload
from athena_strategy.agents import write_decision
from athena_strategy.draft_facts import (
    fact_pack_prompt_block,
    fact_pack_reason,
    format_draft_reason,
    moneyness,
    premium_captured_pct,
)
from athena_strategy.advisor import today_signals
from athena_strategy.drafts import has_open_draft, last_tradeable_price, stage_draft

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
    strike: Decimal | None = None,
    right: str | None = None,
    spot: Decimal | None = None,
    tags: list[str] | tuple[str, ...] | None = None,
    ticket_reason: str = "manage_close",
) -> tuple[str, str] | None:
    """CLOSE when a short option has captured enough premium. A long option stays put."""
    del roll_dte
    if sec_type != "OPT" or quantity >= 0:
        return None
    captured = premium_captured_pct(avg_price, market_price)
    if captured is None or captured < profit_take_pct:
        return None
    reason = fact_pack_reason(
        symbol="",
        tactic="CLOSE",
        ticket_reason=ticket_reason,
        spot=spot,
        strike=strike,
        right=right,
        expiry=expiry,
        today=today,
        avg_price=avg_price,
        market_price=market_price,
        tags=tags,
    )
    if not reason:
        reason = f"Premium captured {captured:.0f}%."
    return "CLOSE", reason


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
    advisor = today_signals(conn, account_id, day) if settings.advisor.enabled else {}
    created: list[str] = []
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row[0], []).append(row)
    for symbol, legs in grouped.items():
        spot = last_tradeable_price(conn, symbol)
        tags = _symbol_tags(conn, account_id, symbol)
        signal = advisor.get(symbol)
        chosen = []
        for symbol_name, quantity, avg_price, sec_type, expiry, right, strike, market_price in legs:
            del symbol_name
            threshold = settings.strategy.profit_take_pct
            choice = management_tactic(
                sec_type=sec_type,
                quantity=quantity,
                avg_price=avg_price,
                market_price=market_price,
                expiry=expiry,
                today=day,
                roll_dte=settings.strategy.roll_dte,
                profit_take_pct=threshold,
                strike=strike,
                right=right,
                spot=spot,
                tags=tags,
            )
            if choice is None:
                continue
            tactic, reason = choice
            if signal and _advisor_acts(signal, settings):
                reason = f"Advisor: {signal['reason']}\n\n{reason}"
            chosen.append(((tactic, reason), quantity, sec_type, expiry, right, strike, market_price or avg_price, avg_price))
        if not chosen:
            continue
        rolls = [item for item in chosen if item[0][0] == "ROLL"]
        closes = [item for item in chosen if item[0][0] != "ROLL"]
        use = (rolls + closes)[:2]
        ticket_reason = "manage_roll" if any(item[0][0] == "ROLL" for item in use) else "manage_close"
        from athena_strategy.wheel import insert_manage_ticket

        ticket_id = insert_manage_ticket(conn, account_id, symbol, ticket_reason, len(use))
        for index, (choice, quantity, sec_type, expiry, right, strike, mark, avg_price) in enumerate(use):
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
                draft_id = staged["id"]
                explained = _explain_close(
                    conn,
                    settings,
                    account_id,
                    symbol,
                    draft_id,
                    fallback=reason,
                    spot=spot,
                    strike=strike,
                    right=right,
                    expiry=expiry,
                    today=day,
                    avg_price=avg_price,
                    market_price=mark,
                    tags=tags,
                    ticket_reason=ticket_reason,
                )
                if explained and explained != reason:
                    conn.execute("UPDATE drafts SET reason = %s WHERE id = %s", (explained, draft_id))
                created.append(draft_id)
                _book(conn, account_id, symbol, "drafted", f"{tactic} kept", tactic, "kept", staged["detail"], draft_id)
            elif staged["status"] == "blocked":
                _book(conn, account_id, symbol, "dropped", f"{tactic} blocked", tactic, "blocked", staged["detail"], staged["id"])
            else:
                _book(conn, account_id, symbol, "dropped", f"{tactic} dropped", tactic, "dropped", staged["detail"])
    if settings.advisor.enabled:
        created.extend(_advisor_option_drafts(conn, settings, account_id, grouped, advisor, day))
        created.extend(_advisor_stock_drafts(conn, settings, account_id, grouped, advisor, day))
    return created


def _advisor_acts(signal: dict, settings) -> bool:
    adv = settings.advisor
    action = signal.get("action")
    confidence = float(signal.get("confidence") or 0.0)
    if action == "close":
        return confidence >= adv.close_conf_threshold
    if action in {"take_profit", "trim"}:
        return confidence >= adv.trim_conf_threshold
    return False


def _advisor_open(conn, account_id: str, symbol: str, tactic: str, instrument: str) -> bool:
    """True when this symbol already has a pending advisor draft for the same instrument."""
    row = conn.execute(
        """
        SELECT 1 FROM drafts
        WHERE account_id = %s AND symbol = %s AND tactic = %s
          AND coalesce(instrument, '') = %s
          AND status IN ('pending', 'approved')
        LIMIT 1
        """,
        (account_id, symbol, tactic, instrument),
    ).fetchone()
    return row is not None


def _advisor_tactic(action: str) -> str:
    """take_profit and close exit. trim reduces."""
    return "TRIM" if action == "trim" else "CLOSE"


def _advisor_quantity(action: str, held: int) -> int:
    """Full size to exit. Half, at least one, to trim."""
    size = max(1, int(held))
    if action == "trim":
        return max(1, size // 2)
    return size


def _advisor_option_drafts(conn, settings, account_id, grouped, advisor, day) -> list[str]:
    """Stage CLOSE or TRIM for a short option when the advisor acts, even below the profit-take gate."""
    created: list[str] = []
    for symbol, legs in grouped.items():
        signal = advisor.get(symbol)
        if not signal or not _advisor_acts(signal, settings):
            continue
        action = signal["action"]
        tactic = _advisor_tactic(action)
        if _advisor_open(conn, account_id, symbol, tactic, "OPT"):
            continue
        spot = last_tradeable_price(conn, symbol)
        staged_one = False
        for row in legs:
            if staged_one:
                break
            _, quantity, avg_price, sec_type, expiry, right, strike, market_price = row
            qty = Decimal(quantity)
            if sec_type != "OPT" or qty >= 0 or strike is None:
                continue
            mark = Decimal(market_price) if market_price is not None else None
            if mark is None or mark <= 0:
                continue
            leg = _option_leg(conn, symbol, expiry, right, strike, mark)
            if leg is None:
                log.info("advisor waits for a contract symbol=%s", symbol)
                continue
            held = max(1, int(abs(qty)))
            contracts = _advisor_quantity(action, held)
            pack = fact_pack_reason(
                symbol=symbol,
                tactic=tactic,
                ticket_reason="manage_close",
                spot=spot,
                strike=Decimal(strike),
                right=right,
                expiry=expiry,
                today=day,
                avg_price=Decimal(avg_price),
                market_price=mark,
            )
            reason = f"Advisor: {signal['reason']}"
            if pack:
                reason = f"{reason}\n\n{pack}"
            from athena_strategy.wheel import insert_manage_ticket

            ticket_id = insert_manage_ticket(conn, account_id, symbol, "manage_close", 1)
            staged = stage_draft(
                conn,
                settings,
                symbol,
                tactic,
                contracts,
                reason,
                "ADVISOR",
                account_id,
                instrument="OPT",
                limit_price=leg[2],
                expiry=leg[0],
                put_strike=strike if right == "P" else None,
                call_strike=strike if right == "C" else None,
                put_limit=leg[2] if right == "P" else None,
                call_limit=leg[2] if right == "C" else None,
                trading_class=leg[3],
                option_exchange=leg[4],
                multiplier=leg[5],
                order_action="BUY",
                ticket_id=ticket_id,
                leg_index=0,
                enforce_contract_cap=False,
            )
            if staged["status"] == "pending":
                log.info("advisor option account=%s symbol=%s tactic=%s", account_id, symbol, tactic)
                created.append(staged["id"])
                _book(conn, account_id, symbol, "drafted", f"ADVISOR {action}", "ADVISOR", "kept", staged["detail"], staged["id"])
                staged_one = True
            elif staged["status"] == "blocked":
                _book(conn, account_id, symbol, "dropped", "ADVISOR blocked", "ADVISOR", "blocked", staged["detail"], staged["id"])
    return created


def _advisor_stock_drafts(conn, settings, account_id, grouped, advisor, day) -> list[str]:
    """Stage CLOSE / TRIM drafts for long stock positions when advisor says so."""
    created: list[str] = []
    for symbol, legs in grouped.items():
        signal = advisor.get(symbol)
        if not signal or not _advisor_acts(signal, settings):
            continue
        action = signal["action"]
        tactic = _advisor_tactic(action)
        if _advisor_open(conn, account_id, symbol, tactic, "STK"):
            continue
        stock = None
        for row in legs:
            _, quantity, avg_price, sec_type, *_ = row
            if sec_type == "STK" and Decimal(quantity) > 0:
                stock = (Decimal(quantity), Decimal(avg_price))
                break
        if stock is None:
            continue
        qty, avg = stock
        spot = last_tradeable_price(conn, symbol)
        mark_row = None
        for row in legs:
            if row[3] == "STK" and row[7] is not None:
                mark_row = Decimal(row[7])
                break
        if spot is None:
            spot = mark_row
        if spot is None:
            continue
        from athena_strategy.wheel import insert_manage_ticket

        ticket_id = insert_manage_ticket(conn, account_id, symbol, "manage_close", 1)
        contracts = _advisor_quantity(action, int(qty))
        reason = f"Advisor: {signal['reason']}\n\nAction: {action}. Size {contracts} of {int(qty)} shares. Avg {avg:.2f}."
        staged = stage_draft(
            conn,
            settings,
            symbol,
            tactic,
            contracts,
            reason,
            "ADVISOR",
            account_id,
            instrument="STK",
            limit_price=spot,
            order_action="SELL",
            ticket_id=ticket_id,
            leg_index=0,
            enforce_contract_cap=False,
        )
        if staged["status"] == "pending":
            log.info("advisor account=%s symbol=%s action=%s", account_id, symbol, action)
            created.append(staged["id"])
            _book(conn, account_id, symbol, "drafted", f"ADVISOR {action}", "ADVISOR", "kept", staged["detail"], staged["id"])
        elif staged["status"] == "blocked":
            _book(conn, account_id, symbol, "dropped", f"ADVISOR blocked", "ADVISOR", "blocked", staged["detail"], staged["id"])
    return created


def _explain_close(
    conn,
    settings: SettingsPayload,
    account_id: str,
    symbol: str,
    draft_id: str,
    *,
    fallback: str,
    spot,
    strike,
    right,
    expiry,
    today,
    avg_price,
    market_price,
    tags,
    ticket_reason: str,
) -> str:
    """ManageExplainer: deepen Reason + counsel. Never changes the draft itself."""
    facts = fact_pack_prompt_block(
        symbol=symbol,
        tactic="CLOSE",
        ticket_reason=ticket_reason,
        spot=spot,
        strike=strike,
        right=right,
        expiry=expiry,
        today=today,
        avg_price=avg_price,
        market_price=market_price,
        tags=tags,
    )
    try:
        from athena_settings.prompts import resolved_agents
        from athena_settings.resolve import deepseek_token, scoring_model
        from athena_strategy.agents import _ask, fill_prompt
    except Exception as exc:
        log.info("manage explainer import failed draft=%s err=%s", draft_id, exc.__class__.__name__)
        return fallback
    prompts = resolved_agents(settings)
    prompt = prompts.get("ManageExplainer")
    if prompt is None:
        return fallback
    context = {
        "symbol": symbol,
        "draft_id": draft_id,
        "facts": facts or fallback,
        "moneyness": moneyness(spot, strike, right) or "unknown",
    }
    try:
        user = fill_prompt(prompt.execution, context)
    except Exception:
        user = (
            f"Symbol: {symbol}\nDraft: {draft_id}\nMoneyness: {context['moneyness']}\n"
            f"Facts:\n{facts or fallback}"
        )
    model = scoring_model(settings)
    provider = settings.ai.provider
    token = deepseek_token(settings)
    payload, stop = _ask(
        conn, account_id, "ManageExplainer", symbol, model, provider, token,
        prompt.system, user, bool(prompt.reasoning),
    )
    if stop or not payload or payload.get("error"):
        return fallback
    sentence = str(payload.get("sentence") or "").strip()
    counsel = str(payload.get("counsel") or "").strip()
    if not sentence:
        return format_draft_reason(fallback, counsel) if counsel else fallback
    return format_draft_reason(sentence, counsel)


def _symbol_tags(conn, account_id: str, symbol: str) -> list[str]:
    row = conn.execute(
        """
        SELECT tags FROM symbol_phases
        WHERE account_id = %s AND symbol = %s
        ORDER BY updated_at DESC NULLS LAST
        LIMIT 1
        """,
        (account_id, symbol),
    ).fetchone()
    if row is None or row[0] is None:
        return []
    raw = row[0]
    if isinstance(raw, list):
        return [str(item) for item in raw if item]
    if isinstance(raw, str) and raw.strip():
        return [part.strip() for part in raw.replace(",", " ").split() if part.strip()]
    return []


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
