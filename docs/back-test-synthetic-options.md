# Plan: Model-Driven Options Backtesting for NIFTY / BANKNIFTY

**Document owner:** [You]
**Review assigned to:** Junior Developer
**Date:** 2026-10-06
**Status:** DRAFT – for gap review against codebase

---

## 1. Problem Statement

We have a production options expression layer (view → selector → structure → intent), a Phase-A bar-by-bar backtest driver with synthetic Black-Scholes pricing, and the full Indian statutory fee stack. However, the options backtest path is **frozen** (`CHAIN_SOURCES = ()` in `src/backtest/data/sources_policy.py`), making `option_backtest_allowed()` always false. The UI and API do not expose any options backtest mode.

We **do not have historical option chain data** (strike-level OHLCV, IV, OI) for NIFTY or BANKNIFTY. We **do** have 1-minute index bars in TimescaleDB (`market_data_cache`) for the curated NIFTY 200 + NSE index universes.

**Goal:** Enable a **model-driven options backtest** where:
- The **signal** comes from an existing equity strategy (or a `MarketView`-producing strategy) run on index candles.
- The **option pricing** is synthetic Black-Scholes, fed by **realized volatility** computed from those same index candles, plus a configurable skew/spread model.
- The **execution** reuses the existing structure/leg/fee/exit logic.
- All outputs are clearly labelled **"MODEL – synthetic BS pricing, not historical market premiums."**

This is **not** a data-driven backtest. It is a **stress-test / signal-validation tool** that captures delta, gamma, theta, and vega effects that a plain index % return cannot.

---

## 2. Current State – What Exists (Verify Each Item)

> **Junior: For every row below, open the referenced file and confirm it exists, is importable, and behaves as described. Mark ✅ / ❌ / ⚠️ (exists but differs) in the "Verified" column. Note the exact line number or symbol if it differs.**

| # | Component | Expected Location | What to Verify | Verified |
|---|-----------|-------------------|----------------|----------|
| 2.1 | Options expression layer (view → selector → structure → intent) | `src/backtest/options/` | `MarketView`, `StrikeSelector`, `StructureBuilder`, `TradeIntent` classes exist and are importable. Check `__init__.py` exports. | |
| 2.2 | Phase-A backtest driver (bar-by-bar BS pricing) | `src/backtest/options/` (likely `backtest_driver.py` or similar) | A function/class that iterates over a candle frame, prices structures bar-by-bar, applies exits, and produces a trade log + equity curve. Confirm it supports: long call, long put, bull call spread, bear put spread. Confirm `set_reference(bar_time)` pins quotes to bar time. | |
| 2.3 | Freeze gate | `src/backtest/data/sources_policy.py` | `CHAIN_SOURCES = ()` and `option_backtest_allowed()` returns `False`. Confirm the exact mechanism (env var? constant? DB flag?). | |
| 2.4 | Synthetic BS pricing function | `src/backtest/options/` (likely `pricing.py` or `bs.py`) | A `black_scholes(call_or_put, spot, strike, iv, r, t, sigma)` function. Confirm it returns price, delta, gamma, theta, vega. Confirm it uses `Decimal` or `float` (note: README says "Decimal-exact throughout" for options layer). | |
| 2.5 | Indian statutory fee stack | `src/backtest/simulator/` | Fee calculation for options: STT, exchange transaction charge, SEBI turn, stamp duty, GST, ₹20/order brokerage. Confirm the function signature and whether it takes per-leg or per-structure. | |
| 2.6 | Instrument model (option) | `src/backtest/instruments/` | `OptionInstrument` or equivalent with `underlying`, `strike`, `expiry`, `lot_size`, `strike_interval`. Confirm NIFTY lot_size=25, strike_interval=25 (or 50/100 for higher strikes); BANKNIFTY lot_size=15, strike_interval=100. Check if these are in the `instruments` DB table or hardcoded. | |
| 2.7 | Expiry calendar | `src/backtest/instruments/` | Function to get valid NIFTY/BANKNIFTY expiry dates (Thursday weekly / monthly). Confirm source: hardcoded list? NSE API? DB table? | |
| 2.8 | Equity strategies (signal source) | `src/backtest/strategies/`, `plugins/strategies/` | List all strategies that produce `+1 / -1 / 0` signals. Confirm `generate_signals(candles) -> pd.Series` contract. Note which ones are suitable as option signal sources (momentum/trend) vs. mean-reversion (may not map cleanly to directional options). | |
| 2.9 | `MarketView`-producing strategies | `src/backtest/strategies/directional_options.py` (or similar) | A strategy that outputs `MarketView(bullish/bearish)` instead of raw +1/-1/0. Confirm the interface. | |
| 2.10 | Backtest runner (canonical) | `src/backtest/engine/backtest_runner.py` | `run_backtest(candles, strategy_name, params, symbol, capital)` → `BacktestResult`. Confirm the signature. This is the entry point the options backtest will call for the signal phase. | |
| 2.11 | Backtest driver (legacy vectorized) | `src/backtest/engine/backtester.py` | `BacktestConfig`, `BacktestResult`, `run_on_candles`. Confirm it's still used by CLI `run`/`compare`. | |
| 2.12 | Forward paper engine (options bridge) | `src/backtest/forward/` | The options bridge that converts a strategy's directional view into multi-leg structures on a paper book. Confirm the class name and how it fetches the option chain (broker API call). This is the reference for how we'd log chain data. | |
| 2.13 | TimescaleDB `market_data_cache` | `src/backtest/db/` (models) | Hypertable of 1-min OHLCV bars. Confirm the model class, partitioning, and that NIFTY/BANKNIFTY index symbols are stored (e.g. symbol = `NIFTY`, `BANKNIFTY` or their NSE codes). | |
| 2.14 | Data source abstraction | `src/backtest/data/` | `SyntheticSource`, `CSVSource`, `MStockSource`, `DhanSource`, `DBSource`. Confirm the common interface: `get_candles(symbol, start, end, timeframe) -> pd.DataFrame`. | |
| 2.15 | API layer | `src/backtest/api/` | Existing backtest endpoints: `POST /api/backtest/run`, `POST /api/backtest/compare`. Confirm the request/response schema. The options backtest endpoint will follow the same pattern. | |
| 2.16 | Web UI – Backtest tab | `src/backtest/web/` (templates + JS) | The Backtest page: strategy dropdown, symbol picker, date range, params, Run button, results (equity chart, metrics cards, trade table). Confirm the template file and the JS that renders results. | |
| 2.17 | Playbook / runner config | `src/backtest/options/playbook.py` | `Playbook` dataclass, `to_expression()`, `to_runner_config()`. Confirm how a playbook maps to a runner. The options backtest may reuse playbook configs for structure definition. | |
| 2.18 | Risk / exit logic | `src/backtest/forward/execution_engine.py` | `EXIT_PRECEDENCE` (0 emergency > 1 stop > 2 target > 3 DTE > 4 flip). Confirm the exit engine is reusable in backtest mode or if it's forward-only. | |
| 2.19 | Test suite | `tests/` | Existing options tests: `tests/test_options_*.py`, `tests/test_playbooks_conditions.py`. Confirm test count and coverage of the Phase-A driver. | |
| 2.20 | Migrations | `src/backtest/db/` (alembic) | Latest migration number (README says 018). Confirm the migration directory and naming convention. New tables will need migration 019+. | |

