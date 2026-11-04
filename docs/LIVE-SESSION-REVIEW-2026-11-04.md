# Live paper session review — 2026-11-04

Clean-slate session (all instances and trades deleted 2026-11-03, relaunched on `4d13475`).
Seven runners, all `RUNNING` all day, one `:5000` owner (PID 14948) from 02:58:10 to close.
Hourly machine evidence is in `tools/out/LIVE-WATCH-2026-11-04.md` + `.jsonl`; this document is the
judgement layer: what the day's trades say about the system, and what to fix first.

## The money

| book | trades | gross | fees | net |
|---|---|---|---|---|
| `straddle_short_atm·NIFTY·5min` | 3 | +₹2,642.25 | ₹688.25 | **+₹1,954.00** |
| `strangle_wing_points·NIFTY·5min` | 2 | −₹1,033.50 | ₹360.75 | **−₹1,394.25** |
| `CE_instant_buy` / `PE_instant_buy` (1 lot) | 2 | −₹2,847.00 | ₹224.74 | **−₹3,071.74** |
| **closed today** | **7** | **−₹1,238.25** | **₹1,273.74** | **−₹2,511.99** |

Carried into the close: strangle +₹3.25 gross, five equity names +₹1,663.50 ⇒ `open_pnl` +₹1,358.97.
`daily_pnl` −₹1,153.02; equity ₹69,98,846.98 against ₹70,00,000 capital. All three summary figures
reconcile to the ledger exactly.

## Every trade booked

| # | book | entry → exit | held | gross | net | exit_reason |
|---|---|---|---|---|---|---|
| 1 | straddle | 09:15:05 → 09:15:53 | **48 s** | +₹1,352.00 | +₹1,122.29 | take_profit |
| 2 | strangle | 09:15:11 → 09:22:25 | 7 min | −₹2,067.00 | −₹2,248.01 | stop_loss |
| 3 | straddle | 09:20:00 → 10:49:40 | 89 min | **+₹282.75** | +₹53.00 | **stop_loss** |
| 4 | strangle | 09:23:27 → 12:25:23 | 3 h | +₹1,033.50 | +₹853.76 | take_profit |
| 5 | straddle | 10:50:40 → 12:32:34 | 101 min | +₹1,007.50 | +₹778.71 | take_profit |
| 6 | CE 1 lot | 10:06:39 → 15:10:00 | 303 min | −₹237.25 | −₹351.25 | **eod_flat** |
| 7 | PE 1 lot | 10:07:13 → 15:10:00 | 302 min | −₹2,609.75 | −₹2,720.49 | **eod_flat** |

## What the trades say

**1. Trades 6 and 7 are one long straddle that nobody declared.** Same underlying, same strike
(23950), same expiry, opposite sides, opened **34 seconds apart**. Together ₹624.10/unit × 65 =
₹40,566.50 deployed — exactly the `deployed_capital` the summary reported at 14:27. The two runners
are blind to each other, so no net-delta, combined-premium or combined-θ check ever saw the pair.
Their exit block contains **no stop and no target** (`structure_capped: true` and nothing else), so
they were held five hours and exited only because the 15:10 clock forced them. A long straddle bought
19 days from expiry while spot sat flat near 23,920 is a pure theta loss — which is precisely what
−₹2,847 gross is. This is a portfolio-intelligence gap, not a strategy loss.

**2. The clock defect did not just delay the equity book — it concentrated it.** See
`docs/CLOCK-UTC-FLATTEN-LEAK-AUDIT-2026-11-04.md`. `ema_pullback` was refused from 09:15:34 to
14:31:26 (142 refusals), became able to trade at 14:45:00, and on the **first bar it was allowed to
trade** deployed ₹9,94,200.40 — 99.42% of the book — across five names, 44 minutes before the close,
all carried overnight. Its whole tradable window today was 14:45–15:00.

**3. Trade 3 is mislabeled.** +₹282.75 booked as `stop_loss`. `exit_reason` is already wrong on 1 of
7 trades, so any win-rate or exit-quality statistic built on that column is unreliable today.

