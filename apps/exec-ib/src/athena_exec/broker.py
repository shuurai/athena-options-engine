"""IB connection. placeOrder is refused while the session is read-only."""

from __future__ import annotations

import logging
import math
import threading
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Iterator, NamedTuple

log = logging.getLogger("athena.exec")


def _history_162_kind(message: str) -> str:
    """Classify HMDS 162 text: no_data, permission, or other."""
    text = (message or "").lower()
    if "permission" in text:
        return "permission"
    if "no data" in text or "hmds query returned no data" in text:
        return "no_data"
    return "other"


class BrokerPosition(NamedTuple):
    account: str
    symbol: str
    quantity: Decimal
    avg_price: Decimal
    sec_type: str
    expiry: date | None
    right: str
    strike: Decimal | None
    market_price: Decimal | None
    currency: str = ""
    multiplier: Decimal | None = None
    market_value: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    realized_pnl: Decimal | None = None


DISPLAY_CURRENCIES = ("HKD", "USD", "AUD", "CNY", "JPY", "CAD")
_FX_DENIED = {162, 354, 10089, 10186}


class _LineGate:
    """How many IB quote lines may be open. Further requests wait."""

    def __init__(self) -> None:
        self._limit = 2
        self._held = 0
        self._cv = threading.Condition()

    def set_limit(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("concurrent subscriptions must be at least 1")
        with self._cv:
            self._limit = limit
            self._cv.notify_all()

    @contextmanager
    def hold(self) -> Iterator[None]:
        with self._cv:
            while self._held >= self._limit:
                self._cv.wait()
            self._held += 1
        try:
            yield
        finally:
            with self._cv:
                self._held -= 1
                self._cv.notify()


_LINES = _LineGate()


def set_subscription_limit(limit: int) -> None:
    """Apply the settings cap before the next quote."""
    _LINES.set_limit(limit)


def subscription():
    """One open IB quote. Release it when the line is cancelled."""
    return _LINES.hold()


class IbBroker:
    def __init__(self) -> None:
        self._ib = None
        self.readonly = True
        self._fx_closed = False
        self._markets: dict[str, str] = {}
        self._quiet_history: set[str] = set()
        self._unknown_stock: set[str] = set()

    def set_markets(self, mapping: dict[str, str]) -> None:
        """Remember each name's market for this connection."""
        prior = dict(self._markets)
        self._markets = {key.upper(): value for key, value in mapping.items()}
        # Re-try a name when the operator changes us↔ca/hk after an Error 200.
        for key, kind in self._markets.items():
            if prior.get(key) != kind:
                self._unknown_stock.discard(key)

    def history_quiet(self, symbol: str) -> bool:
        """True after IB reported no option history for this name."""
        return symbol.upper() in self._quiet_history

    def note_history_gap(self, symbol: str, code: int) -> None:
        """Error 162 ends further option-history requests for this name."""
        if code != 162:
            return
        self._quiet_history.add(symbol.upper())
        log.info("option history quiet symbol=%s code=162", symbol.upper())

    def unknown_stock(self, symbol: str) -> bool:
        """True after IB Error 200 / failed qualify for this stock venue."""
        return symbol.upper() in self._unknown_stock

    def note_unknown_stock(self, symbol: str) -> None:
        """Stop retrying a name IB cannot resolve on the configured venue."""
        key = symbol.upper()
        if key in self._unknown_stock:
            return
        self._unknown_stock.add(key)
        exchange, currency = self._listed(symbol)
        kind = self._kind(symbol)
        log.warning(
            "unknown stock symbol=%s market=%s venue=%s/%s — "
            "set Universe Market to us/hk/ca (CAD names need ca), or remove foreign listings Athena cannot route",
            key, kind, exchange, currency,
        )

    def _kind(self, symbol: str) -> str:
        from athena_exec.slots import symbol_market

        return symbol_market(symbol, self._markets.get(symbol.upper(), ""))

    def _listed(self, symbol: str) -> tuple[str, str]:
        return stock_venue(symbol, self._kind(symbol))

    def _stock(self, symbol: str):
        return stock_contract(symbol, self._kind(symbol))

    def _qualify_stock(self, symbol: str):
        """Qualified stock contract, or None when IB has no definition on this venue."""
        if self._ib is None or self.unknown_stock(symbol):
            return None
        contract = self._stock(symbol)
        if not self._ib.qualifyContracts(contract):
            self.note_unknown_stock(symbol)
            return None
        return contract

    def connected(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    def connect(self, host: str, port: int, client_id: int, readonly: bool) -> bool:
        if self.connected() and self.readonly == readonly:
            return True
        self.disconnect()
        try:
            import nest_asyncio
            from ib_insync import IB

            nest_asyncio.apply()
            ib = IB()
            # 0 means wait forever. A hung quote must not pin the probe lock.
            ib.RequestTimeout = 30
            ib.connect(host, port, clientId=client_id, readonly=readonly, timeout=3)
        except Exception as exc:
            log.info("gateway down client_id=%s: %s", client_id, exc)
            self._ib = None
            return False
        self._ib = ib
        self.readonly = readonly
        self._fx_closed = False
        return True

    def disconnect(self) -> None:
        if self._ib is None:
            return
        try:
            self._ib.disconnect()
        except Exception as exc:
            log.info("gateway disconnect: %s", exc)
        self._ib = None

    def place_market(self, account: str, symbol: str, action: str, quantity: int, tif: str = "DAY") -> int:
        from ib_insync import MarketOrder

        return self._place_stock(account, symbol, MarketOrder(action, quantity), tif)

    def place_limit(
        self,
        account: str,
        symbol: str,
        action: str,
        quantity: int,
        limit_price: Decimal,
        tif: str = "DAY",
    ) -> int:
        from ib_insync import LimitOrder

        return self._place_stock(account, symbol, LimitOrder(action, quantity, float(limit_price)), tif)

    def place_stop_limit(
        self,
        account: str,
        symbol: str,
        action: str,
        quantity: int,
        limit_price: Decimal,
        stop_price: Decimal,
        tif: str = "DAY",
    ) -> int:
        from ib_insync import StopLimitOrder

        return self._place_stock(
            account,
            symbol,
            StopLimitOrder(action, quantity, float(limit_price), float(stop_price)),
            tif,
        )

    def _place_stock(self, account: str, symbol: str, order, tif: str = "DAY") -> int:
        if self.readonly or self._ib is None:
            raise RuntimeError("placeOrder refused on a read-only session")

        contract = self._qualify_stock(symbol)
        if contract is None:
            exchange, currency = self._listed(symbol)
            raise RuntimeError(f"IB did not find {symbol} on {exchange}/{currency}")
        order.account = account
        order.tif = "GTC" if str(tif).upper() == "GTC" else "DAY"
        trade = self._ib.placeOrder(contract, order)
        status = ""
        for _ in range(5):
            self._ib.sleep(1)
            status = trade.orderStatus.status or ""
            if status and status not in {"PendingSubmit", "ApiPending"}:
                break
        if status in {"Cancelled", "Inactive", "ApiCancelled"} or not trade.order.orderId:
            detail = "; ".join(entry.message for entry in trade.log if entry.message)
            raise RuntimeError(f"IB did not accept the order status={status or 'none'} {detail}".strip())
        return int(trade.order.orderId)

    def last_price(self, symbol: str) -> Decimal | None:
        """Delayed last or close. Live HK quotes are not subscribed on this account."""
        contract = self._qualify_stock(symbol)
        if contract is None:
            return None
        return self._read_quote(contract)

    def quote(self, kind: str, symbol: str) -> Decimal | None:
        """Delayed last for one named kind. None when this session is not connected."""
        if self._ib is None:
            return None
        fields = quote_fields(kind, symbol)
        if isinstance(fields, str):
            return None
        contract = _quote_contract(fields)
        if not self._ib.qualifyContracts(contract):
            return None
        return self._read_quote(contract)

    def _read_quote(self, contract) -> Decimal | None:
        self._ib.reqMarketDataType(3)
        with subscription():
            ticker = self._ib.reqMktData(contract, "", False, False)
            self._ib.sleep(2)
            price = _ticker_price(ticker)
            self._ib.cancelMktData(contract)
        if price is not None:
            return price
        return _daily_close(self._ib, contract)

    def daily_history(self, symbol: str) -> list:
        """Up to 60 daily bars. Empty when IB has no history for this name."""
        contract = self._qualify_stock(symbol)
        if contract is None:
            return []
        with subscription():
            bars = self._ib.reqHistoricalData(contract, "", "60 D", "1 day", "TRADES", True, 1)
        return list(bars or [])

    def head_day(self, symbol: str) -> date | None:
        """Earliest IB TRADES day for this stock. None when IB does not answer."""
        contract = self._qualify_stock(symbol)
        if contract is None:
            return None
        try:
            stamp = self._ib.reqHeadTimeStamp(contract, "TRADES", True, 2)
        except Exception as exc:
            log.info("head day symbol=%s error=%s", symbol.upper(), exc.__class__.__name__)
            return None
        return _as_head_day(stamp)

    def history_span(self, symbol: str, duration: str, end: str = "") -> list[tuple]:
        """Daily TRADES bars for one IB duration, such as '1 Y'.

        end is an IB endDateTime. Empty means now, so a second year can stop
        where the first year starts. Error 162 with no data returns [].
        """
        contract = self._qualify_stock(symbol)
        if contract is None:
            return []
        notes: list[tuple[int, str]] = []

        def hear(_req_id, code, message, _contract) -> None:
            if code == 162:
                notes.append((code, str(message or "")))

        self._ib.errorEvent += hear
        bars = []
        try:
            with subscription():
                bars = self._ib.reqHistoricalData(contract, end, duration, "1 day", "TRADES", True, 1)
        finally:
            self._ib.errorEvent -= hear
        if notes:
            kind = _history_162_kind(notes[0][1])
            if kind == "permission":
                log.warning(
                    "history span permission symbol=%s duration=%s end=%s",
                    symbol.upper(),
                    duration,
                    end or "now",
                )
            else:
                log.info(
                    "history span empty symbol=%s duration=%s end=%s kind=%s",
                    symbol.upper(),
                    duration,
                    end or "now",
                    kind,
                )
            return []
        found = []
        for bar in bars or []:
            found.append(
                (
                    bar.date,
                    Decimal(str(bar.open)),
                    Decimal(str(bar.high)),
                    Decimal(str(bar.low)),
                    Decimal(str(bar.close)),
                    Decimal(str(bar.volume or 0)),
                )
            )
        return found

    def historical(self, contract, duration: str, bar_size: str, end: str = "") -> list[dict]:
        """TRADES candles. An option with no trades falls back to the midpoint."""
        if self._ib is None:
            raise RuntimeError("IB Gateway did not connect.")
        if not self._found(contract):
            label = contract.localSymbol or contract.symbol
            raise RuntimeError(f"IB did not find {label}")
        what = "TRADES"
        with subscription():
            bars = self._ib.reqHistoricalData(contract, end, duration, bar_size, what, True, 1)
            if not bars and getattr(contract, "secType", "") == "OPT":
                what = "MIDPOINT"
                bars = self._ib.reqHistoricalData(contract, end, duration, bar_size, what, True, 1)
        return [_candle(bar) for bar in bars or []]

    def describe(self, contract) -> dict:
        """Qualified contract facts. longName comes from the contract details."""
        if self._ib is None:
            raise RuntimeError("IB Gateway did not connect.")
        if not self._found(contract):
            label = contract.localSymbol or contract.symbol
            raise RuntimeError(f"IB did not find {label}")
        details = self._ib.reqContractDetails(contract)
        long_name = ""
        primary = ""
        if details:
            long_name = details[0].longName or ""
            primary = details[0].contract.primaryExchange or ""
            contract = details[0].contract
        return {
            "symbol": contract.symbol,
            "sec_type": contract.secType or "",
            "exchange": contract.exchange or "",
            "primary_exchange": primary,
            "currency": contract.currency or "",
            "name": long_name,
            "con_id": int(contract.conId or 0),
            "trading_class": contract.tradingClass or "",
        }

    def _found(self, contract) -> bool:
        if self._ib.qualifyContracts(contract):
            return True
        if getattr(contract, "secType", "") == "OPT" and contract.exchange == "SMART":
            contract.exchange = "CBOE"
            return bool(self._ib.qualifyContracts(contract))
        return False

    def place_option_limit(
        self,
        account: str,
        symbol: str,
        expiry: date,
        strike: Decimal,
        right: str,
        trading_class: str,
        exchange: str,
        action: str,
        quantity: int,
        limit_price: Decimal,
        multiplier: str = "100",
    ) -> int:
        if self.readonly or self._ib is None:
            raise RuntimeError("placeOrder refused on a read-only session")
        from ib_insync import LimitOrder

        currency = {"hk": "HKD", "ca": "CAD"}.get(self._kind(symbol), "USD")
        contract = _option(symbol, expiry, strike, right, trading_class, exchange, currency, multiplier)
        if not self._ib.qualifyContracts(contract):
            raise RuntimeError(f"IB did not find {symbol} {right} {strike} {expiry}")
        order = LimitOrder(action, quantity, float(limit_price))
        order.account = account
        order.tif = "DAY"
        trade = self._ib.placeOrder(contract, order)
        status = ""
        for _ in range(5):
            self._ib.sleep(1)
            status = trade.orderStatus.status or ""
            if status and status not in {"PendingSubmit", "ApiPending"}:
                break
        if status in {"Cancelled", "Inactive", "ApiCancelled"} or not trade.order.orderId:
            detail = "; ".join(entry.message for entry in trade.log if entry.message)
            raise RuntimeError(f"IB did not accept the order status={status or 'none'} {detail}".strip())
        return int(trade.order.orderId)

    def option_chain(self, symbol: str):
        """Listed expiries, strikes, trading class, and exchange. Empty when none exist."""
        stock = self._qualify_stock(symbol)
        if stock is None:
            return None
        chains = self._ib.reqSecDefOptParams(stock.symbol, "", stock.secType, stock.conId)
        if not chains:
            return None
        chain = _pick_chain(chains)
        return chain.expirations, chain.strikes, chain.tradingClass, chain.exchange, str(chain.multiplier)

    def expiry_strikes(self, symbol: str, expiry: date, trading_class: str, exchange: str) -> list[float]:
        """Strikes on this expiry and trading class. The full chain mixes months and classes."""
        if self._ib is None:
            return []
        from ib_insync import Option

        currency = {"hk": "HKD", "ca": "CAD"}.get(self._kind(symbol), "USD")
        contract = Option(
            symbol,
            expiry.strftime("%Y%m%d"),
            exchange=exchange,
            currency=currency,
        )
        if trading_class:
            contract.tradingClass = trading_class
        return _class_strikes(self._ib.reqContractDetails(contract), trading_class)

    def option_quote(
        self,
        symbol: str,
        expiry: date,
        strike: Decimal,
        right: str,
        trading_class: str,
        exchange: str,
        multiplier: str = "100",
    ) -> tuple[Decimal | None, Decimal | None]:
        """Delayed ask and bid from one ticker. The bid is the positive bid only."""
        if self._ib is None:
            return None, None
        currency = {"hk": "HKD", "ca": "CAD"}.get(self._kind(symbol), "USD")
        contract = _option(symbol, expiry, strike, right, trading_class, exchange, currency, multiplier)
        if not self._ib.qualifyContracts(contract):
            return None, None
        self._ib.reqMarketDataType(3)
        with subscription():
            ticker = self._ib.reqMktData(contract, "", False, False)
            self._ib.sleep(2)
            ask = _ask(ticker)
            bid = _positive(ticker.bid)
            self._ib.cancelMktData(contract)
        if ask is None and self._kind(symbol) == "us" and not self.history_quiet(symbol):
            ask, code = _cboe_close(self._ib, symbol, expiry, strike, right, trading_class, multiplier)
            self.note_history_gap(symbol, code)
        return ask, bid

    def option_ask(
        self,
        symbol: str,
        expiry: date,
        strike: Decimal,
        right: str,
        trading_class: str,
        exchange: str,
        multiplier: str = "100",
    ) -> Decimal | None:
        """Delayed ask. A sell limit at the ask rests instead of crossing the bid."""
        ask, _bid = self.option_quote(
            symbol, expiry, strike, right, trading_class, exchange, multiplier,
        )
        return ask

    def qualifies_stock(self, symbol: str) -> bool:
        """True when IB accepts this stock symbol on its venue."""
        return self._qualify_stock(symbol) is not None

    def option_mid(
        self,
        symbol: str,
        expiry: date,
        strike: Decimal,
        right: str,
        trading_class: str,
        exchange: str,
        multiplier: str = "100",
    ) -> Decimal | None:
        """Delayed (bid + ask) / 2. Missing either side writes nothing."""
        if self._ib is None:
            return None
        currency = {"hk": "HKD", "ca": "CAD"}.get(self._kind(symbol), "USD")
        contract = _option(symbol, expiry, strike, right, trading_class, exchange, currency, multiplier)
        if not self._ib.qualifyContracts(contract):
            return None
        self._ib.reqMarketDataType(3)
        with subscription():
            ticker = self._ib.reqMktData(contract, "", False, False)
            self._ib.sleep(2)
            bid = _positive(ticker.bid)
            ask = _positive(ticker.ask)
            self._ib.cancelMktData(contract)
        if bid is None or ask is None:
            return None
        return (bid + ask) / Decimal(2)

    def order_states(self) -> dict[int, str]:
        """Status of each open order on this login, including orders from client 1."""
        if self._ib is None:
            return {}
        self._ib.reqAllOpenOrders()
        self._ib.sleep(1)
        states: dict[int, str] = {}
        for trade in self._ib.trades():
            order_id = int(trade.order.orderId or 0)
            if order_id:
                states[order_id] = trade.orderStatus.status or ""
        return states

    def cancel(self, ib_order_id: int) -> str:
        if self._ib is None:
            return "not_connected"
        self._ib.reqAllOpenOrders()
        self._ib.sleep(1)
        for trade in self._ib.openTrades():
            if trade.order.orderId != ib_order_id:
                continue
            self._ib.cancelOrder(trade.order)
            self._ib.sleep(2)
            return trade.orderStatus.status or "cancel_sent"
        return "not_open"

    def positions(self, account: str = "") -> list[BrokerPosition]:
        """Open book. An account id waits for that account's prices before reading."""
        if self._ib is None:
            return []
        self._load_portfolio(account)
        books = {
            item.contract.conId: item
            for item in (self._ib.portfolio(account) if account else self._ib.portfolio())
        }
        seen: set[int] = set()
        rows: list[BrokerPosition] = []
        held = self._ib.positions(account) if account else self._ib.positions()
        for item in held:
            seen.add(item.contract.conId)
            rows.append(
                _broker_position(
                    item.account,
                    item.contract,
                    item.position,
                    item.avgCost,
                    None,
                    books.get(item.contract.conId),
                )
            )
        for item in books.values():
            if item.contract.conId in seen:
                continue
            rows.append(
                _broker_position(
                    item.account,
                    item.contract,
                    item.position,
                    item.averageCost,
                    item.marketPrice,
                    item,
                )
            )
        return rows

    def _load_portfolio(self, account: str) -> None:
        """IB sends mark, value, and open P&L only after an account update."""
        if not account or self._ib is None:
            return
        try:
            self._ib.reqAccountUpdates(account)
        except (TimeoutError, RuntimeError) as exc:
            log.info("account updates missing account=%s error=%s", account, exc.__class__.__name__)

    def net_liquidation(self, account_id: str) -> Decimal | None:
        """Account net liquidation. Paper books here are in HKD, not only USD."""
        return self._account_number(account_id, "NetLiquidation")

    def cash(self, account_id: str) -> Decimal | None:
        return self._account_number(account_id, "TotalCashValue")

    def excess_liquidity(self, account_id: str) -> Decimal | None:
        """IB soft buffer before liquidation. Prefer this over TotalCashValue on margin."""
        return self._account_number(account_id, "ExcessLiquidity")

    def cash_ledger(self, account_id: str) -> tuple[str, dict[str, Decimal], dict[str, Decimal]]:
        """Base currency, cash by currency, and how many base units buy one unit.

        Cash can be negative on a margin book. BASE is the consolidated total, so it
        stays out of the per-currency rows.
        """
        if self._ib is None:
            return "", {}, {}
        balances: dict[str, Decimal] = {}
        rates: dict[str, Decimal] = {}
        for item in self._ib.accountValues(account_id):
            if item.currency in ("", "BASE") or item.value in ("", None):
                continue
            number = _number(item.value)
            if number is None:
                continue
            if item.tag == "CashBalance":
                balances[item.currency] = number
            elif item.tag == "ExchangeRate":
                rates[item.currency] = number
        base = next((code for code, rate in rates.items() if rate == 1), "")
        return base, balances, rates

    def fill_rates(
        self,
        base: str,
        rates: dict[str, Decimal],
        wanted: tuple[str, ...] = DISPLAY_CURRENCIES,
    ) -> dict[str, Decimal]:
        """Add base-per-unit rates for display currencies the ledger does not price.

        A rate is how many units of `base` buy one unit of the code, the same
        shape as the account ExchangeRate tag. One refused forex contract stops
        further pair requests on this connection.
        """
        filled = dict(rates)
        if not base:
            return filled
        filled[base] = Decimal(1)
        for code in wanted:
            if code == base or filled.get(code, 0) > 0:
                continue
            rate = self._rate_in_base(code, base, filled)
            if rate is not None and rate > 0:
                filled[code] = rate
                log.info("fx pair %s quoted", code)
        return filled

    def _rate_in_base(self, code: str, base: str, known: dict[str, Decimal]) -> Decimal | None:
        symbols = ("CNH", "CNY") if code == "CNY" else (code,)
        for symbol in symbols:
            rate = self._pair_or_cross(symbol, base, known)
            if rate is not None:
                return rate
        return None

    def _pair_or_cross(self, code: str, base: str, known: dict[str, Decimal]) -> Decimal | None:
        if self._fx_closed:
            return None
        direct = self._fx_price(code, base)
        if direct is not None:
            return direct
        if self._fx_closed:
            return None
        inverse = self._fx_price(base, code)
        if inverse is not None:
            return Decimal(1) / inverse
        if self._fx_closed:
            return None
        usd = known.get("USD")
        if usd is None or usd <= 0 or code in ("USD", base) or base == "USD":
            return None
        via_usd = self._fx_price(code, "USD")
        if via_usd is not None:
            return via_usd * usd
        if self._fx_closed:
            return None
        usd_per_code = self._fx_price("USD", code)
        if usd_per_code is not None and usd_per_code > 0:
            return usd / usd_per_code
        return None

    def _fx_price(self, left: str, right: str) -> Decimal | None:
        """Last midpoint of left.right, in units of `right` per 1 `left`."""
        if self._ib is None or self._fx_closed or not left or not right or left == right:
            return None
        from ib_insync import Forex

        log.info("fx %s%s", left, right)
        contract = Forex(left + right)
        try:
            found = self._ib.qualifyContracts(contract)
        except Exception as exc:
            log.info("fx pair %s%s: %s", left, right, exc.__class__.__name__)
            return None
        if not found:
            return None
        denied: list[int] = []

        def hear(_req_id, code, _message, _contract) -> None:
            if code in _FX_DENIED:
                denied.append(code)

        self._ib.errorEvent += hear
        bars = []
        try:
            with subscription():
                bars = self._ib.reqHistoricalData(
                    contract, "", "1 D", "1 min", "MIDPOINT", False, 1
                )
        except Exception as exc:
            log.info("fx pair %s%s: %s", left, right, exc.__class__.__name__)
        finally:
            self._ib.errorEvent -= hear
        if denied:
            self._fx_closed = True
            log.info("fx feed closed code=%s", denied[0])
            return None
        if not bars:
            return None
        close = bars[-1].close
        if close is None or not math.isfinite(close) or close <= 0:
            return None
        return Decimal(str(close))

    def margin(self, account_id: str) -> Decimal | None:
        return self._account_number(account_id, "MaintMarginReq")

    def _account_number(self, account_id: str, tag: str) -> Decimal | None:
        if self._ib is None:
            return None
        values = [item for item in self._ib.accountValues(account_id) if item.tag == tag and item.value not in ("", None)]
        for currency in ("USD", "BASE"):
            for item in values:
                if item.currency == currency:
                    return Decimal(item.value)
        if not values:
            return None
        return Decimal(values[0].value)

    def fills(self) -> list[tuple]:
        if self._ib is None:
            return []
        rows: list[tuple] = []
        for fill in self._ib.fills():
            report = getattr(fill, "commissionReport", None)
            commission = _fill_commission(report)
            currency = str(getattr(report, "currency", "") or "") if report is not None else ""
            rows.append(
                (
                    int(fill.execution.orderId),
                    fill.execution.acctNumber,
                    fill.contract.symbol,
                    Decimal(str(fill.execution.shares)),
                    Decimal(str(fill.execution.price)),
                    str(fill.execution.execId or ""),
                    str(fill.execution.side or ""),
                    str(fill.contract.secType or "STK"),
                    commission,
                    currency,
                )
            )
        return rows


def _class_strikes(details, trading_class: str) -> list[float]:
    """Keep strikes from this trading class. A different class is not that contract."""
    strikes = []
    for detail in details or []:
        contract = detail.contract
        listed = contract.tradingClass or ""
        if trading_class and listed and listed != trading_class:
            continue
        strikes.append(contract.strike)
    return strikes


def _option(
    symbol: str,
    expiry: date,
    strike: Decimal,
    right: str,
    trading_class: str,
    exchange: str,
    currency: str,
    multiplier: str,
):
    from ib_insync import Option

    return Option(
        symbol,
        expiry.strftime("%Y%m%d"),
        float(strike),
        right,
        exchange,
        currency=currency,
        multiplier=str(multiplier),
        tradingClass=trading_class,
    )


def _as_head_day(stamp) -> date | None:
    """Parse reqHeadTimeStamp result into a calendar day."""
    if stamp is None or stamp == "":
        return None
    if isinstance(stamp, datetime):
        return stamp.date()
    if isinstance(stamp, date):
        return stamp
    text = str(stamp).strip()
    if not text:
        return None
    if len(text) >= 8 and text[:8].isdigit():
        try:
            return datetime.strptime(text[:8], "%Y%m%d").date()
        except ValueError:
            pass
    if text[:10].count("-") == 2:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            pass
    if text.isdigit():
        try:
            return datetime.fromtimestamp(int(text), tz=UTC).date()
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _daily_close(ib, contract) -> Decimal | None:
    with subscription():
        bars = ib.reqHistoricalData(contract, "", "5 D", "1 day", "TRADES", True, 1)
    if not bars or not bars[-1].close or bars[-1].close <= 0:
        return None
    return Decimal(str(bars[-1].close))


def _cboe_close(ib, symbol: str, expiry: date, strike: Decimal, right: str, trading_class: str, multiplier: str) -> tuple[Decimal | None, int]:
    """SMART has no option history on this login. The CBOE print is the sell reference.

    Error 162 means this name has no history to walk. The caller stops the rest of its strikes.
    """
    contract = _option(symbol, expiry, strike, right, trading_class, "CBOE", "USD", multiplier)
    if not ib.qualifyContracts(contract):
        return None, 0
    notes: list[int] = []

    def hear(_req_id, code, _message, _contract) -> None:
        if code == 162:
            notes.append(code)

    ib.errorEvent += hear
    bars = []
    try:
        with subscription():
            bars = ib.reqHistoricalData(contract, "", "1 D", "1 hour", "TRADES", False, 1)
    finally:
        ib.errorEvent -= hear
    if notes:
        return None, 162
    if not bars or not bars[-1].close or bars[-1].close <= 0:
        return None, 0
    return Decimal(str(bars[-1].close)), 0


def _number(value) -> Decimal | None:
    """A finite number. Open P&L and market value may be negative."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return Decimal(str(number))


def _fill_commission(report) -> Decimal | None:
    """IB commission when the report has arrived. Missing or UNSET stays blank."""
    if report is None:
        return None
    number = _number(getattr(report, "commission", None))
    if number is None or abs(number) > Decimal("1e10"):
        return None
    return abs(number)


def _positive(value) -> Decimal | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or number <= 0:
        return None
    return Decimal(str(number))


def _ask(ticker) -> Decimal | None:
    """Ask when subscribed. Otherwise the higher of the last print and the close."""
    ask = _positive(ticker.ask)
    if ask is not None:
        return ask
    prints = [price for price in (_positive(ticker.last), _positive(ticker.close)) if price is not None]
    if not prints:
        return None
    return max(prints)


def _ticker_price(ticker) -> Decimal | None:
    for value in (ticker.marketPrice(), ticker.last, ticker.close):
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isnan(number) or number <= 0:
            continue
        return Decimal(str(number))
    return None


def _pick_chain(chains):
    """US names list many venues. SMART is the one the order can use."""
    for chain in chains:
        if chain.exchange == "SMART":
            return chain
    return max(chains, key=lambda chain: len(chain.strikes))


def _candle(bar) -> dict:
    raw = bar.date
    if isinstance(raw, datetime):
        day = raw.date().isoformat()
        stamp = raw.isoformat()
    elif isinstance(raw, date):
        day = raw.isoformat()
        stamp = day
    else:
        text = str(raw)
        compact = text[:10].replace("-", "")
        day = f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}" if len(compact) >= 8 and compact[:8].isdigit() else text[:10]
        stamp = text
    return {
        "date": day,
        "time": stamp,
        "open": float(bar.open),
        "high": float(bar.high),
        "low": float(bar.low),
        "close": float(bar.close),
        "volume": float(bar.volume or 0),
    }


def stock_venue(symbol: str, market: str = "") -> tuple[str, str]:
    """Venue and currency for one listed name. A blank market infers Hong Kong from a numeric code.

    Athena markets are us / hk / ca only. Letter tickers default to us (SMART/USD).
    Canadian listings must set Universe Market = ca (SMART/CAD, primary TSE).
    European listings (e.g. IFX on Xetra) are not routed — use a US ADR or remove them.
    """
    from athena_exec.slots import symbol_market

    kind = symbol_market(symbol, market)
    if kind == "hk":
        return "SEHK", "HKD"
    if kind == "ca":
        return "SMART", "CAD"
    return "SMART", "USD"


def stock_contract(symbol: str, market: str = ""):
    """ib_insync Stock for Athena's us / hk / ca venues."""
    from ib_insync import Stock
    from athena_exec.slots import symbol_market

    kind = symbol_market(symbol, market)
    code = str(symbol).strip().upper()
    if kind == "hk":
        return Stock(code, "SEHK", "HKD")
    if kind == "ca":
        # TSE primary disambiguates SMART CAD names for IB.
        return Stock(code, "SMART", "CAD", primaryExchange="TSE")
    return Stock(code, "SMART", "USD")


def quote_fields(kind: str, symbol: str) -> dict | str:
    """Contract fields for one quote command, or a sentence when the symbol does not fit."""
    raw = symbol.strip().upper()
    if kind in {"stock", "etf"}:
        if raw.isdigit():
            return "Use /hk for a Hong Kong stock."
        if not raw.isalpha():
            example = "AAPL" if kind == "stock" else "SPY"
            return f"Use letters, such as {example}."
        label = "stock" if kind == "stock" else "ETF"
        return {
            "kind": kind, "symbol": raw, "show": raw, "label": label,
            "exchange": "SMART", "currency": "USD", "sec": "STK",
        }
    if kind == "hk":
        if not raw.isdigit():
            return "Use the SEHK number, such as 700."
        code = raw.lstrip("0") or "0"
        return {
            "kind": "hk", "symbol": code, "show": code, "label": "HK stock",
            "exchange": "SEHK", "currency": "HKD", "sec": "STK",
        }
    if kind == "fx":
        pair = raw.replace("/", "")
        if len(pair) != 6 or not pair.isalpha():
            return "Use a pair such as EURUSD or EUR/USD."
        return {
            "kind": "fx", "symbol": pair, "show": pair, "label": "FX",
            "exchange": "IDEALPRO", "currency": pair[3:], "sec": "CASH", "base": pair[:3],
        }
    return "Unknown quote."


def _quote_contract(fields: dict):
    if fields["sec"] == "CASH":
        from ib_insync import Forex

        return Forex(fields["symbol"])
    from ib_insync import Stock

    return Stock(fields["symbol"], fields["exchange"], fields["currency"])


def _broker_position(account: str, contract, quantity, avg_cost, market_price, book=None) -> BrokerPosition:
    multiplier = Decimal(str(contract.multiplier or "1"))
    avg = _number(avg_cost) or Decimal(0)
    mark = _positive(market_price)
    if book is not None and mark is None:
        mark = _positive(book.marketPrice)
    value = unrealized = realized = None
    if book is not None:
        value = _number(book.marketValue)
        unrealized = _number(book.unrealizedPNL)
        realized = _number(book.realizedPNL)
    if mark is None and value is not None:
        scale = Decimal(str(quantity)) * multiplier
        if scale != 0:
            derived = value / scale
            if derived > 0:
                mark = derived
    if contract.secType == "OPT" and multiplier > 1 and mark is not None and avg > mark * multiplier / 2:
        avg = avg / multiplier
    strike = contract.strike
    return BrokerPosition(
        account,
        contract.symbol,
        Decimal(str(quantity)),
        avg,
        contract.secType or "STK",
        _expiry(getattr(contract, "lastTradeDateOrContractMonth", None)),
        contract.right or "",
        None if strike in (None, 0) else Decimal(str(strike)),
        mark,
        contract.currency or "",
        multiplier,
        value,
        unrealized,
        realized,
    )


def _expiry(text: str | None) -> date | None:
    raw = (text or "").strip()
    if len(raw) >= 8 and raw[:8].isdigit():
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    return None
