"""IB Connection Manager — handles TWS connection, market data, account info, and option chains."""

import asyncio
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any
from concurrent.futures import ThreadPoolExecutor

from ib_insync import IB, Contract, Stock, Future, Option, Index, util
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
        """Synchronous account summary fetch."""
        ib = self._get_ib()
        ib.reqAccountSummary()
        util.sleep(1)
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
            })
        # Also try portfolio for market values
        portfolio = ib.portfolio()
        port_map = {}
        for p in portfolio:
            key = f"{p.contract.symbol}_{p.contract.secType}_{p.contract.strike}_{p.contract.right}"
            port_map[key] = {
                "marketPrice": float(p.marketPrice),
                "marketValue": float(p.marketValue),
                "unrealizedPNL": float(p.unrealizedPNL),
                "realizedPNL": float(p.realizedPNL),
            }
        for item in result:
            key = f"{item['symbol']}_{item['secType']}_{item['strike']}_{item['right']}"
            if key in port_map:
                item.update(port_map[key])
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

    def _make_contract(self, symbol: str, sec_type: str = "STK", exchange: str = "SMART", currency: str = "USD") -> Contract:
        """Create a contract object."""
        if sec_type == "STK":
            return Stock(symbol, exchange, currency)
        elif sec_type == "FUT":
            # For futures like MNQ, MES — need to handle continuous
            return Future(symbol, exchange=exchange, currency=currency)
        elif sec_type == "IND":
            return Index(symbol, exchange, currency)
        else:
            return Contract(symbol=symbol, secType=sec_type, exchange=exchange, currency=currency)

    def _sync_get_daily_bars(self, symbol: str, sec_type: str, exchange: str, currency: str, 
                               duration: str, bar_size: str) -> Optional[pd.DataFrame]:
        """Synchronous daily bars fetch."""
        ib = self._get_ib()
        contract = self._make_contract(symbol, sec_type, exchange, currency)
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
                             currency: str = "USD", duration: str = "6 M", bar_size: str = "1 day") -> Optional[pd.DataFrame]:
        """Fetch historical daily bars."""
        try:
            if not self.connected:
                return None
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                _ib_executor, 
                lambda: self._sync_get_daily_bars(symbol, sec_type, exchange, currency, duration, bar_size)
            )
        except Exception as e:
            logger.error("Failed to get daily bars for %s: %s", symbol, e)
            return None

    def _sync_get_current_price(self, symbol: str, sec_type: str, exchange: str, currency: str) -> Optional[float]:
        """Synchronous price fetch."""
        ib = self._get_ib()
        contract = self._make_contract(symbol, sec_type, exchange, currency)
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
                                 currency: str = "USD") -> Optional[float]:
        """Get the current/last price for a symbol."""
        try:
            if not self.connected:
                return None
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                _ib_executor,
                lambda: self._sync_get_current_price(symbol, sec_type, exchange, currency)
            )
        except Exception as e:
            logger.error("Failed to get price for %s: %s", symbol, e)
            return None

    def _sync_get_option_chain(self, symbol: str, sec_type: str, exchange: str, currency: str,
                                 ma_price: float, right: str, num_strikes: int) -> List[Dict[str, Any]]:
        """Synchronous option chain fetch."""
        ib = self._get_ib()
        contract = self._make_contract(symbol, sec_type, exchange, currency)
        qualified = ib.qualifyContracts(contract)
        if not qualified:
            return []
        contract = qualified[0]

        # Get option chains
        chains = ib.reqSecDefOptParams(contract.symbol, "", contract.secType, contract.conId)
        if not chains:
            logger.warning("No option chains found for %s", symbol)
            return []

        # Pick the chain with SMART exchange if available
        chain = None
        for c in chains:
            if c.exchange == "SMART":
                chain = c
                break
        if not chain:
            chain = chains[0]

        # Find nearest 2 expirations (at least 1 day out)
        today = datetime.now()
        min_exp = today + timedelta(days=1)
        valid_exps = sorted([e for e in chain.expirations if datetime.strptime(e, "%Y%m%d") >= min_exp])
        if not valid_exps:
            return []
        expirations_to_fetch = valid_exps[:2]  # nearest 2

        # Get strikes sorted by proximity to MA
        all_strikes = sorted(chain.strikes)

        # Filter OTM strikes
        if right == "C":
            otm_strikes = [s for s in all_strikes if s > ma_price]
        else:
            otm_strikes = [s for s in all_strikes if s < ma_price]
            otm_strikes = list(reversed(otm_strikes))

        selected_strikes = otm_strikes[:num_strikes]
        if not selected_strikes:
            return []

        result = []
        for exp in expirations_to_fetch:
            # Create option contracts
            options = [Option(symbol, exp, strike, right, "SMART", currency=currency) for strike in selected_strikes]
            qualified_opts = ib.qualifyContracts(*options)

            for opt in qualified_opts:
                if opt.conId == 0:
                    continue
                ticker = ib.reqMktData(opt, "", False, False)
                util.sleep(0.5)
                ib.cancelMktData(opt)

                exp_label = f"{exp[4:6]}/{exp[6:8]}"
                name = f"{opt.symbol} {exp_label} {opt.strike}{opt.right}"
                result.append({
                    "conId": opt.conId,
                    "symbol": opt.symbol,
                    "expiry": opt.lastTradeDateOrContractMonth,
                    "expiryLabel": exp_label,
                    "strike": float(opt.strike),
                    "right": opt.right,
                    "name": name,
                    "bid": float(ticker.bid) if ticker.bid and ticker.bid > 0 else None,
                    "ask": float(ticker.ask) if ticker.ask and ticker.ask > 0 else None,
                    "last": float(ticker.last) if ticker.last and ticker.last > 0 else None,
                    "volume": int(ticker.volume) if ticker.volume and ticker.volume > 0 else 0,
                })
        return result

    async def get_option_chain(self, symbol: str, sec_type: str = "STK", exchange: str = "SMART",
                                currency: str = "USD", ma_price: float = 0,
                                right: str = "C", num_strikes: int = 5) -> List[Dict[str, Any]]:
        """
        Fetch OTM option strikes closest to the MA price.
        right: 'C' for calls, 'P' for puts
        Returns num_strikes nearest OTM options with prices.
        """
        try:
            if not self.connected:
                return []
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                _ib_executor,
                lambda: self._sync_get_option_chain(symbol, sec_type, exchange, currency, ma_price, right, num_strikes)
            )
        except Exception as e:
            logger.error("Failed to get option chain for %s: %s", symbol, e)
            return []

    async def sleep(self, seconds: float = 0):
        """Keep IB event loop alive."""
        if self.connected:
            util.sleep(seconds)
