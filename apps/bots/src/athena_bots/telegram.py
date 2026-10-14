"""Long-poll Telegram. Only allowlisted user ids can Approve or Reject."""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from athena_exec.accounts import active_accounts, trading_mode
from athena_exec.halt import active_reason, halt, resume
from athena_exec.ops import gateway_open, log_tail, ops_action
from athena_exec.slots import slot_state
from athena_exec.slots import HKT
from athena_settings import format_settings, load_active, set_knob
from athena_settings.model import SettingsPayload
from athena_settings.repository import save_settings
from athena_settings.resolve import bot_token, live_allowlist, live_settings
from athena_store import connect
from athena_store.backup import write_snapshot
from athena_store.s3 import sync_snapshot
from athena_bots.public_url import control_page_url
from athena_strategy import decide, draft_facts

log = logging.getLogger("athena.telegram")

OFFSET_PATH = Path("var/telegram.offset")
polling = False
_thread: threading.Thread | None = None
_link_codes: dict[str, datetime] = {}


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
            raise RuntimeError(f"telegram {method} failed: {_safe(detail, self._token)}") from None
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
    token, allow = _live_send()
    if not token:
        log.info("telegram token missing")
        return
    text = _draft_text(
        draft_id, symbol, tactic, contracts, phase, reason, control_url, account_id,
        _facts(draft_id),
    )
    markup = _markup(draft_id, control_url)
    client = TelegramClient(token)
    for chat_id in allow:
        client.call("sendMessage", {"chat_id": chat_id, "text": text, "reply_markup": markup})
        log.info("telegram sent draft=%s chat=%s", draft_id, chat_id)


def fill_text(
    account_id: str,
    symbol: str,
    tactic: str,
    action: str,
    draft_id: str,
    price: object | None,
    shares: int | None = None,
) -> str:
    """One execution notice. Filled is only for an Athena order."""
    if (tactic or "").upper() == "EXTERNAL":
        verb = "sell" if str(action).upper() in {"SLD", "SELL"} else "buy"
        if shares is not None and price is not None:
            amount = f"{shares} at {price}"
        elif price is not None:
            amount = f"Price {price}"
        else:
            amount = "Price unknown"
        return f"External {verb} {symbol}\n{amount}\nAccount {account_id}"
    price_line = f"Price {price}" if price is not None else "IB reports the order filled."
    return f"Filled {action} {symbol} {tactic}\n{price_line}\nAccount {account_id}\nDraft {draft_id}"


def notify_fill(
    account_id: str,
    symbol: str,
    tactic: str,
    action: str,
    draft_id: str,
    price: object | None = None,
    shares: int | None = None,
) -> None:
    """Tell the allowlist about an execution. No-op when the token is empty."""
    _send_plain(fill_text(account_id, symbol, tactic, action, draft_id, price, shares))
    log.info("telegram fill symbol=%s draft=%s", symbol, draft_id)


def notify_expired(draft_id: str, symbol: str, tactic: str, account_id: str) -> None:
    """Tell the allowlist that an unapproved draft closed itself."""
    text = f"Draft expired. {symbol} {tactic} was not approved in time. No order sent.\nDraft {draft_id}\nAccount {account_id}"
    _send_plain(text)
    log.info("telegram expired draft=%s", draft_id)


def _send_plain(text: str) -> None:
    token, allow = _live_send()
    if not token:
        log.info("telegram token missing")
        return
    client = TelegramClient(token)
    for chat_id in allow:
        client.call("sendMessage", {"chat_id": chat_id, "text": text})


def start_telegram_polling(database_url: str, token: str = "", allowlist: set[str] | None = None) -> None:
    """Start one poll thread. Later token and allowlist changes are read from settings."""
    ensure_polling(database_url)


