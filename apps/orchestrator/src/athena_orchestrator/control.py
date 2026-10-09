"""One-time Approve page. A token works once, then it is spent."""

from __future__ import annotations

import os
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from athena_store import connect
from athena_strategy import use_link

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
</style>
</head>
<body>
<main>
__BODY__
</main>
</body>
</html>
"""

_FORM = """<h1>Approve draft</h1>
<p>This link works once.</p>
<form method="post">
  <input type="hidden" name="actor" value="__ACTOR__">
  <button name="decision" value="approve" type="submit">Approve</button>
  <button name="decision" value="reject" type="submit">Reject</button>
</form>
"""


def render_page(actor: str) -> str:
    body = _FORM.replace("__ACTOR__", escape(actor, quote=True))
    return _SHELL.replace("__BODY__", body)


def render_message(title: str, detail: str) -> str:
    body = f"<h1>{escape(title)}</h1><p>{escape(detail)}</p>"
    return _SHELL.replace("__BODY__", body)


def start_control_server(database_url: str, port: int, allowlist: set[str]) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            token = _token(self.path)
            if token is None:
                self._send(404, render_message("Missing link", "This page needs a one-time token."))
                return
            actor = next(iter(allowlist), "")
            self._send(200, render_page(actor))

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
                with connect(database_url) as conn:
                    draft_id = use_link(conn, token, decision, actor, allowlist)
            except PermissionError:
                self._send(403, render_message("Not allowed", "This account is not on the allowlist."))
                return
            except ValueError as exc:
                self._send(410, render_message("Link used", str(exc)))
                return
            title = "Approved" if decision == "approve" else "Rejected"
            self._send(200, render_message(title, f"{title} {draft_id}."))

        def log_message(self, fmt: str, *args: object) -> None:
            return

        def _send(self, status: int, body: str, content_type: str = "text/html; charset=utf-8") -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return server


def _token(path: str) -> str | None:
    parts = [part for part in urlparse(path).path.split("/") if part]
    if len(parts) == 2 and parts[0] == "c":
        return parts[1]
    return None


def allowlist_from_env() -> set[str]:
    raw = os.environ.get("TELEGRAM_ALLOWLIST_USER_IDS", "")
    return {part.strip() for part in raw.split(",") if part.strip()}
