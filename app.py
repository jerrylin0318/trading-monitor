"""Trading Monitor — FastAPI application with WebSocket real-time updates.

Architecture (event-driven):
  - Streaming prices from IB (reqMktData, continuous)
  - 1-second loop reads cached prices → compares with pre-calculated thresholds
  - Hourly: recalculate MA + thresholds from daily bars
  - Signal fires instantly when price enters trigger zone
"""

import asyncio
import json
import logging
import math
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Set, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from strategy import StrategyEngine, WatchItem, SignalType

# Only import IB when not in demo mode
DEMO_MODE = os.environ.get("DEMO_MODE", "false").lower() in ("true", "1", "yes")

if not DEMO_MODE:
    # Patch asyncio for ib_insync compatibility
    from ib_insync import util
    util.patchAsyncio()
    from ib_manager import IBManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# --- State ---
ib = None if DEMO_MODE else IBManager(host="127.0.0.1", port=int(os.environ.get("IB_PORT", "7496")), client_id=10)
engine = StrategyEngine()
ws_clients: Set[WebSocket] = set()
monitor_task: Optional[asyncio.Task] = None
CONFIG_FILE = Path(__file__).parent / "config.json"

# --- Demo data ---
DEMO_ACCOUNT = {
    "NetLiquidation": {"value": "125430.50", "currency": "USD"},
    "AvailableFunds": {"value": "98200.75", "currency": "USD"},
    "BuyingPower": {"value": "392803.00", "currency": "USD"},
    "UnrealizedPnL": {"value": "1250.30", "currency": "USD"},
    "TotalCashValue": {"value": "85000.00", "currency": "USD"},
    "GrossPositionValue": {"value": "40430.50", "currency": "USD"},
}
DEMO_POSITIONS = [
    {"symbol": "SPY", "secType": "STK", "exchange": "SMART", "currency": "USD",
     "strike": None, "right": None, "expiry": None, "position": 50.0,
     "avgCost": 598.25, "marketPrice": 602.10, "marketValue": 30105.0,
     "unrealizedPNL": 192.50, "realizedPNL": 0.0},
    {"symbol": "QQQ", "secType": "OPT", "exchange": "SMART", "currency": "USD",
     "strike": 520.0, "right": "C", "expiry": "20260220", "position": 5.0,
     "avgCost": 8.50, "marketPrice": 10.60, "marketValue": 5300.0,
     "unrealizedPNL": 1050.0, "realizedPNL": 0.0},
]


# --- Config persistence ---
def load_config():
    """Load watch list and settings from config.json."""
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text())
            for item_data in data.get("watch_list", []):
                # Backward compat: remove deprecated 'strategy' field
                item_data.pop("strategy", None)
                item = WatchItem(**item_data)
                engine.add_watch(item)
            logger.info("Loaded %d watch items from config", len(engine.watch_list))
        except Exception as e:
            logger.error("Failed to load config: %s", e)


def save_config():
    """Save current watch list to config.json."""
    try:
        data = {
            "watch_list": engine.get_watch_list(),
            "saved_at": datetime.now().isoformat(),
        }
        CONFIG_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        logger.error("Failed to save config: %s", e)


# --- WebSocket broadcast ---
async def broadcast(msg: Dict[str, Any]):
    """Send message to all connected WebSocket clients."""
    if not ws_clients:
        return
    text = json.dumps(msg, default=str)
    disconnected = set()
    for ws in list(ws_clients):  # iterate copy to avoid "Set changed size" error
        try:
            await ws.send_text(text)
        except Exception:
            disconnected.add(ws)
    ws_clients.difference_update(disconnected)


# --- Options cache ---
_options_cache: Dict[str, Dict] = {}  # watch_id -> {"call": [...], "put": [...], "ma_price": float}

# --- Active trades ---
_active_trades: Dict[str, Dict] = {}  # trade_id -> trade dict


