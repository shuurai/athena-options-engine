"""Signaled entry. Taggers and tactic agents run side by side. The consolidator chooses drafts."""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from athena_exec.halt import active_reason
from athena_marketdata.pricing import Bar, daily_bars, last_daily_close
from athena_settings.model import SettingsPayload, StrategySettings, TacticFlags
from athena_settings.prompts import RUN_ORDER, TRADE_NAME, range_names, resolved_agents
from athena_settings.resolve import deepseek_token, scoring_model
from athena_strategy.drafts import apply_ticket_block, has_open_draft, open_quantity, stage_draft
from athena_strategy.dsh import chat
from athena_strategy.indicators import Indicators, indicators_enabled, read_indicators, store_reading
from athena_strategy.iv import tags_from_iv
from athena_strategy.phases import _load
from athena_strategy.schemas import parse_response, schema_line
from athena_strategy.tags import (
    CHANNEL_RISING,
    NEWS_WASH,
    PULLBACK,
    TAG_REGISTRY,
    kept_stored,
    names_owned_by,
    unknown_names,
)

log = logging.getLogger("athena.strategy")

_TACTICS = {"BUY": "BUY", "CSP": "OPEN_CSP", "CC": "OPEN_CC", "STRANGLE": "OPEN_STRANGLE"}
_MIN_BARS = 63
_INVALID_RETRIES = 1
_TAG_AGENTS = ("Location", "News")


def suggest_trade_price(conn, settings: SettingsPayload, account_id: str, symbol: str, last: str, side: str, order: str) -> dict:
    """Ask the Trade prompt for a limit and a stop. Does not send an order."""
    name = symbol.upper()
    prompt = resolved_agents(settings)[TRADE_NAME]
    context = _trade_context(conn, settings, name, last, side, order)
    user = fill_prompt(prompt.execution, context)
    model = scoring_model(settings)
    provider = settings.ai.provider
    token = deepseek_token(settings)
    thinking = bool(prompt.reasoning)
    result = chat(prompt.system, user, model, provider, token, thinking)
    marked = "on" if thinking and provider != "DSH" else "off"
    if result["error"]:
        _record(conn, account_id, TRADE_NAME, name, model, provider, marked, result, {"error": result["error"]}, "error")
        return {"ok": False, "message": result["error"]}
    payload = _trade_reply(result["text"])
    if payload is None:
        _record(
            conn, account_id, TRADE_NAME, name, model, provider, marked, result,
            {"raw": result["text"], "stage": "schema"}, "invalid",
        )
        return {"ok": False, "message": "The reply was not a price."}
    _record(conn, account_id, TRADE_NAME, name, model, provider, marked, result, payload, "ok")
    return {
        "ok": True,
        "limit": payload["limit"],
        "stop": payload["stop"],
        "reason": payload["reason"],
    }


def _trade_reply(text: str) -> dict | None:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    limit = _number(payload.get("limit"))
    stop = _number(payload.get("stop"))
    if limit is None or limit <= 0:
        return None
    return {
        "limit": format(limit, "f"),
        "stop": "" if stop is None or stop <= 0 else format(stop, "f"),
        "reason": str(payload.get("reason") or "").strip(),
    }


def _trade_context(conn, settings: SettingsPayload, symbol: str, last: str, side: str, order: str) -> dict[str, str]:
    from datetime import datetime, time, timedelta
    from zoneinfo import ZoneInfo

    from athena_exec.slots import symbol_market
    from athena_marketdata.pricing import last_tradeable_price

    flags = settings.tactic_flags.get(symbol, TacticFlags())
    kind = symbol_market(symbol, flags.market or "")
    zone_name = {"hk": "Asia/Hong_Kong", "ca": "America/Toronto"}.get(kind, "America/New_York")
    zone = ZoneInfo(zone_name)
    day = datetime.now(zone).date()
    start = datetime.combine(day, time.min, tzinfo=zone)
    shown = last.strip()
    if not shown or shown in {"—", "-"}:
        price = last_tradeable_price(conn, symbol)
        shown = "none" if price is None else format(price, "f")
    bar = conn.execute(
        """
        SELECT open, high, low, close, volume
        FROM market_bars
        WHERE symbol = %s AND source = 'ib-daily' AND ts::date = %s AND close > 0
        ORDER BY ts DESC
        LIMIT 1
        """,
        (symbol, day),
    ).fetchone()
    news = conn.execute(
        """
        SELECT title FROM news_events
        WHERE symbol = %s AND dismissed = false AND ts >= %s AND ts < %s
        ORDER BY ts DESC
        LIMIT 8
        """,
        (symbol, start, start + timedelta(days=1)),
    ).fetchall()
    headlines = "none" if not news else "\n".join(f"- {row[0]}" for row in news)

    def piece(value) -> str:
        if bar is None or value is None:
            return "none"
        return format(Decimal(value), "f")

    return {
        "symbol": symbol,
        "side": side or "BUY",
        "order": order or "LMT",
        "last": shown,
        "open": piece(None if bar is None else bar[0]),
        "high": piece(None if bar is None else bar[1]),
        "low": piece(None if bar is None else bar[2]),
        "close": piece(None if bar is None else bar[3]),
        "volume": piece(None if bar is None else bar[4]),
        "headlines": headlines,
    }


