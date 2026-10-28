"""Strategy-owned execution configuration (Refactor Plan v2.1, Phase 0).

This module is the *contract* layer of the Strategy-Owned Execution
refactor (``docs/STRATEGY_EXECUTION_REFACTOR_PLAN.md``):

* A strategy declares how its signals become trades, once, in code
  (:class:`ExecutionSpec` / :class:`DirectionRule` / :class:`ExitPlan`).
* Deployment layers (playbook bundle, user at spawn) may only *select*
  values inside the menus the strategy declares (:func:`resolve_expression`)
  — they can never invent a mapping, delete a direction, or remove a stop.
* Permission (margin, ceilings, kill switch) is **not** decided here; that
  is the single Pre-Trade Risk Gate's job (Phase 1). This module decides
  only whether a declaration/config is *well-formed*.

Phase-0 scope: parsing, canonicalisation, strict validation, resolution
with per-field provenance, and expression (de)serialisation. Nothing here
is wired into the live bridge yet — existing runners are unaffected.

Selector / expiry wiring table (plan §6.2 — plan word → implementation):

===================  =====================================================
``strike_selection`` Implementation (``backtest.options.selector``)
===================  =====================================================
``atm``              ``ATMSelector``
``delta``            ``DeltaSelector(delta_target=...)``
``fixed_distance``   ``FixedDistanceSelector(distance_pct=...)``
``target_price``     ``TargetPriceSelector``
===================  =====================================================

=======================  ==================================================
``expiry_policy``        Implementation (``backtest.options.expiry_policy``)
=======================  ==================================================
``nearest``              ``NearestExpiryPolicy``
``weekly``               ``WeeklyExpiryPolicy``
``fixed_days``           ``FixedDaysExpiryPolicy(days=...)``
``minimum_days``         ``MinimumDaysExpiryPolicy(min_days=...)``
=======================  ==================================================

Note the three distinct expiry concepts (do not conflate):
``expiry_policy``/``expiry_min_days`` choose the contract at *entry*;
``ExitPlan.min_days_to_expiry`` squares off an *open* trade near expiry.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from .intent import Direction

__all__ = [
    "EXECUTION_SCHEMA_VERSION",
    "KNOWN_STRUCTURES",
    "ExecutionSpecError",
    "DirectionRule",
    "ExitPlan",
    "parse_exit_plan_strict",
    "ExecutionSpec",
    "canonical_direction",
    "resolve_expression",
    "validate_expression",
    "execution_summary",
    "config_sha256",
    "STRATEGY_CODE_CHANGED",
]

#: Version of the ExitPlan schema *inside* a v2 expression (plan Q2:
#: the exit vocabulary is versioned independently of expression_version).
EXECUTION_SCHEMA_VERSION = 1

#: Coded state stamped on a runner whose recomputed bundle hash (§8) does
#: not match its spawn snapshot at rehydration. Defined here so Phase 1
#: wiring and Phase 0 tests share one constant.
STRATEGY_CODE_CHANGED = "STRATEGY_CODE_CHANGED"

#: Structures the bridge can actually materialise today
#: (``options_bridge.STRUCTURES``). Kept as data here to avoid an import
#: cycle (the bridge imports from this package); Phase 1 makes the Risk
#: Gate the single source and this set becomes gate-derived.
KNOWN_STRUCTURES: frozenset[str] = frozenset(
    {
        "long_call",
        "long_put",
        "bull_call_spread",
        "bear_put_spread",
        "strangle",
        "long_straddle",
        "short_straddle",
    }
)

#: Naked short-option structures whose worst-case loss is unbounded at the
#: structure level (OPT-4): ``structure_capped=True`` without ``max_loss_inr``
#: cannot satisfy their hard-stop requirement.
_UNCAPPED_SHORT_STRUCTURES: frozenset[str] = frozenset({"strangle", "short_straddle"})

_STRIKE_SELECTIONS = frozenset({"atm", "delta", "fixed_distance", "target_price"})
_EXPIRY_POLICIES = frozenset({"nearest", "weekly", "fixed_days", "minimum_days"})

#: Expression fields a deployment layer may select (plan §7: overrides are
#: field selections from this list, never free-form document merges).
#:
#: ``expiry_min_days`` is deliberately NOT selectable at top level: it has
#: exactly one canonical home in v2 — ``DirectionRule.expiry_min_days``
#: (per direction, plan §4.4; "one value, one file"). The legacy top-level
#: key survives only as an opaque passthrough when resolve_expression is
#: handed a legacy strategy_expression; the Phase 1 bridge reads the
#: per-direction value in strict mode.
_SELECTABLE_FIELDS = frozenset(
    {
        "type",
        "quantity",
        "exit",
        "squareoff_minutes_before",
        "max_loss_per_trade",
        "stale_after_seconds",
    }
)

#: Top-level expression extras a strategy may declare defaults for (the
#: legacy expression surface that must survive v2 conversion with no key
#: loss — plan Phase 0 round-trip requirement).
_DECLAREABLE_EXTRAS = frozenset(
    {"squareoff_minutes_before", "max_loss_per_trade", "stale_after_seconds"}
)

_EXIT_KEYS = frozenset(
    {
        "schema_version",
        "signal_flip",
        "neutral_bars",
        "stop_loss_pct",
        "take_profit_pct",
        "stop_loss_points",
        "take_profit_points",
        "stop_loss_price",
        "structure_capped",
        "max_loss_inr",
        "max_bars",
        "min_days_to_expiry",
        "reenter",
        "max_reentries_per_day",
        "strategy_exits",
    }
)


class ExecutionSpecError(ValueError):
    """A strategy execution declaration or v2 expression is not well-formed.

    The strict counterpart of the legacy tolerant parsers: where
    ``ExitConfig.from_expression`` warns and skips, this layer refuses.
    """


def canonical_direction(raw: Any) -> Direction:
    """The one place direction spellings are canonicalised (plan §4.4).

    Accepts a ``Direction``, or any case/whitespace variant of its name or
    value (``"NEUTRAL"``, ``"neutral"``, ``" Bullish "``). Anything else is
    an :class:`ExecutionSpecError` — never a guess.
    """
    if isinstance(raw, Direction):
        return raw
    if isinstance(raw, str):
        key = raw.strip().lower()
        for member in Direction:
            if key in (member.value, member.name.lower()):
                return member
    raise ExecutionSpecError(
        f"unknown direction {raw!r}; expected one of {[d.value for d in Direction]}"
    )


# --------------------------------------------------------------------------- #
# DirectionRule
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DirectionRule:
    """What to open when the strategy emits this direction (plan §4.4)."""

    structure: str
    strike_selection: str = "atm"
    delta_target: float | None = None
    distance_pct: float | None = None
    expiry_policy: str = "nearest"
    expiry_min_days: int = 0

    def __post_init__(self) -> None:
        structure = str(self.structure).strip().lower()
        object.__setattr__(self, "structure", structure)
        if structure not in KNOWN_STRUCTURES:
            raise ExecutionSpecError(
                f"unknown structure {structure!r}; known: {sorted(KNOWN_STRUCTURES)}"
            )
        selection = str(self.strike_selection).strip().lower()
        object.__setattr__(self, "strike_selection", selection)
        if selection not in _STRIKE_SELECTIONS:
            raise ExecutionSpecError(
                f"strike_selection must be one of {sorted(_STRIKE_SELECTIONS)}, "
                f"got {selection!r}"
            )
        if selection == "delta":
            if self.delta_target is None or not (0.0 < float(self.delta_target) <= 1.0):
                raise ExecutionSpecError(
                    "strike_selection='delta' requires delta_target in (0, 1]"
                )
        if selection == "fixed_distance":
            if self.distance_pct is None or float(self.distance_pct) <= 0:
                raise ExecutionSpecError(
                    "strike_selection='fixed_distance' requires distance_pct > 0"
                )
        policy = str(self.expiry_policy).strip().lower()
        object.__setattr__(self, "expiry_policy", policy)
        if policy not in _EXPIRY_POLICIES:
            raise ExecutionSpecError(
                f"expiry_policy must be one of {sorted(_EXPIRY_POLICIES)}, got {policy!r}"
            )
        if int(self.expiry_min_days) < 0:
            raise ExecutionSpecError("expiry_min_days must be >= 0")

    @classmethod
    def parse(cls, raw: "DirectionRule | Mapping[str, Any] | str") -> "DirectionRule":
        """Parse the dict form, or the legacy bare-string form (``"long_call"``)."""
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, str):  # legacy string form ≡ {"structure": raw}
            return cls(structure=raw)
        if isinstance(raw, Mapping):
            unknown = set(raw) - {
                "structure",
                "strike_selection",
                "delta_target",
                "distance_pct",
                "expiry_policy",
                "expiry_min_days",
            }
            if unknown:
                raise ExecutionSpecError(
                    f"unknown DirectionRule keys: {sorted(unknown)}"
                )
            if "structure" not in raw:
                raise ExecutionSpecError("DirectionRule requires 'structure'")
            return cls(
                structure=str(raw["structure"]),
                strike_selection=str(raw.get("strike_selection", "atm")),
                delta_target=raw.get("delta_target"),
                distance_pct=raw.get("distance_pct"),
                expiry_policy=str(raw.get("expiry_policy", "nearest")),
                expiry_min_days=int(raw.get("expiry_min_days", 0) or 0),
            )
        raise ExecutionSpecError(f"cannot parse DirectionRule from {type(raw).__name__}")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "structure": self.structure,
            "strike_selection": self.strike_selection,
            "expiry_policy": self.expiry_policy,
            "expiry_min_days": int(self.expiry_min_days),
        }
        if self.delta_target is not None:
            out["delta_target"] = float(self.delta_target)
        if self.distance_pct is not None:
            out["distance_pct"] = float(self.distance_pct)
        return out


# --------------------------------------------------------------------------- #
# ExitPlan (strict)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExitPlan:
    """A trade's exit contract (plan §4.3). A hard stop is mandatory.

    Exactly one hard-stop form must be present: ``stop_loss_pct``,
    ``stop_loss_points``, ``stop_loss_price``, or ``structure_capped=True``
    (long option / defined-risk spread, whose worst case is computed from
    the legs at entry). ``strategy_exits`` (indicator/rule exits) may ride
    alongside but never substitute for the hard stop.
    """

    stop_loss_pct: float | None = None
    stop_loss_points: float | None = None
    stop_loss_price: float | None = None
    structure_capped: bool = False
    take_profit_pct: float | None = None
    take_profit_points: float | None = None
    signal_flip: bool = True
    neutral_bars: int = 0
    max_loss_inr: float | None = None
    max_bars: int | None = None
    min_days_to_expiry: int | None = 1
    reenter: bool = False
    max_reentries_per_day: int = 2
    strategy_exits: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        hard = [
            self.stop_loss_pct is not None,
            self.stop_loss_points is not None,
            self.stop_loss_price is not None,
            bool(self.structure_capped),
        ]
        if sum(hard) != 1:
            raise ExecutionSpecError(
                "ExitPlan requires exactly one hard stop form: stop_loss_pct, "
                "stop_loss_points, stop_loss_price, or structure_capped=True "
                f"(got {sum(hard)})"
            )
        # stop_loss_pct is bounded by the premium itself (a long option
        # cannot lose more than it cost); take_profit_pct is not — a
        # premium can more than double, and legacy playbooks/strategies
        # legitimately declare 1.5–2.0 targets. The shared (0, 1] bound
        # was a Phase 0 conflation, exposed when Phase 3 playbook bundles
        # began resolving through the strict parser.
        if self.stop_loss_pct is not None and not (0.0 < float(self.stop_loss_pct) <= 1.0):
            raise ExecutionSpecError(
                f"stop_loss_pct must be in (0, 1], got {self.stop_loss_pct!r}"
            )
        if self.take_profit_pct is not None and not (float(self.take_profit_pct) > 0.0):
            raise ExecutionSpecError(
                f"take_profit_pct must be > 0, got {self.take_profit_pct!r}"
            )
        for name in ("stop_loss_points", "stop_loss_price", "take_profit_points", "max_loss_inr"):
            value = getattr(self, name)
            if value is not None and float(value) <= 0:
                raise ExecutionSpecError(f"{name} must be > 0, got {value!r}")
        if int(self.neutral_bars) < 0:
            raise ExecutionSpecError("neutral_bars must be >= 0")

    @property
    def has_hard_stop(self) -> bool:
        return (
            self.stop_loss_pct is not None
            or self.stop_loss_points is not None
            or self.stop_loss_price is not None
            or self.structure_capped
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"schema_version": EXECUTION_SCHEMA_VERSION}
        for key in (
            "stop_loss_pct",
            "stop_loss_points",
            "stop_loss_price",
            "take_profit_pct",
            "take_profit_points",
            "max_loss_inr",
            "max_bars",
        ):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        # ``min_days_to_expiry`` is emitted even when None: unlike the numbers
        # above, null is a declared *value* — ride to settlement (plan §4.3).
        # Omitting it made the spec→expression→strict-parse round trip re-apply
        # the dataclass default of 1, so a 0DTE strategy declared "ride" and the
        # bridge gated it as "square off 1d early": entry blocked on every bar.
        out["min_days_to_expiry"] = self.min_days_to_expiry
        out["structure_capped"] = bool(self.structure_capped)
        out["signal_flip"] = bool(self.signal_flip)
        out["neutral_bars"] = int(self.neutral_bars)
        out["reenter"] = bool(self.reenter)
        out["max_reentries_per_day"] = int(self.max_reentries_per_day)
        if self.strategy_exits:
            out["strategy_exits"] = list(self.strategy_exits)
        return out


def parse_exit_plan_strict(raw: "ExitPlan | Mapping[str, Any] | None") -> ExitPlan:
    """Strict ExitPlan parser for v2 configs (plan §4.3).

    Unlike the legacy ``ExitConfig.from_expression`` (tolerant by design,
    retained for v1), unknown keys and a missing hard stop are refusals.
    """
    if isinstance(raw, ExitPlan):
        return raw
    if raw is None:
        raise ExecutionSpecError("v2 expressions require an 'exit' block with a hard stop")
    if not isinstance(raw, Mapping):
        raise ExecutionSpecError(f"'exit' must be a dict, got {type(raw).__name__}")
    unknown = set(raw) - _EXIT_KEYS
    if unknown:
        raise ExecutionSpecError(f"unknown exit keys: {sorted(unknown)}")
    version = raw.get("schema_version", EXECUTION_SCHEMA_VERSION)
    if int(version) != EXECUTION_SCHEMA_VERSION:
        raise ExecutionSpecError(
            f"unsupported exit schema_version {version!r}; expected {EXECUTION_SCHEMA_VERSION}"
        )
    exits = raw.get("strategy_exits") or ()
    if isinstance(exits, str):
        exits = (exits,)
    return ExitPlan(
        stop_loss_pct=raw.get("stop_loss_pct"),
        stop_loss_points=raw.get("stop_loss_points"),
        stop_loss_price=raw.get("stop_loss_price"),
        structure_capped=bool(raw.get("structure_capped", False)),
        take_profit_pct=raw.get("take_profit_pct"),
        take_profit_points=raw.get("take_profit_points"),
        signal_flip=bool(raw.get("signal_flip", True)),
        neutral_bars=int(raw.get("neutral_bars", 0) or 0),
        max_loss_inr=raw.get("max_loss_inr"),
        max_bars=raw.get("max_bars"),
        min_days_to_expiry=raw.get("min_days_to_expiry", 1),
        reenter=bool(raw.get("reenter", False)),
        max_reentries_per_day=int(raw.get("max_reentries_per_day", 2) or 0),
        strategy_exits=tuple(str(e) for e in exits),
    )


# --------------------------------------------------------------------------- #
# ExecutionSpec
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExecutionSpec:
    """A strategy's declarative execution declaration (plan §4.4). Immutable."""

    emits: frozenset[Direction]
    structures: Mapping[Direction, DirectionRule]
    exit_plan: ExitPlan
    default_lots: int = 1
    min_lots: int = 1
    max_lots: int = 1
    allowed_overrides: frozenset[str] = frozenset({"quantity"})
    #: Declared defaults for the rest of the legacy expression surface
    #: (squareoff/stale/risk-cap keys), emitted verbatim by to_expression
    #: so v2 conversion loses no keys.
    expression_extras: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def __post_init__(self) -> None:
        emits = frozenset(self.emits)
        structures = MappingProxyType(dict(self.structures))
        object.__setattr__(self, "emits", emits)
        object.__setattr__(self, "structures", structures)
        # NEUTRAL in `emits` declares "may stay silent" — it needs (and may
        # never have) a structure rule; every other emitted direction does.
        missing = emits - set(structures) - {Direction.NEUTRAL}
        if missing:
            raise ExecutionSpecError(
                "every non-neutral direction in 'emits' needs a structure rule; "
                f"missing: {sorted(d.value for d in missing)}"
            )
        extra = set(structures) - emits
        if extra:
            raise ExecutionSpecError(
                "structure rules declared for directions not in 'emits': "
                f"{sorted(d.value for d in extra)}"
            )
        if Direction.NEUTRAL in structures:
            raise ExecutionSpecError(
                "NEUTRAL may not carry a structure rule: NEUTRAL is silence, "
                "never an entry (plan §4.2). Emit an explicit opening intent instead."
            )
        object.__setattr__(
            self, "expression_extras", MappingProxyType(dict(self.expression_extras))
        )
        bad_extras = frozenset(self.expression_extras) - _DECLAREABLE_EXTRAS
        if bad_extras:
            raise ExecutionSpecError(
                f"expression extras outside the declareable surface: {sorted(bad_extras)}"
            )
        if not (1 <= int(self.min_lots) <= int(self.default_lots) <= int(self.max_lots)):
            raise ExecutionSpecError(
                "lot menu must satisfy 1 <= min_lots <= default_lots <= max_lots, "
                f"got min={self.min_lots} default={self.default_lots} max={self.max_lots}"
            )
        bad_overrides = frozenset(self.allowed_overrides) - _SELECTABLE_FIELDS
        if bad_overrides:
            raise ExecutionSpecError(
                f"allowed_overrides outside selectable fields: {sorted(bad_overrides)}"
            )
        uncapped = sorted(
            {
                r.structure
                for r in structures.values()
                if r.structure in _UNCAPPED_SHORT_STRUCTURES
            }
        )
        if uncapped and self.exit_plan.structure_capped and self.exit_plan.max_loss_inr is None:
            raise ExecutionSpecError(
                f"short-premium structure(s) {uncapped} carry undefined risk: "
                "ExitPlan cannot rely on structure_capped=True without "
                "stop_loss_pct, stop_loss_points, or max_loss_inr"
            )

    # -- parsing ---------------------------------------------------------- #

    @classmethod
    def from_declared(cls, raw: "ExecutionSpec | Mapping[str, Any]") -> "ExecutionSpec":
        """Normalise the dict form declared on a strategy class, or pass a spec through."""
        if isinstance(raw, cls):
            return raw
        if not isinstance(raw, Mapping):
            raise ExecutionSpecError(
                f"execution declaration must be a dict or ExecutionSpec, got {type(raw).__name__}"
            )
        unknown = set(raw) - {
            "emits",
            "structures",
            "exit",
            "exit_plan",
            "quantity",
            "default_lots",
            "min_lots",
            "max_lots",
            "min_quantity",
            "max_quantity",
            "allowed_overrides",
            *_DECLAREABLE_EXTRAS,
        }
        if unknown:
            raise ExecutionSpecError(f"unknown execution keys: {sorted(unknown)}")
        if "emits" not in raw or "structures" not in raw:
            raise ExecutionSpecError("execution declaration requires 'emits' and 'structures'")
        emits = frozenset(canonical_direction(d) for d in raw["emits"])
        structures_raw = raw["structures"]
        if not isinstance(structures_raw, Mapping):
            raise ExecutionSpecError("'structures' must map direction -> rule")
        structures = {
            canonical_direction(k): DirectionRule.parse(v)
            for k, v in structures_raw.items()
        }
        exit_raw = raw.get("exit_plan", raw.get("exit"))
        exit_plan = parse_exit_plan_strict(exit_raw)
        default_lots = int(raw.get("default_lots", raw.get("quantity", 1)) or 1)
        min_lots = int(raw.get("min_lots", raw.get("min_quantity", 1)) or 1)
        max_lots = int(raw.get("max_lots", raw.get("max_quantity", default_lots)) or default_lots)
        extras = {k: raw[k] for k in _DECLAREABLE_EXTRAS if k in raw}
        overrides = raw.get("allowed_overrides") or ("quantity",)
        if isinstance(overrides, str):
            overrides = (overrides,)
        return cls(
            emits=emits,
            structures=structures,
            exit_plan=exit_plan,
            default_lots=default_lots,
            min_lots=min_lots,
            max_lots=max_lots,
            allowed_overrides=frozenset(str(o) for o in overrides),
            expression_extras=extras,
        )

    # -- serialisation ----------------------------------------------------- #

    def to_expression(self) -> dict[str, Any]:
        """Emit a v2 expression dict (plan §4.4). Strategy layer — provenance 'strategy'."""
        out: dict[str, Any] = {
            "expression_version": 2,
            "type": {
                direction.name: rule.to_dict()
                for direction, rule in sorted(
                    self.structures.items(), key=lambda kv: kv[0].name
                )
            },
            "quantity": int(self.default_lots),
            "exit": self.exit_plan.to_dict(),
        }
        out.update(copy.deepcopy(dict(self.expression_extras)))
        return out

    def direction_rule_for(self, direction: Any) -> DirectionRule | None:
        """The strategy-side direction→rule lookup (plan §4.1, end state).

        NEUTRAL returns ``None`` always: silence never opens a trade.
        A direction outside ``emits`` is a strategy contract breach.
        """
        direction = canonical_direction(direction)
        if direction is Direction.NEUTRAL:
            return None
        if direction not in self.emits:
            raise ExecutionSpecError(
                f"STRATEGY_EMITS_UNDECLARED_DIRECTION: strategy emitted "
                f"{direction.value!r} which is not in its declared emits "
                f"{sorted(d.value for d in self.emits)}"
            )
        return self.structures[direction]


