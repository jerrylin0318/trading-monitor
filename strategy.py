"""Strategy Engine — MA-based signal detection for buy/sell."""

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Optional, List, Dict, Any

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


class SignalType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class WatchItem:
    """A single instrument being monitored."""
    id: str  # unique id
    symbol: str
    sec_type: str = "STK"  # STK, FUT, IND
    exchange: str = "SMART"
    currency: str = "USD"
    ma_period: int = 21
    n_points: float = 5.0
    enabled: bool = True

    def to_dict(self):
        return asdict(self)


@dataclass
class Signal:
    """A triggered signal."""
    timestamp: str
    watch_id: str
    symbol: str
    signal_type: str  # BUY or SELL
    price: float
    ma_value: float
    ma_period: int
    n_points: float
    distance: float  # price distance from MA
    acknowledged: bool = False

    def to_dict(self):
        return asdict(self)


class StrategyEngine:
    def __init__(self):
        self.watch_list: Dict[str, WatchItem] = {}
        self.signals: List[Signal] = []
        self.latest_data: Dict[str, Dict[str, Any]] = {}  # symbol -> latest calc data
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def start(self):
        self._running = True
        logger.info("Strategy engine started")

    def stop(self):
        self._running = False
        logger.info("Strategy engine stopped")

    def add_watch(self, item: WatchItem):
        self.watch_list[item.id] = item
        logger.info("Added watch: %s (%s)", item.symbol, item.id)

    def remove_watch(self, watch_id: str):
        if watch_id in self.watch_list:
            del self.watch_list[watch_id]
            logger.info("Removed watch: %s", watch_id)

    def update_watch(self, watch_id: str, updates: Dict[str, Any]):
        if watch_id in self.watch_list:
            item = self.watch_list[watch_id]
            for k, v in updates.items():
                if hasattr(item, k):
                    setattr(item, k, v)
            logger.info("Updated watch %s: %s", watch_id, updates)

    def calculate_ma(self, df: pd.DataFrame, period: int) -> pd.Series:
        """Calculate Simple Moving Average."""
        return df["close"].rolling(window=period).mean()

    def check_signal(self, df: pd.DataFrame, watch: WatchItem, current_price: float) -> Optional[Signal]:
        """
        Check if a signal should trigger.

        BUY: MA is rising AND price is within N points above MA (MA <= price <= MA + N)
        SELL: MA is falling AND price is within N points below MA (MA - N <= price <= MA)
        """
        if not watch.enabled or df is None or len(df) < watch.ma_period + 1:
            return None

        ma = self.calculate_ma(df, watch.ma_period)

        if len(ma) < 2 or pd.isna(ma.iloc[-1]) or pd.isna(ma.iloc[-2]):
            return None

        current_ma = ma.iloc[-1]
        prev_ma = ma.iloc[-2]
        ma_rising = current_ma > prev_ma
        ma_falling = current_ma < prev_ma

        # Store latest calculation data
        self.latest_data[watch.id] = {
            "symbol": watch.symbol,
            "current_price": current_price,
            "ma_value": round(current_ma, 4),
            "prev_ma": round(prev_ma, 4),
            "ma_period": watch.ma_period,
            "ma_direction": "RISING" if ma_rising else ("FALLING" if ma_falling else "FLAT"),
            "n_points": watch.n_points,
            "distance_from_ma": round(current_price - current_ma, 4),
            "buy_zone": f"{round(current_ma, 2)} ~ {round(current_ma + watch.n_points, 2)}" if ma_rising else None,
            "sell_zone": f"{round(current_ma - watch.n_points, 2)} ~ {round(current_ma, 2)}" if ma_falling else None,
            "last_updated": datetime.now().isoformat(),
        }

        now = datetime.now().isoformat()

        # BUY: MA rising, price within N points above MA
        if ma_rising and current_ma <= current_price <= current_ma + watch.n_points:
            distance = current_price - current_ma
            signal = Signal(
                timestamp=now,
                watch_id=watch.id,
                symbol=watch.symbol,
                signal_type=SignalType.BUY,
                price=current_price,
                ma_value=round(current_ma, 4),
                ma_period=watch.ma_period,
                n_points=watch.n_points,
                distance=round(distance, 4),
            )
            self.signals.append(signal)
            logger.info("BUY signal: %s @ %.2f (MA%.0f=%.2f, dist=%.2f)",
                        watch.symbol, current_price, watch.ma_period, current_ma, distance)
            return signal

        # SELL: MA falling, price within N points below MA
        if ma_falling and current_ma - watch.n_points <= current_price <= current_ma:
            distance = current_ma - current_price
            signal = Signal(
                timestamp=now,
                watch_id=watch.id,
                symbol=watch.symbol,
                signal_type=SignalType.SELL,
                price=current_price,
                ma_value=round(current_ma, 4),
                ma_period=watch.ma_period,
                n_points=watch.n_points,
                distance=round(distance, 4),
            )
            self.signals.append(signal)
            logger.info("SELL signal: %s @ %.2f (MA%.0f=%.2f, dist=%.2f)",
                        watch.symbol, current_price, watch.ma_period, current_ma, distance)
            return signal

        return None

    def get_signals(self, limit: int = 50) -> List[Dict[str, Any]]:
        return [s.to_dict() for s in reversed(self.signals[-limit:])]

    def get_watch_list(self) -> List[Dict[str, Any]]:
        return [w.to_dict() for w in self.watch_list.values()]

    def get_latest_data(self) -> Dict[str, Any]:
        return self.latest_data

    def clear_signals(self):
        self.signals.clear()
