"""Short-horizon Prediction LLM (OpenAI protocol). Advisory / shadow only."""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib import error, request

from psycopg.types.json import Jsonb

from athena_settings.model import SettingsPayload
from athena_settings.prompts import PREDICTION_NAME, resolved_agents
from athena_settings.resolve import prediction_token

log = logging.getLogger("athena.strategy.forecast")

_HORIZON_KEYS = ("bid", "ask", "stop", "take_profit", "dir_conf")


def parse_forecast(
    text: str,
    *,
    symbol: str = "",
    horizons: list[int] | None = None,
) -> dict | None:
    """Strict forecast JSON. Returns None when the reply does not match."""
    payload = _json_object(text)
    if payload is None:
        return None
    wanted = [int(item) for item in (horizons or [1, 3, 5])]
    raw_horizons = payload.get("horizons")
    if not isinstance(raw_horizons, dict):
        return None
    cleaned: dict[str, dict] = {}
    for h in wanted:
        key = str(h)
        leg = raw_horizons.get(key)
        if leg is None and h in raw_horizons:
            leg = raw_horizons.get(h)
        parsed = _horizon_leg(leg)
        if parsed is None:
            return None
        cleaned[key] = parsed
    risks = _risks(payload.get("risks"))
    if risks is None:
        return None
    shown = str(payload.get("symbol") or symbol or "").strip().upper()
    if not shown:
        return None
    model = str(payload.get("model") or "").strip() or "forecast"
    as_of = str(payload.get("as_of") or "").strip()
    if not as_of:
        as_of = datetime.now(timezone.utc).isoformat()
    return {
        "symbol": shown,
        "as_of": as_of,
        "model": model,
        "horizons": cleaned,
        "risks": risks,
    }


def prediction_request_body(
    *,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    temperature: float,
    stop: list[str],
    reasoning: bool,
) -> dict:
    """OpenAI chat.completions body for the Prediction endpoint."""
    body: dict[str, Any] = {
        "model": model or "forecast",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
        "stream": False,
    }
    if stop:
        body["stop"] = list(stop)
    if reasoning:
        body["reasoning_effort"] = "medium"
        body["thinking"] = {"type": "enabled"}
    return body


