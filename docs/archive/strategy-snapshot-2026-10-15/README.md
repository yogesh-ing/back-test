# Strategy Snapshot — 2026-10-15 (Pre-Rewrite Freeze)

**Status:** Frozen reference copy. **NOT live code** — strategy discovery scans only `plugins/strategies/` and `src/backtest/strategies/`, so nothing here is imported, registered, or traded.

**Purpose:** Preserve the exact behaviour of every strategy that existed before the Strategy-Owned Execution rewrite (see `docs/STRATEGY_EXECUTION_REFACTOR_PLAN.md` v2), so that (a) rewritten strategies can be diffed against the original signal logic, (b) old runner snapshots referencing these names/versions remain explainable, and (c) originals can be safely deleted from the live directories once each replacement passes the cutover gate — nothing is lost by deleting them.

**Contents:** `plugins/` = copy of `plugins/strategies/*.py` (24 files incl. `__init__`-less plugin modules); `builtin/` = copy of `src/backtest/strategies/*.py` (14 files incl. `__init__.py`). `manifest.json` records SHA-256, byte size, declared strategy `name`, and class for every file (38 files).

**Cutover rule:** A live original is deleted only when its rewritten replacement (or a documented `RETIRED` decision) passes the cutover gate in the refactor plan §9. Until then originals stay live and untouched.

## Inventory (declared name may collide — see notes)

| Origin | File | Strategy name | Class |
|---|---|---|---|
| plugins | `atm_instant_buy.py` | `atm_instant_buy` | `AtmInstantBuy` |
| plugins | `delta_momentum.py` | `delta_momentum` | `DeltaMomentum` |
| plugins | `ema_reversion_pob.py` | `ema_reversion_pob` | `EmaReversionPob` |
| plugins | `hf_equity_scalp.py` | `hf_equity_scalp` | `HF_EquityScalp` |
| plugins | `immediate_entry.py` | `immediate_entry` | `ImmediateEntry` |
| plugins | `immediate_strangle.py` | `immediate_strangle` | `ImmediateStrangle` |
| plugins | `momentum_burst.py` | `momentum_burst` | `MomentumBurst` |
| plugins | `my_supertrend_v1_imported.py` | `mysupertrendv1` | `MySupertrendV1` |
| plugins | `nifty_instant_buy.py` | `nifty_instant_buy` | `Nifty_InstantBuy` |
| plugins | `one_min_scalp.py` | `one_min_scalp` | `OneMinScalp` |
| plugins | `outside_bar_strategy_imported.py` | `outsidebarstrategy` | `OutsideBarStrategy` |
| plugins | `outsidebarstrategy_imported.py` | `outsidebarstrategy` | `OutsideBarStrategy` |
| plugins | `supertrend_strategy_imported.py` | `supertrendstrategy` | `SupertrendStrategy` |
| plugins | `supertrendstrategy_imported.py` | `supertrendstrategy` | `SupertrendStrategy` |
| plugins | `swing_donchian_breakout.py` | `swing_donchian_breakout` | `SwingDonchianBreakout` |
| plugins | `swing_ema_pullback.py` | `swing_ema_pullback` | `SwingEmaPullback` |
| plugins | `swing_hourly_supertrend.py` | `swing_hourly_supertrend` | `SwingHourlySupertrend` |
| plugins | `swing_rsi2_meanrev.py` | `swing_rsi2_meanrev` | `SwingRsi2MeanRev` |
| plugins | `time_alternator.py` | `time_alternator` | `TimeAlternator` |
| plugins | `v1_imported.py` | `v1` | `V1` |
| plugins | `v2_imported.py` | `v2` | `V2` |
| plugins | `v3_imported.py` | `v3` | `V3` |
| plugins | `volume_oi_surge.py` | `volume_oi_surge` | `VolumeOISurge` |
| plugins | `vwap_ema_rsi.py` | `vwap_ema_rsi` | `VwapEmaRsi` |
| builtin | `__init__.py` | `None` | `None` |
| builtin | `atm_instant_buy.py` | `atm_instant_buy` | `AtmInstantBuy` |
| builtin | `banknifty_straddle.py` | `banknifty_straddle` | `BankNiftyStraddle` |
| builtin | `bollinger_reversion.py` | `bollinger_reversion` | `BollingerReversion` |
| builtin | `buy_and_hold.py` | `buy_and_hold` | `BuyAndHold` |
| builtin | `donchian_breakout.py` | `donchian_breakout` | `DonchianBreakout` |
| builtin | `ema_pullback.py` | `ema_pullback` | `EmaPullback` |
| builtin | `macd_trend.py` | `macd_trend` | `MacdTrend` |
| builtin | `momentum_roc.py` | `momentum_roc` | `MomentumROC` |
| builtin | `nifty_scalper.py` | `nifty_scalper` | `NiftyScalper` |
| builtin | `option_directional.py` | `directional_options` | `DirectionalOptions` |
| builtin | `price_move.py` | `price_move` | `PriceMove` |
| builtin | `rsi_reversion.py` | `rsi_reversion` | `RsiReversion` |
| builtin | `sma_crossover.py` | `sma_crossover` | `SmaCrossover` |

## Known collisions / duplicates found at freeze time

- `atm_instant_buy` / `AtmInstantBuy` exists in BOTH `plugins/` and `builtin/` (different implementations — plugin is the NEUTRAL-view option test instrument; builtin is an equity signal strategy). Registry duplicate-name handling decides the winner today; the rewrite must resolve this by renaming/retiring one, never by silent precedence.
- `outsidebarstrategy` is declared by two plugin files (`outside_bar_strategy_imported.py`, `outsidebarstrategy_imported.py`); `supertrendstrategy` likewise (`supertrend_strategy_imported.py`, `supertrendstrategy_imported.py`). One of each pair is dead/shadowed — confirm which, keep it here, delete the shadow in the rewrite.
- Imported strategies (`v1/v2/v3`, `mysupertrendv1`, etc.) have opaque names and no clear owner intent — prime `RETIRED`-or-rename candidates in the rewrite triage (§9.2).

## Freeze checklist (blocking pre-Phase-0 gate — plan §9.1)

- [ ] (a) Snapshot directory committed as a dedicated commit recording the manifest hash
- [ ] (b) `manifest.json` SHA-256s re-verified clean from that commit
- [ ] (c) Tarball of this directory + manifest exported outside both repository checkouts — Location: _(fill in)_ · Tarball SHA-256: _(fill in)_

Until all three are checked, this snapshot is a working-tree copy, **not** a freeze.
