# Merge Summary: main → feature/options-trading-dev

**Date:** 2026-10-15  
**Merge Commit:** `a6bbda8`  
**Branches:** `main` (10 commits ahead) → `feature/options-trading-dev`

---

## Objective

Bring critical trade persistence fixes, validation infrastructure, and schema migrations from `main` into `options-trading-dev` to enable reliable testing of option strategies with proper PnL tracking and audit trail.

---

## What Was Merged

### 1. Critical Bug Fixes

#### Daily PnL Calculation (`paper_runner.py`)
**Problem:** Previous implementation tried to sum `_closed_trades` list, but records had no `net_pnl` field populated.

**Solution (from main):**
```python
def daily_pnl(self) -> float:
    # Realized PnL since session start (portfolio tracks cumulative)
    session_realized = float(self.portfolio.realized_pnl) - self._session_start_realized
    
    # Add unrealized PnL on open positions (MTM)
    unrealized = float(self.portfolio.unrealized_pnl)
    
    # Add option book contribution if present
    if self.options_bridge is not None:
        session_realized += float(self.options_bridge.option_broker.total_realized_pnl) - self._option_session_start_realized
        unrealized += float(self.options_bridge.unrealized_pnl)
    
    return session_realized + unrealized
```

**Session Anchors (added to `__init__`):**
```python
self._session_start_realized: float = float(self.portfolio.realized_pnl)
self._option_session_start_realized: float = 0.0
if self.options_bridge is not None:
    self._option_session_start_realized = float(self.options_bridge.option_broker.total_realized_pnl)
```

**Impact:** Accurate daily PnL that correctly separates realized vs unrealized components, works for both equity and options runners.

---

### 2. Validation Infrastructure

#### Trade Export Script (`scripts/export_trades.py`)
- Exports all closed trades to CSV with full details (strategy, symbol, entry/exit prices, PnL, charges)
- Exports open positions snapshot
- Supports mode filtering (paper/live)
- Output: `tradeData/closed_trades.csv`, `tradeData/open_positions.csv`

#### PnL Reconciliation Validator (`scripts/validate_pnl_reconciliation.py`)
Three-way cross-check:
1. **DB vs API Realized PnL** — Verifies trade-level persistence matches portfolio aggregate
2. **Today's Trades vs Daily PnL** — Validates intraday trade sum (accounts for unrealized MTM)
3. **Equity Accounting Identity** — Confirms: `equity = capital + realized + unrealized` (< 0.1% tolerance)

**Usage:**
```bash
python scripts/validate_pnl_reconciliation.py --mode paper
```

---

### 3. Documentation

#### Trade Persistence Architecture (`docs/TRADE_PERSISTENCE_ARCHITECTURE.md`)
Complete system documentation including:
- Database schema (portfolios, trades, positions, orders tables)
- JSON state file structure and atomic write mechanism
- Mode separation (paper vs live) via `mode` column
- Trade persistence flow (flush cycle every 5 ticks)
- Schema drift bug analysis (migration 008 gap)
- Validation test plans

#### After-Market Validation Report (`docs/AFTERMARKET_VALIDATION_2026-10-14.md`)
Detailed test results from 2026-10-14:
- Portfolio page data consistency ✅ PASS
- Market-hours DB gating ✅ PASS (zero heavy queries when closed)
- Runner sums match bucket aggregates ✅ PASS
- Equity accounting identity ✅ PASS (0.055% tolerance)

---

### 4. Schema Migrations

#### Migration 020: Upstox Source Support
Both branches had this migration; merged to ensure consistency:
- Adds `upstox` to allowed source values in portfolios table
- SQL: `ALTER TABLE portfolios ADD COLUMN ... CHECK(source IN ('synthetic', 'replay', 'mstock', 'dhan', 'upstox'))`

**Note:** Options branch already has migrations 009-020, so this merge doesn't add new migrations beyond what was already there.

---

### 5. Feed Registry Enhancements

#### Upstox Support in BidAskQuoteProvider (`feed_registry.py`)
Merged Upstox broker support into the options chain provider:
```python
* source="mstock"/"dhan"/"upstox" + authenticated broker → LiveChainProvider wrapped in BidAskQuoteProvider
```

**Impact:** Option runners can now use Upstox as a data source with proper bid-ask spread crossing for fills.

---

### 6. Strategy Plugins

Merged from main (already existed in options branch, but updated):
- `plugins/strategies/hf_equity_scalp.py` — High-frequency equity scalper
- `plugins/strategies/nifty_instant_buy.py` — NIFTY options instant ATM buy
- `plugins/strategies/one_min_scalp.py` — Simple 1-min momentum scalp
- `plugins/strategies/delta_momentum.py` — Delta-based momentum strategy
- `plugins/strategies/volume_oi_surge.py` — Volume + OI surge detection

---

