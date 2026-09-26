/* Orders tab — the order ledger's read surface (Live Order Management).
 *
 * "Open Positions" answers *what am I holding*. This tab answers the other
 * question an operator asks during a live session: *did my order fill, and at
 * what price?* The data is the engine's own OrderLedger — every order it
 * tagged, with the fate it actually had:
 *
 *   PENDING   still working (aged, so a stuck order is visible)
 *   FILLED    filled, with the requested price, the fill price and the
 *             slippage between them (adverse-positive — +₹2/unit means the
 *             fill cost money, whichever side it was)
 *   REJECTED  terminal failure with the reason (never left PENDING forever)
 *   CANCELLED cancelled locally (venue refusal / operator cancel)
 *
 * Phase 3 adds the two things a passive ledger cannot do:
 *
 *   AMEND     a working order at its venue (quantity / limit price) via
 *             `POST /api/portfolio/orders/<coid>/modify`. The venue is asked
 *             first — a refusal leaves local state untouched and is shown
 *             verbatim. Only orders the server marks `modifiable` (working at
 *             a venue) get the button; a simulated order fills or rejects in
 *             the same call, so amending one would amend nothing.
 *   AGING      a working order past the server's thresholds is badged and
 *             colour-banded (warn 60s / alert 5min). The bands come from the
 *             server with the rows, so the page, the audit trail and the
 *             engine's own alerts agree on what "stuck" means.
 *
 * Reads `/api/portfolio/orders` (bucketed by the page's mode) and cancels via
 * `/api/portfolio/orders/<coid>/cancel`. Relative URLs only, so the page works
 * through the platform proxy.
 */