def prediction_chat(
    *,
    base_url: str,
    token: str,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    temperature: float,
    stop: list[str],
    reasoning: bool,
    timeout_seconds: int,
) -> dict:
    """One OpenAI-protocol completion. Token is never logged."""
    started = time.perf_counter()
    posted = prediction_request_body(
        model=model,
        system=system,
        user=user,
        max_tokens=max_tokens,
        temperature=temperature,
        stop=stop,
        reasoning=reasoning,
    )
    root = (base_url or "").rstrip("/")
    if not root:
        return {
            "text": "", "tokens": 0, "error": "Prediction URL is not set.",
            "request": posted, "latency_ms": 0,
        }
    if not token.strip():
        return {
            "text": "", "tokens": 0, "error": "Prediction token is not set.",
            "request": posted, "latency_ms": 0,
        }
    url = root + "/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + token.strip(),
    }
    call = request.Request(url, data=json.dumps(posted).encode(), headers=headers, method="POST")
    try:
        with request.urlopen(call, timeout=max(1, int(timeout_seconds))) as response:
            payload = json.loads(response.read().decode())
        message = (payload.get("choices") or [{}])[0].get("message") or {}
        text = str(message.get("content") or "")
        usage = payload.get("usage") or {}
        tokens = int(usage.get("total_tokens") or 0)
    except error.HTTPError as exc:
        log.info("prediction skipped status=%s", exc.code)
        return {
            "text": "", "tokens": 0, "error": f"HTTP {exc.code}",
            "request": posted, "latency_ms": int((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:
        log.info("prediction skipped error=%s", exc.__class__.__name__)
        return {
            "text": "", "tokens": 0, "error": exc.__class__.__name__,
            "request": posted, "latency_ms": int((time.perf_counter() - started) * 1000),
        }
    latency = int((time.perf_counter() - started) * 1000)
    if not text.strip():
        return {"text": "", "tokens": tokens, "error": "empty", "request": posted, "latency_ms": latency}
    return {"text": text, "tokens": tokens, "error": "", "request": posted, "latency_ms": latency}


def fetch_forecast(
    settings: SettingsPayload,
    *,
    symbol: str,
    last: str = "",
    news: str = "",
    as_of: str = "",
) -> tuple[dict | None, dict]:
    """Call Prediction when enabled. Returns (signal, transport result)."""
    pred = settings.prediction
    empty = {"text": "", "tokens": 0, "error": "", "request": {}, "latency_ms": 0}
    if not pred.enabled:
        return None, {**empty, "error": "disabled"}
    prompts = resolved_agents(settings)
    prompt = prompts.get(PREDICTION_NAME)
    if prompt is None:
        return None, {**empty, "error": "Prediction prompt missing"}
    horizons = [int(item) for item in (pred.horizons or [1, 3, 5])]
    moment = as_of or datetime.now(timezone.utc).isoformat()
    values = {
        "symbol": symbol.upper(),
        "last": last or "unknown",
        "news": news or "none",
        "horizons": ", ".join(str(item) for item in horizons),
        "as_of": moment,
        "model": pred.model or "forecast",
    }
    system = _fill(prompt.system, values)
    user = _fill(prompt.execution, values)
    reasoning = pred.reasoning if prompt.reasoning is None else bool(prompt.reasoning)
    result = prediction_chat(
        base_url=pred.base_url,
        token=prediction_token(settings),
        model=pred.model,
        system=system,
        user=user,
        max_tokens=pred.max_tokens,
        temperature=pred.temperature,
        stop=list(pred.stop or []),
        reasoning=reasoning,
        timeout_seconds=pred.timeout_seconds,
    )
    if result["error"]:
        return None, result
    signal = parse_forecast(result["text"], symbol=symbol, horizons=horizons)
    if signal is None:
        result = {**result, "error": "invalid schema"}
        return None, result
    if not signal.get("model"):
        signal["model"] = pred.model or "forecast"
    return signal, result


def store_forecast_mark(conn, account_id: str, signal: dict) -> None:
    """One row per account/symbol/day with the full shadow payload."""
    day = _mark_day(signal.get("as_of"))
    conn.execute(
        """
        INSERT INTO forecast_marks (account_id, symbol, day, as_of, model, payload)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (account_id, symbol, day) DO UPDATE SET
            as_of = EXCLUDED.as_of,
            model = EXCLUDED.model,
            payload = EXCLUDED.payload,
            created_at = now()
        """,
        (
            account_id,
            str(signal.get("symbol") or "").upper(),
            day,
            str(signal.get("as_of") or ""),
            str(signal.get("model") or "forecast"),
            Jsonb(signal),
        ),
    )
    conn.commit()


def _mark_day(as_of: object) -> date:
    text = str(as_of or "").strip()
    if text:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            pass
    return datetime.now(timezone.utc).date()


def _json_object(text: str) -> dict | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _horizon_leg(leg) -> dict | None:
    if not isinstance(leg, dict):
        return None
    out = {}
    for key in _HORIZON_KEYS:
        number = _number(leg.get(key))
        if number is None:
            return None
        if key == "dir_conf" and not (Decimal(0) <= number <= Decimal(1)):
            return None
        out[key] = float(number)
    if out["ask"] < out["bid"]:
        return None
    return out


def _risks(raw) -> list[dict] | None:
    if raw is None:
        return []
    if not isinstance(raw, list):
        return None
    out = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        code = str(item.get("code") or "").strip()
        note = str(item.get("note") or "").strip()
        if not code:
            return None
        out.append({"code": code, "note": note})
    return out


def _number(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _fill(template: str, values: dict[str, str]) -> str:
    text = template
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text