def fill_prompt(template: str, values: dict[str, Any]) -> str:
    text = template
    for key, value in values.items():
        if not isinstance(value, str):
            continue
        text = text.replace("{{" + key + "}}", value)
    numbers = values.get("numbers")
    if isinstance(numbers, str) and numbers and "{{numbers}}" not in template and "Numbers:" not in text:
        text = text.rstrip() + "\nNumbers: " + numbers
    return text


def parse_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text or "", re.S)
    if match is None:
        return {}
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def run_observe(conn: psycopg.Connection, settings: SettingsPayload, account_id: str) -> None:
    """Location and News only. Writes the state and does not draft."""
    for symbol in settings.watchlist:
        try:
            level, reason = eligible(conn, settings, account_id, symbol)
            if level == "none":
                write_decision(
                    conn, account_id, symbol, "wheel", "no_data", reason,
                    {"filter": reason, "items": []},
                )
                continue
            _observe_symbol(conn, settings, account_id, symbol)
        except Exception as exc:
            log.error("observe failed symbol=%s error=%s", symbol, exc.__class__.__name__)
            write_decision(
                conn, account_id, symbol, "wheel", "dropped", exc.__class__.__name__,
                {"items": [_step("", "dropped", exc.__class__.__name__)], "err": str(exc)[:500]},
            )


def _observe_symbol(conn, settings: SettingsPayload, account_id: str, symbol: str) -> None:
    prompts = resolved_agents(settings)
    flags = settings.tactic_flags.get(symbol.upper(), TacticFlags())
    context = _context(conn, settings, account_id, symbol, flags)
    model = scoring_model(settings)
    provider = settings.ai.provider
    token = deepseek_token(settings)
    answers: dict[str, dict] = {}
    for name in ("Location", "News"):
        prompt = prompts[name]
        user = fill_prompt(prompt.execution, context)
        payload, stop = _ask(
            conn, account_id, name, symbol, model, provider, token, prompt.system, user, bool(prompt.reasoning),
        )
        if stop:
            write_decision(
                conn, account_id, symbol, "wheel", "invalid", stop,
                {"items": [_step(name, "dropped", stop)]},
            )
            return
        answers[name] = payload
    write_decision(
        conn, account_id, symbol, "wheel", "observed", "outside_draft_slot",
        {"items": [_step(name, "ok", "") for name in ("Location", "News")], "tags": sorted(_tag_set(context, answers))},
    )


def run_signaled(conn: psycopg.Connection, settings: SettingsPayload, account_id: str) -> list[str]:
    """One signaled-entry pass. Location, News, then the wheel ticket."""
    return _run_wheel_pass(conn, settings, account_id)


def run_legacy_signaled(conn: psycopg.Connection, settings: SettingsPayload, account_id: str) -> list[str]:
    """The seven-agent pass. Location, News, four tactics, then the consolidator."""
    created: list[str] = []
    for symbol in settings.watchlist:
        try:
            level, reason = eligible(conn, settings, account_id, symbol)
            if level == "none":
                write_decision(
                    conn, account_id, symbol, "consolidator", "no_data", reason,
                    {"filter": reason, "items": []},
                )
                continue
            flags = settings.tactic_flags.get(symbol.upper(), TacticFlags())
            if level == "full":
                names = _names_for(flags, chain=True)
            elif level == "shares":
                names = _names_for(flags, chain=False)
            else:
                names = _TAG_AGENTS
            limited = "" if level in {"full", "shares"} else reason
            created.extend(_run_symbol(conn, settings, account_id, symbol, names, limited))
        except Exception as exc:
            log.error("signaled entry failed symbol=%s error=%s", symbol, exc.__class__.__name__)
            write_decision(
                conn, account_id, symbol, "consolidator", "dropped", exc.__class__.__name__,
                {"items": [_step("", "dropped", exc.__class__.__name__)], "err": str(exc)[:500]},
            )
    return created


def _run_wheel_pass(conn: psycopg.Connection, settings: SettingsPayload, account_id: str) -> list[str]:
    """Location, News, then one code ticket. Narrator may only explain or pick a listed strike."""
    created: list[str] = []
    for symbol in settings.watchlist:
        try:
            level, reason = eligible(conn, settings, account_id, symbol)
            if level == "none":
                write_decision(
                    conn, account_id, symbol, "wheel", "no_data", reason,
                    {"filter": reason, "items": []},
                )
                continue
            created.extend(_run_wheel_symbol(conn, settings, account_id, symbol))
        except Exception as exc:
            log.error("wheel entry failed symbol=%s error=%s", symbol, exc.__class__.__name__)
            write_decision(
                conn, account_id, symbol, "wheel", "dropped", exc.__class__.__name__,
                {"items": [_step("", "dropped", exc.__class__.__name__)], "err": str(exc)[:500]},
            )
    return created


