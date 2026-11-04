# Why the 5h30 clock defect is still live — front-by-front audit (2026-11-04)

The question: *"we fixed timestamps everywhere in the 2026-10-27 clock policy — why is a UTC stamp
still popping up and blocking entries?"*

Short answer: the 2026-10-27 work closed the **construction** doors (`utcnow()`, bare `now()`,
`replace(tzinfo=None)`) and `tests/test_timekeeping_policy.py` enforces exactly those four patterns.
Today's defect is a **serialization** door that no rule covers:

```python
datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
```

This line is compliant with every rule in the policy. It is not `utcnow()`, it is not a bare `now()`
(it passes `timezone.utc`), and it never calls `replace(tzinfo=...)`. The offset is destroyed by a
*sibling* call — `strftime` with a format string that has no `%z` — and the AST classifier walks calls
independently, so it cannot correlate the two. The resulting naive string then walks in through
`from_market_clock`, whose documented convention is *"a naive input here means IST"*, and comes out
5h30 in the past.

So: **not fixed everywhere.** It is fixed on the durability fronts and still open on the live-guard
front.

## Today's measured evidence

All 142 `ema_pullback·NIFTY50·15min` refusals in `server_20261104_025809.err.log` (134.1 MB), bucketed
by the reason string:

| reason | lines | window (IST) |
|---|---|---|
| `ENTRY_STALE_BAR_SKIPPED: bar closed before armed_at_utc` | 122 | 09:15:34 → 14:01:27 |
| `09:01:26 IST is before the 09:15:00 open — no entry outside market hours` | 20 | 14:15:27 → 14:31:26 |
| *(entries)* | 5 fills | **14:45:30** |

The second row is the smoking gun: at real time **14:31:26** the runner believed it was **09:01:26** —
exactly `−5h30m`, the IST offset. The first fill is 14:45:30, the first tick after the shifted clock
crosses 09:15:00. The book's entire tradable window today was 14:45–15:00.

One cause, three faces:

1. **09:15 → 14:01** — the shifted instant is still earlier than `armed_at_utc`, so the stale-bar guard
   fires.
2. **14:15 → 14:31** — the shifted instant has cleared `armed_at_utc`, but its *wall time* is still
   before 09:15, so the market-hours guard fires instead.
3. **≥14:45** — both clear, and the runner trades.

## The path, end to end

```
feed_registry.py:750 / :785   on_tick_end(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
        ↓  naive string carrying UTC components
portfolio_manager.py:182/206/212/217  →  _on_tick_end  (portfolio_manager.py:3988)
        ↓
paper_runner.py:3317 on_tick_end(tick_ts)  →  _process_pool(tick_ts)
        ↓
paper_runner.py:2268 _is_entry_bar_stale / :2282 _is_entry_tick_stale / :4210 _entry_window_block
        ↓  instant = from_market_clock(ts)          # timekeeping.py:97 — "naive means IST"
timekeeping.py:338 _as_exchange(instant) → renders 09:01:26 as IST
```

`_entry_window_block` (paper_runner.py:4210) is the clearest single line:

```python
instant = from_market_clock(ts)      # naive UTC components read as IST → wrong instant
return entry_window_refusal(instant) # _as_exchange() renders it back as "09:01:26 IST"
```

**Why the option books were unaffected all day:** they enter through
`_act_on_signal(symbol, signal, price, bar["ts"])` with a real IST bar stamp from the SDK, and
`options_bridge.py:3265` judges `entry_window_refusal` on `_ist_clock_from_ts(ts)`. The straddle and
strangle traded normally from 09:15:05. Only the `SYMBOL_UNIVERSE` (pool) path forwards the
`on_tick_end` stamp — which is why this surfaced on exactly one runner.

## Front-by-front audit

| # | front | status | evidence |
|---|---|---|---|
| 1 | DB writes (`trades`, `orders`, `positions`) | **covered** | `paper_runner.py:802 stamp = _ledger_iso_utc(ts)`; `trade_persistence.py:643 _parse_ts` reads a naive value through `from_stored_utc`, never refuses-and-loses. Verified today: all 31 orders and 7 trades render correct IST under `at time zone 'Asia/Kolkata'`. |
| 2 | State file / JSON / signal log | **covered** | `paper_runner.py:4775 "ts": datetime.now(timezone.utc).isoformat()` — offset preserved. |
| 3 | API payloads to the browser | **covered for order stamps** | same writer as front 1. The 2026-11-02 `filled_ts` payload-mixing note still deserves a re-check; no at-rest damage was found then. |
| 4 | **Live entry-guard seam** | **BROKEN** | `feed_registry.py:750` and `:785`. This is today's bug. |
| 5 | `_normalize_bar` epoch branch | **latent BROKEN** | `feed_registry.py:873-874 pd.to_datetime(raw_ts, unit="ms")` yields a **naive UTC** Timestamp, flattened at `:881`. Any source that hands epoch numbers produces bar stamps 5h30 early. Reachability today: the mStock WS path does not feed it epoch values, so it is dormant — but the branch exists because some source does. Verify per source before claiming impact. |
| 6 | `simulate_crash` | **BROKEN (test path)** | `portfolio_manager.py:4781 (datetime.now(timezone.utc) + 1min).strftime(...)` — same shape, feeds the same guards during a breaker drill. |
| 7 | Feed producers | **correct** | `dhan_live_feed.py:209`, `mstock_live_feed.py:554`, `upstox_live_feed.py:226`, `warmup.py:127/140/143/810` all deliberately emit naive **IST**, which is the convention `from_market_clock` documents at `timekeeping.py:100-101`. Not defects. |
| 8 | `tests/test_timekeeping_policy.py` | **blind** | `_call_sites` classifies only `utcnow`, bare `now`, `replace(tzinfo=None)`, local `IST`. `feed_registry.py` appears in **no** baseline dict because it contains no violating node. |
| 9 | Seam tests | **missing** | `tests/forward/test_phase1_warmup_and_timeframes.py:628/646` call `mgr._on_tick_end("2026-10-23 09:15:00")` — a naive *IST-shaped* string. The producer's actual output format is never exercised, so the tests pass while production is broken. |

