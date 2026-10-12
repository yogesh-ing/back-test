/**
 * Compare Metrics Table (PRD backTest-enhance §4.1) + §5 readiness.
 * renderCompareTable(containerId, slots, onAction)
 *   slots: [{id, label, color, result:{metrics, readiness}} | {id, label, color, error}]
 *   onAction(slot, kind): kind = 'backtest' | 'forward'
 *
 * Failed slots get a column like any other (§4.1). A three-column table after
 * one slot blew up reads as "that is what the comparison was"; keeping the
 * fourth column with its error in a Status row is the difference between an
 * incomplete comparison and a misleading one.
 *
 * Best value per row is highlighted green with 🏆. A failed slot never wins
 * anything — it has no numbers, not zero numbers.
 */
function renderCompareTable(containerId, slots, onAction) {
    const el = document.getElementById(containerId);
    if (!el) return;

    const okSlots = slots.filter((s) => s.result && !s.error);
    // A slot error is server-supplied text that ends up in an attribute, so it
    // is escaped rather than trusted — an unescaped quote or tag there would
    // break the table or, worse, inject markup into the results page.
    const esc = (v) => String(v === null || v === undefined ? "" : v)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
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
        const bad = !s.result || s.error;
        html += `<th class="col-head${bad ? " col-head-failed" : ""}">`
            + `<span class="slot-dot" style="background:${esc(s.color)}"></span> ${esc(s.label)}`
            + (bad ? ' <span class="neg" title="This slot did not run">⚠</span>' : "")
            + "</th>";
    });
    html += "</tr></thead><tbody>";

    // metric rows with best-per-row
    rows.forEach((r) => {
        let bestIdx = -1, bestVal = null;
        if (r.best) {
            okSlots.forEach((s) => {
                const v = r.get(s);
                if (v == null) return;
                if (bestVal === null || (r.best === "max" ? v > bestVal : v < bestVal)) {
                    bestVal = v; bestIdx = s.id;
                }
            });
        }
        html += `<tr><td>${r.label}</td>`;
        slots.forEach((s) => {
            if (!s.result || s.error) { html += `<td class="metric-cell metric-cell-void">—</td>`; return; }
            const v = r.get(s);
            const isBest = String(s.id) === String(bestIdx);
            html += `<td class="metric-cell ${isBest ? "best" : ""}">${r.fmt(v)}${isBest ? " 🏆" : ""}</td>`;
        });
        html += "</tr>";
    });

    // Readiness row (§5) — one compact cell per strategy. The full eight-check
    // breakdown lives on the Backtest page; here the reader's question is
    // comparative ("which of these is actually certifiable?"), so the cell
    // shows the tally and names whatever is not green.
    if (slots.some((s) => s.result && s.result.readiness)) {
        html += "<tr><td>Readiness (§5)</td>";
        slots.forEach((s) => {
            const rd = s.result && s.result.readiness;
            if (!rd) { html += '<td class="metric-cell-void">—</td>'; return; }
            const c = rd.counts || {};
            const icons = [["green", "✅", c.green], ["yellow", "⚠️", c.yellow],
                          ["red", "❌", c.red], ["unknown", "⬜", c.unknown]]
                .filter(([, , n]) => n)
                .map(([, icon, n]) => `${icon}${n}`).join(" ");
            const why = (rd.red_flags || []).concat(rd.unproven || []).join("; ");
            html += `<td class="cmp-ready-cell" title="${esc(why || "All checks passed")}">`
                + `<span class="cmp-ready-tally-inline">${esc(icons)}</span>`
                + `<span class="cmp-ready-why">${esc(why || "")}</span></td>`;
        });
        html += "</tr>";
    }

    // Status row — the visible home of a slot's failure (§4.1)
    if (slots.some((s) => !s.result || s.error)) {
        html += "<tr><td>Status</td>";
        slots.forEach((s) => {
            const bad = !s.result || s.error;
            const msg = bad ? (s.error || "no result returned") : "✓ ran";
            html += `<td class="metric-status ${bad ? "neg" : "pos"}" `
                + `title="${esc(msg)}">`
                + `<span class="metric-status-text">${esc(msg)}</span></td>`;
        });
        html += "</tr>";
    }

    // actions row
    html += `<tr><td>Actions</td>`;
    slots.forEach((s) => {
        if (!s.result || s.error) {
            // No Backtest/Forward for a slot with no result — there is nothing
            // to open. The row stays, empty, so the column is visibly empty
            // rather than offering a button that cannot work.
            html += `<td class="metric-cell-void">—</td>`;
            return;
        }
        html += `<td><div class="slot-actions-cell" style="display: flex; gap: 4px; flex-wrap: wrap;">
            <button class="btn" data-act="backtest" data-id="${s.id}">🔍 Backtest</button>
            <button class="btn btn-accent" data-act="forward" data-id="${s.id}">▶ Forward</button>
            <a href="/analytics" class="btn btn-ghost" style="text-decoration: none; font-size: 0.8rem; padding: 4px 8px;" title="View Live/Paper Performance">📈 Live</a>
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

if (typeof module !== "undefined" && module.exports) module.exports = { renderCompareTable };
