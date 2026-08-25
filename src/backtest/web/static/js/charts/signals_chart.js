/**
 * Price + Signals chart (single strategy).
 * renderSignalsChart(canvasId, data)
 *   data: {
 *     candles: [{ date, open, high, low, close }],
 *     buys:    [{ date, price }],
 *     sells:   [{ date, price }]
 *   }
 */
const _signalsInstances = {};

function renderSignalsChart(canvasId, data) {
    const ctx = document.getElementById(canvasId);
    if (!ctx || !data || !data.candles || !data.candles.length) return;

    if (_signalsInstances[canvasId]) {
        _signalsInstances[canvasId].destroy();
    }

    const dates = data.candles.map((c) => c.date);
    const closes = data.candles.map((c) => c.close);

    // Build buy/sell annotation datasets (scattered points)
    const buyData = new Array(dates.length).fill(null);
    const sellData = new Array(dates.length).fill(null);
    const dateIndex = new Map(dates.map((d, i) => [d, i]));

    (data.buys || []).forEach((b) => {
        const i = dateIndex.get(b.date);
        if (i !== undefined) buyData[i] = b.price;
    });
    (data.sells || []).forEach((s) => {
        const i = dateIndex.get(s.date);
        if (i !== undefined) sellData[i] = s.price;
    });

    const datasets = [
        {
            label: "Close",
            data: closes,
            borderColor: "#60a5fa",
            backgroundColor: "transparent",
            tension: 0.1,
            pointRadius: 0,
            borderWidth: 1.5,
            fill: false,
            yAxisID: "y",
        },
        {
            label: "Buy",
            data: buyData,
            type: "scatter",
            borderColor: "#22c55e",
            backgroundColor: "#22c55e",
            pointRadius: 7,
            pointStyle: "triangle",
            showLine: false,
            yAxisID: "y",
        },
        {
            label: "Sell",
            data: sellData,
            type: "scatter",
            borderColor: "#ef4444",
            backgroundColor: "#ef4444",
            pointRadius: 7,
            pointStyle: "rectRot",
            showLine: false,
            yAxisID: "y",
        },
    ];

    _signalsInstances[canvasId] = new Chart(ctx, {
        type: "line",
        data: { labels: dates, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: "index", intersect: false },
            plugins: {
                legend: { labels: { color: "#e2e8f0", boxWidth: 14 } },
                tooltip: {
                    callbacks: {
                        label: (item) => {
                            if (item.parsed.y === null) return null;
                            return `${item.dataset.label}: ${Number(item.parsed.y).toFixed(2)}`;
                        },
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
