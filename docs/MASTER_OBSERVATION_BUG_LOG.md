# Master Observation & Bug Log — `feature/options-trading-dev`

**Date**: 2026-11-23  
**Branch**: `feature/options-trading-dev`  
**Purpose**: Single source of truth for every open bug, known defect, and operator observation.  
**Convention**: `BUG-NN` IDs are preserved exactly as they appear in source code comments. New observations are prefixed `OBS-`.

---

## Open Bugs (from source-code `BUG-NN` references)

| ID | Summary | Evidence / Location | Status |
|----|---------|---------------------|--------|
| **BUG-01** | One entry per timeframe bucket — an "1hour" runner entered 205 times instead of once per bar. Tick-cadence entries latched. | `tests/invariants/test_trading_system_invariants.py:6`; `src/backtest/forward/paper_runner.py:1616`; `src/backtest/forward/warmup.py:213`; `src/backtest/forward/options_bridge.py:506` | Open |
| **BUG-02** | Fee-floor / bracket inside fees: old ±₹100 strangle bracket sat inside the ₹193 round-trip fee so every "win" was a net loss. Entry budget is per session day, not per signal. Declared take-profit must clear 1.2× its own fees. Re-entry cooldown is a market-clock rule. | `plugins/strategies/immediate_strangle.py:10,149`; `src/backtest/forward/options_bridge.py:515,672,707,1042,2427,3038,3111`; `src/backtest/options/exit_policy.py:166`; `src/backtest/forward/paper_runner.py:1616`; `tests/forward/test_reentry_budget_and_fee_floor.py:107`; `tests/forward/test_phase3_subsecond_options_and_ticks.py:332`; `tests/test_phase2_rewrites.py:161` | Open |
| **BUG-03** | Capital ledger accumulator — added to on spawn and subtracted on close, not a single source of truth. | `src/backtest/forward/portfolio_manager.py:392`; `tests/forward/test_capital_ledger_identity.py:1` | Open |
| **BUG-04** | One NET P&L contract on every row/snapshot — `realized_pnl` is the NET half; gross P&L lives on legs. Day anchors roll at 00:00 IST. | `tests/forward/test_net_pnl_contract.py:1`; `tests/test_options_web.py:199`; `src/backtest/forward/paper_runner.py:1825,4507,4603`; `src/backtest/forward/portfolio_manager.py:470`; `src/backtest/web/options_api.py:561` (marked B9) | Open |
| **BUG-06** | Every option fill must land on the ₹0.05 exchange price grid — 733 of 910 booked prices were off-grid. | `tests/test_option_fill_grid.py:1`; `tests/invariants/test_trading_system_invariants.py:8`; `src/backtest/options/paper_trading.py:611,652,808,853,1068,1425` | Open |
| **BUG-15** | Deterministic pre-trade refusals — same-code same-contract repeat is throttled by a rejection streak; a stuck runner says so instead of spinning at 🟢 RUNNING. | `src/backtest/forward/options_bridge.py:164,526,2916,3049,3545`; `src/backtest/forward/paper_runner.py:3566,3642`; `tests/forward/test_reentry_budget_and_fee_floor.py:369` | Open |
| **BUG-16** | A tick entry passes `entry_dt`. | `src/backtest/forward/options_bridge.py:2891` | Open |
| **BUG-18** | No entry outside the session window — a bar stamped 15:30:30 IST opened a fresh structure. Overnight gap must not be booked as this session's P&L. | `tests/invariants/test_trading_system_invariants.py:16`; `tests/forward/test_carry_policy_and_eod_sweep.py:10`; `src/backtest/forward/paper_runner.py:2737,4167,4236,4536,4611`; `src/backtest/forward/options_bridge.py:513,678`; `src/backtest/timekeeping.py:441`; `src/backtest/options/paper_trading.py:401,463,486,1543,1620` | Open |
| **BUG-26** | Fee-honest round trips — every fill carries its real cost stack; equity paper round trip pays its own costs. Cost split: what broker kept vs total_fees. | `tests/invariants/test_trading_system_invariants.py:10`; `tests/forward/test_equity_paper_costs.py:1`; `tests/forward/test_trade_persistence.py:108`; `tests/forward/test_ledger_persistence.py:456`; `src/backtest/forward/paper_runner.py:281,332,376,700,790,1087,1173`; `src/backtest/forward/trade_persistence.py:573`; `src/backtest/simulator/fill.py:258`; `src/backtest/simulator/position.py:137`; `src/backtest/simulator/portfolio.py:930`; `src/backtest/forward/portfolio_manager.py:1799` | Open |
| **BUG-33** | Capital flows must not read as trading — `daily_pnl = equity - day_start` only measures trading while capital is static; moves with capital flow. | `src/backtest/forward/portfolio_manager.py:413`; `tests/forward/test_capital_flow_anchors.py:1`; `tests/test_breaker_independence.py:243`; `tests/test_cross_broker_risk.py:204` | Open |
| **BUG-37** | Retire orphan portfolio rows — ledger keeps 'active' rows for books no runner owns (capital booked against them). One-shot reap script exists. | `src/backtest/forward/portfolio_reaper.py:1`; `scripts/reap_orphan_portfolios.py:1`; `src/backtest/forward/portfolio_manager.py:343`; `src/backtest/db/config.py:271`; `tests/forward/test_portfolio_ledger_reaper.py:1`; `tests/forward/test_signal_portfolio_placeholder.py:1` | Open |
| **BUG-38** | Alembic migration chain fork — revision 020 duplicated (026 head vs 023). Unforked; guardrail enforces single linear head. | `db/alembic/versions/20261013_1200_021_add_upstox_source_to_portfolios.py:12`; `db/alembic/versions/20261008_1200_020_option_intent_journal.py:16`; `tests/db/test_migration_022_trades_dedupe.py:12`; `tests/db/test_migrations_009_013.py:225`; `tests/db/test_migrations_020.py:9,331` | Open |
| **BUG-39** | Intraday option book must square off on session clock — three monthly legs rode into next session. Carry policy must be declared/visible. Wall-clock square-off, no tick required. | `tests/invariants/test_trading_system_invariants.py:13`; `tests/forward/test_carry_policy_and_eod_sweep.py:6`; `src/backtest/forward/options_bridge.py:143,267,426,1045,1740`; `src/backtest/forward/paper_runner.py:673,3785`; `src/backtest/forward/portfolio_manager.py:357,2190,2218`; `src/backtest/timekeeping.py:416` | Open |

