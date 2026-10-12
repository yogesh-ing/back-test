/**
 * Backtest page controller (PRD Tasks 2.3, 2.9, 2.10).
 * Orchestrates: strategy dropdown + dynamic params, run, render results,
 * chart tabs, Save to Compare, Export CSV, Promote to Forward, pre-fill.
 */
let lastRun = null;          // {config, result, resultId, stored?}
let currentParams = {};      // schema for the selected strategy
let symbolPicker = null;     // components/symbol_picker.js handle
let recentRuns = [];         // flat ledger rows from GET /api/backtest/runs

// --- Recent-run history (server ledger) --------------------------------------
// The run-persistence PRD R4 retires the localStorage cache: the server's
// ledger IS the history, so tiles list R1 flat metrics and opening a run
// reads the stored payload back (GET /api/backtest/runs/<id>). Nothing is
// re-run by clicking a tile, and nothing is cached in the browser.
const RECENT_RUNS_LIMIT = 12;

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (m) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m]
));

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

/** Show (or clear, with "") the sticky run-failure banner. */
function showRunError(message) {
    const el = $("runError");
    if (!el) return;
    el.textContent = message || "";
    el.hidden = !message;
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
        timeframe: timeframeValue(),
        from_date: $("fromDate").value,
        to_date: $("toDate").value,
        capital: Number($("capital").value) || 0,
        params: collectParams(),
        // Empty = canonical fill-exact engine. "quick_screen" is only ever an
        // explicit opt-in (the Fast Preview toggle) — never the default, so a
        // result can never silently come from the approximate path.
        mode: engineMode(),
    };
}

/**
 * The timeframe to send. §1.4: the dropdown only ever offers what the
 * selected symbol actually has cached, and never claims an intraday
 * granularity on a source that stores daily bars only.
 */
function timeframeValue() {
    const sel = $("timeframe");
    if (!sel || !sel.value) return "1day";
    const canonical = Timeframes.toCanonical(sel.value);
    const available = symbolPicker ? symbolPicker.timeframesFor($("symbol").value) : [];
    if (available.length && !available.includes(canonical)) {
        showToast(`${sel.value} has no data for ${$("symbol").value} — using ${available[0]}`, "warning");
        return available[0];
    }
    return canonical;
}

/** Requested engine mode: "" (full engine) or "quick_screen" (fast preview). */
function engineMode() {
    const box = $("fastPreview");
    return box && box.checked ? "quick_screen" : "";
}

// ---------------------------------------------------------------------------
// Run + render
// ---------------------------------------------------------------------------

async function runBacktest() {
    if (!$("strategy").value) { showToast("Select a strategy first", "warning"); return; }
    if (!$("symbol").value) { showToast("Select an instrument first", "warning"); $("symbol").focus(); return; }
    if (!$("fromDate").value || !$("toDate").value) { showToast("Pick a date range", "warning"); return; }

    const runButton = $("runBtn");
    if (runButton.disabled) return;
    const buttonMarkup = runButton.innerHTML;
    runButton.disabled = true;
    runButton.textContent = "Running backtest…";
    runButton.setAttribute("aria-busy", "true");
    const config = collectConfig();
    $("results").setAttribute("aria-busy", "true");
    if ($("backtestOutput")) $("backtestOutput").hidden = true;
    if ($("resultProvenance")) $("resultProvenance").innerHTML = "";
    $("emptyState").hidden = true;
    $("results").hidden = false;
    showRunError("");   // last failure must not outlive the run that follows it
    showLoader("metricsCards", "Running backtest…");
    $("tradeTable-wrap").querySelector("tbody").innerHTML = "";
    $("pagination").innerHTML = "";

    try {
        const result = await fetchJSON("/api/backtest/run", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(config),
        });
        // §6: a handle for this rendered result, so the Optimize hand-off and
        // its audit chain have something to name. The ledger row (if the
        // write landed) is identified by result.run_id — the browser keeps
        // no cache of its own.
        lastRun = { config, result, resultId: TuneThis.mintResultId() };
        renderResults(result);
        loadRecentRuns();
        showToast("Backtest complete", "success");
    } catch (err) {
        const message = err.message || "Backtest failed";
        showToast(message, "error");
        // ...and again, persistently. A data error names which dates the
        // instrument DOES hold, which is the whole instruction the user needs;
        // a 3-second toast is gone before it can be read, let alone typed.
        showRunError(message);
        $("results").hidden = true;
        $("emptyState").hidden = false;
    } finally {
        runButton.disabled = runButton.dataset.dsgPinned === "1";
        runButton.innerHTML = buttonMarkup;
        runButton.removeAttribute("aria-busy");
        $("results").removeAttribute("aria-busy");
    }
}

