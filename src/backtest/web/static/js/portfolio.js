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
    // issues.txt P4: Positions is the default view (template lists it first).
  tab: "positions",
    chart: null,
    audit: [],
    backendAudit: [],
    // Positions tab (Live Order Management): which rows the operator is
    // looking at — the table is filtered client-side off the live snapshot.
    posSearch: "",
    posRulesOnly: false,
    // MTM Staleness Observability (R2, R7): track active staleness episodes
    // per runner so the toast fires once on healthy → stale transition, and
    // track inline manual refresh results per runner.
    staleEpisodes: {},
    refreshStatus: {},
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
  // Prices are shown at 2dp and "—" when unknown; a blank cell in a money
  // table reads as zero, which is a different (and wrong) statement.
  const legPriceOf = (v) => (v == null ? "—" : Number(v).toFixed(2));
  const expiryLabelOf = (iso) => {
    const view = (typeof window !== "undefined" && window.OptionView) || null;
    if (view && typeof view.expiryLabel === "function") return view.expiryLabel(iso);
    return iso ? String(iso).slice(0, 10) : "—";
  };
  const positionActionsCell = (row) => {
    const api = (typeof window !== "undefined" && window.PositionActions) || null;
    return api && typeof api.buttonCell === "function" ? api.buttonCell(row) : "";
  };

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
        // Backend audit rows are UTC-aware ISO; show them local like the
        // position times (previously sliced raw "06:57:34" strings).
        ts: e.ts && !isNaN(parseExecDate(e.ts).getTime())
          ? parseExecDate(e.ts).toLocaleString("en-IN", {
              day: "2-digit", month: "2-digit", hour: "2-digit",
              minute: "2-digit", second: "2-digit", hour12: false,
            })
          : (e.ts || "").replace("T", " ").slice(5, 19),
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

    const staleMarksCount = typeof p.stale_marks_count === "number"
      ? p.stale_marks_count
      : (p.runners || []).reduce((n, r) => n + OptionView.staleCount(r), 0);
    $("m-positions").textContent = staleMarksCount > 0
      ? p.open_positions + " pos · " + staleMarksCount + " stale mark"
      : p.open_positions + " active";
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
    if (OptionView.isMarkStale(r)) {
      html += '<button class="row-btn row-btn-refresh-marks" data-act="refresh_marks" data-id="' +
        r.instance_id + '" title="Re-bind option contracts and refresh stale marks">↻ Refresh marks</button>';
    }
    if (r.status === "RUNNING") {
      html += '<button class="row-btn" data-act="pause" data-id="' + r.instance_id + '" title="Pause">⏸</button>';
    } else if (r.status === "PAUSED") {
      html += '<button class="row-btn" data-act="resume" data-id="' + r.instance_id + '" title="Resume">▶</button>';
    }
    html += '<button class="row-btn row-btn-stop" data-act="stop" data-id="' + r.instance_id + '" title="Stop">⏹</button>';
    html += '<button class="row-btn" data-act="deep_dive" data-id="' + r.instance_id + '" title="Deep dive">🔍</button>';
    html += '<button class="row-btn row-btn-danger" data-act="remove" data-id="' + r.instance_id + '" title="Remove instance (flattens its book, deletes the row)">🗑</button>';
    return html;
  }

  // Mode/source badge (ticket P4.2) — mirrors templates/_macros.html::badge.
  // The text carries the value; the colour is only a supplement.
  const SOURCE_LABELS = { synthetic: "SYNTH", mstock: "MSTOCK", replay: "REPLAY" };
  function badgeHtml(mode, source) {
    const m = (mode || "paper").toUpperCase();
    const raw = String(source || "").toLowerCase();
    const s = raw ? (SOURCE_LABELS[raw] || raw).toUpperCase() : "UNKNOWN";
    const cls = mode === "live" ? "badge-live" : "badge-paper";
    return '<span class="badge ' + cls + '">' + m + "/" + s + "</span>";
  }

  // 2026-09-22: strategy-kind badge — 🎯 for option runners, 📈 for equity.
  // Mirrors the spawn-form optgroups so a row reads instantly: which kind,
  // which strategy (with timeframe), on what instrument.
  function kindBadge(r) {
    const isOption = OptionView.isOption(r) ||
      (r.instrument && String(r.instrument.type || "").toLowerCase() === "option");
    return isOption
      ? '<span title="Option strategy" style="margin-right:4px">🎯</span>'
      : '<span title="Equity strategy" style="margin-right:4px">📈</span>';
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
      const isStale = OptionView.isMarkStale(r);
      const staleItem = {
        mark_stale: isStale,
        mark_ts: r.mark_ts || (r.options && r.options.mark_ts),
        quote_error: r.quote_error || (r.options && r.options.quote_error),
      };
      const refreshMsg = state.refreshStatus[r.instance_id] ||
        (r.options && r.options.last_refresh && r.options.last_refresh.message) || "";
      return (
        '<tr class="matrix-row' + (option ? " matrix-row-option" : "") +
          (isStale ? " stale-mark-row" : "") +
          ' status-' + r.status.toLowerCase() + '">' +
        '<td>' + (i + 1) + '</td>' +
        '<td class="cell-name">' + kindBadge(r) + r.name +
          '<div class="cell-sub">' + r.strategy_name + " · " + r.timeframe + '</div></td>' +
        '<td>' + targetCell + '</td>' +
        '<td>' + instrument + '</td>' +
        '<td>' + badgeHtml(r.mode, r.source) + '</td>' +
        '<td>' + r.timeframe + '</td>' +
        '<td class="num">' + fmtMoney(r.allocated_capital) + '</td>' +
        '<td class="num ' + pnlClass(r.open_pnl) + '">' + fmtSigned(r.open_pnl) +
          OptionView.staleBadgeHtml(staleItem) + '</td>' +
        '<td class="num ' + pnlClass(r.daily_pnl) + '">' + fmtSigned(r.daily_pnl) + '</td>' +
        '<td class="num">' + posCell.primary +
          (posCell.sub ? '<div class="cell-sub">' + posCell.sub + '</div>' : "") + '</td>' +
        '<td><span class="status-cell">' + (STATUS_DOT[r.status] || "⚪") + " " + r.status + "</span>" +
          (r.error ? '<div class="cell-sub cell-error" title="' + (r.error || "") + '">⚠ risk halt</div>' : "") +
        '</td>' +
        '<td><div class="row-actions">' + rowActions(r) + "</div>" +
          (refreshMsg ? '<div class="cell-sub refresh-marks-result">' + refreshMsg + '</div>' : "") +
        "</td>" +
        "</tr>"
      );
    }).join("");
  }

  // ---------------------------------------------------------------- tabs

  // --------------------------------------------------------- positions tab
  // Live Order Management (2026-09-23). The tab used to be a runner-summary:
  // an equity runner with three tickets collapsed into one "3 pos" row with
  // dashes for entry/current price, and nothing could be acted on. The
  // snapshot now carries `positions` — one flat row per open position
  // (PortfolioManager.list_positions), including the operator's stop/target,
  // option Greeks when they are computable, and the `position_key` the action
  // buttons send back. Older payloads (no `positions`) still render through
  // the runner-shaped fallback below, so nothing regresses.
  let POSITION_ROWS = [];

  function positionFiltered(rows) {
    const q = String(state.posSearch || "").trim().toLowerCase();
    let out = rows;
    if (q) {
      out = out.filter((r) =>
        String(r.symbol || "").toLowerCase().includes(q) ||
        String(r.label || "").toLowerCase().includes(q) ||
        String(r.runner || "").toLowerCase().includes(q) ||
        String(r.strategy_name || "").toLowerCase().includes(q) ||
        String(r.instance_id || "").toLowerCase().includes(q));
    }
    if (state.posRulesOnly) {
      out = out.filter((r) => r.stop_loss !== null && r.stop_loss !== undefined ||
        (r.target !== null && r.target !== undefined));
    }
    return out;
  }

  function posLevelCell(level, kind) {
    if (level === null || level === undefined) {
      return '<td class="num muted">—</td>';
    }
    const cls = kind === "stop" ? "pos-sl" : "pos-tp";
    return '<td class="num ' + cls + '">' + Number(level).toLocaleString("en-IN", {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    }) + "</td>";
  }

  function greeksCell(row) {
    if (row.net_delta === null || row.net_delta === undefined) {
      return '<td class="num muted" title="' + (row.kind === "option"
        ? "Greeks need a spot, an expiry and an implied vol for the legs"
        : "Not an option position") + '">—</td>';
    }
    const delta = Number(row.net_delta);
    const theta = row.net_theta === null || row.net_theta === undefined ? null : Number(row.net_theta);
    return '<td class="num" title="' + (row.greeks_source ? "Black-Scholes (" + row.greeks_source + ")" : "Black-Scholes") +
      '"><span class="' + (delta >= 0 ? "pnl-pos" : "pnl-neg") + '">Δ ' +
      delta.toLocaleString("en-IN", { maximumFractionDigits: 1 }) + "</span>" +
      (theta === null ? "" : '<span class="muted"> Θ ' +
        theta.toLocaleString("en-IN", { maximumFractionDigits: 1 }) + "</span>") + "</td>";
  }

  // Execution times arrive as UTC ISO strings (datetime.now(timezone.utc) on
  // the backend). The broker terminal speaks IST, so render browser-local
  // time — a bare "06:57" next to a broker's "12:27" is the same instant.
  // Some books persist naive UTC (no +00:00 suffix); assume UTC when absent.
  function parseExecDate(iso) {
    let s = String(iso || "");
    if (s && !/([zZ]|[+-]\d{2}:?\d{2})$/.test(s)) s += "Z";
    return new Date(s);
  }
  function execTimeLabel(iso) {
    if (!iso) return "";
    const d = parseExecDate(iso);
    if (isNaN(d.getTime())) return "";
    const t = d.toLocaleTimeString("en-IN", { hour12: false });
    if (d.toDateString() === new Date().toDateString()) return t;
    return String(d.getDate()).padStart(2, "0") + "-" +
      String(d.getMonth() + 1).padStart(2, "0") + " " + t;
  }

  function positionRowHtml(row) {
    const pnl = Number(row.unrealized_pnl || 0);
    const stale = row.stale
      ? '<span class="muted" title="Runner is not running — this mark is frozen, actions still work">⏸</span> '
      : "";
    const label = row.kind === "option"
      ? '<span class="badge badge-option">OPTION</span> ' + (row.label || row.symbol) + " <span class=\"muted\">(net)</span>"
      : row.label || row.symbol;
    const entered = row.entry_ts
      ? '<span class="muted" title="Entry executed ' + String(row.entry_ts).replace(/[<>&"]/g, "") + ' (server UTC)"> · in ' + execTimeLabel(row.entry_ts) + "</span>"
      : "";
    const sub = row.kind === "option"
      ? '<div class="cell-sub">' + (row.structure_type || "").replace(/_/g, " ") +
        (row.expiry ? " · exp " + expiryLabelOf(row.expiry) : "") +
        (row.bars_held ? " · " + row.bars_held + " bars" : "") + entered + "</div>"
      : '<div class="cell-sub">' + (row.strategy_name || "") + entered + "</div>";
    return (
      '<tr class="pos-row' + (row.stale ? " pos-row-stale" : "") +
        (row.mark_stale ? " stale-mark-row" : "") +
        (row.kind === "option" ? " matrix-row-option" : "") + '">' +
      '<td>' + stale + (row.runner || String(row.instance_id || "").slice(0, 8)) +
        '<div class="cell-sub">' + (row.status || "") +
        // Multi-broker Phase B: which broker holds this position (manual
        // close routes there) + its segment when set.
        (row.broker ? " · <span class=\"pos-broker\" title=\"Execution broker\">" +
          row.broker + "</span>" : "") +
        (row.segment ? " · " + row.segment : "") +
        "</div></td>" +
      '<td class="cell-name">' + label + sub + "</td>" +
      "<td>" + (row.side || "") + "</td>" +
      '<td class="num">' + Number(row.qty || 0).toLocaleString("en-IN") + "</td>" +
      '<td class="num">' + legPriceOf(row.entry_price) + "</td>" +
      '<td class="num">' + legPriceOf(row.current_price) + OptionView.staleBadgeHtml(row) + "</td>" +
      '<td class="num ' + pnlClass(pnl) + '">' + fmtSigned(pnl) + "</td>" +
      posLevelCell(row.target, "target") +
      posLevelCell(row.stop_loss, "stop") +
      greeksCell(row) +
      "<td>" + positionActionsCell(row) + "</td>" +
      "</tr>" +
      optionLegRows(row)
    );
  }

  /** Leg rows under a structure — the net row above is what gets acted on. */
  function optionLegRows(row) {
    if (row.kind !== "option" || !Array.isArray(row.legs) || !row.legs.length) return "";
    return row.legs.map((leg) => {
      const legPnl = Number(leg.pnl || 0);
      const greeks = leg.delta === null || leg.delta === undefined ? "—"
        : "Δ " + Number(leg.delta).toFixed(2) +
          (leg.theta === null || leg.theta === undefined ? "" : " Θ " + Number(leg.theta).toFixed(1));
      return (
        '<tr class="opt-leg-row' + (leg.mark_stale ? " stale-mark-row" : "") + '">' +
        "<td></td>" +
        '<td class="cell-sub">↳ leg ' + (leg.trading_symbol || "") +
          '<span class="muted"> ' + Number(leg.strike || 0).toLocaleString("en-IN") +
          " " + (leg.option_type || "") + "</span></td>" +
        "<td>" + (leg.side || "") + "</td>" +
        '<td class="num">' + Number(leg.qty || 0).toLocaleString("en-IN") + "</td>" +
        '<td class="num">' + legPriceOf(leg.entry_price) + "</td>" +
        '<td class="num">' + legPriceOf(leg.current_price) + OptionView.staleBadgeHtml(leg) + "</td>" +
        '<td class="num ' + pnlClass(legPnl) + '">' + fmtSigned(legPnl) + "</td>" +
        '<td class="num muted">—</td><td class="num muted">—</td>' +
        '<td class="num muted">' + greeks + "</td><td></td></tr>"
      );
    }).join("");
  }

  function renderPositionRows(rows) {
    const tbody = $("aggregate-positions");
    if (!tbody) return;
    POSITION_ROWS = rows;
    const shown = positionFiltered(rows);
    if (!rows.length) {
      tbody.innerHTML =
        '<tr><td colspan="11" class="muted" style="padding:16px">No open positions. ' +
        "Nothing is held right now — deploy an instance or check that the feed is running.</td></tr>";
    } else if (!shown.length) {
      tbody.innerHTML =
        '<tr><td colspan="11" class="muted" style="padding:16px">No positions match the filter.</td></tr>';
    } else {
      tbody.innerHTML = shown.map(positionRowHtml).join("");
    }

    const pnls = rows.map((r) => Number(r.unrealized_pnl || 0));
    const withRules = rows.filter((r) =>
      (r.stop_loss !== null && r.stop_loss !== undefined) ||
      (r.target !== null && r.target !== undefined)).length;
    const stale = rows.filter((r) => r.stale).length;
    const markStale = rows.filter((r) => r.mark_stale).length;
    const summaryEl = $("pos-summary");
    if (summaryEl) {
      // Multi-broker Phase B: per-broker totals when positions span more
      // than one broker (deployed view honesty — PRD §8.4).
      const byBroker = {};
      for (const r of rows) {
        const b = r.broker || "paper";
        if (!byBroker[b]) byBroker[b] = { n: 0, pnl: 0 };
        byBroker[b].n += 1;
        byBroker[b].pnl += Number(r.unrealized_pnl || 0);
      }
      const brokerNames = Object.keys(byBroker);
      const brokerStrip = brokerNames.length > 1
        ? "<div class=\"cell-sub\">" + brokerNames.map((b) =>
            b + ": " + byBroker[b].n + " pos · P&L " +
            (byBroker[b].pnl >= 0 ? "+" : "-") + fmtMoney(Math.abs(byBroker[b].pnl))
          ).join(" · ") + "</div>"
        : "";
      summaryEl.innerHTML = rows.length
        ? "<strong>" + rows.length + "</strong> pos · " +
          (markStale ? '<span class="stale-mark-summary">' + markStale + " stale mark</span> · " : "") +
          rows.filter((r) => r.kind === "option").length + " option · " +
          rows.filter((r) => r.kind === "equity").length + " equity · " +
          withRules + " with a manual level · open P&L " +
          (pnls.reduce((a, b) => a + b, 0) >= 0 ? "" : "-") +
          fmtMoney(Math.abs(pnls.reduce((a, b) => a + b, 0))) + brokerStrip
        : "nothing held";
    }
    const footEl = $("pos-footnote");
    if (footEl) {
      footEl.innerHTML =
        (stale ? "⏸ " + stale + " row(s) belong to a paused/stopped runner — their mark is frozen " +
          "(actions still work; resume the runner to see live P&L). " : "") +
        "Stop and Target are <strong>price</strong> levels: the share price for an equity position, the " +
        "net premium per unit for an option structure. Both are checked on every bar and exit at market — " +
        "the strategy does not have to trade for them to fire.";
    }
    // Re-arm the action buttons against THIS render's rows: a click resolves
    // against the book the operator can see, never a stale page-load copy.
    if (window.PositionActions) window.PositionActions.rows(rows);
  }

  function renderAggregatePositions(p) {
    // Live Order Management: the flat positions array is the primary source.
    if (Array.isArray(p.positions)) {
      return renderPositionRows(p.positions);
    }
    const tbody = $("aggregate-positions");
    // Legacy fallback (payloads without `positions`): row-level aggregate —
    // open count + open P&L per runner; per-symbol detail is available via
    // each runner's deep-dive drawer.
    // C2: an option runner holds whole structures, not equity tickets — show
    // each leg (trading symbol · strike · side) rather than three dashes, and
    // fall back to one summarising row when the leg detail is unavailable.
    const rows = [];
    const legPrice = (v) => (v == null ? "—" : Number(v).toFixed(2));


    // 2026-09-23 (owner correction): SHOW every runner's positions, but tag
    // non-running ones ⏸ FROZEN. The earlier hide-rule made paused books
    // vanish from the aggregate while the bucket card still counted them —
    // misrepresentation from the other side. Hiding was wrong, fake +₹0 was
    // wrong; labeled-frozen is honest. A paused runner's prices are its LAST
    // marks (no bars while paused) — the tag says so at the name.
    const frozenTag = (r) =>
      r.status === "RUNNING"
        ? ""
        : ' <span class="badge badge-option" title="Runner not running — prices frozen at the last mark">⏸ ' +
          r.status + '</span>';

    p.runners.filter((r) => r.open_positions > 0).forEach((r) => {
      const structures = OptionView.openStructures(r);
      if (!structures.length) {
        rows.push(
          '<tr><td>' + r.name + frozenTag(r) + '</td><td>' + r.target_label + '</td><td>' +
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
            '<tr' + (s.mark_stale ? ' class="stale-mark-row"' : "") + '><td>' + r.name + frozenTag(r) + '</td><td>' + s.symbol + '</td><td>' + s.side + '</td>' +
            '<td class="num">' + s.units + '</td>' +
            '<td class="num">' + legPrice(s.entry_price) + '</td>' +
            '<td class="num">' + legPrice(s.current_price) + OptionView.staleBadgeHtml(s) + '</td>' +
            '<td class="num ' + pnlClass(s.unrealized_pnl) + '">' + fmtSigned(s.unrealized_pnl) +
            '</td></tr>');
          return;
        }
        legs.forEach((leg, idx) => {
          const rowCls = [idx === 0 ? "opt-first-leg" : "", leg.mark_stale ? "stale-mark-row" : ""].filter(Boolean).join(" ");
          rows.push(
            '<tr' + (rowCls ? ' class="' + rowCls + '"' : "") + '>' +
            '<td>' + (idx === 0 ? r.name + frozenTag(r) : "") + '</td>' +
            '<td>' + (leg.trading_symbol || s.symbol) + '</td>' +
            '<td>' + leg.side + '</td>' +
            '<td class="num">' + leg.qty + '</td>' +
            '<td class="num">' + legPrice(leg.entry_price) + '</td>' +
            '<td class="num">' + legPrice(leg.current_price) + OptionView.staleBadgeHtml(leg) + '</td>' +
            '<td class="num ' + pnlClass(leg.pnl) + '">' + fmtSigned(leg.pnl) + '</td></tr>');
        });
        rows.push(
          '<tr class="opt-total' + (s.mark_stale ? " stale-mark-row" : "") + '"><td></td><td>' + s.symbol + ' (net)</td><td>' + s.side +
          '</td><td class="num">' + s.qty + ' lot' + (s.qty === 1 ? "" : "s") + '</td>' +
          '<td class="num">' + legPrice(s.entry_price) + '</td>' +
          '<td class="num">' + legPrice(s.current_price) + OptionView.staleBadgeHtml(s) + '</td>' +
          '<td class="num ' + pnlClass(s.unrealized_pnl) + '">' + fmtSigned(s.unrealized_pnl) +
          ' <span class="muted">exp ' + OptionView.expiryLabel(s.expiry) + '</span></td></tr>');
      });
    });
    const base = rows.length ? "" :
      '<tr><td colspan="7" class="muted" style="padding:16px">No open positions.</td></tr>';
    tbody.innerHTML = base + rows.join("");
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

  function checkStaleTransitions(p) {
    const runners = (p && p.runners) || [];
    runners.forEach((r) => {
      const id = r.instance_id;
      if (!id) return;
      const isStale = OptionView.isMarkStale(r);
      const epKey = (r.options && r.options.stale_episode_id) || (isStale ? 1 : 0);
      if (isStale) {
        if (state.staleEpisodes[id] !== epKey) {
          state.staleEpisodes[id] = epKey;
          const err = r.quote_error || (r.options && r.options.quote_error) || "quote failed";
          toast("⚠ Stale mark on " + (r.name || id) + ": " + err, "warning");
        }
      } else if (state.staleEpisodes[id]) {
        delete state.staleEpisodes[id];
      }
    });
  }

  // ---------------------------------------------------------------- render
  function render(p) {
    // SSE broadcasts the combined snapshot — drop other buckets on a scoped page.
    if (PAGE_MODE) {
      p.runners = (p.runners || []).filter((r) => (r.mode || "paper") === PAGE_MODE);
      if (Array.isArray(p.positions)) {
        p.positions = p.positions.filter((pos) => (pos.mode || "paper") === PAGE_MODE);
      }
    }
    state.portfolio = p;
    checkStaleTransitions(p);
    // T2.1: Metrics use bucket-scoped data when PAGE_MODE is set.
    renderMetrics(bucketMetrics(p));
    renderBanner(p);
    renderMatrix(p);
    renderAggregatePositions(p);
    // Orders tab: the ledger rows come from REST, but the COUNTS ride the SSE
    // snapshot, so the tab badge is live even before the tab is ever opened.
    if (window.OrdersTab) window.OrdersTab.noteTick(p);
    renderChart(p);
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
        $("feed-dot").dataset.state = "connected";
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
        // Bar-clock countdown (2026-09-23): bars arrive every POLL_S —
        // show when the next one lands so "prices not moving" is
        // distinguishable from "waiting for the next minute bar".
        const POLL_S = 60;
        const BAR_PERIOD_S = 60; // 1-minute bars
        window.__lastBarTs = p.last_bar_ts || window.__lastBarTs || null;
        window.__barPeriodS = BAR_PERIOD_S;
        window.__lastMarketState = p.market || window.__lastMarketState || null;
        if (p.market && window.updateMarketChip) {
          window.updateMarketChip(p.market);
        }
        render(p);
      } catch (e) { /* ignore malformed frame */ }
    });
    es.addEventListener("error", () => {
      $("feed-dot").textContent = "🔴";
      $("feed-dot").dataset.state = "disconnected";
      $("feed-label").textContent = "Feed disconnected — retrying…";
    });
    es.onerror = () => { /* browser auto-reconnects */ };
    function formatBarElapsed(elapsed) {
      if (elapsed < 60) return elapsed + "s ago";
      if (elapsed < 3600) return Math.floor(elapsed / 60) + "m ago";
      if (elapsed < 86400) {
        const h = Math.floor(elapsed / 3600);
        const m = Math.floor((elapsed % 3600) / 60);
        return m > 0 ? h + "h " + m + "m ago" : h + "h ago";
      }
      const d = Math.floor(elapsed / 86400);
      const h = Math.floor((elapsed % 86400) / 3600);
      return h > 0 ? d + "d " + h + "h ago" : d + "d ago";
    }

    function getIstTime() {
      const now = new Date();
      const utcMs = now.getTime() + (now.getTimezoneOffset() * 60000);
      return new Date(utcMs + (5.5 * 3600000));
    }

    function formatDayDDMMM(isoStr) {
      if (!isoStr) return "";
      try {
        const dt = new Date(isoStr);
        const utcMs = dt.getTime() + (dt.getTimezoneOffset() * 60000);
        const istDt = new Date(utcMs + (5.5 * 3600000));
        const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
        const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
        const day = days[istDt.getDay()];
        const dd = String(istDt.getDate()).padStart(2, "0");
        const mmm = months[istDt.getMonth()];
        return `${day} ${dd} ${mmm}`;
      } catch (e) {
        return "";
      }
    }

    // 1-second countdown ticker (MARKET-STATUS-SPEC-2026-10-02 Table 2):
    // reads __lastBarTs and __lastMarketState set by the SSE handler.
    setInterval(() => {
      const el = document.getElementById("bar-countdown");
      if (!el) return;

      const market = window.__lastMarketState || null;
      const lastTs = window.__lastBarTs;
      const period = window.__barPeriodS || 60;
      let elapsed = null;
      if (lastTs) {
        const last = new Date(String(lastTs).replace(" ", "T") + "+05:30");
        elapsed = Math.max(0, Math.round((Date.now() - last.getTime()) / 1000));
      }

      const ist = getIstTime();
      const curMins = ist.getHours() * 60 + ist.getMinutes();
      const curSecs = ist.getSeconds();
      const totalSecs = curMins * 60 + curSecs;

      let dayState = market ? market.day_state : (ist.getDay() === 0 || ist.getDay() === 6 ? "WEEKEND" : "TRADING_DAY");
      let sessionState = "POST_CLOSE";
      if (dayState === "TRADING_DAY") {
        if (totalSecs < 9 * 3600) {
          sessionState = "POST_CLOSE";
        } else if (totalSecs < (9 * 3600 + 15 * 60)) {
          sessionState = "PRE_OPEN";
        } else if (totalSecs <= (15 * 3600 + 30 * 60)) {
          sessionState = "OPEN";
        } else {
          sessionState = "POST_CLOSE";
        }
      }

      if (dayState === "HOLIDAY") {
        const name = (market && market.holiday_name) ? market.holiday_name : "Holiday";
        const dateStr = (market && market.next_open_ts) ? formatDayDDMMM(market.next_open_ts) : "Mon 05 Oct";
        el.textContent = `Market holiday · ${name} · Opens ${dateStr} 09:15 IST`;
        return;
      }

      if (dayState === "WEEKEND") {
        el.textContent = "Weekend · Market closed · Opens Monday 09:15 IST";
        return;
      }

      // TRADING_DAY
      if (sessionState === "PRE_OPEN") {
        const openSecs = 9 * 3600 + 15 * 60;
        const diffSecs = Math.max(0, openSecs - totalSecs);
        const xM = Math.floor(diffSecs / 60);
        const yS = diffSecs % 60;
        el.textContent = `Pre-open: opens 09:15 IST (in ${xM}m ${yS}s)`;
        return;
      }

      if (sessionState === "POST_CLOSE") {
        el.textContent = "Session closed (15:30 IST) · Opens tomorrow 09:15 IST";
        return;
      }

      // TRADING_DAY + OPEN
      if (elapsed === null) {
        el.textContent = "waiting for first bar…";
        return;
      }

      if (elapsed > 300) {
        el.textContent = "⏸ market session idle · last bar " + formatBarElapsed(elapsed);
        return;
      }

      const barElapsed = elapsed % period;
      const remain = Math.max(0, period - barElapsed);
      el.textContent = `Bar clock ticking: ${barElapsed}s ago · Next bar in ${remain}s`;
    }, 1000);
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

  async function refreshRunnerMarks(id) {
    try {
      const data = await api("/api/portfolio/runner/" + id + "/refresh-marks", "POST", {});
      const msg = data.message || "marks refreshed";
      state.refreshStatus[id] = msg;
      addAudit("REFRESH_MARKS [" + id.slice(0, 8) + "] — " + msg, "action");
      if (state.portfolio) renderMatrix(state.portfolio);
      toast(msg, data.still_failing > 0 || data.cooldown ? "warning" : "success");
    } catch (e) {
      toast("Refresh marks failed: " + e.message, "error");
    }
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
      // 2026-09-22: group by signal_kind so option vs equity strategies are
      // visually obvious at pick time — the "which strategy is for which
      // market" confusion came from one flat alphabetical list.
      const eq = catalogue.filter((s) => s.signal_kind !== "option");
      const op = catalogue.filter((s) => s.signal_kind === "option");
      stratSel.innerHTML =
        '<optgroup label="📈 Stocks / Equity">' +
        eq.map((s) => '<option value="' + s.name + '">' + s.name + "</option>").join("") +
        '</optgroup><optgroup label="🎯 Options (index)">' +
        op.map((s) => '<option value="' + s.name + '">' + s.name + "</option>").join("") +
        "</optgroup>";
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

      // Routing selectors first and in their own guard (2026-10-02): a render
      // error below (e.g. a missing component) must never hide the segment/
      // broker choice again — that left the Broker dropdown with only the
      // static "— default routing —" placeholder.
      try {
        loadSegmentSelector();
        loadBrokerSelector();
      } catch (e) { toast("Broker/segment picker failed: " + e.message, "error"); }
      renderSpawnParams();
      syncSpawnForm();
    } catch (e) { toast("Failed to load spawn form: " + e.message, "error"); }
  }

  // Multi-broker Phase B: segment selector — runners are created INTO a
  // segment (capital partition mapped to a broker). Hidden when no segments
  // are configured (legacy single-broker behaviour unchanged).
  async function loadSegmentSelector() {
    const row = $("spawn-segment-row");
    const sel = $("spawn-segment");
    if (!row || !sel) return;
    let segments = [];
    try {
      const data = await fetch("/api/segments").then((r) => r.json());
      segments = (data && data.segments) || [];
    } catch (e) { /* no segments API — keep hidden */ }
    if (!segments.length) { row.hidden = true; return; }
    row.hidden = false;
    sel.innerHTML = '<option value="">— none (legacy default) —</option>' +
      segments.map((s) =>
        '<option value="' + s.name + '" data-mode="' + s.mode + '" data-broker="' + s.broker + '"' +
        ' data-capital="' + (s.allocated_capital || 0) + '">' +
        (s.display_name || s.name) + "</option>").join("");
    sel._segments = segments;
    if (!sel._wired) {
      sel._wired = true;
      sel.addEventListener("change", () => {
        // S2: any change event on the segment control is a user decision —
        // the strategy-driven preselect must stop overriding it.
        sel._userChose = true;
        applySegment((sel._segments || []).find((s) => s.name === sel.value));
      });
    }
    // A strategy may already be selected (loadSpawnForm calls syncSpawnForm
    // before this async fetch resolves) — apply the preselect now too.
    preselectSegment();
  }

  // S2 (issues.txt 2026-10-01): every strategy is LINKED to a segment so
  // backtest → paper → live land in the same capital partition. The link is
  // the strategy's explicit `default_segment` when it declares one, else its
  // signal_kind (option strategies belong in the index-options segment,
  // equity strategies in the equity-intraday one). It is a preselect, not a
  // lock: the operator can always pick another segment before deploying.
  function preferredSegmentFor(strat) {
    if (strat && strat.default_segment) return strat.default_segment;
    const isOption = !!(strat && strat.signal_kind === "option");
    return isOption ? "options_index" : "equity_intraday";
  }

  function preselectSegment() {
    const sel = $("spawn-segment");
    if (!sel || !sel._segments || !sel._segments.length || sel._userChose) return;
    const want = preferredSegmentFor(selectedStrategy());
    if (sel.value === want) return;
    const seg = sel._segments.find((s) => s.name === want);
    if (!seg) return; // segment not configured — leave the form untouched
    sel.value = want;
    applySegment(seg);
  }

  /** Apply a picked segment to the form: hint, bucket mode and broker sync. */
  function applySegment(seg) {
    const sel = $("spawn-segment");
    const hint = $("spawn-segment-hint");
    if (!sel) return;
    sel._picked = seg || null;
    if (!seg) { if (hint) hint.textContent = ""; return; }
    const cap = Number(seg.allocated_capital || 0);
    const capLabel = cap >= 100000 ? "₹" + (cap / 100000).toFixed(cap % 100000 ? 1 : 0) + "L"
      : "₹" + cap.toLocaleString("en-IN");
    if (hint) {
      hint.textContent = seg.broker + " • " + (seg.mode === "live" ? "Live" : "Paper") +
        " • " + capLabel;
    }
    // The segment defines the bucket mode — sync the mode select so the
    // form never submits a conflicting pair (the API refuses those).
    const modeSel = $("spawn-mode");
    if (modeSel && seg.mode) modeSel.value = seg.mode;
    // Sync the broker select to the segment's broker so the venue the
    // orders will route to is always visible. Changing it afterwards is
    // the explicit execution_broker override (wins over the segment per
    // the routing rules).
    const brokerSel = $("spawn-broker");
    if (brokerSel && seg.broker &&
        brokerSel.querySelector('option[value="' + seg.broker + '"]')) {
      brokerSel.value = seg.broker;
      brokerSel.dispatchEvent(new Event("change"));
    }
  }

  // Multi-broker: the direct execution-venue choice. Populated from the
  // broker registry (GET /api/broker/list) so a newly registered broker
  // appears without UI changes. Value feeds the runner's execution_broker —
  // for live runners the explicit choice wins over the segment's broker
  // (PRD §4.3); for paper runners it is recorded as intent while fills stay
  // simulated on the paper book.
  async function loadBrokerSelector() {
    const sel = $("spawn-broker");
    const hint = $("spawn-broker-hint");
    if (!sel) return;
    let brokers = [];
    try {
      const data = await fetch("/api/broker/list").then((r) => r.json());
      brokers = (data && data.brokers) || [];
    } catch (e) { /* registry API unavailable — keep the default option */ }
    sel.innerHTML = '<option value="">— default routing —</option>' +
      brokers.map((b) =>
        '<option value="' + b.name + '">' + (b.display_name || b.name) + "</option>").join("");
    sel._brokers = brokers;
    if (!sel._wired) {
      sel._wired = true;
      sel.addEventListener("change", () => {
        if (!hint) return;
        const broker = (sel._brokers || []).find((b) => b.name === sel.value);
        if (!broker) { hint.textContent = ""; return; }
        const seg = ($("spawn-segment") || {})._picked;
        const override = !!(seg && seg.broker && seg.broker !== sel.value);
        hint.textContent = (broker.authenticated ? "session live" : "not authenticated") +
          (override ? " — overrides segment “" + (seg.display_name || seg.name) + "”" : "");
      });
    }
  }

  function renderSpawnParams() {
    const sel = $("spawn-strategy");
    const strat = (sel._catalogue || []).find((s) => s.name === sel.value);
    const box = $("spawn-params");
    if (!strat || !strat.params) { box.innerHTML = ""; return; }
    // issues.txt P2 (2026-10-01): for an option strategy the Instrument field
    // already asks NIFTY/BANKNIFTY — rendering the `underlying` param beside
    // it asked the same question twice ("static Nifty" vs the picker) and the
    // two could disagree. The field is suppressed here and synced from the
    // Instrument at submit; the adapter still reads params.underlying for the
    // view, so the value must travel, just not as a second question.
    const isOption = strat.signal_kind === "option";
    const entries = Object.entries(strat.params)
      .filter(([key]) => !(isOption && key === "underlying"));
    if (!entries.length) { box.innerHTML = ""; return; }
    box.innerHTML = "<div class='spawn-params-title'>Strategy parameters</div>" +
      entries.map(([key, spec], index) => {
        const type = spec.type === "bool" ? "checkbox"
          : spec.type === "int" || spec.type === "float" ? "number" : "text";
        const val = spec.default !== null && spec.default !== undefined ? spec.default : "";
        return '<div class="form-row"><label for="spawn-param-' + index + '">' + (spec.label || key) +
          (spec.tooltip ? ' <span class="muted" title="' + spec.tooltip + '">ⓘ</span>' : "") +
          '</label><input id="spawn-param-' + index + '" class="input spawn-param" data-param="' + key +
          '" type="' + type + '" value="' + val + '"></div>';
      }).join("");
  }

  /**
   * Lot-size unit label + capital guard (2026-09-22).
   * Pulls the real exchange lot sizes from /api/symbols/lot-sizes
   * (mStock scriptmaster when logged in, static fallback otherwise) and:
   * 1. shows "1 lot = N units" next to the Lots field for the selected index;
   * 2. warns (and blocks submit) if lots × units × est. premium > allocated
   *    capital — oversizing a spread should fail at the form, not in the book.
   */
  let LOT_SIZES = null;
  function fetchLotSizes() {
    if (LOT_SIZES) return Promise.resolve(LOT_SIZES);
    return api("/api/symbols/lot-sizes").then((d) => {
      LOT_SIZES = (d && d.lot_sizes) || {};
      return LOT_SIZES;
    }).catch(() => (LOT_SIZES = {}));
  }

  function selectedLotUnits() {
    const sel = $("spawn-symbol-select");
    const idx = (sel && !sel.hidden ? sel.value : $("spawn-symbol").value || "").toUpperCase();
    return LOT_SIZES ? LOT_SIZES[idx] || 0 : 0;
  }

  function syncLotUnitLabel() {
    const units = selectedLotUnits();
    let hint = document.getElementById("spawn-lots-hint");
    if (!hint) {
      hint = document.createElement("small");
      hint.id = "spawn-lots-hint";
      hint.className = "muted";
      const lotsInput = $("spawn-lots");
      if (lotsInput && lotsInput.parentNode) lotsInput.parentNode.appendChild(hint);
    }
    if (units > 0) {
      const lotsN = Math.max(1, parseInt($("spawn-lots") && $("spawn-lots").value, 10) || 1);
      hint.textContent = "1 lot = " + units + " units · " + lotsN + " lot" + (lotsN === 1 ? "" : "s") + " = " + (lotsN * units) + " units per leg";
    } else {
      hint.textContent = "";
    }
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
    const strat = selectedStrategy();
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

    // Symbol: RESTRICTED strategies (eligible_instruments) get a dropdown of
    // exactly those instruments — no free typing, no wrong-instrument errors.
    // Option runners also get the lots-per-leg field (defaults 1, hidden otherwise).
    // Open strategies keep the free-text input (equity symbols are open-ended),
    // but the EQUITY strategy type also gets the shared SymbolPicker so the
    // user sees every equity instrument the server knows instead of typing a
    // ticker blind — the issue.txt complaint was "did not show all the symboles
    // in the instrument section when selecting the equity".
    const symbolInput = $("spawn-symbol");
    const symbolSelect = $("spawn-symbol-select");
    const eligible = strat && strat.eligible_instruments;
    const pickerExtras = $("spawn-symbol-picker-extras");
    if (!isOption && !eligible) {
      // Free equity strategy (no eligible_instruments): mount the SymbolPicker
      // on the Equity tab so the user picks from the full equity universe
      // instead of typing a ticker blind — the issue.txt complaint was "did
      // not show all the symboles in the instrument section when selecting
      // the equity". Re-mounted on every sync (cheap: one coverage fetch, and
      // this mount adds no listeners to the select) so the handle is never
      // stale after a visit to an option/restricted strategy.
      symbolSelect._picker = SymbolPicker.mount({
        select: symbolSelect,
        search: null,
        tabs: "spawn-symbol-tabs",
        summary: "spawn-symbol-hint",
        defaultTab: "equity",
        placeholder: "Select an equity instrument…",
      });
      symbolSelect.hidden = false;
      symbolInput.hidden = true;
      if (pickerExtras) pickerExtras.hidden = false;
      // Seed the picker with the current value if it is a valid equity symbol.
      if (symbolInput.value.trim()) {
        symbolSelect._picker.setValue(symbolInput.value.trim().toUpperCase());
      }
    } else {
      // Restricted (eligible_instruments) or option strategy: the dropdown of
      // exactly those instruments replaces the picker. Drop the picker handle
      // first so submit-time validation knows which control is active.
      symbolSelect._picker = null;
      if (eligible && eligible.length) {
        symbolSelect.innerHTML = eligible.map((i) =>
          '<option value="' + i + '">' + i + "</option>").join("");
        symbolSelect.hidden = false;
        symbolInput.hidden = true;
        symbolInput.value = eligible[0];
      } else {
        symbolSelect.hidden = true;
        symbolInput.hidden = false;
      }
      if (pickerExtras) pickerExtras.hidden = true;
    }
    if (isOption) {
      symbolInput.setAttribute("list", "spawn-index-list");
      if (!OPTION_INDEXES.includes((symbolInput.value || "").toUpperCase())) {
        symbolInput.value = "NIFTY";
      }
      syncLotUnitLabel();
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

    // Lots-per-leg: option strategies only.
    const lotsRow = $("spawn-lots-row");
    if (lotsRow) {
      lotsRow.hidden = !isOption;
      syncLotUnitLabel();
    }

    // Pool rows follow target type (unchanged behaviour).
    const pool = targetSel.value === "pool";
    $("spawn-symbol-row").hidden = pool;
    $("spawn-universe-row").hidden = !pool;
    $("spawn-maxpos-row").hidden = !pool;

    // S2: the strategy↔segment link preselects the capital partition this
    // strategy is designed for (until the operator picks one themselves).
    preselectSegment();
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
      // 2026-09-22: restricted strategies render a dropdown; read whichever
      // element is active so the dropdown value is what gets spawned.
      symbol: (() => {
        const sel = $("spawn-symbol-select");
        return sel && !sel.hidden ? sel.value : $("spawn-symbol").value.trim();
      })(),
      params,
      // Ticket #10 — the bucket (mode/source) is ALWAYS sent so the runner is
      // labelled from the user's selection; scoped pages default the controls
      // to the page's bucket, but the landing page sends the explicit choice
      // too (never an implicit backend default the user didn't see).
      mode: $("spawn-mode") ? $("spawn-mode").value : (PAGE_MODE || "paper"),
      // Empty when the control is absent: the server's data policy then picks
      // the broker feed. A literal "synthetic" here was a client-side default
      // that could name a source the deployment has switched off.
      source: $("spawn-source") ? $("spawn-source").value : "",
      // Multi-broker Phase B: the segment routes this runner's orders to
      // its broker; empty string → no segment (legacy default path).
      segment: $("spawn-segment") ? ($("spawn-segment").value || null) : null,
      // Direct execution-venue choice: empty → routing falls to the segment's
      // broker, or the legacy default when neither is set. In live mode the
      // explicit broker wins over the segment's broker.
      execution_broker: $("spawn-broker") ? ($("spawn-broker").value || null) : null,
    };

    // U6.1: option routing comes from signal_kind + the selected playbook.
    // The expression block is the playbook's snapshot — never form fields.
    const kind = selectedSignalKind();
    // issues.txt P1: an equity SymbolPicker that was never answered must not
    // silently fall back to the template's RELIANCE default — the user picked
    // nothing, and "nothing" becoming a live RELIANCE runner is a surprise.
    if (kind === "equity" && targetType !== "pool") {
      const sel = $("spawn-symbol-select");
      if (sel && !sel.hidden && sel._picker && !body.symbol) {
        toast("Pick an instrument from the list — nothing is selected.", "error");
        return;
      }
    }
    if (kind === "option") {
      const symbolUpper = (body.symbol || "").toUpperCase();
      if (!OPTION_INDEXES.includes(symbolUpper)) {
        toast("Option strategies trade an index — pick NIFTY or BANKNIFTY.", "error");
        return;
      }
      // issues.txt P2: the suppressed `underlying` param follows the
      // Instrument choice — a BANKNIFTY runner must never carry a NIFTY
      // view (the adapter builds the view from params.underlying).
      params.underlying = symbolUpper;
      const pbId = $("spawn-playbook") ? $("spawn-playbook").value : "";
      const lots = Math.max(1, parseInt($("spawn-lots") && $("spawn-lots").value, 10) || 1);
      // Capital guard: lots × exchange units × est. premium must fit the
      // allocation — oversizing fails HERE, not as a -98% runner in the book.
      const units = selectedLotUnits();
      const capital = parseFloat($("spawn-capital").value) || 0;
      if (units > 0 && capital > 0) {
        const estPremium = 150; // conservative per-unit estimate for ATM-adjacent legs
        const estExposure = lots * units * estPremium;
        if (estExposure > capital) {
          toast(
            lots + " lot" + (lots === 1 ? "" : "s") + " × " + units + " units ≈ ₹" +
            estExposure.toLocaleString("en-IN") + " exposure — above your ₹" +
            capital.toLocaleString("en-IN") + " allocation. Lower the lots or raise capital.",
            "error"
          );
          return;
        }
      }
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
          if (body.instrument && body.instrument.expression) {
            body.instrument.expression.quantity = lots;
          }
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
            quantity: lots,
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
      // 2026-09-24: keep the symbol resolved at the TOP of submitSpawn (the
      // Instrument dropdown when it is visible). Re-reading the hidden
      // free-text input here clobbered the dropdown with eligible[0] — a
      // BANKNIFTY dropdown selection spawned a NIFTY runner.
      if (!body.symbol) body.symbol = $("spawn-symbol").value.trim();
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

    // PnL-vs-spot export (2026-09-24): manual trigger of the same sweep the
    // 15:30 IST scheduler runs server-side. Results land in charts/<date>/.
    const exportBtn = $("btn-watch-export");
    if (exportBtn) {
      exportBtn.addEventListener("click", async () => {
        exportBtn.disabled = true;
        try {
          const data = await api("/api/portfolio/watch/export", "POST", {});
          const n = (data.exported || []).length;
          toast(
            n ? "Exported " + n + " chart(s) to charts/" : "Nothing to export — no live option runner has data yet",
            n ? "success" : "error"
          );
        } catch (e) {
          toast("Export failed: " + e.message, "error");
        } finally {
          exportBtn.disabled = false;
        }
      });
    }
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
      } else if (act === "refresh_marks") {
        refreshRunnerMarks(id);
      } else if (act === "remove") {
        // Destructive: confirm first — removal flattens the book and
        // deletes the instance; there is no undo.
        if (!confirm("Remove this instance? Its open positions are flattened and the row is deleted (no undo).")) return;
        try {
          await api("/api/portfolio/runner/" + id, "DELETE");
          toast("Instance removed.", "success");
        } catch (err) { toast("Remove failed: " + err.message, "error"); }
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
      // Scope honesty: on a bucket page the API flattens ONLY that bucket —
      // the modal must say so, never "all runners".
      const scopeLine = $("emergency-scope-line");
      if (scopeLine) {
        scopeLine.innerHTML = PAGE_MODE
          ? `This will <strong>cancel new entries and market-exit every open position in the ${PAGE_MODE.toUpperCase()} bucket only</strong>, then halt that bucket. Other buckets keep trading.`
          : "This will <strong>cancel new entries and market-exit every open position across all runners</strong>, then halt the portfolio.";
      }
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
    // Naming convention (2026-09-22): show the auto-name live while the name
    // field is empty, so every instance reads strategy·instrument·timeframe.
    const nameInput = $("spawn-name");
    const updateNameHint = () => {
      if (!nameInput) return;
      nameInput.removeAttribute("placeholder");
      if (nameInput.value.trim()) return; // user is naming it — stay out of the way
      const sel = $("spawn-symbol-select");
      const instrument = sel && !sel.hidden ? sel.value : $("spawn-symbol").value;
      nameInput.placeholder = "auto: " + $("spawn-strategy").value + "·" +
        (instrument || "?") + "·" + $("spawn-timeframe").value;
    };
    if (nameInput) {
      nameInput.addEventListener("input", updateNameHint);
      ["spawn-strategy", "spawn-timeframe", "spawn-symbol", "spawn-symbol-select"].forEach((id) => {
        const el = $(id);
        if (el) el.addEventListener("change", () => { updateNameHint(); });
      });
    }
    // Index datalist for the option symbol picker.
    if (!document.getElementById("spawn-index-list")) {
      const dl = document.createElement("datalist");
      dl.id = "spawn-index-list";
      dl.innerHTML = OPTION_INDEXES.map((i) => '<option value="' + i + '">').join("");
      document.body.appendChild(dl);
    }
    // Real lot sizes: prefetch once, refresh the unit label on lots/instrument
    // changes (2026-09-22 — user-selected lots show exchange units + a
    // capital guard at submit).
    fetchLotSizes().then(() => syncLotUnitLabel());
    ["spawn-lots", "spawn-symbol", "spawn-symbol-select"].forEach((id) => {
      const el = $(id);
      if (el) el.addEventListener("change", syncLotUnitLabel);
      if (el) el.addEventListener("input", syncLotUnitLabel);
    });
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
        // Orders are a REST read (the ledger is not in the SSE payload, only
        // its counts are) — refresh the moment the tab is opened.
        if (state.tab === "orders" && window.OrdersTab) window.OrdersTab.refresh(true);
      }));

    // Positions tab controls: a client-side filter over the same snapshot the
    // table already renders, so "which of my positions has no stop?" is one
    // click during a live session.
    const posSearch = $("pos-search");
    if (posSearch) posSearch.addEventListener("input", (e) => {
      state.posSearch = e.target.value;
      renderPositionRows(POSITION_ROWS);
    });
    const posRules = $("pos-rules-only");
    if (posRules) posRules.addEventListener("change", (e) => {
      state.posRulesOnly = e.target.checked;
      renderPositionRows(POSITION_ROWS);
    });
    const posRefresh = $("pos-refresh");
    if (posRefresh) posRefresh.addEventListener("click", () => {
      if (state.portfolio) renderAggregatePositions(state.portfolio);
      toast("Positions re-rendered from the latest snapshot", "success");
    });

    // Manual position management (Live Order Management): the action modals +
    // the delegated buttons in the positions table.
    if (window.PositionActions) {
      window.PositionActions.init({
        tableId: "aggregate-positions",
        rows: POSITION_ROWS,
        toast,
        onDone: () => {
          if (state.portfolio) renderAggregatePositions(state.portfolio);
          if (window.OrdersTab) window.OrdersTab.refresh(true);
          fetchBackendAudit();
        },
      });
    }
    if (window.OrdersTab) {
      window.OrdersTab.init({
        mode: PAGE_MODE,
        toast,
        onChanged: () => { if (state.portfolio) renderAggregatePositions(state.portfolio); },
      });
    }

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
