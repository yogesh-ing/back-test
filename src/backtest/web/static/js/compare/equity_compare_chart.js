/**
 * Overlaid Equity Curves, indexed to 100 (PRD backTest-enhance §4.5).
 * renderCompareEquity(canvasId, slots)
 *   slots: [{id, label, color, result:{equity:{dates,values}, metrics}}]
 *
 * Every curve starts at 100 on the first shared date and is drawn in index
 * units, not rupees. The reason is comparative, not cosmetic: a ₹1,00,000 slot
 * and a ₹1,00,000 slot that made 40% and 4% produce two lines 100 apart and
 * 100.4 apart, so the chart's whole vertical range is spent re-stating the
 * starting capital instead of showing which strategy won. Indexing puts the
 * start on one line and spends the axis on the difference.
 *
 * Bars are re-based to the *first shared* date, not each slot's own first bar.
 * If a slot starts later it therefore begins above or below 100 rather than
 * being falsely shown as a winner that started flat.
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

/**
 * Rebase an equity curve to 100 at `base` (PRD §4.5).
 *
 * A curve with no value at the base date, or a non-positive base, cannot be
 * indexed — it is returned unchanged rather than producing Infinity/NaN that
 * would blank the whole chart. Mirrors `rebase_to_100()` on the server.
 */
function indexTo100(dates, values, base) {
    if (base === null || base === undefined || !isFinite(base) || base <= 0) return values.slice();
    return values.map((v) => (v === null || v === undefined ? null : (v / base) * 100));
}

const _equityCompare = {};
function renderCompareEquity(canvasId, slots) {
    const ctx = document.getElementById(canvasId);
    if (!ctx) return;
    if (_equityCompare[canvasId]) _equityCompare[canvasId].destroy();
    if (!slots.length) { _equityCompare[canvasId] = null; return; }

    const master = unionDates(slots.map((s) => s.result.equity.dates));
    // The first date every slot actually has data for. Using the union's first
    // date would re-base onto a bar some slot never traded.
    const shared = master.filter((d) => slots.every((s) => s.result.equity.dates.includes(d)));
    const baseDate = shared[0] || master[0];

    const datasets = slots.map((s) => {
        const ret = s.result.metrics.total_return_pct;
        const base = s.result.equity.values[s.result.equity.dates.indexOf(baseDate)];
        return {
            label: `${s.label} (${ret >= 0 ? "+" : ""}${ret.toFixed(1)}%)`,
            // Index first, then align to the master dates, so a slot that is
            // missing early bars still starts from the common base rather than
            // from whatever bar it happened to have.
            data: alignSeries(master, s.result.equity.dates, indexTo100(
                s.result.equity.dates, s.result.equity.values, base
            )),
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
                tooltip: { callbacks: { label: (item) => item.dataset.label } },
            },
            scales: {
                x: { ticks: { color: "#94a3b8", maxTicksLimit: 8 }, grid: { color: "rgba(148,163,184,.15)" } },
                y: {
                    ticks: { color: "#94a3b8", callback: (v) => v.toFixed(0) },
                    grid: { color: "rgba(148,163,184,.15)" },
                    title: { display: true, text: "Indexed to 100", color: "#94a3b8" },
                },
            },
        },
    });
}

if (typeof module !== "undefined" && module.exports) {
    module.exports = { indexTo100, unionDates, alignSeries, renderCompareEquity };
}
