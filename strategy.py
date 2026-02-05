"""Strategy Engine — MA-based signal detection with pre-calculated thresholds.

Architecture (event-driven):
  1. Hourly: fetch daily bars → calculate MA → pre-compute trigger thresholds
  2. Streaming: IB pushes prices → check_price() does a simple comparison
  3. Signal fires instantly when price enters trigger zone
"""

import logging
import math
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Optional, List, Dict, Any

try:
    import pandas as pd
    import numpy as np
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

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
    contract_month: str = ""  # YYYYMM for futures
    direction: str = "LONG"  # LONG or SHORT

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


@dataclass
class ThresholdCache:
    """Pre-calculated trigger thresholds — recalculated hourly from daily bars.

    The monitor loop reads streaming prices and compares against these values.
    No MA recalculation needed on each tick.
    """
    ma_value: float
    prev_ma: float
    ma_rising: bool
    trigger_low: float       # lower bound of trigger zone
    trigger_high: float      # upper bound of trigger zone
    signal_type: Optional[str]  # "BUY" or "SELL" or None (flat MA)
    last_calc: float         # unix timestamp
    last_price: float = 0.0
    signal_fired: bool = False  # prevent repeated signals in same zone entry

    # Display data for frontend
    ma_direction: str = "FLAT"
    n_points: float = 0.0
    ma_period: int = 0
    buy_zone: Optional[str] = None
    sell_zone: Optional[str] = None


@dataclass
class ActiveTrade:
    """An active trade with entry and exit conditions."""
    id: str                          # unique trade id
    watch_id: str
    symbol: str
    direction: str                   # BUY or SELL
    entry_time: str                  # ISO timestamp
    orders: List[Dict[str, Any]]     # [{conId, symbol, strike, right, expiry, qty, avg_cost, ib_order_id}]
    exit_strategies: Dict[str, Any]  # {limit: {enabled, pts, dir, order_ids}, time: {enabled, value}, ma: {enabled, cond, dir, pts}}
    status: str = "pending"          # pending → filled → exiting → closed

    def to_dict(self):
        return asdict(self)


