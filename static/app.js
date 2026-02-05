/* Trading Monitor — Frontend Application */

const API = '';
let ws = null;
let authToken = null;
let state = {
    connected: false,
    monitoring: false,
    demoMode: false,
    watchList: [],
    expandedWatch: null,
    expandedChart: null,  // watch_id of expanded chart
    signals: [],
    optSelections: {},  // { watchId: { optId: { checked, amount }, exitProfit: bool, ... } }
    latestData: {},
    account: {},
    positions: [],
};

// Chart instances cache
const charts = {};        // { watchId: chart }
const chartSeries = {};   // { watchId: { candle, ma } }
const todayCandle = {};   // { watchId: { time, open, high, low, close } }
let lastChartRender = 0;

// ─── Authentication ───
function getStoredAuth() {
    return {
        username: localStorage.getItem('auth_username') || '',
        password: localStorage.getItem('auth_password') || '',
        token: localStorage.getItem('auth_token') || '',
        rememberUser: localStorage.getItem('auth_remember_user') === 'true',
        rememberPass: localStorage.getItem('auth_remember_pass') === 'true',
        autoLogin: localStorage.getItem('auth_auto_login') === 'true',
    };
}

function saveAuthPrefs(username, password, rememberUser, rememberPass, autoLogin) {
    localStorage.setItem('auth_remember_user', rememberUser);
    localStorage.setItem('auth_remember_pass', rememberPass);
    localStorage.setItem('auth_auto_login', autoLogin);
    if (rememberUser) {
        localStorage.setItem('auth_username', username);
    } else {
        localStorage.removeItem('auth_username');
    }
    if (rememberPass) {
        localStorage.setItem('auth_password', password);
    } else {
        localStorage.removeItem('auth_password');
    }
}

function saveToken(token) {
    authToken = token;
    localStorage.setItem('auth_token', token);
}

function clearToken() {
    authToken = null;
    localStorage.removeItem('auth_token');
}

async function checkAuth() {
    const stored = getStoredAuth();
    if (stored.token) {
        authToken = stored.token;
        try {
            const res = await fetch('/api/auth/check', {
                headers: { 'Authorization': `Bearer ${authToken}` }
            });
            if (res.ok) {
                hideLogin();
                return true;
            }
        } catch (e) {}
        clearToken();
    }
    return false;
}

async function handleLogin(e) {
    e.preventDefault();
    const username = document.getElementById('login-username').value;
    const password = document.getElementById('login-password').value;
    const rememberUser = document.getElementById('login-remember-user').checked;
    const rememberPass = document.getElementById('login-remember-pass').checked;
    const autoLogin = document.getElementById('login-auto').checked;
    const errorEl = document.getElementById('login-error');
    
    errorEl.textContent = '';
    
    try {
        const res = await fetch('/api/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password })
        });
        const data = await res.json();
        if (res.ok && data.token) {
            saveToken(data.token);
            saveAuthPrefs(username, password, rememberUser, rememberPass, autoLogin);
            hideLogin();
            initApp();
        } else {
            errorEl.textContent = data.detail || '登入失敗';
        }
    } catch (err) {
        errorEl.textContent = '連線錯誤';
    }
    return false;
}

function showLogin() {
    const overlay = document.getElementById('login-overlay');
    overlay.classList.remove('hidden');
    
    // Restore saved values
    const stored = getStoredAuth();
    document.getElementById('login-username').value = stored.username;
    document.getElementById('login-password').value = stored.password;
    document.getElementById('login-remember-user').checked = stored.rememberUser;
    document.getElementById('login-remember-pass').checked = stored.rememberPass;
    document.getElementById('login-auto').checked = stored.autoLogin;
    
    // Auto login if enabled
    if (stored.autoLogin && stored.username && stored.password) {
        handleLogin({ preventDefault: () => {} });
    }
}

function hideLogin() {
    document.getElementById('login-overlay').classList.add('hidden');
}

async function logout() {
    try {
        await fetch('/api/auth/logout', {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${authToken}` }
        });
    } catch (e) {}
    clearToken();
    location.reload();
}

// ─── WebSocket ───
let wsPingInterval = null;

function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws`);

    ws.onopen = () => {
        log('WebSocket 已連線', 'success');
        // Clear old interval if exists
        if (wsPingInterval) clearInterval(wsPingInterval);
        // Ping every 15s to keep connection alive
        wsPingInterval = setInterval(() => {
            if (ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: 'ping' }));
            }
        }, 15000);
    };

    ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        handleMessage(msg);
    };

    ws.onclose = () => {
        log('WebSocket 斷線，3秒後重連...', 'warning');
        if (wsPingInterval) clearInterval(wsPingInterval);
        wsPingInterval = null;
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
        case 'data_update': {
            // Merge incoming data, preserve client-side state (selected_expiry, etc.)
            const prev = state.latestData[msg.watch_id] || {};
            state.latestData[msg.watch_id] = { ...prev, ...msg.data };
            if (prev.selected_expiry && !msg.data.selected_expiry) {
                state.latestData[msg.watch_id].selected_expiry = prev.selected_expiry;
            }
            // If chart is expanded, only update prices (don't destroy chart)
            if (state.expandedChart && chartSeries[state.expandedChart]) {
                updatePriceDisplays(msg.watch_id, msg.data);
                // Update live candle
                if (state.expandedChart === msg.watch_id) {
                    updateLiveCandle(msg.watch_id, msg.data.current_price);
                }
            } else {
                renderWatchList();
                // Render chart if just expanded
                if (state.expandedChart && !chartSeries[state.expandedChart]) {
                    setTimeout(() => renderChart(state.expandedChart), 100);
                }
            }
            break;
        }
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
        case 'trade_update': {
            const t = msg.trade;
            if (t) {
                const emoji = t.status === 'closed' ? '✅' : t.status === 'exiting' ? '⏳' : '📊';
                log(`${emoji} 交易 ${t.symbol} [${t.id}]: ${t.status}`, t.status === 'closed' ? 'success' : 'info');
                if (t.status === 'closed' || t.status === 'exiting') {
                    showToast(`${t.symbol} 已平倉`, 'sell');
                }
            }
            break;
        }
        case 'error':
            log(msg.message, 'error');
            break;
    }
}

// ─── API calls ───
async function _realApi(path, method = 'GET', body = null) {
    const opts = { method, headers: { 'Content-Type': 'application/json' } };
    if (authToken) opts.headers['Authorization'] = `Bearer ${authToken}`;
    if (body) opts.body = JSON.stringify(body);
    try {
        const res = await fetch(API + path, opts);
        if (res.status === 401) {
            // Token expired or invalid
            clearToken();
            showLogin();
            return null;
        }
        return await res.json();
    } catch (e) {
        log(`API 錯誤: ${e.message}`, 'error');
        return null;
    }
}
async function api(path, method = 'GET', body = null) {
    return _realApi(path, method, body);
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
    renderFavorites();
}
function hideAddWatch() {
    document.getElementById('add-watch-form').style.display = 'none';
    document.getElementById('favorites-bar').style.display = 'none';
}