const OrdersTab = (function () {
  "use strict";

  const LIST_URL = "/api/portfolio/orders";
  const CANCEL_URL = (coid) => "/api/portfolio/orders/" + encodeURIComponent(coid) + "/cancel";
  const MODIFY_URL = (coid) => "/api/portfolio/orders/" + encodeURIComponent(coid) + "/modify";
  const REFRESH_MS = 3000; // while the tab is visible; the SSE already runs at 1 Hz

  const STATUS_META = {
    PENDING: { dot: "⏳", cls: "order-pending", label: "Pending" },
    FILLED: { dot: "✅", cls: "order-filled", label: "Filled" },
    REJECTED: { dot: "⛔", cls: "order-rejected", label: "Rejected" },
    CANCELLED: { dot: "🚫", cls: "order-cancelled", label: "Cancelled" },
  };

  const state = {
    mode: "",
    orders: [],
    summary: null,
    lastFetch: 0,
    inFlight: false,
    filters: { status: "", search: "", limit: 100, agingOnly: false },
    toast: null,
    onChanged: null,
    //: The order the open amend modal is acting on (never a "current row"
    //: global that a 3s refresh could swap underneath the form).
    amendTarget: null,
    amendBusy: false,
  };

  const $ = (id) => document.getElementById(id);

  const statusLabel = (order) =>
    (STATUS_META[order.status] || { label: order.status }).label;

  function toast(message, kind) {
    if (state.toast) return state.toast(message, kind);
    if (typeof window !== "undefined" && window.showToast) return window.showToast(message, kind);
    console.log("[orders]", kind || "info", message);
  }

  const money = (v) =>
    typeof Money !== "undefined" ? Money.format(v) : "₹" + Number(v || 0).toFixed(2);
  const num = (v, dp) =>
    v === null || v === undefined ? "—"
      : Number(v).toLocaleString("en-IN", { minimumFractionDigits: dp || 2, maximumFractionDigits: dp || 2 });

  function esc(text) {
    return String(text === null || text === undefined ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /** "14:07:31" from an ISO stamp (the date is noise inside a session view). */
  function clock(iso) {
    const text = String(iso || "");
    const m = text.match(/T(\d{2}:\d{2}:\d{2})/);
    return m ? m[1] : (text.slice(11, 19) || "—");
  }

  function ageSeconds(order) {
    // The server's age is authoritative when present (it is what the aging
    // bands and the engine's alerts were computed from); the client only
    // falls back to its own arithmetic for a row that carries no age.
    if (order.age_s !== null && order.age_s !== undefined) return Number(order.age_s);
    const created = Date.parse(String(order.created_ts || "").replace(" ", "T"));
    if (!Number.isFinite(created)) return null;
    return Math.max(0, Math.round((Date.now() - created) / 1000));
  }

  function ageText(order) {
    if (order.status !== "PENDING") return "";
    const secs = ageSeconds(order);
    if (secs === null) return "";
    if (secs < 60) return secs + "s";
    if (secs < 3600) return Math.round(secs / 60) + "m";
    return Math.round(secs / 3600) + "h";
  }

  /** Age cell: a plain number normally, a warning once the venue goes quiet. */
  function ageCell(order) {
    if (order.status !== "PENDING") return '<td class="num muted">—</td>';
    const band = order.aging || "";
    const text = ageText(order);
    if (!band) return '<td class="num muted">' + text + "</td>";
    const icon = band === "alert" ? "🚨" : "⏰";
    const why = band === "alert"
      ? "Working far longer than a fill should take — the venue is not answering"
      : "Working longer than normal — watch it";
    return '<td class="num order-aging-' + band + '" title="' + esc(why) + '">' +
      icon + " " + text + "</td>";
  }

  /** Retry lineage (Phase 3 auto-retry) — what this order replaced. */
  function lineageText(order) {
    if (!order.retry_of) return "";
    return "↻ retry " + (order.retry_attempt || 1) + " of " + String(order.retry_of).slice(0, 18);
  }

  function amendText(order) {
    if (!order.amend_count) return "";
    const bits = [];
    if (order.amended_quantity !== null && order.amended_quantity !== undefined) {
      bits.push("qty " + Number(order.amended_quantity).toLocaleString("en-IN"));
    }
    if (order.amended_limit_price !== null && order.amended_limit_price !== undefined) {
      bits.push("limit " + num(order.amended_limit_price));
    }
    return "✎ amended" + (bits.length ? " → " + bits.join(", ") : "");
  }

  function slippageCell(order) {
    if (order.status !== "FILLED" || order.slippage === null || order.slippage === undefined) {
      return '<td class="num muted">—</td>';
    }
    const slip = Number(order.slippage);
    const pct = order.slippage_pct === null || order.slippage_pct === undefined
      ? null : Number(order.slippage_pct) * 100;
    // Adverse-positive: positive = the fill cost money.
    const cls = slip > 0 ? "pnl-neg" : slip < 0 ? "pnl-pos" : "pnl-flat";
    const tone = slip > 0 ? "worse than the request" : slip < 0 ? "better than the request" : "filled at the request";
    return '<td class="num ' + cls + '" title="' + esc(tone) + '">' +
      (slip > 0 ? "+" : "") + money(slip).replace("-", "-") +
      (pct === null ? "" : ' <span class="muted">(' + pct.toFixed(2) + "%)</span>") +
      "</td>";
  }

  function rowHtml(order) {
    const meta = STATUS_META[order.status] || { dot: "•", cls: "", label: order.status };
    const detail = order.status === "REJECTED"
      ? esc(order.reject_reason || "rejected")
      : order.status === "CANCELLED"
        ? "cancelled locally"
        : order.status === "PENDING"
          ? esc((order.tag && order.tag.reason) || (order.tag && order.tag.kind) || "working")
          : esc((order.tag && order.tag.reason) || (order.tag && order.tag.kind) || "filled");
    const notes = [lineageText(order), amendText(order)].filter(Boolean).join(" · ");
    return (
      '<tr class="order-row ' + meta.cls + (order.aging ? " order-row-" + order.aging : "") + '">' +
      "<td>" + clock(order.updated_ts || order.created_ts) + "</td>" +
      "<td>" + esc(order.runner || String(order.instance_id || "").slice(0, 8)) + "</td>" +
      '<td class="cell-name">' + esc(order.symbol) + "</td>" +
      "<td>" + esc(order.side) + "</td>" +
      '<td class="num">' + num(order.quantity, 0) + "</td>" +
      '<td class="num">' + num(order.requested_price) + "</td>" +
      '<td class="num">' + num(order.avg_fill_price) + "</td>" +
      slippageCell(order) +
      '<td><span class="order-status">' + meta.dot + " " + esc(meta.label) + "</span></td>" +
      ageCell(order) +
      '<td class="muted">' + detail +
        (notes ? '<div class="cell-sub">' + esc(notes) + "</div>" : "") + "</td>" +
      '<td><div class="pos-actions">' + (order.modifiable
        ? '<button class="row-btn" data-amend="' + esc(order.client_order_id) +
          '" title="Amend this working order at the venue">✎ Amend</button>'
        : "") + (order.cancellable
        ? '<button class="row-btn row-btn-stop" data-cancel="' + esc(order.client_order_id) +
          '" title="Cancel this working order">✕ Cancel</button>'
        : (order.modifiable ? "" : '<span class="muted">—</span>')) + "</div></td>" +
      "</tr>"
    );
  }

  function visibleOrders() {
    let rows = state.orders;
    if (state.filters.agingOnly) {
      rows = rows.filter((o) => o.aging === "warn" || o.aging === "alert");
    }
    const q = String(state.filters.search || "").trim().toLowerCase();
    if (!q) return rows;
    return rows.filter((o) =>
      String(o.symbol || "").toLowerCase().includes(q) ||
      String(o.runner || "").toLowerCase().includes(q) ||
      String(o.instance_id || "").toLowerCase().includes(q) ||
      String(o.status || "").toLowerCase().includes(q));
  }

  function renderSummary() {
    const el = $("orders-summary");
    if (!el) return;
    const s = state.summary || {};
    const dot = (label, value, cls) =>
      '<span class="orders-stat ' + (cls || "") + '"><span class="muted">' + label +
      "</span><strong>" + value + "</strong></span>";
    el.innerHTML =
      dot("Working", s.pending || 0, s.pending ? "orders-stat-warn" : "") +
      dot("Filled", s.filled || 0) +
      dot("Rejected", s.rejected || 0, s.rejected ? "orders-stat-bad" : "") +
      dot("Cancelled", s.cancelled || 0) +
      dot("Total", s.total || 0) +
      dot("Avg slippage",
        (s.slippage_samples ? (Number(s.avg_slippage) > 0 ? "+" : "") + money(s.avg_slippage) : "—"),
        Number(s.avg_slippage) > 0 ? "orders-stat-warn" : "") +
      dot("Worst slippage",
        (s.slippage_samples ? "+" + money(s.worst_slippage) : "—")) +
      (s.oldest_pending_age_s
        ? dot("Oldest working",
              (s.oldest_pending_coid ? String(s.oldest_pending_coid).slice(0, 12) + " · " : "") +
              Math.round(s.oldest_pending_age_s) + "s",
              s.aging_alert_count ? "orders-stat-bad"
                : s.aging_warn_count ? "orders-stat-warn" : "")
        : "") +
      ((s.aging_warn_count || s.aging_alert_count)
        ? dot("⏰ Aging", (s.aging_warn_count || 0) + " warn · " + (s.aging_alert_count || 0) + " alert",
              s.aging_alert_count ? "orders-stat-bad" : "orders-stat-warn")
        : "");
  }

  function renderRows() {
    const body = $("orders-body");
    if (!body) return;
    const rows = visibleOrders();
    if (!rows.length) {
      const why = state.orders.length
        ? (state.filters.agingOnly
            ? "Nothing is aging — every working order is inside the normal window."
            : "No orders match the current filter.")
        : "No orders in this scope yet — nothing has been routed through the ledger.";
      body.innerHTML = '<tr><td colspan="11" class="muted" style="padding:16px">' + why + "</td></tr>";
      return;
    }
    body.innerHTML = rows.map(rowHtml).join("");
  }

  function updateBadge() {
    const badge = $("orders-tab-badge");
    if (!badge) return;
    // The badge is a nudge, not a notification system: it shows that something
    // needs attention (working or rejected orders) while the tab is closed.
    const working = state.summary ? Number(state.summary.pending || 0) : 0;
    const rejected = state.summary ? Number(state.summary.rejected || 0) : 0;
    const count = working + rejected;
    badge.hidden = count === 0;
    badge.textContent = working && rejected
      ? working + "⏳ " + rejected + "⛔"
      : working ? working + "⏳" : rejected + "⛔";
    badge.title = working + " working, " + rejected + " rejected";
  }

  function query() {
    const params = new URLSearchParams();
    if (state.mode) params.set("mode", state.mode);
    if (state.filters.status) params.set("status", state.filters.status);
    params.set("limit", String(state.filters.limit || 100));
    return LIST_URL + "?" + params.toString();
  }

  async function refresh(force) {
    if (state.inFlight) return state.orders;
    if (!force && Date.now() - state.lastFetch < REFRESH_MS) return state.orders;
    state.inFlight = true;
    try {
      const res = await fetch(query());
      const data = await res.json();
      if (data.success === false) throw new Error(data.error || "orders fetch failed");
      state.orders = data.orders || [];
      state.summary = data.summary || null;
      state.lastFetch = Date.now();
      renderSummary();
      renderRows();
      updateBadge();
    } catch (err) {
      toast("Orders refresh failed: " + err.message, "error");
    } finally {
      state.inFlight = false;
    }
    return state.orders;
  }

  async function cancelOrder(coid) {
    try {
      const res = await fetch(CANCEL_URL(coid), { method: "POST" });
      const data = await res.json();
      if (!res.ok || data.success === false) throw new Error(data.error || ("HTTP " + res.status));
      toast("Order cancelled: " + coid.slice(0, 18), "success");
      await refresh(true);
      if (typeof state.onChanged === "function") state.onChanged(data);
    } catch (err) {
      toast(err.message, "error");
      await refresh(true);
    }
  }

  // ---------------------------------------------------------- amend a working order

  function openAmend(coid) {
    const order = state.orders.find((o) => o.client_order_id === coid);
    const modal = $("order-modify-modal");
    if (!order || !modal) return;
    state.amendTarget = order;
    const context = $("order-modify-context");
    if (context) {
      context.innerHTML =
        '<div class="pos-modal-line"><strong>' + esc(order.symbol) + "</strong> · " +
        esc(order.side) + " " + num(order.quantity, 0) + " · " + esc(statusLabel(order)) +
        "</div>" +
        '<div class="pos-modal-line muted">working ' + ageText(order) +
        " · venue order " + esc(String(order.broker_order_id || "?")) + "</div>" +
        '<div class="pos-modal-line muted">coid ' + esc(order.client_order_id) + "</div>" +
        (order.amend_count
          ? '<div class="pos-modal-line muted">already amended ' + order.amend_count + "x</div>"
          : "");
    }
    // Pre-fill with what the venue currently holds: an amend is usually a
    // nudge, and retyping the current size invites typos.
    const qty = $("order-modify-qty");
    const price = $("order-modify-price");
    if (qty) qty.value = String(Math.round(Number(order.quantity) || 0));
    if (price) price.value = order.limit_price === null || order.limit_price === undefined
      ? "" : String(order.limit_price);
    modal.hidden = false;
    if (qty) { try { qty.focus(); qty.select(); } catch (_e) { /* stub DOM */ } }
  }

  function closeAmend() {
    const modal = $("order-modify-modal");
    if (modal) modal.hidden = true;
    state.amendTarget = null;
  }

  async function submitAmend() {
    const order = state.amendTarget;
    if (!order || state.amendBusy) return;
    const rawQty = $("order-modify-qty") ? $("order-modify-qty").value : "";
    const rawPrice = $("order-modify-price") ? $("order-modify-price").value : "";
    if (rawQty === "" && rawPrice === "") {
      toast("Nothing to amend — set a quantity or a limit price.", "error");
      return;
    }
    // Send only what actually differs from the venue's current terms: the
    // form is pre-filled, so re-sending the untouched field would make the
    // venue process a change nobody asked for. It also makes "I typed an
    // identical value" a no-op here instead of a pointless round trip.
    const body = {};
    if (rawQty !== "" && Number(rawQty) !== Number(order.quantity)) {
      body.quantity = Number(rawQty);
    }
    const heldLimit = order.limit_price === null || order.limit_price === undefined
      ? null : Number(order.limit_price);
    if (rawPrice !== "" && Number(rawPrice) !== heldLimit) {
      body.limit_price = Number(rawPrice);
    }
    if (body.quantity === undefined && body.limit_price === undefined) {
      toast("The order already has those terms — nothing to amend.", "error");
      return;
    }
    state.amendBusy = true;
    try {
      const res = await fetch(MODIFY_URL(order.client_order_id), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      let data = null;
      try { data = await res.json(); } catch (_e) { data = {}; }
      if (!res.ok || data.success === false) {
        throw new Error((data && data.error) || ("HTTP " + res.status));
      }
      const terms = [];
      if (data.order && data.order.quantity !== undefined) terms.push(num(data.order.quantity, 0) + " units");
      if (data.order && data.order.limit_price) terms.push("limit " + num(data.order.limit_price));
      toast("Amended at the venue: " + esc(order.symbol) + " → " +
        (terms.length ? terms.join(", ") : "new terms"), "success");
      closeAmend();
      await refresh(true);
      if (typeof state.onChanged === "function") state.onChanged(data);
    } catch (err) {
      // The venue refused: nothing changed locally, so the modal stays open
      // and the operator keeps the terms they typed.
      toast(err.message, "error");
    } finally {
      state.amendBusy = false;
    }
  }

  function bind() {
    const refreshBtn = $("orders-refresh");
    if (refreshBtn && !refreshBtn._ordersBound) {
      refreshBtn.addEventListener("click", () => refresh(true));
      refreshBtn._ordersBound = true;
    }
    const statusSel = $("orders-status");
    if (statusSel && !statusSel._ordersBound) {
      statusSel.addEventListener("change", () => {
        state.filters.status = statusSel.value;
        refresh(true);
      });
      statusSel._ordersBound = true;
    }
    const limitSel = $("orders-limit");
    if (limitSel && !limitSel._ordersBound) {
      limitSel.addEventListener("change", () => {
        state.filters.limit = Number(limitSel.value) || 100;
        refresh(true);
      });
      limitSel._ordersBound = true;
    }
    const search = $("orders-search");
    if (search && !search._ordersBound) {
      search.addEventListener("input", () => {
        state.filters.search = search.value;
        renderRows();
      });
      search._ordersBound = true;
    }
    const agingOnly = $("orders-aging-only");
    if (agingOnly && !agingOnly._ordersBound) {
      agingOnly.addEventListener("change", () => {
        state.filters.agingOnly = !!agingOnly.checked;
        renderRows(); // already-loaded rows: no need to ask the server
      });
      agingOnly._ordersBound = true;
    }
    const body = $("orders-body");
    if (body && !body._ordersBound) {
      body.addEventListener("click", (e) => {
        const target = e.target && e.target.closest ? e.target : null;
        if (!target) return;
        const amend = target.closest("[data-amend]");
        if (amend) { openAmend(amend.dataset.amend); return; }
        const btn = target.closest("[data-cancel]");
        if (!btn) return;
        if (typeof window !== "undefined" && window.confirm &&
            !window.confirm("Cancel this working order?")) return;
        cancelOrder(btn.dataset.cancel);
      });
      body._ordersBound = true;
    }
    const amendSubmit = $("order-modify-submit");
    if (amendSubmit && !amendSubmit._ordersBound) {
      amendSubmit.addEventListener("click", submitAmend);
      amendSubmit._ordersBound = true;
    }
  }

  /**
   * init({ mode, toast, onChanged })
   *
   * `mode` is the page's bucket scope ("" on the combined landing page) and is
   * re-applied on every query — a paper page can never list a live order.
   */
  function init(opts) {
    const options = opts || {};
    state.mode = options.mode || "";
    state.toast = options.toast || null;
    state.onChanged = options.onChanged || null;
    bind();
    return OrdersTab;
  }

  /** Called from the host render loop; only refetches while the tab is open. */
  function noteTick(snapshot) {
    if (snapshot && snapshot.orders_summary && !state.summary) {
      // SSE already carries the counts (and the badge) even before the tab is
      // ever opened; the rows themselves come from the REST read, which is
      // where the full order list lives.
      state.summary = snapshot.orders_summary;
      renderSummary();
      updateBadge();
    }
    const panel = $("tab-orders");
    if (!panel || panel.hidden) return;
    refresh(false);
  }

  return { init, refresh, noteTick, cancelOrder, submitAmend, openAmend, closeAmend, _state: state };
})();

if (typeof globalThis !== "undefined") globalThis.OrdersTab = OrdersTab;
