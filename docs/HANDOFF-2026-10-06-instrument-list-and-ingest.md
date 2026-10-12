# Hand-off: instrument lists, run-time availability, and the 1-min ingest work

**Date:** 2026-10-06 · **Commits:** `5215a79`, `341b808`, `dd54ac9`, `0c363b4`,
`18da195`, `c3f897f`, `8c4fa29` (branch `arena/01a10cbb-back-test`)

Read this if you are touching instrument lists, `market_data_cache` reads, the
Data tab, or the fetch/write path. Section 4 is the part that needs a decision
from whoever owns ingest — **it is not optional, and it fails silently.**

---

## 1. Why this changed

The user's report: with 200+ instruments the symbol dropdown "feels like
something is wrong with the system". It was not a feeling.

Measured on a fixture matching production shape — 216 symbols with bars
(1,041,768 rows) against a 140,000-row scriptmaster `instruments` table:

| What | Before | After |
|---|---|---|
| Picker list (`/api/data/coverage`, cold) | **3,385–4,525 ms** | **96 ms** (`names_only=1`) |
| Data tab page load (list + table) | **1,054 ms** | **271 ms** (one scan) |
| `load_catalogue` alone | 822–1,396 ms | not called by either caller |
| `build_coverage` merge | 1,844 ms | not called by the picker |

**99.85% of the work was on rows both callers discard.** The picker asked
`available=1` (show only symbols with bars) and the Data tab asked `curated=1`
(show only the NIFTY 200). Both forced the server to measure every symbol —
bar counts, from/to dates, serviceable timeframes — for the whole catalogue
before it could render one row. `_derivative_underlying` alone ran **280,648
times** (561,728 regex matches, 0.92s) folding NFO contract ids back to their
underlyings, to build rows that were then filtered away.

---

## 2. What changed

### 2.1 The list carries names; the run answers availability

`GET /api/data/coverage` now has three shapes, chosen by request:

| Shape | Request | Sources | Cost |
|---|---|---|---|
| **names** | `names_only=1` | shipped universe ∪ `SELECT DISTINCT symbol FROM market_data_cache` | ~10 ms local |
| **universe** | `include_catalogue=0` | + the bar aggregate (real coverage per row) | ~280 ms |
| **full** | *no flag* | + the `instruments` catalogue | 3.4–5.6 s |

`include_catalogue` **defaults to on**, so nothing that existed before changed
behaviour. The full report is still there for external callers; no UI calls it.

Names-only rows carry `data_available: null` + `coverage_known: false` and **no
`hint`** — *not asked* is not *no data*. The picker tests `coverage_known ===
false` **first**: `!row.data_available` is true for null, so getting the order
wrong disables every option in the dropdown. `available_total` and
`db_available` are `null` in that mode for the same reason (`db_available:
false` renders as "no data source connected" — a claim that path never checked).

`/api/data/inventory` is unchanged in shape; it now reshapes the cached report
instead of running its own identical `GROUP BY`, and asks for the **same cache
key** the Data tab's list asks for. Asking for the curated key instead still
scans twice — I shipped that bug for one commit and a test caught it.

### 2.2 Failure now names the available dates

`POST /api/backtest/run` for a window with no bars:

```
HTTP 400
Symbol 'RELIANCE' has no 1day data between 2026-10-01 and 2026-10-31.
Available — 1min 02 Sep 2026 to 30 Sep 2026 (4,817 bars).
```

- Reports the **requested** timeframe, not the internal one it resolved to.
- The old message printed the query's own bounds back at the user, which they
  had just typed, and never mentioned what was stored.
- `DbSource._describe_stored` sits on the failure path and **never raises**; any
  reporting failure degrades to a generic sentence. Pinned by a test.
- The UI shows it in a toast **and** a persistent banner (`#runError`), cleared
  at the start of the next run. A 3s toast cannot carry dates you must type.

### 2.3 The one-way rule is enforced at read time

`DbSource.get_candles` now refuses a timeframe it cannot BUILD from the stored
bars. Before, a symbol stored only at `1day` answered a `1min` request by
returning its **daily bars as minutes** — three daily candles presented as three
minutes of trading, no error, no warning, a backtest number that meant nothing.
`list_symbols()` already refused that request, so the same symbol could be
absent from `/api/symbols` and still be "run" here.