function renderFavorites() {
    const favs = loadFavorites();
    const bar = document.getElementById('favorites-bar');
    const chips = document.getElementById('fav-chips');
    if (favs.length === 0) {
        bar.style.display = 'none';
        return;
    }
    bar.style.display = 'block';
    chips.innerHTML = '<span style="font-size:11px;color:var(--text-muted);">收藏：</span>' +
        favs.map(f => `
            <span class="fav-chip" onclick="quickAddFromFav('${f.symbol}','${f.sec_type}','${f.exchange}','${f.currency}')">
                ${f.symbol} <span class="type-tag">${f.sec_type}</span>
                <span class="remove-fav" onclick="event.stopPropagation();removeFavAndRender('${f.symbol}','${f.sec_type}')" title="取消收藏">×</span>
            </span>
        `).join('');
}

function removeFavAndRender(symbol, secType) {
    removeFavorite(symbol, secType);
    renderFavorites();
}

function quickAddFromFav(symbol, secType, exchange, currency) {
    // Fill form fields, let user edit before confirming
    document.getElementById('w-symbol').value = symbol;
    document.getElementById('w-sectype').value = secType || 'STK';
    document.getElementById('w-exchange').value = exchange || 'SMART';
    document.getElementById('w-currency').value = currency || 'USD';
    document.getElementById('w-symbol').focus();
}

async function addWatch() {
    const symbol = document.getElementById('w-symbol').value.trim().toUpperCase();
    if (!symbol) return;
    const secType = document.getElementById('w-sectype').value;
    
    // 期貨必須選擇合約月份
    if (secType === 'FUT') {
        const contractMonth = document.getElementById('w-contract').value;
        if (!contractMonth) {
            log('期貨必須選擇合約月份', 'error');
            return;
        }
    }
    
    const confirmMaEnabled = document.getElementById('w-confirm-ma-enabled').checked;
    const strategyType = document.getElementById('w-strategy-type').value;
    const item = {
        symbol,
        sec_type: secType,
        direction: document.getElementById('w-direction').value,
        exchange: document.getElementById('w-exchange').value.trim() || 'SMART',
        currency: document.getElementById('w-currency').value.trim() || 'USD',
        ma_period: parseInt(document.getElementById('w-ma-period').value) || 21,
        n_points: parseFloat(document.getElementById('w-n-points').value) || 5,
        contract_month: secType === 'FUT' ? document.getElementById('w-contract').value : null,
        confirm_ma_enabled: confirmMaEnabled,
        confirm_ma_period: confirmMaEnabled ? parseInt(document.getElementById('w-confirm-ma-period').value) || 55 : 55,
        strategy_type: strategyType,
        bb_std_dev: strategyType === 'BB' ? parseFloat(document.getElementById('w-bb-std-dev').value) || 2 : 2,
        enabled: true,
    };
    const res = await api('/api/watch', 'POST', item);
    if (res) {
        // Auto-save to favorites
        addFavorite(item);
        log(`已新增觀察: ${symbol}（已收藏 ⭐）`, 'success');
        if (!standaloneMode) state.watchList.push(res);
        renderWatchList();
        renderFavorites();
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
        const stratClass = w.direction === 'LONG' ? 'BUY' : 'SELL';
        const stratLabel = w.direction === 'LONG' ? '📈 做多' : '📉 做空';
        const strategyType = w.strategy_type || data.strategy_type || 'MA';
        const strategyLabel = strategyType === 'BB' ? '布林帶' : 'MA';
        
        // 觸發區 + 有效性判斷
        let zone = '--';
        let zoneActive = false;
        let zoneReady = false;
        
        if (strategyType === 'BB') {
            // Bollinger Bands: trigger when approaching band within N points
            const bbUpper = data.bb_upper;
            const bbLower = data.bb_lower;
            const nPts = w.n_points || 0;
            if (w.direction === 'LONG' && bbLower) {
                const triggerPrice = bbLower + nPts;
                zone = `≤ ${triggerPrice.toFixed(2)} (下軌+${nPts})`;
                zoneActive = data.current_price <= triggerPrice;
                zoneReady = true;  // BB always ready (no direction requirement)
            } else if (w.direction === 'SHORT' && bbUpper) {
                const triggerPrice = bbUpper - nPts;
                zone = `≥ ${triggerPrice.toFixed(2)} (上軌-${nPts})`;
                zoneActive = data.current_price >= triggerPrice;
                zoneReady = true;
            }
        } else {
            // MA Strategy: price within N points of MA when direction matches
            const maRight = (w.direction === 'LONG' && dir === 'RISING') || (w.direction === 'SHORT' && dir === 'FALLING');
            zoneReady = maRight;
            if (data.ma_value) {
                if (w.direction === 'LONG') {
                    zone = `${data.ma_value.toFixed(2)} ~ ${(data.ma_value + w.n_points).toFixed(2)}`;
                    zoneActive = maRight && data.current_price >= data.ma_value && data.current_price <= data.ma_value + w.n_points;
                } else {
                    zone = `${(data.ma_value - w.n_points).toFixed(2)} ~ ${data.ma_value.toFixed(2)}`;
                    zoneActive = maRight && data.current_price >= data.ma_value - w.n_points && data.current_price <= data.ma_value;
                }
            }
        }
        const zoneStatus = (strategyType === 'BB' ? (data.bb_upper || data.bb_lower) : data.ma_value) ? (zoneActive ? '🟢' : zoneReady ? '🟡' : '⚪') : '';
        
        // Confirmation MA status
        const confirmEnabled = w.confirm_ma_enabled || data.confirm_ma_enabled;
        const confirmOk = data.confirm_ma_ok !== false;  // Default true if not set
        const confirmDir = data.confirm_ma_direction || '--';
        const confirmDirLabel = confirmDir === 'RISING' ? '↑' : confirmDir === 'FALLING' ? '↓' : '—';
        const confirmStatus = confirmEnabled ? (confirmOk ? '✓' : '✗') : '';

        const callOpts = data.options_call || [];
        const putOpts = data.options_put || [];
        const expanded = state.expandedWatch === w.id;

        html += `
        <div class="watch-item ${w.enabled ? '' : 'disabled'}" data-watch-id="${w.id}">
            <div class="watch-top-row">
                <div class="watch-symbol">
                    <span class="strategy-badge ${stratClass}">${stratLabel}</span>
                    ${w.symbol}${w.contract_month ? ` <span style="font-size:11px;color:var(--yellow);font-weight:500;">${formatContractMonth(w.contract_month)}</span>` : ''} <span style="font-size:11px;color:var(--text-muted);font-weight:400;">${w.sec_type}</span>
                </div>
                <div class="watch-actions">
                    <button class="btn btn-sm btn-icon" onclick="toggleWatch('${w.id}')" title="${w.enabled ? '停用' : '啟用'}">
                        ${w.enabled ? '⏸' : '▶️'}
                    </button>
                    <button class="btn btn-sm btn-icon btn-danger" onclick="removeWatch('${w.id}')" title="移除">🗑</button>
                </div>
            </div>
            <div class="watch-details">
                <span style="color:var(--blue)">${strategyLabel}</span>
                <span>MA${w.ma_period}</span>
                ${strategyType === 'BB' ? `<span>σ=${w.bb_std_dev || 2}</span>` : `<span>N=${w.n_points}</span>`}
                ${confirmEnabled && strategyType === 'MA' ? `<span style="color:${confirmOk ? 'var(--green)' : 'var(--red)'}">確認MA${w.confirm_ma_period || data.confirm_ma_period}${confirmDirLabel}${confirmStatus}</span>` : ''}
                <span>價格: ${price}</span>
                ${strategyType === 'BB' ? `<span>上: ${data.bb_upper ? data.bb_upper.toFixed(2) : '--'}</span><span>下: ${data.bb_lower ? data.bb_lower.toFixed(2) : '--'}</span>` : `<span>MA: ${ma}</span><span>距離: ${dist}</span>`}
            </div>
            <div class="watch-bottom-row">
                <div class="watch-ma-info">
                    <span class="ma-badge ${dirClass}">${dirLabel}</span>
                    <span class="trigger-zone ${zoneActive ? 'active' : zoneReady ? 'ready' : ''}" title="${zoneReady ? (zoneActive ? '條件滿足！' : '方向正確，等待價格進入') : '方向不符，暫不觸發'}">${zoneStatus} 觸發區: ${zone}</span>
                </div>
                <div style="display:flex;gap:4px;">
                    <button class="btn btn-sm" onclick="toggleChart('${w.id}')" title="K線圖">
                        ${state.expandedChart === w.id ? '📉' : '📈'}
                    </button>
                    ${expanded ? `<button class="btn btn-sm" onclick="updateOptionPrices('${w.id}')" title="更新報價">🔄</button>` : ''}
                    <button class="btn btn-sm" onclick="toggleExpand('${w.id}')">
                        ${expanded ? '收起 ▲' : '選擇權 ▼'}
                    </button>
                </div>
            </div>
            ${state.expandedChart === w.id ? `<div class="chart-container" id="chart-${w.id}"></div>` : ''}
            ${expanded ? renderInlineOptions(w, data, callOpts, putOpts, price) : ''}
        </div>`;
    }
    container.innerHTML = html;
}