async def _init_new_watch(watch):
    """Initialize a newly added watch item during live monitoring.

    Steps: fetch daily bars → calculate thresholds → subscribe streaming → cache options.
    """
    try:
        df = await ib.get_daily_bars(
            watch.symbol, watch.sec_type, watch.exchange, watch.currency,
            contract_month=watch.contract_month
        )
        if df is None or len(df) < watch.ma_period + 1:
            logger.warning("Insufficient data for new watch %s", watch.symbol)
            await broadcast({"type": "error", "message": f"{watch.symbol} 數據不足，請確認交易所和合約月份"})
            return

        # Calculate MA + trigger thresholds
        cache = engine.calculate_thresholds(watch.id, df, watch)
        if not cache:
            await broadcast({"type": "error", "message": f"{watch.symbol} MA 計算失敗"})
            return

        # Subscribe to streaming prices
        await ib.subscribe_price(
            watch.id, watch.symbol, watch.sec_type, watch.exchange,
            watch.currency, watch.contract_month
        )

        # Cache option contracts
        await cache_options_for_watch(watch.id, watch, cache.ma_value)

        # Build and broadcast initial data
        data = engine.latest_data.get(watch.id, {})
        if watch.id in _options_cache:
            data["options_call"] = _options_cache[watch.id]["call"]
            data["options_put"] = _options_cache[watch.id]["put"]
            data["locked_ma"] = cache.ma_value

        await broadcast({"type": "data_update", "watch_id": watch.id, "data": data})
        logger.info("Initialized new watch %s: MA%.0f=%.2f %s, zone=[%.2f,%.2f]",
                     watch.symbol, cache.ma_period, cache.ma_value,
                     cache.ma_direction, cache.trigger_low, cache.trigger_high)
    except Exception as e:
        logger.error("Failed to init new watch %s: %s", watch.symbol, e)
        await broadcast({"type": "error", "message": f"{watch.symbol} 初始化失敗: {e}"})


def _group_options(flat_list: list) -> Dict:
    """Group flat option list into {expiry: {expiry: {value, label}, options: [...]}}."""
    grouped = {}
    for o in flat_list:
        exp = o["expiry"]
        if exp not in grouped:
            grouped[exp] = {
                "expiry": {"value": exp, "label": o.get("expiryLabel", f"{exp[4:6]}/{exp[6:8]}")},
                "options": [],
            }
        # Don't send internal _contract object to frontend
        clean = {k: v for k, v in o.items() if k != "_contract"}
        grouped[exp]["options"].append(clean)
    return grouped


async def cache_options_for_watch(watch_id: str, watch, ma_price: float, fetch_prices: bool = True):
    """Fetch and cache option contracts based on direction, optionally with initial prices.
    
    LONG → only Call (buy calls on BUY signal)
    SHORT → only Put (buy puts on SELL signal)
    3 nearest expirations × 5 OTM strikes ≈ 15 contracts per watch item.
    """
    try:
        direction = getattr(watch, 'direction', 'LONG')
        calls = []
        puts = []
        
        if direction == "LONG":
            calls = await ib.get_option_chain(
                watch.symbol, watch.sec_type, watch.exchange, watch.currency,
                ma_price=ma_price, right="C", num_strikes=5,
                contract_month=watch.contract_month, num_expirations=3
            )
        else:  # SHORT
            puts = await ib.get_option_chain(
                watch.symbol, watch.sec_type, watch.exchange, watch.currency,
                ma_price=ma_price, right="P", num_strikes=5,
                contract_month=watch.contract_month, num_expirations=3
            )
        
        # Fetch initial prices (batch snapshot)
        if fetch_prices and (calls or puts):
            if calls:
                calls = await ib.refresh_option_prices(calls)
            if puts:
                puts = await ib.refresh_option_prices(puts)
            logger.info("Fetched initial prices for %s options", watch.symbol)
        
        _options_cache[watch_id] = {
            "call_raw": calls, "put_raw": puts,
            "call": _group_options(calls), "put": _group_options(puts),
            "ma_price": ma_price,
        }
        total = len(calls) + len(puts)
        side = "calls" if direction == "LONG" else "puts"
        logger.info("Cached %d %s for %s (direction=%s, MA=%.2f)",
                     total, side, watch.symbol, direction, ma_price)
    except Exception as e:
        logger.error("Failed to cache options for %s: %s", watch.symbol, e)


