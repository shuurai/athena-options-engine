"""Headline intake. Tags come from stored events, not from a single post."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import re
import socket
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import psycopg

from athena_settings.model import SettingsPayload
from athena_settings.resolve import deepseek_token, scoring_model
from athena_strategy.dsh import enrich_sentiment, read_article
from athena_strategy.types import NEWS_TYPE_CODES

log = logging.getLogger("athena.news")

NEWS_OWNED = {"NEWS_WASH", "FED_EVENT", "EARNINGS_NEAR"}
ET = ZoneInfo("America/New_York")
_UA = "Mozilla/5.0 (compatible; AthenaNews/0.1)"
_PAGE_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
_PAGE_LIMIT = 1_500_000
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
    fresh: list[tuple[str, str, str]] = []
    news = settings.news
    if news.yahoo:
        for symbol in symbols:
            count, rows = _save_headlines(conn, symbol, "yahoo", _yahoo(symbol))
            stored += count
            fresh.extend(rows)
    if news.finviz:
        for symbol in symbols:
            count, rows = _save_headlines(conn, symbol, "finviz", _finviz(symbol))
            stored += count
            fresh.extend(rows)
    if news.fed_calendar:
        _save_marks(conn, "FED", "", _fed_meetings())
    for symbol, event_day, title in _earnings(symbols, today, news.earnings_near_days):
        _save_marks(conn, "EARNINGS", symbol, [(event_day, title)])
    conn.commit()
    updated = enrich_sentiment(
        conn, fresh, scoring_model(settings), settings.ai.provider, deepseek_token(settings)
    )
    if updated:
        log.info("dsh sentiment updated=%s", updated)
    for symbol in symbols:
        tags = tags_from_events(conn, symbol, settings, today)
        if tags:
            log.info("news_tags symbol=%s tags=%s", symbol, ",".join(sorted(tags)))
    return stored


def dismiss_event(conn: psycopg.Connection, fingerprint: str) -> bool:
    """A false alarm stays stored and no longer counts toward wash density."""
    row = conn.execute(
        """
        UPDATE news_events
        SET dismissed = true
        WHERE fingerprint = %s AND dismissed = false
        RETURNING fingerprint
        """,
        (fingerprint,),
    ).fetchone()
    conn.commit()
    return row is not None


def store_manual(conn: psycopg.Connection, symbol: str, title: str) -> int:
    """Store one pasted headline. The next phase pass can tag it."""
    count, _fresh = _save_headlines(conn, symbol.upper(), "manual", [(datetime.now(UTC), title, "")])
    conn.commit()
    return count


def ingest_url(
    conn: psycopg.Connection,
    url: str,
    symbols: list[str],
    model: str,
    provider: str = "DSH",
    token: str = "",
) -> str:
    """Pull a public page and store the event DSH reads from it."""
    watched = [item.strip().upper() for item in symbols if item.strip()]
    if not watched:
        raise ValueError("Add a watched symbol before ingesting a page.")
    target = public_url(url)
    html = _fetch_public(target)
    title, text = page_text(html)
    if len(text) < 40:
        raise ValueError("That page had no article text.")
    article = read_article(title, text, target, watched, model, provider, token)
    symbol = article["symbol"]
    if symbol not in set(watched):
        raise ValueError("That page is not about a watched symbol.")
    event_type = article["event_type"] if article["event_type"] in NEWS_TYPE_CODES else "GENERIC"
    headline = article["title"] or title
    if not headline:
        raise ValueError("The model did not return a headline.")
    count = _insert_event(conn, symbol, "manual", event_type, article["sentiment"], headline[:240], target)
    conn.commit()
    if not count:
        return f"{symbol} already has that headline."
    log.info("manual ingest symbol=%s", symbol)
    return f"Stored {symbol}. {headline[:120]}"


def public_url(raw: str) -> str:
    """An http(s) page on a public host. Other targets are refused."""
    parsed = urlparse(raw.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Use an http or https URL.")
    if parsed.username or parsed.password:
        raise ValueError("The URL cannot include a login.")
    if parsed.port not in {None, 80, 443}:
        raise ValueError("Use a normal web page URL.")
    host = parsed.hostname
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not _public_ip(literal):
        raise ValueError("That address is not a public page.")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError("That host did not resolve.") from exc
    addresses = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not addresses or any(not _public_ip(ip) for ip in addresses):
        raise ValueError("That address is not a public page.")
    return parsed.geturl()


def page_text(html: str) -> tuple[str, str]:
    """Title and visible article text. Scripts and styles are dropped."""
    without = re.sub(
        r"<(script|style|noscript|svg)\b[^>]*>.*?</\1>",
        " ",
        html,
        flags=re.I | re.S,
    )
    title = _meta_title(without)
    parts = [
        _plain(part)
        for part in re.findall(r"<p\b[^>]*>(.*?)</p>", without, flags=re.I | re.S)
    ]
    text = " ".join(part for part in parts if part)
    if len(text) < 80:
        text = _plain(without)
    return title[:240], text[:6000]


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
        WHERE symbol = %s AND sentiment < %s AND ts >= %s AND dismissed = false
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
    local = now.astimezone(ET)
    label, _, clock_text = text.partition(" ")
    if label.lower() == "today" and clock_text:
        clock = datetime.strptime(clock_text, "%I:%M%p")
        day = local.date()
        return datetime(day.year, day.month, day.day, clock.hour, clock.minute, tzinfo=ET)
    if label.lower() == "yesterday" and clock_text:
        clock = datetime.strptime(clock_text, "%I:%M%p")
        day = local.date() - timedelta(days=1)
        return datetime(day.year, day.month, day.day, clock.hour, clock.minute, tzinfo=ET)
    if re.fullmatch(r"\d{1,2}:\d{2}[AP]M", text):
        clock = datetime.strptime(text, "%I:%M%p")
        day = previous.date() if previous else local.date()
        return datetime(day.year, day.month, day.day, clock.hour, clock.minute, tzinfo=ET)
    try:
        parsed = datetime.strptime(text, "%b-%d-%y %I:%M%p")
    except ValueError:
        log.info("finviz stamp skipped text=%s", text)
        return local
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


class _NoRedirect(HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        raise HTTPError(req.full_url, code, msg, headers, fp)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


_OPENER = build_opener(_NoRedirect)


def _public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(ip.is_global)


def _fetch_public(url: str) -> str:
    current = url
    for _hop in range(4):
        request = Request(current, headers={"User-Agent": _PAGE_UA, "Accept": "text/html,application/xhtml+xml"})
        try:
            with _OPENER.open(request, timeout=12) as response:
                kind = (response.headers.get("Content-Type") or "").lower()
                if kind and "html" not in kind and "xml" not in kind and not kind.startswith("text/"):
                    raise ValueError("That URL is not a web page.")
                raw = _read_limited(response)
                charset = response.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, "replace")
        except HTTPError as exc:
            location = exc.headers.get("Location") if exc.code in {301, 302, 303, 307, 308} else ""
            exc.close()
            if not location:
                raise ValueError("The page did not load.") from exc
            current = public_url(urljoin(current, location))
        except ValueError:
            raise
        except (URLError, TimeoutError, OSError) as exc:
            raise ValueError("The page did not load.") from exc
    raise ValueError("The page did not load.")


def _read_limited(response) -> bytes:
    chunks = []
    total = 0
    while total < _PAGE_LIMIT:
        chunk = response.read(min(65536, _PAGE_LIMIT - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _meta_title(html: str) -> str:
    patterns = (
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
        r"<title[^>]*>(.*?)</title>",
    )
    for pattern in patterns:
        match = re.search(pattern, html, re.I | re.S)
        if not match:
            continue
        text = _plain(match.group(1))
        if text:
            return text
    return ""


def _plain(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _insert_event(
    conn: psycopg.Connection,
    symbol: str,
    source: str,
    event_type: str,
    sentiment: float,
    title: str,
    url: str,
) -> int:
    fingerprint = hashlib.sha256(f"{symbol.upper()}|{title.lower()}".encode()).hexdigest()[:24]
    result = conn.execute(
        """
        INSERT INTO news_events (fingerprint, ts, symbol, source, event_type, sentiment, title, url)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (fingerprint) DO NOTHING
        """,
        (fingerprint, datetime.now(UTC), symbol, source, event_type, sentiment, title, url),
    )
    return result.rowcount


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
) -> tuple[int, list[tuple[str, str, str]]]:
    written = 0
    fresh: list[tuple[str, str, str]] = []
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
        if result.rowcount:
            fresh.append((fingerprint, symbol, title))
    return written, fresh


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
