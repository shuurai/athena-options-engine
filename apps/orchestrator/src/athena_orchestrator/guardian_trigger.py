"""R5 Guardian static trigger lines (booklet 14 section 19; D84-D90).

Layering mirrors R2 / R4:

* pure:      ``level_text``, ``line_key``, ``crossed``, ``trigger_plan``
* thin SQL:  ``load_lines``, ``load_prices``, ``load_trigger_rows``,
             ``apply_trigger_plan``
* one tick:  ``evaluate_tick`` -- called from R4's fixed 300s ``nag_tick`` (D87)

Reads only ``settings_versions`` and ``market_bars`` (through
``last_tradeable_price``, D94 / D88); writes only ``guardian_alerts`` rows with
``source='trigger'``. No migration, no new feed, no second channel (D84 / D91).

Lifecycle (D88, state judgement, not edge detection): one line has one stable
``breach`` key. A crossed line opens one red row; a row in a non-rearmed state
(open / skipped / resolved) blocks another insert, so an acknowledged line does
not re-nag while the price stays crossed. When the price returns inside the
line, R5 itself auto-clears those rows -- that clear is also the re-arm, so the
line can fire again on the next crossing. A line removed from Settings is
auto-cleared the same way. ``reason`` records the crossing price and moment.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Mapping, Sequence

from athena_settings.model import SettingsPayload, TriggerLine

__all__ = [
    "TRIGGER_SOURCE",
    "TRIGGER_TIER",
    "TriggerRow",
    "OpenLine",
    "TriggerPlan",
    "level_text",
    "line_key",
    "crossed",
    "trigger_plan",
    "load_lines",
    "load_prices",
    "load_trigger_rows",
    "apply_trigger_plan",
    "evaluate_tick",
]

log = logging.getLogger("athena")

TRIGGER_SOURCE = "trigger"
TRIGGER_TIER = "danger"          # D89: red => _DUE_SQL nags it, zero sender change
_REARMED = "auto_cleared"


@dataclass(frozen=True)
class TriggerRow:
    id: int
    account_id: str
    breach: str
    state: str
    skip_until: datetime | None = None


@dataclass(frozen=True)
class OpenLine:
    account_id: str
    symbol: str
    breach: str
    reason: str


@dataclass(frozen=True)
class TriggerPlan:
    opens: tuple[OpenLine, ...] = ()
    clears: tuple[tuple[int, str, str], ...] = ()   # (id, account, reason)
    reopens: tuple[tuple[int, str], ...] = ()       # (id, account)

    def empty(self) -> bool:
        return not (self.opens or self.clears or self.reopens)


# ---------------------------------------------------------------------------
# Pure core (zero I/O)
# ---------------------------------------------------------------------------


def level_text(level: float) -> str:
    """Stable decimal text for both the breach key and the reason line."""
    return format(Decimal(str(level)).normalize(), "f")


def line_key(line: TriggerLine) -> str:
    """D83: one stable key per (symbol, op, level); uniqueness lives in state."""
    return f"trigger:{line.symbol}:{line.op}:{level_text(line.level)}"


def crossed(line: TriggerLine, price: Decimal | None) -> bool | None:
    """None = no price (D68/D73 honest gap), never a fabricated crossing."""
    if price is None:
        return None
    level = Decimal(str(line.level))
    return price < level if line.op == "<" else price > level


def trigger_plan(
    lines: Mapping[str, Sequence[TriggerLine]],
    prices: Mapping[str, Decimal | None],
    rows: Sequence[TriggerRow],
    now: datetime,
) -> TriggerPlan:
    """Pure. Compare current snapshots against configured lines and live rows."""
    configured: dict[tuple[str, str], tuple[TriggerLine, Decimal | None, bool | None]] = {}
    for account, items in lines.items():
        for line in items:
            price = prices.get(line.symbol)
            configured[(account, line_key(line))] = (line, price, crossed(line, price))

    stamp = now.isoformat()
    opens: list[OpenLine] = []
    clears: list[tuple[int, str, str]] = []
    reopens: list[tuple[int, str]] = []
    seen: set[tuple[str, str]] = set()

    for row in rows:
        key = (row.account_id, row.breach)
        seen.add(key)
        entry = configured.get(key)
        if entry is None:
            # D90: the operator removed or changed the line. Re-arm its rows.
            clears.append((row.id, row.account_id, f"trigger line removed or reconfigured at {stamp}"))
            continue
        line, price, is_crossed = entry
        if is_crossed is None:
            continue                                   # hold: no price, never invent
        if not is_crossed:
            clears.append((
                row.id, row.account_id,
                f"back inside {line.symbol} {line.op} {level_text(line.level)} "
                f"at {price} at {stamp}",
            ))
            continue
        # Still crossed. A resolved row stays put (no re-add), an expired
        # 24h Skip comes back so the 5-minute nag is not lost forever.
        if row.state == "skipped" and (row.skip_until is None or row.skip_until <= now):
            reopens.append((row.id, row.account_id))

    for key, (line, price, is_crossed) in configured.items():
        if key in seen or is_crossed is not True:
            continue
        opens.append(OpenLine(
            account_id=key[0], symbol=line.symbol, breach=key[1],
            reason=(f"crossed {line.symbol} {line.op} {level_text(line.level)} "
                    f"at {price} at {stamp}"),
        ))
    opens.sort(key=lambda item: (item.account_id, item.breach))
    return TriggerPlan(tuple(opens), tuple(clears), tuple(reopens))


# ---------------------------------------------------------------------------
# Thin SQL
# ---------------------------------------------------------------------------

_LINES_SQL = """
SELECT account_id, payload
FROM settings_versions
WHERE active AND account_id = ANY(%s)
"""

_ROWS_SQL = """
SELECT id, account_id, breach, state, skip_until
FROM guardian_alerts
WHERE source = 'trigger' AND state <> 'auto_cleared'
  AND account_id = ANY(%s)
