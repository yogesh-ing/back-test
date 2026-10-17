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
* Stateful loop strategies (e.g. the Pine converter's `strategy.order`
  output) must write the position at **bar end** on every bar — `1`/`-1`
  for the whole time the position is held, `0` when flat — because the
  engine reads the series one bar late. A one-bar entry *pulse* is read as
  "enter and immediately exit"; never emit pulses.

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
| 4 | metadata | missing `description` / `version` / `author`; a malformed `description_sections` card (unknown/empty sections) |
| 5 | params UI labels | schema params without a `label` |
| 6 | output shape | non-Series / misaligned equity output; direction-less views |
| 7 | determinism | two runs, different outputs |

Loader semantics per file: clean-import gate → AST import-ban → battery.
Failures are **logged and skipped**; the loader pops the `__init_subclass__`
auto-registration of failed classes, so a broken file can never leave a
half-valid strategy registered. A missing `plugins/` folder is a no-op.

---

## 6. The built-in catalog & engine guarantees

The catalog in `src/backtest/strategies/` ships more than a dozen strategies
— useful as reading examples and baselines. Equity examples: `buy_and_hold`
(always-long benchmark), `sma_crossover` (fast/slow SMA), `rsi_reversion`
(oversold entry, level exit), `donchian_breakout` (N-bar high/low breakout),
`bollinger_reversion`, `macd_trend`, `momentum_roc`, `ema_pullback`,
`price_move`, `nifty_scalper`. Option examples (override
`generate_market_view`): `banknifty_straddle`, `option_directional`.

Programmatic access goes through the registry (`backtest.strategy.registry`):

```python
from backtest.strategy.registry import list_strategies, get_strategy

names = list_strategies()                 # e.g. ["buy_and_hold", "sma_crossover", …]
cls = get_strategy("sma_crossover")
strategy = cls(fast=20, slow=50)          # params type-coerced from the schema
signals = strategy.generate_signals(candles)
```

Engine guarantees that hold for every strategy (built-in or plugin):

* **No lookahead** — your signal acts on the *next* bar (`target.shift(1)`).
* **Costs** — commission + slippage apply on every position change.
* **Sizing** — 100% of allocated capital per trade (no partial positions).
* **Vectorized engine** — the backtest runs on the full candle series, not a
  Python bar loop.
* ⚠️ Class-level `stop_loss` / `take_profit` attributes are honoured **only**
  by the legacy `quick_screen` path — the default engine and forward runners
  ignore them (see §2). Enforce stops in your signal logic or the option
  `expression.exit`.

---

## 7. Lifecycle & promotion

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

---

# v2 Addendum — Strategy-Owned Execution (Refactor Plan v2.1, Phase 0)

> Status: Phase 0 (foundations). The contract below is **available now**
> and will become **mandatory for option strategies** as each family is
> rewritten (plan §9). Legacy strategies keep working untouched until
> their cutover.

## A. Pick a template (decision tree)

```
What does the strategy trade?
├─ Equity / index shares .................. EquityStrategy
└─ Options
   ├─ "Trade NOW, no signal" .............. InstantLongOptionStrategy
   │    └─ states side explicitly: CE (calls) or PE (puts)
   ├─ Signal says bullish/bearish ......... DirectionalOptionStrategy
   ├─ Two-leg spread (defined risk) ....... SpreadStrategy
   └─ Strangle / condor family ............ StructureStrategy
        (name is neutral on purpose: a short strangle is undefined
         risk — a template name must never claim a risk property)
```

Templates live in `src/backtest/strategy/templates/`. Resolution of
direction → structure rule happens **in the template** (`direction_rule_for`),
strategy-side — never in the bridge (plan §4.1).

## B. The non-negotiables (checklist — conformance will enforce these)

- [ ] **Explicit intent.** The strategy names the actual trade (structure,
      strike rule, expiry rule) at trigger time. No "direction + hope".
- [ ] **NEUTRAL = silence.** NEUTRAL never opens a trade. There is no
      NEUTRAL structure rule; declaring one is a spec error. Want an
      instant trade? Use `InstantLongOptionStrategy` and state CE/PE.
- [ ] **Mandatory hard stop.** Every spec's `exit` carries exactly one:
      `stop_loss_pct`, `stop_loss_points`, `stop_loss_price`, or
      `structure_capped: true` (long option / defined-risk spread).
      Target (`take_profit_*`) is optional. Indicator exits go in
      `strategy_exits` — allowed, but never the *only* protection on an
      undefined-risk structure (naked shorts, short strangles).
