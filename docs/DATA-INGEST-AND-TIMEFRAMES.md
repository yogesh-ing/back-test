# Ingesting your own data, and what "correct" resampling looks like

Two things go wrong when a 1-minute export is turned into backtests, and both
are silent:

1. **The bars are stored under timestamps that do not mean what the resampler
   thinks they mean.** A file of naive *UTC* timestamps resampled to `1hour` /
   `4hour` / `1day` produces windows that start at 03:45 IST instead of 09:15.
   Nothing errors. `1day` and `1week` look perfect even when the file is UTC,
   because the UTC day boundary falls outside the NSE session either way — so
   *a daily check does not test this*.
2. **The export does not look like the parser expects** (a `ts_ist` column, a
   `date` + `time` pair, a bare `time` column). The quiet failure mode is the
   last one: a `time`-only column spanning several days, accepted as a full
   timestamp, stamps every row onto the day the import ran.

The tooling below exists to make both of those loud.

> **If you are moving to "download 1-minute candles only and derive everything
> coarser", read `docs/HANDOFF-2026-10-06-instrument-list-and-ingest.md` §4
> first.** The read path supports it already, and the storage timezone decision
> (§4.1 below) was **made** before the first bulk fetch (2026-10-06): convert
> on write, on the exchange session clock. Both writers bind
> `data.base.bar_timestamp()` and a one-off migration
> (`scripts/migrate_cache_ts_to_session_clock.py`) shifted the whole existing
> cache onto it — no re-fetch. `scripts/diagnose_timeframe_alignment.py` is
> the check that would have caught it, and remains the regression gate.

---

## 1. Load the CSV

```bash
PYTHONPATH=src python scripts/ingest_csv_to_mdc.py \
    --csv tools/out/reliance_1min_sept2026.csv \
    --symbol RELIANCE --timeframe 1min --source mstock
```

Dry run first if you want to see the shape without writing:

```bash
PYTHONPATH=src python scripts/ingest_csv_to_mdc.py --csv ... --symbol RELIANCE --dry-run
```

This is the offline equivalent of the Data tab fetch. It writes to the same
`market_data_cache` table with the same upsert semantics and the same value
checks (`_persist_bars`), so `DbSource` — and therefore every backtest, compare
and forward run — sees the data exactly as if the HTTP fetch had succeeded.

**Tolerant about shape:** comma/semicolon/tab/whitespace delimiters; `ts|time|
timestamp|date|datetime` and broker-suffixed variants such as `ts_ist`;
`open|o`, `high|h`, `low|l`, `close|c`, `volume|vol|v`; an optional symbol
column; ISO stamps, `YYYY-MM-DD HH:MM:SS`, and epoch seconds or millis.

**Strict about values:** positive prices and OHLC consistency are enforced, so a
bad bar is reported instead of stored. Duplicate timestamps are dropped (last
wins, matching the SQL upsert), because the table's
`UNIQUE(symbol, exchange, timeframe, ts)` would otherwise reject the batch.

What it will *not* do is rename your columns for you. The timestamp column it
chose is printed, and the storage timezone is *detected and reported*, never
assumed.

## 2. Check the alignment before you trust a backtest

```bash
PYTHONPATH=src python scripts/diagnose_timeframe_alignment.py --symbol RELIANCE
```

It answers three questions and prints the raw evidence for each:

* **What timezone are the stored timestamps in?** Verdict is `ist`, `utc` or
  `unknown`, from the first minute of each session (09:15 vs 03:45).
* **Do the derived buckets land where they should?** Per timeframe, the actual
  bucket start times and how many bars each holds. For an IST file the
  `4hour` buckets are `08:00`–`11:45` (165 min) and `12:00`–`15:29`
  (206 min) — window-labelled and clock-aligned, not broken. `10min`, `30min`
  and `1hour` similarly start on clock boundaries, so the first session bucket
  is short.
* **Does a resampled `1day` reproduce a native one?** Only when the symbol also
  has native daily rows; otherwise it says so rather than implying a pass.

## 3. Run the backtest