// Lightweight price update without full DOM re-render (when chart is open)
function updatePriceDisplays(watchId, data) {
    // Find the watch item by index in watchList
    const idx = state.watchList.findIndex(w => w.id === watchId);
    if (idx < 0) return;
    
    const w = state.watchList[idx];
    const strategyType = w.strategy_type || 'MA';
    
    const items = document.querySelectorAll('.watch-item');
    if (idx >= items.length) return;
    
    const item = items[idx];
    const details = item.querySelector('.watch-details');
    if (details && data.current_price) {
        const spans = details.querySelectorAll('span');
        // spans: [strategyLabel, MA period, σ/N, 價格, 上/MA, 下/距離] or with confirm MA
        // Find the price span and update from there
        for (let i = 0; i < spans.length; i++) {
            const text = spans[i].textContent;
            if (text.startsWith('價格:')) {
                spans[i].textContent = `價格: ${data.current_price.toFixed(2)}`;
                if (strategyType === 'BB') {
                    if (spans[i+1]) spans[i+1].textContent = `上: ${data.bb_upper ? data.bb_upper.toFixed(2) : '--'}`;
                    if (spans[i+2]) spans[i+2].textContent = `下: ${data.bb_lower ? data.bb_lower.toFixed(2) : '--'}`;
                } else {
                    if (spans[i+1]) spans[i+1].textContent = `MA: ${(data.ma_value || 0).toFixed(2)}`;
                    if (spans[i+2]) spans[i+2].textContent = `距離: ${(data.distance_from_ma || 0).toFixed(2)}`;
                }
                break;
            }
        }
    }
    
    // Update trigger zone display
    const triggerZone = item.querySelector('.trigger-zone');
    if (triggerZone && data.current_price) {
        let zone = '--';
        let zoneActive = false;
        let zoneReady = false;
        
        if (strategyType === 'BB') {
            const bbUpper = data.bb_upper;
            const bbLower = data.bb_lower;
            const nPts = w.n_points || 0;
            if (w.direction === 'LONG' && bbLower) {
                const triggerPrice = bbLower + nPts;
                zone = `≤ ${triggerPrice.toFixed(2)} (下軌+${nPts})`;
                zoneActive = data.current_price <= triggerPrice;
                zoneReady = true;
            } else if (w.direction === 'SHORT' && bbUpper) {
                const triggerPrice = bbUpper - nPts;
                zone = `≥ ${triggerPrice.toFixed(2)} (上軌-${nPts})`;
                zoneActive = data.current_price >= triggerPrice;
                zoneReady = true;
            }
        } else {
            const dir = data.ma_direction || '--';
            const maRight = (w.direction === 'LONG' && dir === 'RISING') || (w.direction === 'SHORT' && dir === 'FALLING');
            zoneReady = maRight;
            if (data.ma_value) {
                if (w.direction === 'LONG') {
                    zone = `${data.ma_value.toFixed(2)} ~ ${(data.ma_value + w.n_points).toFixed(2)}`;
                    zoneActive = maRight && data.current_price >= data.ma_value && data.current_price <= data.ma_value + w.n_points;
                } else {
                    zone = `${(data.ma_value - w.n_points).toFixed(2)} ~ ${data.ma_value.toFixed(2)}`;
                    zoneActive = maRight && data.current_price >= data.ma_value - w.n_points && data.current_price <= data.ma_value;
                }
            }
        }
        const zoneStatus = (strategyType === 'BB' ? (data.bb_upper || data.bb_lower) : data.ma_value) ? (zoneActive ? '🟢' : zoneReady ? '🟡' : '⚪') : '';
        triggerZone.textContent = `${zoneStatus} 觸發區: ${zone}`;
        triggerZone.className = `trigger-zone ${zoneActive ? 'active' : zoneReady ? 'ready' : ''}`;
    }
}

async function resetOptions(watchId) {
    const data = state.latestData[watchId];
    const w = state.watchList.find(x => x.id === watchId);
    if (!w) return;
    const base = data?.current_price || 100;
    const maVal = data?.ma_value || base;
    
    if (standaloneMode) {
        // Demo mode: use generated data
        if (data) {
            data.options_call = genDemoOptions(w.symbol, 'C', maVal, base);
            data.options_put = genDemoOptions(w.symbol, 'P', maVal, base);
            data.locked_ma = maVal;
            data.selected_expiry = Object.keys(data.options_call)[0];
        }
        renderWatchList();
        log(`${w.symbol} 選擇權已依 MA=${maVal.toFixed(2)} 重新篩選`, 'success');
        return;
    }
    
    // Real mode: call backend refresh endpoint (re-cache + price update)
    log(`正在刷新 ${w.symbol} 期權...`, 'info');
    try {
        const res = await api(`/api/options/refresh/${watchId}`, 'POST');
        if (res?.ok) {
            log(`${w.symbol} 期權已刷新 (Call:${res.calls} Put:${res.puts})`, 'success');
            // data_update will arrive via WebSocket and trigger re-render
        } else {
            log(`刷新失敗: ${res?.error || '未知錯誤'}`, 'error');
        }
    } catch (e) {
        log(`刷新失敗: ${e.message}`, 'error');
    }
}

async function updateOptionPrices(watchId) {
    const w = state.watchList.find(x => x.id === watchId);
    if (!w) return;
    log(`正在更新 ${w.symbol} 報價...`, 'info');
    try {
        const res = await api(`/api/options/prices/${watchId}`, 'POST');
        if (res?.ok) {
            log(`${w.symbol} 報價已更新`, 'success');
        } else {
            log(`更新失敗: ${res?.error || '未知'}`, 'error');
        }
    } catch (e) {
        log(`更新失敗: ${e.message}`, 'error');
    }
}

