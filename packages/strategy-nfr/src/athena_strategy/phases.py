"""News Front-Running phase and tactic. One strategy, six tactics."""

from __future__ import annotations

import logging
from datetime import date

import psycopg

from athena_exec.halt import active_reason
from athena_settings.model import SettingsPayload
from athena_strategy.drafts import has_open_draft, propose
from athena_strategy.iv import IV_OWNED, tags_from_iv
from athena_strategy.mock import mock_input
from athena_strategy.news import NEWS_OWNED, tags_from_events
from athena_strategy.price import PRICE_OWNED, tags_from_prices

log = logging.getLogger("athena.strategy")

OPEN_TACTICS = {"OPEN_STRANGLE", "OPEN_CSP", "OPEN_CC", "CLOSE", "ROLL"}


def choose_phase(current: str, tags: set[str], event_offset: int | None, settings: SettingsPayload | None = None) -> str:
    """Move one step on the NFR cycle. Days are counted from the event date."""
    phase = current or "P1"
    windows = settings.strategy if settings is not None else None
    p1_from = -7 if windows is None else windows.p1_from
    p1_to = -3 if windows is None else windows.p1_to
    p2_from = -3 if windows is None else windows.p2_from
    p2_to = -1 if windows is None else windows.p2_to
    p4_from = 1 if windows is None else windows.p4_from
    p4_to = 3 if windows is None else windows.p4_to
    p5_from = 5 if windows is None else windows.p5_from
    p5_to = 10 if windows is None else windows.p5_to
    if event_offset is not None:
        if p2_from <= event_offset <= p2_to and "NEWS_WASH" in tags and "HIGH_IV" in tags:
            return "P2"
        if event_offset == 0:
            return "P3"
        if p4_from <= event_offset <= p4_to and "LOW_IV" in tags:
            return "P4"
        if p5_from <= event_offset <= p5_to and phase in {"P4", "P5"} and (
            "OVERBOUGHT" in tags or "RESISTANCE_TOUCH" in tags
        ):
            return "P5"
        if p1_from <= event_offset <= p1_to:
            return "P1"
        if event_offset < p1_from or event_offset > p5_to:
            return "P1"
        return phase
    if phase == "P2" and "NEWS_WASH" not in tags:
        return "P1"
    if "NEWS_WASH" in tags and "HIGH_IV" in tags:
        return "P2"
    if phase == "P3" and "LOW_IV" in tags:
        return "P4"
    if phase == "P4" and ("OVERBOUGHT" in tags or "RESISTANCE_TOUCH" in tags):
        return "P5"
    if phase == "P5" and not tags:
        return "P1"
    return phase


def choose_tactic(phase: str, tags: set[str], has_shares: bool, has_short: bool = False) -> str:
    """Pick the tactic for this phase. HOLD when there is no edge."""
    if phase == "P2" and "HIGH_IV" in tags and "NEWS_WASH" in tags:
        return "OPEN_STRANGLE"
    if phase == "P4" and "LOW_IV" in tags and has_short:
        return "CLOSE"
    if phase in {"P4", "P5"} and "OVERSOLD" in tags and "SUPPORT_TOUCH" in tags:
        return "OPEN_CSP"
    if phase == "P5" and "OVERBOUGHT" in tags and has_shares:
        return "OPEN_CC"
    return "HOLD"


def evaluate_watchlist(conn: psycopg.Connection, settings: SettingsPayload, account_id: str) -> list[str]:
    """Store phase and tactic for each name on this account. Return ids of new drafts."""
    if active_reason(conn, account_id):
        return []
    created: list[str] = []
    for symbol in settings.watchlist:
        draft_id = evaluate_symbol(conn, settings, symbol, account_id=account_id)
        if draft_id:
            created.append(draft_id)
    return created