# --- Monitor loop (event-driven) ---
async def monitor_loop():
    """Main monitoring loop — event-driven architecture.

    Phase 1: Fetch daily bars → calculate thresholds → subscribe streaming → cache options
    Phase 2: Read streaming prices every ~1s → check thresholds → trigger signals
    Hourly:  Recalculate MA + thresholds from fresh daily bars
    """
    logger.info("Monitor loop started (event-driven architecture)")

    # ── Phase 1: Initialize all watch items ──
    initialized = set()
    for watch_id, watch in list(engine.watch_list.items()):
        if not watch.enabled:
            continue
        try:
            # Fetch daily bars for MA calculation
            df = await ib.get_daily_bars(
                watch.symbol, watch.sec_type, watch.exchange, watch.currency,
                contract_month=watch.contract_month
            )
            if df is None or len(df) < watch.ma_period + 1:
                logger.warning("Insufficient data for %s", watch.symbol)
                continue

            # Calculate MA + trigger thresholds (stored in engine._thresholds)
            cache = engine.calculate_thresholds(watch_id, df, watch)
            if not cache:
                continue

            # Subscribe to streaming price data (continuous, not snapshot)
            subscribed = await ib.subscribe_price(
                watch_id, watch.symbol, watch.sec_type, watch.exchange,
                watch.currency, watch.contract_month
            )
            if not subscribed:
                logger.warning("Failed to subscribe %s", watch.symbol)
                continue

            # Cache option contracts
            await cache_options_for_watch(watch_id, watch, cache.ma_value)

            # Broadcast initial data
            data = engine.latest_data.get(watch_id, {})
            if watch_id in _options_cache:
                data["options_call"] = _options_cache[watch_id]["call"]
                data["options_put"] = _options_cache[watch_id]["put"]
                data["locked_ma"] = cache.ma_value

            await broadcast({"type": "data_update", "watch_id": watch_id, "data": data})
            initialized.add(watch_id)
            logger.info("Initialized %s: MA%.0f=%.2f %s, zone=[%.2f,%.2f]",
                         watch.symbol, cache.ma_period, cache.ma_value,
                         cache.ma_direction, cache.trigger_low, cache.trigger_high)
        except Exception as e:
            logger.error("Failed to init %s: %s", watch.symbol, e)

    await broadcast({"type": "status", "connected": True, "monitoring": True,
                     "message": f"監控已啟動 ({len(initialized)}/{len(engine.watch_list)} 標的)"})

    # ── Phase 2: Event-driven price monitoring ──
    last_calc_time = time.time()
    last_account_time = 0
    last_broadcast_prices: Dict[str, float] = {}  # track last broadcast price to avoid spam

    while engine.running:
        try:
            # Reconnect if needed
            if not ib.connected:
                await broadcast({"type": "status", "connected": False, "message": "IB 斷線，重連中..."})
                connected = await ib.connect()
                if not connected:
                    await asyncio.sleep(10)
                    continue

            # Read all streaming prices (fast — no API calls, just reads cached ticker values)
            prices = await ib.read_prices()

            for watch_id, price in prices.items():
                watch = engine.watch_list.get(watch_id)
                if not watch or not watch.enabled:
                    continue

                # Skip broadcast if price unchanged (avoid WebSocket spam)
                old_price = last_broadcast_prices.get(watch_id, 0)
                price_changed = abs(price - old_price) >= 0.001

                # Check signal using pre-calculated thresholds (just a comparison)
                signal = engine.check_price(watch_id, price)

                # Broadcast data_update when price changes
                if price_changed or signal:
                    data = engine.latest_data.get(watch_id, {})

                    # Include cached options
                    if watch_id in _options_cache:
                        data["options_call"] = _options_cache[watch_id]["call"]
                        data["options_put"] = _options_cache[watch_id]["put"]
                        data["locked_ma"] = _options_cache[watch_id]["ma_price"]

                    await broadcast({"type": "data_update", "watch_id": watch_id, "data": data})
                    last_broadcast_prices[watch_id] = price

                if signal:
                    # 🔔 Signal triggered! Refresh option prices for order-ready data
                    opt_cache = _options_cache.get(watch_id)
                    if opt_cache:
                        if opt_cache["call_raw"]:
                            await ib.refresh_option_prices(opt_cache["call_raw"])
                            opt_cache["call"] = _group_options(opt_cache["call_raw"])
                        if opt_cache["put_raw"]:
                            await ib.refresh_option_prices(opt_cache["put_raw"])
                            opt_cache["put"] = _group_options(opt_cache["put_raw"])
                        data["options_call"] = opt_cache["call"]
                        data["options_put"] = opt_cache["put"]
                        await broadcast({"type": "data_update", "watch_id": watch_id, "data": data})
                        logger.info("Signal triggered: refreshed %s option prices", watch.symbol)

                    await broadcast({
                        "type": "signal",
                        "signal": signal.to_dict(),
                        "options": None,
                        "underlying": {"symbol": watch.symbol, "price": price, "sec_type": watch.sec_type},
                    })

            # ── Exit strategy monitoring ──
            for trade_id, trade in list(_active_trades.items()):
                if trade["status"] not in ("filled", "exiting"):
                    continue

                exit_cfg = trade.get("exit", {})
                watch_id = trade["watch_id"]

                # Time-based exit
                time_exit = exit_cfg.get("time", {})
                if time_exit.get("enabled"):
                    exit_time_str = time_exit.get("value", "")  # "HH:MM"
                    if exit_time_str:
                        now_time = datetime.now().strftime("%H:%M")
                        if now_time >= exit_time_str and trade["status"] != "exiting":
                            trade["status"] = "exiting"
                            logger.info("⏰ Time exit triggered for trade %s at %s", trade_id, now_time)
                            for order_info in trade["orders"]:
                                if order_info.get("conId") and order_info.get("qty_requested"):
                                    try:
                                        close_result = await ib.place_market_order(order_info["conId"], "SELL", order_info["qty_requested"])
                                        order_info["exit_order"] = close_result
                                    except Exception as e:
                                        logger.error("Time exit order failed: %s", e)
                            trade["status"] = "closed"
                            await broadcast({"type": "trade_update", "trade": trade})

                # MA-based exit
                ma_exit = exit_cfg.get("ma", {})
                if ma_exit.get("enabled") and trade["status"] != "exiting":
                    threshold = engine._thresholds.get(watch_id)
                    if threshold and threshold.last_price > 0:
                        current_price = threshold.last_price
                        ma_val = threshold.ma_value
                        cond = ma_exit.get("cond", "above")  # "above" or "below"
                        offset_dir = ma_exit.get("dir", "+")
                        offset_pts = float(ma_exit.get("pts", 5))

                        if offset_dir == "+":
                            target = ma_val + offset_pts
                        else:
                            target = ma_val - offset_pts

                        triggered = False
                        if cond == "above" and current_price > target:
                            triggered = True
                        elif cond == "below" and current_price < target:
                            triggered = True

                        if triggered:
                            trade["status"] = "exiting"
                            logger.info("📊 MA exit triggered for trade %s: price=%.2f target=%.2f", trade_id, current_price, target)
                            for order_info in trade["orders"]:
                                if order_info.get("conId") and order_info.get("qty_requested"):
                                    try:
                                        close_result = await ib.place_market_order(order_info["conId"], "SELL", order_info["qty_requested"])
                                        order_info["exit_order"] = close_result
                                    except Exception as e:
                                        logger.error("MA exit order failed: %s", e)
                            trade["status"] = "closed"
                            await broadcast({"type": "trade_update", "trade": trade})

            # ── Hourly recalculation ──
            now = time.time()
            if now - last_calc_time >= 3600:
                logger.info("⏰ Hourly recalculation started")
                for watch_id, watch in list(engine.watch_list.items()):
                    if not watch.enabled:
                        continue
                    try:
                        df = await ib.get_daily_bars(
                            watch.symbol, watch.sec_type, watch.exchange, watch.currency,
                            contract_month=watch.contract_month
                        )
                        if df is not None and len(df) >= watch.ma_period + 1:
                            new_cache = engine.calculate_thresholds(watch_id, df, watch)
                            if new_cache:
                                # Re-cache options if MA shifted significantly
                                old_ma = _options_cache.get(watch_id, {}).get("ma_price", 0)
                                if abs(new_cache.ma_value - old_ma) > watch.n_points * 0.5:
                                    await cache_options_for_watch(watch_id, watch, new_cache.ma_value)
                                    logger.info("Re-cached options for %s (MA shifted)", watch.symbol)
                    except Exception as e:
                        logger.error("Hourly recalc error for %s: %s", watch.symbol, e)
                last_calc_time = now
                logger.info("⏰ Hourly recalculation complete")

            # ── Account update every 5 minutes ──
            if now - last_account_time >= 300:
                try:
                    account = await ib.get_account_summary()
                    positions = await ib.get_positions()
                    await broadcast({"type": "account", "summary": account, "positions": positions, "connected": True})
                    last_account_time = now
                except Exception as e:
                    logger.error("Account data error: %s", e)

        except Exception as e:
            logger.error("Monitor loop error: %s", e)
            await broadcast({"type": "error", "message": str(e)})

        await asyncio.sleep(1)  # 1-second tick — reads cached prices, no heavy API calls

    # Cleanup: unsubscribe all price streams
    await ib.unsubscribe_all()
    logger.info("Monitor loop stopped")


