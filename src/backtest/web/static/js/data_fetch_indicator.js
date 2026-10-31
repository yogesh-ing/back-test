/**
 * Global data fetch status indicator for navbar.
 * Shows when a background fetch is running and allows stopping it.
 */

(function() {
    const indicator = document.getElementById('data-fetch-indicator');
    const spinner = document.getElementById('data-fetch-spinner');
    const countEl = document.getElementById('data-fetch-count');
    
    if (!indicator || !countEl) return;
    
    let pollTimer = null;
    let lastKnownRunning = false;
    let lastCheckTs = 0;

    function clearScheduled() {
        if (pollTimer) {
            clearTimeout(pollTimer);
            pollTimer = null;
        }
    }

    function scheduleCheck(delayMs) {
        clearScheduled();
        if (delayMs === null || delayMs === undefined) return;
        pollTimer = setTimeout(checkStatus, delayMs);
    }

    function computeDelay(isRunning) {
        const pc = typeof window !== "undefined" && window.PollCadence;
        if (pc) return pc.dataStatusDelay(isRunning);
        return (typeof document.hidden === "boolean" && document.hidden)
            ? (isRunning ? 6000 : 45000)
            : (isRunning ? 3000 : 20000);
    }

    function startPolling() {
        checkStatus(); // immediate check
        const pc = typeof window !== "undefined" && window.PollCadence;
        if (pc) {
            pc.onDataJobWake(() => { checkStatus(); });
        }
        if (typeof document !== "undefined" && document && typeof document.addEventListener === "function") {
            document.addEventListener("visibilitychange", () => {
                if (!document.hidden) {
                    if (lastKnownRunning || (Date.now() - lastCheckTs >= 30000)) {
                        checkStatus();
                    } else {
                        scheduleCheck(computeDelay(lastKnownRunning));
                    }
                } else {
                    scheduleCheck(computeDelay(lastKnownRunning));
                }
            });
        }
    }

    async function checkStatus() {
        clearScheduled();
        let isRunning = false;
        const wasRunning = lastKnownRunning;
        lastCheckTs = Date.now();
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
        scheduleCheck(computeDelay(isRunning));
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
