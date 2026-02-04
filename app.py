"""Trading Monitor — FastAPI application with WebSocket real-time updates."""

import asyncio
import json
import logging
import os
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


async def _init_new_watch(watch):
    """Initialize a newly added watch item: fetch data, calculate MA, cache options."""
    try:
        df = await ib.get_daily_bars(
            watch.symbol, watch.sec_type, watch.exchange, watch.currency,
            contract_month=watch.contract_month
        )
        if df is None or len(df) < watch.ma_period + 1:
            logger.warning("Insufficient data for new watch %s", watch.symbol)
            await broadcast({"type": "error", "message": f"{watch.symbol} 數據不足，請確認交易所和合約月份"})
            return

        current_price = await ib.get_current_price(
            watch.symbol, watch.sec_type, watch.exchange, watch.currency,
            contract_month=watch.contract_month
        )
        if current_price is None:
            current_price = float(df["close"].iloc[-1])

        engine.check_signal(df, watch, current_price)
        data = engine.latest_data.get(watch.id, {})
        ma_price = data.get("ma_value", current_price)

        await cache_options_for_watch(watch.id, watch, ma_price)

        if watch.id in _options_cache:
            data["options_call"] = _options_cache[watch.id]["call"]
            data["options_put"] = _options_cache[watch.id]["put"]
            data["locked_ma"] = ma_price

        await broadcast({"type": "data_update", "watch_id": watch.id, "data": data})
        logger.info("Initialized new watch %s: price=%.2f MA=%.2f", watch.symbol, current_price, ma_price)
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


async def cache_options_for_watch(watch_id: str, watch, ma_price: float):
    """Fetch and cache Call+Put option contracts for a watch item."""
    try:
        calls = await ib.get_option_chain(
            watch.symbol, watch.sec_type, watch.exchange, watch.currency,
            ma_price=ma_price, right="C", num_strikes=5,
            contract_month=watch.contract_month
        )
        puts = await ib.get_option_chain(
            watch.symbol, watch.sec_type, watch.exchange, watch.currency,
            ma_price=ma_price, right="P", num_strikes=5,
            contract_month=watch.contract_month
        )
        _options_cache[watch_id] = {
            "call_raw": calls, "put_raw": puts,
            "call": _group_options(calls), "put": _group_options(puts),
            "ma_price": ma_price,
        }
        logger.info("Cached options for %s: %d calls, %d puts (MA=%.2f)",
                     watch.symbol, len(calls), len(puts), ma_price)
    except Exception as e:
        logger.error("Failed to cache options for %s: %s", watch.symbol, e)


