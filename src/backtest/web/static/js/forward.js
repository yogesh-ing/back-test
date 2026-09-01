/**
 * Forward Test page controller — paper forward replay.
 *
 * Start → POST /api/forward/start; the server owns the replay
 * (ForwardSession + run_quick_screen, simulated fills) and a polling snapshot
 * is rendered every 2 s. This page NEVER paper-fills a live-labelled run:
 * mode=live is refused by the server (live_execution_not_wired) so a user
 * cannot silently start something the backend classifies live.
 *
 * Taxonomy (ticket #10): run mode (paper|live) and data source
 * (synthetic|replay|mstock) are first-class, selected from the canonical
 * vocabulary the server injects into the template — this file only reads
 * the controls; it re-declares no backend constants.
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
let currentBuckets = null;   // {mode, source} of the running session

function runSelection() {
    return {
        mode: $("runMode")?.value || "paper",
        source: $("dataSource")?.value || "synthetic",
    };
}

/** Risk implication surfaced per selection (ticket #9-aware, display copy). */
function updateTaxonomyHint() {
    const hint = $("taxonomyHint");
    if (!hint) return;
    const { mode, source } = runSelection();
    if (mode === "live") {
        hint.textContent = "⚠ LIVE = real fills, tight caps (10% per name, 10k max, 5 positions); " +
            "refuses synthetic/replay data, requires mStock + auth. This web replay executes " +
            "simulated fills only — the server refuses live rather than paper-filling it. " +
            "Real-fill live runs use the engine path.";
        hint.className = "warn-text";
    } else if (source === "mstock") {
        hint.textContent = "Paper + mstock = LIVE DATA, PAPER RISK (simulated fills on broker bars).";
        hint.className = "muted";
    } else {
        hint.textContent = "Paper = free-play risk caps; data source only affects data trust (paper accepts all).";
        hint.className = "muted";
    }
}

function updateStartButtonForAuth() {
    const btn = $("startBtn");
    if (!btn) return;
    const { mode } = runSelection();
    // Paper replays need no broker session; live does (and is then refused by
    // the server as not-wired — the hint explains that before the click).
    if (mode === "paper" || brokerAuthenticated) {
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
    const { mode } = runSelection();
    // Paper replay needs no broker auth.
    if (mode === "paper") {
        return startBot();
    }
    // Live needs broker auth — the server then refuses live_execution_not_wired
    // (this page cannot real-fill); the refusal toast explains, never silent.
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
    const { mode, source } = runSelection();
    const symbol = (val("symbol") || "").toUpperCase();
    const cfg = {
        strategy: val("strategy"),
        symbol,
        timeframe: val("timeframe", "1D"),
        capital: Number(val("capital")) || 100000,
        mode: mode,
        source: source,
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
        currentBuckets = { mode: result.mode || runSelection().mode,
                           source: result.source || runSelection().source };
        const bucket = `${(currentBuckets.mode || "").toUpperCase()}/` +
                       `${(currentBuckets.source || "").toUpperCase()}`;
        showToast(`Forward test started: ${result.symbol} · ${result.total} bars @ ` +
                  `${result.bars_per_second} bars/s · ${bucket}`, "success");
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
    currentBuckets = null;
    showResumeBanner(null);
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

// ---- Resume affordance (ticket #10, T7-aware) ----------------------------
// Web forward sessions are in-memory (survive a page refresh, not a process
// restart); engine state-file resume is the CLI `papertrade --state-file`
// path. The banner makes the re-attach a visible choice: Resume vs fresh.
function showResumeBanner(stateId) {
    const banner = $("resumeBanner");
    const idEl = $("resumedSessionId");
    if (!banner) return;
    if (!stateId) {
        banner.hidden = true;
        if (idEl) idEl.textContent = "—";
        return;
    }
    if (idEl) idEl.textContent = stateId.slice(0, 8);
    banner.hidden = false;
}

async function resumeRunningSession() {
    if (!currentStateId) return;
    $("livePanel").hidden = false;
    try {
        const data = await fetchJSON(
            `/api/forward/status?state_id=${encodeURIComponent(currentStateId)}`);
        renderLive(data);
        if (data.status === "running" && !pollTimer) {
            pollTimer = setInterval(poll, POLL_MS);
        }
    } catch (err) {
        showToast(err.message || "Resume failed", "error");
        rememberSession(null);
        showResumeBanner(null);
    }
}

function startFresh() {
    rememberSession(null);
    currentStateId = null;
    currentBuckets = null;
    showResumeBanner(null);
}

async function init() {
    on("startBtn", "click", handleStartClick);
    on("stopBtn", "click", stopBot);
    on("runMode", "change", () => { updateStartButtonForAuth(); updateTaxonomyHint(); });
    on("dataSource", "change", updateTaxonomyHint);
    on("resumeBtn", "click", resumeRunningSession);
    on("freshStartBtn", "click", startFresh);
    document.addEventListener("broker:status", onBrokerStatusUpdate);
    updateStartButtonForAuth();
    updateTaxonomyHint();

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
            currentBuckets = {
                mode: (status.config && status.config.mode) || runSelection().mode,
                source: (status.config && status.config.source) || runSelection().source,
            };
            $("livePanel").hidden = false;
            setStatus("running");
            renderLive(status);
            showResumeBanner(status.state_id);   // visible resume affordance
            if (!pollTimer) pollTimer = setInterval(poll, POLL_MS);
        } else if (stored) {
            rememberSession(null);
            showResumeBanner(null);
        }
    } catch { /* ignore */ }

    setStatus("idle");
}

document.addEventListener("DOMContentLoaded", init);
