"""IB connection. placeOrder is refused while the session is read-only."""

from __future__ import annotations

import logging
import math
from datetime import date, datetime
from decimal import Decimal
from typing import NamedTuple

log = logging.getLogger("athena.exec")


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


class IbBroker:
    def __init__(self) -> None:
        self._ib = None
        self.readonly = True
        self._fx_closed = False

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
            ib.connect(host, port, clientId=client_id, readonly=readonly, timeout=3)
        except Exception as exc:
            log.info("gateway down: %s", exc)
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

    def place_limit(
        self,
        account: str,
        symbol: str,
        action: str,
        quantity: int,
        limit_price: Decimal,
    ) -> int:
        if self.readonly or self._ib is None:
            raise RuntimeError("placeOrder refused on a read-only session")
        from ib_insync import LimitOrder, Stock

        exchange, currency = stock_venue(symbol)
        contract = Stock(symbol, exchange, currency)
        qualified = self._ib.qualifyContracts(contract)
        if not qualified:
            raise RuntimeError(f"IB did not find {symbol} on {exchange}")
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

    def last_price(self, symbol: str) -> Decimal | None:
        """Delayed last or close. Live HK quotes are not subscribed on this account."""
        if self._ib is None:
            return None
        from ib_insync import Stock

        exchange, currency = stock_venue(symbol)
        contract = Stock(symbol, exchange, currency)
        if not self._ib.qualifyContracts(contract):
            return None
        self._ib.reqMarketDataType(3)
        ticker = self._ib.reqMktData(contract, "", False, False)
        self._ib.sleep(2)
        price = _ticker_price(ticker)
        self._ib.cancelMktData(contract)
        if price is not None:
            return price
        return _daily_close(self._ib, contract)

    def daily_history(self, symbol: str) -> list:
        """Up to 60 daily bars. Empty when IB has no history for this name."""
        if self._ib is None:
            return []
        from ib_insync import Stock

        exchange, currency = stock_venue(symbol)
        contract = Stock(symbol, exchange, currency)
        if not self._ib.qualifyContracts(contract):
            return []
        bars = self._ib.reqHistoricalData(contract, "", "60 D", "1 day", "TRADES", True, 1)
        return list(bars or [])

    def history_span(self, symbol: str, duration: str) -> list[tuple]:
        """Daily TRADES bars for one IB duration, such as '2 Y'. Empty when none exist."""
        if self._ib is None:
            return []
        from ib_insync import Stock

        exchange, currency = stock_venue(symbol)
        contract = Stock(symbol, exchange, currency)
        if not self._ib.qualifyContracts(contract):
            return []
        bars = self._ib.reqHistoricalData(contract, "", duration, "1 day", "TRADES", True, 1)
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

        currency = "HKD" if symbol.isdigit() else "USD"
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
        if self._ib is None:
            return None
        from ib_insync import Stock

        exchange, currency = stock_venue(symbol)
        stock = Stock(symbol, exchange, currency)
        if not self._ib.qualifyContracts(stock):
            return None
        chains = self._ib.reqSecDefOptParams(stock.symbol, "", stock.secType, stock.conId)
        if not chains:
            return None
        chain = _pick_chain(chains)
        return chain.expirations, chain.strikes, chain.tradingClass, chain.exchange, str(chain.multiplier)

    def expiry_strikes(self, symbol: str, expiry: date, trading_class: str, exchange: str) -> list[float]:
        """Strikes that exist on this expiry. The chain list mixes every month."""
        if self._ib is None:
            return []
        from ib_insync import Option

        currency = "HKD" if symbol.isdigit() else "USD"
        details = self._ib.reqContractDetails(
            Option(
                symbol,
                expiry.strftime("%Y%m%d"),
                exchange=exchange,
                currency=currency,
            )
        )
        return [detail.contract.strike for detail in details]

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
        if self._ib is None:
            return None
        currency = "HKD" if symbol.isdigit() else "USD"
        contract = _option(symbol, expiry, strike, right, trading_class, exchange, currency, multiplier)
        if not self._ib.qualifyContracts(contract):
            return None
        self._ib.reqMarketDataType(3)
        ticker = self._ib.reqMktData(contract, "", False, False)
        self._ib.sleep(2)
        price = _ask(ticker)
        self._ib.cancelMktData(contract)
        if price is not None:
            return price
        if currency != "USD":
            return None
        return _cboe_close(self._ib, symbol, expiry, strike, right, trading_class, multiplier)

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

    def positions(self) -> list[BrokerPosition]:
        if self._ib is None:
            return []
        books = {item.contract.conId: item for item in self._ib.portfolio()}
        seen: set[int] = set()
        rows: list[BrokerPosition] = []
        for item in self._ib.positions():
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
        for item in self._ib.portfolio():
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

    def net_liquidation(self, account_id: str) -> Decimal | None:
        """Account net liquidation. Paper books here are in HKD, not only USD."""
        return self._account_number(account_id, "NetLiquidation")

    def cash(self, account_id: str) -> Decimal | None:
        return self._account_number(account_id, "TotalCashValue")

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
                )
            )
        return rows


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


def _daily_close(ib, contract) -> Decimal | None:
    bars = ib.reqHistoricalData(contract, "", "5 D", "1 day", "TRADES", True, 1)
    if not bars or not bars[-1].close or bars[-1].close <= 0:
        return None
    return Decimal(str(bars[-1].close))


def _cboe_close(ib, symbol: str, expiry: date, strike: Decimal, right: str, trading_class: str, multiplier: str) -> Decimal | None:
    """SMART has no option history on this login. The CBOE print is the sell reference."""
    contract = _option(symbol, expiry, strike, right, trading_class, "CBOE", "USD", multiplier)
    if not ib.qualifyContracts(contract):
        return None
    bars = ib.reqHistoricalData(contract, "", "1 D", "1 hour", "TRADES", False, 1)
    if not bars or not bars[-1].close or bars[-1].close <= 0:
        return None
    return Decimal(str(bars[-1].close))


def _number(value) -> Decimal | None:
    """A finite number. Open P&L and market value may be negative."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return Decimal(str(number))


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


def stock_venue(symbol: str) -> tuple[str, str]:
    """Hong Kong numeric codes trade on SEHK in HKD. Other names stay US stocks."""
    if symbol.isdigit():
        return "SEHK", "HKD"
    return "SMART", "USD"


def _broker_position(account: str, contract, quantity, avg_cost, market_price, book=None) -> BrokerPosition:
    multiplier = Decimal(str(contract.multiplier or "1"))
    avg = _number(avg_cost) or Decimal(0)
    mark = _positive(market_price)
    if book is not None and mark is None:
        mark = _positive(book.marketPrice)
    if contract.secType == "OPT" and multiplier > 1 and mark is not None and avg > mark * multiplier / 2:
        avg = avg / multiplier
    strike = contract.strike
    value = unrealized = realized = None
    if book is not None:
        value = _number(book.marketValue)
        unrealized = _number(book.unrealizedPNL)
        realized = _number(book.realizedPNL)
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
