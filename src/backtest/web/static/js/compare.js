/**
 * Compare page controller (PRD backTest-enhance §4).
 *
 * Two modes, one set of shared conditions:
 *   strategies      — different strategies/parameters, one shared symbol (§4.2)
 *   generalization  — ONE strategy and parameter set, up to 4 symbols (§4.2)
 *
 * Everything except strategy and parameters is a SHARED condition (§4.1):
 * symbol (in strategies mode), dates, capital, timeframe, engine. Two slots
 * that disagree on any of those are not two readings of one experiment, they
 * are two experiments, and the table would be comparing them anyway.
 */
const PALETTE = ["#3b82f6", "#d4b26a", "#7fc8a0", "#e0938f"]; // blue, orange, green, red
const MAX_SLOTS = 4;          // §4.2 caps generalization at four symbols

let strategies = [];          // [{name,...}]
let slots = [];               // [{id, color, card, strategy, params, symbol, runConfig, result, label}]
let nextId = 1;
let mode = "strategies";      // §4.2
let lastResults = null;       // successful slots from last run (for tab charts)
let lastProvenance = null;    // provenance of the SHARED conditions (engine + data)
let lastComparison = null;    // §4.3/§4.4 correlation + significance from the server
let symbolPicker = null;      // components/symbol_picker.js handle

const $ = (id) => document.getElementById(id);
/** Server errors carry a request_id that also appears in the app log — quoting it
 *  here means a screenshot of a toast is enough to find the traceback. */
function _apiError(data, status) {
    const id = data && data.request_id ? ` [req ${data.request_id}]` : "";
    return `${(data && data.error) || `HTTP ${status}`}${id}`;
}

async function fetchJSON(url, opts) {
    const r = await fetch(url, opts);
    const d = await r.json();
    if (!r.ok) throw new Error(_apiError(d, r.status));
    return d;
}

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (m) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m]
));

function fmtDateRange(from, to) {
    const f = (d) => (d || "").split("-").reverse().join("/");
    return `${f(from)} – ${f(to)}`;
}

// ---------------------------------------------------------------------------
// Param rendering is provided by components/params_form.js (shared).
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Slot management (Task 3.3)
// ---------------------------------------------------------------------------

function strategiesOptions(selected) {
    return strategies.map((s) => {
        const kindBadge = s.signal_kind === "option" ? " [Options]" : "";
        return `<option value="${s.name}" ${s.name === selected ? "selected" : ""}>${s.name}${kindBadge}</option>`;
    }).join("");
}
function addSlot(prefill) {
    if (slots.length >= MAX_SLOTS) { showToast(`Maximum of ${MAX_SLOTS} slots`, "warning"); return null; }
    const id = nextId++;
    const color = PALETTE[slots.length % PALETTE.length];
    const card = document.createElement("div");
    card.className = "slot-card";
    card.style.borderTopColor = color;
    card.dataset.id = id;
    const generalization = mode === "generalization";
    // In generalization mode the strategy/params live in ONE shared editor
    // above the slots, so a slot card is just "run this symbol". Re-showing a
    // strategy dropdown per slot would quietly allow four different parameter
    // sets, which is the thing §4.2 exists to rule out.
    card.innerHTML = `
        <div class="slot-head">
            <span class="slot-label"><span class="slot-dot" style="background:${color}"></span> <span class="slot-num"></span></span>
            <button class="btn-icon remove-slot" title="Remove">✕</button>
        </div>
        ${generalization
            ? `<div class="form-row"><label for="slot-symbol-${id}">Symbol</label><select id="slot-symbol-${id}" class="slot-symbol input"></select></div>
               <div class="slot-params-note muted small"></div>`
            : `<div class="form-row"><label for="slot-strategy-${id}">Strategy</label><select id="slot-strategy-${id}" class="slot-strategy input"></select></div>
               <div class="slot-params"></div>`}
        <div class="slot-status muted small"></div>`;
    $("slotsRow").appendChild(card);

    const slot = { id, color, card, strategy: "", symbol: "", runConfig: null, result: null, label: "" };
    card.querySelector(".remove-slot").addEventListener("click", () => removeSlot(slot));

    if (generalization) {
        const symSel = card.querySelector(".slot-symbol");
        fillSymbolSelect(symSel, prefill && prefill.symbol ? [prefill.symbol] : []);
        symSel.addEventListener("change", () => { slot.symbol = symSel.value; updateLabel(slot); });
        slot.symbol = symSel.value || "";
        updateLabel(slot);
    } else {
        const stratSel = card.querySelector(".slot-strategy");
        stratSel.addEventListener("change", () => onStrategyChange(slot, stratSel.value));
        const cfg = prefill && prefill.config;
        if (cfg && cfg.strategy) {
            stratSel.innerHTML = strategiesOptions(cfg.strategy);
            onStrategyChange(slot, cfg.strategy, cfg.params);
        } else if (strategies.length) {
            stratSel.innerHTML = strategiesOptions(strategies[0].name);
            onStrategyChange(slot, strategies[0].name);
        } else {
            stratSel.innerHTML = '<option value="">loading…</option>';
        }
    }
    slots.push(slot);
    relabelSlots();
    refreshControls();
    return slot;
}

