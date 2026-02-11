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
    confirm_ma_enabled: bool = False  # Enable MA confirmation
    confirm_ma_period: int = 55  # Confirmation MA period
    strategy_type: str = "MA"  # MA or BB (Bollinger Bands)
    bb_std_dev: float = 2.0  # Bollinger Bands standard deviation multiplier
    timeframe: str = "D"  # D=日線, W=週線, M=月線
    trading_config: Optional[Dict[str, Any]] = None  # Auto-trading config: {auto_trade, targets, exit}

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
    Real-time MA is calculated using historical closes + current price.
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
    qualified: bool = True  # whether watch is qualified based on initial price position

    # Display data for frontend
    ma_direction: str = "FLAT"
    n_points: float = 0.0
    ma_period: int = 0
    buy_zone: Optional[str] = None
    sell_zone: Optional[str] = None
    
    # Historical closes for real-time MA calculation (last N-1 closes, excludes today)
    hist_closes: List[float] = None  # type: ignore
    confirm_hist_closes: List[float] = None  # type: ignore
    
    # Confirmation MA
    confirm_ma_value: Optional[float] = None
    confirm_ma_direction: Optional[str] = None  # "RISING", "FALLING", "FLAT"
    confirm_ma_ok: bool = True  # Whether confirmation condition is met
    
    # Bollinger Bands
    strategy_type: str = "MA"  # MA or BB
    bb_std_dev: float = 2.0
    bb_upper: Optional[float] = None
    bb_lower: Optional[float] = None
    bb_middle: Optional[float] = None  # Same as MA


@dataclass
class ActiveTrade:
    """An active trade with entry and exit conditions."""
    id: str                          # unique trade id
    watch_id: str
    symbol: str
    direction: str                   # BUY or SELL
    entry_time: str                  # ISO timestamp
    orders: List[Dict[str, Any]]     # [{conId, symbol, strike, right, expiry, qty, avg_cost, ib_order_id}]
    exit_strategies: Dict[str, Any]  # {limit: {enabled, pts, dir, order_ids}, time: {enabled, value}, ma: {enabled, cond, dir, pts}, bb: {enabled, cond, target, dir, pts}}
    status: str = "pending"          # pending → filled → exiting → closed

    def to_dict(self):
        return asdict(self)


