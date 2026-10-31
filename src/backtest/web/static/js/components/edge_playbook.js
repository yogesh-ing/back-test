/**
 * Institutional Edge & Performance Playbook Controller.
 *
 * Pure read-only UI reference controller: zero API calls, zero background
 * timers, and zero mutation of Paper/Live trading state.
 */
(function (root) {
  "use strict";

  const VALID_TABS = ["synergy", "params", "workflow", "prescriptions", "roster", "formulas"];

  function byId(id) {
    return typeof document !== "undefined" && document ? document.getElementById(id) : null;
  }

  function queryAll(container, selector) {
    if (!container || typeof container.querySelectorAll !== "function") return [];
    return Array.from(container.querySelectorAll(selector));
  }

  function selectTab(tabKey, focusButton) {
    const modal = byId("edge-playbook-modal");
    if (!modal) return false;
    const target = VALID_TABS.indexOf(tabKey) >= 0 ? tabKey : "synergy";

    for (let i = 0; i < VALID_TABS.length; i += 1) {
      const key = VALID_TABS[i];
      const btn = byId("epb-tab-btn-" + key);
      const panel = byId("epb-panel-" + key);
      const active = key === target;
      if (btn) {
        btn.setAttribute("aria-selected", active ? "true" : "false");
        btn.tabIndex = active ? 0 : -1;
        if (active && focusButton && typeof btn.focus === "function") {
          btn.focus();
        }
      }
      if (panel) {
        panel.hidden = !active;
      }
    }
    return true;
  }

  function applyFilter(rawQuery) {
    const modal = byId("edge-playbook-modal");
    if (!modal) return { matchedTabs: [], totalMatches: 0 };
    const q = String(rawQuery || "").trim().toLowerCase();
    const banner = byId("epb-search-banner");

    if (!q) {
      queryAll(modal, "[data-epb-searchable]").forEach((el) => {
        el.classList.remove("epb-filter-hidden");
      });
      if (banner) {
        banner.hidden = true;
        banner.textContent = "";
      }
      return { matchedTabs: VALID_TABS.slice(), totalMatches: 0 };
    }

    const matchedTabs = [];
    let totalMatches = 0;

    for (let i = 0; i < VALID_TABS.length; i += 1) {
      const key = VALID_TABS[i];
      const panel = byId("epb-panel-" + key);
      if (!panel) continue;
      const items = queryAll(panel, "[data-epb-searchable]");
      let tabMatches = 0;
      items.forEach((el) => {
        const text = String(el.textContent || "").toLowerCase();
        const hit = text.indexOf(q) >= 0;
        if (hit) {
          el.classList.remove("epb-filter-hidden");
          tabMatches += 1;
          totalMatches += 1;
        } else {
          el.classList.add("epb-filter-hidden");
        }
      });
      if (tabMatches > 0) matchedTabs.push(key);
    }

    // If current tab has 0 matches while another tab matches, jump automatically
    const activeTabBtn = VALID_TABS.map((k) => ({ k, btn: byId("epb-tab-btn-" + k) }))
      .find((x) => x.btn && x.btn.getAttribute("aria-selected") === "true");
    const activeKey = activeTabBtn ? activeTabBtn.k : "synergy";
    if (matchedTabs.length > 0 && matchedTabs.indexOf(activeKey) === -1) {
      selectTab(matchedTabs[0], false);
    }

    if (banner) {
      banner.hidden = false;
      banner.textContent = totalMatches > 0
        ? `Showing ${totalMatches} matching rule${totalMatches === 1 ? "" : "s"} across ${matchedTabs.length} section${matchedTabs.length === 1 ? "" : "s"} for "${rawQuery.trim()}".`
        : `No playbook entries matched "${rawQuery.trim()}". Clear the filter to view all sections.`;
    }
    return { matchedTabs, totalMatches };
  }

  function open(tabKey) {
    const modal = byId("edge-playbook-modal");
    if (!modal) return false;
    const filterInput = byId("epb-filter-input");
    if (filterInput && filterInput.value) {
      filterInput.value = "";
      applyFilter("");
    }
    selectTab(tabKey || "synergy", false);
    modal.hidden = false;
    return true;
  }

  function close() {
    const modal = byId("edge-playbook-modal");
    if (!modal) return false;
    modal.hidden = true;
    return true;
  }

  function bind() {
    const modal = byId("edge-playbook-modal");
    if (!modal || modal.dataset.epbBound === "1") return;
    modal.dataset.epbBound = "1";

    // Open buttons anywhere on the page
    queryAll(document, "[data-open-edge-playbook]").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        if (ev && typeof ev.preventDefault === "function") ev.preventDefault();
        const targetTab = btn.getAttribute("data-open-edge-playbook") || "synergy";
        open(targetTab);
      });
    });

    // Close buttons inside modal
    queryAll(modal, '[data-close="edge-playbook-modal"]').forEach((btn) => {
      btn.addEventListener("click", () => {
        close();
      });
    });

    // Backdrop click
    modal.addEventListener("click", (ev) => {
      if (ev && ev.target === modal) {
        close();
      }
    });

    // Tab buttons + keyboard arrow navigation
    const tablist = byId("epb-tablist");
    if (tablist) {
      queryAll(tablist, "[data-epb-tab]").forEach((btn) => {
        btn.addEventListener("click", () => {
          selectTab(btn.getAttribute("data-epb-tab"), false);
        });
      });

      tablist.addEventListener("keydown", (ev) => {
        if (!ev) return;
        const currentIdx = VALID_TABS.findIndex((k) => {
          const b = byId("epb-tab-btn-" + k);
          return b && b.getAttribute("aria-selected") === "true";
        });
        const idx = currentIdx >= 0 ? currentIdx : 0;
        if (ev.key === "ArrowRight") {
          ev.preventDefault();
          selectTab(VALID_TABS[(idx + 1) % VALID_TABS.length], true);
        } else if (ev.key === "ArrowLeft") {
          ev.preventDefault();
          selectTab(VALID_TABS[(idx - 1 + VALID_TABS.length) % VALID_TABS.length], true);
        } else if (ev.key === "Home") {
          ev.preventDefault();
          selectTab(VALID_TABS[0], true);
        } else if (ev.key === "End") {
          ev.preventDefault();
          selectTab(VALID_TABS[VALID_TABS.length - 1], true);
        }
      });
    }

    // Live filter search input
    const filterInput = byId("epb-filter-input");
    if (filterInput) {
      filterInput.addEventListener("input", () => {
        applyFilter(filterInput.value);
      });
    }

    // Escape key fallback when workspace.js is not mounted
    if (typeof document !== "undefined" && document && typeof document.addEventListener === "function") {
      document.addEventListener("keydown", (ev) => {
        if (ev && ev.key === "Escape" && !modal.hidden) {
          close();
        }
      });
    }

    // URL deep-link (?playbook=1&playbook_tab=params)
    try {
      const search = root && root.location && root.location.search;
      if (search && typeof URLSearchParams === "function") {
        const params = new URLSearchParams(search);
        if (params.get("playbook") === "1" || params.get("playbook") === "true") {
          open(params.get("playbook_tab") || "synergy");
        }
      }
    } catch (_) {
      // Ignore malformed search query
    }
  }

  const api = {
    VALID_TABS,
    open,
    close,
    selectTab,
    applyFilter,
    init: bind,
  };

  if (root) root.EdgePlaybook = api;
  if (typeof globalThis !== "undefined") globalThis.EdgePlaybook = api;

  if (!root || !root.__EDGE_PLAYBOOK_NO_AUTOINIT__) {
    if (typeof document !== "undefined" && document) {
      if (document.readyState === "loading" && typeof document.addEventListener === "function") {
        document.addEventListener("DOMContentLoaded", bind);
      } else {
        bind();
      }
    }
  }
})(typeof window !== "undefined" ? window : globalThis);