# --- Monitor loop ---
async def monitor_loop():
    """Main monitoring loop — checks signals periodically."""
    logger.info("Monitor loop started")
    
    # Phase 1: Initial data fetch + cache options for all watches
    initialized = set()
    for watch_id, watch in list(engine.watch_list.items()):
        if not watch.enabled:
            continue
        try:
            df = await ib.get_daily_bars(
                watch.symbol, watch.sec_type, watch.exchange, watch.currency,
                contract_month=watch.contract_month
            )
            if df is None or len(df) < watch.ma_period + 1:
                logger.warning("Insufficient data for %s", watch.symbol)
                continue

            current_price = await ib.get_current_price(
                watch.symbol, watch.sec_type, watch.exchange, watch.currency,
                contract_month=watch.contract_month
            )
            if current_price is None:
                current_price = float(df["close"].iloc[-1])

            # Calculate MA and store data
            engine.check_signal(df, watch, current_price)
            data = engine.latest_data.get(watch_id, {})
            ma_price = data.get("ma_value", current_price)

            # Cache option contracts (no prices yet — fast)
            await cache_options_for_watch(watch_id, watch, ma_price)

            # Attach cached options to data for frontend
            if watch_id in _options_cache:
                data["options_call"] = _options_cache[watch_id]["call"]
                data["options_put"] = _options_cache[watch_id]["put"]
                data["locked_ma"] = ma_price

            await broadcast({"type": "data_update", "watch_id": watch_id, "data": data})
            initialized.add(watch_id)
            logger.info("Initialized %s: price=%.2f MA=%.2f", watch.symbol, current_price, ma_price)
        except Exception as e:
            logger.error("Failed to init %s: %s", watch.symbol, e)

    await broadcast({"type": "status", "connected": True, "monitoring": True, 
                     "message": f"監控已啟動 ({len(initialized)}/{len(engine.watch_list)} 標的)"})

    # Phase 2: Periodic updates (price + MA, options prices on signal)
    tick = 0
    while engine.running:
        try:
            tick += 1
            if not ib.connected:
                await broadcast({"type": "status", "connected": False, "message": "IB 斷線，重連中..."})
                connected = await ib.connect()
                if not connected:
                    await asyncio.sleep(10)
                    continue

            for watch_id, watch in list(engine.watch_list.items()):
                if not watch.enabled or not engine.running:
                    continue

                df = await ib.get_daily_bars(
                    watch.symbol, watch.sec_type, watch.exchange, watch.currency,
                    contract_month=watch.contract_month
                )
                if df is None or len(df) < watch.ma_period + 1:
                    continue

                current_price = await ib.get_current_price(
                    watch.symbol, watch.sec_type, watch.exchange, watch.currency,
                    contract_month=watch.contract_month
                )
                if current_price is None:
                    current_price = float(df["close"].iloc[-1])

                signal = engine.check_signal(df, watch, current_price)
                data = engine.latest_data.get(watch_id, {})

                # Always include cached options
                if watch_id in _options_cache:
                    data["options_call"] = _options_cache[watch_id]["call"]
                    data["options_put"] = _options_cache[watch_id]["put"]
                    data["locked_ma"] = _options_cache[watch_id]["ma_price"]

                await broadcast({"type": "data_update", "watch_id": watch_id, "data": data})

                if signal:
                    # On signal: refresh option prices (batch snapshot)
                    cache = _options_cache.get(watch_id)
                    if cache:
                        refreshed_calls = await ib.refresh_option_prices(cache["call_raw"])
                        refreshed_puts = await ib.refresh_option_prices(cache["put_raw"])
                        cache["call"] = _group_options(refreshed_calls)
                        cache["put"] = _group_options(refreshed_puts)
                        data["options_call"] = cache["call"]
                        data["options_put"] = cache["put"]

                    await broadcast({
                        "type": "signal",
                        "signal": signal.to_dict(),
                        "options": None,
                        "underlying": {"symbol": watch.symbol, "price": current_price, "sec_type": watch.sec_type},
                    })

                await asyncio.sleep(1)

            # Account update every 5 ticks (5 min)
            if tick % 5 == 0:
                try:
                    account = await ib.get_account_summary()
                    positions = await ib.get_positions()
                    await broadcast({"type": "account", "summary": account, "positions": positions, "connected": True})
                except Exception as e:
                    logger.error("Account data error: %s", e)

        except Exception as e:
            logger.error("Monitor loop error: %s", e)
            await broadcast({"type": "error", "message": str(e)})

        await asyncio.sleep(60)

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
    strategy: str = "BOTH"


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
    strategy: Optional[str] = None


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
        strategy=item.strategy,
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
        refreshed_calls = await ib.refresh_option_prices(cache["call_raw"])
        refreshed_puts = await ib.refresh_option_prices(cache["put_raw"])
        cache["call"] = _group_options(refreshed_calls)
        cache["put"] = _group_options(refreshed_puts)
        
        data["options_call"] = cache["call"]
        data["options_put"] = cache["put"]
        data["locked_ma"] = ma_price
        
        await broadcast({"type": "data_update", "watch_id": watch_id, "data": data})
    
    return {"ok": True, "calls": len(cache.get("call_raw", [])), "puts": len(cache.get("put_raw", []))}


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
        # Send initial state
        init_msg = {
            "type": "init",
            "connected": ib.connected if ib else DEMO_MODE,
            "monitoring": engine.running,
            "watch_list": engine.get_watch_list(),
            "signals": engine.get_signals(20),
            "latest_data": engine.get_latest_data(),
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