def _run_wheel_symbol(conn, settings: SettingsPayload, account_id: str, symbol: str) -> list[str]:
    from decimal import Decimal

    from athena_strategy.wheel import decide_opening, execute_opening

    prompts = resolved_agents(settings)
    flags = settings.tactic_flags.get(symbol.upper(), TacticFlags())
    context = _context(conn, settings, account_id, symbol, flags)
    model = scoring_model(settings)
    provider = settings.ai.provider
    token = deepseek_token(settings)
    answers: dict[str, dict] = {}
    for name in ("Location", "News"):
        prompt = prompts[name]
        user = fill_prompt(prompt.execution, context)
        payload, stop = _ask(
            conn, account_id, name, symbol, model, provider, token, prompt.system, user, bool(prompt.reasoning),
        )
        if stop:
            write_decision(
                conn, account_id, symbol, "wheel", "invalid", stop,
                {"items": [_step(name, "dropped", stop)]},
            )
            return []
        answers[name] = payload
    tags = set(_tag_set(context, answers))
    preview = decide_opening(conn, settings, account_id, symbol, tags)
    menu = preview.call_lines or preview.put_lines
    context["tags"] = ", ".join(sorted(tags)) or "none"
    context["ticket"] = preview.reason or preview.quiet or "quiet"
    context["strikes"] = ", ".join(str(line.strike) for line in menu) or "none"
    prompt = prompts["Narrator"]
    payload, _stop = _ask(
        conn, account_id, "Narrator", symbol, model, provider, token,
        prompt.system, fill_prompt(prompt.execution, context), bool(prompt.reasoning),
    )
    sentence = str((payload or {}).get("sentence") or "")
    raw_strike = (payload or {}).get("strike")
    strike = None
    if raw_strike is not None:
        try:
            strike = Decimal(str(raw_strike))
        except Exception:
            strike = None
    return execute_opening(conn, settings, account_id, symbol, tags, strike, sentence)


def eligible(conn, settings: SettingsPayload, account_id: str, symbol: str) -> tuple[str, str]:
    """How far this symbol may go before a model call. none, tag_only, or full."""
    if active_reason(conn, account_id):
        return "none", "halted"
    blocked = {item.upper() for item in settings.blocked_symbols}
    if symbol.upper() in blocked:
        return "none", "symbol blocked"
    if len(_bars(conn, symbol)) < _MIN_BARS:
        return "none", "no_bars"
    flags = settings.tactic_flags.get(symbol.upper(), TacticFlags())
    from athena_strategy.wheel import may_buy

    shares = flags.long or may_buy(flags)
    if not _has_chain(conn, symbol):
        if shares:
            return "shares", ""
        return "tag_only", "no_chain"
    if not flags.options_on() and not shares:
        return "tag_only", "options off"
    return "full", ""


def _names_for(flags: TacticFlags, chain: bool) -> tuple[str, ...]:
    """Location and News, then only the toggles that are on, then the consolidator."""
    from athena_strategy.wheel import may_buy

    names = ["Location", "News"]
    if chain and flags.csp:
        names.append("CSP")
    if flags.long or may_buy(flags):
        names.append("Purchase")
    if chain and flags.strangle:
        names.append("Strangle")
    if chain and flags.cc:
        names.append("Call")
    names.append("Consolidator")
    return tuple(names)


def _has_chain(conn, symbol: str) -> bool:
    from athena_strategy.screener import has_choices

    name = symbol.upper()
    if has_choices(conn, name):
        return True
    plan = conn.execute(
        "SELECT 1 FROM option_plans WHERE symbol = %s LIMIT 1",
        (name,),
    ).fetchone()
    return plan is not None


def _run_symbol(
    conn,
    settings: SettingsPayload,
    account_id: str,
    symbol: str,
    names: tuple[str, ...] = RUN_ORDER,
    limited: str = "",
) -> list[str]:
    prompts = resolved_agents(settings)
    flags = settings.tactic_flags.get(symbol.upper(), TacticFlags())
    context = _context(conn, settings, account_id, symbol, flags)
    answers: dict[str, dict] = {}
    model = scoring_model(settings)
    provider = settings.ai.provider
    token = deepseek_token(settings)
    for name in names:
        if name in {"CSP", "Purchase", "Strangle", "Call", "Consolidator"}:
            context["tags"] = ", ".join(_tag_set(context, answers)) or "none"
        if name == "Call":
            context["purchase"] = _proposal_text(answers.get("Purchase") or {})
        if name == "Consolidator":
            context["proposals"] = _proposal_block(answers)
        prompt = prompts[name]
        thinking = bool(prompt.reasoning)
        user = fill_prompt(prompt.execution, context)
        payload, stop = _ask(
            conn, account_id, name, symbol, model, provider, token, prompt.system, user, thinking,
        )
        if stop:
            write_decision(
                conn, account_id, symbol, "consolidator", "invalid", stop,
                {"items": [_step(name, "dropped", stop)]},
            )
            return []
        answers[name] = payload
    if limited:
        write_decision(
            conn, account_id, symbol, "consolidator", "no_data", limited,
            {"filter": limited, "items": []},
        )
        return []
    return _drafts(conn, settings, account_id, symbol, flags, answers, context)


