# Open Items Tracker

**How to use:** tick `[x]` when done. "Who: You" = needs your action (credentials/money/account). "Who: Agent" = I can do it on request. Everything stays on this page — nothing held in your head.

**Last updated:** 2026-10-06 · full suite 4,849 collected · multi-broker Phases A–D shipped (`docs/MULTI-BROKER-PRD.md`) · date-rot test repairs landed (pricing-clock pins + replay-anchored expiry) · doc sweep: item 7 marked done, item 12 half-done note · **Consolidated P&L & Tax Reporting (PRD-002) merged and pushed to remote**

> **2026-10-06 update — shipped since the last sweep:**
> - Backtest run-ledger persistence merged 2026-10-03 (migration 018, `api/backtest_run_store.py`) — every completed run/compare persists payload + provenance.
> - Data tab rework + instrument-list availability slice (PRs #39/#40/#42): names-only picker, curated NIFTY 200 + NSE index universes, dedupe + 502-retry + holiday-aware fetch; timeframes derived by resampling.
> - Broker session-expiry alerts (PR #41): in-app widget + Telegram + re-login popup.
> - `market_data_cache` UTC→IST clock repair (writer fixes + 45.6M-row migration, commit f2eb7e3).
> - NSE holiday table seeded 2022–2026 (`tools/seed_market_holidays.py`, migration 017); fetches skip closures.
> - Swing battery (2026-10): **all 8 strategy combos failed their gates — nothing was deployed** (`plugins/strategies/swing_*` stay as unadopted plugins).
>
> **2026-10-06 open items — verified against code / observed on the live box:**
> - [ ] **13. DB connection exhaustion** — Flask `:5000` hoards ~99/100 PostgreSQL connections over ~9h. Known, untraced — needs a pool-leak hunt (`FORWARD_TEST_DB_POOL_*`, per-request session handling). *Who: Agent*
> - [ ] **14. Equity 1-min backfill interrupted at 77/200 symbols** (2026-10-04 broker 502 storm) — resume the remaining symbols. *Who: Agent*
> - [ ] **15. Index 1-min backfill** — NIFTY done; BANKNIFTY + FINNIFTY + MIDCPNIFTY **paused awaiting go-ahead**; SENSEX + INDIAVIX **deferred**. *Who: You*
> - [ ] **16. Daily-loss breaker UTC-window blind spot** — the day anchor is keyed on the UTC date, so 00:00–05:30 IST can be blind to the IST trading day's losses. *Who: Agent*
> - [ ] **17. `/reporting` 500 on paper trades** — timezone mix between paper-trade stamps and the consolidation path. *Who: Agent*
> - [ ] **18. Live page leaks paper positions** — `/portfolio/live` rows are not fully filtered to the live bucket. *Who: Agent*
> - [ ] **19. Data-refresh chip built but not live until restart** — the topbar freshness chip (`components/freshness_chip.js`, `GET /api/data/freshness`) only appears after the next server restart. *Who: Agent*

> **Expiry-day pricing fix (2026-09-24):** `SyntheticChainGenerator.MIN_PRICING_YEARS`
> (half a trading day) in `price_contract` — near/post-expiry synthetic options keep a
> real premium smile instead of collapsing to ₹0, which used to trip the fail-closed
> ltp=0 guard and block all entries on monthly expiry days. 31+12 date-brittle test
> failures fixed, 0 introduced.

---

## 🔥 Do today (market is open)

- [ ] **1. Real-session smoke test (T9.5)** — *~30 min — Who: You + Agent*
  Start app → mStock login + TOTP → spawn ONE option runner `source: mstock, mode: paper` → confirm quote label is NOT `synthetic:bs`.
  Command: `cd src && python -m backtest.web.app --port 5000`

- [ ] **2. Start chain snapshot capture** — *~5 min to start, runs all day — Who: You*
  Command: `PYTHONPATH=src python scripts/snapshot_option_chains.py --interval-seconds 300`
  Win: today's option chain data starts accruing for future backtests.

- [ ] **3. Intraday strategy paper run** — *~1 hr of market time — Who: Agent (on request)*
  Spawn runner on the new strategy, watch it trade real 1-hour bars until close.

- [ ] **4. Record findings** — *~10 min after close — Who: Agent*
  Log what worked/broke in `docs/OPTIONS-FORWARD-TEST-EXPERIMENT.md`.

---

## 🧠 HFT strategy — what's real (opinion)

**True HFT is not possible here.** No tick feed, no sub-second latency, no colocation.
**What IS possible: high-turnover intraday on 1-hour bars** (shortest real feed).
Plan: new strategy in `plugins/strategies/` from the option template + tighter
exit knobs (`reenter: true`, `max_reentries_per_day: 4`). Est: half a day of
agent work. Blocked only by item 1 passing.

---

## 🟠 This week

- [x] **5. HFT-style strategy built + conformance-tested** — ✅ DONE (owner confirmed 2026-09-28): the high-turnover intraday family shipped in `plugins/strategies/` — `momentum_burst` (draft #1 adapted 2026-09-18), `atm_instant_buy`, `immediate_entry`, `immediate_strangle`, `time_alternator`, `ema_reversion_pob` (reenter knobs) — all pass the conformance battery (`tests/test_strategy_conformance.py`) and register at boot.
- [ ] **6. CI workflow push** (`.github/workflows/ci.yml` needs your account — I can't push workflows) — *15 min — Who: You*
- [x] **7. Portfolio-page option trade rows (UI polish)** — ✅ DONE (verified in code 2026-09-28): `portfolio.js` renders per-structure rows via `OptionView.openStructures` (matrix + deep-dive); API side covered by `tests/engine/test_portfolio_options_rows.py`, JS by `tests/js/test_option_view.mjs` + `render_option_views.mjs`.
- [ ] **8. Consultant answers on 6 open questions** — `CONSULTANT_RESPONSE.md` §0.4 — *Who: You*

### Consolidated P&L / tax report (PRD-002) — shipped ✅

**Merged 2026-09-29:** Complete reporting system with correct Indian tax classification, exports, reconciliation, and monthly email reports. All 7 follow-up items from the original PRD are now live:

- ✅ Tax engine with correct FY 2026-27 rates (STCG 20%, LTCG 12.5% above ₹1,25,000, business slab default 30%)
- ✅ Consolidated P&L across all brokers/books with itemised fees
- ✅ Exports: PDF statement, ITR annexures (xlsx/csv/json), trade ledger CSV
- ✅ Contract-note reconciliation per broker with PASS/WARNING/FAIL tolerance bands
- ✅ Monthly email reports (dry-run to outbox by default)
- ✅ Settings panel with broker catalogue grouped by adoption
- ✅ Comprehensive test suite (93% coverage, every module ≥88%)

**Remaining follow-ups (user action required):**
- [ ] **Tax config sign-off** — *Who: You (+ your CA)*. Verify `config/reporting.yaml` rates match your actual marginal rate before treating "estimated tax" as your number.
- [ ] **Validate dhan broker profile** — *Who: You (one contract note)*. `/settings` → dhan card → Edit/validate with real contract note amounts.
- [ ] **Turn monthly email on** — *Who: You*. Set `monthly_email.enabled: true` and SMTP host in `config/reporting.yaml`; export `REPORTING_SMTP_PASSWORD`.
- [ ] **Reconciliation against real contract notes** — *Who: You + Agent*. `/reporting` → paste each broker's note net P&L → verify PASS/WARNING/FAIL.

## 🟢 Later (no date)

- [x] 9. Options-tab hard delete — ✅ DONE early (2026-09-22): GAP-3 resolved as REMOVE — /options page, options.js, Manual Options Book tab/banner deleted; JSON endpoints + book singleton kept (portfolio merge + emergency flatten). Review date no longer needed.
- [ ] 10. Risk envelope V2 (BS+IV+SPAN) — after consultant answers
- [ ] 11. Multi-leg structures (straddles/condors) — Phase B, don't bundle
- [ ] 12. Sizing presets + richer metrics (Sortino, profit factor) — vectorized path
      — *metrics half is DONE (2026-09-21: `engine/metrics.py` has Sortino, profit
      factor, expectancy, VaR/ES); sizing presets on the vectorized path remain.*

---

**Wins today so far:** ✅ Gap validation done — P1.1 live chain wiring, F-12 equity fills, P2.4 persistence, fail-closed trader all verified in code + tests. One date-brittle test fixed.