`_can_serve()` is a rank comparison (`1min → 1week` passes, `1day → 1min`
does not). Unknown spellings answer "can serve" — the guard stops a
known-impossible request, and rejecting an unrankable one would break a working
run. The servable list in the message is computed with the **same bars-cap** the
picker uses, so it cannot promise a timeframe the dropdown would not offer.

### 2.4 The Data tab loads one payload for both views

`data_manager.js` requests `include_catalogue=0` once; the checkbox list and the
inventory table both render from it, so they cannot disagree about what is on
disk. Row labels gained the window: `1min · 4,817 bars · 02 Sep → 30 Sep`.

- Date formatting is **string slicing, never `new Date()`** —
  `new Date("2026-09-02")` parses as UTC midnight and renders *01 Sep* in any
  negative-offset browser.
- The table is one row per symbol with a Stored column; per-timeframe dates ride
  in the tooltip, from the new `BarCoverage.dates_by_timeframe` (the aggregate
  was already reading `MIN(ts)`/`MAX(ts)` per timeframe and discarding them —
  which is exactly why the table needed its own scan).
- A universe symbol with no bars reads **"not fetched yet"** and stays listed:
  on this page that row is a tickable instruction, not a defect.

### 2.5 Cache correctness

Two bugs fixed while wiring this up, both silent:

1. The coverage cache was **one slot for several shapes**. Once three shapes
   existed, a names-only request could be served a cached full report, and a
   curated-only report could answer an unfiltered request while dropping every
   scriptmaster symbol. The key is now the shape.
2. `symbols._CACHED_SYMBOLS` had **no expiry at all**. The Forward page kept
   showing the symbol set from boot; a symbol fetched minutes ago stayed
   invisible until the process restarted. `invalidate_coverage_cache()` — which
   the fetch job already calls — now clears it too, and never raises.

---

## 3. If you are changing instrument lists

- **Do not put coverage back on the run pages' list.** `names_only=1` is what
  makes a 5-mount page load cost 96ms instead of 4.5s. If you need coverage,
  ask for `include_catalogue=0` and accept ~280ms.
- **`coverage_known === false` means "not asked".** Never render it as "no
  data", and never let a null `data_available` disable a row.
- **Adding a shape** means adding a `_coverage_cache_key` branch; two request
  shapes sharing one key is the bug in 2.5(1).
- **Row shape is stable on purpose.** Every consumer indexes these keys without
  checking; a missing key surfaces as `undefined` in the UI, not as an error.
- **Shared picker mounts:** Backtest (`backtest.js:454`), Compare
  (`compare.js:547`), Forward (`forward.js:480`), Optimize
  (`optimize_setup.js:68`), Portfolio spawn (`portfolio.js:1245`). Compare makes
  no coverage call of its own — it inherits the picker, which is why it needed
  no separate change.

---

## 4. ⚠️ For the 1-min-only ingest work — what you must handle

The plan: download **1-minute candles only** and derive every coarser timeframe
by resampling. The read path already supports it
(`derive_serviceable_timeframes`, one-way rule, 9 canonical timeframes) — four
things need handling; the first was silent and is now **resolved on main**
(2026-10-06), the other three are still open calls.

### 4.1 Storage timezone — DECIDED: session clock (merged to main 2026-10-06)

Derived timeframes **inherit the timestamp convention of whatever is stored**.
Measured on the same 4,817 minutes of RELIANCE (2–30 Sep 2026), stored two ways:

| timeframe | IST-stored | UTC-stored | what changes |
|---|---|---|---|
| 1min / 5min / 10min / 15min / 30min | 4817 / 975 / 494 / 325 / 169 | identical | clock-aligned either way; windows differ by up to 15 min at the open |
| **1hour** | 91 | 91 | first bar is a **15-minute stub** (03:45–03:59) labelled as an hour |
| **4hour** | **26** | **39** | **13 extra "4-hour" bars** built from 15-minute fragments |
| **1day / 1week** | **13 / 5** | **13 / 5** | **unaffected — a session never crosses UTC midnight** |

**A daily check does not test this.** 1day and 1week are tz-invariant, so a
"1day matches, we're fine" conclusion is worthless here. Use:

```bash
PYTHONPATH=src python scripts/diagnose_timeframe_alignment.py --symbol RELIANCE
```

It prints the storage timezone (`ist` / `utc` / `unknown` from the first minute
of each session) and the actual bucket starts per timeframe.

