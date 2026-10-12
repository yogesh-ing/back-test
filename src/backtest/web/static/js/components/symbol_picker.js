/**
 * Symbol picker — the ONE instrument list for Backtest, Compare and Optimize
 * (PRD backTest-enhance §1.3).
 *
 *   const picker = SymbolPicker.mount({ select, search, tabs, summary, onChange });
 *
 * Reads GET /api/data/coverage?names_only=1 and renders:
 *   - a search box
 *   - All / Equity / Index / F&O filter tabs
 *   - a <select> of instrument NAMES — the static list, every row selectable
 *   - a one-line summary: how many instruments, and that stored data is
 *     checked when the backtest runs
 *
 * The list is deliberately name-only. It once asked `available=1`, which made
 * the server answer "which symbols have bars, how many, over which dates, at
 * which timeframes" for the entire catalogue before the dropdown could draw a
 * single row: seconds of work per load on a real database, for numbers that
 * only matter at the moment of running something. A row with no candles is
 * not filtered out or greyed out any more, because whether a symbol can serve
 * a request depends on the timeframe AND the dates asked for — a fact an
 * option in a dropdown cannot express. The run is where that is decided, and
 * it fails with the dates that DO exist:
 *
 *   data error: Symbol 'X' has no 1day data between 2026-10-01 and
 *   2026-10-31. Available — 1min 02 Sep 2026 to 14 Sep 2026 (4,823 bars).
 *
 * Consumers must treat `coverage_known === false` as "not asked", never as
 * "no data" — `data_available` is null in that mode, and null is falsy.
 *
 * Before this, each page kept its own hard-coded <option> list, so a symbol
 * silently vanished unless the page's author had remembered to add it. The
 * server decides what is knowable; this file renders what it is told and
 * never invents or hides an entry.
 */