# --- Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    load_config()
    if DEMO_MODE:
        logger.info("🎮 Running in DEMO MODE — no IB connection required")
    yield
    engine.stop()
    if ib:
        await ib.disconnect()
    save_config()


# --- App ---
app = FastAPI(title="Trading Monitor", lifespan=lifespan)


# --- Pydantic models ---
class OrderRequest(BaseModel):
    watch_id: str
    items: List[Dict[str, Any]]  # [{conId, right, strike, expiry, amount, ask}]
    exit: Dict[str, Any] = {}     # {limit: {enabled, dir, pts}, time: {enabled, value}, ma: {enabled, cond, dir, pts}}


class WatchItemCreate(BaseModel):
    symbol: str
    sec_type: str = "STK"
    exchange: str = "SMART"
    currency: str = "USD"
    ma_period: int = 21
    n_points: float = 5.0
    enabled: bool = True
    contract_month: str = ""
    direction: str = "LONG"


class WatchItemUpdate(BaseModel):
    symbol: Optional[str] = None
    sec_type: Optional[str] = None
    exchange: Optional[str] = None
    currency: Optional[str] = None
    ma_period: Optional[int] = None
    n_points: Optional[float] = None
    enabled: Optional[bool] = None
    contract_month: Optional[str] = None
    direction: Optional[str] = None