/** Populate a <select> with the picker's symbols, keeping `preferred` first. */
function fillSymbolSelect(sel, preferred) {
    if (!sel) return;
    const all = symbolPicker && typeof symbolPicker.symbols === "function" ? symbolPicker.symbols() : [];
    const want = (preferred || []).map((s) => String(s).toUpperCase());
    const list = want.concat(all.filter((s) => !want.includes(String(s).toUpperCase())));
    sel.innerHTML = list.map((s) => `<option value="${s}">${s}</option>`).join("")
        || '<option value="">no symbols</option>';
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
    // The slot label is what every chart legend and correlation axis reads, so
    // it has to name what actually distinguishes this slot: the symbol in
    // generalization mode, the strategy elsewhere.
    slot.label = mode === "generalization"
        ? (slot.symbol || "no symbol")
        : (slot.strategy || "no strategy");
    const note = slot.card.querySelector(".slot-params-note");
    if (note) note.textContent = `${genStrategyName()} · ${sharedTimeframe()}`;
}

function genStrategyName() {
    const sel = $("genStrategy");
    return (sel && sel.value) || "strategy";
}

function relabelSlots() {
    slots.forEach((s, i) => {
        s.card.querySelector(".slot-num").textContent =
            mode === "generalization" ? `Symbol ${i + 1}` : `Slot ${i + 1}`;
        s.card.style.borderTopColor = PALETTE[i % PALETTE.length];
        s.color = PALETTE[i % PALETTE.length];
        s.card.querySelector(".slot-dot").style.background = s.color;
    });
    const title = $("slotsTitle");
    if (title) title.textContent = mode === "generalization" ? "Symbols" : "Slots";
}

function refreshControls() {
    $("addSlotBtn").style.display = slots.length >= MAX_SLOTS ? "none" : "";
    const sharedTfRow = $("sharedTimeframeRow");
    if (sharedTfRow) sharedTfRow.hidden = mode === "generalization";
}

// ---------------------------------------------------------------------------
// Run All (Task 3.4)
// ---------------------------------------------------------------------------

/**
 * §4.1 — the shared conditions. These are read by EVERY slot; nothing below
 * may override them. Dates, capital, engine and (in strategies mode) symbol
 * all differ between slots would make the table a comparison of different
 * experiments rather than of different strategies.
 */
function sharedConfig() {
    return {
        symbol: sharedSymbol(), from_date: $("fromDate").value,
        to_date: $("toDate").value, capital: Number($("capital").value) || 0,
        mode: engineMode(),
    };
}

/** Strategies mode compares on the shared symbol; generalization ignores it. */
function sharedSymbol() {
    return ($("symbol") && $("symbol").value) || "";
}

function sharedTimeframe() {
    return ($("sharedTimeframe") && $("sharedTimeframe").value) || "1day";
}

/**
 * §4.1: the timeframe is a shared condition, filled from the shared symbol's
 * REAL coverage (§1.4) — no static list of granularities the data may not have.
 */
function applySharedTimeframe() {
    const sel = $("sharedTimeframe");
    if (!sel) return;
    const available = symbolPicker ? symbolPicker.timeframesFor(sharedSymbol()) : null;
    Timeframes.applyTo(sel, available);
}

