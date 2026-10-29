# Production Caveats, Risk Guardrails & `e06c003` Audit Log (v2.0)

**Status**: Reconciled & Verified against `origin/feature/options-trading-dev` @ `e06c003`  
**Date**: 2026-10-24  
**Base Commit**: `e06c00365ed778ee6cbacb7e78bc4d01592568e6` (`arena/ab59b7e5-back-test`)  
**Companion Document**: [`docs/REALTIME_OPTIONS_AND_SWING_POOL_IMPLEMENTATION_PLAN.md`](./REALTIME_OPTIONS_AND_SWING_POOL_IMPLEMENTATION_PLAN.md)

This document records **(Part A) all active engineering, quantitative, NSE/NFO market-microstructure, database, and repository caveats** that every implementation PR must satisfy, and **(Part B) the completed audit record** reconciling commit `e06c003` and the 2026-10-23 live session findings.

---

## Part A — Mandatory Production Caveats & Guardrails

### 1. mStock WebSocket (`MTicker` & `MStockWebSocketWrapper`) Caveats

| ID | Caveat | Exact Code / Incident Source | Mandatory Guardrail |
| :--- | :--- | :--- | :--- |
| **WS-1** | **`mticker.py` Is a Locked Patched Vendor SDK** | `AGENTS.md`, `docs/MSTOCK-WS-FEED-ROOT-CAUSE-2026-10-23.md`, `tests/test_mticker_ws_fixes.py` | **Do NOT edit `src/backtest/data/mticker.py`**. `_send_frame()` (`reactor.callFromThread`), `_on_open()` (`LOGIN` before `resubscribe`), and tuple-free `open_interest*` assignments are already fixed in `e06c003`. Attach all tick-bus push callbacks in `MStockWebSocketWrapper` (`mstock_ws_wrapper.py`) and wire `ticker.on_order_update` / `on_trade_update` externally in `MStockBroker._init_websocket_feed()`. |
| **WS-2** | **Dead `max_age_seconds` Check vs. Quiet-OTM-Strike Trap** | `mstock.py:512–522` vs `mticker.py:725` (`"%Y-%m-%dT%I:%M:%S%p"`, e.g. `"2026-10-23T03:30:17PM"`) | `datetime.fromisoformat()` throws `ValueError` on `AM`/`PM` strings, which `except (ValueError, AttributeError): pass` swallows—so stale post-15:30 quotes never expire (Recon Finding F5). Fix in `MStockWebSocketWrapper` using `recv_mono = time.monotonic()` AND **separate socket liveness from strike activity**: if the socket is connected and index ticks (`26000`/`26009`) arrived `< 5s` ago, do **not** evict a resting OTM strangle quote just because nobody traded that OTM strike for 15s (evicting it forces an HTTP LTP-only fallback → `QUOTE_ONE_SIDED` block!). Reject only when the socket is closed, outside market hours, or global socket frames stalled `> max_age_seconds`. |
| **WS-3** | **Twisted Global Reactor Cannot Restart (`ReactorNotRestartable`)** | `mticker.py:338` | Never call `reactor.stop()` during broker logout or token refresh; close the transport (`ws.sendClose()`, `factory.stopTrying()`) while keeping Twisted's singleton reactor alive for the next TOTP login. |
| **WS-4** | **After-Hours 3-Second Server Flap Is Normal** | `MSTOCK-WS-FEED-ROOT-CAUSE-2026-10-23.md` §5 | After `15:30 IST`, mStock's server closes even authenticated sockets every `~3s`. Gate WebSocket reconnect/tick-bus loops by NSE market status (`09:00–15:35 IST` trading days) so after-hours sessions rest quietly instead of spamming reconnect logs. |
| **WS-5** | **Single-Socket Subscription Budget (~300 Tokens Max)** | Retail WebSocket limits | Cap active WS tokens (`MAX_WS_TOKENS = 300`): `MODE_FULL` strictly for `HotStrikeRing` + open option legs (~30–60 tokens); `MODE_QUOTE` for active equity watchlists (~15–200 tokens). Never stream all 500 NSE stocks live—filter after-hours via `/screener`. |
| **WS-6** | **5-Second Quote Cache Hides Sub-Second WS Moves** | `quote_providers.py:438–450` (`cache_ttl = 5.0`) | Bypass the 5s TTL in `LiveQuoteProvider.get_quote()` when `quote.get("source") == "ws"`. Keep the 5s TTL solely for HTTP fallback calls to protect HTTP rate limits. |

