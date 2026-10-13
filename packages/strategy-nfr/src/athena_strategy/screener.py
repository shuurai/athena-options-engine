"""One ranked option menu. Agents and drafts read this, not option_choices."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from athena_exec.options import bs_delta, rv_multiple, target_otm_pct
from athena_settings.model import SettingsPayload, TacticFlags
from athena_strategy.iv import estimated_iv

_HUNDRED = Decimal(100)


def screener_enabled() -> bool:
    flag = os.environ.get("ATHENA_SCREENER", "on").strip().lower()
    return flag not in {"0", "off", "false", "no"}


@dataclass(frozen=True)
class Choice:
    expiry: date
    right: str
    strike: Decimal
    ask: Decimal | None
    bid: Decimal | None = None
    open_interest: Decimal | None = None
    volume: Decimal | None = None


@dataclass(frozen=True)
class ScreenerLine:
    symbol: str
    expiry: date
    right: str
    strike: Decimal
    ask: Decimal
    spot: Decimal
    dte: int
    otm_pct: Decimal
    delta: Decimal | None
    delta_source: str
    moneyness: Decimal
    premium_per_contract: Decimal
    breakeven: Decimal
    yield_on_strike_pct: Decimal
    yield_on_basis_pct: Decimal | None
    avg_price: Decimal | None
    earnings_after_expiry: bool | None
    liquidity: str
    rank_key: tuple


def sigma_for(conn, settings: SettingsPayload, symbol: str, today: date, rv20) -> tuple[float | None, str]:
    """Measured vol when it exists, otherwise realized times the assumption."""
    measured = estimated_iv(conn, symbol, today) if conn is not None else None
    if measured is not None and measured > 0:
        return float(measured), "iv"
    if rv20 is not None and rv20 > 0:
        multiple = rv_multiple(Decimal(rv20), settings)
        return float(multiple * Decimal(rv20) / _HUNDRED), "rv_mult"
    return None, "otm_band"


def build_line(
    choice: Choice,
    *,
    symbol: str,
    spot: Decimal,
    today: date,
    sigma: float | None,
    source: str,
    multiplier: Decimal,
    avg_price: Decimal | None,
    earnings_in: int | None,
    rank_key: tuple = (),
    max_spread_pct: int = 20,
) -> ScreenerLine | None:
    """One priced row. A missing ask never becomes a line. A wide spread does not either."""
    if choice.ask is None or choice.ask <= 0 or spot <= 0:
        return None
    dte = (choice.expiry - today).days
    if dte <= 0 or choice.strike <= 0:
        return None
    if _spread_wide(choice, max_spread_pct):
        return None
    if choice.right == "P":
        otm = (spot - choice.strike) / spot * _HUNDRED
        breakeven = choice.strike - choice.ask
    else:
        otm = (choice.strike - spot) / spot * _HUNDRED
        breakeven = choice.strike + choice.ask
    delta = None if sigma is None else bs_delta(spot, choice.strike, dte, sigma, choice.right)
    basis = None
    if avg_price is not None and avg_price > 0 and choice.right == "C":
        basis = choice.ask / avg_price * Decimal(365) / Decimal(dte) * _HUNDRED
    earnings = None if earnings_in is None else earnings_in > dte
    if choice.bid is None and choice.open_interest is None and choice.volume is None:
        liquidity = "unknown"
    else:
        liquidity = "quoted"
    return ScreenerLine(
        symbol=symbol.upper(),
        expiry=choice.expiry,
        right=choice.right,
        strike=choice.strike,
        ask=choice.ask,
        spot=spot,
        dte=dte,
        otm_pct=otm,
        delta=delta,
        delta_source=source if delta is not None else "otm_band",
        moneyness=choice.strike / spot,
        premium_per_contract=choice.ask * multiplier,
        breakeven=breakeven,
        yield_on_strike_pct=choice.ask / choice.strike * Decimal(365) / Decimal(dte) * _HUNDRED,
        yield_on_basis_pct=basis,
        avg_price=avg_price if choice.right == "C" else None,
        earnings_after_expiry=earnings,
        liquidity=liquidity,
        rank_key=rank_key,
    )


def _spread_wide(choice: Choice, max_spread_pct: int) -> bool:
    """True when both sides exist and the spread is above the limit. Zero turns this off."""
    if not max_spread_pct or choice.bid is None or choice.bid <= 0 or choice.ask is None:
        return False
    if choice.bid >= choice.ask:
        return False
    mid = (choice.ask + choice.bid) / Decimal(2)
    if mid <= 0:
        return False
    spread = (choice.ask - choice.bid) / mid * _HUNDRED
    return spread > Decimal(max_spread_pct)


def _gap(line: ScreenerLine, target: Decimal, target_otm: Decimal) -> Decimal:
    if line.delta is None:
        return abs(line.otm_pct - target_otm)
    return abs(abs(line.delta) - target)


def _keyed(line: ScreenerLine, key: tuple) -> ScreenerLine:
    return ScreenerLine(
        symbol=line.symbol,
        expiry=line.expiry,
        right=line.right,
        strike=line.strike,
        ask=line.ask,
        spot=line.spot,
        dte=line.dte,
        otm_pct=line.otm_pct,
        delta=line.delta,
        delta_source=line.delta_source,
        moneyness=line.moneyness,
        premium_per_contract=line.premium_per_contract,
        breakeven=line.breakeven,
        yield_on_strike_pct=line.yield_on_strike_pct,
        yield_on_basis_pct=line.yield_on_basis_pct,
        avg_price=line.avg_price,
        earnings_after_expiry=line.earnings_after_expiry,
        liquidity=line.liquidity,
        rank_key=key,
    )


def rank_choices(
    choices: list[Choice],
    *,
    symbol: str,
    spot: Decimal,
    today: date,
    settings: SettingsPayload,
    purpose: str,
    sigma: float | None,
    source: str,
    target_otm: Decimal,
    multiplier: Decimal = Decimal(100),
    avg_price: Decimal | None = None,
    earnings_in: int | None = None,
) -> list[ScreenerLine]:
    """Top rows from the nearest expiry that still has a legal strike."""
    strategy = settings.strategy
    lines: list[ScreenerLine] = []
    for choice in choices:
        line = build_line(
            choice,
            symbol=symbol,
            spot=spot,
            today=today,
            sigma=sigma,
            source=source,
            multiplier=multiplier,
            avg_price=avg_price,
            earnings_in=earnings_in,
            max_spread_pct=settings.risk.max_spread_pct,
        )
        if line is not None and line.otm_pct > 0:
            lines.append(line)
    expiries = sorted({line.expiry for line in lines}, key=lambda item: (abs((item - today).days - strategy.dte_target), item))
    target = _target(strategy, purpose)
    lo = Decimal(str(strategy.csp_delta_lo))
    hi = Decimal(str(strategy.csp_delta_hi))
    for expiry in expiries:
        group = [line for line in lines if line.expiry == expiry]
        kept = [_keyed(line, _key(line, purpose, target, target_otm)) for line in group if _keeps(line, purpose, spot, avg_price, lo, hi)]
        if not kept:
            continue
        kept.sort(key=lambda line: line.rank_key)
        return kept[: max(1, strategy.screener_top_n)]
    return []


def _target(strategy, purpose: str) -> Decimal:
    if purpose == "cc":
        return Decimal(str(strategy.cc_delta_target))
    if purpose == "strangle_call" or purpose == "strangle_put":
        return Decimal(str(strategy.strangle_delta_target))
    return Decimal(str(strategy.csp_delta_target))


def basis_block(choices: list[Choice], spot: Decimal | None, avg_price: Decimal | None) -> str | None:
    """Name the case where every listed call sits below the stock cost."""
    if spot is None or spot <= 0 or avg_price is None or avg_price <= spot:
        return None
    calls = [choice for choice in choices if choice.right == "C" and choice.ask is not None and choice.ask > 0]
    if calls and all(choice.strike < avg_price for choice in calls):
        return "cc_no_strike_above_basis"
    return None


def _keeps(line: ScreenerLine, purpose: str, spot: Decimal, avg_price: Decimal | None, lo: Decimal, hi: Decimal) -> bool:
    if purpose == "cc":
        floor = spot if avg_price is None else max(avg_price, spot)
        return line.right == "C" and line.strike >= floor
    if line.delta is None:
        return (line.right == "P" and purpose in {"csp", "strangle_put"}) or (
            line.right == "C" and purpose == "strangle_call" and line.strike >= spot
        )
    band = lo <= abs(line.delta) <= hi
    if purpose in {"csp", "strangle_put"}:
        return line.right == "P" and band
    if purpose == "strangle_call":
        return line.right == "C" and line.strike >= spot and band
    return False


def _key(line: ScreenerLine, purpose: str, target: Decimal, target_otm: Decimal) -> tuple:
    gap = _gap(line, target, target_otm)
    if purpose == "cc":
        delta_gap = gap
        return (line.strike, delta_gap, -line.yield_on_strike_pct, line.expiry.toordinal())
    return (gap, -line.yield_on_strike_pct, line.strike, line.expiry.toordinal())


def _show(value) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, Decimal):
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"
    return str(value)


def render_table(lines: list[ScreenerLine], label: str) -> str:
    """One code-ranked table. Every row keeps the five figures, using unknown when one is missing."""
    if not lines:
        return "none"
    rows = [
        f"Listed {label} (top {len(lines)}, code-ranked):",
        "expiry       dte  strike  ask   otm%   delta  yield%  be     liq   src",
    ]
    for line in lines:
        side = "put" if line.right == "P" else "call"
        basis = ""
        if line.yield_on_basis_pct is not None:
            basis = f" basis% {_show(line.yield_on_basis_pct.quantize(Decimal('0.1')))}"
        cost = ""
        if line.avg_price is not None:
            cost = f" avg {_show(line.avg_price)}"
        rows.append(
            " ".join(
                (
                    str(line.expiry),
                    str(line.dte),
                    _show(line.strike),
                    _show(line.ask),
                    _show(line.otm_pct.quantize(Decimal("0.1"))) + "%",
                    _show(line.delta.quantize(Decimal("0.01")) if line.delta is not None else None),
                    _show(line.yield_on_strike_pct.quantize(Decimal("0.1"))),
                    _show(line.breakeven.quantize(Decimal("0.01"))),
                    line.liquidity,
                    line.delta_source,
                )
            )
            + basis
            + cost
        )
    pick = lines[0]
    side = "put" if pick.right == "P" else "call"
    rows.append(
        f"screener_pick: {pick.expiry} {side} {_show(pick.strike)} (default if there is no other reason)"
    )
    return "\n".join(rows)


def _reading_rv(reading) -> Decimal | None:
    if reading is None or getattr(reading, "rv20", None) is None:
        return None
    return Decimal(reading.rv20)


def _earnings(reading) -> int | None:
    if reading is None:
        return None
    return getattr(reading, "earnings_in", None)


def stock_basis(conn, account_id: str, symbol: str) -> Decimal | None:
    """Stock cost for this account's live book, or its replay book while that symbol is replaying."""
    if not account_id:
        return None
    name = symbol.upper()
    replay = conn.execute(
        """
        SELECT 1 FROM sim_runs
        WHERE account_id = %s AND status IN ('active', 'halted')
          AND position(',' || %s || ',' in ',' || symbols || ',') > 0
        LIMIT 1
        """,
        (account_id, name),
    ).fetchone()
    row = conn.execute(
        """
        SELECT avg_price, quantity FROM positions
        WHERE account_id = %s AND symbol = %s AND sec_type = 'STK' AND simulated = %s
        ORDER BY snapshot_at DESC, id DESC
        LIMIT 1
        """,
        (account_id, name, replay is not None),
    ).fetchone()
    if row is None or row[0] is None or row[1] is None or row[1] <= 0 or row[0] <= 0:
        return None
    return Decimal(row[0])


