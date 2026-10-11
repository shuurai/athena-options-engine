"""News Front-Running phase and tactic. One strategy, six tactics."""

from __future__ import annotations

import logging
from datetime import date

import psycopg
from psycopg.types.json import Jsonb

from athena_exec.halt import active_reason
from athena_settings.model import SettingsPayload

from athena_strategy.indicators import indicators_enabled, read_indicators
from athena_strategy.iv import tags_from_iv
from athena_strategy.mock import mock_input
from athena_strategy.news import tags_from_events
from athena_strategy.price import tags_from_prices
from athena_strategy.tags import (
    OVERBOUGHT,
    PRICE_WEAK,
    RESISTANCE_TOUCH,
    VOLUME_SPIKE,
    kept_stored,
)

log = logging.getLogger("athena.strategy")


def choose_phase(current: str, tags: set[str], event_offset: int | None, settings: SettingsPayload | None = None) -> str:
    """Move one step on the NFR cycle. Days are counted from the event date."""
    phase = current or "P1"
    windows = settings.strategy if settings is not None else None
    p1_from = -7 if windows is None else windows.p1_from
    p1_to = -3 if windows is None else windows.p1_to
    p2_from = -3 if windows is None else windows.p2_from
    p2_to = -1 if windows is None else windows.p2_to
    p5_from = 5 if windows is None else windows.p5_from
    p5_to = 10 if windows is None else windows.p5_to
    if event_offset is not None:
        if p2_from <= event_offset <= p2_to and _panic(tags):
            return "P2"
        if event_offset == 0:
            return "P3"
        if p5_from <= event_offset <= p5_to and phase in {"P4", "P5"} and (
            OVERBOUGHT in tags or RESISTANCE_TOUCH in tags
        ):
            return "P5"
        if p1_from <= event_offset <= p1_to:
            return "P1"
        if event_offset < p1_from or event_offset > p5_to:
            return "P1"
        return phase
    if phase == "P2" and not _panic(tags):
        return "P1"
    if _panic(tags):
        return "P2"
    if phase == "P4" and (OVERBOUGHT in tags or RESISTANCE_TOUCH in tags):
        return "P5"
    if phase == "P5" and not tags:
        return "P1"
    return phase


def _panic(tags: set[str]) -> bool:
    """A weak price plus a volume spike. The premium-ratio proxy is not a gate."""
    return PRICE_WEAK in tags and VOLUME_SPIKE in tags


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
    """Store the phase and the position state. This function does not open a trade."""
    if active_reason(conn, account_id):
        return None
    if symbol.upper() in {item.upper() for item in settings.blocked_symbols}:
        return None
    current, stored = _load(conn, account_id, symbol)
    mocked = mock_input(conn, symbol) if tags is None else None
    reading = None
    no_event = False
    if mocked is not None:
        active_tags, event_offset = mocked
    elif tags is None:
        from athena_strategy.simbars import replay_cursor

        as_of = replay_cursor(conn, symbol) or date.today()
        active_tags = (
            kept_stored(stored)
            | tags_from_events(conn, symbol, settings, as_of)
            | tags_from_iv(conn, symbol, settings)
            | tags_from_prices(conn, symbol, settings)
        )
        if indicators_enabled():
            reading = read_indicators(conn, settings, symbol)
            active_tags |= reading.tags
        if event_offset is None:
            event_offset = calendar_offset(conn, symbol, as_of, settings)
            no_event = event_offset is None
    else:
        active_tags = set(tags)
    del has_shares
    if no_event:
        phase = "P1"
        log.info("phase idle symbol=%s no event in the window", symbol)
    else:
        phase = choose_phase(current, active_tags, event_offset, settings)
    tactic = "HOLD"
    if event_offset is not None:
        log.info("phase offset symbol=%s days=%s", symbol, event_offset)
    tag_text = ",".join(sorted(active_tags))
    from athena_strategy.wheel import alias_of, leg_text, wheel_state

    state = wheel_state(conn, account_id, symbol)
    state_name = alias_of(state)
    counts = leg_text(state)
    if reading is None:
        conn.execute(
            """
            INSERT INTO symbol_phases (account_id, symbol, phase, tags, tactic, state, leg_counts)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (account_id, symbol) DO UPDATE
            SET phase = EXCLUDED.phase, tags = EXCLUDED.tags, tactic = EXCLUDED.tactic,
                state = EXCLUDED.state, leg_counts = EXCLUDED.leg_counts, updated_at = now()
            """,
            (account_id, symbol, phase, tag_text, tactic, state_name, counts),
        )
    else:
        conn.execute(
            """
            INSERT INTO symbol_phases (
                account_id, symbol, phase, tags, tactic, indicators, state, leg_counts
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (account_id, symbol) DO UPDATE
            SET phase = EXCLUDED.phase, tags = EXCLUDED.tags, tactic = EXCLUDED.tactic,
                indicators = EXCLUDED.indicators, state = EXCLUDED.state,
                leg_counts = EXCLUDED.leg_counts, updated_at = now()
            """,
            (account_id, symbol, phase, tag_text, tactic, Jsonb(reading.payload()), state_name, counts),
        )
    conn.commit()
    log.info("phase account=%s symbol=%s phase=%s state=%s", account_id, symbol, phase, state_name)
    return None


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


def _load(conn: psycopg.Connection, account_id: str, symbol: str) -> tuple[str, set[str]]:
    row = conn.execute(
        "SELECT phase, tags FROM symbol_phases WHERE account_id = %s AND symbol = %s",
        (account_id, symbol),
    ).fetchone()
    if row is None:
        return "P1", set()
    tags = {part for part in row[1].split(",") if part}
    return row[0], tags