// ─── Chart Functions ───

// Update today's live candle and MA with new price
function updateLiveCandle(watchId, price) {
    if (!price || !chartSeries[watchId]?.candle) return;
    
    const todayTime = Math.floor(Date.now() / 1000 / 86400) * 86400;
    
    // Update live MA point (middle line for BB)
    const data = state.latestData[watchId];
    if (data?.ma_value && chartSeries[watchId]?.ma) {
        chartSeries[watchId].ma.update({ time: todayTime, value: data.ma_value });
    }
    
    // Update live BB bands
    if (data?.bb_upper && chartSeries[watchId]?.bbUpper) {
        chartSeries[watchId].bbUpper.update({ time: todayTime, value: data.bb_upper });
    }
    if (data?.bb_lower && chartSeries[watchId]?.bbLower) {
        chartSeries[watchId].bbLower.update({ time: todayTime, value: data.bb_lower });
    }
    
    // Update live confirm MA point
    if (data?.confirm_ma_value && chartSeries[watchId]?.confirmMa) {
        chartSeries[watchId].confirmMa.update({ time: todayTime, value: data.confirm_ma_value });
    }
    
    if (!todayCandle[watchId] || todayCandle[watchId].time < todayTime) {
        // New day - create new candle
        todayCandle[watchId] = {
            time: todayTime,
            open: price,
            high: price,
            low: price,
            close: price,
        };
    } else {
        // Update existing candle
        todayCandle[watchId].close = price;
        todayCandle[watchId].high = Math.max(todayCandle[watchId].high, price);
        todayCandle[watchId].low = Math.min(todayCandle[watchId].low, price);
    }
    
    chartSeries[watchId].candle.update(todayCandle[watchId]);
}

async function toggleChart(watchId) {
    if (state.expandedChart === watchId) {
        // Collapse chart
        state.expandedChart = null;
        if (charts[watchId]) {
            charts[watchId].remove();
            delete charts[watchId];
        }
        delete chartSeries[watchId];
        delete todayCandle[watchId];
    } else {
        // Expand chart — close other chart first
        if (state.expandedChart && charts[state.expandedChart]) {
            charts[state.expandedChart].remove();
            delete charts[state.expandedChart];
        }
        state.expandedChart = watchId;
    }
    renderWatchList();
    
    // Render chart after DOM update
    if (state.expandedChart === watchId) {
        setTimeout(() => renderChart(watchId), 50);
    }
}

async function renderChart(watchId) {
    const container = document.getElementById(`chart-${watchId}`);
    if (!container) return;
    if (typeof LightweightCharts === 'undefined') {
        container.innerHTML = '<div style="padding:20px;color:var(--red)">圖表庫載入失敗</div>';
        return;
    }
    
    // Fetch candle data
    let chartData;
    try {
        const res = await fetch(`${API}/api/candles/${watchId}`, {
            headers: authToken ? { 'Authorization': `Bearer ${authToken}` } : {}
        });
        chartData = await res.json();
    } catch (e) {
        container.innerHTML = '<div style="padding:20px;color:var(--text-muted)">載入圖表失敗</div>';
        return;
    }
    
    if (!chartData.candles?.length) {
        container.innerHTML = '<div style="padding:20px;color:var(--text-muted)">無K線數據</div>';
        return;
    }
    
    // Create chart
    const chart = LightweightCharts.createChart(container, {
        width: container.clientWidth,
        height: 280,
        layout: {
            background: { color: '#0d1117' },
            textColor: '#8b949e',
        },
        grid: {
            vertLines: { color: '#21262d' },
            horzLines: { color: '#21262d' },
        },
        crosshair: {
            mode: LightweightCharts.CrosshairMode.Normal,
        },
        rightPriceScale: {
            borderColor: '#30363d',
        },
        timeScale: {
            borderColor: '#30363d',
            timeVisible: true,
        },
    });
    charts[watchId] = chart;
    
    // Candlestick series
    const candleSeries = chart.addCandlestickSeries({
        upColor: '#238636',
        downColor: '#da3633',
        borderUpColor: '#238636',
        borderDownColor: '#da3633',
        wickUpColor: '#238636',
        wickDownColor: '#da3633',
    });
    candleSeries.setData(chartData.candles);
    
    // Store series reference for live updates
    chartSeries[watchId] = { candle: candleSeries, ma: null };
    
    // Initialize today's candle from current price
    const data = state.latestData[watchId];
    if (data?.current_price) {
        const todayTime = Math.floor(Date.now() / 1000 / 86400) * 86400;  // Start of today UTC
        const lastCandle = chartData.candles[chartData.candles.length - 1];
        
        // Only add today's candle if it's a new day
        if (lastCandle.time < todayTime) {
            todayCandle[watchId] = {
                time: todayTime,
                open: data.current_price,
                high: data.current_price,
                low: data.current_price,
                close: data.current_price,
            };
            candleSeries.update(todayCandle[watchId]);
        }
    }
    
    // MA line (middle line for BB)
    let maSeries = null;
    if (chartData.ma?.length) {
        maSeries = chart.addLineSeries({
            color: '#f0883e',
            lineWidth: 2,
            title: chartData.strategy_type === 'BB' ? `中軌MA${chartData.ma_period}` : `MA${chartData.ma_period}`,
        });
        maSeries.setData(chartData.ma);
        chartSeries[watchId].ma = maSeries;
        
        // Add today's MA point from real-time data
        if (data?.ma_value) {
            const todayTime = Math.floor(Date.now() / 1000 / 86400) * 86400;
            const lastMATime = chartData.ma[chartData.ma.length - 1]?.time || 0;
            if (lastMATime < todayTime) {
                maSeries.update({ time: todayTime, value: data.ma_value });
            }
        }
    }
    
    // BB upper/lower bands
    if (chartData.bb_upper?.length && chartData.bb_lower?.length) {
        const bbUpperSeries = chart.addLineSeries({
            color: '#8b5cf6',  // Purple for upper band
            lineWidth: 1,
            lineStyle: LightweightCharts.LineStyle.Dotted,
            title: `上軌 (${chartData.bb_std_dev}σ)`,
        });
        bbUpperSeries.setData(chartData.bb_upper);
        chartSeries[watchId].bbUpper = bbUpperSeries;
        
        const bbLowerSeries = chart.addLineSeries({
            color: '#8b5cf6',  // Purple for lower band
            lineWidth: 1,
            lineStyle: LightweightCharts.LineStyle.Dotted,
            title: `下軌 (${chartData.bb_std_dev}σ)`,
        });
        bbLowerSeries.setData(chartData.bb_lower);
        chartSeries[watchId].bbLower = bbLowerSeries;
        
        // Add today's BB points from real-time data
        if (data?.bb_upper && data?.bb_lower) {
            const todayTime = Math.floor(Date.now() / 1000 / 86400) * 86400;
            const lastBBTime = chartData.bb_upper[chartData.bb_upper.length - 1]?.time || 0;
            if (lastBBTime < todayTime) {
                bbUpperSeries.update({ time: todayTime, value: data.bb_upper });
                bbLowerSeries.update({ time: todayTime, value: data.bb_lower });
            }
        }
    }
    
    // Confirm MA line (if enabled)
    if (chartData.confirm_ma?.length) {
        const confirmMaSeries = chart.addLineSeries({
            color: '#58a6ff',  // Blue color for confirm MA
            lineWidth: 1,
            lineStyle: LightweightCharts.LineStyle.Dashed,
            title: `確認MA${chartData.confirm_ma_period}`,
        });
        confirmMaSeries.setData(chartData.confirm_ma);
        chartSeries[watchId].confirmMa = confirmMaSeries;
    }
    
    // Trigger zone markers (horizontal lines)
    if (chartData.trigger_low && chartData.trigger_high) {
        const zoneSeries = chart.addLineSeries({
            color: chartData.direction === 'LONG' ? '#238636' : '#da3633',
            lineWidth: 1,
            lineStyle: LightweightCharts.LineStyle.Dashed,
            priceLineVisible: false,
            lastValueVisible: false,
        });
        const lastTime = chartData.candles[chartData.candles.length - 1].time;
        zoneSeries.setData([
            { time: lastTime - 86400 * 30, value: chartData.trigger_low },
            { time: lastTime, value: chartData.trigger_low },
        ]);
        const zoneSeries2 = chart.addLineSeries({
            color: chartData.direction === 'LONG' ? '#238636' : '#da3633',
            lineWidth: 1,
            lineStyle: LightweightCharts.LineStyle.Dashed,
            priceLineVisible: false,
            lastValueVisible: false,
        });
        zoneSeries2.setData([
            { time: lastTime - 86400 * 30, value: chartData.trigger_high },
            { time: lastTime, value: chartData.trigger_high },
        ]);
    }
    
    // Resize handler
    const resizeObserver = new ResizeObserver(() => {
        chart.applyOptions({ width: container.clientWidth });
    });
    resizeObserver.observe(container);
    
    chart.timeScale().fitContent();
}