# --- API Routes ---
@app.get("/api/status")
async def get_status():
    connected = ib.connected if ib else DEMO_MODE
    return {
        "connected": connected,
        "monitoring": engine.running,
        "watch_count": len(engine.watch_list),
        "signal_count": len(engine.signals),
        "demo_mode": DEMO_MODE,
        "timestamp": datetime.now().isoformat(),
    }


@app.post("/api/connect")
async def connect_ib():
    if DEMO_MODE:
        return {"connected": True, "demo": True}
    result = await ib.connect()
    return {"connected": result}


@app.post("/api/disconnect")
async def disconnect_ib():
    if DEMO_MODE:
        return {"connected": False, "demo": True}
    await ib.disconnect()
    return {"connected": False}


@app.get("/api/account")
async def get_account():
    if DEMO_MODE:
        return {"summary": DEMO_ACCOUNT, "positions": DEMO_POSITIONS}
    summary = await ib.get_account_summary()
    positions = await ib.get_positions()
    return {"summary": summary, "positions": positions}


@app.get("/api/positions")
async def get_positions():
    if DEMO_MODE:
        return DEMO_POSITIONS
    return await ib.get_positions()


# --- Watch list ---
@app.get("/api/watch")
async def get_watch_list():
    return engine.get_watch_list()


@app.post("/api/watch")
async def add_watch(item: WatchItemCreate):
    watch = WatchItem(
        id=str(uuid.uuid4())[:8],
        symbol=item.symbol.upper(),
        sec_type=item.sec_type,
        exchange=item.exchange,
        currency=item.currency,
        ma_period=item.ma_period,
        n_points=item.n_points,
        enabled=item.enabled,
        contract_month=item.contract_month,
        direction=item.direction,
    )
    engine.add_watch(watch)
    save_config()
    await broadcast({"type": "watch_update", "watch_list": engine.get_watch_list()})
    
    # If monitoring, initialize the new watch (fetch data + cache options)
    if engine.running and not DEMO_MODE and ib and ib.connected:
        asyncio.create_task(_init_new_watch(watch))
    
    return watch.to_dict()


@app.put("/api/watch/{watch_id}")
async def update_watch(watch_id: str, updates: WatchItemUpdate):
    update_dict = {k: v for k, v in updates.model_dump().items() if v is not None}
    engine.update_watch(watch_id, update_dict)
    save_config()
    await broadcast({"type": "watch_update", "watch_list": engine.get_watch_list()})
    return {"ok": True}


@app.delete("/api/watch/{watch_id}")
async def delete_watch(watch_id: str):
    # Unsubscribe price stream
    if not DEMO_MODE and ib and engine.running:
        await ib.unsubscribe_price(watch_id)
    _options_cache.pop(watch_id, None)
    engine.remove_watch(watch_id)
    save_config()
    await broadcast({"type": "watch_update", "watch_list": engine.get_watch_list()})
    return {"ok": True}


# --- Strategy control ---
@app.post("/api/start")
async def start_monitoring():
    global monitor_task
    if engine.running:
        return {"status": "already running"}
    if DEMO_MODE:
        engine.start()
        monitor_task = asyncio.create_task(demo_monitor_loop())
        return {"status": "started", "demo": True}
    engine.start()
    monitor_task = asyncio.create_task(monitor_loop())
    return {"status": "started"}


@app.post("/api/stop")
async def stop_monitoring():
    global monitor_task
    engine.stop()
    if not DEMO_MODE and ib:
        await ib.unsubscribe_all()
    if monitor_task:
        monitor_task.cancel()
        monitor_task = None
    return {"status": "stopped"}


# --- Signals ---
@app.get("/api/signals")
async def get_signals(limit: int = 50):
    return engine.get_signals(limit)


@app.delete("/api/signals")
async def clear_signals():
    engine.clear_signals()
    return {"ok": True}


# --- Market data ---
@app.get("/api/data")
async def get_latest_data():
    return engine.get_latest_data()


