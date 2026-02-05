/* Trading Monitor — Frontend Application */

const API = '';
let ws = null;
let state = {
    connected: false,
    monitoring: false,
    demoMode: false,
    watchList: [],
    expandedWatch: null,
    signals: [],
    optSelections: {},  // { watchId: { optId: { checked, amount }, exitProfit: bool, ... } }
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
        case 'data_update': {
            // Merge incoming data, preserve client-side state (selected_expiry, etc.)
            const prev = state.latestData[msg.watch_id] || {};
            state.latestData[msg.watch_id] = { ...prev, ...msg.data };
            if (prev.selected_expiry && !msg.data.selected_expiry) {
                state.latestData[msg.watch_id].selected_expiry = prev.selected_expiry;
            }
            renderWatchList();
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
        case 'error':
            log(msg.message, 'error');
            break;
    }
}

// ─── API calls ───
async function _realApi(path, method = 'GET', body = null) {
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
    const item = {
        symbol,
        sec_type: secType,
        strategy: document.getElementById('w-strategy').value,
        exchange: document.getElementById('w-exchange').value.trim() || 'SMART',
        currency: document.getElementById('w-currency').value.trim() || 'USD',
        ma_period: parseInt(document.getElementById('w-ma-period').value) || 21,
        n_points: parseFloat(document.getElementById('w-n-points').value) || 5,
        contract_month: secType === 'FUT' ? document.getElementById('w-contract').value : null,
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
        const stratClass = w.strategy === 'BUY' ? 'BUY' : 'SELL';
        const stratLabel = w.strategy === 'BUY' ? '📈 做多' : '📉 做空';
        
        // 觸發區 + 有效性判斷
        const maRight = (w.strategy === 'BUY' && dir === 'RISING') || (w.strategy === 'SELL' && dir === 'FALLING');
        let zone = '--';
        let zoneActive = false;
        if (data.ma_value) {
            if (w.strategy === 'BUY') {
                zone = `${data.ma_value.toFixed(2)} ~ ${(data.ma_value + w.n_points).toFixed(2)}`;
                zoneActive = maRight && data.current_price >= data.ma_value && data.current_price <= data.ma_value + w.n_points;
            } else {
                zone = `${(data.ma_value - w.n_points).toFixed(2)} ~ ${data.ma_value.toFixed(2)}`;
                zoneActive = maRight && data.current_price >= data.ma_value - w.n_points && data.current_price <= data.ma_value;
            }
        }
        const zoneStatus = !data.ma_value ? '' : zoneActive ? '🟢' : maRight ? '🟡' : '⚪';

        const callOpts = data.options_call || [];
        const putOpts = data.options_put || [];
        const expanded = state.expandedWatch === w.id;

        html += `
        <div class="watch-item ${w.enabled ? '' : 'disabled'}">
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
                <span>MA${w.ma_period}</span>
                <span>N=${w.n_points}</span>
                <span>價格: ${price}</span>
                <span>MA: ${ma}</span>
                <span>距離: ${dist}</span>
            </div>
            <div class="watch-bottom-row">
                <div class="watch-ma-info">
                    <span class="ma-badge ${dirClass}">${dirLabel}</span>
                    <span class="trigger-zone ${zoneActive ? 'active' : maRight ? 'ready' : ''}" title="${maRight ? (zoneActive ? '條件滿足！' : 'MA方向正確，等待價格進入') : 'MA方向不符，暫不觸發'}">${zoneStatus} 觸發區: ${zone}</span>
                </div>
                <div style="display:flex;gap:4px;">
                    ${expanded ? `<button class="btn btn-sm" onclick="updateOptionPrices('${w.id}')" title="更新報價">🔄</button>` : ''}
                    <button class="btn btn-sm" onclick="toggleExpand('${w.id}')">
                        ${expanded ? '收起 ▲' : '選擇權 ▼'}
                    </button>
                </div>
            </div>
            ${expanded ? renderInlineOptions(w, data, callOpts, putOpts, price) : ''}
        </div>`;
    }
    container.innerHTML = html;
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

function toggleExpand(watchId) {
    if (state.expandedWatch === watchId) {
        state.expandedWatch = null;
    } else {
        state.expandedWatch = watchId;
        // Refresh current expiry prices when expanding
        if (!standaloneMode) {
            const data = state.latestData[watchId];
            const expiries = Object.keys(data?.options_call || {});
            const expiry = data?.selected_expiry || expiries[0];
            if (expiry) {
                api(`/api/options/prices/${watchId}?expiry=${expiry}`, 'POST').catch(() => {});
            }
        }
    }
    renderWatchList();
}

function renderInlineOptions(watch, data, callOptsData, putOptsData, price) {
    const expiries = Object.keys(callOptsData || {});
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
            const optKey = `${o.right}-${i}`;
            const optSel = sel[optKey] || {};
            const checked = optSel.checked ? 'checked' : '';
            const amt = optSel.amount || 1000;
            return `
            <div class="opt-inline-row">
                <input type="checkbox" id="opt-${watch.id}-${o.right}-${i}" class="opt-check" data-ask="${o.ask}" data-key="${optKey}" ${checked} onchange="saveOptSel('${watch.id}','${optKey}',this.checked)">
                <span class="opt-inline-strike">${o.strike}</span>
                <span class="opt-inline-name">${o.expiryLabel || ''} ${o.right}</span>
                <span class="opt-inline-ba">${o.bid?.toFixed(2)}/${o.ask?.toFixed(2)}</span>
                <span class="opt-inline-last" style="color:${color}">$${o.last?.toFixed(2) || '--'}</span>
                <input type="number" value="${amt}" min="100" step="100" class="opt-inline-amt" placeholder="金額" onchange="saveOptAmt('${watch.id}','${optKey}',this.value)">
            </div>`;
        }).join('');
        return `<div class="opt-inline-group">
            <div class="opt-inline-label" style="color:${color}">${label}</div>
            <div class="opt-inline-header">
                <span></span><span>履約價</span><span>到期</span><span>Bid/Ask</span><span>Last</span><span>金額$</span>
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
            <div class="exit-actions">
                <button class="btn btn-sm btn-success" onclick="placeOrder('${watch.id}')">📥 市價下單</button>
            </div>
        </div>`;

    const lockedInfo = data.locked_ma
        ? `<div style="font-size:11px;color:var(--text-muted);margin-bottom:6px;">🔒 鎖定 MA = ${data.locked_ma.toFixed(2)}</div>`
        : '';

    const strategy = watch.strategy || 'BOTH';
    const showCall = strategy === 'BUY' || strategy === 'BOTH';
    const showPut = strategy === 'SELL' || strategy === 'BOTH';

    return `<div class="opts-section">
        <div class="expiry-tabs-row">
            <span style="font-size:11px;color:var(--text-muted);margin-right:8px;">到期日:</span>
            ${expiryTabs}
        </div>
        ${lockedInfo}
        ${underlying}
        ${showCall ? renderSide(callOpts, 'Call 價外5檔（買進用）', 'var(--green)') : ''}
        ${showPut ? renderSide(putOpts, 'Put 價外5檔（賣出用）', 'var(--red)') : ''}
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

function placeOrder(watchId) {
    const w = state.watchList.find(x => x.id === watchId);
    if (!w) return;

    const sel = state.optSelections[watchId] || {};
    const ex = sel.exit || {};

    // Collect checked options from saved state
    const checkedOpts = Object.entries(sel).filter(([k, v]) => k !== 'exit' && v.checked);
    if (checkedOpts.length === 0) {
        log('請先勾選要交易的商品', 'warning');
        return;
    }

    // Collect exit strategies from saved state
    const exitStrategies = [];
    if (ex.profit) {
        exitStrategies.push(`限價止盈: 成交價${ex.profitDir || '+'}${ex.profitPts || 0.5}點`);
    }
    if (ex.time) {
        exitStrategies.push(`時間平倉: ${ex.timeVal || '15:55'}`);
    }
    if (ex.ma) {
        const cond = ex.maCond === 'below' ? '低於' : '高於';
        exitStrategies.push(`均線平倉: 標的${cond}MA${ex.maDir || '+'}${ex.maPts || 5}點`);
    }

    // Calculate quantities from amounts
    const orders = [];
    checkedOpts.forEach(([key, opt]) => {
        const amount = opt.amount || 1000;
        // Get ask from DOM as it updates
        const chk = document.querySelector(`input[data-key="${key}"]`);
        const ask = parseFloat(chk?.dataset.ask) || 1;
        const isStock = key === 'stk';
        const qty = isStock ? Math.floor(amount / ask) : Math.floor(amount / (ask * 100));
        orders.push({ key, amount, ask, qty: Math.max(qty, 1) });
    });

    // Log the order (demo mode)
    log(`📥 下單 ${w.symbol}:`, 'success');
    orders.forEach(o => {
        const label = o.key === 'stk' ? '標的' : o.key;
        log(`   ${label} | 金額$${o.amount} ÷ Ask$${o.ask} = ${o.qty}口 市價買入`, 'info');
    });
    if (exitStrategies.length) {
        log(`   平倉策略: ${exitStrategies.join(', ')}`, 'info');
    } else {
        log(`   ⚠️ 未設定平倉策略`, 'warning');
    }

    showToast(`${w.symbol} 模擬下單成功`, 'buy');
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
            // 5% chance signal — only if matches strategy direction
            const strategy = w.strategy || 'BOTH';
            const canBuy = (strategy === 'BUY' || strategy === 'BOTH') && rising;
            const canSell = (strategy === 'SELL' || strategy === 'BOTH') && !rising;
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
window.addEventListener('load', () => {
    log('Trading Monitor 已載入', 'info');

    // Register Service Worker
    if ('serviceWorker' in navigator) {
        navigator.serviceWorker.register('/static/sw.js').then(() => {
            log('PWA Service Worker 已註冊', 'info');
        }).catch(e => log('SW 註冊失敗: ' + e.message, 'warning'));
    }

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

    // Show/hide contract month for futures
    document.getElementById('w-sectype').addEventListener('change', updateContractDropdown);
    updateContractDropdown();
});

function updateContractDropdown() {
    const secType = document.getElementById('w-sectype').value;
    const group = document.getElementById('w-contract-group');
    const select = document.getElementById('w-contract');
    const exchangeInput = document.getElementById('w-exchange');
    if (secType === 'FUT') {
        group.style.display = 'block';
        exchangeInput.value = 'CME';  // 期貨自動設為 CME
        const months = getNearestContractMonths(2);
        select.innerHTML = months.map((m, i) => 
            `<option value="${m.value}">${m.label}${i === 0 ? ' (近月)' : ' (次近月)'}</option>`
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

function getNearestContractMonths(count) {
    // Futures typically have quarterly contracts: Mar(H), Jun(M), Sep(U), Dec(Z)
    const codes = ['H', 'M', 'U', 'Z']; // Mar, Jun, Sep, Dec
    const codeMonths = [3, 6, 9, 12];
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