function renderResults(result) {
    if ($("backtestOutput")) $("backtestOutput").hidden = false;
    const stored = lastRun && lastRun.stored;
    const caption = $("backtest-result-caption");
    if (caption) {
        caption.textContent = stored
            ? `stored run ${stored.created_at || "?"}, source ${stored.data_source || "unknown"}`
            : (lastRun ? `${lastRun.config.symbol} · ${lastRun.config.from_date} — ${lastRun.config.to_date}` : "Historical simulation");
    }
    // PRD R3/R4: a run whose ledger write failed is NOT in the history list,
    // so the result page must say so persistently — a toast would be gone by
    // the time anyone acts on the number.
    const badge = $("persistBadge");
    if (badge) {
        const lost = result && result.persisted === false;
        badge.hidden = !lost;
        if (lost) {
            badge.innerHTML = `<span class="badge bg-light-danger">⚠ NOT STORED</span> `
                + `<span class="muted small">This run is not in the server history`
                + (result.persist_error ? ` (${esc(result.persist_error)})` : "")
                + ` — re-run it if the result matters.</span>`;
        }
    }
    // A stored run whose heavy series is gone renders its flat metrics honestly
    // instead of drawing an empty chart (PRD R4 series_status handling).
    const seriesStatus = stored ? stored.series_status : "present";
    const degraded = seriesStatus !== "present";
    const notice = $("seriesNotice");
    if (notice) {
        notice.hidden = !degraded;
        notice.innerHTML = seriesStatus === "evicted"
            ? `<div class="card"><span class="badge badge-subtle">📉 Chart data expired</span> `
              + `<span class="muted small">Series evicted by retention policy. The metrics above are `
              + `the stored ledger row.</span></div>`
            : `<div class="card"><span class="badge bg-light-danger">⛔ Storage error</span> `
              + `<span class="muted small">The chart series failed to persist. Re-run recommended.</span></div>`;
    }
    if ($("chartCard")) $("chartCard").hidden = degraded;
    if ($("tradeLedgerCard")) $("tradeLedgerCard").hidden = degraded;
    if ($("runChecks")) $("runChecks").hidden = degraded;
    // Engine + data provenance first: every number below it is read through
    // these two badges (PRD backTest-enhance §1.1/§1.2).
    if (typeof Provenance !== "undefined") Provenance.renderInto("resultProvenance", result.provenance);
    renderMetricsCards("metricsCards", result.metrics);
    // PRD §2.2: risk/tail, drawdown detail, trade quality and statistical
    // confidence, plus the insufficient-sample banner when it applies.
    if (typeof MetricSections !== "undefined") MetricSections.renderInto("metricSections", result.metrics);
    // PRD §3.1/§3.2/§3.3: buy-and-hold reference, slippage stress and trade
    // sequence resampling. Read from the same payload as the cards above, so a
    // check can never qualify a different result than the one being shown.
    if (!degraded && typeof RunChecks !== "undefined") RunChecks.renderInto("runChecks", result);
    // PRD §5: the readiness summary, from the same payload as everything above.
    if (typeof Certification !== "undefined") Certification.renderInto("certification", result.readiness);
    // PRD §6: carry this result into Optimize. Never hidden — see the note in
    // tune_this.js about why a not-yet-certifiable result is precisely when
    // someone wants to tune.
    if (typeof TuneThis !== "undefined") TuneThis.renderInto("tuneThis", lastRun);
    if (!degraded && Array.isArray(result.trades)) TradeTable.render("tradeTable-wrap", result.trades);
    // default tab = equity; render lazily on tab switch
    renderChartForPane("equity");
}

const CHART_CANVASES = { equity: "equityChart", drawdown: "drawdownChart", signals: "signalsChart" };

