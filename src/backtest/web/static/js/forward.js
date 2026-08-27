/**
 * Forward Test page controller — live paper trading.
 *
 * Start → launches LiveForwardEngine (background thread).
 * Status polled every 2 seconds. Equity chart updates in real-time.
 * Trade feed shows executed paper trades with PnL.
 */
const $ = (id) => document.getElementById(id);
const POLL_MS = 2000;
let pollTimer = null;
let currentStateId = null;

// ---- Auth gate -----------------------------------------------------------
let brokerAuthenticated = false;

function updateStartButtonForAuth() {
    const btn = $("startBtn");
    if (!btn) return;
    if (brokerAuthenticated) {
        btn.disabled = false;
        btn.textContent = "▶ Start";
        btn.title = "";
        btn.classList.remove("btn-disabled-auth");
    } else {
        btn.disabled = true;
        btn.textContent = "🔴 Connect mStock to Start";
        btn.title = "Authentication required before starting forward test";
        btn.classList.add("btn-disabled-auth");
    }
}

function onBrokerStatusUpdate(event) {
    const payload = event.detail;
    const state = payload && payload.status;
    brokerAuthenticated = (state === "authenticated" || state === "expiring_soon");
    updateStartButtonForAuth();
}

function handleStartClick(e) {
    if (!brokerAuthenticated) {
        e.preventDefault();
        e.stopPropagation();
        if (window.BrokerAuthUI && typeof window.BrokerAuthUI.open === "function") {
            window.BrokerAuthUI.open();
        } else {
            showToast("Broker authentication required", "warning");
        }
        return false;
    }
    return startBot();
}

async function fetchJSON(url, opts) {
    const r = await fetch(url, opts);
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
    return d;
}

// ---- Status badge --------------------------------------------------------
function setStatus(status) {
    const badge = $("statusBadge");
    const label = { idle: "Idle", running: "Running", stopped: "Stopped", error: "Error" }[status] || status;
    badge.textContent = label;
    badge.className = `status-badge status-${status}`;
    const running = status === "running";
    $("startBtn").disabled = running || !brokerAuthenticated;
    if (running) {
        $("startBtn").textContent = "▶ Start";
        $("startBtn").classList.remove("btn-disabled-auth");
    } else {
        updateStartButtonForAuth();
    }
    $("stopBtn").disabled = !running;
}

// ---- Live status banner --------------------------------------------------
function updateLiveBanner(data) {
    const banner = $("liveStatusBanner");
    if (data.status === "idle" || data.status === "stopped") {
        banner.style.display = "none";
        return;
    }
    banner.style.display = "block";

    // Market indicator
    const dot = $("marketDot");
    const label = $("marketLabel");
    if (data.market_open) {
        dot.style.background = "#4caf50";
        label.textContent = "Market Open";
    } else {
        dot.style.background = "#f44336";
        label.textContent = "Market Closed";
    }

    // Last bar
    if (data.last_bar_ts) {
        const dt = new Date(data.last_bar_ts);
        $("lastBarInfo").textContent = `Last bar: ${dt.toLocaleString("en-IN")}`;
    }

    // Stats
    $("barsProcessed").textContent = `Bars: ${data.total_bars || 0}`;
    $("tradesCount").textContent = `Trades: ${data.total_trades || 0}`;
    $("equityDisplay").textContent = `₹${(data.equity || 0).toLocaleString("en-IN", { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`;

    // Unrealized PnL
    const pnlEl = $("unrealizedPnl");
    if (data.unrealized_pnl && data.unrealized_pnl !== 0) {
        const cls = data.unrealized_pnl >= 0 ? "pos" : "neg";
        pnlEl.textContent = ` (${data.unrealized_pnl >= 0 ? "+" : ""}₹${data.unrealized_pnl.toFixed(2)})`;
        pnlEl.className = cls;
    } else {
        pnlEl.textContent = "";
    }
}

// ---- Positions table -----------------------------------------------------
function renderPositions(positions) {
    const body = $("positionsBody");
    if (!positions || !positions.length) {
        body.innerHTML = '<tr><td colspan="6" class="muted">No open positions</td></tr>';
        return;
    }
    body.innerHTML = positions.map((p) => {
        const cls = p.unrealized_pnl_pct >= 0 ? "pos" : "neg";
        return `<tr>
            <td>${p.symbol}</td><td>${p.side}</td>
            <td>₹${p.entry}</td><td>₹${p.current || "—"}</td>
            <td class="${cls}">${p.unrealized_pnl_pct >= 0 ? "+" : ""}${p.unrealized_pnl_pct || 0}%</td>
            <td>${p.entry_date || "—"}</td></tr>`;
    }).join("");
}

// ---- Trade feed ----------------------------------------------------------
function renderTrades(trades) {
    if (!trades || !trades.length) return;
    const tbody = $("tradeTable")?.querySelector("tbody");
    if (!tbody) return;
    tbody.innerHTML = trades.map((t, i) => {
        const pnlCls = (t.pnl || 0) >= 0 ? "pos" : "neg";
        const result = t.status === "open" ? "Open" : ((t.pnl || 0) >= 0 ? "Win" : "Loss");
        return `<tr>
            <td>${i + 1}</td>
            <td>${t.entry_date || "—"}</td>
            <td>${t.side}</td>
            <td>₹${t.entry}</td>
            <td>${t.exit ? "₹" + t.exit : "—"}</td>
            <td class="${pnlCls}">${t.pnl != null ? "₹" + t.pnl.toFixed(2) : "—"}</td>
            <td class="${pnlCls}">${result}</td></tr>`;
    }).join("");
}

