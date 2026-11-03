# Option depth ladder garbage — fabricated straddle profit, root cause (2026-11-03)

Investigation opened by: "straddle_nifty_atm_1min — I feel the profit is unreasonable".
Verdict: **the profit is not real.** Four of the five trades closed a leg at a price
NSE could never have printed. The reported +₹71,639 net is a feed defect converted into
booked P&L by four of our own code gaps.

Related incident doc: [MSTOCK-WS-FEED-ROOT-CAUSE-2026-10-23.md](MSTOCK-WS-FEED-ROOT-CAUSE-2026-10-23.md)
(six stacked socket causes + cause 7, all real, all fixed — this is a **different** failure).

---

## 1. The subject

| | |
|---|---|
| Instance | `straddle_nifty_atm_1min` → `1ef1043ba93644afbb26693495f3ee45` |
| Portfolio | `99a425ec-b1fb-4add-a8db-19936c3cee1d` |
| Strategy | `straddle_short_atm` (short ATM straddle, NIFTY, lot 65) |
| As of 2026-11-03 ~11:00 IST | equity ₹171,639.02, realized ₹71,639.02 net (₹72,764.25 gross − ₹1,125.23 fees), `daily_pnl` ₹55,185.70, win rate 0.8, 5 trades, 0 open |
| Feed state reported | `LIVE`, `mark_stale False`, `quote_source "live:mstock+bidask"` |

## 2. The leg ledger — one impossible leg per win (IST)

| Entry | Straddle | Sold CE / PE | Bought CE / PE | Gross |
|---|---|---|---|---|
| Nov 2 12:15:47 | 23350 | 351.35 / 289.10 | **11.25** / 354.50 | +17,855.50 |
| Nov 2 13:19:04 | 23250 | 338.20 / 303.35 | 384.80 / 262.85 | −396.50 (net −629.55, `eod_flat`) |
| Nov 3 09:17:48 | 23550 | 337.10 / 282.70 | 355.30 / **4.00** | +16,932.50 |
| Nov 3 09:25:18 | 23600 | 331.15 / 295.90 | **0.05** / 272.60 | +23,036.00 |
| Nov 3 10:57:23 | 23650 | 341.60 / 293.60 | 343.45 / **55.80** | +15,336.75 |

P&L verified as `(|entry| − |exit|) × 65 × qty`. The bolded legs are the fabricated side.

Two arithmetic impossibilities, not judgement calls:

- **Intrinsic floor.** A ₹60-in-the-money call cannot trade at ₹0.05. If it did, buying it
  and exercising pays ₹60 for zero risk. Everyone would do it; the price cannot exist.
- **Put-call parity.** Same strike, same expiry ⇒ `CE − PE ≈ spot − strike`. At 10:56 the
  feed priced the 23650 **CE below the PE** while spot was ₹60.40 *above* 23650.

Parity residual (r=0.06, T=20/365) measured on these rows:

