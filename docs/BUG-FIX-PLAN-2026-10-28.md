# Quant Architect Post-Mortem, Prevention Doctrine & Junior Engineering Bug-Fix Plan (28 Oct 2026)

**Source Report:** `docs/QA-LIVE-PAPER-BUG-REPORT-2026-10-28.md` (`39` findings: `7 S1 / 14 S2 / 11 S3 / 7 S4`, plus `35` passing checks to protect)  
**Baseline Commit:** `bd5f559` (`feature/options-trading-dev` / `arena/f7407685-back-test`)  
**Active Scope:** **37 active findings** (`BUG-01`–`BUG-30` [buffer half], `BUG-33`–`BUG-39`).  
**Parked Scope (per `bd5f559` §1.1):** `BUG-31`, `BUG-32`, and `BUG-30` (`SPAWN` preview verb split) remain **PARKED** until the 5-point Core Trading Loop Gate passes on a live paper session.

---

## 1. Quant Architect Root-Cause Synthesis: *Why Did These 37 Bugs Happen?*

In institutional prop/quant trading systems, 37 bugs rarely come from 37 unrelated typos. They almost always stem from a **small number of missing structural invariants**. Every finding from the 28-Oct live QA run traces back to **7 architectural root causes**:

| # | Structural Root Cause | Bugs Caused | Why It Crept In | How Tier-1 Trading Desks Prevent It Forever |
|---|---|---|---|---|
| **RC-1** | **Conflating *Exit/Risk Cadence* (Tick) with *Signal/Entry Cadence* (Timeframe Bar)** | `BUG-01`, `BUG-02` (churn multiplier), `BUG-16` | To let `CE_instant_buy` / `immediate_strangle` enter immediately on deploy without waiting up to 59 min for a `1hour` bar close, `warmup.py:227` disabled `requires_timeframe_aggregation()` permanently and `paper_runner.py:289,2530` ran unlatched tick entries every time the book went flat — ignoring `timeframe` while `stale_after_seconds` still used `timeframe`. | **Two-Clock Separation + Per-Bucket Entry Latch:** Protective exits (`SL`/`TP`/`EOD`) ALWAYS run on sub-second ticks; strategy entries run strictly on `(symbol, timeframe)` bar boundaries (plus an optional one-shot `enter_on_deploy` token consumed once, never re-armed inside the same bar bucket). |
| **RC-2** | **Violating Exchange Microstructure & Fee-Floor Physics ("Paper Optimism")** | `BUG-02`, `BUG-06`, `BUG-26`, `BUG-15` | Strangle SL/TP (`±₹100` = `₹1.54/unit` on 65-lot) was narrower than the ₹2–4 spread bounce and 52% of the ₹193 round-trip fee; `_apply_slippage` multiplied already-crossed `bid`/`ask` by `1 ± 0.001` rounded to `₹0.01` instead of `₹0.05` tick size; Equity paper bypassed `CommissionCalculator` (`₹0` fees on 2,749 trades); un-tradeable runners (`₹27.8k > ₹25k` cap) stayed `🟢 RUNNING`. | **Tick-Grid Law + Unified Cost Engine + Pre-Trade Economic Gate:** Every fill is quantized adversely to `contract.tick_size` (`0.05`); L1 depth fills don't double-pay percentage slippage; **one** `CommissionCalculator` prices both Equity and Options; spawn/runtime marks economically impossible configs `BLOCKED_BY_RISK`. |
| **RC-3** | **Stateful `+=` Accumulators & Asymmetric Book Folding Instead of Pure Accounting Identities** | `BUG-03`, `BUG-04`, `BUG-33`, `BUG-37` | `self.total_capital` used `+=`/`-=` instead of summing live runners (`+₹82.5L` drift); empty ₹10L `dashboard_book` was added to `total_equity` but not `total_capital`; `realized_pnl` was gross while `daily_pnl`/`equity` were net; `_bucket_day_start` ignored mid-day `+₹39L` runner spawns; pytest wrote `LIVE-STRANGLE` rows into production Postgres. | **Derived-Only Double-Entry Ledger + Cash-Flow-Adjusted Anchor + Hermetic Test Firewall:** Totals are pure functions of `get_bucket_aggregates()`; every row emits `{gross_pnl, fees, net_pnl}` with UI bound strictly to **NET**; mid-day capital spawns adjust `day_start_equity`; `conftest.py` blocks production DB URLs during `pytest`. |
| **RC-4** | **Implicit Overnight Carry & Un-gated Session Boundaries** | `BUG-39`, `BUG-18`, `BUG-22` | Intraday square-off required an unset env/expression opt-in and only fired on incoming ticks; a `15:30:30 IST` bar opened trades post-close; monthly options carried overnight unmarked; boot staleness timers started before warmup finished. | **Mandatory `CarryPolicy` + Wall-Clock EOD Sweep + Hard Entry Window:** Every strategy declares `intraday` (default 15:10 IST timer-driven square-off) vs `positional` (explicit `OVERNIGHT_CARRY` badge); entries hard-blocked outside `09:15:00–15:25:00 IST`; 180s boot grace on staleness alerts. |
| **RC-5** | **Split-Brain Risk Configs, Destructive Chaos Hooks & Global Kill-Switch Leak** | `BUG-27`, `BUG-28`, `BUG-07`, `BUG-35`, `BUG-19` | 5 YAML/dataclass risk surfaces with only hardcoded defaults active; `#risk-strip-flatten` flattened LIVE from `/portfolio/paper`; `/api/portfolio/test/breach` mutated live `supervisor.config`, overwrote real OHLC `_bars`, skipped post-close breaker evaluation, and saved corrupted anchors to disk. | **One Risk Config Loader + Pure Ephemeral Chaos Simulation + Scoped Flatten:** Boot loads `GlobalRiskConfig` from one YAML and logs provenance; stress tests run on a cloned shadow state (never touching `_bars` or `portfolio_state.json`); UI kill switches always pass `PAGE_MODE`. |
| **RC-6** | **Silent Persistence Latches, Forked Migrations & Unjoined Analytics** | `BUG-38`, `BUG-14`, `BUG-23`, `BUG-36`, `BUG-13`, `BUG-05`, `BUG-34`, `BUG-17`, `BUG-21` | Two PRs created Alembic `020`; `_guarded()` hid the real SQL error and latched `alerts` off for the session while returning `200 OK` from RAM; DB `trades` wrote `net_pnl = gross_pnl`; `options_bridge` emitted `null` Greeks while `intelligence` substituted `15%` IV silently; Warmup ran 4 duplicate 23s hypertable scans concurrently. | **Single-Head Alembic CI Test + Loud Retryable Persistence + Mid-Solved IV with Provenance + Singleflight Warmup:** CI fails if `alembic heads != 1`; persistence logs tracebacks and retries with backoff; Greeks solve implied IV from live mid-price (or badge `assumed_15pct` explicitly); Concentration uses `delta_notional`; Warmup coalesces concurrent reads. |
| **RC-7** | **Ad-Hoc UI String Slicing, Un-namespaced IDs & Clock-Ratchet Blind Spot** | `BUG-08`–`BUG-12`, `BUG-20`, `BUG-24`, `BUG-25`, `BUG-29`, `BUG-30` | Each JS file parsed ISO timestamps and `#risk-mode` differently; `structure_id` lacked `instance_id`; `PAUSED` lacked `pause_source`; `HotStrikeRing` logged no recenters; `watch_export.py` stripped tz via `.strftime()`. | **Shared Frontend `Clock` + `BucketScope` SDK + Globally Scoped IDs + 5th AST Ratchet Rule:** One JS clock/duration formatter (`Asia/Kolkata`); `struct_<iid8>_<utc>_<seq>`; structured `pause_reason`; AST test banning `.strftime("%Y-%m-%d %H:%M:%S")` outside `timekeeping.py`. |