# --------------------------------------------------------------------------- #
# Resolution (§7) and validation
# --------------------------------------------------------------------------- #


def _leaf_paths(value: Any, prefix: str) -> list[str]:
    if isinstance(value, Mapping):
        paths: list[str] = []
        for key in sorted(value, key=str):
            paths.extend(_leaf_paths(value[key], f"{prefix}.{key}" if prefix else str(key)))
        return paths
    return [prefix]


def _restates_current(current: Any, incoming: Any) -> bool:
    """True when every leaf of ``incoming`` already equals ``current``.

    Partial mappings restate leaf-by-leaf: naming a subset of a rule's
    leaves with their current values changes nothing.
    """
    if isinstance(incoming, Mapping):
        if not isinstance(current, Mapping):
            return False
        return all(
            key in current and _restates_current(current[key], value)
            for key, value in incoming.items()
        )
    return current == incoming


def _apply_selections(
    base: dict[str, Any],
    selections: Mapping[str, Any] | None,
    layer: str,
    spec: ExecutionSpec | None,
    provenance: dict[str, str],
) -> None:
    """Apply one layer's *field selections* onto ``base`` (mutates base).

    Selections are leaf-level: a nested dict selects the leaves it names.
    ``None`` values are refused — a selection cannot delete anything (§7).
    """
    if not selections:
        return
    for key, value in selections.items():
        if key not in _SELECTABLE_FIELDS:
            raise ExecutionSpecError(
                f"{layer} selection names unselectable field {key!r}; "
                f"selectable: {sorted(_SELECTABLE_FIELDS)}"
            )
        if spec is not None and key not in spec.allowed_overrides and layer != "strategy":
            # Menu enforcement is about *changes*: a selection that merely
            # restates the strategy's own current values for a non-menu
            # field is a no-op (a playbook naming the strategy's own
            # structures must not refuse against itself), while any leaf
            # that would move the value refuses, naming field and menu.
            if _restates_current(base.get(key), value):
                continue
            raise ExecutionSpecError(
                f"{layer} may not override {key!r}: not in the strategy's "
                f"allowed_overrides {sorted(spec.allowed_overrides)}"
            )
        if value is None:
            raise ExecutionSpecError(
                f"{layer} selection for {key!r} is null: selections cannot delete fields"
            )
        if isinstance(value, Mapping) and isinstance(base.get(key), Mapping):
            for sub_key, sub_value in value.items():
                if sub_value is None and not (
                    key == "exit" and sub_key == "min_days_to_expiry"
                ):
                    # Sole exception: exit.min_days_to_expiry=null is a
                    # *value* (ride to settlement, plan §4.3), not a deletion.
                    raise ExecutionSpecError(
                        f"{layer} selection for {key}.{sub_key} is null: "
                        "selections cannot delete fields"
                    )
                if isinstance(sub_value, Mapping) and isinstance(
                    base[key].get(sub_key), Mapping
                ):
                    merged = copy.deepcopy(base[key][sub_key])
                    merged.update(copy.deepcopy(dict(sub_value)))
                    base[key][sub_key] = merged
                    for leaf in _leaf_paths(sub_value, f"{key}.{sub_key}"):
                        provenance[leaf] = layer
                else:
                    base[key][sub_key] = copy.deepcopy(sub_value)
                    for leaf in _leaf_paths(sub_value, f"{key}.{sub_key}"):
                        provenance[leaf] = layer
        else:
            base[key] = copy.deepcopy(value)
            for leaf in _leaf_paths(value, str(key)):
                provenance[leaf] = layer