def ensure_polling(database_url: str) -> None:
    """Start the poll thread when it is not already running. Never starts a second consumer."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _thread = threading.Thread(
        target=_poll_forever,
        args=(database_url,),
        name="telegram-poll",
        daemon=True,
    )
    _thread.start()


def issue_link_code() -> tuple[str, datetime]:
    """A one-time /start code. It lives in this process for ten minutes."""
    code = secrets.token_hex(2).upper()
    expires = datetime.now(UTC) + timedelta(minutes=10)
    _link_codes[code] = expires
    return code, expires


def _poll_forever(database_url: str) -> None:
    global polling
    client: TelegramClient | None = None
    token = ""
    announced = False
    offset = _read_offset()
    while True:
        try:
            live_token, allow = _credentials(database_url)
        except Exception:
            log.exception("telegram settings unread")
            polling = False
            time.sleep(5)
            continue
        if not live_token:
            polling = False
            client = None
            token = ""
            time.sleep(5)
            continue
        if client is None or live_token != token:
            client = TelegramClient(live_token)
            token = live_token
            try:
                client.call("deleteWebhook", {"drop_pending_updates": False})
            except RuntimeError as exc:
                log.error("%s", exc)
            try:
                me = client.call("getMe")
                log.info("telegram bot=@%s", me.get("username") if isinstance(me, dict) else "")
            except RuntimeError as exc:
                log.error("%s", exc)
                polling = False
                client = None
                token = ""
                time.sleep(5)
                continue
            polling = True
            if not announced:
                try:
                    _announce_pending(client, database_url, allow)
                except RuntimeError as exc:
                    log.error("%s", exc)
                announced = True
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
                _handle(client, database_url, allow, update)
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
            _facts(draft_id),
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
    if command == "/start" and arg.strip():
        _claim_link(database_url, actor, arg.strip())
    allowlist = _fresh_allowlist(database_url)
    if actor not in allowlist:
        client.call("sendMessage", {"chat_id": chat_id, "text": f"Not allowlisted. Your id is {actor}."})
        return
    if command == "/start":
        client.call("sendMessage", {"chat_id": chat_id, "text": f"Linked. Your id is {actor}."})
    elif command == "/drafts":
        _send_draft_list(client, database_url, chat_id)
    elif command == "/status":
        client.call("sendMessage", {"chat_id": chat_id, "text": _status_text(database_url)})
    elif command == "/slots":
        client.call("sendMessage", {"chat_id": chat_id, "text": _slots_text(database_url)})
    elif command == "/backup" and arg.strip() == "now":
        client.call("sendMessage", {"chat_id": chat_id, "text": _backup_now(database_url)})
    elif command == "/approve" and arg:
        client.call("sendMessage", {"chat_id": chat_id, "text": _apply(database_url, allowlist, arg.strip(), "approve", actor)})
    elif command == "/reject" and arg:
        draft_id, _, reason = arg.strip().partition(" ")
        client.call("sendMessage", {"chat_id": chat_id, "text": _apply(database_url, allowlist, draft_id, "reject", actor, reason)})
    elif command == "/halt":
        client.call("sendMessage", {"chat_id": chat_id, "text": _halt(database_url)})
    elif command == "/resume":
        client.call("sendMessage", {"chat_id": chat_id, "text": _resume(database_url)})
    elif command == "/ops" and arg:
        client.call("sendMessage", {"chat_id": chat_id, "text": _ops(database_url, actor, allowlist, arg)})
    elif command == "/news" and arg:
        client.call("sendMessage", {"chat_id": chat_id, "text": _news(database_url, arg)})
    elif command == "/settings":
        client.call("sendMessage", {"chat_id": chat_id, "text": _settings(database_url, actor, arg)})
    elif command == "/help":
        client.call("sendMessage", {"chat_id": chat_id, "text": help_text()})
    elif command in {"/stock", "/etf", "/hk", "/fx"}:
        client.call("sendMessage", {"chat_id": chat_id, "text": quote_text(database_url, command, arg)})
    else:
        client.call("sendMessage", {"chat_id": chat_id, "text": "Use /help to see the commands."})


def help_text() -> str:
    """What the bot does, then each command in one sentence."""
    return "\n".join((
        "Athena sends drafts to this chat. Only a linked user can run a command. Nothing is sent to the broker until you Approve.",
        "",
        "Prices. IB is asked first. The word delayed means that quote. If the gateway is down, the word stored means the last price already on disk.",
        "/stock AAPL is a US stock. Use letters. A number such as 700 belongs on /hk.",
        "/etf SPY is a US ETF. It uses the same listing as a stock, and the reply says ETF.",
        "/hk 700 is a Hong Kong stock on SEHK, in HKD. 0700 is read as 700.",
        "/fx EURUSD is a currency pair. EUR/USD is the same pair, on IDEALPRO.",
        "",
        "Drafts.",
        "/drafts lists what is waiting.",
        "/approve <id> queues that draft for the next Athena slot.",
        "/reject <id> [reason] drops it.",
        "",
        "The book.",
        "/status shows the account, the slot, and open positions.",
        "/slots shows the Athena and Mobile windows. HK and US sessions.",
        "/halt pauses new trading. /resume clears that pause.",
        "/news AAPL lists the stored headlines. /news AAPL <headline> stores one you paste.",
        "/settings get risk prints the limits.",
        "/ops health checks the gateway, the store, and the bot.",
        "/backup now writes a local snapshot.",
    ))


def quote_text(database_url: str, command: str, arg: str) -> str:
    """One price line, or a sentence when the symbol does not fit that command."""
    from decimal import Decimal

    from athena_exec.broker import quote_fields

    kind = command.lstrip("/")
    if not arg.strip():
        return {
            "stock": "Name a US stock. Example: /stock AAPL",
            "etf": "Name a US ETF. Example: /etf SPY",
            "hk": "Name a Hong Kong stock by its SEHK number. Example: /hk 700",
            "fx": "Name a pair. Example: /fx EURUSD",
        }.get(kind, "Use /help to see the commands.")
    fields = quote_fields(kind, arg)
    if isinstance(fields, str):
        return fields
    price = _live_quote(fields)
    source = "delayed"
    if price is None:
        price = _stored_quote(database_url, fields["show"])
        source = "stored"
    if price is None:
        return f"The gateway is not open, and there is no stored price for {fields['show']}."
    number = Decimal(price)
    shown = f"{number:,.2f}"
    return f"{fields['show']}  {fields['label']}  {fields['exchange']} {fields['currency']}  {shown}  {source}"


def _live_quote(fields: dict):
    from athena_exec.broker import IbBroker

    host = os.environ.get("IB_GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("IB_GATEWAY_PORT", "4001"))
    from athena_exec.clients import client_id

    login = client_id("telegram")
    broker = IbBroker()
    try:
        if not broker.connect(host, port, login, True):
            return None
        return broker.quote(fields["kind"], fields["symbol"])
    finally:
        broker.disconnect()


def _stored_quote(database_url: str, symbol: str):
    from athena_marketdata.pricing import last_tradeable_price

    try:
        with connect(database_url) as conn:
            return last_tradeable_price(conn, symbol)
    except Exception as exc:
        log.info("stored quote failed symbol=%s error=%s", symbol, exc.__class__.__name__)
        return None


def _handle_callback(client: TelegramClient, database_url: str, allowlist: set[str], query: dict) -> None:
    actor = str((query.get("from") or {}).get("id", ""))
    allowlist = _fresh_allowlist(database_url)
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


def _apply(database_url: str, allowlist: set[str], draft_id: str, decision: str, actor: str, reason: str = "") -> str:
    try:
        with connect(database_url) as conn:
            decide(conn, draft_id, decision, actor, live_allowlist(conn), reason)
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


def draft_list_messages(conn) -> list[tuple[str, dict]]:
    """One text and keyboard per pending draft on the active accounts."""
    accounts = active_accounts()
    rows = conn.execute(
        """
        SELECT id, account_id, symbol, tactic, contracts
        FROM drafts
        WHERE status = 'pending' AND account_id = ANY(%s)
        ORDER BY created_at
        """,
        (accounts,),
    ).fetchall()
    messages = []
    for draft_id, account_id, symbol, tactic, contracts in rows:
        text = f"{draft_id}  {account_id}  {symbol}  {tactic}  {contracts}"
        messages.append((text, _markup(draft_id, "")))
    return messages


def _send_draft_list(client: TelegramClient, database_url: str, chat_id: int) -> None:
    with connect(database_url) as conn:
        messages = draft_list_messages(conn)
    if not messages:
        client.call("sendMessage", {"chat_id": chat_id, "text": "No draft is waiting."})
        return
    for text, markup in messages:
        client.call("sendMessage", {"chat_id": chat_id, "text": text, "reply_markup": markup})


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
            lines.extend(_position_lines(conn, account))
    return "\n".join(lines)


def _slots_text(database_url: str) -> str:
    from datetime import datetime

    accounts = active_accounts()
    if not accounts:
        return "No account configured."
    lines = []
    with connect(database_url) as conn:
        for account in accounts:
            settings = load_active(conn, account)
            state = slot_state(datetime.now(HKT), settings.ib)
            lines.append(f"{account} now {state}")
            lines.append(f"Athena {settings.ib.athena_slots}")
            lines.append(f"Mobile {settings.ib.mobile_slots}")
    lines.append("HK weekdays, plus the US session through Saturday 04:00. Lunch stays idle.")
    return "\n".join(lines)


def _position_lines(conn, account: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT ON (symbol, sec_type, expiry, contract_right, strike)
            symbol, sec_type, contract_right, strike, quantity, avg_price
        FROM positions
        WHERE account_id = %s
        ORDER BY symbol, sec_type, expiry, contract_right, strike, snapshot_at DESC, id DESC
        """,
        (account,),
    ).fetchall()
    open_rows = [row for row in rows if row[4] != 0]
    if not open_rows:
        return ["No open position."]
    lines = []
    for symbol, sec_type, right, strike, quantity, price in open_rows:
        leg = f" {right} {strike}" if right else ""
        lines.append(f"{symbol} {sec_type}{leg} {quantity} @ {price}")
    return lines