def evaluate_symbol(
    conn: psycopg.Connection,
    settings: SettingsPayload,
    symbol: str,
    account_id: str,
    tags: set[str] | None = None,
    event_offset: int | None = None,
    has_shares: bool = False,
) -> str | None:
    """Persist phase plus tactic for one account. Draft only an actionable tactic."""
    if active_reason(conn, account_id):
        return None
    current, stored = _load(conn, account_id, symbol)
    mocked = mock_input(conn, symbol) if tags is None else None
    no_event = False
    if mocked is not None:
        active_tags, event_offset = mocked
        has_shares = _long_shares(conn, account_id, symbol)
    elif tags is None:
        active_tags = (
            (stored - NEWS_OWNED - IV_OWNED - PRICE_OWNED)
            | tags_from_events(conn, symbol, settings)
            | tags_from_iv(conn, symbol, settings)
            | tags_from_prices(conn, symbol, settings)
        )
        has_shares = _long_shares(conn, account_id, symbol)
        if event_offset is None:
            event_offset = calendar_offset(conn, symbol, date.today(), settings)
            no_event = event_offset is None
    else:
        active_tags = set(tags)
    has_short = _has_short(conn, account_id, symbol)
    if no_event:
        phase, tactic = "P1", "HOLD"
        log.info("phase idle symbol=%s no event in the window", symbol)
    else:
        phase = choose_phase(current, active_tags, event_offset, settings)
        tactic = choose_tactic(phase, active_tags, has_shares, has_short)
    if event_offset is not None:
        log.info("phase offset symbol=%s days=%s", symbol, event_offset)
    tag_text = ",".join(sorted(active_tags))
    conn.execute(
        """
        INSERT INTO symbol_phases (account_id, symbol, phase, tags, tactic)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (account_id, symbol) DO UPDATE
        SET phase = EXCLUDED.phase, tags = EXCLUDED.tags, tactic = EXCLUDED.tactic, updated_at = now()
        """,
        (account_id, symbol, phase, tag_text, tactic),
    )
    conn.commit()
    log.info("phase account=%s symbol=%s phase=%s tactic=%s", account_id, symbol, phase, tactic)
    if tactic not in OPEN_TACTICS:
        return None
    if has_open_draft(conn, account_id, symbol, tactic):
        return None
    if tactic == "OPEN_STRANGLE":
        option = _strangle_plan(conn, symbol)
        if option is None:
            log.info("strangle draft waits for a priced plan symbol=%s", symbol)
            return None
        expiry, put_strike, call_strike, put_limit, call_limit, trading_class, option_exchange, multiplier = option
        credit = put_limit + call_limit
        return propose(
            conn,
            settings,
            symbol,
            tactic,
            1,
            f"{phase} {tactic}. Sell {put_strike} put and {call_strike} call.",
            phase,
            account_id,
            instrument="OPT",
            limit_price=credit,
            expiry=expiry,
            put_strike=put_strike,
            call_strike=call_strike,
            put_limit=put_limit,
            call_limit=call_limit,
            trading_class=trading_class,
            option_exchange=option_exchange,
            multiplier=multiplier,
        )
    if tactic == "OPEN_CSP":
        leg = _put_leg(conn, symbol)
        if leg is None:
            log.info("csp draft waits for a priced put symbol=%s", symbol)
            return None
        expiry, strike, limit_price, trading_class, option_exchange, multiplier = leg
        return propose(
            conn,
            settings,
            symbol,
            tactic,
            1,
            f"{phase} {tactic}. Sell {strike} put.",
            phase,
            account_id,
            instrument="OPT",
            limit_price=limit_price,
            expiry=expiry,
            put_strike=strike,
            put_limit=limit_price,
            trading_class=trading_class,
            option_exchange=option_exchange,
            multiplier=multiplier,
        )
    if tactic == "OPEN_CC":
        leg = _call_leg(conn, symbol)
        if leg is None:
            log.info("cc draft waits for a priced call symbol=%s", symbol)
            return None
        expiry, strike, limit_price, trading_class, option_exchange, multiplier = leg
        return propose(
            conn,
            settings,
            symbol,
            tactic,
            1,
            f"{phase} {tactic}. Sell {strike} call.",
            phase,
            account_id,
            instrument="OPT",
            limit_price=limit_price,
            expiry=expiry,
            call_strike=strike,
            call_limit=limit_price,
            trading_class=trading_class,
            option_exchange=option_exchange,
            multiplier=multiplier,
        )
    if tactic == "CLOSE" and _working_sells(conn, account_id, symbol):
        return propose(
            conn,
            settings,
            symbol,
            tactic,
            1,
            f"{phase} {tactic}. Cancel the working sells. Nothing has filled.",
            phase,
            account_id,
            instrument="OPT",
            limit_price=1,
            order_action="CANCEL",
        )
    if tactic in {"CLOSE", "ROLL"}:
        log.info("manage owns tactic symbol=%s tactic=%s", symbol, tactic)
        return None
    return propose(conn, settings, symbol, tactic, 1, f"{phase} {tactic}.", phase, account_id)


