/**
 * Data Manager page controller.
 * Handles: instrument picker (Equity/Index tabs, search, multi-select),
 * fetch start/stop, progress polling, inventory display.
 *
 * The fetch controls are STATE-DRIVEN: every /api/data/status poll (and the
 * init poll on page load) decides whether Start or Stop is showing, so a
 * reload mid-fetch still offers the Stop button.
 *
 * ONE REQUEST PER PAGE LOAD (2026-10-06): the picker list and the inventory
 * table below it are two views of one payload. Before, the list asked
 * `curated=1` and the table asked `/api/data/inventory`, which ran the SAME
 * `GROUP BY symbol, timeframe` scan a second time — the page counted every bar
 * in the cache twice to draw one screen. The list now asks
 * `include_catalogue=0` (the fetchable universe UNION everything already
 * fetched, with bars and from→to per row) and BOTH views render from it, so
 * they can never disagree about what is on disk.
 */

const $ = (id) => document.getElementById(id);

// Set default to_date to today
$('dm-toDate').value = new Date().toISOString().split('T')[0];

let pollTimer = null;

// -----------------------------------------------------------------------
// Instrument picker (replaces the old free-text symbols box)
// -----------------------------------------------------------------------

const DM_TABS = [
    { id: 'equity,index', label: 'All' },
    { id: 'equity', label: 'Equity' },
    { id: 'index', label: 'Index' },
];
const DM_PAGE_SIZE = 500;

const dmState = {
    tab: 'equity,index',
    rows: [],
    total: 0,              // matching rows before paging (may exceed rows.length)
    timeframes: [],        // canonical vocabulary, from the server
    selected: new Set(),   // symbols ticked for the next fetch
    error: null,
};

