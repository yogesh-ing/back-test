# Regime UI Observation & Impact Assessment
**Document type:** Cross-functional review brief  
**Prepared for:** @quant-architect (QUANT-ARCH), @quant-engineer-qwen (QUANT-ENGINEER-QWEN), @quant-researcher (QUANT-RESEARCHER)  
**Status:** Live on :5000, verified against real mStock/INDIAVIX feed  
**Date:** 2026-11-26

---

## 1. Current state — what the UI shows today

### Market Regime card (`src/backtest/web/templates/_portfolio_center.html:388`)
| Element | Source | Current live value |
|---------|--------|--------------------|
| Regime badge | `feed:INDIAVIX` | `moderate_vol` (VIX 17.36) |
| VIX + 24h change | live WS bars | VIX 17.36 |
| Source / since | `feed:INDIAVIX`, `regime_since` | 2026-11-26 06:50 UTC |
| Bands | `config/portfolio_intelligence.yaml` | low<15≤moderate<22≤high |
| VIX history chart | last 288 samples (every 5 min) | rendering |
| Strategy regime fit table | `regime.strategy_fit` | runner / strategy / range / fit |
| Manual VIX override | `POST /api/market/vix` | input box + Apply |

### Regime factors grid (new, `_portfolio_center.html:412`)
| Factor | Status | Tone | Source | Live value |
|--------|--------|------|--------|------------|
| VIX | moderate_vol | neutral | `feed:INDIAVIX` | VIX 17.36 |
| Trend | down | negative | `1day:200sma` (DB) | -6.52% vs SMA200 |
| Volatility | calm | positive | `realized_vol:VIX` | RV 13.1 vs VIX 17.4 |
| Breadth | narrow | negative | `1day:200sma:nifty200` (DB) | 42/199 = 21% |
| Session | midday_chop | neutral | `clock:IST` | 11:30–13:30 IST |
| Gamma | ok | positive | `portfolio_greeks` | net γ -10.18 |
| IV/RV | unknown | unknown | `portfolio_iv:realized_vol` | — (no leg-IV yet) |
| Composite | `risk_off` | negative | derived | 2 risk-off / 1 risk-on |

### Alert delivery (verified live)
- `vix_regime_change`: fired on `MODERATE → HIGH → MODERATE` flips on live feed
- `session_phase`: fired `off_hours → morning_trend → midday_chop`
- `concentration_high`: fired on NIFTY 100% concentration
- Alerts reach `alert-notifier` + subscribed runners

### Runner detail (`/api/portfolio/runner/<id>`)
- `pause_reason`, `block_reason`, `trade_health`, `feed_state`, `bars_processed`, `open_pnl`, `realized`

---

## 2. What's missing for live algo monitoring

### G2.1 — No pause-status linkage in the regime card
The user sees the regime (`moderate_vol`, `down`, `midday_chop`) but **cannot see which runners are paused because of it**. `pause_reason` lives only on the runner detail endpoint. A regime watcher must click into each runner to know if entries are paused.

**QUANT-ARCH view:** This violates "show the why not just the what" (I.14.UX). A number without context is a liability.

### G2.2 — No factor history / sparklines
Only VIX has a 288-sample chart. Trend, breadth, session, gamma, IV/RV are single-point snapshots. The user cannot see **direction of travel** (is trend deepening? is breadth recovering?).

**QUANT-RESEARCHER view:** Regime analysis is a weekly deliverable (III.8). Without history, the operator can't see regime transitions or decay — only the current slice.

### G2.3 — Composite is text-only
`risk_off` / `risk_on` / `neutral` is rendered as a muted text span, not a color gauge. No immediate visual severity.

**UI-designer view:** Direction must never rely on color alone (rule: pair with sign/arrow). But a severity gauge without color/position encoding is weak scan-ability.

### G2.4 — Alerts live in a separate bottom-right widget
Regime alerts (`vix_regime_change`, `session_phase`) appear in the alert widget, **not inline in the regime card**. The operator looks at two places to answer "what's the regime and what changed?"

### G2.5 — IV/RV is `unknown` with no explanation
The user sees `IV — vs RV —` with no hint that it's waiting on leg-IV population. Looks like broken data, not intentional deferral.

**QUANT-ARCH view:** Honesty principle — every output carries provenance and state. "unknown" must say *why* it's unknown and *what would fix it*.

### G2.6 — No manual test controls for non-VIX factors
Only VIX can be overridden via `POST /api/market/vix`. Cannot manually test trend/session/iv_rv/dislocation against live runners.

---

## 3. Live test results (verified 2026-11-26)

Two paper runners on live mStock feed:

| Runner | Strategy | Mode | Feed | Subscribed alerts | State |
|--------|----------|------|------|-------------------|-------|
| `4e438df5...` | old_immediate_strangle | paper | mStock LIVE | vix, gamma | RUNNING, 1 open structure, realized +₹768.42 |
| `95a392f6...` | immediate_short_straddle | paper | mStock LIVE | vix, trend, session, iv_rv, gamma | RUNNING, 1 open structure, spot 25920.6 |

Alerts confirmed firing on live feed:
- `vix_regime_change`: MODERATE→HIGH→MODERATE (live INDIAVIX oscillations)
- `session_phase`: off_hours→morning_trend→midday_chop (clock-driven)
- `concentration_high`: NIFTY 100%

No regime pause has triggered yet because VIX has stayed at 17.36 (moderate_vol) since spawn. The wiring is confirmed; the pause path is untested in live conditions.

---

## 4. Recommended UI changes (for discussion)

| # | Change | Effort | Persona driver |
|---|--------|--------|----------------|
| R1 | Add pause-status chips in the regime fit table (e.g. "paused · regime" next to each runner) | S | QUANT-ARCH (show the why) |
| R2 | Mini sparklines under each factor (trend slope, breadth %) | M | QUANT-RESEARCHER (regime direction) |
| R3 | Color-coded composite gauge (red/amber/green) | S | UI-designer (scan speed) |
| R4 | Inline regime alerts in the card (not just bottom-right widget) | S | QUANT-ARCH (single source of truth) |
| R5 | Explain `unknown` states (tooltip: "waiting on leg-IV population") | S | QUANT-ARCH (honest provenance) |
| R6 | Manual override inputs for trend/session/iv_rv (test only, gated behind dev flag) | M | QUANT-RESEARCHER (test regime impact) |

---

## 5. QUANT-RESEARCHER stance

**Position: REGIME UI IS NEEDED BUT NOT SUFFICIENT FOR LIVE OPERATIONS.**

### Why I support the direction
The regime factors map directly to my Monday regime report (agents/quant-reasercher-qwen.md:503):
- **Trend** → long-term regime filter (above/below 200DMA) — determines whether we run trend vs. mean-reversion strategies
- **Volatility (RV vs VIX)** → IV/RV dislocation signal — sell vol when IV > RV + 5, buy vol when IV < RV - 5
- **Breadth** → % NIFTY 200 above 200DMA — broad vs. narrow market
- **Session** → intraday session structure — no trades 11:30-13:00, avoid 15:00-15:30
- **Gamma** → short-γ tail risk exposure — pause new entries when critical

These are exactly the inputs a researcher needs to size positions by regime (my regime overlay table at agents/quant-reasercher-qwen.md:612).

### What concerns me (from a research perspective)

**1. IV/RV is `unknown` in production.** This is my single most important options signal (agents/quant-reasercher-qwen.md:242-248). If the UI shows "unknown" the operator can't make IV-regime decisions. The leg-IV proxy needs to populate from real chain data, or the factor should be suppressed until it has data — not shown as a blank that looks like a bug.

**2. No regime transition history.** My regime report tracks transitions (off_hours→morning→midday). The UI shows only the current state. Without history, I can't see if we're in a *changing* regime (which is when strategies bleed).

**3. Strategy fit is static.** The fit table shows `regime_vix_range` vs current VIX, but doesn't show **how the strategy is actually performing** in this regime. A strategy can be "favorable" on paper and bleeding in live conditions.

**4. Pause mechanics are invisible.** When `session_phase = midday_chop` pauses a short-straddle runner, the operator sees `pause_reason` only by drilling into runner detail. For live trading, this needs to be glanceable: "Straddle paused · session · midday_chop".

### My recommendation

| Priority | Action | Rationale |
|----------|--------|-----------|
| P0 | Populate IV/RV from real chain data (ATM IV proxy) | Without this, my core vol edge is invisible |
| P0 | Show pause-reason inline in regime card | Operator must know *why* entries are paused at a glance |
| P1 | Factor sparklines (7-day mini-trend per factor) | Regime direction matters more than regime state |
| P1 | Composite gauge with color + text | Scan speed during market hours |
| P2 | Manual factor overrides (dev-gated) | Test regime impact without waiting for live flips |

**Bottom line:** The engine is correct and live. The UI shows the right *numbers* but not the right *story* for an operator managing real capital. Fix the IV/RV visibility and pause linkage first; sparklines and gauges next.

---

## 6. Team Review Comments

### quant-risk-manager — Review
**Overall: BLOCK for live capital until G2.1, G2.5, and the cooldown blind spot are fixed.**