---

### 2. Quantitative, Timeframe & Signal-Integrity Caveats

| ID | Caveat | Exact Failure Mode | Mandatory Guardrail |
| :--- | :--- | :--- | :--- |
| **Q-1** | **Timeframe-Blind Bar Fan-Out (Recon Finding F1)** | `portfolio_manager.py:2145–2150` — On 2026-10-23, `banknifty_ema_momentum·15min` received all 213 raw `1min` bars and traded identically to the `1min` runner, doubling realized loss (`-₹6,645.30 × 2`). | Aggregate live `1min` bars per `(symbol, runner.config.timeframe)` aligned to IST `09:15` session boundaries (`09:15`, `09:20`, `09:30`, `10:15`, `15:30`) before invoking `runner.process_candle_event()`. Add a spawn-time duplicate-runner warning when `(strategy, symbols, timeframe, mode, params)` matches an active runner. |
| **Q-2** | **Lookback Compression on Raw Ticks** | Appending 1-second ticks to `StrategyRunner._bars` turns a `21-bar 1-min EMA` into a `21-second EMA`, violating backtest parity. | Keep `StrategyRunner._bars[sym]` strictly as **immutable closed timeframe bars**; maintain intra-interval state separately in `_forming_bar[sym]`. |
| **Q-3** | **Intra-Bar Whipsaw Churn** | Evaluating EMA crossovers on every tick crosses back and forth inside noisy candles (98% of 2026-10-23's option loss came from BANKNIFTY whipsaw flips). | Default indicator strategies to `evaluation_cadence = "bar_close"` (entries & `signal_flip` on closed timeframe bars only), while upgrading **protective exits (`stop_loss`, `take_profit`, `risk_cap`, manual stops)** to sub-second `on_tick` (solving Finding F3's `-₹546` overshoot without adding entry churn!). |
| **Q-4** | **Tick Double-Fire Race (`_entry_in_flight`)** | Coalesced tick #1 at `09:20:00.150` triggers entry; while broker/bridge execution runs, tick #2 at `09:20:00.300` sees `open_structure_id is None` and fires a duplicate structure. | Guard tick-cadence entries with an atomic per-runner `_entry_in_flight` lock + configurable `reentry_cooldown_seconds` after stop-out. |
| **Q-5** | **Full-Day `quote/ohlc` Candle-Body Corruption** | `mstock_live_feed.py:378` — `quote/ohlc` returns **09:15 full-day** Open/High/Low, corrupting per-minute `close > open` / candle-range strategies (`VolumeOISurge`, `OneMinScalp`). | Construct live intraday candles from `MTicker` ticks via `BarBuilder` and backfill completed minutes from `instruments/intraday/.../minute`—never treat `quote/ohlc` as a 1-minute candle. |
| **Q-6** | **Phantom Historical Fills on Warmup** | Feeding warmup candles into `process_candle_event()` executes stale morning signals at current live prices. | Warmup **must** call `StrategyRunner.seed_history()` which populates `_bars`, `_last_bar_ts`, `last_price`, and `LiveChainProvider.set_spot()` with **zero** signal evaluation or order placement. |
| **Q-7** | **09:15:00–09:15:05 Opening Spread Spike** | First 3–5 seconds after NSE 09:15 open often show wide option spreads before market-makers quote tight two-sided ladders. | Enforce `market_open_stabilization_seconds = 5` (configurable) + `max_bid_ask_spread_pct` sanity check on instant-at-open tick entries. |

---

### 3. NSE/NFO Options, Straddle & Accounting Caveats