---

## 2. Non-Negotiable Guardrails for the Junior Engineer (Read Before Coding!)

1. **DO NOT TOUCH `src/backtest/data/mticker.py` or `tests/test_mticker_ws_fixes.py`.** All WebSocket sends must continue routing through `subscribe` / `set_mode` / `unsubscribe` → `_send_frame` → `reactor.callFromThread`.
2. **Clock Policy (`src/backtest/timekeeping.py`):** Never introduce `datetime.utcnow()`, bare `datetime.now()`, `replace(tzinfo=...)`, or private `+05:30` offsets. Use `now_utc()`, `iso_utc()`, `from_market_clock()`, `from_stored_utc()`, and `EXCHANGE_TZ`. Run `pytest tests/test_timekeeping_policy.py` after every commit.
3. **Never Enable Live Orders:** Keep `config/risk_limits.yaml` `entitlement.live` strictly `[long_call, long_put]` and never set `ALLOW_LIVE_ORDERS` / `CONFIRM_LIVE_ORDERS` in `.env` or tests.
4. **Protect the 35 Passing QA Checks (§5 of Bug Report):** Every sprint below lists the exact passing assertions you must not regress (e.g., `bucketMetrics()` scoping on `/portfolio/paper`, LOM 409 validations, atomic option closes, `source_id == instance_id` Greeks join, P1 steady `pg_stat_activity = 3`).
5. **Do Not Work on Parked Items:** Skip `BUG-31`, `BUG-32`, and the `SPAWN_PREVIEW` verb split of `BUG-30` until Sprints 1–2 pass live verification.

---

## 3. Sequenced Engineering Execution Plan (8 Sprints — Junior Developer Runbook)

Work strictly in sprint order (`Sprint 1` → `Sprint 7`). Open **one focused PR per sprint** so reviews are fast and bisecting regressions is trivial.

---

### Sprint 1 (P0 · Blocker): Honest Trading Loop — Timeframe Gate, Tick-Grid Fills, Symmetric Fees & EOD Governance
**Unlocks the §1.1 Gate · Fixes:** `BUG-01` (S1), `BUG-02` (S1), `BUG-26` (S1), `BUG-39` (S1), `BUG-06` (S2), `BUG-15` (S3), `BUG-16` (S3), `BUG-18` (S3)

#### Ticket 1.1 — Enforce Declared `timeframe` on Option Entries & Separate Exit Cadence from Entry Cadence (`BUG-01`, `BUG-16`)
- **Files:** `src/backtest/forward/warmup.py` (`lines 210–231`), `src/backtest/forward/paper_runner.py` (`lines 276–296`, `2515–2580`), `src/backtest/forward/portfolio_manager.py` (`lines 2550–2575`), `src/backtest/forward/options_bridge.py` (`lines 865–880`, `1055–1075`)
- **What to Change:**
  1. **Remove the permanent aggregation bypass in `warmup.py:227–228`:** `requires_timeframe_aggregation(runner)` must return `True` whenever `normalize_timeframe(runner.config.timeframe) != "1min"` — for **all** strategies, including `instant_long_option` and `structure`.
  2. **Model "Deploy Entry" as a one-shot latch, NOT permanent tick-entry churn:**
     - On `StrategyRunner`, add `_initial_deploy_entry_pending: bool = True` for `InstantLongOptionStrategy` and `StructureStrategy`, plus `_last_entry_bucket_ts: Optional[str] = None`.
     - When deployed mid-bar (e.g., at `12:44 IST` on a `1hour` runner), allow **at most ONE** immediate entry for the current `bucket_start_ts(ts, timeframe)` (`12:15`–`13:15` bucket), immediately setting `_initial_deploy_entry_pending = False` and `_last_entry_bucket_ts = current_bucket`.
     - If that structure hits SL/TP inside the same `15min`/`1hour` bucket, **block any new entry until `bucket_start_ts(ts, timeframe) > _last_entry_bucket_ts`** (i.e., the next genuine `15min`/`1hour` bar boundary)!
     - Keep `OptionsBridge.on_tick()` running on **every WS tick** so MTM, SL, TP, and trailing stops still fire sub-second!
  3. **Fix `stale_after_seconds` & `bars_processed` & `entry_ts` (`BUG-01` secondary + `BUG-16`):**
     - On WS-streamed option legs, base `stale_after_seconds` on tick/quote SLA (`STALE_AFTER_SECONDS`, e.g. `180–240s`), NOT `3,780s` (`1hour`), so a dead WS feed on an hourly option runner flags stale in ~3–4 minutes instead of 63 minutes!
     - Increment `bars_processed` **only** in `process_candle_event()` on genuine sealed `runner.config.timeframe` bars (never on 1-min mark/tick updates).
     - Stamp tick-triggered entries with actual `now_utc()` (or the live tick `ts` via `from_market_clock`), NOT the floor minute `self._bar_dt`, so a 34-second hold has accurate microsecond `entry_ts` and `exit_ts`.