def _ask(conn, account_id, name, symbol, model, provider, token, system, user, thinking) -> tuple[dict, str]:
    """One agent call, plus one retry when the reply is invalid. A transport error is not retried."""
    result = chat(system, user, model, provider, token, thinking)
    if result["error"]:
        _keep(conn, account_id, name, symbol, model, provider, thinking, result, {"error": result["error"]}, "error")
        return {"error": result["error"]}, ""
    payload, status, stage = parse_response(name, result["text"])
    if status == "ok":
        _keep(conn, account_id, name, symbol, model, provider, thinking, result, payload or {}, "ok")
        return payload or {}, ""
    _keep(
        conn, account_id, name, symbol, model, provider, thinking, result,
        {"raw": result["text"], "stage": stage}, "invalid",
    )
    if _INVALID_RETRIES < 1:
        return {}, f"{name} {stage}"
    retry_user = user + "\n\nReply with JSON only, matching: " + schema_line(name)
    second = chat(system, retry_user, model, provider, token, thinking)
    if second["error"]:
        _keep(
            conn, account_id, name, symbol, model, provider, thinking, second,
            {"error": second["error"]}, "error",
        )
        return {"error": second["error"]}, ""
    payload, status, stage = parse_response(name, second["text"])
    if status == "ok":
        _keep(conn, account_id, name, symbol, model, provider, thinking, second, payload or {}, "ok")
        return payload or {}, ""
    _keep(
        conn, account_id, name, symbol, model, provider, thinking, second,
        {"raw": second["text"], "stage": stage}, "invalid",
    )
    return {}, f"{name} {stage}"


def _keep(conn, account_id, name, symbol, model, provider, thinking, result, payload, call_status) -> None:
    _record(
        conn,
        account_id,
        name,
        symbol,
        model,
        provider,
        "on" if thinking and provider != "DSH" else "off",
        result,
        payload,
        call_status,
    )


def _drafts(conn, settings, account_id, symbol, flags: TacticFlags, answers: dict[str, dict], context: dict) -> list[str]:
    choice = answers.get("Consolidator") or {}
    phase, _stored = _load(conn, account_id, symbol)
    trail = _trail_context(context, flags, phase or "P1", choice)
    if choice.get("error"):
        detail = str(choice.get("error"))
        trail["items"] = [_step("", "dropped", detail)]
        write_decision(conn, account_id, symbol, "consolidator", "dropped", detail, trail)
        return []
    created: list[str] = []
    items = choice.get("drafts") or []
    if not isinstance(items, list) or not items:
        trail["items"] = [_step("", "dropped", "no drafts")]
        write_decision(conn, account_id, symbol, "consolidator", "quiet", "no drafts", trail)
        return []
    steps = []
    for item in items:
        if not isinstance(item, dict):
            steps.append(_step("", "dropped", "draft was not an object"))
            continue
        raw = str(item.get("tactic") or "").upper()
        tactic = _TACTICS.get(raw)
        label = raw or "draft"
        if tactic is None:
            steps.append(_step(label, "dropped", "unknown tactic"))
            continue
        if tactic == "OPEN_CSP" and not flags.csp:
            steps.append(_step("CSP", "dropped", "csp off"))
            continue
        if tactic == "OPEN_CC" and not flags.cc:
            steps.append(_step("CC", "dropped", "cc off"))
            continue
        if tactic == "OPEN_STRANGLE" and not flags.strangle:
            steps.append(_step("STRANGLE", "dropped", "strangle off"))
            continue
        if tactic == "BUY":
            from athena_strategy.wheel import buy_access, may_buy

            allowed, why = buy_access(flags)
            if not may_buy(flags) or not allowed:
                steps.append(_step("BUY", "dropped", why))
                continue
        if has_open_draft(conn, account_id, symbol, tactic):
            steps.append(_step(_label(tactic), "dropped", "draft already open"))
            continue
        limit = _number(item.get("limit"))
        reason = str(item.get("reason") or "").strip() or tactic
        step = _propose_one(
            conn, settings, account_id, symbol, phase or "P1", tactic, limit, reason, answers,
        )
        steps.append(step)
        if step["outcome"] == "kept" and step.get("draft_id"):
            created.append(step["draft_id"])
    pending = [step["draft_id"] for step in steps if step.get("outcome") == "kept" and step.get("draft_id")]
    ticket = apply_ticket_block(conn, settings, account_id, symbol, pending)
    if ticket:
        blocked_ids = set(pending)
        for step in steps:
            if step.get("draft_id") in blocked_ids and step.get("outcome") == "kept":
                step["outcome"] = "blocked"
                step["detail"] = ticket
        created = [item for item in created if item not in blocked_ids]
    trail["items"] = steps
    outcome, summary = _trail_outcome(steps)
    write_decision(conn, account_id, symbol, "consolidator", outcome, summary, trail)
    return created