```bash
BACKTEST_SOURCE=db FORWARD_TEST_DB_URL=sqlite:///tools/out/reliance.db \
    python -m backtest.web.app --port 5003
```

`GET /api/data/coverage` then advertises every timeframe the stored set can
serve (from one 1-minute set: 1min, 5min, 10min, 15min, 30min, 1hour, 4hour,
1day, 1week), and `POST /api/backtest/run` accepts any of them.

`GET /api/data/freshness` reports the staleness chip. It is available on SQLite
as well as PostgreSQL — it used to raise on SQLite (`MAX(ts::date)` is
Postgres cast syntax) and report *"database unreachable"* for a database that
was answering queries fine.

The same cast syntax in the Data tab's coverage probe
(`data_manager._probe_covered_days`) made it return `{}` on SQLite, which is its
documented "probe failed, fetch everything" path — safe, but it meant the
resume/skip feature never once fired on the default profile: every re-run
re-fetched every symbol in full. `_day_expr()` now emits `date(ts)` on SQLite.

## The end date includes the day it names

`to_date`/`end` is a plain date — the UI control is an
`<input type="date">`. It means "up to and including this date".
`DbSource.get_candles` now resolves the window through `window_bounds()` into a
half-open `[start, end + 1 day)`, so the final session is read. A caller that
passes a real timestamp instead of a date still gets an exact instant:

| `end` | Resolved upper bound | Last session read |
|---|---|---|
| `2026-09-30` | `2026-10-01 00:00` (exclusive) | 2026-09-30 |
| `2026-09-30 12:00` | `2026-09-30 12:00` (inclusive) | 2026-09-30, up to noon |

