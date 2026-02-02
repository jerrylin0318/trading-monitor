"""Trading Monitor — FastAPI application with WebSocket real-time updates."""

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from typing import Optional

from strategy import StrategyEngine, WatchItem, SignalType

# Only import IB when not in demo mode
DEMO_MODE = os.environ.get("DEMO_MODE", "false").lower() in ("true", "1", "yes")

if not DEMO_MODE:
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
    for ws in ws_clients:
        try:
            await ws.send_text(text)
        except Exception:
            disconnected.add(ws)
    ws_clients -= disconnected


# --- Monitor loop ---
async def monitor_loop():
    """Main monitoring loop — checks signals periodically."""
    logger.info("Monitor loop started")
    while engine.running:
        try:
            if not ib.connected:
                await broadcast({"type": "status", "connected": False, "message": "IB disconnected, retrying..."})
                connected = await ib.connect()
                if not connected:
                    await asyncio.sleep(10)
                    continue

            for watch_id, watch in list(engine.watch_list.items()):
                if not watch.enabled or not engine.running:
                    continue

                # Fetch daily bars
                df = await ib.get_daily_bars(
                    watch.symbol, watch.sec_type, watch.exchange, watch.currency
                )
                if df is None or len(df) < watch.ma_period + 1:
                    logger.warning("Insufficient data for %s", watch.symbol)
                    continue

                # Get current price
                current_price = await ib.get_current_price(
                    watch.symbol, watch.sec_type, watch.exchange, watch.currency
                )
                if current_price is None:
                    # Use last close as fallback
                    current_price = float(df["close"].iloc[-1])

                # Check for signal
                signal = engine.check_signal(df, watch, current_price)

                # Broadcast updated data
                await broadcast({
                    "type": "data_update",
                    "watch_id": watch_id,
                    "data": engine.latest_data.get(watch_id, {}),
                })

                if signal:
                    # Fetch option chain when signal triggers
                    right = "C" if signal.signal_type == SignalType.BUY else "P"
                    options = await ib.get_option_chain(
                        watch.symbol, watch.sec_type, watch.exchange, watch.currency,
                        ma_price=signal.ma_value, right=right, num_strikes=5
                    )
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

                # Small delay between symbols
                await asyncio.sleep(1)

            # Broadcast account info
            try:
                account = await ib.get_account_summary()
                positions = await ib.get_positions()
                await broadcast({
                    "type": "account",
                    "summary": account,
                    "positions": positions,
                    "connected": True,
                })
            except Exception as e:
                logger.error("Account data error: %s", e)

        except Exception as e:
            logger.error("Monitor loop error: %s", e)
            await broadcast({"type": "error", "message": str(e)})

        # Check every 60 seconds (daily strategy doesn't need faster)
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


class WatchItemUpdate(BaseModel):
    symbol: Optional[str] = None
    sec_type: Optional[str] = None
    exchange: Optional[str] = None
    currency: Optional[str] = None
    ma_period: Optional[int] = None
    n_points: Optional[float] = None
    enabled: Optional[bool] = None


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
    )
    engine.add_watch(watch)
    save_config()
    await broadcast({"type": "watch_update", "watch_list": engine.get_watch_list()})
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
        await ws.send_text(json.dumps({
            "type": "init",
            "connected": ib.connected,
            "monitoring": engine.running,
            "watch_list": engine.get_watch_list(),
            "signals": engine.get_signals(20),
            "latest_data": engine.get_latest_data(),
        }, default=str))

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