class StrategyEngine:
    def __init__(self):
        self.watch_list: Dict[str, WatchItem] = {}
        self.signals: List[Signal] = []
        self.latest_data: Dict[str, Dict[str, Any]] = {}  # watch_id -> frontend data
        self._thresholds: Dict[str, ThresholdCache] = {}   # watch_id -> cached thresholds
        self.active_trades: Dict[str, ActiveTrade] = {}
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
        self._thresholds.pop(watch_id, None)
        self.latest_data.pop(watch_id, None)
        logger.info("Removed watch: %s", watch_id)

    def update_watch(self, watch_id: str, updates: Dict[str, Any]):
        if watch_id in self.watch_list:
            item = self.watch_list[watch_id]
            for k, v in updates.items():
                if hasattr(item, k):
                    setattr(item, k, v)
            # Invalidate threshold cache if strategy params changed
            if any(k in updates for k in ('ma_period', 'n_points', 'direction', 'enabled')):
                self._thresholds.pop(watch_id, None)
            logger.info("Updated watch %s: %s", watch_id, updates)

    # ─── MA Calculation ───

    def calculate_ma(self, df, period: int):
        """Calculate Simple Moving Average. Works with pandas DataFrame or list of dicts."""
        if HAS_PANDAS and hasattr(df, 'rolling'):
            return df["close"].rolling(window=period).mean()
        # Fallback: plain Python
        closes = [row["close"] for row in df] if isinstance(df, list) else list(df["close"])
        result = [None] * len(closes)
        for i in range(period - 1, len(closes)):
            result[i] = sum(closes[i - period + 1:i + 1]) / period
        return result

    # ─── Threshold Calculation (hourly) ───

    def calculate_thresholds(self, watch_id: str, df, watch: WatchItem) -> Optional[ThresholdCache]:
        """Calculate MA from daily bars and pre-compute trigger thresholds.

        Called once on startup, then hourly. Stores result in self._thresholds.
        Also populates self.latest_data with initial display values.
        """
        if not watch.enabled or df is None:
            return None

        length = len(df) if hasattr(df, '__len__') else 0
        if length < watch.ma_period + 1:
            return None

        ma = self.calculate_ma(df, watch.ma_period)

        if HAS_PANDAS and hasattr(ma, 'iloc'):
            if len(ma) < 2 or pd.isna(ma.iloc[-1]) or pd.isna(ma.iloc[-2]):
                return None
            current_ma = float(ma.iloc[-1])
            prev_ma = float(ma.iloc[-2])
        else:
            if len(ma) < 2 or ma[-1] is None or ma[-2] is None:
                return None
            current_ma = float(ma[-1])
            prev_ma = float(ma[-2])

        ma_rising = current_ma > prev_ma
        ma_falling = current_ma < prev_ma

        # Determine trigger zone
        trigger_low = 0.0
        trigger_high = 0.0
        signal_type = None
        buy_zone = None
        sell_zone = None

        if ma_rising:
            trigger_low = current_ma
            trigger_high = current_ma + watch.n_points
            signal_type = "BUY"
            buy_zone = f"{round(current_ma, 2)} ~ {round(current_ma + watch.n_points, 2)}"
        elif ma_falling:
            trigger_low = current_ma - watch.n_points
            trigger_high = current_ma
            signal_type = "SELL"
            sell_zone = f"{round(current_ma - watch.n_points, 2)} ~ {round(current_ma, 2)}"

        ma_direction = "RISING" if ma_rising else ("FALLING" if ma_falling else "FLAT")

        cache = ThresholdCache(
            ma_value=round(current_ma, 4),
            prev_ma=round(prev_ma, 4),
            ma_rising=ma_rising,
            trigger_low=round(trigger_low, 4),
            trigger_high=round(trigger_high, 4),
            signal_type=signal_type,
            last_calc=time.time(),
            signal_fired=False,
            ma_direction=ma_direction,
            n_points=watch.n_points,
            ma_period=watch.ma_period,
            buy_zone=buy_zone,
            sell_zone=sell_zone,
        )

        self._thresholds[watch_id] = cache

        # Populate latest_data for frontend (no price yet — will be updated by check_price)
        self.latest_data[watch_id] = {
            "symbol": watch.symbol,
            "current_price": cache.last_price or 0,
            "ma_value": cache.ma_value,
            "prev_ma": cache.prev_ma,
            "ma_period": cache.ma_period,
            "ma_direction": ma_direction,
            "n_points": cache.n_points,
            "distance_from_ma": 0,
            "buy_zone": buy_zone,
            "sell_zone": sell_zone,
            "last_updated": datetime.now().isoformat(),
        }

        logger.info("Thresholds for %s: MA%.0f=%.2f %s, zone=[%.2f, %.2f]",
                     watch.symbol, watch.ma_period, current_ma, ma_direction,
                     trigger_low, trigger_high)
        return cache

    # ─── Price Check (streaming, every tick) ───

    def check_price(self, watch_id: str, price: float) -> Optional[Signal]:
        """Check if streaming price triggers a signal using pre-calculated thresholds.

        Called on every price update from IB streaming. Very fast — just comparisons.
        Returns Signal if triggered, None otherwise.
        """
        cache = self._thresholds.get(watch_id)
        if not cache or not cache.signal_type:
            return None

        watch = self.watch_list.get(watch_id)
        if not watch or not watch.enabled:
            return None

        # Update latest_data for frontend display
        distance = round(price - cache.ma_value, 4)
        self.latest_data[watch_id] = {
            "symbol": watch.symbol,
            "current_price": price,
            "ma_value": cache.ma_value,
            "prev_ma": cache.prev_ma,
            "ma_period": cache.ma_period,
            "ma_direction": cache.ma_direction,
            "n_points": cache.n_points,
            "distance_from_ma": distance,
            "buy_zone": cache.buy_zone,
            "sell_zone": cache.sell_zone,
            "last_updated": datetime.now().isoformat(),
        }

        cache.last_price = price

        # Direction filter: LONG only triggers on BUY, SHORT only on SELL
        if watch.direction == "LONG" and cache.signal_type != "BUY":
            return None
        if watch.direction == "SHORT" and cache.signal_type != "SELL":
            return None

        # Check if price is in trigger zone
        in_zone = cache.trigger_low <= price <= cache.trigger_high

        if in_zone and not cache.signal_fired:
            # 🔔 Signal fires!
            cache.signal_fired = True
            signal = Signal(
                timestamp=datetime.now().isoformat(),
                watch_id=watch_id,
                symbol=watch.symbol,
                signal_type=cache.signal_type,
                price=price,
                ma_value=cache.ma_value,
                ma_period=cache.ma_period,
                n_points=cache.n_points,
                distance=abs(distance),
            )
            self.signals.append(signal)
            logger.info("🔔 %s signal: %s @ %.2f (MA%.0f=%.2f, zone=[%.2f,%.2f])",
                        cache.signal_type, watch.symbol, price,
                        cache.ma_period, cache.ma_value,
                        cache.trigger_low, cache.trigger_high)
            return signal
        elif not in_zone:
            # Price left zone — reset so signal can fire again on re-entry
            cache.signal_fired = False

        return None

    # ─── Legacy (backward compat) ───

    def check_signal(self, df, watch: WatchItem, current_price: float) -> Optional[Signal]:
        """Legacy signal check — calculates MA from df each time.

        Kept for backward compatibility (demo mode, etc.). Use check_price() in production.
        """
        if not watch.enabled or df is None:
            return None

        length = len(df) if hasattr(df, '__len__') else 0
        if length < watch.ma_period + 1:
            return None

        ma = self.calculate_ma(df, watch.ma_period)

        if HAS_PANDAS and hasattr(ma, 'iloc'):
            if len(ma) < 2 or pd.isna(ma.iloc[-1]) or pd.isna(ma.iloc[-2]):
                return None
            current_ma = ma.iloc[-1]
            prev_ma = ma.iloc[-2]
        else:
            if len(ma) < 2 or ma[-1] is None or ma[-2] is None:
                return None
            current_ma = ma[-1]
            prev_ma = ma[-2]

        ma_rising = current_ma > prev_ma
        ma_falling = current_ma < prev_ma

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

        if ma_rising and current_ma <= current_price <= current_ma + watch.n_points:
            distance = current_price - current_ma
            signal = Signal(
                timestamp=now, watch_id=watch.id, symbol=watch.symbol,
                signal_type=SignalType.BUY, price=current_price,
                ma_value=round(current_ma, 4), ma_period=watch.ma_period,
                n_points=watch.n_points, distance=round(distance, 4),
            )
            self.signals.append(signal)
            return signal

        if ma_falling and current_ma - watch.n_points <= current_price <= current_ma:
            distance = current_ma - current_price
            signal = Signal(
                timestamp=now, watch_id=watch.id, symbol=watch.symbol,
                signal_type=SignalType.SELL, price=current_price,
                ma_value=round(current_ma, 4), ma_period=watch.ma_period,
                n_points=watch.n_points, distance=round(distance, 4),
            )
            self.signals.append(signal)
            return signal

        return None

    # ─── Getters ───

    def get_threshold(self, watch_id: str) -> Optional[Dict]:
        """Get threshold cache for a watch item (for API/debug)."""
        cache = self._thresholds.get(watch_id)
        if not cache:
            return None
        return {
            "ma_value": cache.ma_value,
            "prev_ma": cache.prev_ma,
            "ma_rising": cache.ma_rising,
            "trigger_low": cache.trigger_low,
            "trigger_high": cache.trigger_high,
            "signal_type": cache.signal_type,
            "ma_direction": cache.ma_direction,
            "signal_fired": cache.signal_fired,
            "last_calc": cache.last_calc,
            "last_price": cache.last_price,
        }

    def get_signals(self, limit: int = 50) -> List[Dict[str, Any]]:
        return [s.to_dict() for s in reversed(self.signals[-limit:])]

    def get_watch_list(self) -> List[Dict[str, Any]]:
        return [w.to_dict() for w in self.watch_list.values()]

    def get_latest_data(self) -> Dict[str, Any]:
        return self.latest_data

    def clear_signals(self):
        self.signals.clear()