| ID | Caveat | Exact Failure Mode | Mandatory Guardrail |
| :--- | :--- | :--- | :--- |
| **OPT-1** | **`mode="live"` Option Bookkeeper & Orders-Tab Parity** | `paper_runner.py:1179, 1199`, `options_bridge.py:277` — `LiveOptionTrader` alone has no `StructurePosition` MTM book, state persistence, or `on_leg_fill` hook. | Implement `LiveOptionBrokerAdapter` delegating real order placement to `LiveOptionTrader` while recording actual broker leg fills in the `StructurePosition` book and firing `on_leg_fill` → `OrderLedger.record_filled()` so the Orders tab (`Lots`, `structure_id`, live `slippage`) and MTM/ExitPolicy work identically in `paper` and `live`. |
| **OPT-2** | **NFO Market-Order Rejection & Thin-Depth Slippage** | Naked `MARKET` orders on NSE NFO risk broker rejection or freak-print fills across empty ladder rungs. | Default live option legs to **Marketable `LIMIT` orders** (`ask + buffer` for BUY, `bid - buffer` for SELL, tick-rounded to `₹0.05`) derived from live WebSocket 5-level depth. |
| **OPT-3** | **`OrderOutcomeUnknown` Non-Retry Invariant** | `options/live_trading.py:13–24` — Retrying or blind-flattening after an HTTP timeout on Leg 2 risks naked double exposure if Leg 2 actually filled at NFO. | Never auto-retry an uncertain submit. Check `MTicker.on_order_update` push cache + reconcile `get_order_book()` once; if still indeterminate, halt the runner loudly for operator review. |
| **OPT-4** | **Hedged Margin Execution Order & Straddle Separation** | Submitting `SELL` before `BUY` on spreads rejects for naked SPAN margin; lumping debit and credit straddles together blurs capped vs unlimited risk. | Define distinct `long_straddle` (debit, `structure_capped=True`) and `short_straddle` (credit, mandatory hard stop in `ExitPlan` + explicit `config/risk_limits.yaml` entitlement). On spreads, confirm `BUY` wing fill via WS order push before sending `SELL` leg; on 2-leg same-side straddles/strangles, pre-check margin and dispatch both legs concurrently (`<100 ms`). |
| **OPT-5** | **15:15–15:20 IST Broker RMS Auto-Squareoff Collision** | `product="INTRADAY"` positions are auto-closed by mStock RMS ~15:15–15:20 IST. Firing runner square-off at the same minute can double-close into a naked reverse position. | Default `squareoff_minutes_before >= 20` (`<= 15:10 IST`) for intraday structures, and reconcile open broker net qty before sending EOD close orders. Make overnight carry vs EOD square-off explicit in UI/config (Recon Finding F5). |
| **OPT-6** | **Exit Unit Naming & Option Commission / `session_date` Accounting (Recon Findings F2, F4, & `daily_pnl` Gap)** | `exit_policy.py:40` (`stop_loss_points` = ₹ of structure P&L, causing 1.5-pt noise stops on 65-lot NIFTY); `TRADE-RECONCILIATION-2026-10-23.md` F4 (`StructurePosition.realized_pnl` excludes commission); `paper_runner.py:3142` (option closes lack `session_date`). | (1) Document/label `stop_loss_points` clearly in UI as **Structure ₹ P&L** (or add `stop_loss_premium_pts`); (2) Stamp `session_date` (`00:00 IST`) on closed `StructurePosition`s so `daily_pnl()` rolls options across midnight/restarts just like equities; (3) Align `net_pnl` (after `commission` + statutory fees) vs `gross_pnl` consistently across Trade History and runner summary. |

---

### 4. Database, Rate-Limit, Dynamic Swing Pool & Repo Caveats

| ID | Caveat | Exact Failure Mode | Mandatory Guardrail |
| :--- | :--- | :--- | :--- |
| **POOL-1** | **Orphaned Multi-Week Swing Holds on Nightly Pool Sync** | Replacing `runner.config.symbols` from tonight's VCP scan evicts `TRENT` (bought 4 days ago), killing its live price feed and stop-loss protection. | Enforce the **Pinned-Holdings Invariant**: `runner.pinned_symbols()` (`open_positions ∪ pending_orders ∪ active_retries`) can **never** be unsubscribed or dropped by `sync_runner_pool()`. Only flat, un-triggered `watchlist_symbols` swap out. |
| **POOL-2** | **Restart Regression or False `STRATEGY_CODE_CHANGED` Halt** | Persisting dynamic pool edits inside the frozen spawn config trips `config_sha256` validation, while not persisting them reverts to Day-1 symbols on restart. | Persist `watchlist_symbols` and Screener `setup_levels` as **mutable runtime state** in `PortfolioStateStore` alongside positions (separate from the immutable strategy-bundle hash). |
| **POOL-3** | **`DbSource` Raw Engine Bypass** | `src/backtest/data/db_source.py:118–122` still calls `sqlalchemy.create_engine()` directly instead of `DatabaseManager.from_env()` (`db/manager.py:216`). | Route `DbSource`, `WarmupService`, and `Screener` through `DatabaseManager.from_env().engine` + single bulk `ANY(:symbols)` SQL queries. |
| **POOL-4** | **mStock `T+1` Historical Blind Spot & `2.5 req/s` HTTP Ceiling** | `mstock_live_feed.py` / `data_manager.py:114` — `historical` lacks today's bars; bursting 50 `intraday/minute` calls triggers HTTP 502 storms; cold symbol lookups re-download full `scriptmaster`. | Cache `ScriptMasterCache` once daily; stitch DB historical + Session Bar Recorder + 4-worker rate-paced (`0.45s`, `<2.5 req/s`) `intraday/minute` gap-fill; isolate bad/suspended tickers via per-symbol `WARMUP_DEGRADED` status. |
| **REPO-1** | **Duplicate Alembic Revision `020` Across Branches** | `docs/BRANCH-SOURCE-MATRIX-2026-10-20.md` ("Hazards") — two `down_revision = "019"` migrations already exist (`020_option_intent_journal` and `020_add_upstox_source_to_portfolios`), plus `migrations/020_coverage_summary_table.sql` on `main`. | Avoid adding conflicting Alembic `020/021` heads before renumbering; keep Dynamic Pool & Screener state in existing tables or JSON/config store first, or chain cleanly off `020_option_intent_journal` with explicit merge-note documentation. |