function dmEsc(v) {
    return String(v === null || v === undefined ? '' : v).replace(/[&<>"']/g, (c) => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
}

async function dmFetchCoverage(params) {
    const qs = new URLSearchParams(params).toString();
    const r = await fetch(`/api/data/coverage?${qs}`);
    const data = await r.json();
    if (!r.ok) throw new Error((data && data.error) || `HTTP ${r.status}`);
    return data;
}

/** Label detail for a Data-tab row: "1min · 4,817 bars · 02 Sep → 30 Sep".
 *
 * timeframes_STORED, deliberately — what was downloaded is the fact a row label
 * can convey; timeframes_available is the derived set the Backtest picker
 * offers, nine entries for any symbol with healthy 1min data, so printing it
 * here told every row the same long thing. Mirrors SymbolPicker.detailOf().
 *
 * The window is the third fact, and on this tab it is the one that answers
 * "did I actually fetch the range I asked for?" — the question a fetch of
 * "1 Jan to 30 Sep" leaves open when the broker only had from July.
 */
function dmDetail(row) {
    if (!row.data_available) return '';
    const bits = [];
    const stored = row.timeframes_stored || [];
    if (stored.length) bits.push(stored.join('/'));
    if (row.bars_count) bits.push(`${row.bars_count.toLocaleString()} bars`);
    const window = dmWindow(row);
    if (window) bits.push(window);
    return bits.join(' · ');
}

/** "02 Sep → 30 Sep", or "" when the row has no dates. Day+month, not ISO:
 *  the table has a From/To column pair for exact dates; this one is a label. */
function dmWindow(row) {
    const from = dmDay(row.from_date);
    const to = dmDay(row.to_date);
    if (!from && !to) return '';
    return `${from || '?'} → ${to || '?'}`;
}

function dmDay(value) {
    if (!value) return '';
    const parts = String(value).slice(0, 10).split('-');
    if (parts.length !== 3) return String(value);
    const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    const month = months[Number(parts[1]) - 1];
    return month ? `${parts[2]} ${month}` : String(value);
}

function dmRenderList() {
    const list = $('dm-symbol-list');
    if (dmState.error) {
        list.innerHTML = `<div class="dm-symbol-empty">Could not load instruments — ${dmEsc(dmState.error)}</div>`;
        return;
    }
    if (!dmState.rows.length) {
        list.innerHTML = '<div class="dm-symbol-empty">No instruments match this search.</div>';
        return;
    }
    list.innerHTML = dmState.rows.map((row) => {
        const checked = dmState.selected.has(row.symbol) ? 'checked' : '';
        const detail = dmDetail(row);
        const name = row.name && row.name !== row.symbol ? `<span class="dm-symbol-name">${dmEsc(row.name)}</span>` : '';
        // Not-yet-fetched is a TODO for this page, not a defect: the row is
        // still listed (it is in the fetchable universe) and tickable for the
        // next fetch, so the label points at the action.
        const cov = detail
            ? `<span class="dm-symbol-cov">${dmEsc(detail)}</span>`
            : '<span class="dm-symbol-cov dm-symbol-nodata">not fetched yet</span>';
        return `
            <label class="dm-symbol-row" title="${dmEsc(row.symbol)}${row.name ? ' — ' + dmEsc(row.name) : ''}">
                <input type="checkbox" value="${dmEsc(row.symbol)}" ${checked}>
                <span class="dm-symbol-sym">${dmEsc(row.symbol)}</span>
                ${name}
                ${cov}
            </label>`;
    }).join('');
}

function dmRenderCount() {
    const n = dmState.selected.size;
    const parts = [`${dmState.rows.length} shown`];
    if (n) {
        const names = [...dmState.selected].slice(0, 6).join(', ');
        parts.push(`${n} selected: ${names}${n > 6 ? '…' : ''}`);
    } else {
        const unfetched = {
            'equity,index': 'fetches all NIFTY 200 stocks + NSE indices',
            equity: 'fetches all NIFTY 200 stocks',
            index: 'fetches all NSE indices',
        };
        parts.push(`none selected — ${unfetched[dmState.tab] || unfetched['equity,index']}`);
    }
    $('dm-symbol-count').textContent = parts.join(' · ');
}

async function dmLoadSymbols() {
    try {
        // include_catalogue=0: the fetchable universe UNION everything with
        // bars, coverage measured once. `curated=1` would restrict the list to
        // the universe and hide a symbol somebody fetched outside it; the full
        // report would add ~140k contract rows this page cannot fetch.
        const params = { limit: DM_PAGE_SIZE, types: dmState.tab, include_catalogue: 0 };
        const q = $('dm-symbol-search').value.trim();
        if (q) params.q = q;
        const data = await dmFetchCoverage(params);
        dmState.rows = data.instruments || [];
        dmState.total = data.total || 0;
        // Server-owned vocabulary (finest first). Used to order the timeframe
        // SET in the summary — encounter order would print "1day, 1min" purely
        // because OBSCUREMIDCAP sorts before RELIANCE.
        dmState.timeframes = data.timeframes || [];
        dmState.error = null;
    } catch (err) {
        dmState.error = err.message || String(err);
        dmState.rows = [];
        dmState.total = 0;
    }
    dmRenderList();
    dmRenderCount();
    // The table below renders from the SAME rows — no second request, and no
    // chance of the list and the table disagreeing about what is on disk.
    dmRenderInventory();
}

/** Symbols in the loaded page that actually hold bars. */
function dmStoredRows() {
    return dmState.rows.filter((r) => r && r.data_available);
}

/** The inventory table: one row per symbol, from the payload already in hand.
 *
 * One row per symbol rather than per (symbol, timeframe): with the pipeline
 * moving to "store 1min, derive everything coarser" a symbol has exactly one
 * stored granularity, and repeating its dates once per timeframe was noise.
 * Mixed historical data still reads honestly — the Stored column lists every
 * granularity, and the per-timeframe dates ride along in the row's tooltip.
 */
function dmRenderInventory() {
    const tbody = $('dm-inv-table') ? $('dm-inv-table').querySelector('tbody') : null;
    const rows = dmStoredRows().slice().sort((a, b) => String(a.symbol).localeCompare(String(b.symbol)));

    if (tbody) {
        tbody.innerHTML = '';
        if (!rows.length) {
            tbody.innerHTML = '<tr><td colspan="5" class="muted" style="text-align:center; padding:16px;">No bars stored yet — tick instruments above and fetch.</td></tr>';
        }
        for (const r of rows) {
            const stored = (r.timeframes_stored || []).join(', ') || '-';
            const bars = Number(r.bars_count || 0);
            const perTf = Object.entries(r.dates_by_timeframe || {})
                .map(([tf, d]) => `${tf}: ${d.from || '?'} → ${d.to || '?'}`)
                .join('\n');
            const tr = document.createElement('tr');
            if (perTf && (r.timeframes_stored || []).length > 1) tr.title = perTf;
            tr.innerHTML = `
                <td><strong>${dmEsc(r.symbol)}</strong></td>
                <td>${dmEsc(stored)}</td>
                <td>${bars.toLocaleString()}</td>
                <td>${dmEsc(r.from_date || '-')}</td>
                <td>${dmEsc(r.to_date || '-')}</td>
            `;
            tbody.appendChild(tr);
        }
    }

    const empty = $('dm-inv-empty');
    const table = $('dm-inv-table');
    if (empty && table) {
        empty.hidden = rows.length > 0;
        table.hidden = rows.length === 0;
    }

    const timeframes = new Set();
    let bars = 0;
    for (const r of rows) {
        for (const tf of r.timeframes_stored || []) timeframes.add(tf);
        bars += Number(r.bars_count || 0);
    }
    // Order by the server's vocabulary; anything it does not know (an older
    // server, a new granularity) sorts last rather than disappearing.
    const rank = (tf) => {
        const i = dmState.timeframes.indexOf(tf);
        return i === -1 ? dmState.timeframes.length : i;
    };
    $('dm-inv-symbols').textContent =
        `${rows.length} symbol${rows.length === 1 ? '' : 's'}` +
        (dmState.total > dmState.rows.length ? ` (of ${dmState.total} shown)` : '');
    $('dm-inv-bars').textContent = `${bars.toLocaleString()} bars`;
    $('dm-inv-timeframes').textContent =
        [...timeframes].sort((a, b) => rank(a) - rank(b)).join(', ') || '-';
}

function dmRenderTabs() {
    const tabsEl = $('dm-symbol-tabs');
    tabsEl.innerHTML = DM_TABS.map((t) => (
        `<button type="button" class="sym-tab${t.id === dmState.tab ? ' active' : ''}" data-tab="${t.id}">${t.label}</button>`
    )).join('');
    tabsEl.addEventListener('click', (e) => {
        const btn = e.target.closest('[data-tab]');
        if (!btn) return;
        dmState.tab = btn.dataset.tab;
        tabsEl.querySelectorAll('[data-tab]').forEach((b) => b.classList.toggle('active', b === btn));
        dmLoadSymbols();
    });
}

$('dm-symbol-list').addEventListener('change', (e) => {
    const box = e.target.closest('input[type="checkbox"]');
    if (!box) return;
    if (box.checked) dmState.selected.add(box.value);
    else dmState.selected.delete(box.value);
    dmRenderCount();
});

$('dm-symbol-search').addEventListener('input', (() => {
    let timer = null;
    return () => {
        clearTimeout(timer);
        timer = setTimeout(dmLoadSymbols, 250);
    };
})());

$('dm-symbol-clear').addEventListener('click', () => {
    dmState.selected.clear();
    dmRenderList();
    dmRenderCount();
});

// -----------------------------------------------------------------------
// Fetch start / stop
// -----------------------------------------------------------------------

function dmSetFormEnabled(enabled) {
    [$('dm-timeframe'), $('dm-fromDate'), $('dm-toDate'), $('dm-symbol-search'), $('dm-symbol-clear')]
        .forEach((el) => { el.disabled = !enabled; });
    $('dm-symbol-list').querySelectorAll('input[type="checkbox"]')
        .forEach((el) => { el.disabled = !enabled; });
}

/** The single place that decides Start-vs-Stop visibility. */
function applyJobState(j) {
    const running = j.status === 'running';
    $('dm-fetchBtn').hidden = running;
    $('dm-stopBtn').hidden = !running;
    $('dm-stopBtn').disabled = running && !!j.cancel;  // stop already requested
    // Clear is only meaningful once the panel is showing a FINISHED job —
    // while running, the stop flow owns the panel.
    $('dm-clearProgress').hidden = running;
    if (running || j.status === 'done' || j.status === 'error') {
        $('dm-progress-section').hidden = false;
    }
    dmSetFormEnabled(!running);
}

$('dm-fetchBtn').addEventListener('click', async () => {
    const timeframe = $('dm-timeframe').value;
    const fromDate = $('dm-fromDate').value;
    const toDate = $('dm-toDate').value;

    if (!fromDate || !toDate) {
        showToast('Pick a date range', 'warning');
        return;
    }

    const body = { timeframe, from_date: fromDate, to_date: toDate };
    if (dmState.selected.size) {
        body.symbols = [...dmState.selected].sort();
    }
    // Nothing ticked: the active tab is the fetch scope (All = NIFTY 200
    // equities + indices, Equity = 200 stocks, Index = indices). The server
    // enforces this too — the old "no list = every NSE/BSE stock" fallback
    // is gone (2026-10-02).
    body.scope = dmState.tab;

    try {
        const resp = await fetch('/api/data/fetch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (!resp.ok) {
            showToast(data.error || 'Failed to start fetch', 'error');
            return;
        }
        showToast('Fetch started', 'success');
        applyJobState({ status: 'running' });
        startPolling();
    } catch (err) {
        showToast(err.message, 'error');
    }
});

$('dm-stopBtn').addEventListener('click', async () => {
    try {
        const resp = await fetch('/api/data/stop', { method: 'POST' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            showToast(data.error || 'Could not stop the fetch', 'error');
            return;
        }
        showToast('Stopping after current chunk/symbol…', 'warning');
        pollStatus();  // reflect the cancel flag on the button right away
    } catch (err) {
        showToast(err.message, 'error');
    }
});

$('dm-clearProgress').addEventListener('click', async () => {
    try {
        const resp = await fetch('/api/data/clear', { method: 'POST' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            showToast(data.error || 'Could not clear the progress panel', 'error');
            return;
        }
        // The server is back to idle, so a page reload won't resurrect the
        // finished panel either.
        $('dm-progress-section').hidden = true;
        applyJobState({ status: 'idle' });
        showToast('Fetch progress cleared', 'success');
    } catch (err) {
        showToast(err.message, 'error');
    }
});

// -----------------------------------------------------------------------
// Progress polling
// -----------------------------------------------------------------------

function startPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(pollStatus, 2000);
}

function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

async function pollStatus() {
    try {
        const resp = await fetch('/api/data/status');
        const j = await resp.json();
        updateProgress(j);
        applyJobState(j);

        if (j.status === 'running') {
            // Covers a page load while a job is already running: keep polling.
            if (!pollTimer) startPolling();
            return;
        }

        if (j.status === 'done' || j.status === 'error' || j.status === 'idle') {
            const wasRunning = pollTimer !== null;
            stopPolling();
            if (wasRunning && j.status === 'done') {
                // Coverage-aware skip: a resume over an already-fetched range
                // legitimately stores ~0 bars — say so, or it reads as a hang.
                const skipNote = j.skipped > 0
                    ? ` (${j.skipped} already covered, skipped)`
                    : '';
                if (j.chunk_errors > 0) {
                    // mStock ate chunks even after retries — a "done" with
                    // holes must never render as a clean success.
                    showToast(`Finished, but ${j.chunk_errors} chunk request(s) failed even after retries — data may be partial; re-run the same fetch, it only pulls the missing days`, 'error');
                } else {
                    showToast(`Fetch complete: ${j.fetched} symbols${skipNote}, ${j.bars_total.toLocaleString()} bars`, 'success');
                }
                dmLoadSymbols();  // one request refreshes the list AND the table
            }
            if (wasRunning && j.status === 'error') {
                showToast(j.error || 'Fetch failed', 'error');
            }
        }
    } catch (err) {
        // server might be restarting
    }
}

function dmElapsed(j) {
    // Prefer a live countdown (the server only refreshes `elapsed` per
    // symbol, which freezes for minutes on wide 1-min ranges).
    if (j.status === 'running' && j.started_at) {
        const s = Math.max(0, Math.floor(Date.now() / 1000 - j.started_at));
        return `${Math.floor(s / 60)}m ${s % 60}s`;
    }
    return j.elapsed || '';
}

function updateProgress(j) {
    const total = j.total || 1;
    // Continuous progress: completed symbols + the fraction of chunks done
    // inside the current symbol, so the bar moves every request.
    const inSymbol = j.chunk_total > 0 ? Math.min((j.chunk_done || 0) / j.chunk_total, 1) : 0;
    const pct = Math.min(100, Math.round(((j.fetched || 0) + inSymbol) / total * 100));

    $('dm-progress-bar').style.width = pct + '%';
    $('dm-status-text').textContent =
        j.status === 'running' ? (j.cancel ? 'Stopping…' : `Fetching ${j.timeframe} data...`) :
        j.status === 'done' ? (j.error === 'Cancelled by user' ? 'Cancelled' : 'Done') :
        j.status === 'error' ? (j.error ? 'Error: ' + j.error.split(';')[0] : 'Error') : 'Idle';
    $('dm-fetched-count').textContent = j.chunk_total > 0 && j.status === 'running'
        ? `${j.fetched || 0} / ${j.total || 0} symbols · chunk ${j.chunk_done || 0}/${j.chunk_total}`
        : `${j.fetched || 0} / ${j.total || 0} symbols`;
    $('dm-bars-count').textContent = `${(j.bars_total || 0).toLocaleString()} bars`;
    $('dm-elapsed').textContent = dmElapsed(j);

    if (j.symbol) {
        $('dm-current-symbol').hidden = false;
        $('dm-current-name').textContent = j.symbol;
    } else {
        $('dm-current-symbol').hidden = true;
    }

    // Show errors
    if (j.failed_list && j.failed_list.length > 0) {
        $('dm-errors').hidden = false;
        $('dm-error-list').innerHTML = j.failed_list.map(
            ([sym, err]) => `<div class="error-item"><strong>${dmEsc(sym)}</strong>: ${dmEsc(err)}</div>`
        ).join('');
    }
}

// Refresh re-requests the ONE payload; both views re-render from it. It used
// to fetch the inventory separately (a second full scan of the same rows).
$('dm-refreshBtn').addEventListener('click', dmLoadSymbols);

// -----------------------------------------------------------------------
// Init
// -----------------------------------------------------------------------
dmRenderTabs();
dmLoadSymbols();   // renders the list and the inventory table
pollStatus();  // restore running state (stop button + polling) if a job is live
