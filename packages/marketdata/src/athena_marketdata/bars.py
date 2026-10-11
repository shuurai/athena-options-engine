"""iTick kline fetch. A missing quote is left missing. Seed bars are not written."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from urllib.error import URLError
from urllib.request import Request, urlopen

import psycopg

from athena_marketdata.pricing import has_intraday

log = logging.getLogger("athena.marketdata")


def write_watchlist_bars(
    conn: psycopg.Connection,
    symbols: list[str],
    base_url: str,
    api_key: str,
) -> int:
    if not api_key:
        log.info("itick key missing, wrote nothing")
    hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    written = 0
    for symbol in symbols:
        bar = _from_itick(base_url, api_key, symbol) if api_key else None
        if bar is None:
            if symbol.isdigit() and not has_intraday(conn, symbol):
                log.info("hk bar waits for the IB session symbol=%s", symbol)
            continue
        conn.execute(
            """
            INSERT INTO market_bars (symbol, ts, open, high, low, close, volume, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'itick')
            ON CONFLICT (symbol, ts, source) DO UPDATE SET close = EXCLUDED.close
            """,
            (symbol, hour, bar, bar, bar, bar, 0),
        )
        written += 1
    if written:
        conn.commit()
    return written


def _from_itick(base_url: str, api_key: str, symbol: str) -> Decimal | None:
    url = f"{base_url.rstrip('/')}/stock/kline?region=US&code={symbol}&kType=8&limit=1"
    request = Request(url, headers={"token": api_key, "accept": "application/json"})
    try:
        with urlopen(request, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        log.info("itick fetch failed symbol=%s error=%s", symbol, exc.__class__.__name__)
        return None
    return _close_from_body(body)


def _close_from_body(body: object) -> Decimal | None:
    if not isinstance(body, dict):
        return None
    data = body.get("data")
    row = data[-1] if isinstance(data, list) and data else data
    if not isinstance(row, dict):
        return None
    raw = row.get("c", row.get("close"))
    if raw is None:
        return None
    return Decimal(str(raw))
