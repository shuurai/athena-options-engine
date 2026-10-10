"""Optional headline scores from a local dsh one-shot. Keyword scores stay if it is absent."""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from urllib import error, request

import psycopg

from athena_settings.model import MODELS, normalize_provider
from athena_strategy.classifiers import CLASSIFIER_CODES, clean_classifiers
from athena_strategy.types import NEWS_TYPE_CODES

log = logging.getLogger("athena.news")

_BATCH = 6
_TIMEOUT_SECONDS = 120
_SECRET_PREFIXES = (
    "TELEGRAM_",
    "DATABASE_",
    "ATHENA_S3_",
    "ITICK_",
    "FEISHU_",
    "CONTROL_WEB_SIGNING",
    "DSH_API",
    "DEEPSEEK_",
    "OPENROUTER_",
    "ANTHROPIC_",
    "MOONSHOT_",
)


def parse_scores(stream: str) -> dict[str, float]:
    """Read the headless `final` event. Unknown lines and bad rows are ignored."""
    payload = _final_json(stream)
    if not isinstance(payload, dict):
        return {}
    scores: dict[str, float] = {}
    for row in payload.get("scores") or []:
        if not isinstance(row, dict):
            continue
        fingerprint = str(row.get("id") or "")
        try:
            value = float(row["sentiment"])
        except (KeyError, TypeError, ValueError):
            continue
        if fingerprint:
            scores[fingerprint] = max(-1.0, min(1.0, value))
    return scores


def parse_article(stream: str) -> dict | None:
    """One event from the headless `final` text. Missing fields return None."""
    payload = _final_json(stream)
    if not isinstance(payload, dict):
        return None
    title = " ".join(str(payload.get("title") or "").split())
    try:
        sentiment = float(payload["sentiment"])
    except (KeyError, TypeError, ValueError):
        return None
    if not title:
        return None
    return {
        "symbol": str(payload.get("symbol") or "").strip().upper(),
        "title": title[:240],
        "event_type": str(payload.get("event_type") or "").strip().upper(),
        "sentiment": max(-1.0, min(1.0, sentiment)),
    }


def read_article(
    title: str,
    text: str,
    url: str,
    symbols: list[str],
    model: str,
    provider: str = "DSH",
    token: str = "",
) -> dict:
    """Ask the selected model for the news fields. Raises ValueError when it cannot answer."""
    missing = _missing(provider, token)
    if missing:
        raise ValueError(missing)
    stream = _complete(_article_prompt(title, text, url, symbols), model or "deepseek-flash", provider, token)
    if not stream:
        raise ValueError(_empty_message(provider, "page"))
    article = parse_article(stream)
    if article is None:
        raise ValueError(_empty_message(provider, "news event"))
    return article


def parse_profile(stream: str) -> dict | None:
    """Classifiers and thesis from a headless final JSON object."""
    payload = _final_json(stream)
    if not isinstance(payload, dict):
        return None
    raw = payload.get("classifiers")
    if isinstance(raw, list):
        text = ",".join(str(item) for item in raw)
    else:
        text = str(raw or "")
    classifiers = clean_classifiers(text, limit=3)
    thesis = " ".join(str(payload.get("thesis") or "").split())[:240]
    name = " ".join(str(payload.get("name") or "").split())[:80]
    if not classifiers and not thesis:
        return None
    return {"name": name, "classifiers": classifiers, "thesis": thesis}


def classify_symbol(symbol: str, facts: dict, model: str, provider: str = "DSH", token: str = "") -> tuple[dict | None, dict]:
    """One classifier call. The result always has latency, tokens, and error."""
    result = chat(
        "Reply with JSON only.",
        _profile_prompt(symbol, facts),
        model or "deepseek-flash",
        provider,
        token,
        False,
    )
    if result["error"] == "dsh missing":
        result["error"] = "DSH is not configured."
    if result["error"]:
        return None, result
    profile = parse_profile(json.dumps({"type": "final", "text": result["text"]}))
    if profile is None:
        result["error"] = _empty_message(provider, "classifiers")
        return None, result
    return profile, result


