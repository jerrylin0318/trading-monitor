"""IB Connection Manager — handles TWS connection, market data, account info, and option chains.

Architecture:
  - Single-threaded executor for all IB operations (ib_insync is not thread-safe)
  - Thread-local IB instance with delayed data (type 3)
  - Streaming price subscriptions: subscribe once, read cached ticker values
  - Account summary: persistent subscription (avoids Error 322)
"""

import asyncio
import logging
import math
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any, Tuple
from concurrent.futures import ThreadPoolExecutor

from ib_insync import IB, Contract, Stock, Future, ContFuture, Option, Index, MarketOrder, LimitOrder, util
import pandas as pd

logger = logging.getLogger(__name__)

# Thread-local storage for IB instance
_thread_local = threading.local()


def _get_ib(host, port, client_id) -> IB:
    """Get or create thread-local IB instance."""
    if not hasattr(_thread_local, 'ib') or _thread_local.ib is None:
        _thread_local.ib = IB()
    ib = _thread_local.ib
    if not ib.isConnected():
        try:
            ib.connect(host, port, clientId=client_id, timeout=10)
            ib.reqMarketDataType(3)  # Delayed data
            logger.info("IB connected in thread %s", threading.current_thread().name)
        except Exception as e:
            logger.error("IB connect error: %s", e)
    return ib


# Single thread executor - all IB ops run here
_ib_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ib_")

# Streaming price subscriptions: watch_id -> (Contract, Ticker)
_subscriptions: Dict[str, Tuple[Contract, Any]] = {}

# Account summary subscription state
_account_subscribed = False