def _propose_one(conn, settings, account_id, symbol, phase, tactic, limit, reason, answers) -> dict:
    label = _label(tactic)
    if tactic == "BUY":
        if limit is None or limit <= 0:
            return _step(label, "dropped", "missing limit")
        contracts = open_quantity(conn, settings, account_id, symbol, "1")
        staged = stage_draft(
            conn, settings, symbol, tactic, contracts, reason, phase, account_id,
            instrument="STK", limit_price=limit, multiplier="1",
        )
        return _staged_step(label, staged)
    if tactic == "OPEN_CSP":
        source = answers.get("CSP") or {}
        if source.get("quiet") is True:
            return _step(label, "dropped", "CSP agent was quiet")
        expiry = str(source.get("expiry") or "")
        strike = _number(source.get("strike"))
        price = limit if limit and limit > 0 else _number(source.get("limit"))
        if not expiry or strike is None or price is None:
            return _step(label, "dropped", "missing expiry, strike, or limit")
        plan = _plan(conn, symbol)
        contracts = open_quantity(conn, settings, account_id, symbol, (plan or {}).get("multiplier") or "100")
        staged = stage_draft(
            conn, settings, symbol, tactic, contracts, reason, phase, account_id,
            instrument="OPT", limit_price=price, expiry=expiry, put_strike=strike, put_limit=price,
            trading_class=(plan or {}).get("trading_class") or "",
            option_exchange=(plan or {}).get("option_exchange") or "",
            multiplier=(plan or {}).get("multiplier") or "100",
        )
        return _staged_step(label, staged)
    if tactic == "OPEN_CC":
        source = answers.get("Call") or {}
        if source.get("quiet") is True:
            return _step(label, "dropped", "Call agent was quiet")
        expiry = str(source.get("expiry") or "")
        strike = _number(source.get("strike"))
        price = limit if limit and limit > 0 else _number(source.get("limit"))
        if not expiry or strike is None or price is None:
            return _step(label, "dropped", "missing expiry, strike, or limit")
        plan = _plan(conn, symbol)
        contracts = open_quantity(conn, settings, account_id, symbol, (plan or {}).get("multiplier") or "100")
        staged = stage_draft(
            conn, settings, symbol, tactic, contracts, reason, phase, account_id,
            instrument="OPT", limit_price=price, expiry=expiry, call_strike=strike, call_limit=price,
            trading_class=(plan or {}).get("trading_class") or "",
            option_exchange=(plan or {}).get("option_exchange") or "",
            multiplier=(plan or {}).get("multiplier") or "100",
        )
        return _staged_step(label, staged)
    source = answers.get("Strangle") or {}
    if source.get("quiet") is True:
        return _step(label, "dropped", "Strangle agent was quiet")
    expiry = str(source.get("expiry") or "")
    put_strike = _number(source.get("put_strike"))
    call_strike = _number(source.get("call_strike"))
    put_limit = _number(source.get("put_limit"))
    call_limit = _number(source.get("call_limit"))
    if not expiry or None in {put_strike, call_strike, put_limit, call_limit}:
        return _step(label, "dropped", "missing expiry or strikes")
    plan = _plan(conn, symbol)
    contracts = open_quantity(conn, settings, account_id, symbol, (plan or {}).get("multiplier") or "100")
    staged = stage_draft(
        conn, settings, symbol, tactic, contracts, reason, phase, account_id,
        instrument="OPT", limit_price=put_limit + call_limit, expiry=expiry,
        put_strike=put_strike, call_strike=call_strike, put_limit=put_limit, call_limit=call_limit,
        trading_class=(plan or {}).get("trading_class") or "",
        option_exchange=(plan or {}).get("option_exchange") or "",
        multiplier=(plan or {}).get("multiplier") or "100",
    )
    return _staged_step(label, staged)