function renderChartForPane(pane) {
    if (!lastRun) return;
    const canvasId = CHART_CANVASES[pane];
    if (!canvasId) return;
    const el = $(canvasId);
    if (!el) return;
    const data = lastRun.result?.[pane];
    // The adapter sends equity/drawdown as {dates, values} and signals as
    // {candles, buys, sells}; the axis field is what counts as "no points".
    // Testing the wrapper for an Array hid all three panes for every run.
    const axis = pane === "signals" ? data?.candles : data?.dates;
    if (!Array.isArray(axis) || !axis.length) {
        // A run without this series must not leave the previous run's curve up.
        window.Chart?.getChart?.(el)?.destroy();
        return;
    }
    if (pane === "equity") renderEquityChart(canvasId, data);
    else if (pane === "drawdown") renderDrawdownChart(canvasId, data);
    else renderSignalsChart(canvasId, data);
}

// ---------------------------------------------------------------------------
// Actions (Tasks 2.9, 2.10, Promote)
// ---------------------------------------------------------------------------

function saveToCompare() {
    if (!lastRun) { showToast("Run a backtest first", "warning"); return; }
    if (lastRun.stored && lastRun.stored.series_status !== "present") {
        showToast("Chart data is not stored for this run — re-run it before saving to compare", "warning");
        return;
    }
    const res = SessionState.addCompareSlot(lastRun);
    if (!res.ok) { showToast("Compare is full (4/4)", "warning"); return; }
    showToast(`Saved to Compare — slot ${res.index}/${SessionState.maxCompareSlots}`, "success");
}

function exportCsv() {
    if (!lastRun) { showToast("Run a backtest first", "warning"); return; }
    const trades = lastRun.result.trades;
    if (!Array.isArray(trades)) {
        showToast("Stored series unavailable for this run — export needs a fresh run", "warning");
        return;
    }
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

// ---------------------------------------------------------------------------
// Recent runs — tiles of ledger rows (flat R1 metrics only). Clicking a tile
// reads the stored payload back; it never re-runs the simulation.
// ---------------------------------------------------------------------------

function fmtDateRange(from, to) {
    const f = (d) => (d || "").split("-").reverse().join("/");
    return `${f(from)} – ${f(to)}`;
}

async function loadRecentRuns() {
    try {
        const body = await fetchJSON(`/api/backtest/runs?limit=${RECENT_RUNS_LIMIT}`);
        recentRuns = body.runs || [];
    } catch {
        recentRuns = [];  // ledger offline (or a source that never persists)
    }
    renderRecentRuns();
}

function renderRecentRuns() {
    const wrap = document.getElementById("recentRuns");
    if (!wrap) return;
    if (!recentRuns.length) { wrap.hidden = true; wrap.innerHTML = ""; return; }
    wrap.hidden = false;
    wrap.innerHTML =
        `<div class="recent-head"><h2 class="card-title">Recent Runs</h2></div>` +
        `<div class="recent-grid">` +
        recentRuns.map((r, i) => {
            const ret = (r.total_return ?? 0) * 100;
            const wr = r.total_trades === 0 ? "—" : `${(r.win_rate ?? 0).toFixed(1)}%`;
            const dd = ((r.max_drawdown ?? 0) * 100).toFixed(1);
            const status = r.series_status === "evicted"
                ? `<span class="badge badge-subtle" title="Charts evicted by retention; metrics still stored">no chart</span>`
                : r.series_status === "write_failed"
                    ? `<span class="badge bg-light-danger" title="Series failed to persist — re-run recommended">⛔</span>`
                    : "";
            return `
            <div class="card recent-tile g-card-hover" data-idx="${i}" role="button" tabindex="0"
                 title="Open the stored result — read from the ledger, no re-run">
                <div class="recent-tile-top">
                    <strong>${esc(r.strategy_id)}</strong>
                    <span class="badge ${ret >= 0 ? "bg-light-success" : "bg-light-danger"}">${ret >= 0 ? "+" : ""}${ret.toFixed(1)}%</span>
                    ${status}
                </div>
                <div class="recent-tile-sub muted">${esc(r.symbol)} · ${esc(r.timeframe)} · ${fmtDateRange(r.date_from, r.date_to)}</div>
                <div class="recent-tile-stats">
                    <span>WR ${wr}</span>
                    <span>DD ${dd}%</span>
                    <span>${r.total_trades ?? 0} trades</span>
                    <span>Sharpe ${(r.sharpe ?? 0).toFixed(2)}</span>
                </div>
            </div>`;
        }).join("") +
        `</div>`;
    wrap.querySelectorAll(".recent-tile").forEach((tile) => {
        const open = () => openStoredRun(recentRuns[Number(tile.dataset.idx)].run_id);
        tile.addEventListener("click", open);
        tile.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
        });
    });
}