def _backup_now(database_url: str) -> str:
    directory = Path(os.environ.get("ATHENA_LOCAL_BACKUP_DIR", "var/backups"))
    with connect(database_url) as conn:
        path = write_snapshot(conn, directory)
    try:
        key = sync_snapshot(path)
    except Exception:
        return f"Backup {path.name}. S3 sync failed."
    if key:
        return f"Backup {path.name}. Synced."
    return f"Backup {path.name}."


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
        if parts[0] == "get":
            domain = parts[1] if len(parts) > 1 else ""
            if domain != "risk":
                return "Use /settings get risk"
            lines = []
            for account in accounts:
                risk = load_active(conn, account).risk
                lines.append(account)
                lines.append(f"profile {risk.profile}")
                lines.append(f"symbol {risk.max_symbol_pct_nav}")
                lines.append(f"cash {risk.cash_reserve_pct}")
                lines.append(f"halt {risk.daily_loss_halt_pct}")
                lines.append(f"contracts {risk.max_contracts_per_symbol}")
                lines.append(f"position {risk.max_position_per_symbol}")
                lines.append(f"rv {risk.min_rv_pctl_new_short}")
                lines.append(f"dte {risk.min_dte_new}")
                lines.append(f"margin {risk.max_short_opt_margin_pct}")
                lines.append(f"earnings {risk.earnings_blackout_days}")
                lines.append(f"orders {risk.max_orders_per_day}")
                lines.append("privilege all_orders")
            return "\n".join(lines)
        if len(parts) != 2:
            return "Use /settings get risk or /settings roll_dte 14"
        key, raw = parts
        lines = []
        for account in accounts:
            try:
                lines.append(set_knob(conn, account, key, raw, actor))
            except ValueError as exc:
                return str(exc)
        return "\n".join(lines)


