# Real-Time Options Execution, Unified Warmup & Swing Pool Screener — Implementation Plan (v2.0)

**Status**: Reconciled Blueprint v2.0 (Synchronized against `origin/feature/options-trading-dev` @ `e06c003`)  
**Date**: 2026-10-24  
**Base Commit**: `e06c00365ed778ee6cbacb7e78bc4d01592568e6` (`arena/ab59b7e5-back-test` fast-forwarded to `origin/feature/options-trading-dev`)  
**Companion Document**: [`docs/REALTIME_OPTIONS_AND_SWING_POOL_CAVEATS_AND_AUDIT.md`](./REALTIME_OPTIONS_AND_SWING_POOL_CAVEATS_AND_AUDIT.md)

---

## 1. Reconciliation Summary (`f49e89a` → `e06c003`)

All commits pushed to `origin/feature/options-trading-dev` (`d7c3adb`, `8074fca`, `599be5d`, `e06c003`) and their session artifacts (`docs/MSTOCK-WS-FEED-ROOT-CAUSE-2026-10-23.md`, `docs/TRADE-RECONCILIATION-2026-10-23.md`, `docs/BRANCH-SOURCE-MATRIX-2026-10-20.md`) have been audited line-by-line and reconciled into this plan:

### 1.1 Already Completed in `e06c003` (Removed from Scope → Protected as Invariants)
1. **mStock WebSocket Depth Plumbing (All 6 Stacked Causes Fixed)**:
   * `MStockBroker.verify_totp()` (`mstock.py:310`) and `restore_session()` (`mstock.py:565`) both initialize `_ws_feed`.
   * `_ws_login` sends `LOGIN:<access_token>` immediately on socket open (`mstock.py:389–396`).
   * `MTicker._send_frame()` (`mticker.py:381–395`) marshals all outgoing frames onto Twisted's reactor via `reactor.callFromThread`.
   * `MTicker._on_open()` (`mticker.py:509–523`) sends `LOGIN` *before* `resubscribe()` on reconnect.
   * `MTicker._parse_binary()` (`mticker.py:722–724`) removed trailing commas on `open_interest*` fields.
   * `LiveChainProvider.get_quote()` (`quote_providers.py:829–832`), `LiveQuoteProvider._fetch()` (`quote_providers.py:454–482`), and `MStockBroker._is_numeric_key()` (`mstock.py:1093–1107`) route the numeric `instrument_token` to WebSocket depth first and never leak numeric tokens to the HTTP symbol endpoint.
   * **Hard Constraint (`AGENTS.md`)**: `src/backtest/data/mticker.py` is now a **locked patched vendor SDK** pinned by `tests/test_mticker_ws_fixes.py`. **This plan does not edit `mticker.py`**—all new tick-push hooks and arrival timestamps attach cleanly in `MStockWebSocketWrapper` and `MStockBroker`.
2. **Process-Wide `DatabaseManager.from_env()` Pool Reuse** (`src/backtest/db/manager.py:212–257`, `tests/test_db_manager.py`):
   * Solved the 200-connection market-status/intelligence leak. *(Remaining gap: `DbSource` in `src/backtest/data/db_source.py:118–122` still calls raw `create_engine()` directly instead of reusing `DatabaseManager.from_env().engine` — addressed in Phase 1 below.)*
3. **Order Ledger Option-Leg Mirroring, Equity `closed_quantity`, & `00:00 IST` Daily-Loss Fuse** (`599be5d`, `d7c3adb`):
   * `OptionPaperBroker.on_leg_fill` → `StrategyRunner._record_option_leg_fill` → `OrderLedger.record_filled()` (`paper_runner.py:486, 1199, 1408`) populates the Orders tab with `Lots`, `structure_id`, `requested_price`, and adverse-positive `slippage`.
   * `Position.closed_quantity` (`simulator/position.py:139, 425`) preserves equity closed-trade size and direction.
   * `StrategyRunner.daily_pnl()` (`paper_runner.py:3129`) rolls equity realized P&L at `00:00 IST` from `portfolio.closed_positions` and logs `RISK_UTILIZATION 50%/80%` (`risk_supervisor.py:218`).

