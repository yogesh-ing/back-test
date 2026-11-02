# Institutional Runner Lifecycle, Late-Login Auto-Wake & Nov-02 Bug-Fix Plan

**Date**: 2026-11-02  
**Baseline Commit**: `43d3322` (`feature/options-trading-dev` — includes PR #51 `8e1cba4`, `7fc3a08`, and `3ae2888`)  
**Status**: Approved Architecture & Implementation Reference (Read before touching `forward/`, `brokers/`, or `portfolio` UI)

---

## 1. Core Institutional Doctrine

At an institutional prop desk or systematic fund, runner lifecycle management obeys **one non-negotiable invariant**:

> **“Never go blind on an open position, and never abandon a live trade.”**

Broker authentication (`mStock` / `Zerodha` / `Upstox` TOTP) must be treated as **turning on the live market-data & execution pipe** — **never** as rebuilding the portfolio or forcing an operator to restart processes or manually hunt down paused open trades.

### 1.1 Separating Feed State, Position Protection, and New-Entry Arming

Current code conflates `RUNNING` vs `PAUSED` with broker login state and hides unauthenticated option runners inside `_parked_snapshots`. The target architecture separates **three orthogonal concerns**:

| Situation | Current App Behavior (`43d3322`) | Target Institutional Behavior |
| :--- | :--- | :--- |
| **1. Server boots at `08:30 IST` while broker is logged out (`remembered: false`)** | Option runners fail construction with `ValueError` and vanish into `_parked_snapshots` (`0` option runners visible on screen). | **All runners load and render on screen immediately** with yesterday's closing P&L, strikes, lots, and SL/TP levels under status badge **`WAITING_FOR_FEED`**. Nothing is hidden. |
| **2. Operator logs in late (e.g. `09:45 IST` after `09:15 IST` open)** | `_parked_snapshots` stay dead in memory until the Python server is restarted; restored runners stay `PAUSED`. | **Zero-restart auto-wake (< 1s)** on `verify-totp`: live quote providers + shared bar/WS feeds bind automatically, marks refresh immediately, and P&L updates live. |
| **3. Instance holds an OPEN trade / option structure across restart or late login** | `state_store.py:572` blindly forces `RUNNING -> PAUSED` on every runner alike, leaving open risk unmanaged until manual Resume. | **Open trades are NEVER paused** (unless halted by a risk circuit breaker). In-trade instances auto-enter **`RUNNING`** the moment the feed attaches, immediately checking SL / Target / Expiry exits. |
| **4. Today's P&L when logging in after `09:15 IST`** | Works once running (`_prev_close_open_pnl` anchor), but invisible while parked. | **Flat book**: starts at `₹0.00` at login and counts trades from login onward.<br>**Overnight carry book**: immediately marks `Live Price (09:45) − Yesterday 15:30 Close` so real morning gap P&L is never hidden from accounting or the `₹2.5L` daily fuse. |
| **5. Pre-login bars (`09:15–09:44 IST`) on flat strategies** | Risk of firing late on historical warmup bars if resumed blindly. | **No stale-signal chasing**: warmup populates indicator lookbacks (`EMA`, `ATR`, `VWAP`), while new entries only fire on bars/ticks sealed **at or after** `armed_at_utc`. |
| **6. Mid-day broker token/WS drop (e.g. `13:15 IST`)** | Logs quote failures; requires manual intervention if session died. | Flips to **`DEGRADED_FEED`** (blocks *new* entries while blind, keeps last known P&L + staleness timer visible) and **auto-heals** to `RUNNING` the instant TOTP/WS reconnects. |
| **7. Mid-day server restart after losses** | Daily `₹2.5L` fuse survives (via runner `daily_pnl()`), **but** `peak_equity` / `_bucket_peak` and `halted` banner reset to `0` / `False`. | **Same-session risk memory**: `(session_date_ist, peak_equity, _bucket_peak, halted_state)` persist across same-day restarts and reset cleanly only on a new IST session date (`00:00 IST`). |

---

## 2. Ground-Truth Audit of the Nov-02 Engineering Board

Before writing code, engineers must work from **verified code facts** at `43d3322` rather than assumptions:

1. **Manager Daily Loss Fuse vs. Drawdown Peak (`portfolio_manager.py:2687, 2815-2845, 3015-3031, 3115-3123`)**
   - **Verified Fact**: The bucket, segment, and global `₹2.5L` **Daily Loss Fuses** do **not** read `_bucket_day_start` or `_day_start_equity`. They evaluate `sum(r.daily_pnl() for r in runners)`, which uses each runner's persisted `pnl_session` / `_prev_close_realized_net` / `_prev_close_open_pnl` anchors (fixed in `7fc3a08`). `_bucket_day_start` is only used as a fallback denominator for `daily_pnl_pct` (`:618`, `:3360`), and `test_state_persistence.py::test_bucket_anchors_are_session_scoped` (`2026-09-23`) is stale against the `2026-10-15` denominator initialization.
   - **Real Gap to Fix**: `self.peak_equity`, `self._bucket_peak[mode]`, and same-day `halted` / `_bucket_halted[mode]` latches **are** wiped on every restart (`:2794, :2817, :2822`), which resets **Max Drawdown %** peak memory mid-session and clears active halt latches — **even while `portfolio_manager.py:2687` logs the misleading message `"restored … + breaker latches"`!**

2. **Parked Option Books & Blanket `RUNNING -> PAUSED` (`portfolio_manager.py:2758-2768`, `state_store.py:569-578`)**
   - **Verified Fact**: `_parked_snapshots` has **no** rehydration hook when `verify_totp` succeeds (`_parked_snapshots` is only touched at boot, `remove_runner`, and `_persist_state`). And `restore_runner()` maps `STATUS_RUNNING -> STATUS_PAUSED` unconditionally without checking whether `runner.positions` or `runner.options_bridge.get_open_structures()` holds live exposure.

3. **Option Structures Missing from SQL `positions` & Parked UI (`#24` — `trade_persistence.py:194-199`, `portfolio_manager.py:1454-1491`)**
   - **Verified Fact**: `PortfolioManager.list_positions()` (`:1454`) already emits open option structures to the UI `Positions` tab when runners are in `self._runners` (they vanished on screen only when hidden in `_parked_snapshots`). In PostgreSQL, `OrderLedger.record_filled()` intentionally leaves `position_id = None` so option legs never pollute the equity share-simulator `Portfolio.positions`.

4. **Frozen Runner Configuration at Spawn (`#3` — `state_store.py:514-530`, `options_bridge.py:147`, `3ae2888`)**
   - **Verified Fact**: By design, `RunnerConfig` (`expression`, `carry_policy`, SL/TP) is frozen at spawn so code deploys never mutate a live trade's carry horizon mid-flight. The canonical enum is `CARRY_POLICIES = {"intraday", "positional"}` (`options_bridge.py:147`) — existing intraday option runners must be retired/respawned once (when flat) to adopt `carry_policy = "positional"`, and the UI should surface a clear `CONFIG_DRIFT / FROZEN_AT_SPAWN` indicator when template defaults changed.

5. **Two-Clock Strings in `OrderLedger` (`#23` — `paper_runner.py:674, 756, 1917`)**
   - **Verified Fact**: SQL persistence (`trade_persistence.py:150`) already normalizes to UTC `TIMESTAMPTZ` at rest, so Postgres is clean. Only in-memory `OrderLedger` JSON mixes `+05:30` bar strings with `+00:00` fallback strings, scrambling text-sorted `Orders` rows by `5h 30m`.

6. **Manual Mark Refresh `broker.calls == 2` (`test_options_forward_mtm.py:948`, `quote_providers.py:501-539`, `mstock.py:1702-1715`)**
   - **Verified Fact**: On **mStock**, `get_option_quote("49228")` checks the in-memory WebSocket cache and short-circuits (`if self._is_numeric_key: return {}` at `mstock.py:1711`) with **zero HTTP calls**, then fetches `"NIFTY261024800CE"` over HTTP (**1 HTTP call total**). The test's `_CountingBroker` lacked `bid`/`ask` and counted the in-memory numeric probe as an HTTP call. (`UpstoxBroker.get_option_quote` at `upstox.py:780` lacks that numeric-key guard, but zero calls ever cross from mStock to Upstox and Upstox remains parked/untouched for now.)

---

## 3. Sequenced Implementation Plan (Sprints A → D)

Implement in strict sprint order (**one small, reviewable PR per sprint**).

---

### Sprint A (P0 — Highest Priority, Staged Across 2 Sub-PRs for Blast-Radius Control)
**Goal**: Logging in before *or* after `09:15 IST` wakes all waiting runners automatically in `< 1s`, updates P&L immediately, pages the operator inside active market hours, and (in sub-PR `A-2` after `A-1` bakes cleanly in a live session) guarantees any instance with an open trade is actively `RUNNING` and protected.

> **Release Sequencing Rule (Junior Review Guardrail)**:
> - **PR `Sprint A-1` (`Tickets A1 + A2 + A4`)** ships **first**: unauthenticated boot visibility (`WAITING_FOR_FEED` with strict zero-synthetic invariant), `< 1s` `verify-totp` auto-wake, and market-hours-only `T − 10m` / expired operator paging + `"Broker login needed"` topbar banner.
> - **PR `Sprint A-2` (`Ticket A3`)** ships **second** (in its own isolated PR after `A-1` completes a live session): reverses the legacy blanket `RUNNING -> PAUSED` restore rule for open-exposure books (`has_open_exposure() -> RUNNING` + `armed_at_utc` stale-signal guard), giving 1-commit rollback isolation.

#### Ticket A1 (`Sprint A-1`) — Construct Unauthenticated Runners in `WAITING_FOR_FEED` (Hard Zero-Synthetic Invariant)
- **Files**: `src/backtest/forward/portfolio_manager.py`, `src/backtest/forward/feed_registry.py`, `src/backtest/forward/paper_runner.py`, `src/backtest/forward/options_bridge.py`
- **Hard Non-Negotiable Invariant (Synthetic-Data Ban Preserved)**:
  - `WAITING_FOR_FEED` must **NEVER** instantiate `SyntheticChainGenerator`, Black-Scholes pricing, or generated candles (`synthetic: enabled: false` in `config/data_sources.yaml` stays 100% enforced).
  - Instead, attach an explicit **`DisconnectedQuoteProvider` (Frozen-Mark Stub)** while unauthenticated: `get_quote()` returns unavailable (`mark_stale = True`), open structures strictly display their **frozen yesterday/`last_mark_price`** (never a fabricated mark), and any new entry attempt fails closed with reason `WAITING_FOR_FEED`.
- **Design**:
  1. When `PortfolioManager` restores or spawns a runner whose `source` (`mstock` / `upstox` / `dhan`) or `mode="live"` broker is currently unauthenticated, **do not fail construction into `_parked_snapshots`** (reserve `_parked_snapshots` strictly for corrupt/deleted strategy names).
  2. Construct the `StrategyRunner` + restore its `portfolio` and `options_bridge` book against `DisconnectedQuoteProvider` so all open structures, strikes, lots, frozen yesterday P&L, and manual SL/TP levels are **immediately visible in `/portfolio/paper` and `/portfolio/live`**.
  3. Set an explicit feed-readiness state on the runner (`feed_state = "WAITING_FOR_FEED"`, surfaced in `get_state()`) with a clear UI status pill (`Waiting for mStock login · Frozen mark`).

#### Ticket A2 (`Sprint A-1`) — Automatic `on_broker_authenticated(broker_name)` Hook on Login / TOTP Verify
- **Files**: `src/backtest/brokers/session_manager.py`, `src/backtest/api/broker_auth.py`, `src/backtest/forward/portfolio_manager.py`
- **Design**:
  1. Register a lightweight post-auth callback in `BrokerSessionManager.verify_totp()` (and session-restore on boot when `remembered: true`) that notifies `PortfolioManager.on_broker_authenticated(broker_name)`.
  2. Inside `on_broker_authenticated(broker_name)`:
     - Unpark any legacy `_parked_snapshots` belonging to `broker_name`.
     - Atomically hot-swap `DisconnectedQuoteProvider` → `BidAskQuoteProvider(LiveChainProvider(broker))` + live execution gateway + shared bar/WS feed subscriptions onto every runner using `broker_name`, flipping `feed_state = "LIVE"`.
     - Trigger an immediate non-blocking `refresh_option_marks(force=True)` + `_evaluate_risk()` pass so Today's P&L (`Live Mark − Yesterday Close`) updates within `1–2s` of TOTP submit — **zero server restart required**.

#### Ticket A4 (`Sprint A-1`) — Active-Market-Window `T − 10 min` All-Channel Paging, Auto-Popup Login Modal & Persistent `"Broker login needed"` Topbar Banner
- **Institutional EMS Connectivity Standard ("Market-Hours-Only Operator Paging")**:
  - **Strict Active Market Window (`session_state in {"PRE_OPEN", "OPEN"}` = `09:00–15:30 IST` on NSE `TRADING_DAY`s only, via `src/backtest/live/market_status.py`)**:
    - Institutional desks **never** page operators or throw login popups after `15:30 IST`, overnight, on weekends, or on NSE holidays when live market feeds are idle and tokens naturally expire.
    - Outside `09:00–15:30 IST` (`CLOSED_TODAY`, `CLOSED_WEEKEND`, `CLOSED_HOLIDAY`): **zero outbound alerts**, **zero auto-popup login modals** (so evening backtesting/research/diary review is never interrupted), and a calm/neutral topbar chip.
    - At **`09:00 IST` (`PRE_OPEN`) on the next `TRADING_DAY`**: if `mstock` is unauthenticated while active `mstock` runners exist, the window opens cleanly — firing the morning reminder once, showing **`🔴 Broker login needed`**, and opening the login popup ahead of the `09:15 IST` bell.
  - **3-Stage Escalation Ladder (Active ONLY inside `09:00–15:30 IST` on `TRADING_DAY`)**:
    1. **Soft Heads-Up (`T − 30 min`)**: Topbar badge turns Amber (`🟡 Expiring soon`).
    2. **Urgent Action Gate (`T − 10 min`)**: Broadcasts across **every configured alert channel** (Telegram, Email, Slack, Discord, SMS, Desktop, In-App Alert Widget — exempt from rate limits) **AND automatically pops open the Login/TOTP Modal** on screen with a live countdown so the operator re-authenticates seamlessly *before* a single tick is dropped.
    3. **Hard Expiry (`T = 0` or `401`) / Unauthenticated Active Book**: Fires a `CRITICAL` all-channel alert, **immediately pops open the Login Modal** on screen, and pins a high-visibility **persistent red topbar CTA (`🔴 Broker login needed`)** across all workspace pages until `verify-totp` succeeds (even if the operator dismisses `[×]` the modal temporarily).
- **Files**: `src/backtest/brokers/session_manager.py`, `src/backtest/brokers/mstock.py`, `src/backtest/live/market_status.py`, `config/alerts.yaml`, `src/backtest/web/static/js/broker_status.js`, `src/backtest/web/static/js/broker_auth_modal.js`, `src/backtest/web/templates/base.html`
- **Design (Closing Current Code Gaps)**:
  1. Tighten `BrokerSessionManager` monitor cadence from `300s` (5 min) to **`30s`** so the `T − 10 min` boundary and `09:00 IST` pre-open gate are never missed.
  2. Gate all session-expiry / login-needed outbound alerts (`_raise_session_alert`) on **`get_market_day_state()["session_state"] in ("PRE_OPEN", "OPEN")`** (`09:00–15:30 IST` on `TRADING_DAY`). Outside that window, silently clear expired tokens without firing Telegram/Email/toast noise.
  3. Add an explicit **`expiring_10m` (`broker_session_expiring_urgent`)** transition alongside `expired` (`broker_session_expired`), and update `config/alerts.yaml` routing so both fan out to **all configured outbound channels** (`[telegram, email, slack, discord, sms, desktop, log]`, rate-limit bypass enabled).
  4. Update `broker_status.js` + `broker_auth_modal.js` (gated on `#market-status-chip` / `session_state in {"PRE_OPEN", "OPEN"}`):
     - Inside `09:00–15:30 IST` on a `TRADING_DAY`:
       - At **`T <= 10 min`** (once per session cycle): **auto-open `BrokerAuthUI.open("mstock")`**.
       - On **`expired`** transition (or page load / `09:00 IST` transition when `unauthenticated`/`expired` while active `mstock` runners exist): **auto-open `BrokerAuthUI.open("mstock")`** immediately and pin **`🔴 Broker login needed`** in the topbar (`🟡 Re-login needed (<10m)` inside 10 min).
     - Outside `09:00–15:30 IST`, weekends, and NSE holidays: suppress all auto-popups and red `"Broker login needed"` banners (manual click on `Brokers` still works anytime).

#### Ticket A3 (`Sprint A-2` — Isolated Sub-PR After `A-1` Live Session Bake) — Open-Trade Continuity Rule & No-Stale-Signal Entry Guard
- **Files**: `src/backtest/forward/state_store.py`, `src/backtest/forward/paper_runner.py`
- **Design**:
  1. Helper `runner.has_open_exposure() -> bool`: returns `True` when `len(runner.portfolio.positions) > 0` or `runner.options_bridge.has_open_structure()` (or working live venue orders exist).
  2. **Restore & Auto-Wake Status Rule**:
     - If `runner.has_open_exposure()` is `True` **and** bucket/runner is **not** risk-halted (`BLOCKED_BY_RISK` / circuit breaker):
       - Restore/wake directly into **`STATUS_RUNNING`** (even if `strategy_code_changed` is flagged — log a warning badge `CODE_UPDATED_WHILE_IN_TRADE` and block *new* entries after exit if desired, **never** abandon protection of the currently open trade!).
       - On the very first live quote/tick after login (e.g. `09:45 IST`), evaluate SL / Target / Expiry exits immediately so a morning gap breach exits cleanly at `09:45 IST` market price.
     - If `runner.has_open_exposure()` is `False` (flat book):
       - On unexpected process restart: restore as `STATUS_PAUSED` (`pause_reason="RESTART_FLAT_SAFETY"`) unless operator clicks `Resume All`.
       - Stamp `armed_at_utc = now_utc()` whenever a runner transitions to `RUNNING` or attaches to feed mid-session; ignore entry intents on historical warmup bars with `bar_close_utc < armed_at_utc` (prevents chasing a stale `09:16 IST` signal at `09:45 IST`).

---

### Sprint B (P1 — Institutional RMS Risk Integrity): Cash-Flow-Neutral Session High-Water Mark & Restart-Proof Halt Latches
**Goal**: Match Tier-1 institutional Risk Management System (RMS) behavior on intraday restarts without ever resurrecting cross-day ghost anchors or triggering false drawdown trips when runners are added/deleted.

#### Institutional RMS Standard Adopted
1. **Separate Intraday Session Peak vs. Lifetime HWM**: Intraday drawdown breakers track *today's* IST session peak (`09:15–15:30 IST`), while multi-week/month cumulative High-Water Mark lives in `/diary` and `/analytics`.
2. **Restart-Proof Risk State**: Restarting the server mid-day must **never** erase morning peak drawdown or unlock an active `🔴 HALTED` circuit breaker.
3. **Cash-Flow Neutrality (`session_peak_daily_pnl`)**: Anchor today's high-water mark on **pure session Net P&L (`max(0, max_today(daily_pnl))`)** rather than raw rupee `equity`. If an operator deletes 3 runners (`−₹3L` capital) or spawns 2 runners (`+₹2L` capital) while the server is down, `effective_peak_equity = current_allocated_capital + prev_close_pnl_offset + session_peak_daily_pnl` — **zero fake drawdown spikes, zero forgotten trading losses**.
4. **Session Lockout vs. Next-Day Roll**: An auto-tripped breaker stays locked (`halted = True`) across all restarts for the rest of `today_ist` until an explicit operator **Risk Override / Reset Breaker** action, and rolls clean automatically on the next IST session date (`saved_session_date_ist < today_ist`).

#### Ticket B1 — Persist `(session_date_ist, session_peak_daily_pnl, halted)` Across Same-Day Restarts
- **Files**: `src/backtest/forward/portfolio_manager.py`, `tests/forward/test_state_persistence.py`
- **Design**:
  1. Stamp `session_date_ist = current_session_date().isoformat()` plus `session_peak_daily_pnl` (`>= 0.0`) and `halted` / `halt_reason` / `halt_mode` / `halted_ts` into `manager`, `buckets[mode]`, and `segment:*` inside `portfolio_state.json`.
  2. Recompute effective peak dynamically from cash-flow-neutral P&L (`peak_equity = baseline_session_open_equity + session_peak_daily_pnl`), so adding/removing runners mid-day or offline never distorts drawdown %.
  3. On `_apply_state_payload()` boot:
     - **If `saved_session_date_ist == today_ist` (Same-Day Restart)**:
       - Restore `session_peak_daily_pnl` and active `halted` / `_bucket_halted[mode]` latches verbatim! Mid-day restarts preserve exact drawdown % and cannot bypass a tripped `-₹2.5L` or Max-DD fuse.
       - Fix the boot log at `portfolio_manager.py:2687` so it truthfully reports `"same-day session risk peak & breaker latches restored (session=%s)"`.
     - **If `saved_session_date_ist != today_ist` (New Trading Day)**:
       - Reset `session_peak_daily_pnl = 0.0` and `halted = False` cleanly for the new IST session (preventing cross-day ghost halts), and make `portfolio_manager.py:2687` log `"prior-session (%s) risk peak & breaker latches rolled clean for new IST session (%s)"` instead of falsely claiming breaker latches were restored.
  4. Replace the stale Sept-23 `test_bucket_anchors_are_session_scoped` with three explicit RMS contract tests:
     - `test_prior_day_anchors_and_halts_reset_on_new_ist_session`
     - `test_same_day_restart_preserves_peak_drawdown_and_halt_latch`
     - `test_offline_runner_removal_does_not_trigger_false_drawdown_on_restart`

---

### Sprint C (P2 — Institutional Clock Law, mStock Layer-0 Auth Gate & Test Debt): Clean Board
**Goal**: Lock the Two-Layer Clock Law (`UTC` engine / `IST` display), enforce a strict zero-packet Layer-0 Auth Gate on mStock when logged out (**keeping Upstox 100% parked & untouched**), and turn legacy test debt green.

#### Ticket C1 (P2-A — Locked) — Institutional Two-Layer Clock Law in `OrderLedger` (`#23`)
- **Institutional OMS Standard**: Engine, memory, and database store **100% canonical UTC (`+00:00` / `Z`)** so sorting, latency math, and hold-time queries can never shift by 5h 30m; conversion to **`Asia/Kolkata` (`IST`)** happens strictly at the UI / Diary display boundary.
- **Files**: `src/backtest/forward/paper_runner.py`, `tests/forward/test_order_ledger_clock.py`
- **Design**:
  - Pass `ts` in `OrderLedger.submit()`, `apply_fill()`, `record_filled()`, and `cancel()` through a single helper `_ledger_iso_utc(ts)` backed by `src/backtest/timekeeping.py` (`iso_utc(from_market_clock(ts))` / `iso_utc(now_utc())`).
  - Ensures lexicographical sorting on `created_ts` / `filled_ts` / `updated_ts` is 100% chronological across equity and option fills while UI formatters render IST (`+05:30`).

#### Ticket C2 (P2-B — Locked Scope: mStock Only, Upstox Parked) — Layer-0 mStock Auth Gate & MTM Test Mock Fix
- **Institutional Market-Data Standard**:
  1. **Zero Packets Before Login**: If `SessionManager.is_authenticated("mstock")` is `False`, background pollers and quote providers send **`0` HTTP packets**, **`0` retries**, and **never** read stale `.mstock_session_token` files or trigger CLI `connect/login` calls from server threads.
  2. **Instant `401/403` Kill-Latch**: On the very first HTTP `401/403` token rejection mid-session, trip `SessionManager` to `expired` immediately (triggering Ticket A4's all-channel alert + UI login popup + `"Broker login needed"` topbar banner) instead of hammering mStock across remaining symbols.
  3. **Upstox Parked**: Zero code paths cross from mStock to Upstox; leave `upstox.py` completely untouched while parked.
- **Files**: `src/backtest/forward/feed_registry.py`, `src/backtest/data/mstock_live_feed.py`, `src/backtest/brokers/session_manager.py`, `tests/forward/test_options_forward_mtm.py`
- **Design**:
  - Gate `MStockBarFeed._poll_once()` and `MStockLiveFeed._credentials()` strictly on `get_session_manager().get_active_session_token("mstock")` in server/daemon mode (never falling through to `.mstock_session_token` or interactive `auth.login()` from background threads).
  - On HTTP `401/403` from mStock TypeA endpoints, immediately mark the mStock session `expired` on `BrokerSessionManager` so subsequent symbol polls abort in `0 ms` and Ticket A4 notifies the operator.
  - Update the 6-line fake `_CountingBroker` in `tests/forward/test_options_forward_mtm.py::test_manual_refresh_marks_endpoint_and_rate_limit` to mirror `MStockBroker`'s numeric-key memory check (`0` HTTP calls for numeric WS probe, `1` HTTP call for trading symbol).

#### Ticket C3 — Stale Test-Suite Signatures & Historical Timestamp Repair Cutover
- **Files**: `tests/forward/test_options_forward_mtm.py`, `tests/test_paper_trading_validation.py`, `tests/options/test_options_exit_policy.py`, `scripts/repair_trade_timestamps.py`
- **Design**:
  - Align stale test kwargs (`preserve_candle_timestamps`, `SlippageCalculator` signature, and test capital/risk caps vs `₹25k` per-trade ceiling) without weakening production risk limits.
  - Run `scripts/repair_trade_timestamps.py` against a backed-up DB snapshot first, verify `Gross − Commission = Net` and row count (`2,159` rows) invariant, then execute `--apply` during a post-market maintenance window.

---

### Sprint D (P3 — Institutional PMS Architecture & Operator UX): PostgreSQL Single-Source-of-Truth, Exact P&L Engine & Config-Drift Rollover
**Goal**: Eliminate the `portfolio_state.json` split-brain so PostgreSQL is the sole durable source of truth while RAM serves sub-second live math at zero tick-level DB load, unify Equity + Option open positions across SQL and UI, and make frozen runner rulebooks explicit and 1-click upgradable when flat.

#### Ticket D1 — Replace `portfolio_state.json` Split-Brain with PostgreSQL Single-Source-of-Truth (`#24` + Institutional Ledger)
- **Institutional PMS Standard**:
  - **PostgreSQL Is the Sole System of Record; RAM Is the Real-Time Calculator**: Never rely on a local flat JSON file for open trades, runner configs, or daily P&L anchors. Hydrate RAM 100% from PostgreSQL on startup (run a brief dual-write parity window, then retire `portfolio_state.json` dependency).
  - **The 7-Event Database Touch Contract (Zero Tick / Poll DB Load)**:
    1. **Server Boot (`READ` once)**: Hydrate active/paused `portfolios`, open Equity + Option `positions` (with `entry_price` & `prev_close_mark`), and today's `risk_session_state` into RAM via the shared SQLAlchemy pool.
    2. **Broker Login `verify-totp` (`READ/WRITE` once)**: For `mode="live"`, reconcile `mStock.get_positions()` against DB `positions` and raise a loud mismatch alert if external/phone-app trades occurred; record first live mark.
    3. **Order / Fill Entry or Exit (`WRITE` atomic tx)**: Persist `orders` + `fills` + open/update/close `positions` (both Equity and Option structures/legs tagged `instrument_type = 'EQ' | 'OPT'` without mixing options into equity share-averaging) + closed `trades`.
    4. **Operator UI Action (`WRITE`)**: Spawn, Pause, Resume, Delete, manual SL/TP edit, Flatten, or Risk Override.
    5. **Risk Milestone (`WRITE` throttled/immediate)**: Throttled update on new `session_peak_daily_pnl`; immediate write on `halted = True`.
    6. **60s Crash Checkpoint & `15:30 IST` EOD Settlement (`WRITE` 1 batch query/min during `09:15–15:30 IST`)**: Flush `last_mark_price` once per minute in one batch SQL statement; at `15:30 IST` stamp official `prev_close_mark` on all open carry positions.
    7. **Diary / Analytics / Reports Views (`READ`)**: Read via shared DB pool (`POOL-3` compliant — zero ad-hoc `create_engine`).
    - *Explicit Non-Touch Rule*: Live WebSocket ticks (`10–100/s`), non-trading 1-minute candle evaluations, and `/api/portfolio` SSE/HTTP UI refreshes run **100% in RAM (`0` DB queries)**.
  - **Canonical P&L Formulas (Zero JSON Anchors Required)**:
    - **Individual Trade P&L (Entry → Now / Exit)**:
      - *Open*: `(Live Mark − Entry Price) × Signed Qty − Entry Commission`
      - *Closed (`trades`)*: `Gross P&L − Total Commission = Net P&L` (single combined `Commission = entry_fee + exit_fee`).
    - **Day's P&L (`09:15–15:30 IST` Session Slice)**:
      - Anchor = `Entry Price` (if opened today) **or** `prev_close_mark` from `15:30 IST` DB settlement (if carried from a prior day; flat morning book = `₹0.00`).
      - `Day P&L = Sum(Today's Closed Slices) + Sum((Live Mark − Anchor) × Signed Qty − Today's Fee Slice)` — guaranteeing `Sum of Daily P&L Slices == Lifetime Individual Trade Net P&L` to the paisa.
  - **3-Layer P&L Update Cadence**:
    1. *Engine & Risk Fuse (`RAM`)*: Every coalesced WebSocket tick (`~100–250 ms`) + 1-min bar close (`30s` HTTP repair watchdog fallback if a leg misses WS ticks).
    2. *Operator Browser UI (`SSE → RAM`)*: Every `1–2s` via SSE push (`5s` HTTP fallback; idle outside market hours).
    3. *PostgreSQL Checkpoint (`Batch SQL`)*: Once every `60s` (`09:15–15:30 IST`) + on fills + at `15:30 IST` close.
  - **Repository Schema Constraints & Migration Safety Rules (Mandatory)**:
    1. **Migration Chain (`db/migrations/024_*.sql` + `024_*.sqlite.sql`)**: Current migration head is `db/migrations/023_diary_journal_notes.sql` (dual Postgres `.sql` + SQLite `.sqlite.sql` test-parity files — **not** Alembic). Sprint D starts at `024_`.
    2. **Append-Only PostgreSQL Views (`014`/`015` Lesson)**: Never reorder, rename, or drop existing columns in `CREATE OR REPLACE VIEW` statements (Postgres rejects in-place column shifts on fresh migration chains). Keep new state in **additive tables** (`option_open_structures`, `option_open_legs`, `risk_session_state`) or strictly additive nullable columns on `positions` — never mutating existing view column order.
    3. **TimescaleDB Hypertable Non-Interference**: `market_data_cache` is a TimescaleDB hypertable where cross-chunk key `UPDATE`s fail and writes use `ON CONFLICT DO NOTHING`. Sprint D **never** touches `market_data_cache`; `last_mark_price` and `prev_close_mark` live strictly on relational `positions` / `option_open_structures` tables.
  - **Institutional 3-Phase Dual-Write Parity & Zero-Downtime Rollback Plan**:
    - **Phase D1-a — Shadow Dual-Write (`STATE_STORE_PRIMARY=json`, 5 Live Sessions)**: `portfolio_state.json` remains the primary boot reader. All 7 state-change events dual-write to PostgreSQL (`024_*`) inside fail-soft wrappers. An automated parity verifier runs on every boot and `15:30 IST` close comparing JSON vs. PG (`runner count`, `open structures/legs`, `prev_close_mark`, `daily_pnl` within `₹0.01`) and logs `PARITY_OK` / `PARITY_DIFF`.
    - **Phase D1-b — Cutover with Hot-Standby Rollback (`STATE_STORE_PRIMARY=postgres`, 5 Live Sessions)**: Flip primary boot hydration to PostgreSQL while continuing to write `portfolio_state.json` as hot standby. **Rollback SLA (< 10 seconds, zero SQL rollback)**: flipping `STATE_STORE_PRIMARY=json` in `.env` immediately reverts boot authority to JSON without dropping a single table or row.
    - **Phase D1-c — Retire JSON Dependency**: Decommission `portfolio_state.json` writes only after **5 consecutive trading sessions under `STATE_STORE_PRIMARY=postgres` with `0` `PARITY_DIFF` events**.
- **Files**: `db/migrations/024_open_option_positions_and_risk_session.sql`, `db/migrations/024_open_option_positions_and_risk_session.sqlite.sql`, `src/backtest/db/models.py`, `src/backtest/forward/trade_persistence.py`, `src/backtest/forward/state_store.py`, `src/backtest/forward/portfolio_manager.py`

#### Ticket D2 — Unified `Positions` Blotter & Carry-Horizon Badges (`#24` UI)
- **Files**: `src/backtest/web/static/js/portfolio.js`, `src/backtest/web/templates/portfolio.html`
- **Design**:
  - With P0 (`Ticket A1`) keeping `WAITING_FOR_FEED` runners in `self._runners`, `list_positions()` automatically surfaces every open Option structure alongside Equity positions even before broker login.
  - Add an explicit carry-horizon badge (**`⏱ INTRADAY 15:10`** for `carry_policy="intraday"` vs **`📅 POSITIONAL (HOLD TO EXPIRY)`** for `carry_policy="positional"`) plus `prev_close_mark` / Today vs. Total Trade P&L clarity on each open structure row in the `Positions` tab.

#### Ticket D3 — `FROZEN_AT_SPAWN` Config-Drift Badge & 1-Click Flat Rollover (`#3`)
- **Institutional Desk Standard**: Never silently mutate exit/carry rules mid-trade, always display active carry policy + template-drift warnings on the blotter, and allow 1-click rollover when flat.
- **Files**: `src/backtest/forward/paper_runner.py`, `src/backtest/api/portfolio.py`, `src/backtest/web/static/js/portfolio.js`, `src/backtest/web/templates/portfolio.html`
- **Design**:
  1. Display active `carry_policy` badge (`INTRADAY 15:10` for `"intraday"` vs `POSITIONAL` for `"positional"`, matching `CARRY_POLICIES` in `options_bridge.py:147`) on every Option runner row.
  2. Show an amber **`⚠️ Outdated config — respawn when flat`** pill whenever a runner's frozen spawn snapshot differs from the current strategy template on disk (`strategy_code_changed` or `expression` / `carry_policy` defaults drift).
  3. Add a **1-click `"Respawn with latest rules"`** action (enabled strictly when `open_positions == 0`) that retires the flat runner and spawns its replacement from the latest template defaults with zero manual re-typing.

---

## 4. Non-Negotiable Guardrails During Implementation

1. **Protected WebSocket & Strategy Files**: Do **not** modify `src/backtest/data/mticker.py` or `tests/test_mticker_ws_fixes.py`. Keep `config/risk_limits.yaml` `entitlement.live` strictly `[long_call, long_put]`.
2. **Single Clock Authority (`src/backtest/timekeeping.py`)**: All IST session dates (`EXCHANGE_TZ`) and UTC conversions must use `src/backtest/timekeeping.py`. `pytest tests/test_timekeeping_policy.py` must stay 100% green without raising ratchet baselines.
3. **PnL Diary & E2 Ledger Invariant**: Preserve strict `paper` vs `live` book isolation and single combined `Commission` (`Gross − Commission = Net` to `2dp`).
4. **Verification Gate per Sprint**:
   - `PYTHONPATH=src pytest tests/forward/ tests/diary/ tests/test_workspace_design.py tests/test_timekeeping_policy.py`
   - `node --test tests/js/test_*.mjs`
   - `python3 -m flake8 --max-line-length=100 <touched_files>` (`0` warnings)
