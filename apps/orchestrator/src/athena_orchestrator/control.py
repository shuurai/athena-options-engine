"""One-time Approve page. A token works once, then it is spent."""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal, InvalidOperation
from html import escape
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from athena_orchestrator.httpd import QuietHandler
from athena_store import connect
from athena_strategy import AmendError, amend_view, use_link

_SHELL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Athena</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #f3efe6;
    --ink: #1c1915;
    --muted: #6d665e;
    --line: #ddd4c6;
    --approve: #1d6b43;
    --approve-ink: #f4fff8;
    --reject-ink: #8d3128;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #141311;
      --ink: #f4efe6;
      --muted: #b7b0a6;
      --line: #3c362f;
      --approve: #2f9d62;
      --approve-ink: #07140c;
      --reject-ink: #ffb4a8;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    min-height: 100dvh;
    padding: 28px 16px 40px;
    background: var(--bg);
    color: var(--ink);
    font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  main { max-width: 28rem; margin: 0 auto; }
  h1 {
    margin: 0 0 8px;
    font-size: 1.75rem;
    font-weight: 650;
    letter-spacing: -0.03em;
    line-height: 1.15;
  }
  p { margin: 0 0 28px; color: var(--muted); font-size: 1rem; line-height: 1.45; }
  ul.facts {
    margin: 0 0 28px;
    padding: 0;
    list-style: none;
    display: grid;
    gap: 8px;
  }
  ul.facts li {
    padding: 12px 14px;
    border: 1px solid var(--line);
    border-radius: 12px;
    font-size: 1rem;
    line-height: 1.35;
  }
  form { display: grid; gap: 12px; }
  button {
    display: block;
    width: 100%;
    margin: 0;
    padding: 18px 20px;
    border: 1px solid transparent;
    border-radius: 14px;
    font: inherit;
    font-size: 1.125rem;
    font-weight: 650;
    line-height: 1.2;
    cursor: pointer;
  }
  button[value="approve"] { background: var(--approve); color: var(--approve-ink); }
  button[value="reject"] {
    background: transparent;
    color: var(--reject-ink);
    border-color: var(--line);
  }
  label {
    display: grid;
    gap: 6px;
    font-size: 0.95rem;
    font-weight: 650;
  }
  input, select {
    width: 100%;
    margin: 0;
    padding: 16px 14px;
    border: 1px solid var(--line);
    border-radius: 12px;
    background: transparent;
    color: inherit;
    font: inherit;
    font-size: 1.125rem;
    font-weight: 500;
  }
  input:focus, select:focus, textarea:focus,
  input:focus-visible, select:focus-visible, textarea:focus-visible {
    outline: none;
    box-shadow: none;
    border-color: rgba(107, 150, 242, 0.38);
  }
  p.note { margin: 0; font-size: 0.95rem; }
  p.error { margin: 0 0 8px; color: var(--reject-ink); }
