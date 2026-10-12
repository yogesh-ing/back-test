# How to use Backtest

**Page:** `/backtest` (`/` redirects here) · **Use it when…** you want historical
evidence for one strategy before risking anything.

Index: [USER-GUIDE.md](../USER-GUIDE.md) · engine detail: [BACKTEST-ENGINE.md](../BACKTEST-ENGINE.md)

## Configuring a run

1. **Strategy** — fed by `/api/strategies` (built-ins + plugins from
   `plugins/strategies/`). The hint under it shows which segment the strategy runs in.
2. **Instrument** — SymbolPicker tabs `All / Equity / Index / F&O` + search. A
   symbol with no bars is visible but not selectable and says so ("No data loaded.
   Go to Data tab → fetch data for this symbol"). Synthetic-only deployments get
   `Use generated demo instrument`.
3. **Timeframe**, **From / To**, **Initial capital**, then the strategy's own
   parameters (auto-generated from its schema).
4. Tick **Fast preview** only when you want the approximate vectorised engine —
   faster, but *not* what a paper or live run reproduces.
5. `▶ Run backtest`.

## Reading the results

1. **Provenance strip** — data source and run identity, so a number is never
   divorced from where it came from; the persist badge marks a run saved to history.
2. **Metric cards** — P&L, Win Rate, Max Drawdown, Sharpe, Trades. Win rate counts
   **closed** trades only: a run still holding shows `—` rather than a misleading
   `0.00 %`, and the Trades card notes open positions.
3. **Chart tabs:** `Equity curve` · `Drawdown` · `Price & signals`.
4. **Trade ledger** with pagination; open rows read `⏳ Open` instead of ✅/❌.
   Actions:
   - `Save to compare` — carries the run to `/compare`.
   - `Export CSV` — the trade list.
   - `Open in playground` — pre-fills `/forward` with this run for a live-clock replay.
5. **Metric sections** — four collapsible blocks of richer metrics below the cards,
   preceded by a non-dismissable sample-size banner when the trade count is too
   small to trust.
6. **Run checks** (collapsed by default) — benchmark, cost shock and Monte Carlo
   panels that qualify the headline numbers. A check that could not run says why
   rather than silently disappearing.
7. **Certification** — an advisory eight-check traffic light (✅/⚠️/❌/⬜ unknown).
   "Unknown" is a real state and never counts as a pass; nothing here disables a
   button.
8. **Tune This** — hands the run to **Optimize** with the common fields prefilled.
   It never starts a search by itself; you still press `Start optimization`.
9. **Recent runs** — tiles from the server-side run ledger
   (`GET /api/backtest/runs`); opening a tile reads the stored payload back. A run
   whose ledger write failed says so instead of pretending it was saved.

## Next steps

- Is it better than a baseline? → [compare.md](compare.md)
- Are the parameters robust? → [optimization.md](optimization.md)
- Watch it on a live clock → [engine-playground.md](engine-playground.md)
- Deploy to paper → [portfolio.md](portfolio.md)

## API

`POST /api/backtest/run` (single), `POST /api/backtest/run-many` (slots),
`GET /api/backtest/runs[/<id>]` (history ledger).
