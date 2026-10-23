# Paper Trade Reconciliation — 2026-10-23 (Friday)

Reconciliation of the paper book for the 2026-10-23 NSE session (09:15–15:30 IST),
the first full session after the mStock WebSocket root-cause fixes
([MSTOCK-WS-FEED-ROOT-CAUSE-2026-10-23.md](MSTOCK-WS-FEED-ROOT-CAUSE-2026-10-23.md)).
Server build: `feature/options-trading-dev` with all six one-sided-quote fixes,
restarted 11:06:58 IST (`server_20261023g.log`).

**Verdict: the ledger reconciles to the paisa.** Daily P&L, per-runner P&L, the
trade ledger, and the open-position marks all agree exactly. Every option intent
filled on real two-sided depth — zero one-sided blocks, zero phantom prices. The
day's loss is strategy behavior (whipsaw + duplicate exposure), not plumbing.

All times below are IST. API `entry_ts` values are UTC (+05:30 for IST).

---

## 1. Sources

| Source | Endpoint / path |
|---|---|
| Closed trades | `GET /api/portfolio/trades/aggregated?mode=paper&kind=option\|equity` |
| Open positions + final marks | `GET /api/portfolio/positions?mode=paper` |
| Portfolio summary | `GET /api/portfolio/summary?mode=paper` |
| Runner detail | `GET /api/portfolio/runner/<instance_id>` |
| Audit (spawn/pause/resume) | `GET /api/portfolio/audit?mode=paper` |
| Execution markers | `server_20261023g.log` — `INTENT_RECEIVED` / `ENTRY_SELECT` / `ENTRY_FILLED` / `OPTION_EXIT` |

## 2. Executive summary

| Measure | Value |
|---|---|
| Options realized (25 closed trades) | **−₹13,558.40** |
| Equity realized today (16 trades) | **−₹910.36** |
| Realized today, total | **−₹14,468.76** |
| Unrealized (4 open positions, marked 15:30:17) | **−₹5,923.80** |
| **Daily P&L** | **−₹20,392.56** |
| Paper equity (7 runners × ₹10,00,000 capital) | ₹69,74,946.98 |
| Daily-loss fuse utilization | ₹20,392.56 / ₹2,50,000 = **8.16%** (no breaker, no warnings — correct) |
| Option intents → fills | **29 / 29 (100%)** |
| One-sided / blocked quotes all day | `QUOTE_ONE_SIDED` 0 · `NO_EXECUTION_QUOTE` 0 · `OPTION_BLOCKED` 0 |

Cross-checks (exact):

- `Σ runner daily_pnl` = 937.95 − 2,810.60 − 306.15 − 4,012.80 − 6,645.30 − 6,645.30 − 910.36 = **−20,392.56** = summary `daily_pnl`.
- Summary `realized_pnl` (−15,673.77) is **manager-lifetime**, not today: options −13,558.40 (all option runners spawned today) + equity runner lifetime −2,115.37 (56 trades since 2026-10-19). Today's equity portion is −910.36; prior days −1,205.01.
- The daily-loss fuse's calendar-day semantics (fix merged 2026-10-22, 8074fca) held live: prior-day equity realized is excluded from `daily_pnl`, which equals today's realized + today's unrealized exactly.
- Portfolio equity = Σ runner equities exactly (the 7th runner, `ema_pullback`, holds ₹9,97,884.63 = 10,00,000 − 2,115.37 lifetime).

## 3. Session timeline (IST)