def resolve_expression(
    *,
    spec: ExecutionSpec | None = None,
    strategy_expression: Mapping[str, Any] | None = None,
    playbook_selections: Mapping[str, Any] | None = None,
    user_selections: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Resolve the final v2 expression + per-leaf provenance (plan §7, §8).

    Precedence (low → high): strategy default → playbook selections →
    user selections. Each layer selects *fields within the strategy's
    menus*; quantities outside the declared lot menu are refused here
    (spawn turns the refusal into a 400), never clamped.
    """
    if spec is not None:
        resolved = spec.to_expression()
    elif strategy_expression is not None:
        resolved = copy.deepcopy(dict(strategy_expression))
    else:
        raise ExecutionSpecError(
            "no strategy execution spec and no strategy expression: "
            "an option runner cannot be resolved (fail closed)"
        )
    provenance = {leaf: "strategy" for leaf in _leaf_paths(resolved, "") if leaf}
    _apply_selections(resolved, playbook_selections, "playbook", spec, provenance)
    _apply_selections(resolved, user_selections, "user", spec, provenance)
    resolved["expression_version"] = 2
    if spec is not None and "quantity" in resolved:
        qty = int(resolved["quantity"])
        if not (spec.min_lots <= qty <= spec.max_lots):
            raise ExecutionSpecError(
                f"quantity {qty} outside the strategy's lot menu "
                f"[{spec.min_lots}, {spec.max_lots}] — refused, not clamped"
            )
    errors = validate_expression(resolved, spec=spec)
    if errors:
        raise ExecutionSpecError("; ".join(errors))
    return resolved, provenance


def validate_expression(
    expression: Mapping[str, Any] | None,
    *,
    spec: ExecutionSpec | None = None,
    permitted_structures: frozenset[str] | None = None,
) -> list[str]:
    """Well-formedness errors for a v2 expression ([] == valid).

    ``permitted_structures`` is the caller's entitlement view (Phase 1:
    supplied by the Risk Gate, plan §6.1). When omitted, only structural
    known-ness is checked here — permission is the gate's question, asked
    at trade time as well as spawn.
    """
    errors: list[str] = []
    if not isinstance(expression, Mapping):
        return ["expression must be a dict"]
    if int(expression.get("expression_version", 0) or 0) != 2:
        errors.append("expression_version must be 2 for strict validation")
    type_cfg = expression.get("type")
    if type_cfg is None:
        errors.append("expression requires a 'type' direction map")
        type_cfg = {}
    if isinstance(type_cfg, str):  # legacy string form: one rule, all directions
        try:
            DirectionRule.parse(type_cfg)
        except ExecutionSpecError as exc:
            errors.append(str(exc))
        type_cfg = {}
    if isinstance(type_cfg, Mapping):
        for raw_direction, raw_rule in type_cfg.items():
            try:
                direction = canonical_direction(raw_direction)
            except ExecutionSpecError as exc:
                errors.append(str(exc))
                continue
            if direction is Direction.NEUTRAL:
                errors.append(
                    "NEUTRAL may not carry a structure rule (plan §4.2)"
                )
                continue
            try:
                rule = DirectionRule.parse(raw_rule)
            except ExecutionSpecError as exc:
                errors.append(f"{direction.name}: {exc}")
                continue
            if permitted_structures is not None and rule.structure not in permitted_structures:
                errors.append(
                    f"{direction.name}: structure {rule.structure!r} is not "
                    "permitted for this mode/account (Risk Gate entitlement)"
                )
            if spec is not None and direction not in spec.emits:
                errors.append(
                    f"{direction.name}: rule declared for a direction the "
                    "strategy does not emit"
                )
    else:
        errors.append("'type' must be a direction map or a structure string")
    # Exit block: strict for v2 (legacy tolerant parsing stays in exit_policy).
    try:
        _plan = parse_exit_plan_strict(expression.get("exit"))
        if _plan.stop_loss_price is not None:
            # Disposition (Phase 1 verification flag): a price-based stop is
            # a valid ExitPlan form in the abstract, but the exit engine
            # (ExitConfig) cannot represent it today and the strict bridge
            # mapping would drop it — a price-only-stop runner would trade
            # unprotected. Refuse until the engine supports it; never drop.
            errors.append(
                "stop_loss_price is refused until the exit engine supports "
                "price-based stops; use stop_loss_pct, stop_loss_points, or "
                "structure_capped instead"
            )
        if (
            _plan.structure_capped
            and _plan.max_loss_inr is None
            and _plan.stop_loss_pct is None
            and _plan.stop_loss_points is None
        ):
            declared_structs: set[str] = set()
            raw_type_cfg = expression.get("type")
            if isinstance(raw_type_cfg, str):
                declared_structs.add(raw_type_cfg.strip().lower())
            elif isinstance(raw_type_cfg, Mapping):
                for raw_r in raw_type_cfg.values():
                    if isinstance(raw_r, str):
                        declared_structs.add(raw_r.strip().lower())
                    elif isinstance(raw_r, Mapping) and raw_r.get("structure"):
                        declared_structs.add(str(raw_r["structure"]).strip().lower())
            bad_short = sorted(declared_structs & _UNCAPPED_SHORT_STRUCTURES)
            if bad_short:
                errors.append(
                    f"short-premium structure(s) {bad_short} carry undefined risk: "
                    "ExitPlan cannot rely on structure_capped=True without "
                    "stop_loss_pct, stop_loss_points, or max_loss_inr"
                )
    except ExecutionSpecError as exc:
        errors.append(str(exc))
    if spec is not None:
        qty = expression.get("quantity", spec.default_lots)
        try:
            if not (spec.min_lots <= int(qty) <= spec.max_lots):
                errors.append(
                    f"quantity {qty} outside lot menu [{spec.min_lots}, {spec.max_lots}]"
                )
        except (TypeError, ValueError):
            errors.append(f"quantity must be an int, got {qty!r}")
        if isinstance(type_cfg, Mapping):
            mapped = set()
            for raw_direction in type_cfg:
                try:
                    mapped.add(canonical_direction(raw_direction))
                except ExecutionSpecError:
                    pass
            missing = (spec.emits - {Direction.NEUTRAL}) - mapped
            if missing:
                errors.append(
                    "missing structure rules for emitted directions: "
                    f"{sorted(d.value for d in missing)}"
                )
    return errors


def config_sha256(resolved_expression: Mapping[str, Any]) -> str:
    """Fingerprint of a resolved spawn config (plan §8: ``config_sha256``).

    Canonical JSON (sorted keys, compact separators) so the hash depends
    on content only — never on dict insertion order or platform.
    """
    import hashlib
    import json

    canonical = json.dumps(
        dict(resolved_expression), sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def execution_summary(spec: ExecutionSpec | None) -> dict[str, Any] | None:
    """Catalogue/UI block for a strategy's execution declaration (Phase 0).

    Returns ``None`` for legacy strategies (``execution is None``) so the
    API stays additive: existing clients see exactly what they saw before.
    """
    if spec is None:
        return None
    parts: list[str] = []
    for direction in sorted(spec.emits, key=lambda d: d.name):
        if direction is Direction.NEUTRAL:
            parts.append("NEUTRAL → no trade")
        else:
            rule = spec.structures[direction]
            parts.append(
                f"{direction.name} → {rule.structure} ({rule.strike_selection})"
            )
    exit_plan = spec.exit_plan
    if exit_plan.stop_loss_pct is not None:
        parts.append(f"stop −{exit_plan.stop_loss_pct:.0%}")
    elif exit_plan.structure_capped:
        parts.append("stop = structure max loss")
    elif exit_plan.stop_loss_points is not None:
        parts.append(f"stop −₹{exit_plan.stop_loss_points:g}")
    elif exit_plan.stop_loss_price is not None:
        parts.append(f"stop at ₹{exit_plan.stop_loss_price:g}")
    if exit_plan.take_profit_pct is not None:
        parts.append(f"target +{exit_plan.take_profit_pct:.0%}")
    parts.append(
        f"{spec.default_lots} lot"
        + ("s" if spec.default_lots != 1 else "")
        + f" (menu {spec.min_lots}–{spec.max_lots})"
    )
    return {
        "emits": sorted(d.value for d in spec.emits),
        "structures": {
            d.name: rule.to_dict() for d, rule in spec.structures.items()
        },
        "exit": exit_plan.to_dict(),
        "lots": {
            "default": spec.default_lots,
            "min": spec.min_lots,
            "max": spec.max_lots,
        },
        "allowed_overrides": sorted(spec.allowed_overrides),
        "summary": " · ".join(parts),
    }