function toggleExpand(watchId) {
    if (state.expandedWatch === watchId) {
        state.expandedWatch = null;
    } else {
        state.expandedWatch = watchId;
        // Refresh current expiry prices when expanding
        if (!standaloneMode) {
            const data = state.latestData[watchId];
            const callKeys = Object.keys(data?.options_call || {});
            const putKeys = Object.keys(data?.options_put || {});
            const expiries = callKeys.length ? callKeys : putKeys;
            const expiry = data?.selected_expiry || expiries[0];
            if (expiry) {
                api(`/api/options/prices/${watchId}?expiry=${expiry}`, 'POST').catch(() => {});
            }
        }
    }
    renderWatchList();
}

function renderInlineOptions(watch, data, callOptsData, putOptsData, price) {
    // Determine expiries from whichever side has data (LONG=call, SHORT=put)
    const callKeys = Object.keys(callOptsData || {});
    const putKeys = Object.keys(putOptsData || {});
    const expiries = callKeys.length ? callKeys : putKeys;
    if (!expiries.length) {
        return '<div class="opts-section"><div class="empty-state">尚無選擇權數據</div></div>';
    }

    const selectedExpiry = data.selected_expiry || expiries[0];
    const callOpts = (callOptsData[selectedExpiry]?.options || []).slice().sort((a, b) => b.strike - a.strike);
    const putOpts = (putOptsData[selectedExpiry]?.options || []).slice().sort((a, b) => b.strike - a.strike);

    // Expiry tabs
    const expiryTabs = expiries.map(exp => {
        const info = callOptsData[exp]?.expiry || {};
        const isActive = exp === selectedExpiry;
        return `<button class="expiry-tab ${isActive ? 'active' : ''}" onclick="selectExpiry('${watch.id}','${exp}')">${info.label || exp}${isActive ? ' ✓' : ''}</button>`;
    }).join('');

    const sel = state.optSelections[watch.id] || {};
    const renderSide = (opts, label, color) => {
        if (!opts.length) return '';
        let rows = opts.map((o, i) => {
            const optKey = `${o.conId || o.right + '-' + i}`;
            const optSel = sel[optKey] || {};
            const checked = optSel.checked ? 'checked' : '';
            const amt = optSel.amount || 1000;
            const mult = o.multiplier || 100;
            return `
            <div class="opt-inline-row">
                <input type="checkbox" id="opt-${watch.id}-${optKey}" class="opt-check"
                    data-conid="${o.conId}" data-ask="${o.ask}" data-bid="${o.bid}"
                    data-strike="${o.strike}" data-right="${o.right}" data-expiry="${o.expiry}"
                    data-multiplier="${mult}" data-key="${optKey}" ${checked}
                    onchange="saveOptSel('${watch.id}','${optKey}',this.checked)">
                <span class="opt-inline-strike">${o.strike}</span>
                <span class="opt-inline-mult">×${mult}</span>
                <span class="opt-inline-name">${o.expiryLabel || ''} ${o.right}</span>
                <span class="opt-inline-ba">${o.bid?.toFixed(2) ?? '--'}/${o.ask?.toFixed(2) ?? '--'}</span>
                <span class="opt-inline-last" style="color:${color}">$${o.last?.toFixed(2) || '--'}</span>
                <span class="opt-inline-vol">${o.volume || '--'}</span>
                <input type="number" value="${amt}" min="100" step="100" class="opt-inline-amt" placeholder="金額" onchange="saveOptAmt('${watch.id}','${optKey}',this.value)">
            </div>`;
        }).join('');
        return `<div class="opt-inline-group">
            <div class="opt-inline-label" style="color:${color}">${label}</div>
            <div class="opt-inline-header">
                <span></span><span>履約價</span><span>乘數</span><span>到期</span><span>Bid/Ask</span><span>Last</span><span>Vol</span><span>金額$</span>
            </div>
            ${rows}
        </div>`;
    };

    // Also show underlying as tradeable
    const stkSel = sel['stk'] || {};
    const stkChecked = stkSel.checked ? 'checked' : '';
    const stkAmt = stkSel.amount || 5000;
    const underlying = `
        <div class="opt-inline-row" style="border-bottom:1px solid var(--border);padding-bottom:6px;margin-bottom:6px;">
            <input type="checkbox" id="opt-${watch.id}-stk" class="opt-check" data-ask="${price}" data-key="stk" ${stkChecked} onchange="saveOptSel('${watch.id}','stk',this.checked)">
            <span class="opt-inline-strike" style="color:var(--blue);">標的</span>
            <span class="opt-inline-name">📈 ${watch.symbol}</span>
            <span class="opt-inline-ba">--</span>
            <span class="opt-inline-last" style="color:var(--blue)">$${price}</span>
            <input type="number" value="${stkAmt}" min="100" step="100" class="opt-inline-amt" placeholder="金額" onchange="saveOptAmt('${watch.id}','stk',this.value)">
        </div>`;
    
    // Exit strategy configuration (preserve state)
    const ex = sel.exit || {};
    const exitConfig = `
        <div class="exit-config">
            <div class="exit-config-title">📤 平倉策略（可多選）</div>
            <div class="exit-option">
                <label><input type="checkbox" id="exit-${watch.id}-profit" ${ex.profit ? 'checked' : ''} onchange="saveExitSel('${watch.id}','profit',this.checked)"> 1️⃣ 限價止盈</label>
                <span>成交價 <select id="exit-${watch.id}-profit-dir" onchange="saveExitVal('${watch.id}','profitDir',this.value)">
                    <option value="+" ${ex.profitDir === '+' || !ex.profitDir ? 'selected' : ''}>+</option>
                    <option value="-" ${ex.profitDir === '-' ? 'selected' : ''}>-</option>
                </select>
                <input type="number" id="exit-${watch.id}-profit-pts" value="${ex.profitPts || 0.5}" step="0.1" min="0" class="exit-input" onchange="saveExitVal('${watch.id}','profitPts',this.value)"> 點</span>
            </div>
            <div class="exit-option">
                <label><input type="checkbox" id="exit-${watch.id}-time" ${ex.time ? 'checked' : ''} onchange="saveExitSel('${watch.id}','time',this.checked)"> 2️⃣ 時間平倉</label>
                <input type="time" id="exit-${watch.id}-time-val" value="${ex.timeVal || '15:55'}" class="exit-input" onchange="saveExitVal('${watch.id}','timeVal',this.value)">
            </div>
            <div class="exit-option">
                <label><input type="checkbox" id="exit-${watch.id}-ma" ${ex.ma ? 'checked' : ''} onchange="saveExitSel('${watch.id}','ma',this.checked)"> 3️⃣ 均線平倉</label>
                <span>標的 <select id="exit-${watch.id}-ma-cond" onchange="saveExitVal('${watch.id}','maCond',this.value)">
                    <option value="above" ${ex.maCond === 'above' || !ex.maCond ? 'selected' : ''}>高於</option>
                    <option value="below" ${ex.maCond === 'below' ? 'selected' : ''}>低於</option>
                </select> MA <select id="exit-${watch.id}-ma-dir" onchange="saveExitVal('${watch.id}','maDir',this.value)">
                    <option value="+" ${ex.maDir === '+' || !ex.maDir ? 'selected' : ''}>+</option>
                    <option value="-" ${ex.maDir === '-' ? 'selected' : ''}>-</option>
                </select>
                <input type="number" id="exit-${watch.id}-ma-pts" value="${ex.maPts || 5}" step="0.5" min="0" class="exit-input" onchange="saveExitVal('${watch.id}','maPts',this.value)"> 點</span>
            </div>
            <div class="exit-option">
                <label><input type="checkbox" id="exit-${watch.id}-bb" ${ex.bb ? 'checked' : ''} onchange="saveExitSel('${watch.id}','bb',this.checked)"> 4️⃣ 布林帶平倉</label>
                <span>價格 <select id="exit-${watch.id}-bb-cond" onchange="saveExitVal('${watch.id}','bbCond',this.value)">
                    <option value="above" ${ex.bbCond === 'above' || !ex.bbCond ? 'selected' : ''}>高於</option>
                    <option value="below" ${ex.bbCond === 'below' ? 'selected' : ''}>低於</option>
                </select> <select id="exit-${watch.id}-bb-target" onchange="saveExitVal('${watch.id}','bbTarget',this.value)">
                    <option value="middle" ${ex.bbTarget === 'middle' || !ex.bbTarget ? 'selected' : ''}>中軌</option>
                    <option value="opposite" ${ex.bbTarget === 'opposite' ? 'selected' : ''}>反向軌</option>
                </select> <select id="exit-${watch.id}-bb-dir" onchange="saveExitVal('${watch.id}','bbDir',this.value)">
                    <option value="+" ${ex.bbDir === '+' || !ex.bbDir ? 'selected' : ''}>+</option>
                    <option value="-" ${ex.bbDir === '-' ? 'selected' : ''}>-</option>
                </select>
                <input type="number" id="exit-${watch.id}-bb-pts" value="${ex.bbPts || 0}" step="0.5" min="0" class="exit-input" onchange="saveExitVal('${watch.id}','bbPts',this.value)"> 點</span>
            </div>
            <div class="exit-actions">
                <button class="btn btn-sm btn-success" onclick="placeOrder('${watch.id}')">📥 市價下單</button>
            </div>
        </div>`;

    const lockedInfo = data.locked_ma
        ? `<div style="font-size:11px;color:var(--text-muted);margin-bottom:6px;">🔒 鎖定 MA = ${data.locked_ma.toFixed(2)}</div>`
        : '';

    const direction = watch.direction || 'LONG';
    const showCall = direction === 'LONG';
    const showPut = direction === 'SHORT';

    return `<div class="opts-section">
        <div class="expiry-tabs-row">
            <span style="font-size:11px;color:var(--text-muted);margin-right:8px;">到期日:</span>
            ${expiryTabs}
        </div>
        ${lockedInfo}
        ${underlying}
        ${showCall ? renderSide(callOpts, 'Call 價外5檔（做多買權）', 'var(--green)') : ''}
        ${showPut ? renderSide(putOpts, 'Put 價外5檔（做空買權）', 'var(--red)') : ''}
        ${exitConfig}
    </div>`;
}