- **Acceptance Test:** Spawn `immediate_strangle` on `15min` and `1hour`; force an immediate SL exit at `13:01`; verify **zero** re-entries occur at `13:02..13:14` (`15min`) or `13:02..13:14/14:14` (`1hour`).

#### Ticket 1.2 — Economic SL/TP Floor, Cooldown & `max_reentries_per_day` Enforcement (`BUG-02`, `BUG-15`)
- **Files:** `plugins/strategies/immediate_strangle.py` (`lines 102–125`) *(coordinate unlock if still guarded, or enforce in `options/exit_policy.py` + `options_bridge.py`)*, `src/backtest/options/exit_policy.py`, `src/backtest/forward/options_bridge.py`, `src/backtest/forward/paper_runner.py`
- **What to Change:**
  1. **Fix `max_reentries_per_day` counting (`options_bridge.py`):** Currently `max_reentries_per_day` only counts signal-flip re-entries, allowing 205 stop-loss re-entries in 1 hour! Count **every closed structure on the current `session_date`** against `max_entries_per_day` / `max_reentries_per_day` (default e.g. `max_reentries_per_day = 2` → max `3` entries/day unless overridden), plus add `reentry_cooldown_seconds` (default `60s` on `1min` runners).
  2. **Resize `immediate_strangle` default bracket above round-trip fees + spread:** A `±₹100` bracket on a ₹193-fee 2-leg structure has **0% mathematical chance of net profit**. Update strangle default exit policy to `stop_loss_pct: 0.35` / `take_profit_pct: 0.50` of net credit collected (or `≥ ₹1,000` / `~15 pts` on 65-lot NIFTY) and add a pre-spawn warning/refusal when `declared_take_profit_inr <= estimated_roundtrip_fees * 1.2`.
  3. **Stop silent-dead `🟢 RUNNING` loop (`BUG-15`):** When `OptionsBridge` rejects an entry with deterministic sizing/cap errors (`MAX_LOSS_EXCEEDED`, `LOT_COST_EXCEEDS_CAPITAL`) 3 consecutive times on the same contract, transition runner status (or `trade_health` badge) to `BLOCKED_BY_RISK` with `block_reason = "Per-trade worst-case ₹27,814 exceeds ₹25,000 limit"`, and throttle log output to once per 15 minutes instead of 11,410×! Also validate at `/api/portfolio/runner/create` when spot is known.

#### Ticket 1.3 — Exchange ₹0.05 Tick-Grid Fill Quantization (`BUG-06`)
- **Files:** `src/backtest/options/paper_trading.py` (`lines 635–650`, `1215–1235`)
- **What to Change:**
  1. Add helper `quantize_nse_tick(price: Decimal, side: str, tick_size: Decimal = Decimal("0.05")) -> Decimal`:
     - Round `BUY` fills **up** (`ROUND_CEILING` to nearest `0.05` multiple) and `SELL` fills **down** (`ROUND_FLOOR` to nearest `0.05` multiple) — adverse rounding that is 100% exchange-legal!
  2. **Eliminate spread + bps double-counting:** When `quote_source` is live two-sided L1/L5 WS depth (`live:mstock+bidask`, where `BUY` already pays the full `ask` and `SELL` hits the `bid`), default additional model slippage to `0` ticks (or configurable integer `slippage_ticks >= 0`, default `0` when `lots <= top_book_lots`), applying percentage slippage **only** when filling against synthetic/mid quotes — and **always** pass the final `avg_fill_price` through `quantize_nse_tick()`.
- **Acceptance Test:** Assert `(fill_price * 20) == int(fill_price * 20)` (`fill_price % Decimal("0.05") == 0`) across 100% of paper option fills!

#### Ticket 1.4 — Route Equity Paper Trading Through `CommissionCalculator` & Record `exit_reason` (`BUG-26`)
- **Files:** `src/backtest/forward/paper_runner.py` (`lines 1505–1540` and equity fill handler / `OrderExecutor`), `src/backtest/simulator/execution.py`, `src/backtest/simulator/position.py`
- **What to Change:**
  1. Instantiate `CommissionCalculator.for_broker(config.execution_broker or config.source or "mstock")` on `StrategyRunner` for equity runners, using segment `"equity_intraday"` for intraday timeframes (`1min`, `5min`, `15min`, `30min`, `1hour`) and `"equity_delivery"` for swing (`4hour`, `1day`, `1week`) unless overridden by `config.segment`.
  2. Deduct entry + exit brokerage and statutory charges (STT, exchange txn, SEBI, stamp duty, GST) from `self.portfolio.cash` on every equity fill, and stamp `gross_pnl`, `commission`, `statutory_fees`, `net_pnl`, `segment`, and `exit_reason` (`signal_exit`, `stop_loss`, `take_profit`, `time_exit`, `manual_close`, `eod_squareoff`) onto every closed equity position and `closed_trades` dict!
- **Acceptance Test:** Replay a 100-share ICICIBANK round-trip on `hf_equity_scalp`; verify `commission > 0`, `statutory_fees > 0`, `net_pnl == gross_pnl - commission - statutory_fees`, and `exit_reason` is non-null.

