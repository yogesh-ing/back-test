# Strategy-Owned Execution Configuration — Architectural Refactor

**Status**: Draft for architectural review  
**Date**: 2026-10-15  
**Author**: Quant Engineering Team  
**Reviewer**: [Architect Name]

---

## Executive Summary

This document proposes refactoring how option strategies define their execution behavior to establish **separation of concerns** and **single source of truth**. Currently, execution configuration (direction → structure type mapping, stop-loss rules, quantity sizing) is fragmented across three layers: strategy code, backend bridge logic, and UI JavaScript. This causes silent misconfigurations like the NIFTY/BANKNIFTY incident where identical strategy classes produced different trading behavior.

**Proposed Solution**: Move all execution behavior into strategy classes as declarative metadata. The strategy author expresses intent once; the engine validates and executes; the UI displays and allows bounded overrides.

---

## Problem Statement

### Current Architecture Failure

The execution responsibility is scattered across three disconnected layers:

| Layer | File:Line | Current Role | Problem |
|-------|-----------|--------------|---------|
| **Strategy** | `plugins/strategies/atm_instant_buy.py` | Outputs `MarketView(direction=NEUTRAL)` | Strategy knows it wants to trade but doesn't say *how* |
| **UI JavaScript** | `src/backtest/web/static/js/portfolio.js:1424` | Hardcodes `{BULLISH: bull_call_spread, BEARISH: bear_put_spread}` | UI invents mapping strategy never declared; missing NEUTRAL key |
| **Backend Bridge** | `src/backtest/forward/options_bridge.py:1830` | Falls back NEUTRAL → BULLISH silently | Bridge guesses intent instead of enforcing contract |
| **Playbooks** | `src/backtest/playbooks/models.py:171` | Override strategy behavior | Strategy-agnostic layer above strategy (inverted ownership) |

### Concrete Incident

**NIFTY vs BANKNIFTY deployment divergence (2026-10-15)**:

- Both instances used `atm_instant_buy` strategy class
- Strategy returns `Direction.NEUTRAL` always (unconditional entry design)
- NIFTY deployment manually added `"NEUTRAL": "long_call"` to expression config → executed trades
- BANKNIFTY deployment used UI default (no NEUTRAL key) → bridge fell back to BULLISH mapping → attempted bull_call_spread → failed silently
- Result: Same strategy, same market session, opposite outcomes due to who hand-edited which browser form

**Root cause**: The execution contract between strategy and engine is implicit, spread across UI code and fallback logic, not owned by the strategy itself.

---

## Design Principles

1. **Strategy Owns Execution Rules**: The class that generates signals declares what to do with those signals
2. **Explicit Over Implicit**: Every silent default becomes either explicit declaration or loud refusal
3. **Fail Closed**: Invalid/missing config stops deployment, never guesses at runtime
4. **Single Source of Truth**: One parser, one validator, one serializer for execution config
5. **Reversible Migration**: Existing runners continue working; new behavior opt-in via versioned schema

---

## Proposed Architecture

### 1. Strategy Interface (New)

Strategies declare execution behavior via a new class attribute `execution`, normalized by `ExecutionSpec`:

```python
# src/backtest/strategy/execution.py (NEW MODULE)

from dataclasses import dataclass
from typing import Mapping

@dataclass(frozen=True)
class DirectionRule:
    """What to open when the strategy emits this direction."""
    structure: str                       # "long_call" | "bull_call_spread" | "strangle" | ...
    strike_selection: str = "atm"        # atm | delta | fixed_distance | target_price
    delta_target: float | None = None    # required iff strike_selection == "delta"
    distance_pct: float | None = None    # required iff strike_selection == "fixed_distance"
    expiry_policy: str = "nearest"       # nearest | weekly | fixed_days | minimum_days
    expiry_min_days: int = 0             # entry-side floor

@dataclass(frozen=True)
class ExecutionSpec:
    # Directions this strategy's MarketView can carry
    emits: frozenset[str]                # e.g. {"neutral"}, {"bullish","bearish"}
    
    # direction -> DirectionRule
    structures: Mapping[str, DirectionRule]
    
    # Exit rules — reuses existing ExitConfig vocabulary:
    # signal_flip, neutral_bars, stop_loss_pct, take_profit_pct,
    # stop_loss_points, take_profit_points, max_loss_inr, max_bars,
    # min_days_to_expiry, reenter, max_reentries_per_day
    exit: ExitConfig
    
    # Sizing
    quantity: int = 1                    # default lots per leg
    max_quantity: int = 10               # hard ceiling UI/API may not exceed
    
    def to_expression(self) -> dict:
        """Emits today's expression JSON shape (extended additively)."""
        
    @classmethod
    def from_declared(cls, raw: "ExecutionSpec | dict") -> "ExecutionSpec":
        """Normalize dict or dataclass input."""
```

