/**
 * Data-freshness chip (topbar). Color = staleness of the last completed
 * trading session in market_data_cache; see data_manager.get_data_freshness.
 * Server-renders on every page; this script adds the two live behaviors:
 *   1. amber/red click -> jump to the Data tab (its to-date already defaults
 *      to today, so the fetch is one more click away), and
 *   2. re-render when a fetch job finishes (data_fetch_indicator.js calls
 *      window.refreshDataFreshnessChip on the running -> idle transition).
 */
(function () {
    const chip = document.getElementById('data-freshness-chip');
    if (!chip) return;
    const label = document.getElementById('data-freshness-label');

    function actionable(state) {
        return state === 'amber' || state === 'red';
    }

    chip.addEventListener('click', async () => {
        if (actionable(chip.dataset.state)) {
            // Fetch freshness data to get pre-filled dates
            try {
                const resp = await fetch('/api/data/freshness');
                if (resp.ok) {
                    const data = await resp.json();
                    // Find the worst timeframe (most missing sessions)
                    let bestFrom = null;
                    let bestTo = null;
                    for (const [tf, info] of Object.entries(data.per_timeframe || {})) {
                        if (info.from_date && info.to_date) {
                            // Use the earliest from_date across timeframes
                            if (!bestFrom || info.from_date < bestFrom) {
                                bestFrom = info.from_date;
                                bestTo = info.to_date;
                            }
                        }
                    }
                    // Redirect with date params if available
                    if (bestFrom && bestTo) {
                        window.location.href = `/data?from=${bestFrom}&to=${bestTo}`;
                    } else {
                        window.location.href = '/data';
                    }
                } else {
                    window.location.href = '/data';
                }
            } catch (err) {
                window.location.href = '/data';
            }
        }
    });

    window.refreshDataFreshnessChip = async function () {
        try {
            const resp = await fetch('/api/data/freshness');
            if (!resp.ok) return;
            const data = await resp.json();
            chip.dataset.state = data.level;
            chip.title = data.title;
            if (label) label.textContent = data.label;
        } catch (err) {
            /* keep the server-rendered state; a missing ping is not news */
        }
    };
})();