**4. Fees consumed the entire edge, and trade 1 is the illustration.** ₹1,352 gross in 48 seconds
cost ₹229.71 — 17% of the win. The trigger was `take_profit_points: 1000` compared as **₹1,000 of
structure P&L** on a ₹40,615 credit straddle, i.e. 2.5% of premium collected, which is mark noise.
Trades 4 and 5 earned nearly the same gross while holding for 101 and 181 minutes.

**5. `deployed_capital` ignores short structures.** The strangle's ₹18,541.25 credit contributed
nothing to the ₹9,94,200.40 figure, so the risk dashboard reads "100% equity" while an unlimited-risk
naked short strangle is open.

## What worked — verified, not assumed

- **No fabricated prices anywhere.** 31 orders, all `filled`, every `average_fill_price` and every leg
  mark on the ₹0.05 grid (`mod(price*100,5)=0`); min fill ₹107.85, min trade entry ₹297.60, min exit
  ₹276.05. CE/PE parity implies a coherent spot at each entry (23950 pair → ≈23,941.7; 23850 pair →
  ≈23,887.7). The `OPTION-DEPTH-GARBAGE-2026-11-03` signature is absent from the clean slate.
- **`carry_policy: positional` held.** The 15:10 sweep squared only the four `intraday` books and left
  the strangle open by design, with one `EOD_SQUAREOFF` audit line per intraday book.
- **Both EOD closes reconciled to the paisa** against the runner log (`pnl Rs-237.25` / `Rs-2609.75`
  == `trades.gross_pnl`), so the phantom-mark signature is gone from these paths.
- **Broker-expiry alerting worked**: 15:09:47 `broker_session_expiring_urgent` (9.6 min notice),
  15:19:47 `broker_session_expired`, then `authenticated=false` with a `quote_error` on every open leg.
- **The loss fuse survived**: peak `daily_loss_used` ₹2,408 of ₹2,50,000 (0.96%),
  `halt_mode`/`halt_reason` null all session, zero Traceback in 134 MB of log.
- **Sprint D's `halted BOOLEAN DEFAULT false` fix held a full session**: mirror counts tracked the
  live set (9 `runner_instances` = 7 RUNNING + 2 RETIRED, 14 legs, 3 risk rows), zero
  `DatatypeMismatch`, and the two newly spawned 1-lot books were mirrored on their first write.

## Open product gaps found while the books were blocked

- **No notification on a pre-trade ceiling refusal.** The refusal sets only bridge
  `trade_health`/`block_reason`; `AlertType.RISK_LIMIT_BREACH` is emitted only for breaker trip/reset,
  and `src/backtest/web` contains zero reads of either field. A dead CSS selector
  (`diary.css:712`) is the only trace of the intended badge.
- **No way to revise lots or capital.** The expression is frozen at spawn; `respawn` re-carries the
  old `quantity`, and `/orders/<id>/modify` amends a working order, not a runner. 5 lots → 1 lots
  means delete and re-create.
- **The spawn form's sizing guard is capital-based (₹150/unit estimate), not ceiling-based**, so a book
  whose worst case is 4× the ₹25,000 limit deploys with a success toast and then refuses every entry.
- **The Type column is state-dependent**: an open structure renders its `structure_type`, a flat one
  renders "Direction-aware" from the config's `expression.type` dict — so identical runners display
  different types purely because one is in a trade.

## Fix order for the next session

1. Naive-UTC flatten at the live guard seam (caused both the 5.5-hour blackout and the 14:45 dump).
2. Equity MTM never reaches the ledger — all five rows still read `current_price == average_entry_price`,
   `unrealized_pnl 0`, `last_updated == opened_at` after the close, so tonight's DB asserts them at cost.
3. `risk_session_state` writes `current_daily_pnl 0.0000` and `deployed_capital 70,00,000` on every
   close write — a boot from the SSoT sees a flat, unexposed day.
4. `*_points` → rename to `*_inr` or multiply by lot units; delete the dead `take_profit_pct` that the
   points rule pre-empts.
5. Cross-runner net exposure, so a CE and a PE on the same strike/expiry are one position.
6. `exit_reason` attribution; `deployed_capital` for short structures; refusal notifications.
