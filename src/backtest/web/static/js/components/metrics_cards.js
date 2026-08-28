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

    // Win rate is measured over CLOSED trades, so "0.00%" before anything has
    // closed would read as a losing strategy rather than "no result yet" (G1).
    const closed = typeof m.closed_trades === "number" ? m.closed_trades : null;
    const open = typeof m.open_trades === "number" ? m.open_trades : 0;
    const winRate = closed === 0 ? "—" : fmtPct(m.win_rate_pct);
    const winRateHint = closed === null ? "" :
        (closed === 0 ? "nothing closed yet" : `${closed} closed${open ? ` · ${open} open` : ""}`);

    const cards = [
        { label: "Total P&L", value: fmtMoney(m.total_pnl), cls: pnlClass },
        { label: "Win Rate", value: winRate, sub: winRateHint },
        { label: "Max Drawdown", value: fmtPct(m.max_drawdown_pct), cls: `dd-sev-${ddSev}` },
        { label: "Sharpe", value: fmtNum(m.sharpe) },
        { label: "Trades", value: String(m.total_trades), sub: open ? `${open} still open` : "" },
    ];
    el.innerHTML = cards.map(c =>
        `<div class="metric-card">
            <div class="label">${c.label}</div>
            <div class="value ${c.cls || ""}">${c.value}</div>
            ${c.sub ? `<div class="metric-sub">${c.sub}</div>` : ""}
        </div>`
    ).join("");
}

function fmtMoney(v) {
    // Single money formatter for the app (₹ default) — this used to hard-code "$".
    return (typeof Money !== "undefined")
        ? Money.signed(v)
        : `${v < 0 ? "-" : "+"}$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}
function fmtPct(v) { return `${v.toFixed(2)}%`; }
function fmtNum(v) { return Number(v).toFixed(2); }
