# Trade Bug Finding SOP — Option Strategy Exit & Feed Integrity

**Purpose:** Standard, repeatable procedure to diagnose trades that exit with `exit_price = 0`, wrong `exit_reason`, or instant closes on entry — so every bug is traced root-cause-first instead of guessed.

**When to use:** Any trade in history with `exit_price == 0`, `exit_reason` that doesn't match intent, position closed within seconds of entry, or P&L that doesn't match the mark you see on screen.

---

## Phase 1 — Reproduce & Capture the Symptom

| Step | Action | Where | What to record |
|------|--------|-------|----------------|
| 1.1 | Query aggregated option trades | `GET /api/portfolio/trades/aggregated?kind=option` | Full JSON; save to file |
| 1.2 | Filter for suspect trades | `exit_price == 0` OR `holding_seconds < 60` OR `exit_reason` unexpected | Trade rows: symbol, strikes, entry_price, exit_price, pnl, exit_reason, strategy, instance_prefix, entry_ts, exit_ts, exit_ist_label |
| 1.3 | Note the market session | Current date / time (IST) | Was the market open? Was it expiry day? |
| 1.4 | Check broker status | `GET /api/broker/status` | Which broker is UI-active? mStock vs Upstox? Authenticated? Expires when? |

**Output:** A "suspect trade sheet" — one row per bad trade with all fields above.

---

## Phase 2 — Map the Trade to Its Strategy & Exit Rule

| Step | Action | File:Line | What to check |
|------|--------|-----------|---------------|
| 2.1 | Identify strategy from `strategy` field | `plugins/strategies/<name>.py` | Read the strategy's `params` block |
| 2.2 | Read `min_days_to_expiry` | Strategy plugin, `params` | Default is `None` in plugin, but `ExitConfig.min_days_to_expiry` defaults to **1** (`src/backtest/options/exit_policy.py:327`). 0DTE → `0 <= 1` → fires on bar 1 |
| 2.3 | Read SL/TP defaults | Strategy plugin `params` | `stop_loss_points`, `take_profit_points`, `stop_loss_rupee`, `take_profit_rupee`. All opt-in; None = disabled |
| 2.4 | Read squareoff knob | Strategy plugin `params` | `squareoff_minutes_before_close` (default 30) maps to `squareoff_minutes_before` at execution top level |
| 2.5 | Confirm which exit rule fired | `src/backtest/options/exit_policy.py:485` (`ExitPolicy.evaluate`) | Rule order: risk cap → **DTE square-off** → stop loss → take profit → time stop → signal flip → signal neutral. First match wins |

**Key insight:** DTE (`min_days_to_expiry`) fires **before** SL/TP and does **not** need a valid mark price. A 0DTE strangle on expiry day closes on bar 1 regardless of SL/TP settings.

---

## Phase 3 — Trace the Feed Subscription (mStock vs Upstox)

### mStock path
| Step | Action | File:Line |
|------|--------|-----------|
| 3.1 | WS feed init | `src/backtest/brokers/mstock.py:370` (`_init_websocket_feed`) |
| 3.2 | Default index tokens subscribed on connect | `mstock.py:800` (`_subscribe_default_indices`), `mstock.py:488` (`NIFTY→26000`, etc.) |
| 3.3 | Leg tokens pinned after entry | `src/backtest/forward/options_bridge.py:3617` (`_pin_structure_legs` → `quote_provider.pin_contracts`) |
| 3.4 | WS subscribe frame | `src/backtest/data/mticker.py:397` (`subscribe` → `{"a":"subscribe","v":[tokens]}`) |
| 3.5 | Instrument tokens are **numeric ints** | `mstock.py:833` (converts each token to `int`) |

### Upstox path
| Step | Action | File:Line |
|------|--------|-----------|
| 3.1 | WS instrument subscription | `src/backtest/brokers/upstox.py:1032` (`subscribe_tokens_ws`) |
| 3.2 | Instrument keys are `NSE_FO|...` strings | `upstox.py:810`, `upstox.py:244` (`resolve_instrument_key`) |
| 3.3 | Quote HTTP fallback | `broker.get_option_quote(token)` |

### Diagnostic question
> Was the leg's `instrument_token` registered/pinned with the quote provider **before** the exit?

- If **no** → `get_option_quote` returns empty or `{"ltp": 0, ...}` with no `"error"` key
- If **yes** but WS feed died → cached quote may be stale (>10s) → collar returns `None` → zero fill booked

---

## Phase 4 — Trace the Exit Pricing (The Zero-Price Bug)

| Step | Action | File:Line |
|------|--------|-----------|
| 4.1 | Exit decision → close | `src/backtest/forward/options_bridge.py:2299` (`_exit_structure`) → `option_broker.close_structure()` |
| 4.2 | Price each leg before closing | `src/backtest/options/paper_trading.py:1173` (`close_structure` → `_price_exit_leg` per leg) |
| 4.3 | Get execution quote | `paper_trading.py:1373` (`_execution_quote(quote_provider, token, side)`) |
| 4.4 | Read LTP | `paper_trading.py:1407`: `quoted_price = Decimal(str(quote.get("ltp", 0) or 0))` |
| 4.5 | Failed-quote check | `paper_trading.py:1374`: `is_failed_quote` = non-dict / empty / `quote.get("error")` truthy |
| 4.6 | **The gap** | A quote with `ltp=0` and **no** `"error"` key passes `is_failed_quote = False` → exit_price = 0 is booked |

**Root cause:** `src/backtest/options/quote_providers.py:571-575` — `_fetch` returns `{"ltp": 0.0, "bid": ..., "ask": ...}` with no `"error"` when bid/ask exist but LTP is zero. The exit path treats zero LTP as a valid price.

