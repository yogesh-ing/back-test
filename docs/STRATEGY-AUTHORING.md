# Strategy Authoring — the one-minute drop-in flow

> Audience: anyone adding a trading strategy to this system. You write **one
> Python file**; the engine, data, costs, exits, UI and portfolio plumbing are
> fixed infrastructure you never touch.
>
> **Rules & quality bar:** [STRATEGY-GUIDELINES.md](STRATEGY-GUIDELINES.md) — the
> rulebook (lookahead, backtest≡forward, risk ownership, costs, parameters) and
> `templates/strategy_test_template.py`, the tests every new strategy should pass.
>
> Related: `docs/ARCHITECTURE-UNIFIED-TRADING.md` (§5 data ownership),
> `PROJECT-CONTEXT.md` (invariants), `src/backtest/plugins/__init__.py` (the loader).

---

## 1. The one-minute flow

```
1. cp templates/equity_strategy_template.py  plugins/strategies/my_strategy.py
   (or templates/option_strategy_template.py for an option strategy;
    create the plugins/strategies/ folder if it does not exist)
2. Rename the class; change `name` (must be UNIQUE across all strategies).
3. Edit the logic — the hook body is the whole strategy.
4. Restart the app. The loader vets the file (imports → conformance) and
   registers it; the spawn form and /api/strategies pick it up automatically.
5. If the file is skipped, the reason is in the log (logger `backtest.plugins`).
   Fix and restart. The app never crashes on a bad plugin.
```

Drop-ins live in `plugins/strategies/*.py` at the **repo root**. Discovery
runs at `create_app` and re-imports a file only when its mtime changes.

---

## 2. The hooks the engine calls

Implement **one** signal hook (or the market view for options):

| Hook | Returns | Use when |
|---|---|---|
| `generate_signals(candles)` | `Series[int]` aligned to candles: `1` long, `0` flat, `-1` short | You think in target positions. The canonical equity hook. |
| `entries(candles)` (+ optional `exits(candles)`) | `Series[bool]` | You think in entry/exit rules; the base class builds positions for you. ⚠️ Class-level `stop_loss`/`take_profit` are enforced **only** by the legacy `quick_screen` backtest — the default engine and forward runners ignore them (verified; see STRATEGY-GUIDELINES R-R1). Enforce stops in your signal logic or in the option `expression.exit`. |
| `generate_market_view(candles)` | `MarketView` or `None` | **Option strategies.** You emit a direction + confidence; the OptionsBridge selects strikes and trades the playbook's structure. |

Notes:

* Implement `generate_signals` **or** `entries` — never both. A class with
  neither fails `Strategy.validate()` ("must implement generate_signals or entries").
* Overriding `generate_market_view` is what *makes* your strategy an option
  strategy: `signal_kind` is **derived** from the override (never declared),
  and the spawn form then locks to Single Symbol + index underlyings.
* Option templates keep the trivial `entries()` fallback so the class remains
  valid through the equity path too (see the option template).
* `candles` is the canonical OHLCV frame — lowercase `open/high/low/close/
  volume`, tz-naive ascending `DatetimeIndex` — a **trailing window ending at
  the current bar**. The engine acts on your signal at the **next** bar
  (no-lookahead invariant #1: `target.shift(1)`), so backward-looking logic
  only. There is nothing forward to peek at, by construction.

---

## 3. Parameters (they become the spawn form)

Declare on `params` in **schema form** — every entry becomes a form field in
the UI, `label`/`tooltip` are what the user sees:

```python
params = {
    "period": {
        "default": 20, "min": 2, "max": 200, "type": "int",
        "label": "EMA Period", "tooltip": "Lookback over the close series.",
    },
}
```

* Allowed types: `int | float | bool | str`. Defaults are bound as instance
  attributes (`self.period`) and constructor overrides are type-coerced.
* Flat form (`params = {"period": 14}`) still works, but gives the UI no
  labels/constraints — prefer schema form.
* The conformance battery refuses params without a usable label.

---

## 4. The hard rules (enforced at load — violations are refused)

### C2 — the engine hands you data (import ban)

A strategy receives **all** market data through `candles` and returns a
signal/view. It must never fetch, price or order anything itself. The loader
AST-parses every plugin file and **refuses** imports of:

```
backtest.brokers   backtest.forward   backtest.options
backtest.data      backtest.live
requests           urllib             urllib3
websocket          websockets
```

(including submodules — `from backtest.brokers.base import ...` is caught).
This is what keeps a backtest, a paper test and a live run the *same
function*: the only variable across environments is the data the engine
supplies.

### Determinism

Same candles twice → **identical output**, value-compared (not repr). No
`datetime.now()`, no `random`, no module-level mutable state, no reads of
anything outside the frame you were given. The battery runs your strategy
twice and refuses differences. A strategy that cannot be replayed cannot be
backtested honestly.

### Output contract

* Equity: a `pd.Series` aligned to `candles` (same index, same length), values
  in `{-1, 0, 1}` for `generate_signals`, booleans for entries/exits.
* Option: a `MarketView` (direction + confidence + underlying + spot) or
  `None` — `None` means "no conviction, no trade this bar". Views without a
  direction are refused.

---

## 5. The conformance battery (what the loader checks)

`backtest.plugins.conformance_errors(cls)` — the same battery guards the
loader (skip-with-warning) and `tests/test_strategy_conformance.py`
(assert-empty), so there is exactly one contract:

| # | Check | Refuses |
|---|---|---|
| 1 | unique non-empty `name` | empty/blank names; a name colliding with a registered strategy (the file is skipped, the built-in untouched) |
| 2 | `Strategy.validate()` | no signal hook; malformed params schema |
| 3 | signal-kind consistency | kind derived from the `generate_market_view` override — a class cannot claim "option" without implementing it |
| 4 | metadata | missing `description` / `version` / `author` |
| 5 | params UI labels | schema params without a `label` |
| 6 | output shape | non-Series / misaligned equity output; direction-less views |
| 7 | determinism | two runs, different outputs |

Loader semantics per file: clean-import gate → AST import-ban → battery.
Failures are **logged and skipped**; the loader pops the `__init_subclass__`
auto-registration of failed classes, so a broken file can never leave a
half-valid strategy registered. A missing `plugins/` folder is a no-op.

---

## 6. Lifecycle & promotion

* **Plugin** (drop-in): lives in `plugins/strategies/`, loaded at app start,
  reloaded on file change (mtime), registered into the normal strategy
  registry. Perfect for experimentation and personal desks.
* **Built-in**: lives in `src/backtest/strategies/`, ships with the repo,
  covered by the full engine test matrix.
* **Promotion**: when a plugin has proven itself (forward-tested, deterministic
  by construction, param surface stable), move it into
  `src/backtest/strategies/`, add unit tests, and delete the plugin file —
  same `name` keeps runners spawning identically. Bump `version` on any
  behavioural change; the change should be visible in `GET /api/strategies`.

### Review checklist (before you merge a strategy)

- [ ] Unique `name`; honest `description`, `version`, `author`
- [ ] Schema-form params with labels/tooltips a human can act on
- [ ] No banned imports (the loader would refuse, but don't make it)
- [ ] Deterministic: no clock/random/module state
- [ ] Backward-looking only; comfortable with next-bar execution
- [ ] Passes `tests/test_strategy_conformance.py`
- [ ] Passes a copy of `templates/strategy_test_template.py` and the full
      checklist in [STRATEGY-GUIDELINES.md §9](STRATEGY-GUIDELINES.md#9-review-checklist)
- [ ] If option-kind: `MarketView.confidence` semantics documented
      (confidence is the bridge's trade/no-trade dial)
