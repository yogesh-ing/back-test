"""Strategy templates (Refactor Plan v2.1, §5.2, Phase 0 skeleton).

No strategy is written from a blank file: the author picks a template,
fills in the signal/intent hooks, and the template enforces the contract
(complete intent, NEUTRAL = silence, mandatory hard stop — plan §4).

Phase 0 ships the skeleton: the base class with strategy-side
direction→rule resolution (the end-state owner, plan §4.1/Gap #2) and the
five template classes as thin, named specialisations. Intent *execution*
wiring lands in Phase 1/MVP; nothing here changes live behaviour, and the
template classes themselves are never registered (they declare no name).

Templates:
    DirectionalOptionStrategy   signal → BULLISH/BEARISH, own mapping
    InstantLongOptionStrategy   CE_instant_buy / PE_instant_buy shape
    SpreadStrategy              two-leg defined-risk spreads
    StructureStrategy           strangle/condor-type structures (named
                                neutrally: a short strangle is undefined
                                risk — plan §5.2/Gap #7)
    EquityStrategy              the existing equity path, unchanged
"""

from __future__ import annotations

from typing import Any

from ..base import Strategy
from ..execution import DirectionRule, ExecutionSpecError
from ..intent import MarketView

__all__ = [
    "TemplateBase",
    "DirectionalOptionStrategy",
    "InstantLongOptionStrategy",
    "SpreadStrategy",
    "StructureStrategy",
    "EquityStrategy",
]


class TemplateBase(Strategy):
    """Common contract for template-derived strategies.

    Owns direction→rule resolution (plan §4.1): a subclass's declared
    ``execution`` spec is consulted *strategy-side*, so the bridge only
    ever receives a complete intent. NEUTRAL resolves to ``None`` —
    silence never opens a trade.
    """

    #: Which template this strategy was built from (catalogue/telemetry).
    template: str = ""

    def direction_rule_for(self, view: MarketView | None) -> DirectionRule | None:
        """Resolve the rule for a view's direction, or ``None`` for silence.

        Returns ``None`` for a viewless bar and for NEUTRAL (by definition,
        plan §4.2). Raises :class:`ExecutionSpecError` (coded
        ``STRATEGY_EMITS_UNDECLARED_DIRECTION``) when the strategy emits a
        direction it did not declare — that is a bug in the strategy, and
        it must be loud, not mapped by someone else's fallback.
        """
        if view is None:
            return None
        spec = self.execution_spec()
        if spec is None:
            raise ExecutionSpecError(
                f"{self.name}: template-derived strategy must declare an "
                "execution spec (plan §4.4)"
            )
        return spec.direction_rule_for(view.direction)

    def build_opening_intent(self, view: MarketView | None) -> dict[str, Any] | None:
        """Phase-0 intent assembly: the complete, self-describing order ticket.

        Returns ``None`` when the view carries no opening conviction
        (viewless / NEUTRAL). Phase 1 hands this dict to the Risk Gate +
        bridge; in Phase 0 it is exercised by tests only.
        """
        rule = self.direction_rule_for(view)
        if rule is None or view is None:
            return None
        spec = self.execution_spec()
        assert spec is not None  # direction_rule_for already enforced this
        return {
            "action": "OPEN",
            "direction": view.direction.name,
            "structure": rule.structure,
            "strike_selection": rule.strike_selection,
            "delta_target": rule.delta_target,
            "distance_pct": rule.distance_pct,
            "expiry_policy": rule.expiry_policy,
            "expiry_min_days": rule.expiry_min_days,
            "exit": spec.exit_plan.to_dict(),
            "quantity": spec.default_lots,
            "strategy": {"name": self.name, "version": self.version},
        }


class DirectionalOptionStrategy(TemplateBase):
    """Signal logic → BULLISH/BEARISH; the strategy's spec maps each to a structure."""

    template = "directional_option"


class InstantLongOptionStrategy(TemplateBase):
    """Trades on the first eligible bar — CE or PE, named explicitly.

    ``CE_instant_buy`` / ``PE_instant_buy`` (plan §9.4 MVP) are instances
    of this shape: ``side = "CE"`` with a NEUTRAL-free, single-direction
    spec whose only emitted direction is the one that buys that side.
    The subclass states the side; it never smuggles it through NEUTRAL.
    """

    template = "instant_long_option"
    #: "CE" (buy calls) or "PE" (buy puts) — stated, never inferred.
    side: str = ""


class SpreadStrategy(TemplateBase):
    """Two-leg defined-risk spreads; worst case is computed from the legs at entry."""

    template = "spread"


class StructureStrategy(TemplateBase):
    """Multi-leg structures (strangle/condor family), opened by explicit intent.

    Loss is *not* inherently capped for short structures, so the spec's
    ExitPlan must carry a price/%/points hard stop (``structure_capped``
    alone does not satisfy this template — enforced at registration in
    Phase 2's conformance extension).
    """

    template = "structure"

    #: The explicit opening trade, stated by the subclass — never inferred
    #: from a direction. Shape: {"structure": ..., "strike_selection": ...,
    #: "distance_pct"/"delta_target": ..., "expiry_policy": ...,
    #: "expiry_min_days": ...}. The strategy emits a NEUTRAL *view* (it has
    #: no directional opinion) plus this intent; NEUTRAL itself never opens
    #: anything (plan §4.2) — the intent does, by name.
    opening_structure: dict = {}

    def build_opening_intent(self, view: MarketView | None) -> dict[str, Any] | None:
        if view is None or not self.opening_structure:
            return None
        spec = self.execution_spec()
        if spec is None:
            raise ExecutionSpecError(
                f"{self.name}: structure strategy must declare an execution spec"
            )
        return {
            "action": "OPEN",
            "direction": view.direction.name,
            "exit": spec.exit_plan.to_dict(),
            "quantity": spec.default_lots,
            "strategy": {"name": self.name, "version": self.version},
            **dict(self.opening_structure),
        }


class EquityStrategy(Strategy):
    """The existing equity path. The option contract is not imposed here."""

    template = "equity"
