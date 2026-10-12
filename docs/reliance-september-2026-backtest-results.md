# RELIANCE backtest — every timeframe, September 2026

Data: `tools/out/reliance_1min_sept2026.csv` — 4,817 one-minute bars, 13
sessions, 2026-09-02 → 2026-09-30, naive IST, `source: mstock`. Nothing here is
synthetic; every number comes from a real broker export.

Reproduce (two commands, no broker login):

```bash
PYTHONPATH=src python scripts/setup_reliance_demo_db.py
BACKTEST_SOURCE=db FORWARD_TEST_DB_URL=sqlite:////abs/path/tools/out/reliance.db \
    PYTHONPATH=src python -m backtest.web.app --port 5003
```

Then `POST /api/backtest/run` per timeframe, or set the timeframe in the UI.

---

## 1. Bar counts: what is stored vs what the engine receives

The whole point of a multi-timeframe test is that the engine sees the same
period at every granularity. These counts are the proof — the middle column is
the independent bucket count from
`scripts/diagnose_timeframe_alignment.py`, computed straight from the stored
minutes without going through the backtest.

| Timeframe | bars the engine received | bars the DB derives | match |
|---|---|---|---|
| 1min | 4817 | 4817 | ✓ |
| 5min | 975 | 975 | ✓ |
| 10min | 494 | 494 | ✓ |
| 15min | 325 | 325 | ✓ |
| 30min | 169 | 169 | ✓ |
| 1hour | 91 | 91 | ✓ |
| 4hour | 26 | 26 | ✓ |
| 1day | 13 | 13 | ✓ |
| 1week | 5 | 5 | ✓ |

Before the end-date fix the left column was short by one session on every row
(4447 / 900 / 456 / 300 / 156 / 84 / 24 / 12), always the last day. See
`DATA-INGEST-AND-TIMEFRAMES.md`.

## 2. `sma_crossover` fast=5 / slow=20

| Timeframe | Bars | Return | Trades | Sharpe | Max DD | Win % |
|---|---|---|---|---|---|---|
| 1min | 4817 | −3.37% | 141 | −6.13 | −5.16% | 29.1 |
| 5min | 975 | −3.90% | 29 | −6.77 | −5.36% | 24.1 |
| 10min | 494 | −4.52% | 19 | −9.21 | −5.12% | 21.1 |
| 15min | 325 | −3.57% | 10 | −7.90 | −3.70% | 10.0 |
| 30min | 169 | −1.24% | 3 | −2.73 | −1.52% | 0.0 |
| 1hour | 91 | −0.78% | 1 | −2.97 | −1.56% | 0.0 |
| 4hour | 26 | 0.00% | 0 | — | — | — |
| 1day | 13 | 0.00% | 0 | — | — | — |
| 1week | 5 | 0.00% | 0 | — | — | — |

**The three flat rows are warmup, not defects.** `slow=20` needs 20 bars before
it can emit a first signal, and those timeframes hold 26 / 13 / 5. The app logs
a warning when a run produces no signals for this reason.

Do not read the falling trade count as "coarser is safer": every row is the same
losing month, and the coarse rows simply take fewer, larger positions — 30min
and 1hour traded once and were stopped out once. The declining trade count is
the identical strategy being sampled less often, nothing more.

## 3. The same period with parameters sized to each timeframe

| Timeframe | Bars | Params | Return | Trades |
|---|---|---|---|---|
| 4hour | 26 | fast=3 slow=8 | −4.31% | 1 |
| 4hour | 26 | fast=5 slow=20 | 0.00% | 0 |
| 1day | 13 | fast=2 slow=5 | −3.34% | 1 |
| 1day | 13 | fast=3 slow=8 | 0.00% | 0 |
| 1week | 5 | fast=2 slow=3 | 0.00% | 0 |
| 1week | 5 | fast=2 slow=4 | 0.00% | 0 |

`1week` never trades: 5 bars cannot warm up even a 2/3 pair and still leave a
bar to act on. A one-month test at weekly granularity has five samples — it is
not a meaningful backtest window, whatever the parameters.

## 4. `buy_and_hold` — the control

A strategy that always enters, so a zero here would mean a data problem rather
than a parameter one. Every timeframe trades.

| Timeframe | Bars | Strategy | Buy & hold benchmark |
|---|---|---|---|
| 1min | 4817 | −8.39% | −8.64% |
| 5min | 975 | −8.46% | −8.71% |
| 10min | 494 | −8.46% | −8.71% |
| 15min | 325 | −8.14% | −8.33% |
| 30min | 169 | −8.14% | −8.33% |
| 1hour | 91 | −8.57% | −8.78% |
| 4hour | 26 | −8.78% | −8.98% |
| 1day | 13 | −9.46% | −9.60% |
| 1week | 5 | −8.78% | −8.87% |

The spread (−8.14% to −9.46%) is **entry timing, not data**: signals are
evaluated on a closed bar and executed on the next one, so each timeframe buys
at a different point on 2–3 September. The monthly close-to-close decline was
−8.64%.

---

## Reading this honestly

* **Every timeframe works**, from 1min to 1week, on one stored 1-minute series.
  Bar counts match the independent derivation exactly, so nothing is being
  lost, duplicated or invented on the way to the engine.
* **This is a losing month for RELIANCE** (−8.6% close-to-close), and no
  timeframe turns that into a profit with a 5/20 crossover. A parameter sweep
  is the next step if you want to know whether *any* crossover setting worked —
  one parameter pair is not evidence about a strategy.
* **Do not generalise from 13 sessions.** The export covers 13 of the 20
  trading days in the range; the other 7 were not holidays and are simply
  absent (see `DATA-INGEST-AND-TIMEFRAMES.md`). Treat these returns as a
  smoke test of the pipeline — which is what they are and what they show —
  not as a judgement on the strategy.
* `1week` and `1day` over one month have 5 and 13 bars. Timeframes that coarse
  need quarters or years of data before the numbers mean anything.
