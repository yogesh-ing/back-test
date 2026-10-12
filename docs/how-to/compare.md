# How to use Compare strategies

**Page:** `/compare` · **Use it when…** you need a difference you can trust: two to
four combinations under *identical* conditions.

Index: [USER-GUIDE.md](../USER-GUIDE.md)

## Step 1 — pick the comparison mode

| Mode | Held constant | Varies |
|------|---------------|--------|
| **Compare Strategies** (default) | symbol, date range, capital, timeframe | strategy and/or parameters, up to 4 slots |
| **Test Generalization** | one strategy + one parameter set | up to 4 symbols — does the edge survive other instruments? |

## Step 2 — shared config

Symbol, From, To, Capital, and a **shared Timeframe** (a 5-minute and a daily run
are not comparable, so it is a condition rather than a per-slot choice). `Fast
Preview` applies to every slot — approximate engine, not what paper/live reproduces.

## Step 3 — slots

`+ Add slot` / remove, set each slot's strategy and parameters, then
`▶ Run comparison`. In generalization mode the strategy + params live in **one**
shared editor and each slot is a *symbol* instead.

## Reading the results

| Tab | What it shows |
|-----|---------------|
| `Metrics Table` | Side-by-side numbers |
| `Equity Curves` | Every curve **indexed to 100 at the first shared date**, so the chart compares performance rather than starting balances |
| `Drawdown` | Overlaid drawdown |
| `Comparison` | The ranking view (Sharpe / return / drawdown) |

**Comparison history** below the results stores past comparisons from the same run
ledger; a comparison that is not in the ledger says so rather than appearing saved.

## API

`POST /api/backtest/run-many` → `{shared: {...}, slots: [{id, strategy, params}]}`,
history via `GET /api/backtest/runs`.