// ---- Equity chart --------------------------------------------------------
let equityData = [];

async function fetchEquity() {
    try {
        const data = await fetchJSON("/api/forward/equity");
        if (data && data.length > 0) {
            equityData = data;
            renderEquityChart("equityChart", data.map(d => d.equity));
        }
    } catch { /* ignore */ }
}

// ---- Render all ----------------------------------------------------------
function renderLive(data) {
    setStatus(data.status);
    updateLiveBanner(data);
    renderPositions(data.positions);
    if (data.trades) renderTrades(data.trades);
}

// ---- Start / Stop / Poll -------------------------------------------------
function forwardConfig() {
    return {
        strategy: $("strategy").value,
        symbol: $("symbol").value.toUpperCase(),
        timeframe: $("timeframe").value,
        capital: Number($("capital").value) || 100000,
        params: collectParamsFrom($("params-container")),
    };
}

async function startBot() {
    if (!$("strategy").value) { showToast("Select a strategy first", "warning"); return; }
    if (!$("symbol").value) { showToast("Enter a symbol", "warning"); return; }

    $("livePanel").hidden = false;
    setStatus("running");

    try {
        const result = await fetchJSON("/api/forward/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(forwardConfig()),
        });
        currentStateId = result.state_id;
        showToast(`Forward test started: ${result.symbol}`, "success");
        poll();
        pollTimer = setInterval(poll, POLL_MS);
    } catch (err) {
        showToast(err.message || "Start failed", "error");
        setStatus("idle");
    }
}

async function stopBot() {
    try {
        await fetch("/api/forward/stop", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ state_id: currentStateId }),
        });
    } catch { /* ignore */ }
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    await poll();
    setStatus("stopped");
    showToast("Forward test stopped", "info");
}

async function poll() {
    try {
        const data = await fetchJSON("/api/forward/status");
        renderLive(data);
        await fetchEquity();
        if (data.status !== "running") {
            if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        }
    } catch (err) {
        showToast(err.message || "Status failed", "error");
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    }
}

// ---- Symbol autocomplete from DB -----------------------------------------
async function loadSymbols() {
    try {
        const resp = await fetchJSON("/api/symbols");
        const symbols = resp.symbols || resp;
        const list = $("symbolList");
        if (list && Array.isArray(symbols)) {
            list.innerHTML = symbols.map(s => `<option value="${s}">`).join("");
        }
    } catch { /* ignore */ }
}

// ---- Init ----------------------------------------------------------------
async function init() {
    $("startBtn").addEventListener("click", handleStartClick);
    $("stopBtn").addEventListener("click", stopBot);
    document.addEventListener("broker:status", onBrokerStatusUpdate);
    updateStartButtonForAuth();

    // Load strategies
    let strategies = [];
    try {
        strategies = await fetchJSON("/api/strategies");
        $("strategy").innerHTML = strategies.map((s) => `<option value="${s.name}">${s.name}</option>`).join("");
    } catch {
        $("strategy").innerHTML = '<option value="">failed to load</option>';
    }

    $("strategy").addEventListener("change", async () => {
        try {
            renderParamsInto($("params-container"),
                await fetchJSON(`/api/strategies/${encodeURIComponent($("strategy").value)}/params`));
        } catch (err) { showToast(err.message, "error"); }
    });

    // Load symbol autocomplete + add input filtering
    await loadSymbols();
    const symbolInput = $("symbol");
    symbolInput.addEventListener("input", () => {
        const val = symbolInput.value.toUpperCase();
        const list = $("symbolList");
        if (!list) return;
        const options = list.querySelectorAll("option");
        let shown = 0;
        options.forEach(opt => {
            const match = opt.value.toUpperCase().includes(val);
            opt.style.display = match ? "" : "none";
            if (match && shown < 20) shown++;
        });
    });

    // Pre-fill from backtest
    const pre = SessionState.forwardPrefill;
    if (pre && pre.config && pre.config.strategy) {
        $("strategy").value = pre.config.strategy;
        if (pre.config.symbol) $("symbol").value = pre.config.symbol;
        if (pre.config.timeframe) $("timeframe").value = pre.config.timeframe;
        if (pre.config.capital) $("capital").value = pre.config.capital;
        try {
            renderParamsInto($("params-container"),
                await fetchJSON(`/api/strategies/${encodeURIComponent(pre.config.strategy)}/params`));
            if (pre.config.params) applyOverridesInto($("params-container"), pre.config.params);
        } catch { /* ignore */ }
        SessionState.clear(SessionState.keys.forwardPrefill);
        $("prefillBanner").hidden = false;
    } else if ($("strategy").value) {
        try {
            renderParamsInto($("params-container"),
                await fetchJSON(`/api/strategies/${encodeURIComponent($("strategy").value)}/params`));
        } catch { /* ignore */ }
    }

    // Check if engine is already running
    try {
        const status = await fetchJSON("/api/forward/status");
        if (status.status === "running") {
            currentStateId = status.state_id;
            $("livePanel").hidden = false;
            setStatus("running");
            renderLive(status);
            pollTimer = setInterval(poll, POLL_MS);
        }
    } catch { /* ignore */ }

    setStatus("idle");
}

document.addEventListener("DOMContentLoaded", init);