def write_decision(conn, account_id: str, symbol: str, source: str, outcome: str, summary: str, steps: dict) -> None:
    """One decision row. steps holds the context and the ordered reasons."""
    name = symbol.upper() if symbol else "—"
    conn.execute(
        """
        INSERT INTO decision_trails (account_id, symbol, source, outcome, summary, steps, simulated)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (account_id, name, source, outcome, summary, Jsonb(steps), _simulated(conn, name)),
    )
    conn.commit()


def _step(tactic: str, outcome: str, detail: str, draft_id: str = "") -> dict:
    return {"tactic": tactic, "outcome": outcome, "detail": detail, "draft_id": draft_id or ""}


def _label(tactic: str) -> str:
    return {"BUY": "BUY", "OPEN_CSP": "CSP", "OPEN_CC": "CC", "OPEN_STRANGLE": "STRANGLE"}.get(tactic, tactic)


def _staged_step(label: str, staged: dict) -> dict:
    if staged["status"] == "pending":
        return _step(label, "kept", staged["detail"], staged["id"] or "")
    if staged["status"] == "blocked":
        return _step(label, "blocked", staged["detail"], staged["id"] or "")
    return _step(label, "dropped", staged["detail"])


def _trail_context(context: dict, flags: TacticFlags, phase: str, choice: dict) -> dict:
    tags = context.get("tags") or "none"
    thinking = choice.get("thinking") if isinstance(choice.get("thinking"), str) else ""
    trail = {
        "phase": phase,
        "intent": flags.intent,
        "long": "on" if flags.long else "off",
        "flags": context.get("flags") or "",
        "tags": tags,
        "thinking": thinking,
        "items": [],
    }
    if context.get("numbers"):
        trail["numbers"] = context["numbers"]
    if context.get("indicators"):
        trail["indicators"] = context["indicators"]
    if context.get("unknown_tags"):
        trail["unknown_tags"] = context["unknown_tags"]
    if context.get("otm_clamped"):
        trail["otm_clamped"] = context["otm_clamped"]
    if context.get("cc_block"):
        trail["cc_block"] = context["cc_block"]
    if context.get("delta_source"):
        trail["delta_source"] = context["delta_source"]
    return trail


def _trail_outcome(steps: list[dict]) -> tuple[str, str]:
    if any(step.get("outcome") == "kept" for step in steps):
        summary = ", ".join(
            f"{step.get('tactic') or 'draft'} {step.get('outcome')}" for step in steps
        ) or "QUIET"
        return "drafted", summary
    if not steps:
        return "quiet", "QUIET"
    if all(step.get("outcome") in {"dropped", "blocked"} for step in steps):
        summary = ", ".join(str(step.get("detail") or step.get("outcome") or "") for step in steps)
        return "quiet", summary or "QUIET"
    summary = ", ".join(
        f"{step.get('tactic') or 'draft'} {step.get('outcome')}" for step in steps
    ) or "QUIET"
    return "dropped", summary


def record_call(conn, account_id, agent, symbol, model, provider, thinking, result, payload) -> None:
    """Store one model call on the audit list."""
    _record(conn, account_id, agent, symbol, model, provider, thinking, result, payload)


def _simulated(conn, symbol: str) -> bool:
    """True while this symbol is inside a paper replay. Live calls stay untagged."""
    if not symbol or symbol == "—":
        return False
    from athena_strategy.simbars import in_replay

    return in_replay(conn, symbol)


def _record(conn, account_id, agent, symbol, model, provider, thinking, result, payload, call_status: str = "") -> None:
    status = call_status or ("error" if result["error"] else "ok")
    line = _result_line(agent, payload, result["error"])
    if status == "invalid":
        line = f"invalid {payload.get('stage') or 'schema'}"
    name = symbol.upper()
    conn.execute(
        """
        INSERT INTO ai_calls (
            account_id, agent, symbol, model, provider, thinking,
            latency_ms, tokens, status, result, response, request, simulated
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            account_id, agent, name, model, provider, thinking,
            result["latency_ms"], result["tokens"], status,
            line,
            Jsonb(payload or {"error": result["error"] or "empty"}),
            Jsonb(result["request"]) if isinstance(result.get("request"), dict) else None,
            _simulated(conn, name),
        ),
    )
    conn.commit()


def _result_line(agent: str, payload: dict, error: str) -> str:
    if error:
        return error
    if agent == "Location":
        tags = payload.get("tags") if isinstance(payload.get("tags"), list) else []
        return ", ".join(str(tag) for tag in tags) or "QUIET"
    if agent == "News":
        if payload.get("news_wash"):
            return "News wash"
        return str(payload.get("event_type") or "QUIET")
    if agent in {"CSP", "Call"}:
        if payload.get("quiet") is not False:
            return "QUIET"
        return f"{payload.get('strike')} limit {payload.get('limit')}"
    if agent == "Purchase":
        if payload.get("quiet") is not False:
            return "QUIET"
        return f"Buy {payload.get('limit')}"
    if agent == "Strangle":
        if payload.get("quiet") is not False:
            return "QUIET"
        return f"Put {payload.get('put_strike')} call {payload.get('call_strike')}"
    if agent == "Universe":
        codes = str(payload.get("classifiers") or "")
        return codes.replace(",", ", ") or "QUIET"
    if agent == TRADE_NAME:
        limit = payload.get("limit") or ""
        stop = payload.get("stop") or ""
        if not limit:
            return "QUIET"
        return f"limit {limit}" + (f" stop {stop}" if stop else "")
    drafts = payload.get("drafts") if isinstance(payload.get("drafts"), list) else []
    if not drafts:
        return "QUIET"
    return ", ".join(
        f"{item.get('id') or item.get('tactic')}" for item in drafts if isinstance(item, dict)
    )


