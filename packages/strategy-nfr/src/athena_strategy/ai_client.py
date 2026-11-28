"""Provider-neutral chat client (S0b: relocated from the retired news scorer).

Only the generic multi-provider completion primitives live here. All news
analysis — headline sentiment enrichment, article reading, score parsing and
the score listener — was **dropped**, not relocated, per booklet 16 §16.5 R1.
This module is a temporary bridge: ``agents.py`` (deleted in S0c) is its last
consumer; the AI recommender itself is deferred to booklet 16 §5.3.
"""

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

from athena_settings.model import MODELS, normalize_provider
from athena_strategy.classifiers import CLASSIFIER_CODES, clean_classifiers

log = logging.getLogger("athena.ai")

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