---

## Part B — Completed Audit Record (`f49e89a` → `e06c003`)

- [x] **1. WebSocket Auth, Thread Safety & Binary Parser (`mstock.py`, `mticker.py`, `mstock_ws_wrapper.py`)**
  - **Verified**: All 6 causes fixed in `e06c003` and pinned by `tests/test_mticker_ws_fixes.py` (8 tests). `mticker.py` marked read-only vendor SDK in `AGENTS.md`. Uncovered dead `fromisoformat("%I:%M:%S%p")` freshness check in `mstock.py:517` → scheduled for fix in `MStockWebSocketWrapper` (`WS-2`).
- [x] **2. Option Quote Token Routing (`brokers/mstock.py`, `options/quote_providers.py`)**
  - **Verified**: Numeric token is now tried first for WS depth; HTTP rejects numeric keys cleanly (`_is_numeric_key`); `QUOTE_ONE_SIDED` logged (`tests/test_option_quote_token_routing.py`). Identified `5.0s` `cache_ttl` on `LiveQuoteProvider` (`WS-6`) and `0.5s` cold-subscribe sleep (`mstock.py:1146`) → addressed via `HotStrikeRing` + WS zero-TTL bypass in Phase 2.
- [x] **3. Straddle Structures & Entitlements (`options/structures.py`, `strategy/execution.py`, `config/risk_limits.yaml`)**
  - **Verified**: Only `strangle` (`ShortStrangle`), `long_call`, `long_put`, `bull_call_spread`, `bear_put_spread` exist today; `long_straddle` and `short_straddle` are not yet defined, and `live` entitlements in `config/risk_limits.yaml` currently list only `long_call`, `long_put` → scheduled for Phase 4 (`OPT-4`).
- [x] **4. Live Option Execution Wiring (`paper_runner.py`, `options_bridge.py`, `options/live_trading.py`)**
  - **Verified**: `StrategyRunner.__init__` (`paper_runner.py:1179`) constructs `OptionsBridge` without passing `LiveOptionTrader`, so `mode="live"` currently falls back to `OptionPaperBroker` in `options_bridge.py:277`. `OptionPaperBroker.on_leg_fill` → `OrderLedger.record_filled()` (`599be5d`) was added for Orders-tab visibility → Phase 4 introduces `LiveOptionBrokerAdapter` to preserve full bookkeeping + `OrderLedger` parity while executing real orders via `LiveOptionTrader` (`OPT-1`).
- [x] **5. Database Pooling, Daily P&L Fuse & 2026-10-23 Live Reconciliation (`db/manager.py`, `paper_runner.py`, `TRADE-RECONCILIATION-2026-10-23.md`)**
  - **Verified**: `DatabaseManager.from_env()` now shares one pool per `DatabaseConfig` (`e06c003`), though `DbSource` (`db_source.py:118`) still calls raw `create_engine()` (`POOL-3`). Reconciled all findings `F1–F8` from `TRADE-RECONCILIATION-2026-10-23.md`—especially **F1** (timeframe-blind 1m fan-out into `15min` runner → `Q-1`), **F3** (`-₹546` bar-close stop overshoot on `-₹100` strangle stop → solved by Phase 3 `OptionsBridge.on_tick()`), **F2/F4/F5** (`OPT-5`, `OPT-6`), and `BRANCH-SOURCE-MATRIX-2026-10-20.md` Alembic `020` hazard (`REPO-1`).
