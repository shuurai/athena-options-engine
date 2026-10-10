"""Signaled entry. Taggers and tactic agents run side by side. The consolidator chooses drafts."""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb

from athena_settings.model import SettingsPayload, TacticFlags
from athena_settings.prompts import RUN_ORDER, resolved_agents
from athena_settings.resolve import deepseek_token, scoring_model
from athena_strategy.drafts import has_open_draft, open_quantity, stage_draft
from athena_strategy.dsh import chat
from athena_strategy.iv import tags_from_iv
from athena_strategy.phases import _load

log = logging.getLogger("athena.strategy")

_TACTICS = {"BUY": "BUY", "CSP": "OPEN_CSP", "CC": "OPEN_CC", "STRANGLE": "OPEN_STRANGLE"}


def fill_prompt(template: str, values: dict[str, str]) -> str:
    text = template
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
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


def run_signaled(conn: psycopg.Connection, settings: SettingsPayload, account_id: str) -> list[str]:
    """One signaled-entry pass. Returns new draft ids. A failed symbol does not stop the rest."""
    created: list[str] = []
    blocked = {item.upper() for item in settings.blocked_symbols}
    for symbol in settings.watchlist:
        if symbol.upper() in blocked:
            write_decision(
                conn, account_id, symbol, "consolidator", "dropped", "skipped, symbol blocked",
                {"items": [_step("", "dropped", "symbol blocked")]},
            )
            continue
        try:
            created.extend(_run_symbol(conn, settings, account_id, symbol))
        except Exception as exc:
            log.error("signaled entry failed symbol=%s error=%s", symbol, exc.__class__.__name__)
            write_decision(
                conn, account_id, symbol, "consolidator", "dropped", exc.__class__.__name__,
                {"items": [_step("", "dropped", exc.__class__.__name__)]},
            )
    return created


def _run_symbol(conn, settings: SettingsPayload, account_id: str, symbol: str) -> list[str]:
    prompts = resolved_agents(settings)
    flags = settings.tactic_flags.get(symbol.upper(), TacticFlags())
    context = _context(conn, settings, account_id, symbol, flags)
    answers: dict[str, dict] = {}
    model = scoring_model(settings)
    provider = settings.ai.provider
    token = deepseek_token(settings)
    for name in RUN_ORDER:
        if name in {"CSP", "Purchase", "Strangle", "Call", "Consolidator"}:
            context["tags"] = ", ".join(_tag_set(context, answers)) or "none"
        if name == "Call":
            context["purchase"] = _proposal_text(answers.get("Purchase") or {})
        if name == "Consolidator":
            context["proposals"] = _proposal_block(answers)
        prompt = prompts[name]
        thinking = name == "Consolidator"
        result = chat(
            prompt.system,
            fill_prompt(prompt.execution, context),
            model,
            provider,
            token,
            thinking,
        )
        payload = parse_json(result["text"]) if not result["error"] else {"error": result["error"]}
        answers[name] = payload
        _record(
            conn,
            account_id,
            name,
            symbol,
            model,
            provider,
            "on" if thinking and provider == "DIRECT" else "off",
            result,
            payload,
        )
    return _drafts(conn, settings, account_id, symbol, flags, answers, context)


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
        write_decision(conn, account_id, symbol, "consolidator", "quiet", "Quiet", trail)
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
        if tactic == "BUY" and (not flags.long or flags.intent != "accumulate"):
            why = "Long is off" if not flags.long else "intent is not Accumulate"
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
    conn.execute(
        """
        INSERT INTO decision_trails (account_id, symbol, source, outcome, summary, steps)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (account_id, symbol.upper() if symbol else "—", source, outcome, summary, Jsonb(steps)),
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
    return {
        "phase": phase,
        "intent": flags.intent,
        "long": "on" if flags.long else "off",
        "flags": context.get("flags") or "",
        "tags": tags,
        "thinking": thinking,
        "items": [],
    }


def _trail_outcome(steps: list[dict]) -> tuple[str, str]:
    if any(step.get("outcome") == "kept" for step in steps):
        outcome = "drafted"
    elif steps and all(step.get("outcome") == "dropped" and step.get("detail") == "no drafts" for step in steps):
        outcome = "quiet"
    else:
        outcome = "dropped"
    summary = ", ".join(
        f"{step.get('tactic') or 'draft'} {step.get('outcome')}" for step in steps
    ) or "Quiet"
    return outcome, summary


def record_call(conn, account_id, agent, symbol, model, provider, thinking, result, payload) -> None:
    """Store one model call on the audit list."""
    _record(conn, account_id, agent, symbol, model, provider, thinking, result, payload)


def _record(conn, account_id, agent, symbol, model, provider, thinking, result, payload) -> None:
    status = "error" if result["error"] else "ok"
    conn.execute(
        """
        INSERT INTO ai_calls (
            account_id, agent, symbol, model, provider, thinking,
            latency_ms, tokens, status, result, response
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            account_id, agent, symbol.upper(), model, provider, thinking,
            result["latency_ms"], result["tokens"], status,
            _result_line(agent, payload, result["error"]),
            Jsonb(payload or {"error": result["error"] or "empty"}),
        ),
    )
    conn.commit()