### 1.2 Live-Session Evidence & Newly Uncovered Gaps Added to This Plan
From the 2026-10-23 live paper reconciliation (`29/29` depth fills, `0` one-sided blocks) and source audit:
1. **60s Bar-Poll Clock Still Drives Everything + 5.5× Stop Overshoot (Recon Finding F3)**:
   * `MStockBarFeed` (`feed_registry.py:493`) still sleeps `60.0s` and `mstock_live_feed.py:412` floors timestamps to `:00`. On `immediate_strangle` trade #9 (`13:33–13:39 IST`), a `-₹100` stop blew out to **`-₹546.00`** because NIFTY moved `+73 pts` intra-bar while `OptionsBridge` waited for bar close.
2. **Timeframe-Blind Bar Fan-Out (Recon Finding F1 — High Severity)**:
   * `PortfolioManager._on_bar_core()` (`portfolio_manager.py:2145–2150`) feeds every 1-minute bar straight into `runner.process_candle_event(symbol, bar)` regardless of `runner.config.timeframe` (`1min` vs `5min` vs `15min` vs `1hour` vs `1day`). On 2026-10-23, `banknifty_ema_momentum·15min` evaluated 213 1-minute bars identically to the `1min` runner (`-₹6,645.30 × 2`). Fixing this is mandatory for both `15min` option strategies and `5min`/`1day` equity pools.