async function selectExpiry(watchId, expiry) {
    if (state.latestData[watchId]) {
        state.latestData[watchId].selected_expiry = expiry;
        renderWatchList();

        // Fetch latest prices for this expiry (snapshot)
        if (!standaloneMode) {
            try {
                await api(`/api/options/prices/${watchId}?expiry=${expiry}`, 'POST');
            } catch (e) {
                // Silent fail — cached data still shows
            }
        }
    }
}

// Save/restore option selections
function saveOptSel(watchId, key, checked) {
    if (!state.optSelections[watchId]) state.optSelections[watchId] = {};
    if (!state.optSelections[watchId][key]) state.optSelections[watchId][key] = {};
    state.optSelections[watchId][key].checked = checked;
}
function saveOptAmt(watchId, key, amount) {
    if (!state.optSelections[watchId]) state.optSelections[watchId] = {};
    if (!state.optSelections[watchId][key]) state.optSelections[watchId][key] = {};
    state.optSelections[watchId][key].amount = parseFloat(amount);
}
function saveExitSel(watchId, key, checked) {
    if (!state.optSelections[watchId]) state.optSelections[watchId] = {};
    if (!state.optSelections[watchId].exit) state.optSelections[watchId].exit = {};
    state.optSelections[watchId].exit[key] = checked;
}
function saveExitVal(watchId, key, value) {
    if (!state.optSelections[watchId]) state.optSelections[watchId] = {};
    if (!state.optSelections[watchId].exit) state.optSelections[watchId].exit = {};
    state.optSelections[watchId].exit[key] = value;
}

