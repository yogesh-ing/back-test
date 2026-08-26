/**
 * Data Manager page controller.
 * Handles: fetch start/stop, progress polling, inventory display.
 */

const $ = (id) => document.getElementById(id);

// Set default to_date to today
$('dm-toDate').value = new Date().toISOString().split('T')[0];

let pollTimer = null;

// -----------------------------------------------------------------------
// Fetch start / stop
// -----------------------------------------------------------------------

$('dm-fetchBtn').addEventListener('click', async () => {
    const timeframe = $('dm-timeframe').value;
    const fromDate = $('dm-fromDate').value;
    const toDate = $('dm-toDate').value;
    const symbolsRaw = $('dm-symbols').value.trim();

    if (!fromDate || !toDate) {
        showToast('Pick a date range', 'warning');
        return;
    }

    const body = { timeframe, from_date: fromDate, to_date: toDate };
    if (symbolsRaw) {
        body.symbols = symbolsRaw.split(',').map(s => s.trim().toUpperCase()).filter(Boolean);
    }

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
        $('dm-fetchBtn').hidden = true;
        $('dm-stopBtn').hidden = false;
        $('dm-progress-section').hidden = false;
        startPolling();
    } catch (err) {
        showToast(err.message, 'error');
    }
});

$('dm-stopBtn').addEventListener('click', async () => {
    try {
        await fetch('/api/data/stop', { method: 'POST' });
        showToast('Stopping after current symbol...', 'warning');
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

        if (j.status === 'done' || j.status === 'error' || j.status === 'idle') {
            stopPolling();
            $('dm-fetchBtn').hidden = false;
            $('dm-stopBtn').hidden = true;
            if (j.status === 'done') {
                showToast(`Fetch complete: ${j.fetched} symbols, ${j.bars_total.toLocaleString()} bars`, 'success');
                loadInventory();  // refresh inventory
            }
            if (j.status === 'error') {
                showToast(j.error || 'Fetch failed', 'error');
            }
        }
    } catch (err) {
        // server might be restarting
    }
}

function updateProgress(j) {
    const total = j.total || 1;
    const pct = Math.round((j.fetched / total) * 100);

    $('dm-progress-bar').style.width = pct + '%';
    $('dm-status-text').textContent =
        j.status === 'running' ? `Fetching ${j.timeframe} data...` :
        j.status === 'done' ? 'Done' :
        j.status === 'error' ? 'Error' : 'Idle';
    $('dm-fetched-count').textContent = `${j.fetched || 0} / ${j.total || 0} symbols`;
    $('dm-bars-count').textContent = `${(j.bars_total || 0).toLocaleString()} bars`;
    $('dm-elapsed').textContent = j.elapsed || '';

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
            ([sym, err]) => `<div class="error-item"><strong>${sym}</strong>: ${err}</div>`
        ).join('');
    }
}

// -----------------------------------------------------------------------
// Inventory
// -----------------------------------------------------------------------

async function loadInventory() {
    try {
        const resp = await fetch('/api/data/inventory');
        const j = await resp.json();
        if (!resp.ok) return;

        const syms = j.symbols || {};
        const names = Object.keys(syms).sort();
        const timeframes = new Set();

        const tbody = $('dm-inv-table').querySelector('tbody');
        tbody.innerHTML = '';

        if (names.length === 0) {
            $('dm-inv-empty').hidden = false;
            $('dm-inv-table').hidden = true;
        } else {
            $('dm-inv-empty').hidden = true;
            $('dm-inv-table').hidden = false;

            for (const sym of names) {
                const entries = syms[sym];
                for (const e of entries) {
                    timeframes.add(e.timeframe);
                    const tr = document.createElement('tr');
                    tr.innerHTML = `
                        <td><strong>${sym}</strong></td>
                        <td>${e.timeframe}</td>
                        <td>${e.bars.toLocaleString()}</td>
                        <td>${e.earliest || '-'}</td>
                        <td>${e.latest || '-'}</td>
                    `;
                    tbody.appendChild(tr);
                }
            }
        }

        $('dm-inv-symbols').textContent = `${names.length} symbols`;
        $('dm-inv-bars').textContent = `${(j.total_bars || 0).toLocaleString()} bars`;
        $('dm-inv-timeframes').textContent = [...timeframes].join(', ') || '-';
    } catch (err) {
        console.error('Inventory load failed:', err);
    }
}

$('dm-refreshBtn').addEventListener('click', loadInventory);

// -----------------------------------------------------------------------
// Init
// -----------------------------------------------------------------------
loadInventory();
pollStatus();  // check if a job is already running
