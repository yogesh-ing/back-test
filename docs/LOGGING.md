# Logging & Debugging

Every entry point now shares one logging setup, so the server tells you what it
actually did. Before this, nothing was configured: module loggers had no handler,
so `logger.info` / `logger.debug` lines were **silently dropped** and a `400`
from `/api/backtest/run` left no trace beyond a request line.

## Turn it on

```bash
# one-liner: readable trace of every request
PYTHONPATH=src python -m backtest.web.app --source synthetic --log-level DEBUG

# or via env (works for gunicorn/CLI too)
BACKTEST_LOG_LEVEL=DEBUG PYTHONPATH=src python -m backtest.web.app --source db

# keep a file you can grep / tail while the UI is open
PYTHONPATH=src python -m backtest.web.app --log-file logs/app.log
tail -f logs/app.log
```

| Switch | Values | Notes |
|---|---|---|
| `--log-level` / `BACKTEST_LOG_LEVEL` | `DEBUG`, `INFO` (default), `WARNING`, `ERROR`, `ALL` | `ALL`/`DEBUG` also un-quiets `werkzeug`, `urllib3`, SQLAlchemy |
| `--log-file` / `BACKTEST_LOG_FILE` | any path | appends; parent dirs created; console output still happens |
| `--debug` | flag | Flask debug + our DEBUG default noise; never in production |

`INFO` is the everyday setting: one line per request plus the decisions that
matter. `DEBUG` adds bars fetched, engine path, per-slot and per-poll detail.
`backtest.logging_config.configure_logging()` is idempotent — calling it twice
(from tests and from `create_app`) never duplicates lines.

## What you get

```
2026-08-28 09:15:45 INFO  backtest.web.app        [979be616]| → POST /api/backtest/run
2026-08-28 09:15:45 INFO  backtest.api.backtest   [979be616]| [run] strategy=rsi_reversion symbol=DEMO timeframe=1D→day range=2024-01-01..2024-02-28 capital=10000.0 params={'period': 60}
2026-08-28 09:15:45 WARN  backtest.api.backtest   [979be616]| [params] run/rsi_reversion rejected: period=60 is above max 50
2026-08-28 09:15:45 DEBUG backtest.data.synthetic [979be616]| [synthetic] DEMO 2024-01-01..2024-02-28 → 43 bars
2026-08-28 09:15:45 WARN  backtest.data.synthetic [979be616]| [synthetic] DEMO … only 43 bars (need > 50)
2026-08-28 09:15:45 WARN  backtest.web.app        [979be616]| ← POST /api/backtest/run 400 in 3.4 ms
```

Prefixes: `[req]` correlates every line of one HTTP call; the bracketed tags are
the subsystem — `[data]`, `[run]`, `[slot N]`, `[engine]`, `[adapter]`,
`[params]`, `[timeframe]`, `[forward]`, `[db]`, `[csv]`, `[synthetic]`, `[result]`.

**Request-id round trip.** Every response carries `X-Request-Id`, and every `/api`
error body carries the same value as `request_id`, which the UI appends to its
toast (`error: … [req 979be616]`). Grep that id to get the exact traceback:

```bash
grep 979be616 logs/app.log
```

Polling endpoints (`/api/forward/status`, `/api/broker/status`,
`/api/portfolio/summary|stream`, `/health`) log their request lines at `DEBUG`
only, so an INFO log stays readable while a bot is running.

## Recipes for the usual suspects

| Symptom | What the log says | Fix |
|---|---|---|
| Backtest runs, results are empty | `sma_crossover produced NO signals on DEMO (262 bars, params={'fast':200,'slow':250})` + `[engine] result is flat` | Shorten the indicator windows or widen the date range — warmup must fit inside it |
| Metric card disagrees with the trade table | `[adapter] metrics say trades=4 win_rate=100.00% but the trade table has 2 round trips with 0 wins` | Known defects **G1/G2** in `instructions/TASK-TRACKER.md` |
| `1D` and `1H` give identical charts | `[synthetic] interval 'hour' is not supported — returning daily business bars` | Only `--source db` honours timeframes today (**G6**) |
| `403` on forward Start | `[forward] /start refused for X/Y: broker session not authenticated (client=…) — open the broker auth modal or use mode=synthetic` | Authenticate, or switch the page's Mode to Synthetic |
| `400` "data error: …" | the data source's own warning line right above it | Read the source line: missing symbol, too few bars, no CSV file |
| Data fetch job silently did nothing | `[data] DEMO: API returned no bars …` / `[data] fetch job finished: 0/200 symbols ok, 200 failed` / `api_key=MISSING` | Set `MSTOCK_API_KEY`, check the instruments table |
| A new strategy file never shows up | `Skipping strategy module backtest.strategies.foo: SyntaxError: …` | Fix the file; the app keeps running on purpose |

## Library use

```python
from backtest.logging_config import configure_logging, get_logger, timed, request_id_scope

configure_logging("DEBUG", log_file="logs/scratch.log")
log = get_logger(__name__)                      # → "backtest.<module>"

with timed(log, "walkforward run", level=0):    # logs elapsed ms, tracebacks on error
    ...

with request_id_scope("job01"):                 # everything in here is tagged [job01]
    ...
```

`get_logger()` never double-prefixes, and `with_request_context(fn)` carries the id
into `ThreadPoolExecutor` workers (contextvars don't cross threads on their own) —
that is how the Compare page's parallel slots stay attributable.

## Files

| File | Role |
|---|---|
| `src/backtest/logging_config.py` | handlers, levels, format, request id, `timed()` |
| `src/backtest/web/app.py` | per-request logging, JSON errors + traceback, `--log-*` flags |
| `src/backtest/api/backtest.py` | `[run]` / `[slot N]` / `[params]` / `[result]` lines |
| `src/backtest/api/forward.py` | replay lifecycle, auth rejections, cursor advance |
| `src/backtest/runner.py`, `engine/backtester.py` | signal/engine diagnostics |
| `src/backtest/data/*` | per-source fetch summaries + interval warnings |
| `src/backtest/adapters/backtest_adapter.py` | card-vs-table reconciliation warning |
| `src/backtest/forward/engine.py` | the standalone engine reuses the same setup |

Tests: `tests/test_logging_config.py`. Tracker: gap **U1** in `instructions/TASK-TRACKER.md`.