/** Rebuild the form-side config from a stored payload.config (ledger shape:
 *  strategy_params + engine, not the UI's params + mode). */
function configFromLedger(cfg) {
    return {
        strategy: cfg.strategy,
        symbol: cfg.symbol,
        timeframe: cfg.timeframe,
        from_date: cfg.from_date,
        to_date: cfg.to_date,
        capital: cfg.capital,
        params: cfg.strategy_params || {},
        mode: cfg.engine === "quick_screen" ? "quick_screen" : "",
    };
}

async function openStoredRun(runId) {
    let rec;
    try {
        rec = await fetchJSON(`/api/backtest/runs/${encodeURIComponent(runId)}`);
    } catch (err) {
        showToast(err.message || "Could not open stored run", "error");
        return;
    }
    const payload = rec.payload;
    const cfg = configFromLedger(payload.config || {});
    lastRun = { config: cfg, result: payload, resultId: TuneThis.mintResultId(), stored: rec.ledger };
    // pre-fill the form so "Run" would re-produce the same test
    try {
        $("strategy").value = cfg.strategy || "";
        if (cfg.symbol) $("symbol").value = cfg.symbol;
        if (cfg.timeframe) {
            const want = Timeframes.toCanonical(cfg.timeframe);
            const sel = $("timeframe");
            if (want && [...sel.options].some((o) => o.value === want)) sel.value = want;
        }
        if (cfg.from_date) $("fromDate").value = cfg.from_date;
        if (cfg.to_date) $("toDate").value = cfg.to_date;
        if (cfg.capital) $("capital").value = cfg.capital;
        if ($("fastPreview")) $("fastPreview").checked = cfg.mode === "quick_screen";
    } catch { /* form nodes always present */ }
    fetchJSON(`/api/strategies/${encodeURIComponent(cfg.strategy)}/params`)
        .then((p) => { renderParams(p); applyParamOverrides(cfg.params); })
        .catch(() => { /* stored result still shown */ });
    $("emptyState").hidden = true;
    $("results").hidden = false;
    renderResults(payload);
    showToast(`Opened stored run — ${cfg.strategy} (${cfg.symbol}). Read from the ledger, not re-run.`, "info");
}

