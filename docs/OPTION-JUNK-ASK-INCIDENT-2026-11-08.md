# Incident: a single uncorroborated offer closed a straddle for −₹11,794

**Session:** Monday 2026-11-09 → incident dated **2026-11-08 (Muhurat special session, 14:20–15:40 IST)**
**Book:** `straddle_short_atm·NIFTY·1min`, instance `4e639d15`, paper, execution broker mStock
**Status:** root cause located, one link still unproven (see "What we cannot prove")
**Severity:** High — on live money this is a real market order at a price that never traded
**Audience decision required:** see "Options for the architect"

---

## 1. What happened, in trading terms

The book sold the NIFTY 23-Nov-2026 **25050 call at ₹337.10** and the **25050 put at
₹320.85** at 14:20:11 IST, with NIFTY at 25,062.60. Five seconds later, with NIFTY *higher*
at 25,079.60, it bought the call back at **₹345.75** and the put back at **₹493.65**, booked
a loss of **−₹11,794.25**, and the stop-loss rule retired the structure.

The whole loss is the put leg:

| leg | sold (bid taken) | bought (ask paid) | ₹/share | × 65 (1 lot) |
|---|---|---|---|---|
| 25050 CE | 337.10 | 345.75 | −8.65 | −562.25 |
| 25050 PE | 320.85 | 493.65 | −172.80 | −11,232.00 |
| | | | | **−11,794.25** ✓ matches the booked figure exactly |

Three independent arguments say ₹493.65 was not a real price for that put at that moment.

**(a) Direction, needs no model.** NIFTY rose 17 points in those five seconds. A put cannot
gain ₹172.80 when the index rises.

**(b) Put-call parity, same strike, same expiry.** Call − put must equal roughly how far the
*futures* sit above the strike: spot 25,079.60 − strike 25,050 = +29.60, plus about ₹72 of
interest-and-carry for the 15 days to expiry ⇒ expected ≈ **+₹100**. Observed
₹345.75 − ₹493.65 = **−₹147.90**. The pair is dislocated by ≈ ₹248 a share, i.e. **₹16,120
per lot** — more than the loss we booked. Both legs cannot be true.

**(c) The tape.** The operator checked the broker's own 5-second chart: **₹493.65 never
printed on that contract all day.**

For shape, this is what a healthy mStock book on the same contract family looks like (our own
dual-broker recorder, Friday 2026-11-06 11:24:06 IST, NIFTY 24,594):

```
NIFTY26NOV25000CE (token 64439)   last 168.40   spread 0.75   status ok
  bids  168.05 / 168.00 / 167.95 / 167.90 / 167.80   qty 65 / 975 / 65 / 65 / 65
  asks  168.80 / 168.85 / 168.90 / 168.95 / 169.00   qty 65 / 260 / 130 / 130 / 520
```

Five offers, stepping 5 paise apart, first offer under a rupee above first bid. For ₹493.65 to
be genuine, the packet would have had to show five asks at 493.65, 493.70, 493.75 … while the
same message reported last traded ₹367.00 and the bid side had been taken at ₹320.85 five
seconds earlier — a 54% one-sided gap on the most liquid strike in India, which the exchange
fills in one tick.

---

## 2. Where the number came from

Proven, in order of elimination:

* **It was mStock's websocket depth row.** The runner is single-broker
  (`source`/`execution_broker = mstock`); the session log has zero Upstox traffic between
  14:19 and 14:21, so Upstox was never consulted for this price.
