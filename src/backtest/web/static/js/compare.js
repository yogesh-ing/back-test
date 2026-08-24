/**
 * Compare page controller (PRD Tasks 3.3, 3.4, 3.8).
 * Slot management (add/remove, independent strategy + params), Run All →
 * /api/backtest/run-many, render 3 views, per-slot Open-in-Backtest / Promote.
 */
const PALETTE = ["#3b82f6", "#f59e0b", "#22c55e", "#ef4444"]; // blue, orange, green, red
const TF_OPTIONS = ["1D", "1H", "4H", "1W"].map((t) => `<option>${t}</option>`).join("");

let strategies = [];          // [{name,...}]
let slots = [];               // [{id, color, card, strategy, timeframe, runConfig, result, label}]
let nextId = 1;
let lastResults = null;       // successful slots from last run (for tab charts)

const $ = (id) => document.getElementById(id);
async function fetchJSON(url, opts) {
    const r = await fetch(url, opts);
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
    return d;
}

// ---------------------------------------------------------------------------
// Param rendering is provided by components/params_form.js (shared).
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Slot management (Task 3.3)
// ---------------------------------------------------------------------------

function strategiesOptions(selected) {
    return strategies.map((s) => `<option value="${s.name}" ${s.name === selected ? "selected" : ""}>${s.name}</option>`).join("");
}

function addSlot(prefill) {
    if (slots.length >= 4) { showToast("Maximum of 4 slots", "warning"); return null; }
    const id = nextId++;
    const color = PALETTE[slots.length % PALETTE.length];
    const card = document.createElement("div");
    card.className = "slot-card";
    card.style.borderTopColor = color;
    card.dataset.id = id;
    card.innerHTML = `
        <div class="slot-head">
            <span class="slot-label"><span class="slot-dot" style="background:${color}"></span> <span class="slot-num">Slot ${slots.length + 1}</span></span>
            <button class="btn-icon remove-slot" title="Remove">✕</button>
        </div>
        <div class="form-row"><label>Strategy</label><select class="slot-strategy input"></select></div>
        <div class="form-row"><label>Timeframe</label><select class="slot-tf input">${TF_OPTIONS}</select></div>
        <div class="slot-params"></div>
        <div class="slot-status muted small"></div>`;
    $("slotsRow").appendChild(card);

    const slot = { id, color, card, strategy: "", timeframe: "1D", runConfig: null, result: null, label: "" };
    const stratSel = card.querySelector(".slot-strategy");
    const tfSel = card.querySelector(".slot-tf");
    stratSel.addEventListener("change", () => onStrategyChange(slot, stratSel.value));
    tfSel.addEventListener("change", () => { slot.timeframe = tfSel.value; updateLabel(slot); });
    card.querySelector(".remove-slot").addEventListener("click", () => removeSlot(slot));

    slots.push(slot);

    const cfg = prefill && prefill.config;
    if (cfg && cfg.strategy) {
        stratSel.innerHTML = strategiesOptions(cfg.strategy);
        slot.strategy = cfg.strategy;
        if (cfg.timeframe) { tfSel.value = cfg.timeframe; slot.timeframe = cfg.timeframe; }
        onStrategyChange(slot, cfg.strategy, cfg.params);
    } else if (strategies.length) {
        stratSel.innerHTML = strategiesOptions(strategies[0].name);
        slot.strategy = strategies[0].name;
        onStrategyChange(slot, strategies[0].name);
    } else {
        stratSel.innerHTML = '<option value="">loading…</option>';
    }
    relabelSlots();
    refreshControls();
    return slot;
}

function removeSlot(slot) {
    if (slots.length <= 1) { showToast("Keep at least one slot", "warning"); return; }
    slot.card.remove();
    slots = slots.filter((s) => s !== slot);
    relabelSlots();
    refreshControls();
}

async function onStrategyChange(slot, name, overrides) {
    slot.strategy = name;
    try {
        const params = await fetchJSON(`/api/strategies/${encodeURIComponent(name)}/params`);
        renderParamsInto(slot.card.querySelector(".slot-params"), params, overrides);
    } catch (err) { showToast(err.message, "error"); }
    updateLabel(slot);
}

function updateLabel(slot) {
    slot.label = `${slot.strategy} ${slot.timeframe}`;
}

function relabelSlots() {
    slots.forEach((s, i) => {
        s.card.querySelector(".slot-num").textContent = `Slot ${i + 1}`;
        s.card.style.borderTopColor = PALETTE[i % PALETTE.length];
        s.color = PALETTE[i % PALETTE.length];
        s.card.querySelector(".slot-dot").style.background = s.color;
    });
}

