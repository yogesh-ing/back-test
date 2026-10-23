# mStock WebSocket Feed — Root Cause & Fixes (2026-10-23)

**Status:** all six causes fixed and live-verified. Paper entries now fill on real
market depth. Tests pin every fix: `tests/test_mticker_ws_fixes.py`,
`tests/test_option_quote_token_routing.py`, `tests/test_risk_supervisor_utilization.py`.

**Do not casually edit `src/backtest/data/mticker.py`** — it is a copied vendor SDK
with three deliberate deviations from the original. Every deviation was paid for
with a full-day outage. See "Guardrails" at the bottom.

---

## 1. The Symptom

Every option entry all session was refused by the fail-closed execution gate:

```
OPTION_BLOCKED NO_EXECUTION_QUOTE: 51330: no ask to BUY against
(quote=one-sided or zero ask)
```

The strategy layer was healthy — `INTENT_RECEIVED` fired every bar, a strike was
selected, the token auto-subscribed — but the quote came back with LTP only:
`bid=0.0 ask=0.0`. The gate correctly refused to buy against a phantom price.
Meanwhile a raw probe harness on the same account received a genuine 5-level
ladder for the same token (`bid 191.60×195 / ask 192.05×1365`), so the server
was never the limitation.

## 2. Why It Was Failing — Six Stacked Causes

The WebSocket path has six independent failure points. Each one alone produces
one-sided or dead quotes. Four days of symptoms were caused by *different
subsets* being fixed at different times.

| # | Cause | Layer | Effect |
|---|-------|-------|--------|
| 1 | WS feed never initialized on the login path | `mstock.py` `_init_websocket_feed` | No socket at all → HTTP LTP only (fixed 2026-10-22) |
| 2 | Mandatory `LOGIN:<token>` text frame never sent after handshake | `mticker.py` `send_login_after_connect` shipped uncalled | Server clean-closes (1000) within ~20 ms of first subscribe → ~3 s reconnect flap forever (fixed 2026-10-22) |
| 3 | Numeric instrument token translated to trading symbol before the depth lookup | `quote_providers.py` `LiveChainProvider.get_quote` | WS cache (keyed on numeric token) never consulted → HTTP LTP only (fixed 2026-10-22) |
| 4 | `sendMessage` called from runner/Flask threads directly | `mticker.py` `subscribe`/`set_mode`/LOGIN | Twisted transport is not thread-safe; the `set_mode(full)` frame was silently lost → token stayed in quote mode → LTP-only ticks even on a healthy socket (fixed 2026-10-23) |
| 5 | On reconnect, resubscribe fired *before* the LOGIN frame | `mticker.py` `_on_open` | Server clean-closes a socket whose first frame is a subscribe → flap returns after every drop (fixed 2026-10-23) |
| 6 | **Trailing commas made OI fields tuples** — `d["open_interest"]=.../divisor,` | `mticker.py` `_parse_binary` (full-mode branch) | `MStockWebSocketWrapper._normalize_tick` did `float(tick['open_interest'])` → `TypeError` **inside the reactor thread** → Twisted killed the socket on *every full-mode option tick* → feed went silent exactly when the first option token subscribed; 32 "Unhandled Error" tracebacks per session (fixed 2026-10-23) |

### Why cause #6 hid for so long

Only full-mode option ticks (184/200-byte packets) hit the tuple bug — index
packets are 32 bytes and assign OI with plain `= 0`. So a freshly-restarted app
streamed index ticks happily, looked healthy, and died the moment a runner
subscribed its first option contract. The offline parser unit-test also passed
because the test data path printed `ltp`/`depth` and never touched
`open_interest`.

## 3. The Fixes (all in `src/backtest/data/mticker.py` unless noted)

1. **`_send_frame()`** — every outgoing text frame (subscribe, set_mode, LOGIN)
   is marshalled onto the reactor thread via `reactor.callFromThread` when the
   reactor is running; direct send only when it is not.
2. **`_on_open()` reordering** — the `on_open` callback (which sends the LOGIN
   frame) fires *before* `resubscribe()`, not after.
3. **Trailing commas removed** on `open_interest`, `open_interest_high`,
   `open_interest_low` — they are floats now, and the wrapper's `float()`
   conversion can no longer throw in the reactor.

Plus the observability layer that made the diagnosis possible (kept on
permanently — one line per event, never per tick):

- `INTENT_RECEIVED` / `ENTRY_SELECT` / `ENTRY_FILLED` — `options_bridge.py`
- `QUOTE_ONE_SIDED` — `quote_providers.py` `_fetch` (the upstream of every
  `NO_EXECUTION_QUOTE`)
- `tick profile` — `mstock_ws_wrapper.py` (per-token mode/depth transitions)
- `RISK_UTILIZATION 50%/80%` — `risk_supervisor.py` (edge-triggered)

## 4. Verification Ladder

1. **Raw probe** (`tools/mticker_ws_probe.py`): V1 URL + LOGIN + subscribe →
   streams 204-byte binary frames that decode to a full ladder; without LOGIN →
   clean 1000 close. Proved the server contract.
2. **Parser offline**: the captured live frame parsed through the production
   `_parse_binary` → 5 bids + 5 asks with sane prices.
3. **End-to-end harness** through MTicker → `MStockWebSocketWrapper` →
   `get_quote("51332")` → `bid=129.8 ask=130.1 oi=47649.55` — the app's own
   code path, after the fixes.
4. **Live paper fills (2026-10-23 11:13 IST, market open):**
   - `CE_instant_buy: BUY NIFTY26OCT22500CE @170.57` (filled against the ask)
   - `PE_instant_buy: BUY NIFTY26OCT22500PE @144.29`
   - `immediate_strangle: SELL 22950CE @31.77 + SELL 22000PE @22.73` (filled
     against the bids, both legs)
   - `tick profile … depth=True`, zero "Unhandled Error", MTM PnL updating on
     subsequent bars.
5. **Regression tests**: each fix has a test that provably fails against the
   pre-fix code (verified via `git stash`), 15 new tests total.

## 5. Guardrails — Read Before Touching `mticker.py`

> **`src/backtest/data/mticker.py` is a patched vendor SDK, not our code style.**
> Do NOT: call `self.ws.sendMessage(...)` directly from anywhere — always go
> through `_send_frame()`; do NOT reorder `_on_open()` (LOGIN must precede
> resubscribe or the server flaps the socket); do NOT add trailing commas or
> "cleanup" the parser assignments (a trailing comma silently turns the field
> into a tuple that kills the reactor on every full-mode tick). If you must
> change this file, run `tests/test_mticker_ws_fixes.py` first and after.

Post-close note: after 15:30 IST the mStock gateway closes sockets every ~3 s
even when fully authenticated — an after-hours flap is the server, not a
regression.
