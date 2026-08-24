/**
 * Metrics Cards component (PRD Task 2.7).
 * renderMetricsCards(containerId, metrics)
 */
function renderMetricsCards(containerId, m) {
    const el = document.getElementById(containerId);
    if (!el) return;

    const pnlClass = m.total_pnl >= 0 ? "pos" : "neg";
    // drawdown severity by magnitude (0-5 mild, 5-15 warn, >15 severe)
    const ddAbs = Math.abs(m.max_drawdown_pct);
    const ddSev = ddAbs < 5 ? 1 : ddAbs < 15 ? 2 : 3;

    const cards = [
        { label: "Total P&L", value: fmtMoney(m.total_pnl), cls: pnlClass },
        { label: "Win Rate", value: fmtPct(m.win_rate_pct) },
        { label: "Max Drawdown", value: fmtPct(m.max_drawdown_pct), cls: `dd-sev-${ddSev}` },
        { label: "Sharpe", value: fmtNum(m.sharpe) },
        { label: "Trades", value: String(m.total_trades) },
    ];
    el.innerHTML = cards.map(c =>
        `<div class="metric-card">
            <div class="label">${c.label}</div>
            <div class="value ${c.cls || ""}">${c.value}</div>
        </div>`
    ).join("");
}

function fmtMoney(v) {
    const sign = v < 0 ? "-" : "+";
    return `${sign}$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}
function fmtPct(v) { return `${v.toFixed(2)}%`; }
function fmtNum(v) { return Number(v).toFixed(2); }