**Collar check (may or may not catch it):** `paper_trading.py:1270` (`_collar_problem`) — in `"enforce"` mode with a fresh mark (age ≤ 10s), a zero fill deviating from `position.current_price` raises `ProtectedExitRefusedError`. In `"off"`/`"shadow"` mode, or if mark is stale (>10s), zero fill is booked.

---

## Phase 5 — Confirm the Fix Point

Two places to harden (pick one or both):

1. **`quote_providers.py:571-575`** (`_fetch`): treat `ltp <= 0` as failed → return `{"error": "zero_ltp", ...}` so `ProtectedExitRefusedError` fires and the structure stays open.

2. **`paper_trading.py:1374`** (`is_failed_quote`): add `quote.get("ltp", 0) <= 0` to the failure condition so a zero-LTP quote is refused before booking.

---

## Phase 6 — Strategy Creation / Update Review Checklist

Before deploying or editing any option strategy, verify:

| # | Check | Pass criterion |
|---|-------|---------------|
| 1 | `min_days_to_expiry` | For 0DTE intraday strategies: set explicitly to `None` (ride to expiry) **or** accept DTE square-off. Default platform value is 1 — 0DTE closes on bar 1. |
| 2 | SL/TP values | `stop_loss_points` / `take_profit_points` (or rupee variants) set to intended values. All `None` = no SL/TP — the only exit is DTE/time/signal. |
| 3 | `exit_unit` | `"points"` or `"rupee"` — matches how SL/TP params are interpreted. |
| 4 | `squareoff_minutes_before_close` | Set (default 30) so positions flatten before broker RMS auto-squareoff (~15:15–15:20 IST). |
| 5 | IV filter params | `min_iv_percentile` / `max_iv_percentile` — prevents entry in extreme IV regimes. |
| 6 | Alert hooks | `on_alert` / `on_alert_resolved` / `subscribed_alerts` — operator notified on exit. |
| 7 | Feed subscription confirmed | Leg `instrument_token` registered with quote provider before entry (`_pin_structure_legs`). |
| 8 | Exit pricing guard | Zero-LTP quotes refused (fix applied in Phase 5) — no trade closes at `exit_price = 0`. |

---

## Phase 7 — Post-Fix Verification

| Step | Action |
|------|--------|
| 7.1 | Re-run the suspect trade scenario in paper mode |
| 7.2 | Confirm exit now books at a non-zero LTP from the feed |
| 7.3 | Confirm `exit_reason` matches the actual rule (e.g. `auto_square_off` → `eod_flat`, not a mystery) |
| 7.4 | Run `pytest` for strategy execution + base + bundle + adapter tests |
| 7.5 | Check `docs/MASTER_OBSERVATION_BUG_LOG.md` — add new bug entry if root cause is new |

---

## Quick Reference: The Full Flow (Deploy → Exit)

```
1. Deploy strategy (e.g. strangle_wing_points)
   └─ Params: stop_loss_points=13, take_profit_points=7, squareoff_minutes_before_close=30,
        min_days_to_expiry=None (plugin), ExitConfig default=1 (platform)

2. OptionsBridge._execute() (options_bridge.py:2782)
   ├─ Generate chain → pick strikes → build TradeIntent
   ├─ Each leg: instrument_token + trading_symbol from chain contracts

3. OptionPaperBroker.execute_structure() (paper_trading.py:899)
   ├─ Entry: _execution_quote() → fill at LTP/bid-ask
   ├─ Create OptionPosition per leg (instrument_token, trading_symbol)
   └─ _pin_structure_legs() (options_bridge.py:3617) → register leg tokens with quote provider

4. Market feed (per broker)
   ├─ mStock: MTicker.subscribe() → {"a":"subscribe","v":[numeric_tokens]} (mticker.py:397)
   └─ Upstox: subscribe_tokens_ws() → subscribes NSE_FO|... keys (upstox.py:1032)

5. Per-bar loop (options_bridge.py)
   ├─ Mark legs via quote_provider.get_quote(token)
   ├─ _evaluate_exit() → ExitPolicy.evaluate()
   │   ├─ Step 1: Risk cap (max_loss_inr)
   │   ├─ Step 2: DTE square-off ← FIRES for 0DTE (0 ≤ 1 day) on bar 1
   │   ├─ Step 3: Stop loss (points × units or pct × premium)
   │   ├─ Step 4: Take profit
   │   ├─ Step 5: Time stop (max_bars)
   │   └─ Step 6: Signal flip / neutral
   └─ _exit_structure() → close_structure() → _price_exit_leg()

6. Exit pricing (paper_trading.py:1347-1407)
   ├─ quoted_price = quote.get("ltp", 0)
   ├─ ltp=0 + no "error" key → NOT failed → exit_price = 0 booked  ← THE BUG
   └─ Exit collar may refuse (enforce mode + fresh mark) or allow (off/shadow/stale mark)

7. Trade record (trade_persistence.py:584)
   └─ exit_price = 0 → shows as 0.00 in history; exit_reason mapped via _map_exit_reason()
```

---

## Files to Keep Handy

- `src/backtest/options/exit_policy.py` — exit rule logic & defaults
- `src/backtest/forward/options_bridge.py` — bridge, entry, exit, pinning
- `src/backtest/options/paper_trading.py` — `_price_exit_leg`, `close_structure`, collar
- `src/backtest/options/quote_providers.py` — `_fetch`, `LiveQuoteProvider.get_quote`
- `src/backtest/forward/trade_persistence.py` — `_map_exit_reason`, DB persistence
- `plugins/strategies/strangle_wing_points.py` — example rectified strategy
- `docs/MASTER_OBSERVATION_BUG_LOG.md` — bug log
- `docs/STRATEGY_RECTIFICATION_FINDINGS.md` — strategy rectification reference