### 2. Strategy Base Class Changes

```python
# src/backtest/strategy/base.py

class Strategy(ABC):
    # NEW: None = legacy strategy (equity, or option strategy accepting engine default)
    execution: ClassVar["ExecutionSpec | dict | None"] = None
    
    @classmethod
    def execution_spec(cls) -> "ExecutionSpec | None":
        """Normalized spec; parses dict form, validates, caches on the class."""
        if cls.execution is None:
            return None
        return ExecutionSpec.from_declared(cls.execution)
    
    @classmethod
    def default_expression(cls) -> "dict | None":
        """instrument.expression block derived from the spec."""
        spec = cls.execution_spec()
        return spec.to_expression() if spec else None
    
    @classmethod
    def validate(cls) -> None:
        # ...existing checks...
        if cls.execution is not None:
            spec = cls.execution_spec()
            # Validates:
            # - structure names in bridge STRUCTURES registry
            # - every direction in emits has rule in structures
            # - exit block parses via ExitConfig
            # - 1 <= quantity <= max_quantity
```

### 3. Example: Migrated Strategy

**Before** (current state, broken by default):

```python
class AtmInstantBuy(Strategy):
    name = "atm_instant_buy"
    eligible_instruments = ["NIFTY", "BANKNIFTY"]
    # No execution config — hopes UI or backend guesses correctly
    
    def generate_market_view(self, candles):
        return MarketView(direction=Direction.NEUTRAL, confidence=1.0, ...)
```

**After** (self-contained):

```python
class AtmInstantBuy(Strategy):
    name = "atm_instant_buy"
    eligible_instruments = ["NIFTY", "BANKNIFTY"]
    
    execution = {
        "emits": ["neutral"],
        "structures": {
            "neutral": {
                "structure": "long_call",
                "strike_selection": "atm",
            },
        },
        "exit": {
            "signal_flip": False,        # NEUTRAL flip must not close day-watch
            "neutral_bars": 0,
            "min_days_to_expiry": None,  # ride to settlement
        },
        "quantity": 1,
        "max_quantity": 5,
    }
    
    def generate_market_view(self, candles):
        return MarketView(direction=Direction.NEUTRAL, confidence=1.0, ...)
```

The author's intent — "when I'm NEUTRAL, buy ATM calls, hold to settlement" — appears **exactly once**, in code, not in comments hoping someone configures UI correctly.

### 4. Expression JSON Schema (Extended)

String values in `type` keep parsing as before (`"long_call"` ≡ `{"structure": "long_call"}`), so persisted runners remain loadable:

```json
{
  "expression_version": 2,
  "type": {
    "BULLISH": {"structure": "bull_call_spread", "strike_selection": "atm"},
    "BEARISH": {"structure": "bear_put_spread", "strike_selection": "atm"},
    "NEUTRAL": {"structure": "strangle", "strike_selection": "fixed_distance", "distance_pct": 2.0}
  },
  "quantity": 1,
  "expiry_min_days": 1,
  "exit": {
    "signal_flip": true,
    "stop_loss_pct": 0.5,
    "take_profit_pct": 1.0,
    "min_days_to_expiry": 1,
    "reenter": false
  }
}
```

### 5. OptionsBridge Changes

**Principle**: Bridge becomes consumer/validator, not decision-maker.

```python
# src/backtest/forward/options_bridge.py

class OptionsBridge:
    def __init__(self, ..., strict: bool = False):
        # strict=True iff expression carries expression_version: 2
        self.strict = strict
        
        if strict:
            # NO silent merge with DEFAULT_EXPRESSION
            validate_expression(expression)  # raises on invalid
        else:
            # Legacy path for old state files
            expression = {**DEFAULT_EXPRESSION, **expression}
    
    def _rule_for(self, view: MarketView) -> DirectionRule | None:
        """No cross-direction fallback, ever."""
        type_cfg = self.expression.get("type")
        key = view.direction.value.upper()  # BULLISH / BEARISH / NEUTRAL
        
        if isinstance(type_cfg, dict):
            raw = type_cfg.get(key)  # NO .get(key, <other direction>)
        else:
            raw = type_cfg  # legacy string: all directions
        
        if raw is None and self.strict:
            # Coded rejection — visible in logs, journaled
            self._journal_halt(REASON_NO_DIRECTION_MAPPING, view)
            return None
        
        return DirectionRule.parse(raw) if raw is not None else None
```

