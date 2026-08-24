/**
 * Forward Test page controller (PRD Task 4.1).
 * Pre-fill from forward_prefill, Start/Stop, live polling of /api/forward/status.
 */
const $ = (id) => document.getElementById(id);
const POLL_MS = 1500;
let pollTimer = null;

async function fetchJSON(url, opts) {
    const r = await fetch(url, opts);
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
    return d;
}

// ---- status badge ----
function setStatus(status) {
    const badge = $("statusBadge");
    const label = { idle: "Idle", running: "Running", stopped: "Stopped" }[status] || status;
    badge.textContent = label;
    badge.className = `status-badge status-${status}`;
    const running = status === "running";
    $("startBtn").disabled = running;
    $("stopBtn").disabled = !running;
}

function setProgress(p) {
    const pct = p ? p.pct : 0;
    $("progressBar").style.width = `${pct}%`;
    $("progressText").textContent = p ? `${p.revealed} / ${p.total} bars (${pct}%)` : "";
}

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
            <td>${p.entry}</td><td>${p.current}</td>
            <td class="${cls}">${p.unrealized_pnl_pct >= 0 ? "+" : ""}${p.unrealized_pnl_pct}%</td>
            <td>${p.entry_date}</td></tr>`;
    }).join("");
}

function renderLive(data) {
    setStatus(data.status);
    setProgress(data.progress);
    renderMetricsCards("metricsCards", data.metrics);
    renderEquityChart("equityChart", data.equity);
    renderPositions(data.positions);
    TradeTable.render("tradeTable-wrap", data.trades);
}

// ---- start / stop / poll ----
function forwardConfig() {
    return {
        strategy: $("strategy").value, symbol: $("symbol").value,
        timeframe: $("timeframe").value, from_date: $("fromDate").value,
        to_date: $("toDate").value, capital: Number($("capital").value) || 0,
        params: collectParamsFrom($("params-container")),
    };
}

async function startBot() {
    if (!$("strategy").value) { showToast("Select a strategy first", "warning"); return; }
    if (!$("fromDate").value || !$("toDate").value) { showToast("Pick a date range", "warning"); return; }
    $("livePanel").hidden = false;
    setStatus("running");
    setProgress(null);
    try {
        await fetchJSON("/api/forward/start", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(forwardConfig()),
        });
        showToast("Forward test started", "success");
        poll();   // immediate first poll
        pollTimer = setInterval(poll, POLL_MS);
    } catch (err) {
        showToast(err.message || "Start failed", "error");
        setStatus("idle");
    }
}

async function stopBot() {
    try { await fetch("/api/forward/stop", { method: "POST" }); } catch { /* ignore */ }
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    await poll();   // final refresh
    setStatus("stopped");
}

async function poll() {
    try {
        const data = await fetchJSON("/api/forward/status");
        renderLive(data);
        if (data.status !== "running") {
            if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
            if (data.status === "stopped" && data.progress && data.progress.pct >= 100) {
                showToast("Replay complete", "success");
            }
        }
    } catch (err) {
        showToast(err.message || "Status failed", "error");
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    }
}

// ---- init + pre-fill (Task 4.1) ----
function applyConfig(cfg) {
    if (cfg.strategy) $("strategy").value = cfg.strategy;
    if (cfg.symbol) $("symbol").value = cfg.symbol;
    if (cfg.timeframe) $("timeframe").value = cfg.timeframe;
    if (cfg.from_date) $("fromDate").value = cfg.from_date;
    if (cfg.to_date) $("toDate").value = cfg.to_date;
    if (cfg.capital) $("capital").value = cfg.capital;
}

async function init() {
    $("startBtn").addEventListener("click", startBot);
    $("stopBtn").addEventListener("click", stopBot);

    let strategies = [];
    try {
        strategies = await fetchJSON("/api/strategies");
        $("strategy").innerHTML = strategies.map((s) => `<option value="${s.name}">${s.name}</option>`).join("");
    } catch {
        $("strategy").innerHTML = '<option value="">failed to load</option>';
        showToast("Could not load strategies", "error");
    }

    $("strategy").addEventListener("change", async () => {
        try {
            renderParamsInto($("params-container"),
                await fetchJSON(`/api/strategies/${encodeURIComponent($("strategy").value)}/params`));
        } catch (err) { showToast(err.message, "error"); }
    });

    const pre = SessionState.forwardPrefill;
    if (pre && pre.config && pre.config.strategy) {
        applyConfig(pre.config);
        try {
            renderParamsInto($("params-container"),
                await fetchJSON(`/api/strategies/${encodeURIComponent(pre.config.strategy)}/params`));
            applyOverridesInto($("params-container"), pre.config.params);
        } catch { /* ignore */ }
        SessionState.clear(SessionState.keys.forwardPrefill);
        $("prefillBanner").hidden = false;
    } else if ($("strategy").value) {
        try {
            renderParamsInto($("params-container"),
                await fetchJSON(`/api/strategies/${encodeURIComponent($("strategy").value)}/params`));
        } catch { /* ignore */ }
    }
    setStatus("idle");
}

document.addEventListener("DOMContentLoaded", init);
