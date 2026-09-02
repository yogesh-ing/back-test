# Card 06 — Acceptance tests (pins behavior; ALL must pass)

**Prerequisite:** the cards for whatever you're testing. **Purpose:** make
"rebuilt correctly" a green/red signal, not a judgment call.

Use `pytest`. Add a helper that builds an OHLCV frame from a close list (optional
high/low) indexed by consecutive business days.

## `test_backtest.py`
1. Synthetic source: canonical columns, ascending `DatetimeIndex`, > 50 rows.
2. Synthetic source deterministic (same symbol/date ⇒ identical frame).
3. `sma_crossover`, `rsi_reversion`, `buy_and_hold`, `donchian_breakout` all
   auto-registered.
4. Unknown strategy param ⇒ `ValueError`.
5. A full run exposes `total_return, cagr, max_drawdown, sharpe, num_trades,
   win_rate, final_equity` and `len(equity)==metrics["bars"]`.
6. **No look-ahead:** first held position is 0.
7. **Zero-cost buy-and-hold** total return == `close[-1]/close[0]−1`.

## `test_exits.py`
8. Entries `close>100`/exits `close<90` over `[95,101,102,88,89,105]` ⇒
   positions `[0,1,1,0,0,1]`.
9. `donchian_breakout` registered with non-None `stop_loss` & `take_profit`.
10. **Stop-loss caps a loss:** long entered at 100, later bar low pierces the 5%
    stop ⇒ total return == −0.05 (zero costs); held 1 → 0 → stays 0.
11. **Take-profit caps a win:** symmetric ⇒ total return == +0.10.
12. **No-risk path == vectorized:** no stop/target ⇒ total return ==
    `close[-1]/close[0]−1`.

## `test_compare.py`
13. `compare_strategies` runs all requested strategies; each has `sharpe`.
14. All results share an identical bar count (same candles reused).
15. **No stop leak:** `donchian_breakout` + `sma_crossover` together ⇒ donchian
    `stop_loss` non-None, sma_crossover `stop_loss` is `None`.

## `test_mstock_auth.py` (offline; fake client; patch token cache to no-ops)
16. `auth_mode="totp"` ⇒ calls `verify_totp` with the preset code (not
    `generate_session`).
17. `auth_mode="otp"` ⇒ calls `generate_session` with the preset code.
18. `MSTOCK_TOTP` env used when no explicit code is passed.

## `test_plotting.py`
19. `plot_result` returns a figure with 3 axes and writes a PNG.

## `test_forward.py` (Card 05)
20. Offline walk-forward equity reconciles with the vectorized backtest on the
    same slice (no-stop strategy), within tolerance.
21. Per-strategy capital isolation holds.
22. Portfolio state snapshot round-trips.

## Final gate (all must succeed)
```
$env:PYTHONPATH="src"
python -m backtest list
python -m backtest run --strategy donchian_breakout --source synthetic --symbol DEMO --from 2021-01-01 --to 2025-01-01
python -m backtest compare --strategies sma_crossover,rsi_reversion,buy_and_hold --source synthetic --symbol DEMO --from 2021-01-01 --to 2025-01-01
python -m pytest -q
```
**If anything fails, fix the code — never weaken a test.**
