/**
 * Equity Curve chart (single strategy).
 * renderEquityChart(canvasId, data)
 *   data: { dates: string[], values: number[], benchmark?: number[] }
 */
const _equityInstances = {};

function renderEquityChart(canvasId, data) {
    const ctx = document.getElementById(canvasId);
    if (!ctx || !data || !data.dates || !data.dates.length) return;

    if (_equityInstances[canvasId]) {
        _equityInstances[canvasId].destroy();
    }

    const datasets = [
        {
            label: "Equity",
            data: data.values,
            borderColor: "#3b82f6",
            backgroundColor: "rgba(59,130,246,.10)",
            tension: 0.15,
            pointRadius: 0,
            borderWidth: 2,
            fill: true,
        },
    ];

    if (data.benchmark && data.benchmark.length) {
        datasets.push({
            label: "Buy & Hold",
            data: data.benchmark,
            borderColor: "#94a3b8",
            backgroundColor: "transparent",
            tension: 0.15,
            pointRadius: 0,
            borderWidth: 1.5,
            borderDash: [6, 3],
            fill: false,
        });
    }

    _equityInstances[canvasId] = new Chart(ctx, {
        type: "line",
        data: { labels: data.dates, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: "index", intersect: false },
            plugins: {
                legend: { labels: { color: "#e2e8f0", boxWidth: 14 } },
                tooltip: {
                    callbacks: {
                        label: (item) =>
                            `${item.dataset.label}: $${Number(item.parsed.y).toLocaleString(undefined, { minimumFractionDigits: 2 })}`,
                    },
                },
            },
            scales: {
                x: {
                    ticks: { color: "#94a3b8", maxTicksLimit: 8 },
                    grid: { color: "rgba(148,163,184,.15)" },
                },
                y: {
                    ticks: { color: "#94a3b8" },
                    grid: { color: "rgba(148,163,184,.15)" },
                },
            },
        },
    });
}
