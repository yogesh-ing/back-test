# Deterministic Options Replay (Internal V1)

> **Purpose:** developer and Phase 1 gate-evidence tool for the existing forward
> options engine. This is not after-hours paper trading, a strategy-performance
> backtester, or evidence that live execution will be safe or profitable.
>
> **Live soak remains mandatory.** Replay evidence supplements the gate; it does
> not replace the required live soak. For a hands-on checklist using your own
> CSVs, see the [manual test plan](OPTIONS-DETERMINISTIC-REPLAY-MANUAL-TEST.md).

## Architecture and scope

Replay sends historical rows through the existing forward decision path:

```text
CSV candles → ReplayDataset → point-in-time feed + simulated clock
            → StrategyRunner → OptionsBridge → selector/structure
            → OptionPaperBroker → journal + deterministic JSON evidence
```

The replay does **not** use the disabled/dedicated
`engine/option_backtest_driver.py` loop. It adds only feed, clock, deferred
next-bar fill, audit, and OHLC-exit seams around the forward path. No database
is opened or written (`db_writes: 0`); results are tagged `REPLAY`. The export
contains the event journal and accounting reconciliation for regression and
gate review, not a performance dashboard or performance claim.

V1 supports one index underlying (`NIFTY` or `BANKNIFTY`) and 1-, 5-, or
15-minute replay bars. Start with one-minute data. Option data must be one CSV
per contract. The repository currently contains no historical index-options
OHLCV CSVs (the repository's stock-list CSV is not options data), so approved
vendor files must be supplied by the developer running the replay.

## Input format and time normalization

Underlying CSV requires a timestamp plus `open,high,low,close`; `volume` is
optional. Option CSVs require the same OHLC columns and contract identity:

```text
timestamp,instrument_token,trading_symbol,underlying,expiry,strike,option_type,lot_size,open,high,low,close,volume
2026-09-15 09:15:00,C123,NIFTY26SEP24800CE,NIFTY,2026-09-24,24800,CE,75,101,103,100,102,1500
```

This row illustrates the CSV shape only; use timestamps from your data and a
non-expired contract expiry that is valid for the replay period.

`instrument_token`, `trading_symbol`, `underlying`, `expiry`, `strike`,
`option_type` (`CE`/`PE`) and `lot_size` may be constant columns within a
contract file; if `lot_size` is present per row, the row history is retained.
Optional `bid` and `ask` columns are counted in the data-quality report, but
are not used to create fills in V1.

- Source candles are interpreted as **bar-open timestamps** by default.
  Naive timestamps mean `Asia/Kolkata`; timezone-aware timestamps are converted
to UTC. Use `--timestamp-kind close` only when vendor timestamps label the
end of each one-minute source candle; the loader moves those timestamps back
one minute before normalization/resampling.
- NSE regular-session rows are normalized to weekdays, 09:15–15:30 IST. The
  5-/15-minute resampling grid is anchored at 09:15 IST and never crosses a
  session boundary. Incomplete underlying bars are dropped, including a
  partial final bar. Option rows are aggregated only from observations that
  exist; a missing row is never filled forward. A lot-size change inside one
  resampled option candle is rejected as ambiguous.
- Bar `T` becomes strategy-visible only after its close. The next entry window
  is bar `T+1` and uses its open. Each selected leg must have a fresh quote
  whose first constituent observation is exactly that next bar's open, with
  unchanged lot size. Missing legs, stale observations, or changed lots reject
  the entry; a held position with missing quotes halts strategy decisions and
  keeps its last mark without manufacturing a replacement quote.

The loader records source-file SHA-256 digests, a normalized data fingerprint,
bar/quote counts, bid/ask coverage, and underlying gaps in the JSON evidence.
Vendor-specific column mapping should be normalized before use rather than
silently guessed by the harness.

## Fill and exit semantics (important limitations)

- **Entry timing:** the signal is generated from closed-bar `T`; the intent is
  timestamped at `T` close; a deferred entry may fill only on the immediately
  following bar's open. An assertion checks bar indexes, timestamps, the exact
  leg quote audit, open prices, and fill prices. If the next bar or any leg is
  unavailable, the order is rejected rather than carried forward.
- **OHLC exits:** V1 has no tick or executable multi-leg quote path. If one
  candle touches both stop and target, stop is assumed first. A stop gap uses
  the open; otherwise the adverse per-leg extreme is used. An intrabar target
  uses the candle close (a target gap uses the open). The event journal records
  the selected exit and ambiguity detail.
- **One-minute OHLCV cannot validate bid/ask fills or slippage.** The harness
  does not establish fill quality, market impact, brokerage behavior, or live
  quote-stream behavior. Any optional bid/ask columns are diagnostic only; V1
  fills use recorded OHLC prices, with the paper broker's configured
  commission and no synthetic spread/slippage model.
- Restart recovery is **not** tested: the replay is in-memory/process-local
  and adds no ephemeral persistence. Do not report restart coverage.
- No strategy profitability, Sharpe ratio, return ranking, or performance
  dashboard is produced by this tool. A valid replay is mechanism evidence
  only. **Live soak remains mandatory.**

## Run the CLI

```bash
PYTHONPATH=src python -m backtest replay \
  --underlying-csv /path/to/nifty_1m.csv \
  --options-dir /path/to/one-csv-per-contract \
  --underlying-symbol NIFTY \
  --timeframe 1min \
  --timestamp-kind open \
  --strategy directional_options \
  --strategy-params '{}' \
  --expression '{"quantity":1,"strike_selection":"atm","exit":{"signal_flip":false}}' \
  --capital 1000000 \
  --commission-per-lot 20 \
  --export artifacts/nifty-replay.json \
  --verify-no-lookahead
```

`--option-csv FILE` may be repeated instead of (or in addition to)
`--options-dir`. `--export` is required so every CLI run yields a portable,
reviewable artifact. `--verify-no-lookahead` compares the complete run with
**every closed-bar prefix**; it is an exhaustive discrete-time truncation check
and costs O(number of bars squared), so omit it for large initial exploratory
runs and include it for gate/regression runs.

The CLI supports authored failures; timestamps below are bar-open timestamps
in ISO-8601 form:

```text
--drop-quote TOKEN@TIMESTAMP
--drop-contract 'TIMESTAMP;EXPIRY;STRIKE;CE|PE'
--lot-size-override TOKEN@TIMESTAMP=SIZE
--reject-entry TIMESTAMP
--stale-window START_TIMESTAMP..END_TIMESTAMP
```

Faults are preserved as rejections/feed halts in the journal. They do not
fabricate quotes. A lot-size change on an already selected next-bar order is a
rejection, not a silent quantity adjustment; a change on a held contract halts
marking/strategy decisions and retains the prior mark.

## Evidence and regression checks

The JSON file is labeled `REPLAY` and includes:

- replay engine version, run ID, strategy/config and config fingerprint;
- per-source and aggregate data fingerprints and quality report;
- decisions, intents, fills, exits, rejects, feed halts, and open-at-end state;
- internal cash/equity reconciliation after each bar and `db_writes: 0`;
- the no-lookahead prefix-comparison result when requested; and
- an explicit evidence boundary/limitations list.

Focused checks:

```bash
PYTHONPATH=src .venv/bin/pytest -q tests/replay
PYTHONPATH=src .venv/bin/pytest -q tests/forward/test_options_forward_mtm.py \
  tests/forward/test_options_exit_policy.py \
  tests/forward/test_options_expiry_settlement.py
PYTHONPATH=src .venv/bin/pytest -q tests/
```

Do not commit licensed/vendor market data or generated replay artifacts unless
project data-storage policy explicitly requires it. Keep gate evidence with
the team-approved artifact retention process.