- [ ] **`emits` ⊇ everything the code can produce.** A runtime view in an
      undeclared direction is a coded contract breach, not a fallback.
- [ ] **Sizing is a menu, not a risk limit.** `default_lots/min_lots/max_lots`
      bound what deployment may *select*. Real protection is ₹-based at
      the Risk Gate (margin, worst-case loss vs `config/risk_limits.yaml`).
- [ ] **No data/broker calls from strategy code** (existing C2 rule):
      bars and chains flow engine → strategy, never the reverse.
- [ ] **Unique `name`** — collisions are registration errors, never
      silent precedence.

## C. Declaring the spec (worked skeleton)

```python
from backtest.strategy.templates import DirectionalOptionStrategy

class MyDirectional(DirectionalOptionStrategy):
    name = "my_directional"
    description = "..."
    # Strategy card (required): structured long-form description shown on
    # the backtest page above the performance metrics. All six sections,
    # plain language, honest about worst case. Enforced by
    # tests/test_strategy_descriptions.py.
    description_sections = {
        "overview": "One or two sentences: what it does.",
        "regime": "Where it works, and where it struggles.",
        "entry": "The exact trigger; what silence means.",
        "construction": "Structure/strikes/expiry (options) or position shape (equity).",
        "exits_risk": "Stop, target, exit rules — and the true worst case.",
        "parameters": "Key parameters with defaults and what they trade off.",
    }
    version = "1.0"
    author = "..."
    eligible_instruments = ["NIFTY", "BANKNIFTY"]

    execution = {
        "emits": ["bullish", "bearish"],
        "structures": {
            "bullish": {"structure": "long_call", "strike_selection": "atm"},
            "bearish": {"structure": "long_put",  "strike_selection": "atm"},
        },
        "exit": {"stop_loss_pct": 0.30, "take_profit_pct": 1.0,
                 "signal_flip": True, "min_days_to_expiry": 1},
        "quantity": 1, "min_quantity": 1, "max_quantity": 5,
        "allowed_overrides": ["quantity", "exit"],
    }
    # generate_market_view() emits BULLISH/BEARISH (NEUTRAL = no trade).
```

Instant-buy shape (the `atm_instant_buy` replacement pattern, plan §9.4):

```python
class CeInstantBuy(InstantLongOptionStrategy):
    name = "CE_instant_buy"
    side = "CE"                      # stated, never smuggled via NEUTRAL
    execution = {
        "emits": ["bullish"],        # the one direction that buys calls
        "structures": {"bullish": {"structure": "long_call",
                                   "strike_selection": "atm"}},
        "exit": {"structure_capped": True, "signal_flip": False,
                 "neutral_bars": 0, "min_days_to_expiry": None},
        "quantity": 1, "max_quantity": 5,
    }
```

## D. Selector / expiry wiring (plan §6.2)

| Spec word | Implementation | Required companion |
|---|---|---|
| `strike_selection: atm` | `ATMSelector` | — |
| `strike_selection: delta` | `DeltaSelector` | `delta_target` ∈ (0, 1] |
| `strike_selection: fixed_distance` | `FixedDistanceSelector` | `distance_pct` > 0 |
| `strike_selection: target_price` | `TargetPriceSelector` | view `target_price` |
| `expiry_policy: nearest` | `NearestExpiryPolicy` | — |
| `expiry_policy: weekly` | `WeeklyExpiryPolicy` | — |
| `expiry_policy: fixed_days` | `FixedDaysExpiryPolicy` | days (Phase 1 wiring) |
| `expiry_policy: minimum_days` | `MinimumDaysExpiryPolicy` | `expiry_min_days` |

Three expiry concepts, never conflated: `expiry_policy` + `expiry_min_days`
pick the contract at **entry**; `exit.min_days_to_expiry` exits an **open**
trade near expiry (explicit `null` = ride to settlement).

## E. Who owns what

| Question | Owner |
|---|---|
| What to trade, when, with what stop | **Strategy** (this spec) |
| May this trade happen, at this size, now | **Risk Gate** over `config/risk_limits.yaml` (Phase 1) |
| What actually ran, under which code | **Spawn snapshot** — `bundle_sha256` over the strategy + its template chain; a mismatch at restart pauses the runner (`STRATEGY_CODE_CHANGED`, plan §8) |
| Intraday intervention | Operator: pause / close / tighten stop only — never loosen or enlarge |