@app.get("/api/thresholds")
async def get_thresholds():
    """Debug endpoint: show pre-calculated thresholds for all watch items."""
    result = {}
    for watch_id in engine.watch_list:
        t = engine.get_threshold(watch_id)
        if t:
            result[watch_id] = t
    return result


@app.get("/api/options/{symbol}")
async def get_options(symbol: str, right: str = "C", ma_price: float = 0, sec_type: str = "STK",
                      exchange: str = "SMART", currency: str = "USD", num_strikes: int = 5):
    if DEMO_MODE:
        return _demo_options(symbol.upper(), right, ma_price, num_strikes)
    options = await ib.get_option_chain(symbol.upper(), sec_type, exchange, currency, ma_price, right, num_strikes)
    return options


@app.post("/api/options/refresh/{watch_id}")
async def refresh_watch_options(watch_id: str):
    """Re-cache options for a watch item based on current MA, then refresh prices."""
    if DEMO_MODE:
        return {"ok": True}
    watch = engine.watch_list.get(watch_id)
    if not watch:
        return {"error": "Watch not found"}
    
    data = engine.latest_data.get(watch_id, {})
    ma_price = data.get("ma_value", 0)
    if ma_price <= 0:
        return {"error": "No MA data yet"}
    
    # Re-fetch contracts based on current MA
    await cache_options_for_watch(watch_id, watch, ma_price)
    
    # Refresh prices
    cache = _options_cache.get(watch_id)
    if cache:
        if cache.get("call_raw"):
            await ib.refresh_option_prices(cache["call_raw"])
            cache["call"] = _group_options(cache["call_raw"])
        if cache.get("put_raw"):
            await ib.refresh_option_prices(cache["put_raw"])
            cache["put"] = _group_options(cache["put_raw"])
        
        data["options_call"] = cache.get("call", {})
        data["options_put"] = cache.get("put", {})
        data["locked_ma"] = ma_price
        
        await broadcast({"type": "data_update", "watch_id": watch_id, "data": data})
    
    total = len(cache.get("call_raw", [])) + len(cache.get("put_raw", []))
    return {"ok": True, "total": total}


@app.post("/api/options/prices/{watch_id}")
async def update_option_prices(watch_id: str, expiry: Optional[str] = None):
    """Update prices for cached options. If expiry given, only refresh that expiry's options."""
    if DEMO_MODE:
        return {"ok": True}
    cache = _options_cache.get(watch_id)
    if not cache:
        return {"error": "No cached options"}

    if expiry:
        # Refresh only the specified expiry's options (fast — ~5 contracts)
        call_subset = [o for o in cache.get("call_raw", []) if o["expiry"] == expiry]
        put_subset = [o for o in cache.get("put_raw", []) if o["expiry"] == expiry]
        if call_subset:
            await ib.refresh_option_prices(call_subset)
        if put_subset:
            await ib.refresh_option_prices(put_subset)
        # Regroup all (includes freshly updated subset)
        if cache.get("call_raw"):
            cache["call"] = _group_options(cache["call_raw"])
        if cache.get("put_raw"):
            cache["put"] = _group_options(cache["put_raw"])
        refreshed = len(call_subset) + len(put_subset)
        logger.info("Refreshed prices for %s expiry %s (%d contracts)", watch_id, expiry, refreshed)
    else:
        # Refresh all expirations
        if cache.get("call_raw"):
            await ib.refresh_option_prices(cache["call_raw"])
            cache["call"] = _group_options(cache["call_raw"])
        if cache.get("put_raw"):
            await ib.refresh_option_prices(cache["put_raw"])
            cache["put"] = _group_options(cache["put_raw"])

    data = engine.latest_data.get(watch_id, {})
    data["options_call"] = cache.get("call", {})
    data["options_put"] = cache.get("put", {})

    await broadcast({"type": "data_update", "watch_id": watch_id, "data": data})
    return {"ok": True, "expiry": expiry}


# --- Order / Trade endpoints ---