def _tag_set(context: dict[str, Any], answers: dict[str, dict]) -> list[str]:
    raw = [part for part in str(context.get("base_tags") or "").split(",") if part]
    unknown = unknown_names(raw)
    iv_owned = names_owned_by("iv")
    indicator_owned = names_owned_by("indicators")
    tags = [tag for tag in raw if tag in TAG_REGISTRY and tag not in iv_owned and not TAG_REGISTRY[tag].deprecated]
    location = answers.get("Location") or {}
    if location and not location.get("error"):
        location_owned = names_owned_by("location")
        tags = [tag for tag in tags if tag not in location_owned]
        for tag in location.get("tags") or []:
            text = str(tag)
            if text in location_owned and text not in tags:
                tags.append(text)
            elif text and text not in TAG_REGISTRY:
                unknown.extend(unknown_names([text]))
    news = answers.get("News") or {}
    if not news.get("error"):
        tags = [tag for tag in tags if tag not in names_owned_by("news")]
        if news.get("news_wash") and NEWS_WASH not in tags:
            tags.append(NEWS_WASH)
    tags = [tag for tag in tags if tag not in indicator_owned]
    for tag in str(context.get("indicator_tags") or "").split(","):
        if tag and tag not in tags:
            tags.append(tag)
    if _pullback(context, tags) and PULLBACK not in tags:
        tags.append(PULLBACK)
    context["unknown_tags"] = unknown
    return tags


def _pullback(context: dict[str, Any], tags: list[str]) -> bool:
    """A mega-cap dip in the deep part of a rising channel."""
    if CHANNEL_RISING not in tags:
        return False
    codes = {part.strip() for part in (context.get("classifiers") or "").split(",") if part.strip()}
    if not codes & {"MAG7", "MEGA_CAP"}:
        return False
    last = _decimal(context.get("last"))
    high = _decimal(context.get("high_63"))
    low = _decimal(context.get("low_63"))
    if last is None or high is None or low is None or high <= low:
        return False
    raw = context.get("dip_deep_pos")
    limit = Decimal(str(raw)) if raw else Decimal(str(StrategySettings().dip_deep_pos))
    return (last - low) / (high - low) <= limit


def _decimal(value: str | None) -> Decimal | None:
    if not value or value == "unknown":
        return None
    try:
        return Decimal(value)
    except Exception:
        return None


def _position_cap(value: int) -> str:
    """Zero is the switch for no money cap. The model must not read it as a ban."""
    if value <= 0:
        return "off"
    return str(value)


def _proposal_text(payload: dict) -> str:
    if not payload or payload.get("quiet") is not False or payload.get("error"):
        return "none"
    return json.dumps(payload, separators=(",", ":"))


def _proposal_block(answers: dict[str, dict]) -> str:
    lines = []
    for name in ("CSP", "Call", "Purchase", "Strangle"):
        lines.append(f"{name}: {_proposal_text(answers.get(name) or {})}")
    return "\n".join(lines)


def kept_prompt(stored: set[str]) -> set[str]:
    """Prompt base keeps price and location. IV aliases and indicator tags are recomputed."""
    return kept_stored(stored, drop_owners=frozenset({"iv", "indicators"}))


def _reading(conn, settings: SettingsPayload, account_id: str, symbol: str) -> Indicators | None:
    if not indicators_enabled():
        return None
    reading = read_indicators(conn, settings, symbol)
    store_reading(conn, account_id, symbol, reading)
    return reading