* **It was not the price-only web route.** mStock's HTTP quote endpoint answers last-traded
  only; with no ask the engine raises instead of filling
  ([`_side_price`, quote_providers.py:974](file:///C:/learning/bt-feeddump/src/backtest/options/quote_providers.py#L974)).
  A fill therefore proves a non-zero ask came from the websocket row
  ([`get_option_quote`, mstock.py:1800](file:///C:/learning/bt-feeddump/src/backtest/brokers/mstock.py#L1800)).
* **The row was freshly written, not a stale book retained in our cache.** A frame with no
  usable ladder keeps the previous book but re-labels it `kept:*`, which logs a new
  `tick profile` line ([`_retain_ladder`, mstock_ws_wrapper.py:181](file:///C:/learning/bt-feeddump/src/backtest/data/mstock_ws_wrapper.py#L181)).
  Token 64442 shows `full/ok` at 14:20:05 and no signature change until 15:41:33, so every
  frame in between carried a live two-sided book.
* **₹493.65 was the ask field, and only the ask field.** The engine prices a buy from the ask
  and a sell from the bid, then stamps that side price as the trade price
  ([`execution_quote`, quote_providers.py:941](file:///C:/learning/bt-feeddump/src/backtest/options/quote_providers.py#L941)).
  Last traded in that same row was ₹367.00, so the fill did not come from last traded.
* **The offer it crossed is not in any table or log we own.** The app caches only best bid,
  best ask and best trade per contract and discards levels 2-5; the `orders` row stores a
  single average fill price and no depth columns
  ([db/models.py:397](file:///C:/learning/bt-feeddump/src/backtest/db/models.py#L397)).
  The session log's depth instrumentation prints last traded only. `option_chain_snapshots`
  is dead as a witness: 857,582 rows, newest 2026-09-23, all blank ltp/bid/ask.

### What we cannot prove

Whether mStock **published** a garbage offer side, or published a good book and **we misread
it**. The packet is gone. Everything downstream of "an ask of ₹493.65 was in the row" is
proven; that one hop is not. This is the gap the recorder in §5A exists to close — and until it
runs, we should assume neither and hold both open.

---

## 3. Why our guards did not stop it

The quote seam has exactly three checks before an offer becomes a fill
([`_sanity_problem`, quote_providers.py:835](file:///C:/learning/bt-feeddump/src/backtest/options/quote_providers.py#L835)):

1. **Side price > 0** — 493.65 passes.
2. **Not below intrinsic value** — a 25050 put with NIFTY above 25050 has an intrinsic floor
   of zero, so this check *structurally cannot fire* for an ATM/OTM put.
3. **Within 5× of its own last trade** (`LTP_DISTANCE_MAX_RATIO = 5.0`,
   [quote_providers.py:743](file:///C:/learning/bt-feeddump/src/backtest/options/quote_providers.py#L743))
   — ₹493.65 against ₹367.00 is 1.34×. Passes with room to spare.

`quote_sanity` appears **zero times** in the entire 20 MB session log: the guard ran and was
never even close to objecting. The band was set wide in 2026-11-03's response to ₹0.05 fills,
and it is measured against the one field of the same packet that the 2026-11-06 recorder had
already caught being wrong (a last trade of ₹0.82 against a book at ₹185, 1 in 3,690 packets).

Two structural problems behind that:

* **One number does two jobs.** A short leg is *marked* at the ask
  ([`mark_quote`, quote_providers.py:948](file:///C:/learning/bt-feeddump/src/backtest/options/quote_providers.py#L948))
  and *exited* at the ask. So a single unverified field both created a −₹11,794 unrealised loss
  and priced the fill that booked it. Had the trigger read last traded, the stop would not have
  fired at all.
* **The stop rule compares rupees against points.**
  [exit_policy.py:462](file:///C:/learning/bt-feeddump/src/backtest/options/exit_policy.py#L462)
  evaluates `pnl ≤ -cfg.stop_loss_points`, and this book's rule says `stop_loss_points: 2000`,
  which is being read as **₹2,000 per structure**, not 2,000 points. The log line
  `stop -11,794 ≤ -2,000` is that comparison. (Separate defect, tracked as task #18; it decides
  *when* we exited, not *why the number was wrong*.)

---

## 4. Recurrence — this is a class, not an accident

| date | what the same seam did | evidence |
|---|---|---|
| 2026-11-03 | straddle booked **+₹71,639 profit** from legs bought at ₹0.05 and ₹4 below intrinsic | `docs/OPTION-DEPTH-GARBAGE-ROOT-CAUSE-2026-11-03.md`, containment 2026-11-04 (4 books stopped, straddle paused) |
| 2026-11-05 | 13:12 strangle take-profit mostly fake: 23900 PE bought ₹5.65, same leg sold ₹127.95 | live-session findings 2026-11-05 |
| 2026-11-06 | recorder finds mStock last-trade field wrong by 3 orders of magnitude in 1/3,690 packets | feed-reliability recorder verdict |
| 2026-11-08 | −₹11,794 stop on an offer that never traded | this report |

Roughly one damaging event per live session, in both directions (fake profit and fake loss).
Priced against a ₹25,000 paper ceiling per strategy, one event is half a book. On live money
each of these is a real order.

---

## 5. Options for the architect

Ordered by what we can do safely, not by what is most elegant. A is built; B–E are not.

**A. Evidence first — flag-gated packet recorder (`ENABLE_FEED`).** *No behaviour change.*
Every depth packet from both brokers, all 5 levels per side with quantities, exchange stamp
plus our arrival stamp, appended per packet to a daily file per broker; plus one line per fill
naming which field of which packet became the price. Off by default; writing is off the
trading thread and cannot slow or fail a fill.

Built as agreed — worktree `bt-feeddump`, branch `feature/feed-depth-recorder`,
[feed_depth_recorder.py](file:///C:/learning/bt-feeddump/src/backtest/data/feed_depth_recorder.py),
10 tests in [test_feed_depth_recorder.py](file:///C:/learning/bt-feeddump/tests/test_feed_depth_recorder.py).
Enable with `ENABLE_FEED=1` in the environment before the server starts (optional
`ENABLE_FEED_DIR`, default `logs/feed_depth/`), which writes
`feed_mstock_<session-date>.jsonl` and `feed_upstox_<…>.jsonl`, rolls at the IST session day,
caps at 64 MB/day and counts dropped lines instead of raising. *One caveat:* nothing in the
app currently runs the Upstox WebSocket wrapper, so Upstox lines only appear once that socket
is actually started (probe/recorder) — wiring Upstox depth into the live app is its own
decision. *Buys:* settles broker-vs-our-read, gives the population needed to calibrate every threshold
below, and makes each future dispute a five-minute lookup. *Cost:* disk (tens of MB/session),
one hook per wrapper. **Not the subject of this review's decision — the decisions are B–E.**

What one captured packet looks like (real output, levels abridged; `price_used` is the same
shape plus `side`, `field`, `price_used`):

```json
{"v":1,"kind":"packet","broker":"mstock","source_label":"mstock","token":"64442",
 "symbol":"NIFTY26NOV25050PE",
 "mode":"full","ladder_status":"ok","ltp":367.0,"bid":320.85,"ask":493.65,
 "bids":[[320.85,65],[320.80,65]],"asks":[[493.65,65],[493.70,65]],
 "oi":54465.0,"volume":5981950,"exchange_ts":"2026-11-08T14:20:14+05:30",
 "recv_utc":"2026-11-08T08:50:14.812337+00:00","recv_mono":41223.551,
 "ltp_mono":41200.1,"ladder_mono":41223.551}
```

**B. Corroboration before an offer may be traded.** Two witnesses instead of one field:
(i) **pair parity** — for the same strike/expiry, `| (call − put) − (spot − strike ± carry) |`
inside a tolerance; (ii) **tighten the single-leg band** from 5× to a rupee/percentage figure
and add a *last-trade-age* condition, since a ₹367 last trade that is 40 seconds old
corroborates nothing. Failure raises the existing `UntrustedOptionQuoteError`, which already
means "no fill, keep the last good mark" — so it fails closed with no new exit path.
*Cost:* it will refuse legitimate fast-market prices in the opening minutes and in a genuine
squeeze, so it must run in `shadow` (log-only) for days first using A's files, and the
tolerance must be measured, not guessed. *Risk if wrong:* an exit we cannot complete.

**C. Split "decide" from "transact".** Unrealised P&L and every trigger read last traded (or
mid); only the fill price reads the book. Kills the single-number dependency in §3 outright.
*Cost:* MTM stops being conservative — with a genuinely wide book you look better than you can
realise, and in illiquid strikes that gap is real money. *This is a policy choice about what
your P&L screen means, so it is yours, not the engine's.*

**D. Protective exits as priced orders, not arithmetic.** Today a bad quote *is* the fill. If
the protective exit instead went to the broker as a limit at a bounded price (market-with-
collar), no feed value could ever fill worse than the collar — the protection stops depending
on the data being right. *Cost:* needs the live order lifecycle and a real re-price/reject
path (task #18), and it can leave us in the position when the market runs away. *This is the
only option that also protects live trading; B and C only reduce how often we are wrong.*

**E. Cheap policy guards.** No new entries in the first N seconds of a session (this one was
opened and closed inside 16 seconds of the Muhurat open, when books are thinnest); refuse
structures whose two legs are marked from packets more than N seconds apart; and a maximum
loss-per-structure cap measured in rupees with correct units.

**Also owed regardless of the above:** fix the ₹-vs-points reading in the stop rule (§3), and
decide whether a structure closed within seconds of opening should be allowed to consume a
daily-loss fuse at all — it did.

---

## 6. Standing constraints

* **No fabricated prices.** If no broker publishes a level, the trade does not happen
  (synthetic-data ban). Every option above refuses or re-prices; none invents.
* **No more gates by reflex.** The 2026-11-03 response added gates that were calibrated by
  hand and still let today's number through. Thresholds in B/E should come out of A's data.
* **Live-money standard.** Anything built for paper must be the same code path live will use.

## 7. Evidence index

* Session log: `server_20261108_muhurat.out.log` (worktree `bt-mainmerge`) — key lines:
  `tick profile token=64442 mode=full ladder=ok ltp=367.0 blank_frames=13` 14:20:05;
  `ENTRY_SELECT … tokens=[{'CE': '64441', 'PE': '64442'}]` 14:20:11; `ENTRY_FILLED …
  SELL x1 @337.10 / SELL x1 @320.85` 14:20:11; `[option-paper] Closed structure=struct_2
  pnl=₹-11794.25 fees=₹71.49` 14:20:16; `closed short_straddle … stop -11,794 ≤ -2,000`.
* Postgres `forward_test.orders` — the four fills above, entry 14:20:05, exit 14:20:14.
* Depth shape (untracked runtime evidence, worktree `bt-mainmerge`):
  `tools/out/depth_compare/depth_20261106.csv` (5 levels/side, `depth_levels=10`) and
  `tools/out/depth_compare/mstock_ticks_20261106.jsonl`.
* Prior incidents: `docs/OPTION-DEPTH-GARBAGE-ROOT-CAUSE-2026-11-03.md`,
  containment note 2026-11-04, live-session findings 2026-11-05.
* Tasks: #19 (corroboration at the seam, this report's option B), #18 (broker-faithful
  execution and units, options C/D), #26 (recorder, option A).
