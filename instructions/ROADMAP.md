# ROADMAP — from backtester to a complete trading system

This is the big-picture, phased plan. Near-term tactical items live in
[`BACKLOG.md`](./BACKLOG.md); this file is the destination they ladder up to.

**North star:** research a strategy → backtest it → **forward-test (paper) it with
per-strategy dummy capital** → (much later, carefully) trade it live.

**Guiding principles**
1. **One code path for logic.** Strategy signals, costs, and exit rules are
   *shared* across backtest, forward test, and any future live path — otherwise
   results aren't comparable.
2. **Only code the strategy.** Infrastructure (data, execution, metrics) stays
   fixed; adding a strategy remains a small class.
3. **Read-only until proven.** The mStock client stays market-data-only; live
   order execution is a deliberately gated, late phase.
4. **Fail closed.** Any future live/risk feature defaults to *not trading* on
   doubt (per `PRD.md`).

Legend: `done` | `in-progress` | `todo` | `later`

---

## Phase 0 — Foundations ✅ DONE
- Pluggable strategy engine (signal + entries/exits models)
- Look-ahead-free vectorized backtest + costs
- Engine-enforced stop-loss / take-profit
- Metrics, equity/drawdown plotting, auto-charts
- `compare` command (rank multiple strategies)
- mStock TypeA data client (auth OTP/TOTP, history, caching) — read-only
- (in-progress) Option C: live mStock data verification (blocked on network only)

---

## Phase 1 — Backtest engine depth  `in-progress`
Make single-strategy backtests realistic and expressive.
- **Trailing stop** and **time-based exit** (max bars in trade) ✅ Done
- **Short entries** in the entries/exits model (`short_entries`/`short_exits`) ✅ Done
- **Position sizing:** fixed fraction, fixed cash, volatility/ATR target `todo`
- **Fill models:** next-open fill vs close; configurable slippage models `todo`
- **Trade log:** per-trade records (entry/exit, bars held, PnL, R-multiple) +
  CSV/JSON export `todo`
- **Richer metrics:** Sortino, expectancy, avg win/loss, profit factor,
  monthly-returns heatmap `todo`

## Phase 2 — Robustness & research  `todo`
Guard against overfitting; find durable parameters.
- **Parameter optimization** (grid/random) with a results table
- **Walk-forward analysis** (rolling in-sample/out-of-sample)
- **Out-of-sample split**, Monte-Carlo / trade bootstrap, deflated Sharpe
- **Multi-symbol / portfolio backtests** with capital allocation

## Phase 3 — Forward testing (paper trading) ✅ DONE (core)
Your idea: run selected strategies forward on fresh data with **dummy capital,
allocated per strategy**, and measure out-of-sample performance.

### 3a. Shared execution layer ✅ DONE
- `Portfolio` + `SimulatedBroker` (virtual fills, cash, positions, costs) used
  by *both* backtest and paper trading.
- Event-driven runner: on each new bar, fetch trailing window → call the *same*
  `strategy.generate_signals` → latest signal → broker acts.
- State persistence (JSON) so sessions survive restarts.

### 3b. Live paper trading ✅ DONE (synthetic) / 🔶 Partial (real data)
- **Synthetic data:** Full forward testing with synthetic bars — works end-to-end
- **Real-time feed:** mStock REST polling implemented; blocked on network egress
- Per-strategy capital allocation (e.g. RSI 50k, SMA 30k)
- Daily PnL + positions report; frontend real-time dashboard
- SSE streaming for live updates to the browser

### 3c. Portfolio Command Center ✅ DONE (just completed 2026-09-02)
- **Multi-strategy orchestration:** Run multiple strategies simultaneously
- **Per-strategy capital allocation:** Each strategy gets its own sandbox
- **Bucket separation (Live/Paper):**
  - Per-bucket state: equity, peak, drawdown, daily P&L — all *derived* from runners
  - Independent circuit breakers — paper breach does NOT halt live
  - Scoped bulk control — pause/resume/emergency scoped to target bucket
  - Master kill — emergency flatten both buckets at once
  - Capability flag — REAL MONEY banner driven by broker connection status
  - SSE carries embedded bucket data — single stream, frontend filters client-side
- **Three views:** Overview (combined), Live (scoped), Paper (sandbox)
- **116 tests** covering per-bucket state, breaker independence, flow semantics,
  scoped API endpoints, and UI views

### 3d. Remaining forward testing work  `todo`
- [ ] Walk-forward replay (offline) — replay unseen history bar-by-bar
- [ ] Strategy auto-kill on prolonged underperformance
- [ ] Real-time alerts (email/Telegram) for breaker trips and daily PnL

## Phase 4 — Live trading  `later` (high-risk, gated)
Only after paper trading proves an edge, and with eyes open.
- Order execution via mStock (adds the *first* write methods to the client)
- **Fail-closed risk gate**, **2% kill-switch**, position reconciliation
- Idempotent orders, broker-state sync, full audit log
- **Prerequisite:** mStock network egress must be working (Option C unblocked)

## Phase 5 — Platform & infra  `later` (cross-cutting)
- Persistence (SQLite/Postgres) for audit trail and historical PnL
- Config management (YAML/TOML), structured logging (JSON)
- Scheduling/automation, notifications (email/Telegram)
- CI + coverage pipeline, deployment automation

## Phase 6 — Options / F&O engine  `later` (the big PRD V1.0)
- The naked-risk options backtester from `PRD.md`; separate, large effort.
- **Blocked on `DEC-001`:** historical per-contract option data feasibility.

---

## Dependency map (what unlocks what)
- **Shared `SimulatedBroker` (Phase 3a)** is the keystone: it powers offline
  walk-forward, live paper trading, and later the live-execution seam.
- **Phase 3b live paper** needs Option C (mStock connectivity) resolved.
- **Phase 3c Portfolio Command Center** is ✅ DONE — provides the operational
  dashboard for monitoring and controlling live/paper strategies.
- **Phase 4 live** must come after Phase 3 proves out, and reuses the same
  Portfolio/Broker seam with a `LiveBroker`.
- **Phase 6 options** is independent but gated on data feasibility.

## Suggested near-term order
1. ~~Phase 1: trailing stop + time exit + trade log~~ (partial — trailing stop done)
2. ~~Phase 3a: SimulatedBroker + offline walk-forward~~ ✅ Done
3. ~~Phase 3b: live paper trading (synthetic)~~ ✅ Done
4. ~~Phase 3c: Portfolio Command Center~~ ✅ Done
5. Phase 2: walk-forward optimization
6. Phase 1 remaining: trade log, richer metrics, position sizing
7. Phase 3d: walk-forward replay, parameter optimization, alerts
8. Phase 4: live execution (once mStock network is unblocked)