@app.post("/api/order")
async def place_order(req: OrderRequest):
    """Place market orders for selected options + set up exit strategies."""
    if DEMO_MODE:
        return {"ok": True, "demo": True}

    watch = engine.watch_list.get(req.watch_id)
    if not watch:
        return {"error": "Watch not found"}

    # Look up multiplier from options cache
    opt_cache = _options_cache.get(req.watch_id, {})
    cached_opts = (opt_cache.get("call_raw") or []) + (opt_cache.get("put_raw") or [])
    multiplier_map = {o.get("conId"): o.get("multiplier", 100) for o in cached_opts if o.get("conId")}

    results = []
    for item in req.items:
        con_id = item.get("conId")
        ask = item.get("ask", 0)
        amount = item.get("amount", 1000)

        if not con_id or ask <= 0:
            results.append({"conId": con_id, "error": "Invalid conId or ask price"})
            continue

        # Calculate quantity: amount / (ask × multiplier)
        multiplier = multiplier_map.get(con_id, 100)
        qty = max(1, int(amount / (ask * multiplier)))

        # Determine action: BUY for calls/puts entry
        action = "BUY"

        order_result = await ib.place_market_order(con_id, action, qty)
        order_result["qty_requested"] = qty
        order_result["conId"] = con_id
        results.append(order_result)

    # Create active trade record
    trade_id = str(uuid.uuid4())[:8]
    trade = {
        "id": trade_id,
        "watch_id": req.watch_id,
        "symbol": watch.symbol,
        "entry_time": datetime.now().isoformat(),
        "orders": results,
        "exit": req.exit,
        "status": "filled",
    }
    _active_trades[trade_id] = trade

    # If limit exit enabled, place limit orders for each filled order
    exit_cfg = req.exit
    if exit_cfg.get("limit", {}).get("enabled"):
        limit_dir = exit_cfg["limit"].get("dir", "+")
        limit_pts = float(exit_cfg["limit"].get("pts", 0.5))
        for r in results:
            if r.get("status") in ("Filled", "PreSubmitted", "Submitted") and r.get("avgFillPrice", 0) > 0:
                fill_price = r["avgFillPrice"]
                if limit_dir == "+":
                    limit_price = round(fill_price + limit_pts, 2)
                else:
                    limit_price = round(fill_price - limit_pts, 2)
                limit_result = await ib.place_limit_order(r["conId"], "SELL", r.get("qty_requested", 1), limit_price)
                r["exit_limit_order"] = limit_result

    await broadcast({"type": "trade_update", "trade": trade})
    logger.info("Order placed: trade=%s, %d items for %s", trade_id, len(results), watch.symbol)
    return {"ok": True, "trade_id": trade_id, "orders": results}


@app.get("/api/trades")
async def get_trades():
    """Get all active trades."""
    return list(_active_trades.values())


@app.post("/api/trades/{trade_id}/close")
async def close_trade(trade_id: str):
    """Manually close a trade by selling all positions."""
    trade = _active_trades.get(trade_id)
    if not trade:
        return {"error": "Trade not found"}

    if trade["status"] in ("closed", "exiting"):
        return {"error": f"Trade already {trade['status']}"}

    if DEMO_MODE:
        trade["status"] = "closed"
        return {"ok": True, "demo": True}

    trade["status"] = "exiting"
    close_results = []
    for order_info in trade["orders"]:
        if order_info.get("conId") and order_info.get("qty_requested"):
            try:
                # Cancel any existing limit exit orders first
                exit_limit = order_info.get("exit_limit_order", {})
                if exit_limit.get("orderId"):
                    await ib.cancel_order(exit_limit["orderId"])

                close_result = await ib.place_market_order(order_info["conId"], "SELL", order_info["qty_requested"])
                order_info["close_order"] = close_result
                close_results.append(close_result)
            except Exception as e:
                logger.error("Close order failed for trade %s: %s", trade_id, e)
                close_results.append({"error": str(e)})

    trade["status"] = "closed"
    await broadcast({"type": "trade_update", "trade": trade})
    logger.info("Trade %s manually closed", trade_id)
    return {"ok": True, "trade_id": trade_id, "close_results": close_results}


def _demo_options(symbol: str, right: str, ma_price: float, num_strikes: int = 5):
    """Generate demo option data."""
    import random
    options = []
    base_strike = round(ma_price / 5) * 5  # round to nearest 5
    for i in range(num_strikes):
        if right == "C":
            strike = base_strike + (i + 1) * 5
        else:
            strike = base_strike - (i + 1) * 5
        bid = round(random.uniform(1.5, 15.0), 2)
        ask = round(bid + random.uniform(0.1, 0.5), 2)
        options.append({
            "conId": random.randint(100000, 999999),
            "symbol": symbol,
            "expiry": "20260220",
            "strike": float(strike),
            "right": right,
            "name": f"{symbol} 20260220 {strike} {right}",
            "bid": bid,
            "ask": ask,
            "last": round((bid + ask) / 2, 2),
            "volume": random.randint(100, 5000),
        })
    return options


