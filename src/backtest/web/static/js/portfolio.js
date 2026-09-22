/* Portfolio Command Center — high-density multi-strategy matrix (PRD Phase 6).
 *
 * Live state arrives over Server-Sent Events (/api/portfolio/stream) as a
 * 1-second JSON snapshot; the matrix/search/sort render from that snapshot so
 * 50+ rows stay smooth without polling jank.
 */
(function () {
  "use strict";

  const state = {
    portfolio: null,
    search: "",
    filter: "all",
    sort: "default",
    tab: "equity",
    chart: null,
    audit: [],
    backendAudit: [],
  };



  // ---------------------------------------------------------------- helpers
  const $ = (id) => document.getElementById(id);

  // Bucket scope (ticket P4.1): /portfolio/paper and /portfolio/live set
  // data-mode on #portfolio-page; the combined landing leaves it empty.
  const PAGE_MODE = $("portfolio-page") ? $("portfolio-page").dataset.mode || "" : "";

  // T2.1: Derive scoped metrics from embedded bucket aggregates (C4).
  // When PAGE_MODE is set, the metric cards show THAT bucket's numbers,
  // not the combined total.  This is the single source of truth (AC-15).
  function bucketMetrics(p) {
    if (!PAGE_MODE || !p.buckets || !p.buckets[PAGE_MODE]) return p;
    const b = p.buckets[PAGE_MODE];
    // Merge bucket aggregate into the top-level shape that renderMetrics expects.
    return Object.assign({}, p, {
      total_capital: b.capital,
      total_equity: b.equity,
      deployed_capital: b.deployed_capital,
      deployed_pct: b.capital > 0 ? b.deployed_capital / b.capital : 0,
      daily_pnl: b.daily_pnl,
      daily_pnl_pct: b.daily_pnl_pct,
      realized_pnl: b.realized_pnl,
      open_positions: b.open_positions,
      runner_count: b.count,
      running: b.running,
      paused: b.paused,
      halted: b.halted,
      halt_reason: b.halt_reason,
      peak_equity: b.peak_equity,
      drawdown_pct: b.drawdown_pct,
      daily_loss_used: b.daily_loss_used,
      daily_loss_pct: b.daily_loss_pct,
    });
  }
  const fmtMoney = (n, currency) => {
    const c = currency || "₹";
    const sign = n < 0 ? "-" : "";
    return sign + c + Math.abs(Math.round(n || 0)).toLocaleString("en-IN");
  };
  const fmtSigned = (n) => {
    const v = Math.round(n || 0);
    return (v >= 0 ? "+₹" : "-₹") + Math.abs(v).toLocaleString("en-IN");
  };
  const pnlClass = (n) => (n > 0 ? "pnl-pos" : n < 0 ? "pnl-neg" : "pnl-flat");
  const pct = (x) => ((x || 0) * 100).toFixed(2) + "%";

  function toast(msg, kind) {
    // Reuse the platform toast helper when present.
    if (window.showToast) return window.showToast(msg, kind);
    console.log("[toast]", msg);
  }

  async function api(url, method, body) {
    const opts = { method: method || "GET", headers: { "Content-Type": "application/json" } };
    if (body) opts.body = JSON.stringify(body);
    const res = await fetch(url, opts);
    let data = null;
    try { data = await res.json(); } catch (e) { data = {}; }
    if (!res.ok || data.success === false) {
      throw new Error(data.error || ("HTTP " + res.status));
    }
    return data;
  }

  function addAudit(message, kind) {
    state.audit.unshift({
      ts: new Date().toLocaleTimeString(),
      message,
      kind: kind || "info",
      live: true,
    });
    state.audit = state.audit.slice(0, 200);
    renderAudit();
  }

  // GAP-1 fix (2026-09-21): the tab used to render ONLY browser-session
  // events and showed "Waiting for activity…" forever. It now fetches the
  // backend's real audit trail ([AUDIT] entries from PortfolioManager) every
  // time the tab opens, and keeps live addAudit events on top.
  async function fetchBackendAudit() {
    try {
      const d = await api("/api/portfolio/audit?scope=all&limit=200");
      state.backendAudit = (d.audit || []).map((e) => ({
        ts: (e.ts || "").replace("T", " ").slice(5, 19),
        message:
          (e.action || "") +
          (e.instance_id ? " [" + e.instance_id.slice(0, 8) + "]" : "") +
          (e.detail ? " — " + e.detail : ""),
        kind: e.scope === "live" ? "danger" : e.action && e.action.toUpperCase().startsWith("SPAWN") ? "spawn" : "action",
        scope: e.scope,
      }));
    } catch (_e) {
      state.backendAudit = [];
    }
    renderAudit();
  }

  // ---------------------------------------------------------------- metrics
  function renderMetrics(p) {
    $("m-total-capital").textContent = fmtMoney(p.total_capital);
    $("m-total-equity").textContent = fmtMoney(p.total_equity);
    $("m-deployed").textContent = fmtMoney(p.deployed_capital) +
      " (" + pct(p.deployed_pct) + ")";
    $("m-deployed-bar").style.width = Math.min(100, (p.deployed_pct || 0) * 100) + "%";

    const daily = $("m-daily-pnl");
    daily.textContent = fmtSigned(p.daily_pnl);
    daily.className = "metric-value " + pnlClass(p.daily_pnl);
    $("m-daily-pnl-pct").textContent = (p.daily_pnl >= 0 ? "+" : "") +
      ((p.daily_pnl_pct || 0) * 100).toFixed(2) + "%";

    const real = $("m-realized-pnl");
    real.textContent = fmtSigned(p.realized_pnl);
    real.className = "metric-value " + pnlClass(p.realized_pnl);

    $("m-positions").textContent = p.open_positions + " active";
    $("m-runner-count").textContent =
      p.runner_count + " instances · " + p.running + " running · " +
      p.paused + " paused";

    const lossPct = Math.min(100, (p.daily_loss_pct || 0) * 100);
    $("m-loss-bar").style.width = lossPct + "%";
    $("m-loss-text").textContent =
      fmtMoney(p.daily_loss_used) + " / " + fmtMoney(p.daily_loss_limit) +
      " (" + lossPct.toFixed(1) + "%)";
  }

  // ---------------------------------------------------------------- banner
  function renderBanner(p) {
    const banner = $("cb-banner");
    if (p.halted) {
      banner.hidden = false;
      $("cb-reason").textContent = p.halt_reason || "Risk limit breached";
    } else {
      banner.hidden = true;
    }

    const wb = $("warning-banner");
    if (p.warnings && p.warnings.length) {
      wb.hidden = false;
      wb.innerHTML = p.warnings.map((w) =>
        '<div class="warning-line">⚠️ ' + w.message + "</div>").join("");
    } else {
      wb.hidden = true;
    }
  }

  // ---------------------------------------------------------------- matrix
  const STATUS_DOT = { RUNNING: "🟢", PAUSED: "🟡", STOPPED: "⚫", ERROR: "🔴" };

  function filteredRunners(p) {
    let rows = p.runners.slice();
    const q = state.search.trim().toLowerCase();
    if (q) {
      rows = rows.filter((r) =>
        r.name.toLowerCase().includes(q) ||
        r.strategy_name.toLowerCase().includes(q) ||
        r.target_label.toLowerCase().includes(q) ||
        r.symbols.some((s) => s.toLowerCase().includes(q)));
    }
    if (state.filter !== "all") rows = rows.filter((r) => r.status === state.filter);

    const key = state.sort.replace(/^-/, "");
    const sorters = {
      daily_pnl: (a, b) => b.daily_pnl - a.daily_pnl,
      open_pnl: (a, b) => b.open_pnl - a.open_pnl,
      allocated: (a, b) => b.allocated_capital - a.allocated_capital,
      name: (a, b) => a.name.localeCompare(b.name),
      status: (a, b) => a.status.localeCompare(b.status),
    };
    if (sorters[key]) rows.sort(sorters[key]);
    return rows;
  }

  function rowActions(r) {
    let html = "";
    if (r.status === "RUNNING") {
      html += '<button class="row-btn" data-act="pause" data-id="' + r.instance_id + '" title="Pause">⏸</button>';
    } else if (r.status === "PAUSED") {
      html += '<button class="row-btn" data-act="resume" data-id="' + r.instance_id + '" title="Resume">▶</button>';
    }
    html += '<button class="row-btn row-btn-stop" data-act="stop" data-id="' + r.instance_id + '" title="Stop">⏹</button>';
    html += '<button class="row-btn" data-act="deep_dive" data-id="' + r.instance_id + '" title="Deep dive">🔍</button>';
    return html;
  }

  // Mode/source badge (ticket P4.2) — mirrors templates/_macros.html::badge.
  // The text carries the value; the colour is only a supplement.
  const SOURCE_LABELS = { synthetic: "SYNTH", mstock: "MSTOCK", replay: "REPLAY" };
  function badgeHtml(mode, source) {
    const m = (mode || "paper").toUpperCase();
    const raw = (source || "synthetic").toLowerCase();
    const s = (SOURCE_LABELS[raw] || raw).toUpperCase();
    const cls = mode === "live" ? "badge-live" : "badge-paper";
    return '<span class="badge ' + cls + '">' + m + "/" + s + "</span>";
  }

  function renderMatrix(p) {
    const body = $("matrix-body");
    const rows = filteredRunners(p);
    $("matrix-empty").hidden = p.runners.length !== 0;

    body.innerHTML = rows.map((r, i) => {
      const typeBadge = r.target_type === "SINGLE_SYMBOL"
        ? '<span class="badge badge-single">Single</span>'
        : '<span class="badge badge-pool">Pool(' + r.symbol_count + ')</span>';
      // C2: an option runner gets option-shaped cells — the structure it is
      // holding, the legs, the premium tied up and the next expiry. Equity
      // rows are untouched (every helper returns "" / null for them).
      const option = OptionView.isOption(r);
      const instrument = option
        ? '<span class="badge badge-option">' + OptionView.instrumentLabel(r) + '</span>'
        : typeBadge;
      const posCell = OptionView.positionsCell(r);
      const notes = OptionView.matrixNotes(r);
      const targetCell = r.target_label +
        notes.map((n) => '<div class="cell-sub">' + n + '</div>').join("");
      return (
        '<tr class="matrix-row' + (option ? " matrix-row-option" : "") +
          ' status-' + r.status.toLowerCase() + '">' +
        '<td>' + (i + 1) + '</td>' +
        '<td class="cell-name">' + r.name +
          '<div class="cell-sub">' + r.strategy_name + '</div></td>' +
        '<td>' + targetCell + '</td>' +
        '<td>' + instrument + '</td>' +
        '<td>' + badgeHtml(r.mode, r.source) + '</td>' +
        '<td>' + r.timeframe + '</td>' +
        '<td class="num">' + fmtMoney(r.allocated_capital) + '</td>' +
        '<td class="num ' + pnlClass(r.open_pnl) + '">' + fmtSigned(r.open_pnl) + '</td>' +
        '<td class="num ' + pnlClass(r.daily_pnl) + '">' + fmtSigned(r.daily_pnl) + '</td>' +
        '<td class="num">' + posCell.primary +
          (posCell.sub ? '<div class="cell-sub">' + posCell.sub + '</div>' : "") + '</td>' +
        '<td><span class="status-cell">' + (STATUS_DOT[r.status] || "⚪") + " " + r.status + "</span>" +
          (r.error ? '<div class="cell-sub cell-error" title="' + (r.error || "") + '">⚠ risk halt</div>' : "") +
        '</td>' +
        '<td><div class="row-actions">' + rowActions(r) + "</div></td>" +
        "</tr>"
      );
    }).join("");
  }

  // ---------------------------------------------------------------- tabs
  function renderAggregatePositions(p) {
    const tbody = $("aggregate-positions");
    // Row-level aggregate: open count + open P&L per runner; per-symbol detail
    // is available via each runner's deep-dive drawer.
    // C2: an option runner holds whole structures, not equity tickets — show
    // each leg (trading symbol · strike · side) rather than three dashes, and
    // fall back to one summarising row when the leg detail is unavailable.
    // Also include dashboard book (second book) — fix for invisible trades.
    const rows = [];
    const legPrice = (v) => (v == null ? "—" : Number(v).toFixed(2));

    // Dashboard book (manual options tab) — now visible in portfolio
    if (p.dashboard_book && p.dashboard_book.exists && p.dashboard_book.positions && p.dashboard_book.positions.length) {
      const db = p.dashboard_book;
      (db.positions || []).forEach((pos) => {
        rows.push(
          '<tr class="matrix-row-option"><td>Manual Book</td><td>' + (pos.trading_symbol || "") + '</td><td>' + (pos.side || "") + '</td>' +
          '<td class="num">' + (pos.quantity || 0) + ' × ' + (pos.lot_size || 0) + '</td>' +
          '<td class="num">' + legPrice(pos.entry_price) + '</td>' +
          '<td class="num">' + legPrice(pos.current_price) + '</td>' +
          '<td class="num ' + (pos.unrealized_pnl >= 0 ? "pnl-pos" : "pnl-neg") + '">' + (pos.unrealized_pnl ? "₹" + Math.round(pos.unrealized_pnl).toLocaleString("en-IN") : "—") +
          ' <span class="muted">(' + (pos.underlying || "") + ' ' + (pos.strike || "") + ' ' + (pos.option_type || "") + ')</span></td></tr>');
      });
    }

    p.runners.filter((r) => r.open_positions > 0).forEach((r) => {
      const structures = OptionView.openStructures(r);
      if (!structures.length) {
        rows.push(
          '<tr><td>' + r.name + '</td><td>' + r.target_label + '</td><td>' +
          (OptionView.isOption(r) ? "OPTION" : "LONG") + '</td>' +
          '<td class="num">—</td><td class="num">—</td><td class="num">—</td>' +
          '<td class="num ' + pnlClass(r.open_pnl) + '">' + fmtSigned(r.open_pnl) +
          ' <span class="muted">(' + r.open_positions + ' pos)</span></td></tr>');
        return;
      }
      structures.forEach((s) => {
        const legs = (s.legs_detail && s.legs_detail.length) ? s.legs_detail : null;
        if (!legs) {
          rows.push(
            '<tr><td>' + r.name + '</td><td>' + s.symbol + '</td><td>' + s.side + '</td>' +
            '<td class="num">' + s.units + '</td>' +
            '<td class="num">' + legPrice(s.entry_price) + '</td>' +
            '<td class="num">' + legPrice(s.current_price) + '</td>' +
            '<td class="num ' + pnlClass(s.unrealized_pnl) + '">' + fmtSigned(s.unrealized_pnl) +
            '</td></tr>');
          return;
        }
        legs.forEach((leg, idx) => {
          rows.push(
            '<tr' + (idx === 0 ? ' class="opt-first-leg"' : "") + '>' +
            '<td>' + (idx === 0 ? r.name : "") + '</td>' +
            '<td>' + (leg.trading_symbol || s.symbol) + '</td>' +
            '<td>' + leg.side + '</td>' +
            '<td class="num">' + leg.qty + '</td>' +
            '<td class="num">' + legPrice(leg.entry_price) + '</td>' +
            '<td class="num">' + legPrice(leg.current_price) + '</td>' +
            '<td class="num ' + pnlClass(leg.pnl) + '">' + fmtSigned(leg.pnl) + '</td></tr>');
        });
        rows.push(
          '<tr class="opt-total"><td></td><td>' + s.symbol + ' (net)</td><td>' + s.side +
          '</td><td class="num">' + s.qty + ' lot' + (s.qty === 1 ? "" : "s") + '</td>' +
          '<td class="num">' + legPrice(s.entry_price) + '</td>' +
          '<td class="num">' + legPrice(s.current_price) + '</td>' +
          '<td class="num ' + pnlClass(s.unrealized_pnl) + '">' + fmtSigned(s.unrealized_pnl) +
          ' <span class="muted">exp ' + OptionView.expiryLabel(s.expiry) + '</span></td></tr>');
      });
    });
    tbody.innerHTML = rows.join("") ||
      '<tr><td colspan="7" class="muted" style="padding:16px">No open positions.</td></tr>';
  }

  function renderAudit() {
    const el = $("audit-log");
    if (!el) return;
    const live = state.audit || [];
    const backend = state.backendAudit || [];
    const merged = live.concat(backend);
    el.innerHTML = merged.map((a) =>
      '<div class="audit-line audit-' + a.kind + '"><span class="audit-ts">' + a.ts +
      "</span><span>" + a.message + "</span></div>").join("") ||
      '<p class="muted" style="padding:12px">No audit entries yet.</p>';
  }

  // ---------------------------------------------------------------- chart
  // Owner decision 2026-09-21: the equity view is ON-DEMAND, not a ticking
  // line. "Refresh snapshot" fetches /api/portfolio/equity/snapshot once and
  // renders (a) session summary stats, (b) day-close series, (c) runner
  // comparison. The SSE stream no longer appends equity points per frame.
  function renderChart(p) {
    if (state.tab !== "equity") return;
    if (state.equitySnapshot) renderEquitySnapshot(state.equitySnapshot);
  }

  async function refreshEquitySnapshot() {
    const url =
      "/api/portfolio/equity/snapshot" + (PAGE_MODE ? "?mode=" + PAGE_MODE : "");
    try {
      const d = await api(url);
      state.equitySnapshot = d.snapshot;
      renderEquitySnapshot(d.snapshot);
    } catch (err) {
      toast("Snapshot failed: " + err.message, "error");
    }
  }

  function renderEquitySnapshot(snap) {
    const canvas = $("portfolio-equity-chart");
    const statsEl = $("equity-session-stats");
    const runnerEl = $("equity-runner-table");
    if (!canvas || !statsEl || !runnerEl) return;

    // (a) Session summary card
    const s = snap.session || {};
    statsEl.hidden = false;
    statsEl.innerHTML =
      '<div class="equity-stat"><span>Day start</span><strong>' + fmtMoney(s.day_start_equity) +
      '</strong></div><div class="equity-stat"><span>Equity now</span><strong>' + fmtMoney(s.equity) +
      '</strong></div><div class="equity-stat"><span>Net today</span><strong class="' + pnlClass(s.net_today) + '">' + fmtSigned(s.net_today) +
      '</strong></div><div class="equity-stat"><span>Realized (all)</span><strong>' + fmtSigned(s.realized_total) +
      '</strong></div><div class="equity-stat"><span>Drawdown</span><strong>' + pct(s.drawdown_pct) +
      '</strong></div><div class="equity-stat"><span>Trades / Win rate</span><strong>' + (s.trades_total || 0) +
      " / " + (s.win_rate == null ? "—" : (s.win_rate * 100).toFixed(1) + "%") + "</strong></div>";

    // (b) Chart: day closes (one point per day); fall back to today's
    // intraday when the book has seen a single session.
    let labels = [], data = [], label = "";
    if (snap.day_closes && snap.day_closes.length > 1) {
      labels = snap.day_closes.map((d) => d.date);
      data = snap.day_closes.map((d) => d.equity);
      label = "Equity at day close";
    } else if (snap.intraday_today && snap.intraday_today.length > 1) {
      labels = snap.intraday_today.map((pt) => (pt.ts || "").slice(11, 16));
      data = snap.intraday_today.map((pt) => pt.equity);
      label = "Today's session";
    }
    if (state.chart) { state.chart.destroy(); state.chart = null; }
    if (labels.length && typeof Chart !== "undefined") {
      state.chart = new Chart(canvas.getContext("2d"), {
        type: "line",
        data: { labels, datasets: [{ label, data, borderColor: "#3b82f6", backgroundColor: "rgba(59,130,246,.12)", fill: true, borderWidth: 2, pointRadius: 2, tension: 0.25 }] },
        options: {
          responsive: true, animation: false,
          plugins: { legend: { labels: { color: "#94a3b8" } } },
          scales: { x: { ticks: { color: "#94a3b8" }, grid: { color: "rgba(148,163,184,.1)" } }, y: { ticks: { color: "#94a3b8" }, grid: { color: "rgba(148,163,184,.1)" } } },
        },
      });
    } else {
      canvas.replaceWith(canvas.cloneNode(false));
    }

    // (c) Runner comparison table
    const rows = (snap.per_runner || []).map((r) =>
      '<tr><td>' + r.name + '</td><td>' + r.strategy + '</td><td>' + r.target +
      '</td><td class="num">' + fmtMoney(r.equity) + '</td><td class="num ' + pnlClass(r.realized_pnl) + '">' + fmtSigned(r.realized_pnl) +
      '</td><td class="num ' + pnlClass(r.daily_pnl) + '">' + fmtSigned(r.daily_pnl) +
      '</td><td class="num">' + (r.win_rate == null ? "—" : (r.win_rate * 100).toFixed(1) + "%") +
      '</td><td class="num">' + r.trades_today + " / " + r.trades + "</td></tr>"
    );
    runnerEl.innerHTML = rows.join("") ||
      '<tr><td colspan="8" class="muted" style="padding:16px">No runners in this bucket.</td></tr>';
  }

  // ---------------------------------------------------------------- dashboard book banner
  function renderDashboardBookBanner(p) {
    const banner = $("dashboard-book-banner");
    const countEl = $("db-book-count");
    if (!banner || !countEl) return;
    const db = p.dashboard_book;
    if (db && db.exists && db.open_structures > 0) {
      banner.hidden = false;
      countEl.textContent = `${db.open_structures} structure(s), ${db.open_positions} leg(s) — Equity ₹${Math.round(db.equity).toLocaleString("en-IN")} (from Options tab)`;
    } else {
      banner.hidden = true;
    }
  }

  // ---------------------------------------------------------------- render
  function render(p) {
    // SSE broadcasts the combined snapshot — drop other buckets on a scoped page.
    if (PAGE_MODE) p.runners = (p.runners || []).filter((r) => (r.mode || "paper") === PAGE_MODE);
    state.portfolio = p;
    // T2.1: Metrics use bucket-scoped data when PAGE_MODE is set.
    renderMetrics(bucketMetrics(p));
    renderBanner(p);
    renderMatrix(p);
    renderAggregatePositions(p);
    renderChart(p);
    renderDashboardBookBanner(p);
    // T2.5: Hide Emergency Flatten on Paper page (paper = no real money at risk).
    const emergencyBtn = $("btn-emergency");
    if (emergencyBtn) emergencyBtn.hidden = (PAGE_MODE === "paper");
    // C6: Capability-driven banner on Live page.
    const capBanner = $("capability-banner");
    if (capBanner && PAGE_MODE === "live" && p.capability) {
      capBanner.style.display = "block";
      if (p.capability.broker_connected) {
        capBanner.textContent = "🔴 " + p.capability.live_banner + " — broker connected, real fills active";
        capBanner.className = "capability-banner capability-live";
      } else {
        capBanner.textContent = "📋 " + p.capability.live_banner + " — connect broker to enable real fills";
        capBanner.className = "capability-banner capability-sim";
      }
    }
  }

  // ---------------------------------------------------------------- SSE
  function connectStream() {
    const es = new EventSource("/api/portfolio/stream");
    es.addEventListener("portfolio", (ev) => {
      try {
        const p = JSON.parse(ev.data);
        $("feed-dot").textContent = "🟢";
        // T2.3: Show bucket-scoped runner count when PAGE_MODE is set.
        const runnerCount = PAGE_MODE && p.buckets && p.buckets[PAGE_MODE]
          ? p.buckets[PAGE_MODE].count
          : p.runner_count;
        const runningCount = PAGE_MODE && p.buckets && p.buckets[PAGE_MODE]
          ? p.buckets[PAGE_MODE].running
          : p.running;
        $("feed-label").textContent =
          (PAGE_MODE ? PAGE_MODE.charAt(0).toUpperCase() + PAGE_MODE.slice(1) + " · " : "Live · ") +
          runnerCount + " runners · " + runningCount + " running · " +
          (p.tick || 0) + " ticks · " + p.fill_count + " fills";
        render(p);
      } catch (e) { /* ignore malformed frame */ }
    });
    es.addEventListener("error", () => {
      $("feed-dot").textContent = "🔴";
      $("feed-label").textContent = "Feed disconnected — retrying…";
    });
    es.onerror = () => { /* browser auto-reconnects */ };
    return es;
  }

  // ---------------------------------------------------------------- actions
  async function controlRunner(id, action) {
    try {
      await api("/api/portfolio/runner/" + id + "/control", "POST", { action });
      addAudit(action.toUpperCase() + " sent to instance " + id.slice(0, 8), "action");
      toast(action + " sent", "success");
    } catch (e) { toast(e.message, "error"); }
  }

  // T2.4: Bulk actions scoped to PAGE_MODE when set.
  async function bulk(action, confirmMsg) {
    if (confirmMsg && !window.confirm(confirmMsg)) return;
    try {
      const url = "/api/portfolio/control/" + action + (PAGE_MODE ? "?mode=" + PAGE_MODE : "");
      const data = await api(url, "POST", {});
      addAudit("Bulk action " + action + (PAGE_MODE ? " [" + PAGE_MODE + "]" : "") +
        " (" + (data.affected || 0) + " affected)", "action");
      toast(action + " done", "success");
    } catch (e) { toast(e.message, "error"); }
  }

  // ---------------------------------------------------------------- spawn modal
  // U6.1: the form is ROUTING only (architecture §5.1) — strategy, timeframe,
  // target type (locked by signal_kind), symbol/index, bucket mode, data
  // source, allocation. Trading logic comes from the Playbook the user picks;
  // the form never asks instrument/structure/exit questions.

  // Index underlyings the option path accepts (mirrors the synthetic chain +
  // mStock FNO index set; OptionConfig.SYNTHETIC_UNDERLYINGS for the BS path).
  const OPTION_INDEXES = ["NIFTY", "BANKNIFTY"];

  function selectedStrategy() {
    const sel = $("spawn-strategy");
    return (sel._catalogue || []).find((s) => s.name === sel.value) || null;
  }

  function selectedSignalKind() {
    const strat = selectedStrategy();
    return strat && strat.signal_kind === "option" ? "option" : "equity";
  }

  async function loadSpawnForm() {
    try {
      const [strats, unis, pbs] = await Promise.all([
        fetch("/api/strategies").then((r) => r.json()),
        api("/api/portfolio/universes"),
        fetch("/api/playbooks").then((r) => r.json()).catch(() => ({ playbooks: [] })),
      ]);
      const stratSel = $("spawn-strategy");
      const catalogue = strats.strategies || strats || [];
      stratSel.innerHTML = catalogue.map((s) =>
        '<option value="' + s.name + '">' + (s.name) + "</option>").join("");
      stratSel._catalogue = catalogue;

      const uniSel = $("spawn-universe");
      uniSel.innerHTML = unis.universes.map((u) =>
        '<option value="' + u.id + '">' + u.label + " (" + u.size + " symbols)</option>").join("");

      const pbSel = $("spawn-playbook");
      const playbookList = (pbs.playbooks || []).filter(
        (p) => !String(p.playbook_id || "").startsWith("pb_default_") || true,
      );
      pbSel.innerHTML = '<option value="">(engine defaults — no playbook)</option>' +
        playbookList.map((p) =>
          '<option value="' + p.playbook_id + '">' + p.name + " v" + (p.version ?? 1) + "</option>").join("");

      renderSpawnParams();
      syncSpawnForm();
    } catch (e) { toast("Failed to load spawn form: " + e.message, "error"); }
  }

  function renderSpawnParams() {
    const sel = $("spawn-strategy");
    const strat = (sel._catalogue || []).find((s) => s.name === sel.value);
    const box = $("spawn-params");
    if (!strat || !strat.params) { box.innerHTML = ""; return; }
    box.innerHTML = "<div class='spawn-params-title'>Strategy parameters</div>" +
      Object.entries(strat.params).map(([key, spec]) => {
        const type = spec.type === "bool" ? "checkbox"
          : spec.type === "int" || spec.type === "float" ? "number" : "text";
        const val = spec.default !== null && spec.default !== undefined ? spec.default : "";
        return '<div class="form-row"><label>' + (spec.label || key) +
          (spec.tooltip ? ' <span class="muted" title="' + spec.tooltip + '">ⓘ</span>' : "") +
          '</label><input class="input spawn-param" data-param="' + key +
          '" type="' + type + '" value="' + val + '"></div>';
      }).join("");
  }

  /**
   * U6.1 sync — one rule set, driven by the selected strategy's signal_kind:
   * - option → target type locked to Single Symbol; symbol becomes an index
   *   picker (NIFTY / BANKNIFTY); Playbook row shown.
   * - equity → Single Symbol or Pool; free-text symbol; Playbook row hidden.
   */
  function syncSpawnForm() {
    const kind = selectedSignalKind();
    const isOption = kind === "option";

    const hint = $("spawn-strategy-hint");
    if (hint) {
      hint.textContent = isOption
        ? "Option strategy — its view is executed as the playbook's structure on an index."
        : "Equity strategy — trades the symbol(s) directly. Signal owns trigger/stop/target logic.";
    }

    // Target type: pool is impossible for options (a pool would never open a
    // structure — the engine refuses it). Lock it, don't just disable: the
    // user asked for the form to decide, not to warn afterwards.
    const targetSel = $("spawn-target-type");
    const poolOpt = targetSel.querySelector('option[value="pool"]');
    if (poolOpt) poolOpt.disabled = isOption;
    if (isOption && targetSel.value === "pool") targetSel.value = "single";

    // Symbol: index picker for options, free text for equity.
    const symbolInput = $("spawn-symbol");
    if (isOption) {
      symbolInput.setAttribute("list", "spawn-index-list");
      if (!OPTION_INDEXES.includes((symbolInput.value || "").toUpperCase())) {
        symbolInput.value = "NIFTY";
      }
    } else {
      symbolInput.removeAttribute("list");
      // Coming back from an option strategy: NIFTY is a valid equity symbol too,
      // but BTC/USD (the old default) is not — reset only when the current
      // value is an index the user was funnelled into.
      if (symbolInput.value === "NIFTY" && symbolInput.dataset.wasIndex === "1") {
        symbolInput.value = "RELIANCE";
      }
    }
    symbolInput.dataset.wasIndex = isOption ? "1" : "0";

    // Playbook row: only meaningful for option strategies.
    const pbRow = $("spawn-playbook-row");
    if (pbRow) pbRow.hidden = !isOption;

    // Pool rows follow target type (unchanged behaviour).
    const pool = targetSel.value === "pool";
    $("spawn-symbol-row").hidden = pool;
    $("spawn-universe-row").hidden = !pool;
    $("spawn-maxpos-row").hidden = !pool;
  }

  async function submitSpawn() {
    const targetType = $("spawn-target-type").value;
    const params = {};
    document.querySelectorAll(".spawn-param").forEach((el) => {
      let v = el.value;
      if (el.type === "checkbox") v = el.checked;
      else if (el.type === "number") v = parseFloat(v);
      params[el.dataset.param] = v;
    });

    const body = {
      name: $("spawn-name").value.trim(),
      strategy: $("spawn-strategy").value,
      timeframe: $("spawn-timeframe").value,
      allocated_capital: parseFloat($("spawn-capital").value) || 100000,
      // GAP-2 fix (2026-09-21): symbol must be read HERE, before the option
      // validation below — it used to be attached only AFTER the check, so
      // every option deployment failed with "pick NIFTY or BANKNIFTY".
      symbol: $("spawn-symbol").value.trim(),
      params,
      // Ticket #10 — the bucket (mode/source) is ALWAYS sent so the runner is
      // labelled from the user's selection; scoped pages default the controls
      // to the page's bucket, but the landing page sends the explicit choice
      // too (never an implicit backend default the user didn't see).
      mode: $("spawn-mode") ? $("spawn-mode").value : (PAGE_MODE || "paper"),
      source: $("spawn-source") ? $("spawn-source").value : "synthetic",
    };

    // U6.1: option routing comes from signal_kind + the selected playbook.
    // The expression block is the playbook's snapshot — never form fields.
    const kind = selectedSignalKind();
    if (kind === "option") {
      const symbolUpper = (body.symbol || "").toUpperCase();
      if (!OPTION_INDEXES.includes(symbolUpper)) {
        toast("Option strategies trade an index — pick NIFTY or BANKNIFTY.", "error");
        return;
      }
      const pbId = $("spawn-playbook") ? $("spawn-playbook").value : "";
      if (pbId) {
        try {
          const pbResp = await api("/api/playbooks/" + pbId + "/spawn", "POST", {
            strategy: body.strategy,
            allocated_capital: body.allocated_capital,
            mode: body.mode,
            source: body.source,
          });
          const rc = pbResp.runner_config || pbResp.config;
          if (rc && rc.instrument) body.instrument = rc.instrument;
          if (rc && rc.playbook_id) {
            body.playbook_id = rc.playbook_id;
            body.playbook_version = rc.playbook_version;
          }
        } catch (e) { toast("Playbook spawn failed: " + e.message, "error"); return; }
      } else {
        // Engine defaults (direction-aware spreads, churn-guarded exits).
        body.instrument = {
          type: "option",
          expression: {
            type: { BULLISH: "bull_call_spread", BEARISH: "bear_put_spread" },
          },
        };
      }
    }
    if (targetType === "pool") {
      body.target_type = "SYMBOL_UNIVERSE";
      body.universe_id = $("spawn-universe").value;
      body.max_pool_positions = parseInt($("spawn-maxpos").value, 10) || 5;
      delete body.symbol;
    } else {
      body.target_type = "SINGLE_SYMBOL";
      body.symbol = $("spawn-symbol").value.trim();
    }

    try {
      const data = await api("/api/portfolio/runner/create", "POST", body);
      const kindLabel = body.instrument && body.instrument.type === "option"
        ? " · option (" + ((body.playbook_id && $("spawn-playbook") && $("spawn-playbook").selectedOptions[0]) ? $("spawn-playbook").selectedOptions[0].textContent : "engine defaults") + ")"
        : "";
      addAudit(
        "Spawned " + data.runner.name + " (" + data.runner.target_label + kindLabel + ")",
        "spawn"
      );
      toast("Instance deployed: " + data.runner.name, "success");
      $("spawn-modal").hidden = true;
    } catch (e) { toast(e.message, "error"); }
  }

  // ---------------------------------------------------------------- events
  function bindEvents() {
    // On-demand equity snapshot (owner decision 2026-09-21).
    const snapBtn = $("btn-equity-snapshot");
    if (snapBtn) snapBtn.addEventListener("click", refreshEquitySnapshot);
    $("matrix-search").addEventListener("input", (e) => {
      state.search = e.target.value;
      if (state.portfolio) renderMatrix(state.portfolio);
    });
    $("matrix-filter").addEventListener("change", (e) => {
      state.filter = e.target.value;
      if (state.portfolio) renderMatrix(state.portfolio);
    });
    $("matrix-sort").addEventListener("change", (e) => {
      state.sort = e.target.value;
      if (state.portfolio) renderMatrix(state.portfolio);
    });

    // Delegated row actions
    $("matrix-body").addEventListener("click", async (e) => {
      const btn = e.target.closest(".row-btn");
      if (!btn) return;
      const id = btn.dataset.id;
      const act = btn.dataset.act;
      if (act === "deep_dive") {
        window.DeepDive.open(id, state.portfolio);
      } else {
        controlRunner(id, act);
      }
    });

    $("btn-add").addEventListener("click", () => {
      loadSpawnForm();
      $("spawn-modal").hidden = false;
    });
    $("btn-pause-all").addEventListener("click", () => bulk("pause_all"));
    $("btn-resume-all").addEventListener("click", () => bulk("resume_all"));
    $("btn-emergency").addEventListener("click", () => {
      $("emergency-modal").hidden = false;
    });
    $("emergency-confirm").addEventListener("click", async () => {
      $("emergency-modal").hidden = true;
      try {
        // T2.4: Send mode= when scoped to a bucket.
        const body = { reason: "manual" };
        if (PAGE_MODE) body.mode = PAGE_MODE;
        const data = await api("/api/portfolio/emergency_stop", "POST", body);
        addAudit("EMERGENCY FLATTEN" + (PAGE_MODE ? " [" + PAGE_MODE + "]" : "") +
          ": " + data.flattened_positions + " positions closed", "danger");
        toast("Emergency flatten executed", "error");
      } catch (e) { toast(e.message, "error"); }
    });
    $("cb-reset-btn").addEventListener("click", () => {
      // T2.4: Reset breaker scoped to PAGE_MODE, then resume scoped runners.
      bulk("reset_breaker").then(() => bulk("resume_all"));
    });

    // Modal close buttons
    document.querySelectorAll("[data-close]").forEach((b) =>
      b.addEventListener("click", () => { $(b.dataset.close).hidden = true; }));

    $("spawn-target-type").addEventListener("change", syncSpawnForm);
    $("spawn-strategy").addEventListener("change", () => { renderSpawnParams(); syncSpawnForm(); });
    // Index datalist for the option symbol picker.
    if (!document.getElementById("spawn-index-list")) {
      const dl = document.createElement("datalist");
      dl.id = "spawn-index-list";
      dl.innerHTML = OPTION_INDEXES.map((i) => '<option value="' + i + '">').join("");
      document.body.appendChild(dl);
    }
    $("spawn-submit").addEventListener("click", submitSpawn);

    // Expose for Playbooks UI — allows playbook spawn to pre-fill modal
    window.PortfolioSpawn = {
      loadSpawnForm,
      syncSpawnForm,
      renderSpawnParams,
      submitSpawn,
    };
    // Backward compat: some playbooks.js checks for loadSpawnForm globally
    window.loadSpawnForm = loadSpawnForm;

    // Tabs — extended for risk board + aggregated trades
    document.querySelectorAll(".tab").forEach((t) =>
      t.addEventListener("click", () => {
        document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
        t.classList.add("active");
        state.tab = t.dataset.tab;
        document.querySelectorAll(".tab-panel").forEach((p) => { p.hidden = true; });
        const panel = $("tab-" + state.tab);
        if (panel) panel.hidden = false;
        if (state.tab === "equity" && state.portfolio) renderChart(state.portfolio);
        if (state.tab === "log") fetchBackendAudit();
        if (state.tab === "risk" && window.RiskBoard) window.RiskBoard.refresh();
        if (state.tab === "aggregated-trades" && window.RiskBoard) window.RiskBoard.refreshAggregatedTrades();
        if (state.tab === "positions" && state.portfolio) renderAggregatePositions(state.portfolio);
      }));

    // Demo: Ctrl+Shift+T injects a crash for circuit-breaker verification.
    document.addEventListener("keydown", (e) => {
      if (e.ctrlKey && e.shiftKey && e.key === "T") {
        e.preventDefault();
        api("/api/portfolio/test/breach", "POST", { crash_pct: 0.25 })
          .then(() => addAudit("Simulated crash injected (-25%)", "danger"))
          .catch((err) => toast(err.message, "error"));
      }
    });
  }

  // ---------------------------------------------------------------- boot
  document.addEventListener("DOMContentLoaded", () => {
    bindEvents();
    connectStream();
    // Initial snapshot (SSE will take over)
    const summaryUrl = "/api/portfolio/summary" + (PAGE_MODE ? "?mode=" + PAGE_MODE : "");
    api(summaryUrl).then((d) => render(d.portfolio)).catch(() => {});
  });
})();