def _width(settings: SettingsPayload, rv20: Decimal | None, dte: int) -> tuple[Decimal, str | None]:
    if rv20 is None or rv20 <= 0 or dte <= 0:
        return Decimal(settings.risk.otm_floor_pct), None
    return target_otm_pct(rv20, settings, dte)


def candidates(
    conn,
    settings: SettingsPayload,
    symbol: str,
    reading,
    today: date,
    *,
    account_id: str = "",
    purpose: str = "csp",
    spot: Decimal | None = None,
    choices: list[Choice] | None = None,
) -> tuple[list[ScreenerLine], str | None]:
    """Ranked lines for one purpose, and floor or ceiling when the width was clamped."""
    rv20 = _reading_rv(reading)
    width, note = _width(settings, rv20, settings.strategy.dte_target)
    sigma, source = sigma_for(conn, settings, symbol, today, rv20)
    rows = choices if choices is not None else load_choices(conn, symbol)
    if spot is None or spot <= 0:
        return [], note
    ranked = rank_choices(
        rows,
        symbol=symbol,
        spot=spot,
        today=today,
        settings=settings,
        purpose=purpose,
        sigma=sigma,
        source=source,
        target_otm=width,
        avg_price=stock_basis(conn, account_id, symbol) if conn is not None and account_id else None,
        earnings_in=_earnings(reading),
    )
    return ranked, note


