"""Headline intake. Tags come from stored events, not from a single post."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.error import URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import psycopg

from athena_settings.model import SettingsPayload

log = logging.getLogger("athena.news")

NEWS_OWNED = {"NEWS_WASH", "FED_EVENT", "EARNINGS_NEAR"}
ET = ZoneInfo("America/New_York")
_UA = "Mozilla/5.0 (compatible; AthenaNews/0.1)"
_NEGATIVE = (
    "downgrade",
    "miss",
    "misses",
    "plunge",
    "plunges",
    "slump",
    "slumps",
    "warning",
    "lawsuit",
    "probe",
    "investigation",
    "subpoena",
    "antitrust",
    "fraud",
    "recall",
    "layoff",
    "layoffs",
    "selloff",
    "sell-off",
    "bearish",
    "shortfall",
    "disappointing",
    "slides",
    "slide",
    "sinks",
    "tumbles",
    "tumble",
    "crash",
    "halted",
)
_POSITIVE = ("upgrade", "beat", "beats", "rally", "record", "surge", "surges", "raises")


def ingest_news(conn: psycopg.Connection, symbols: list[str], settings: SettingsPayload) -> int:
    """Fetch enabled sources and store new headlines plus calendar marks."""
    today = datetime.now(ET).date()
    stored = 0
    news = settings.news
    if news.yahoo:
        for symbol in symbols:
            stored += _save_headlines(conn, symbol, "yahoo", _yahoo(symbol))
    if news.finviz:
        for symbol in symbols:
            stored += _save_headlines(conn, symbol, "finviz", _finviz(symbol))
    if news.fed_calendar:
        _save_marks(conn, "FED", "", _fed_meetings())
    for symbol, event_day, title in _earnings(symbols, today, news.earnings_near_days):
        _save_marks(conn, "EARNINGS", symbol, [(event_day, title)])
    conn.commit()
    for symbol in symbols:
        tags = tags_from_events(conn, symbol, settings, today)
        if tags:
            log.info("news_tags symbol=%s tags=%s", symbol, ",".join(sorted(tags)))
    return stored


def tags_from_events(
    conn: psycopg.Connection,
    symbol: str,
    settings: SettingsPayload,
    today: date | None = None,
) -> set[str]:
    """NEWS_WASH, FED_EVENT, and EARNINGS_NEAR for one symbol."""
    day = today or datetime.now(ET).date()
    news = settings.news
    tags: set[str] = set()
    start = datetime.combine(day - timedelta(days=news.wash_window_days), datetime.min.time(), ET)
    count = conn.execute(
        """
        SELECT count(*) FROM news_events
        WHERE symbol = %s AND sentiment < %s AND ts >= %s
        """,
        (symbol, news.wash_sentiment_below, start),
    ).fetchone()[0]
    if count >= news.wash_min_negative:
        tags.add("NEWS_WASH")
    fed = conn.execute(
        "SELECT event_date FROM calendar_marks WHERE kind = 'FED' AND symbol = ''"
    ).fetchall()
    if any(abs((row[0] - day).days) <= news.fed_window_days for row in fed):
        tags.add("FED_EVENT")
    earnings = conn.execute(
        """
        SELECT event_date FROM calendar_marks
        WHERE kind = 'EARNINGS' AND symbol = %s
        """,
        (symbol,),
    ).fetchall()
    if any(abs((row[0] - day).days) <= news.earnings_near_days for row in earnings):
        tags.add("EARNINGS_NEAR")
    return tags


def score_title(title: str) -> tuple[float, str]:
    """Keyword sentiment. Official releases stay off the wash path."""
    words = set(re.findall(r"[a-z0-9-]+", title.lower()))
    negative = bool(words & set(_NEGATIVE))
    positive = bool(words & set(_POSITIVE))
    if "earnings" in words:
        kind = "EARNINGS_RELEASE"
    elif words & {"fed", "fomc"}:
        kind = "FED_RATE_DECISION"
    elif negative and not positive:
        kind = "GENERIC_NEGATIVE"
    elif positive and not negative:
        kind = "GENERIC_POSITIVE"
    else:
        kind = "GENERIC"
    if negative and not positive:
        return -0.6, kind
    if positive and not negative:
        return 0.4, kind
    if negative and positive:
        return -0.2, kind
    return 0.0, kind


def parse_yahoo_rss(xml_text: str, now: datetime) -> list[tuple[datetime, str, str]]:
    root = ElementTree.fromstring(xml_text)
    rows: list[tuple[datetime, str, str]] = []
    for item in root.iter("item"):
        title = unescape((item.findtext("title") or "")).strip()
        link = (item.findtext("link") or "").strip()
        if not title:
            continue
        raw = item.findtext("pubDate")
        when = now
        if raw:
            parsed = parsedate_to_datetime(raw)
            when = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        rows.append((when, title, link))
    return rows


def parse_finviz_news(html: str, now: datetime) -> list[tuple[datetime, str, str]]:
    rows: list[tuple[datetime, str, str]] = []
    previous: datetime | None = None
    pattern = re.compile(
        r'<td width="130" align="right">\s*([^<]+?)\s*</td>.*?class="tab-link-news" href="([^"]+)"[^>]*>\s*([^<]+)',
        re.S,
    )
    for stamp, link, title in pattern.findall(html):
        when = _finviz_stamp(unescape(stamp).strip(), previous, now)
        previous = when
        text = unescape(title).strip()
        if text:
            rows.append((when, text, unescape(link).strip()))
    return rows


def parse_fomc_dates(html: str) -> list[tuple[date, str]]:
    parts = re.split(r'<a id="\d+">(\d{4}) FOMC Meetings</a>', html)
    found: list[tuple[date, str]] = []
    for year_text, block in zip(parts[1::2], parts[2::2], strict=False):
        year = int(year_text)
        pairs = re.findall(
            r'fomc-meeting__month[^>]*>\s*<strong>([A-Za-z]+)</strong>.*?fomc-meeting__date[^>]*>\s*([0-9]{1,2}(?:-[0-9]{1,2})?)',
            block,
            re.S,
        )
        for month_name, days in pairs:
            month = datetime.strptime(month_name, "%B").month
            last_day = int(days.split("-")[-1])
            event_day = date(year, month, last_day)
            found.append((event_day, f"FOMC {event_day.isoformat()}"))
    return found


def parse_nasdaq_earnings(body: object, symbols: set[str]) -> list[tuple[str, date, str]]:
    if not isinstance(body, dict):
        return []
    data = body.get("data")
    if not isinstance(data, dict):
        return []
    rows = []
    as_of = _nasdaq_as_of(str(data.get("asOf") or ""))
    wanted = {symbol.upper() for symbol in symbols}
    for row in data.get("rows") or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        if symbol not in wanted or as_of is None:
            continue
        rows.append((symbol, as_of, f"{symbol} earnings"))
    return rows


def _finviz_stamp(text: str, previous: datetime | None, now: datetime) -> datetime:
    if re.fullmatch(r"\d{1,2}:\d{2}[AP]M", text):
        clock = datetime.strptime(text, "%I:%M%p")
        day = previous.date() if previous else now.astimezone(ET).date()
        return datetime(day.year, day.month, day.day, clock.hour, clock.minute, tzinfo=ET)
    parsed = datetime.strptime(text, "%b-%d-%y %I:%M%p")
    return parsed.replace(tzinfo=ET)


def yahoo_symbol(symbol: str) -> str:
    """Hong Kong numeric codes use the Yahoo .HK ticker."""
    if symbol.isdigit():
        return f"{symbol.zfill(4)}.HK"
    return symbol


def _yahoo(symbol: str) -> list[tuple[datetime, str, str]]:
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={yahoo_symbol(symbol)}&region=US&lang=en-US"
    body = _get(url, "yahoo", symbol)
    if body is None:
        return []
    try:
        return parse_yahoo_rss(body, datetime.now(UTC))[:20]
    except ElementTree.ParseError:
        log.info("yahoo parse failed symbol=%s", symbol)
        return []


def _finviz(symbol: str) -> list[tuple[datetime, str, str]]:
    if symbol.isdigit():
        return []
    body = _get(f"https://finviz.com/quote.ashx?t={symbol}", "finviz", symbol)
    if body is None:
        return []
    return parse_finviz_news(body, datetime.now(ET))[:20]


def _fed_meetings() -> list[tuple[date, str]]:
    body = _get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", "fed", "")
    if body is None:
        return []
    return parse_fomc_dates(body)


def _earnings(symbols: list[str], today: date, window: int) -> list[tuple[str, date, str]]:
    wanted = set(symbols)
    found: list[tuple[str, date, str]] = []
    for offset in range(window + 1):
        day = today + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        body = _get(f"https://api.nasdaq.com/api/calendar/earnings?date={day.isoformat()}", "earnings", "")
        if body is None:
            continue
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            continue
        found.extend(parse_nasdaq_earnings(payload, wanted))
    return found


def _get(url: str, source: str, symbol: str) -> str | None:
    request = Request(url, headers={"User-Agent": _UA, "Accept": "*/*"})
    try:
        with urlopen(request, timeout=8) as response:
            return response.read().decode("utf-8", "replace")
    except (URLError, TimeoutError, OSError) as exc:
        log.info("news fetch failed source=%s symbol=%s error=%s", source, symbol or "-", exc.__class__.__name__)
        return None


def _save_headlines(
    conn: psycopg.Connection,
    symbol: str,
    source: str,
    rows: list[tuple[datetime, str, str]],
) -> int:
    written = 0
    for when, title, url in rows:
        sentiment, event_type = score_title(title)
        fingerprint = hashlib.sha256(f"{symbol.upper()}|{title.lower()}".encode()).hexdigest()[:24]
        result = conn.execute(
            """
            INSERT INTO news_events (fingerprint, ts, symbol, source, event_type, sentiment, title, url)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (fingerprint) DO NOTHING
            """,
            (fingerprint, when, symbol, source, event_type, sentiment, title, url),
        )
        written += result.rowcount
    return written


def _save_marks(conn: psycopg.Connection, kind: str, symbol: str, rows: list[tuple[date, str]]) -> None:
    for event_day, title in rows:
        conn.execute(
            """
            INSERT INTO calendar_marks (kind, symbol, event_date, title)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (kind, symbol, event_date) DO NOTHING
            """,
            (kind, symbol, event_day, title),
        )


def _nasdaq_as_of(text: str) -> date | None:
    text = text.strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%a, %b %d, %Y").date()
    except ValueError:
        return None
