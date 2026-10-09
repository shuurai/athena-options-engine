"""Long-poll Telegram. Only allowlisted user ids can Approve or Reject."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from athena_exec.accounts import active_accounts, trading_mode
from athena_exec.halt import active_reason, halt, resume
from athena_exec.ops import ops_action
from athena_exec.slots import slot_state
from athena_exec.slots import HKT
from athena_settings import format_settings, load_active, set_knob
from athena_store import connect
from athena_bots.public_url import control_page_url
from athena_strategy import decide

log = logging.getLogger("athena.telegram")

OFFSET_PATH = Path("var/telegram.offset")


class TelegramClient:
    def __init__(self, token: str) -> None:
        self._token = token

    def call(self, method: str, payload: dict[str, object] | None = None, timeout: int = 35) -> object:
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        data = json.dumps(payload or {}).encode("utf-8")
        request = Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with _open(request, timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"telegram {method} failed: {_safe(detail)}") from None
        except URLError as exc:
            raise RuntimeError(f"telegram {method} failed: {exc.reason}") from None
        if not body.get("ok"):
            raise RuntimeError(f"telegram {method} failed: {body.get('description')}")
        return body.get("result")


def notify_draft(
    draft_id: str,
    symbol: str,
    tactic: str,
    contracts: int,
    phase: str,
    reason: str,
    control_url: str = "",
    account_id: str = "",
) -> None:
    """Send one draft card to every allowlisted chat. No-op when the token is empty."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        log.info("telegram token missing")
        return
    text = _draft_text(draft_id, symbol, tactic, contracts, phase, reason, control_url, account_id)
    markup = _markup(draft_id, control_url)
    client = TelegramClient(token)
    for chat_id in _allowlist():
        client.call("sendMessage", {"chat_id": chat_id, "text": text, "reply_markup": markup})
        log.info("telegram sent draft=%s chat=%s", draft_id, chat_id)


def notify_expired(draft_id: str, symbol: str, tactic: str, account_id: str) -> None:
    """Tell the allowlist that an unapproved draft closed itself."""
    text = f"Draft expired. {symbol} {tactic} was not approved in time. No order sent.\nDraft {draft_id}\nAccount {account_id}"
    _send_plain(text)
    log.info("telegram expired draft=%s", draft_id)


def _send_plain(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        log.info("telegram token missing")
        return
    client = TelegramClient(token)
    for chat_id in _allowlist():
        client.call("sendMessage", {"chat_id": chat_id, "text": text})


def start_telegram_polling(database_url: str, token: str, allowlist: set[str]) -> None:
    thread = threading.Thread(
        target=_poll_forever,
        args=(database_url, token, allowlist),
        name="telegram-poll",
        daemon=True,
    )
    thread.start()


def _poll_forever(database_url: str, token: str, allowlist: set[str]) -> None:
    client = TelegramClient(token)
    try:
        client.call("deleteWebhook", {"drop_pending_updates": False})
        me = client.call("getMe")
        log.info("telegram bot=@%s", me.get("username") if isinstance(me, dict) else "")
    except RuntimeError as exc:
        log.error("%s", exc)
        return
    try:
        _announce_pending(client, database_url, allowlist)
    except RuntimeError as exc:
        log.error("%s", exc)
    offset = _read_offset()
    while True:
        payload: dict[str, object] = {"timeout": 25}
        if offset is not None:
            payload["offset"] = offset
        try:
            updates = client.call("getUpdates", payload, timeout=40)
        except RuntimeError as exc:
            log.error("%s", exc)
            time.sleep(5)
            continue
        if not isinstance(updates, list):
            continue
        for update in updates:
            offset = int(update["update_id"]) + 1
            _write_offset(offset)
            try:
                _handle(client, database_url, allowlist, update)
            except Exception:
                log.exception("telegram update failed id=%s", update.get("update_id"))


def _announce_pending(client: TelegramClient, database_url: str, allowlist: set[str]) -> None:
    accounts = active_accounts()
    with connect(database_url) as conn:
        rows = conn.execute(
            """
            SELECT id, symbol, tactic, contracts, phase, reason, account_id
            FROM drafts
            WHERE status = 'pending' AND account_id = ANY(%s)
            ORDER BY created_at
            """,
            (accounts,),
        ).fetchall()
    if not rows:
        text = "Athena is online. No draft is waiting. Send /drafts anytime."
        for chat_id in allowlist:
            client.call("sendMessage", {"chat_id": chat_id, "text": text})
        return
    for draft_id, symbol, tactic, contracts, phase, reason, account_id in rows:
        text = _draft_text(
            draft_id, symbol, tactic, contracts, phase, reason,
            _open_link(database_url, draft_id), account_id,
        )
        markup = _markup(draft_id, _open_link(database_url, draft_id))
        for chat_id in allowlist:
            client.call("sendMessage", {"chat_id": chat_id, "text": text, "reply_markup": markup})
            log.info("telegram sent draft=%s chat=%s", draft_id, chat_id)


def _handle(client: TelegramClient, database_url: str, allowlist: set[str], update: dict) -> None:
    if "callback_query" in update:
        _handle_callback(client, database_url, allowlist, update["callback_query"])
        return
    message = update.get("message") or {}
    text = (message.get("text") or "").strip()
    chat = message.get("chat") or {}
    user = message.get("from") or {}
    chat_id = chat.get("id")
    actor = str(user.get("id", ""))
    if chat_id is None or not text.startswith("/"):
        return
    command, _, arg = text.partition(" ")
    command = command.split("@", 1)[0]
    if actor not in allowlist:
        client.call("sendMessage", {"chat_id": chat_id, "text": f"Not allowlisted. Your id is {actor}."})
        return
    if command == "/start":
        client.call("sendMessage", {"chat_id": chat_id, "text": f"Linked. Your id is {actor}."})
    elif command == "/drafts":
        client.call("sendMessage", {"chat_id": chat_id, "text": _drafts_text(database_url)})
    elif command == "/status":
        client.call("sendMessage", {"chat_id": chat_id, "text": _status_text(database_url)})
    elif command == "/approve" and arg:
        client.call("sendMessage", {"chat_id": chat_id, "text": _apply(database_url, allowlist, arg.strip(), "approve", actor)})
    elif command == "/reject" and arg:
        client.call("sendMessage", {"chat_id": chat_id, "text": _apply(database_url, allowlist, arg.strip(), "reject", actor)})
    elif command == "/halt":
        client.call("sendMessage", {"chat_id": chat_id, "text": _halt(database_url)})
    elif command == "/resume":
        client.call("sendMessage", {"chat_id": chat_id, "text": _resume(database_url)})
    elif command == "/ops" and arg:
        client.call("sendMessage", {"chat_id": chat_id, "text": _ops(actor, allowlist, arg)})
    elif command == "/settings":
        client.call("sendMessage", {"chat_id": chat_id, "text": _settings(database_url, actor, arg)})
    else:
        client.call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": "Commands: /drafts /status /approve <id> /reject <id> /halt /resume /settings /ops athena restart",
            },
        )