- honest entries: −34.85, −37.01, −29.63 — tight and one-signed (that's the spread we crossed)
- the exits above: +237.20, −410.41, +203.42 — an order of magnitude off, both directions

The order record closes the loop on how a ₹0.01 entered the book:
`requested_price 0.01 → avg_fill_price 0.05, slippage_pct 4.0` — the raw ask was ₹0.01 and
[`quantize_nse_tick`](../src/backtest/options/paper_trading.py) rounded it *adversely* onto the
₹0.05 grid. The grid check made an impossible number look official.

## 3. Blast radius

NIFTY trended 23,350 → 23,700 across these two sessions — hostile territory for short premium,
which independently corroborates that a straddle "win" is a fill artifact.

| Book | Net |
|---|---|
| `straddle_nifty_atm_1min` | **+72,764 gross** (the only winner) |
| `strangle_nifty_atm300pts_1min` | −68,448 (3 trades, spawned minutes apart on the same socket) |
| `QA14_strangle_NIFTY_1min` | −83,580 |
| `P3TICK_strangle_NIFTY_15min` | −84,452 |
| `immediate_strangle·NIFTY·1hour [6dbd2475]` | −84,607 (one `min_exit_leg = 0E-8`) |

The strangle books got the **same garbage marks** and simply never converted them into profit,
because their ₹100 target is refused by the `TAKE_PROFIT_BELOW_FEE_FLOOR` guard (BUG-02).
The straddle's ₹1,000 target was just low enough for a garbage mark to trip it.

## 4. What the broker actually sent — proven

Today's socket log ([mticker.py](../src/backtest/data/mticker.py)):

```
746262  "Returning 1 ticks"
      0  "Returning 0 ticks"
      0  "Buffer size mismatch"
      6  connection closes
```

Every frame was well-shaped and parsed into exactly one tick. No drops, no desync, no short
slices. So the zeros are **inside mStock's own bytes**, not an artifact of our reading.

The 09:44 cache row is the smoking gun: correct header, `error = None`, **correct
`ltp = 340.65`**, and `bid = 0.0 / ask = 0.0`. A real packet whose price-ladder section was
blank. Today's `QUOTE_ONE_SIDED` lines cluster on the ATM strikes and the ones that just
became ATM — consistent with a broker fan-out layer that publishes the cheap cached fields
before it has assembled the expensive 5-level book for a token it just (re)registered.

Analogy: a delivery slip with the right address and the right weight, and an empty item list.
The slip is not damaged. The warehouse sent it that way.

## 5. Why we turned it into profit — four defects, all ours

1. **Last-write-wins cache with no merge and no purge.**
   [mstock_ws_wrapper.py:63](../src/backtest/data/mstock_ws_wrapper.py#L63) overwrites
   `self._quotes[token]` on every tick. One blank-ladder frame destroys the last honest ladder.
   On unsubscribe, [mstock.py:944-945](../src/backtest/brokers/mstock.py#L944-L945) clears the
   subscription registry but never deletes the cached row.
2. **Freshness is judged globally, not per token.**
   [mstock.py:1082-1108](../src/backtest/brokers/mstock.py#L1082-L1108): if `token_age > 10s`,
   the row is still served when the *socket-wide* `last_any_tick_mono` is fresh and the token is
   in the subscribed set. A token evicted and re-subscribed instantly re-serves its own
   pre-eviction garbage through this door.
3. **The execution/mark gate asks exactly one question: `price <= 0`.**
   [quote_providers.py:796](../src/backtest/options/quote_providers.py#L796) (and `mark_quote`
   at :780). `NoExecutableQuoteError` is correct for a literal zero and blind to everything
   nonzero. The wrapper's own docstring (:730-737) says it exists so we never "price a leg off a
   number nobody was willing to trade at"; the implementation only tests for *no number at all*.
   `source_name` at :819-822 returns a static `"<inner>+bidask"`, so
   `_fills_against_live_depth` ([paper_trading.py:631-641](../src/backtest/options/paper_trading.py#L631-L641))
   reports "live depth" for a garbage row — it never inspects `quote["source"]`.
4. **`take_profit_points` is ₹ of structure P&L, not option points.**
   [exit_policy.py:474](../src/backtest/options/exit_policy.py#L474) compares the raw rupee P&L
   against the configured number (same at :460 for stop-loss); the docstring in
   [straddle_short_atm.py:52-58](../plugins/strategies/straddle_short_atm.py#L52-L58) calls them
   "2000 points / 1000 points", which reads as option points. On a book where each leg is ~₹40k
   of premium, the real target is ₹1,000 **gross** — comfortably reachable by one fabricated leg.

Secondary live defect: `straddle_short_atm.py:83` declares `carry_policy: positional`, but the
running instance reports `intraday` and flattened at 15:10 on Nov 2 (`eod_flat`). Runners freeze
their expression at spawn, so this book needs a **re-spawn**, not a code change.

## 6. Our instrumentation is blind at exactly the point that matters

[mstock_ws_wrapper.py:72](../src/backtest/data/mstock_ws_wrapper.py#L72) computes
`has_depth = bool(normalized.get("depth"))`. In Python `bool({"bid": [], "ask": []})` is `True`,
so the `tick profile … depth=True` log line cannot distinguish "ladder decoded" from "no levels
parsed at all". This is why the defect survived four weeks of green diagnostics.
`tools/mticker_ws_probe.py:237` has the identical blind spot (counts a token only if a level-0
price is nonzero).

## 7. Ruled out, with evidence

- **Parser layout bug.** Our depth decoder is byte-identical to the vendored
  `reference-code/mstock-pytradingapi-typeA-OG/tradingapi_a/mticker.py:718-745`
  (`git log -S "len(packet) == 200"` → vendor-origin, commit `2d47e4a`, not our patch `e06c003`),
  and the same decoder printed a genuine ladder (`bid 191.60×195 / ask 192.05×1365`) during the
  2026-10-23 incident.
- **Token translation / whole-row contamination.** The 09:44 row carried the *correct* LTP; only
  the ladder was empty.
- **Divisor / scale error.** `divisor` is hardcoded `100.0` for all segments
  ([mticker.py:580](../src/backtest/data/mticker.py#L580)); every field is consistently `x/100`.
- **Dropped or unknown packet shapes.** See the 746,262 / 0 / 0 distribution in §4.
- **Synthetic provider.** `MIN_QUOTE_TICK = 0.05`
  ([quote_providers.py:94](../src/backtest/options/quote_providers.py#L94)) belongs to
  `SyntheticQuoteProvider`, which is policy-disabled and excluded by `SYNTHETIC_SOURCE_MARKERS`.
  The ₹0.05 was broker ₹0.01 + adverse grid rounding.

**Still unsettled:** whether a given ₹0.01 / ₹4.00 / ₹55.80 came from an empty ladder straight off
the server, or from a stale row served through the §5.2 liveness bypass. The first is *proven to
occur*; the second is *proven possible*. Settling it needs one raw full-mode frame with per-frame
level-0 prices plus the `quote_age` at serve time. That requires a second WS socket on the live
session, so it is an operator gate, not an agent action. The probe must first be extended to
print `bid[0].price`, `ask[0].price` and `quote_age` per frame — today it only tests nonzero-ness.

## 8. Verification design — "is this bid/ask real?"

Four independent questions, cheapest first. Any single failure ⇒ refuse the price.

**(a) Structural.** Ladder non-empty; skip zero-price levels instead of reading `bids[0]` /
`asks[0]` blind ([mstock_ws_wrapper.py:231-234](../src/backtest/data/mstock_ws_wrapper.py#L231-L234));
bids descending / asks ascending; `bid ≤ ask`. Crossed-quote guards already exist at
[live/data_validator.py:279](../src/backtest/live/data_validator.py#L279) and
[simulator/slippage.py:187](../src/backtest/simulator/slippage.py#L187) — the options path never
calls them.

**(b) Intrinsic floor.** `price ≥ max(0, spot − strike)` for CE, `≥ max(0, strike − spot)` for PE.
Model-free, opinion-free. Helper already exists:
[`_intrinsic_value`](../src/backtest/options/expiry.py#L500). This alone refuses all four fake legs.

**(c) Parity band on the pair being closed.** `|CE − PE − (spot − strike)| ≤ band`, using the
sibling leg already in the book. Calibrated from today's rows: honest marks pass within ~₹37
(spread), garbage is ₹200+ off. A few-rupee band separates them.

**(d) Per-token freshness.** Age from the last frame *for that token*; purge `_quotes[token]` on
unsubscribe so a re-subscribed token cannot re-serve its evicted row.

Grid validity is already enforced; §5.3 explains why that guard made the bad number look better.

**What the gate can currently see:** `_execution_quote` / `_mark_quote`
([paper_trading.py:516, :536](../src/backtest/options/paper_trading.py#L516)) receive only
`(provider, instrument_token, side)` — no strike, spot, or expiry. But this file already resolves
a contract from a token for tick size
([paper_trading.py:690-701](../src/backtest/options/paper_trading.py#L690-L701)), so strike / type /
expiry come from that same lookup and spot from the underlying's own quote. No new plumbing needed.
Precedent that sanity gating belongs at this seam: `NSE_OPTION_TICK_SIZE` and its BUG-06 comment
("733/910 paper fills on 2026-10-28 were exactly that: numbers no counterparty ever quoted").

**Honest limit:** (a)–(d) prove a price is *possible*, not that it is *liquid*. A genuine ladder
one lot deep passes everything. Depth-vs-lot-size is a follow-up.

## 9. Third layer — the NSE option chain as an independent referee (proposed, not built)

`C:\learning\Python-NSE-Option-Chain-Analyzer-master` scrapes `option-chain-v3` every 3–60s and
persists per-strike `ce_bid/ce_ask/ce_ident`, `pe_bid/pe_ask/pe_ident`, `spot`, `strike`,
`snapshot_at` into Postgres
([option_chain_live.py:290-320](../../Python-NSE-Option-Chain-Analyzer-master/option_chain_live.py#L290-L320)).
`ce_ident` is the instrument token, so it joins our contracts directly.

- **Use as a referee, never as a source.** It is top-of-book only, has no ladder, no size, and is
  3–60s old by construction — strictly worse than the broker WS for fill prices, and we cannot
  execute at NSE.
- **Its value is independence.** No check against the broker's own cache can catch "header looks
  fine, ladder blank". A different provider and a different failure mode can.
- **Veto-only.** NSE 403s and renames fields without warning (their code already hedges with four
  key aliases per field). So: fresh row disagrees ⇒ refuse; row stale/absent ⇒ **no opinion**,
  never block a trade on the scrape being down.
- **What it adds over (b)/(c):** it catches a *stale-but-internally-consistent* ladder (₹340 served
  20 minutes after the market moved), which parity happily passes.

## 10. Fix set, ordered by bleeding stopped

1. Intrinsic-value floor on the execution + mark seams (§8b).
2. CE/PE parity band on the pair being closed (§8c).
3. Skip zero-price depth levels; require non-empty and ordered before claiming `has_depth`
   (§6 — also fixes the lying diagnostic).
4. Per-token age in the paper path; purge `_quotes[token]` on unsubscribe (§5.1–5.2).
5. Rename/rescale `take_profit_points` — it is ₹1,000 gross on a ~₹40k-per-leg premium book (§5.4).
6. Re-spawn `straddle_nifty_atm_1min` so it inherits `carry_policy: positional`.
7. Restate/void the four fabricated trades: writer fix plus a one-off at-rest repair with `bak_`
   snapshots — no read-time compensation.

### 10.1 What landed on 2026-11-04 (`fix/ladder-perimeter-gates`)

Implemented, with tests in `tests/test_option_ladder_gates.py`:

- **§10.1 (§5.1, §8a)** `mstock_ws_wrapper.py` — `_sanitize_depth` drops levels
  without a positive price *or* positive quantity, prices each side from the
  first surviving level, and labels the row `ok | one_sided | crossed | blank |
  none`. `_retain_ladder` makes it last-valid-write-wins: a frame with no usable
  ladder keeps the previous book and its `ladder_mono`, takes only the fields it
  genuinely carries (under `ltp_mono`), and is stamped `kept:<reason>`. The
  `tick profile` line now logs `ladder=<status>` instead of `depth=bool(dict)`.
- **§10.4 (§5.1–5.2)** `mstock.py` — `get_ws_quote` runs two clocks: LTP age with
  the socket-liveness bypass (now also requiring the token to be *in* the
  registry — the `not self._ws_subscribed_tokens` fail-open is gone), and a
  separate `WS_LADDER_TTL_SECONDS = 120` ladder trust window past which the row
  is served with `bid`/`ask` zeroed and `ladder_status=expired:*`.
  `unsubscribe_tokens_ws` calls `forget_token`, and the wrapper's `on_connect`
  starts a new cache generation (`_purge_all`), so no row outlives the socket
  that wrote it.
- **§10.1-verification (§8b, §8d)** `quote_providers.py` —
  `BidAskQuoteProvider` gained `_sanity_problem` on both seams
  (`execution_quote` → `UntrustedOptionQuoteError`, `mark_quote` → `error` row):
  an `intrinsic_floor` and an `ltp_distance` check, mode `$OPTION_QUOTE_SANITY`
  = `off|shadow|enforce` (default `enforce`), counted in `sanity_stats`.

Three deliberate deviations from §8, each because the gate would otherwise cause
a different outage than the one being fixed:

1. **Intrinsic fires only when the row's own LTP corroborates it.** A held ITM
   leg whose whole row lags a fast spot (LTP 45 vs intrinsic 500) is stale, not
   fabricated; refusing it locks the *exit* of real risk. Age gates own lag, this
   gate owns contradiction.
2. **The intrinsic gate abstains when the spot is unusable — it never refuses on
   a missing spot.** Failing closed there would turn one spot-cache hiccup into a
   mass exit refusal across every book at once.
3. **Ladder ordering is not a refusal.** §8a asks for "bids descending / asks
   ascending", but no captured real frame proves mStock's array convention — and
   `tests/test_mticker_ws_fixes.py` builds its packet with the same layout it
   asserts (§11), so it cannot settle it. Best-first is assumed for pricing (the
   first surviving level), which is the conservative reading either way.

**Not implemented:** §10.2 / §8c (CE-PE parity band). It is computable from the
book's own legs only for a same-strike straddle; a strangle's `fixed_distance`
wings are different strikes, so the pair the gate needs is not in the trade
record. It also cannot be back-verified here — no ladder has ever been persisted
on this system (§6), so there is no tape to calibrate a band against. The
intrinsic floor and the LTP-distance check both fire on all four fabricated legs
without it.

§10.3 is covered by the `ladder_status` label. §10.5–§10.7 (`take_profit_points`
units, the `carry_policy` re-spawn, the at-rest restatement of the four trades)
are separate work and still open.

### 10.8 Clean-start square-off (2026-11-04, same branch)

Four legs were still open in the stopped books when the operator chose to start
04-Nov with new instances and nothing carried. On this deployment the live
database is at alembic **022** — `option_open_structures` / `option_open_legs`
do not exist, `trade_structures` has 0 rows and `trades` has 0 option rows — so
an open option leg has **no DB row at all**: it lives only in
`src/data/portfolio_state.json`, and `remove_runner`
([portfolio_manager.py:1783](../src/backtest/forward/portfolio_manager.py#L1783))
deletes that entry without closing the leg. Squaring off at rest is therefore
the only way to retire these positions with a price on them instead of letting
them vanish mid-air, and the full books are archived in the same step because
removal would otherwise erase the option books' entire history.

`scripts/squareoff_open_option_legs.py` (dry-run default, `bak_` snapshot,
`--rollback`) settles each leg at its last known mark — the same fail-closed
rule the writer uses when a close cannot get a quote
([paper_trading.py:1101-1114](../src/backtest/options/paper_trading.py#L1101-L1114)) —
after re-running §8b's intrinsic floor over that mark with the book's own
`bridge.scalars.last_spot`, plus a distance check against its own entry using
`LTP_DISTANCE_MAX_RATIO`. A mark that fails a gate is reported and left open;
the script never invents a closing price. All four current marks pass
(NIFTY 23350 CE 579.7 vs intrinsic 460.85; BANKNIFTY 55300 CE 868.0; the two
puts OTM), so the square-off books ₹6,605.50 gross realized and ₹276.19 of exit
statutory charges, using the writer's cash and fee arithmetic.

Instance removal is deliberately *not* in the script: `remove_runner` folds the
book's equity into the capital ledger and releases its feed subscriptions, so it
has to go through `DELETE /api/portfolio/runner/<id>` after the restart.

This is not §10.7 — the four already-closed fabricated trades are still to be
restated.

## 11. Guardrails for whoever edits the feed

Before touching `src/backtest/data/mticker.py`: read
[MSTOCK-WS-FEED-ROOT-CAUSE-2026-10-23.md](MSTOCK-WS-FEED-ROOT-CAUSE-2026-10-23.md) and run
`tests/test_mticker_ws_fixes.py` before and after. Note that
[tests/test_mticker_ws_fixes.py:126-160](../tests/test_mticker_ws_fixes.py#L126-L160) builds its
synthetic packet with the *same assumed* byte layout the parser uses, so
`test_full_mode_depth_ladder_parses` (:178) is self-confirming — it can never catch a wrong layout
assumption. A layout fix needs a captured real frame, not a new synthetic one.
The vendor's own hedge,
[mticker.py:744](../src/backtest/data/mticker.py#L744) `depth_end = min(len(packet), 184)
# Limit to 184 to match expected format`, is the one remaining reason to doubt the layout.
