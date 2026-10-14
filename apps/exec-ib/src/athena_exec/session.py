"""Connect only in an Athena slot. Mobile and idle leave the database unchanged."""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime
from decimal import Decimal

from psycopg.types.json import Jsonb

from athena_exec.broker import BrokerPosition, IbBroker, set_subscription_limit
from athena_exec.gate import SOFT_BLOCKS, live_block_reason
from athena_exec.halt import active_reason, note_nav
from athena_exec.slots import HKT, cash_open, slot_state, symbol_market
from athena_settings.model import SettingsPayload, TacticFlags
from athena_exec.options import (
    atm_strikes,
    choose_expiry,
    choose_strikes,
    discovery_params,
    expiry_band,
    plan_otm_pct,
    strike_shortlist,
    wide_band,
)
from athena_marketdata.pricing import has_daily_on, has_ib_daily, last_tradeable_price, run_bar_write, upsert_delayed

log = logging.getLogger("athena.exec")


def run_session(
    conn,
    settings: SettingsPayload,
    account: str,
    broker=None,
    now: datetime | None = None,
    sync: bool = True,
    trading: bool = True,
    also_sync: list[str] | None = None,
) -> list[tuple]:
    """Run one Athena slot. Returns fills that were new this cycle."""
    created = broker is None
    broker = broker or IbBroker()
    set_subscription_limit(settings.ib.concurrent_subscriptions)
    try:
        moment = now or datetime.now(HKT)
        slot = slot_state(moment, settings.ib)
        host = os.environ.get("IB_GATEWAY_HOST", settings.ib.gateway_host)
        port = int(os.environ.get("IB_GATEWAY_PORT", str(settings.ib.gateway_port)))
        from athena_exec.clients import client_id

        login = client_id("session", settings.ib.client_id)
        if not _connect_athena(broker, host, port, login, slot):
            log.info("ib_session=%s account=%s", "disconnected" if slot == "mobile" else "idle", account)
            return []
        log.info("ib_session=connected account=%s", account)
        if hasattr(broker, "set_markets"):
            broker.set_markets({symbol: _name_market(settings, symbol) for symbol in settings.watchlist})

        _cache_quotes(conn, broker, settings, moment)
        _cache_history(conn, broker, settings, moment)
        _cache_strangles(conn, broker, settings, moment)
        _record_chain_marks(conn, broker, settings, moment)
        if sync:
            _refresh_account(conn, broker, settings, account, moment)
        for other in also_sync or []:
            if other and other != account:
                _refresh_account(conn, broker, settings, other, moment)
        if trading:
            _submit_queued(conn, broker, slot, account)
        else:
            log.info("trading off account=%s", account)
        noticed = _apply_fills(conn, account, broker.fills())
        noticed.extend(_sync_order_status(conn, broker, account))
        return noticed
    finally:
        if created:
            broker.disconnect()


def _publish_order(account: str, order_id: str) -> None:
    from athena_exec.order_watch import publish

    publish(account, order_id)


def _name_market(settings: SettingsPayload, symbol: str) -> str:
    flags = settings.tactic_flags.get(symbol.upper(), TacticFlags())
    return symbol_market(symbol, flags.market or "")


def _cache_quotes(conn, broker, settings: SettingsPayload, now: datetime) -> None:
    hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    for symbol in settings.watchlist:
        if not cash_open(symbol, now, _name_market(settings, symbol)):
            log.info("quote closed symbol=%s", symbol)
            continue
        price = broker.last_price(symbol)
        if price is None:
            log.info("quote missing symbol=%s", symbol)
            continue
        if upsert_delayed(conn, symbol, price, hour):
            log.info("quote symbol=%s close=%s", symbol, price)


def _cache_history(conn, broker, settings: SettingsPayload, now: datetime) -> None:
    today = now.astimezone(HKT).date()
    for symbol in settings.watchlist:
        if not cash_open(symbol, now, _name_market(settings, symbol)):
            if has_ib_daily(conn, symbol):
                continue
        elif has_daily_on(conn, symbol, today):
            continue
        bars = broker.daily_history(symbol)
        if not bars:
            log.info("history missing symbol=%s", symbol)
            continue

        def write(bars=bars, symbol=symbol) -> None:
            for bar in bars:
                day = bar.date
                ts = datetime(day.year, day.month, day.day, tzinfo=UTC)
                conn.execute(
                    """
                    INSERT INTO market_bars (symbol, ts, open, high, low, close, volume, source)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'ib-daily')
                    ON CONFLICT (symbol, ts, source) DO UPDATE SET
                        open = EXCLUDED.open,
                        high = EXCLUDED.high,
                        low = EXCLUDED.low,
                        close = EXCLUDED.close,
                        volume = EXCLUDED.volume
                    """,
                    (symbol, ts, bar.open, bar.high, bar.low, bar.close, bar.volume or 0),
                )

        if run_bar_write(conn, write):
            log.info("history symbol=%s bars=%s", symbol, len(bars))


