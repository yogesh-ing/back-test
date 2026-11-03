/**
 * Client Poll Cadence Governor ("Option 1+")
 *
 * Governs non-trading topbar/chrome pollers so idle after-hours, weekend,
 * holiday, logged-out, and backgrounded tabs stay quiet — WITHOUT touching
 * SSE (/api/portfolio/stream), Paper/Live runner controllers (portfolio.js,
 * forward.js), or after-hours data backfills.
 *
 * Safety invariants:
 *   1. Fail-open: when window.__lastMarketState is missing/unknown, assumes
 *      market is active (never throttles in the dark).
 *   2. Active-runner override: whenever any Paper, Live, or Synthetic runner
 *      is running (activeRunners > 0), session is treated as active even
 *      during POST_CLOSE / WEEKEND / HOLIDAY.
 *   3. Data fetch jobs are NEVER gated on market hours — only on job status
 *      (running vs idle) + cross-tab instant wake on job start.
 *   4. Multi-broker aware: broker status polling pauses ONLY when every
 *      configured broker is unauthenticated/expired.
 */
(function (root) {
  "use strict";

  const TRADING_ROUTE_RE = /^\/(portfolio(\/.*)?|forward(\/.*)?|risk(\/.*)?)$/i;
  const RESEARCH_ROUTE_RE = /^\/(backtest|compare|optimize|strategy-builder|data|settings|diary|analytics|screener)(\/.*)?$/i;

  const senderId = Math.random().toString(36).slice(2) + Date.now().toString(36);
  let lastSseTs = 0;
  let activeRunners = 0;

  let dataChannel = null;
  let brokerChannel = null;
  const dataWakeListeners = [];
  const brokerWakeListeners = [];

  if (typeof BroadcastChannel === "function") {
    try {
      dataChannel = new BroadcastChannel("backtest:data-job");
      dataChannel.onmessage = (ev) => {
        if (ev && ev.data && ev.data.senderId === senderId) return;
        dataWakeListeners.slice().forEach((fn) => {
          try { fn(ev && ev.data); } catch (_) { /* ignore listener error */ }
        });
      };
    } catch (_) {
      dataChannel = null;
    }

    try {
      brokerChannel = new BroadcastChannel("backtest:broker-auth");
      brokerChannel.onmessage = (ev) => {
        if (ev && ev.data && ev.data.senderId === senderId) return;
        brokerWakeListeners.slice().forEach((fn) => {
          try { fn(ev && ev.data); } catch (_) { /* ignore listener error */ }
        });
      };
    } catch (_) {
      brokerChannel = null;
    }
  }

  function getNowDate(nowOverride) {
    if (nowOverride instanceof Date) return nowOverride;
    if (typeof nowOverride === "number") return new Date(nowOverride);
    return new Date();
  }

  function getIstSecondsOfDay(nowOverride) {
    const d = getNowDate(nowOverride);
    const utcMs = d.getTime() + (d.getTimezoneOffset() * 60000);
    const ist = new Date(utcMs + (5.5 * 3600000));
    return (ist.getHours() * 3600) + (ist.getMinutes() * 60) + ist.getSeconds();
  }

  function isHidden() {
    return Boolean(typeof document !== "undefined" && document && document.hidden);
  }

  function currentPathname(pathnameOverride) {
    if (typeof pathnameOverride === "string") return pathnameOverride;
    if (root && root.location && typeof root.location.pathname === "string") {
      return root.location.pathname;
    }
    return "/";
  }

  function isTradingRoute(pathnameOverride) {
    return TRADING_ROUTE_RE.test(currentPathname(pathnameOverride));
  }

  function isResearchRoute(pathnameOverride) {
    return RESEARCH_ROUTE_RE.test(currentPathname(pathnameOverride));
  }

  function resolveMarketSnapshot(rawState, nowOverride) {
    const st = rawState || (root && root.__lastMarketState) || null;
    if (!st || !st.day_state) {
      // Fail-open (MARKET-STATUS-SPEC §4): never claim closed without server truth.
      return {
        known: false,
        dayState: null,
        sessionState: "OPEN",
        isOpenOrPreOpen: true,
      };
    }

    const dayState = String(st.day_state).toUpperCase();
    if (dayState === "HOLIDAY" || dayState === "WEEKEND") {
      return {
        known: true,
        dayState,
        sessionState: "POST_CLOSE",
        isOpenOrPreOpen: false,
      };
    }

    const secs = getIstSecondsOfDay(nowOverride);
    const preOpenSecs = 9 * 3600;               // 09:00:00 IST
    const marketOpenSecs = 9 * 3600 + 15 * 60;  // 09:15:00 IST
    const marketCloseSecs = 15 * 3600 + 30 * 60; // 15:30:00 IST

    let sessionState = "POST_CLOSE";
    if (secs >= preOpenSecs && secs < marketOpenSecs) {
      sessionState = "PRE_OPEN";
    } else if (secs >= marketOpenSecs && secs <= marketCloseSecs) {
      sessionState = "OPEN";
    }

    return {
      known: true,
      dayState,
      sessionState,
      isOpenOrPreOpen: sessionState === "OPEN" || sessionState === "PRE_OPEN",
    };
  }

  function extractRunningCount(payload) {
    if (!payload || typeof payload !== "object") return null;
    if (typeof payload.running === "number" && !Number.isNaN(payload.running)) {
      return Math.max(0, payload.running);
    }
    if (Array.isArray(payload.runners)) {
      return payload.runners.filter(
        (r) => r && (r.running === true || r.state === "running" || r.status === "running"),
      ).length;
    }
    return null;
  }

  function noteSseFrame(payload, nowMs) {
    lastSseTs = typeof nowMs === "number" ? nowMs : Date.now();
    const count = extractRunningCount(payload);
    if (count !== null) {
      activeRunners = count;
      if (root) root.__activeRunnerCount = count;
    }
  }

  function noteRiskSnapshot(payload) {
    const count = extractRunningCount(payload);
    if (count !== null) {
      activeRunners = count;
      if (root) root.__activeRunnerCount = count;
    }
  }

  function setActiveRunners(count) {
    activeRunners = Math.max(0, Number(count) || 0);
    if (root) root.__activeRunnerCount = activeRunners;
  }

  function getActiveRunners() {
    return activeRunners;
  }

  function isSseFresh(maxAgeMs, nowMs) {
    if (!lastSseTs) return false;
    const limit = typeof maxAgeMs === "number" ? maxAgeMs : 15000;
    const now = typeof nowMs === "number" ? nowMs : Date.now();
    return (now - lastSseTs) < limit;
  }

  function isSessionLiveOrActive(opts) {
    const market = resolveMarketSnapshot(
      opts && opts.marketState,
      opts && opts.now,
    );
    return market.isOpenOrPreOpen || activeRunners > 0;
  }

  function marketStatusDelay(opts) {
    if (isHidden()) return null;
    if (isSseFresh(15000, opts && opts.nowMs)) return null;
    const market = resolveMarketSnapshot(
      opts && opts.marketState,
      opts && opts.now,
    );
    if (!market.known) return 30000;
    if (market.dayState === "WEEKEND" || market.dayState === "HOLIDAY") {
      return 30 * 60 * 1000; // 30 min
    }
    return 5 * 60 * 1000; // 5 min (local 1s IST clock flips 09:00/09:15/15:30 live)
  }

  function inspectBrokerSessions(payloadOrState) {
    if (!payloadOrState) {
      return { anyAuthenticated: false, anyExpiringSoon: false };
    }
    if (typeof payloadOrState === "string") {
      const s = payloadOrState;
      return {
        anyExpiringSoon: s === "expiring_soon",
        anyAuthenticated: s === "authenticated" || s === "expiring_soon",
      };
    }
    const topStatus = payloadOrState.status || "";
    let anyExpiringSoon = topStatus === "expiring_soon";
    let anyAuthenticated = topStatus === "authenticated" || anyExpiringSoon;
    const sessions = payloadOrState.sessions;
    if (sessions && typeof sessions === "object") {
      for (const key of Object.keys(sessions)) {
        const row = sessions[key];
        if (!row) continue;
        if (row.status === "expiring_soon") {
          anyExpiringSoon = true;
          anyAuthenticated = true;
        } else if (row.authenticated === true || row.status === "authenticated") {
          anyAuthenticated = true;
        }
      }
    }
    return { anyAuthenticated, anyExpiringSoon };
  }

  function brokerStatusDelay(payloadOrState, opts) {
    const { anyAuthenticated, anyExpiringSoon } = inspectBrokerSessions(payloadOrState);
    if (!anyAuthenticated) {
      // Logged-out / expired sessions cannot expire further; login modal &
      // cross-tab auth channel trigger immediate refresh on login.
      return null;
    }
    if (isHidden()) return null;
    if (anyExpiringSoon || isSessionLiveOrActive(opts)) {
      return 60000; // 60 s PRD cadence
    }
    return 5 * 60 * 1000; // 5 min post-close idle
  }

  function dataStatusDelay(isRunning) {
    // NEVER gate on market hours — OHLCV backfills run after close & weekends.
    if (isRunning) {
      return isHidden() ? 6000 : 3000;
    }
    return isHidden() ? null : 120000; // 2 min visible idle; paused when hidden
  }

  function riskStripDelay(opts) {
    const pathname = currentPathname(opts && opts.pathname);
    const sseActive = Boolean(opts && opts.sseActive);
    if (isHidden()) {
      return (isTradingRoute(pathname) && activeRunners > 0) ? 30000 : null;
    }
    if (sseActive && isSseFresh(12000, opts && opts.nowMs)) {
      return 15000;
    }
    if (isSessionLiveOrActive(opts)) {
      return isResearchRoute(pathname) ? 15000 : 5000;
    }
    return 5 * 60 * 1000; // 5 min post-close with 0 active runners
  }

  function alertWidgetDelay(opts) {
    const pathname = currentPathname(opts && opts.pathname);
    if (isHidden()) {
      return (isTradingRoute(pathname) && activeRunners > 0) ? 30000 : null;
    }
    if (isSessionLiveOrActive(opts)) {
      return 10000; // 10 s active cadence
    }
    return 5 * 60 * 1000; // 5 min post-close with 0 active runners
  }

  function landingRiskDelay(opts) {
    if (isHidden()) return null;
    if (isSseFresh(12000, opts && opts.nowMs)) return null;
    if (isSessionLiveOrActive(opts)) {
      return 10000; // 10 s active cadence
    }
    return 5 * 60 * 1000; // 5 min post-close with 0 active runners
  }

  function notifyDataJobStarted() {
    const msg = { type: "data:job-started", senderId, ts: Date.now() };
    dataWakeListeners.slice().forEach((fn) => {
      try { fn(msg); } catch (_) { /* ignore */ }
    });
    if (typeof CustomEvent === "function" && typeof document !== "undefined" && document && typeof document.dispatchEvent === "function") {
      try {
        document.dispatchEvent(new CustomEvent("data:job-started", { detail: msg }));
      } catch (_) { /* ignore */ }
    }
    if (dataChannel) {
      try { dataChannel.postMessage(msg); } catch (_) { /* ignore */ }
    }
  }

  function onDataJobWake(fn) {
    if (typeof fn === "function") dataWakeListeners.push(fn);
  }

  function notifyBrokerAuthChanged(fingerprint) {
    const msg = { type: "broker:auth-changed", senderId, fingerprint: fingerprint || "", ts: Date.now() };
    if (brokerChannel) {
      try { brokerChannel.postMessage(msg); } catch (_) { /* ignore */ }
    }
  }

  function onBrokerAuthWake(fn) {
    if (typeof fn === "function") brokerWakeListeners.push(fn);
  }

  function resetForTests() {
    lastSseTs = 0;
    activeRunners = 0;
    if (root) root.__activeRunnerCount = 0;
  }

  const api = {
    isHidden,
    isTradingRoute,
    isResearchRoute,
    resolveMarketSnapshot,
    noteSseFrame,
    noteRiskSnapshot,
    setActiveRunners,
    getActiveRunners,
    isSseFresh,
    isSessionLiveOrActive,
    inspectBrokerSessions,
    marketStatusDelay,
    brokerStatusDelay,
    dataStatusDelay,
    riskStripDelay,
    alertWidgetDelay,
    landingRiskDelay,
    notifyDataJobStarted,
    onDataJobWake,
    notifyBrokerAuthChanged,
    onBrokerAuthWake,
    resetForTests,
  };

  if (root) root.PollCadence = api;
  if (typeof globalThis !== "undefined") globalThis.PollCadence = api;
})(typeof window !== "undefined" ? window : globalThis);