async function placeOrder(watchId) {
    const w = state.watchList.find(x => x.id === watchId);
    if (!w) return;

    const sel = state.optSelections[watchId] || {};
    const ex = sel.exit || {};

    // Collect checked options from DOM (has latest data attributes)
    const checkboxes = document.querySelectorAll(`input.opt-check[id^="opt-${watchId}-"]:checked`);
    if (checkboxes.length === 0) {
        log('請先勾選要交易的選擇權', 'warning');
        return;
    }

    // Build order items from DOM
    const items = [];
    const displayItems = [];
    checkboxes.forEach(chk => {
        const conId = parseInt(chk.dataset.conid);
        const ask = parseFloat(chk.dataset.ask);
        const strike = chk.dataset.strike;
        const right = chk.dataset.right;
        const expiry = chk.dataset.expiry;
        const key = chk.dataset.key;
        const amtInput = chk.closest('.opt-inline-row')?.querySelector('.opt-inline-amt');
        const amount = parseFloat(amtInput?.value) || 1000;

        if (key === 'stk') return; // Skip underlying for now (options only)
        if (!conId || !ask || ask <= 0) return;

        // Use actual multiplier from contract (FOP: MNQ=2, MES=5; STK options=100)
        const multiplier = parseFloat(chk.dataset.multiplier) || 100;
        const qty = Math.max(1, Math.floor(amount / (ask * multiplier)));
        items.push({ conId, ask, amount, right, strike: parseFloat(strike), expiry });
        displayItems.push({ strike, right, expiry: expiry?.slice(4), ask, qty, amount, multiplier });
    });

    if (items.length === 0) {
        log('無有效選擇權可下單（需有 Ask 價格）', 'warning');
        return;
    }

    // Build exit strategies
    const exitConfig = {
        limit: {
            enabled: !!ex.profit,
            dir: ex.profitDir || '+',
            pts: parseFloat(ex.profitPts) || 0.5,
        },
        time: {
            enabled: !!ex.time,
            value: ex.timeVal || '15:55',
        },
        ma: {
            enabled: !!ex.ma,
            cond: ex.maCond || 'above',
            dir: ex.maDir || '+',
            pts: parseFloat(ex.maPts) || 5,
        },
        bb: {
            enabled: !!ex.bb,
            cond: ex.bbCond || 'above',       // 'above' or 'below'
            target: ex.bbTarget || 'middle',  // 'middle' or 'opposite'
            dir: ex.bbDir || '+',             // '+' or '-'
            pts: parseFloat(ex.bbPts) || 0,
        },
    };

    // Show confirmation
    const exitDesc = [];
    if (exitConfig.limit.enabled) exitDesc.push(`限價止盈 ${exitConfig.limit.dir}${exitConfig.limit.pts}點`);
    if (exitConfig.time.enabled) exitDesc.push(`時間平倉 ${exitConfig.time.value}`);
    if (exitConfig.ma.enabled) exitDesc.push(`均線平倉 ${exitConfig.ma.cond === 'above' ? '高於' : '低於'}MA${exitConfig.ma.dir}${exitConfig.ma.pts}點`);
    if (exitConfig.bb.enabled) {
        const targetLabel = exitConfig.bb.target === 'middle' ? '中軌' : '反向軌';
        const condLabel = exitConfig.bb.cond === 'above' ? '高於' : '低於';
        exitDesc.push(`BB平倉 ${condLabel}${targetLabel}${exitConfig.bb.dir}${exitConfig.bb.pts}點`);
    }

    let confirmMsg = `確認下單 ${w.symbol}？\n\n`;
    displayItems.forEach(d => {
        const cost = d.ask * d.qty * d.multiplier;
        confirmMsg += `${d.right} ${d.strike} (${d.expiry}) | Ask $${d.ask} × ${d.qty}口 × ${d.multiplier} = $${cost.toFixed(0)}\n`;
    });
    if (exitDesc.length) {
        confirmMsg += `\n平倉策略: ${exitDesc.join(' / ')}`;
    } else {
        confirmMsg += `\n⚠️ 未設定平倉策略`;
    }

    if (!confirm(confirmMsg)) {
        log('下單已取消', 'info');
        return;
    }

    // Send order to backend
    log(`📥 正在下單 ${w.symbol}...`, 'info');
    try {
        const res = await api('/api/order', 'POST', {
            watch_id: watchId,
            items,
            exit: exitConfig,
        });

        if (res?.ok) {
            log(`✅ ${w.symbol} 下單成功！Trade ID: ${res.trade_id}`, 'success');
            (res.orders || []).forEach(o => {
                const status = o.status || 'Unknown';
                const fill = o.avgFillPrice ? `成交 $${o.avgFillPrice}` : '等待成交';
                log(`   Order #${o.orderId}: ${status} — ${fill} (${o.qty_requested}口)`, 'info');
                if (o.exit_limit_order) {
                    log(`   📤 限價止盈已掛: Order #${o.exit_limit_order.orderId}`, 'info');
                }
            });
            showToast(`${w.symbol} 下單成功`, 'buy');
        } else {
            log(`❌ 下單失敗: ${res?.error || '未知錯誤'}`, 'error');
            showToast(`${w.symbol} 下單失敗`, 'sell');
        }
    } catch (e) {
        log(`❌ 下單錯誤: ${e.message}`, 'error');
        showToast('下單失敗', 'sell');
    }
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

// ─── Favorites (localStorage) ───
const FAV_KEY = 'trademon_favorites';
function loadFavorites() {
    try { return JSON.parse(localStorage.getItem(FAV_KEY)) || []; } catch(e) { return []; }
}
function saveFavorites(favs) {
    localStorage.setItem(FAV_KEY, JSON.stringify(favs));
}
function addFavorite(item) {
    const favs = loadFavorites();
    // Dedupe by symbol+secType
    if (favs.some(f => f.symbol === item.symbol && f.sec_type === item.sec_type)) return;
    favs.push({ symbol: item.symbol, sec_type: item.sec_type, exchange: item.exchange, currency: item.currency });
    saveFavorites(favs);
}
function removeFavorite(symbol, secType) {
    const favs = loadFavorites().filter(f => !(f.symbol === symbol && f.sec_type === secType));
    saveFavorites(favs);
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

            // Lock options at start — only generate once
            const existing = state.latestData[w.id];
            const callOptsData = (existing && existing.options_call) ? existing.options_call : genDemoOptions(w.symbol, 'C', maVal, base);
            const putOptsData = (existing && existing.options_put) ? existing.options_put : genDemoOptions(w.symbol, 'P', maVal, base);
            const lockedMa = (existing && existing.locked_ma) ? existing.locked_ma : maVal;
            const selectedExpiry = (existing && existing.selected_expiry) ? existing.selected_expiry : Object.keys(callOptsData)[0];

            state.latestData[w.id] = {
                symbol: w.symbol, current_price: price, ma_value: maVal,
                prev_ma: prevMa, ma_period: w.ma_period,
                ma_direction: rising ? 'RISING' : 'FALLING',
                n_points: w.n_points, distance_from_ma: dist,
                buy_zone: rising ? `${maVal.toFixed(2)} ~ ${(maVal + w.n_points).toFixed(2)}` : null,
                sell_zone: !rising ? `${(maVal - w.n_points).toFixed(2)} ~ ${maVal.toFixed(2)}` : null,
                last_updated: new Date().toISOString(),
                options_call: callOptsData,
                options_put: putOptsData,
                locked_ma: lockedMa,
                selected_expiry: selectedExpiry,
            };
            // 5% chance signal — only if matches direction
            const direction = w.direction || 'LONG';
            const canBuy = direction === 'LONG' && rising;
            const canSell = direction === 'SHORT' && !rising;
            if (Math.random() < 0.03 && w.enabled && (canBuy || canSell)) {
                const sigType = canBuy ? 'BUY' : 'SELL';
                const sig = { timestamp: new Date().toISOString(), watch_id: w.id, symbol: w.symbol,
                    signal_type: sigType, price, ma_value: maVal, ma_period: w.ma_period,
                    n_points: w.n_points, distance: Math.abs(dist) };
                state.signals.unshift(sig);
                renderSignals();
                showSignalToast(sig);
            }
        }
        renderWatchList();
    }, 8000);
}