</style>
</head>
<body>
<main>
__BODY__
</main>
</body>
</html>
"""

def render_page(
    actor: str,
    heading: str = "Approve draft",
    facts: list[str] | None = None,
    form: dict | None = None,
    error: str = "",
) -> str:
    items = "".join(f"<li>{escape(line)}</li>" for line in facts or [])
    fact_html = f'<ul class="facts">{items}</ul>' if items else ""
    error_html = f'<p class="error">{escape(error)}</p>' if error else ""
    body = (
        f"<h1>{escape(heading)}</h1>"
        f"{fact_html}"
        f"{error_html}"
        "<form method=\"post\">"
        f"{_amend_fields(form)}"
        f"<p class=\"note\">{escape(_note(form))}</p>"
        f"<input type=\"hidden\" name=\"actor\" value=\"{escape(actor, quote=True)}\">"
        "<button name=\"decision\" value=\"approve\" type=\"submit\">Approve</button>"
        "<button name=\"decision\" value=\"reject\" type=\"submit\">Reject</button>"
        "</form>"
    )
    return _SHELL.replace("__BODY__", body)


def _note(form: dict | None) -> str:
    if form and form.get("expiries"):
        return "This link works once. Tactic stays. Pick a listed expiry and strike."
    return "This link works once. Strike, expiry, and tactic stay on this draft."


def _amend_fields(form: dict | None) -> str:
    if not form:
        return ""
    parts = [
        _number("Contracts", "contracts", form["contracts"], step="1", low="1", high=str(form["max_contracts"])),
    ]
    if form.get("expiries"):
        parts.append(_select("Expiry", "expiry", [(item, item.isoformat()) for item in form["expiries"]], form.get("expiry")))
    if form.get("puts"):
        parts.append(_strike_select("Put strike", "put_strike", form["puts"], form.get("put_strike")))
    if form.get("calls"):
        parts.append(_strike_select("Call strike", "call_strike", form["calls"], form.get("call_strike")))
    if form.get("put_limit") is not None:
        parts.append(_number("Put limit", "put_limit", form["put_limit"]))
    if form.get("call_limit") is not None:
        parts.append(_number("Call limit", "call_limit", form["call_limit"]))
    if form.get("limit_price") is not None:
        parts.append(_number("Limit", "limit_price", form["limit_price"]))
    if form.get("expiries"):
        parts.append(
            "<script>"
            "const day=document.querySelector('[name=expiry]');"
            "function show(){const want=day.value;"
            "document.querySelectorAll('[data-expiry]').forEach(opt=>{"
            "opt.hidden=opt.dataset.expiry!==want;"
            "if(opt.hidden&&opt.selected)opt.selected=false;});"
            "document.querySelectorAll('select[name$=_strike]').forEach(sel=>{"
            "if(!sel.selectedOptions.length||sel.selectedOptions[0].hidden){"
            "const next=[...sel.options].find(opt=>!opt.hidden);if(next)next.selected=true;}});}"
            "if(day){day.addEventListener('change',show);show();}"
            "</script>"
        )
    return "".join(parts)


def _select(label: str, name: str, options: list[tuple[object, str]], current: object) -> str:
    rows = []
    for value, shown in options:
        selected = " selected" if value == current else ""
        rows.append(f"<option value=\"{escape(shown, quote=True)}\"{selected}>{escape(shown)}</option>")
    return f"<label>{escape(label)}<select name=\"{name}\">{''.join(rows)}</select></label>"


def _strike_select(label: str, name: str, options: list[tuple], current: object) -> str:
    rows = []
    for expiry, strike in options:
        shown = _input_value(strike)
        selected = " selected" if strike == current else ""
        rows.append(
            f"<option value=\"{escape(shown, quote=True)}\" data-expiry=\"{expiry.isoformat()}\"{selected}>"
            f"{escape(shown)}</option>"
        )
    return f"<label>{escape(label)}<select name=\"{name}\">{''.join(rows)}</select></label>"


def _number(label: str, name: str, value: object, step: str = "0.01", low: str = "0.01", high: str = "") -> str:
    high_attr = f' max="{escape(high, quote=True)}"' if high else ""
    shown = _input_value(value)
    return (
        f"<label>{escape(label)}"
        f"<input name=\"{name}\" type=\"number\" inputmode=\"decimal\" required"
        f" min=\"{low}\" step=\"{step}\"{high_attr} value=\"{escape(shown, quote=True)}\">"
        f"</label>"
    )


def _input_value(value: object) -> str:
    text = format(value, "f") if isinstance(value, Decimal) else str(value)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def render_message(title: str, detail: str) -> str:
    body = f"<h1>{escape(title)}</h1><p>{escape(detail)}</p>"
    return _SHELL.replace("__BODY__", body)


def start_control_server(database_url: str, port: int, allowlist: set[str]) -> ThreadingHTTPServer:
    class Handler(QuietHandler):
        def do_GET(self) -> None:
            token = _token(self.path)
            if token is None:
                self._send(404, render_message("Missing link", "This page needs a one-time token."))
                return
            with connect(database_url) as conn:
                from athena_settings.resolve import live_allowlist

                actor = next(iter(live_allowlist(conn)), "")
                draft_id = _live_draft(conn, token)
                view = amend_view(conn, draft_id) if draft_id else None
            if view is None:
                self._send(410, render_message("Link used", "This link is used or expired."))
                return
            self._send(200, render_page(actor, view["heading"], view["facts"], view))

        def do_POST(self) -> None:
            token = _token(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            form = parse_qs(self.rfile.read(length).decode("utf-8"))
            decision = (form.get("decision") or ["approve"])[0]
            actor = (form.get("actor") or [""])[0]
            if token is None:
                self._send(404, render_message("Missing link", "This page needs a one-time token."))
                return
            try:
                amend = _amend_from_form(form) if decision == "approve" else None
                with connect(database_url) as conn:
                    from athena_settings.resolve import live_allowlist

                    draft_id = use_link(conn, token, decision, actor, live_allowlist(conn), amend)
            except PermissionError:
                self._send(403, render_message("Not allowed", "This account is not on the allowlist."))
                return
            except AmendError as exc:
                self._send(400, self._again(database_url, token, actor, str(exc)))
                return
            except ValueError as exc:
                self._send(410, render_message("Link used", str(exc)))
                return
            title = "Approved" if decision == "approve" else "Rejected"
            self._send(200, render_message(title, f"{title} {draft_id}."))

        def _again(self, database_url: str, token: str, actor: str, error: str) -> str:
            with connect(database_url) as conn:
                draft_id = _live_draft(conn, token)
                view = amend_view(conn, draft_id) if draft_id else None
            if view is None:
                return render_message("Link used", "This link is used or expired.")
            return render_page(actor, view["heading"], view["facts"], view, error)

        def _send(self, status: int, body: str, content_type: str = "text/html; charset=utf-8") -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return server


def _amend_from_form(form: dict) -> dict:
    return {
        "contracts": _int_field(form, "contracts"),
        "put_limit": _decimal_field(form, "put_limit"),
        "call_limit": _decimal_field(form, "call_limit"),
        "limit_price": _decimal_field(form, "limit_price"),
        "expiry": _date_field(form, "expiry"),
        "put_strike": _decimal_field(form, "put_strike"),
        "call_strike": _decimal_field(form, "call_strike"),
    }


def _int_field(form: dict, key: str) -> int | None:
    raw = (form.get(key) or [""])[0].strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise AmendError(f"{key} must be a whole number") from exc


def _date_field(form: dict, key: str):
    raw = (form.get(key) or [""])[0].strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise AmendError("expiry must be a listed date") from exc


def _decimal_field(form: dict, key: str) -> Decimal | None:
    raw = (form.get(key) or [""])[0].strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise AmendError(f"{key} must be a number") from exc


def _live_draft(conn, token: str) -> str | None:
    row = conn.execute(
        """
        SELECT draft_id FROM control_links
        WHERE token = %s AND used_at IS NULL AND expires_at > now()
        """,
        (token,),
    ).fetchone()
    if row is None:
        return None
    return row[0]


def _token(path: str) -> str | None:
    parts = [part for part in urlparse(path).path.split("/") if part]
    if len(parts) == 2 and parts[0] == "c":
        return parts[1]
    return None


def allowlist_from_env() -> set[str]:
    raw = os.environ.get("TELEGRAM_ALLOWLIST_USER_IDS", "")
    return {part.strip() for part in raw.split(",") if part.strip()}
