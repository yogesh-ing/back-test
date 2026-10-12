/**
 * Global Market Status Chip (MARKET-STATUS-SPEC-2026-10-02)
 *
 * Visual indicator in topbar header reflecting market status at a glance.
 * State machine handles TRADING_DAY (PRE_OPEN, OPEN, POST_CLOSE),
 * HOLIDAY, and WEEKEND. Recalculates dynamically every second so transitions
 * (like PRE_OPEN -> OPEN at 09:15 IST) flip live without reload.
 */
(function() {
  "use strict";

  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

  let currentState = null;

  function pad(n) {
    return n < 10 ? "0" + n : String(n);
  }

  function getIstDate(now) {
    const d = now || new Date();
    // IST is UTC + 5h 30m
    const utcMs = d.getTime() + (d.getTimezoneOffset() * 60000);
    return new Date(utcMs + (5.5 * 3600000));
  }

  function formatNextOpen(nextOpenStr) {
    if (!nextOpenStr) return null;
    try {
      const dt = new Date(nextOpenStr);
      if (isNaN(dt.getTime())) return null;
      const ist = getIstDate(dt);
      const dayName = DAYS[ist.getDay()];
      const dayNum = pad(ist.getDate());
      const monName = MONTHS[ist.getMonth()];
      const hh = pad(ist.getHours());
      const mm = pad(ist.getMinutes());
      return `${dayName} ${dayNum} ${monName} ${hh}:${mm} IST`;
    } catch (e) {
      return null;
    }
  }

  function resolveLiveState(baseState) {
    if (!baseState) return null;
    const st = Object.assign({}, baseState);

    // If day_state is HOLIDAY or WEEKEND, session_state is always POST_CLOSE
    if (st.day_state === "HOLIDAY" || st.day_state === "WEEKEND") {
      st.session_state = "POST_CLOSE";
      st.is_open = false;
      return st;
    }

    // On a TRADING_DAY, recalculate time-of-day in IST dynamically
    const ist = getIstDate();
    const curMins = ist.getHours() * 60 + ist.getMinutes();
    const curSecs = ist.getSeconds();
    const totalSecs = curMins * 60 + curSecs;

    const preOpenSecs = 9 * 3600;           // 09:00:00
    const marketOpenSecs = 9 * 3600 + 15 * 60; // 09:15:00
    const marketCloseSecs = 15 * 3600 + 30 * 60; // 15:30:00

    if (totalSecs < preOpenSecs) {
      st.session_state = "POST_CLOSE";
      st.is_open = false;
    } else if (totalSecs < marketOpenSecs) {
      st.session_state = "PRE_OPEN";
      st.is_open = false;
    } else if (totalSecs <= marketCloseSecs) {
      st.session_state = "OPEN";
      st.is_open = true;
    } else {
      st.session_state = "POST_CLOSE";
      st.is_open = false;
    }
    return st;
  }

  function renderChip(state) {
    const chip = document.getElementById("market-status-chip");
    if (!chip) return;
    const textEl = document.getElementById("market-status-text");
    if (!textEl) return;

    const live = resolveLiveState(state);
    if (!live) return;

    let visualKey = "POST_CLOSE";
    let label = "NSE CLOSED";

    if (live.day_state === "HOLIDAY") {
      visualKey = "HOLIDAY";
      const rawName = live.holiday_name || "HOLIDAY";
      let displayName = rawName;
      if (displayName.length > 18) {
        displayName = displayName.slice(0, 17) + "…";
      }
      label = `NSE CLOSED · ${displayName}`;
    } else if (live.day_state === "WEEKEND") {
      visualKey = "WEEKEND";
      label = "NSE CLOSED · WEEKEND";
    } else {
      // TRADING_DAY
      if (live.session_state === "OPEN") {
        visualKey = "OPEN";
        label = "NSE OPEN";
      } else if (live.session_state === "PRE_OPEN") {
        visualKey = "PRE_OPEN";
        label = "PRE-OPEN";
      } else {
        visualKey = "POST_CLOSE";
        label = "NSE CLOSED";
      }
    }

    chip.setAttribute("data-state", visualKey);
    textEl.textContent = label;

    // Tooltip formatting
    const formattedNextOpen = live.next_open_formatted || formatNextOpen(live.next_open_ts);
    let title = "NSE Trading Hours: 09:15 - 15:30 IST.";
    if (formattedNextOpen) {
      title += ` Next open: ${formattedNextOpen}.`;
    }
    if (live.day_state === "HOLIDAY" && live.holiday_name) {
      title += ` Holiday: ${live.holiday_name}.`;
    }
    if (live.gap_warning) {
      title += ` Calendar warning: ${live.gap_warning}`;
    }
    chip.setAttribute("title", title);
  }

  function updateMarketChip(state) {
    if (!state) return;
    currentState = Object.assign({}, currentState || {}, state);
    window.__lastMarketState = currentState;
    renderChip(currentState);
  }

  window.updateMarketChip = updateMarketChip;

  // Poll fallback for pages without active SSE stream
  async function fetchMarketStatus() {
    try {
      const res = await fetch("/api/market/status");
      if (!res.ok) return;
      const data = await res.json();
      if (data && data.success) {
        updateMarketChip(data);
      }
    } catch (e) {
      // Best-effort network poll
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    const chip = document.getElementById("market-status-chip");
    if (chip && chip.dataset.market) {
      try {
        const init = JSON.parse(chip.dataset.market);
        updateMarketChip(init);
      } catch (e) {
        // Fallback to fetch
        fetchMarketStatus();
      }
    } else {
      fetchMarketStatus();
    }

    // 1-second dynamic update ticker (re-evaluates time-of-day without reloading)
    setInterval(() => {
      if (currentState) {
        renderChip(currentState);
      }
    }, 1000);

    // 30-second poll fallback if page is open a long time
    setInterval(fetchMarketStatus, 30000);
  });
})();
