"""Local HTTP servers. One request per connection, then the socket is closed."""

from __future__ import annotations

import logging
from http.server import BaseHTTPRequestHandler

log = logging.getLogger("athena.http")


class QuietHandler(BaseHTTPRequestHandler):
    """Serve one request. A peer that resets the socket is logged and released."""

    def handle(self) -> None:
        self.close_connection = True
        try:
            self.handle_one_request()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            client_closed(self)

    def log_message(self, fmt: str, *args: object) -> None:
        return


def client_closed(handler) -> None:
    """The browser dropped this socket. Record it and do not read another request."""
    handler.close_connection = True
    path = getattr(handler, "path", "") or "before the request"
    host = getattr(handler, "client_address", ("", 0))[0]
    log.info("client closed %s %s", host, path)
