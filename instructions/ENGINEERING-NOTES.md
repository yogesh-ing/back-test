# Engineering Notes — Forward Testing Simulator

Running reference for **debugging and maintenance**. Companion to
`instructions/TASK-TRACKER.md` (which tracks *what* is done); this file records
*why* things are the way they are, what has already bitten us, and where to
look when something breaks.

Updated at the end of every step. Last updated: **Step 5**.

---

## 1. Debugging playbook

Start here. Symptom → most likely cause → where to look.

### Money and P&L

| Symptom | Likely cause | Check |
|---|---|---|
| Equity ≠ cash + position value | A cash mutation bypassed the convention in §2.1 | `Portfolio.open_position` / `reduce_position` — every cash change lives there |
| P&L drifts by tiny amounts over many trades | A float leaked into the Decimal pipeline | `money.to_decimal` rejects floats via `repr()`; grep for `float(` in `simulator/` |
| Short position shows inverted P&L | Sign convention broken | `quantity` must be **negative** for shorts; `unrealized_pnl` uses one signed formula for both sides |
| Money values slightly wrong on SQLite only | SQLite stores `NUMERIC` as float | Expected. Use PostgreSQL for anything you report on |
| Partial close realises the wrong amount | Cost-basis method not what you assumed | `position.cost_basis_method`; FIFO/LIFO/AVERAGE give **different** answers (§2.3) |
| Split appears to create profit | Split logic wrong | `apply_split` must leave `market_value` and `unrealized_pnl` unchanged |

### Database

| Symptom | Likely cause | Check |
|---|---|---|
| `uq_positions_one_open_per_symbol` violation on save | Open rows written before closed ones | `Portfolio.save_to_db` writes **closed first**, then flushes (§4.1) |
| `current transaction is aborted, commands ignored` | An earlier statement in the same transaction failed | Scroll **up** to the first real error; the rest is noise |
| FK violations silently don't fire | SQLite with `PRAGMA foreign_keys` off | `DatabaseManager` sets it per-connection; ad-hoc `sqlite3` sessions do not |
| `ck_orders_filled_consistency` violation | `status='filled'` without `filled_at` or full quantity | `Order.add_fill` sets both together |
| `ck_orders_rejection_reason` violation | Rejected without a reason | `Order.reject` requires a non-empty reason |
| Retry storm on a typo'd table name | Transient classification too broad | `manager._is_transient` — message-based, not type-based (§4.2) |
| `IntegrityError` on duplicate `client_order_id` | Working as intended — idempotency key | Generate a fresh id per submission attempt |

### Orders

| Symptom | Likely cause | Check |
|---|---|---|
| `InvalidTransitionError` on a normal-looking flow | Order is terminal, or was never submitted | `VALID_TRANSITIONS` in `simulator/enums.py`; error message lists legal moves |
| Stop order never fills | Trigger has not fired | `order.triggered`; `check_trigger()` uses `last`, `is_fillable()` uses bid/ask |
| Trailing stop loosened after a retrace | Ratchet broken | `update_trailing` must only move the stop favourably (§2.4) |
| `TypeError: '<' not supported between str and Decimal` | A Decimal field not coerced after `from_dict` | All Decimal fields coerce in `__post_init__`; add new ones to that loop |
| Callback exception vanished | Deliberate — callbacks are isolated | Look for `order callback ... failed` in the `backtest.simulator.order` log |
| Average fill price looks wrong | Expected: it is quantity-weighted across **all** fills | `Order.average_fill_price` |

