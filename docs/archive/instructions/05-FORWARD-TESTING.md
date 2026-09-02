# Card 05 — Forward testing / paper trading (`forward/`) — NEW WORK

**Prerequisite:** Cards 00–04. **Builds:** paper trading that REUSES the same
strategies, cost model, and SL/TP rules so results reconcile with the backtester.

> This layer does not exist in the reference implementation — build it to this
> spec. Live paper trading needs network + market-data access (Card 07).

## F1. `forward/portfolio.py` — `Portfolio`
- Tracks per-strategy **cash**, **position**, **entry price**, realized/unrealized
  PnL, and an equity series.
- **Per-strategy capital allocation** (e.g. `{"rsi_reversion":50_000,
  "sma_crossover":30_000}`).
- Methods: `allocate(strategy, capital)`, `mark_to_market(prices)`, `equity()`,
  `snapshot()` (JSON-serializable) and load-from-snapshot.

## F2. `forward/broker.py` — `SimulatedBroker`
- Consumes a **target position** (per symbol/strategy) + the current bar; produces
  fills using the **same cost model** `(commission+slippage)×|Δposition|` and the
  **same intrabar SL/TP rule** (Card 00 rule 5).
- Fill reference configurable; **default close-to-close** to match the
  backtester so walk-forward reconciles.
- Emits trade records (entry/exit, price, bars held, PnL).
- Interface must be implementable later by a `LiveBroker` (the only seam that
  would send real orders — **not** built now).

### ⭐ Reconciliation trick (do this exactly or F4 test 20 fails)
The broker's per-bar update **must mirror the engine's per-bar math** so a
bar-by-bar replay equals the vectorized backtest. Implement `step` as the engine
loop body for ONE bar (equity compounds by `net`, not shares):
- Track `held` (position carried into the bar), `entry_price`, `prev_close`,
  `blocked`.
- `desired` passed in = the strategy target for the **previous** bar (caller does
  the shift; first bar desired = 0).
- Unblock when `desired == 0`; `want = 0 if blocked else desired`.
- If `want != held`: change position at the **prior close**; set
  `entry_price = prev_close` when opening.
- `turnover = |held − prev_held|`; `bar_cost = turnover × cost_rate`.
- Return `r = held × (end/prev_close − 1)` where `end = close` unless an intrabar
  stop/target is hit (then `end` = that level, **stop checked first**); on a hit,
  add exit turnover cost, go flat, set `blocked = True`.
- `net = r − bar_cost`; `equity *= (1 + net)`; then set `prev_close = close`.

With **no** stop/target this reduces exactly to `held×pct_change − turnover×cost`
— identical to the vectorized engine. **Verify:** a single strategy allocated
capital `C` over a slice must produce the **same equity curve** as
`Backtester(initial_capital=C)` on that slice.

## F3. `forward/paper.py` — event-driven runner + CLI
- **Event loop:** step **bar by bar**; on each new bar fetch the **trailing
  window**, call the *same* `strategy.generate_signals(window)`, take the
  **latest** signal, pass to `SimulatedBroker`, update `Portfolio`.
- **F3a Offline walk-forward (build FIRST, no network):** replay a held-out slice
  through the loop; assert equity/metrics **reconcile with the vectorized
  backtester** on the same slice.
- **F3b Live paper (needs connectivity):** poll the mStock source on a
  **schedule**; **persist state** (SQLite/JSON) each step (restart-safe); emit a
  **daily PnL + positions** report; optional alerts.
- **CLI — add `papertrade`:** `--strategies a,b,c`, `--alloc name=amount`
  (repeatable), `--source`, `--symbol`, `--interval`, `--mode
  {walkforward,live}`, `--from/--to` (walk-forward), `--state-file`,
  cost/risk flags, `--report`.

## F4. Forward invariants (pinned by Card 06 tests 20–22)
- Offline walk-forward equity over a slice **equals** the vectorized backtest over
  the same slice (float tolerance) for a no-stop strategy.
- Per-strategy capital is isolated (one strategy's fills never touch another's
  cash).
- State snapshot round-trips (save → load → identical portfolio).

**Verify:** run Card 06 tests 20–22, plus:
```
$env:PYTHONPATH="src"
python -m backtest papertrade --mode walkforward --strategies sma_crossover --alloc sma_crossover=100000 --source synthetic --symbol DEMO --from 2021-01-01 --to 2024-01-01
```