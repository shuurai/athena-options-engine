"""Saved settings win. None inherits the process environment."""

from __future__ import annotations

import os

from athena_exec.accounts import active_accounts
from athena_settings.model import SettingsPayload
from athena_settings.repository import load_active

_PLACEHOLDER_MODEL = "sentiment-v1"


def pick(stored: str | None, env_name: str) -> str:
    """None uses the environment. Any saved string, including empty, wins."""
    if stored is None:
        return os.environ.get(env_name, "").strip()
    return stored.strip()


def itick_key(settings: SettingsPayload) -> str:
    return pick(settings.itick.api_key, "ITICK_API_KEY")


def deepseek_token(settings: SettingsPayload) -> str:
    """The credential for the selected provider. DSH does not use one."""
    field = {
        "DEEPSEEK": "deepseek_token",
        "OPENROUTER": "openrouter_token",
        "CLAUDE": "claude_token",
        "KIMI": "kimi_token",
    }.get(settings.ai.provider)
    if field is None:
        return ""
    from athena_settings.model import CREDENTIAL_ENV

    return pick(getattr(settings.ai, field), CREDENTIAL_ENV[settings.ai.provider])


def scoring_model(settings: SettingsPayload) -> str:
    model = (settings.ai.model or "").strip()
    if model in {"", _PLACEHOLDER_MODEL}:
        return "deepseek-flash"
    return model


def bot_token(settings: SettingsPayload) -> str:
    return pick(settings.notify.bot_token, "TELEGRAM_BOT_TOKEN")


def bot_username(settings: SettingsPayload) -> str:
    return pick(settings.notify.bot_username, "TELEGRAM_BOT_USERNAME").lstrip("@")


def allowlist_ids(settings: SettingsPayload) -> set[str]:
    raw = settings.notify.telegram_user_ids
    if raw is None:
        raw = os.environ.get("TELEGRAM_ALLOWLIST_USER_IDS", "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def control_flag(settings: SettingsPayload, account_id: str, field: str, default: bool) -> bool:
    """A missing switch uses the default. A saved true or false wins."""
    entry = settings.account_controls.get(account_id)
    if entry is None:
        return default
    value = getattr(entry, field)
    return default if value is None else value


def live_settings(conn):
    """Active book settings, or None when this mode has no account."""
    accounts = active_accounts()
    if not accounts:
        return None
    return load_active(conn, accounts[0])


def live_allowlist(conn) -> set[str]:
    settings = live_settings(conn)
    if settings is None:
        raw = os.environ.get("TELEGRAM_ALLOWLIST_USER_IDS", "")
        return {part.strip() for part in raw.split(",") if part.strip()}
    return allowlist_ids(settings)