### Environment

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: backtest` | `PYTHONPATH=src` — required for every command |
| `ModuleNotFoundError: dotenv` / `yaml` | `pip install -r requirements.txt` |
| `.venv` disappeared | It is excluded from workspace snapshots; recreate it |
| `ConfigError: No database URL configured` | Set `FORWARD_TEST_DB_URL`, or pass `url=` to `from_env()` |

---

## 2. Conventions and invariants

Break one of these and something downstream silently goes wrong.

### 2.1 Cash convention

```
open  long    cash -= qty * price + commission
open  short   cash += qty * price - commission   (proceeds credited)
close long    cash += qty * price - commission
close short   cash -= qty * price + commission
```

Combined with **signed** `market_value` (negative for shorts), this makes one
formula correct in both directions:

```
total_equity = cash + position_value
```

*Worked check:* short 10 @ 100 → cash +1,000, position value −1,000, equity
unchanged. Price falls to 90 → position value −900, equity +100. Correct.

### 2.2 Sign conventions

| Thing | Convention |
|---|---|
| `Position.quantity` | Signed: `+` long, `−` short, `0` closed |
| `Order.quantity` | **Always positive.** Direction lives in `side` |
| `Lot.quantity` | Always positive. Direction belongs to the position |
| `Fill.slippage_bps` | Signed; positive = adverse to the order side |
| `DividendResult.cash_amount` | Signed; a short **pays** dividends |
| `EquityCurve.drawdown_pct` | Fractional (`0.10` = 10%), not percent points |

### 2.3 Cost basis changes the answer

Buy 10@100, buy 10@120, sell 10@130:

| Method | Realised | Remaining basis |
|---|---|---|
| FIFO | 300 | 10 @ 120 |
| LIFO | 100 | 10 @ 100 |
| AVERAGE | 200 | 10 @ 110 |

Default is `AVERAGE` (matches the vectorised backtest engine). Indian equity
delivery mandates **FIFO**. Always state the method when comparing runs.

**Invariant:** `LotBook.total_quantity == abs(position.quantity)` and
`LotBook.weighted_average_price == position.average_entry_price`. Under
`AVERAGE` the book collapses to one pooled lot after every mutation, so the
second half holds by construction.

### 2.4 Order rules

- **Triggering is sticky.** Once a stop fires it stays fired. Un-triggering
  would turn it into a limit order and silently change the risk profile.
- **Trailing stops ratchet one way only.** Sell-side tracks the high-water
  mark and the stop only rises; buy-side mirrors it. A stop that loosens is
  not a stop.
- **Terminal is terminal.** `FILLED`/`CANCELLED`/`REJECTED` have no outgoing
  transitions. `from_dict` restores status directly rather than replaying it.
- **Callbacks are isolated.** A handler that raises is logged and swallowed —
  a broken alert hook must not roll back a fill that genuinely happened.

### 2.5 No-lookahead

`strategy_signals.bar_ts` (open time of the **completed** bar) must always be
strictly earlier than `generated_at`. Step 22's bias detector is a query over
that pair. The legacy engine's rule still applies: position at bar *t* comes
from the signal at bar *t−1* (`target.shift(1)`).

---

## 3. Layering

```
simulator/   pure domain logic, no I/O    ──uses──>  db.DatabaseManager
db/          ORM + connection management
engine/ forward/ live/ strategy/          pre-existing, untouched
```

`simulator/` must **not** import from `engine/` or `forward/`. Enforced by
`test_simulator_does_not_import_engine_or_forward`, which parses the AST (an
earlier grep version false-positived on a docstring that merely *mentioned*
the rule).

**Three different `Portfolio` classes exist.** Import the right one:

| Class | Purpose |
|---|---|
| `backtest.simulator.Portfolio` | Domain model — cash, positions, limits |
| `backtest.db.models.Portfolio` | ORM row |
| `backtest.forward.portfolio.Portfolio` | Legacy multi-strategy allocator (`paper.py`) |

---

## 4. Bugs found, and why they happened

Kept because each one is a trap that could recur.

### 4.1 Save order violated the partial unique index (Step 3)

**Symptom:** `uq_positions_one_open_per_symbol` violation on the *second*
save, only against PostgreSQL.

**Cause:** `save_to_db` wrote open positions before closed ones. A symbol
closed and reopened after a prior save momentarily had two rows with
`status='open'`.

**Why SQLite missed it:** the earlier test closed and reopened *before* the
first save, so the stale row never existed.

**Fix:** write closed rows first, `flush()`, then open rows — inside
`session.no_autoflush` so `session.get()` cannot flush a half-built unit of
work mid-loop.

**Lesson:** partial unique indexes are order-sensitive. Test multi-save
sequences, not just single saves.

### 4.2 `OperationalError` is not a reliable "transient" signal (Step 2)

**Symptom:** a typo'd table name was retried three times and reported as a
*connection failure*.

**Cause:** PostgreSQL raises `OperationalError` for a dropped socket; SQLite
raises it for `no such table`.

**Fix:** `_is_transient()` inspects `connection_invalidated` plus a
conservative allow-list of driver messages.

**Second bite:** libpq's real outage message is
`connection to server on socket ... failed: No such file or directory /
Is the server running locally...`, which the first pattern list missed — so
recovery silently did not work. Both patterns are now covered.

**Lesson:** classify on the message, and verify against a *real* server
outage, not a mock.

### 4.3 `BigInteger` primary keys don't autoincrement on SQLite (Step 1)

SQLite only autoincrements a column declared exactly `INTEGER PRIMARY KEY`.
Fixed with `BigInteger().with_variant(Integer, "sqlite")`.

### 4.4 `CREATE EXTENSION pgcrypto` aborted the whole migration (Step 1)

The file is one transaction, so a missing extension killed all 80 statements
behind a wall of `current transaction is aborted`. PG 13+ has
`gen_random_uuid()` in core; the call is now a guarded `DO` block.

### 4.5 `extreme_price` was not coerced to Decimal (Step 5)

After `from_dict` it stayed a `str`, so the next `min()/max()` in
`update_trailing` would raise `TypeError` — but only after a restart, which is
exactly when you least want it.

**Lesson:** every Decimal field must be in the `__post_init__` coercion loop.
There is now a test asserting that for all of them.

### 4.6 Test-only mistakes worth remembering

- `pgserver.psql()` **prints** errors instead of raising — an early constraint
  test reported "no error" 13 times when all 13 constraints had fired. Use
  psycopg2 directly for assertions.
- A split on a short *preserves* market value (`−10×100` → `−20×50`). An early
  assertion of `−2000` was wrong, not the code.

---

## 5. Module notes

### `simulator/money.py`
`to_decimal` converts floats via `repr()` so `0.1` → `Decimal("0.1")`, not the
binary expansion. `bool` is rejected outright (it is an `int` subclass, and
accepting it hides bugs). Precision mirrors the schema: 4 dp money, 8 dp
prices, 6 dp ratios.

### `simulator/lots.py`
Under `AVERAGE` the book is collapsed to one lot after every mutation. The
merged lot keeps the **oldest** acquisition time (conservative for
holding-period reporting). `reduce_cost_basis` floors prices above zero to
preserve `Lot`'s positive-price invariant.

### `simulator/position.py`
`average_entry_price` is re-derived from lots after every mutation via
`_sync_average_from_lots`. Under `AVERAGE` a partial close leaves it unchanged;
under FIFO/LIFO it moves. Realised P&L is **gross of commission** —
commissions accumulate separately in `commission_total`, matching the schema's
separate `gross_pnl` / `commission_total` columns.

### `simulator/order.py`
`PENDING` covers both "created" and "working" because those are the only
values the CHECK constraint allows; use `is_submitted` to distinguish.
`is_fillable` checks trigger *then* limit. Limit orders fill at the **better**
of limit and market (price improvement). `calculate_fill_price` raises if the
order is not fillable, so a caller cannot book a fill that should not happen.

### `db/manager.py`
Retries are **disabled inside an explicit transaction** — replaying a
statement whose predecessors already applied would corrupt data. Pool choice:
`QueuePool` (5/20) for PostgreSQL, `NullPool` for SQLite files, `StaticPool`
for in-memory SQLite (whose database *lives inside* one connection).

---

## 6. Known limitations

| # | Limitation | Impact / workaround |
|---|---|---|
| 1 | Tax lots are not persisted (no `lots` table) | A reloaded FIFO position collapses to one lot at the average. `to_dict()`/`from_dict()` **is** lossless — Step 20 state persistence should use it |
| 2 | `Order.status_history`, `triggered`, `extreme_price` not persisted | Same: no columns. Survives in the JSON snapshot |
| 3 | SQLite `NUMERIC` is float-backed | Dev only. Never report from SQLite |
| 4 | `graphify-out/` is committed | Tool cache; `.gitignore` only affects new files |

---

## 7. Running things

```bash
# Everything needs PYTHONPATH=src
PYTHONPATH=src pytest tests/ -q
PYTHONPATH=src pytest tests/test_simulator_order.py -q -k trailing

