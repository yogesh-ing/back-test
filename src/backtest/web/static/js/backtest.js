/**
 * Backtest page controller (PRD Tasks 2.3, 2.9, 2.10).
 * Orchestrates: strategy dropdown + dynamic params, run, render results,
 * chart tabs, Save to Compare, Export CSV, Promote to Forward, pre-fill.
 */
let lastRun = null;          // {config, result}
let currentParams = {};      // schema for the selected strategy

const $ = (id) => document.getElementById(id);

/** Server errors carry a request_id that also appears in the app log — quoting it
 *  here means a screenshot of a toast is enough to find the traceback. */
function _apiError(data, status) {
    const id = data && data.request_id ? ` [req ${data.request_id}]` : "";
    return `${(data && data.error) || `HTTP ${status}`}${id}`;
}

async function fetchJSON(url, opts) {
    const r = await fetch(url, opts);
    const data = await r.json();
    if (!r.ok) throw new Error(_apiError(data, r.status));
    return data;
}

// ---------------------------------------------------------------------------
// Dynamic params (Task 2.3)
// ---------------------------------------------------------------------------

function renderParams(params) {
    currentParams = params || {};
    renderParamsInto($("params-container"), currentParams);
}

function collectParams() {
    return collectParamsFrom($("params-container"));
}

function applyParamOverrides(overrides) {
    applyOverridesInto($("params-container"), overrides);
}

function collectConfig() {
    return {
        strategy: $("strategy").value,
        symbol: $("symbol").value,
        timeframe: $("timeframe").value,
        from_date: $("fromDate").value,
        to_date: $("toDate").value,
        capital: Number($("capital").value) || 0,
        params: collectParams(),
    };
}

// ---------------------------------------------------------------------------
// Run + render
// ---------------------------------------------------------------------------

async function runBacktest() {
    if (!$("strategy").value) { showToast("Select a strategy first", "warning"); return; }
    if (!$("fromDate").value || !$("toDate").value) { showToast("Pick a date range", "warning"); return; }

    const config = collectConfig();
    $("emptyState").hidden = true;
    $("results").hidden = false;
    showLoader("metricsCards", "Running backtest…");
    $("tradeTable-wrap").querySelector("tbody").innerHTML = "";
    $("pagination").innerHTML = "";

    try {
        const result = await fetchJSON("/api/backtest/run", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(config),
        });
        lastRun = { config, result };
        renderResults(result);
        showToast("Backtest complete", "success");
    } catch (err) {
        showToast(err.message || "Backtest failed", "error");
        $("results").hidden = true;
        $("emptyState").hidden = false;
    }
}

function renderResults(result) {
    renderMetricsCards("metricsCards", result.metrics);
    TradeTable.render("tradeTable-wrap", result.trades);
    // default tab = equity; render lazily on tab switch
    renderChartForPane("equity");
}

function renderChartForPane(pane) {
    if (!lastRun) return;
    if (pane === "equity") renderEquityChart("equityChart", lastRun.result.equity);
    else if (pane === "drawdown") renderDrawdownChart("drawdownChart", lastRun.result.drawdown);
    else if (pane === "signals") renderSignalsChart("signalsChart", lastRun.result.signals);
}

// ---------------------------------------------------------------------------
// Actions (Tasks 2.9, 2.10, Promote)
// ---------------------------------------------------------------------------

function saveToCompare() {
    if (!lastRun) { showToast("Run a backtest first", "warning"); return; }
    const res = SessionState.addCompareSlot(lastRun);
    if (!res.ok) { showToast("Compare is full (4/4)", "warning"); return; }
    showToast(`Saved to Compare — slot ${res.index}/${SessionState.maxCompareSlots}`, "success");
}

