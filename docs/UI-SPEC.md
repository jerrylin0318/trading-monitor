# Trading Monitor — UI 規格文檔 (v2 基準)

> 此文檔記錄 2026-02-03 確認的 UI 版本功能，作為日後開發的參考基準。

## 整體架構

- **Mobile-first PWA** — 支援 manifest.json, service worker, apple-mobile-web-app
- **Dark theme** — `#0d1117` 底色，GitHub-style 配色
- **WebSocket 即時更新** — 雙向通訊，支援離線 fallback (standalone demo mode)
- **Backend**: FastAPI + IB TWS (ib_insync)，thread-local IB connection

---

## Header 區

| 元素 | 功能 |
|------|------|
| IB 狀態燈 | 綠色=已連線，紅色=未連線 |
| 連線/斷線按鈕 | 呼叫 `/api/connect` `/api/disconnect` |
| 監控狀態燈 | 綠色=監控中 |
| 啟動/停止按鈕 | 呼叫 `/api/start` `/api/stop` |

---

## 卡片區（由上至下）

### 1. 💰 帳戶資訊
- **淨值** (NetLiquidation)
- **可用資金** (AvailableFunds)  
- **購買力** (BuyingPower)
- **未實現盈虧** (UnrealizedPnL) — 正綠負紅
- 格式: `{ tag: { value, currency } }`

### 2. 📋 持倉部位
- 顯示 symbol, secType, position, avgCost, marketPrice, marketValue, unrealizedPNL
- 正綠負紅配色
- 無持倉顯示「無持倉」

### 3. 👁️ 觀察清單 (核心)

#### 新增表單
- **標的代碼** — text input
- **類型** — STK / FUT / IND 下拉
- **策略方向** — 買進(BUY) / 賣出(SELL) / 雙向(BOTH)
- **交易所** — text (預設 SMART)
- **合約月份** — 期貨(FUT)時才顯示，動態生成近4個季月
- **MA 週期** — number (預設 21)
- **N 點** — number (預設 5)
- **幣別** — text (預設 USD)
- **收藏欄** — 自動保存新增過的標的到 localStorage，顯示為 chips 快速新增

#### Watch Item 卡片
```
┌─────────────────────────────────┐
│ TSLA STK  📈 買    ⏸ 🗑         │  ← 標的 + 類型 + 策略 tag + 操作按鈕
│ MA21  N=5  價格:421.48  MA:435.51│  ← 參數 + 即時數據
│ ↓ 下降  觸發區: 430.51~435.51    │  ← MA 方向 + 觸發區間
│         [🔄] [選擇權 ▼]          │  ← 刷新 + 展開期權
│                                  │
│ ▼ 選擇權 (展開後)                │
│ 到期日: [02/06 ✓] [02/13]       │  ← expiry tabs
│ 🔒 鎖定 MA = 435.51              │  ← 鎖定的 MA 參考值
│ 📈 標的  TSLA  --  $421.48  金額 │  ← 可選標的本身
│ Call 價外5檔（買進用）            │
│ ☐ 440  02/06 C  1.50/1.60  $1.55│  ← checkbox + strike + bid/ask + last
│ ☐ 445  02/06 C  1.20/1.30  $1.25│     + 金額 input
│ Put 價外5檔（賣出用）             │
│ ☐ 430  02/06 P  2.10/2.20  $2.15│
│ 📤 平倉策略（可多選）             │
│ ☐ 1️⃣ 限價止盈 成交價 +/- N 點    │
│ ☐ 2️⃣ 時間平倉 HH:MM             │
│ ☐ 3️⃣ 均線平倉 高於/低於 MA +/- N│
│ [📥 市價下單]                     │
└─────────────────────────────────┘
```

#### 選擇權內嵌邏輯
- **啟動時緩存**: 監控啟動時，根據每個 watch item 的 MA 值一次性獲取 Call+Put option contracts
- **展開即顯示**: 點擊「選擇權 ▼」立即顯示緩存的數據，不用等 API
- **🔄 刷新**: 重新依當前 MA 篩選合約 + batch snapshot 更新報價
- **信號觸發時**: 自動刷新報價（batch snapshot）
- **鎖定 MA**: 記錄緩存時的 MA 值，顯示在期權區上方
- **Call/Put 根據策略方向顯示**: BUY=只顯示 Call, SELL=只顯示 Put, BOTH=都顯示

#### Options 數據格式 (grouped by expiry)
```json
{
  "options_call": {
    "20260206": {
      "expiry": { "value": "20260206", "label": "02/06" },
      "options": [
        { "conId": 123, "symbol": "TSLA", "expiry": "20260206", "expiryLabel": "02/06",
          "strike": 440.0, "right": "C", "name": "TSLA 02/06 440C",
          "bid": 1.50, "ask": 1.60, "last": 1.55, "volume": 500 }
      ]
    }
  },
  "options_put": { ... same structure ... }
}
```

