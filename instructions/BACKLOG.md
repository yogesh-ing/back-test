# Backtest — Backlog

Enhancement options discussed after the initial pluggable backtester (Option A)
was built and verified. Tracked here so we don't lose them.

> Big picture: see [`ROADMAP.md`](./ROADMAP.md) for the phased plan toward a
> complete trading system (backtest → forward/paper test → live).

Legend: **status** = `todo` | `in-progress` | `done`

---

## Option A — Strategy comparison command
- **Status:** `done` (2026-08-16)
- **What:** A `compare` CLI command that runs **multiple** strategies (N ≥ 2)
  over the same symbol/date range/costs and prints a ranked table.
- **Delivered:**
  - `python -m backtest compare --strategies a,b,c ...` (validates ≥2 names).
  - Candles fetched **once** and reused for all strategies (`runner.compare_strategies`
    + `run_on_candles`); per-strategy config copy so declared stops don't leak.
  - Ranked console table; `--sort-by sharpe|cagr|total_return|max_drawdown|calmar|win_rate|volatility|num_trades`
    (default Sharpe; volatility ranks low→high).
  - `--csv out.csv` export; `--json` output.
  - Overlaid equity+drawdown chart to
    `charts/compare_<symbol>_<timeframe>_<epoch>.png` (`plotting.plot_comparison`),
    with `--plot/--chart-dir/--no-chart/--show`.
  - Buy-and-hold is just another strategy you can list (no special benchmark flag).
  - Tests: all strategies run, identical bar windows, no stop-leak across strategies.

---

## Exit criteria — stop-loss / take-profit (engine-level)
- **Status:** `done` (2026-08-16)
- **What:** Give every strategy realistic price-based exits without per-strategy
  code, via `--stop-loss 0.02 --take-profit 0.05` (fractions of entry price),
  or by declaring `stop_loss` / `take_profit` on the strategy class.
- **Delivered:**
  - `BacktestConfig.stop_loss` / `take_profit`; engine runs a per-bar risk loop
    when either is set (pure-vectorized fast path preserved otherwise).
  - Intrabar checks against each bar's `low`/`high`; conservative same-bar rule
    (stop-loss assumed first); re-entry blocked until the signal goes flat.
  - Works for long and short positions.
  - Runner merges CLI override > strategy-declared value; metrics carry the
    effective `stop_loss`/`take_profit`; CLI prints a `Risk` line.
  - Tests: stop caps a losing trade at exactly -SL; take-profit caps a winner
    at +TP; no-risk path equals the vectorized engine.
- **Later:** trailing stop, time-based exit (max bars in trade).

---

## Strategy authoring model — revisit how a strategy is written
- **Status:** `done` (decided + delivered 2026-08-16)
- **Decision:** **M3 — entries/exits split + engine-enforced risk params**,
  layered on top of the existing signal model (fully backward compatible).
- **Delivered:**
  - `Strategy` now supports two authoring styles:
    - override `generate_signals(candles)` (target position {-1,0,1}) — unchanged, or
    - override `entries(candles)` (+ optional `exits(candles)`) boolean Series;
      the base builds the target positions for you.
  - Class-level `stop_loss` / `take_profit` declarations, enforced by the engine.
  - Example: `strategies/donchian_breakout.py` (entries/exits + declared stops).
  - Existing `generate_signals` strategies (sma_crossover, rsi_reversion,
    buy_and_hold) keep working untouched.
- **Follow-ups (future):** short entries in the entries/exits model, position
  sizing / scaling, trailing stop, time-based exits.

---

## Option B — Equity-curve plotting
- **Status:** `done`
- **What:** Plot the equity curve (and drawdown + price-with-position) for a
  run and save to PNG (optionally show interactively).
- **Why:** Visual inspection of performance and drawdowns is far faster than
  reading numbers.