def _news(database_url: str, arg: str) -> str:
    from athena_strategy.news import store_manual

    symbol, _, title = arg.strip().partition(" ")
    if not symbol:
        return "Use /news <symbol> or /news <symbol> <headline>"
    if not title:
        return _stored_headlines(database_url, symbol)
    with connect(database_url) as conn:
        stored = store_manual(conn, symbol, title)
    return f"Stored {symbol.upper()}." if stored else f"{symbol.upper()} was already stored."


def _stored_headlines(database_url: str, symbol: str) -> str:
    name = symbol.upper()
    with connect(database_url) as conn:
        rows = conn.execute(
            """
            SELECT ts, source, title
            FROM news_events
            WHERE symbol = %s AND dismissed = false
            ORDER BY ts DESC
            LIMIT 8
            """,
            (name,),
        ).fetchall()
    return format_headlines(name, rows)


def format_headlines(symbol: str, rows: list[tuple]) -> str:
    """The newest stored headlines for one symbol. Dismissed rows are already left out."""
    if not rows:
        return f"No stored headline for {symbol}."
    lines = [f"{symbol}  {len(rows)} stored"]
    for ts, source, title in rows:
        when = ts.astimezone(HKT).strftime("%Y-%m-%d %H:%M")
        lines.append("")
        lines.append(f"{when}  {source}")
        lines.append((title or "").strip()[:180])
    return "\n".join(lines)