---

## 3. Proposed Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        OPTIONS BACKTEST REQUEST                              │
│  strategy: "sma_crossover"  (or any equity strategy)                        │
│  underlying: "NIFTY"  (or "BANKNIFTY")                                      │
│  structure: "bull_call_spread"  (or long_call, long_put, bear_put_spread)   │
│  strike_selection: "atm"  (or delta_35, otm_1, itm_1)                       │
│  expiry_dte: 7  (days to expiry)                                            │
│  params: {fast: 20, slow: 50}                                               │
│  from_date, to_date, capital                                                │
│  model_params: {iv_source: "realized_20", skew_pts: 3, spread_pct: 0.005}   │
└──────────────────────────┬──────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  PHASE 1: SIGNAL GENERATION (reuse existing equity engine)                  │
│                                                                             │
│  1a. Fetch index candles:  DBSource.get_candles("NIFTY", from, to, "1min")  │
│  1b. Run strategy:   run_backtest(candles, strategy, params, ...)           │
│      → signal series: pd.Series of +1 / -1 / 0 per bar                     │
│      → OR: MarketView series (bullish/bearish/neutral)                      │
│  1c. Compute realized vol series:                                           │
│      rv = candles['close'].pct_change().rolling(20).std() * sqrt(252)       │
│      (window configurable: 10 / 20 / 50 bars)                               │
│                                                                             │
└──────────────────────────┬──────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  PHASE 2: OPTION STRUCTURE PRICING (synthetic BS, bar-by-bar)               │
│                                                                             │
│  For each bar t in the signal series:                                      │
│                                                                             │
│  2a. If signal changes (entry/exit/flip):                                   │
│      - Determine structure from signal + structure_type + strike_selection  │
│      - Select strikes: ATM = round(spot / strike_interval) * strike_interval│
│      - Select expiry: next valid expiry ≤ expiry_dte days away              │
│      - Price each leg: BS(spot_t, strike, iv_t, r, t_to_expiry)            │
│        where iv_t = rv_t + skew_adjustment(strike_moneyness)               │
│      - Apply bid-ask spread: buy at mid + spread/2, sell at mid - spread/2 │
│      - Book entry: premium paid + fees (per leg × lot_size × qty)          │
│                                                                             │
│  2b. If position is open (every bar):                                       │
│      - Reprice all legs at spot_t, iv_t                                    │
│      - Check exits (precedence order):                                      │
│        0. Emergency (engine-level)                                          │
│        1. Stop loss (on net premium or % of entry)                         │
│        2. Take profit (on net premium or % of entry)                       │
│        3. DTE expiry (auto square-off N bars before expiry)                │
│        4. Signal flip (close on opposite signal)                           │
│      - If exit fires: book exit premium + fees, record trade               │
│                                                                             │
│  2c. Track per-bar:                                                         │
│      - Net premium (mark-to-market)                                         │
│      - Greeks: Δ, Γ, Θ, V of the net structure                             │
│      - Equity curve: initial_capital + Σ(realised P&L) + unrealised P&L    │
│                                                                             │
└──────────────────────────┬──────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  PHASE 3: METRICS + ADAPTER (reuse existing)                                │
│                                                                             │
│  3a. compute_metrics(equity_curve, trades) → dict                           │
│      (Sharpe, max DD, win rate, profit factor, etc. – same as equity)      │
│  3b. Additional option metrics:                                             │
│      - Avg Δ at entry, Avg Θ decay per trade                                │
│      - % of P&L from theta vs. delta vs. vega (attribution)                │
│      - Max gamma exposure                                                   │
│  3c. BacktestAdapter.to_all() → JSON for UI                                │
│  3d. Add "disclaimer" field: "MODEL – synthetic Black-Scholes pricing"     │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Phase-by-Phase Implementation Plan

