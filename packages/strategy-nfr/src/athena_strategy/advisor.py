"""Orchestrator Advisor: take-profit / trim guidance per held position.

Advisory only. Writes advisor_signals; manage_book may act on them.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from athena_marketdata.pricing import Bar, daily_bars
from athena_settings.model import SettingsPayload
from athena_settings.prompts import resolved_agents
from athena_settings.resolve import deepseek_token, scoring_model
from athena_strategy.agents import _ask, fill_prompt
from athena_strategy.drafts import last_tradeable_price
from athena_strategy.draft_facts import moneyness, premium_captured_pct
from athena_strategy.news import tags_from_events

log = logging.getLogger("athena.strategy.advisor")

HKT = ZoneInfo("Asia/Hong_Kong")
_ACTIONS = {"hold", "take_profit", "trim", "close"}


def run_advisor(
    conn: psycopg.Connection,
    settings: SettingsPayload,
    account_id: str,
    today: date | None = None,
) -> list[dict]:
    """Write one advisor signal per held symbol. Returns the rows for manage / page."""
    if not settings.advisor.enabled:
        return []
    day = today or date.today()
    rows = conn.execute(
        """
        SELECT DISTINCT ON (symbol, sec_type, expiry, contract_right, strike)
            symbol, quantity, avg_price, sec_type, expiry, contract_right, strike, market_price, currency
        FROM positions
        WHERE account_id = %s AND quantity <> 0
        ORDER BY symbol, sec_type, expiry, contract_right, strike, snapshot_at DESC
        """,
        (account_id,),
    ).fetchall()
    if not rows:
        return []
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row[0], []).append(row)
    guidance = _guidance_snapshot(conn, settings, account_id, day)
    out: list[dict] = []
    for symbol, legs in grouped.items():
        signal = _symbol_advice(conn, settings, account_id, symbol, legs, guidance, day)
        if signal is None:
            continue
        out.append(signal)
        _store_signal(conn, account_id, signal)
    return out


def _guidance_snapshot(conn: psycopg.Connection, settings: SettingsPayload, account_id: str, day: date) -> dict:
    """Average dir_conf + notes for configured guidance symbols."""
    adv = settings.advisor
    rows = conn.execute(
        """
        SELECT symbol, payload FROM forecast_marks
        WHERE account_id = %s AND day = %s AND symbol = ANY(%s)
        """,
        (account_id, day, [s.upper() for s in adv.guidance_symbols]),
    ).fetchall()
    notes: list[str] = []
    total = 0.0
    count = 0
    for symbol, payload in rows:
        data = payload if isinstance(payload, dict) else {}
        conf = _dir_conf(data)
        if conf is None:
            continue
        total += conf
        count += 1
        notes.append(f"{symbol} dir_conf {conf:.2f}")
    avg = total / count if count else None
    label = "weak" if avg is not None and avg < 0.45 else ("neutral" if avg is not None and avg < 0.6 else "strong" if avg is not None else "unknown")
    symbols = [s.upper() for s in adv.guidance_symbols]
    notes.extend(_ib_quotes(conn, symbols))
    notes.extend(_yahoo_notes(conn, symbols))
    notes.extend(_calendar_lines(conn, symbols, day))
    return {
        "symbols": symbols,
        "avg_dir_conf": avg,
        "label": label,
        "notes": "; ".join(notes) if notes else "no guidance forecast",
    }


def _symbol_advice(
    conn: psycopg.Connection,
    settings: SettingsPayload,
    account_id: str,
    symbol: str,
    legs: list,
    guidance: dict,
    day: date,
) -> dict | None:
    adv = settings.advisor
    spot = last_tradeable_price(conn, symbol)
    if spot is None:
        return _hold(symbol, "no spot")
    facts = _fact_lines(conn, settings, account_id, symbol, legs, spot, guidance, day)
    forecast = _latest_forecast(conn, account_id, symbol, day)
    events = tags_from_events(conn, symbol, settings, day)
    prompt = resolved_agents(settings).get("AdvisorExplainer")
    if prompt is None:
        high = _year_high(conn, symbol)
        near = high is not None and spot >= high * Decimal("0.98")
        return _rule_fallback(account_id, symbol, legs, spot, guidance, forecast, adv, day, facts, near)
    context = {
        "symbol": symbol,
        "position": _position_summary(legs, spot),
        "forecast": _forecast_summary(forecast),
        "guidance": guidance["notes"],
        "events": ",".join(sorted(events)) if events else "none",
        "facts": "\n".join(facts),
    }
    model = scoring_model(settings)
    provider = settings.ai.provider
    token = deepseek_token(settings)
    payload, stop = _ask(
        conn, account_id, "AdvisorExplainer", symbol, model, provider, token,
        prompt.system, fill_prompt(prompt.execution, context), bool(prompt.reasoning),
    )
    if stop or not payload or payload.get("error"):
        high = _year_high(conn, symbol)
        near = high is not None and spot >= high * Decimal("0.98")
        return _rule_fallback(account_id, symbol, legs, spot, guidance, forecast, adv, day, facts, near)
    action = str(payload.get("action") or "hold").strip().lower()
    confidence = float(payload.get("confidence") or 0.0)
    reason = str(payload.get("reason") or "").strip()
    if action not in _ACTIONS:
        action = "hold"
    if action == "hold" and confidence < adv.trim_conf_threshold:
        confidence = min(confidence, adv.trim_conf_threshold)
    confidence = min(confidence, adv.max_conf)
    return {
        "account_id": account_id,
        "symbol": symbol,
        "day": day,
        "action": action,
        "confidence": confidence,
        "reason": reason or facts[0] if facts else "advisor",
        "payload": {
            "facts": facts,
            "guidance": guidance,
            "forecast": forecast,
            "events": sorted(events),
            "llm": True,
        },
    }


def _rule_fallback(
    account_id: str,
    symbol: str,
    legs: list,
    spot: Decimal,
    guidance: dict,
    forecast: dict | None,
    adv,
    day: date,
    facts: list[str],
    near_high: bool = False,
) -> dict:
    """Deterministic verdict when the LLM is unavailable."""
    score = 0.0
    why: list[str] = []
    if guidance["label"] == "weak":
        score += 0.35
        why.append("guidance weak")
    conf = _dir_conf(forecast or {})
    if conf is not None and conf < 0.45:
        score += 0.3
        why.append(f"forecast dir_conf {conf:.2f}")
    if near_high:
        score += 0.2
        why.append("near range high")
    captured = _max_capture(legs, spot)
    if captured is not None and captured >= 40:
        score += 0.15
        why.append(f"premium captured {captured:.0f}%")
    action = "hold"
    if score >= adv.close_conf_threshold:
        action = "take_profit"
    elif score >= adv.trim_conf_threshold:
        action = "trim"
    reason = "; ".join(why) if why else "no elevated risk"
    return {
        "account_id": account_id,
        "symbol": symbol,
        "day": day,
        "action": action,
        "confidence": min(score, adv.max_conf),
        "reason": reason,
        "payload": {"facts": facts, "guidance": guidance, "forecast": forecast, "llm": False},
    }


def _store_signal(conn: psycopg.Connection, account_id: str, signal: dict) -> None:
    conn.execute(
        """
        INSERT INTO advisor_signals (account_id, symbol, day, action, confidence, reason, payload)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (account_id, symbol, day) DO UPDATE
        SET action = EXCLUDED.action,
            confidence = EXCLUDED.confidence,
            reason = EXCLUDED.reason,
            payload = EXCLUDED.payload,
            created_at = now()
        """,
        (
            account_id,
            signal["symbol"],
            signal["day"],
            signal["action"],
            signal["confidence"],
            signal["reason"],
            Jsonb(signal.get("payload") or {}),
        ),
    )


def today_signals(conn: psycopg.Connection, account_id: str, day: date | None = None) -> dict[str, dict]:
    """Latest advisor signal per symbol for today."""
    day = day or date.today()
    rows = conn.execute(
        """
        SELECT symbol, action, confidence, reason, payload
        FROM advisor_signals
        WHERE account_id = %s AND day = %s
        """,
        (account_id, day),
    ).fetchall()
    return {
        row[0]: {"action": row[1], "confidence": row[2], "reason": row[3], "payload": row[4] or {}}
        for row in rows
    }


def _fact_lines(conn, settings, account_id, symbol, legs, spot, guidance, day) -> list[str]:
    lines: list[str] = []
    lines.append(f"Spot {spot:.2f}.")
    high = _year_high(conn, symbol)
    if high is not None and spot >= high * Decimal("0.98"):
        lines.append(f"Near 52w high {high:.2f} (within 2%).")
    for row in legs:
        _, quantity, avg_price, sec_type, expiry, right, strike, market_price, currency = row
        qty = Decimal(quantity)
        if sec_type == "OPT" and qty < 0 and strike is not None:
            money = moneyness(spot, Decimal(strike), right)
            captured = premium_captured_pct(Decimal(avg_price), None if market_price is None else Decimal(market_price))
            dte = (expiry - day).days if expiry else None
            lines.append(
                f"Short {abs(qty):.0f}x {strike}{right}: {money or '—'}, "
                f"captured {captured:.0f}%" if captured is not None else f"Short {abs(qty):.0f}x {strike}{right}: {money or '—'}"
                + (f", DTE {dte}." if dte is not None else ".")
            )
        elif sec_type == "STK":
            pnl = (spot - Decimal(avg_price)) / Decimal(avg_price) * Decimal(100) if avg_price else None
            lines.append(f"Long {qty:.0f} shares avg {Decimal(avg_price):.2f}, PnL {pnl:.1f}%." if pnl is not None else f"Long {qty:.0f} shares.")
    lines.append(f"Guidance basket: {guidance['label']}.")
    if guidance.get("notes"):
        lines.append(f"Guidance detail: {guidance['notes']}.")
    lines.extend(_calendar_lines(conn, [symbol], day, fed=False))
    return lines


def _position_summary(legs, spot) -> str:
    parts = []
    for row in legs:
        _, quantity, avg_price, sec_type, expiry, right, strike, *_ = row
        qty = Decimal(quantity)
        if sec_type == "OPT":
            parts.append(f"short {abs(qty):.0f} {strike}{right} {expiry}")
        else:
            parts.append(f"long {qty:.0f} shares avg {Decimal(avg_price):.2f}")
    return "; ".join(parts)


def _forecast_summary(forecast: dict | None) -> str:
    if not forecast:
        return "none"
    conf = _dir_conf(forecast)
    tp = _take_profit(forecast)
    bits = []
    if conf is not None:
        bits.append(f"dir_conf {conf:.2f}")
    if tp is not None:
        bits.append(f"take_profit {tp}")
    return ", ".join(bits) if bits else "stored"


def _latest_forecast(conn, account_id, symbol, day) -> dict | None:
    row = conn.execute(
        """
        SELECT payload FROM forecast_marks
        WHERE account_id = %s AND symbol = %s AND day = %s
        """,
        (account_id, symbol, day),
    ).fetchone()
    if row is None or not isinstance(row[0], dict):
        return None
    return row[0]


def _dir_conf(payload: dict) -> float | None:
    horizons = payload.get("horizons") if isinstance(payload.get("horizons"), dict) else {}
    values = []
    for leg in horizons.values():
        if isinstance(leg, dict) and leg.get("dir_conf") is not None:
            try:
                values.append(float(leg["dir_conf"]))
            except (TypeError, ValueError):
                pass
    if not values:
        return None
    return sum(values) / len(values)


def _take_profit(payload: dict) -> str | None:
    horizons = payload.get("horizons") if isinstance(payload.get("horizons"), dict) else {}
    first = horizons.get("1") or horizons.get(1)
    if isinstance(first, dict) and first.get("take_profit") is not None:
        return str(first["take_profit"])
    return None


def _quote_line(symbol: str, last: Decimal | None, previous: Decimal | None) -> str | None:
    """Same print the universe row shows. No price means the IB update has not landed."""
    if last is None:
        return None
    if previous is not None and previous > 0:
        change = (last - previous) / previous * Decimal(100)
        return f"{symbol} {last} ({change:+.1f}%)"
    return f"{symbol} {last}"


def _ib_quotes(conn, symbols: list[str]) -> list[str]:
    """Guidance prices from the universe IB print, not a Yahoo chart."""
    from athena_marketdata.pricing import quote_pair

    lines: list[str] = []
    for symbol in symbols:
        last, previous = quote_pair(conn, symbol)
        line = _quote_line(symbol, last, previous)
        if line:
            lines.append(line)
    return lines


def _yahoo_notes(conn, symbols: list[str]) -> list[str]:
    """Headlines Yahoo has and IB does not. A name with no stored row is fetched once."""
    if not symbols:
        return []
    rows = conn.execute(
        """
        SELECT DISTINCT ON (symbol) symbol, title
        FROM news_events
        WHERE symbol = ANY(%s) AND source = 'yahoo' AND dismissed = false
        ORDER BY symbol, ts DESC
        """,
        (symbols,),
    ).fetchall()
    lines = [f"{symbol} yahoo: {title}" for symbol, title in rows if title]
    have = {symbol for symbol, _title in rows}
    missing = [symbol for symbol in symbols if symbol not in have]
    if not missing:
        return lines
    from athena_strategy.news import yahoo_headlines

    for symbol in missing:
        titles = yahoo_headlines(symbol, limit=1)
        if titles:
            lines.append(f"{symbol} yahoo: {titles[0]}")
    return lines


def _calendar_lines(conn, symbols: list[str], day: date, *, fed: bool = True) -> list[str]:
    """Fed and earnings dates within two weeks, from marks already stored."""
    lines: list[str] = []
    if fed:
        meetings = conn.execute(
            "SELECT event_date FROM calendar_marks WHERE kind = 'FED' AND symbol = ''"
        ).fetchall()
        for row in meetings:
            delta = (row[0] - day).days
            if abs(delta) <= 14:
                lines.append(f"FOMC {row[0].isoformat()} ({delta:+d}d)")
    if symbols:
        earnings = conn.execute(
            """
            SELECT symbol, event_date FROM calendar_marks
            WHERE kind = 'EARNINGS' AND symbol = ANY(%s)
            """,
            (symbols,),
        ).fetchall()
        for symbol, event_day in earnings:
            delta = (event_day - day).days
            if abs(delta) <= 14:
                lines.append(f"{symbol} earnings {event_day.isoformat()} ({delta:+d}d)")
    return lines


def _year_high(conn, symbol: str) -> Decimal | None:
    bars = daily_bars(conn, symbol, 252)
    if not bars:
        return None
    return max(Decimal(bar.high) for bar in bars)


def _max_capture(legs, spot) -> Decimal | None:
    best = None
    for row in legs:
        _, quantity, avg_price, sec_type, _, right, strike, market_price, _ = row
        if sec_type != "OPT" or Decimal(quantity) >= 0:
            continue
        captured = premium_captured_pct(Decimal(avg_price), None if market_price is None else Decimal(market_price))
        if captured is not None and (best is None or captured > best):
            best = captured
    return best


def _first_account(conn) -> str:
    row = conn.execute("SELECT account_id FROM settings_versions WHERE active ORDER BY id DESC LIMIT 1").fetchone()
    return row[0] if row else ""


def _hold(symbol: str, reason: str) -> dict:
    return {
        "account_id": "",
        "symbol": symbol,
        "day": date.today(),
        "action": "hold",
        "confidence": 0.0,
        "reason": reason,
        "payload": {"llm": False},
    }