/** Requested engine mode: "" (full engine) or "quick_screen" (fast preview). */
function engineMode() {
    const box = $("fastPreview");
    return box && box.checked ? "quick_screen" : "";
}

function slotConfig(slot) {
    return {
        strategy: mode === "generalization" ? $("genStrategy").value : slot.strategy,
        symbol: mode === "generalization" ? slot.symbol : sharedSymbol(),
        timeframe: sharedTimeframe(), from_date: $("fromDate").value,
        to_date: $("toDate").value, capital: Number($("capital").value) || 0,
        params: mode === "generalization"
            ? collectParamsFrom($("genParams"))
            : collectParamsFrom(slot.card.querySelector(".slot-params")),
    };
}

async function runAll() {
    if (!$("fromDate").value || !$("toDate").value) { showToast("Pick a date range", "warning"); return; }
    const generalization = mode === "generalization";
    const strategy = generalization ? $("genStrategy").value : "";
    if (!strategy && !strategies.length) { showToast("No strategy selected", "warning"); return; }
    if (generalization && !strategy) { showToast("Pick a strategy to test", "warning"); return; }
    const blank = slots.filter((s) => (generalization ? !s.symbol : !s.strategy));
    // The message has to name what is actually missing: telling someone whose
    // symbols are unset that they "need a strategy" sends them to edit the one
    // control that is already correct.
    if (blank.length) {
        showToast(generalization
            ? "Every symbol slot needs a symbol"
            : "Every slot needs a strategy", "warning");
        return;
    }

    const sharedParams = generalization ? collectParamsFrom($("genParams")) : null;
    const payload = {
        shared: sharedConfig(),
        slots: slots.map((s) => (generalization
            ? { id: s.id, symbol: s.symbol, strategy, timeframe: sharedTimeframe(),
                mode: engineMode(), params: sharedParams }
            : { id: s.id, strategy: s.strategy, timeframe: sharedTimeframe(),
                mode: engineMode(), params: collectParamsFrom(s.card.querySelector(".slot-params")) })),
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
        // §4.1: a failed slot stays on the page with its error. Dropping the
        // column would make three results look like a complete four-way test.
        if (!ok.length) { showToast("All slots failed", "error"); $("results").hidden = true; $("emptyState").hidden = false; return; }
        lastResults = ok;
        lastProvenance = data.provenance || null;
        lastComparison = data.comparison || null;
        renderResults(slots);
        const badge = $("comparePersistBadge");
        if (badge) {
            const lost = data.persisted === false;
            badge.hidden = !lost;
            if (lost) {
                badge.innerHTML = `<span class="badge bg-light-danger">⚠ NOT STORED</span> `
                    + `<span class="muted small">This comparison is not in the history ledger`
                    + (data.persist_error ? ` (${esc(data.persist_error)})` : "")
                    + ` — re-run it if the result matters.</span>`;
            }
        }
        const failed = slots.length - ok.length;
        showToast(`Compared ${ok.length} slot${ok.length > 1 ? "s" : ""}`
            + (failed ? ` · ${failed} failed` : ""), failed ? "warning" : "success");
        loadCompareHistory();
    } catch (err) {
        showToast(err.message || "Run failed", "error");
        document.getElementById("compareTable").innerHTML = "";
        $("results").hidden = true;
        $("emptyState").hidden = false;
    }
}

/**
 * `allSlots` includes the failed ones so their columns render (§4.1); only
 * successful slots reach the charts, which need an equity curve to draw.
 */