### 7. .gitignore Updates

Added exclusions for:
- Portfolio state backups (`*.bak_*`)
- Test artifacts (`test-results/`, `tradeData/`)
- Runtime logs (`*.log`, `*.err`, `*.out`)
- Imported plugins (`*_imported.py`)
- Node modules, temp scripts

---

## Merge Conflicts Resolved

### 1. `src/backtest/forward/paper_runner.py` (2 conflicts)

**Conflict 1: Session anchors initialization**
- **HEAD (options):** Empty block
- **main:** Added `_session_start_realized` and `_option_session_start_realized`
- **Resolution:** Kept main's version (required for daily_pnl fix)

**Conflict 2: daily_pnl() implementation**
- **HEAD (options):** Date-based filtering of `_closed_trades` (less robust)
- **main:** Portfolio-based calculation using `realized_pnl` (superior architecture)
- **Resolution:** Kept main's version (matches Portfolio's single-source-of-truth design)

### 2. `src/backtest/forward/feed_registry.py` (1 conflict)

**Conflict: Upstox support in docstring**
- **HEAD (options):** Listed mstock/dhan only, mentioned BidAskQuoteProvider wrapping
- **main:** Added upstox to list, didn't mention BidAskQuoteProvider
- **Resolution:** Merged both — kept Upstox addition AND BidAskQuoteProvider wrapping description

---

## Files Modified by Merge

**New files added (11):**
- `scripts/export_trades.py`
- `scripts/validate_pnl_reconciliation.py`
- `docs/TRADE_PERSISTENCE_ARCHITECTURE.md`
- `docs/AFTERMARKET_VALIDATION_2026-10-14.md`
- `db/alembic/versions/20261013_1200_020_add_upstox_source_to_portfolios.py`
- `db/migrations/020_add_upstox_source.sql`
- `db/migrations/020_add_upstox_source.sqlite.sql`
- Plus 4 more (config/docs updates)

**Modified files (28):**
- `src/backtest/forward/paper_runner.py` (daily_pnl fix)
- `src/backtest/forward/feed_registry.py` (Upstox support)
- `.env.example`, `.gitignore`
- Config files (`brokers.yaml`, `data_sources.yaml`, `segments.yaml`)
- Documentation (`DATA-SOURCES.md`, `DATABASE.md`, etc.)

**Unchanged (options branch retains its versions):**
- All `src/backtest/options/*` files (intent journal, execution gate, etc.)
- Upstox broker integration (`brokers/upstox.py`, `data/upstox_live_feed.py`)
- Pine script enhancements
- Options-specific tests

---

## Testing Plan for Options Branch

### Immediate Verification (After Merge)
1. **Start server on options branch:**
   ```bash
   git checkout feature/options-trading-dev
   export PYTHONPATH=src && python src/backtest/web/app.py
   ```

2. **Verify clean state:**
   ```bash
   curl -s "http://127.0.0.1:5000/api/portfolio/buckets" | python -c "import sys, json; data = json.load(sys.stdin); print(f'Runners: {data[\"buckets\"][\"paper\"][\"count\"]}')"
   # Expected: 0 runners (clean slate)
   ```

3. **Deploy test option strategy:**
   - Use UI to create NIFTY options runner (e.g., `atm_instant_buy`)
   - Verify it starts and receives live bars

4. **Wait for flush cycle (10 minutes)**

5. **Validate trade persistence:**
   ```bash
   python scripts/validate_pnl_reconciliation.py --mode paper
   ```
   **Expected:** Check 1 passes (DB realized = API realized), Check 3 passes (equity identity)

### Live Market Testing (Next Trading Day)
1. Deploy at least one option strategy during market hours
2. Monitor for signals and executions
3. After 15 minutes, run validation script
4. Export trades: `python scripts/export_trades.py --mode paper`
5. Verify CSV contains option trades with correct strike/expiry/premium

---

## Known Differences Retained

The options branch still has features NOT in main (by design):
- Complete options trading module (`src/backtest/options/`)
- Intent journal for audit trail
- Advanced exit policies (cap → square-off → stop/target → time)
- Pine script converter improvements
- Upstox OAuth login flow
- Deterministic replay harness

These are intentional divergences that make the options branch specialized for options trading development.

---

## Rollback Plan

If merge causes issues:
```bash
git checkout feature/options-trading-dev
git reset --hard <pre-merge-commit-hash>  # Use git reflog to find it
```

The pre-merge commit is preserved in git reflog for 90 days.

---

## Next Steps

1. ✅ Merge complete — all conflicts resolved
2. ⏳ Test on options branch (start server, deploy strategy, validate persistence)
3. ⏳ Live market validation tomorrow
4. ⏳ Consider merging options branch back to main after successful testing

---

**Status:** MERGE COMPLETE ✅  
**Ready for:** Testing on options-trading-dev branch
