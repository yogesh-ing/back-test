# Strategy Guidelines — rules for building strategies that survive contact with the market

> **Who this is for:** anyone writing a strategy for this platform (equity or
> options, plugin or built-in).
> **What it is:** the *rulebook*. Every rule is numbered (`R-x`), says **why**,
> and — where the platform behaves in a non-obvious way — cites what was
> verified in the code. The mechanics of dropping a file in are in
> [STRATEGY-AUTHORING.md](STRATEGY-AUTHORING.md); alert hooks are in
> [STRATEGY-ALERTS.md](STRATEGY-ALERTS.md).
> **The executable half:** [`templates/strategy_test_template.py`](../templates/strategy_test_template.py)
> turns most of these rules into tests. Copy it for every new strategy.

Most strategies that fail in production don't fail because the idea was bad.
They fail because the backtest was lying (lookahead, zero costs, a stop that
was never enforced), because forward-testing silently computed something
different from the backtest, or because a parameter meant "points" on one
screen and "percent" in the code. This document exists to make those failures
impossible to ship by accident.

---

## Contents

0. [The non-negotiables](#0-the-non-negotiables)
1. [Before you code: the strategy spec sheet](#1-before-you-code-the-strategy-spec-sheet)
2. [Anatomy: every field and hook, and who reads it](#2-anatomy-every-field-and-hook-and-who-reads-it)
3. [Ownership: what is yours and what is not](#3-ownership-what-is-yours-and-what-is-not)
4. [The rules](#4-the-rules)
   — [R-C Contract](#r-c--contract) · [R-D Data & time-consistency](#r-d--data-lookahead-and-backtest--forward)
   · [R-T Time & sessions](#r-t--time-and-sessions) · [R-S Equity signals](#r-s--equity-signal-semantics)
   · [R-O Option views](#r-o--option-view-semantics) · [R-R Risk & exits](#r-r--risk-and-exits)
   · [R-M Parameters](#r-m--parameters) · [R-E Evaluation](#r-e--evaluation-and-overfitting)
   · [R-P Performance](#r-p--performance) · [R-A Alerts](#r-a--portfolio-alerts)
   · [R-V Versioning & docs](#r-v--versioning-and-documentation)
5. [Anti-pattern catalogue (bad → good)](#5-anti-pattern-catalogue)
6. [Proven patterns (copy these)](#6-proven-patterns)
7. [Testing and the validation ladder](#7-testing-and-the-validation-ladder)
8. [Platform facts that bite (verified)](#8-platform-facts-that-bite-verified)
9. [Review checklist](#9-review-checklist)

---

## 0. The non-negotiables

If you remember nothing else:

1. **A strategy is a pure function of `(candles, params)`.** No clock, no
   randomness, no state carried on `self` between calls, no I/O. *(R-C2, R-D4)*
2. **It must compute the same decision on a 500-bar window as on full
   history.** Backtest sees everything; the forward runner sees a sliding
   500-bar buffer. *(R-D2)*
3. **The default backtest and paper runners charge zero costs, and ignore a
   class-level `stop_loss` / `take_profit`.** Your exits and your cost
   haircut are your job. *(R-R1, R-E1)*
4. **For options, `None` means "no trade" and `NEUTRAL` means "open a
   trade".** `confidence` is not a gate unless you make it one. *(R-O1..O3)*
5. **Equity strategies are long-only in practice.** `-1` is treated as flat
   in the default engine and in forward runners. *(R-S2)*

---

## 1. Before you code: the strategy spec sheet

Write this in the module docstring **before** writing logic. If you can't fill
a line in, you are not ready to code. Reviewers reject strategies without it.

```text
STRATEGY SPEC — <name> v<version>

Hypothesis     Why should this make money? Who is on the other side, and why
               do they keep losing to you? (Behavioural, structural, risk
               premium — pick one and defend it.)
Instrument     Equity (single / pool) or option; which underlyings
               (→ eligible_instruments).
Timeframe      Bar size it is designed for (1min / 5min / 15min / 1hour /
               1day) and why. Holding period in bars AND in wall time.
Entry          Exact rule, in terms of closed bars only.
Exit           Every exit, and WHO enforces it (strategy signal, runner
               manual stop, option expression.exit, DTE square-off).
Risk           Max loss per trade (₹ or % of bucket); where it is enforced.
Frequency      Expected trades per day/week. (Too few → no statistics;
               too many → costs eat it.)
Cost budget    Round-trip cost estimate and the break-even gross edge/trade.
Regime         When it should work / fail (→ regime_vix_range).
Capacity       Liquidity assumptions (lots, spread, OI).
Kill criteria  What forward result makes you switch it off (e.g. "30
               trades, expectancy < 0" or "drawdown > 2× backtest max").
Approximations What the spec asks for that the platform cannot express, and
               the stand-in you used (see ema_reversion_pob.py for a good
               example of this section).
Recommended runner config
               The RunnerConfig / expression JSON you tested with.
```

---

## 2. Anatomy: every field and hook, and who reads it

### Class attributes

| Field | Req. | Type | Read by | If wrong / missing |
|---|---|---|---|---|
| `name` | ✅ | `str`, unique, snake_case | registry, spawn form, runner configs, persisted state | Collision → plugin skipped. **Renaming orphans every saved runner.** Never rename a deployed strategy. |
| `description` | ✅ | `str` | catalog / spawn form | Loader refuses. Write what it does *and* what timeframe it expects. |
| `version` | ✅ | `str` (`"1.2"`) | catalog, audit | Loader refuses. Bump on **any** behaviour change (R-V1). |
| `author` | ✅ | `str` | catalog | Loader refuses. |
| `params` | ✅ (may be `{}`) | schema dict | spawn form, `__init__` binding, validation | Unlabelled → refused. Unbounded → UI allows nonsense (R-M2). |
| `eligible_instruments` | options: ✅ | `list[str]` or `None` | spawn form dropdown + create API | `None` on an index-only strategy lets users spawn it on RELIANCE. |
| `regime_vix_range` | recommended | `(lo, hi)` or `None` | Risk Board "regime fit", `vix_regime_change` alert | Informational only; lying here misleads the operator. |
| `subscribed_alerts` | optional | `tuple[str]` | alert broker | Unknown type → `ValueError` at subscribe. See R-A. |
| `stop_loss`, `take_profit` | ⚠️ **avoid** | fraction | **only** the legacy `quick_screen` backtest | Silently ignored by the default engine and forward runners (R-R1). |

### Hooks (implement exactly one signal path)

| Hook | Returns | Kind | Notes |
|---|---|---|---|
| `generate_signals(candles)` | `pd.Series[int]` in `{-1,0,1}`, aligned to `candles` | equity | Preferred for equity. Vectorise it (R-P1). |
| `entries(candles)` + optional `exits(candles)` | `pd.Series[bool]` | equity | Convenience; the base class converts with a per-bar `.loc` loop (~25 ms / 500 bars — R-P2). |
| `generate_market_view(candles)` | `MarketView` or `None` | **option** | Overriding it is what *makes* the strategy an option strategy. Keep a trivial `entries()` too (R-O7). |
| `on_alert(type, data)` / `on_alert_resolved(type, data)` | ignored | both | Runs under the runner lock on the evaluator thread (R-A2). |

### Levers the strategy may pull (and nothing else)

| Lever | Effect |
|---|---|
| `self.pause_new_entries = True/False` | Runner skips new entries; exits keep working. |
| `self.request_exit(fraction, position_key, reason)` | Runner closes through the engine on the next bar. Option structures: all-or-nothing. |

### `candles` — what you receive

* Columns `open, high, low, close, volume` (float64), ascending.
* **Backtest:** the whole requested range plus warmup, one call.
* **Forward:** the trailing **≤ 500** bars (`MAX_BARS_PER_SYMBOL`), **one call
  per bar**, every bar.
* Index: a tz-naive `DatetimeIndex` (IST wall-clock) when the feed supplies
  parseable timestamps — **otherwise a `RangeIndex`** (R-T2).
* Every row is a **closed** bar. The last row is the bar that just closed.

---

## 3. Ownership: what is yours and what is not

The architecture is *strategy owns WHAT, engine owns HOW*. Crossing that line
is the single most common source of backtest/live divergence.

| Decision | Owner | Where it is configured |
|---|---|---|
| Direction / target position | **Strategy** | `generate_signals` / `generate_market_view` |
| Conviction filter | **Strategy** | your own `min_confidence` param (R-O3) |
| Signal-based exit (flip, indicator exit) | **Strategy** | the signal going to `0` / the view flipping |
| Price-based stop / target — **equity** | Strategy logic *or* operator | compute it inside the signal (pattern 6.3), or per-position manual SL/TP in the UI |
| Price-based stop / target — **options** | Runner config | `instrument.expression.exit`: `stop_loss_pct`, `take_profit_pct`, `stop_loss_points`, `take_profit_points`, `max_bars`, `min_days_to_expiry`, `neutral_bars`, `signal_flip`, `reenter` |
| Position size | Runner | `RunnerConfig.position_pct` (default 95% single / `1/max_pool_positions` pool); strategy cannot size |
| Structure type, strikes, lots, expiry | Runner config | `expression.type` (str or `{"BULLISH":…, "BEARISH":…}`), `strike_selection` (`atm`/`delta`), `delta_target`, `quantity`, `expiry_min_days` |
| Lot size | Instrument master | never hard-code |
| Fills, slippage, fees | Engine | executor profile (zero-cost by default — R-E1) |
| Timeframe | Runner | `RunnerConfig.timeframe`; the strategy is **not told** (R-T3) |
| Circuit breakers (drawdown 25%, daily loss 15%) | Platform | `RunnerConfig.max_drawdown_pct`, `daily_loss_limit_pct` — a last resort, not your stop (R-R5) |
| Response to a portfolio alert | **Strategy** | `on_alert` (R-A) — the platform never acts on alerts |

---

## 4. The rules

`MUST` = review blocker. `SHOULD` = needs a written reason to skip.

### R-C — Contract

**R-C1 (MUST) Pass the conformance battery.** Unique name, metadata, labelled
params, aligned output, determinism. The loader runs it and skips failures;
`tests/test_strategy_conformance.py` asserts it.

**R-C2 (MUST) Import only `pandas`, `numpy`, `math`/stdlib maths,
`backtest.strategy.*` and `backtest.alerts.*`.** Banned (AST-checked,
refused at load): `backtest.brokers|forward|options|data|live`, `requests`,
`urllib*`, `websocket*`. The engine hands you data; you never fetch it. This
is what makes backtest, paper and live the *same function*.

**R-C3 (MUST) No clock, no randomness.** No `datetime.now()`, `time.time()`,
`random`, `np.random` (not even seeded — seeds hide non-stationarity rather
than remove it). Time comes from `candles.index`.

**R-C4 (SHOULD) Validate cross-parameter constraints in `__init__`.**
`fast < slow`, `lower < upper`: call `super().__init__(**overrides)` then
raise `ValueError` with a message a trader understands.

### R-D — Data, lookahead, and backtest ≡ forward

**R-D1 (MUST) No lookahead.** The decision for bar *t* may use rows `≤ t`
only. The obvious form (`shift(-1)`) is rare; the subtle forms are common:

| Subtle lookahead | Why it leaks |
|---|---|
| `rolling(n, center=True)` | window includes future rows |
| z-score / min-max with **full-series** mean, std, max | the normaliser knows the future |
| `fillna(method="bfill")` / `.bfill()` | copies future values backwards |
| `resample(...)` with `label="right"`/`closed="right"` then aligning to the *start* | the bucket's value is only known at its end |
| using today's daily high/low/close before the day ends | the "daily" bar is incomplete |
| `np.polyfit` / regression over the whole frame, then reading slope at *t* | fit used later points |

Test: **truncation** — the decision at *t* on `candles[:t+1]` must equal the
decision at *t* on the full frame. The test template does this on three
regimes.

**R-D2 (MUST) Backtest ≡ forward: be window-invariant within 500 bars.**
The forward runner calls you with the last ≤ 500 bars. So:

* **No positional logic.** `len(candles)`, `iloc[0]`, `range(len(...))`
  parity, "bars since the start of the frame". *Verified:* `time_alternator`
  uses `len(candles)` parity; once the forward buffer fills, its direction
  **freezes** (the template fails it on 100% of probes).
* **No frame-anchored cumulative measures.** VWAP / cumsum / expanding
  max "since the first row" differ between backtest and forward. Anchor to
  the **session date** instead (pattern 6.1). *Verified:* `vwap_ema_rsi`
  anchors VWAP to the first row and disagrees with itself on ~2% of 1-min
  bars.
* **Lookback budget.** An EMA needs ~4–5× its span to forget its seed; a
  stateful loop needs its state to *reset* inside the window. Rule of thumb:
  **longest lookback × 3 ≤ 500 → ≤ ~160 bars.**
* **Know what 500 bars is in wall time:**

  | Bar | 500 bars ≈ | Consequence |
  |---|---|---|
  | 1min | 1.3 sessions | no multi-day indicators at all |
  | 5min | 6.7 sessions | daily EMA(5) is barely possible |
  | 15min | 20 sessions | ~1 month |
  | 1hour | ~70 sessions | ~3 months (7 bars/session) |
  | 1day | ~2 years | fine for almost anything |

**R-D3 (MUST) Warm up honestly.** With fewer bars than your lookback,
return flat (`0` / `None`). Never emit a trade off a half-formed indicator.
Option runners **skip** the platform's 12-bar warmup, so you may be called
with a single bar.

**R-D4 (MUST) Keep `self` read-only during evaluation.** In a backtest you
are called once over the whole series; in forward, once per bar on the same
instance, for weeks; after a restart, on a fresh instance. Any state carried
on `self` makes those three disagree. If you need state (position-aware
logic, trailing stops), **recompute it from the frame** each call
(pattern 6.3) and make sure it resets (R-D2). *Exception:* the alert levers
(`pause_new_entries`, exit requests) — they are control flags, not signal
inputs.

**R-D5 (MUST) Survive degenerate data.** Flat prices (σ = 0 → RSI and
z-scores divide by zero), zero volume (VWAP divides by zero), a NaN, a
single bar, an overnight gap of several %. Never return NaN; guard
divisions; treat "undefined" as flat.

**R-D6 (SHOULD) Don't depend on the fill price.** Backtest fills at the
**next bar's open**; paper runners fill at **this bar's close**. They agree
only on gapless data. An edge that disappears with one bar of latency, or
that lives inside the bar you signalled on, is not an edge you can trade.

### R-T — Time and sessions

**R-T1 (MUST) Treat the index as IST wall-clock, tz-naive.** If you ever
receive a tz-aware index, convert (`tz_convert("Asia/Kolkata").tz_localize(None)`)
before comparing to `09:15` / `15:00`.

**R-T2 (MUST) Handle a `RangeIndex`.** If timestamps are unusable the runner
passes a positional index. Time-aware strategies must detect it
(`isinstance(candles.index, pd.DatetimeIndex)`) and return flat — not raise.
A raise is caught and logged as `ERROR` **every bar**, and the runner never
trades. *Verified:* `ema_reversion_pob` raises `AttributeError` here.

**R-T3 (MUST) Don't assume a bar size.** The strategy is not told the
timeframe. Either write logic in bars and state the intended timeframe in
`description`, or derive it: `candles.index.to_series().diff().median()`.
Express session rules (no entries after 15:00, first 30-minute candle) in
**clock time from the index**, not in bar counts.

**R-T4 (SHOULD) Reconstruct higher timeframes from completed buckets only.**
Daily values: group by date and **exclude today** (pattern 6.2). Resampled
bars: only use buckets whose end ≤ the last bar's timestamp.

### R-S — Equity signal semantics

**R-S1 (MUST) Return target positions:** `int` Series in `{-1, 0, 1}`,
same index and length as `candles`. `1` held for 10 bars means "hold for 10
bars", not "buy 10 times". A pulse (`1` then `0` next bar) means a one-bar
trade.

**R-S2 (MUST) Assume long-only.** *Verified:* an always-`-1` strategy makes
**zero trades** in the default backtest engine and in a forward runner even
with `allow_short=True` (`-1` is treated as exit/flat). Only the legacy
`quick_screen` mode shorts. Express bearish views through an **option**
strategy.

**R-S3 (MUST) Make sure it actually trades.** A strategy that never changes
its decision across trend, chop and crash has a warmup longer than the buffer
or thresholds that never trigger. The runner logs "produced NO signals" —
don't make it.

**R-S4 (SHOULD) Know how pool mode ranks you.** In `SYMBOL_UNIVERSE` runners,
when more symbols signal `1` than there are free slots, candidates are ranked
by a **generic** score, `(SMA20 − close) / close` — it favours laggards. A
momentum strategy in pool mode gets contrarian ranking. Account for it (e.g.
keep `max_pool_positions` ≥ typical simultaneous signals) until strategies
can supply their own score.

### R-O — Option view semantics

**R-O1 (MUST) `None` = no trade.** Return `None` whenever you have no view.
With a structure open, a `None` bar counts toward `exit.neutral_bars`.

**R-O2 (MUST) `NEUTRAL` opens a trade.** *Verified:* a `MarketView(NEUTRAL,
confidence=0.0)` with nothing open made the bridge **open a
`bull_call_spread`** (the default direction map sends anything not BEARISH
to the BULLISH structure). Only return `NEUTRAL` if you *mean* to open the
expression's structure (e.g. a short strangle with `type: "strangle"`), and
set `ALLOWS_NEUTRAL_VIEW = True` in your test. The base class's derived view
(for strategies that don't override it) returns `NEUTRAL` on a flat signal —
another reason to override it properly.

**R-O3 (MUST) Gate on confidence yourself.** The bridge logs `confidence` but
does **not** filter or size on it. Declare a `min_confidence` param and
return `None` below it. Define what confidence *means* (e.g. "distance from
EMA / 100 pts, capped at 1") in the tooltip.

**R-O4 (MUST) Fill the view honestly.** `spot_price` = last close;
`bar_timestamp` = last index value; `underlying` from a param and inside
`eligible_instruments`; `confidence` in `[0, 1]`; put diagnostic values
(RSI, gap %) in `metadata`, not in log lines.

**R-O5 (SHOULD) Add hysteresis.** While a structure is open, every view is
an exit signal: a direction flip closes it (`signal_flip` defaults to true)
and, with `reenter`, opens the reverse on the next bar. A view that flickers
BULLISH/BEARISH around a threshold churns premium and spread. Use separate
enter/exit thresholds or a hold band (pattern 6.5).

**R-O6 (MUST) Leave structure, strikes, lots and expiry to the expression.**
Document the recommended expression in the docstring; don't encode strike
logic in the view. If the spec needs something the expression cannot do,
write it down under *Approximations*.

**R-O7 (SHOULD) Keep a trivial `entries()` fallback** so the class stays
valid on the equity path (`validate()` requires a signal hook).

### R-R — Risk and exits

**R-R1 (MUST) Never rely on class-level `stop_loss` / `take_profit`.**
*Verified with an always-long probe on a 30% decline:* `quick_screen` stopped
out at −2.2%; the **default backtest engine lost 29.4%** (stop ignored); the
**forward runner rode it down until the 25% portfolio drawdown breaker
flattened everything**. Until the engine enforces them, treat those
attributes as documentation only.

**R-R2 (MUST) Every strategy has a defined maximum loss per trade, enforced
somewhere you can point to.** Equity: inside the signal (ATR / % stop
state machine — pattern 6.3), or a documented manual stop. Options:
`expression.exit.stop_loss_pct` or `stop_loss_points`.

**R-R3 (MUST) Short premium always has a stop and a DTE rule.** Short
strangles/straddles: `stop_loss_points` (₹) or `stop_loss_pct`, and
`min_days_to_expiry` (default 1) or `squareoff_minutes_before`. Subscribe to
`portfolio_gamma_critical` (R-A1).

**R-R4 (SHOULD) Long premium has a time stop.** Theta is a guaranteed loss;
`max_bars` caps it.

**R-R5 (MUST) Breakers are not your stop.** The per-runner drawdown (25%)
and daily-loss (15%) breakers exist so a bug can't empty the account. A
strategy whose risk plan is "the breaker will catch it" fails review.

### R-M — Parameters

**R-M1 (SHOULD) Few parameters, each with economic meaning.** ≤ 5 tunable
params. Every extra knob is another dimension to overfit. "Lookback",
"entry z", "stop in ATRs" — yes. "Magic multiplier 3" — no.

**R-M2 (MUST) Schema form, bounded, labelled, with units in the tooltip.**
`min`/`max` on every numeric param, set to the *sensible* range, not the
representable one — the spawn form lets users pick any value inside it, and
the test template runs your strategy at both ends.

**R-M3 (MUST) Say the unit. Prefer scale-free units.** "Points", "%",
"bars", "ATR multiples" — say which. Points are instrument-specific (100 pts
is 0.4% of NIFTY and 30% of a ₹300 stock); `%` or ATR multiples work on
both. If you must use points, say "NIFTY-sized" in the tooltip.

**R-M4 (SHOULD) Defaults are robust, not optimal.** Pick defaults from the
middle of a performance plateau, not the peak (R-E3).

**R-M5 (MUST) No hidden constants.** A number that changes behaviour is a
param or a named module constant with a comment explaining it
(`POB_LOWER = 0.44  # Fibonacci level from the spec`).

### R-E — Evaluation and overfitting

**R-E1 (MUST) Haircut for costs.** *Verified:* the default backtest engine
and paper runners use a **zero-cost executor** (no brokerage, no statutory
fees, no slippage). A strategy that is profitable at zero cost can be a
loser at real cost. Minimum standard:

1. Compute **gross edge per trade** (avg P&L per trade) and **turnover**.
2. Estimate round-trip cost for the instrument (brokerage + STT/exchange/
   SEBI/stamp/GST + half-spread × 2; options: the bid-ask on premium usually
   dominates). Use your broker's contract note — statutory rates change.
3. Require **gross edge ≥ 2× round-trip cost**. Report net expectancy.
4. Cross-check with the backtest's `quick_screen` mode (applies 3 bps
   commission + 5 bps slippage per side) — if the equity curve collapses
   there, it will collapse live.

**R-E2 (MUST) Out-of-sample.** Choose parameters on one period; report on a
later, untouched one. Better: walk-forward (re-fit on a rolling window,
trade the next block). Never report the in-sample curve as "the result".

**R-E3 (MUST) Parameter stability.** Performance at the chosen parameters
must be similar at ±20% of each. A sharp peak surrounded by losses is a
fitted artefact.

**R-E4 (SHOULD) Enough trades.** < 30 trades: anecdote. ~100+: you can
start talking about expectancy. Report the count with every metric.

**R-E5 (MUST) Test across regimes.** Trend, chop, crash/vol spike
(the template's scenarios), and at least one real high-VIX period. State
the regime it loses in — every strategy has one.

**R-E6 (SHOULD) Beat the dumb benchmark.** Compare against buy-and-hold
(equity) or the unconditional structure (options: always-on spread) on
the same period, net of costs.

**R-E7 (MUST) Paper before live.** Forward-test in the paper bucket for
enough sessions to see the planned number of trades, and compare realised
behaviour (trade frequency, avg P&L, hold time) with the backtest. A large
divergence is a bug until proven otherwise. Apply your kill criteria.

### R-P — Performance

**R-P1 (MUST) ≤ 20 ms per evaluation on a 500-bar frame.** Forward runners
evaluate every symbol every bar; a 50-symbol pool at 20 ms is already 1 s per
tick. Vectorise with pandas/numpy.

**R-P2 (MUST) Stateful loops run on numpy arrays, never `.loc`.**
*Measured:* the same Bollinger state machine takes **23.9 ms** with
`.loc[i]` access and **0.15 ms** over `.to_numpy()` arrays — identical
output, ~160× faster (pattern 6.3). The base class's `entries/exits`
conversion is itself a `.loc` loop (~25 ms/500 bars); prefer a vectorised
`generate_signals` for pool runners.

**R-P3 (MUST) No I/O, no per-bar logging above DEBUG.** A strategy never
writes files or calls anything over a network (R-C2), and doesn't spam logs
500 times a second.

### R-A — Portfolio alerts

**R-A1 (SHOULD) Subscribe to what threatens your structure.** Short gamma /
short premium: `portfolio_gamma_critical`, `vix_regime_change`. Anything:
`data_feed_stale` if trading on stale marks is dangerous for you.

**R-A2 (MUST) Handlers are fast, idempotent, and don't trade.** They run on
the evaluator thread under your runner's lock. Set `pause_new_entries` or
call `request_exit`; never compute indicators or block. Track pause reasons
as a set so one alert's resolution doesn't lift another's pause (see
`immediate_strangle.py`).

**R-A3 (SHOULD) Declare `regime_vix_range` honestly.**

Full API: [STRATEGY-ALERTS.md](STRATEGY-ALERTS.md).

### R-V — Versioning and documentation

**R-V1 (MUST) Bump `version` on any behaviour change** — logic, defaults,
param meaning. Keep `name`. Add a dated changelog line in the docstring.
Runners keep their saved params; a changed *default* does not change a
running strategy, a changed *meaning* does — call it out.

**R-V2 (MUST) The module docstring carries the spec sheet** (section 1),
including *Approximations* and the recommended runner config.

**R-V3 (SHOULD) Promote only proven strategies.** Plugin → forward-tested →
built-in (`src/backtest/strategies/`) with its own test file.

---

## 5. Anti-pattern catalogue

Each of these has shipped somewhere. Don't.

**5.1 Future data in a "past" indicator (R-D1)**
```python
# BAD — centred window and full-sample normalisation both see the future
smooth = candles["close"].rolling(20, center=True).mean()
z = (candles["close"] - candles["close"].mean()) / candles["close"].std()
# GOOD — trailing only
mean = candles["close"].rolling(100).mean()
z = (candles["close"] - mean) / candles["close"].rolling(100).std()
```

**5.2 Positional logic (R-D2)**
```python
# BAD — freezes once the forward buffer is full (len is always 500)
bullish = (len(candles) - 1) // step % 2 == 0
# GOOD — derive from time
minutes = candles.index[-1].hour * 60 + candles.index[-1].minute
bullish = (minutes // (15 * step)) % 2 == 0
```

**5.3 Frame-anchored cumulative measures (R-D2)**
```python
# BAD — "VWAP" since whatever row the frame happens to start at
vwap = (tp * vol).cumsum() / vol.cumsum()
# GOOD — anchored to the session (pattern 6.1)
```

**5.4 State on `self` (R-D4)**
```python
# BAD — backtest (1 call), forward (N calls) and restart (fresh object) differ
def generate_signals(self, candles):
    if candles["close"].iloc[-1] > self._last_high:
        self._last_high = candles["close"].iloc[-1]
# GOOD — recompute from the frame every call (pattern 6.3)
```

**5.5 NEUTRAL for "no opinion" (R-O2)**
```python
# BAD — opens a bull call spread under the default expression
return MarketView(direction=Direction.NEUTRAL, confidence=0.0, ...)
# GOOD
return None
```

**5.6 Trusting the class stop (R-R1)**
```python
# BAD — ignored by the default backtest and by forward runners
stop_loss = 0.02
# GOOD — enforce inside the signal (pattern 6.3) or in expression.exit
```

**5.7 `.loc` in a loop (R-P2)**
```python
# BAD — ~24 ms per call on 500 bars
for i in candles.index:
    if candles["close"].loc[i] < lower.loc[i]: ...
# GOOD — ~0.15 ms
close, low = candles["close"].to_numpy(), lower.to_numpy()
for i in range(len(close)):
    if close[i] < low[i]: ...
```

**5.8 Unguarded maths on degenerate bars (R-D5)**
```python
# BAD — σ = 0 → inf/NaN signal
z = (close - mean) / std
# GOOD
z = ((close - mean) / std.replace(0.0, np.nan)).fillna(0.0)
```

**5.9 Using the incomplete day (R-T4)**
```python
# BAD — "yesterday's close" is actually the current bar on day one
daily = candles["close"].resample("1D").last()
# GOOD — completed sessions only (pattern 6.2)
```

**5.10 Time logic without a clock (R-T2)**
```python
# BAD — AttributeError on a RangeIndex; logged as ERROR every bar
today = candles.index[-1].date()
# GOOD
if not isinstance(candles.index, pd.DatetimeIndex):
    return None  # (or a flat Series for equity)
```

---

## 6. Proven patterns

All snippets are verified against the test template (they pass every check).

**6.1 Session-anchored VWAP (window-invariant for any intraday bar size)**
```python
def session_vwap(candles: pd.DataFrame) -> pd.Series:
    tp = (candles["high"] + candles["low"] + candles["close"]) / 3.0
    vol = candles["volume"].fillna(0.0)
    day = candles.index.normalize()
    num = (tp * vol).groupby(day).cumsum()
    den = vol.groupby(day).cumsum()
    return (num / den.replace(0.0, np.nan)).fillna(candles["close"])
```

**6.2 Daily closes from intraday bars — completed sessions only**
```python
def completed_daily_closes(candles: pd.DataFrame) -> pd.Series:
    closes = candles["close"].groupby(candles.index.normalize()).last()
    return closes.iloc[:-1]  # drop the session still in progress
```

**6.3 Stateful entry with an ATR stop, recomputed from the frame (R-D4, R-R2, R-P2)**
```python
def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
    close = candles["close"]
    prev = close.shift(1)
    tr = pd.concat([candles["high"] - candles["low"],
                    (candles["high"] - prev).abs(),
                    (candles["low"] - prev).abs()], axis=1).max(axis=1)
    atr = tr.rolling(self.atr_period).mean().to_numpy()
    fast = close.ewm(span=self.fast, adjust=False).mean().to_numpy()
    slow = close.ewm(span=self.slow, adjust=False).mean().to_numpy()
    c = close.to_numpy()
    out = np.zeros(len(c), dtype=np.int64)
    held, stop = False, 0.0
    for i in range(len(c)):
        if np.isnan(atr[i]):
            continue                       # warmup → flat (R-D3)
        if held:
            stop = max(stop, c[i] - self.atr_mult * atr[i])   # trailing
            if c[i] < stop or fast[i] < slow[i]:
                held = False               # stop or trend exit
        elif fast[i] > slow[i]:
            held, stop = True, c[i] - self.atr_mult * atr[i]
        out[i] = 1 if held else 0
    return pd.Series(out, index=candles.index)
```
The state resets every time the position closes, so a 500-bar window
reaches the same state as full history (R-D2).

**6.4 Session rules in clock time (R-T3, R-T2)** — gate *new entries* inside
the state machine of 6.3, never exits:
```python
if isinstance(candles.index, pd.DatetimeIndex):
    t = candles.index.hour * 60 + candles.index.minute
    can_enter = np.asarray((t >= 9 * 60 + 30) & (t < 15 * 60))   # 09:30–15:00 IST
else:
    can_enter = np.zeros(len(candles), dtype=bool)   # no clock → no new entries
# … in the 6.3 loop:
#     elif fast[i] > slow[i] and can_enter[i]:
```
Don't do this with a vectorised mask such as
`signals.where(in_window | (signals.shift(1) == 1), 0)`: the shift reads the
*unmasked* state, so an entry blocked at 15:10 still counts as held and
slips in at 15:15, outside the window.

**6.5 Option view with a confidence gate and hysteresis (R-O1..O5)**
```python
def generate_market_view(self, candles):
    if len(candles) < self.ema_period + 1:
        return None                                       # R-D3
    close = candles["close"]
    ema = close.ewm(span=self.ema_period, adjust=False).mean()
    gap = ((close - ema) / ema * 100.0).to_numpy()        # % — scale-free (R-M3)
    state = 0
    for g in gap:                                         # replayed from the frame (R-D4)
        if g > self.enter_pct:
            state = 1
        elif g < -self.enter_pct:
            state = -1
        elif abs(g) < self.exit_pct:
            state = 0                                     # hold band in between
    confidence = min(1.0, abs(gap[-1]) / (2 * self.enter_pct))
    if state == 0 or confidence < self.min_confidence:
        return None                                       # R-O1, R-O3
    return MarketView(
        direction=Direction.BULLISH if state > 0 else Direction.BEARISH,
        confidence=confidence,
        underlying=str(self.underlying).upper(),
        spot_price=float(close.iloc[-1]),
        bar_timestamp=candles.index[-1],
        metadata={"gap_pct": round(float(gap[-1]), 3)},
    )
```

---

## 7. Testing and the validation ladder

### The test template

```bash
mkdir -p tests/strategies
cp templates/strategy_test_template.py tests/strategies/test_my_strategy.py
# edit STRATEGY_NAME (and ALLOWS_NEUTRAL_VIEW / EXPECTS_DECISION_CHANGES if they apply)
cd src && python -m pytest ../tests/strategies/test_my_strategy.py -q
```

| Test | Rule |
|---|---|
| `test_conformance_battery` | R-C1 |
| `test_params_are_bounded_and_documented`, `test_runs_at_param_extremes` | R-M2 |
| `test_no_lookahead_by_truncation` (trend/chop/crash) | R-D1 |
| `test_backtest_equals_forward_window` (trend/chop/crash) | R-D2 |
| `test_instance_is_not_mutated_by_evaluation` | R-D4 |
| `test_short_history_is_safe` | R-D3 |
| `test_degenerate_bars` | R-D5 |
| `test_positional_index_fallback` | R-T2 |
| `test_output_semantics` | R-S1, R-O2, R-O4 |
| `test_decisions_change_somewhere` | R-S3 |
| `test_per_bar_budget` | R-P1 |

Add your own tests for the **logic**: a hand-built frame where you know the
answer ("close crosses above EMA on bar 40 → signal 1 from bar 40").

### The validation ladder — climb in order, don't skip

| Step | Gate to pass |
|---|---|
| 1. Spec sheet | Every line filled; hypothesis defensible |
| 2. Template tests | All green |
| 3. Backtest, in-sample | Plausible; trade count as expected |
| 4. Costs | Net expectancy > 0 at realistic cost; survives `quick_screen` (R-E1) |
| 5. Out-of-sample / walk-forward | Holds up without re-tuning (R-E2) |
| 6. Stability + regimes | Plateau not peak; known losing regime (R-E3, R-E5) |
| 7. Paper forward | Behaviour matches backtest; kill criteria not hit (R-E7) |
| 8. Live, small | One lot / minimum size, alerts subscribed |

---

## 8. Platform facts that bite (verified)

Checked by running the code on 2026-09-27, not just by reading it. Treat
these as constraints until the platform changes; each has a matching rule.

| Fact | Evidence | Rule |
|---|---|---|
| Class `stop_loss`/`take_profit` enforced only by `quick_screen` | always-long probe on a −30% path: quick_screen −2.2%, default engine −29.4%, forward rode to the 25% breaker | R-R1 |
| Default backtest and paper runners are zero-cost | both use `free_executor` / `PAPER_FREE_PROFILE` | R-E1 |
| `-1` is flat outside `quick_screen` | always-short probe: 0 trades in default engine and forward (`allow_short=True`) | R-S2 |
| A `NEUTRAL` view opens a structure | NEUTRAL, conf 0 → `bull_call_spread` opened | R-O2 |
| `confidence` isn't a gate | bridge only logs it | R-O3 |
| Forward buffer is 500 bars | `MAX_BARS_PER_SYMBOL = 500` | R-D2 |
| Option runners skip the 12-bar warmup | `warmup_ok = … or options_bridge is not None` | R-D3 |
| Index may be a `RangeIndex` | `_bars_to_frame` fallback | R-T2 |
| Backtest fills next open, paper fills this close | engine loop vs `PaperBroker` | R-D6 |
| Pool ranking is generic, favours laggards | `_entry_score = (SMA20 − close)/close` | R-S4 |
| `entries/exits` conversion ≈ 25 ms / 500 bars | base-class `.loc` loop | R-P2 |

Existing strategies that the template currently flags (known issues, not
fixed by this document): `time_alternator` (R-D2 — direction freezes in
forward), `ema_reversion_pob` (R-T2 — raises on a RangeIndex),
`vwap_ema_rsi` (R-D2 — frame-anchored VWAP, rare disagreements on 1-min),
`bollinger_reversion`, `donchian_breakout`, `nifty_scalper` (R-P1 — 25–31 ms
per evaluation).

---

## 9. Review checklist

Copy into the PR description.

**Spec & docs**
- [ ] Spec sheet in the docstring, including *Approximations* and recommended runner config (R-V2)
- [ ] `description` states the intended timeframe; `version` bumped with a changelog line (R-V1)

**Contract**
- [ ] Unique `name` (never renamed once deployed); metadata filled (R-C1)
- [ ] Imports: pandas/numpy/stdlib/`backtest.strategy`/`backtest.alerts` only (R-C2)
- [ ] No clock, randomness, I/O, or state on `self` (R-C3, R-D4, R-P3)
- [ ] `eligible_instruments` set for index/option strategies

**Correctness**
- [ ] Template tests green, including lookahead and backtest≡forward (R-D1, R-D2)
- [ ] Longest lookback × 3 ≤ 500 bars at the intended timeframe (R-D2)
- [ ] Flat/None during warmup and on degenerate data; no NaN out (R-D3, R-D5)
- [ ] Time logic uses the index clock and handles a RangeIndex (R-T1..T3)
- [ ] Equity: long-only assumption respected (R-S2) · Options: `None` for no view, no accidental NEUTRAL, own confidence gate, hysteresis (R-O1..O5)

**Risk**
- [ ] Max loss per trade defined and enforced in a place you can point to — not the class `stop_loss` (R-R1, R-R2)
- [ ] Short premium: stop + DTE rule + gamma alert subscription (R-R3, R-A1)

**Parameters**
- [ ] ≤ 5 tunables, bounded, units in tooltips, scale-free where possible (R-M1..M3)
- [ ] Cross-param constraints validated in `__init__` (R-C4); no hidden constants (R-M5)

**Evidence**
- [ ] Net-of-cost expectancy with trade count; survives `quick_screen` (R-E1, R-E4)
- [ ] Out-of-sample result and parameter-stability table (R-E2, R-E3)
- [ ] Regime behaviour stated, including the regime it loses in (R-E5)
- [ ] ≤ 20 ms per evaluation (R-P1)
- [ ] Paper-forward plan with kill criteria (R-E7)
