/**
 * Global data fetch status indicator for navbar.
 * Shows when a background fetch is running and allows stopping it.
 */

(function() {
    const indicator = document.getElementById('data-fetch-indicator');
    const spinner = document.getElementById('data-fetch-spinner');
    const countEl = document.getElementById('data-fetch-count');
    
    if (!indicator || !countEl) return;
    
    let pollInterval = null;
    
    let pollTimer = null;
    let lastKnownRunning = false;
    
    function scheduleCheck(delayMs) {
        if (pollTimer) clearTimeout(pollTimer);
        pollTimer = setTimeout(checkStatus, delayMs);
    }

    function startPolling() {
        checkStatus(); // immediate check
    }
    
    async function checkStatus() {
        let isRunning = false;
        const wasRunning = lastKnownRunning;
        try {
            const resp = await fetch('/api/data/status');
            const data = await resp.json();
            
            if (data.status === 'running') {
                isRunning = true;
                // Show indicator
                indicator.hidden = false;
                countEl.textContent = `${data.fetched || 0}/${data.total || 0}`;
                
                // Update tooltip with current symbol
                if (data.symbol) {
                    indicator.title = `Fetching ${data.symbol}... Click to stop`;
                }
            } else {
                // Hide indicator
                indicator.hidden = true;
            }
        } catch (err) {
            // Server might be restarting, ignore
        }
        lastKnownRunning = isRunning;
        // A finished job just moved MAX(ts::date) — flip the freshness chip
        // without waiting for the next page navigation.
        if (wasRunning && !isRunning && window.refreshDataFreshnessChip) {
            window.refreshDataFreshnessChip();
        }
        const delay = (typeof document.hidden === "boolean" && document.hidden)
            ? (isRunning ? 6000 : 45000)
            : (isRunning ? 3000 : 20000);
        scheduleCheck(delay);
    }
    
    // Click to stop
    indicator.addEventListener('click', async () => {
        if (confirm('Stop the data fetch after the current symbol?')) {
            try {
                const resp = await fetch('/api/data/stop', { method: 'POST' });
                const data = await resp.json();
                
                if (resp.ok) {
                    alert('Data fetch will stop after completing the current symbol.');
                    indicator.style.background = '#dc2626';
                    indicator.title = 'Stopping...';
                } else {
                    alert(data.error || 'Failed to stop fetch');
                }
            } catch (err) {
                alert('Error: ' + err.message);
            }
        }
    });
    
    // Start polling when page loads
    startPolling();
})();
