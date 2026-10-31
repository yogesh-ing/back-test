# PR #49 Merge Conflict Audit & Resolution Plan

- **PR**: [#49 — `feat(diary): PnL Diary & Institutional Edge Suite (Slices 0–5, E1–E3, Alembic 021/022)`](https://github.com/yogesh-ing/back-test/pull/49)
- **Head Branch (`OURS`)**: `arena/f7407685-back-test` (`0545c89`, 7 commits ahead of `80c2246`)
- **Base Branch (`THEIRS`)**: `feature/options-trading-dev` (`3d5a073`, 19 commits ahead of `80c2246` from **Sprint 2 / PR #48**)
- **Common Ancestor (`merge-base`)**: `80c2246d2231bd1abe4fb34e8ad3566b75577307`
- **Status**: **Read-only audit complete — awaiting your validation before resolving.**

---

## 1. Root Cause of Conflicts

Both branches diverged from `80c2246` and independently addressed overlapping ledger/migration items while building complementary layers:

- **`THEIRS` (`feature/options-trading-dev` — Sprint 2 / PR #48)**: Built the **core execution & accounting ledger fixes**:
  - `BUG-04` / `B5`: Unified **NET P&L contract** across `StrategyRunner`, `PaperRunner`, `PortfolioManager`, and API read surfaces (`realized_pnl` is now lifetime NET; added `gross_realized_pnl`, `total_fees_paid`, `realized_pnl_today`, `open_pnl`, `prev_close_open_pnl`).
  - `BUG-26` / `B7`: `bind_executor()` so restored runners after restart continue charging brokerage + statutory fees.
  - `B1` + `B3`: Durable SHA-256 `trades.dedupe_key` (`022_trades_dedupe_key`) + durable `orders`/`fills`/`positions` DB mirroring (`attach_order_ledger`, `_mirror_book_slice`).
  - `BUG-03` / `BUG-33` / `BUG-37` / `f4e77d8`: Capital-flow-adjusted day/peak anchors, session-move daily-loss fuses, scoped `dashboard_book` folding, and orphan portfolio reaper.
  - `BUG-38` / `B11`: Unforked duplicate Alembic `020` $\rightarrow$ `021` + added linear-head CI guardrail.

- **`OURS` (`arena/f7407685-back-test` — PR #49)**: Built the **6-slice PnL Diary & Institutional Edge Suite (`/diary`, `/api/diary/*`) + `E1`/`E2`/`E3` operator ledger UX**:
  - `Slice 0`: `E1` IST day scope + `E2` single `Commission` sum (`Gross − Commission = Net` at `2dp`, zero fee sub-component split) + Live-mode `portfolios.mode` stamping fix.
  - `Slices 1–5`: `/diary` workspace, `Paper | Live` selector, IST Session Calendar Heatmap, 3-level `Strategy → Spawned Instance → Trade/Structure` tree, `Exit-Reason` & `NSE IST Session Regime` matrices, `Hold-Duration & Commission-Drag` + `Payoff Quadrant`, 6-gate `Paper → Live Graduation Scorecard`, and `DiaryNote` persistent journal (`022_diary_journal_notes`).

---

## 2. Executive Decision Matrix (File-by-File)

`git merge-tree` reports **7 conflicting files** plus **2 auto-merged files with semantic overlap** (`models.py` and `test_migrations_009_013.py` / duplicate `022` migration ID).

| # | File / Surface | Better Version | Recommended Resolution | Why |
|---|---|---|---|---|
| 1 | `db/alembic/versions/*020*.py`, `*021*.py` & `tests/db/test_migrations_020.py` | **THEIRS (Sprint 2)** | Keep **THEIRS** verbatim | Sprint 2 adds `has_table("option_intent_events")` idempotence on `020` (safe for live `forward_test` stamped at `019`) + `test_alembic_has_single_linear_head` CI guardrail. |
| 2 | Migration `022` collision + `tests/db/test_migrations_009_013.py` | **BOTH (`022` + `023`)** | Keep Sprint 2's `022_trades_dedupe_key`; renumber PR #49's `diary_journal_notes` **`022` $\rightarrow$ `023`** (`down_revision = "022"`); set head test to `["023"]` | Prevents recreating a `['022', '022']` Alembic fork; preserves deployed `022` on base branch and chains `diary_notes` cleanly as `023`. |
| 3 | `src/backtest/db/models.py` *(auto-merged)* | **BOTH (Clean Union)** | Keep Sprint 2's `Trade.dedupe_key` + `uq_trades_dedupe_key` **and** PR #49's `DiaryNote` + `DIARY_TABLES` | Completely orthogonal tables/columns; zero schema conflict once `diary_notes` is migration `023`. |
| 4 | `src/backtest/forward/trade_persistence.py` | **THEIRS + 1 bugfix from OURS** | Keep **THEIRS** (`B1` `trade_dedupe_key` + `B3` order/fill/position mirroring + `_trade_values` split) **and graft OUR `_resolve_runner_mode(cfg)`** into `_ensure_portfolio_row()` | Sprint 2's `B1`+`B3` persistence is far stronger (fee-restatement-safe SHA-256 key + DB order ledger), **but** Sprint 2 still hardcodes `mode="paper"` in `_ensure_portfolio_row()`, which mis-tags Live runners as Paper in PostgreSQL. Grafting `_resolve_runner_mode` fixes that bug cleanly. |
| 5 | `src/backtest/forward/paper_runner.py` | **THEIRS + OUR additive breakdown** | Keep **100% of THEIRS** (`BUG-04` NET contract, `bind_executor` `BUG-26`, `B3` ledger sink, `daily_pnl()`, `realized_pnl_today`, `open_pnl()`) + keep **OUR** additive `daily_realized_breakdown()` (`daily_gross_pnl`, `daily_commission`) | Sprint 2's `daily_pnl()` properly captures overnight open-mark deltas (`prev_close_open_pnl`) and partial scale-outs on open tickets; our `daily_realized_breakdown()` adds today's single-day `Commission` & `Gross` needed by `E1`/`E2` UI subtitles without altering Sprint 2 math. |
| 6 | `src/backtest/forward/portfolio_manager.py` | **THEIRS + OUR 5 additive keys** | Keep **100% of THEIRS** (`BUG-03` dashboard scoping, `BUG-33` capital-flow anchors, session-move daily fuses, `BUG-37` reaper, `BUG-04` fields) + append **OUR** `session_date_ist`, `session_scope_label`, `daily_gross_pnl`, `daily_commission`, `daily_realized_net_pnl` | Preserves every Sprint 2 risk breaker and capital identity invariant while keeping IST session labels and today's single `Commission` sum on `/portfolio/<mode>`. |
| 7 | `src/backtest/api/portfolio.py` | **SYNTHESIS (Both)** | In `risk_board()`: keep **THEIRS** (`t["net_pnl"]`, `B5`). In `aggregated_trades()`: keep **OURS** (dual DB+RAM deduped ledger, `?scope=today\|all` defaulting to `"all"`, NET `total_pnl`, `Gross`/`Commission`/`Net` sums, mismatch flags) **+ add Sprint 2's `"per_runner_cap": MAX_TRADE_LOG`** | Satisfies Sprint 2's `test_net_pnl_contract.py` (`total_pnl == net`, `per_runner_cap == MAX_TRADE_LOG`) **and** `E1`/`E2`/`E3` (persisted history across restarts, IST `today` filter, single `Commission` sum). |
| 8 | `src/backtest/web/static/js/components/risk_board.js` | **OURS** | Keep **OURS** (11-column `Gross \| Commission \| Net P&L` `2dp` renderer + `#agg-trade-scope` selector) | Matches the 11-column header in `_portfolio_center.html` and enforces `E2` (`Gross − Commission = Net` visibly on every row). Taking Sprint 2's 9-cell row renderer would break column alignment against `_portfolio_center.html`. |

---

## 3. Detailed Hunk-by-Hunk Comparison

### 3.1 Alembic Chain & Migration `022` $\rightarrow$ `023`
- **What conflicted**:
  - Both branches renamed `20261013_1200_020_add_upstox_source_to_portfolios.py` $\rightarrow$ `20261013_1200_021_add_upstox_source_to_portfolios.py`.
  - Sprint 2 chained `20261031_1200_022_trades_dedupe_key.py` (`022` $\leftarrow$ `021`).
  - PR #49 chained `20261030_1200_022_diary_journal_notes.py` (`022` $\leftarrow$ `021`).
- **Why THEIRS + renumbering OURS to `023` wins**:
  - Sprint 2 is already on `feature/options-trading-dev`, so `022` is canonically `trades.dedupe_key`.
  - Renaming our new journal migration to:
    - `db/alembic/versions/20261030_1200_023_diary_journal_notes.py` (`revision = "023"`, `down_revision = "022"`),
    - `db/migrations/023_diary_journal_notes.sql` (`VALUES ('023', ...)`),
    - `db/migrations/023_diary_journal_notes.sqlite.sql` (`VALUES ('023', ...)`),
    produces a clean single-head chain: `019 → 020 → 021 → 022 → 023` that passes Sprint 2's new `test_alembic_has_single_linear_head` guardrail.

---

### 3.2 `src/backtest/forward/trade_persistence.py`
- **Sprint 2 (`THEIRS`)**:
  - Adds `DEDUPE_COLUMNS = ("portfolio_id", "symbol", "direction", "entry_time", "exit_time", "entry_price", "exit_price", "quantity")` and `trade_dedupe_key(values)` (deliberately **excluding** P&L so `scripts/repair_net_pnl_fee_floor.py` can repair `net_pnl` without changing the dedupe hash).
  - Adds `attach_order_ledger()`, `mirror_order()`, `_write_ledger_order()`, and `_mirror_book_slice()` (`B3` — fixes empty `orders`/`fills` tables in Postgres).
- **PR #49 (`OURS`)**:
  - Added in-memory `_seed_existing_signatures` (superseded by Sprint 2's stronger DB-backed `_already_persisted` + `uq_trades_dedupe_key`), **plus** `_resolve_runner_mode(cfg)` in `_ensure_portfolio_row()`.
- **Critical Catch**:
  - In Sprint 2's `_ensure_portfolio_row()` (line 446), `PortfolioRow(..., mode="paper", ...)` is still hardcoded! Without PR #49's `_resolve_runner_mode(cfg)`, every Live broker trade persisted by `LiveTradePersister` lands under a `portfolios` row stamped `mode='paper'`.
- **Exact Synthesis**:
  - Keep 100% of Sprint 2's `trade_persistence.py`, and apply `_resolve_runner_mode(cfg)` inside `_ensure_portfolio_row()` (creating rows with `mode=resolved_mode` and healing legacy mis-stamped rows when `cfg.mode` is explicit).

---

### 3.3 `src/backtest/forward/paper_runner.py` & `portfolio.js`
- **Sprint 2 (`THEIRS`)**:
  - Redefines `runner.realized_pnl` from lifetime **gross** to lifetime **NET** (`gross_realized_pnl - closed_fees`), adds `gross_realized_pnl`, `total_fees_paid`, `open_pnl()` (NET MTM), `realized_pnl_today`, `prev_close_open_pnl`, and `daily_pnl() = realized_pnl_today + (open_pnl() - prev_close_open_pnl)`.
  - Fixes `BUG-26` via `bind_executor()` on snapshot restore.
- **PR #49 (`OURS`)**:
  - Added `daily_realized_breakdown()` (`daily_gross_pnl`, `daily_commission`, `daily_realized_net_pnl`, `daily_trade_count`) and replaced two `datetime.now(_IST).date()` calls with `current_session_date()`.
- **Exact Synthesis**:
  - Keep **all** of Sprint 2's `paper_runner.py` methods and `state_dict()` fields (`gross_realized_pnl`, `total_fees_paid`, `open_pnl`, `realized_pnl_today`, `prev_close_open_pnl`, `daily_pnl`, `realized_pnl`).
  - Keep PR #49's `daily_realized_breakdown()` + `daily_gross_pnl` / `daily_commission` / `daily_trade_count` on `StrategyRunner.state_dict()` so `/portfolio/<mode>` continues displaying today's IST single `Commission` sum (`E2`), while `portfolio.js` uses `p.realized_pnl_today ?? p.daily_realized_net_pnl` so partial scale-outs match `BUG-04` to the paisa.

---

### 3.4 `src/backtest/forward/portfolio_manager.py`
- **Sprint 2 (`THEIRS`)**:
  - Fixes `BUG-03` (dashboard book no longer leaks ₹10,00,000 phantom capital into scoped `paper`/`live` summaries), `BUG-33` (`_record_capital_flow` adjusts day/peak anchors on spawn/delete), `f4e77d8` (daily-loss fuses evaluate session `daily_pnl` rather than `equity - spawn_anchor`), and `BUG-37` (`portfolio_reaper`).
- **PR #49 (`OURS`)**:
  - Added `session_date_ist`, `session_scope_label`, `daily_gross_pnl`, `daily_commission`, and `daily_realized_net_pnl` to `get_bucket_aggregates()` and `get_summary()`.
- **Exact Synthesis**:
  - Keep 100% of Sprint 2's `portfolio_manager.py` logic and append those 5 read-only dictionary keys into `get_bucket_aggregates()` and `get_summary()`.

---

### 3.5 `src/backtest/api/portfolio.py` & `src/backtest/web/static/js/components/risk_board.js`
- **`risk_board()` (`/api/portfolio/risk`)**:
  - Keep **THEIRS** (`pnls = [float(t["net_pnl"]) for t in all_trades]`), fixing `B5`.
- **`aggregated_trades()` (`/api/portfolio/trades/aggregated`)**:
  - Sprint 2 only switched `pnl` $\rightarrow$ `net_pnl` over the in-memory `MAX_TRADE_LOG = 50` buffer and added `"per_runner_cap": MAX_TRADE_LOG`.
  - PR #49 upgraded `aggregated_trades()` to join persisted `trades` + `portfolios` with the live in-memory tail (deduped via `dedupe_normalized_trades`), added `?scope=today|all` & `?session_date=YYYY-MM-DD`, and emitted `total_gross_pnl`, `total_commission`, `total_net_pnl` (`total_pnl` = NET), `duplicates_collapsed`, and `mismatch_count`.
  - **Synthesis**: Keep PR #49's upgraded `aggregated_trades()` + `risk_board.js` (11-column `Gross | Commission | Net P&L` table matching `_portfolio_center.html`) **and** include `"per_runner_cap": MAX_TRADE_LOG` in `summary` so both Sprint 2's `test_net_pnl_contract.py` and PR #49's `test_slice0_ledger_truth.py` pass without compromise.

---

### 3.6 Cross-Check: Impact of Sprint 2 on `/diary` (`Slices 1–5`)
- **Zero conflicts** in `src/backtest/diary/*`, `src/backtest/api/diary.py`, `diary.html`, `diary.css`, or `diary.js`.
- **Positive synergy**: Sprint 2 changed `state_dict()["open_pnl"]` from gross MTM to **NET open P&L** (`unrealized_pnl - _open_fees`). `DiaryService` reads `rs.get("open_pnl")` for today's `running_mtm`, so `total_pnl_with_mtm` on `/diary` automatically reflects entry brokerage already paid on open positions (`BUG-04` compliant).
- **Test Firewall compatibility**: All `tests/diary/test_slice{0..5}_*.py` use isolated `sqlite://` fixtures and comply with Sprint 2's new `tests/db/test_pytest_db_firewall.py`.

---

## 4. Proposed Execution Steps (Once You Approve)

1. Merge `origin/feature/options-trading-dev` (`3d5a073`) into `arena/f7407685-back-test`.
2. Resolve the 7 conflicting files + `models.py` / `022` $\rightarrow$ `023` migration renumbering exactly per the matrix in **§2**.
3. Update `tests/diary/test_slice5_graduation_and_journal.py` (`022` $\rightarrow$ `023` path references) and `tests/db/test_migrations_009_013.py` (head `["023"]`).
4. Run full verification:
   - Sprint 2 suites (`tests/forward/test_net_pnl_contract.py`, `test_ledger_persistence.py`, `test_capital_flow_anchors.py`, `test_capital_ledger_identity.py`, `test_trade_persistence.py`, `tests/db/`),
   - PR #49 suites (`tests/diary/`, `tests/test_workspace_design.py`, `tests/test_timekeeping_policy.py`, `tests/invariants/`, `node --test tests/js/test_diary_slice{1..5}.mjs`),
   - `.venv/bin/flake8` across all touched Python files.
5. Commit the merge resolution on `arena/f7407685-back-test` and push to update **PR #49** (`mergeable: MERGEABLE`).
