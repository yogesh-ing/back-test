/**
 * Overlaid Equity Curves (PRD Task 3.6).
 * renderCompareEquity(canvasId, slots)
 *   slots: [{id, label, color, result:{equity:{dates,values}, metrics}}]
 *
 * Slots may have different bar counts (different timeframes), so series are
 * aligned to the union of all dates — missing points are null (gaps).
 */

/** Union + sort all date arrays. */
function unionDates(dateArrays) {
    const set = new Set();
    dateArrays.forEach((arr) => arr.forEach((d) => set.add(d)));
    return [...set].sort();           // ISO dates sort lexicographically
}

/** Align a {dates, values} series to a master date array (null where absent). */
function alignSeries(master, dates, values) {
    const map = new Map(dates.map((d, i) => [d, values[i]]));
    return master.map((d) => (map.has(d) ? map.get(d) : null));
}

const _equityCompare = {};
function renderCompareEquity(canvasId, slots) {
    const ctx = document.getElementById(canvasId);
    if (!ctx) return;
    if (_equityCompare[canvasId]) _equityCompare[canvasId].destroy();

    const master = unionDates(slots.map((s) => s.result.equity.dates));
    const datasets = slots.map((s) => {
        const ret = s.result.metrics.total_return_pct;
        return {
            label: `${s.label} (${ret >= 0 ? "+" : ""}${ret.toFixed(1)}%)`,
            data: alignSeries(master, s.result.equity.dates, s.result.equity.values),
            borderColor: s.color, backgroundColor: s.color,
            tension: 0.15, pointRadius: 0, borderWidth: 2, fill: false, spanGaps: true,
        };
    });

    _equityCompare[canvasId] = new Chart(ctx, {
        type: "line",
        data: { labels: master, datasets },
        options: {
            responsive: true, maintainAspectRatio: false,
            interaction: { mode: "index", intersect: false },
            plugins: {
                legend: { labels: { color: "#e2e8f0", boxWidth: 14 } },
                tooltip: { callbacks: { label: (item) => `${item.dataset.label}` } },
            },
            scales: {
                x: { ticks: { color: "#94a3b8", maxTicksLimit: 8 }, grid: { color: "rgba(148,163,184,.15)" } },
                y: { ticks: { color: "#94a3b8" }, grid: { color: "rgba(148,163,184,.15)" } },
            },
        },
    });
}
