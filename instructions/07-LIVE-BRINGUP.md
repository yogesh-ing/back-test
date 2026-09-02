# Card 07 — Live-market bring-up + build-order checklist

**Prerequisite:** Cards 00–06 green. **Purpose:** connect to mStock safely and
sequence the whole build. Only do the live steps where market access exists.

## Live bring-up (before any live paper trading)
1. Fill `.env` (set `MSTOCK_AUTH_MODE=totp` if you use an authenticator app).
2. `python -m backtest preflight` — must be **all-green**, especially **DNS +
   HTTPS reachability** for `api.mstock.trade`. This is the real gate for live
   forward testing; if DNS fails, fix network/VPN/proxy first.
3. First `--source mstock` run triggers OTP/TOTP; enter it promptly (TOTP
   expires ~30s). Confirm a historical pull parses. If the payload shape differs,
   adjust **only** `_candles_to_frame` (Card 02).
4. Start forward testing in **`walkforward`** mode (offline) to validate, then
   move to **`live`** paper mode with **small per-strategy dummy capital**.

## Build-order checklist
1. **Card 01** skeleton + deps + packaging → `python -m backtest` importable.
2. **Card 02** data layer → synthetic source works.
3. **Card 03** strategy system → `list` shows 4 strategies.
4. **Card 04** engine/metrics/runner/CLI → `run` + `compare` work on synthetic.
5. **Card 06** tests 1–19 green.
6. **Card 05** forward layer → Card 06 tests 20–22 green.
7. **This card** live bring-up (only where market access exists).

## Golden rules (repeat)
- Keep **Card 00** invariants exact.
- Never edit shared infra to change results; never weaken a test.
- This system trades against a live market — correctness first.