def read_profile(symbol: str, facts: dict, model: str, provider: str = "DSH", token: str = "") -> dict:
    """Ask the selected model for classifier badges. Raises ValueError when it cannot answer."""
    profile, result = classify_symbol(symbol, facts, model, provider, token)
    if result["error"]:
        raise ValueError(result["error"])
    return profile


_score_listener = None


def set_score_listener(listener) -> None:
    """Called with a status and a short line. Status is running, skipped, or done."""
    global _score_listener
    _score_listener = listener


def _hear(status: str, detail: str) -> None:
    if _score_listener is not None:
        _score_listener(status, detail)


def enrich_sentiment(
    conn: psycopg.Connection,
    rows: list[tuple[str, str, str]],
    model: str = "deepseek-flash",
    provider: str = "DSH",
    token: str = "",
) -> int:
    """Replace keyword sentiment for new headlines. Returns how many rows changed."""
    name = "dsh" if normalize_provider(provider) == "DSH" else normalize_provider(provider).lower()
    if _missing(provider, token) or not rows:
        _hear("skipped", f"{name} skipped")
        return 0
    _hear("running", f"{name} scoring")
    batch = rows[:_BATCH]
    stream = _complete(_prompt(batch), model or "deepseek-flash", provider, token)
    if not stream:
        _hear("done", f"{name}=0")
        return 0
    scores = parse_scores(stream)
    known = {fingerprint for fingerprint, _symbol, _title in batch}
    updated = 0
    for fingerprint, sentiment in scores.items():
        if fingerprint not in known:
            continue
        result = conn.execute(
            "UPDATE news_events SET sentiment = %s WHERE fingerprint = %s",
            (sentiment, fingerprint),
        )
        updated += result.rowcount
    if updated:
        conn.commit()
    _hear("done", f"{name}={updated}")
    return updated


def _missing(provider: str, token: str) -> str:
    if normalize_provider(provider) != "DSH":
        return "" if token.strip() else "Credential token is not set."
    if os.environ.get("DSH_BIN", "").strip():
        return ""
    return "DSH is not configured."


def _provider_name(provider: str) -> str:
    return {
        "DSH": "DSH",
        "DEEPSEEK": "DeepSeek",
        "OPENROUTER": "OpenRouter",
        "CLAUDE": "Claude",
        "KIMI": "Kimi",
    }.get(normalize_provider(provider), "AI")


def _empty_message(provider: str, kind: str) -> str:
    who = _provider_name(provider)
    if kind == "page":
        return f"{who} did not finish the page."
    if kind == "news event":
        return f"{who} did not return a news event."
    if kind == "symbol":
        return f"{who} did not classify the symbol."
    return f"{who} did not return classifiers."


