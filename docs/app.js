/* Trading Monitor — Frontend Application */

const API = '';
let ws = null;
let state = {
    connected: false,
    monitoring: false,
    demoMode: false,
    watchList: [],
    signals: [],
    latestData: {},
    account: {},
    positions: [],
};

// ─── WebSocket ───
function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws`);

    ws.onopen = () => {
        log('WebSocket 已連線', 'success');
        // Ping every 30s
        setInterval(() => {
            if (ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: 'ping' }));
            }
        }, 30000);
    };

    ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        handleMessage(msg);
    };

    ws.onclose = () => {
        log('WebSocket 斷線，3秒後重連...', 'warning');
        setTimeout(connectWS, 3000);
    };

    ws.onerror = (err) => {
        log('WebSocket 錯誤', 'error');
    };
}

function handleMessage(msg) {
    switch (msg.type) {
        case 'init':
            state.connected = msg.connected;
            state.monitoring = msg.monitoring;
            state.demoMode = msg.demo_mode || false;
            state.watchList = msg.watch_list || [];
            state.signals = msg.signals || [];
            state.latestData = msg.latest_data || {};
            renderAll();
            if (state.demoMode) log('🎮 Demo 模式 — 數據為模擬', 'warning');
            break;
        case 'status':
            state.connected = msg.connected;
            updateStatusUI();
            if (msg.message) log(msg.message, 'info');
            break;
        case 'account':
            state.connected = msg.connected;
            state.account = msg.summary || {};
            state.positions = msg.positions || [];
            renderAccount();
            renderPositions();
            updateStatusUI();
            break;
        case 'data_update':
            state.latestData[msg.watch_id] = msg.data;
            renderWatchList();
            break;
        case 'watch_update':
            state.watchList = msg.watch_list || [];
            renderWatchList();
            break;
        case 'signal':
            state.signals.unshift(msg.signal);
            renderSignals();
            showSignalToast(msg.signal);
            if (msg.options || msg.underlying) {
                showOptionsPanel(msg.signal, msg.options, msg.underlying);
            }
            // Play sound
            try { new Audio('data:audio/wav;base64,UklGRl9vT19teleQBAAAAAABAAEARKwAAIhYAQACABAAAABkYXRhQW9PbwA=').play(); } catch(e) {}
            break;
        case 'error':
            log(msg.message, 'error');
            break;
    }
}

// ─── API calls ───
async function api(path, method = 'GET', body = null) {
    const opts = { method, headers: { 'Content-Type': 'application/json' } };
    if (body) opts.body = JSON.stringify(body);
    try {
        const res = await fetch(API + path, opts);
        return await res.json();
    } catch (e) {
        log(`API 錯誤: ${e.message}`, 'error');
        return null;
    }
}

async function toggleConnect() {
    const btn = document.getElementById('btn-connect');
    if (state.connected) {
        await api('/api/disconnect', 'POST');
        state.connected = false;
    } else {
        btn.textContent = '連線中...';
        const res = await api('/api/connect', 'POST');
        state.connected = res?.connected || false;
    }
    updateStatusUI();
    if (state.connected) {
        log('已連線至 IB TWS', 'success');
        // Fetch account
        const acc = await api('/api/account');
        if (acc) {
            state.account = acc.summary || {};
            state.positions = acc.positions || [];
            renderAccount();
            renderPositions();
        }
    }
}

async function toggleMonitor() {
    if (state.monitoring) {
        await api('/api/stop', 'POST');
        state.monitoring = false;
        log('監控已停止', 'warning');
    } else {
        if (!state.connected) {
            log('請先連線 IB', 'warning');
            return;
        }
        await api('/api/start', 'POST');
        state.monitoring = true;
        log('監控已啟動', 'success');
    }
    updateStatusUI();
}

// ─── Watch list ───
function showAddWatch() {
    document.getElementById('add-watch-form').style.display = 'block';
    document.getElementById('w-symbol').focus();
}
function hideAddWatch() {
    document.getElementById('add-watch-form').style.display = 'none';
}

async function addWatch() {
    const symbol = document.getElementById('w-symbol').value.trim().toUpperCase();
    if (!symbol) return;
    const item = {
        symbol,
        sec_type: document.getElementById('w-sectype').value,
        exchange: document.getElementById('w-exchange').value.trim() || 'SMART',
        currency: document.getElementById('w-currency').value.trim() || 'USD',
        ma_period: parseInt(document.getElementById('w-ma-period').value) || 21,
        n_points: parseFloat(document.getElementById('w-n-points').value) || 5,
        enabled: true,
    };
    const res = await api('/api/watch', 'POST', item);
    if (res) {
        log(`已新增觀察: ${symbol}`, 'success');
        state.watchList.push(res);
        renderWatchList();
        hideAddWatch();
        document.getElementById('w-symbol').value = '';
    }
}

async function removeWatch(id) {
    await api(`/api/watch/${id}`, 'DELETE');
    state.watchList = state.watchList.filter(w => w.id !== id);
    renderWatchList();
    log('已移除觀察標的', 'info');
}

async function toggleWatch(id) {
    const item = state.watchList.find(w => w.id === id);
    if (!item) return;
    await api(`/api/watch/${id}`, 'PUT', { enabled: !item.enabled });
    item.enabled = !item.enabled;
    renderWatchList();
}

async function clearSignals() {
    await api('/api/signals', 'DELETE');
    state.signals = [];
    renderSignals();
}

// ─── Rendering ───
function renderAll() {
    updateStatusUI();
    renderAccount();
    renderPositions();
    renderWatchList();
    renderSignals();
}

function updateStatusUI() {
    const ibDot = document.getElementById('ib-dot');
    const ibLabel = document.getElementById('ib-label');
    const btnConnect = document.getElementById('btn-connect');
    const monDot = document.getElementById('monitor-dot');
    const monLabel = document.getElementById('monitor-label');
    const btnMon = document.getElementById('btn-monitor');

    ibDot.className = `status-dot ${state.connected ? 'connected' : 'disconnected'}`;
    ibLabel.textContent = state.connected ? 'IB 已連線' : 'IB 未連線';
    btnConnect.textContent = state.connected ? '斷線' : '連線';
    btnConnect.className = state.connected ? 'btn btn-sm btn-danger' : 'btn btn-sm btn-success';

    monDot.className = `status-dot ${state.monitoring ? 'monitoring' : 'disconnected'}`;
    monLabel.textContent = state.monitoring ? '監控中' : '未啟動';
    btnMon.textContent = state.monitoring ? '停止監控' : '啟動監控';
    btnMon.className = state.monitoring ? 'btn btn-sm btn-danger' : 'btn btn-sm btn-success';
}

function renderAccount() {
    const s = state.account;
    const get = (tag) => {
        if (s[tag]) return s[tag].value;
        return '--';
    };
    const fmt = (v) => {
        if (v === '--') return v;
        const n = parseFloat(v);
        return isNaN(n) ? v : n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    };
    const pnlClass = (v) => {
        if (v === '--') return '';
        const n = parseFloat(v);
        return n >= 0 ? 'positive' : 'negative';
    };

    document.getElementById('acc-equity').textContent = '$' + fmt(get('NetLiquidation'));
    document.getElementById('acc-available').textContent = '$' + fmt(get('AvailableFunds'));
    document.getElementById('acc-buying-power').textContent = '$' + fmt(get('BuyingPower'));

    const pnl = get('UnrealizedPnL');
    const el = document.getElementById('acc-unrealized-pnl');
    el.textContent = pnl === '--' ? '--' : '$' + fmt(pnl);
    el.className = 'value ' + pnlClass(pnl);
}

function renderPositions() {
    const container = document.getElementById('positions-container');
    if (!state.positions || state.positions.length === 0) {
        container.innerHTML = '<div class="empty-state">無持倉</div>';
        return;
    }
    let html = `<table>
        <thead><tr>
            <th>標的</th><th>類型</th><th>數量</th><th>均價</th><th>市值</th><th>盈虧</th>
        </tr></thead><tbody>`;
    for (const p of state.positions) {
        const pnl = p.unrealizedPNL;
        const pnlClass = pnl >= 0 ? 'positive' : 'negative';
        const name = p.right ? `${p.symbol} ${p.expiry} ${p.strike}${p.right}` : p.symbol;
        html += `<tr>
            <td><strong>${name}</strong></td>
            <td>${p.secType}</td>
            <td>${p.position}</td>
            <td>${p.avgCost?.toFixed(2) || '--'}</td>
            <td>${p.marketValue?.toFixed(2) || '--'}</td>
            <td class="${pnlClass}">${pnl != null ? pnl.toFixed(2) : '--'}</td>
        </tr>`;
    }
    html += '</tbody></table>';
    container.innerHTML = html;
}

function renderWatchList() {
    const container = document.getElementById('watch-list');
    if (state.watchList.length === 0) {
        container.innerHTML = '<div class="empty-state">尚無觀察標的，點擊「+ 新增」開始</div>';
        return;
    }
    let html = '';
    for (const w of state.watchList) {
        const data = state.latestData[w.id] || {};
        const dir = data.ma_direction || '--';
        const dirClass = dir === 'RISING' ? 'rising' : dir === 'FALLING' ? 'falling' : 'flat';
        const dirLabel = dir === 'RISING' ? '↑ 上升' : dir === 'FALLING' ? '↓ 下降' : '— 持平';
        const price = data.current_price ? data.current_price.toFixed(2) : '--';
        const ma = data.ma_value ? data.ma_value.toFixed(2) : '--';
        const dist = data.distance_from_ma != null ? data.distance_from_ma.toFixed(2) : '--';
        const zone = data.buy_zone || data.sell_zone || '--';

        html += `
        <div class="watch-item ${w.enabled ? '' : 'disabled'}">
            <div>
                <div class="watch-symbol">${w.symbol}</div>
                <div class="watch-details">
                    <span>${w.sec_type} · ${w.exchange}</span>
                    <span>MA${w.ma_period}</span>
                    <span>N=${w.n_points}</span>
                    <span>價格: ${price}</span>
                    <span>MA: ${ma}</span>
                    <span>距離: ${dist}</span>
                </div>
            </div>
            <div style="display:flex;align-items:center;gap:12px;">
                <div class="watch-ma-info">
                    <span class="ma-badge ${dirClass}">${dirLabel}</span>
                    <span style="font-size:11px;color:var(--text-muted)">觸發區: ${zone}</span>
                </div>
                <div class="watch-actions">
                    <button class="btn btn-sm btn-icon" onclick="toggleWatch('${w.id}')" title="${w.enabled ? '停用' : '啟用'}">
                        ${w.enabled ? '⏸' : '▶️'}
                    </button>
                    <button class="btn btn-sm btn-icon btn-danger" onclick="removeWatch('${w.id}')" title="移除">🗑</button>
                </div>
            </div>
        </div>`;
    }
    container.innerHTML = html;
}

function renderSignals() {
    const container = document.getElementById('signals-container');
    if (!state.signals || state.signals.length === 0) {
        container.innerHTML = '<div class="empty-state">尚無信號</div>';
        return;
    }
    let html = '';
    for (const s of state.signals.slice(0, 20)) {
        const time = new Date(s.timestamp).toLocaleString('zh-TW');
        html += `
        <div class="signal-item ${s.signal_type}">
            <div class="signal-header">
                <span>
                    <span class="signal-type ${s.signal_type}">${s.signal_type === 'BUY' ? '🟢 買進' : '🔴 賣出'}</span>
                    <strong style="margin-left:8px;">${s.symbol}</strong>
                </span>
                <span class="signal-time">${time}</span>
            </div>
            <div class="signal-details">
                價格 ${s.price?.toFixed(2)} | MA${s.ma_period} = ${s.ma_value?.toFixed(2)} | 距離 = ${s.distance?.toFixed(2)} | N = ${s.n_points}
            </div>
        </div>`;
    }
    container.innerHTML = html;
}

// ─── Options Panel ───
function showOptionsPanel(signal, options, underlying) {
    const card = document.getElementById('options-card');
    const body = document.getElementById('options-body');
    card.style.display = 'block';

    const type = signal.signal_type === 'BUY' ? 'Call（買權）' : 'Put（賣權）';
    let html = `
        <div class="options-panel">
            <h3>📌 ${signal.symbol} ${signal.signal_type} 信號 — ${type} 價外5檔</h3>
            <p style="font-size:12px;color:var(--text-secondary);margin-bottom:12px;">
                MA${signal.ma_period} = ${signal.ma_value?.toFixed(2)} | 觸發價 = ${signal.price?.toFixed(2)}
            </p>`;

    // Underlying as option
    html += `
        <div class="option-row" style="border-bottom:1px solid var(--border);padding-bottom:8px;margin-bottom:8px;">
            <input type="checkbox" id="opt-underlying">
            <span class="name">📈 ${underlying.symbol} (標的本身)</span>
            <span class="price">$${underlying.price?.toFixed(2) || '--'}</span>
            <input type="number" value="1" min="1" style="width:60px;" id="qty-underlying">
        </div>`;

    if (options && options.length > 0) {
        for (let i = 0; i < options.length; i++) {
            const o = options[i];
            const priceDisplay = o.last ? o.last.toFixed(2) : (o.ask ? o.ask.toFixed(2) : '--');
            const bidAsk = `${o.bid?.toFixed(2) || '--'} / ${o.ask?.toFixed(2) || '--'}`;
            html += `
            <div class="option-row">
                <input type="checkbox" id="opt-${i}">
                <span class="name">${o.name}</span>
                <span style="font-size:11px;color:var(--text-muted);min-width:100px;">${bidAsk}</span>
                <span class="price">$${priceDisplay}</span>
                <input type="number" value="1" min="1" style="width:60px;" id="qty-${i}">
            </div>`;
        }
    } else {
        html += '<div class="empty-state">無法取得選擇權資料</div>';
    }

    html += `
            <div style="margin-top:16px;display:flex;gap:8px;justify-content:flex-end;">
                <button class="btn btn-sm">稍後再說</button>
                <button class="btn btn-sm btn-primary" onclick="confirmTrade()">確認下單（模擬）</button>
            </div>
        </div>`;

    body.innerHTML = html;
    card.scrollIntoView({ behavior: 'smooth' });
}

function closeOptions() {
    document.getElementById('options-card').style.display = 'none';
}

function confirmTrade() {
    log('📋 下單確認（模擬模式）— 實際下單功能尚未啟用', 'warning');
    showToast('模擬下單已記錄', 'info');
}

// ─── Toast ───
function showSignalToast(signal) {
    const type = signal.signal_type === 'BUY' ? 'buy' : 'sell';
    const label = signal.signal_type === 'BUY' ? '🟢 買進信號' : '🔴 賣出信號';
    showToast(`${label}: ${signal.symbol} @ ${signal.price?.toFixed(2)}`, type);
}

function showToast(text, type = 'info') {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = text;
    container.appendChild(toast);
    setTimeout(() => toast.remove(), 5000);
}

// ─── Log ───
function log(msg, level = 'info') {
    const container = document.getElementById('log-container');
    const time = new Date().toLocaleTimeString('zh-TW');
    const entry = document.createElement('div');
    entry.className = `log-entry ${level}`;
    entry.innerHTML = `<span class="time">[${time}]</span> ${msg}`;
    container.appendChild(entry);
    container.scrollTop = container.scrollHeight;
    // Keep last 200 entries
    while (container.children.length > 200) {
        container.removeChild(container.firstChild);
    }
}

// ─── Standalone Demo (no backend) ───
let standaloneMode = false;
let standaloneTicker = null;

function startStandaloneDemo() {
    standaloneMode = true;
    state.connected = true;
    state.demoMode = true;
    state.account = {
        NetLiquidation: { value: "125430.50", currency: "USD" },
        AvailableFunds: { value: "98200.75", currency: "USD" },
        BuyingPower: { value: "392803.00", currency: "USD" },
        UnrealizedPnL: { value: "1250.30", currency: "USD" },
    };
    state.positions = [
        { symbol: "SPY", secType: "STK", position: 50, avgCost: 598.25,
          marketPrice: 602.10, marketValue: 30105, unrealizedPNL: 192.5 },
        { symbol: "QQQ", secType: "OPT", strike: 520, right: "C", expiry: "20260220",
          position: 5, avgCost: 8.50, marketPrice: 10.60, marketValue: 5300, unrealizedPNL: 1050 },
    ];
    renderAll();
    log('🎮 離線 Demo 模式 — 後端未連線', 'warning');

    // Simulate data updates
    standaloneTicker = setInterval(() => {
        for (const w of state.watchList) {
            const base = { SPY: 602, QQQ: 520, AAPL: 235, MSFT: 420, NVDA: 880,
                           MNQ: 21500, MES: 6050, TSLA: 390 }[w.symbol] || 100;
            const noise = (Math.random() - 0.5) * base * 0.01;
            const price = +(base + noise).toFixed(2);
            const maNoise = (Math.random() - 0.3) * base * 0.005;
            const maVal = +(base - base * 0.005 + maNoise).toFixed(4);
            const prevMa = +(maVal - (Math.random() - 0.5) * 0.5).toFixed(4);
            const rising = maVal > prevMa;
            const dist = +(price - maVal).toFixed(4);
            state.latestData[w.id] = {
                symbol: w.symbol, current_price: price, ma_value: maVal,
                prev_ma: prevMa, ma_period: w.ma_period,
                ma_direction: rising ? 'RISING' : 'FALLING',
                n_points: w.n_points, distance_from_ma: dist,
                buy_zone: rising ? `${maVal.toFixed(2)} ~ ${(maVal + w.n_points).toFixed(2)}` : null,
                sell_zone: !rising ? `${(maVal - w.n_points).toFixed(2)} ~ ${maVal.toFixed(2)}` : null,
                last_updated: new Date().toISOString(),
            };
            // 5% chance signal
            if (Math.random() < 0.03 && w.enabled) {
                const sigType = rising ? 'BUY' : 'SELL';
                const sig = { timestamp: new Date().toISOString(), watch_id: w.id, symbol: w.symbol,
                    signal_type: sigType, price, ma_value: maVal, ma_period: w.ma_period,
                    n_points: w.n_points, distance: Math.abs(dist) };
                state.signals.unshift(sig);
                renderSignals();
                showSignalToast(sig);
                const right = sigType === 'BUY' ? 'C' : 'P';
                const baseStrike = Math.round(maVal / 5) * 5;
                const opts = [];
                for (let i = 0; i < 5; i++) {
                    const strike = right === 'C' ? baseStrike + (i+1)*5 : baseStrike - (i+1)*5;
                    const bid = +(Math.random() * 13 + 1.5).toFixed(2);
                    const ask = +(bid + Math.random() * 0.5 + 0.1).toFixed(2);
                    opts.push({ symbol: w.symbol, expiry: '20260220', strike, right,
                        name: `${w.symbol} 20260220 ${strike} ${right}`,
                        bid, ask, last: +((bid+ask)/2).toFixed(2), volume: Math.floor(Math.random()*5000+100) });
                }
                showOptionsPanel(sig, opts, { symbol: w.symbol, price, sec_type: w.sec_type || 'STK' });
            }
        }
        renderWatchList();
    }, 8000);
}

// Override API for standalone mode
const _origApi = api;
async function api(path, method = 'GET', body = null) {
    if (standaloneMode) {
        // Handle locally
        if (path === '/api/connect') { state.connected = true; updateStatusUI(); return { connected: true }; }
        if (path === '/api/disconnect') { state.connected = false; updateStatusUI(); return { connected: false }; }
        if (path === '/api/start') { state.monitoring = true; updateStatusUI(); return { status: 'started' }; }
        if (path === '/api/stop') { state.monitoring = false; updateStatusUI(); return { status: 'stopped' }; }
        if (path === '/api/account') return { summary: state.account, positions: state.positions };
        if (path === '/api/signals' && method === 'DELETE') { state.signals = []; renderSignals(); return { ok: true }; }
        if (path.startsWith('/api/watch') && method === 'POST') {
            const w = { ...body, id: Math.random().toString(36).slice(2,10), enabled: true };
            state.watchList.push(w);
            renderWatchList();
            return w;
        }
        if (path.startsWith('/api/watch/') && method === 'DELETE') {
            const id = path.split('/').pop();
            state.watchList = state.watchList.filter(w => w.id !== id);
            renderWatchList();
            return { ok: true };
        }
        if (path.startsWith('/api/watch/') && method === 'PUT') {
            const id = path.split('/')[3];
            const w = state.watchList.find(x => x.id === id);
            if (w && body) Object.assign(w, body);
            renderWatchList();
            return { ok: true };
        }
        return {};
    }
    return _origApi(path, method, body);
}

// ─── Init ───
window.addEventListener('load', () => {
    log('Trading Monitor 已載入', 'info');

    // Try WebSocket, fall back to standalone demo
    try {
        connectWS();
        // If WS fails to connect in 3s, switch to standalone
        setTimeout(() => {
            if (!ws || ws.readyState !== WebSocket.OPEN) {
                log('WebSocket 連線失敗，切換離線 Demo', 'warning');
                startStandaloneDemo();
            }
        }, 3000);
    } catch (e) {
        startStandaloneDemo();
    }

    // Keyboard shortcut: Enter to add watch
    document.getElementById('w-symbol').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') addWatch();
    });
});
