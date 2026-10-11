"""Reply shapes for the signaled agents. A bad shape is invalid, not quiet."""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from athena_strategy.tags import CHANNEL_RISING, names_owned_by
from athena_strategy.types import NEWS_TYPE_CODES

LOCATION_TAGS = names_owned_by("location")
_TACTICS = frozenset({"BUY", "CSP", "CC", "STRANGLE"})
_PRICE_KEYS = frozenset(
    {
        "strike",
        "limit",
        "expiry",
        "put_strike",
        "put_limit",
        "call_strike",
        "call_limit",
    }
)
_SCHEMA = {
    "Location": '{"tags":["' + CHANNEL_RISING + '"]}',
    "News": '{"news_wash":false,"event_type":"GENERIC","sentiment":0}',
    "CSP": '{"quiet":false,"expiry":"YYYY-MM-DD","strike":1,"limit":1,"reason":""}',
    "Call": '{"quiet":false,"expiry":"YYYY-MM-DD","strike":1,"limit":1,"reason":""}',
    "Purchase": '{"quiet":false,"limit":1,"reason":""}',
    "Strangle": '{"quiet":false,"expiry":"YYYY-MM-DD","put_strike":1,"put_limit":1,"call_strike":1,"call_limit":1,"reason":""}',
    "Consolidator": '{"thinking":"","drafts":[{"tactic":"BUY","limit":1,"reason":""}]}',
    "Narrator": '{"sentence":"","strike":null}',
}


def schema_line(agent: str) -> str:
    """One line the retry can show the model."""
    return _SCHEMA.get(agent, "{}")


def parse_response(agent: str, text: str) -> tuple[dict | None, str, str]:
    """Return payload, ok or invalid, and parse or schema when invalid."""
    payload = _object(text)
    if payload is None:
        return None, "invalid", "parse"
    if not _matches(agent, payload):
        return None, "invalid", "schema"
    return payload, "ok", ""


def _object(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text or "", re.S)
    if match is None:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _matches(agent: str, payload: dict) -> bool:
    if agent == "Location":
        tags = payload.get("tags")
        return isinstance(tags, list) and all(isinstance(tag, str) and tag in LOCATION_TAGS for tag in tags)
    if agent == "News":
        return _news(payload)
    if agent in {"CSP", "Call"}:
        return _quiet_leg(payload, ("expiry", "strike", "limit", "reason"))
    if agent == "Purchase":
        return _quiet_leg(payload, ("limit", "reason"))
    if agent == "Strangle":
        return _quiet_leg(payload, ("expiry", "put_strike", "put_limit", "call_strike", "call_limit", "reason"))
    if agent == "Consolidator":
        return _consolidator(payload)
    if agent == "Narrator":
        return _narrator(payload)
    return False


def _narrator(payload: dict) -> bool:
    if not isinstance(payload.get("sentence"), str):
        return False
    strike = payload.get("strike")
    if strike is None:
        return True
    number = _decimal(strike)
    return number is not None and number > 0


def _news(payload: dict) -> bool:
    if not isinstance(payload.get("news_wash"), bool):
        return False
    if "event_type" in payload and payload["event_type"] is not None:
        if payload["event_type"] not in NEWS_TYPE_CODES:
            return False
    if "sentiment" in payload and payload["sentiment"] is not None:
        score = _decimal(payload["sentiment"])
        if score is None or score < -1 or score > 1:
            return False
    return True


def _quiet_leg(payload: dict, required: tuple[str, ...]) -> bool:
    if not isinstance(payload.get("quiet"), bool):
        return False
    if payload["quiet"]:
        return all(key not in payload or payload[key] is None for key in _PRICE_KEYS)
    for key in required:
        if key not in payload:
            return False
    for key in required:
        value = payload[key]
        if key == "reason":
            if not isinstance(value, str):
                return False
            continue
        if key == "expiry":
            if not _date(value):
                return False
            continue
        if _decimal(value) is None or _decimal(value) <= 0:
            return False
    return True


def _consolidator(payload: dict) -> bool:
    if not isinstance(payload.get("thinking"), str):
        return False
    drafts = payload.get("drafts")
    if not isinstance(drafts, list):
        return False
    for item in drafts:
        if not isinstance(item, dict):
            return False
        if item.get("tactic") not in _TACTICS:
            return False
        if not isinstance(item.get("reason"), str):
            return False
        limit = _decimal(item.get("limit"))
        if limit is None or limit <= 0:
            return False
    return True


def _date(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