function genDemoOptions(symbol, right, maVal, basePrice) {
    const expiries = getNearestExpiries(2);
    const step = basePrice > 1000 ? 25 : basePrice > 100 ? 5 : 1;
    const baseStrike = Math.round(maVal / step) * step;
    const result = {};
    
    for (const exp of expiries) {
        const opts = [];
        for (let i = 0; i < 5; i++) {
            const strike = right === 'C' ? baseStrike + (i + 1) * step : baseStrike - (i + 1) * step;
            const dist = Math.abs(strike - basePrice);
            const bid = +(Math.max(0.5, (15 - dist / basePrice * 100) * Math.random() + 1)).toFixed(2);
            const ask = +(bid + Math.random() * 0.5 + 0.05).toFixed(2);
            opts.push({
                symbol, expiry: exp.value, expiryLabel: exp.label, strike, right,
                name: `${symbol} ${exp.label} ${strike}${right}`,
                bid, ask, last: +((bid + ask) / 2).toFixed(2),
                volume: Math.floor(Math.random() * 5000 + 100),
            });
        }
        result[exp.value] = { expiry: exp, options: opts };
    }
    return result;
}

// Override API for standalone mode
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
    return _realApi(path, method, body);
}

// ─── Init ───
function initApp() {
    log('Trading Monitor 已載入', 'info');

    // Register Service Worker
    if ('serviceWorker' in navigator) {
        navigator.serviceWorker.register('/static/sw.js').then(() => {
            log('PWA Service Worker 已註冊', 'info');
        }).catch(e => log('SW 註冊失敗: ' + e.message, 'warning'));
    }

    // Connect WebSocket (will auto-reconnect on failure)
    connectWS();

    // Keyboard shortcut: Enter to add watch
    document.getElementById('w-symbol').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') addWatch();
    });
    
    // Update contract months when symbol changes (for different futures types)
    document.getElementById('w-symbol').addEventListener('change', updateContractDropdown);
    document.getElementById('w-symbol').addEventListener('blur', updateContractDropdown);

    // Show/hide contract month for futures
    document.getElementById('w-sectype').addEventListener('change', updateContractDropdown);
    updateContractDropdown();
}

window.addEventListener('load', async () => {
    // Check if already authenticated
    const isAuthed = await checkAuth();
    if (isAuthed) {
        initApp();
    } else {
        showLogin();
    }
});

function updateStrategyFields() {
    const strategyType = document.getElementById('w-strategy-type').value;
    const nPointsGroup = document.getElementById('w-n-points-group');
    const bbStdGroup = document.getElementById('w-bb-std-group');
    const confirmMaGroup = document.getElementById('w-confirm-ma-group');
    
    if (strategyType === 'BB') {
        // Bollinger Bands: show std dev, hide confirm MA (BB doesn't need MA direction)
        bbStdGroup.style.display = 'block';
        confirmMaGroup.style.display = 'none';
        const label = document.querySelector('#w-n-points-group label');
        if (label) label.textContent = '緩衝點數';
    } else {
        // MA Strategy: hide std dev, show confirm MA
        bbStdGroup.style.display = 'none';
        confirmMaGroup.style.display = 'block';
        const label = document.querySelector('#w-n-points-group label');
        if (label) label.textContent = 'N 點';
    }
}

function updateContractDropdown() {
    const secType = document.getElementById('w-sectype').value;
    const symbol = document.getElementById('w-symbol').value.trim().toUpperCase();
    const group = document.getElementById('w-contract-group');
    const select = document.getElementById('w-contract');
    const exchangeInput = document.getElementById('w-exchange');
    if (secType === 'FUT') {
        group.style.display = 'block';
        // 金屬期貨用 COMEX，其他用 CME
        const metalSymbols = ['GC', 'MGC', 'SI', 'HG', 'PA', 'PL'];
        exchangeInput.value = metalSymbols.includes(symbol) ? 'COMEX' : 'CME';
        const months = getNearestContractMonths(3, symbol);  // 顯示3個月份
        select.innerHTML = months.map((m, i) => 
            `<option value="${m.value}">${m.label}${i === 0 ? ' (近月)' : ''}</option>`
        ).join('');
    } else {
        group.style.display = 'none';
        exchangeInput.value = 'SMART';  // 股票恢復 SMART
    }
}

function formatContractMonth(yyyymm) {
    if (!yyyymm) return '';
    const y = yyyymm.slice(0, 4);
    const m = parseInt(yyyymm.slice(4, 6));
    const codes = { 3: 'H', 6: 'M', 9: 'U', 12: 'Z' };
    const code = codes[m] || '';
    return `${y}/${String(m).padStart(2, '0')}${code ? ` (${code}${y.slice(-2)})` : ''}`;
}

function getNearestExpiries(count) {
    // Get nearest weekly/monthly option expiries (Fridays)
    const results = [];
    const now = new Date();
    let d = new Date(now);
    // Find next Friday
    while (d.getDay() !== 5) d.setDate(d.getDate() + 1);
    // If today is Friday and market closed, skip to next
    if (d.toDateString() === now.toDateString() && now.getHours() >= 16) {
        d.setDate(d.getDate() + 7);
    }
    for (let i = 0; i < count; i++) {
        const yyyy = d.getFullYear();
        const mm = String(d.getMonth() + 1).padStart(2, '0');
        const dd = String(d.getDate()).padStart(2, '0');
        results.push({
            value: `${yyyy}${mm}${dd}`,
            label: `${mm}/${dd}`,
            full: `${yyyy}/${mm}/${dd}`
        });
        d.setDate(d.getDate() + 7);
    }
    return results;
}

function getNearestContractMonths(count, symbol = '') {
    // Different futures have different contract months:
    // - Index (ES, MES, NQ, MNQ, BRR, MBT): Quarterly (3, 6, 9, 12)
    // - Metals (GC, MGC, SI, HG): Bi-monthly (2, 4, 6, 8, 10, 12)
    // - Energy (CL, NG): Monthly (1-12)
    
    const sym = symbol.toUpperCase();
    let codeMonths, codes;
    
    if (['GC', 'MGC', 'SI', 'HG', 'PA', 'PL'].includes(sym)) {
        // Metals: bi-monthly
        codeMonths = [2, 4, 6, 8, 10, 12];
        codes = ['G', 'J', 'M', 'Q', 'V', 'Z'];
    } else if (['CL', 'NG', 'RB', 'HO'].includes(sym)) {
        // Energy: monthly
        codeMonths = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12];
        codes = ['F', 'G', 'H', 'J', 'K', 'M', 'N', 'Q', 'U', 'V', 'X', 'Z'];
    } else {
        // Default: quarterly (ES, MES, NQ, MNQ, etc.)
        codeMonths = [3, 6, 9, 12];
        codes = ['H', 'M', 'U', 'Z'];
    }
    
    const now = new Date();
    let year = now.getFullYear();
    let month = now.getMonth() + 1;
    const results = [];
    
    while (results.length < count) {
        for (let i = 0; i < codeMonths.length && results.length < count; i++) {
            const cm = codeMonths[i];
            const cy = cm < month ? year + 1 : year;
            if (cy > year || cm >= month) {
                const yy = String(cy).slice(-2);
                const value = `${cy}${String(cm).padStart(2, '0')}`;
                const label = `${cy}/${String(cm).padStart(2, '0')} (${codes[i]}${yy})`;
                if (!results.find(r => r.value === value)) {
                    results.push({ value, label });
                }
            }
        }
        year++;
    }
    return results;
}