| Time | Event |
|---|---|
| 11:06:58 | Server restart on the fixed build; 4 runners restored PAUSED from `portfolio_state.json` |
| 11:07:37 | CE/PE instant-buy runners resumed |
| 11:12:21 | `ema_pullback` + `immediate_strangle` resumed |
| 11:13:19–21 | First three option entries filled on live WS depth — **first clean two-sided fills after the fixes** |
| 11:19–11:28 | `ema_pullback` exits its 16 open equity positions (strategy exits) |
| 11:28:10 | `ema_pullback` paused (audit `action=pause`); equity trading stops for the day |
| 11:28:34 | `CE_instant_buy·BANKNIFTY·1hour` spawned (BANKNIFTY chain first fetched here) |
| 11:51:37 / 12:17:47 | `banknifty_ema_momentum·BANKNIFTY·15min` / `·1min` spawned |
| 12:52–15:30 | EMA runners' three round trips (put → call → put, all signal-flip exits) |
| 13:33–13:39 | Largest single loss: strangle stopped −₹546 (spot +73 pts in ~6 min) |
| 15:30:00–17 | Close. Final marks 15:30:17 (last WS ticks), `mtm_failures=0`, `mark_stale=0`. EMA runners exit on the 15:30 flip bar; strangle #20 and both instant-buy positions carry open. |
| 17:42 | mStock session token expired (`broker_connected=false` afterwards) |

## 4. Options closed-trade ledger

### 4.1 `immediate_strangle·NIFTY·1hour` — 20 entries, 19 closed, 1 open

Short strangle, 1 lot (65) each leg, weekly expiry 2026-10-27. Stop/target ₹100
per structure (see F2). PnL is net premium change × 65; exit_ts is the signal bar.

| # | Entry (IST) | Short CE @ | Short PE @ | Exit (IST) | Reason | P&L (₹) |
|---|---|---|---|---|---|---|
| 1 | 11:13:21 | 22950 @31.77 | 22000 @22.73 | 11:19 | stop_loss | −130.65 |
| 2 | 11:20:27 | 23000 @28.12 | 22050 @24.13 | 11:42 | take_profit | +97.50 |
| 3 | 11:43:41 | 22950 @27.62 | 22000 @22.98 | 11:54 | take_profit | +113.75 |
| 4 | 11:55:49 | 22900 @30.72 | 21950 @18.98 | 12:23 | take_profit | +120.25 |
| 5 | 12:24:48 | 22900 @28.37 | 21950 @18.48 | 12:34 | take_profit | +97.50 |
| 6 | 12:35:45 | 22900 @27.17 | 21950 @18.13 | 12:46 | stop_loss | −104.00 |
| 7 | 12:48:21 | 22900 @27.72 | 21950 @18.43 | 13:07 | take_profit | +107.25 |
| 8 | 13:08:16 | 22850 @31.37 | 21950 @18.08 | 13:32 | stop_loss | −120.90 |
| 9 | 13:33:38 | 22900 @29.02 | 21950 @15.88 | 13:39 | stop_loss | **−546.00** |
| 10 | 13:40:53 | 23000 @29.67 | 22050 @16.73 | 13:41 | stop_loss | −126.75 |
| 11 | 13:42:46 | 23050 @27.52 | 22100 @19.48 | 13:51 | take_profit | +117.65 |
| 12 | 13:52:44 | 23000 @28.17 | 22050 @18.58 | 13:54 | take_profit | +117.00 |
| 13 | 13:55:40 | 23000 @27.92 | 22050 @18.03 | 14:02 | stop_loss | −126.75 |
| 14 | 14:03:52 | 23050 @25.47 | 22100 @20.03 | 14:06 | take_profit | +104.65 |
| 15 | 14:07:40 | 23050 @23.73 | 22050 @15.98 | 14:09 | take_profit | +111.80 |
| 16 | 14:10:57 | 23050 @21.98 | 22100 @20.18 | 14:22 | take_profit | +134.55 |
| 17 | 14:23:41 | 23000 @23.78 | 22050 @18.08 | 14:34 | stop_loss | −191.10 |
| 18 | 14:36:00 | 23000 @25.12 | 22050 @16.18 | 14:41 | stop_loss | −142.35 |
| 19 | 14:42:44 | 22950 @22.43 | 22000 @19.98 | 14:54 | take_profit | +98.80 |
| 20 | 14:55:48 | 22950 @24.43 | 22000 @16.08 | — | **open at close** | −38.35 (unrealized) |

**Closed subtotal: 19 trades — 11 TP / 8 SL, −₹267.80.** Median hold ~9 minutes.

### 4.2 `banknifty_ema_momentum·BANKNIFTY` — both runners, 6 closed trades