**Key semantic change**: NEUTRAL view with no NEUTRAL rule degrades to viewless-bar semantics (drives `neutral_bars` exit counting) rather than silently mapping to BULLISH. This preserves "no conviction" idiom for directional strategies while preventing unauthorized trades.

### 6. Runner Creation API

All resolution happens **once, at spawn, server-side**:

```python
# src/backtest/api/portfolio.py

@api.route("/portfolio/runner/create", methods=["POST"])
def create_runner():
    data = request.get_json()
    strategy_cls = registry.get(data["strategy_name"])
    
    # NEW: Resolve expression with explicit precedence
    resolved, provenance = resolve_expression(
        strategy_cls=strategy_cls,
        playbook_expression=(instrument.get("expression") if data.get("playbook_id") else None),
        request_expression=(instrument.get("expression") if not data.get("playbook_id") else None),
    )
    # Precedence: request overrides > playbook snapshot > strategy default > refuse
    
    errors = validate_expression(resolved, strategy_cls)
    if errors:
        return jsonify({"error": "; ".join(errors)}), 400
    
    resolved["expression_version"] = 2
    instrument["expression"] = resolved
    instrument["expression_provenance"] = provenance  # per-key: strategy|playbook|user
    
    # ... rest of runner creation
```

**Fail-closed rule**: No strategy spec + no explicit expression → 400 error during migration window, hard refusal after cleanup. This replaces the current NEUTRAL→BULLISH guess.

### 7. UI Changes

**Show (read-only)**: One-line execution summary built from `/api/strategies` catalogue:

```
"NEUTRAL → long_call (ATM) · no stop/target · rides to settlement · 1 lot"
```

**Show (editable, "Advanced overrides" expander)**:
- Per-direction structure pickers rendered **only for directions in `emits`**
- Strike selection + delta/distance params per direction
- ExitConfig fields (stop-loss %, target %, etc.)
- Lots, bounded by `max_quantity`

**Never strategy-owned** (user-controlled):
- Allocated capital
- Lots (within strategy ceiling)
- Mode/source/segment/broker
- Manual per-position stop/target (existing operator override layer)

**Hide**: Raw expression JSON, `expression_version`, `allowed_structures` internals, provenance mechanics.

**JavaScript change**:

```javascript
// src/backtest/web/static/js/portfolio.js

// DELETE THIS (line 1424):
// body.instrument = {
//   type: "option",
//   expression: {
//     type: { BULLISH: "bull_call_spread", BEARISH: "bear_put_spread" },
//     quantity: lots,
//   },
// };

// REPLACE WITH: send only user overrides
body.instrument = {
  type: "option",
  expression: userOverrides || null,  // null = use strategy defaults
  quantity: lots,
};
```

---

## Migration Path

### Phase 0: Additive Foundation (Zero Behavior Change)

**Deliverables**:
- New module `src/backtest/strategy/execution.py` (ExecutionSpec, DirectionRule, resolve_expression, validate_expression)
- `Strategy.execution` attribute, `execution_spec()`, `default_expression()` methods
- `Strategy.validate()` extended to check execution spec
- `/api/strategies` endpoint gains `execution` block in response
- Tests: spec parsing, emits↔structures coverage, expression round-trip

**Rollback**: Revert module addition; existing strategies unaffected (execution=None).

### Phase 1: Bridge Strictness Behind Flag

**Deliverables**:
- Per-direction DirectionRule parsing in options_bridge
- `_structure_type_for` no-fallback logic (strict mode only)
- `create_selector` and `create_expiry_policy` wiring (unlocks fixed_distance, target_price selectors)
- Strict mode gated on `expression_version == 2`
- Legacy runners (old state files, old API clients) get current lenient path
- Tests: strict-mode rejection journal, per-direction strike selection, string-form back-compat

**Rollback**: Revert bridge changes; legacy path untouched.

### Phase 2: Migrate Strategy Classes

**Deliverables**:
- Add `execution` blocks to plugin strategies:
  - `plugins/strategies/atm_instant_buy.py` (NEUTRAL → long_call)
  - `plugins/strategies/immediate_entry.py`
  - `plugins/strategies/immediate_strangle.py` (2% OTM strangle → fixed_distance)
  - `plugins/strategies/ema_reversion_pob.py`
  - `plugins/strategies/momentum_burst.py`
  - `plugins/strategies/time_alternator.py`
