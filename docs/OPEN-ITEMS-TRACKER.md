# Open Items Tracker

**How to use:** tick `[x]` when done. "Who: You" = needs your action (credentials/money/account/live box). "Who: Agent" = I can do it on request. Everything stays on this page — nothing held in your head.

**Last updated:** 2026-10-30 (`feature/bugfix-2026-10-28` worktree) · **+ Portfolio P&L & UI enhancement points E1–E3** (§📊 below, from the 28-Oct paper-loss review) · 2026-10-25 (`arena/ab59b7e5-back-test` @ `258366a`) · Strategy-Owned Execution v2 Phases 0–4 + strategy cards (`PR #45`) · mStock WS 6-cause depth fix (`e06c003`, 29/29 live paper fills reconciled to the paisa) · IST `00:00` daily-loss roll (`8074fca`) · Realtime Options & Swing Pool **Phases 1–4 + Phase 5.0 shipped** (`97430a8` → `258366a`)

> **2026-10-25 update — shipped since the 2026-10-06 sweep:**
> - **Strategy-Owned Execution v2 (`PR #45`, `f49e89a`)**: unified `EquityStrategy` / `DirectionalOptionStrategy` / `StructureStrategy` templates, single Risk Gate, selections-only spawn UI, and structured strategy cards.
> - **Daily-loss fuse IST session-date anchor (`d7c3adb`, `8074fca`, `9cfdf0a`, `7b48448`)**: stamps `session_date` in IST, rolls realized P&L cleanly at `00:00 IST` across equity and option structures, and deducts open-option entry fees in `daily_pnl()`.
> - **Orders tab & closed-trade size fix (`599be5d`)**: records option leg fills, adds `Lots` column, preserves equity `closed_quantity` on close, and adds `tools/repair_closed_quantity.py` for historic DB rows.
> - **mStock WebSocket depth root-cause fixes (`e06c003`)**: reactor-thread `_send_frame`, `LOGIN`-before-`resubscribe` ordering, binary full-mode OI float fix, and numeric-token depth routing — live-verified on 2026-10-23 (`29/29` fills, `QUOTE_ONE_SIDED=0`, `docs/TRADE-RECONCILIATION-2026-10-23.md`).
> - **Phase 1 (`97430a8` → `c778b7a`)**: shared `DatabaseManager` pool reuse across `PortfolioManager`/`DbSource` (fixes DB pool leak), `ScriptMasterCache`, 1-query `ts`-bounded `WarmupService` (`DEGRADED` badge on partial quorum), and `TimeframeBarAggregator` (`1min` → `5min`/`15min`/`30min`/`1hour` closed session bars).
> - **Phase 2 (`2a22cf2`, `6b002d1`)**: `MStockTickBus` (`FULL`/`LTP`/`QUOTE`), `HotStrikeRing` (±10 strikes auto-recentered on ATM shift + TOTP re-auth resubscribe + pin refcounting), and zero-TTL WS depth quotes with fail-closed staleness guard.
> - **Phase 3 (`9cfdf0a`, `7b48448`)**: sub-second MTM & SL/TP/trailing exit checks on WS ticks (`OptionsBridge.on_tick()`), tick-cadence option evaluation, WS `BarBuilder`, option commission netting (`net_pnl` + open entry fees), and `OPTIONS_INTRADAY_SQUAREOFF_MINUTES` EOD auto-square-off.
> - **Phase 4 (`85ebf0a`)**: `LongStraddle` & `ShortStraddle` (`StructureKind.STRADDLE`, paper-entitled), external WS order/trade push `<50 ms` fill wakeup, marketable `LIMIT` option pricing (`OPTIONS_LIVE_ORDER_TYPE=LIMIT`), and `LiveOptionBrokerAdapter` (`mode="live"` runner routing via `LiveOptionTrader` + `OptionExecutionGate`).
> - **Phase 5.0 (`258366a`)**: TimescaleDB-safe insert-only `1day` equity ingest (`ON CONFLICT DO NOTHING`, canonical `03:45Z` / `09:15 IST`), `1min -> 1day` completeness-quorum (`>=338/375`) rollup, pilot ceiling (`<=10` symbols, `<=120d`), 502 circuit breaker + checkpointing, and mandatory day-by-day NSE session verifier (`tools/daily_equity_ingest.py`).

