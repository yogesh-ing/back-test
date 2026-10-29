# Manual Test Plan — Deterministic Options Replay

This checklist is for a developer manually validating the internal replay harness with approved historical CSVs. It tests the mechanics of the existing forward options path; it is **not** a profitability test, fill-quality assessment, after-hours paper-trading mode, or replacement for the mandatory live soak.

> **Data note:** no historical index-options CSVs are included in this repository. Use data you are licensed and approved to handle. A generated/toy dataset can check that the CLI runs, but is not market evidence.

## 1. Prepare a small, known dataset

Start with one underlying (`NIFTY` or `BANKNIFTY`), one session, and one-minute bars. Use a short interval that includes enough bars for the configured strategy to emit a signal and a following fill bar. Keep the underlying CSV outside the options directory; `--options-dir` reads every `*.csv` in that directory as a contract file.

Required input columns and timestamp conventions are documented in [Deterministic Options Replay](OPTIONS-DETERMINISTIC-REPLAY.md#input-format-and-time-normalization). In short:

- Underlying: `timestamp,open,high,low,close` (`volume` optional).
- One option CSV per contract: `timestamp,instrument_token,trading_symbol,underlying,expiry,strike,option_type,lot_size,open,high,low,close` (`volume`, `bid`, and `ask` optional).
- `option_type` is `CE` or `PE`. Include the expiry and strikes the configured structure may select, and quotes for all its legs.
- Timestamps label candle opens by default. Naive timestamps are interpreted as `Asia/Kolkata`; timezone-aware timestamps are converted to UTC. For end-labelled source candles, pass `--timestamp-kind close`.
- Keep genuine gaps and missing option rows. The loader does not carry quotes forward. For the first test, prefer complete, contiguous one-minute bars so the expected next-bar fill is unambiguous.

Example local layout:

```text
/tmp/nifty-replay-test/
├── nifty_1m.csv
└── options/                    # option CSVs only
    ├── call_strike_1.csv
    ├── call_strike_2.csv
    ├── put_strike_1.csv
    └── put_strike_2.csv
```

## 2. Check the CLI is available

Install the project dependencies as described in the README Quick Start (`pip install -r requirements.txt`), then use your installed Python environment. The examples below use a Bash-like shell, `.venv/bin/python`, and `/tmp`; substitute your interpreter and output paths if they differ.

```bash
PYTHONPATH=src .venv/bin/python -m backtest replay --help
```

In Windows PowerShell, set `$env:PYTHONPATH = "src"`, use `.\.venv\Scripts\python.exe` for the Python executable, and replace Bash backslash line continuations with PowerShell backticks.

## 3. Run a baseline replay

Use the same strategy, parameters, expression, and timeframe you intend to validate. The example below uses the built-in directional strategy and the forward engine's default expression; replace these settings if your test plan specifies another configuration.

```bash
PYTHONPATH=src .venv/bin/python -m backtest replay \
  --underlying-csv /tmp/nifty-replay-test/nifty_1m.csv \
  --options-dir /tmp/nifty-replay-test/options \
  --underlying-symbol NIFTY \
  --timeframe 1min \
  --timestamp-kind open \
  --strategy directional_options \
  --strategy-params '{}' \
  --expression '{}' \
  --export /tmp/nifty-replay-baseline.json \
  --verify-no-lookahead
```

The exhaustive truncation check is O(number of bars squared). For a first run, use a short dataset; omit `--verify-no-lookahead` on larger exploratory runs and enable it again for a short gate/regression slice.

### Baseline acceptance checklist

- The command exits successfully and prints `label=REPLAY`, a data fingerprint, and an artifact path.
- With `--verify-no-lookahead`, output says `no_lookahead=PASS`; the checkpoint count equals the number of normalized underlying bars.
- The exported JSON has `label: "REPLAY"`, non-empty `data_fingerprint` and `config_fingerprint`, `summary.db_writes: 0`, and a no-lookahead result marked as passed.
- Review `data_quality` for the normalized bar count, quote-row count, session timezone, timestamp convention, and any underlying gaps. Investigate unexpected gaps or missing contracts before interpreting the run.
- A run may legitimately have zero intents or fills if the selected strategy produces no signal or the required contracts/quotes are unavailable. That verifies loading/export, but **does not** complete the intent/fill checks below.

## 4. Check intent timing and fills (when the run generates an intent)

Inspect the exported `journal` for `intent`, `fill`, and `rejected` events. For each fill, verify:

- `intent_bar_index + 1 == fill_bar_index`.
- The intent timestamp is the close of decision bar T; the fill timestamp is the next bar's open. On contiguous one-minute bars, these coordinates meet at the same boundary.
- `quote_reads` contains the selected option legs and uses phase `open`; each fill price is the recorded next-bar option open.
- No fill uses a previous bar's quote. If a required leg is absent, the order must be rejected rather than assigned a carried or synthetic price.

A small deterministic regression check is to repeat the exact baseline command with a second export path, then compare the JSON files:

```bash
# Repeat the same command, changing --export to /tmp/nifty-replay-repeat.json
cmp /tmp/nifty-replay-baseline.json /tmp/nifty-replay-repeat.json
```

The files should be identical for unchanged input/configuration. Keep both artifacts outside the repository unless the team's evidence-retention process says otherwise.

## 5. Exercise one failure at a time

First run a clean baseline. Use its journal to identify the selected contract token and the next-bar **open** timestamp, then rerun with one fault flag added. Use an ISO-8601 timestamp (prefer `YYYY-MM-DDTHH:MM:SS+05:30` for NSE local time) and quote the whole flag argument in the shell if it contains special characters or spaces.

| Scenario | Add to the baseline command | Expected evidence |
| --- | --- | --- |
| Missing quote for a selected leg | `--drop-quote 'TOKEN@FILL_BAR_OPEN'` | Rejection; no fill from an earlier quote. |
| Missing contract at the fill window | `--drop-contract 'FILL_BAR_OPEN;EXPIRY;STRIKE;CE'` (use the selected contract's actual identity) | Rejection; no fabricated contract/quote. |
| Broker rejection | `--reject-entry 'FILL_BAR_OPEN'` | A rejected event with the injected-reject reason; no fill. |
| Lot-size change at fill | `--lot-size-override 'TOKEN@FILL_BAR_OPEN=NEW_SIZE'` | Rejection, not a silent order resize. |
| Stale feed while holding a position | `--stale-window 'START_BAR_OPEN..END_BAR_OPEN'` after an observed fill | A feed-halt event; the last valid mark is retained and strategy decisions are suppressed during the halt. |
| Lot-size change while holding | `--lot-size-override 'TOKEN@HELD_BAR_OPEN=NEW_SIZE'` after an observed fill | Feed halt / suppressed decisions; the prior valid mark is retained. |

Use timestamps from the first run and the actual selected leg identity; a fault aimed at an unused token or a bar with no pending/held position may not exercise the intended path. Do not combine faults until each individual scenario is understood.

## 6. Record results and boundaries

Keep the JSON export with the approved internal evidence process and record the input file hashes, strategy/config, command, and observed event counts. Do not commit licensed/vendor data or generated artifacts to Git by default.

This harness does not validate bid/ask fills, slippage, market impact, broker behavior, live quote-stream behavior, or fill quality. OHLC exit tests use the documented conservative rule (stop first if stop and target are touched in one candle). Restart recovery is not supported or tested in V1. A successful replay is mechanism/regression evidence only; **live soak remains mandatory**.

## 7. Optional automated checks

For code-level regression checks from the repository root:

```bash
PYTHONPATH=src .venv/bin/pytest -q tests/replay
PYTHONPATH=src .venv/bin/pytest -q \
  tests/forward/test_options_forward_mtm.py \
  tests/forward/test_options_exit_policy.py \
  tests/forward/test_options_expiry_settlement.py
```