#### Ticket 1.5 — Session-Clock EOD Square-Off, Hard Entry Window & Explicit `CarryPolicy` (`BUG-39`, `BUG-18`)
- **Files:** `src/backtest/forward/options_bridge.py` (`lines 1408–1445`, `2628–2645`), `src/backtest/forward/portfolio_manager.py` (background 1s session-clock monitor), `src/backtest/strategy/execution.py`
- **What to Change:**
  1. **Explicit `carry_policy`:** Add `carry_policy: "intraday" | "positional"` to option expressions:
     - Default to `"intraday"` (`intraday_squareoff_minutes_before = 20`, i.e., **15:10 IST**) for all `1min`/`5min`/`15min`/`30min`/`1hour` runners **unless** the strategy explicitly sets `carry_policy: "positional"` (or `min_days_to_expiry: null` with explicit `overnight_carry: True`).
  2. **Wall-Clock EOD Sweep (Do NOT rely on ticks arriving at 15:10!):** Add an EOD check inside `PortfolioManager`'s 1-second watchdog loop between `15:10:00` and `15:29:59 IST` that triggers `_maybe_intraday_squareoff(now_utc())` on every `RUNNING` intraday runner even if an illiquid strike had no tick that second!
  3. **Hard Market-Hours Entry Gate (`BUG-18`):** Refuse any new option or equity entry before `09:15:00 IST`, after `15:25:00 IST` (or after `15:30 - squareoff_minutes` for intraday runners), and ** strictly after `15:30:00 IST`** (`2026-10-27 15:30:30 IST` late-bar entry bug!).
  4. **Overnight Attribution Honesty (`BUG-18`):** On deliberately carried positional structures, stamp `entry_session_date` alongside `exit_session_date`, mark `carried_overnight: true` on open rows in `Positions`, and track prior-close MTM so overnight settlement P&L only books today's price change (`mark_now - prev_session_close_mark`) into `daily_pnl` while lifetime `realized_pnl` records full trade P&L!

---

### Sprint 2 (P0 · Blocker): Double-Entry Accounting Identities, Capital Anchors & Pytest DB Firewall
**Fixes:** `BUG-03` (S1), `BUG-04` (S1), `BUG-33` (S2), `BUG-37` (S2)

#### Ticket 2.1 — Eliminate Phantom ₹82.5L Capital & ₹10L Dashboard Equity (`BUG-03`)
- **Files:** `src/backtest/forward/portfolio_manager.py` (`lines 580–668`, `1020–1025`, `1128–1135`, `2970–3055`)
- **What to Change:**
  1. Delete reliance on the drifting `self.total_capital` `+=` / `-=` accumulator in `summary()`: compute unscoped `total_capital = round(sum(b["capital"] for b in buckets.values()), 2)` and re-sync `self.total_capital` on `_restore_state()`, `add_runner()`, and `remove_runner()`.
  2. **Fix the ₹10L `dashboard_book` asymmetry (`portfolio_manager.py:3008–3035`):**
     - On scoped calls (`mode in ("paper", "live")`), **never** fold `dashboard_book` into `total_equity`.
     - Even on unscoped calls (`mode=None`), if `dashboard_book` has `open_positions == 0` and `realized_pnl == 0`, it is an idle unallocated singleton — **do not add ₹10,00,000 to `total_equity`!** If it is ever used, include `dashboard_capital` in **both** `total_capital` and `total_equity` or keep it isolated in `dashboard_book` only.
  3. Update `components/landing_risk.js:159` to read `buckets[mode].equity` and `buckets[mode].capital` directly.
- **Protect Passing Check:** Keep `portfolio.js` `bucketMetrics()` untouched (§5 `TC-1.4 cards` PASS).

