# Engineering Notes — Forward Testing Simulator

Running reference for **debugging and maintenance**. Companion to
`instructions/TASK-TRACKER.md` (which tracks *what* is done); this file records
*why* things are the way they are, what has already bitten us, and where to
look when something breaks.

Updated at the end of every step. Last updated: **Step 12** (Phase 4 Live Data complete, mock-only).

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
| Strategy profitable in backtest, loses live | Execution friction | Compare `backtest` vs `realistic` slippage profiles (§2.8). Slippage typically dwarfs commission |
| Limit order reported filling worse than its limit | Slippage cap bypassed | `SlippageCalculator` caps against `order.limit_price` and sets `estimate.capped` |
| Slippage looks free on daily bars | No bid/ask in the data | `SpreadSlippage.fallback_bps` covers this; check it is non-zero |
| Fees ~8x too high or low on a round trip | Wrong `TradeSegment` | Delivery pays STT both sides at 0.1%; intraday sell-only at 0.025% (§2.9) |
| "Zero brokerage" run still loses money | Correct — statutory charges remain | A ₹1L Indian delivery round trip costs ~₹238 with zero brokerage |
| Fee totals don't match the `fills` table | Bucket mapping | brokerage→`commission`, exchange/IPFT/DP→`exchange_fees`, STT/SEBI/stamp/GST→`regulatory_fees` |
| Large order only partly fills | Working as intended — liquidity cap | `ExecutionConfig.max_participation` (10% of bar volume by default) |
| Limit order at the touch didn't fill | Queue position | `touch_fill_probability` (0.5 realistic). Trading *through* the limit always fills |
| Order died when it should have rested | no-fill vs rejection confusion | `NO_FILL` keeps the order working; `REJECTED` is terminal (§2.10) |
| Execution results differ between identical runs | RNG not seeded | `ExecutionConfig.seed` (default 42); `executor.reset()` replays identically |
| Everything rejected as `market_closed` | `enforce_market_hours` on with daily bars | Off by default for exactly this reason |

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
| `ForeignKeyViolation` on `fk_fills_position` | Saving out of dependency order | Use `Portfolio.save_to_db()` — it writes portfolios→positions→orders→fills atomically (§4.7) |
| Cash off by exactly the fee amount | Fees counted twice, or slippage added to cash | Fees are applied **once**, by whoever moves the cash. Slippage is *never* cash (§2.6) |

### Orders

| Symptom | Likely cause | Check |
|---|---|---|
| `InvalidTransitionError` on a normal-looking flow | Order is terminal, or was never submitted | `VALID_TRANSITIONS` in `simulator/enums.py`; error message lists legal moves |
| Stop order never fills | Trigger has not fired | `order.triggered`; `check_trigger()` uses `last`, `is_fillable()` uses bid/ask |
| Trailing stop loosened after a retrace | Ratchet broken | `update_trailing` must only move the stop favourably (§2.4) |
| `TypeError: '<' not supported between str and Decimal` | A Decimal field not coerced after `from_dict` | All Decimal fields coerce in `__post_init__`; add new ones to that loop |
| Callback exception vanished | Deliberate — callbacks are isolated | Look for `order callback ... failed` in the `backtest.simulator.order` log |
| Average fill price looks wrong | Expected: it is quantity-weighted across **all** fills | `Order.average_fill_price` |

### Strategy Adapter (Step 13)

| Symptom | Likely cause | Check |
|---|---|---|
| `HOLD` signals flooding DB | Logging HOLD twice (generate + execute) | `execute_signals` now skips HOLD logging; HOLD already logged in `generate_signals` |
| `FOREIGN KEY constraint failed` on `strategy_signals.portfolio_id` | Portfolio row not yet persisted | Adapter auto-creates minimal portfolio row in `_save_signal_to_db` if missing |
| `FOREIGN KEY constraint failed` on `strategy_signals.order_id` | Order row not yet persisted | Adapter drops FK (saves without order_id) when order not in DB |
| Signal generated but no order created | Portfolio validation failed (insufficient funds, duplicate position, short disabled) | Check `skip_reason` in DB or logs; `portfolio.can_open_position` |
| Short signal ignored | `allow_short=False` by default | Set `allow_short=True` for short strategies |
| Lookahead bias suspicion | `bar_ts` >= `generated_at` | Adapter sets `bar_ts` from completed bar index, `generated_at` = now; assert `bar_ts < generated_at` |
| Position not opened after signal | No executor, or executor without portfolio sync | Without executor, order stays pending; with executor, fill applied via `on_order_filled` |

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