function refreshControls() {
    $("addSlotBtn").style.display = slots.length >= 4 ? "none" : "";
}

// ---------------------------------------------------------------------------
// Run All (Task 3.4)
// ---------------------------------------------------------------------------

function sharedConfig() {
    return {
        symbol: $("symbol").value, from_date: $("fromDate").value,
        to_date: $("toDate").value, capital: Number($("capital").value) || 0,
    };
}

function slotConfig(slot) {
    return {
        strategy: slot.strategy, symbol: $("symbol").value,
        timeframe: slot.timeframe, from_date: $("fromDate").value,
        to_date: $("toDate").value, capital: Number($("capital").value) || 0,
        params: collectParamsFrom(slot.card.querySelector(".slot-params")),
    };
}

async function runAll() {
    if (!$("fromDate").value || !$("toDate").value) { showToast("Pick a date range", "warning"); return; }
    const empty = slots.filter((s) => !s.strategy);
    if (empty.length) { showToast("Every slot needs a strategy", "warning"); return; }

    const payload = {
        shared: sharedConfig(),
        slots: slots.map((s) => ({ id: s.id, strategy: s.strategy, timeframe: s.timeframe,
                                    params: collectParamsFrom(s.card.querySelector(".slot-params")) })),
    };

    slots.forEach((s) => { s.result = null; s.card.querySelector(".slot-status").innerHTML = '<span class="muted">Running…</span>'; });
    $("emptyState").hidden = true;
    $("results").hidden = false;
    showLoader("compareTable", "Running all slots…");

    try {
        const data = await fetchJSON("/api/backtest/run-many", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        slots.forEach((s) => {
            const r = data.results[String(s.id)];
            s.result = r || null;
            s.runConfig = slotConfig(s);
            const st = s.card.querySelector(".slot-status");
            if (!r) st.innerHTML = '<span class="neg">no result</span>';
            else if (r.error) st.innerHTML = `<span class="neg">⚠ ${r.error}</span>`;
            else st.innerHTML = `<span class="pos">✓ ${(r.metrics.total_return_pct >= 0 ? "+" : "") + r.metrics.total_return_pct.toFixed(2)}%</span>`;
        });

        const ok = slots.filter((s) => s.result && !s.result.error);
        document.getElementById("compareTable").innerHTML = "";   // clear loader
        if (!ok.length) { showToast("All slots failed", "error"); $("results").hidden = true; $("emptyState").hidden = false; return; }
        lastResults = ok;
        renderResults(ok);
        showToast(`Compared ${ok.length} slot${ok.length > 1 ? "s" : ""}`, "success");
    } catch (err) {
        showToast(err.message || "Run failed", "error");
        document.getElementById("compareTable").innerHTML = "";
        $("results").hidden = true;
        $("emptyState").hidden = false;
    }
}

function renderResults(okSlots) {
    renderCompareTable("compareTable", okSlots, onSlotAction);
    renderChartForPane(document.querySelector(".tab.active")?.dataset.tab || "metrics");
}

function renderChartForPane(pane) {
    if (!lastResults) return;
    if (pane === "equity") renderCompareEquity("equityCompareChart", lastResults);
    else if (pane === "drawdown") renderCompareDrawdown("drawdownCompareChart", lastResults);
}

// ---------------------------------------------------------------------------
// Per-slot actions (Task 3.8)
// ---------------------------------------------------------------------------

function onSlotAction(slot, kind) {
    const config = slot.runConfig || slotConfig(slot);
    if (kind === "backtest") {
        SessionState.backtestPrefill = { config };
        showToast("Opening in Backtest…", "success");
        setTimeout(() => { window.location.href = "/backtest"; }, 400);
    } else {
        SessionState.forwardPrefill = { config };
        showToast("Promoting to Forward…", "success");
        setTimeout(() => { window.location.href = "/forward"; }, 400);
    }
}

// ---------------------------------------------------------------------------
// Tabs + init
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

async function init() {
    wireTabs();
    $("addSlotBtn").addEventListener("click", () => addSlot());
    $("runAllBtn").addEventListener("click", runAll);

    try {
        strategies = await fetchJSON("/api/strategies");
    } catch (err) {
        showToast("Could not load strategies", "error");
        strategies = [];
    }

    const saved = SessionState.compareSlots;
    if (saved && saved.length) {
        saved.slice(0, 4).forEach((s) => addSlot(s));
        showToast(`Loaded ${Math.min(saved.length, 4)} saved slot(s)`, "success");
    } else {
        addSlot(); addSlot();   // start with 2 slots
    }
}

document.addEventListener("DOMContentLoaded", init);