---

## 📊 Portfolio P&L & UI Enhancements (raised 30-Oct 2026, during the ~₹3.0L paper-loss review)

Raised by the operator while reviewing the 28-Oct paper loss. **No code written — enhancement
points only.** Evidence: `tools/out/TRADES-300K-LOSS-2026-10-30.md` (+ `trades_2026-10-28_dedup.csv`),
`tools/out/SPRINT2-BUG-NOTES-2026-10-30.md` (bug side, Sprint 2 side agent).

### E1 — Day-scoped paper P&L (Daily P&L = today's realized + running)

- **Want:** every P&L number on the paper pages describes **today's session**, not the lifetime of the book.
- **Today:** the header sums each runner's *restored closed-trade cache*, so it silently mixes 27-Oct, 28-Oct and today's closes — and one screen shows three different answers (header −₹2,98,230 vs trade-history −₹1,60,753.68 over 1,307 rows vs DB −₹4,52,543.09 deduped / −₹6,76,178.83 raw).
- **Rule to implement:** `Daily P&L = equity_now − day_start_anchor`, and that must equal `realized (exit_time inside the IST session date, duplicates removed) + unrealized on open positions`. The anchor re-bases on capital flows, so a deposit never reads as P&L. Fees on still-open positions are not booked until close — "running" is a gross mark.
- **Accept when:** every P&L tile on `/portfolio/paper` states its scope ("Today · 30-Oct IST") and the day's realized equals the sum of that day's trade rows exactly.

### E2 — Commission visible on the UI as one sum (brokerage + taxes)

- **Want:** the operator sees the total commission the run paid (brokerage + statutory taxes) — a single number, on the trade history and on the P&L tiles.
- **Scope guard (operator decision, 30-Oct-2026):** **no per-component breakdown** (no separate brokerage / STT / GST / stamp columns). One sum only.
- **Today:** `trades.commission_total` already holds that sum, but the UI never shows it — the trade-history table prints gross and net with the difference unexplained, which is precisely why the ₹40 fee-floor disagreement (B2 in the Sprint 2 notes, 1,344 rows / ₹51,200) went unnoticed.
- **Accept when:** each trade row and the day tile show one `Commission` figure, and `gross − commission = net` visibly holds for every row. Rows where it does not hold are a data bug and must be surfaced, not smoothed over.

### E3 — PnL daily diary: strategy-wise P&L for a chosen day

- **Want:** a diary view where the operator picks a session date and reads that day's P&L **per strategy** (and per runner underneath it): realized, running, trade count, win rate, commission — plus the day's totals and the anchor the day started from.
- **Depends on E1 and E2.** While Sprint 2's B1 (duplicate trade re-inserts, ₹2.24L phantom) and B2 (fee-floor disagreement) are open, any per-strategy daily figure is unreliable: duplicates inflate it, the fee-floor disagreement understates it. **Ship the diary after B1/B2, or ship it with a visible "ledger known inconsistent" banner.**
- **Where:** a new tab on `/portfolio/paper` (or the reporting page). Realized comes from `trades` grouped by `strategy_name` + `portfolios.name`, day-scoped by IST exit date; the running column comes from live open structures, not from the DB.
- **Accept when:** for 30-Oct the diary reproduces the 13 closed rows measured in `TRADES-300K-LOSS-2026-10-30.md` (net −₹10,520.51, including the five 15:10 `eod_flat` rows), and every strategy row sums to the day total.

### Explicitly out of scope

- Fee-component breakdown in the UI (brokerage / STT / GST / stamp) — operator declined 30-Oct-2026.

---

## 🚨 Immediate: Pre-Monday & Monday 09:15 IST Gate

- [ ] **20. Set intraday EOD auto-square-off in `.env`** — *2 min — Who: You*
  Add `OPTIONS_INTRADAY_SQUAREOFF_MINUTES=20` to `.env` so intraday option runners auto-flatten 20 minutes before the 15:30 IST close instead of carrying frozen post-close marks (`TRADE-RECONCILIATION` F5).