def load_choices(conn, symbol: str) -> list[Choice]:
    """option_choices reader. The session writer is the only other touch of this table."""
    rows = conn.execute(
        """
        SELECT expiry, contract_right, strike, limit_price, bid, open_interest, volume
        FROM option_choices
        WHERE symbol = %s
        ORDER BY expiry, contract_right, strike
        """,
        (symbol.upper(),),
    ).fetchall()
    found = []
    for expiry, right, strike, ask, bid, interest, volume in rows:
        found.append(
            Choice(
                expiry=expiry,
                right=right,
                strike=Decimal(strike),
                ask=None if ask is None else Decimal(ask),
                bid=None if bid is None else Decimal(bid),
                open_interest=None if interest is None else Decimal(interest),
                volume=None if volume is None else Decimal(volume),
            )
        )
    return found


def has_choices(conn, symbol: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM option_choices WHERE symbol = %s LIMIT 1",
        (symbol.upper(),),
    ).fetchone()
    return row is not None


def legacy_text(conn, symbol: str, plan: dict | None) -> tuple[str, str]:
    """The old free-text lines, used only when the screener flag is off."""
    puts: list[str] = []
    calls: list[str] = []
    for choice in load_choices(conn, symbol):
        if choice.ask is None or choice.ask <= 0:
            continue
        side = "put" if choice.right == "P" else "call"
        line = f"{choice.expiry} {side} {_show(choice.strike)} limit {_show(choice.ask)}"
        if choice.right == "P":
            puts.append(line)
        else:
            calls.append(line)
    if puts or calls:
        return "\n".join(puts) or "none", "\n".join(calls) or "none"
    return _listed(plan)


