# Strategy Catalogue — Built-ins + Plugins

Analysis snapshot (2026-11-21). Every registered strategy, its signal kind, behaviour,
known defects, and where it lives. Source of truth for each entry is the strategy file
itself; `__all__` in `src/backtest/strategies/__init__.py` is **stale** (missing 5 names)
and does not affect discovery — discovery uses `pkgutil.iter_modules` for built-ins and
`discover_plugins()` for plugins.

---

## Built-in Strategies (`src/backtest/strategies/`)

Visible via `list_strategies()` / `get_all()` without booting plugins. All appear on the
backtest page catalogue and at Portfolio → Add Instance.

| File | Class | Name | Signal Kind | Instruments | Version | Known Issues |
|------|-------|------|-------------|-------------|---------|--------------|
| `buy_and_hold.py` | BuyAndHold | `buy_and_hold` | equity | any | 1.0 | None (benchmark — deliberately simple) |
| `sma_crossover.py` | SmaCrossover | `sma_crossover` | equity | any | 1.1 | None |
| `rsi_reversion.py` | RsiReversion | `rsi_reversion` | equity | any | 1.0 | None |
| `donchian_breakout.py` | DonchianBreakout | `donchian_breakout` | equity | any | 1.0 | R-P1 perf xfail (25–31 ms/eval, over ~20 ms budget) |
| `price_move.py` | PriceMove | `price_move` | equity | any | 1.0 | R-E1 cost-collapse xfail (net −10.5% at mstock costs) |
| `option_directional.py` | DirectionalOptions | `directional_options` | option | NIFTY, BANKNIFTY | 1.0 | None |
| `momentum_roc.py` | MomentumROC | `momentum_roc` | equity | any | 1.0 | None |
| `macd_trend.py` | MacdTrend | `macd_trend` | equity | any | 1.0 | None |
| `ema_pullback.py` | EmaPullback | `ema_pullback` | equity | any | 1.1 | R-P1 perf xfail (25–31 ms/eval) |
| `bollinger_reversion.py` | BollingerReversion | `bollinger_reversion` | equity | any | 1.0 | R-P1 perf xfail (25–31 ms/eval) |
| `banknifty_ema_momentum.py` | BankNiftyEmaMomentum | `banknifty_ema_momentum` | option | BANKNIFTY, NIFTY | 1.0 | None |

### Analysis — Built-ins

**No unused strategies.** Every one of the 11 is referenced by at least one test, script, config,
or the canonical `test_backtest.py` expected set.

**`__init__.py` `__all__` is stale.** It lists only 6 (`buy_and_hold`, `sma_crossover`, `rsi_reversion`,
`donchian_breakout`, `price_move`, `option_directional`). The missing 5 (`momentum_roc`, `macd_trend`,
`ema_pullback`, `bollinger_reversion`, `banknifty_ema_momentum`) still register via `pkgutil` discovery,
but `from backtest.strategies import *` would silently omit them. Fix: add the 5 missing names.

**Closest-to-redundant pair:** `option_directional` and `banknifty_ema_momentum`. Both are option-native
directional EMA strategies emitting bullish/bearish `MarketView` with near-identical exit blocks
(stop −50%, target +100%, signal flip, min 1 DTE). Differ mainly in EMA count (single vs fast/slow)
and default underlying (NIFTY vs BANKNIFTY). Consider unifying into one parameterized option strategy.

**Conceptual overlap (complementary, not redundant):**
- Trend family: `sma_crossover`, `macd_trend`, `donchian_breakout`, `momentum_roc`, `ema_pullback` —
  different speeds / entry timing, good for portfolio stress-testing.
- Mean-reversion family: `rsi_reversion`, `bollinger_reversion` — different indicators, long when trend
  strategies are flat.

**Known CI defects (tracked as xfails in `tests/strategies/test_registry_template.py`):**
- `bollinger_reversion`, `donchian_breakout`, `ema_pullback` — R-P1 per-bar budget violations. Vectorize
  the hold-state loops.
- `price_move` — R-E1 cost collapse; needs turnover dampener or intraday-segment restriction.

---

## Plugin Strategies (`plugins/strategies/`)

Registered only after `discover_plugins()` runs — which happens inside `create_app()` (Flask web app boot,
`src/backtest/web/app.py:556`). Invisible to `list_strategies()` / `get_all()` / CLI `backtest list` unless
plugins are booted first. Sandboxed: forbidden imports (`backtest.brokers`, `backtest.forward`, `backtest.options`,
`backtest.data`, `backtest.live`, `requests`, `urllib*`, `websocket*`); each must pass a conformance battery
(determinism, signal alignment, option-view honesty) or it is skipped without crashing boot.