# Verbose SQL while debugging
FORWARD_TEST_DB_LOG_QUERIES=true PYTHONPATH=src pytest tests/... -s

# Apply the schema by hand (see db/DB-IMPLEMENTATION-GUIDE.md)
psql -d forward_test -f db/migrations/001_initial_schema.sql
psql -d forward_test -f db/verify_schema.sql        # expect all PASS
```

**Testing against real PostgreSQL without installing one:** the `pgserver`
package ships an embedded server, which is how every "verified against real
PostgreSQL" claim in the commit log was produced.

```python
import pgserver, tempfile
srv = pgserver.get_server(tempfile.mkdtemp())
srv.psql(open('db/migrations/001_initial_schema.sql').read())
url = srv.get_uri().replace("postgresql://", "postgresql+psycopg2://")
```

Note the server dies with its parent process — drive Alembic through its
Python API rather than the CLI when using it.

---

## 8. Test map

| File | Covers | Tests |
|---|---|---|
| `test_db_schema.py` | Tables, constraints, cascades, SQL↔ORM parity | 44 |
| `test_db_manager.py` | Config precedence, pooling, transactions, retry classification | 107 |
| `test_simulator_portfolio.py` | Cash accounting, limits, exposure, persistence | 130 |
| `test_simulator_position.py` | FIFO/LIFO/average, splits, dividends | 77 |
| `test_simulator_order.py` | State machine, 5 order types, triggers, callbacks | 115 |
| Pre-existing | Backtest engine, mStock | 25 (+4 skipped) |

**Drift guards** — these fail loudly if two sources of truth diverge:

- `test_enums_match_the_orm` / `test_enums_match_the_sql_check_constraints`
- `test_sqlite_migration_file_matches_orm`
- `test_simulator_does_not_import_engine_or_forward`
- Alembic autogenerate produces no diff against the ORM
