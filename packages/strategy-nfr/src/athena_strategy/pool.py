"""One-shot lookup when a symbol joins the pool. No IB orders."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import psycopg

from athena_strategy.agents import record_call
from athena_strategy.dsh import classify_symbol
from athena_strategy.news import yahoo_symbol

log = logging.getLogger("athena.pool")

_UA = "Mozilla/5.0 (compatible; AthenaNews/0.1)"
_TITLE = re.compile(r"<title>\s*[A-Z0-9.]+\s+-\s+(.+?)\s+Stock Price", re.I)
_SNAPSHOT = re.compile(
    r">(?P<label>Market Cap|Price|Prev Close)</div></td>.*?<b>(?P<value>[0-9][^<]*)</b>",
    re.S,
)
_SECTOR = re.compile(r'f=sec_[^"]+"[^>]*>([^<]+)</a>')
_INDUSTRY = re.compile(r'f=ind_[^"]+"[^>]*>([^<]+)</a>')
_SCALES = (("T", Decimal("1e12")), ("B", Decimal("1e9")), ("M", Decimal("1e6")), ("K", Decimal("1e3")))


def clean_symbol(raw: str) -> str:
    text = (raw or "").strip().upper()
    if not text or len(text) > 12 or any(char != "." and not char.isalnum() for char in text):
        raise ValueError("Use a ticker such as NVDA or 700.")
    if text.startswith(".") or text.endswith(".") or ".." in text:
        raise ValueError("Use a ticker such as NVDA or 700.")
    return text


def parse_finviz(html: str) -> dict:
    """Name, size, and last two closes from a Finviz quote page."""
    found = {}
    for match in _SNAPSHOT.finditer(html or ""):
        found.setdefault(match.group("label"), match.group("value").strip())
    title = _TITLE.search(html or "")
    sector = _SECTOR.search(html or "")
    industry = _INDUSTRY.search(html or "")
    return {
        "name": "" if title is None else " ".join(title.group(1).split())[:80],
        "market_cap": _parse_amount(found.get("Market Cap", "")),
        "price": _parse_amount(found.get("Price", "")),
        "prev": _parse_amount(found.get("Prev Close", "")),
        "sector": "" if sector is None else " ".join(sector.group(1).split())[:80],
        "industry": "" if industry is None else " ".join(industry.group(1).split())[:80],
    }


def parse_chart(body: dict) -> dict:
    """Name and closes from a Yahoo chart payload. Market cap is not in that payload."""
    result = (body.get("chart") or {}).get("result") or []
    meta = result[0].get("meta") if result else None
    if not isinstance(meta, dict):
        return {"name": "", "market_cap": None, "price": None, "prev": None, "sector": "", "industry": ""}
    name = str(meta.get("longName") or meta.get("shortName") or "")
    prev = meta.get("chartPreviousClose")
    if prev is None:
        prev = meta.get("previousClose")
    return {
        "name": " ".join(name.split())[:80],
        "market_cap": None,
        "price": _parse_amount(str(meta.get("regularMarketPrice") or "")),
        "prev": _parse_amount("" if prev is None else str(prev)),
        "sector": "",
        "industry": "",
    }


def lookup_company(symbol: str) -> dict:
    """Public name, size, and a fallback quote. Price on the book comes from IB when it answers."""
    return _lookup(symbol)


def store_profile(
    conn: psycopg.Connection,
    account: str,
    symbol: str,
    facts: dict,
    model: str,
    verb: str = "Added",
    quote_note: str = "",
    provider: str = "DSH",
    token: str = "",
) -> str:
    """Store the name, size, and DSH classifier badges. A blank field leaves the stored value."""
    picked, result = classify_symbol(symbol, facts, model, provider, token)
    payload = picked if picked is not None else {"error": result["error"]}
    record_call(conn, account, "Universe", symbol, model or "deepseek-flash", provider, "off", result, payload)
    ai_note = result["error"]
    name = facts["name"] or ("" if picked is None else picked["name"])
    classifiers = "" if picked is None else picked["classifiers"]
    thesis = "" if picked is None else picked["thesis"]
    conn.execute(
        """
        INSERT INTO symbol_facts (symbol, name, market_cap)
        VALUES (%s, %s, %s)
        ON CONFLICT (symbol) DO UPDATE
        SET name = CASE WHEN EXCLUDED.name <> '' THEN EXCLUDED.name ELSE symbol_facts.name END,
            market_cap = COALESCE(EXCLUDED.market_cap, symbol_facts.market_cap),
            updated_at = now()
        """,
        (symbol, name, facts["market_cap"]),
    )
    conn.execute(
        """
        INSERT INTO symbol_phases (account_id, symbol, phase, tags, tactic, classifiers, thesis)
        VALUES (%s, %s, 'P1', '', 'HOLD', %s, %s)
        ON CONFLICT (account_id, symbol) DO UPDATE
        SET classifiers = CASE WHEN EXCLUDED.classifiers <> '' THEN EXCLUDED.classifiers ELSE symbol_phases.classifiers END,
            thesis = CASE WHEN EXCLUDED.thesis <> '' THEN EXCLUDED.thesis ELSE symbol_phases.thesis END,
            updated_at = now()
        """,
        (account, symbol, classifiers, thesis),
    )
    if not quote_note:
        quote_note = "Price from the public quote." if _sync_once(conn, symbol, facts["price"], facts["prev"]) else ""
    conn.commit()
    log.info("pool fill symbol=%s classifiers=%s", symbol, classifiers or "-")
    return _note(verb, symbol, name, classifiers, ai_note, quote_note)


def fill_symbol(conn: psycopg.Connection, account: str, symbol: str, model: str) -> str:
    """Store the public name and size, one price pair, and DSH classifier badges."""
    return store_profile(conn, account, symbol, lookup_company(symbol), model)


def _lookup(symbol: str) -> dict:
    empty = {"name": "", "market_cap": None, "price": None, "prev": None, "sector": "", "industry": ""}
    if not symbol.isdigit():
        page = _get(f"https://finviz.com/quote.ashx?t={symbol}", "text/html")
        if page:
            parsed = parse_finviz(page)
            if parsed["name"] or parsed["price"] is not None:
                return parsed
    ticker = yahoo_symbol(symbol)
    raw = _get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d",
        "application/json",
    )
    if not raw:
        return empty
    try:
        return parse_chart(json.loads(raw))
    except json.JSONDecodeError:
        log.info("pool chart skipped symbol=%s", symbol)
        return empty


def _sync_once(conn: psycopg.Connection, symbol: str, price: Decimal | None, prev: Decimal | None) -> bool:
    if price is None:
        return False
    existing = conn.execute(
        "SELECT 1 FROM market_bars WHERE symbol = %s LIMIT 1",
        (symbol,),
    ).fetchone()
    if existing is not None:
        return False
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    rows = [(now, price)]
    if prev is not None:
        rows.append((now - timedelta(days=1), prev))
    for ts, close in rows:
        conn.execute(
            """
            INSERT INTO market_bars (symbol, ts, open, high, low, close, volume, source)
            VALUES (%s, %s, %s, %s, %s, %s, 0, 'pool-sync')
            ON CONFLICT (symbol, ts, source) DO NOTHING
            """,
            (symbol, ts, close, close, close, close),
        )
    return True


def _note(verb: str, symbol: str, name: str, classifiers: str, ai_note: str, quote_note: str) -> str:
    parts = [f"{verb} {symbol}."]
    if name:
        parts.append(f"{name}.")
    if classifiers:
        parts.append(classifiers.replace(",", ", ") + ".")
    elif ai_note:
        parts.append(ai_note)
    else:
        parts.append("No classifier was selected.")
    if quote_note:
        parts.append(quote_note)
    return " ".join(parts)


def _parse_amount(raw: str) -> Decimal | None:
    text = raw.strip().replace(",", "").replace("$", "")
    if not text:
        return None
    scale = Decimal(1)
    suffix = text[-1:].upper()
    if suffix in {code for code, _scale in _SCALES}:
        scale = dict(_SCALES)[suffix]
        text = text[:-1]
    try:
        return (Decimal(text) * scale).quantize(Decimal("1")) if scale != 1 else Decimal(text)
    except InvalidOperation:
        return None


def _get(url: str, accept: str) -> str:
    request = Request(url, headers={"User-Agent": _UA, "Accept": accept})
    try:
        with urlopen(request, timeout=12) as response:
            return response.read(1_500_000).decode("utf-8", "replace")
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        log.info("pool lookup skipped error=%s", exc.__class__.__name__)
        return ""