- Add `execution` blocks to built-in strategies:
  - `src/backtest/strategies/option_directional.py`
  - `src/backtest/strategies/banknifty_straddle.py`
- Extend plugin conformance battery (`plugins/__init__.py:149`) to validate specs at load
- Tests: each strategy's declared spec validates; spawn with strategy defaults succeeds

**Rollback**: Remove execution blocks; strategies fall back to legacy path.

**Note**: Resolve name collision — two `AtmInstantBuy` classes exist (src/ vs plugins/). Only plugin version gets execution spec.

### Phase 3: UI Updates

**Deliverables**:
- `submitSpawn` deletes hardcoded default expression (portfolio.js:1424)
- Form renders strategy's execution summary from catalogue
- "Advanced overrides" expander shows per-direction pickers for directions in `emits`
- Playbook picker becomes explicit override layer
- Update JS contract tests in `tests/test_options_spawn_ui.py`

**Rollback**: Revert JS changes; old deployments unaffected.

### Phase 4: Cleanup

**Deliverables**:
- Delete `DEFAULT_EXPRESSION` constant
- Remove legacy merge in OptionsBridge constructor
- Delete NEUTRAL→BULLISH fallback in `_structure_type_for`
- Make strict mode unconditional for option runners
- Deprecation notice for `Strategy.stop_loss`/`take_profit` class attrs (equity backtest path only)

**Rollback**: Restore constants/fallbacks (though all runners should be v2 by now).

---

## Validation Rules

| Case | Where Caught | Behavior |
|------|--------------|----------|
| Strategy emits direction with no rule in structures | `Strategy.validate()` at plugin load + `validate_expression()` at spawn | Plugin refused at load; spawn → 400 naming missing direction |
| Runtime view arrives with unmapped direction | `OptionsBridge._rule_for` in strict mode | Coded halt, journaled, logged as OPTION_BLOCKED; no trade |
| Structure name unknown / outside allowed_structures gate | Spawn-time `validate_expression` + execution-time `check_structure_type` | 400 at spawn; defense-in-depth gate stays |
| `quantity` > strategy `max_quantity` | Spawn | 400 (v2) instead of silent clamp |
| Unknown `exit` keys | Spawn | 400 for v2 expressions; legacy expressions keep warn behavior |
| `delta_target` outside (0, 1], `fixed_distance` without `distance_pct` | Spec parse + spawn | `StrategyContractError` at load / 400 at spawn |
| Strategy with `execution=None` spawned as option with no playbook/expression | Spawn | 400 with remediation text (migration window); hard refusal post-cleanup |

**Unifying rule**: Every silent default becomes either explicit strategy declaration or loud, coded refusal. No third option.

---

## Testing Strategy

### Unit Tests

**New module** (`src/backtest/strategy/execution.py`):
- `test_execution_spec_from_dict`
- `test_execution_spec_validates_structure_names`
- `test_execution_spec_requires_all_emits_directions`
- `test_expression_round_trip_preserves_data`
- `test_resolve_expression_precedence_order`

**Strategy base** (`src/backtest/strategy/base.py`):
- `test_strategy_with_execution_spec_validates_at_load`
- `test_strategy_default_expression_matches_spec`
- `test_strategy_without_execution_returns_none`

**Bridge** (`src/backtest/forward/options_bridge.py`):
- `test_strict_mode_rejects_unmapped_direction`
- `test_strict_mode_no_cross_direction_fallback`
- `test_legacy_mode_still_supports_string_type`
- `test_per_direction_strike_selection_applied`

**API** (`src/backtest/api/portfolio.py`):
- `test_create_runner_with_strategy_defaults`
- `test_create_runner_with_user_overrides`
- `test_create_runner_fails_when_no_expression_source`
- `test_create_runner_respects_max_quantity`

### Integration Tests

- Deploy NIFTY + BANKNIFTY instances of `atm_instant_buy` with no overrides → both get long_call for NEUTRAL
- Deploy `immediate_strangle` → verify fixed_distance selector with 2% distance
- Deploy directional strategy with playbook override → verify playbook wins over strategy defaults
- Restart server with v1 legacy runner state → verify lenient path still works

### UI Contract Tests

- Form renders strategy execution summary from catalogue
- Advanced overrides show only directions in `emits`
- Submit sends only deltas, not full expression
- Playbook selection populates overrides correctly

---

## Backward Compatibility

**State files**: Runners persisted with v1 expressions (no `expression_version` key) load in lenient mode, behave exactly as today. No migration needed.

