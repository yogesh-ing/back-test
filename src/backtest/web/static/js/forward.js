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

/** Sessions are keyed server-side, so a refresh has to re-attach to *this*
 *  browser's replay rather than whatever started most recently. */
const SESSION_KEY = "forward_***";

function rememberSession(stateId) {
    currentStateId = stateId || null;
    try {
        if (stateId) sessionStorage.setItem(SESSION_KEY, stateId);
        else sessionStorage.removeItem(SESSION_KEY);
    } catch { /* private mode: memory only */ }
}

function rememberedSession() {
    try { return sessionStorage.getItem(SESSION_KEY); } catch { return null; }
}

// ---- Auth gate -----------------------------------------------------------
let brokerAuthenticated = false;

function updateStartButtonForAuth() {
    const btn = $("startBtn");
    if (!btn) return;
    const mode = $("dataMode")?.value;
    if (mode === "synthetic" || brokerAuthenticated) {
        btn.disabled = false;
        btn.textContent = "▶ Start";
        btn.title = "";
        btn.classList.remove("btn-disabled-auth");
    } else {
        btn.disabled = true;
        btn.textContent = "🔴 Connect mStock to Start";
        btn.title = "Authentication required for live mode";
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
    const mode = dataMode();
    // Synthetic mode doesn't need broker auth
    if (mode === "synthetic") {
        return startBot();
    }
    // Live mode needs broker auth
    if (!brokerAuthenticated) {
        e.preventDefault();
        e.stopPropagation();
        const authUI = (typeof window !== "undefined" && window.BrokerAuthUI) ||
                       (typeof BrokerAuthUI !== "undefined" ? BrokerAuthUI : null);
        if (authUI && typeof authUI.open === "function") {
            authUI.open();
        } else {
            showToast("Broker authentication required for live mode", "warning");
        }
        return false;
    }
    return startBot();
}

async function fetchJSON(url, opts) {
    const r = await fetch(url, opts);
    const d = await r.json();
    // `d.request_id` is also in the server log, so a toast can be traced exactly.
    if (!r.ok) {
        const id = d && d.request_id ? ` [req ${d.request_id}]` : "";
        throw new Error(`${(d && d.error) || `HTTP ${r.status}`}${id}`);
    }
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
    $("equityDisplay").textContent = data.equity != null
        ? Money.format(data.equity, 0) : Money.format(0, 0);

    // Unrealized PnL
    const pnlEl = $("unrealizedPnl");
    if (data.unrealized_pnl && data.unrealized_pnl !== 0) {
        pnlEl.textContent = ` (${Money.signed(data.unrealized_pnl)})`;
        pnlEl.className = data.unrealized_pnl >= 0 ? "pos" : "neg";
    } else {
        pnlEl.textContent = "";
    }
}

// ---- Positions table -----------------------------------------------------
function renderPositions(positions) {
    const body = $("positionsBody");
    if (!body) return;
    if (!positions || !positions.length) {
        body.innerHTML = '<tr><td colspan="7" class="muted">No open positions</td></tr>';
        return;
    }
    // Entry and current are different prices now, and the P&L is the open trade's
    // equity delta — this table used to be structurally unable to move (gap G3).
    body.innerHTML = positions.map((p) => {
        const cls = (p.unrealized_pnl || 0) >= 0 ? "pos" : "neg";
        const pct = p.unrealized_pnl_pct || 0;
        const exposure = p.exposure_pct != null ? p.exposure_pct : 100;
        return `<tr>
            <td>${p.symbol}</td>
            <td>${p.side} <span class="muted small">(${exposure}%)</span></td>
            <td>${Money.format(p.entry)}</td>
            <td>${Money.format(p.current)}</td>
            <td class="${cls}">${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%</td>
            <td class="${cls}">${Money.signed(p.unrealized_pnl || 0)}</td>
            <td>${p.entry_date || "-"}${p.bars_held ? ` <span class="muted small">${p.bars_held}b</span>` : ""}</td>
        </tr>`;
    }).join("");
}

// ---- Trade feed ----------------------------------------------------------
function renderTrades(trades) {
    // TradeTable is container-scoped and already renders ✅ Win / ❌ Loss /
    // ⏳ Open with pagination, so the feed reuses it instead of duplicating rows.
    if (typeof TradeTable !== "undefined") TradeTable.render("tradeTable-wrap", trades || []);
}

// ---- Equity chart --------------------------------------------------------
function renderLiveEquity(equity) {
    // /status already returns the adapter shape {dates, values, benchmark}. The
    // old code fetched /api/forward/equity and handed renderEquityChart a bare
    // number array, which it rejects — so the "live" chart never drew a point.
    if (!equity || !equity.dates || !equity.dates.length) return;
    renderEquityChart("equityChart", equity);
}

// ---- Progress ------------------------------------------------------------
function renderProgress(progress) {
    const p = progress || {};
    const el = $("progressText");
    if (el) el.textContent = p.total ? `${p.revealed} / ${p.total} bars · ${p.pct}%` : "";
    const fill = $("progressFill");
    if (fill) fill.style.width = `${Math.max(0, Math.min(100, p.pct || 0))}%`;
}

// ---- Render all ----------------------------------------------------------
function renderLive(data) {
    setStatus(data.status);
    updateLiveBanner(data);
    renderProgress(data.progress);
    // The live panels PRD 4.2 asks for: running metrics, equity curve, positions
    // and the trade feed — all off this one snapshot.
    if (data.metrics) renderMetricsCards("metricsCards", data.metrics);
    renderLiveEquity(data.equity);
    renderPositions(data.positions);
    if (data.trades) renderTrades(data.trades);
}

// ---- Start / Stop / Poll -------------------------------------------------
function val(id, fallback = "") {
    const el = $(id);
    return el ? el.value : fallback;
}

function forwardConfig() {
    const mode = dataMode();
    const symbol = (val("symbol") || "").toUpperCase();
    const cfg = {
        strategy: val("strategy"),
        symbol,
        timeframe: val("timeframe", "1D"),
        capital: Number(val("capital")) || 100000,
        mode: mode,
        from_date: val("fromDate") || "2024-01-01",
        to_date: val("toDate") || new Date().toISOString().slice(0, 10),
        params: collectParamsFrom($("params-container")),
    };
    // Bars/second for the server clock. Only sent when the picker is set, so the
    // server default (--replay-speed) otherwise applies.
    const speed = val("replaySpeed");
    if (speed !== "") cfg.bars_per_second = Number(speed);
    return cfg;
}

async function startBot() {
    if (!$("strategy").value) { showToast("Select a strategy first", "warning"); return; }
    if (!$("symbol").value.trim()) { showToast("Enter a symbol (e.g. MAZDOCK)", "warning"); return; }

    $("livePanel").hidden = false;
    setStatus("running");

    try {
        const result = await fetchJSON("/api/forward/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(forwardConfig()),
        });
        rememberSession(result.state_id);
        showToast(`Forward test started: ${result.symbol} · ${result.total} bars @ ` +
                  `${result.bars_per_second} bars/s`, "success");
        // A window the server had to invent is not a window we chose — say so
        // loudly instead of showing a replay that looks like the user's range.
        if (result.defaults_applied && result.defaults_applied.length && result.config) {
            showToast(
                `Date range defaulted by the server (${result.defaults_applied.join(", ")}): ` +
                `${result.config.from_date} → ${result.config.to_date}`, "warning", 6000,
            );
        }
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
    rememberSession(null);
    await poll();
    setStatus("stopped");
    showToast("Forward test stopped", "warning");
}

async function poll() {
    const qs = currentStateId ? `?state_id=${encodeURIComponent(currentStateId)}` : "";
    try {
        const data = await fetchJSON(`/api/forward/status${qs}`);
        renderLive(data);
        if (data.status !== "running") {
            if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        }
    } catch (err) {
        // A 404 means our session is gone (the server restarted) — forget the id
        // rather than polling something that can never answer.
        if (/404/.test(err.message || "")) rememberSession(null);
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
// Bind a handler only when the element exists (guards partial/test DOMs).
function on(id, event, handler) {
    const el = $(id);
    if (el) el.addEventListener(event, handler);
}

// Data-source mode ("synthetic" | "live"); defaults to live when the control
// is absent so the auth gate still applies in a minimal/partial DOM.
function dataMode() {
    return $("dataMode")?.value || "live";
}

async function init() {
    on("startBtn", "click", handleStartClick);
    on("stopBtn", "click", stopBot);
    on("dataMode", "change", updateStartButtonForAuth);
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
        const banner = $("prefillBanner");
        if (banner) banner.hidden = false;
    } else if ($("strategy").value) {
        try {
            renderParamsInto($("params-container"),
                await fetchJSON(`/api/strategies/${encodeURIComponent($("strategy").value)}/params`));
        } catch { /* ignore */ }
    }

    // Re-attach to this browser's session after a refresh, else to whatever runs.
    const stored = rememberedSession();
    if (stored) rememberSession(stored);
    try {
        const qs = currentStateId ? `?state_id=${encodeURIComponent(currentStateId)}` : "";
        const status = await fetchJSON(`/api/forward/status${qs}`);
        if (status.status === "running") {
            if (status.state_id) rememberSession(status.state_id);
            $("livePanel").hidden = false;
            setStatus("running");
            renderLive(status);
            if (!pollTimer) pollTimer = setInterval(poll, POLL_MS);
        } else if (stored) {
            rememberSession(null);
        }
    } catch { /* ignore */ }

    setStatus("idle");
}

document.addEventListener("DOMContentLoaded", init);