- **Delivered:**
  - Added `matplotlib` dependency.
  - `engine/plotting.py` with `plot_result(result, path=None, show=False)` —
    3 panels: equity curve, drawdown %, close price shaded by in-market position.
  - CLI: `--plot [PATH]` (default name if no path) and `--show` on `run`.
  - Test `tests/test_plotting.py` (saves a PNG, asserts 3 panels).
  - Usage: `python -m backtest run --strategy rsi_reversion --plot chart.png`

---

## Option C — Live mStock wiring test
- **Status:** `in-progress` (blocked on network egress; auth flow now ready)
- **What:** Do a real authenticated pull from mStock and confirm the historical
  candle payload shape, then lock `_candles_to_frame` to the real format.
- **Why:** This is the `PRD.md` `DEC-001` capability check — the one unknown
  before mStock data is trustworthy.
- **Prerequisite check done (2026-08-16):**
  - ✅ Deps (incl. `pyarrow` now installed), `.env` creds load, both auth flows ready.
  - ✅ Fixed client vs reference-SDK discrepancies: capitalized `Username`/`Password`,
    omit `Authorization` header until a token exists, added `verify_totp` (TOTP)
    alongside `generate_session` (OTP), plain-date history params.
  - ✅ Added `python -m backtest preflight` (non-destructive prerequisite check).
  - ❌ **BLOCKER:** DNS for `api.mstock.trade` does not resolve on this machine
    (`getaddrinfo failed`) — needs VPN/proxy/corporate-network egress.
  - ❓ Confirm account auth method: SMS **OTP** (default) vs authenticator **TOTP**
    (`MSTOCK_AUTH_MODE=totp`).
- **Next (once network is up):**
  - User runs `--source mstock --symbol RELIANCE ...`, completes OTP/TOTP.
  - Capture raw historical JSON + a `scriptmaster` sample; verify symbol→token
    columns and adjust `_candles_to_frame` in one place if needed.
  - Add a recorded-fixture test so we don't need live access to re-verify.
- **Touches:** `data/mstock_source.py::_candles_to_frame`, possibly
  `data/mstock_client.py`, plus a new fixture-based test.
- **Blocked by:** network egress to `api.mstock.trade`; interactive OTP/TOTP.

---

## Trailing stop & time-based exit  `todo` (Phase 1)
- Extend the engine risk loop with a **trailing stop** (ratchets with favorable
  price) and a **max-bars-in-trade** exit. Config + strategy-declared, like SL/TP.

## Trade log & richer metrics  `todo` (Phase 1)
- Emit **per-trade records** (entry/exit time & price, bars held, PnL,
  R-multiple); export CSV/JSON. Add Sortino, expectancy, profit factor,
  avg win/loss, monthly-returns heatmap.

## Position sizing & fill models  `todo` (Phase 1)
- Sizing: fixed fraction / fixed cash / volatility(ATR) target.
- Fills: next-open vs close; pluggable slippage model.

## Short entries in entries/exits model  `todo` (Phase 1)
- Add `short_entries` / `short_exits` so the entries/exits model can go short
  (engine already accepts `-1`).

## SimulatedBroker + offline walk-forward  `done` (Phase 3a) — foundation ✅
- **Delivered (2026-08-17):**
  - `forward/portfolio.py` — `Portfolio` with **per-strategy capital allocation**,
    isolated `Account`s (cash/equity/position/entry), snapshot + load.
  - `forward/broker.py` — `SimulatedBroker.step` mirrors the engine's per-bar math
    (costs + intrabar SL/TP, stop-first, re-entry block); `LiveBroker` seam noted.
  - `forward/paper.py` — event-driven `replay` / `run_walkforward`, plus
    `save_state`/`load_state`.
  - CLI `papertrade --mode walkforward --alloc name=amount ...` with a ranked
    per-strategy + total report and `--state-file`.
  - Tests (`test_forward.py`): walk-forward equity **reconciles exactly** with the
    vectorized backtest (with and without stops); per-strategy capital isolation;
    snapshot round-trip. **23/23 suite green.**