**This question is answered.** Main now writes every bar through
`data.base.bar_timestamp()` (naive = exchange wall clock, bound as aware UTC)
and a one-off migration shifted the whole existing cache (~45.6M rows) onto the
session clock on 2026-10-06 — live one-day fetches verify 09:15→15:29 sessions
with zero pre-open rows. `diagnose_timeframe_alignment.py` against the live DB
must now report `ist`; if it ever prints `utc`, a writer regressed.

**Acceptance tests to add** (they are what stops this regressing quietly):
a 4hour-from-1min run whose buckets start at 09:15, and a 1day-from-1min run
whose daily bar equals the trading date. Both must be asserted on **intraday**
evidence, not on a daily match. (The 1-minute storage itself is verified live;
the derived-intraday assertions above are still open.)

### 4.2 The fetch dropdown still offers 5 timeframes that will all mean "1min"

`data_manager.html:15` hard-codes `1min / 5min / 15min / 1hour / 1day`; it is
missing 10min, 30min, 4hour, 1week entirely. Once ingest is 1min-only, picking
"1day" stores 1min and derives 1day — which is correct but invisible, and the
four missing options are unreachable through the UI. Relabel it (e.g. *"1 Minute
(everything coarser is derived)"*) or make it a documented no-op selector.

### 4.3 Volume and retention become real decisions

One year of the full 200-symbol universe at 1min is **~19 million rows**,
against ~50,000 storing daily — roughly 400x. Consequences worth deciding
before the first bulk pull:

- **Retention:** how far back? A rolling window (e.g. 2 years) needs a
  documented purge/archive step; nothing does this today.
- **The read path is now load-bearing.** The aggregates in §2 are single grouped
  scans over that table. They are fast because they are one scan, not because
  the table is small — a 19M-row cache is where 271ms page loads can turn into
  seconds again. If that happens, the fix is an index/rollup, not another flag.
- **Options data is separate.** `market_data_cache` holds candles; historical
  option chains come from the snapshot capture, not from this path. Storing 1min
  does not by itself make option backtests possible.

### 4.4 Do not "optimise" the write path by pre-computing timeframes

Timeframes are derived at read time on purpose (`data/base.py:160`): it repairs
instruments downloaded before the rule existed, with no re-fetch and no backfill
migration. Persisting a derived set means every new granularity needs a
backfill, and the stored set can silently disagree with the resampler.

---

## 5. What I deliberately did not touch

- **The storage write path** (`_run_fetch_job_inner`, `ingest_csv_to_mdc.py`,
  migrations) — owned by the ingest work in §4.
- **`/api/symbols`' behaviour** beyond invalidating its cache. It still lists
  symbols, not coverage; it is the Forward page's endpoint.
- **The full `include_catalogue=1` report's speed.** Its merge still calls
  `_derivative_underlying` twice per row. Left alone because no UI calls it and
  changing what the default returns is a contract change — say the word if
  external tooling depends on it and wants it made opt-in.
- **`SOURCE_LABELS["db"] = "Real (PostgreSQL)"`** — wrong on SQLite, but a
  separate labelled bug.
- **The fetch form's timeframe dropdown** (§4.2) — the ingest owner's call.

---

## 6. Verifying

```bash
# the whole touched surface
PYTHONPATH=src python -m pytest tests/test_api_data_coverage.py tests/test_data_coverage.py \
    tests/data tests/test_web_components.py -q

# the two JS harnesses (tests/test_web_components.py asserts exact counts)
node tests/js/test_symbol_picker.mjs        # 31
node tests/js/test_data_manager_labels.mjs  # 16
```

Live check of the two shapes:

```bash
curl -s 'localhost:5003/api/data/coverage?names_only=1&limit=3'      # names only, ~10ms
curl -s 'localhost:5003/api/data/coverage?include_catalogue=0&limit=3' # real coverage, ~280ms
curl -s localhost:5003/api/data/inventory                             # shares the cache
```

Full suite at `8c4fa29`: **4,785 passed / 19 failed**. All 19 are pre-existing —
confirmed by running the same selection on a clean worktree at the previous
commit. They live in `tests/strategies/test_registry_template.py` (13, flaky
under load — 23 vs 13 across runs, it measures a per-bar time budget),
`tests/test_market_status.py` (2, clock-dependent: they fail while the market is
open), `tests/test_strategy_conformance.py`, `tests/test_chain_snapshots.py`,
`tests/db/test_migrations_009_013.py`, and `test_lint_baseline.py` (an E501 in
`brokers/session_manager.py:147`, somebody else's file).