def _context(conn, settings: SettingsPayload, account_id: str, symbol: str, flags: TacticFlags) -> dict[str, Any]:
    bars = _bars(conn, symbol)
    last = _last_close(conn, symbol)
    shares = _shares(conn, account_id, symbol)
    plan = _plan(conn, symbol)
    _current, stored = _load(conn, account_id, symbol)
    iv_tags = tags_from_iv(conn, symbol, settings)
    reading = _reading(conn, settings, account_id, symbol)
    fresh = set() if reading is None else set(reading.tags)
    base = sorted(kept_prompt(stored or set()) | iv_tags | fresh)
    from athena_strategy.screener import prompt_sides

    puts, calls, clamped, cc_block, delta_source = prompt_sides(
        conn, settings, account_id, symbol, reading, plan, last, date.today(), flags,
    )
    risk = settings.risk
    return {
        "symbol": symbol.upper(),
        "last": _show(last),
        "high_63": _show(_rail(bars, "high", 63)),
        "low_63": _show(_rail(bars, "low", 63, high=False)),
        "high_63_prior": _show(_prior_rail(bars, "high")),
        "low_63_prior": _show(_prior_rail(bars, "low", high=False)),
        "high_252": _show(_rail(bars, "high", 252)),
        "low_252": _show(_rail(bars, "low", 252, high=False)),
        "headlines": _headlines(conn, symbol),
        "tags": "none",
        "base_tags": ",".join(base),
        "numbers": "" if reading is None else reading.line(),
        "indicator_tags": "" if reading is None else ",".join(sorted(reading.tags)),
        "indicators": {} if reading is None else reading.payload(),
        "dip_deep_pos": str(settings.strategy.dip_deep_pos),
        "intent": flags.intent,
        "long": "on" if flags.long else "off",
        "shares": _show(shares),
        "puts": puts,
        "calls": calls,
        "otm_clamped": clamped or "",
        "cc_block": cc_block or "",
        "delta_source": delta_source or "",
        "purchase": "none",
        "classifiers": _classifiers(conn, account_id, symbol),
        "position_cap": _position_cap(risk.max_position_per_symbol),
        "contract_cap": str(risk.max_contracts_per_symbol),
        "min_dte": str(risk.min_dte_new),
        "earnings_blackout": str(risk.earnings_blackout_days),
        "cash_reserve": str(risk.cash_reserve_pct),
        "flags": f"csp={'on' if flags.csp else 'off'} cc={'on' if flags.cc else 'off'} strangle={'on' if flags.strangle else 'off'}",
        "proposals": "none",
        **range_names(_range_sessions(conn, symbol, bars)),
    }


def _range_sessions(conn, symbol: str, bars: list[Bar]) -> int:
    from athena_marketdata.pricing import ib_daily_count
    from athena_strategy.simbars import in_replay

    if in_replay(conn, symbol):
        return len(bars)
    return ib_daily_count(conn, symbol)


def _bars(conn, symbol: str) -> list[Bar]:
    from athena_strategy.simbars import in_replay, load_daily

    if in_replay(conn, symbol):
        found = []
        for row in load_daily(conn, symbol)[-273:]:
            found.append(
                Bar(
                    day=date.min,
                    open=row[2],
                    high=row[0],
                    low=row[1],
                    close=row[2],
                    volume=row[3] if len(row) > 3 else Decimal(0),
                )
            )
        return found
    return daily_bars(conn, symbol, 273)


def _rail(bars: list[Bar], field: str, sessions: int, high: bool = True):
    window = bars[-sessions:]
    if not window:
        return None
    values = [getattr(row, field) for row in window if getattr(row, field) is not None]
    if not values:
        return None
    return max(values) if high else min(values)


def _prior_rail(bars: list[Bar], field: str, high: bool = True):
    if len(bars) < 22:
        return None
    window = bars[:-21][-63:]
    values = [getattr(row, field) for row in window if getattr(row, field) is not None]
    if not values:
        return None
    return max(values) if high else min(values)


def _last_close(conn, symbol: str):
    from athena_strategy.simbars import in_replay, last_close

    if in_replay(conn, symbol):
        return last_close(conn, symbol)
    return last_daily_close(conn, symbol)


def _shares(conn, account_id: str, symbol: str):
    from athena_strategy.simbars import in_replay

    row = conn.execute(
        """
        SELECT quantity FROM positions
        WHERE account_id = %s AND symbol = %s AND sec_type = 'STK' AND simulated = %s
        ORDER BY snapshot_at DESC, id DESC
        LIMIT 1
        """,
        (account_id, symbol.upper(), in_replay(conn, symbol)),
    ).fetchone()
    if row is None or row[0] is None:
        return 0
    return row[0]


def _headlines(conn, symbol: str) -> str:
    rows = conn.execute(
        """
        SELECT title FROM news_events
        WHERE symbol = %s
        ORDER BY ts DESC
        LIMIT 8
        """,
        (symbol.upper(),),
    ).fetchall()
    if not rows:
        return "none"
    return "\n".join(f"- {row[0]}" for row in rows)


def _plan(conn, symbol: str) -> dict | None:
    row = conn.execute(
        """
        SELECT expiry, put_strike, call_strike, put_limit, call_limit, trading_class, option_exchange, multiplier
        FROM option_plans WHERE symbol = %s
        """,
        (symbol.upper(),),
    ).fetchone()
    if row is None:
        return None
    return {
        "expiry": row[0],
        "put_strike": row[1],
        "call_strike": row[2],
        "put_limit": row[3],
        "call_limit": row[4],
        "trading_class": row[5] or "",
        "option_exchange": row[6] or "",
        "multiplier": row[7] or "100",
    }


def _classifiers(conn, account_id: str, symbol: str) -> str:
    row = conn.execute(
        "SELECT classifiers FROM symbol_phases WHERE account_id = %s AND symbol = %s",
        (account_id, symbol.upper()),
    ).fetchone()
    if row is None or not row[0]:
        return ""
    return row[0]


def _show(value) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, Decimal):
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text
    return str(value)


def _number(value):
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    if number <= 0:
        return None
    return number