async function init() {
    wireTabs();

    // Which prices this run is measured on. A disabled source blocks the Run
    // button and says why, rather than letting it 409 on click.
    if (window.DataSourceGate) {
        DataSourceGate.mount("dataSourceGate", {
            status: $("dataSourceGate").dataset.status,
            blockIds: ["runBtn"],
        });
    }

    $("runBtn").addEventListener("click", runBacktest);
    $("saveCompareBtn").addEventListener("click", saveToCompare);
    $("exportCsvBtn").addEventListener("click", exportCsv);
    $("promoteBtn").addEventListener("click", promoteToForward);
    loadRecentRuns();

    // §1.3/§1.4: the picker and the timeframe dropdown are driven by what the
    // server says the chosen symbol actually has, so neither can offer a
    // phantom option.
    symbolPicker = SymbolPicker.mount({
        select: "symbol", search: "symbol-search", tabs: "symbol-tabs", summary: "symbol-status",
        onChange: () => Timeframes.applyTo($("timeframe"), document.body.dataset.source === "synthetic"
            ? ["1day"] : symbolPicker.timeframesFor($("symbol").value)),
    });
    Timeframes.applyTo($("timeframe"), document.body.dataset.source === "synthetic" ? ["1day"] : null);
    const demoButton = $("useSyntheticDemo");
    if (demoButton && document.body.dataset.source === "synthetic") {
        demoButton.addEventListener("click", () => {
            // An explicit choice of the already-configured generated source.
            // It never enables a source, changes a guard, or starts a run.
            symbolPicker.setValue("DEMO");
            const selected = $("symbol").selectedOptions[0];
            if (selected) selected.textContent = "DEMO — generated random walk";
            Timeframes.applyTo($("timeframe"), ["1day"]);
            $("symbol-status").textContent = "Generated daily candles · not real market prices";
        });
    }

    // load strategies
    let strategies = [];
    function escapeHtml(text) {
        return String(text ?? "").replace(/[&<>"']/g, (m) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m]));
    }

    function updateStrategySegmentInfo(stratName) {
        const infoEl = $("strategy-segment-info");
        if (!infoEl) return;
        const strat = strategies.find((s) => s.name === stratName);
        if (!strat) {
            infoEl.innerHTML = "";
            return;
        }
        const seg = strat.default_segment || "equity_intraday";
        let html = `<span>Linked segment:</span> <span class="badge badge-subtle" style="font-family:monospace;">${escapeHtml(seg)}</span> <span class="text-muted" style="font-size:11px;">(execution fees & margins follow Settings)</span>`;
        if (strat.readable_criteria && (strat.readable_criteria.entry || strat.readable_criteria.entry_strike)) {
            const rc = strat.readable_criteria;
            html += `<div style="width:100%; font-size:11px; color:var(--muted); margin-top:2px;">`
                + `<strong>Criteria:</strong> `
                + (rc.entry ? `Entry: <em>${escapeHtml(rc.entry)}</em>` : "")
                + (rc.entry_strike ? ` · Strike: <em>${escapeHtml(rc.entry_strike)}</em>` : "")
                + (rc.take_profit ? ` · TP: <em>${escapeHtml(rc.take_profit)}</em>` : "")
                + (rc.stop_loss ? ` · SL: <em>${escapeHtml(rc.stop_loss)}</em>` : "")
                + `</div>`;
        }
        infoEl.innerHTML = html;
    }

    try {
        // venue=backtest: the server omits option strategies, because a
        // backtest runs on DB candles and the DB holds no historical chains.
        strategies = await fetchJSON("/api/strategies?venue=backtest");
        $("strategy").innerHTML = "";
        strategies.forEach((strategy) => {
            const option = document.createElement("option");
            option.value = strategy.name;
            option.title = strategy.name;
            const acronyms = new Set(["sma", "ema", "rsi", "macd", "roc", "vwap"]);
            option.textContent = strategy.name.split("_").map((part, index) =>
                acronyms.has(part) ? part.toUpperCase() : index === 0
                    ? part.charAt(0).toUpperCase() + part.slice(1) : part
            ).join(" ");
            $("strategy").appendChild(option);
        });
    } catch (err) {
        $("strategy").innerHTML = `<option value="">failed to load</option>`;
        showToast("Could not load strategies", "error");
    }

    // strategy change → dynamic params & segment info
    $("strategy").addEventListener("change", async () => {
        updateStrategySegmentInfo($("strategy").value);
        try {
            renderParams(await fetchJSON(`/api/strategies/${encodeURIComponent($("strategy").value)}/params`));
        } catch (err) { showToast(err.message, "error"); }
    });

    // pre-fill from Compare's "Open in Backtest" (Task 3.8 hand-off)
    const pre = SessionState.backtestPrefill;
    if (pre && pre.config && pre.config.strategy) {
        const cfg = pre.config;
        $("strategy").value = cfg.strategy;
        updateStrategySegmentInfo(cfg.strategy);
        if (cfg.symbol) {
            symbolPicker.setValue(cfg.symbol);
            Timeframes.applyTo($("timeframe"), document.body.dataset.source === "synthetic" ? ["1day"] : symbolPicker.timeframesFor(cfg.symbol));
        }
        if (cfg.timeframe) {
            const want = Timeframes.toCanonical(cfg.timeframe);
            const sel = $("timeframe");
            if (want && [...sel.options].some((o) => o.value === want)) sel.value = want;
        }
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
        // default: render params and segment info for the first strategy
        updateStrategySegmentInfo($("strategy").value);
        try {
            renderParams(await fetchJSON(`/api/strategies/${encodeURIComponent($("strategy").value)}/params`));
        } catch { /* ignore */ }
    }
}

document.addEventListener("DOMContentLoaded", init);