### 2.6 Fees vs slippage

| | Paid to someone? | In `fill_price`? | Moves cash? |
|---|---|---|---|
| `commission`, `exchange_fees`, `regulatory_fees` | yes | no | **yes** |
| `slippage_amount` | no | **yes** | no |

Slippage is execution shortfall versus the decision price — it is already
inside `fill_price`. Adding it to cash double-counts. `calculate_total_cost()`
deliberately excludes it; `total_cost_of_trading` includes it and is for
**attribution only** (Step 22).

Signed slippage is positive when **adverse**: a buy above the reference or a
sell below it. "Higher is worse" holds for both sides.

### 2.8 Slippage dominates commission

Measured on 40 round-trip legs of 300 shares at an unchanged ₹1,500 market:

| Profile | Slippage cost | Commission | Ratio |
|---|---|---|---|
| `backtest` | ₹0 | ₹800 | — |
| `optimistic` | ₹3,060 | ₹800 | 4x |
| `realistic` | ₹40,244 | ₹800 | **50x** |
| `pessimistic` | ₹104,285 | ₹800 | 130x |

Commission is the visible cost; slippage is the one that actually decides
whether a strategy survives contact with the market. Always sanity-check a
strategy at `pessimistic` — if the edge only exists at `realistic`, it is too
thin to trade.

Note the hybrid default is **volatility-dominated** at typical NSE parameters
(ATR 1.5% contributes ~15 of ~22 bps). That is a modelling choice, not a law;
re-estimate `atr_fraction` against your own fill data once you have some.

### 2.9 Indian fee stack: segment matters

| Charge | Delivery | Intraday |
|---|---|---|
| STT | 0.1% **both sides** | 0.025% **sell only** |
| Stamp duty | 0.015% buy only | 0.003% buy only |
| DP charges | flat, sell only | none |

Measured on ₹1,00,000, zero brokerage: delivery round trip **₹237.82**,
intraday **₹82.68**. Setting `TradeSegment` wrong misprices by ~3x overall
and ~8x on STT alone.

GST (18%) applies to brokerage + exchange + SEBI charges — **not** to STT or
stamp duty, which are themselves taxes and are not taxed again.

Rates are FY 2024-25 and **do change**. Verify against a recent contract note
before trusting a cost-sensitive result; override in `config/brokers.yaml`.

### 2.10 Execution outcomes are not all failures

| Status | Order afterwards | Meaning |
|---|---|---|
| `FILLED` / `PARTIAL` | filled / still working | traded |
| `NO_FILL` | **still working** | limit away from market, queue miss — normal |
| `REJECTED` | terminal | market closed, halted, no liquidity |
| `CANCELLED` | terminal | IOC remainder, or FOK that couldn't fill whole |

Conflating `NO_FILL` and `REJECTED` either strands orders that should have
died or kills orders that should still be working. FOK is **cancelled**, not
rejected — matching exchange semantics — but still carries a
`rejection_code` so it shows up in the report.

### 2.7 Database write order

Foreign keys impose a strict order:

```
portfolios -> positions -> orders -> fills
```