> **Bug docs**: `docs/BUG-FIX-PLAN-2026-10-28.md` (plan), `docs/QA-LIVE-PAPER-BUG-REPORT-2026-10-28.md` (QA report, BUG-02 / BUG-15).

---

## Operator Observations (from live testing)

| ID | Observation | Evidence / Context | Open Questions |
|----|-------------|-------------------|----------------|
| **OBS-01** | Segment paper-mode blocks live trades | `config/segments.yaml:29` `options_index` mode: `paper`; `:39` `equity_intraday` mode: `paper`. Error message: `"segment 'options_index' is configured as paper — a live runner cannot be created into it"` (and same for `equity_intraday`). Both segments have comment: `"start paper, flip to live when ready"`. | Flip `mode: paper` → `mode: live` in `config/segments.yaml` for the segment(s) intended for live trading, then restart. Short-premium structures (strangle/short_straddle) are still withheld from `live` entitlement in `config/risk_limits.yaml:54-56` regardless of segment mode. |
| **OBS-02** | Strangle instance shows P&L of ₹4,240 with zero open positions | User opened one strangle instance. Runner took **no open position** (no legs booked), yet still shows a P&L of ₹4,240. Entry is unconditional (fires on first bar after deploy), so a trade was expected after warmup. | (a) Where is ₹4,240 coming from if no legs were ever booked? Possible causes: fees/commission charged without a trade, mark-to-market on a phantom position, carried-over P&L from restored state (`portfolio_state.json`), or a P&L calculation bug (BUG-04 NET contract mismatch). (b) Why did the unconditional strangle not enter after warmup? Possible causes: segment is paper-mode (OBS-01), broker not authenticated, IV filter suppressed entry, gamma/regime alert paused entries, or `min_days_to_expiry` / carry policy blocked it. Needs runner logs and `/api/portfolio/runners/<id>` state inspection. |

---

## Rectification Work Completed (this session, committed `9eae6d2`)

| File | Change |
|------|--------|
| `plugins/strategies/immediate_strangle.py` | Parameterized all exit values; `exit_unit` (points/rupee); trailing stop params; `squareoff_minutes_before_close`; IV percentile params; portfolio alert hooks (`on_alert`/`on_alert_resolved`). |
| `plugins/strategies/strangle_wing_points.py` | Same rectification pattern; wing stated in index points. |
| `plugins/strategies/straddle_short_atm.py` | Same rectification pattern; fixed `parameters` field (was string, now proper description). |
| `src/backtest/strategy/templates/__init__.py` | Added `on_alert`/`on_alert_resolved` no-op stubs to `StructureStrategy`; added `AlertType` import. |
| `templates/option_strategy_template.py` | Added selling-strategy params skeleton (exit_unit, stop/target points/rupee, trailing, squareoff, IV filter, alert_response). |
| `templates/strategy_template.py` | Same selling-strategy params added. |
| `docs/STRATEGY_CATALOGUE.md` | Full catalogue of built-ins (11) + plugins (13). |
| `docs/STRATEGY_RECTIFICATION_FINDINGS.md` | Reference document for rectifying strategies and updating templates. |

**Verification**: `discover_plugins(force=True)` registers all three plugins; `tests/test_strategy_conformance.py` (22/22), `tests/test_strategy_execution.py` + `test_strategy_base.py` + `test_strategy_bundle.py` + `test_strategy_adapter.py` (86/86) pass.

---

## Live Environment (as of 2026-11-23 04:23 IST)

| Component | Status |
|-----------|--------|
| App | `http://127.0.0.1:5000/` — HTTP 200, source=mstock |
| mStock session | authenticated, expires 2026-11-23 15:53 IST |
| Upstox session | authenticated (UI active), expires 2026-11-24 03:30 IST |
| Both broker feeds | available simultaneously for cross-broker feed integrity comparison |
| Segment mode | both `paper` (see OBS-01) |
| Short structures on live | withheld by `config/risk_limits.yaml` (strangle, short_straddle not in `live` entitlement) |

**Feed integrity tool**: `tools/feed_reconcile.py` (read-only — `probe` / `bars` / `chain` subcommands). See `docs/FEED-SOURCE-UPSTOX-RECONCILIATION-2026-11-06.md`.