ORDER BY id
"""

# D83: the partial unique index covers live rows; NOT EXISTS spans every
# non-rearmed row so a resolved line is not re-added while still crossed.
# ON CONFLICT DO NOTHING absorbs a concurrent tick. No in-memory dedup table.
_INSERT_SQL = """
INSERT INTO guardian_alerts
    (account_id, symbol, breach, tier, state, source, reason, created_at, updated_at)
SELECT %s, %s, %s, 'danger', 'open', 'trigger', %s, %s, %s
WHERE NOT EXISTS (
    SELECT 1 FROM guardian_alerts
     WHERE account_id = %s AND breach = %s AND source = 'trigger'
       AND state <> 'auto_cleared'
)
ON CONFLICT DO NOTHING
"""

_CLEAR_SQL = """
UPDATE guardian_alerts
   SET state = 'auto_cleared', reason = %s, skip_until = NULL, updated_at = %s
 WHERE id = %s AND account_id = %s AND source = 'trigger' AND state <> 'auto_cleared'
"""

_REOPEN_SQL = """
UPDATE guardian_alerts
   SET state = 'open', skip_until = NULL, updated_at = %s
 WHERE id = %s AND account_id = %s AND source = 'trigger' AND state = 'skipped'
"""


def load_lines(conn, accounts: Sequence[str]) -> dict[str, list[TriggerLine]]:
    """Read the declared trigger_lines field from each active settings row."""
    out: dict[str, list[TriggerLine]] = {str(account): [] for account in accounts}
    if not out:
        return out
    found = conn.execute(_LINES_SQL, (list(out),)).fetchall()
    for account, raw in found:
        try:
            data = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
            payload = SettingsPayload.model_validate(data)
        except Exception:
            log.warning("guardian trigger: unreadable settings payload for %s", account)
            continue
        out[str(account)] = list(payload.trigger_lines)
    return out


def load_prices(conn, lines: Mapping[str, Sequence[TriggerLine]]) -> dict[str, Decimal | None]:
    """D94: last_tradeable_price() is the only price reader, unchanged."""
    from athena_marketdata.pricing import last_tradeable_price

    symbols = sorted({line.symbol.upper() for items in lines.values() for line in items})
    return {symbol: last_tradeable_price(conn, symbol) for symbol in symbols}


def load_trigger_rows(conn, accounts: Sequence[str]) -> list[TriggerRow]:
    found = conn.execute(_ROWS_SQL, (list(accounts),)).fetchall()
    return [
        TriggerRow(int(r[0]), str(r[1]), str(r[2]), str(r[3]), r[4])
        for r in found
    ]


def apply_trigger_plan(conn, plan: TriggerPlan, now: datetime) -> None:
    """Parameterised, account-scoped, source-scoped. No DDL of any kind."""
    for item in plan.opens:
        conn.execute(
            _INSERT_SQL,
            (
                item.account_id, item.symbol, item.breach, item.reason, now, now,
                item.account_id, item.breach,
            ),
        )
    for alert_id, account, reason in plan.clears:
        conn.execute(_CLEAR_SQL, (reason, now, alert_id, account))
    for alert_id, account in plan.reopens:
        conn.execute(_REOPEN_SQL, (now, alert_id, account))


# ---------------------------------------------------------------------------
# One tick (connection injected so unit tests use a fake)
# ---------------------------------------------------------------------------


def evaluate_tick(conn, now: datetime, accounts: Sequence[str]) -> TriggerPlan:
    """One R5 evaluation on R4's fixed 300s tick. No sending, no sleeping."""
    names = [str(account) for account in accounts if account]
    if not names:
        return TriggerPlan()
    lines = load_lines(conn, names)
    prices = load_prices(conn, lines)
    rows = load_trigger_rows(conn, names)
    plan = trigger_plan(lines, prices, rows, now)
    apply_trigger_plan(conn, plan, now)
    return plan