function renderResults(allSlots) {
    // Engine + data provenance for the SHARED conditions every slot ran under
    // (PRD backTest-enhance §1.1/§1.2). Slots that disagreed on the engine are
    // stamped "mixed" and the server's warning is shown alongside.
    if (typeof Provenance !== "undefined") Provenance.renderInto("compareProvenance", lastProvenance);
    renderCompareTable("compareTable", allSlots, onSlotAction);
    if (typeof ComparePanels !== "undefined") {
        ComparePanels.renderInto("comparePanels", lastComparison);
    }
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
// Stored comparison history (run-persistence PRD R4)
// Tiles list backtest_compare_runs rows; loading one reconstructs the table,
// panels and provenance from stored payloads — no process pool, no re-run.
// ---------------------------------------------------------------------------

const COMPARISON_LIST_VERSION = 1;  // mirrors backtest_run_store.COMPARISON_VERSION

async function loadCompareHistory() {
    let rows = [];
    try {
        const body = await fetchJSON("/api/backtest/compares?limit=6");
        rows = body.compares || [];
    } catch { /* ledger offline — no history section */ }
    renderCompareHistory(rows);
}

function renderCompareHistory(rows) {
    const wrap = $("compareHistory");
    if (!wrap) return;
    if (!rows.length) { wrap.hidden = true; wrap.innerHTML = ""; return; }
    wrap.hidden = false;
    wrap.innerHTML =
        `<div class="recent-head"><h2 class="card-title">Comparison history</h2></div>` +
        `<div class="recent-grid">` +
        rows.map((r) => {
            const outdated = r.comparison_version !== COMPARISON_LIST_VERSION;
            const syms = (r.symbols_used || []).join(", ") || "—";
            return `
            <div class="card recent-tile${outdated ? "" : " g-card-hover"}" data-id="${esc(r.compare_id)}"
                 role="button" tabindex="0" ${outdated ? 'aria-disabled="true"' : ""}
                 title="${outdated ? "Stored with different comparison math — re-run" : "Load the stored comparison — no re-run"}">
                <div class="recent-tile-top">
                    <strong>${esc(r.comparison_mode)}</strong>
                    <span class="badge badge-subtle">${r.slot_count} slots</span>
                    ${outdated
                        ? `<span class="badge bg-light-danger">⛠ Outdated calc logic — re-run</span>`
                        : `<span class="badge bg-light-success">Load</span>`}
                </div>
                <div class="recent-tile-sub muted">${esc(syms)} · ${fmtDateRange(r.date_from, r.date_to)}</div>
                <div class="recent-tile-stats">
                    <span>${esc(r.data_source || "source ?")}</span>
                    <span>${esc((r.created_at || "").slice(0, 10))}</span>
                </div>
            </div>`;
        }).join("") +
        `</div>`;
    wrap.querySelectorAll(".recent-tile").forEach((tile) => {
        if (tile.getAttribute("aria-disabled") === "true") return;
        const load = () => openStoredCompare(tile.dataset.id);
        tile.addEventListener("click", load);
        tile.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") { e.preventDefault(); load(); }
        });
    });
}