# --- Demo monitor loop ---
async def demo_monitor_loop():
    """Demo mode monitor — simulates price movements and signals."""
    import random
    logger.info("Demo monitor loop started")

    while engine.running:
        try:
            for watch_id, watch in list(engine.watch_list.items()):
                if not watch.enabled or not engine.running:
                    continue

                # Simulate price and MA data
                base_price = {"SPY": 602, "QQQ": 520, "AAPL": 235, "MSFT": 420,
                              "NVDA": 880, "MNQ": 21500, "MES": 6050, "TSLA": 390}.get(watch.symbol, 100)
                noise = random.uniform(-0.5, 0.5) * base_price * 0.01
                current_price = round(base_price + noise, 2)

                ma_noise = random.uniform(-0.3, 0.3) * base_price * 0.005
                ma_value = round(base_price - base_price * 0.005 + ma_noise, 4)
                prev_ma = round(ma_value - random.uniform(-0.5, 0.5), 4)
                ma_rising = ma_value > prev_ma
                distance = round(current_price - ma_value, 4)

                engine.latest_data[watch_id] = {
                    "symbol": watch.symbol,
                    "current_price": current_price,
                    "ma_value": round(ma_value, 4),
                    "prev_ma": round(prev_ma, 4),
                    "ma_period": watch.ma_period,
                    "ma_direction": "RISING" if ma_rising else "FALLING",
                    "n_points": watch.n_points,
                    "distance_from_ma": distance,
                    "buy_zone": f"{round(ma_value, 2)} ~ {round(ma_value + watch.n_points, 2)}" if ma_rising else None,
                    "sell_zone": f"{round(ma_value - watch.n_points, 2)} ~ {round(ma_value, 2)}" if not ma_rising else None,
                    "last_updated": datetime.now().isoformat(),
                }

                await broadcast({
                    "type": "data_update",
                    "watch_id": watch_id,
                    "data": engine.latest_data[watch_id],
                })

                # Occasionally trigger a demo signal (5% chance)
                if random.random() < 0.05:
                    from strategy import Signal, SignalType
                    sig_type = SignalType.BUY if ma_rising else SignalType.SELL
                    signal = Signal(
                        timestamp=datetime.now().isoformat(),
                        watch_id=watch_id,
                        symbol=watch.symbol,
                        signal_type=sig_type,
                        price=current_price,
                        ma_value=round(ma_value, 4),
                        ma_period=watch.ma_period,
                        n_points=watch.n_points,
                        distance=abs(distance),
                    )
                    engine.signals.append(signal)
                    right = "C" if sig_type == SignalType.BUY else "P"
                    options = _demo_options(watch.symbol, right, ma_value)
                    await broadcast({
                        "type": "signal",
                        "signal": signal.to_dict(),
                        "options": options,
                        "underlying": {
                            "symbol": watch.symbol,
                            "price": current_price,
                            "sec_type": watch.sec_type,
                        },
                    })

                await asyncio.sleep(0.5)

            # Broadcast demo account
            await broadcast({
                "type": "account",
                "summary": DEMO_ACCOUNT,
                "positions": DEMO_POSITIONS,
                "connected": True,
            })

        except Exception as e:
            logger.error("Demo monitor error: %s", e)

        await asyncio.sleep(10)

    logger.info("Demo monitor loop stopped")


# --- WebSocket ---
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.add(ws)
    logger.info("WebSocket client connected (total: %d)", len(ws_clients))
    try:
        # Send initial state (include cached options in latest_data)
        latest = engine.get_latest_data()
        for wid, data in latest.items():
            if wid in _options_cache:
                data["options_call"] = _options_cache[wid]["call"]
                data["options_put"] = _options_cache[wid]["put"]
                data["locked_ma"] = _options_cache[wid]["ma_price"]

        init_msg = {
            "type": "init",
            "connected": ib.connected if ib else DEMO_MODE,
            "monitoring": engine.running,
            "watch_list": engine.get_watch_list(),
            "signals": engine.get_signals(20),
            "latest_data": latest,
        }
        await ws.send_text(json.dumps(init_msg, default=str))
        
        # Send account data if connected
        if not DEMO_MODE and ib and ib.connected:
            try:
                account = await ib.get_account_summary()
                positions = await ib.get_positions()
                await ws.send_text(json.dumps({
                    "type": "account",
                    "summary": account,
                    "positions": positions,
                    "connected": True,
                }, default=str))
            except Exception:
                pass

        while True:
            data = await ws.receive_text()
            # Handle client messages if needed
            msg = json.loads(data)
            if msg.get("type") == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error("WebSocket error: %s", e)
    finally:
        ws_clients.discard(ws)
        logger.info("WebSocket client disconnected (total: %d)", len(ws_clients))


# --- Static files ---
@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8888)