def _final_json(stream: str) -> object | None:
    final = ""
    for line in stream.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "final":
            final = str(event.get("text") or "")
    if not final:
        return None
    match = re.search(r"\{.*\}", final, re.S)
    if match is None:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _complete(prompt: str, model: str, provider: str = "DSH", token: str = "") -> str:
    """Model text as a headless final event, or empty when the call does not finish."""
    if normalize_provider(provider) != "DSH":
        result = chat("", prompt, model, provider, token, False)
        if result["error"] or not str(result["text"]).strip():
            return ""
        return json.dumps({"type": "final", "text": result["text"]})
    command = os.environ.get("DSH_BIN", "").strip()
    if not command:
        return ""
    patch = _model_patch(model or "deepseek-flash")
    try:
        try:
            completed = subprocess.run(
                [*shlex.split(command), "--profile", "headless", "--patch", patch, "--json", prompt],
                cwd=_empty_cwd(),
                env=_child_env(),
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.info("dsh skipped error=%s", exc.__class__.__name__)
            return ""
    finally:
        Path(patch).unlink(missing_ok=True)
    if completed.returncode != 0:
        log.info("dsh skipped exit=%s", completed.returncode)
        return ""
    return completed.stdout


_CHAT_URL = {
    "DEEPSEEK": "https://api.deepseek.com/chat/completions",
    "OPENROUTER": "https://openrouter.ai/api/v1/chat/completions",
    "KIMI": "https://api.moonshot.ai/v1/chat/completions",
    "CLAUDE": "https://api.anthropic.com/v1/messages",
}


def request_body(provider: str, model: str, system: str, user: str, reasoning: bool) -> dict:
    """The JSON body for one chat. Reasoning on means that provider's medium setting."""
    chosen = normalize_provider(provider)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    if chosen in {"DSH", "DEEPSEEK"}:
        body = {
            "model": model or "deepseek-flash",
            "messages": messages,
            "temperature": 0,
            "stream": False,
        }
        if chosen == "DEEPSEEK":
            if reasoning:
                body["thinking"] = {"type": "enabled"}
                body["reasoning_effort"] = "medium"
            else:
                body["thinking"] = {"type": "disabled"}
        return body
    if chosen == "OPENROUTER":
        return {
            "model": model or MODELS["OPENROUTER"][0],
            "messages": messages,
            "temperature": 0,
            "stream": False,
            "reasoning": {"effort": "medium" if reasoning else "none"},
        }
    if chosen == "KIMI":
        picked = model or MODELS["KIMI"][0]
        body = {
            "model": picked,
            "messages": messages,
            "temperature": 0,
            "stream": False,
        }
        if picked == "kimi-k3":
            if reasoning:
                body["reasoning_effort"] = "high"
            return body
        if reasoning:
            body["thinking"] = {"type": "enabled"}
        elif picked != "kimi-k2.7-code":
            body["thinking"] = {"type": "disabled"}
        return body
    return {
        "model": model or MODELS["CLAUDE"][0],
        "max_tokens": 8192,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_config": {"effort": "medium"},
    }


def chat(
    system: str,
    user: str,
    model: str,
    provider: str,
    token: str,
    thinking: bool,
) -> dict:
    """One completion. Returns text, tokens, and error. The token is never logged."""
    started = time.perf_counter()
    timeout = 90 if thinking else 30
    chosen = normalize_provider(provider)
    if chosen == "DSH":
        result = _dsh_chat(system, user, model, timeout)
    else:
        result = _provider_chat(chosen, system, user, model, token, thinking, timeout)
    result["latency_ms"] = int((time.perf_counter() - started) * 1000)
    return result


def _provider_chat(
    provider: str, system: str, user: str, model: str, token: str, thinking: bool, timeout: int
) -> dict:
    posted = request_body(provider, model, system, user, thinking)
    if not token.strip():
        return {"text": "", "tokens": 0, "error": "Credential token is not set.", "request": posted}
    headers = {"Content-Type": "application/json"}
    if provider == "CLAUDE":
        headers["x-api-key"] = token.strip()
        headers["anthropic-version"] = "2023-06-01"
    else:
        headers["Authorization"] = "Bearer " + token.strip()
    call = request.Request(_CHAT_URL[provider], data=json.dumps(posted).encode(), headers=headers, method="POST")
    try:
        with request.urlopen(call, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
        text, tokens = _reply(provider, payload)
    except error.HTTPError as exc:
        log.info("%s skipped status=%s", provider.lower(), exc.code)
        return {"text": "", "tokens": 0, "error": f"HTTP {exc.code}", "request": posted}
    except Exception as exc:
        log.info("%s skipped error=%s", provider.lower(), exc.__class__.__name__)
        return {"text": "", "tokens": 0, "error": exc.__class__.__name__, "request": posted}
    if not text.strip():
        return {"text": "", "tokens": tokens, "error": "empty", "request": posted}
    return {"text": text, "tokens": tokens, "error": "", "request": posted}


def _reply(provider: str, payload: dict) -> tuple[str, int]:
    if provider == "CLAUDE":
        blocks = payload.get("content") or []
        text = "".join(
            str(block.get("text") or "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
        usage = payload.get("usage") or {}
        tokens = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
        return text, tokens
    message = (payload.get("choices") or [{}])[0].get("message") or {}
    usage = payload.get("usage") or {}
    return str(message.get("content") or ""), int(usage.get("total_tokens") or 0)


def _dsh_chat(system: str, user: str, model: str, timeout: int) -> dict:
    posted = request_body("DSH", model, system, user, False)
    command = os.environ.get("DSH_BIN", "").strip()
    if not command:
        return {"text": "", "tokens": 0, "error": "dsh missing", "request": posted}
    patch = _model_patch(model or "deepseek-flash")
    prompt = system + "\n\n" + user
    try:
        try:
            completed = subprocess.run(
                [*shlex.split(command), "--profile", "headless", "--patch", patch, "--json", prompt],
                cwd=_empty_cwd(),
                env=_child_env(),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"text": "", "tokens": 0, "error": "TimeoutError", "request": posted}
        except OSError as exc:
            log.info("dsh skipped error=%s", exc.__class__.__name__)
            return {"text": "", "tokens": 0, "error": exc.__class__.__name__, "request": posted}
    finally:
        Path(patch).unlink(missing_ok=True)
    if completed.returncode != 0:
        log.info("dsh skipped exit=%s", completed.returncode)
        return {"text": "", "tokens": 0, "error": f"exit {completed.returncode}", "request": posted}
    payload = _final_json(completed.stdout)
    if isinstance(payload, dict):
        return {"text": json.dumps(payload), "tokens": 0, "error": "", "request": posted}
    final = ""
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "final":
            final = str(event.get("text") or "")
    if not final.strip():
        return {"text": "", "tokens": 0, "error": "empty", "request": posted}
    return {"text": final, "tokens": 0, "error": "", "request": posted}


def _profile_prompt(symbol: str, facts: dict) -> str:
    codes = ", ".join(sorted(CLASSIFIER_CODES))
    lines = [
        "Classify this listed company for a news front-running options watchlist.",
        f"Pick 1 to 3 classifiers from this list only: {codes}.",
        "thesis is one sentence, at most 180 characters, about why the name can see a news wash.",
        "If the company name is unknown, set name to the common company name. If it is known, repeat it.",
        "Do not invent a market cap. Do not use tools. Reply with JSON only.",
        '{"name":"","classifiers":["AI_NARRATIVE"],"thesis":""}',
        f"Ticker: {symbol}",
        f"Name: {facts.get('name') or 'unknown'}",
        f"Sector: {facts.get('sector') or 'unknown'}",
        f"Industry: {facts.get('industry') or 'unknown'}",
    ]
    return "\n".join(lines)


def _article_prompt(title: str, text: str, url: str, symbols: list[str]) -> str:
    names = ", ".join(symbols) or "(none)"
    lines = [
        "Read this public article and write one news event for the watchlist.",
        f"symbol must be one of: {names}. If the article is not about one of them, use an empty symbol.",
        "event_type must be one of: " + ", ".join(sorted(NEWS_TYPE_CODES)) + ".",
        "sentiment is a number from -1.0 (bad for that symbol) to 1.0 (good for that symbol).",
        "title is a short headline.",
        "Do not use tools. Reply with JSON only.",
        '{"symbol":"NVDA","title":"...","event_type":"GENERIC_NEGATIVE","sentiment":-0.4}',
        f"URL: {url}",
        f"Page title: {title}",
        "Article:",
        text[:6000],
    ]
    return "\n".join(lines)


def _model_patch(model: str) -> str:
    """A one-shot overlay. Headless dsh has no model flag."""
    handle = tempfile.NamedTemporaryFile("w", prefix="athena-dsh-model-", suffix=".yml", delete=False)
    handle.write(
        "- name: '@deepseek-ai/dsh-agent-default-model'\n"
        "  config:\n"
        "    provider: deepseek-official\n"
        f"    model: {json.dumps(model)}\n"
    )
    handle.close()
    return handle.name


def _prompt(rows: list[tuple[str, str, str]]) -> str:
    lines = [
        "Score each stock headline from -1.0 (bad for that stock) to 1.0 (good for that stock).",
        "Do not use tools. Reply with JSON only.",
        '{"scores":[{"id":"...","sentiment":0.0}]}',
        "Headlines:",
    ]
    for fingerprint, symbol, title in rows:
        lines.append(f"id={fingerprint} symbol={symbol} title={title}")
    return "\n".join(lines)


def _empty_cwd() -> str:
    """A directory with no project .env, so dsh cannot read Athena secrets from one."""
    path = Path(tempfile.mkdtemp(prefix="athena-dsh-"))
    return str(path)


def _child_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_SECRET_PREFIXES)
    }
