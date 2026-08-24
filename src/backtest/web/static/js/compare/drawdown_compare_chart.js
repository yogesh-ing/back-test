/**
 * Overlaid Drawdown (PRD Task 3.7).
 * renderCompareDrawdown(canvasId, slots)
 *   slots: [{id, label, color, result:{drawdown:{dates,values}, metrics}}]
 * Adapter returns values as raw ratios → scaled to % for display.
 */
const _ddCompare = {};
function renderCompareDrawdown(canvasId, slots) {
    const ctx = document.getElementById(canvasId);
    if (!ctx) return;
    if (_ddCompare[canvasId]) _ddCompare[canvasId].destroy();

    const master = unionDates(slots.map((s) => s.result.drawdown.dates));
    const datasets = slots.map((s) => {
        const dd = s.result.drawdown;
        return {
            label: `${s.label} (${dd.worst_dd_pct.toFixed(1)}%)`,
            data: alignSeries(master, dd.dates, dd.values.map((v) => +(v * 100).toFixed(4))),
            borderColor: s.color, backgroundColor: s.color + "33",
            tension: 0.15, pointRadius: 0, borderWidth: 2, fill: false, spanGaps: true,
        };
    });

    _ddCompare[canvasId] = new Chart(ctx, {
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
                y: { ticks: { color: "#94a3b8", callback: (v) => `${v}%` }, grid: { color: "rgba(148,163,184,.15)" } },
            },
        },
    });
}