Two more things that are genuinely fixed and should not be re-litigated: `iso_utc` / `parse_utc_iso`
refuse naive values at the boundary, and the 2026-10-27 at-rest repair of the 2,159 legacy `trades`
rows plus the writer fixes are all merged. The remaining leak is not in the ledger — it is in the
**live decision path**, which is why it shows up as blocked entries rather than bad rows.

## Fix plan (writer-side, per the "fix at source, not on read" rule)

1. **`feed_registry.py:750/785`** — pass the sealed bar stamp instead of the wall clock:
   `self.on_tick_end(bar["ts"])` (the guard is about *which bar closed*, not *when we noticed*).
   If a wall-clock tick end is genuinely wanted, serialize with `iso_utc(now_utc())` so the value
   states its own clock. Same change at `portfolio_manager.py:4781`.
2. **`feed_registry._normalize_bar:873`** — make the epoch branch exchange time before flattening,
   exactly as `warmup.py:125-127` already does (`tz_convert(EXCHANGE_TZ).tz_localize(None)`).
3. **Regression test at the seam** — assert that the string `on_tick_end` emits, read back through
   `from_market_clock`, equals the emitting instant within 1 s. A second test feeds an epoch-ms bar
   through `_normalize_bar` and asserts the ts is exchange time. These fail on today's build.
4. **Close the policy blind spot** — add an AST rule for `strftime`/`%Y-%m-%d %H:%M:%S` applied to an
   expression that produced an aware UTC value (a `datetime.now(timezone.utc)` chain), so a new site
   is a deliberate edit to the baseline rather than an accident. The current ratchet cannot see it.
5. **Then, separately, the deployment cap.** The 99.42% single-bar dump at 14:45:30 was a
   *consequence* of the clock bug — with the stamp fixed the book spreads entries again — but nothing
   in the pool path caps how much of a book one bar may deploy. That is a risk-policy decision, not a
   clock fix.

## Why this was missed for five days
The 2026-10-27 incident was a *persistence* incident (mixed clocks in `trades`), so the fix and its
ratchet were aimed at persistence. The same class of error in the live guard path produces no bad row
and no traceback — it produces a runner that looks perfectly healthy (`bars` advancing, `error=None`,
`trade_health=OK`) while refusing every entry, and a log message that reads like a legitimate
market-hours rule. The `09:01:26 IST is before the 09:15:00 open` line is the most misleading
diagnostic this system has produced: it is *syntactically correct and factually false*.

## Applied 2026-11-04 (post-close, on `feature/options-trading-dev`)

Steps 1-4 are implemented; step 5 is open as a risk-policy decision.

| # | Change | Proof |
|---|---|---|
| 1 | `feed_registry._poll_once` / `ingest_ws_bar` emit the **sealed bar stamp**, not a wall clock | `test_tick_end_carries_the_sealed_bar_not_the_wall_clock`, `test_ws_bar_tick_end_carries_the_bar_stamp` |
| 1b | `portfolio_manager.simulate_crash` stamps exchange wall time; `options_bridge` MTM label renders `EXCHANGE_TZ` and says "IST" | suite-wide ratchet run |
| 2 | `_normalize_bar` renders an instant in `EXCHANGE_TZ` before dropping the offset (epoch-ms branch was naive UTC) | `test_epoch_millis_ts_becomes_exchange_wall_time` |
| 3 | Seam regression tests above — they fail on the 04-Nov build, which is what the missing tests cost us | 33 passed in `tests/forward/test_mstock_live_bus.py` |
| 4 | New fifth rule in `tests/test_timekeeping_policy.py`: `_flattens_an_instant()` — zero tolerance, with a positive-control test so the detector cannot go vacuous | `test_no_instant_is_flattened_into_an_undeclared_string`, `test_the_flatten_rule_actually_detects_the_2026_11_04_shape` |

The contract in `src/backtest/timekeeping.py` gained a fourth clause: formatting is a clock
decision too. Two ledger-side defects from the same session were fixed alongside it — an open
`positions` row now re-sends when its mark moves (it used to freeze at cost until close), and
`risk_session_state` publishes derived `current_daily_pnl` / `deployed_capital` instead of zeros
and the allocation.

Still open: the pool path has no cap on how much of a book one bar may deploy (the 99.42% dump at
14:45:30 was a consequence of the clock bug, but nothing prevents a repeat).