function exportCsv() {
    if (!lastRun) { showToast("Run a backtest first", "warning"); return; }
    const trades = lastRun.result.trades;
    const header = ["id", "date", "exit_date", "side", "entry", "exit", "pnl", "result"];
    const lines = [header.join(",")];
    trades.forEach((t) => {
        lines.push([t.id, t.date, t.exit_date || "", t.side, t.entry, t.exit, t.pnl, t.result]
            .map(csvCell).join(","));
    });
    downloadFile("backtest_trades.csv", lines.join("\n"), "text/csv");
    showToast(`Exported ${trades.length} trades`, "success");
}

function csvCell(v) {
    const s = String(v ?? "");
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function downloadFile(name, content, type) {
    const blob = new Blob([content], { type });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = name; a.click();
    URL.revokeObjectURL(url);
}

function promoteToForward() {
    if (!lastRun) { showToast("Run a backtest first", "warning"); return; }
    SessionState.forwardPrefill = { config: lastRun.config };
    showToast("Pre-filled Forward Test — redirecting…", "success");
    setTimeout(() => { window.location.href = "/forward"; }, 500);
}

// ---------------------------------------------------------------------------
// Tabs + pre-fill
// ---------------------------------------------------------------------------

function wireTabs() {
    document.querySelectorAll(".tab").forEach((tab) => {
        tab.addEventListener("click", () => {
            document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
            document.querySelectorAll(".tab-pane").forEach((p) => p.classList.remove("active"));
            tab.classList.add("active");
            const pane = tab.dataset.tab;
            document.querySelector(`.tab-pane[data-pane="${pane}"]`).classList.add("active");
            renderChartForPane(pane);
        });
    });
}

function showBanner(text) {
    const main = document.querySelector(".container");
    const b = document.createElement("div");
    b.className = "banner";
    b.textContent = text;
    main.prepend(b);
}

async function init() {
    wireTabs();
    $("runBtn").addEventListener("click", runBacktest);
    $("saveCompareBtn").addEventListener("click", saveToCompare);
    $("exportCsvBtn").addEventListener("click", exportCsv);
    $("promoteBtn").addEventListener("click", promoteToForward);

    // load strategies
    let strategies = [];
    try {
        strategies = await fetchJSON("/api/strategies");
        $("strategy").innerHTML = strategies
            .map((s) => `<option value="${s.name}">${s.name}</option>`).join("");
    } catch (err) {
        $("strategy").innerHTML = `<option value="">failed to load</option>`;
        showToast("Could not load strategies", "error");
    }

    // strategy change → dynamic params
    $("strategy").addEventListener("change", async () => {
        try {
            renderParams(await fetchJSON(`/api/strategies/${encodeURIComponent($("strategy").value)}/params`));
        } catch (err) { showToast(err.message, "error"); }
    });

    // pre-fill from Compare's "Open in Backtest" (Task 3.8 hand-off)
    const pre = SessionState.backtestPrefill;
    if (pre && pre.config && pre.config.strategy) {
        const cfg = pre.config;
        $("strategy").value = cfg.strategy;
        if (cfg.symbol) $("symbol").value = cfg.symbol;
        if (cfg.timeframe) $("timeframe").value = cfg.timeframe;
        if (cfg.from_date) $("fromDate").value = cfg.from_date;
        if (cfg.to_date) $("toDate").value = cfg.to_date;
        if (cfg.capital) $("capital").value = cfg.capital;
        try {
            renderParams(await fetchJSON(`/api/strategies/${encodeURIComponent(cfg.strategy)}/params`));
            applyParamOverrides(cfg.params);
        } catch { /* ignore param load errors */ }
        SessionState.clear(SessionState.keys.backtestPrefill);
        showBanner("Pre-filled from a saved comparison slot");
    } else if ($("strategy").value) {
        // default: render params for the first strategy
        try {
            renderParams(await fetchJSON(`/api/strategies/${encodeURIComponent($("strategy").value)}/params`));
        } catch { /* ignore */ }
    }
}

document.addEventListener("DOMContentLoaded", init);