**Concerns:**
- **G2.1 — Pause status invisible in the regime card.** If `session_phase = midday_chop` pauses a short-straddle runner, the operator must drill into runner detail to discover it. A paused runner bleeding theta while the operator thinks it's "RUNNING" is a silent failure during market hours.
- **G2.5 — IV/RV showing `unknown` with no explanation** is fail-open behavior dressed as data. The operator sees a blank factor and assumes "not applicable" rather than "waiting on leg-IV." For an options book, not knowing whether IV is cheap or expensive is a material risk blind spot.
- **60s re-notify cooldown blind spot:** A `HIGH → MODERATE` flip inside the window creates an alert but doesn't redeliver. A strategy stays paused on the stale `HIGH` state — capital frozen until the next alert cycle.
- **Composite `risk_off` is text-only.** No color, no gauge. In fast markets an operator needs to scan and act in <2 seconds. Text requires reading; color communicates instantly.

**Acceptable as-is:**
- Session-based pause (midday_chop, closing_squareoff) is sound risk reduction.
- Gamma factor with `critical = -150` is reasonable; net γ -10.18 currently reads `ok`.

**Verdict: Do not deploy regime-driven pausing to live capital with the current UI.** Fix G2.1 (pause chips inline), G2.5 (explain unknown states), and the cooldown blind spot first. R2 sparklines are P1 — regime *direction* matters for risk.

---

### quant-options-engineer — Review
**Overall: ENGINE IS CORRECT, UI IS MISSING THE OPTIONS-CRITICAL SIGNAL.**

**Concerns:**
- **IV/RV is `unknown` — P0 for options.** The core edge is IV vs RV dislocation (sell when IV > RV + 5, buy when IV < RV - 5). If the UI shows "unknown," vol-regime decisions can't be made. The leg-IV proxy (volume-weighted IV of open legs) is a reasonable stopgap but needs real chain data. Until then, suppress the factor with a clear "no data" state — don't show a blank that looks like a bug.
- **Gamma factor threshold.** `gamma_critical = -150` (1% spot move). Net γ -10.18 reads `ok`. But for a short straddle on NIFTY, gamma risk concentrates near ATM and explodes in the last 2 days to expiry. The UI should show DTE-aware gamma context, not just a flat threshold.
- **Session phase and options liquidity.** `midday_chop` and `closing_squareoff` are correct pauses for short premium, but the UI doesn't show *why* (liquidity thin, pin risk).
- **Trend regime vs short premium.** A short straddle loses in trending markets. The `trend = down` factor pausing entries is correct. But the UI doesn't show the trader *which* structure is affected — straddle vs strangle have different gamma/vega profiles.
- **Manual VIX override.** `POST /api/market/vix` is useful for testing but dangerous if left pointing at a manual value in live. The UI should show a prominent "OVERRIDE ACTIVE" banner when manual VIX is set.

**Verdict: Fix IV/RV population first (P0), then add pause-status chips (P0).** The engine logic is sound; the UI hides the options-critical signals. R6 manual overrides for non-VIX factors are useful for testing regime impact on short structures, but must be dev-gated and clearly labeled.

---

### quant-architect — Review
**Overall: ARCHITECTURALLY SOUND, UI VIOLATES MULTIPLE CORE PRINCIPLES.**

**Principle violations:**
| Principle | Violation | File:line |
|-----------|-----------|-----------|
| "Show the why not just the what" (I.14.UX) | Regime card shows regime but not pause linkage | `_portfolio_center.html:388` |
| Honesty/provenance — every output carries source and state | IV/RV `unknown` with no explanation | `factors.py:233` |
| Fail-closed — unknown data should refuse, not guess | IV/RV unknown renders as blank factor | `service.py` `_rule_iv_rv` |
| Data provenance — every number labeled with source | Factors show `source` but composite has no source | `factors.py:_composite` |

**The cooldown bug is architectural:**
The broker's 60s re-notify cooldown survives `resolve_missing`. A regime flip back inside the window doesn't redeliver, leaving a strategy paused on stale state. This violates the observer pattern contract — subscribers must converge to current state. Fix at the rule layer (per-state subject) or broker layer (clear cooldown on resolve).

**UI-specific concerns:**
- **G2.4 — Alerts in a separate widget** violates single source of truth. The regime card should be the canonical place for regime state + changes.
- **G2.2 — No factor history** means the operator can't see regime transitions. Regime *changes* are when strategies bleed, not static state.
- **R3 composite gauge** — color alone is insufficient per UI rules, but color + text + position encoding is acceptable for severity.