#### 平倉策略
1. **限價止盈** — 成交價 +/- N 點
2. **時間平倉** — 指定 HH:MM
3. **均線平倉** — 標的 高於/低於 MA +/- N 點

#### 下單
- **市價單** (Market Order)
- **金額換算口數**: `金額 ÷ Ask ÷ 100 = 口數` (Options multiplier=100)
- 勾選要交易的 option → 填入金額 → 點「📥 市價下單」

### 4. 🔔 信號記錄
- Signal toast + 聲音通知
- 信號列表：timestamp, symbol, signal_type (BUY/SELL), price, MA, distance
- 清除按鈕

### 5. ⚡ 選擇權交易面板 (獨立 — 信號觸發時彈出)
- 與 inline options 不同，這是 signal 觸發後的全幅面板
- 顯示完整期權資訊

### 6. 📜 日誌
- 彩色日誌 (success=green, warning=yellow, error=red, info=blue)
- 自動滾動到底部

---

## WebSocket 訊息格式

### Server → Client
| type | 用途 | 關鍵欄位 |
|------|------|----------|
| `init` | 連線初始化 | connected, monitoring, watch_list, signals, latest_data |
| `account` | 帳戶更新 | summary, positions, connected |
| `data_update` | 標的數據更新 | watch_id, data (含 options_call/put) |
| `watch_update` | 觀察清單變更 | watch_list |
| `signal` | 信號觸發 | signal, options, underlying |
| `status` | 狀態變更 | connected, monitoring, message |
| `error` | 錯誤 | message |
| `pong` | 心跳回應 | — |

### Client → Server
| type | 用途 |
|------|------|
| `ping` | 心跳 (每30秒) |

---

## API Endpoints

| Method | Path | 用途 |
|--------|------|------|
| GET | `/api/status` | 系統狀態 |
| POST | `/api/connect` | 連線 IB |
| POST | `/api/disconnect` | 斷線 |
| POST | `/api/start` | 啟動監控 |
| POST | `/api/stop` | 停止監控 |
| GET | `/api/account` | 帳戶+持倉 |
| GET | `/api/watch` | 觀察清單 |
| POST | `/api/watch` | 新增標的 |
| PUT | `/api/watch/{id}` | 更新標的 |
| DELETE | `/api/watch/{id}` | 移除標的 |
| GET | `/api/data` | 最新計算數據 |
| GET | `/api/options/{symbol}` | 獲取期權 (flat list) |
| POST | `/api/options/refresh/{watch_id}` | 刷新期權 (re-cache + prices) |
| GET | `/api/signals` | 信號記錄 |
| DELETE | `/api/signals` | 清除信號 |

---

## WatchItem 資料結構

```json
{
  "id": "tsla01",
  "symbol": "TSLA",
  "sec_type": "STK",
  "exchange": "SMART",
  "currency": "USD",
  "ma_period": 21,
  "n_points": 5.0,
  "enabled": true,
  "contract_month": "",
  "direction": "LONG",
  "strategy": "BUY"
}
```

- `sec_type`: STK (股票), FUT (期貨), IND (指數)
- `exchange`: SMART (股票), CME (期貨)
- `contract_month`: YYYYMM (期貨專用)
- `strategy`: BUY / SELL / BOTH (控制顯示 Call/Put)

---

## 技術要點

### IB 連線
- Thread-local IB instances (解決 asyncio event loop 衝突)
- Delayed data type 3 (免費延遲數據)
- Client ID 10 (避免衝突)
- 期貨需要 `exchange=CME` + `contract_month`

### 期權處理
- **Stock options**: `secType=OPT`, exchange=SMART
- **Futures options**: `secType=FOP`, exchange=CME, 需要 multiplier
- 合約用 `reqSecDefOptParams` 取得 chain → `qualifyContracts` 確認
- 報價用 `reqMktData(snapshot=True)` batch 取得

### 前端狀態管理
- `state.watchList` — 觀察清單
- `state.latestData[watch_id]` — 即時數據 (含 options_call/put)
- `state.optSelections[watch_id]` — 用戶勾選的 option + 金額 (localStorage)
- `state.expandedWatch` — 當前展開的 watch item
- `standaloneMode` — WebSocket 連不上時切換為離線 demo

### Standalone Demo Mode
- WebSocket 3秒未連線 → 自動切換 standalone
- `genDemoOptions()` 生成模擬期權數據
- API 調用被本地攔截處理
- 每8秒模擬價格更新
- 5% 機率模擬信號觸發