**Playbooks**: Existing playbook snapshots continue to work. Playbook layer becomes explicit override above strategy defaults.

**API clients**: Old clients sending full expression get legacy path. New clients can send `expression: null` to use strategy defaults.

**Equity strategies**: Unaffected. `execution` attribute ignored for non-option runners.

---

## Risk Assessment

| Risk | Mitigation |
|------|------------|
| Existing strategies break during migration | Phased rollout; execution=None preserves current behavior |
| User confusion over "strategy defaults" vs "overrides" | UI clearly labels provenance; advanced overrides hidden by default |
| Playbook layer becomes redundant | Keep as explicit override; document precedence clearly |
| Performance impact of validation | Validation happens once at spawn, not per-bar; negligible |
| Legacy runners behave differently after upgrade | Version-gated strict mode; v1 runners never hit strict path |

---

## Success Metrics

**Immediate**:
- NIFTY and BANKNIFTY deployments of same strategy produce identical behavior
- Zero silent fallbacks in bridge code (grep for `DEFAULT_EXPRESSION` returns no results)
- All plugin strategies have `execution` blocks with passing validation

**Long-term**:
- Deployment misconfigurations drop to zero (spawn-time 400s replace runtime surprises)
- Strategy authors self-document via code, not docstring prayers
- Support burden shifts from "why didn't this trade?" to "here's what the 400 error said"

---

## Open Questions for Architect Review

1. **Should `ExecutionSpec.quantity` live here or in runner config?** Current design puts default in strategy, user override in runner. Alternative: strategy declares only `max_quantity`, runner always specifies quantity.

2. **Should we version the exit vocabulary separately?** `ExitConfig` is reused across bridge and spec. If we extend exit rules, do we bump `expression_version` or version `exit` block independently?

3. **Plugin discovery order**: If plugin and built-in strategy share a name, which wins? (Currently plugins/ overrides src/, but should this be explicit in conformance battery?)

4. **Multi-leg structures**: `DirectionRule` currently assumes single structure per direction. Should we support conditional structures (e.g., "if IV rank > 50, use spread; else use naked call")?

5. **Playbook deprecation timeline**: How long do we support playbooks as an override layer before making strategy the sole authority?

---

## Appendix A: File Touchpoints

**New files**:
- `src/backtest/strategy/execution.py` — ExecutionSpec, DirectionRule, resolve_expression, validate_expression

**Modified files**:
- `src/backtest/strategy/base.py` — Add `execution`, `execution_spec()`, `default_expression()`, extend `validate()`
- `src/backtest/forward/options_bridge.py` — Strict mode, `_rule_for` no-fallback, selector factory wiring
- `src/backtest/api/portfolio.py` — `create_runner` resolution + validation
- `src/backtest/api/strategies.py` — Include `execution` in catalogue response
- `src/backtest/web/static/js/portfolio.js` — Delete hardcoded defaults, render strategy config
- `src/backtest/plugins/__init__.py` — Extend conformance battery to validate specs

**Migrated strategies** (Phase 2):
- `plugins/strategies/atm_instant_buy.py`
- `plugins/strategies/immediate_entry.py`
- `plugins/strategies/immediate_strangle.py`
- `plugins/strategies/ema_reversion_pob.py`
- `plugins/strategies/momentum_burst.py`
- `plugins/strategies/time_alternator.py`
- `src/backtest/strategies/option_directional.py`
- `src/backtest/strategies/banknifty_straddle.py`

**Test files** (new or extended):
- `tests/test_strategy_execution.py` (new)
- `tests/test_options_bridge_strict.py` (new)
- `tests/test_runner_create_validation.py` (new)
- `tests/test_options_spawn_ui.py` (extended)

---

## Appendix B: Glossary

- **Direction**: MarketView's directional output (BULLISH, BEARISH, NEUTRAL)
- **Structure**: Option structure type (long_call, bull_call_spread, strangle, etc.)
- **Expression**: JSON config mapping directions to structures with sizing and exit rules
- **Execution Spec**: Strategy's declarative execution metadata (the `execution` class attribute)
- **Playbook**: User-defined override layer above strategy defaults
- **Strict mode**: Bridge behavior when `expression_version == 2` (no fallbacks, fail closed)
- **Provenance**: Per-key tracking of config source (strategy / playbook / user override)

---

## Approval

**Reviewed by**: _________________  
**Date**: _________________  
**Status**: ☐ Approved  ☐ Approved with changes  ☐ Rejected  
**Comments**: 

---

*End of document*