#### Ticket 2.2 — Single NET P&L Contract Across Every Row, Card & Snapshot (`BUG-04`)
- **Files:** `src/backtest/forward/paper_runner.py` (`state()`, `daily_pnl()`, `realized_pnl()`), `src/backtest/forward/portfolio_manager.py` (`get_bucket_aggregates()`, `equity_snapshot()`), `src/backtest/web/static/js/portfolio.js`
- **What to Change:**
  1. Standardize every runner state dict to emit explicit fields:
     - `gross_realized_pnl`: lifetime gross realized P&L
     - `total_fees_paid`: lifetime `commission + statutory_fees` (both equity and options)
     - `realized_pnl`: **lifetime NET realized P&L** (`gross_realized_pnl - total_fees_paid`)
     - `realized_pnl_today`: **today's NET realized P&L** (`session_date == today_ist`)
     - `open_pnl`: **current NET unrealized P&L** (after entry fees on open structures/positions so closing at flat entry/exit marks yields continuous equity!)
     - `daily_pnl`: `realized_pnl_today + (open_pnl - prev_close_open_pnl)`
  2. Fix the equity partial-close `daily_pnl` drop on `TCS` (`BUG-04` defect #2): ensure `pos.closed_at` / `session_date` is stamped via `now_utc()` on partial scale-out (`close_position(fraction=0.5)`) so the realized half (`+₹429.20`) stays inside `realized_pnl_today` and `daily_pnl` (`429.20 + 493.00 = +922.20`)!
- **Invariant Unit Test (Mandatory):** For any runner spawned today (`prev_close_open_pnl == 0`), assert:
  - `abs(runner.equity - (runner.allocated_capital + runner.realized_pnl + runner.open_pnl)) <= 0.02`
  - `abs(runner.daily_pnl - (runner.realized_pnl_today + runner.open_pnl)) <= 0.02`

#### Ticket 2.3 — Capital-Flow-Adjusted `day_start_equity` (`BUG-33`)
- **Files:** `src/backtest/forward/portfolio_manager.py` (`add_runner`, `delete_runner`, `reset_daily_anchors`, `equity_snapshot`, `summary`)
- **What to Change:**
  1. When a runner is added to bucket `mode` mid-session with `allocated_capital = C`, increment `self._bucket_day_start[mode] += C` and `self._day_start_equity += C` (and decrement by runner equity on mid-day runner deletion, or track `capital_inflows_today`).
  2. Compute `daily_pnl_pct = daily_pnl / max(day_start_adjusted, allocated_capital, 1.0)`.
- **Acceptance Test:** Boot with ₹63L across 8 runners, spawn ₹39L across 6 new runners mid-day with ₹0 trades → assert `day_start_equity == ₹1,02,00,000`, `net_today == ₹0`, and `daily_pnl_pct == 0.0%` (never `+38.8%` or `-2.02%`!).

#### Ticket 2.4 — Hermetic Pytest DB Firewall & Boot Orphan Reaper (`BUG-37`)
- **Files:** `tests/conftest.py`, `src/backtest/db/manager.py` (`from_env`), `src/backtest/forward/portfolio_manager.py`, `src/backtest/forward/strategy_adapter.py` (`_save_signal_to_db`)
- **What to Change:**
  1. **Hard Firewall in `DatabaseManager.from_env()` / `tests/conftest.py`:** When `"PYTEST_CURRENT_TEST" in os.environ` and `os.environ.get("ALLOW_LIVE_DB_IN_PYTEST") != "1"`, **never** read `FORWARD_TEST_DB_URL` from `.env` if it points to the live `forward_test` Postgres database — redirect to an ephemeral SQLite in-memory/temp engine! Also fix `tests/forward/test_phase3_subsecond_options_and_ticks.py:873–900` to inject an isolated `state_store` & `db=None`.
  2. **Stop FK-stub capital inflation (`strategy_adapter.py:1352–1372`):** Never create `status="active", initial_capital=100000` portfolio rows as a side effect of logging a signal; use `status="shadow_fk"` (`initial_capital=0`) or require the runner's registered `portfolio_id`.
  3. **Boot Reaper + One-Shot Cleanup Tool:** On `PortfolioManager` startup (after `_restore_state()`), mark any `portfolios` DB row with `status='active'` whose `[instance_id[:8]]` is not in `self._runners` (including the 13 `LIVE-STRANGLE` fixture rows and 27 historical orphans) as `status='archived'` so `SELECT sum(initial_capital) FROM portfolios WHERE status='active'` equals `₹1,02,00,000`!

---

### Sprint 3 (P0 · Safety): Kill-Switch Scoping, Non-Destructive Stress Sandbox & Single Risk Authority
**Fixes:** `BUG-07` (S2), `BUG-28` (S1), `BUG-27` (S2), `BUG-35` (S2), `BUG-19` (S4)

#### Ticket 3.1 — Scope or Hide Global Topbar `#risk-strip-flatten` (`BUG-07`)
- **Files:** `src/backtest/web/static/js/components/risk_strip.js` (`lines 380–405`), `src/backtest/web/templates/base.html`
- **What to Change:**
  1. Detect `const pageMode = document.getElementById("portfolio-page")?.dataset?.mode || "";`.
  2. On `/portfolio/paper` (`pageMode === "paper"`), **hide `#risk-strip-flatten`** (matching `#btn-emergency`), or change label to `"Flatten Paper"` and strictly POST `{"reason": "sticky_strip_emergency", "mode": "paper"}`.
  3. On `/portfolio/live`, POST `{"mode": "live"}` and explicitly state the target bucket in `#risk-flatten-body`. Never allow an unscoped `mode: null` flatten from a bucket-scoped page!

#### Ticket 3.2 — Sandbox `/api/portfolio/test/breach` (Zero State/Bar Corruption + Immediate Breaker Trip) (`BUG-28`)
- **Files:** `src/backtest/api/portfolio.py` (`lines 1140–1165`), `src/backtest/forward/portfolio_manager.py` (`lines 3240–3300`), `src/backtest/forward/paper_runner.py` (`apply_markdown`)
- **What to Change:**
  1. Require `mode="paper"` (refuse `mode="live"` with `403`), default `tighten_limits` to `False`, and default `dry_run` to `True` (or snapshot-and-restore).
  2. **Never overwrite `runner._bars[symbol][-1]`** (which corrupts live EMA/SMA/ATR indicator buffers with a fake `-25%` candle and naive UTC `strftime`!). Compute simulated crash P&L in a **shadow evaluation pass** (or temporary `stress_price_override` cleared on reset).
  3. **Never call `reset_daily_anchors()` → `_persist_state()`** inside `stress_test()`!
  4. If `tighten_limits=True`, mutate via `GlobalRiskConfig.update_limits(...)` inside a context/snapshot that restores the original config on `reset_breaker`.
  5. **Call `self._evaluate_risk(mode="paper")` immediately inside `stress_test()`** so the breaker **actually trips to `HALTED`** even at `16:41 IST` after market close when no live ticks are flowing!

#### Ticket 3.3 — Single Risk Limit Authority, Segment Derivation & Spawn Entitlement Order (`BUG-27`, `BUG-35`, `BUG-19`)
- **Files:** `src/backtest/forward/risk_supervisor.py`, `src/backtest/forward/portfolio_manager.py` (`lines 90–115`, `375–395`), `src/backtest/api/portfolio.py` (`lines 895–970`), `config/forward_testing.yaml`
- **What to Change:**
  1. **One Loader (`BUG-27`):** Add `load_bucket_risk_configs("config/forward_testing.yaml")` at `PortfolioManager.__init__` so `supervisor.config`, `_risk_configs["paper"]`, and `_risk_configs["live"]` come from YAML (not invisible dataclass defaults), align `paper_runner.py` instance defaults (`0.25` DD / `0.15` daily loss) with `api/portfolio.py`, log resolved limits at `INFO` on startup, and include `limit_source: "config/forward_testing.yaml"` in `/api/portfolio/risk`.
  2. **Auto-derive `segment` & clean `by_broker` (`BUG-35`):** At runner create (and on `_restore_state` backfill when `segment is None`), derive default segment from `instrument.type` + timeframe (`option` on index → `options_index`; intraday `equity` → `equity_intraday`; daily `equity` → `equity_swing`). Never allow an `equity` runner in `options_index`. In `concentration.by_broker`, report `runner.config.execution_broker or runner.config.source` (`"mstock"`), with `mode` in its own `mode` field. Hide or dim segment breaker cards on `/portfolio/paper` with explicit badge `"Live-only segment breaker (0 live runners)"`.
  3. **Entitlement Check Before Gateway Flag (`BUG-19`):** In `POST /api/portfolio/runner/create`, run `OptionExecutionGate` structure entitlement check (`config/risk_limits.yaml` → `entitlement[mode]`) **before** checking `confirm_live_orders`, so attempting a `live` `immediate_strangle` returns a clear `403: Structure 'strangle' is not entitled in 'live' mode (allowed: long_call, long_put)`!

---

### Sprint 4 (P1 · Data Integrity): Unfork Alembic `020` & Repair the Durable Audit Ledger
**Fixes:** `BUG-38` (S3 — **Prerequisite Gate**), `BUG-14` (S3), `BUG-23` (S3), `BUG-36` (S2), `BUG-13` (S3)

#### Ticket 4.1 — Unfork Alembic `020` + Add Single-Head CI Test (`BUG-38` — DO THIS FIRST!)
- **Files:** `db/alembic/versions/20261013_1200_020_add_upstox_source_to_portfolios.py`, `db/migrations/`, `tests/db/test_migrations_020.py`
- **What to Change:**
  1. Keep `20261008_1200_020_option_intent_journal.py` as `revision = "020"` (`down_revision = "019"`, since `option_intent_events` already exists in the live DB).
  2. Re-number `20261013_1200_020_add_upstox_source_to_portfolios.py` to `revision = "021"` with `down_revision = "020"` (making all `ALTER`/`ADD` steps idempotent `IF NOT EXISTS` so stamping/upgrading `019 → 021` on live `forward_test` is 100% safe).
  3. **Permanent CI Guardrail:** Add `test_alembic_has_single_linear_head()` using `alembic.script.ScriptDirectory.from_config(cfg).get_heads()` asserting `len(heads) == 1`. Two branches can **never** merge duplicate revision numbers again!

#### Ticket 4.2 — Fix `trades` Net P&L / Statutory Fees / `structure_id` Persistence (`BUG-14`)
- **Files:** `db/alembic/versions/20261029_022_trades_statutory_and_structure.py` (new linear revision `022`, `down_revision = "021"`), `src/backtest/db/models.py` (`Trade`), `src/backtest/forward/trade_persistence.py`
- **What to Change:**
  1. Add nullable-safe columns `statutory_fees` (`Numeric(14, 4)`, default `0`), `structure_id` (`String(64)`), `segment` (`String(32)`), and `session_date` (`Date`) to `trades`.
  2. In `trade_persistence.py`, write **`net_pnl = gross_pnl - commission_total - statutory_fees`** (fixing the bug where `3,166/3,166` DB rows had `net_pnl == gross_pnl`!), and persist `exit_reason` for both equity and option rows.

#### Ticket 4.3 — Un-latch `alerts` Persistence, Log Real Exceptions & Wire Paper Audit Durability (`BUG-23`, `BUG-36`, `BUG-13`)
- **Files:** `src/backtest/intelligence/persistence.py` (`lines 145–175`), `src/backtest/api/intelligence.py` (`lines 225–245`), `src/backtest/forward/paper_runner.py` (`OptionsBridge` init + order/signal persistence)
- **What to Change:**
  1. **Fix `_guarded()` in `intelligence/persistence.py`:** Log the **actual exception class + message** (`logger.warning(..., exc_info=True)` — remove the hardcoded `"apply migration 005"` guess!). Replace the permanent process-lifetime `self._disabled.add(table)` latch with a **60-second cooldown retry** (`_retry_after[table] = monotonic() + 60`) and expose `disabled_tables` + `last_persistence_error` in `/api/portfolio/intelligence`.
  2. In `/api/alerts/history`, include `"durable": source == "db"` and render a visible `"⚠ In-memory fallback (DB persistence degraded)"` pill in UI whenever `durable === false`.
  3. Inject `intent_journal` into `OptionsBridge` for paper runners (`BUG-13`, tagged `mode="paper"`) and persist terminal paper order rows (or batch-flush them via `trade_persistence`) so post-session SQL audits match in-memory `orders` and `option_intent_events`.

---

### Sprint 5 (P1 · Quant Analytics): Unified Greeks, Implied-From-Mid IV & Delta-Adjusted Concentration
**Fixes:** `BUG-05` (S2), `BUG-34` (S2), `BUG-17` (S3)  
**Protect Passing Check:** Keep `breakdown_by_strategy[].source_id == instance_id` and equity `Γ = ν = Θ = 0` intact (§5 `TC-4.1/4.2` PASS).

#### Ticket 5.1 — Solve Implied Volatility from Live Option Mid-Price & Tag Provenance (`BUG-05`, `BUG-34`)
- **Files:** `src/backtest/intelligence/greeks.py`, `src/backtest/forward/options_bridge.py` (`_leg_vol`, `_leg_greeks`, `open_structures_snapshot`)
- **What to Change:**
  1. **Standard Quant Desk Fix for Missing Broker IV:** We already stream live two-sided L1 `bid`/`ask` on every open option leg, plus live underlying `spot` and `expiry`! Add a fast 8-iteration Brenner-Subrahmanyam + Newton/bisection Black–Scholes implied-volatility solver (`solve_iv_from_mid(mid_price, spot, strike, t_years, r, option_type)`) in `options/pricing.py` (takes `< 0.05 ms` per leg!):
     - Priority 1: Broker-supplied IV → `greeks_source = "broker:iv=16.4%"`
     - Priority 2: Solved from live two-sided mid `(bid + ask) / 2` → `greeks_source = "implied:iv=16.8%"`
     - Priority 3 (only if deep ITM/illiquid inversion fails): Fallback `DEFAULT_IV = 0.15` → `greeks_source = "model:assumed_iv=15%"`, incrementing `assumed_iv_legs` in `/api/portfolio/greeks` (never claim `missing_greeks: []` without `assumed_iv_legs` count and UI badge!).
  2. **Bridge & Positions Surfacing (`BUG-05`):** Call the exact same helper inside `OptionsBridge._leg_greeks()` so `legs_detail[]` (`delta`, `gamma`, `vega`, `theta`, `iv`, `greeks_source`) and structure `net_delta` / `net_theta` populate live on the `Positions` tab!

#### Ticket 5.2 — Switch Concentration Alert Basis from `spot × units` to `delta_notional` (`BUG-17`)
- **Files:** `src/backtest/intelligence/concentration.py`
- **What to Change:**
  1. Compute underlying concentration `%` and trigger `high_concentration` (`> 60%`) using `abs(delta_notional)` (which equals `spot × shares` for cash equities — 0 change to equities! — and `|net_delta| × spot` for options, eliminating false 60.18% alerts on ₹62k of OTM/ATM option premium!). Keep `gross_spot_notional` as a secondary informational field.

---

### Sprint 6 (P1 · Performance & Reliability): Coalesced Warmup, Boot Alert Grace & Ring Observability
**Fixes:** `BUG-21` (S2), `BUG-22` (S3), `BUG-25` (S4)  
**Protect Passing Check:** Keep steady-state `pg_stat_activity` flat (`total = 3, busy = 1`, §5 `P1-DB` PASS).

#### Ticket 6.1 — Singleflight Warmup Bar Cache & In-Memory Holiday Cache (`BUG-21`)
- **Files:** `src/backtest/forward/warmup.py`, `src/backtest/live/market_status.py` (or holiday loader)
- **What to Change:**
  1. Cache `market_holidays` in memory with a 6-hour TTL (eliminating repeated 280–589 ms DB reads).
  2. Add a **singleflight lock + 30-second TTL cache** keyed by `(timeframe_base, symbol_set_hash, lookback_bucket)` in `WarmupService` so spawning 4 NIFTY/BANKNIFTY runners in the same minute shares **one** DB query instead of firing four concurrent 23-second hypertable scans! Also tighten the lower `ts >= cutoff` bound so TimescaleDB prunes old chunks cleanly.

#### Ticket 6.2 — Suppress False Post-Restart `CRITICAL data_feed_stale` Alerts (`BUG-22`)
- **Files:** `src/backtest/forward/portfolio_manager.py` (`_mtm_watchdog_loop` / feed health check), `src/backtest/intelligence/alerts.py`
- **What to Change:**
  1. Record `_boot_or_reauth_monotonic = time.monotonic()` on manager start and broker re-login.
  2. Suppress outbound `data_feed_stale` WARNING/CRITICAL alerts while `monotonic() - _boot_or_reauth_monotonic < 180` **or** while `runner.warmup_status == "WARMING_UP"`, and measure bar silence from the last expected bar boundary after feed subscription rather than process boot.

#### Ticket 6.3 — Structured `INFO` Log Line on `HotStrikeRing` Recenter / Pin / Evict (`BUG-25`)
- **Files:** `src/backtest/options/quote_providers.py` (`lines 1175–1255`)
- **What to Change:**
  1. Emit **one** concise event-driven `INFO` log line (never per-tick!) whenever `HotStrikeRing` recenters or releases:
     `[hot-ring] recenter underlying=NIFTY atm=22700->22750 subscribed=+4 evicted=-4 pinned=2 total_ring=22`

---

### Sprint 7 (P2 · End-User UX & Polish): Clocks, Filters, Strategy SL/TP Visibility, IDs & AST Ratchet
**Fixes:** `BUG-08` (S2), `BUG-09` (S2), `BUG-10` (S2), `BUG-11` (S2), `UX-3` (Strategy SL/TP visibility), `BUG-12` (S3), `BUG-20` (S4), `BUG-24` (+ Ratchet Addendum, S4), `BUG-29` (S4), `BUG-30` (buffer half, S3)

#### Ticket 7.1 — Shared Frontend IST Clock SDK & Backend Status Aware Timestamps (`BUG-08`, `BUG-11`)
- **Files:** `src/backtest/web/static/js/components/currency.js` (or new `clock.js` loaded in `base.html`), `orders_tab.js:86`, `risk_board.js:252`, `deep_dive.js:173`, `_portfolio_center.html:461`, `api/broker.py`, `api/market.py`
- **What to Change:**
  1. Create `window.ExchangeClock.fmtTimeIST(iso)` (`timeZone: "Asia/Kolkata"`, `hour12: false`) and `window.ExchangeClock.fmtHold(entryIso, exitIso)` (`34s`, `4m 12s`, `1h 18m`).
  2. Replace raw UTC regex/slice in `orders_tab.js` and `risk_board.js` so `Positions`, `Orders`, `Trade history`, and `Deep-Dive → Trades` all display **identical IST times**!
  3. Add **`Entry Time`**, **`Exit Time`**, and **`Hold`** columns to both `Trade history` (`_portfolio_center.html` + `risk_board.js`) and `🔍 Deep-Dive → Trades` (`deep_dive.js`).
  4. Return `iso_utc()` aware timestamps on `/api/broker/status` (`expires_at`) and `/api/market/status`.

#### Ticket 7.2 — Fix `#risk-mode` Split-Brain & Honor `instance_id` on Aggregated Trades (`BUG-09`, `BUG-10`)
- **Files:** `src/backtest/web/static/js/components/risk_board.js` (`lines 7, 214, 230`), `src/backtest/web/static/js/components/portfolio_intelligence.js` (`lines 50–56`), `src/backtest/api/portfolio.py` (`trades_aggregated`)
- **What to Change:**
  1. On scoped pages (`data-mode="paper"` or `"live"`), initialize `#risk-mode.value = pageMode` and `risk_board.js` `state.mode = pageMode` on DOMContentLoaded, and hide/lock `#risk-mode` to a `"PAPER BUCKET"` pill so `Trade history`, `Risk gauges`, and `Portfolio Intelligence` all query `?mode=paper` from frame 1! On `/portfolio` (combined page, `data-mode=""`), let `#risk-mode` drive **both** `portfolio_intelligence.js` and `risk_board.js`.
  2. In `GET /api/portfolio/trades/aggregated`, read `instance_id = request.args.get("instance_id")` and filter returned rows to that runner (`BUG-10`).

#### Ticket 7.3 — Surface Strategy Automatic `ExitPlan` SL/TP Directly in `Positions` (`UX-3`)
- **Files:** `src/backtest/forward/options_bridge.py` (`open_structures_snapshot`), `src/backtest/forward/paper_runner.py` (`positions_detail`), `src/backtest/web/static/js/portfolio.js` (`posLevelCell`)
- **What to Change:**
  1. Alongside manual `stop_loss` / `target`, emit `strategy_stop_label` and `strategy_target_label` computed from the runner's `ExitPlan` (e.g., `Auto ₹172.15 (-50%)` or `Auto -35% credit`, vs `Manual ₹178.50` when overridden via LOM, vs muted `Hold / Flip` when unset). Users immediately see how every open trade is protected without digging into `🔍 → Config`.

#### Ticket 7.4 — Globally Unique `structure_id`, `pause_reason`, Durable `audit_log`, Watch Export & 5th Clock Ratchet (`BUG-12`, `BUG-20`, `BUG-24`, `BUG-29`, `BUG-30`)
- **Files:** `src/backtest/options/paper_trading.py` / `live_trading.py` (`structure_id` generator), `src/backtest/forward/watch_export.py`, `src/backtest/api/portfolio.py` (`watch/export`, `watch/series`), `src/backtest/forward/paper_runner.py` + `state_store.py` (`pause_source`), `tests/test_timekeeping_policy.py`
- **What to Change:**
  1. **`BUG-12`:** Format `structure_id` as `struct_{instance_id[:8]}_{utc_YYYYMMDDHHMMSS}_{seq:04d}` — zero cross-runner collisions possible.
  2. **`BUG-29`:** Record `pause_source` (`"operator" | "instance_breaker" | "bucket_breaker" | "boot_restore" | "vix_regime_alert"`), `pause_reason`, and `paused_at` (`iso_utc()`) on `StrategyRunner` + `portfolio_state.json`, rendered as a badge/tooltip next to `🟡 PAUSED`.
  3. **`BUG-30` (buffer half):** Persist `_audit_log_entries` into a small `portfolio_audit_events` DB table (and/or `portfolio_state.json` tail of 200 items) so restarts never erase operator control history.
  4. **`BUG-20` + `BUG-24` + Filename Collision:**
     - Route `watch_export.py` date folder calculation through `backtest.timekeeping.session_date(now_utc())` and write `iso_utc()` timestamps into watch CSVs & `watch_series[].ts`.
     - Name exported files `<safe_runner_name>_<instance_id[:8]>.csv|.png` (no duplicate-name overwrite), return relative download URLs (`/api/portfolio/watch/download/...`) instead of `C:\learning\...` server paths, and fix `/api/portfolio/watch/series/<iid>` lookup key.
     - **5th AST Ratchet Rule (`tests/test_timekeeping_policy.py`):** Ban `.strftime("%Y-%m-%d %H:%M:%S")` on aware datetimes outside `timekeeping.py` so clock-stripping at serialization time can never return!

---

### Sprint 8 (PARKED — Do Not Start Until Sprints 1–2 Pass Live Paper Verification)
- **Parked Items:** `BUG-31` (`Playbook.from_dict` `structure_type` validation against `KNOWN_STRUCTURES`), `BUG-32` (refuse deploying an option Playbook onto an `EquityStrategy`), and `BUG-30` preview half (log `SPAWN_PREVIEW` instead of `SPAWN` in `POST /api/playbooks/<id>/spawn`).
- **Un-Park Gate (§1.1):** Resume only after a live NSE paper session proves: (1) zero off-boundary timeframe entries, (2) positive-expectancy spread/fee-valid brackets, (3) accurate first-hit SL/TP exits, (4) symmetric fee-honest NET P&L on both Equities and Options, and (5) automatic 15:10 IST EOD square-off on intraday structures.

---

## 4. Institutional CI Invariant Suite ("Never Let Small Bugs Slip Again")

Every junior PR must add its test to a dedicated **`tests/invariants/test_trading_system_invariants.py`** gate that runs in `< 5 seconds` on every commit:

1. **`test_accounting_identity_all_modes`:** Spawns equity + single-leg option + 2-leg strangle runners, executes entries, partial equity close, and full exits → asserts `total_capital == Σ runner.allocated_capital`, `total_equity == Σ runner.equity`, `equity == capital + net_realized + net_unrealized`, and `daily_pnl == net_realized_today + net_unrealized_today` within `₹0.02`.
2. **`test_timeframe_entry_latch_under_tick_churn`:** Feeds 60 ticks across 10 minutes into a `15min` `immediate_strangle` that stops out 3 seconds after entry → asserts **at most 1 entry** per 15-minute bucket (`0` off-boundary re-entries).
3. **`test_all_fills_on_nse_tick_grid`:** Executes 50 randomized BUY/SELL option fills across prices → asserts `100%` of `avg_fill_price` values satisfy `price % Decimal("0.05") == 0`.
4. **`test_symmetric_fee_charging_equity_and_options`:** Asserts both an equity scalp and an option trade record `commission > 0`, `statutory_fees > 0`, `exit_reason != None`, and `db_row.net_pnl == db_row.gross_pnl - db_row.commission_total - db_row.statutory_fees`.
5. **`test_single_alembic_head`:** Asserts `len(ScriptDirectory.from_config(alembic_cfg).get_heads()) == 1`.
6. **`test_pytest_cannot_touch_production_db`:** Asserts `DatabaseManager.from_env()` under `PYTEST_CURRENT_TEST` never connects to the `.env` production Postgres URL.
7. **`test_stress_endpoint_is_nondestructive`:** Calls `/api/portfolio/test/breach` → asserts breaker transitions to `HALTED`, runner `_bars` OHLC buffers are byte-identical before/after, and `portfolio_state.json` `day_start_equity` is untouched.
8. **`test_timekeeping_ast_ratchet_v2`:** Enforces all 5 clock rules including no tz-stripping `.strftime()` outside `src/backtest/timekeeping.py`.