### Phase 0: Gap Review & Unfreeze (1 day)

**Owner:** Junior Developer (this review) + Senior sign-off

| Task | Detail |
|------|--------|
| 0.1 | Complete the verification table in §2. Flag every ❌ / ️. |
| 0.2 | Identify the exact file and function that implements the Phase-A driver. If it's in a separate branch or was deleted, note it. |
| 0.3 | Determine the exact mechanism of the freeze: is `CHAIN_SOURCES = ()` a constant, an env var, a DB flag? What's the minimal change to allow a `"model"` source? |
| 0.4 | Confirm whether the Phase-A driver's exit logic is shared with `execution_engine.py` or is a separate implementation. If separate, decide whether to unify or keep parallel. |
| 0.5 | Confirm the `instruments` table has NIFTY/BANKNIFTY option rows with correct lot_size and strike_interval. If not, plan a seed script. |
| 0.6 | **Deliverable:** A 1-page gap report: "Here's what exists, here's what's missing, here's what I need to build." |

---

### Phase 1: Model Pricing Layer (2–3 days)

**Goal:** A standalone, testable module that prices an option structure at a given (spot, iv, time_to_expiry) using BS, with skew and spread.

| Task | Detail |
|------|--------|
| 1.1 | **New file:** `src/backtest/options/model_pricing.py` (or extend existing `pricing.py`). |
| 1.2 | `class ModelPricingConfig`: `iv_source` (enum: `realized_10`, `realized_20`, `realized_50`, `constant`), `iv_constant` (float, default 0.15), `skew_pts` (float, default 3.0 – added to put IV vs. call IV at same moneyness), `spread_pct` (float, default 0.005 – 0.5% of premium), `risk_free_rate` (float, default 0.065 – current RBI repo rate), `lot_size` (int), `strike_interval` (int). |
| 1.3 | `def price_leg(spot, strike, iv, r, t, is_call) -> LegPrice`: returns `price`, `delta`, `gamma`, `theta`, `vega`. Use `Decimal` if the existing options layer does (verify in §2.4). |
| 1.4 | `def price_structure(legs: list[LegPrice], spot, iv, r, t) -> StructurePrice`: net premium, net Δ, net Γ, net Θ, net V. |
| 1.5 | `def apply_spread(price, spread_pct, side) -> float`: buy at `price * (1 + spread_pct/2)`, sell at `price * (1 - spread_pct/2)`. |
| 1.6 | `def compute_realized_vol(closes: pd.Series, window: int) -> pd.Series`: rolling std of log returns × √252. Handle NaN at the start of the series (forward-fill or skip). |
| 1.7 | **Tests:** `tests/test_model_pricing.py` – ≥ 15 tests: known BS prices (compare to a reference), skew application, spread application, realized vol computation, edge cases (t=0, iv=0, deep ITM/OTM). |

**Acceptance criteria:**
- `price_leg(100, 100, 0.20, 0.065, 7/365, True)` returns a value within 0.01 of the Black-Scholes textbook price.
- Skew: a put at the same strike gets `iv + skew_pts/100` vs. the call.
- Spread: a buy leg costs 0.25% more than mid; a sell leg receives 0.25% less.
- Realized vol: 20-bar window on a known series produces the expected std.

---

### Phase 2: Bar-by-Bar Backtest Loop (3–5 days)

**Goal:** The core engine that iterates over bars, generates signals, prices structures, applies exits, and produces an equity curve + trade log.