async function openStoredCompare(compareId) {
    let rec;
    try {
        rec = await fetchJSON(`/api/backtest/compares/${encodeURIComponent(compareId)}`);
    } catch (err) {
        showToast(err.message || "Could not load stored comparison", "error");
        return;
    }
    const p = rec.payload;
    const errors = p.slot_errors || {};
    const generalization = rec.ledger.comparison_mode === "generalization";
    const built = [];
    const kids = rec.children || [];
    for (let i = 0; i < kids.length; i++) {
        let detail;
        try {
            detail = await fetchJSON(`/api/backtest/runs/${encodeURIComponent(kids[i].run_id)}`);
        } catch { continue; }  // child read failure: show what we can
        const cfg = detail.payload.config || {};
        const slot = {
            id: kids[i].run_id,
            label: generalization
                ? `${cfg.strategy || "?"} · ${cfg.symbol || "?"}`
                : (cfg.strategy || kids[i].strategy_id || `slot ${i + 1}`),
            color: PALETTE[i % PALETTE.length],
            result: detail.payload,
            card: null,
            // Form-side shape for the "Open in Backtest" hand-off: the ledger
            // stores strategy_params/engine, the prefills read params/mode.
            runConfig: {
                strategy: cfg.strategy, symbol: cfg.symbol, timeframe: cfg.timeframe,
                from_date: cfg.from_date, to_date: cfg.to_date, capital: cfg.capital,
                params: cfg.strategy_params || {},
                mode: cfg.engine === "quick_screen" ? "quick_screen" : "",
            },
        };
        built.push(slot);
    }
    Object.entries(errors).forEach(([sid, e], j) => {
        built.push({ id: `err-${sid}`, label: `slot ${sid}`, error: e.error,
                     color: PALETTE[(built.length + j) % PALETTE.length], card: null });
    });
    if (!built.some((s) => s.result)) {
        showToast("Stored comparison has no readable slot results — re-run", "error");
        return;
    }
    // A slot is plottable when its equity series has points — {dates, values} from
    // the adapter, not an Array (an Array test dropped every slot on reload).
    lastResults = built.filter((s) => s.result?.equity?.dates?.length);
    lastProvenance = p.provenance || null;
    lastComparison = p.comparison || null;
    const badge = $("comparePersistBadge");
    if (badge) badge.hidden = true;
    $("emptyState").hidden = true;
    $("results").hidden = false;
    renderResults(built);
    const partial = Object.keys(errors).length;
    showToast(`Loaded stored comparison${partial ? ` — ⚠ partial result (${partial} slot(s) failed)` : ""}`,
              partial ? "warning" : "info");
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

// ---------------------------------------------------------------------------
// §4.2 Mode: Compare Strategies <-> Test Generalization
// ---------------------------------------------------------------------------

const MODE_NOTES = {
    strategies: "Each slot may use a different strategy and parameters. They all run on "
        + "the symbol above under the same dates, capital, timeframe and engine.",
    generalization: "One strategy, one parameter set, run across every symbol below under "
        + "the same dates, capital, timeframe and engine. A strategy that only works on one "
        + "of them was fitted to that symbol, not to the market.",
};

function wireModeToggle() {
    document.querySelectorAll(".mode-btn").forEach((btn) => {
        btn.addEventListener("click", () => setMode(btn.dataset.mode));
    });
}

function setMode(next) {
    if (next === mode) return;
    mode = next;
    document.querySelectorAll(".mode-btn").forEach((b) => {
        const on = b.dataset.mode === mode;
        b.classList.toggle("active", on);
        b.setAttribute("aria-checked", on ? "true" : "false");
    });
    const note = document.querySelector(".mode-note");
    if (note) note.textContent = MODE_NOTES[mode];
    $("generalizationSetup").hidden = mode !== "generalization";
    $("slotsRow").dataset.mode = mode;
    // Slot cards are structurally different between modes (strategy+params vs
    // symbol), so they are rebuilt rather than refilled.
    slots.forEach((s) => s.card.remove());
    slots = [];
    if (mode === "generalization" && strategies.length) {
        $("genStrategy").innerHTML = strategiesOptions($("genStrategy").value || strategies[0].name);
    }
    addSlot(); addSlot();
    refreshControls();
}

async function onGenStrategyChange(name) {
    try {
        const params = await fetchJSON(`/api/strategies/${encodeURIComponent(name)}/params`);
        renderParamsInto($("genParams"), params);
    } catch (err) { showToast(err.message, "error"); }
    slots.forEach(updateLabel);
}

async function init() {
    wireTabs();
    wireModeToggle();
    $("addSlotBtn").addEventListener("click", () => addSlot());
    $("runAllBtn").addEventListener("click", runAll);
    const note = document.querySelector(".mode-note");
    if (note) note.textContent = MODE_NOTES[mode];

    // §1.3/§1.4 — one picker for the page; the shared timeframe follows the
    // shared symbol so it only ever offers granularities that exist (§4.1).
    symbolPicker = SymbolPicker.mount({
        select: "symbol", search: "symbol-search", tabs: "symbol-tabs", summary: "symbol-status",
        onChange: applySharedTimeframe,
    });
    applySharedTimeframe();

    try {
        // venue=compare: option strategies are not offered here — a backtest
        // runs on DB candles and the DB holds no historical option chains.
        strategies = await fetchJSON("/api/strategies?venue=compare");
    } catch (err) {
        showToast("Could not load strategies", "error");
        strategies = [];
    }

    const genSel = $("genStrategy");
    if (genSel) {
        genSel.innerHTML = strategiesOptions(strategies.length ? strategies[0].name : "");
        genSel.addEventListener("change", () => onGenStrategyChange(genSel.value));
        if (strategies.length) onGenStrategyChange(genSel.value);
    }

    const saved = SessionState.compareSlots;
    if (saved && saved.length) {
        saved.slice(0, MAX_SLOTS).forEach((s) => addSlot(s));
        showToast(`Loaded ${Math.min(saved.length, MAX_SLOTS)} saved slot(s)`, "success");
    } else {
        addSlot(); addSlot();   // start with 2 slots
    }
    loadCompareHistory();
}

document.addEventListener("DOMContentLoaded", init);