def _ops(database_url: str, actor: str, allowlist: set[str], command: str) -> str:
    text = command.strip()
    if text == "health":
        return _health(database_url)
    if text == "logs":
        return log_tail(Path("var/athena.log"))
    try:
        return ops_action(actor, allowlist, text)
    except (PermissionError, ValueError) as exc:
        return str(exc)


def _health(database_url: str) -> str:
    from datetime import datetime

    accounts = active_accounts()
    if not accounts:
        return "No account configured."
    with connect(database_url) as conn:
        settings = load_active(conn, accounts[0])
        state = slot_state(datetime.now(HKT), settings.ib)
    directory = Path(os.environ.get("ATHENA_LOCAL_BACKUP_DIR", "var/backups"))
    files = sorted(directory.glob("athena-*.json")) if directory.is_dir() else []
    backup = files[-1].name if files else "none"
    host = os.environ.get("IB_GATEWAY_HOST", settings.ib.gateway_host)
    port = int(os.environ.get("IB_GATEWAY_PORT", str(settings.ib.gateway_port)))
    gateway = "open" if gateway_open(host, port) else "closed"
    return f"Slot {state}\nBackup {backup}\nGateway {gateway}"


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


def _facts(draft_id: str) -> list[str]:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return []
    try:
        with connect(url) as conn:
            return draft_facts(conn, draft_id)
    except Exception:
        log.info("draft facts missing id=%s", draft_id)
        return []


def _draft_text(
    draft_id: str,
    symbol: str,
    tactic: str,
    contracts: int,
    phase: str,
    reason: str,
    control_url: str,
    account_id: str = "",
    facts: list[str] | None = None,
) -> str:
    lines = [f"Approve or reject {symbol} {tactic}", f"Draft {draft_id}"]
    if facts:
        lines.extend(facts)
    else:
        lines.extend([
            account_id,
            f"{contracts} contract" if contracts == 1 else f"{contracts} contracts",
            f"Phase {phase}",
            reason,
        ])
    lines.append("Use the buttons on this message.")
    if control_url:
        lines.append(f"Approve page {control_url}")
    return "\n".join(line for line in lines if line)


def _open(request: Request, timeout: int):
    proxy = os.environ.get("TELEGRAM_HTTPS_PROXY", "").strip()
    if not proxy:
        return urlopen(request, timeout=timeout)
    opener = build_opener(ProxyHandler({"http": proxy, "https": proxy}))
    return opener.open(request, timeout=timeout)


def _allowlist() -> set[str]:
    raw = os.environ.get("TELEGRAM_ALLOWLIST_USER_IDS", "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def _credentials(database_url: str) -> tuple[str, set[str]]:
    with connect(database_url) as conn:
        settings = live_settings(conn)
        if settings is None:
            token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
            return token, _allowlist()
        return bot_token(settings), live_allowlist(conn)


def _fresh_allowlist(database_url: str) -> set[str]:
    try:
        _token, allow = _credentials(database_url)
    except Exception:
        log.info("telegram allowlist unread")
        return _allowlist()
    return allow


def _live_send() -> tuple[str, set[str]]:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), _allowlist()
    try:
        return _credentials(database_url)
    except Exception:
        log.info("telegram settings unread")
        return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), _allowlist()


def _claim_link(database_url: str, actor: str, code: str) -> bool:
    expires = _link_codes.get(code)
    if expires is None or expires < datetime.now(UTC):
        return False
    accounts = active_accounts()
    if not accounts or not actor:
        return False
    with connect(database_url) as conn:
        current = load_active(conn, accounts[0])
        payload = SettingsPayload.model_validate(current.model_dump())
        payload.notify = payload.notify.model_copy(update={"telegram_user_ids": actor})
        try:
            save_settings(conn, accounts[0], payload, f"telegram:{actor}", "link telegram")
        except ValueError:
            pass
    _link_codes.pop(code, None)
    log.info("telegram linked")
    return True


def _read_offset() -> int | None:
    if not OFFSET_PATH.is_file():
        return None
    raw = OFFSET_PATH.read_text(encoding="utf-8").strip()
    return int(raw) if raw else None


def _write_offset(offset: int) -> None:
    OFFSET_PATH.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_PATH.write_text(str(offset), encoding="utf-8")


def _safe(detail: str, token: str = "") -> str:
    for secret in (token, os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()):
        if secret:
            detail = detail.replace(secret, "[redacted]")
    return detail[:300]