The rule reads only the caller's dates, never the stored timestamps, so it
holds under both storage conventions and needs no change when the UTC write
path is fixed. That is verified, not asserted:
`tests/data/test_window_bounds.py` runs the rule against IST-stamped and
UTC-stamped caches (NSE's session fits inside one UTC calendar day — 09:15 IST
= 03:45 UTC, 15:29 IST = 09:59 UTC — so the end date keeps meaning "that
trading day" either way).

---

## Two conventions were in play — the write path is fixed (2026-10-06)

The export is naive **IST**. Until 2026-10-06 the Data fetch path stored UTC
stamps wearing an IST label, so:

* **this file** exercised the IST branch, where no offset problem appears;
* **the live database** held the broken branch, where the offsets did.

Both writers now bind the exchange session clock (`data.base.bar_timestamp()`),
and a one-off migration shifted every existing cache row onto it — the failure
mode documented below is history, kept here as the evidence for *why* the
storage convention matters. `scripts/diagnose_timeframe_alignment.py` is the
regression check: against today's cache it must report the session clock
(`ist`); a `utc` verdict means a writer regressed. Against the old UTC-stored
cache for the same data it reported:

```
1. STORAGE TIMEZONE
   naive UTC (session starts ~03:45)

2. BUCKET STARTS AS PRODUCED  (stored -> same instant in IST)
   1min   ['03:45:00', ...]  ->  ['09:15:00', ...]
   1hour  ['03:00:00', ...]  ->  ['08:30:00', ...]     <-- tz-sensitive
   4hour  ['00:00:00', ...]  ->  ['05:30:00', ...]     <-- tz-sensitive
   1day   ['00:00:00', ...]  ->  ['05:30:00', ...]

VERDICT: storage is UTC and resampling does not convert to IST, ...
```

The two tz-sensitive rows are the whole story, and one is worse than it sounds:

| timeframe | IST-stored | UTC-stored | what changes |
|---|---|---|---|
| 1min / 5min / 10min / 15min / 30min | 4817 / 975 / 494 / 325 / 169 | identical | clock-aligned either way; windows differ by up to 15 min at the open |
| 1hour | 91 | 91 | first bar is a **15-minute stub** (03:45–03:59) labelled as an hour |
| 4hour | 26 | **39** | **gains stub bars** — 13 extra "4-hour" bars built from 15-minute fragments |
| 1day / 1week | 13 / 5 | 13 / 5 | unaffected; a session never crosses UTC midnight |

So a `4hour` strategy was being fed 39 bars where 26 existed, and a `1hour`
strategy's first bar of every session was a quarter of an hour long. Both are
price-correct — no bar is invented or mispriced — which is exactly why nothing
errored and the only symptom was a backtest that "looked wrong". The read path
is not the place to fix this: the fix went to the writers (convert on write,
session clock), with the existing rows repaired once at rest — no per-read
compensation anywhere.

Anyone re-importing an export should note that `ingest_csv_to_mdc.py` will
*warn* rather than proceed quietly if it sees this shape:

```
storage timezone  naive UTC (session starts 03:45)
                  -> 1hour/4hour resampling will be offset. Run
                     scripts/diagnose_timeframe_alignment.py after ingesting.
```

---

## Worked example: RELIANCE, September 2026

`tools/out/reliance_1min_sept2026.csv` — 4,817 bars, 13 sessions, 2 Sep – 30 Sep
2026, naive IST, `source: mstock`. It is kept in the repo so these checks can be
re-run rather than re-argued; `tests/test_real_reliance_september.py` runs the
whole path and is skipped when the file is absent.

Facts worth knowing about this file. **One of them is not benign:**

| Observation | Reading |
|---|---|
| Every session is missing 15:16–15:19; six also miss 15:29 | A consistent 4-minute hole, not scattered corruption |
| 126 bars have `o == h == l == c` | The single-price closing auction, all at or after 15:15 |
| 370–371 bars/session | vs 375 for a full session, given the hole above |
| **8 of 21 weekdays in 2–30 Sep hold no bars** | **7 of them are missing data, not holidays** |

The file covers 13 sessions where 2–30 September 2026 contains 21 weekdays, one
of which (Monday 14 September, Ganesh Chaturthi) was an NSE holiday — so **20
sessions were available and 7 have no bars at all**:

```
2026-09-04 (Fri)   2026-09-16 (Wed)   2026-09-28 (Mon)
2026-09-07 (Mon)   2026-09-22 (Tue)
2026-09-10 (Thu)   2026-09-25 (Fri)
```

September 2026 has exactly one exchange holiday (14 September — confirmed
against the NSE/BSE calendars for 2026, which list Ganesh Chaturthi as the only
September closure). The other seven weekdays were trading days. Whatever
produced this export dropped them — a 1-minute fetch that returned 13 of 20
sessions, a third of the month missing.

That is worth more than any resampling question here: a backtest over this file
runs on 13 days of a 20-day month, and nothing in the app says so. The ingest
script now prints the weekday gaps for exactly this reason (it cannot know the
holiday calendar, so it lists them and lets you judge), and `market_holidays`
seeded from `tools/seed_market_holidays.py` is what turns those gaps into a
proper "missing" verdict instead of a guess.

Resampling the stored minutes reproduces each session exactly (`open` at 09:15,
`close` at 15:28/15:29, true high/low, summed volume) — checked against an
independent pandas aggregation, and pinned in the test file as a literal table
of 13 daily bars. For reference, the first and last:

> Those 13 sessions are what a `1day` backtest over `to_date=2026-09-30` now
> returns. Before the end-date fix it returned 12, and the missing one was the
> 30th — the most recent day, which is the one you are most likely to be
> looking at when you decide the backtest looks wrong.

| Session | Open | High | Low | Close | Volume |
|---|---|---|---|---|---|
| 2026-09-02 | 1298.00 | 1321.80 | 1293.60 | 1313.10 | 26,013,588 |
| 2026-09-30 | 1182.00 | 1196.50 | 1181.70 | 1187.00 | 29,722,953 |

`sma_crossover` at fast=5 / slow=20 over the month, by timeframe:

| Timeframe | Bars | Return | Trades | Max DD |
|---|---|---|---|---|
| 1min | 4817 | −3.37% | 141 | −5.16% |
| 5min | 975 | −3.90% | 29 | −5.36% |
| 10min | 494 | −4.52% | 19 | −5.12% |
| 15min | 325 | −3.57% | 10 | −3.70% |
| 30min | 169 | −1.24% | 3 | −1.52% |
| 1hour | 91 | −0.78% | 1 | −1.56% |
| 4hour | 26 | 0.00% | 0 | 0.00% |
| 1day | 13 | 0.00% | 0 | 0.00% |
| 1week | 5 | 0.00% | 0 | 0.00% |

**The bar counts are the point.** Each one now equals the number of bars the
database actually holds at that granularity (compare with the bucket counts in
step 2). Before the end-date fix in `DbSource.get_candles`, every row here was
short by exactly one session — 4447 / 900 / 456 / 300 / 156 / 84 / 24 / 12 — the
whole of 30 September, silently dropped because `ts BETWEEN :start AND :end`
compared timestamps against midnight on the end date. A single-day run was worse
than lossy: `from_date == to_date` returned *no rows* and failed with "Symbol
not found in database".

The three zero-trade rows are warmup, not a defect: `slow=20` needs 20 bars to
form a first signal and those timeframes only have 26 / 13 / 5. The app logs a
warning for this. With a shorter pair the same `1day` request trades normally —
`fast=2, slow=5` gives 1 trade at −3.34%.

`buy_and_hold` over the same `1day` window returns −9.46% against a benchmark of
−9.60%, entering on 2026-09-03 at 1302.50 and still holding at the final close
of 1187.00. It enters on the *second* bar, not the first: signals are evaluated
on a closed bar and executed on the next one, so the strategy does not get to
buy at the first bar's open.

---

## 1-minute-only ingest: what it inherits, and what it costs

**Direction (2026-10-06):** download 1-minute bars only and derive every coarser
timeframe by resampling. This is already how the read path works, so the change
is on the write side — but three things come with it.

### It inherits the storage convention, exactly as measured above

The tables earlier in this file are the acceptance criteria, not background: with
UTC-stored minutes a `4hour` strategy is fed **39 bars where 26 exist** and every
`1hour` session opens with a 15-minute stub. `1day` and `1week` are unaffected
either way, so **a daily check does not test this** — check intraday bucket
starts (`scripts/diagnose_timeframe_alignment.py`), and assert
`4hour-from-1min` buckets starting at 09:15 plus a `1day-from-1min` bar equal to
the trading date.

Decided (2026-10-06), before bulk fetching: convert on write. The fetch path
and `ingest_csv_to_mdc.py` both bind the exchange session clock
(`data.base.bar_timestamp()`), and the one-off migration
(`scripts/migrate_cache_ts_to_session_clock.py`) repaired the rows the broken
writers had already stored — so nothing was re-fetched. The acceptance tables
above are what `scripts/diagnose_timeframe_alignment.py` re-checks on today's
cache.

### The fetch form's timeframe selector still says five things

`data_manager.html` offers `1min / 5min / 15min / 1hour / 1day` (and is
missing 10min, 30min, 4hour, 1week). Ingest stores 1min regardless, so picking
"1day" stores 1min and derives 1day — correct, but invisible, and the four
missing options are unreachable from the UI. The selector was left as-is: it
describes what you *intend to run* rather than what is written. Relabelling it
(or dropping the selector entirely) remains open if that confuses operators.

### Volume makes retention and read cost real

One year of the 200-symbol universe at 1min is **~19 million rows** against
~50,000 storing daily. The list and Data-tab endpoints are single grouped scans
over that table — they are fast because they are one scan each, not because the
table is small. At 19M rows expect those scans to grow; if a page load turns
back into seconds, the answer is an index or a rollup table, **not** putting
coverage back on the picker (see the hand-off doc §3). Decide retention (how far
back, and what purges or archives) before the first bulk pull.

### Do not persist the derived timeframes

Derivation is read-time on purpose (`data/base.py:160`): it repairs instruments
downloaded before the rule existed, with no re-fetch and no backfill migration,
and it keeps the stored set from silently disagreeing with the resampler. A
stored "1day" column would need a backfill every time a granularity is added.