`Portfolio.save_to_db()` does this atomically in one transaction. Prefer it
over saving objects individually.

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
forward/     orchestration + StrategyAdapter (may import simulator + strategy)
strategy/    abstraction + registry + adapter re-export
engine/ live/ data/                       pre-existing
```

`simulator/` must **not** import from `engine/` or `forward/`. Enforced by
`test_simulator_does_not_import_engine_or_forward`, which parses the AST (an
earlier grep version false-positived on a docstring that merely *mentioned*
the rule).
`forward/` and `strategy/` are allowed to import from `simulator/` — the
adapter bridges `strategy/base.py` (existing) into `simulator.Portfolio`.

**Three different `Portfolio` classes exist.** Import the right one:

| Class | Purpose |
|---|---|
| `backtest.simulator.Portfolio` | Domain model — cash, positions, limits |
| `backtest.db.models.Portfolio` | ORM row |
| `backtest.forward.portfolio.Portfolio` | Legacy multi-strategy allocator (`paper.py`) |

**Two different `Strategy` concepts exist.** Import the right one:

| Class | Purpose |
|---|---|
| `backtest.strategy.base.Strategy` | Abstract base for all strategies (existing, reused in Step 13) |
| `backtest.forward.strategy_adapter.StrategyAdapter` | Bridge that runs a Strategy in forward testing |

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

### 4.7 Fills saved before their positions existed (Step 6)

**Symptom:** a wall of `ForeignKeyViolation: fk_fills_position` traceback
during an end-to-end run against real PostgreSQL.

**Cause:** `Fill.save_to_db()` was called before the position row existed.
The FK order is portfolios → positions → orders → fills, and every caller was
expected to know it.

**Fix:** `Portfolio.save_to_db(include_orders=True)` now writes the entire
graph in dependency order inside one transaction. The standalone
`Fill.save_to_db()` also pre-checks and raises an actionable `ValidationError`
naming the required order instead of letting the driver's FK error escape.

**Lesson:** if correct use requires knowing an ordering constraint, provide an
API that encodes it — and make the low-level path fail with an explanation.

### 4.8 Strategy signals FK failures (Step 13)

**Symptom:** ``FOREIGN KEY constraint failed`` when logging to
``strategy_signals`` — both ``portfolio_id`` and ``order_id`` FKs.

**Cause:** The adapter's portfolio is a ``simulator.Portfolio`` (in-memory)
that has never been persisted, so its ``portfolio_id`` does not exist in the
``portfolios`` table. Similarly, the order row does not exist until
``Portfolio.save_to_db()`` is called.

**Fix:** ``_save_signal_to_db`` now auto-creates a minimal portfolio row if
missing (using the simulator portfolio's name/capital) and drops the
``order_id`` FK when the order is not yet in the DB. Logging must never block
trading.

**Lesson:** Audit-log tables with FKs to operational tables need graceful
degradation when the operational rows have not been flushed yet.

### 4.9 ExecutionConfig unexpected kwarg (Step 20)

**Symptom:** ``Failed to init executor: ExecutionConfig.__init__() got an unexpected keyword argument 'allow_short'``

**Cause:** Engine passed ``allow_short`` to ``ExecutionConfig`` which doesn't accept it — short selling is controlled by ``PortfolioLimits``, not execution.

**Fix:** Removed ``allow_short`` from ExecutionConfig init; executor now uses default config loaded from file/profile.

**Lesson:** Check constructor signatures; portfolio limits vs execution config are separate concerns.

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

### `simulator/commission.py`
Five models: zero, flat, per-share, percentage, tiered. `PercentageCommission`
rejects a rate above 1 because `0.03` meaning "0.03%" rather than 3% is a
classic units slip. `TieredCommission` is a **selected-rate** model (whole
trade at one rate), not marginal — the retail convention; say so loudly if
that ever changes. All models return non-negative amounts, because
`ck_fills_fees_nonneg` forbids rebates; model those in Step 8's fee stack.

### `simulator/fill.py`
Frozen dataclass — an execution is a historical fact. Normalisation in
`__post_init__` goes through `object.__setattr__`, the standard escape hatch.
A fill that would reverse a position through zero is refused rather than
split, because that hides a sizing bug and breaks per-trade attribution.
`from_dict` recomputes slippage from `reference_price` rather than trusting
the payload.

### `simulator/slippage.py`
Slippage is signed **adverse-positive** for both sides, matching
`Fill.slippage_bps`. Limit orders are capped at their limit price — a limit
order that fills worse than its limit is impossible, and `estimate.capped`
records when the cap bit. `SpreadSlippage` falls back to a fixed bps when the
snapshot has no quotes, because daily-bar data would otherwise make execution
look free. `max_bps` (default 1000) is a hard ceiling: a 10% haircut is a bug,
not a market condition.

Market impact follows the square-root law, so cost grows *sub*-linearly with
size — doubling an order does not double impact, which is why splitting helps
but only up to a point.

### `simulator/fees.py`
Two regulatory regimes: `IndiaEquityFees` (default) and `USEquityFees` (SEC
Section 31 + FINRA TAF, both sell-side). Fees round to 2dp like a real
contract note. `FeeBreakdown.as_fill_kwargs()` maps straight onto the three
`fills` cost columns. Tiered brokerage prices off **monthly volume** once any
has been recorded, otherwise off the single trade's value.

`PaymentForOrderFlowCommission` returns zero commission but exposes
`hidden_cost()` — PFOF is funded by worse fills, and reporting it as free is
the most misleading thing a fee model can do. Represent the real cost with a
wider slippage profile, not a fee.

### `simulator/execution.py`
Three realism levers, in order of impact: `max_participation` (forces partial
fills), `touch_fill_probability` (queue risk on resting limits), and latency.
Latency is *reported*, never slept on — a simulator that actually waited
500 ms per order would take hours to replay a day.

All randomness goes through one seeded generator. An execution simulator that
answers differently each run cannot be used to compare strategies.

`enforce_market_hours` is **off** by default: a daily-bar backtest has no
meaningful intraday clock, and rejecting everything would make it useless.

### `db/manager.py`
Retries are **disabled inside an explicit transaction** — replaying a
statement whose predecessors already applied would corrupt data. Pool choice:
`QueuePool` (5/20) for PostgreSQL, `NullPool` for SQLite files, `StaticPool`
for in-memory SQLite (whose database *lives inside* one connection).

### `forward/strategy_adapter.py` (Step 13)
Bridges ``strategy/base.py`` (existing) into ``simulator.Portfolio`` and
``simulator.execution.OrderExecutor``. Key invariants:

* **No lookahead:** only completed bars are passed to ``Strategy.generate_signals``.
  ``bar_ts`` is the timestamp of the completed bar, ``generated_at`` is now,
  so ``bar_ts < generated_at`` always holds — the query Step 22 uses for bias
  detection.
* **Signal → Order:** ``Signal`` is a typed dataclass mirroring the plan's dict
  shape (symbol, action BUY/SELL/HOLD, quantity, order_type, limit_price,
  reason, indicators). ``execute_signals`` validates via
  ``Portfolio.can_open_position``, sizes via ``PositionSizer``, and optionally
  executes via ``OrderExecutor``.
* **Multi-symbol:** per-symbol DataFrames in ``_bars``; supports dict of
  strategies (one per symbol) or single strategy reused.
* **Dry-run:** when ``dry_run=True`` signals are generated and logged but no
  orders are created.
* **DB logging:** ``_save_signal_to_db`` auto-creates portfolio row if missing
  and drops order_id FK when order not yet persisted, so logging never blocks
  trading. Logs to ``strategy_signals`` with ``executed`` flag and ``skip_reason``.
* **State:** ``get_state``/``load_state`` snapshot bars, indicators, last targets
  for Step 20 recovery. ``to_dict``/``from_dict`` are lossless for bars.
* **Sizers:** ``FixedQuantitySizer``, ``FixedDollarSizer``,
  ``PercentagePortfolioSizer`` are minimal implementations; Step 14 will expand
  to risk-based/ATR/Kelly.

Re-exported from ``strategy/adapter.py`` so both
``from backtest.forward.strategy_adapter import StrategyAdapter`` and
``from backtest.strategy.adapter import StrategyAdapter`` work.


### `live/market_data_handler.py` (Step 10)
Normalization and bar aggregation:

* **Normalization:** Converts many broker formats (mStock uses `o/h/l/c/v/t`,
  `tradingsymbol/ltp`, etc.) into standard `{symbol, timestamp, bid, ask,
  last, open, high, low, close, volume, timeframe}`. Missing bid/ask estimated
  from last with 0.1% spread.
* **Bar aggregation:** `BarBuilder` per symbol/timeframe aggregates ticks into
  OHLCV, aligns to boundaries via `TimeManager.align_to_timeframe`, closes bar
  when aligned time advances. Supports 1min/3min/5min/15min/30min/1hr/1day.
* **Multi-symbol:** `_bar_builders` dict symbol->timeframe->builder, `_tick_buffers`
  bounded deques (prevents memory leaks).
* **Observer:** `on_tick_received` and `on_bar_closed` callbacks with isolation
  (one failing callback doesn't break others).
* **Reconnection:** `connect_to_feed` with backoff (2^attempt, max 30s), max attempts,
  auto-reconnect flag.
* **DB cache:** `_store_bar_to_db` inserts into `market_data_cache` with unique
  constraint handling (duplicate bars ignored).
* **Feeds:** Abstract `BrokerFeed`, `MockBrokerFeed` (in-memory inject for tests),
  `MStockBrokerFeed` wrapping existing `MStockSource` (wire to live/mstock.py per
  task tracker).

### `live/data_validator.py` (Step 11)
Quality checks:

* **OHLC:** high>=open/low/close, low<=open/high/close
* **Price:** min 0.01, max 1M, not zero/negative
* **Bid/Ask:** bid<=ask, optional last between bid/ask with tolerance
* **Volume:** non-negative, optional zero check, anomaly vs rolling avg (5x default)
* **Timestamp:** chronological, future check with tolerance, gap detection
  (intraday 5min, daily 3 days) via `_check_gap` using timeframe to choose max
* **Spike:** Z-score vs rolling window (20 bars, min 10 history), threshold 3 std
  (strict 2, lenient 4)
* **Stats:** total/failed/failure rate, failures by code, consecutive failures
  with alert after 10
* **Config:** `ValidatorConfig` with strictness levels adjusting thresholds,
  YAML loader.

### `live/time_manager.py` (Step 12)
NSE time handling:

* **Market hours:** NSE 09:15-15:30 IST default, NYSE 09:30-16:00 ET as reference,
  pre-open 09:00, post-close 16:00, configurable
* **Holidays:** Built-in partial lists for 2024 (NSE 14 holidays, NYSE 10),
  injectable via constructor or YAML
* **Open checks:** `is_market_open` checks weekend (Sat/Sun) + holidays + time
  range; `is_pre_market` and `is_after_hours` for session phases
* **Next open/close:** Iterates up to 365 days, skips weekends/holidays, handles
  equality (<= open returns today open)
* **Bar alignment:** `align_to_timeframe` floors to boundary (1min->second=0,
  5min->minute//5*5, etc.), supports 1min/3min/5min/15min/30min/1hr/day/week/month
* **Mock time:** `set_mock_time` and `advance_mock_time` for controllable clock
  in tests – `get_current_time` returns mock if set
* **Latency:** `measure_latency` samples, `get_latency_stats` mean/p95/min/max


### `forward/engine.py` (Step 20)
Main orchestration engine that ties all components:

* **Config:** ``ForwardTestingConfig`` with 7 sections (portfolio, strategy,
  risk, execution, sizing, data, system). Loaded from YAML with validation;
  explicit missing file raises, implicit missing uses defaults.
* **Placeholders:** Steps 10-12, 15-19 not yet fully implemented, so engine
  provides minimal but functional mocks:
  - ``MockMarketDataHandler``: wraps DataSource for backtest replay, inject_bar for tests
  - ``MockDataValidator``: OHLC sanity (high>=low, high>=close, low<=close, close>0)
  - ``MockTimeManager``: always market open for backtest
  - ``MockRiskManager``: checks ``can_open_position`` and drawdown limits
  - ``MockStopManager``: no-op, placeholder for trailing stops
  - ``MockPerformanceCalculator``: tracks equity curve and simple metrics
* **State:** ``StateManager`` saves full system state (portfolio.to_dict,
  adapter.get_state, performance equity curve) atomically via temp file replace.
  Restores on ``initialize_system``.
* **Loop:** ``run_loop`` for live (polls data_handler, validates, updates prices,
  checks stops, generates signals via adapter, updates performance, saves state
  periodically, heartbeat every 60s, slow-loop warning >1s). ``_run_backtest_mode``
  replays historical candles from DataSource bar by bar.
* **Lifecycle:** ``on_start``, ``on_stop``, ``on_error``, ``on_market_open/close``
  hooks with isolation (one failing hook doesn't break others). ``pause``/``resume``
  sets portfolio status and error count.
* **Error handling:** try/except around loop, error count, auto-pause after
  ``max_errors_before_pause`` (default 5), signal handlers for SIGINT/SIGTERM
  save state before exit.
* **Monitoring:** heartbeat logs equity/cash/positions/exposure/errors, loop time
  tracking, memory monitoring placeholder.
* **Modes:** dry_run (signals but no orders), backtest_mode (replay historical),
  live (polling).

Dockerfile and systemd service included per spec.


### `simulator/position_sizing.py` (Step 14)
Six methods, all pure Decimal:

* **Fixed qty/dollar/%:** trivial division, but with equity and price resolution
  from signal/portfolio.
* **Risk-based:** ``qty = (equity * risk_per_trade) / (price * stop_loss_pct)``
  — if stop is hit, loss equals risk fraction. The most common professional
  method.
* **Volatility/ATR:** ``qty = risk_amount / (ATR * multiplier)`` — higher ATR
  => smaller position, keeping dollar volatility constant. ATR priority:
  explicit param > signal indicators (``atr``/``ATR``) > instance default.
* **Kelly:** ``f* = p - q/b`` where ``b=avg_win/avg_loss``, then
  ``qty = equity * f* * kelly_fraction / price``. Negative Kelly => 0 (don't bet).
  Half-Kelly (0.5) is default — full Kelly is too volatile for most.

Constraints applied after raw sizing, in order: round lots (floor), min trade
value (dust filter => 0), max position value, max position % of equity, max
gross exposure %, max open positions. Each returns ``SizingResult`` with
``constrained`` flag and reason for audit.

Config loader: ``config/position_sizing.yaml`` with 8 profiles (fixed,
fixed_dollar, percentage, conservative, aggressive, volatility, kelly, nse_fo).
Profiles override default; unknown keys raise ValidationError.

Integration: ``StrategyAdapter`` accepts any object with
``calculate_position_size(signal, portfolio, ...)`` — the new ``PositionSizer``
satisfies it, so ``adapter = StrategyAdapter(..., position_sizer=PositionSizer(...))``
works. The adapter's old minimal sizers now re-export from simulator for
backward compatibility.



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
| `test_simulator_fill.py` | Commission models, slippage, position impact, graph persistence | 106 |
| `test_simulator_slippage.py` | 5 slippage models, tiers, time-of-day, limit caps, statistics | 101 |
| `test_simulator_fees.py` | NSE + US fee stacks, 10 broker presets, volume tiers, FX | 109 |
| `test_simulator_execution.py` | Liquidity caps, queue position, rejections, TIF, determinism | 99 |
| `test_strategy_adapter.py` | StrategyAdapter bridge, Signal model, sizers, multi-symbol, dry-run, DB logging, no-lookahead, state persistence | 20 |
| `test_simulator_position_sizing.py` | PositionSizer 6 methods, constraints, risk params, config loader, adapter integration | 25 |
| Pre-existing | Backtest engine, mStock | 25 (+4 skipped) |

**Drift guards** — these fail loudly if two sources of truth diverge:

- `test_enums_match_the_orm` / `test_enums_match_the_sql_check_constraints`
- `test_sqlite_migration_file_matches_orm`
- `test_simulator_does_not_import_engine_or_forward`
- Alembic autogenerate produces no diff against the ORM