## Live paper trading  `done` (Phase 3b) — synthetic data working ✅
- Remaining: real-time **scheduler** loop + polling feed (`--mode live`),
  periodic state persistence, daily PnL/alerts. Foundation (broker/portfolio/
  state) is done above; needs mStock connectivity.

## Parameter optimization / walk-forward  `todo` (Phase 2)
- Grid/random search + rolling in-sample/out-of-sample walk-forward.

## Multi-symbol / portfolio backtests  `todo` (Phase 2)
- Test a basket with capital allocation across symbols/strategies.

## Options/F&O engine (PRD V1.0)  `later` (Phase 6)
- The naked-risk options backtester — separate, large; **blocked on `DEC-001`**
  (historical per-contract option data feasibility).

---

## Portfolio Command Center — Live/Paper separation  `done` (2026-09-02) ✅
- **Delivered:**
  - Per-bucket state: equity, peak, drawdown, daily P&L — all derived from runners (C2)
  - Independent circuit breakers — paper breach does NOT halt live (AC-6/AC-7)
  - Scoped bulk control + emergency — pause/resume/emergency scoped to target bucket (C5)
  - Master kill — emergency flatten both buckets at once
  - Capability flag — REAL MONEY banner driven by broker connection status (C6)
  - SSE carries embedded bucket data — single stream, frontend filters (C4)
  - Three views: Overview (combined), Live (scoped), Paper (sandbox)
  - Back navigation on scoped pages
  - Live/Paper accent colors (red/orange vs blue/neutral)
  - All 13 acceptance criteria met
- **Commits:** `e57a4ed`, `9ca7025`, `15713e9`, `a8b144d`
- **Tests:** 116 portfolio tests (23 bucket state + 11 flow semantics + 27 breaker + 25 API + 30 UI)
- **Design doc:** `REFACTOR-PORTFOLIO-LIVE-PAPER-SEPARATION.md`

---

## Architect follow-up items (from review of portfolio separation)

These items were identified by the architect during code review. The core separation is done;
these are improvements to harden and extend it.

### C2/C3: Derived-not-duplicated accounting  `done` ✅
All per-bucket metrics are derived from runner states, not stored separately.

### C6: Capability-driven banner  `done` ✅
REAL MONEY / Simulated fills banner driven by broker connection status.

### AC-8: Banner rendering test  `todo`
- Add a test that verifies the capability banner renders correctly based on broker state.

### AC-14: Zero phantom P&L on spawn/despawn  `done` ✅
Spawning or despawning a runner creates zero phantom bucket P&L or drawdown.

### AC-15: Single SSE stream, consistent numbers  `done` ✅
All pages consume one SSE stream; Live-page numbers === Overview's live-card numbers.

### AC-16: Audit log with scope  `done` ✅
Every control action is logged server-side with scope; API responses include scope field.

### AC-17: Master kill halts both buckets  `done` ✅

### AC-18: Restart behavior documented and tested  `done` ✅
Restart behavior of halt state is documented and tested (resets, with warning logged).

### Phase 0 greps: What does `mode=live` actually do?  `todo`
- Document the full flow of mode=live from API → manager → runners → SSE.
- Verify no Scenario B issues (which the architect asked about).

### Breaker independence: latency <500ms within bucket  `done` ✅
Verified by latency tests — 20-runner evaluation completes in <500ms.

### Audit scope fields  `todo`
- Audit log entries should include a filterable `scope` field for audit views.
- Currently scope is in API responses; frontend audit log filtering is not yet implemented.

### Restart behavior documentation  `todo`
- §6 says "No DB changes" but a restart resurrects a halted bucket and resets all breakers.
- Today that's zero stakes (all paper); document it, tie it to V2 #3, and at minimum log halt events.
- Halt events are now logged; formal documentation still needed.

> See [`ROADMAP.md`](./ROADMAP.md) for the full phased plan and dependency map.