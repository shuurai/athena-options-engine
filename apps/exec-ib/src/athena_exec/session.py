"""Connect only in an Athena slot. Mobile and idle leave the database unchanged."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from decimal import Decimal

from athena_exec.broker import BrokerPosition, IbBroker
from athena_exec.gate import SOFT_BLOCKS, live_block_reason
from athena_exec.halt import active_reason, note_nav
from athena_exec.slots import HKT, slot_state
from athena_settings.model import SettingsPayload
from athena_exec.options import choose_expiry, choose_strikes

log = logging.getLogger("athena.exec")


def run_session(
    conn,
    settings: SettingsPayload,
    account: str,
    broker=None,
    now: datetime | None = None,
) -> None:
    broker = broker or IbBroker()
    moment = now or datetime.now(HKT)
    slot = slot_state(moment, settings.ib)
    if slot != "athena":
        broker.disconnect()
        log.info("ib_session=%s account=%s", "disconnected" if slot == "mobile" else "idle", account)
        return

    host = os.environ.get("IB_GATEWAY_HOST", settings.ib.gateway_host)
    port = int(os.environ.get("IB_GATEWAY_PORT", str(settings.ib.gateway_port)))
    client_id = int(os.environ.get("IB_CLIENT_ID", str(settings.ib.client_id)))
    if not broker.connect(host, port, client_id, False):
        log.info("ib_session=disconnected account=%s", account)
        return
    log.info("ib_session=connected account=%s", account)

    _cache_quotes(conn, broker, settings.watchlist)
    _cache_history(conn, broker, settings.watchlist, moment.date())
    _cache_strangles(conn, broker, settings.watchlist, moment.date())
    _sync_positions(conn, account, broker.positions())
    nav = broker.net_liquidation(account) if account else None
    if nav is not None:
        reason = note_nav(conn, account, nav, settings.risk.daily_loss_halt_pct, moment)
        if reason:
            log.info("halt=%s account=%s", reason, account)
    _submit_queued(conn, broker, slot, account)
    _apply_fills(conn, account, broker.fills())
    _sync_order_status(conn, broker, account)


def _cache_quotes(conn, broker, symbols: list[str]) -> None:
    hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    wrote = False
    for symbol in symbols:
        price = broker.last_price(symbol)
        if price is None:
            log.info("quote missing symbol=%s", symbol)
            continue
        conn.execute(
            """
            INSERT INTO market_bars (symbol, ts, open, high, low, close, volume, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'ib-delayed')
            ON CONFLICT (symbol, ts, source) DO UPDATE SET close = EXCLUDED.close
            """,
            (symbol, hour, price, price, price, price, 0),
        )
        wrote = True
        log.info("quote symbol=%s close=%s", symbol, price)
    if wrote:
        conn.commit()


def _cache_history(conn, broker, symbols: list[str], today) -> None:
    wrote = False
    for symbol in symbols:
        cached = conn.execute(
            """
            SELECT 1 FROM market_bars
            WHERE symbol = %s AND source = 'ib-daily' AND ts::date = %s
            """,
            (symbol, today),
        ).fetchone()
        if cached is not None:
            continue
        bars = broker.daily_history(symbol)
        if not bars:
            log.info("history missing symbol=%s", symbol)
            continue
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
            wrote = True
        log.info("history symbol=%s bars=%s", symbol, len(bars))
    if wrote:
        conn.commit()


def _cache_strangles(conn, broker, symbols: list[str], today) -> None:
    for symbol in symbols:
        price = conn.execute(
            """
            SELECT close FROM market_bars
            WHERE symbol = %s AND source = 'ib-delayed'
            ORDER BY ts DESC
            LIMIT 1
            """,
            (symbol,),
        ).fetchone()
        if price is None:
            continue
        chain = broker.option_chain(symbol)
        if chain is None:
            continue
        expiries, _strikes, trading_class, exchange, multiplier = chain
        expiry = choose_expiry(list(expiries), today)
        if expiry is None:
            log.info("strangle none symbol=%s", symbol)
            continue
        strikes = broker.expiry_strikes(symbol, expiry, trading_class, exchange)
        picked = choose_strikes(strikes, price[0])
        if picked is None:
            log.info("strangle none symbol=%s", symbol)
            continue
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


def _sync_positions(conn, account: str, rows: list[BrokerPosition]) -> None:
    if not account:
        return
    wrote = False
    for row in rows:
        if row.account != account:
            continue
        _insert_position(conn, row)
        wrote = True
    if wrote:
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
        WHERE orders.status = 'queued' AND orders.account_id = %s
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
        log.info("order submitted id=%s ib=%s", order_id, ib_order_id)


def _sync_order_status(conn, broker, account: str) -> None:
    states = broker.order_states()
    if not states:
        return
    rows = conn.execute(
        """
        SELECT id, ib_order_id FROM orders
        WHERE account_id = %s AND status = 'submitted' AND ib_order_id IS NOT NULL
        """,
        (account,),
    ).fetchall()
    for order_id, ib_order_id in rows:
        live = states.get(int(ib_order_id))
        if live is None:
            log.info("order unseen id=%s ib=%s", order_id, ib_order_id)
            continue
        if live == "Filled":
            conn.execute("UPDATE orders SET status = 'filled' WHERE id = %s", (order_id,))
            log.info("order filled id=%s ib=%s", order_id, ib_order_id)
        elif live in {"Cancelled", "ApiCancelled", "Inactive"}:
            conn.execute("UPDATE orders SET status = 'cancelled' WHERE id = %s", (order_id,))
            log.info("order cancelled id=%s ib=%s status=%s", order_id, ib_order_id, live)
        else:
            log.info("order resting id=%s ib=%s status=%s", order_id, ib_order_id, live)
    conn.commit()


def _apply_fills(conn, account: str, fills: list[tuple[int, str, str, Decimal, Decimal]]) -> None:
    for ib_order_id, fill_account, symbol, quantity, price in fills:
        if fill_account and fill_account != account:
            continue
        row = conn.execute(
            """
            SELECT id, draft_id, tactic, contracts
            FROM orders
            WHERE ib_order_id = %s AND status = 'submitted'
            """,
            (ib_order_id,),
        ).fetchone()
        if row is None:
            continue
        order_id, draft_id, tactic, contracts = row
        conn.execute(
            """
            INSERT INTO fills (draft_id, symbol, tactic, contracts, price, simulated, account_id)
            VALUES (%s, %s, %s, %s, %s, false, %s)
            """,
            (draft_id, symbol, tactic, contracts, price, account),
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
        log.info("fill order=%s symbol=%s price=%s", order_id, symbol, price)


def _insert_position(conn, row: BrokerPosition) -> None:
    conn.execute(
        """
        INSERT INTO positions (
            account_id, symbol, quantity, avg_price, sec_type, expiry,
            contract_right, strike, market_price
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
        ),
    )
