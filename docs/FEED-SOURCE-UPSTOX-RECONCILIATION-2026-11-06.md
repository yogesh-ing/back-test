# Upstox feed for stocks and options — implementation status and mStock reconciliation

Measured 2026-11-06, 09:40–10:40 IST, against the live Upstox and mStock sessions on
:5000. Every number below is from a read-only call made in that window; reproduce
with the commands in §5.

## 1. Verdict

| Need | Source to use | Why |
|---|---|---|
| Live intraday feed (equities, indices) | **mStock** — unchanged | WebSocket depth is healthy (51 symbols, 0 gaps, 0 stale repeats, 0 errors this morning); Upstox has no streaming candle feed wired here |
| 1-minute history, **recent** (last ~6 weeks) | either — they agree | median close delta Rs 0.000, p99 0.043%, 1 bar in 740 above 0.1% |
| 1-minute / daily history, **older** | **mStock only** | Upstox prices are raw; the mStock-sourced cache is dividend-adjusted. Blending them puts a step change in every series |
| Daily bars, current sessions | Upstox is a valid **cross-check** | open and close identical on all 6 shared sessions; only intra-session high/low differ by ≤ Rs 0.30 |
| Option chain (strikes, expiries, lot size) | **Upstox** | 2,050 NIFTY contracts across 18 expiries from one public master call, structured fields, correct lot 65 |
| Option depth / bid-ask marks | **Upstox** | tight two-sided books, no leg priced below intrinsic, no Rs 0.05 garbage |
| Option candle history (backtests) | not yet decided | Upstox does serve it (16/24 near-money legs had bars), but bars are stamped 15:39, not 15:30, and the basis question above applies |

**Do not switch any timeframe to Upstox as a bulk history source.** The bars it
returns for dates before the most recent ex-date are on a different price basis than
what is already stored, and mixing the two is exactly the class of defect that
poisoned `market_data_cache` on 2026-10-03.

## 2. What Upstox's history API actually accepts

Measured per form, this morning, against RELIANCE (`NSE_EQ|INE002A01018`):

| API | interval | result |
|---|---|---|
| v2 | `1minute`, `30minute`, `day`, `week`, `month` | 200, bars |
| v2 | `3minute`, `5minute`, `15minute`, `60minute` | **HTTP 400 `UDAPI1146`** |
| v3 | every `unit:interval` form (`minute:1` … `month:1`) | **HTTP 400** |

So v2 is the only working surface, and it answers **four** of the app's canonical
timeframes (`1min`, `30min`, `1day`, `1week`) — no 5min, no 15min, no 1hour.

Window width is capped per request, separately from how far back data exists:

- `1minute` 31-day window → 200, 7,875 bars (no 1000-bar cap at this width)
- `1minute` 35/40/45/75/200-day windows → **HTTP 400 `UDAPI1148` "Invalid date range"**
- `1minute` narrow windows still return bars back to 2024-04-01 and 2022-04-01 —
  the reach is years; it is the *width* that must stay under ~31 days
- `day` / `week` / `month` accept 200-day windows

Consequence for the fetcher: Upstox chunking needs its own `chunk_days` ceiling
(~30), which `CHUNK_DAYS_MAP` (2 days for 1min) already satisfies.

## 3. The price-basis defect (this is the finding that decides §1)

Daily closes, Upstox minus the mStock-sourced cache, 2025-01-01 → 2026-10-23:

