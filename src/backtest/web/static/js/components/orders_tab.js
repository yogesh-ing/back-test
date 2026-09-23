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
 * Reads `/api/portfolio/orders` (bucketed by the page's mode) and cancels via
 * `/api/portfolio/orders/<coid>/cancel`. Relative URLs only, so the page works
 * through the platform proxy.
 */
const OrdersTab = (function () {
  "use strict";

  const LIST_URL = "/api/portfolio/orders";
  const CANCEL_URL = (coid) => "/api/portfolio/orders/" + encodeURIComponent(coid) + "/cancel";
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
    filters: { status: "", search: "", limit: 100 },
    toast: null,
    onChanged: null,
  };

  const $ = (id) => document.getElementById(id);

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

  function ageText(order) {
    if (order.status !== "PENDING") return "";
    const created = Date.parse(String(order.created_ts || "").replace(" ", "T"));
    if (!Number.isFinite(created)) return "";
    const secs = Math.max(0, Math.round((Date.now() - created) / 1000));
    if (secs < 60) return secs + "s old";
    if (secs < 3600) return Math.round(secs / 60) + "m old";
    return Math.round(secs / 3600) + "h old";
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
          ? ageText(order)
          : esc((order.tag && order.tag.reason) || (order.tag && order.tag.kind) || "filled");
    return (
      '<tr class="order-row ' + meta.cls + '">' +
      "<td>" + clock(order.updated_ts || order.created_ts) + "</td>" +
      "<td>" + esc(order.runner || String(order.instance_id || "").slice(0, 8)) + "</td>" +
      '<td class="cell-name">' + esc(order.symbol) + "</td>" +
      "<td>" + esc(order.side) + "</td>" +
      '<td class="num">' + num(order.quantity, 0) + "</td>" +
      '<td class="num">' + num(order.requested_price) + "</td>" +
      '<td class="num">' + num(order.avg_fill_price) + "</td>" +
      slippageCell(order) +
      '<td><span class="order-status">' + meta.dot + " " + esc(meta.label) + "</span></td>" +
      '<td class="muted">' + detail + "</td>" +
      "<td>" + (order.cancellable
        ? '<button class="row-btn row-btn-stop" data-cancel="' + esc(order.client_order_id) +
          '" title="Cancel this working order">✕ Cancel</button>'
        : '<span class="muted">—</span>') + "</td>" +
      "</tr>"
    );
  }

  function visibleOrders() {
    const q = String(state.filters.search || "").trim().toLowerCase();
    if (!q) return state.orders;
    return state.orders.filter((o) =>
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
        ? dot("Oldest working", Math.round(s.oldest_pending_age_s) + "s",
              s.oldest_pending_age_s > 60 ? "orders-stat-bad" : "orders-stat-warn")
        : "");
  }

  function renderRows() {
    const body = $("orders-body");
    if (!body) return;
    const rows = visibleOrders();
    if (!rows.length) {
      const why = state.orders.length
        ? "No orders match the current filter."
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
    const body = $("orders-body");
    if (body && !body._ordersBound) {
      body.addEventListener("click", (e) => {
        const btn = e.target && e.target.closest ? e.target.closest("[data-cancel]") : null;
        if (!btn) return;
        if (typeof window !== "undefined" && window.confirm &&
            !window.confirm("Cancel this working order?")) return;
        cancelOrder(btn.dataset.cancel);
      });
      body._ordersBound = true;
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

  return { init, refresh, noteTick, cancelOrder, _state: state };
})();

if (typeof globalThis !== "undefined") globalThis.OrdersTab = OrdersTab;