| Task | Detail |
|------|--------|
| 2.1 | **New file:** `src/backtest/options/model_backtest.py` (or unfreeze and extend the Phase-A driver). |
| 2.2 | `class OptionsBacktestConfig`: wraps `BacktestConfig` (capital, commission) + `ModelPricingConfig` + `structure_type` (enum: `long_call`, `long_put`, `bull_call_spread`, `bear_put_spread`) + `strike_selection` (enum: `atm`, `delta_35`, `otm_1`, `itm_1`) + `expiry_dte` (int, default 7) + `exit_config` (stop_loss_pct, take_profit_pct, auto_square_off_bars). |
| 2.3 | `class OptionsBacktestResult`: extends or wraps `BacktestResult` with: `equity` (pd.Series), `trades` (list of `OptionTrade`), `greeks_curve` (pd.DataFrame: Δ, Γ, Θ, V per bar), `disclaimer` (str = "MODEL – synthetic Black-Scholes pricing, not historical market premiums"). |
| 2.4 | `class OptionTrade`: `trade_id`, `entry_bar`, `exit_bar`, `structure_type`, `legs` (list of `LegTrade`), `entry_premium` (net, per lot), `exit_premium`, `realised_pnl` (after fees), `fees` (itemised), `greek_at_entry` (Δ, Γ, Θ, V), `exit_reason` (enum: `stop_loss`, `take_profit`, `dte_expiry`, `signal_flip`, `auto_square_off`). |
| 2.5 | `def run_options_backtest(candles, strategy_name, strategy_params, config) -> OptionsBacktestResult`: |
| | |
| | **Step 1 – Signal:** Call `run_backtest(candles, strategy_name, strategy_params, symbol, capital)` to get the signal series. OR, if the strategy produces `MarketView`, use that directly. **Junior: verify which strategies produce which signal type.** |
| | **Step 2 – Vol:** Compute `rv = compute_realized_vol(candles['close'], window)`. |
| | **Step 3 – Loop:** For each bar `t`: |
| | | a. `spot = candles['close'].iloc[t]` |
| | | b. `iv = rv.iloc[t]` (or `config.iv_constant`) |
| | | c. `t_to_expiry = (expiry_date - bar_date).days / 365` |
| | | d. If no open position AND signal indicates entry: |
| | | | - Select strikes per `strike_selection` |
| | | | - Build structure (legs) |
| | | | - Price each leg with `price_leg` + `apply_spread` |
| | | | - Book entry, record `OptionTrade` (entry) |
| | | e. If open position: |
| | | | - Reprice all legs |
| | | | - Check exits in `EXIT_PRECEDENCE` order (reuse from `execution_engine.py` if possible – **Junior: verify if it's importable or needs to be extracted**) |
| | | | - If exit fires: book exit, close `OptionTrade` |
| | | f. Record bar: net premium, Greeks, equity |
| | **Step 4 – Final bar:** If position still open, mark-to-market at final close, record as open trade (exclude from win rate, same rule as equity backtest). |
| | **Step 5 – Metrics:** Call `compute_metrics` on the equity curve. Add option-specific metrics. |
| | **Step 6 – Adapter:** Return `OptionsBacktestResult`. |
| 2.6 | **Strike selection logic:** |
| | - `atm`: `strike = round(spot / strike_interval) * strike_interval` |
| | - `delta_35`: find the strike where BS delta ≈ 0.35 (binary search over the strike grid) |
| | - `otm_1`: `atm_strike + strike_interval` (for calls) / `atm_strike - strike_interval` (for puts) |
| | - `itm_1`: `atm_strike - strike_interval` (for calls) / `atm_strike + strike_interval` (for puts) |
| | - For spreads: `bull_call_spread` = long ATM call + short (ATM + 1×interval) call. `bear_put_spread` = long ATM put + short (ATM - 1×interval) put. |
| 2.7 | **Expiry handling:** If `t_to_expiry < auto_square_off_bars / bars_per_day`, force close (auto square-off). At `t_to_expiry = 0`, settle: intrinsic value = max(0, spot - strike) for calls, max(0, strike - spot) for puts. |
| 2.8 | **Fees:** Apply the existing fee stack from `src/backtest/simulator/` per leg per trade. **Junior: verify the fee function signature – does it take (premium, lot_size, side, is_intraday) or something else?** |
| 2.9 | **Tests:** `tests/test_model_backtest.py` – ≥ 20 tests: |
| | - Flat market (no signal) → 0 trades, flat equity |
| | - Single bullish signal, long call → 1 trade, P&L ≈ (exit_premium - entry_premium) × lot_size - fees |
| | - Stop loss fires → exit_reason = `stop_loss`, P&L bounded |
| | - DTE expiry → auto square-off, exit_reason = `dte_expiry` |
| | - Signal flip → close on opposite signal |
| | - Bull call spread → 2 legs, net premium = call1 - call2 |
| | - Determinism: two identical runs produce byte-identical trade logs (same as existing Phase-A test) |
| | - Realized vol affects P&L: same signal, different vol window → different P&L |
| | - Skew: put structure P&L differs from call structure in the same market |
| | - Edge: bar 0 (no prior data for vol) → skip or use constant IV |
| | - Edge: spot exactly on a strike boundary |
| | - Edge: expiry falls on a non-trading day (holiday) → shift to next trading day |

**Acceptance criteria:**
- A known scenario (e.g. NIFTY goes from 22000 → 22500 over 5 bars, long ATM call, 7 DTE) produces a P&L that matches a hand-calculated BS price within ₹50/lot.
- The equity curve is monotonically consistent: Σ trade P&L + unrealised P&L == final equity - initial capital.
- All outputs carry the disclaimer string.

---

### Phase 3: API + UI (2–3 days)

**Goal:** Expose the options backtest through the existing REST API and Web UI.

| Task | Detail |
|------|--------|
| 3.1 | **New endpoint:** `POST /api/backtest/run_options` (or extend existing `POST /api/backtest/run` with a `"mode": "options"` field – **Junior: check which is cleaner given the existing request schema**). |
| | Request body: |
| | ```json |
| | { |
| |   "strategy": "sma_crossover", |
| |   "underlying": "NIFTY", |
| |   "structure": "bull_call_spread", |
| |   "strike_selection": "atm", |
| |   "expiry_dte": 7, |
| |   "params": {"fast": 20, "slow": 50}, |
| |   "from_date": "2024-01-01", |
| |   "to_date": "2024-12-31", |
| |   "capital": 100000, |
| |   "model": { |
| |     "iv_source": "realized_20", |
| |     "skew_pts": 3, |
| |     "spread_pct": 0.005, |
| |     "risk_free_rate": 0.065 |
| |   }, |
| |   "exits": { |
| |     "stop_loss_pct": 0.30, |
| |     "take_profit_pct": 0.50, |
| |     "auto_square_off_bars": 12 |
| |   } |
| | } |
| | ``` |
| | Response: same shape as existing backtest response + `greeks_curve`, `disclaimer`, `model_params_echo`. |
| 3.2 | **UI – Backtest tab:** Add a "Mode" toggle: **Equity** | **Options (Model)**. When "Options" is selected: |
| | - Underlying dropdown: NIFTY, BANKNIFTY (from index list in DB) |
| | - Structure dropdown: Long Call, Long Put, Bull Call Spread, Bear Put Spread |
| | - Strike selection dropdown: ATM, Δ35, OTM 1, ITM 1 |
| | - Expiry DTE: number input (default 7) |
| | - Model params section (collapsible): IV source, skew, spread, RF rate |
| | - Exit params: SL %, TP %, auto square-off bars |
| | - **Banner:** "⚠️ MODEL BACKTEST – Synthetic Black-Scholes pricing. Not a data-driven backtest. Results are indicative only." |
| 3.3 | **Results rendering:** Reuse existing equity chart, metrics cards, trade table. Add: |
| | - Greeks curve (Δ, Θ) as a second chart panel |
| | - Trade table columns: entry date, exit date, structure, strikes, entry premium, exit premium, P&L, fees, Δ at entry, exit reason |
| | - Disclaimer text below the metrics cards |
| 3.4 | **Compare mode:** Allow comparing an equity backtest vs. an options backtest on the same signal + underlying. Side-by-side equity curves. |
| 3.5 | **Tests:** `tests/test_api_options_backtest.py` – ≥ 8 tests: valid request → 200, invalid structure → 400, missing underlying → 400, NIFTY not in DB → 404 with message, response contains disclaimer, response contains greeks_curve. |

---

### Phase 4: Chain Data Logging (Forward-Looking, 1 day)

**Goal:** Start building a real historical option chain dataset so that in 3–6 months we can replace synthetic BS with actual premiums.

| Task | Detail |
|------|--------|
| 4.1 | **New migration:** `019_option_chain_history.py` – create hypertable: |
| | ```sql |
| | CREATE TABLE option_chain_history ( |
| |   time        TIMESTAMPTZ NOT NULL, |
| |   underlying  VARCHAR(20) NOT NULL,   -- 'NIFTY', 'BANKNIFTY' |
| |   strike      NUMERIC(10,2) NOT NULL, |
| |   expiry      DATE NOT NULL, |
| |   side        VARCHAR(4) NOT NULL,    -- 'CE', 'PE' |
| |   ltp         NUMERIC(12,2), |
| |   iv          NUMERIC(8,4), |
| |   oi          BIGINT, |
| |   volume      BIGINT, |
| |   bid         NUMERIC(12,2), |
| |   ask         NUMERIC(12,2), |
| |   PRIMARY KEY (time, underlying, strike, expiry, side) |
| | ); |
| | SELECT create_hypertable('option_chain_history', 'time', if_not_exists => TRUE); |
| | CREATE INDEX idx_chain_lookup ON option_chain_history (underlying, expiry, time DESC); |
| | ``` |
| 4.2 | **Hook in forward engine:** In the options bridge (`src/backtest/forward/`), after fetching the option chain from the broker, call a new `ChainHistoryLogger.log(chain_snapshot)`. This is a fire-and-forget async write (batch every N bars or every 5 min to avoid write amplification). |
| 4.3 | **New file:** `src/backtest/data/chain_history.py` – `ChainHistoryLogger` class: buffers snapshots, flushes to DB in batches of 100 or every 60s. Handles broker disconnect gracefully (log warning, drop batch). |
| 4.4 | **Query API:** `GET /api/data/chain-history?underlying=NIFTY&expiry=2026-10-15&from=...&to=...` – returns stored snapshots. For future use in data-driven backtest. |
| 4.5 | **Tests:** `tests/test_chain_history.py` – ≥ 5 tests: insert + query round-trip, batch flush, hypertable partitioning, graceful failure on DB error. |

**Note:** This phase does NOT enable a data-driven backtest. It starts the data collection. The data-driven backtest is a **future project** (Phase 5, not in this plan).

---

### Phase 5 (Future, Out of Scope for This Plan): Data-Driven Options Backtest

Once 3–6 months of `option_chain_history` data is accumulated:
- Replace `ModelPricingConfig` with `DataPricingConfig` that reads actual LTP/IV from the chain history table.
- Use actual bid/ask for fill simulation.
- Use actual OI for liquidity filtering.
- Re-run the same strategies and compare model vs. data results.
- Calibrate skew/spread assumptions.

This is a **separate project** with its own PRD.

---

## 5. Data Model Changes

| Change | Type | Migration # |
|--------|------|-------------|
| `option_chain_history` hypertable | New table | 019 |
| `instruments` table: verify NIFTY/BANKNIFTY option rows exist with correct lot_size, strike_interval | Data seed (if missing) | 020 (or seed script) |
| `backtest_runs` table (migration 018): add `backtest_type` column (`'equity'`, `'options_model'`) | Alter table | 021 |
| `backtest_runs` table: add `model_params` JSONB column (iv_source, skew, spread, etc.) | Alter table | 021 (same migration) |

---

## 6. New / Modified Files Summary

| File | Action | Phase |
|------|--------|-------|
| `src/backtest/options/model_pricing.py` | **New** | 1 |
| `src/backtest/options/model_backtest.py` | **New** (or extend Phase-A driver) | 2 |
| `src/backtest/data/sources_policy.py` | **Modify** – add `"model"` to `CHAIN_SOURCES` or add `option_model_backtest_allowed()` | 0 |
| `src/backtest/data/chain_history.py` | **New** | 4 |
| `src/backtest/forward/options_bridge.py` (or equivalent) | **Modify** – add `ChainHistoryLogger` hook | 4 |
| `src/backtest/api/backtest.py` | **Modify** – add `run_options` endpoint | 3 |
| `src/backtest/web/templates/backtest.html` (or equivalent) | **Modify** – add options mode UI | 3 |
| `src/backtest/web/static/js/backtest.js` (or equivalent) | **Modify** – render options results, Greeks chart | 3 |
| `src/backtest/db/models.py` | **Modify** – add `OptionChainHistory` model | 4 |
| `src/backtest/db/migrations/019_option_chain_history.py` | **New** | 4 |
| `src/backtest/db/migrations/021_backtest_type.py` | **New** | 3 |
| `tests/test_model_pricing.py` | **New** | 1 |
| `tests/test_model_backtest.py` | **New** | 2 |
| `tests/test_api_options_backtest.py` | **New** | 3 |
| `tests/test_chain_history.py` | **New** | 4 |
| `docs/OPTIONS-MODEL-BACKTEST.md` | **New** – design doc, usage guide, disclaimer policy | 3 |
| `docs/USER-GUIDE.md` | **Modify** – add options backtest section | 3 |

---

## 7. Gap Analysis Checklist (For Junior)

> **Instructions:** Work through every item. For each, write:
> - **Status:** ✅ (exists, works as described) / ❌ (missing) / ⚠️ (exists but differs)
> - **Evidence:** file path + line number or symbol name
> - **Gap / Risk:** what's missing or different, and what it means for the plan
> - **Suggested fix:** if you can see a quick fix, note it

### A. Options Expression Layer

- [ ] A1.1: Does `MarketView` exist? What's its interface? (bullish/bearish/neutral? confidence score?)
- [ ] A1.2: Does `StrikeSelector` exist? Does it support `atm`, `delta_35`, `otm_1`, `itm_1`?
- [ ] A1.3: Does `StructureBuilder` support all 4 structures (long call, long put, bull call spread, bear put spread)?
- [ ] A1.4: Does `TradeIntent` carry enough info for the backtest loop (legs, side, qty, lot_size)?
- [ ] A1.5: Is the expression layer **importable and callable** without a broker session? (i.e., can we use it in a backtest context, not just paper/live?)

### B. Phase-A Driver

- [ ] B1.1: Where exactly is the Phase-A driver? (file, class, function)
- [ ] B1.2: Does it iterate bar-by-bar? Does it call `set_reference(bar_time)`?
- [ ] B1.3: Does it support all 4 structures?
- [ ] B1.4: Does it apply exits (SL, TP, DTE, flip)?
- [ ] B1.5: Does it produce a trade log with `exit_reason` labels?
- [ ] B1.6: Is it deterministic (byte-identical across runs)?
- [ ] B1.7: Does it use `Decimal` or `float`? (Must match the rest of the options layer.)
- [ ] B1.8: Is it currently importable, or is it behind the freeze gate?

### C. Freeze Gate

- [ ] C1.1: Exact mechanism of `CHAIN_SOURCES = ()` – constant, env var, DB flag?
- [ ] C1.2: What does `option_backtest_allowed()` check?
- [ ] C1.3: What's the minimal change to allow a `"model"` source without breaking the paper/live path?
- [ ] C1.4: Are there any other gates (feature flags, config files, DB rows) that block options backtest?

### D. Pricing & Fees

- [ ] D1.1: Where is the BS pricing function? What's its exact signature?
- [ ] D1.2: Does it return Greeks (Δ, Γ, Θ, V)?
- [ ] D1.3: Does it use `Decimal`?
- [ ] D1.4: Where is the Indian fee stack? What's its signature? Does it handle per-leg or per-structure?
- [ ] D1.5: Does the fee stack include all 6 components (STT, exchange, SEBI, stamp, GST, brokerage)?
- [ ] D1.6: Is there a bid-ask spread model anywhere? (Probably not – we'll build one.)

### E. Instruments & Expiry

- [ ] E1.1: Does the `instruments` table have NIFTY option rows? What's the lot_size? strike_interval?
- [ ] E1.2: Same for BANKNIFTY?
- [ ] E1.3: Is there an expiry calendar function? Where? Does it handle weekly (Thursday) and monthly expiries?
- [ ] E1.4: Does it handle the NSE lot-size revision (lot sizes change periodically)?
- [ ] E1.5: Are NIFTY/BANKNIFTY index symbols in `market_data_cache`? What's the exact symbol string? (`NIFTY`? `NIFTY 50`? `NSE:NIFTY`?)

### F. Signal Generation

- [ ] F1.1: List all strategies in `src/backtest/strategies/` and `plugins/strategies/`. Which produce `+1/-1/0`? Which produce `MarketView`?
- [ ] F1.2: Does `run_backtest()` (the canonical runner) return the signal series, or only the equity curve? (We need the signal series for the options loop.)
- [ ] F1.3: If it only returns equity, can we extract the signal series? Or do we need to call `strategy.generate_signals()` separately?
- [ ] F1.4: Are there any strategies that are **unsuitable** as option signal sources? (e.g., mean-reversion strategies that flip too frequently for 7-DTE options.)

### G. Exit Logic

- [ ] G1.1: Is `EXIT_PRECEDENCE` in `execution_engine.py` importable from the backtest context?
- [ ] G1.2: Does it depend on a live broker session? (If yes, we need to extract the pure logic.)
- [ ] G1.3: Does the Phase-A driver have its own exit logic, or does it reuse `EXIT_PRECEDENCE`?
- [ ] G1.4: How is "stop loss" defined for options? % of entry premium? Absolute ₹? Net structure premium?

### H. Data & DB

- [ ] H1.1: Does `DBSource.get_candles("NIFTY", ...)` work? What symbol string does it expect?
- [ ] H1.2: Is 1-min data available for NIFTY/BANKNIFTY in the DB? (Check row count.)
- [ ] H1.3: Is there a `market_data_cache` row for NIFTY with 1-min bars going back to at least 2024?
- [ ] H1.4: What's the latest migration number? (Should be 018 per README.)
- [ ] H1.5: Is there an existing `option_chain_history` table or model? (Should not be – we're creating it.)

### I. API & UI

- [ ] I1.1: What's the exact request schema for `POST /api/backtest/run`? (JSON fields, types, defaults.)
- [ ] I1.2: What's the exact response schema? (Fields, nested structure.)
- [ ] I1.3: Where is the Backtest tab template? (file path)
- [ ] I1.4: Where is the JS that renders backtest results? (file path)
- [ ] I1.5: Is there an existing "mode" or "type" selector in the UI? (We'll add Equity | Options.)
- [ ] I1.6: How does the UI handle the "disclaimer" / warning banner? Is there an existing pattern?

### J. Testing

- [ ] J1.1: How many tests currently cover the options layer? (`pytest -k options --collect-only | wc -l`)
- [ ] J1.2: Are there existing tests for the Phase-A driver? Where?
- [ ] J1.3: Is there a test fixture for NIFTY/BANKNIFTY candles? (We'll need one for the model backtest tests.)
- [ ] J1.4: Does the test suite run with `--source synthetic`? Can we use synthetic candles for the options backtest tests?

### K. Multi-Broker / Forward Engine

- [ ] K1.1: Where exactly does the forward engine fetch the option chain? (file, function, line)
- [ ] K1.2: What's the chain data structure? (list of dicts? DataFrame? custom class?)
- [ ] K1.3: Does it fetch all strikes for all expiries, or just the relevant ones?
- [ ] K1.4: Is the chain fetch synchronous or async?
- [ ] K1.5: Can we add a `ChainHistoryLogger` hook without disrupting the forward engine's timing?

---

## 8. Risks & Mitigations

| # | Risk | Likelihood | Impact | Mitigation |
|---|------|-----------|--------|------------|
| R1 | Phase-A driver is broken / incomplete / was deleted in a refactor | Medium | High | Phase 0 gap review catches this. Worst case: rebuild the bar-by-bar loop from scratch using the expression layer + pricing. ~3 extra days. |
| R2 | `run_backtest()` doesn't expose the signal series, only equity | Medium | Medium | Call `strategy.generate_signals(candles)` directly in the options backtest. The strategy registry is accessible. |
| R3 | NIFTY/BANKNIFTY 1-min data in DB is incomplete or missing for the requested date range | Medium | Medium | Validate data coverage before running. Return a clear error: "No 1-min data for NIFTY from 2024-01-01 to 2024-12-31. Use the Data tab to fetch." |
| R4 | BS pricing with realized vol produces unrealistic P&L in low-vol regimes (IV crush not captured) | High | Medium | This is inherent to the model. The disclaimer covers it. Add a "vol regime" indicator to the results (e.g. "Avg IV during backtest: 12.3% – low vol regime, theta effects amplified"). |
| R5 | Strike selection at `delta_35` requires a binary search that's slow for large backtests | Low | Low | Pre-compute a strike grid per bar (NIFTY has ~40 strikes within ±10% of spot). Binary search over 40 items is O(log 40) ≈ 6 comparisons. Negligible. |
| R6 | `Decimal` vs `float` mismatch between the new model pricing and the existing options layer | Medium | Medium | **Junior: verify in §2.4.** If the existing layer uses `Decimal`, the new `model_pricing.py` must too. If it uses `float`, use `float` with explicit rounding at the boundary. |
| R7 | The forward engine's chain fetch is tightly coupled to the broker session; adding a logger hook breaks paper trading | Low | High | Make `ChainHistoryLogger` a separate, optional component. Wire it via a callback or event, not by modifying the fetch function. Test with paper trading after the change. |
| R8 | Junior misidentifies a gap and the plan proceeds on a wrong assumption | Medium | High | Phase 0 is a **hard gate**. No Phase 1 work starts until the gap report is reviewed and signed off by the senior. |

---

## 9. Definition of Done

The project is complete when:

- [ ] **D1:** `POST /api/backtest/run_options` returns a 200 with a valid equity curve, metrics, trade table, and Greeks for a NIFTY bull call spread backtest over 6 months of 1-min data.
- [ ] **D2:** The UI Backtest tab shows the "Options (Model)" mode with all parameters, runs the backtest, and renders the results including the disclaimer banner.
- [ ] **D3:** All new tests pass. Full suite (`pytest ../tests/ -q`) passes with no regressions.
- [ ] **D4:** The Phase-A driver (if reused) or the new `model_backtest.py` is deterministic: two identical runs produce byte-identical trade logs.
- [ ] **D5:** `option_chain_history` table exists, the forward engine logs chain snapshots, and a query API returns them.
- [ ] **D6:** `docs/OPTIONS-MODEL-BACKTEST.md` is written: design, usage, disclaimer policy, limitations, and the path to Phase 5 (data-driven).
- [ ] **D7:** `docs/USER-GUIDE.md` is updated with the options backtest workflow.
- [ ] **D8:** The gap report from Phase 0 is attached to this document, all ❌ items are resolved, and all ⚠️ items have a documented decision.
- [ ] **D9:** No existing paper/live/forward functionality is broken. Run the existing options paper test suite + forward paper smoke test after all changes.

---

## 10. Timeline Estimate

| Phase | Duration | Depends On |
|-------|----------|------------|
| Phase 0: Gap Review & Unfreeze | 1 day | – |
| Phase 1: Model Pricing Layer | 2–3 days | Phase 0 |
| Phase 2: Bar-by-Bar Backtest Loop | 3–5 days | Phase 1 |
| Phase 3: API + UI | 2–3 days | Phase 2 |
| Phase 4: Chain Data Logging | 1 day | Phase 0 (independent of 1–3) |
| **Total** | **9–13 days** (one developer) | |

Phases 1 and 4 can be done in parallel by two developers.

---

## 11. Explicitly Out of Scope

- **Data-driven options backtest** (using actual historical premiums) – Phase 5, future project.
- **Options structures beyond the 4 Phase-A types** (straddle, strangle, iron condor, butterfly, etc.) – requires the "chain-shape refactor" mentioned in the README (Phase B of the options backtest PRD).
- **Intraday (1-min) option backtest with tick-level fills** – the model prices at bar close (or bar open, matching the equity backtest convention). Tick-level is not feasible without tick data.
- **Multi-expiry / rolling structures** (e.g. roll a 7-DTE call to a 14-DTE call when DTE < 2) – future enhancement.
- **Margin / leverage modelling** – the model assumes full premium payment (no margin). Live options use margin; the P&L per rupee of capital will differ.
- **Auto-hedging** (delta-hedge the position by trading the underlying) – out of scope.

---

## Appendix A: Hand-Calculation Validation Scenario

Use this to verify the implementation in Phase 2:

> **Setup:** NIFTY spot = 22,000. ATM strike = 22,000. IV = 20%. RF = 6.5%. T = 7/365 = 0.01918 years. Lot size = 25.
>
> **Structure:** Long 1 lot ATM call.
>
> **BS call price:** C = S·N(d₁) − K·e^(−rT)·N(d₂)
> - d₁ = [ln(22000/22000) + (0.065 + 0.04)·0.01918] / (0.20·√0.01918) = [0 + 0.002004] / 0.02766 = 0.07245
> - d₂ = d₁ − 0.02766 = 0.04479
> - N(d₁) ≈ 0.5289, N(d₂) ≈ 0.5179
> - C = 22000·0.5289 − 22000·e^(−0.001247)·0.5179 = 11635.8 − 21972.6·0.5179 = 11635.8 − 11379.4 = **₹256.4**
>
> **Spread (0.5%):** Buy at 256.4 × 1.0025 = **₹257.0**
>
> **Entry cost:** 257.0 × 25 = **₹6,425**
>
> **5 bars later:** NIFTY = 22,500. T = 2/365 = 0.00548. IV unchanged (20%).
> - d₁ = [ln(22500/22000) + (0.065 + 0.04)·0.00548] / (0.20·√0.00548) = [0.02237 + 0.000574] / 0.01481 = 1.550
> - d₂ = 1.550 − 0.01481 = 1.535
> - N(d₁) ≈ 0.9394, N(d₂) ≈ 0.9378
> - C = 22500·0.9394 − 22000·e^(−0.000356)·0.9378 = 21136.5 − 20992.2·0.9378 = 21136.5 − 19686.5 = **₹1,450.0**
>
> **Spread (0.5%):** Sell at 1450.0 × 0.9975 = **₹1,446.4**
>
> **Exit proceeds:** 1446.4 × 25 = **₹36,160**
>
> **Gross P&L:** 36,160 − 6,425 = **₹29,735**
>
> **Fees (approx, per leg):** STT (0.0625% of premium × 25) + exchange (₹10) + SEBI (₹10) + stamp (0.003% × 25) + GST (18% of above) + brokerage (₹20) ≈ **~₹350**
>
> **Net P&L:** ≈ **₹29,385**
>
> **The test should assert net P&L is within ₹500 of this value** (allowing for floating-point / Decimal rounding differences and the exact fee formula).

---

## Appendix B: Disclaimer Text (Use Everywhere)

> **⚠️ MODEL BACKTEST**
> This backtest uses **synthetic Black-Scholes pricing** with realized volatility derived from index candles. It does **not** use historical option premiums, actual bid-ask spreads, or real order book data. Results are **indicative only** and should not be used as a basis for live trading decisions. The actual P&L of an option strategy depends on IV changes, liquidity, bid-ask spreads, and execution quality, none of which are captured by this model.

---

*End of document. Junior: please complete §2 (verification table) and §7 (gap checklist) and return with your findings before any implementation work begins.*