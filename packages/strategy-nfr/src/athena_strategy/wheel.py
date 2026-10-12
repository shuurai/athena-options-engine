"""One position-derived state and at most one opening ticket per symbol per day."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, ROUND_CEILING

from athena_exec.halt import active_reason
from athena_settings.model import SettingsPayload, TacticFlags
from athena_strategy.drafts import (
    _symbol_in_replay,
    apply_ticket_block,
    last_tradeable_price,
    open_quantity,
    stage_draft,
)
from athena_strategy.indicators import Indicators, read_indicators
from athena_strategy.screener import ScreenerLine, candidates
from athena_strategy.tags import (
    DRAWDOWN_OK,
    EARNINGS_NEAR,
    NEWS_WASH,
    RECLAIM,
    RV_HIGH,
    TREND_STRONG,
    UPPER_RAIL,
)

_OPENING_REASONS = ("dip_long", "dip_csp", "high_cc", "cover_gap", "signaled_strangle")
_OPENING_RANK = {
    "signaled_strangle": 0,
    "high_cc": 1,
    "cover_gap": 2,
    "dip_long": 3,
    "dip_csp": 3,
}
_FROZEN_ORDERS = {"queued", "submitted", "filled"}
_ACTIONS = ("BUY", "OPEN_CSP", "OPEN_CC", "OPEN_STRANGLE", "CLOSE", "ROLL")

# None means the action is legal. A string is the refusal code.
_LONG_ROW = {
    "BUY": None,
    "OPEN_CSP": None,
    "OPEN_CC": None,
    "OPEN_STRANGLE": None,
    "CLOSE": "nothing_to_close",
    "ROLL": "nothing_to_close",
}
_TRANSITIONS = {
    "FLAT": {
        "BUY": None,
        "OPEN_CSP": None,
        "OPEN_CC": "cc_needs_shares",
        "OPEN_STRANGLE": None,
        "CLOSE": "nothing_to_close",
        "ROLL": "nothing_to_close",
    },
    "ODD_LOT": {**_LONG_ROW, "OPEN_CC": "cc_needs_shares"},
    "LONG": dict(_LONG_ROW),
    "SHORT_PUT": {
        "BUY": None,
        "OPEN_CSP": "duplicate_short_put",
        "OPEN_CC": "cc_needs_shares",
        "OPEN_STRANGLE": "leg_already_open",
        "CLOSE": None,
        "ROLL": None,
    },
    "ODD_PUT": {
        "BUY": None,
        "OPEN_CSP": "duplicate_short_put",
        "OPEN_CC": "cc_needs_shares",
        "OPEN_STRANGLE": "leg_already_open",
        "CLOSE": None,
        "ROLL": None,
    },
    "ASSIGNED": {
        "BUY": None,
        "OPEN_CSP": "duplicate_short_put",
        "OPEN_CC": None,
        "OPEN_STRANGLE": "leg_already_open",
        "CLOSE": None,
        "ROLL": None,
    },
    "LONG_CALL": {
        "BUY": None,
        "OPEN_CSP": None,
        "OPEN_CC": "duplicate_short_call",
        "OPEN_STRANGLE": "leg_already_open",
        "CLOSE": None,
        "ROLL": None,
    },
    "WHEELING": {
        "BUY": None,
        "OPEN_CSP": "duplicate_short_put",
        "OPEN_CC": "duplicate_short_call",
        "OPEN_STRANGLE": "leg_already_open",
        "CLOSE": None,
        "ROLL": None,
    },
    "STRANGLE": {
        "BUY": None,
        "OPEN_CSP": "leg_already_open",
        "OPEN_CC": "cc_needs_shares",
        "OPEN_STRANGLE": "leg_already_open",
        "CLOSE": None,
        "ROLL": None,
    },
    "NAKED_CALL": {
        "BUY": None,
        "OPEN_CSP": "already_naked",
        "OPEN_CC": "already_naked",
        "OPEN_STRANGLE": "already_naked",
        "CLOSE": None,
        "ROLL": None,
    },
    "MULTI_CONFLICT": {
        "BUY": "manual_review",
        "OPEN_CSP": "manual_review",
        "OPEN_CC": "manual_review",
        "OPEN_STRANGLE": "manual_review",
        "CLOSE": None,
        "ROLL": "manual_review",
    },
}


def state_machine_enabled() -> bool:
    """The wheel is the only opening path. The environment flag cannot turn it off."""
    return True


@dataclass(frozen=True)
class WheelState:
    symbol: str
    shares: Decimal
    short_puts: int
    short_calls: int
    short_put_strikes: tuple[Decimal, ...]
    short_call_strikes: tuple[Decimal, ...]
    cost_basis: Decimal | None
    block: str | None = None


@dataclass(frozen=True)
class LegPlan:
    tactic: str
    contracts: int
    limit_price: Decimal | None = None
    expiry: date | None = None
    put_strike: Decimal | None = None
    call_strike: Decimal | None = None
    put_limit: Decimal | None = None
    call_limit: Decimal | None = None
    trading_class: str = ""
    option_exchange: str = ""
    multiplier: str = "100"
    instrument: str = "OPT"
    enforce_contract_cap: bool = True


@dataclass(frozen=True)
class Opening:
    alias: str
    state_after: str
    reason: str
    quiet: str | None
    outcome: str
    summary: str
    legs: tuple[LegPlan, ...]
    overruled: bool
    put_lines: tuple[ScreenerLine, ...] = ()
    call_lines: tuple[ScreenerLine, ...] = ()
    replaces: str = ""


def coverable_contracts(shares: Decimal) -> int:
    if shares <= 0:
        return 0
    return int(shares // 100)


def alias_of(state: WheelState) -> str:
    """Audit name. Alarms win. Counts are the real match."""
    puts = state.short_puts
    calls = state.short_calls
    shares = state.shares
    if puts > 1 or calls > 1:
        return "MULTI_CONFLICT"
    covered = coverable_contracts(shares)
    held_strangle = shares == 0 and puts == 1 and calls == 1
    if not held_strangle and calls > covered and shares < 100:
        return "NAKED_CALL"
    if shares == 0 and puts == 0 and calls == 0:
        return "FLAT"
    if 0 < shares < 100 and puts == 0 and calls == 0:
        return "ODD_LOT"
    if shares >= 100 and puts == 0 and calls == 0:
        return "LONG"
    if shares == 0 and puts == 1 and calls == 0:
        return "SHORT_PUT"
    if 0 < shares < 100 and puts == 1 and calls == 0:
        return "ODD_PUT"
    if shares >= 100 and puts == 1 and calls == 0:
        return "ASSIGNED"
    if shares >= 100 and puts == 0 and calls == 1:
        return "LONG_CALL"
    if shares >= 100 and puts == 1 and calls == 1:
        return "WHEELING"
    if shares == 0 and puts == 1 and calls == 1:
        return "STRANGLE"
    return "MULTI_CONFLICT"


def leg_text(state: WheelState) -> str:
    shares = state.shares
    text = str(int(shares)) if shares == shares.to_integral_value() else format(shares, "f")
    return f"p{state.short_puts},c{state.short_calls},s{text}"


def refusal(name: str, action: str) -> str | None:
    """None when this alias may take the action."""
    row = _TRANSITIONS.get(name)
    if row is None or action not in row:
        return "manual_review"
    return row[action]


def wheel_state(conn, account_id: str, symbol: str) -> WheelState:
    """Latest stock and short options, plus submitted sell orders. Same rows twice match."""
    name = symbol.upper()
    simulated = _symbol_in_replay(conn, account_id, name)
    shares = Decimal(0)
    basis = None
    puts = 0
    calls = 0
    put_strikes: list[Decimal] = []
    call_strikes: list[Decimal] = []
    rows = conn.execute(
        """
        SELECT DISTINCT ON (sec_type, expiry, contract_right, strike)
            sec_type, quantity, avg_price, contract_right, strike
        FROM positions
        WHERE account_id = %s AND symbol = %s AND simulated = %s
        ORDER BY sec_type, expiry, contract_right, strike, snapshot_at DESC, id DESC
        """,
        (account_id, name, simulated),
    ).fetchall()
    for sec_type, quantity, avg_price, right, strike in rows:
        if quantity is None:
            continue
        qty = Decimal(quantity)
        if sec_type == "STK":
            if qty > 0:
                shares = qty
                if avg_price is not None and Decimal(avg_price) > 0:
                    basis = Decimal(avg_price)
            continue
        if sec_type != "OPT" or qty >= 0 or strike is None:
            continue
        count = int(abs(qty))
        struck = Decimal(strike)
        if right == "P":
            puts += count
            put_strikes.append(struck)
        elif right == "C":
            calls += count
            call_strikes.append(struck)
    orders = conn.execute(
        """
        SELECT contract_right, strike, contracts
        FROM orders
        WHERE account_id = %s AND symbol = %s AND simulated = %s
          AND status = 'submitted' AND action = 'SELL'
          AND contract_right IN ('P', 'C')
        ORDER BY strike, contract_right, id
        """,
        (account_id, name, simulated),
    ).fetchall()
    for right, strike, contracts in orders:
        count = int(contracts or 0)
        if count < 1 or strike is None:
            continue
        struck = Decimal(strike)
        if right == "P":
            puts += count
            put_strikes.append(struck)
        else:
            calls += count
            call_strikes.append(struck)
    block = "halted" if active_reason(conn, account_id) else None
    return WheelState(
        name,
        shares,
        puts,
        calls,
        tuple(sorted(put_strikes)),
        tuple(sorted(call_strikes)),
        basis,
        block,
    )


def project_alias(state: WheelState, legs: tuple[LegPlan, ...]) -> str:
    shares = state.shares
    puts = state.short_puts
    calls = state.short_calls
    for leg in legs:
        if leg.tactic == "BUY":
            shares += Decimal(leg.contracts)
        elif leg.tactic == "OPEN_CSP":
            puts += leg.contracts
        elif leg.tactic == "OPEN_CC":
            calls += leg.contracts
    return alias_of(replace(state, shares=shares, short_puts=puts, short_calls=calls, block=None))


def may_buy_enabled() -> bool:
    flag = os.environ.get("ATHENA_MAY_BUY", "on").strip().lower()
    return flag not in {"0", "off", "false", "no"}


def dry_run_enabled() -> bool:
    flag = os.environ.get("ATHENA_DRY_RUN", "off").strip().lower()
    return flag in {"1", "on", "true", "yes"}


def slot_split_enabled() -> bool:
    flag = os.environ.get("ATHENA_SLOT_SPLIT", "on").strip().lower()
    return flag not in {"0", "off", "false", "no"}


def may_buy(flags: TacticFlags) -> bool:
    """Share purchases. Income stays off unless may_buy is set on that name."""
    legacy = bool(flags.long) and flags.intent != "income"
    if not may_buy_enabled() or flags.may_buy is None:
        return legacy
    return bool(flags.may_buy)


def buy_access(flags: TacticFlags) -> tuple[bool, str]:
    """Whether this name may buy shares, and the one reason that decides it."""
    if may_buy(flags):
        return True, "allowed"
    if may_buy_enabled() and flags.may_buy is False:
        return False, "may_buy off"
    if not flags.long:
        return False, "long off"
    if flags.intent == "income":
        return False, "income"
    return False, "may_buy off"


def _dip(reading: Indicators | None, tags: set[str], settings: SettingsPayload) -> bool:
    if reading is None:
        return False
    if EARNINGS_NEAR in tags or EARNINGS_NEAR in reading.tags:
        return False
    if TREND_STRONG not in reading.tags and TREND_STRONG not in tags:
        return False
    if RECLAIM not in reading.tags and RECLAIM not in tags:
        return False
    if reading.drawdown_pct is None:
        return DRAWDOWN_OK in reading.tags or DRAWDOWN_OK in tags
    deep = Decimal(str(settings.strategy.drawdown_band_high_pct))
    shallow = Decimal(str(settings.strategy.drawdown_band_low_pct))
    return -deep <= reading.drawdown_pct <= -shallow


def _strangle_signal(reading: Indicators, tags: set[str], flags: TacticFlags) -> bool:
    """The existing strangle rule: news wash and high realized vol, and the flag is on."""
    if not flags.strangle:
        return False
    wash = NEWS_WASH in tags or NEWS_WASH in reading.tags
    rich = RV_HIGH in tags or RV_HIGH in reading.tags
    return wash and rich


def _high_rail(reading: Indicators | None, tags: set[str], settings: SettingsPayload) -> bool:
    if UPPER_RAIL in tags:
        return True
    if reading is None or reading.rail_pos is None:
        return False
    return reading.rail_pos >= Decimal(str(settings.strategy.cc_rail_pos))


def _plan_meta(conn, symbol: str) -> tuple[str, str, str]:
    row = conn.execute(
        """
        SELECT trading_class, option_exchange, multiplier
        FROM option_plans WHERE symbol = %s
        """,
        (symbol.upper(),),
    ).fetchone()
    if row is None:
        return "", "", "100"
    return row[0] or "", row[1] or "", row[2] or "100"


def _option_leg(line: ScreenerLine, meta: tuple[str, str, str], tactic: str, contracts: int) -> LegPlan:
    trading_class, exchange, multiplier = meta
    put = line.strike if line.right == "P" else None
    call = line.strike if line.right == "C" else None
    return LegPlan(
        tactic=tactic,
        contracts=contracts,
        limit_price=line.ask,
        expiry=line.expiry,
        put_strike=put,
        call_strike=call,
        put_limit=line.ask if put is not None else None,
        call_limit=line.ask if call is not None else None,
        trading_class=trading_class,
        option_exchange=exchange,
        multiplier=multiplier,
        instrument="OPT",
    )


def share_gap(shares: Decimal) -> int:
    """Shares still needed to reach one option lot. Zero when the lot is already full."""
    if shares <= 0 or shares >= 100:
        return 0
    missing = Decimal(100) - shares
    return int(missing.to_integral_value(rounding=ROUND_CEILING))


def _buy_leg(contracts: int, limit: Decimal, *, enforce_contract_cap: bool = True) -> LegPlan:
    return LegPlan(
        tactic="BUY",
        contracts=contracts,
        limit_price=limit,
        multiplier="1",
        instrument="STK",
        enforce_contract_cap=enforce_contract_cap,
    )


def decide_opening(
    conn,
    settings: SettingsPayload,
    account_id: str,
    symbol: str,
    tags: set[str] | None = None,
    narrator_strike: Decimal | None = None,
    today: date | None = None,
) -> Opening:
    """The only opening decision. One ticket, or a named quiet reason."""
    state = wheel_state(conn, account_id, symbol)
    name = alias_of(state)
    extra = set(tags or ())
    empty = Opening(name, name, "", None, "quiet", "not_a_dip", (), False)
    if state.block == "halted":
        return replace(empty, outcome="dropped", summary="halted", quiet="halted")
    if name in {"NAKED_CALL", "MULTI_CONFLICT"}:
        return replace(empty, outcome="dropped", summary=name, quiet=name)
    if _manage_pending(conn, account_id, symbol, _symbol_in_replay(conn, account_id, symbol)):
        return replace(empty, quiet="manage_pending", summary="manage_pending")
    reading = read_indicators(conn, settings, symbol)
    if reading.rail_pos is None and reading.drawdown_pct is None:
        return replace(empty, outcome="quiet", summary="no_data", quiet="no_data")
    simulated = _symbol_in_replay(conn, account_id, symbol)
    hold = _opening_hold(conn, account_id, symbol, simulated)
    if hold is not None and hold[0] == "frozen":
        return replace(empty, quiet="opening_already_today", summary="opening_already_today")
    flags = settings.tactic_flags.get(symbol.upper(), TacticFlags())
    day = today or date.today()
    spot = last_tradeable_price(conn, symbol)
    chosen = _choose(conn, settings, account_id, symbol, state, reading, extra, flags, spot, day, narrator_strike)
    if hold is None:
        return chosen
    if chosen.outcome != "drafted" or _opening_rank(chosen.reason) >= _opening_rank(hold[2]):
        return replace(empty, quiet="opening_already_today", summary="opening_already_today")
    return replace(chosen, replaces=hold[1])


def _choose(
    conn,
    settings,
    account_id,
    symbol,
    state: WheelState,
    reading: Indicators,
    tags: set[str],
    flags: TacticFlags,
    spot: Decimal | None,
    today: date,
    narrator_strike: Decimal | None,
) -> Opening:
    name = alias_of(state)
    dip = _dip(reading, tags, settings)
    earnings = EARNINGS_NEAR in tags or EARNINGS_NEAR in reading.tags
    high = _high_rail(reading, tags, settings) and not earnings
    csp_block = refusal(name, "OPEN_CSP")
    buy_block = refusal(name, "BUY")
    cc_block = refusal(name, "OPEN_CC")
    meta = _plan_meta(conn, symbol)
    opt_contracts = open_quantity(conn, settings, account_id, symbol, meta[2] or "100")
    share_count = open_quantity(conn, settings, account_id, symbol, "1")
    price = spot if spot is not None and spot > 0 else None
    puts: list[ScreenerLine] = []
    calls: list[ScreenerLine] = []
    if price is not None and opt_contracts >= 1:
        if dip and csp_block is None and flags.csp:
            puts, _note = candidates(
                conn, settings, symbol, reading, today,
                account_id=account_id, purpose="csp", spot=price,
            )
        if high and cc_block is None and flags.cc:
            calls, _note = candidates(
                conn, settings, symbol, reading, today,
                account_id=account_id, purpose="cc", spot=price,
            )
    rail = _high_rail(reading, tags, settings)
    cc_legs: tuple[LegPlan, ...] = ()
    cc_miss = _cc_miss(
        state, flags, high, rail, earnings, cc_block, calls, opt_contracts, price,
    )
    if (
        high
        and cc_block is None
        and flags.cc
        and calls
        and opt_contracts >= 1
        and state.shares >= Decimal(min(opt_contracts, coverable_contracts(state.shares)) * 100)
    ):
        count = min(opt_contracts, coverable_contracts(state.shares))
        if count >= 1 and (state.cost_basis is None or calls[0].strike >= state.cost_basis):
            cc_legs = (_option_leg(calls[0], meta, "OPEN_CC", count),)
            cc_miss = None
    strangle_legs: tuple[LegPlan, ...] = ()
    strangle_puts: list[ScreenerLine] = []
    strangle_calls: list[ScreenerLine] = []
    strangle_on = (
        price is not None
        and opt_contracts >= 1
        and state.shares >= 100
        and _strangle_signal(reading, tags, flags)
        and not earnings
        and csp_block is None
        and cc_block is None
        and flags.csp
        and flags.cc
    )
    if strangle_on:
        strangle_puts, _note = candidates(
            conn, settings, symbol, reading, today,
            account_id=account_id, purpose="strangle_put", spot=price,
        )
        strangle_calls, _note = candidates(
            conn, settings, symbol, reading, today,
            account_id=account_id, purpose="strangle_call", spot=price,
        )
        call_line = next(
            (line for line in strangle_calls if state.cost_basis is None or line.strike >= state.cost_basis),
            None,
        )
        count = min(opt_contracts, coverable_contracts(state.shares))
        if strangle_puts and call_line is not None and count >= 1:
            strangle_legs = (
                _option_leg(strangle_puts[0], meta, "OPEN_CSP", count),
                _option_leg(call_line, meta, "OPEN_CC", count),
            )
    gap_legs: tuple[LegPlan, ...] = ()
    missing = share_gap(state.shares)
    if (
        high
        and flags.cc
        and cc_block == "cc_needs_shares"
        and buy_block is None
        and price is not None
        and missing >= 1
    ):
        gap_legs = (_buy_leg(missing, price, enforce_contract_cap=False),)
        cc_miss = None
    dip_legs: tuple[LegPlan, ...] = ()
    dip_reason = ""
    dip_miss = _dip_miss(dip, earnings, flags, csp_block, puts, reading)
    if dip and price is not None and share_count >= 1 and buy_block is None and may_buy(flags):
        buy = _buy_leg(share_count, price)
        if csp_block is None and flags.csp and RV_HIGH in reading.tags and puts and opt_contracts >= 1:
            dip_legs = (buy, _option_leg(puts[0], meta, "OPEN_CSP", opt_contracts))
            dip_reason = "dip_long"
            dip_miss = None
        else:
            dip_legs = (buy,)
            dip_reason = "dip_long"
            dip_miss = None
    elif dip and csp_block is None and flags.csp and puts and opt_contracts >= 1 and not may_buy(flags):
        dip_legs = (_option_leg(puts[0], meta, "OPEN_CSP", opt_contracts),)
        dip_reason = "dip_csp"
        dip_miss = None
    if strangle_legs:
        chosen, reason = strangle_legs, "signaled_strangle"
        put_lines, call_lines = strangle_puts, strangle_calls
    elif cc_legs and dip_legs:
        chosen, reason = cc_legs, "high_cc"
        put_lines, call_lines = [], calls
    elif cc_legs:
        chosen, reason = cc_legs, "high_cc"
        put_lines, call_lines = [], calls
    elif gap_legs:
        chosen, reason = gap_legs, "cover_gap"
        put_lines, call_lines = [], []
    elif dip_legs:
        chosen, reason = dip_legs, dip_reason
        put_lines, call_lines = puts, []
    else:
        quiet = _quiet_reason(dip, dip_miss, cc_miss, earnings)
        if strangle_on and not strangle_legs:
            quiet = "no_strangle_candidate"
        return Opening(name, name, "", quiet, "quiet", quiet, (), False)
    chosen, overruled = _apply_strike(chosen, put_lines, call_lines, narrator_strike)
    if not chosen or not any(leg.tactic in {"OPEN_CSP", "OPEN_CC", "BUY"} for leg in chosen):
        return Opening(name, name, "", "narrator_overruled", "dropped", "narrator_overruled", (), True)
    after = project_alias(state, chosen)
    return Opening(
        name,
        after,
        reason,
        None,
        "drafted",
        reason,
        chosen,
        overruled,
        tuple(put_lines),
        tuple(call_lines),
    )


def _cc_miss(state, flags, high, rail, earnings, cc_block, calls, contracts, price) -> str | None:
    if not flags.cc:
        return None
    if earnings and rail:
        return "earnings_near"
    if cc_block == "cc_needs_shares":
        return "cc_needs_shares" if rail else None
    if cc_block is not None:
        return None
    if not high:
        return "cc_rail_not_high" if price is not None else None
    if contracts < 1 or state.shares < 100:
        return "cc_needs_shares"
    if not calls:
        return "cc_below_cost" if state.cost_basis is not None else None
    if state.cost_basis is not None and calls[0].strike < state.cost_basis:
        return "cc_below_cost"
    return None


def _dip_miss(dip, earnings, flags, csp_block, puts, reading) -> str | None:
    if not dip:
        return None
    if earnings:
        return "earnings_near"
    if not may_buy(flags) and csp_block is not None:
        return csp_block
    if not may_buy(flags) and flags.csp and not puts:
        return "no_csp_candidate"
    if may_buy(flags) and csp_block is None and flags.csp and RV_HIGH in reading.tags and not puts:
        return "no_csp_candidate"
    return None


def _quiet_reason(dip: bool, dip_miss: str | None, cc_miss: str | None, earnings: bool) -> str:
    if earnings and (dip_miss == "earnings_near" or cc_miss == "earnings_near"):
        return "earnings_near"
    if dip and dip_miss:
        return dip_miss
    if cc_miss:
        return cc_miss
    if not dip:
        return "not_a_dip"
    return dip_miss or "not_a_dip"


def _apply_strike(
    legs: tuple[LegPlan, ...],
    put_lines: list[ScreenerLine],
    call_lines: list[ScreenerLine],
    strike: Decimal | None,
) -> tuple[tuple[LegPlan, ...], bool]:
    if strike is None:
        return legs, False
    option = [leg for leg in legs if leg.tactic in {"OPEN_CSP", "OPEN_CC"}]
    if not option:
        return legs, False
    put_hit = next((line for line in put_lines if line.strike == strike), None)
    call_hit = next((line for line in call_lines if line.strike == strike), None)
    if put_hit is None and call_hit is None:
        kept = tuple(leg for leg in legs if leg.tactic not in {"OPEN_CSP", "OPEN_CC"})
        return kept, True
    rebuilt: list[LegPlan] = []
    for leg in legs:
        if leg.tactic == "OPEN_CSP" and put_hit is not None:
            trading = (leg.trading_class, leg.option_exchange, leg.multiplier)
            rebuilt.append(_option_leg(put_hit, trading, "OPEN_CSP", leg.contracts))
        elif leg.tactic == "OPEN_CC" and call_hit is not None:
            trading = (leg.trading_class, leg.option_exchange, leg.multiplier)
            rebuilt.append(_option_leg(call_hit, trading, "OPEN_CC", leg.contracts))
        else:
            rebuilt.append(leg)
    return tuple(rebuilt), False


def _manage_pending(conn, account_id: str, symbol: str, simulated: bool) -> bool:
    """A roll or close already waiting for this name. A new open waits for the next pass."""
    row = conn.execute(
        """
        SELECT 1 FROM drafts AS d
        JOIN tickets AS t ON t.id = d.ticket_id
        WHERE d.account_id = %s AND d.symbol = %s AND d.status = 'pending'
          AND t.simulated = %s
          AND t.reason IN ('manage_roll', 'manage_close')
        LIMIT 1
        """,
        (account_id, symbol.upper(), simulated),
    ).fetchone()
    return row is not None


def _opening_rank(reason: str) -> int:
    return _OPENING_RANK.get(reason, len(_OPENING_RANK))


def _opening_hold(conn, account_id: str, symbol: str, simulated: bool) -> tuple[str, str, str] | None:
    """A live opening from today. Frozen wins. A blocked ticket does not hold the day."""
    rows = conn.execute(
        """
        SELECT t.id, t.reason, d.status, o.status
        FROM tickets AS t
        LEFT JOIN drafts AS d ON d.ticket_id = t.id
        LEFT JOIN orders AS o ON o.draft_id = d.id
        WHERE t.account_id = %s AND t.symbol = %s AND t.simulated = %s
          AND t.reason = ANY(%s)
          AND t.created_at::date = CURRENT_DATE
          AND t.status <> 'blocked'
        """,
        (account_id, symbol.upper(), simulated, list(_OPENING_REASONS)),
    ).fetchall()
    pending: tuple[int, str, str] | None = None
    for ticket_id, reason, draft_status, order_status in rows:
        if draft_status == "approved" or order_status in _FROZEN_ORDERS:
            return ("frozen", "", "")
        if draft_status == "pending":
            rank = _opening_rank(reason)
            if pending is None or rank < pending[0]:
                pending = (rank, ticket_id, reason)
    if pending is None:
        return None
    return ("pending", pending[1], pending[2])


def _replace_pending(conn, ticket_id: str, reason: str) -> None:
    text = f"replaced by {reason}"
    conn.execute(
        """
        UPDATE drafts SET status = 'blocked', reason = %s
        WHERE ticket_id = %s AND status = 'pending'
        """,
        (text, ticket_id),
    )
    conn.execute("UPDATE tickets SET status = 'blocked' WHERE id = %s", (ticket_id,))
    conn.commit()


def _short_leg(opening: Opening) -> bool:
    return any(leg.tactic in {"OPEN_CSP", "OPEN_CC", "OPEN_STRANGLE"} for leg in opening.legs)


def _share_legs(opening: Opening) -> tuple[LegPlan, ...]:
    return tuple(leg for leg in opening.legs if leg.tactic == "BUY")


def _short_floor(reading: Indicators, settings: SettingsPayload) -> tuple[str, str] | None:
    """Why a new short is refused. A missing sample is not a low percentile."""
    if reading.rv_pctl is None:
        return "no_data", "rv_samples"
    if reading.rv_pctl < settings.risk.min_rv_pctl_new_short:
        return "dropped", "rv_percentile"
    return None


def _removed_put(conn, account_id: str, symbol: str, opening: Opening, outcome: str, summary: str) -> None:
    """The put leaves the ticket. Its audit row is not a draft."""
    from athena_strategy.agents import write_decision

    write_decision(
        conn, account_id, symbol, "wheel", outcome, summary,
        {
            "state": opening.alias,
            "state_after": opening.alias,
            "reason": opening.reason,
            "items": [{"tactic": "OPEN_CSP", "outcome": outcome, "detail": summary, "draft_id": ""}],
        },
    )


def execute_opening(
    conn,
    settings: SettingsPayload,
    account_id: str,
    symbol: str,
    tags: set[str] | None = None,
    narrator_strike: Decimal | None = None,
    sentence: str = "",
) -> list[str]:
    """Write one opening ticket, or a trail when the day stays quiet."""
    opening = decide_opening(conn, settings, account_id, symbol, tags, narrator_strike)
    phase = _phase(conn, account_id, symbol)
    if opening.outcome == "drafted" and opening.overruled and _share_legs(opening) and not _short_leg(opening):
        _removed_put(conn, account_id, symbol, opening, "dropped", "narrator_overruled")
        opening = replace(opening, overruled=False)
    if opening.outcome == "drafted" and opening.legs and _short_leg(opening):
        reading = read_indicators(conn, settings, symbol)
        floor = _short_floor(reading, settings)
        if floor:
            buys = _share_legs(opening)
            if buys:
                state = wheel_state(conn, account_id, symbol)
                _removed_put(conn, account_id, symbol, opening, floor[0], floor[1])
                opening = replace(opening, legs=buys, state_after=project_alias(state, buys))
            else:
                opening = replace(opening, outcome=floor[0], summary=floor[1], legs=())
    if dry_run_enabled() and opening.outcome == "drafted" and opening.legs:
        opening = replace(opening, outcome="observed", summary=f"would ticket: {opening.reason}", legs=())
    if opening.outcome != "drafted" or not opening.legs:
        _trail(conn, account_id, symbol, opening, sentence, [])
        return []
    if opening.replaces:
        _replace_pending(conn, opening.replaces, opening.reason)
    ticket_id = "ticket_" + secrets.token_hex(4)
    simulated = _symbol_in_replay(conn, account_id, symbol)
    _insert_ticket(
        conn, ticket_id, account_id, symbol, opening.alias, opening.state_after,
        len(opening.legs), opening.reason, simulated,
    )
    pending: list[str] = []
    steps = []
    blocked = None
    for index, leg in enumerate(opening.legs):
        staged = stage_draft(
            conn, settings, symbol, leg.tactic, leg.contracts,
            sentence or opening.reason, phase, account_id,
            instrument=leg.instrument, limit_price=leg.limit_price, expiry=leg.expiry,
            put_strike=leg.put_strike, call_strike=leg.call_strike,
            put_limit=leg.put_limit, call_limit=leg.call_limit,
            trading_class=leg.trading_class, option_exchange=leg.option_exchange,
            multiplier=leg.multiplier, ticket_id=ticket_id, leg_index=index,
            enforce_contract_cap=leg.enforce_contract_cap,
        )
        if staged["status"] == "pending" and staged["id"]:
            pending.append(staged["id"])
        elif staged["status"] == "blocked":
            blocked = staged["detail"]
        steps.append(staged)
    if blocked and pending:
        conn.execute(
            """
            UPDATE drafts SET status = 'blocked', reason = %s
            WHERE ticket_id = %s AND status = 'pending'
            """,
            (blocked, ticket_id),
        )
        conn.commit()
        pending = []
        opening = replace(opening, outcome="dropped", summary=blocked)
    ticket_block = apply_ticket_block(conn, settings, account_id, symbol, pending)
    if ticket_block:
        named = f"{ticket_id}: {ticket_block}"
        conn.execute(
            """
            UPDATE drafts SET status = 'blocked', reason = %s
            WHERE ticket_id = %s
            """,
            (named, ticket_id),
        )
        conn.commit()
        pending = []
        opening = replace(opening, outcome="dropped", summary=named)
    elif opening.overruled:
        opening = replace(opening, summary="narrator_overruled")
    _trail(conn, account_id, symbol, opening, sentence, steps)
    return pending


def commit_refused(
    conn,
    settings: SettingsPayload,
    account_id: str,
    symbol: str,
    tactics: tuple[str, ...],
) -> str:
    """Write a ticket whose legs the transition table refuses. Every leg is blocked."""
    state = wheel_state(conn, account_id, symbol)
    name = alias_of(state)
    reason = next((refusal(name, tactic) for tactic in tactics if refusal(name, tactic)), None)
    if reason is None:
        reason = "manual_review"
    ticket_id = "ticket_" + secrets.token_hex(4)
    simulated = _symbol_in_replay(conn, account_id, symbol)
    _insert_ticket(conn, ticket_id, account_id, symbol, name, name, len(tactics), reason, simulated)
    phase = _phase(conn, account_id, symbol)
    for index, tactic in enumerate(tactics):
        instrument = "STK" if tactic == "BUY" else "OPT"
        multiplier = "1" if tactic == "BUY" else "100"
        stage_draft(
            conn, settings, symbol, tactic, 1, reason, phase, account_id,
            instrument=instrument, limit_price=Decimal("1"), multiplier=multiplier,
            ticket_id=ticket_id, leg_index=index, forced_block=reason,
        )
    return ticket_id


def insert_manage_ticket(
    conn,
    account_id: str,
    symbol: str,
    reason: str,
    leg_count: int,
) -> str:
    state = wheel_state(conn, account_id, symbol)
    name = alias_of(state)
    ticket_id = "ticket_" + secrets.token_hex(4)
    simulated = _symbol_in_replay(conn, account_id, symbol)
    _insert_ticket(conn, ticket_id, account_id, symbol, name, name, leg_count, reason, simulated)
    return ticket_id


def _insert_ticket(conn, ticket_id, account_id, symbol, before, after, legs, reason, simulated) -> None:
    conn.execute(
        """
        INSERT INTO tickets (
            id, account_id, symbol, state_before, state_after, legs, reason, status, simulated
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s)
        """,
        (ticket_id, account_id, symbol.upper(), before, after, legs, reason, simulated),
    )
    conn.commit()


def _phase(conn, account_id: str, symbol: str) -> str:
    row = conn.execute(
        "SELECT phase FROM symbol_phases WHERE account_id = %s AND symbol = %s",
        (account_id, symbol.upper()),
    ).fetchone()
    if row is None or not row[0]:
        return "P1"
    return row[0]


def _trail(conn, account_id, symbol, opening: Opening, sentence: str, steps: list) -> None:
    from athena_strategy.agents import write_decision

    items = []
    for step in steps:
        items.append(
            {
                "tactic": step.get("tactic") or "",
                "outcome": step.get("status") or "",
                "detail": step.get("detail") or "",
                "draft_id": step.get("id") or "",
            }
        )
    payload = {
        "state": opening.alias,
        "state_after": opening.state_after,
        "reason": opening.reason,
        "thinking": sentence,
        "items": items,
    }
    if opening.overruled:
        payload["narrator_overruled"] = True
    summary = opening.summary
    if opening.overruled and opening.outcome == "drafted":
        summary = "narrator_overruled"
    write_decision(conn, account_id, symbol, "wheel", opening.outcome, summary, payload)


def transition_names() -> tuple[str, ...]:
    return tuple(_TRANSITIONS)


def transition_actions() -> tuple[str, ...]:
    return _ACTIONS