| File | Class | Name | Signal Kind | Instruments | Version | Known Issues |
|------|-------|------|-------------|-------------|---------|--------------|
| `ce_instant_buy.py` | CeInstantBuy | `CE_instant_buy` | option | NIFTY, BANKNIFTY | 2.0 | Mechanics validator, not an edge; unconditional BULLISH ATM long call |
| `pe_instant_buy.py` | PeInstantBuy | `PE_instant_buy` | option | NIFTY, BANKNIFTY | 2.0 | Mechanics validator, not an edge; unconditional BEARISH ATM long put |
| `immediate_strangle.py` | ImmediateStrangle | `immediate_strangle` | option (neutral) | NIFTY, BANKNIFTY | 1.1 | Short strangle — uncapped loss; paper-only entitlement; fee-aware bracket (BUG-02 fix) |
| `strangle_wing_points.py` | StrangleWingPoints | `strangle_wing_points` | option (neutral) | NIFTY, BANKNIFTY | 1.0 | Short strangle — uncapped loss; paper-only; wing in points vs live spot |
| `straddle_short_atm.py` | ShortStraddleAtm | `straddle_short_atm` | option (neutral) | NIFTY, BANKNIFTY | 1.0 | Short straddle — uncapped loss; paper-only; positional carry |
| `vwap_ema_rsi.py` | VwapEmaRsi | `vwap_ema_rsi` | option | NIFTY, BANKNIFTY | 1.0 | None |
| `volume_oi_surge.py` | VolumeOISurge | `volume_oi_surge` | option | NIFTY, BANKNIFTY | 1.0 | Declared `strike_selection: volume` not implemented; trades ATM in practice |
| `time_alternator.py` | TimeAlternator | `time_alternator` | option | NIFTY, BANKNIFTY | 1.0 | Deterministic coin-flip mechanics validator; no edge by design |
| `momentum_burst.py` | MomentumBurst | `momentum_burst` | option | NIFTY, BANKNIFTY | 1.0 | None |
| `delta_momentum.py` | DeltaMomentum | `delta_momentum` | option | NIFTY, BANKNIFTY | 1.0 | None |
| `ema_reversion_pob.py` | EmaReversionPob | `ema_reversion_pob` | option | NIFTY, BANKNIFTY | 1.0 | None |
| `one_min_scalp.py` | OneMinScalp | `one_min_scalp` | equity | RELIANCE (default) | 1.0 | 1-min scalper for paper-trading validation; TP/SL/fixed-hold |
| `hf_equity_scalp.py` | HF_EquityScalp | `hf_equity_scalp` | equity | any | 1.0 | HF scalper for paper-trading validation; high turnover |

### Analysis — Plugins

**Two categories:**

1. **Mechanics / validation instruments** (not return strategies — trade to test plumbing):
   - `CE_instant_buy`, `PE_instant_buy` — instant ATM long call/put on first bar, hold to day close. Replaced
     the old `atm_instant_buy` (which emitted unconditional NEUTRAL — "intent is a lie by construction").
   - `immediate_strangle`, `strangle_wing_points`, `straddle_short_atm` — short premium-selling structures,
     uncapped loss, paper-only entitlement.
   - `time_alternator` — deterministic bar-parity flip every N bars; high-turnover chain validator.
   - `one_min_scalp`, `hf_equity_scalp` — equity scalpers generating frequent trades for fill/charge/persistence
     test validation (Bug #1–#4).

2. **Return-seeking option strategies**:
   - `vwap_ema_rsi` — VWAP bias + EMA9/15 cross + RSI 45–60 filter; intraday trend.
   - `volume_oi_surge` — volume-burst gate; declared volume strike selection not implemented (trades ATM).
   - `momentum_burst` — last-bar ±threshold% move → directional view.
   - `delta_momentum` — EMA slope-gated trend; runner can spawn with delta-targeted strikes.
   - `ema_reversion_pob` — daily-5-EMA reversion bias + first-30-min-candle Fib band; aggressive OTM buying.

**Important behaviour note:** Short structures (`immediate_strangle`, `strangle_wing_points`, `straddle_short_atm`)
emit `Direction.NEUTRAL` with full confidence — NEVER to open a directional trade, but because their
`opening_structure` explicitly names the structure, the bridge opens the short legs. This is the Phase 2
refactor contract: NEUTRAL means "no directional view," not "no trade," when `opening_structure` is declared.

---

## Where to Add a New Strategy

**Built-in** → `src/backtest/strategies/<name>.py`
- Use for core strategies you want visible everywhere (backtest page catalogue, CLI, tests, API without
  plugin boot).
- No import sandbox; full access to the codebase.
- Visible via `list_strategies()` immediately after the file exists; restart the app server once to pick it up.

**Plugin** → `plugins/strategies/<name>.py`
- Use for experimental, imported, or mechanics-validator strategies.
- Sandboxed imports + mandatory conformance battery; broken files are skipped, never crash boot.
- Visible only after `discover_plugins()` runs (Flask app boot); restart required once per new file.

**Contract either way** (enforced by `Strategy.validate` / conformance suite):
- Unique lowercase `name`, no spaces
- Rich `description` + all `description_sections` (`overview`, `regime`, `entry`, `construction`, `exits_risk`, `parameters`)
- Typed `params` with `min`/`max`/`tooltip` for every numeric param
- Equity: `entries()`/`exits()` (or legacy `generate_signals()`); Option: `generate_market_view()` → `MarketView \| None` (NEUTRAL never opens a directional trade unless `opening_structure` is declared)
- Deterministic: same candles → same output
- Option strategies should declare `eligible_instruments`

**Template:** `cp templates/strategy_template.py <target>/<your_strategy>.py`

Run the conformance battery after writing:
```
python -m pytest tests/strategies/test_registry_template.py -q
```

---

## Recommended Cleanup / Upgrade Order

1. Fix `__init__.py` `__all__` — add the 5 missing built-in names.
2. Vectorize `ema_pullback`, `bollinger_reversion`, `donchian_breakout` to clear R-P1 perf xfails.
3. Add turnover dampener to `price_move` (or document intraday-only) to clear R-E1 cost-collapse xfail.
4. Evaluate unifying `option_directional` + `banknifty_ema_momentum` into one parameterized option strategy (deduplicate shared `execution` block).
5. Keep `buy_and_hold` as-is (benchmark, intentionally simple).