(function (global) {
    "use strict";

    const TABS = [
        { id: "", label: "All" },
        { id: "equity", label: "Equity" },
        { id: "index", label: "Index" },
        { id: "fno", label: "F&O" },
    ];

    const PAGE_SIZE = 500;
    const NO_DATA_TITLE = "No data loaded. Go to Data tab → fetch data for this symbol.";

    function esc(v) {
        return String(v === null || v === undefined ? "" : v).replace(/[&<>"']/g, (c) => (
            { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
        ));
    }

    /** Coverage string for an option label: "1min · 1,247 bars".
     *
     * Reads timeframes_STORED, not timeframes_AVAILABLE. The two answer
     * different questions and only one belongs on a label: "available" is the
     * derived set the dropdown offers, which is the same nine entries for every
     * instrument holding healthy 1min data — no information about THIS symbol,
     * and long enough to swamp the name beside it. "stored" is the one fact the
     * label can carry that the user cannot guess: what was actually downloaded.
     *
     * The dropdown keeps using timeframes_available, because there the derived
     * set is the whole point (a 1min symbol can answer 1day; a 1day-only symbol
     * must never be offered 1min, since resampling runs one way).
     */
    function detailOf(row) {
        if (!row || !row.data_available) return "";
        const bits = [];
        const stored = row.timeframes_stored || [];
        if (stored.length) bits.push(stored.join("/"));
        if (row.bars_count) bits.push(`${row.bars_count.toLocaleString()} bars`);
        return bits.join(" · ");
    }

    function _el(ref) {
        if (!ref) return null;
        return typeof ref === "string" ? document.getElementById(ref) : ref;
    }

    async function fetchCoverage(params) {
        const qs = new URLSearchParams(params || {}).toString();
        const r = await fetch(`/api/data/coverage${qs ? `?${qs}` : ""}`);
        const data = await r.json();
        if (!r.ok) throw new Error((data && data.error) || `HTTP ${r.status}`);
        return data;
    }

    function makeOption(row) {
        const opt = document.createElement("option");
        opt.value = row.symbol;
        // Readable label: "RELIANCE" when the catalogue has a display name,
        // falling back to the raw symbol. Broker tokens (ISINs, NFO contract
        // ids like 011NSETEST36DECFUT) make poor labels, so a name that is
        // just the symbol re-encoded is not used.
        const display = (row.name && row.name !== row.symbol && !/^[0-9A-Z]*[0-9][0-9A-Z]*$/.test(row.name))
            ? `${row.symbol} (${row.name})`
            : row.symbol;
        opt.textContent = display;
        // `coverage_known === false` means the server deliberately did not
        // look at the stored bars (names-only list). This test must come
        // FIRST: data_available is then null, and reading null as "no data"
        // would disable every option in the list.
        if (row.coverage_known === false) {
            opt.title = row.name && row.name !== row.symbol ? `${row.symbol} — ${row.name}` : row.symbol;
        } else if (!row.data_available) {
            // Listed, but not selectable: the user can see it exists and is told
            // where to get it, instead of it silently not being there.
            opt.disabled = true;
            opt.className = "sym-no-data";
            opt.title = row.hint || NO_DATA_TITLE;
        } else {
            const detail = detailOf(row);
            if (detail) opt.textContent = `${display} — ${detail}`;
            opt.title = [row.name, detail].filter(Boolean).join(" · ");
        }
        return opt;
    }

    function mount(opts) {
        const select = _el(opts.select);
        if (!select) return null;
        const search = _el(opts.search);
        const tabsEl = _el(opts.tabs);
        const summary = _el(opts.summary);
        const summaryBaseClass = (summary && summary.className) || "text-muted";
        const placeholder = opts.placeholder || "Select a symbol…";

        const state = {
            // defaultTab (issues.txt P1): a mount can start on a specific tab
            // — the spawn form's equity picker opens on Equity so an equity
            // strategy is offered equity instruments first.
            tab: TABS.some((t) => t.id === opts.defaultTab) ? opts.defaultTab : "",
            rows: [], total: 0, known: 0, available: 0,
            hidden: 0, dbAvailable: true, hint: "", error: null,
        };

        function render() {
            select.innerHTML = "";
            const head = document.createElement("option");
            head.value = "";
            head.textContent = placeholder;
            head.disabled = true;
            head.selected = true;
            select.appendChild(head);
            state.rows.forEach((row) => select.appendChild(makeOption(row)));

            if (!summary) return;
            // Reset first: a hint class/title from an earlier load must not
            // survive into an error or a no-database answer.
            summary.className = summaryBaseClass;
            summary.title = "";
            if (state.error) {
                summary.textContent = `Could not load symbols — ${state.error}`;
                return;
            }
            if (state.dbAvailable === false) {
                summary.textContent = `${state.known} known · no data source connected`;
                return;
            }
            // Names-only list: there is no "with data" count to report, and
            // inventing one ("0 with data") would be a lie — the server was
            // never asked. Say what the list IS instead, and where the
            // availability question gets answered.
            const namesOnly = state.rows.length > 0 && state.rows[0].coverage_known === false;
            if (namesOnly) {
                let text = `${state.rows.length} instruments`;
                const paged = state.total - state.rows.length;
                if (paged > 0) text += ` · ${paged} more — search to narrow`;
                text += " · data checked when you run";
                summary.title = "Which timeframes and dates are available is resolved at run time.";
                summary.textContent = text;
                return;
            }
            const runnable = state.rows.filter((r) => r.data_available).length;
            let text = `${state.rows.length} shown · ${runnable} with data · ${state.known} known`;
            // issues.txt B1: the data-only list must SAY what it left out.
            // "No data" behind a short list reads as "no such symbol" and as
            // "the indices are missing" — name the count and the fix.
            const notes = [];
            if (state.hidden > 0) {
                const noun = state.hidden === 1 ? "symbol" : "symbols";
                notes.push(`${state.hidden} ${noun} hidden — load data first (Data tab →)`);
                summary.title = state.hint || NO_DATA_TITLE;
            }
            // Same complaint, second cause: more WITH-DATA rows match than one
            // page carries (PAGE_SIZE). Say so rather than look truncated.
            const paged = state.total - state.rows.length;
            if (paged > 0) notes.push(`${paged} more with data — search to find them`);
            if (notes.length) {
                text += ` · ${notes.join(" · ")}`;
                summary.className = (summaryBaseClass + " sym-hidden-hint").trim();
            }
            summary.textContent = text;
        }

        async function load() {
            try {
                // RE-ARCHITECTURE (2026-10-06): the dropdown is a STATIC list
                // of instrument names and nothing else. It used to ask
                // available=1, which made the server compute per-symbol bar
                // counts, from/to dates and serviceable timeframes for the
                // whole catalogue just to render labels — seconds of work on
                // a real database, for numbers the user only needs once they
                // are about to run something.
                //
                // Whether a symbol has candles for a given timeframe is now
                // answered by the backtest itself, which fails with the dates
                // that DO exist ("no 1day data between X and Y — Available:
                // 1min 02 Sep to 14 Sep"). An option that is merely not
                // selectable can say nothing at all; an error can name the
                // dates.
                const params = { limit: PAGE_SIZE, names_only: 1 };
                if (search && search.value.trim()) params.q = search.value.trim();
                if (state.tab) params.types = state.tab;
                const data = await fetchCoverage(params);
                state.rows = data.instruments || [];
                state.total = data.total || 0;
                state.known = data.known_total || 0;
                state.available = data.available_total || 0;
                state.hidden = data.hidden_total || 0;
                state.hint = data.hint || "";
                state.dbAvailable = data.db_available;
                state.error = null;
            } catch (err) {
                state.error = err.message || String(err);
                state.rows = [];
                state.hidden = 0;
            }
            render();
        }

        if (search) {
            let timer = null;
            search.addEventListener("input", () => {
                clearTimeout(timer);
                timer = setTimeout(load, 250);
            });
        }
        if (tabsEl) {
            tabsEl.innerHTML = TABS.map((t) => (
                `<button type="button" class="sym-tab${t.id === state.tab ? " active" : ""}" data-tab="${t.id}">${t.label}</button>`
            )).join("");
            tabsEl.addEventListener("click", (e) => {
                const btn = e.target.closest("[data-tab]");
                if (!btn) return;
                state.tab = btn.dataset.tab;
                tabsEl.querySelectorAll("[data-tab]").forEach((b) => b.classList.toggle("active", b === btn));
                load();
            });
        }
        if (opts.onChange) select.addEventListener("change", () => opts.onChange(select.value));

        const handle = {
            select, search, summary, state,
            load, refresh: load,
            /** Select a symbol, injecting it if it is not on the loaded page. */
            setValue(symbol) {
                if (!symbol) return;
                if (state.rows.some((r) => r.symbol === symbol)) { select.value = symbol; return; }
                // Not on the page: add it as a plain, selectable name. A symbol
                // the caller knows about must be pickable even when it is
                // outside the current filter (and even though we have not asked
                // whether it has bars — that is the run's job now).
                const row = { symbol, name: symbol, coverage_known: false };
                select.insertBefore(makeOption(row), select.firstChild);
                select.value = symbol;
            },
            /**
             * Timeframes this symbol is known to serve, or [] when unknown.
             *
             * Always [] on a names-only list, which is the intended outcome:
             * the dropdown then offers the full canonical set (see
             * Timeframes.applyTo) and the run rejects an unsupported
             * timeframe with the dates that are stored.
             */
            timeframesFor(symbol) {
                const row = state.rows.find((r) => r.symbol === symbol);
                return row && row.timeframes_available ? row.timeframes_available.slice() : [];
            },
            /**
             * Symbols currently loaded, for the Test Generalization slot
             * pickers (PRD §4.2). Returns the loaded page only — callers that
             * need a different symbol should use `setValue` first so the server
             * has a chance to inject it.
             */
            symbols() {
                return state.rows.map((r) => r.symbol).filter(Boolean);
            },
        };

        render();
        load();
        return handle;
    }

    const SymbolPicker = { TABS, NO_DATA_TITLE, mount, esc, detailOf, fetchCoverage };
    global.SymbolPicker = SymbolPicker;
    if (typeof module !== "undefined" && module.exports) module.exports = SymbolPicker;
}(typeof window !== "undefined" ? window : globalThis));