def _listed(plan: dict | None) -> tuple[str, str]:
    if plan is None:
        return "none", "none"
    expiry = plan["expiry"]
    put = f"{expiry} put {_show(plan['put_strike'])} limit {_show(plan['put_limit'])}"
    call = f"{expiry} call {_show(plan['call_strike'])} limit {_show(plan['call_limit'])}"
    return put, call


def _with_alt(text: str, primary: list[ScreenerLine], extra: list[ScreenerLine], label: str) -> str:
    """Keep the tactic table, and add the strangle table only when its strikes differ."""
    if extra and [line.strike for line in extra] != [line.strike for line in primary]:
        return text + "\n" + render_table(extra, label)
    return text


def prompt_sides(conn, settings: SettingsPayload, account_id: str, symbol: str, reading, plan: dict | None, spot, today: date, flags: TacticFlags | None = None) -> tuple[str, str, str | None, str | None, str | None]:
    """Put table, call table, clamp note, covered-call block, and the pick's delta source."""
    if not screener_enabled() or spot is None:
        puts, calls = legacy_text(conn, symbol, plan)
        return puts, calls, None, None, None
    stored = load_choices(conn, symbol)
    basis = stock_basis(conn, account_id, symbol) if account_id else None
    block = basis_block(stored, spot, basis)
    want_csp = flags is None or flags.csp
    want_cc = flags is None or flags.cc
    want_strangle = flags is None or flags.strangle
    puts, note = ([], None)
    calls, call_note = ([], None)
    strangle_calls: list = []
    strangle_puts: list = []
    if want_csp:
        puts, note = candidates(
            conn, settings, symbol, reading, today, account_id=account_id, purpose="csp", spot=spot, choices=stored,
        )
    if want_cc:
        calls, call_note = candidates(
            conn, settings, symbol, reading, today, account_id=account_id, purpose="cc", spot=spot, choices=stored,
        )
    if want_strangle:
        strangle_calls, _strangle_note = candidates(
            conn, settings, symbol, reading, today, account_id=account_id, purpose="strangle_call", spot=spot, choices=stored,
        )
        strangle_puts, _strangle_put_note = candidates(
            conn, settings, symbol, reading, today, account_id=account_id, purpose="strangle_put", spot=spot, choices=stored,
        )
    put_text = _with_alt(render_table(puts, "puts"), puts, strangle_puts, "strangle puts")
    call_text = _with_alt(render_table(calls, "calls"), calls, strangle_calls, "strangle calls")
    if block and call_text == "none":
        call_text = block
    source = ""
    if puts:
        source = puts[0].delta_source
    elif calls:
        source = calls[0].delta_source
    if not (want_csp or want_cc or want_strangle):
        return "none", "none", None, None, None
    if put_text == "none" and call_text == "none":
        listed_puts, listed_calls = _listed(plan)
        return listed_puts, listed_calls, note or call_note, block, source or None
    return put_text, call_text, note or call_note, block, source or None