def _handle_callback(client: TelegramClient, database_url: str, allowlist: set[str], query: dict) -> None:
    actor = str((query.get("from") or {}).get("id", ""))
    data = query.get("data") or ""
    query_id = query.get("id")
    message = query.get("message") or {}
    if actor not in allowlist:
        client.call("answerCallbackQuery", {"callback_query_id": query_id, "text": "Not allowlisted", "show_alert": True})
        return
    decision, _, draft_id = data.partition(":")
    if decision not in {"approve", "reject"} or not draft_id:
        client.call("answerCallbackQuery", {"callback_query_id": query_id, "text": "Unknown button"})
        return
    result = _apply(database_url, allowlist, draft_id, decision, actor)
    client.call("answerCallbackQuery", {"callback_query_id": query_id, "text": result[:180]})
    chat = (message.get("chat") or {}).get("id")
    if chat is not None and message.get("message_id") is not None:
        client.call(
            "editMessageText",
            {"chat_id": chat, "message_id": message["message_id"], "text": f"{message.get('text', '')}\n\n{result}"},
        )


def _apply(database_url: str, allowlist: set[str], draft_id: str, decision: str, actor: str) -> str:
    try:
        with connect(database_url) as conn:
            decide(conn, draft_id, decision, actor, allowlist)
    except (PermissionError, ValueError) as exc:
        return str(exc)
    if decision == "reject":
        return f"Rejected {draft_id}."
    with connect(database_url) as conn:
        order = conn.execute(
            """
            SELECT status, block_reason FROM orders
            WHERE draft_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (draft_id,),
        ).fetchone()
    if order is not None and order[0] == "blocked":
        return f"Approved {draft_id}. No order sent. {order[1]}."
    return f"Approved {draft_id}. Queued for the next Athena slot."


def _drafts_text(database_url: str) -> str:
    accounts = active_accounts()
    with connect(database_url) as conn:
        rows = conn.execute(
            """
            SELECT id, account_id, symbol, tactic, contracts
            FROM drafts
            WHERE status = 'pending' AND account_id = ANY(%s)
            ORDER BY created_at
            """,
            (accounts,),
        ).fetchall()
    if not rows:
        return "No draft is waiting."
    lines = [
        f"{draft_id}  {account_id}  {symbol}  {tactic}  {contracts}"
        for draft_id, account_id, symbol, tactic, contracts in rows
    ]
    return "\n".join(lines)


def _status_text(database_url: str) -> str:
    from datetime import datetime

    accounts = active_accounts()
    lines = [f"Mode {trading_mode()}"]
    if not accounts:
        lines.append("No account configured")
        return "\n".join(lines)
    with connect(database_url) as conn:
        for account in accounts:
            settings = load_active(conn, account)
            pending = conn.execute(
                "SELECT count(*) FROM drafts WHERE status = 'pending' AND account_id = %s",
                (account,),
            ).fetchone()[0]
            reason = active_reason(conn, account)
            state = slot_state(datetime.now(HKT), settings.ib)
            halt_line = f"Halted: {reason}" if reason else "Running"
            lines.append(f"{account}  slot {state}  pending {pending}  {halt_line}")
    return "\n".join(lines)


def _halt(database_url: str) -> str:
    accounts = active_accounts()
    if not accounts:
        return "No account configured."
    with connect(database_url) as conn:
        for account in accounts:
            halt(conn, account, "telegram halt")
    return "Halted " + ", ".join(accounts) + ". New drafts are blocked until /resume."


def _resume(database_url: str) -> str:
    accounts = active_accounts()
    if not accounts:
        return "No account configured."
    cleared = False
    with connect(database_url) as conn:
        for account in accounts:
            cleared = resume(conn, account) or cleared
    return "Resumed." if cleared else "Not halted."


def _settings(database_url: str, actor: str, arg: str) -> str:
    accounts = active_accounts()
    if not accounts:
        return "No account configured."
    parts = arg.split()
    with connect(database_url) as conn:
        if not parts:
            lines = []
            for account in accounts:
                lines.append(account)
                lines.append(format_settings(load_active(conn, account)))
            return "\n".join(lines)
        if len(parts) != 2:
            return "Use /settings or /settings roll_dte 14"
        key, raw = parts
        lines = []
        for account in accounts:
            try:
                lines.append(set_knob(conn, account, key, raw, actor))
            except ValueError as exc:
                return str(exc)
        return "\n".join(lines)


def _ops(actor: str, allowlist: set[str], command: str) -> str:
    try:
        return ops_action(actor, allowlist, command)
    except (PermissionError, ValueError) as exc:
        return str(exc)


def _open_link(database_url: str, draft_id: str) -> str:
    port = os.environ.get("ATHENA_CONTROL_PORT", "").strip()
    if not port:
        return ""
    with connect(database_url) as conn:
        row = conn.execute(
            """
            SELECT token FROM control_links
            WHERE draft_id = %s AND used_at IS NULL AND expires_at > now()
            ORDER BY expires_at DESC
            LIMIT 1
            """,
            (draft_id,),
        ).fetchone()
    if row is None:
        return ""
    return control_page_url(row[0])


def _markup(draft_id: str, control_url: str) -> dict[str, list]:
    """Approve and Reject on the message. The page link is there when a tunnel is up."""
    rows: list[list[dict[str, str]]] = []
    if control_url:
        rows.append([{"text": "Open approve page", "url": control_url}])
    rows.append([
        {"text": "Approve", "callback_data": f"approve:{draft_id}"},
        {"text": "Reject", "callback_data": f"reject:{draft_id}"},
    ])
    return {"inline_keyboard": rows}


def _draft_text(
    draft_id: str,
    symbol: str,
    tactic: str,
    contracts: int,
    phase: str,
    reason: str,
    control_url: str,
    account_id: str = "",
) -> str:
    lines = [
        f"Approve or reject {symbol} {tactic}",
        f"Draft {draft_id}",
        account_id,
        f"{contracts} contract" if contracts == 1 else f"{contracts} contracts",
        f"Phase {phase}",
        reason,
        "Use the buttons on this message.",
    ]
    if control_url:
        lines.append(f"Approve page {control_url}")
    return "\n".join(lines)


def _open(request: Request, timeout: int):
    proxy = os.environ.get("TELEGRAM_HTTPS_PROXY", "").strip()
    if not proxy:
        return urlopen(request, timeout=timeout)
    opener = build_opener(ProxyHandler({"http": proxy, "https": proxy}))
    return opener.open(request, timeout=timeout)


def _allowlist() -> set[str]:
    raw = os.environ.get("TELEGRAM_ALLOWLIST_USER_IDS", "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def _read_offset() -> int | None:
    if not OFFSET_PATH.is_file():
        return None
    raw = OFFSET_PATH.read_text(encoding="utf-8").strip()
    return int(raw) if raw else None


def _write_offset(offset: int) -> None:
    OFFSET_PATH.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_PATH.write_text(str(offset), encoding="utf-8")


def _safe(detail: str) -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if token:
        detail = detail.replace(token, "[redacted]")
    return detail[:300]