class StrategyEngine:
    def __init__(self):
        self.watch_list: Dict[str, WatchItem] = {}
        self.signals: List[Signal] = []
        self.latest_data: Dict[str, Dict[str, Any]] = {}  # watch_id -> frontend data
        self._thresholds: Dict[str, ThresholdCache] = {}   # watch_id -> cached thresholds
        self._candles: Dict[str, List[Dict]] = {}  # watch_id -> [{time, open, high, low, close}]
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
        self._candles.pop(watch_id, None)
        logger.info("Removed watch: %s", watch_id)

    def get_candles(self, watch_id: str) -> List[Dict]:
        """Get stored candles for a watch item."""
        return self._candles.get(watch_id, [])

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

    def calculate_std(self, df, period: int):
        """Calculate Standard Deviation. Works with pandas DataFrame or list of dicts."""
        if HAS_PANDAS and hasattr(df, 'rolling'):
            return df["close"].rolling(window=period).std()
        # Fallback: plain Python
        closes = [row["close"] for row in df] if isinstance(df, list) else list(df["close"])
        result = [None] * len(closes)
        for i in range(period - 1, len(closes)):
            window = closes[i - period + 1:i + 1]
            mean = sum(window) / period
            variance = sum((x - mean) ** 2 for x in window) / period
            result[i] = variance ** 0.5
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
            # Extract last N-1 closes for real-time MA (excludes today)
            hist_closes = list(df["close"].tail(watch.ma_period - 1).astype(float))
        else:
            if len(ma) < 2 or ma[-1] is None or ma[-2] is None:
                return None
            current_ma = float(ma[-1])
            prev_ma = float(ma[-2])
            # Extract last N-1 closes for real-time MA
            closes = [row["close"] for row in df] if isinstance(df, list) else list(df["close"])
            hist_closes = [float(c) for c in closes[-(watch.ma_period - 1):]]

        ma_rising = current_ma > prev_ma
        ma_falling = current_ma < prev_ma
        
        # Calculate Bollinger Bands if BB strategy
        bb_upper = None
        bb_lower = None
        bb_middle = current_ma
        current_std = 0.0
        
        if watch.strategy_type == "BB":
            std = self.calculate_std(df, watch.ma_period)
            if HAS_PANDAS and hasattr(std, 'iloc'):
                if not pd.isna(std.iloc[-1]):
                    current_std = float(std.iloc[-1])
            else:
                if std[-1] is not None:
                    current_std = float(std[-1])
            
            bb_upper = current_ma + watch.bb_std_dev * current_std
            bb_lower = current_ma - watch.bb_std_dev * current_std

        # Determine trigger zone based on strategy type
        trigger_low = 0.0
        trigger_high = 0.0
        signal_type = None
        buy_zone = None
        sell_zone = None

        if watch.strategy_type == "MA":
            # MA Strategy: price within N points of MA
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
        else:
            # BB Strategy: price approaches upper/lower band within N points
            # LONG: trigger when price <= lower band + N points (approaching from above)
            # SHORT: trigger when price >= upper band - N points (approaching from below)
            if watch.direction == "LONG" and bb_lower:
                trigger_low = 0  # No lower limit
                trigger_high = bb_lower + watch.n_points
                signal_type = "BUY"
                buy_zone = f"≤ {round(bb_lower + watch.n_points, 2)}"
            elif watch.direction == "SHORT" and bb_upper:
                trigger_low = bb_upper - watch.n_points
                trigger_high = float('inf')  # No upper limit
                signal_type = "SELL"
                sell_zone = f"≥ {round(bb_upper - watch.n_points, 2)}"

        ma_direction = "RISING" if ma_rising else ("FALLING" if ma_falling else "FLAT")

        # Calculate confirmation MA if enabled
        confirm_ma_value = None
        confirm_ma_direction = None
        confirm_ma_ok = True  # Default: no confirmation required
        confirm_hist_closes = None
        
        if watch.confirm_ma_enabled and watch.confirm_ma_period > 0:
            if length >= watch.confirm_ma_period + 1:
                confirm_ma = self.calculate_ma(df, watch.confirm_ma_period)
                if HAS_PANDAS and hasattr(confirm_ma, 'iloc'):
                    if len(confirm_ma) >= 2 and not pd.isna(confirm_ma.iloc[-1]) and not pd.isna(confirm_ma.iloc[-2]):
                        confirm_current = float(confirm_ma.iloc[-1])
                        confirm_prev = float(confirm_ma.iloc[-2])
                        confirm_ma_value = round(confirm_current, 4)
                        confirm_hist_closes = list(df["close"].tail(watch.confirm_ma_period - 1).astype(float))
                        if confirm_current > confirm_prev:
                            confirm_ma_direction = "RISING"
                        elif confirm_current < confirm_prev:
                            confirm_ma_direction = "FALLING"
                        else:
                            confirm_ma_direction = "FLAT"
                else:
                    if len(confirm_ma) >= 2 and confirm_ma[-1] is not None and confirm_ma[-2] is not None:
                        confirm_current = float(confirm_ma[-1])
                        confirm_prev = float(confirm_ma[-2])
                        confirm_ma_value = round(confirm_current, 4)
                        closes = [row["close"] for row in df] if isinstance(df, list) else list(df["close"])
                        confirm_hist_closes = [float(c) for c in closes[-(watch.confirm_ma_period - 1):]]
                        if confirm_current > confirm_prev:
                            confirm_ma_direction = "RISING"
                        elif confirm_current < confirm_prev:
                            confirm_ma_direction = "FALLING"
                        else:
                            confirm_ma_direction = "FLAT"
                
                # Check if confirmation condition is met
                if confirm_ma_direction:
                    if watch.direction == "LONG":
                        confirm_ma_ok = confirm_ma_direction == "RISING"
                    else:  # SHORT
                        confirm_ma_ok = confirm_ma_direction == "FALLING"

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
            hist_closes=hist_closes,
            confirm_hist_closes=confirm_hist_closes,
            confirm_ma_value=confirm_ma_value,
            confirm_ma_direction=confirm_ma_direction,
            confirm_ma_ok=confirm_ma_ok,
            strategy_type=watch.strategy_type,
            bb_std_dev=watch.bb_std_dev,
            bb_upper=round(bb_upper, 4) if bb_upper else None,
            bb_lower=round(bb_lower, 4) if bb_lower else None,
            bb_middle=round(bb_middle, 4) if bb_middle else None,
        )

        self._thresholds[watch_id] = cache

        # Store candles for chart (last 120 bars — about 6 months of daily data)
        candles = []
        if HAS_PANDAS and hasattr(df, 'iterrows'):
            for idx, row in df.tail(120).iterrows():
                ts = int(idx.timestamp()) if hasattr(idx, 'timestamp') else int(time.time())
                candles.append({
                    "time": ts,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                })
        else:
            # Fallback for list of dicts
            for row in (df[-120:] if len(df) > 120 else df):
                candles.append({
                    "time": int(row.get("time", time.time())),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                })
        self._candles[watch_id] = candles

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
        """Check if streaming price triggers a signal using real-time MA calculation.

        Called on every price update from IB streaming.
        Calculates MA using historical closes + current price for accurate trigger.
        Returns Signal if triggered, None otherwise.
        """
        cache = self._thresholds.get(watch_id)
        if not cache or not cache.signal_type:
            return None

        watch = self.watch_list.get(watch_id)
        if not watch or not watch.enabled:
            return None
        
        # ─── Initial qualification check (only on first price) ───
        # LONG: price must be ABOVE MA at startup (waiting for pullback)
        # SHORT: price must be BELOW MA at startup (waiting for rally)
        if cache.last_price == 0:
            # First price received — check if qualified
            if cache.strategy_type == "MA":
                # MA strategy: check against MA
                if watch.direction == "LONG" and price < cache.ma_value:
                    cache.qualified = False
                    logger.info("⚠️ %s 不合格: 啟動時價格 %.2f 在 MA%.0f=%.2f 之下 (LONG需在MA上方)",
                               watch.symbol, price, cache.ma_period, cache.ma_value)
                elif watch.direction == "SHORT" and price > cache.ma_value:
                    cache.qualified = False
                    logger.info("⚠️ %s 不合格: 啟動時價格 %.2f 在 MA%.0f=%.2f 之上 (SHORT需在MA下方)",
                               watch.symbol, price, cache.ma_period, cache.ma_value)
            else:
                # BB strategy: check against middle band (MA)
                if watch.direction == "LONG" and price < cache.ma_value:
                    cache.qualified = False
                    logger.info("⚠️ %s 不合格: 啟動時價格 %.2f 在中軌 %.2f 之下 (LONG需在中軌上方)",
                               watch.symbol, price, cache.ma_value)
                elif watch.direction == "SHORT" and price > cache.ma_value:
                    cache.qualified = False
                    logger.info("⚠️ %s 不合格: 啟動時價格 %.2f 在中軌 %.2f 之上 (SHORT需在中軌下方)",
                               watch.symbol, price, cache.ma_value)
        
        # If not qualified, skip signal checking
        if not cache.qualified:
            cache.last_price = price
            return None
        
        # Calculate real-time MA using historical closes + current price
        if cache.hist_closes:
            realtime_ma = (sum(cache.hist_closes) + price) / len(cache.hist_closes + [price])
            prev_ma = cache.ma_value  # Yesterday's MA as reference for direction
        else:
            realtime_ma = cache.ma_value  # Fallback to cached MA
            prev_ma = cache.prev_ma
        
        # Determine real-time MA direction
        ma_rising = realtime_ma > prev_ma
        ma_falling = realtime_ma < prev_ma
        ma_direction = "RISING" if ma_rising else ("FALLING" if ma_falling else "FLAT")
        
        # Calculate real-time Bollinger Bands if BB strategy
        bb_upper = cache.bb_upper
        bb_lower = cache.bb_lower
        bb_middle = realtime_ma
        
        if cache.strategy_type == "BB" and cache.hist_closes:
            # Calculate real-time standard deviation
            all_closes = cache.hist_closes + [price]
            mean = sum(all_closes) / len(all_closes)
            variance = sum((x - mean) ** 2 for x in all_closes) / len(all_closes)
            realtime_std = variance ** 0.5
            bb_upper = realtime_ma + cache.bb_std_dev * realtime_std
            bb_lower = realtime_ma - cache.bb_std_dev * realtime_std
        
        # Determine trigger zone based on strategy type
        trigger_low = 0.0
        trigger_high = 0.0
        signal_type = None
        buy_zone = None
        sell_zone = None
        
        if cache.strategy_type == "MA":
            # MA Strategy: price within N points of MA (only when MA direction matches)
            if ma_rising:
                trigger_low = realtime_ma
                trigger_high = realtime_ma + watch.n_points
                signal_type = "BUY"
                buy_zone = f"{round(realtime_ma, 2)} ~ {round(realtime_ma + watch.n_points, 2)}"
            elif ma_falling:
                trigger_low = realtime_ma - watch.n_points
                trigger_high = realtime_ma
                signal_type = "SELL"
                sell_zone = f"{round(realtime_ma - watch.n_points, 2)} ~ {round(realtime_ma, 2)}"
        else:
            # BB Strategy: price approaches upper/lower band within N points
            if watch.direction == "LONG" and bb_lower:
                trigger_low = 0
                trigger_high = bb_lower + watch.n_points
                signal_type = "BUY"
                buy_zone = f"≤ {round(bb_lower + watch.n_points, 2)}"
            elif watch.direction == "SHORT" and bb_upper:
                trigger_low = bb_upper - watch.n_points
                trigger_high = float('inf')
                signal_type = "SELL"
                sell_zone = f"≥ {round(bb_upper - watch.n_points, 2)}"
        
        # Calculate real-time confirmation MA if enabled
        confirm_ma_value = cache.confirm_ma_value
        confirm_ma_direction = cache.confirm_ma_direction
        confirm_ma_ok = True
        
        if watch.confirm_ma_enabled and cache.confirm_hist_closes:
            confirm_realtime_ma = (sum(cache.confirm_hist_closes) + price) / len(cache.confirm_hist_closes + [price])
            confirm_prev_ma = cache.confirm_ma_value or confirm_realtime_ma
            confirm_ma_value = round(confirm_realtime_ma, 4)
            
            if confirm_realtime_ma > confirm_prev_ma:
                confirm_ma_direction = "RISING"
            elif confirm_realtime_ma < confirm_prev_ma:
                confirm_ma_direction = "FALLING"
            else:
                confirm_ma_direction = "FLAT"
            
            # Check if confirmation condition is met
            if watch.direction == "LONG":
                confirm_ma_ok = confirm_ma_direction == "RISING"
            else:  # SHORT
                confirm_ma_ok = confirm_ma_direction == "FALLING"

        # Update latest_data for frontend display
        distance = round(price - realtime_ma, 4)
        self.latest_data[watch_id] = {
            "symbol": watch.symbol,
            "current_price": price,
            "ma_value": round(realtime_ma, 4),
            "prev_ma": round(prev_ma, 4),
            "ma_period": cache.ma_period,
            "ma_direction": ma_direction,
            "n_points": cache.n_points,
            "distance_from_ma": distance,
            "buy_zone": buy_zone,
            "sell_zone": sell_zone,
            "last_updated": datetime.now().isoformat(),
            # Confirmation MA info
            "confirm_ma_enabled": watch.confirm_ma_enabled,
            "confirm_ma_period": watch.confirm_ma_period,
            "confirm_ma_value": confirm_ma_value,
            "confirm_ma_direction": confirm_ma_direction,
            "confirm_ma_ok": confirm_ma_ok,
            # Bollinger Bands info
            "strategy_type": cache.strategy_type,
            "bb_upper": round(bb_upper, 4) if bb_upper else None,
            "bb_lower": round(bb_lower, 4) if bb_lower else None,
            "bb_middle": round(bb_middle, 4) if bb_middle else None,
            # Qualification status
            "qualified": cache.qualified,
        }

        cache.last_price = price

        # Direction filter: LONG only triggers on BUY, SHORT only on SELL
        if watch.direction == "LONG" and signal_type != "BUY":
            return None
        if watch.direction == "SHORT" and signal_type != "SELL":
            return None

        # Check if price is in trigger zone
        in_zone = trigger_low <= price <= trigger_high
        
        # Check confirmation MA condition (if enabled, for both MA and BB strategies)
        if not confirm_ma_ok:
            # Confirmation MA direction doesn't match — don't trigger
            return None

        if in_zone and not cache.signal_fired:
            # 🔔 Signal fires! (one-shot: won't fire again until manually reset)
            cache.signal_fired = True
            signal = Signal(
                timestamp=datetime.now().isoformat(),
                watch_id=watch_id,
                symbol=watch.symbol,
                signal_type=signal_type,
                price=price,
                ma_value=round(realtime_ma, 4),
                ma_period=cache.ma_period,
                n_points=cache.n_points,
                distance=abs(distance),
            )
            self.signals.append(signal)
            logger.info("🔔 %s signal: %s @ %.2f (MA%.0f=%.2f, zone=[%.2f,%.2f]) — 已停止檢查",
                        signal_type, watch.symbol, price,
                        cache.ma_period, realtime_ma,
                        trigger_low, trigger_high)
            return signal
        # Signal already fired — don't check anymore (one-shot mode)

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
