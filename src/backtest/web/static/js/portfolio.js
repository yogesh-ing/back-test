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
  };

  // ---------------------------------------------------------------- helpers
  const $ = (id) => document.getElementById(id);
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
    });
    state.audit = state.audit.slice(0, 200);
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

  function renderMatrix(p) {
    const body = $("matrix-body");
    const rows = filteredRunners(p);
    $("matrix-empty").hidden = p.runners.length !== 0;

    body.innerHTML = rows.map((r, i) => {
      const typeBadge = r.target_type === "SINGLE_SYMBOL"
        ? '<span class="badge badge-single">Single</span>'
        : '<span class="badge badge-pool">Pool(' + r.symbol_count + ')</span>';
      return (
        '<tr class="matrix-row status-' + r.status.toLowerCase() + '">' +
        '<td>' + (i + 1) + '</td>' +
        '<td class="cell-name">' + r.name +
          '<div class="cell-sub">' + r.strategy_name + '</div></td>' +
        '<td>' + r.target_label + '</td>' +
        '<td>' + typeBadge + '</td>' +
        '<td>' + r.timeframe + '</td>' +
        '<td class="num">' + fmtMoney(r.allocated_capital) + '</td>' +
        '<td class="num ' + pnlClass(r.open_pnl) + '">' + fmtSigned(r.open_pnl) + '</td>' +
        '<td class="num ' + pnlClass(r.daily_pnl) + '">' + fmtSigned(r.daily_pnl) + '</td>' +
        '<td class="num">' + r.open_positions + '</td>' +
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
    tbody.innerHTML = p.runners
      .filter((r) => r.open_positions > 0)
      .map((r) =>
        '<tr><td>' + r.name + '</td><td>' + r.target_label + '</td><td>LONG</td>' +
        '<td class="num">—</td><td class="num">—</td><td class="num">—</td>' +
        '<td class="num ' + pnlClass(r.open_pnl) + '">' + fmtSigned(r.open_pnl) +
        ' <span class="muted">(' + r.open_positions + ' pos)</span></td></tr>')
      .join("") || '<tr><td colspan="7" class="muted" style="padding:16px">No open positions.</td></tr>';
  }

  function renderAudit() {
    const el = $("audit-log");
    if (!el) return;
    el.innerHTML = state.audit.map((a) =>
      '<div class="audit-line audit-' + a.kind + '"><span class="audit-ts">' + a.ts +
      "</span><span>" + a.message + "</span></div>").join("") ||
      '<p class="muted" style="padding:12px">Waiting for activity…</p>';
  }

  // ---------------------------------------------------------------- chart
  function renderChart(p) {
    if (state.tab !== "equity") return;
    const canvas = $("portfolio-equity-chart");
    if (!canvas || typeof Chart === "undefined") return;

    // Lightweight live series: total portfolio equity appended on every frame.
    // (Per-runner equity curves are shown in the deep-dive drawer.)
    if (!window._pccEquity) window._pccEquity = [];
    window._pccEquity.push({ t: Date.now(), equity: p.total_equity });
    if (window._pccEquity.length > 300) window._pccEquity.shift();
    const series = window._pccEquity;

    const labels = series.map((_, i) => i);
    const data = series.map((s) => s.equity);

    if (state.chart) {
      state.chart.data.labels = labels;
      state.chart.data.datasets[0].data = data;
      state.chart.update("none");
      return;
    }
    state.chart = new Chart(canvas.getContext("2d"), {
      type: "line",
      data: {
        labels,
        datasets: [{
          label: "Portfolio Equity",
          data,
          borderColor: "#3b82f6",
          backgroundColor: "rgba(59,130,246,.12)",
          fill: true,
          borderWidth: 2,
          pointRadius: 0,
          tension: 0.25,
        }],
      },
      options: {
        responsive: true,
        animation: false,
        plugins: { legend: { labels: { color: "#94a3b8" } } },
        scales: {
          x: { display: false },
          y: { ticks: { color: "#94a3b8" }, grid: { color: "rgba(148,163,184,.1)" } },
        },
      },
    });
  }

  // ---------------------------------------------------------------- render
  function render(p) {
    state.portfolio = p;
    renderMetrics(p);
    renderBanner(p);
    renderMatrix(p);
    renderAggregatePositions(p);
    renderChart(p);
  }

  // ---------------------------------------------------------------- SSE
  function connectStream() {
    const es = new EventSource("/api/portfolio/stream");
    es.addEventListener("portfolio", (ev) => {
      try {
        const p = JSON.parse(ev.data);
        $("feed-dot").textContent = "🟢";
        $("feed-label").textContent = "Live · " + p.runner_count + " runners · " +
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

  async function bulk(action, confirmMsg) {
    if (confirmMsg && !window.confirm(confirmMsg)) return;
    try {
      const data = await api("/api/portfolio/control/" + action, "POST", {});
      addAudit("Bulk action " + action + " (" + (data.affected || 0) + " affected)", "action");
      toast(action + " done", "success");
    } catch (e) { toast(e.message, "error"); }
  }

  // ---------------------------------------------------------------- spawn modal
  async function loadSpawnForm() {
    try {
      const [strats, unis] = await Promise.all([
        fetch("/api/strategies").then((r) => r.json()),
        api("/api/portfolio/universes"),
      ]);
      const stratSel = $("spawn-strategy");
      const catalogue = strats.strategies || strats || [];
      stratSel.innerHTML = catalogue.map((s) =>
        '<option value="' + s.name + '">' + (s.name) + "</option>").join("");
      stratSel._catalogue = catalogue;

      const uniSel = $("spawn-universe");
      uniSel.innerHTML = unis.universes.map((u) =>
        '<option value="' + u.id + '">' + u.label + " (" + u.size + " symbols)</option>").join("");

      renderSpawnParams();
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

  async function submitSpawn() {
    const mode = $("spawn-target-mode").value;
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
      params,
    };
    if (mode === "pool") {
      body.target_type = "SYMBOL_UNIVERSE";
      body.universe_id = $("spawn-universe").value;
      body.max_pool_positions = parseInt($("spawn-maxpos").value, 10) || 5;
    } else {
      body.target_type = "SINGLE_SYMBOL";
      body.symbol = $("spawn-symbol").value.trim();
    }

    try {
      const data = await api("/api/portfolio/runner/create", "POST", body);
      addAudit("Spawned " + data.runner.name + " (" + data.runner.target_label + ")", "spawn");
      toast("Instance deployed: " + data.runner.name, "success");
      $("spawn-modal").hidden = true;
    } catch (e) { toast(e.message, "error"); }
  }

  // ---------------------------------------------------------------- events
  function bindEvents() {
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
        const data = await api("/api/portfolio/emergency_stop", "POST", { reason: "manual" });
        addAudit("EMERGENCY FLATTEN: " + data.flattened_positions + " positions closed", "danger");
        toast("Emergency flatten executed", "error");
      } catch (e) { toast(e.message, "error"); }
    });
    $("cb-reset-btn").addEventListener("click", () => bulk("reset_breaker").then(() => bulk("resume_all")));

    // Modal close buttons
    document.querySelectorAll("[data-close]").forEach((b) =>
      b.addEventListener("click", () => { $(b.dataset.close).hidden = true; }));

    $("spawn-target-mode").addEventListener("change", (e) => {
      const pool = e.target.value === "pool";
      $("spawn-symbol-row").hidden = pool;
      $("spawn-universe-row").hidden = !pool;
      $("spawn-maxpos-row").hidden = !pool;
    });
    $("spawn-strategy").addEventListener("change", renderSpawnParams);
    $("spawn-submit").addEventListener("click", submitSpawn);

    // Tabs
    document.querySelectorAll(".tab").forEach((t) =>
      t.addEventListener("click", () => {
        document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
        t.classList.add("active");
        state.tab = t.dataset.tab;
        document.querySelectorAll(".tab-panel").forEach((p) => { p.hidden = true; });
        $("tab-" + state.tab).hidden = false;
        if (state.tab === "equity" && state.portfolio) renderChart(state.portfolio);
        if (state.tab === "log") renderAudit();
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
    api("/api/portfolio/summary").then((d) => render(d.portfolio)).catch(() => {});
  });
})();