def outside_table(
    conn,
    settings: SettingsPayload,
    account_id: str,
    symbol: str,
    tactic: str,
    expiry,
    put_strike,
    call_strike,
    spot,
    today: date,
    reading=None,
) -> str | None:
    """A named reason when a proposed strike is not in the ranked table."""
    if not screener_enabled() or tactic not in {"OPEN_CSP", "OPEN_CC", "OPEN_STRANGLE"}:
        return None
    if not has_choices(conn, symbol):
        return None
    if spot is None or spot <= 0:
        spot = _live_spot(conn, symbol)
    if spot is None or spot <= 0:
        return None
    if reading is None:
        from athena_strategy.indicators import read_indicators

        reading = read_indicators(conn, settings, symbol)
    if tactic == "OPEN_CSP":
        pairs = [("csp", put_strike)]
    elif tactic == "OPEN_CC":
        pairs = [("cc", call_strike)]
    else:
        pairs = [("strangle_put", put_strike), ("strangle_call", call_strike)]
    stored = load_choices(conn, symbol)
    for purpose, strike in pairs:
        if strike is None:
            continue
        rows, _note = candidates(
            conn, settings, symbol, reading, today, account_id=account_id, purpose=purpose, spot=spot, choices=stored,
        )
        if _matches(rows, expiry, strike):
            continue
        if purpose == "cc" and basis_block(stored, spot, stock_basis(conn, account_id, symbol)):
            return "cc_no_strike_above_basis"
        return "strike is outside the screener table"
    return None


def _live_spot(conn, symbol: str):
    from athena_marketdata.pricing import last_tradeable_price
    from athena_strategy.simbars import in_replay, last_close

    if in_replay(conn, symbol):
        return last_close(conn, symbol)
    return last_tradeable_price(conn, symbol)


def _matches(rows: list[ScreenerLine], expiry, strike) -> bool:
    if expiry is None or strike is None:
        return False
    wanted_day = expiry if isinstance(expiry, date) else date.fromisoformat(str(expiry)[:10])
    wanted_strike = Decimal(str(strike))
    return any(line.expiry == wanted_day and line.strike == wanted_strike for line in rows)


def menu_rows(conn, settings: SettingsPayload, account_id: str, symbol: str, tactic: str, spot, today: date) -> tuple[list[tuple[date, Decimal]], list[tuple[date, Decimal]]]:
    """Strikes the approve page may move to."""
    puts: list[tuple[date, Decimal]] = []
    calls: list[tuple[date, Decimal]] = []
    if tactic not in {"OPEN_STRANGLE", "OPEN_CSP", "OPEN_CC"}:
        return puts, calls
    if not screener_enabled() or spot is None:
        for choice in load_choices(conn, symbol):
            if choice.right == "P" and tactic != "OPEN_CC":
                puts.append((choice.expiry, choice.strike))
            if choice.right == "C" and tactic != "OPEN_CSP":
                calls.append((choice.expiry, choice.strike))
        return puts, calls
    from athena_strategy.indicators import read_indicators

    reading = read_indicators(conn, settings, symbol)
    if tactic != "OPEN_CC":
        purpose = "csp" if tactic == "OPEN_CSP" else "strangle_put"
        rows, _note = candidates(conn, settings, symbol, reading, today, account_id=account_id, purpose=purpose, spot=spot)
        puts = [(line.expiry, line.strike) for line in rows]
    if tactic != "OPEN_CSP":
        purpose = "cc" if tactic == "OPEN_CC" else "strangle_call"
        rows, _note = candidates(conn, settings, symbol, reading, today, account_id=account_id, purpose=purpose, spot=spot)
        calls = [(line.expiry, line.strike) for line in rows]
    return puts, calls