def _connect_athena(broker, host: str, port: int, client_id: int, slot: str, attempts: int = 3, pause: float = 1.0) -> bool:
    """Retry only while the slot is Athena. Mobile and idle stay disconnected."""
    if slot != "athena":
        broker.disconnect()
        return False
    for attempt in range(attempts):
        if broker.connect(host, port, client_id, False):
            return True
        if attempt + 1 < attempts:
            time.sleep(pause)
    return False


def _refresh_account(conn, broker, settings: SettingsPayload, account: str, moment: datetime) -> None:
    log.info("positions %s", account)
    _sync_positions(conn, account, broker.positions(account), replace=True)
    log.info("account marks %s", account)
    _store_account_mark(conn, account, moment.date(), broker, settings.global_currency)
    nav = broker.net_liquidation(account) if account else None
    if nav is None:
        return
    reason = note_nav(conn, account, nav, settings.risk.daily_loss_halt_pct, moment)
    if reason:
        log.info("halt=%s account=%s", reason, account)


def _store_account_mark(conn, account: str, day, broker, currency: str = "") -> None:
    if not account or not hasattr(broker, "cash"):
        return
    cash = broker.cash(account)
    margin = broker.margin(account)
    base, balances, rates = ("", {}, {})
    if hasattr(broker, "cash_ledger"):
        base, balances, rates = broker.cash_ledger(account)
    if base and currency and hasattr(broker, "fill_rates"):
        rates = broker.fill_rates(base, rates, (currency,))
    if cash is None and margin is None and not balances:
        return
    conn.execute(
        """
        INSERT INTO account_marks (
            account_id, day, cash, margin, base_currency, cash_balances, fx_rates
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (account_id, day) DO UPDATE SET
            cash = EXCLUDED.cash,
            margin = EXCLUDED.margin,
            base_currency = EXCLUDED.base_currency,
            cash_balances = EXCLUDED.cash_balances,
            fx_rates = EXCLUDED.fx_rates
        """,
        (
            account,
            day,
            cash,
            margin,
            base,
            Jsonb({code: str(amount) for code, amount in balances.items()}),
            Jsonb({code: str(rate) for code, rate in rates.items()}),
        ),
    )
    conn.commit()


