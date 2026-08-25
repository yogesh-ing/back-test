/**
 * Drawdown chart (single strategy).
 * renderDrawdownChart(canvasId, data)
 *   data: { dates: string[], values: number[], worst_dd_pct: number, worst_dd_date: string }
 */
const _drawdownInstances = {};

function renderDrawdownChart(canvasId, data) {
    const ctx = document.getElementById(canvasId);
    if (!ctx || !data || !data.dates || !data.dates.length) return;

    if (_drawdownInstances[canvasId]) {
        _drawdownInstances[canvasId].destroy();
    }

    _drawdownInstances[canvasId] = new Chart(ctx, {
        type: "line",
        data: {
            labels: data.dates,
            datasets: [
                {
                    label: "Drawdown",
                    data: data.values.map((v) => v * 100),
                    borderColor: "#ef4444",
                    backgroundColor: "rgba(239,68,68,.15)",
                    tension: 0.15,
                    pointRadius: 0,
                    borderWidth: 2,
                    fill: true,
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: "index", intersect: false },
            plugins: {
                legend: { labels: { color: "#e2e8f0", boxWidth: 14 } },
                tooltip: {
                    callbacks: {
                        label: (item) => `Drawdown: ${Number(item.parsed.y).toFixed(2)}%`,
                    },
                },
            },
            scales: {
                x: {
                    ticks: { color: "#94a3b8", maxTicksLimit: 8 },
                    grid: { color: "rgba(148,163,184,.15)" },
                },
                y: {
                    reverse: true,
                    ticks: {
                        color: "#94a3b8",
                        callback: (v) => `${v.toFixed(1)}%`,
                    },
                    grid: { color: "rgba(148,163,184,.15)" },
                },
            },
        },
    });
}