class IBManager:
    def __init__(self, host: str = "127.0.0.1", port: int = 7496, client_id: int = 10):
        self.host = host
        self.port = port
        self.client_id = client_id
        self._connected = False

    def _get_ib(self) -> IB:
        return _get_ib(self.host, self.port, self.client_id)

    @property
    def connected(self) -> bool:
        # Check in executor thread
        try:
            future = _ib_executor.submit(lambda: _get_ib(self.host, self.port, self.client_id).isConnected())
            return future.result(timeout=5)
        except:
            return False

    def _sync_connect(self):
        """Synchronous connection."""
        ib = self._get_ib()
        return ib.isConnected()

    async def connect(self) -> bool:
        """Connect to IB TWS/Gateway."""
        try:
            if self.connected:
                return True
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(_ib_executor, self._sync_connect)
            self._connected = result
            if result:
                logger.info("Connected to IB TWS at %s:%s (delayed data mode)", self.host, self.port)
            return result
        except Exception as e:
            logger.error("IB connection failed: %s", e)
            self._connected = False
            return False

    async def disconnect(self):
        def do_disconnect():
            ib = self._get_ib()
            if ib.isConnected():
                ib.disconnect()
        if self.connected:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(_ib_executor, do_disconnect)
            self._connected = False
            logger.info("Disconnected from IB")

    def _sync_get_account_summary(self) -> Dict[str, Any]:
        """Synchronous account summary fetch.

        Uses persistent subscription to avoid Error 322 (max account summary requests).
        First call subscribes; subsequent calls just read cached data.
        """
        global _account_subscribed
        ib = self._get_ib()

        if not _account_subscribed:
            ib.reqAccountSummary()
            _account_subscribed = True
            util.sleep(1)  # Wait for initial data
        else:
            ib.sleep(0.1)  # Process pending events to get latest data

        summary = ib.accountSummary()
        result = {}
        for item in summary:
            result[item.tag] = {
                "value": item.value,
                "currency": item.currency,
            }
        return result

    async def get_account_summary(self) -> Dict[str, Any]:
        """Get account balance, equity, buying power, etc."""
        try:
            if not self.connected:
                return {"error": "Not connected"}
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(_ib_executor, self._sync_get_account_summary)
        except Exception as e:
            logger.error("Failed to get account summary: %s", e)
            return {"error": str(e)}

    def _sync_get_positions(self) -> List[Dict[str, Any]]:
        """Synchronous positions fetch."""
        ib = self._get_ib()
        positions = ib.positions()
        result = []
        for pos in positions:
            c = pos.contract
            result.append({
                "conId": c.conId,
                "symbol": c.symbol,
                "secType": c.secType,
                "exchange": c.exchange,
                "currency": c.currency,
                "strike": c.strike if hasattr(c, "strike") else None,
                "right": c.right if hasattr(c, "right") else None,
                "expiry": c.lastTradeDateOrContractMonth if hasattr(c, "lastTradeDateOrContractMonth") else None,
                "position": float(pos.position),
                "avgCost": float(pos.avgCost),
                "marketValue": None,
                "marketPrice": None,
                "unrealizedPNL": None,
            })
        # Use portfolio directly for market values (more reliable matching by conId)
        portfolio = ib.portfolio()
        port_map = {}
        for p in portfolio:
            port_map[p.contract.conId] = {
                "marketPrice": float(p.marketPrice) if p.marketPrice else None,
                "marketValue": float(p.marketValue) if p.marketValue else None,
                "unrealizedPNL": float(p.unrealizedPNL) if p.unrealizedPNL is not None else None,
                "realizedPNL": float(p.realizedPNL) if p.realizedPNL is not None else None,
            }
            # Also map by symbol as fallback
            port_map[p.contract.symbol] = port_map[p.contract.conId]
        
        for item in result:
            # Try conId first, then symbol
            pdata = port_map.get(item.get("conId")) or port_map.get(item["symbol"])
            if pdata:
                item.update(pdata)
        return result

    async def get_positions(self) -> List[Dict[str, Any]]:
        """Get current positions."""
        try:
            if not self.connected:
                return []
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(_ib_executor, self._sync_get_positions)
        except Exception as e:
            logger.error("Failed to get positions: %s", e)
            return []

    def _make_contract(self, symbol: str, sec_type: str = "STK", exchange: str = "SMART", 
                       currency: str = "USD", contract_month: str = "",
                       use_contfut: bool = False) -> Contract:
        """Create a contract object.
        
        Args:
            use_contfut: If True and sec_type is FUT with no contract_month,
                        use ContFut (continuous futures) for historical data.
        """
        if sec_type == "STK":
            return Stock(symbol, exchange, currency)
        elif sec_type == "FUT":
            if contract_month:
                return Future(symbol, contract_month, exchange=exchange, currency=currency)
            elif use_contfut:
                # Continuous futures for historical data (long history)
                return ContFuture(symbol, exchange=exchange, currency=currency)
            return Future(symbol, exchange=exchange, currency=currency)
        elif sec_type == "IND":
            return Index(symbol, exchange, currency)
        else:
            return Contract(symbol=symbol, secType=sec_type, exchange=exchange, currency=currency)

    def _sync_get_daily_bars(self, symbol: str, sec_type: str, exchange: str, currency: str, 
                               duration: str, bar_size: str, contract_month: str = "") -> Optional[pd.DataFrame]:
        """Synchronous daily bars fetch."""
        ib = self._get_ib()
        # Use ContFut for futures with no contract_month (continuous contract for long history)
        use_contfut = (sec_type == "FUT" and not contract_month)
        contract = self._make_contract(symbol, sec_type, exchange, currency, contract_month, use_contfut=use_contfut)
        qualified = ib.qualifyContracts(contract)
        if not qualified:
            logger.error("Could not qualify contract: %s", symbol)
            return None
        contract = qualified[0]
        bars = ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )
        if not bars:
            return None
        df = util.df(bars)
        df["date"] = pd.to_datetime(df["date"])
        df.set_index("date", inplace=True)
        return df

    async def get_daily_bars(self, symbol: str, sec_type: str = "STK", exchange: str = "SMART",
                             currency: str = "USD", duration: str = "6 M", bar_size: str = "1 day",
                             contract_month: str = "") -> Optional[pd.DataFrame]:
        """Fetch historical daily bars."""
        try:
            if not self.connected:
                return None
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                _ib_executor, 
                lambda: self._sync_get_daily_bars(symbol, sec_type, exchange, currency, duration, bar_size, contract_month)
            )
        except Exception as e:
            logger.error("Failed to get daily bars for %s: %s", symbol, e)
            return None

    def _sync_get_current_price(self, symbol: str, sec_type: str, exchange: str, currency: str,
                                contract_month: str = "") -> Optional[float]:
        """Synchronous price fetch."""
        ib = self._get_ib()
        contract = self._make_contract(symbol, sec_type, exchange, currency, contract_month)
        qualified = ib.qualifyContracts(contract)
        if not qualified:
            return None
        contract = qualified[0]
        ticker = ib.reqMktData(contract, "", False, False)
        util.sleep(2)
        ib.cancelMktData(contract)
        price = ticker.last if ticker.last and ticker.last > 0 else ticker.close
        return float(price) if price and price > 0 else None

    async def get_current_price(self, symbol: str, sec_type: str = "STK", exchange: str = "SMART",
                                 currency: str = "USD", contract_month: str = "") -> Optional[float]:
        """Get the current/last price for a symbol."""
        try:
            if not self.connected:
                return None
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                _ib_executor,
                lambda: self._sync_get_current_price(symbol, sec_type, exchange, currency, contract_month)
            )
        except Exception as e:
            logger.error("Failed to get price for %s: %s", symbol, e)
            return None

    # ─── Option Chain (2-step: contracts + prices) ───
    def _sync_get_option_contracts(self, symbol: str, sec_type: str, exchange: str, currency: str,
                                    ma_price: float, right: str, num_strikes: int,
                                    contract_month: str = "", num_expirations: int = 5) -> List[Dict[str, Any]]:
        """Step 1: Get option contract info (strikes, expiries) — NO market data request. Fast.

        For futures: merges ALL chains (daily/weekly/monthly) and picks the nearest
        num_expirations dates across all trading classes.
        """
        ib = self._get_ib()
        contract = self._make_contract(symbol, sec_type, exchange, currency, contract_month)
        qualified = ib.qualifyContracts(contract)
        if not qualified:
            return []
        contract = qualified[0]

        # For futures, use the exchange; for stocks, use ""
        # Special handling for COMEX metals (GC, MGC, SI, HG) - try NYMEX first
        fut_fop_exchange = exchange if sec_type == "FUT" else ""
        chains = ib.reqSecDefOptParams(contract.symbol, fut_fop_exchange, contract.secType, contract.conId)
        
        # If no chains found for COMEX, try NYMEX (metals options are often listed there)
        if not chains and exchange == "COMEX":
            logger.info("No chains at COMEX for %s, trying NYMEX...", symbol)
            chains = ib.reqSecDefOptParams(contract.symbol, "NYMEX", contract.secType, contract.conId)
        
        # Last resort: try empty exchange (let IB find it)
        if not chains:
            logger.info("Trying empty exchange for %s options...", symbol)
            chains = ib.reqSecDefOptParams(contract.symbol, "", contract.secType, contract.conId)
        
        if not chains:
            logger.warning("No option chains found for %s (exchange=%s)", symbol, fut_fop_exchange)
            return []

        is_fut = sec_type == "FUT"
        today = datetime.now()
        min_exp = today + timedelta(days=0)  # Include today (0DTE)

        if is_fut:
            # Merge ALL chains: collect (expiry, chain) pairs across all trading classes
            exp_chain_map: Dict[str, Any] = {}  # expiry -> chain (prefer the one with most strikes)
            all_strikes_merged = set()
            for chain in chains:
                for exp in chain.expirations:
                    if datetime.strptime(exp, "%Y%m%d") >= min_exp:
                        if exp not in exp_chain_map or len(chain.strikes) > len(exp_chain_map[exp].strikes):
                            exp_chain_map[exp] = chain
                all_strikes_merged.update(chain.strikes)

            if not exp_chain_map:
                return []

            # Sort by date, pick nearest N
            sorted_exps = sorted(exp_chain_map.keys())[:num_expirations]

            # Use merged strikes for OTM selection
            all_strikes = sorted(all_strikes_merged)
            if right == "C":
                otm = [s for s in all_strikes if s > ma_price][:num_strikes]
            else:
                otm = list(reversed([s for s in all_strikes if s < ma_price]))[:num_strikes]
            if not otm:
                return []

            result = []
            for exp in sorted_exps:
                chain = exp_chain_map[exp]
                opt_exchange = chain.exchange
                # Use strikes available in THIS chain
                chain_strikes = sorted(chain.strikes)
                if right == "C":
                    exp_otm = [s for s in chain_strikes if s > ma_price][:num_strikes]
                else:
                    exp_otm = list(reversed([s for s in chain_strikes if s < ma_price]))[:num_strikes]
                if not exp_otm:
                    continue

                opts = []
                for strike in exp_otm:
                    c = Contract(symbol=symbol, secType='FOP', exchange=opt_exchange,
                                 currency=currency, lastTradeDateOrContractMonth=exp,
                                 strike=strike, right=right, multiplier=chain.multiplier,
                                 tradingClass=chain.tradingClass)
                    opts.append(c)

                qualified_opts = ib.qualifyContracts(*opts)
                for opt in qualified_opts:
                    if opt.conId == 0:
                        continue
                    exp_label = f"{exp[4:6]}/{exp[6:8]}"
                    result.append({
                        "conId": opt.conId,
                        "symbol": opt.symbol,
                        "expiry": opt.lastTradeDateOrContractMonth,
                        "expiryLabel": exp_label,
                        "strike": float(opt.strike),
                        "right": opt.right,
                        "tradingClass": opt.tradingClass,
                        "multiplier": float(opt.multiplier) if opt.multiplier else 100,
                        "name": f"{opt.symbol} {exp_label} {opt.strike}{opt.right}",
                        "bid": None, "ask": None, "last": None, "volume": 0,
                        "_contract": opt,
                    })

            logger.info("Got %d %s option contracts for %s across %d expirations (ma=%.2f)",
                         len(result), right, symbol, len(sorted_exps), ma_price)
            return result

        else:
            # Stocks: pick SMART chain, nearest expirations
            chain = next((c for c in chains if c.exchange == "SMART"), chains[0])
            valid_exps = sorted([e for e in chain.expirations if datetime.strptime(e, "%Y%m%d") >= min_exp])
            if not valid_exps:
                return []
            expirations = valid_exps[:num_expirations]

            all_strikes = sorted(chain.strikes)
            if right == "C":
                otm = [s for s in all_strikes if s > ma_price][:num_strikes]
            else:
                otm = list(reversed([s for s in all_strikes if s < ma_price]))[:num_strikes]
            if not otm:
                return []

            result = []
            opt_exchange = chain.exchange
            logger.info("Using option exchange=%s for %s (STK, %d exps, %d strikes)",
                         opt_exchange, symbol, len(expirations), len(otm))
            for exp in expirations:
                opts = [Option(symbol, exp, strike, right, opt_exchange, currency=currency) for strike in otm]
                qualified_opts = ib.qualifyContracts(*opts)
                for opt in qualified_opts:
                    if opt.conId == 0:
                        continue
                    exp_label = f"{exp[4:6]}/{exp[6:8]}"
                    result.append({
                        "conId": opt.conId,
                        "symbol": opt.symbol,
                        "expiry": opt.lastTradeDateOrContractMonth,
                        "expiryLabel": exp_label,
                        "strike": float(opt.strike),
                        "right": opt.right,
                        "multiplier": float(opt.multiplier) if opt.multiplier else 100,
                        "name": f"{opt.symbol} {exp_label} {opt.strike}{opt.right}",
                        "bid": None, "ask": None, "last": None, "volume": 0,
                        "_contract": opt,
                    })
            logger.info("Got %d %s option contracts for %s (ma=%.2f)", len(result), right, symbol, ma_price)
            return result

    def _sync_refresh_option_prices(self, options: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Step 2: Batch-fetch prices for cached option contracts using snapshots."""
        ib = self._get_ib()
        if not options:
            return options

        # Request all snapshots at once
        tickers = []
        for o in options:
            contract = o.get("_contract")
            if not contract:
                # Reconstruct contract from data
                contract = Option(o["symbol"], o["expiry"], o["strike"], o["right"], "SMART")
                qualified = ib.qualifyContracts(contract)
                if qualified:
                    contract = qualified[0]
                else:
                    tickers.append(None)
                    continue
            ticker = ib.reqMktData(contract, "", True, False)  # snapshot=True
            tickers.append((contract, ticker))

        # Wait once for all snapshots
        ib.sleep(2)

        # Collect results and cancel
        for i, item in enumerate(tickers):
            if item is None:
                continue
            contract, ticker = item
            try:
                ib.cancelMktData(contract)
            except:
                pass
            options[i]["bid"] = float(ticker.bid) if ticker.bid and ticker.bid > 0 else None
            options[i]["ask"] = float(ticker.ask) if ticker.ask and ticker.ask > 0 else None
            options[i]["last"] = float(ticker.last) if ticker.last and ticker.last > 0 else None
            options[i]["volume"] = int(ticker.volume) if ticker.volume and ticker.volume > 0 else 0

        logger.info("Refreshed prices for %d options", len(options))
        return options

    async def get_option_chain(self, symbol: str, sec_type: str = "STK", exchange: str = "SMART",
                                currency: str = "USD", ma_price: float = 0,
                                right: str = "C", num_strikes: int = 5,
                                contract_month: str = "", num_expirations: int = 5) -> List[Dict[str, Any]]:
        """Get option contracts (fast, no market data)."""
        try:
            if not self.connected:
                return []
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                _ib_executor,
                lambda: self._sync_get_option_contracts(symbol, sec_type, exchange, currency, ma_price, right, num_strikes, contract_month, num_expirations)
            )
        except Exception as e:
            logger.error("Failed to get option chain for %s: %s", symbol, e)
            return []

    async def refresh_option_prices(self, options: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Refresh prices for cached option contracts (batch snapshot)."""
        try:
            if not self.connected or not options:
                return options
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                _ib_executor,
                lambda: self._sync_refresh_option_prices(options)
            )
        except Exception as e:
            logger.error("Failed to refresh option prices: %s", e)
            return options

    # ─── Order Management ───

    def _sync_get_contract_by_conid(self, con_id: int) -> Optional[Contract]:
        """Look up a contract by conId from options cache or reconstruct."""
        ib = self._get_ib()
        contract = Contract(conId=con_id)
        qualified = ib.qualifyContracts(contract)
        return qualified[0] if qualified else None

    def _sync_place_market_order(self, contract, action: str, quantity: int) -> Dict[str, Any]:
        """Place a market order. action = 'BUY' or 'SELL'."""
        ib = self._get_ib()
        order = MarketOrder(action, quantity)
        trade = ib.placeOrder(contract, order)
        ib.sleep(2)  # Wait for initial fill
        return {
            "orderId": trade.order.orderId,
            "status": trade.orderStatus.status,
            "filled": float(trade.orderStatus.filled),
            "avgFillPrice": float(trade.orderStatus.avgFillPrice),
            "remaining": float(trade.orderStatus.remaining),
        }

    def _sync_place_limit_order(self, contract, action: str, quantity: int, limit_price: float) -> Dict[str, Any]:
        """Place a limit order for exit strategy."""
        ib = self._get_ib()
        order = LimitOrder(action, quantity, limit_price)
        trade = ib.placeOrder(contract, order)
        ib.sleep(1)
        return {
            "orderId": trade.order.orderId,
            "status": trade.orderStatus.status,
            "filled": float(trade.orderStatus.filled),
            "avgFillPrice": float(trade.orderStatus.avgFillPrice),
        }

    def _sync_cancel_order(self, order_id: int):
        """Cancel an order by orderId."""
        ib = self._get_ib()
        for trade in ib.openTrades():
            if trade.order.orderId == order_id:
                ib.cancelOrder(trade.order)
                ib.sleep(0.5)
                return True
        return False

    def _sync_get_open_orders(self) -> List[Dict]:
        """Get all open orders."""
        ib = self._get_ib()
        result = []
        for trade in ib.openTrades():
            c = trade.contract
            result.append({
                "orderId": trade.order.orderId,
                "symbol": c.symbol,
                "secType": c.secType,
                "action": trade.order.action,
                "qty": float(trade.order.totalQuantity),
                "orderType": trade.order.orderType,
                "limitPrice": float(trade.order.lmtPrice) if trade.order.lmtPrice else None,
                "status": trade.orderStatus.status,
                "filled": float(trade.orderStatus.filled),
                "avgFillPrice": float(trade.orderStatus.avgFillPrice),
            })
        return result

    async def place_market_order(self, con_id: int, action: str, quantity: int) -> Dict[str, Any]:
        """Place a market order by conId (async wrapper)."""
        def _do():
            contract = self._sync_get_contract_by_conid(con_id)
            if not contract:
                return {"error": f"Could not qualify contract conId={con_id}"}
            return self._sync_place_market_order(contract, action, quantity)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_ib_executor, _do)

    async def place_limit_order(self, con_id: int, action: str, quantity: int, limit_price: float) -> Dict[str, Any]:
        """Place a limit order by conId (async wrapper)."""
        def _do():
            contract = self._sync_get_contract_by_conid(con_id)
            if not contract:
                return {"error": f"Could not qualify contract conId={con_id}"}
            return self._sync_place_limit_order(contract, action, quantity, limit_price)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_ib_executor, _do)

    async def cancel_order(self, order_id: int) -> bool:
        """Cancel an order by orderId (async wrapper)."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_ib_executor, lambda: self._sync_cancel_order(order_id))

    async def get_open_orders(self) -> List[Dict]:
        """Get all open orders (async wrapper)."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_ib_executor, self._sync_get_open_orders)

    # ─── Streaming Price Subscriptions ───

    def _sync_subscribe_price(self, watch_id: str, symbol: str, sec_type: str,
                               exchange: str, currency: str, contract_month: str = "") -> bool:
        """Subscribe to streaming market data for a watch item. Runs in executor thread."""
        ib = self._get_ib()
        contract = self._make_contract(symbol, sec_type, exchange, currency, contract_month)
        qualified = ib.qualifyContracts(contract)
        if not qualified:
            logger.error("Cannot qualify contract for subscription: %s", symbol)
            return False
        contract = qualified[0]
        ticker = ib.reqMktData(contract, "", False, False)  # snapshot=False → streaming
        _subscriptions[watch_id] = (contract, ticker)
        logger.info("📡 Subscribed to price stream: %s (conId=%d)", symbol, contract.conId)
        return True

    def _sync_unsubscribe_price(self, watch_id: str):
        """Unsubscribe from market data for a watch item."""
        if watch_id in _subscriptions:
            ib = self._get_ib()
            contract, ticker = _subscriptions.pop(watch_id)
            try:
                ib.cancelMktData(contract)
                logger.info("Unsubscribed: %s", contract.symbol)
            except Exception as e:
                logger.error("Error unsubscribing %s: %s", contract.symbol, e)

    def _sync_unsubscribe_all(self):
        """Unsubscribe all active price streams."""
        ib = self._get_ib()
        for watch_id in list(_subscriptions.keys()):
            contract, ticker = _subscriptions.pop(watch_id)
            try:
                ib.cancelMktData(contract)
            except Exception:
                pass
        logger.info("Unsubscribed all price streams")

    def _sync_read_prices(self) -> Dict[str, float]:
        """Read current prices from all active subscriptions.

        Processes pending IB events, then reads cached ticker values.
        Very fast — no new API requests.
        """
        ib = self._get_ib()
        ib.sleep(0.1)  # Process pending messages from IB

        prices = {}
        for watch_id, (contract, ticker) in _subscriptions.items():
            price = ticker.marketPrice()
            if math.isnan(price):
                # Fallback: try close price
                price = ticker.close
                if math.isnan(price):
                    continue
            if price > 0:
                prices[watch_id] = float(price)
        return prices

    async def subscribe_price(self, watch_id: str, symbol: str, sec_type: str,
                               exchange: str, currency: str, contract_month: str = "") -> bool:
        """Subscribe to streaming price data (async wrapper)."""
        try:
            if not self.connected:
                return False
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                _ib_executor,
                lambda: self._sync_subscribe_price(watch_id, symbol, sec_type, exchange, currency, contract_month)
            )
        except Exception as e:
            logger.error("Failed to subscribe %s: %s", symbol, e)
            return False

    async def unsubscribe_price(self, watch_id: str):
        """Unsubscribe from streaming price data (async wrapper)."""
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(_ib_executor, lambda: self._sync_unsubscribe_price(watch_id))
        except Exception as e:
            logger.error("Failed to unsubscribe %s: %s", watch_id, e)

    async def unsubscribe_all(self):
        """Unsubscribe all price streams (async wrapper)."""
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(_ib_executor, self._sync_unsubscribe_all)
        except Exception as e:
            logger.error("Failed to unsubscribe all: %s", e)

    async def read_prices(self) -> Dict[str, float]:
        """Read all streaming prices (async wrapper). Fast — no API calls."""
        try:
            if not self.connected:
                return {}
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(_ib_executor, self._sync_read_prices)
        except Exception as e:
            logger.error("Failed to read prices: %s", e)
            return {}

    async def sleep(self, seconds: float = 0):
        """Keep IB event loop alive."""
        if self.connected:
            util.sleep(seconds)

    def get_underlying_info(self, watch_id: str) -> Optional[Dict[str, Any]]:
        """Get underlying contract info (conId, multiplier) for a watch item."""
        if watch_id not in _subscriptions:
            return None
        contract, ticker = _subscriptions[watch_id]
        multiplier = 1
        if hasattr(contract, 'multiplier') and contract.multiplier:
            try:
                multiplier = int(contract.multiplier)
            except (ValueError, TypeError):
                multiplier = 1
        return {
            "conId": contract.conId,
            "multiplier": multiplier,
            "symbol": contract.symbol,
            "secType": contract.secType,
        }