**Verdict: APPROVE the engine, REJECT the UI for live ops.** The backend is clean (pure factors, fail-soft caching, additive alerts). The UI needs R1 (pause chips), R4 (inline alerts), R5 (explain unknown), and the cooldown fix before it's fit for an operator managing real capital. R2 sparklines and R3 gauge are P1.

---

### Cross-Persona Discussion

**Agreement (all three):**
- IV/RV `unknown` is P0 — it's the core options edge and it's invisible.
- Pause-status must be inline in the regime card (G2.1) — operator can't drill into every runner during market hours.
- The 60s cooldown blind spot must be fixed — strategies stuck on stale pause state is a real risk.
- R6 manual overrides are useful for testing but must be dev-gated and clearly labeled "OVERRIDE ACTIVE."

**Disagreement / nuance:**
- **Risk-manager** wants to block all regime-driven pausing to live capital until fixes land. **Options-engineer** and **Architect** agree the engine is sound but the UI isn't fit — so the block is on the UI/ops side, not the engine side.
- **Researcher** wants factor history for regime transitions. **Risk-manager** agrees — direction of travel matters for risk. **Architect** notes storage cost is open (quant-engineer-qwen to answer).

---

## 7. Final Conclusion

### Ship as-is (engine)
The regime engine (7 factors, composite, alerts, strategy opt-in) is architecturally sound and verified on live mStock/INDIAVIX feed. No engine changes required.

### Do NOT enable live regime-driven pausing until these UI fixes land

| Priority | Fix | Owner | Rationale |
|----------|-----|-------|-----------|
| P0 | IV/RV: populate from real chain data OR suppress with "no data" explanation | quant-options-engineer + quant-architect | Core options edge is invisible; unknown looks like a bug |
| P0 | Pause-status chips inline in regime fit table | quant-architect | Operator must know *why* entries are paused at a glance |
| P0 | Fix 60s cooldown blind spot (clear on resolve / per-state subject) | quant-architect | Strategies stuck on stale pause state |
| P0 | "OVERRIDE ACTIVE" banner when manual VIX is set | quant-options-engineer | Prevent trading on stale fake data |
| P1 | Factor sparklines (7-day mini-trend per factor) | quant-researcher + quant-architect | Regime direction > regime state for risk |
| P1 | Composite gauge (color + text + position) | UI-designer + quant-architect | Scan-speed severity during market hours |
| P1 | Inline regime alerts in the card (not just bottom-right widget) | quant-architect | Single source of truth |
| P2 | Manual overrides for trend/session/iv_rv (dev-gated) | quant-researcher | Test regime impact without waiting for live flips |

### Go/No-Go for live capital
**NO-GO** for regime-driven pausing on live capital until P0 items are complete. The engine works; the UI doesn't yet give the operator the situational awareness to trust it with real money.

### Next step
quant-engineer-qwen to answer: storage cost of backfilling factor history into `market_regime_history` for sparklines (R2). Then implement P0 fixes one at a time.

---

## 8. Implementation Plan — Task Tracker

| ID | Task | Priority | Status | File(s) |
|----|------|----------|--------|---------|
| T1 | IV/RV: populate from real chain data OR suppress with "no data" explanation | P0 | pending | `factors.py`, `service.py` |
| T2 | Pause-status chips inline in regime fit table | P0 | pending | `_portfolio_center.html`, `portfolio_intelligence.js` |
| T3 | Fix 60s cooldown blind spot (clear on resolve / per-state subject) | P0 | pending | `alerts/broker.py`, `intelligence/service.py` |
| T4 | "OVERRIDE ACTIVE" banner when manual VIX is set | P0 | pending | `_portfolio_center.html`, `api/intelligence.py` |
| T5 | Factor sparklines (7-day mini-trend per factor) | P1 | pending | `intelligence/service.py`, `portfolio_intelligence.js`, `app.css` |
| T6 | Composite gauge (color + text + position) | P1 | pending | `_portfolio_center.html`, `portfolio_intelligence.js`, `app.css` |
| T7 | Inline regime alerts in the regime card | P1 | pending | `_portfolio_center.html`, `portfolio_intelligence.js`, `api/intelligence.py` |
| T8 | Manual overrides for trend/session/iv_rv (dev-gated) | P2 | pending | `_portfolio_center.html`, `api/intelligence.py`, `intelligence/service.py` |

**Execution mode:** One task at a time, in priority order (T1 → T8). Each task: implement → test → mark complete → move to next.

**File location:** `docs/regime-ui-observation.md`  
**Related:** `src/backtest/intelligence/factors.py`, `src/backtest/intelligence/service.py`, `src/backtest/web/templates/_portfolio_center.html`, `src/backtest/web/static/js/components/portfolio_intelligence.js`
