"""Which book the console pages show. The cycle does not read this."""

from __future__ import annotations

import contextvars
from http.cookies import CookieError, SimpleCookie

from athena_exec.accounts import active_accounts

VIEW_COOKIE = "athena_view"
_VIEW: contextvars.ContextVar[str] = contextvars.ContextVar("athena_view", default="")


def bind_view(cookie_header: str | None) -> None:
    """Remember this request's view. An unknown id falls back to the first account."""
    _VIEW.set(_cookie_account(cookie_header))


def current_view() -> str:
    """The book for this request. Empty when this mode has no account."""
    accounts = active_accounts()
    if not accounts:
        return ""
    chosen = _VIEW.get()
    if chosen in accounts:
        return chosen
    return accounts[0]


def _cookie_account(cookie_header: str | None) -> str:
    if not cookie_header:
        return ""
    jar = SimpleCookie()
    try:
        jar.load(cookie_header)
    except CookieError:
        return ""
    morsel = jar.get(VIEW_COOKIE)
    if morsel is None:
        return ""
    return morsel.value