def refresh_display_rates(database_url: str) -> None:
    """Read the chosen currency and store its IB pair when the ledger has no rate."""
    import asyncio

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    from athena_exec.accounts import active_accounts
    from athena_settings.repository import load_active
    from athena_store import connect as store_connect

    host = os.environ.get("IB_GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("IB_GATEWAY_PORT", "4002"))
    accounts = active_accounts()
    if not database_url or not accounts:
        return
    broker = IbBroker()
    if not broker.connect(host, port, 4, True):
        return
    try:
        with store_connect(database_url) as conn:
            for account in accounts:
                base, _balances, rates = broker.cash_ledger(account)
                chosen = ""
                try:
                    chosen = load_active(conn, account).global_currency
                except Exception as exc:
                    log.info("fx settings: %s", exc.__class__.__name__)
                filled = broker.fill_rates(base, rates, (chosen,) if chosen else ())
                if not filled:
                    continue
                conn.execute(
                    """
                    UPDATE account_marks
                    SET base_currency = %s, fx_rates = %s
                    WHERE account_id = %s
                      AND day = (
                          SELECT max(day) FROM account_marks WHERE account_id = %s
                      )
                    """,
                    (
                        base,
                        Jsonb({code: str(rate) for code, rate in filled.items()}),
                        account,
                        account,
                    ),
                )
            conn.commit()
    finally:
        broker.disconnect()


def cache_symbol_chain(conn, broker, settings: SettingsPayload, symbol: str, underlying: Decimal, today) -> None:
    """Snapshot one name's listed band and a strangle plan. Reused by a paper replay."""
    strategy = settings.strategy
    chain = broker.option_chain(symbol)
    if chain is None:
        return
    expiries, _strikes, trading_class, exchange, multiplier = chain
    expiry = choose_expiry(list(expiries), today, strategy.dte_min, strategy.dte_target)
    _store_choices(
        conn, broker, symbol, list(expiries), today, underlying,
        trading_class, exchange, multiplier, strategy, expiry,
    )
    flags = settings.tactic_flags.get(symbol.upper(), TacticFlags())
    if not flags.strangle:
        return
    if expiry is None:
        log.info("strangle none symbol=%s", symbol)
        return
    strikes = broker.expiry_strikes(symbol, expiry, trading_class, exchange)
    distance = plan_otm_pct(settings, _stored_rv(conn, symbol), (expiry - today).days)
    picked = choose_strikes(strikes, underlying, distance)
    if picked is None:
        log.info("strangle none symbol=%s", symbol)
        return
    put_strike, call_strike = picked
    put_limit = broker.option_ask(symbol, expiry, put_strike, "P", trading_class, exchange, multiplier)
    call_limit = broker.option_ask(symbol, expiry, call_strike, "C", trading_class, exchange, multiplier)
    conn.execute(
            """
            INSERT INTO option_plans (
                symbol, expiry, put_strike, call_strike, put_limit, call_limit,
                trading_class, option_exchange, multiplier, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (symbol) DO UPDATE SET
                expiry = EXCLUDED.expiry,
                put_strike = EXCLUDED.put_strike,
                call_strike = EXCLUDED.call_strike,
                put_limit = EXCLUDED.put_limit,
                call_limit = EXCLUDED.call_limit,
                trading_class = EXCLUDED.trading_class,
                option_exchange = EXCLUDED.option_exchange,
                multiplier = EXCLUDED.multiplier,
                updated_at = now()
            """,
            (
                symbol, expiry, put_strike, call_strike, put_limit, call_limit,
                trading_class, exchange, multiplier,
            ),
    )
    conn.commit()
    log.info(
        "strangle symbol=%s expiry=%s put=%s call=%s",
        symbol, expiry, put_strike, call_strike,
    )


def chain_names(settings: SettingsPayload) -> list[str]:
    """Watchlist names that still sell options. A missing flag row stays on."""
    names = []
    for symbol in settings.watchlist:
        flags = settings.tactic_flags.get(symbol.upper(), TacticFlags())
        if flags.options_on():
            names.append(symbol)
    return names


def _cache_strangles(conn, broker, settings: SettingsPayload, now: datetime) -> None:
    today = now.astimezone(HKT).date()
    for symbol in chain_names(settings):
        if not cash_open(symbol, now, _name_market(settings, symbol)):
            log.info("chain closed symbol=%s", symbol)
            continue
        price = last_tradeable_price(conn, symbol)
        if price is None:
            continue
        cache_symbol_chain(conn, broker, settings, symbol, price, today)


def _record_chain_marks(conn, broker, settings: SettingsPayload, now: datetime) -> None:
    """Append one ATM mid per name. A replay never calls this."""
    today = now.astimezone(HKT).date()
    for symbol in chain_names(settings):
        if not cash_open(symbol, now, _name_market(settings, symbol)):
            continue
        price = last_tradeable_price(conn, symbol)
        if price is None:
            continue
        record_chain_snapshot(conn, broker, settings, symbol, price, today)


def record_chain_snapshot(conn, broker, settings: SettingsPayload, symbol: str, underlying: Decimal, today) -> bool:
    """Append the closest listed call and put mids. Skip when either quote is one-sided."""
    chain = broker.option_chain(symbol)
    if chain is None:
        return False
    expiries, _strikes, trading_class, exchange, multiplier = chain
    strategy = settings.strategy
    expiry = choose_expiry(list(expiries), today, strategy.dte_min, strategy.dte_target)
    if expiry is None:
        return False
    picked = atm_strikes(broker.expiry_strikes(symbol, expiry, trading_class, exchange), underlying)
    if picked is None:
        return False
    put_strike, call_strike = picked
    put_mid = broker.option_mid(symbol, expiry, put_strike, "P", trading_class, exchange, multiplier)
    call_mid = broker.option_mid(symbol, expiry, call_strike, "C", trading_class, exchange, multiplier)
    if put_mid is None or call_mid is None:
        log.info("chain mid missing symbol=%s", symbol)
        return False
    distance = max(abs(put_strike - underlying), abs(call_strike - underlying)) / underlying * Decimal(100)
    conn.execute(
        """
        INSERT INTO chain_marks (symbol, expiry, dte, k_call, k_put, moneyness_pct, mid_call, mid_put)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            symbol,
            expiry,
            (expiry - today).days,
            call_strike,
            put_strike,
            distance,
            call_mid,
            put_mid,
        ),
    )
    conn.commit()
    log.info("chain mark symbol=%s expiry=%s put=%s call=%s", symbol, expiry, put_strike, call_strike)
    return True


def _history_quiet(broker, symbol: str) -> bool:
    quiet = getattr(broker, "history_quiet", None)
    return bool(quiet and quiet(symbol))


def _choice_quote(broker, symbol, expiry, strike, right, trading_class, exchange, multiplier):
    """Ask and bid from one chain quote. A broker without a bid still returns the ask."""
    quote = getattr(broker, "option_quote", None)
    if quote is not None:
        return quote(symbol, expiry, strike, right, trading_class, exchange, multiplier)
    return broker.option_ask(symbol, expiry, strike, right, trading_class, exchange, multiplier), None


def _stored_rv(conn, symbol: str) -> Decimal | None:
    row = conn.execute(
        "SELECT rv20 FROM rv_marks WHERE symbol = %s ORDER BY day DESC LIMIT 1",
        (symbol.upper(),),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return Decimal(row[0])


def _store_choices(
    conn, broker, symbol: str, expiries: list[str], today, underlying: Decimal,
    trading_class: str, exchange: str, multiplier: str, strategy, target,
) -> None:
    """Keep a wide listed band and an ask on every expiry. option_choices writer; reads live in screener.py."""
    del target
    band = expiry_band(expiries, today, strategy.dte_min, strategy.dte_max, strategy.dte_target)
    width, distance = discovery_params(strategy)
    inside = wide_band()
    conn.execute("DELETE FROM option_choices WHERE symbol = %s", (symbol,))
    for expiry in band:
        listed = broker.expiry_strikes(symbol, expiry, trading_class, exchange)
        rows = [("P", strike) for strike in strike_shortlist(listed, underlying, "P", width=width, otm_pct=distance, inside=inside)]
        rows.extend(("C", strike) for strike in strike_shortlist(listed, underlying, "C", width=width, otm_pct=distance, inside=inside))
        quiet = False
        for right, strike in rows:
            if _history_quiet(broker, symbol):
                quiet = True
                break
            limit_price, bid = _choice_quote(
                broker, symbol, expiry, strike, right, trading_class, exchange, multiplier,
            )
            if _history_quiet(broker, symbol):
                quiet = True
            conn.execute(
                """
                INSERT INTO option_choices (
                    symbol, expiry, contract_right, strike, limit_price, bid
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (symbol, expiry, right, strike, limit_price, bid),
            )
            if quiet:
                break
        if quiet:
            log.info("option history stopped symbol=%s", symbol)
            break
    conn.commit()


def sync_account_positions(conn, account: str, rows: list[BrokerPosition]) -> int:
    """Replace this account's open book with one IB snapshot. Returns how many are open."""
    _sync_positions(conn, account, rows, replace=True)
    found = conn.execute(
        """
        SELECT count(*) FROM (
            SELECT DISTINCT ON (symbol, sec_type, expiry, contract_right, strike) quantity
            FROM positions
            WHERE account_id = %s AND simulated = false
            ORDER BY symbol, sec_type, expiry, contract_right, strike, snapshot_at DESC, id DESC
        ) latest
        WHERE quantity <> 0
        """,
        (account,),
    ).fetchone()
    return int(found[0] if found else 0)


def _sync_positions(conn, account: str, rows: list[BrokerPosition], replace: bool = False) -> None:
    if not account:
        return
    mine = []
    for row in rows:
        if row.account not in {account, ""}:
            continue
        mine.append(row if row.account else row._replace(account=account))
    if rows and not mine:
        return
    seen = set()
    for row in mine:
        _insert_position(conn, row)
        seen.add(_position_key(row.symbol, row.sec_type, row.expiry, row.right, row.strike))
    if replace:
        _close_missing(conn, account, seen)
    if mine or replace:
        conn.commit()


def _submit_queued(conn, broker, slot: str, account: str) -> None:
    halted = active_reason(conn, account) is not None
    rows = conn.execute(
        """
        SELECT orders.id, drafts.status, drafts.instrument, orders.limit_price,
               orders.symbol, orders.action, orders.contracts, orders.account_id,
               orders.expiry, orders.strike, orders.contract_right,
               orders.trading_class, orders.option_exchange, orders.multiplier,
               orders.ib_order_id
        FROM orders
        JOIN drafts ON drafts.id = orders.draft_id
        WHERE orders.status = 'queued' AND orders.account_id = %s AND orders.simulated = false
        """,
        (account,),
    ).fetchall()
    for (
        order_id, status, instrument, limit_price, symbol, action, contracts, order_account,
        expiry, strike, right, trading_class, option_exchange, multiplier, ib_order_id,
    ) in rows:
        reason = live_block_reason(
            slot=slot,
            draft_status=status,
            halted=halted,
            account_id=order_account,
            instrument=instrument,
            limit_price=limit_price,
        )
        if reason in SOFT_BLOCKS:
            continue
        if reason:
            conn.execute(
                "UPDATE orders SET status = 'blocked', block_reason = %s WHERE id = %s",
                (reason, order_id),
            )
            conn.commit()
            _publish_order(account, order_id)
            log.info("order blocked id=%s reason=%s", order_id, reason)
            continue
        try:
            if action == "CANCEL":
                result = broker.cancel(int(ib_order_id))
                if result not in {"Cancelled", "ApiCancelled", "PendingCancel"}:
                    raise RuntimeError(f"IB did not cancel {ib_order_id} status={result}")
                conn.execute(
                    """
                    UPDATE orders SET status = 'cancelled'
                    WHERE id = %s OR (ib_order_id = %s AND status = 'submitted')
                    """,
                    (order_id, int(ib_order_id)),
                )
                conn.commit()
                _publish_order(account, order_id)
                log.info("order cancelled id=%s ib=%s", order_id, ib_order_id)
                continue
            if instrument == "OPT":
                ib_order_id = broker.place_option_limit(
                    account, symbol, expiry, strike, right, trading_class, option_exchange,
                    action, contracts, limit_price, multiplier or "100",
                )
            else:
                ib_order_id = broker.place_limit(account, symbol, action, contracts, limit_price)
        except RuntimeError as exc:
            conn.execute(
                "UPDATE orders SET status = 'blocked', block_reason = %s WHERE id = %s",
                (str(exc), order_id),
            )
            conn.commit()
            _publish_order(account, order_id)
            log.info("order blocked id=%s reason=%s", order_id, exc)
            continue
        conn.execute(
            """
            UPDATE orders
            SET status = 'submitted', ib_order_id = %s, account_id = %s
            WHERE id = %s
            """,
            (ib_order_id, account, order_id),
        )
        conn.commit()
        _publish_order(account, order_id)
        log.info("order submitted id=%s ib=%s", order_id, ib_order_id)


def _sync_order_status(conn, broker, account: str) -> list[tuple]:
    """Mark working orders from IB. A Filled status with no execution print is still reported."""
    noticed: list[tuple] = []
    changed: list[str] = []
    states = broker.order_states()
    if not states:
        return noticed
    rows = conn.execute(
        """
        SELECT id, ib_order_id, symbol, tactic, action, draft_id FROM orders
        WHERE account_id = %s AND status = 'submitted' AND ib_order_id IS NOT NULL
        """,
        (account,),
    ).fetchall()
    for order_id, ib_order_id, symbol, tactic, action, draft_id in rows:
        live = states.get(int(ib_order_id))
        if live is None:
            log.info("order unseen id=%s ib=%s", order_id, ib_order_id)
            continue
        if live == "Filled":
            conn.execute("UPDATE orders SET status = 'filled' WHERE id = %s", (order_id,))
            noticed.append((account, symbol, tactic, action, draft_id, None))
            changed.append(order_id)
            log.info("order filled id=%s ib=%s", order_id, ib_order_id)
        elif live in {"Cancelled", "ApiCancelled", "Inactive"}:
            conn.execute("UPDATE orders SET status = 'cancelled' WHERE id = %s", (order_id,))
            changed.append(order_id)
            log.info("order cancelled id=%s ib=%s status=%s", order_id, ib_order_id, live)
        else:
            log.info("order resting id=%s ib=%s status=%s", order_id, ib_order_id, live)
    conn.commit()
    for order_id in changed:
        _publish_order(account, order_id)
    return noticed


def _apply_fills(conn, account: str, fills: list[tuple]) -> list[tuple]:
    """Write new executions. A fill with no Athena order is stored as external."""
    noticed: list[tuple] = []
    for item in fills:
        ib_order_id, fill_account, symbol, quantity, price = item[:5]
        exec_id = str(item[5]) if len(item) > 5 and item[5] else f"{ib_order_id}:{symbol}:{quantity}:{price}"
        side = item[6] if len(item) > 6 else ""
        sec_type = item[7] if len(item) > 7 and item[7] else "STK"
        if fill_account and fill_account != account:
            continue
        if conn.execute(
            "SELECT 1 FROM fills WHERE account_id = %s AND exec_id = %s",
            (account, exec_id),
        ).fetchone():
            continue
        row = conn.execute(
            """
            SELECT id, draft_id, tactic, contracts, action
            FROM orders
            WHERE ib_order_id = %s AND account_id = %s AND status = 'submitted'
            """,
            (ib_order_id, account),
        ).fetchone()
        if row is None:
            signed = -quantity if side == "SLD" else quantity
            conn.execute(
                """
                INSERT INTO fills (draft_id, symbol, tactic, contracts, price, simulated, account_id, exec_id)
                VALUES (NULL, %s, 'EXTERNAL', %s, %s, false, %s, %s)
                """,
                (symbol, abs(int(quantity)), price, account, exec_id),
            )
            conn.execute(
                """
                INSERT INTO positions (account_id, symbol, quantity, avg_price, sec_type)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (account, symbol, signed, price, sec_type),
            )
            conn.commit()
            noticed.append((
                account, symbol, "EXTERNAL", "SELL" if side == "SLD" else "BUY", "", price, abs(int(quantity)),
            ))
            log.info("external fill symbol=%s price=%s", symbol, price)
            continue
        order_id, draft_id, tactic, contracts, action = row
        conn.execute(
            """
            INSERT INTO fills (draft_id, symbol, tactic, contracts, price, simulated, account_id, exec_id)
            VALUES (%s, %s, %s, %s, %s, false, %s, %s)
            """,
            (draft_id, symbol, tactic, contracts, price, account, exec_id),
        )
        conn.execute(
            """
            INSERT INTO positions (account_id, symbol, quantity, avg_price, sec_type)
            VALUES (%s, %s, %s, %s, 'STK')
            """,
            (account, symbol, quantity, price),
        )
        conn.execute("UPDATE orders SET status = 'filled' WHERE id = %s", (order_id,))
        conn.commit()
        _publish_order(account, order_id)
        noticed.append((account, symbol, tactic, action, draft_id, price))
        log.info("fill order=%s symbol=%s price=%s", order_id, symbol, price)
    return noticed


def _position_key(symbol: str, sec_type: str, expiry, right: str, strike) -> tuple:
    strike_key = None if strike is None else Decimal(strike).normalize()
    return (symbol, sec_type or "STK", expiry, right or "", strike_key)


def _close_missing(conn, account: str, seen: set[tuple]) -> None:
    rows = conn.execute(
        """
        SELECT DISTINCT ON (symbol, sec_type, expiry, contract_right, strike)
               symbol, sec_type, expiry, contract_right, strike, quantity
        FROM positions
        WHERE account_id = %s AND simulated = false
        ORDER BY symbol, sec_type, expiry, contract_right, strike, snapshot_at DESC, id DESC
        """,
        (account,),
    ).fetchall()
    for symbol, sec_type, expiry, right, strike, quantity in rows:
        if Decimal(quantity) == 0:
            continue
        if _position_key(symbol, sec_type, expiry, right, strike) in seen:
            continue
        conn.execute(
            """
            INSERT INTO positions (
                account_id, symbol, quantity, avg_price, sec_type, expiry,
                contract_right, strike, market_value, unrealized_pnl
            )
            VALUES (%s, %s, 0, 0, %s, %s, %s, %s, 0, 0)
            """,
            (account, symbol, sec_type or "STK", expiry, right or "", strike),
        )


def _insert_position(conn, row: BrokerPosition) -> None:
    conn.execute(
        """
        INSERT INTO positions (
            account_id, symbol, quantity, avg_price, sec_type, expiry,
            contract_right, strike, market_price, currency, multiplier,
            market_value, unrealized_pnl, realized_pnl
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            row.account,
            row.symbol,
            row.quantity,
            row.avg_price,
            row.sec_type,
            row.expiry,
            row.right,
            row.strike,
            row.market_price,
            row.currency,
            row.multiplier,
            row.market_value,
            row.unrealized_pnl,
            row.realized_pnl,
        ),
    )
