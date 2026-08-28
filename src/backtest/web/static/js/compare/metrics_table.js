/**
 * Compare Metrics Table (PRD Task 3.5).
 * renderCompareTable(containerId, slots, onAction)
 *   slots: [{id, label, color, result:{metrics}}]  (only successful slots)
 *   onAction(slot, kind): kind = 'backtest' | 'forward'
 * Best value per row is highlighted green with 🏆.
 */
function renderCompareTable(containerId, slots, onAction) {
    const el = document.getElementById(containerId);
    if (!el) return;

    const pct = (v) => `${v >= 0 ? "+" : ""}${Number(v).toFixed(2)}%`;
    const rows = [
        { label: "Total Return", get: (s) => s.result.metrics.total_return_pct, best: "max", fmt: pct },
        // A slot with nothing closed has no win rate to rank — render "—" and
        // keep it out of the best-per-row contest instead of awarding it 0%.
        { label: "Win Rate",     get: (s) => (s.result.metrics.closed_trades === 0 ? null : s.result.metrics.win_rate_pct),
          best: "max", fmt: (v) => (v == null ? "—" : `${Number(v).toFixed(2)}%`) },
        { label: "Max Drawdown", get: (s) => s.result.metrics.max_drawdown_pct, best: "max", fmt: (v) => `${Number(v).toFixed(2)}%` },
        { label: "Sharpe",       get: (s) => s.result.metrics.sharpe,           best: "max", fmt: (v) => Number(v).toFixed(2) },
        { label: "Total Trades", get: (s) => s.result.metrics.total_trades,     best: null, fmt: (v) => String(v) },
    ];

    // header
    let html = "<thead><tr><th>Metric</th>";
    slots.forEach((s) => {
        html += `<th class="col-head"><span class="slot-dot" style="background:${s.color}"></span> ${s.label}</th>`;
    });
    html += "</tr></thead><tbody>";

    // metric rows with best-per-row
    rows.forEach((r) => {
        let bestIdx = -1, bestVal = null;
        if (r.best) {
            slots.forEach((s, i) => {
                const v = r.get(s);
                if (v == null) return;
                if (bestVal === null || (r.best === "max" ? v > bestVal : v < bestVal)) {
                    bestVal = v; bestIdx = i;
                }
            });
        }
        html += `<tr><td>${r.label}</td>`;
        slots.forEach((s, i) => {
            const v = r.get(s);
            const isBest = i === bestIdx;
            html += `<td class="metric-cell ${isBest ? "best" : ""}">${r.fmt(v)}${isBest ? " 🏆" : ""}</td>`;
        });
        html += "</tr>";
    });

    // actions row
    html += `<tr><td>Actions</td>`;
    slots.forEach((s) => {
        html += `<td><div class="slot-actions-cell">
            <button class="btn" data-act="backtest" data-id="${s.id}">🔍 Backtest</button>
            <button class="btn btn-accent" data-act="forward" data-id="${s.id}">▶ Forward</button>
        </div></td>`;
    });
    html += "</tr></tbody>";
    el.innerHTML = html;

    // wire per-slot actions (Task 3.8)
    el.querySelectorAll("button[data-act]").forEach((btn) => {
        btn.addEventListener("click", () => {
            const slot = slots.find((x) => String(x.id) === btn.dataset.id);
            if (slot && onAction) onAction(slot, btn.dataset.act);
        });
    });
}