| symbol | shared days | most common offset | days differing | last differing day |
|---|---|---|---|---|
| RELIANCE | 442 | **Rs +6.00** ×338, Rs 0.00 ×92 | 350 | 2026-06-04 |
| HDFCBANK | 444 | **Rs +13.00** ×265, Rs 0.00 ×85 | 359 | 2026-06-18 |
| TCS | 442 | Rs +12/+24/+55/+66/**+77** stepping | 435 | 2026-10-13 |
| NIFTY (index) | 433 | **Rs 0.00 ×300** | 133 | 2025-07-16 |

The offsets are a **constant rupee amount within each period**, and they break on
dates that look exactly like ex-dates. An index — which pays no dividend — agrees
almost everywhere. That is the signature of backward dividend adjustment applied by
one feed and not the other: mStock's history is adjusted, Upstox's is raw.

The same offset shows up in 1-minute history, and vanishes as dates get recent:

| window (1min, RELIANCE) | median Upstox/cache close ratio |
|---|---|
| 2024-04-01 | +0.78% (a flat Rs ~11.5) |
| 2025-04-01 | +0.92% |
| 2025-11-06 | +0.40% |
| 2026-04-20 | +0.44% |
| 2026-09-22 | **+0.000%** |
| 2026-10-27/28 | median delta Rs 0.000 |

Two conclusions:

1. Recent Upstox bars are interchangeable with the cache — they are the same prints.
2. Anything older than the latest ex-date is **not** interchangeable, and no
   per-symbol constant fixes it for long, because the constant changes at every
   corporate action (TCS stepped five times in 22 months).

## 4. Clock and coverage differences

- Timestamps align exactly: all 740 cache bars for RELIANCE 2026-10-27/28 matched an
  Upstox bar on the same minute. Both are naive IST wall clock.
- Upstox additionally carries the closing-auction minutes (15:16–15:19, 15:29) that
  the mStock cache does not — 10 extra bars in that window. The single worst 1-minute
  open/high delta (Rs 5.90) is at 15:28, where the auction print lands in a different
  bucket in each feed; that is a boundary, not a disagreement.
- Option bars are stamped **15:39**, not 15:30 (every sampled series, uniformly),
  while equity bars end at 15:29. Any option backtest on Upstox bars must anchor on
  that clock or the session end will look 9 minutes late.
- The cached daily series for RELIANCE stops at 2026-10-23 while 1-minute runs to
  2026-10-28. Upstox has the missing sessions — but see §3 before filling them.

## 5. Upstox as the options surface

`tools/feed_reconcile.py chain --underlying NIFTY`, this morning:

- 2,050 contracts across 18 expiries; front expiry 2026-11-09 with 270 legs over 135
  strikes (20,050–26,750); lot size 65 read from the master, not guessed.
- Quotes are two-sided and sane: `NIFTY 24500 CE 09 NOV 26` ltp 220.10, bid 220.05,
  ask 220.60 against intrinsic 112.75. No leg quoted below intrinsic, no Rs 0.05
  ladder — the failure mode that fabricated the straddle's +Rs 71,639 on 2026-11-03.
- Put-call parity `C − P` vs `S − K` misses by a **constant Rs 22.75–24.30** across
  five strikes at spot 24,612.8. A constant across strikes means the strike prices
  and premiums are self-consistent and the *index quote* is the odd one out: treat
  Upstox's `NSE_INDEX` LTP as ~0.1% biased and derive spot from CE/PE parity, which
  the chain command already does.
- Option candle history exists: 16 of 24 sampled near-money legs returned 1-minute
  bars for 2026-10-27/28; the rest returned 200 with 0 candles (untraded strikes).

Not yet done: the same strikes marked by mStock's WebSocket depth at the same second.
That needs a fresh mStock WS connection, and doing it mid-session risks the live
session the :5000 feed depends on — run it after 15:30.

## 6. What changed in the code

- `src/backtest/data/base.py` — `UPSTOX_HISTORICAL_INTERVAL_MAP`, the four canonical
  timeframes Upstox actually serves, sitting beside `MSTOCK_INTERVAL_MAP`.
- `src/backtest/api/data_manager.py` — the fetcher raises for an unmapped timeframe
  before it touches a session or the network; `POST /api/data/fetch` now validates the
  timeframe against the *requested broker's* map, so an Upstox 5min job is refused with
  a 400 instead of starting and losing every window to a guaranteed 400.
- `src/backtest/brokers/upstox.py` — expiry parsed from the master's epoch-millisecond
  field (the chain was silently empty before); chain filtered by `underlying_symbol`
  and `instrument_type` instead of a symbol regex; plain symbols resolve NSE-first
  rather than letting a BSE row answer `RELIANCE`.
- `tools/feed_reconcile.py` — worst bar per OHLC field is named, and the verdict is
  graded on the gap as a fraction of price, never on tick counts (Rs 0.25 is tick noise
  on a Rs 1,200 stock and a 5% misprice on a Rs 5 premium).
- `tests/test_api_data_fetch_scope.py`, `tests/test_broker_upstox_order_mock.py` —
  coverage for all of the above, fixtures shaped like the real master.

## 7. Still owed

1. Post-close mStock option-mark cross-check (§5).
2. A decision on the daily gap 2026-10-24 → now: fill it, or leave the cache single-
   sourced. Filling it from Upstox writes raw prices into an adjusted series.
3. Symbol-shape classifiers must move to structured `option_type` before any Upstox
   option symbol reaches the trading path. Upstox writes contracts as
   `NIFTY 24600 CE 09 NOV 26` (year last), mStock as `NIFTY26NOV24600CE`, and every
   detector below is anchored to the end of the string:
   - `options/execution_gate.py:95` → `None`, and `:150` only raises when both sides
     are known, so the CE/PE cross-check **fails open** on Upstox symbols.
   - `reporting/records.py:103` and `analytics/portfolio.py:498` → classify the same
     symbol as `equity`, which mislabels fees basis and instrument class downstream.
   - `diary/ledger.py:45` → the `\d{2}[A-Z0-9]{3}\d+(CE|PE)$` arm never matches; only
     the keyword arm (`straddle`, `call`, `put`) still fires, so diary `kind` survives
     today by luck of the strategy name rather than by the contract.
   - `data/coverage.py:107`, `api/symbols.py:76`, `diary/roster_selector.py:65` →
     same mStock-anchored patterns; they resolve Upstox symbols as non-options.
4. Whether stock options (`RELIANCE` etc. in the master) join
   `SUPPORTED_UNDERLYINGS`, which is still `{"NIFTY", "BANKNIFTY"}`.
5. Landing this change set, then restarting :5000 onto it.