1 lot (30) per structure, weekly expiry 2026-10-27. Every exit was a
signal-flip (the −50% / +100% premium rules never triggered). **The two runners
took byte-identical trades** — see F1.

| Runner | Entry (IST) | Structure | Entry @ | Exit (IST) | Exit @ | P&L (₹) |
|---|---|---|---|---|---|---|
| 15min | 12:52:11 | 54000 PE long | 288.14 | 13:39 | 203.40 | −2,542.20 |
| 1min | 12:52:12 | 54000 PE long | 288.14 | 13:39 | 203.40 | −2,542.20 |
| 15min | 13:40:58 | 54200 CE long | 487.74 | 14:40 | 384.32 | −3,102.60 |
| 1min | 13:40:58 | 54200 CE long | 487.74 | 14:40 | 384.32 | −3,102.60 |
| 15min | 14:41:47 | 54000 PE long | 290.34 | 15:30 | 256.99 | −1,000.50 |
| 1min | 14:41:47 | 54000 PE long | 290.34 | 15:30 | 256.99 | −1,000.50 |

**Subtotal: −₹6,645.30 per runner, −₹13,290.60 combined — 98.0% of today's
options realized loss.** BANKNIFTY spot whipsawed (54047 → 54249 → 54058 →
54287); every flip crossed the spread into a fresh ATM premium and decayed.

Note: the 13:40 and 14:41 fills log the same `structure_id` for both runners
(e.g. `struct_20261023081058_0002` twice). This is **not** a shared position —
the id is `<timestamp><per-runner-sequence>`, both runners filled within the
same second on their own sequences, and both booked independent trades with
millisecond-distinct `entry_ts`. Confirmed by the closed-trade ledger (two
trades, two runners) and by the two separate open positions after 12:52.

### 4.3 Instant-buy runners — 0 closed

`CE_instant_buy·NIFTY·1min`, `PE_instant_buy·NIFTY·1min` (entered 11:13) and
`CE_instant_buy·BANKNIFTY·1hour` (entered 11:29) filled once and held all day —
these test strategies declare no exit expression, so nothing closed. See F5.

## 5. Open positions at close (marked 15:30:17 IST)

