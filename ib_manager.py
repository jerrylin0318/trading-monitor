"""IB Connection Manager — handles TWS connection, market data, account info, and option chains."""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any

from ib_insync import IB, Contract, Stock, Future, Option, Index, util
import pandas as pd

logger = logging.getLogger(__name__)


class IBManager:
    def __init__(self, host: str = "127.0.0.1", port: int = 7496, client_id: int = 10):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.ib = IB()
        self._connected = False

    @property
    def connected(self) -> bool:
        return self.ib.isConnected()

    async def connect(self) -> bool:
        """Connect to IB TWS/Gateway."""
        try:
            if self.connected:
                return True
            await asyncio.wait_for(
                asyncio.to_thread(self.ib.connect, self.host, self.port, clientId=self.client_id),
                timeout=10,
            )
            self._connected = True
            logger.info("Connected to IB TWS at %s:%s", self.host, self.port)
            return True
        except Exception as e:
            logger.error("IB connection failed: %s", e)
            self._connected = False
            return False

    async def disconnect(self):
        if self.connected:
            self.ib.disconnect()
            self._connected = False
            logger.info("Disconnected from IB")

    async def get_account_summary(self) -> Dict[str, Any]:
        """Get account balance, equity, buying power, etc."""
        try:
            if not self.connected:
                return {"error": "Not connected"}
            summary = await asyncio.to_thread(self.ib.accountSummary)
            result = {}
            for item in summary:
                result[item.tag] = {
                    "value": item.value,
                    "currency": item.currency,
                }
            return result
        except Exception as e:
            logger.error("Failed to get account summary: %s", e)
            return {"error": str(e)}

    async def get_positions(self) -> List[Dict[str, Any]]:
        """Get current positions."""
        try:
            if not self.connected:
                return []
            positions = await asyncio.to_thread(self.ib.positions)
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
                    "marketValue": None,  # filled by portfolio
                })
            # Also try portfolio for market values
            portfolio = await asyncio.to_thread(self.ib.portfolio)
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

    async def get_daily_bars(self, symbol: str, sec_type: str = "STK", exchange: str = "SMART",
                             currency: str = "USD", duration: str = "6 M", bar_size: str = "1 day") -> Optional[pd.DataFrame]:
        """Fetch historical daily bars."""
        try:
            if not self.connected:
                return None
            contract = self._make_contract(symbol, sec_type, exchange, currency)
            # Qualify the contract
            qualified = await asyncio.to_thread(self.ib.qualifyContracts, contract)
            if not qualified:
                logger.error("Could not qualify contract: %s", symbol)
                return None
            contract = qualified[0]
            bars = await asyncio.to_thread(
                self.ib.reqHistoricalData,
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
        except Exception as e:
            logger.error("Failed to get daily bars for %s: %s", symbol, e)
            return None

    async def get_current_price(self, symbol: str, sec_type: str = "STK", exchange: str = "SMART",
                                 currency: str = "USD") -> Optional[float]:
        """Get the current/last price for a symbol."""
        try:
            if not self.connected:
                return None
            contract = self._make_contract(symbol, sec_type, exchange, currency)
            qualified = await asyncio.to_thread(self.ib.qualifyContracts, contract)
            if not qualified:
                return None
            contract = qualified[0]
            ticker = self.ib.reqMktData(contract, "", False, False)
            await asyncio.sleep(2)  # wait for data
            self.ib.cancelMktData(contract)
            price = ticker.last if ticker.last and ticker.last > 0 else ticker.close
            return float(price) if price and price > 0 else None
        except Exception as e:
            logger.error("Failed to get price for %s: %s", symbol, e)
            return None

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

            contract = self._make_contract(symbol, sec_type, exchange, currency)
            qualified = await asyncio.to_thread(self.ib.qualifyContracts, contract)
            if not qualified:
                return []
            contract = qualified[0]

            # Get option chains
            chains = await asyncio.to_thread(self.ib.reqSecDefOptParams, contract.symbol, "", contract.secType, contract.conId)
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

            # Find nearest expiration (at least 7 days out)
            today = datetime.now()
            min_exp = today + timedelta(days=7)
            valid_exps = sorted([e for e in chain.expirations if datetime.strptime(e, "%Y%m%d") >= min_exp])
            if not valid_exps:
                return []
            nearest_exp = valid_exps[0]

            # Get strikes sorted by proximity to MA
            all_strikes = sorted(chain.strikes)

            # Filter OTM strikes
            if right == "C":
                # OTM calls: strike > current price (use MA as reference)
                otm_strikes = [s for s in all_strikes if s > ma_price]
            else:
                # OTM puts: strike < current price
                otm_strikes = [s for s in all_strikes if s < ma_price]
                otm_strikes = list(reversed(otm_strikes))  # closest first

            # Take nearest num_strikes
            selected_strikes = otm_strikes[:num_strikes]

            if not selected_strikes:
                return []

            # Create option contracts and get prices
            options = []
            for strike in selected_strikes:
                opt = Option(symbol, nearest_exp, strike, right, "SMART", currency=currency)
                options.append(opt)

            # Qualify
            qualified_opts = await asyncio.to_thread(self.ib.qualifyContracts, *options)

            result = []
            for opt in qualified_opts:
                if opt.conId == 0:
                    continue
                ticker = self.ib.reqMktData(opt, "", False, False)
                await asyncio.sleep(1)
                self.ib.cancelMktData(opt)

                name = f"{opt.symbol} {opt.lastTradeDateOrContractMonth} {opt.strike} {opt.right}"
                result.append({
                    "conId": opt.conId,
                    "symbol": opt.symbol,
                    "expiry": opt.lastTradeDateOrContractMonth,
                    "strike": float(opt.strike),
                    "right": opt.right,
                    "name": name,
                    "bid": float(ticker.bid) if ticker.bid and ticker.bid > 0 else None,
                    "ask": float(ticker.ask) if ticker.ask and ticker.ask > 0 else None,
                    "last": float(ticker.last) if ticker.last and ticker.last > 0 else None,
                    "volume": int(ticker.volume) if ticker.volume and ticker.volume > 0 else 0,
                })

            return result
        except Exception as e:
            logger.error("Failed to get option chain for %s: %s", symbol, e)
            return []

    async def sleep(self, seconds: float = 0):
        """Keep IB event loop alive."""
        if self.connected:
            self.ib.sleep(seconds)