- [ ] **21. Repair historic `closed_quantity=0` equity rows in DB** — *5 min (with `:5000` stopped) — Who: You*
  Stop the Flask server on `:5000`, preview, then apply:
  - `python tools/repair_closed_quantity.py`
  - `python tools/repair_closed_quantity.py --apply`

- [ ] **22. Runner roster hygiene before Monday open** — *5 min — Who: You*
  Confirm no duplicate same-strategy + same-underlying runners are left active by accident (`TRADE-RECONCILIATION` F1; note Phase 1's `TimeframeBarAggregator` now keeps `15min` vs `1min` bar cadences distinct).

- [ ] **23. Phase 5.0 `1day` small pilot & day-by-day verification** — *~5 min (safe pre-Monday: `<=10` symbols, `<=120d`) — Who: You*
  Populate real `1day` rows for a 5-stock pilot and confirm `0` missing NSE sessions before unblocking Phase 5.1/5.2:
  - `PYTHONPATH=src python tools/daily_equity_ingest.py rollup --symbols RELIANCE,TCS,HDFCBANK,INFY,ICICIBANK --days 30 --apply`
  - `PYTHONPATH=src python tools/daily_equity_ingest.py fetch --symbols RELIANCE,TCS,HDFCBANK,INFY,ICICIBANK --days 60 --apply`
  - `PYTHONPATH=src python tools/daily_equity_ingest.py verify --symbols RELIANCE,TCS,HDFCBANK,INFY,ICICIBANK --days 60`

- [ ] **24. Monday 09:15 IST 180-second live `MTicker` soak gate** — *3 min at market open (with `:5000` stopped) — Who: You*
  Keep `:5000` stopped so only one WS session uses the mStock login, then run:
  - `PYTHONPATH=src python tools/mticker_ws_probe.py --from-store --mticker-soak --auto-ring NIFTY,BANKNIFTY --duration 180`
  Pass criteria: zero reactor tracebacks, clean `HotStrikeRing` subscriptions, live two-sided bid/ask ladders, and non-zero OI floats.

- [ ] **25. Merge decision (`arena/ab59b7e5-back-test` → `feature/options-trading-dev`)** — *Who: You*
  Fast-forward / merge call once item **24**'s 180s Monday soak readout is green.

---

## 📈 Phase 5: Swing Pool & Screener Roadmap (Sequenced)

- [x] **26. Phase 5.0 — `1day` equity ingest, `1min → 1day` rollup & day-by-day session verifier** — ✅ DONE (`258366a`): `src/backtest/data/daily_bar_ingest.py` + `tools/daily_equity_ingest.py` + 10-test suite (`ON CONFLICT DO NOTHING`, zero cross-chunk `UPDATE`s, canonical `03:45Z` / `09:15 IST`, pilot gate, 502 circuit breaker).

- [ ] **27. Post-Monday bulk `1day` NIFTY 200 / 500 backfill (`--allow-bulk`) + nightly job** — *Held until after Monday soak/demo — Who: You + Agent*
  Keep DB pool and mStock rate budget uncontended through Monday; afterwards run multi-year backfill + day-by-day verification (`0` missing sessions) and schedule post-15:35 IST `nightly --universe nifty_200 --allow-bulk --apply`:
  - `PYTHONPATH=src python tools/daily_equity_ingest.py fetch --universe nifty_200 --days 1095 --allow-bulk --apply`
  - `PYTHONPATH=src python tools/daily_equity_ingest.py verify --universe nifty_200 --days 1095`

- [ ] **28. Phase 5.1 — `/screener` tab (`< 2 s` VCP & RSI + Relative Strength vs NIFTY scanners)** — *Held until item **23**/**27** populates verified `1day` rows in Postgres — Who: Agent*
  Single bulk SQL read over `market_data_cache (timeframe='1day')` → vectorized VCP contraction + RSI(14) + RS vs NIFTY 50 ranking → Top-N table + one-click "Send to Swing Pool".

- [ ] **29. Phase 5.2 — `PortfolioManager.sync_runner_pool()` (`TARGET_POOL` rotation without orphaning open trades)** — *Held until Phase 5.1 — Who: Agent*
  Rotates `SYMBOL_UNIVERSE` swing runners onto the new Top-N symbols while placing deselected symbols that still hold open positions into **exit-only drain mode** until closed.

---

## 🛡️ Live Options Promotion & Branch Convergence (Post-Monday)

- [ ] **30. Live straddle / strangle risk sign-off (~2026-10-31)** — *Who: You + Agent*
  `LiveOptionBrokerAdapter` and `LongStraddle`/`ShortStraddle` shipped in `85ebf0a`, with `config/risk_limits.yaml` `entitlement.live` intentionally locked to `[long_call, long_put]`. Unlock live multi-leg structures only after Monday soak + ~1 week of clean paper straddle/strangle fills and margin/slippage review.

- [ ] **31. Branch convergence (`feature/options-trading-dev` ↔ `feature/trades-db-fallback` & `main`)** — *Who: You + Agent*
  Per `docs/BRANCH-SOURCE-MATRIX-2026-10-20.md`, port `feature/trades-db-fallback` (`_db_closed_trades`, commit `a82dec2`) and `main`'s 18 data-era commits (Upstox historical fetcher, migration `020` coverage summary table) onto the v2 `Strategy-Owned Execution` line without regressing v2 strategies or `mticker.py`.

---

## 🗂️ 2026-10-06 Sweep Items — Updated Status

- [x] **13. DB connection exhaustion (`:5000` hoarding ~99/100 PG connections)** — ✅ FIXED in code (`97430a8`, Phase 1): `PortfolioManager` and `DbSource` now reuse the shared `DatabaseManager` engine (`POOL-3` guard prevents ad-hoc `create_engine()` leaks; `daily_equity_ingest.py` uses bounded `pool_size=1, max_overflow=0`). Monitor `pg_stat_activity` during Monday's full-session run to confirm steady-state count.
- [ ] **14. Equity 1-min backfill interrupted at 77/200 symbols** (2026-10-04 broker 502 storm) — *Held until after Monday — Who: You + Agent*. Keep bulk historical fetches gated until after Monday's demo; note swing pools now use `1day` bars via Phase 5.0 (`tools/daily_equity_ingest.py`) rather than 3 years of `1min` bars.
- [ ] **15. Index 1-min backfill** — NIFTY done; BANKNIFTY + FINNIFTY + MIDCPNIFTY **paused awaiting go-ahead** (post-Monday); SENSEX + INDIAVIX **deferred**. *Who: You*
- [x] **16. Daily-loss breaker UTC-window blind spot** — ✅ DONE (`d7c3adb`, `8074fca`, `9cfdf0a`): `session_date` is stamped in IST at close and rolls at `00:00 IST` across equity trades and option structures; verified live on 2026-10-23 (`docs/TRADE-RECONCILIATION-2026-10-23.md`).
- [x] **17. `/reporting` 500 on paper trades (timezone mix)** — ✅ FIXED in code (`src/backtest/reporting/records.py` `_normalize_aware_dt`, `sources.py`, `consolidator.py`): `TradeRecord.__post_init__` normalizes mixed naive/aware paper and DB timestamps to UTC-aware datetimes before sorting, dedupe, and holding-period math.
- [x] **18. Live page leaks paper positions (`/portfolio/live`)** — ✅ FIXED in code (`portfolio_manager.py:list_positions(mode)`, `api/portfolio.py`, `portfolio.js:755-759`, verified in `docs/AFTERMARKET_VALIDATION_2026-10-14.md`): REST reads and SSE `render(p)` scope `runners`, `positions`, `orders_summary`, and `bucketMetrics` to `PAGE_MODE`.
- [x] **19. Data-refresh chip (`components/freshness_chip.js`, `GET /api/data/freshness`)** — ✅ LIVE across post-2026-10-06 server restarts (`base.html:224`).

---

## 🔥 Earlier Operational & Smoke-Test Items

- [x] **1. Real-session smoke test (T9.5)** — ✅ DONE (2026-10-23): mStock login + TOTP + live WS depth verified across 7 paper runners and 29/29 two-sided option fills (`QUOTE_ONE_SIDED=0`, zero `synthetic:bs` fallbacks; see `docs/MSTOCK-WS-FEED-ROOT-CAUSE-2026-10-23.md`).
- [ ] **2. Start chain snapshot capture** — *~5 min to start, runs all day — Who: You*
  Command: `PYTHONPATH=src python scripts/snapshot_option_chains.py --interval-seconds 300`
  Win: option chain snapshots accrue for historical chain replay/backtests.
- [x] **3. Intraday strategy paper run** — ✅ DONE (2026-10-23): full-session paper run completed across `immediate_strangle`, `banknifty_ema_momentum`, `CE/PE_instant_buy`, and `ema_pullback`.
- [x] **4. Record findings** — ✅ DONE (2026-10-23): logged in `docs/MSTOCK-WS-FEED-ROOT-CAUSE-2026-10-23.md` and `docs/TRADE-RECONCILIATION-2026-10-23.md` (with reconciliation findings F1, F3, F4, F5, F6 addressed in `599be5d` and Phases 1–3).

---

## 🧠 Sub-Second Tick Feed vs True HFT — Architecture Reality

**True colocation HFT (microsecond order-book queue jumping) is out of scope** for a retail Python + REST/WS stack.
**What IS now live (`97430a8` → `85ebf0a`):**
- **Sub-second option monitoring & exits**: `MTicker` binary WebSocket frames → `MStockTickBus` → `HotStrikeRing` (±10 ATM strikes in `MODE_FULL` 5-level depth) → `OptionsBridge.on_tick()` sub-second MTM + SL/TP/trailing exits + `<50 ms` WS order/trade fill wakeup.
- **Multi-timeframe intraday & swing bars**: `TimeframeBarAggregator` builds genuine `1min`, `5min`, `15min`, `30min`, and `1hour` closed session bars from live ticks/bars, plus Phase 5.0 `1day` bars for swing pools.

---

## 🟠 Repo, Governance & Tax Follow-ups

- [x] **5. High-turnover intraday strategy family + conformance battery** — ✅ DONE (2026-09-28, migrated to v2 templates in `PR #45`).
- [ ] **6. CI workflow push** (`.github/workflows/ci.yml` needs your account — workflow scope) — *15 min — Who: You*
- [x] **7. Portfolio-page option trade rows (UI polish)** — ✅ DONE (2026-09-28).
- [ ] **8. Consultant answers on 6 open questions** — `CONSULTANT_RESPONSE.md` §0.4 — *Who: You*

### Consolidated P&L / tax report (PRD-002) — code shipped ✅, user sign-offs open

- [ ] **Tax config sign-off** — *Who: You (+ your CA)*. Verify `config/reporting.yaml` rates match your actual marginal slab before treating "estimated tax" as authoritative.
- [ ] **Validate dhan broker profile** — *Who: You (one contract note)*. `/settings` → dhan card → Edit/validate with real contract note amounts.
- [ ] **Turn monthly email on** — *Who: You*. Set `monthly_email.enabled: true` and SMTP host in `config/reporting.yaml`; export `REPORTING_SMTP_PASSWORD`.
- [ ] **Reconciliation against real contract notes** — *Who: You + Agent*. `/reporting` → paste each broker's note net P&L → verify PASS/WARNING/FAIL.

---

## 🟢 Later (Backlog)

- [x] **9. Options-tab hard delete** — ✅ DONE (2026-09-22).
- [ ] **10. Risk envelope V2 (BS+IV+SPAN)** — after consultant answers (`#8`). *Who: Agent*
- [ ] **11. Multi-leg structures (straddles/condors)** — *straddles half is DONE (`85ebf0a`: `LongStraddle` + `ShortStraddle` shipped on `StructureKind.STRADDLE`, paper-entitled; live entitlement gated under `#30`); 4-leg iron condors / butterflies remain.* *Who: Agent*
- [ ] **12. Sizing presets + richer metrics (Sortino, profit factor) — vectorized path** — *metrics half is DONE (`engine/metrics.py` has Sortino, profit factor, expectancy, VaR/ES); sizing presets on the vectorized path remain.* *Who: Agent*