| Runner | Position | Qty | Entry @ | Mark | Unrealized (₹) |
|---|---|---|---|---|---|
| CE_instant_buy·NIFTY·1min | NIFTY26OCT22500CE LONG | 65 | 170.57 | 185.00 | +937.95 |
| PE_instant_buy·NIFTY·1min | NIFTY26OCT22500PE LONG | 65 | 144.29 | 101.05 | −2,810.60 |
| CE_instant_buy·BANKNIFTY·1hour | BANKNIFTY26OCT54300CE LONG | 30 | 462.61 | 328.85 | −4,012.80 |
| immediate_strangle·NIFTY·1hour (#20) | 22950CE SHORT / 22000PE SHORT | 65+65 | 24.43 / 16.08 | 28.80 / 12.30 | −38.35 |
| **Total** | | | | | **−5,923.80** |

All four carry into the next session (expiry 2026-10-27) with marks frozen at
the 15:30 close. Strike selection was ATM-correct at entry (e.g. spot 22477.45 →
22500; spot 54279.65 → 54300).

## 6. Execution integrity (the fix validation)

- **29/29 intents filled**, from the first bar after resume (11:13:19, six
  seconds after intent) to the last (14:55:48). No rejects, no partials.
- **Zero one-sided quotes all session**: `QUOTE_ONE_SIDED=0`,
  `NO_EXECUTION_QUOTE=0`, `OPTION_BLOCKED=0` in the whole log — the failure
  signature that blocked every option entry before 2026-10-23.
- Fills price against the executable side (BUY←ask, SELL←bid) from WS depth by
  design (`BidAskQuoteProvider.execution_quote`, numeric-token-first lookup);
  no fill used LTP as a price, and no price was fabricated.
- Intent→fill latency: ~6 s on first-ever chain fetch per underlying (11:13
  NIFTY, 11:29 BANKNIFTY — chain HTTP call dominates), ≤1 s once chains are
  cached (12:52 / 13:40 / 14:41 EMA fills), ~4 s typical for strangle re-entries.
- Final marks: 15:30:17 IST, `mtm_failures=0`, `mark_stale=0` on every leg.

Not re-done today: independent fills-vs-historical-bars comparison (methodology
and last run: `tools/out/`, 2026-10-15 validation). Today's evidence is the
depth-side fill mechanism plus the zero-failure markers.

## 7. Findings & flags

- **F1 — Duplicate exposure, doubled loss (high).** The `15min` and `1min` EMA
  runners are functionally identical: the 15min runner processed 213 one-minute
  bars (11:51→15:30) and signaled off 1-min boundaries (12:52, 13:40, 14:41,
  15:30 — none are 15-min boundaries). The timeframe label did not change bar
  cadence, signal timing, or trades. Net effect: the same strategy ran twice on
  the same feed and lost twice (−₹6,645.30 × 2). Either wire the timeframe into
  option-runner bar aggregation or don't spawn the same strategy+underlying
  twice.
- **F2 — Strangle churn by design, but very tight (medium).** `stop_loss_points`
  / `take_profit_points` are **absolute ₹ of structure P&L**, despite the name
  (`exit_policy.py:40`; the strategy docstring also says ₹100). On a 65-lot,
  ₹100 ≈ 1.5 premium points ≈ 2.7% of the ~₹40–55 credit — hence 20 round trips
  in 4h45m (median hold ~9 min) for a net −₹267.80. Behavior matches the
  declared plan; the plan itself is noise-sized. Rename the params or resize them.
- **F3 — Bar-close stop overshoot (medium).** Strangle #9: stop declared at
  −₹100, realized −₹546.00. NIFTY moved 22440.8 → 22513.8 (+73 pts) in ~6 min;
  the stop is evaluated on bar close, so a fast move blows through it 5.5×.
  Same mechanism, smaller magnitude, on #17 (−191) and #18 (−142).
- **F4 — Commissions recorded but not deducted (medium).** Option trades carry
  `commission` (₹20/leg; ₹880 across today's 25 closed) but `pnl` is pure
  premium×lot (verified: e.g. −142.35 = 2.01 pts × 65 exactly). With
  commissions, options realized would be −₹14,438.40. Decide: deduct in `pnl`
  or rename the field; the current state understates cost on a 20-trade day.
- **F5 — No EOD square-off (policy).** Four positions carried past 15:30 with
  frozen marks; the instant-buy test runners have no exit expression at all and
  will hold to expiry (2026-10-27) unless closed manually. For a stakeholder
  demo, decide whether paper options auto-square-off at close or explicitly
  carry (current UI shows a stale mark after close either way).
- **F6 — Equity closed-trade payload still degraded (low).** Option trades now
  return `qty`, `commission`, `exit_reason`; equity trades still return
  `qty=0.0`, `commission=None` (16 trades today affected). Prior memory of this
  gap is now options-only.
- **F7 — Mixed timestamp conventions in one payload (low).** `entry_ts` is UTC
  wall-clock with microseconds; `exit_ts` is IST bar-aligned without
  microseconds. Any consumer must not compare them naively.
- **F8 — Equity ≠ capital + P&L residual (low, documented).** Σ(runner equity
  deltas) is ₹3,455.45 more negative than Σ(runner realized + unrealized)
  (largest: −₹2,780.53 on the short-strangle runner — collateral/margin-style
  book adjustments; `cash` stays flat at 10,00,000 for every runner). 0.05% of
  capital; worth a deep-dive only if it grows.

## 8. Recommendations

1. Resolve F1 before Monday's demo: one BANKNIFTY momentum runner, not two.
2. Decide F5 (EOD square-off) and F4 (commission accounting) explicitly — both
   are demo-visible ("why does this position show a price at 21:00?").
3. Consider widening the strangle's ₹100 bracket or switching to
   `stop_loss_pct`-style rules; at 1.5 points it trades noise and pays the
   spread 40×/day.
4. Re-run the fills-vs-bars validation (tools/out methodology) against today's
   29 fills to close the loop on fill-price accuracy with an independent source.
