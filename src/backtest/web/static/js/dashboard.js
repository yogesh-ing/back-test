/**
 * Dashboard landing page — aggregates cross-page state.
 * Shows strategy count/list + live forward-bot status, plus workflow nav cards.
 */
const $ = (id) => document.getElementById(id);

async function fetchJSON(url) {
    const r = await fetch(url);
    if (!r.ok) return null;
    try { return await r.json(); } catch { return null; }
}

async function loadStrategies() {
    const list = await fetchJSON("/api/strategies");
    if (!list) { $("strategyList").innerHTML = '<span class="muted small">unavailable</span>'; return; }
    $("strategyCount").textContent = String(list.length);
    $("strategyList").innerHTML = list.length
        ? list.map((s) => `<span class="strategy-chip">${s.name}</span>`).join("")
        : '<span class="muted small">none</span>';
}

function fmtPnl(v, metrics) {
    const cls = v >= 0 ? "pos" : "neg";
    const text = (typeof Money !== "undefined") ? Money.signed(v)
        : `${v >= 0 ? "+" : "-"}$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
    const detail = metrics && metrics.closed_trades === 0 ? ' <span class="muted small">nothing closed</span>' : "";
    return `<span class="${cls}">${text}</span>${detail}`;
}

async function refreshBot() {
    const s = await fetchJSON("/api/forward/status");
    if (!s || s.status === "idle") {
        $("botBadge").textContent = "Idle";
        $("botBadge").className = "status-badge status-idle";
        $("botSymbol").textContent = "—";
        $("botPnl").innerHTML = "—";
        $("botDetail").textContent = "No active bot. Promote a backtest result to Forward Test and click Start.";
        $("botProgressTrack").hidden = true;
        return;
    }
    $("botBadge").textContent = s.status === "running" ? "Running" : "Stopped";
    $("botBadge").className = `status-badge status-${s.status}`;
    $("botSymbol").textContent = (s.config && s.config.symbol) || "—";
    $("botPnl").innerHTML = fmtPnl(s.metrics.total_pnl, s.metrics);
    const p = s.progress || {};
    $("botDetail").innerHTML =
        `Strategy <strong>${(s.config && s.config.strategy) || "—"}</strong> · ` +
        `${s.metrics.total_trades} trades · ${p.pct || 0}% replayed · win rate ` +
        (s.metrics.closed_trades === 0 ? "— (nothing closed)" : `${s.metrics.win_rate_pct}%`);
    if (s.status === "running" && p.total) {
        $("botProgressTrack").hidden = false;
        $("botProgressBar").style.width = `${p.pct}%`;
    } else {
        $("botProgressTrack").hidden = true;
    }
}

async function init() {
    await loadStrategies();
    await refreshBot();
    setInterval(refreshBot, 3000);   // live cross-page bot status
}

document.addEventListener("DOMContentLoaded", init);
