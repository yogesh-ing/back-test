# How to use Market data

**Page:** `/data` · **Use it when…** a symbol is missing from the Research
dropdowns, or the stored cache is stale. A symbol must have data before
Backtest / Optimize / Compare list it.

Index: [USER-GUIDE.md](../USER-GUIDE.md) · detail: [DATA-SOURCES.md](../DATA-SOURCES.md),
[DATABASE.md](../DATABASE.md)

## Fetching historical data

1. **Timeframe** — `1min / 5min / 15min / 1hour / 1day`.
2. **From / To** — the date range to pull.
3. **Instruments** — `All / Equity / Index / F&O` tabs, search, then multi-select
   (`Clear` resets). Only instruments on the *active tab* are fetched when you
   leave boxes unticked.
4. **Token status** — real data needs an authenticated broker: use the broker chip
   in the header to `Login` first (mStock: ID/password/PIN/TOTP; Dhan: Client
   ID/PIN/TOTP).
5. `⬇ Start fetch`, watch **Fetch progress**, `⏹ Stop fetch` to cancel.

### Fetch progress panel

Progress bar · status text · `n/m symbols` · total bars · elapsed · the symbol
currently fetching · a per-symbol error list. The header's data-fetch indicator
mirrors it and is clickable to stop.

### Why re-runs are cheap

Fetches are **coverage-aware** (only missing days are re-requested), chunks are
paced adaptively, outages trip a circuit breaker instead of grinding for days, and
any lost-chunk count is reported rather than hidden. NSE-preferred over BSE for
dual-listed symbols.

## Data inventory

`⟳ Refresh` → totals (symbols / bars / timeframes) plus a per-symbol table. This
is how you answer *"do I even have RELIANCE 15 min since March?"*

## How the app picks a source

Real data by default: if the configured source is disabled (e.g. synthetic) it
falls back through **DB bars → broker feeds → CSV**, never silently to synthetic.
Synthetic candles are only used when you explicitly start with `--source synthetic`
or press `Use generated demo instrument`.
