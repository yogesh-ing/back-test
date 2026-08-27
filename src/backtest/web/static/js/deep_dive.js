/* Instance deep-dive slide-over drawer (PRD Task 6.3).
 *
 * Opens from any matrix row's 🔍 button and shows, for one runner:
 *   - individual equity curve
 *   - active open positions
 *   - signal reason log (e.g. "RSI=24.2 < 30 on INFY on 15m bar close")
 *   - parameter configuration inspector
 *   - universe symbol list for pool runners
 */
(function () {
  "use strict";

  const drawer = {
    el: null,
    overlay: null,
    body: null,
    chart: null,
  };

  function fmtSigned(n) {
    const v = Math.round(n || 0);
    return (v >= 0 ? "+₹" : "-₹") + Math.abs(v).toLocaleString("en-IN");
  }
  const pnlClass = (n) => (n > 0 ? "pnl-pos" : n < 0 ? "pnl-neg" : "pnl-flat");
  const $ = (id) => document.getElementById(id);

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function open(id, portfolio) {
    const el = $("deep-dive");
    const overlay = $("drawer-overlay");
    el.hidden = false;
    overlay.hidden = false;
    $("dd-body").innerHTML = '<div class="loader-spinner">Loading instance detail…</div>';
    requestAnimationFrame(() => el.classList.add("open"));

    fetch("/api/portfolio/runner/" + id)
      .then((r) => r.json())
      .then((data) => {
        if (!data.success) throw new Error(data.error || "failed");
        render(data.runner);
      })
      .catch((err) => {
        $("dd-body").innerHTML = '<div class="card-error">Error: ' + esc(err.message) + "</div>";
      });
  }

  function close() {
    const el = $("deep-dive");
    el.classList.remove("open");
    $("drawer-overlay").hidden = true;
    setTimeout(() => { el.hidden = true; }, 200);
    if (drawer.chart) { drawer.chart.destroy(); drawer.chart = null; }
  }

  function positionsHtml(r) {
    if (!r.positions || !r.positions.length) {
      return '<p class="muted" style="padding:10px">No open positions (flat).</p>';
    }
    return '<table class="dd-table"><thead><tr><th>Symbol</th><th>Side</th>' +
      '<th class="num">Qty</th><th class="num">Entry</th><th class="num">Current</th>' +
      '<th class="num">P&amp;L</th></tr></thead><tbody>' +
      r.positions.map((p) =>
        "<tr><td>" + esc(p.symbol) + "</td><td>" + esc(p.side) + "</td>" +
        '<td class="num">' + (p.qty) + "</td>" +
        '<td class="num">' + (p.entry_price) + "</td>" +
        '<td class="num">' + (p.current_price) + "</td>" +
        '<td class="num ' + pnlClass(p.unrealized_pnl) + '">' + fmtSigned(p.unrealized_pnl) +
        "</td></tr>").join("") + "</tbody></table>";
  }

  function tradesHtml(r) {
    if (!r.trades || !r.trades.length) {
      return '<p class="muted" style="padding:10px">No closed trades yet.</p>';
    }
    return '<table class="dd-table"><thead><tr><th>Symbol</th><th class="num">Qty</th>' +
      '<th class="num">Entry</th><th class="num">Exit</th><th class="num">P&amp;L</th></tr></thead><tbody>' +
      r.trades.slice(0, 50).map((t) =>
        "<tr><td>" + esc(t.symbol) + "</td><td class='num'>" + t.qty + "</td>" +
        '<td class="num">' + (t.entry_price) + "</td>" +
        '<td class="num">' + (t.exit_price) + "</td>" +
        '<td class="num ' + pnlClass(t.pnl) + '">' + fmtSigned(t.pnl) + "</td></tr>").join("") +
      "</tbody></table>";
  }

  function signalsHtml(r) {
    if (!r.signals || !r.signals.length) {
      return '<p class="muted" style="padding:10px">No signals logged yet.</p>';
    }
    return '<div class="signal-log">' + r.signals.slice(0, 60).map((s) =>
      '<div class="signal-line signal-' + esc((s.kind || "info").toLowerCase()) + '">' +
      '<span class="signal-kind">' + esc(s.kind) + "</span>" +
      '<span class="signal-symbol">' + esc(s.symbol) + "</span>" +
      '<span class="signal-reason">' + esc(s.reason) + "</span></div>").join("") +
      "</div>";
  }

  function paramsHtml(r) {
    const rows = [
      ["Strategy", r.strategy_name],
      ["Timeframe", r.timeframe],
      ["Target type", r.target_type],
      ["Symbols", r.symbol_count + (r.target_type === "SYMBOL_UNIVERSE" ? " (pool)" : "")],
      ["Max pool positions", r.max_pool_positions],
      ["Allocated capital", "₹" + (r.allocated_capital || 0).toLocaleString("en-IN")],
      ["Cash", "₹" + (r.cash || 0).toLocaleString("en-IN")],
      ["Win rate", ((r.win_rate || 0) * 100).toFixed(1) + "%"],
      ["Max drawdown", ((r.max_drawdown_pct || 0) * 100).toFixed(2) + "%"],
      ["Bars processed", r.bars_processed],
    ];
    const paramRows = Object.entries(r.params || {}).map(([k, v]) =>
      "<tr><td class='muted'>param." + esc(k) + "</td><td>" + esc(JSON.stringify(v)) + "</td></tr>");
    return '<table class="dd-table dd-kv"><tbody>' +
      rows.map(([k, v]) =>
        "<tr><td class='muted'>" + esc(k) + "</td><td>" + esc(v) + "</td></tr>").join("") +
      paramRows.join("") + "</tbody></table>";
  }

  function universeHtml(r) {
    if (r.target_type !== "SYMBOL_UNIVERSE") return "";
    return '<div class="dd-section"><h4>Universe Symbols (' + r.universe_symbols.length + ")</h4>" +
      '<div class="symbol-chips">' + r.universe_symbols.map((s) =>
        '<span class="chip' + (r.positions.some((p) => p.symbol === s) ? " chip-active" : "") +
        '">' + esc(s) + "</span>").join("") + "</div></div>";
  }

  function render(r) {
    $("dd-title").textContent = r.name;
    $("dd-subtitle").textContent = r.strategy_name + " · " + r.target_label +
      " · " + r.timeframe + " · " + r.status;

    $("dd-body").innerHTML =
      '<div class="dd-stats">' +
      stat("Equity", "₹" + Math.round(r.equity || 0).toLocaleString("en-IN")) +
      stat("Daily P&L", fmtSigned(r.daily_pnl), pnlClass(r.daily_pnl)) +
      stat("Open P&L", fmtSigned(r.open_pnl), pnlClass(r.open_pnl)) +
      stat("Realized", fmtSigned(r.realized_pnl), pnlClass(r.realized_pnl)) +
      stat("Positions", r.open_positions) +
      stat("Win Rate", ((r.win_rate || 0) * 100).toFixed(0) + "%") +
      "</div>" +
      '<div class="dd-section"><h4>Instance Equity Curve</h4>' +
      '<canvas id="dd-equity-chart" height="120"></canvas></div>' +
      '<div class="dd-section"><h4>Active Open Positions</h4>' + positionsHtml(r) + "</div>" +
      '<div class="dd-tabs"><button class="tab active" data-ddtab="signals" type="button">Signal Log</button>' +
      '<button class="tab" data-ddtab="trades" type="button">Trades</button>' +
      '<button class="tab" data-ddtab="params" type="button">Config</button></div>' +
      '<div class="dd-tabpanel" data-ddpanel="signals">' + signalsHtml(r) + "</div>" +
      '<div class="dd-tabpanel" data-ddpanel="trades" hidden>' + tradesHtml(r) + "</div>" +
      '<div class="dd-tabpanel" data-ddpanel="params" hidden>' + paramsHtml(r) + "</div>" +
      universeHtml(r);

    // Equity chart
    const canvas = $("dd-equity-chart");
    if (canvas && typeof Chart !== "undefined" && r.equity_curve && r.equity_curve.length) {
      drawer.chart = new Chart(canvas.getContext("2d"), {
        type: "line",
        data: {
          labels: r.equity_curve.map((_, i) => i),
          datasets: [{
            label: "Equity",
            data: r.equity_curve.map((e) => e.equity),
            borderColor: "#8b5cf6",
            backgroundColor: "rgba(139,92,246,.12)",
            fill: true, borderWidth: 2, pointRadius: 0, tension: 0.25,
          }],
        },
        options: {
          responsive: true, animation: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { display: false },
            y: { ticks: { color: "#94a3b8" }, grid: { color: "rgba(148,163,184,.1)" } },
          },
        },
      });
    }

    // Sub tabs
    document.querySelectorAll("[data-ddtab]").forEach((b) =>
      b.addEventListener("click", () => {
        document.querySelectorAll("[data-ddtab]").forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
        document.querySelectorAll("[data-ddpanel]").forEach((p) => {
          p.hidden = p.dataset.ddpanel !== b.dataset.ddtab;
        });
      }));
  }

  function stat(label, value, cls) {
    return '<div class="dd-stat"><div class="dd-stat-label">' + esc(label) + "</div>" +
      '<div class="dd-stat-value ' + (cls || "") + '">' + esc(value) + "</div></div>";
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("dd-close").addEventListener("click", close);
    $("drawer-overlay").addEventListener("click", close);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") close();
    });
  });

  window.DeepDive = { open, close };
})();
