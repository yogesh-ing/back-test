/* Instance deep-dive slide-over drawer (PRD Task 6.3).
 *
 * Opens from any matrix row's 🔍 button and shows, for one runner:
 *   - individual equity curve
 *   - active open positions
 *   - signal reason log (e.g. "RSI=24.2 < 30 on INFY on 15m bar close")
 *   - parameter configuration inspector
 *   - universe symbol list for pool runners
 *   - option book (task C2): open structures with their legs, net premium and
 *     next expiry, plus option-aware closed trades
 */
(function () {
  "use strict";

  const drawer = {
    el: null,
    overlay: null,
    body: null,
    chart: null,
    watchChart: null,
  };

  // Money (components/currency.js, loaded first in base.html) is the single
  // money formatter: symbol/locale come from the <body data-currency-*>
  // attributes, never hard-coded (ticket #10 — the app's currency config is
  // the source of truth; the drawer used to print ₹ for every currency).
  function fmtSigned(n) {
    const v = Math.round(n || 0);
    return Money.signed(v, 0);
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
        // Watch series (PnL-vs-spot, 2026-09-24): live option runners only —
        // the endpoint 404s for unknown ids and returns an empty series for
        // runners without capture, so this is a silent no-op elsewhere.
        return fetch("/api/portfolio/watch/series/" + id)
          .then((r) => r.json())
          .then((watch) => {
            if (watch && watch.success) renderWatchChart(watch.series, watch.label);
          })
          .catch(() => {}); // the drawer must open even if the series fails
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
    if (drawer.watchChart) { drawer.watchChart.destroy(); drawer.watchChart = null; }
  }

  function positionsHtml(r) {
    // C2: an option runner holds structures, not equity positions — those live
    // in the bridge and never appear in `positions`. Render the book instead.
    if (OptionView.isOption(r)) return optionBookHtml(r);
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

  /**
   * The option book (C2): a stat strip (what is open, what it ties up, what it
   * is worth) and one table of open structures with a per-leg breakdown.
   */
  function optionBookHtml(r) {
    const stats = OptionView.bookStats(r);
    const statStrip = stats.length
      ? '<div class="dd-stats dd-stats-4">' + stats.map((s) => {
        let value = s.value;
        if (s.money) value = Money.format(s.value, 0);
        else if (typeof value === "number") value = String(value);
        value += s.sub ? "  (" + s.sub + ")" : "";
        return stat(s.label, value, s.pnl ? pnlClass(s.value) : "");
      }).join("") + "</div>"
      : "";

    const rows = OptionView.structureRows(r);
    if (!rows.length) {
      return '<div class="dd-section"><h4>Options Book</h4>' + statStrip +
        '<p class="muted" style="padding:10px">Flat — no open structures ' +
        '(waiting for a directional view).</p></div>';
    }

    const table = '<table class="dd-table"><thead><tr><th>Structure</th><th>Strikes</th>' +
      '<th class="num">Lots</th><th class="num">Net Premium</th><th class="num">Mark</th>' +
      '<th class="num">P&amp;L</th><th class="num">Expiry</th><th class="num">Bars</th>' +
      '</tr></thead><tbody>' +
      rows.map((s) =>
        '<tr><td>' + esc(s.structure) + ' <span class="muted">(' + esc(s.side) + ')</span></td>' +
        '<td>' + esc(s.strikes) + " " + esc(s.optionType) + '</td>' +
        '<td class="num">' + s.lots + '</td>' +
        '<td class="num">' + s.entryPrice.toFixed(2) + '</td>' +
        '<td class="num">' + s.currentPrice.toFixed(2) + '</td>' +
        '<td class="num ' + pnlClass(s.pnl) + '">' + fmtSigned(s.pnl) +
          '<div class="cell-sub">' + (s.pnlPct * 100).toFixed(1) + '% of premium</div></td>' +
        '<td class="num">' + esc(s.expiryText) + '</td>' +
        '<td class="num">' + s.barsHeld + '</td></tr>' +
        s.legs.map((leg) =>
          '<tr class="opt-leg"><td class="muted">↳ leg</td>' +
          '<td>' + esc(leg.tradingSymbol || "") + '</td>' +
          '<td class="num">' + leg.qty + '</td>' +
          '<td class="num">' + leg.entryPrice.toFixed(2) + '</td>' +
          '<td class="num">' + leg.currentPrice.toFixed(2) + '</td>' +
          '<td class="num ' + pnlClass(leg.pnl) + '">' + fmtSigned(leg.pnl) + '</td>' +
          '<td class="num muted">' + esc(leg.side) + " " + esc(leg.optionType) + '</td>' +
          '<td class="num muted">' + leg.strike.toLocaleString("en-IN") + '</td></tr>').join("")
      ).join("") + "</tbody></table>";

    return '<div class="dd-section"><h4>Options Book</h4>' + statStrip + table + "</div>";
  }

  function tradesHtml(r) {
    if (!r.trades || !r.trades.length) {
      return '<p class="muted" style="padding:10px">No closed trades yet.</p>';
    }
    // C2: option rows are structures — label them, and say *why* they closed
    // (stop / flip / settlement), which the equity columns cannot show.
    return '<table class="dd-table"><thead><tr><th>Symbol</th>' +
      '<th class="num">Qty</th><th class="num">Entry</th><th class="num">Exit</th>' +
      '<th class="num">P&amp;L</th><th>Exit Reason</th></tr></thead><tbody>' +
      r.trades.slice(0, 50).map((t) => {
        const option = OptionView.isOptionTrade(t);
        return "<tr" + (option ? ' class="trade-option"' : "") + ">" +
          "<td>" + esc(option ? (t.label || t.symbol) : t.symbol) +
          (option ? '<div class="cell-sub">' + esc(t.structure_type || "") +
            (t.expiry ? " · exp " + esc(OptionView.expiryLabel(t.expiry)) : "") +
            " · " + t.legs + " legs</div>" : "") + "</td>" +
          "<td class='num'>" + t.qty + "</td>" +
          '<td class="num">' + fmtPrice(t.entry_price) + "</td>" +
          '<td class="num">' + fmtPrice(t.exit_price) + "</td>" +
          '<td class="num ' + pnlClass(t.pnl) + '">' + fmtSigned(t.pnl) + "</td>" +
          "<td>" + esc(option ? OptionView.exitReasonLabel(t.exit_reason)
            : (t.exit_reason || "—")) + "</td></tr>";
      }).join("") + "</tbody></table>";
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

  function fmtPrice(v) {
    const n = Number(v);
    return Number.isFinite(n) ? n.toFixed(2) : "—";
  }

  function paramsHtml(r) {
    const rows = [
      ["Strategy", r.strategy_name],
      ["Timeframe", r.timeframe],
      ["Target type", r.target_type],
      ["Symbols", r.symbol_count + (r.target_type === "SYMBOL_UNIVERSE" ? " (pool)" : "")],
      ["Max pool positions", r.max_pool_positions],
      ["Allocated capital", Money.format(r.allocated_capital || 0, 0)],
      ["Cash", Money.format(r.cash || 0, 0)],
      ["Win rate", ((r.win_rate || 0) * 100).toFixed(1) + "%"],
      ["Max drawdown", ((r.max_drawdown_pct || 0) * 100).toFixed(2) + "%"],
      ["Bars processed", r.bars_processed],
    ];
    // C2: say what the runner actually trades, and for an option runner what
    // its exit plan is — otherwise the Config tab shows nothing option-shaped.
    rows.splice(1, 0, ["Instrument", OptionView.isOption(r) ? "Options" : "Equity"]);
    if (OptionView.isOption(r)) {
      const expr = (r.instrument && r.instrument.expression) || {};
      const exit = expr.exit || {};
      rows.push(["Structure", OptionView.instrumentLabel(r)]);
      rows.push(["Strike selection", String(expr.strike_selection || "atm").toUpperCase() +
        (expr.delta_target ? " (Δ" + expr.delta_target + ")" : "")]);
      rows.push(["Lots per leg", expr.quantity == null ? 1 : expr.quantity]);
      rows.push(["Exit policy", exitSummary(exit)]);
    }
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

  /**
   * Watch chart (2026-09-24): spot vs option MTM PnL, twin axes — the same
   * shape the 15:30 PNG export writes into charts/<date>/. Rendered only
   * when the runner has captured watch points (live mStock option runners);
   * the section is removed entirely for everyone else.
   */
  function renderWatchChart(series, label) {
    if (!series || !series.length) return;
    const sec = $("dd-watch-section");
    if (!sec || typeof Chart === "undefined") return;
    if (drawer.watchChart) { drawer.watchChart.destroy(); drawer.watchChart = null; }

    const labels = series.map((p) => {
      const t = String(p.ts || "");
      // 1-min bars stamp "YYYY-MM-DDTHH:MM:SS" — show HH:MM only.
      return t.length >= 16 ? t.slice(11, 16) : t;
    });
    const spots = series.map((p) => p.spot);
    const pnls = series.map((p) => p.option_pnl);

    sec.hidden = false;
    drawer.watchChart = new Chart($("dd-watch-chart").getContext("2d"), {
      type: "line",
      data: {
        labels: labels,
        datasets: [
          {
            label: "Spot",
            data: spots,
            borderColor: "#38bdf8",
            backgroundColor: "rgba(56,189,248,.08)",
            yAxisID: "y",
            borderWidth: 1.5, pointRadius: 0, tension: 0.2, fill: true,
          },
          {
            label: "Option P&L",
            data: pnls,
            borderColor: "#f87171",
            backgroundColor: "rgba(248,113,113,.10)",
            yAxisID: "y1",
            borderWidth: 1.8, pointRadius: 0, tension: 0.2, fill: true,
          },
        ],
      },
      options: {
        responsive: true, animation: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { labels: { color: "#94a3b8", boxWidth: 12 } },
          tooltip: { callbacks: { label: (c) => c.dataset.label + ": " + Money.signed(Math.round(c.parsed.y), 0) } },
        },
        scales: {
          x: { ticks: { color: "#94a3b8", maxTicksLimit: 10, maxRotation: 0 }, grid: { display: false } },
          y: {
            position: "left", title: { display: true, text: "Spot", color: "#38bdf8" },
            ticks: { color: "#38bdf8" }, grid: { color: "rgba(148,163,184,.1)" },
          },
          y1: {
            position: "right", title: { display: true, text: "P&L", color: "#f87171" },
            ticks: { color: "#f87171" }, grid: { drawOnChartArea: false },
          },
        },
      },
    });
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
      // Watch chart: hidden until the series fetch returns points (live
      // option runners only — synthetic runners never capture, so nobody
      // else ever sees an empty section).
      '<div class="dd-section" id="dd-watch-section" hidden><h4>Spot vs Option P&amp;L' +
      '<span class="muted" style="font-weight:400"> (watch)</span></h4>' +
      '<canvas id="dd-watch-chart" height="140"></canvas></div>' +
      '<div class="dd-section"><h4>' +
      (OptionView.isOption(r) ? "Active Option Book" : "Active Open Positions") +
      "</h4>" + positionsHtml(r) + "</div>" +
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

    // A re-open of the drawer over a previous open must reset the watch
    // section (render() ran before the new series fetch resolved).
    const sec = $("dd-watch-section");
    if (sec) sec.hidden = true;
    if (drawer.watchChart) { drawer.watchChart.destroy(); drawer.watchChart = null; }
  }

  /** Human summary of an option runner's exit plan (mirrors the spawn form). */
  function exitSummary(exit) {
    const bits = [];
    if (exit.signal_flip === false) bits.push("hold through flips");
    if (exit.stop_loss_pct != null) bits.push("stop " + Math.round(exit.stop_loss_pct * 100) + "%");
    if (exit.take_profit_pct != null) bits.push("target " + Math.round(exit.take_profit_pct * 100) + "%");
    if (exit.neutral_bars != null) bits.push("exit after " + exit.neutral_bars + " flat bars");
    if (exit.max_bars != null) bits.push("max " + exit.max_bars + " bars");
    if (exit.min_days_to_expiry === null) bits.push("ride to settlement");
    else if (exit.min_days_to_expiry != null) {
      bits.push("square off " + exit.min_days_to_expiry + "d early");
    }
    if (exit.reenter) bits.push("re-enter on flip");
    return bits.length ? bits.join(", ") : "flip only";
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