def calendar_offset(
    conn: psycopg.Connection,
    symbol: str,
    today: date,
    settings: SettingsPayload,
) -> int | None:
    """Days from today to the nearest Fed or earnings date. None outside the cycle."""
    rows = conn.execute(
        """
        SELECT event_date FROM calendar_marks
        WHERE (kind = 'EARNINGS' AND symbol = %s) OR (kind = 'FED' AND symbol = '')
        """,
        (symbol,),
    ).fetchall()
    if not rows:
        return None
    windows = settings.strategy
    offsets = [(today - row[0]).days for row in rows]
    nearest = min(offsets, key=lambda days: (abs(days), days))
    if nearest < windows.p1_from or nearest > windows.p5_to:
        return None
    return nearest


def _has_short(conn: psycopg.Connection, account_id: str, symbol: str) -> bool:
    if _working_sells(conn, account_id, symbol):
        return True
    row = conn.execute(
        """
        SELECT quantity FROM positions
        WHERE account_id = %s AND symbol = %s AND sec_type = 'OPT'
        ORDER BY snapshot_at DESC, id DESC
        LIMIT 1
        """,
        (account_id, symbol),
    ).fetchone()
    return row is not None and row[0] < 0


def _working_sells(conn: psycopg.Connection, account_id: str, symbol: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM orders
        WHERE account_id = %s AND symbol = %s AND status = 'submitted' AND action = 'SELL'
        LIMIT 1
        """,
        (account_id, symbol),
    ).fetchone()
    return row is not None


def _put_leg(conn: psycopg.Connection, symbol: str):
    return conn.execute(
        """
        SELECT expiry, put_strike, put_limit, trading_class, option_exchange, multiplier
        FROM option_plans
        WHERE symbol = %s AND put_limit > 0
        """,
        (symbol,),
    ).fetchone()


def _call_leg(conn: psycopg.Connection, symbol: str):
    return conn.execute(
        """
        SELECT expiry, call_strike, call_limit, trading_class, option_exchange, multiplier
        FROM option_plans
        WHERE symbol = %s AND call_limit > 0
        """,
        (symbol,),
    ).fetchone()


def _long_shares(conn: psycopg.Connection, account_id: str, symbol: str) -> bool:
    row = conn.execute(
        """
        SELECT quantity FROM positions
        WHERE account_id = %s AND symbol = %s AND sec_type = 'STK'
        ORDER BY snapshot_at DESC, id DESC
        LIMIT 1
        """,
        (account_id, symbol),
    ).fetchone()
    return row is not None and row[0] > 0


def _strangle_plan(conn: psycopg.Connection, symbol: str):
    row = conn.execute(
        """
        SELECT expiry, put_strike, call_strike, put_limit, call_limit, trading_class, option_exchange, multiplier
        FROM option_plans
        WHERE symbol = %s AND put_limit > 0 AND call_limit > 0
        """,
        (symbol,),
    ).fetchone()
    return row


def _load(conn: psycopg.Connection, account_id: str, symbol: str) -> tuple[str, set[str]]:
    row = conn.execute(
        "SELECT phase, tags FROM symbol_phases WHERE account_id = %s AND symbol = %s",
        (account_id, symbol),
    ).fetchone()
    if row is None:
        return "P1", set()
    tags = {part for part in row[1].split(",") if part}
    return row[0], tags