3. **Cold Chain (~6s), Cold Subscribe (`0.5s`/leg), & 5s WS Quote Cache (`quote_providers.py:442`)**:
   * First option entry per underlying took `~6s` (cold chain HTTP fetch); re-entries took `~4s` (`time.sleep(0.5)` in `mstock.py:1146` on unsubscribed strikes + `LiveQuoteProvider`'s `5.0s` TTL caching WS quotes).
4. **Dead `max_age_seconds` Check in `MStockBroker.get_ws_quote()` (`mstock.py:512–522`)**:
   * `mticker.py:725` formats `exchange_timestamp` as `"%Y-%m-%dT%I:%M:%S%p"` (e.g., `"2026-10-23T03:30:17PM"`). `datetime.fromisoformat()` raises `ValueError` on every tick and is silently caught by `except (ValueError, AttributeError): pass`, rendering `max_age_seconds=10.0` completely inert (explaining frozen post-15:30 marks in Finding F5).
5. **Unwired `LiveOptionTrader` on `mode="live"` Option Runners (`paper_runner.py:1179`, `options_bridge.py:277`)**:
   * `OptionsBridge` currently defaults `self.option_broker` to `OptionPaperBroker` even when `mode="live"`. `LiveOptionTrader` (`options/live_trading.py:173`) is not yet adapted onto `OptionsBridge`, and lacks `long_straddle` / `short_straddle` structures in `options/structures.py:534` and `strategy/execution.py:83`.
6. **Zero Historical Warmup on Runner Start** (`feed_registry.py:458`, `paper_runner.py:805`):
   * `_BrokerBarFeedBase.start(warmup=False)` ignores `warmup=True`; indicators start cold (`0` bars).

---

## 2. Target Architecture: The Two-Lane Event-Driven Engine

| Dimension | Lane 1: Fast Lane (Index Options — NIFTY & BANKNIFTY) | Lane 2: Cruise Lane (Equity Swing Pools — VCP, RSI+RS, 5m EMA) |
| :--- | :--- | :--- |
| **Instruments** | `NIFTY` (`26000`), `BANKNIFTY` (`26009`) + **Hot Strike Ring** (`ATM ± 6` CE & PE = 26 tokens/underlying) | Universe (`NIFTY_50` / `200` / `500`) filtered after-hours via Screener to **10–30 active watchlist stocks** (+ held swing positions) |
| **WS Mode** | `MTicker.MODE_FULL` (5-level Bid/Ask depth + OI + LTP) | `MTicker.MODE_QUOTE` (44-byte OHLC/LTP/Volume packets) |
| **Entry Cadence** | **Any-second (`on_tick`, ~150–200 ms coalesced)** for Instant, Straddle/Strangle, Clock-time, Price-breakout; **Timeframe-true `on_bar_close`** (`1min`/`5min`/`15min`) for EMA/indicator views | **Any-second (`on_tick`)** for VCP pivot price breakouts; **Timeframe-true `5min` / `1day` `on_bar_close`** for 5m EMA / RSI / RS |
| **Exit / SL / TP Cadence** | **Sub-second (`on_tick`)** for single-leg & multi-leg straddle/strangle SL/TP, risk cap, & operator stops (eliminates F3 overshoot) | **Sub-second (`on_tick`)** on *held positions only* (5–10 stocks); trailing/indicator exits on bar close |
| **Warmup Backfill** | `market_data_cache` + today `09:15 → now` intraday stitch via `WarmupService` | `2–3 yr` `1day` DB cache (200-DMA, VCP, RS) + `60–90 day` rolling `1min` cache (5m EMA) |
| **Runner Lifecycle** | Per-strategy runner (with optional duplicate `(strategy, underlying, timeframe, mode)` spawn warning for F1) | **One Living Runner per Swing Strategy** bound to a **Hot-Swappable Dynamic Pool** (held trades stay pinned; un-triggered watchlist rotates nightly) |

---

## 3. Pillar 1: Sub-Second NIFTY & BANKNIFTY Options Execution

### 3.1 `MStockTickBus` & Monotonic Freshness (Zero Edits to `mticker.py`)
**Target files**: `src/backtest/data/mstock_ws_wrapper.py`, `src/backtest/brokers/mstock.py`, `src/backtest/options/quote_providers.py`

1. **Monotonic Arrival Stamp + Socket Liveness (`mstock_ws_wrapper.py`)**:
   * Do **not** touch `src/backtest/data/mticker.py`.
   * Inside `MStockWebSocketWrapper.on_ticks()` (`mstock_ws_wrapper.py:30–48`), stamp every normalized tick with:
     * `'recv_mono': time.monotonic()`
     * `'recv_utc': datetime.now(timezone.utc).isoformat()`
     * `'source': 'ws'`
   * Track `self._last_any_tick_mono: float` on the wrapper so `MStockBroker.get_ws_quote()` can distinguish:
     * **Live socket + quiet OTM strike**: Socket is connected (`is_connected()`) and index/heartbeat frames arrived within `5s` → a subscribed OTM strangle leg whose book hasn't printed in 25s is **still valid** (prevents false fallback to one-sided HTTP LTP!).
     * **Dead socket or post-15:30 close**: Socket disconnected or `time.monotonic() - _last_any_tick_mono > max_age_seconds` → reject quote as stale (`return {}`), fixing the broken `fromisoformat("%I:%M:%S%p")` check at `mstock.py:517` and post-close frozen marks (Finding F5).
2. **Coalesced Push Dispatcher (`150 ms` window in `MStockWebSocketWrapper`)**:
   * Maintain `self._quotes[token]` synchronously (`0 ms`) on every packet.
   * Record updated tokens in `self._dirty_tokens: set[str]` and notify a lightweight `150 ms` background dispatcher thread that invokes registered callbacks `on_tick_batch(quotes_by_token: dict[str, dict])` outside Twisted's reactor thread (protecting the reactor from downstream exceptions and preventing GIL thrashing on 9:15 AM bursts).
3. **Bypass 5-Second HTTP Cache for Live WS Depth (`quote_providers.py:436–452`)**:
   * In `LiveQuoteProvider.get_quote()`, only serve the `5.0s` `self._cache` TTL when the cached quote came from the HTTP fallback (`quote.get("source") != "ws"`). When `_ws_feed` is active, numeric-token lookups read `self._ws_feed.get_quote()` in `<10 µs` with **zero staleness lag**, so sub-second MTM and SL/TP always see real-time Bid/Ask.

### 3.2 Pre-Warmed Chain & `HotStrikeRing` (`0 ms` Strike Selection & Fill Quotes)
**Target files**: `src/backtest/forward/feed_registry.py` (`ChainBus`), `src/backtest/brokers/mstock.py`

* **Eager Chain + Ring Warmup on Spawn / Resume**:
  * When `ChainBus` acquires a `LiveChainProvider` for `NIFTY` or `BANKNIFTY` (or in `StrategyRunner._maybe_upgrade_to_live_chain()`), immediately warm the nearest + weekly option chain and pre-subscribe **`HotStrikeRing`**:
    * `NIFTY` (strike step `50`): `ATM ± 6` strikes (`CE` + `PE` = 26 tokens) in `MODE_FULL`.
    * `BANKNIFTY` (strike step `100`): `ATM ± 6` strikes (`CE` + `PE` = 26 tokens) in `MODE_FULL`.
    * Plus `±3%` OTM wings if any active runner declares `strangle` (`distance_pct`).
  * Re-center asynchronously with hysteresis (re-subscribe new edge when spot moves `>= 1` strike step; unsubscribe only outside `ATM ± 8`, and **never** unsubscribe tokens held in an open `StructurePosition`).
  * Eliminates both the `~6s` cold chain delay on first entry and the `time.sleep(0.5)` auto-subscribe pause in `mstock.py:1146`.

### 3.3 Per-Timeframe `BarBuilder` (Fixes Finding F1) + Dual-Cadence `on_tick` / `on_bar_close` (Fixes Finding F3)
**Target files**: `src/backtest/live/market_data_handler.py`, `src/backtest/forward/portfolio_manager.py`, `src/backtest/forward/paper_runner.py`, `src/backtest/forward/options_bridge.py`, `src/backtest/strategy/base.py`

1. **IST Session-Aligned `TimeframeBarAggregator` (Resolves Recon Finding F1)**:
   * Build live `1min` bars from `MTicker` index/equity ticks (`26000` → `NIFTY`, `26009` → `BANKNIFTY`, equity tokens → NSE symbols), keeping `MStockBarFeed` REST polling only as a gap/failover fallback when WS is disconnected (and using `instruments/intraday/.../minute` rather than full-day `quote/ohlc`).
   * Before calling `runner.process_candle_event(symbol, bar)`, aggregate `1min` bars into **`runner.config.timeframe` (`1min`, `5min`, `15min`, `1hour`, `1day`)** aligned to NSE `09:15 IST` session boundaries (matching `docs/DATA-INGEST-AND-TIMEFRAMES.md`).
   * Result: a `15min` BANKNIFTY runner evaluates its EMA **only at `09:30, 09:45, 10:00...`**, never on 1-minute bars! Also add a spawn-time guard/warning when an identical `(strategy_name, symbols, timeframe, mode, params)` runner is already running.
2. **Sub-Second `OptionsBridge.on_tick(symbol, spot, ts)` (Resolves Recon Finding F3)**:
   * On every coalesced `150–200 ms` spot or open-leg option tick:
     1. Update `LiveChainProvider.set_spot(symbol, spot)`.
     2. If `self._has_open_structure()`, re-mark open legs from live WS `mark_quote` (`mid` when 2-sided) and run **protective exits immediately**:
        * Operator manual stop/target (`_check_manual_levels`)
        * Platform `max_loss_inr` (`EXIT_RISK_CAP`)
        * Same-day intraday square-off (`squareoff_minutes_before`, default `>= 20` min before 15:30 to beat broker RMS auto-squareoff)
        * Strategy `stop_loss_pct` / `stop_loss_points` (`EXIT_STOP_LOSS`) & `take_profit_pct` / `take_profit_points` (`EXIT_TAKE_PROFIT`)
     3. Propagate updated `unrealized_pnl` to `RiskSupervisor` so daily-loss circuit breakers also react sub-second.
3. **Any-Second Strategy Entries (`evaluation_cadence`) + `_entry_in_flight` Latch**:
   * Add `evaluation_cadence: Literal["bar_close", "tick", "intrabar_forming"] = "bar_close"` on `Strategy` templates:
     * `"tick"` on `InstantLongOptionStrategy` (`CE_instant_buy`, `PE_instant_buy`), `StructureStrategy` (`immediate_strangle`, ATM straddles, scheduled `09:20:00` straddles), and price-level breakout strategies: evaluates immediately on runner spawn/resume and on every `150–200 ms` tick!
     * `"bar_close"` (default for `DirectionalOptionStrategy` / `EquityStrategy` EMA/RSI/Supertrend): evaluates entries and `signal_flip` exits cleanly on sealed timeframe bar close, preserving 100% backtest indicator parity while still enjoying sub-second `on_tick` SL/TP protection!
   * Protect entry execution with an atomic per-runner `_entry_in_flight` lock + configurable `reentry_cooldown_seconds` (e.g., preventing sub-second re-entry thrash right after a stop-out).

### 3.4 Straddle Structures & Live Option Execution Adapter (`mode="live"`)
**Target files**: `src/backtest/options/structures.py`, `src/backtest/strategy/execution.py`, `config/risk_limits.yaml`, `src/backtest/options/live_trading.py`, `src/backtest/forward/paper_runner.py`

1. **First-Class Straddle Structures**:
   * Add `LongStraddle` (`name = "long_straddle"`, BUY ATM CE + BUY ATM PE, debit / capped loss `structure_capped=True`) and `ShortStraddle` (`name = "short_straddle"`, SELL ATM CE + SELL ATM PE, credit / undefined risk requiring mandatory hard stop in `ExitPlan`) to `src/backtest/options/structures.py` and `KNOWN_STRUCTURES` in `src/backtest/strategy/execution.py:83`.
   * Register `long_straddle` and `short_straddle` in `config/risk_limits.yaml` (`paper` enabled; `live` gated by explicit operator review alongside `long_call`/`long_put`).
2. **`LiveOptionBrokerAdapter` — Wiring `LiveOptionTrader` into `OptionsBridge` (`mode="live"`)**:
   * Wrap `OptionPaperBroker` (as the authoritative `StructurePosition` / MTM / state-snapshot / `on_leg_fill` → `OrderLedger.record_filled()` bookkeeper) around `LiveOptionTrader` (as the real venue executor via `MStockBroker` + `DbIntentJournal`) when `runner.config.mode == "live"`:
     * **Entry (`execute_structure`)**: Submits real orders via `LiveOptionTrader`, records exact broker `avg_fill_price` per leg into `StructurePosition`, and mirrors each filled leg into `OrderLedger.record_filled()` (`requested_price` = WS ask/bid at send time → real live slippage tracking in the Orders tab!).
     * **Exit (`close_structure`)**: Submits closing orders via `LiveOptionTrader`, settles `StructurePosition` at actual broker exit prices, and stamps `session_date` (`_IST` `00:00` roll) so `StrategyRunner.daily_pnl()` (`paper_runner.py:3142`) rolls option realized P&L across restarts identically to equities.
3. **Sub-100ms Multi-Leg Straddle Dispatch + WS Order-Push + Marketable `LIMIT` Orders**:
   * Wire `ticker.on_order_update` and `ticker.on_trade_update` in `MStockBroker._init_websocket_feed()` (without editing `mticker.py`) to signal a `threading.Event` per `broker_order_id` in `LiveOptionTrader._poll_for_fill()` (`<50 ms` fill confirmation instead of `2.0s` HTTP polling).
   * Dispatch 2-leg same-side structures (`long_straddle`, `short_straddle`, `strangle`) concurrently across 2 worker threads after pre-trade margin validation; for hedged spreads (`bull_call_spread`, `bear_put_spread`), fill the `BUY` wing first (~50 ms WS ack), then fire the `SELL` leg for SEBI hedged-margin benefit.
   * Price live option legs as **Marketable `LIMIT` orders** off live WS Bid/Ask (`ask + max(2 * tick, buffer%)` for BUY; `bid - max(2 * tick, buffer%)` for SELL, rounded to `₹0.05`) to prevent NFO naked-market rejections and thin-book freak prints.

---

## 4. Pillar 2: Unified Historical + Intraday Warmup Backfill Engine

### 4.1 Strategy `warmup_bars` Contract & Shared DB Pool in `DbSource`
**Target files**: `src/backtest/strategy/base.py`, `src/backtest/data/db_source.py`, `src/backtest/data/mstock_live_feed.py`, `src/backtest/forward/paper_runner.py`

* **Migrate `DbSource` to `DatabaseManager.from_env()`**: Replace `create_engine(self.db_url)` in `DbSource._get_engine()` (`src/backtest/data/db_source.py:118–122`) with `DatabaseManager.from_env(url=self.db_url).engine` so historical reads, warmup, and screener queries share the single bounded QueuePool fixed in `e06c003`.
* **Process-Wide `ScriptMasterCache`**: Cache mStock's `/openapi/typea/instruments/scriptmaster` lookup (`symbol -> (exchange, segment_id, numeric_token, lot_size, tick_size)`) once per day in memory/disk so resolving 50–200 pool equities takes `0` extra HTTP downloads.
* **Strategy-Declared `warmup_bars`**:
  * Replace hardcoded `MIN_WARMUP_BARS = 12` (`paper_runner.py:805`) with `strategy.warmup_bars` (`0` for `InstantLongOptionStrategy` / `immediate_strangle`; `max(50, 3 * max_lookback)` for EMA/RSI/Supertrend; `260` `1day` bars for VCP / Relative Strength). Bump `MAX_BARS_PER_SYMBOL` from `500` to `600`.

### 4.2 `WarmupService`: Bulk DB Query + Today Intraday Gap-Fill + Silent `seed_history()`
**Target files**: `src/backtest/forward/warmup.py` *(new)*, `src/backtest/forward/paper_runner.py`, `src/backtest/forward/portfolio_manager.py`

1. **Layer 1 — Single Bulk SQL Read (`market_data_cache`)**:
   * Fetch prior sessions for all runner symbols in **one `WHERE symbol = ANY(:symbols)` query** (`timeframe = '1day'` for daily swing runners; `timeframe = '1min'` for intraday runners).
2. **Layer 2 — Today's `09:15 IST → Now` Stitch**:
   * First read any completed 1-minute bars already captured in the in-memory **Session Bar Recorder** (zero HTTP calls for symbols already streaming since 09:15, such as `NIFTY` and `BANKNIFTY`).
   * For uncached symbols spawned mid-session, fetch `instruments/intraday/{seg_id}/{token}/minute` through a **rate-paced 4-worker pool (`0.45s` spacing, `<2.5 req/s`)** respecting mStock's gateway ceiling (`data_manager.py:114`).
3. **Resample & Silent `StrategyRunner.seed_history()`**:
   * Deduplicate and resample to `runner.config.timeframe` (`1min`, `5min`, `15min`, `1hour`, `1day`), then call `runner.seed_history(bars_by_symbol)`.
   * **`seed_history()` populates `_bars`, `_last_bar_ts`, `last_price`, and `LiveChainProvider.set_spot()` silently — zero signal evaluations, zero phantom historical fills.**
   * **Per-Symbol Quorum**: In a 50-stock pool, if 1 symbol is suspended or missing data, flag only that symbol `WARMUP_DEGRADED` and transition the runner from `STATUS_WARMING_UP` to `STATUS_RUNNING` for the remaining 49 stocks.

---

## 5. Pillar 3: After-Hours Swing Screener Tab & Hot-Swappable Dynamic Pools

### 5.1 Dual-Tier Data Retention Policy
* **`1day` Candles (`2–3+ years` for `NIFTY_200` / `NIFTY_500` + `NIFTY`)**: `<50 MB` total in PostgreSQL. Powers VCP stage analysis (`SMA150`/`SMA200`), contraction detection, 52-week high proximity, Relative Strength vs `NIFTY`, and Daily RSI.
* **`1min` Candles (`60–90 rolling days` for active watchlist/universe + indices)**: Powers `5min` EMA strategies, intraday warmup, and intraday backtests with tiny storage (~0.4–1.5 GB/yr).

### 5.2 Screener Tab (`/screener`) & Deterministic Scanners
**Target files**: `src/backtest/screener/` *(new: `engine.py`, `vcp.py`, `rsi_rs.py`)*, `src/backtest/api/screener.py` *(new)*, `src/backtest/web/templates/screener.html` *(new)*, `src/backtest/web/templates/base.html`

* Add **Screener** under **Research** in `base.html`.
* **Scanners** run in `<2 seconds` over bulk-loaded `1day` DataFrames from `market_data_cache`:
  * **VCP Scanner**: Stage-2 trend template (`Close > SMA150 > SMA200`, rising `SMA200`, within 25% of 52W high) + 2–4 tightening volatility contractions + volume dry-up → outputs `pivot_price`, `stop_loss_price`, `tightness_pct`, `rs_rating`.
  * **RSI + Relative Strength Scanner**: Multi-horizon RS percentile vs `NIFTY` (`RS >= 80`) + Daily RSI regime / pullback hook → outputs `rs_rating`, `rsi_14`, `trigger_price`, `atr_stop`.
* **1-Click Workflow**: Operator selects top 10–15 setups → clicks **"Save Dynamic Pool & Sync to Living Runner"**.

### 5.3 Living Runner + Pinned-Holdings Hot-Swap (`sync_runner_pool`)
**Target files**: `src/backtest/data/universe.py`, `src/backtest/forward/paper_runner.py`, `src/backtest/forward/portfolio_manager.py`, `src/backtest/forward/state_store.py`

* **Split Membership Inside `StrategyRunner`**:
  * `watchlist_symbols: set[str]` (+ optional per-symbol `setup_levels: dict[str, dict]` carrying `pivot_price` / `initial_stop` from Screener): eligible for **new entries** up to `open_slots = max_pool_positions - len(open_positions)`.
  * `pinned_symbols() -> set[str]`: any symbol with an **open position** (`portfolio.positions`), a **working/pending order** (`OrderLedger` / `LiveEquityGateway`), or an **active retry** (`_retries`).
  * `active_symbols = watchlist_symbols | pinned_symbols()`.
* **Behavior During Nightly Sync (`PortfolioManager.sync_runner_pool`)**:
  1. Held swing winners (`TRENT`, `BEL` bought days/weeks ago) remain in `pinned_symbols()`—they stay subscribed on `MTicker` (`MODE_QUOTE`) and continue receiving live SL / trailing-stop / indicator-exit protection until fully closed.
  2. Flat, un-triggered stocks from yesterday that dropped off tonight's scan are cleanly unsubscribed from `MTicker` and evicted from `_bars`.
  3. Newly added Screener candidates are warmed up via `WarmupService`, subscribed on `MTicker` (`MODE_QUOTE`), and persisted in `PortfolioStateStore` as mutable runtime watchlist state (without invalidating the strategy code-bundle fingerprint `STRATEGY_CODE_CHANGED`).

---

## 6. Reconciled Phased Implementation Roadmap

| Phase | Scope | Key Files | Acceptance Gate |
| :--- | :--- | :--- | :--- |
| **Phase 1** | **Timeframe Bar Aggregator (Fix F1) + Unified `WarmupService` + `DbSource` Shared Pool** | `db_source.py`, `mstock_live_feed.py`, `forward/warmup.py`, `paper_runner.py`, `portfolio_manager.py` | `15min` & `5min` runners only fire on true `15min`/`5min` IST boundaries (F1 fixed); cold spawn of `DeltaMomentum` or `NIFTY_50` seeds history in `<10s` via 1 SQL query + paced intraday stitch with 0 phantom orders and 1 DB pool. |
| **Phase 2** | **`MStockTickBus` + WS Freshness Fix + `HotStrikeRing` + Zero-TTL WS Quotes** | `mstock_ws_wrapper.py`, `brokers/mstock.py`, `quote_providers.py`, `feed_registry.py` *(0 edits to `mticker.py`!)* | `pytest tests/test_mticker_ws_fixes.py tests/test_option_quote_token_routing.py` green; `recv_mono` freshness distinguishes live quiet OTM strikes from closed/dead sockets; `ATM ± 6` CE/PE depth pre-subscribed (`0 ms` lookup, no `0.5s` sleep). |
| **Phase 3** | **Sub-Second `OptionsBridge.on_tick()` (Fix F3) + `evaluation_cadence="tick"` + Option `session_date` Roll** | `options_bridge.py`, `paper_runner.py`, `portfolio_manager.py`, `options/paper_trading.py`, `strategy/templates/` | Open structure SL/TP/risk-cap triggers within `<250 ms` intra-bar (eliminating 5.5× F3 overshoot); `CE_instant_buy` / `immediate_strangle` enter any second with `_entry_in_flight` lock; closed structures stamp IST `session_date` + reconcile F4 commission display. |
| **Phase 4** | **`LongStraddle` / `ShortStraddle` + `LiveOptionBrokerAdapter` (`mode="live"`) + WS Order Push** | `options/structures.py`, `strategy/execution.py`, `config/risk_limits.yaml`, `options/live_trading.py`, `brokers/mstock.py` | `long_straddle` & `short_straddle` unit-tested; `mode="live"` routes through `LiveOptionTrader` with WS `on_order_update` (`<50 ms`), concurrent 2-leg straddle dispatch, marketable `LIMIT` pricing, and `OrderLedger.record_filled()` parity. |
| **Phase 5** | **After-Hours Swing Screener (`/screener`) + Living Runner `sync_runner_pool()`** | `screener/*`, `api/screener.py`, `web/templates/screener.html`, `universe.py`, `portfolio_manager.py`, `state_store.py` | EOD VCP & RSI+RS scan across 200–500 `1day` stocks runs in `<2s`; 1-click "Sync to Runner" swaps un-triggered watchlist stocks while pinning open swing positions across server restarts. |