def _result_line(agent: str, payload: dict, error: str) -> str:
    if error:
        return error
    if agent == "Location":
        tags = payload.get("tags") if isinstance(payload.get("tags"), list) else []
        return ", ".join(str(tag) for tag in tags) or "Quiet"
    if agent == "News":
        if payload.get("news_wash"):
            return "News wash"
        return str(payload.get("event_type") or "Quiet")
    if agent in {"CSP", "Call"}:
        if payload.get("quiet") is not False:
            return "Quiet"
        return f"{payload.get('strike')} limit {payload.get('limit')}"
    if agent == "Purchase":
        if payload.get("quiet") is not False:
            return "Quiet"
        return f"Buy {payload.get('limit')}"
    if agent == "Strangle":
        if payload.get("quiet") is not False:
            return "Quiet"
        return f"Put {payload.get('put_strike')} call {payload.get('call_strike')}"
    if agent == "Universe":
        codes = str(payload.get("classifiers") or "")
        return codes.replace(",", ", ") or "Quiet"
    drafts = payload.get("drafts") if isinstance(payload.get("drafts"), list) else []
    if not drafts:
        return "Quiet"
    return ", ".join(
        f"{item.get('id') or item.get('tactic')}" for item in drafts if isinstance(item, dict)
    )


def _tag_set(context: dict[str, str], answers: dict[str, dict]) -> list[str]:
    tags = [part for part in context["base_tags"].split(",") if part]
    location = answers.get("Location") or {}
    for tag in location.get("tags") or []:
        text = str(tag)
        if text and text not in tags:
            tags.append(text)
    news = answers.get("News") or {}
    if news.get("news_wash") and "NEWS_WASH" not in tags:
        tags.append("NEWS_WASH")
    return tags


def _proposal_text(payload: dict) -> str:
    if not payload or payload.get("quiet") is not False or payload.get("error"):
        return "none"
    return json.dumps(payload, separators=(",", ":"))


def _proposal_block(answers: dict[str, dict]) -> str:
    lines = []
    for name in ("CSP", "Call", "Purchase", "Strangle"):
        lines.append(f"{name}: {_proposal_text(answers.get(name) or {})}")
    return "\n".join(lines)


def _context(conn, settings: SettingsPayload, account_id: str, symbol: str, flags: TacticFlags) -> dict[str, str]:
    bars = _bars(conn, symbol)
    last = _last_close(conn, symbol)
    shares = _shares(conn, account_id, symbol)
    plan = _plan(conn, symbol)
    _current, stored = _load(conn, account_id, symbol)
    iv_tags = tags_from_iv(conn, symbol, settings)
    base = sorted((stored or set()) | iv_tags)
    puts, calls = _listed(plan)
    return {
        "symbol": symbol.upper(),
        "last": _show(last),
        "high_63": _show(_rail(bars, 0, 63)),
        "low_63": _show(_rail(bars, 1, 63, high=False)),
        "high_63_prior": _show(_prior_rail(bars, 0)),
        "low_63_prior": _show(_prior_rail(bars, 1, high=False)),
        "high_252": _show(_rail(bars, 0, 252)),
        "low_252": _show(_rail(bars, 1, 252, high=False)),
        "headlines": _headlines(conn, symbol),
        "tags": "none",
        "base_tags": ",".join(base),
        "intent": flags.intent,
        "long": "on" if flags.long else "off",
        "shares": _show(shares),
        "puts": puts,
        "calls": calls,
        "purchase": "none",
        "position_cap": str(settings.risk.max_position_per_symbol),
        "contract_cap": str(settings.risk.max_contracts_per_symbol),
        "flags": f"csp={'on' if flags.csp else 'off'} cc={'on' if flags.cc else 'off'} strangle={'on' if flags.strangle else 'off'}",
        "proposals": "none",
    }


def _bars(conn, symbol: str) -> list[tuple]:
    rows = conn.execute(
        """
        SELECT high, low, close FROM market_bars
        WHERE symbol = %s
        ORDER BY ts DESC
        LIMIT 273
        """,
        (symbol.upper(),),
    ).fetchall()
    return list(reversed(rows))


def _rail(bars: list[tuple], index: int, sessions: int, high: bool = True):
    window = bars[-sessions:]
    if not window:
        return None
    values = [row[index] for row in window if row[index] is not None]
    if not values:
        return None
    return max(values) if high else min(values)


def _prior_rail(bars: list[tuple], index: int, high: bool = True):
    if len(bars) < 22:
        return None
    window = bars[:-21][-63:]
    values = [row[index] for row in window if row[index] is not None]
    if not values:
        return None
    return max(values) if high else min(values)


def _last_close(conn, symbol: str):
    row = conn.execute(
        "SELECT close FROM market_bars WHERE symbol = %s ORDER BY ts DESC LIMIT 1",
        (symbol.upper(),),
    ).fetchone()
    return None if row is None else row[0]


def _shares(conn, account_id: str, symbol: str):
    row = conn.execute(
        """
        SELECT quantity FROM positions
        WHERE account_id = %s AND symbol = %s AND sec_type = 'STK'
        ORDER BY snapshot_at DESC, id DESC
        LIMIT 1
        """,
        (account_id, symbol.upper()),
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


def _listed(plan: dict | None) -> tuple[str, str]:
    if plan is None:
        return "none", "none"
    expiry = plan["expiry"]
    put = f"{expiry} put { _show(plan['put_strike']) } limit { _show(plan['put_limit']) }"
    call = f"{expiry} call { _show(plan['call_strike']) } limit { _show(plan['call_limit']) }"
    return put, call


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
