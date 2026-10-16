"""V1 execution whitelist — the gate every option intent must pass before a broker sees it.

PRD `docs/OPTION-STRATEGY-BUILDER-ARCHITECT-PRD.md` §6: the execution gate
accepts **long CE / long PE only**, from *any* source — builder, API, imported
config, or a hand-built `TradeIntent`. The playbook and engine layers
intentionally support wider structures (`bull_call_spread`, `strangle`, …); that
breadth is not a defense, so the check lives at the broker boundary rather than
in the authoring UI.

Three properties are enforced, each with its own rejection reason so a soak can
count them:

* **structure type** — must be in the allowed set;
* **leg count** — exactly one leg (no spreads, no straddles, no multi-leg);
* **leg side** — must be ``BUY`` (a short option has unbounded loss and needs
  margin, which V1 never models).

The option type suffix (``CE``/``PE``) is checked when the symbol carries one,
so a payload labelled ``long_call`` with a put inside it is rejected too.
"""

from __future__ import annotations

from typing import Any, Iterable

#: What V1 may open: a long call or a long put, nothing else.
V1_ALLOWED_STRUCTURES: frozenset[str] = frozenset({"long_call", "long_put"})

#: The wider profile — research and the frozen options-backtest path only.
#: Never the default on a forward runner; a caller must ask for it by name.
RESEARCH_STRUCTURES: frozenset[str] = frozenset(
    {
        "long_call",
        "long_put",
        "bull_call_spread",
        "bear_put_spread",
        "straddle",
        "strangle",
        "iron_condor",
        "calendar_spread",
    }
)

#: structure type → the option side its single leg must be.
_EXPECTED_SIDE = {"long_call": "CE", "long_put": "PE"}

#: Rejection reasons — stable identifiers, safe to log and count.
REASON_STRUCTURE_TYPE = "STRUCTURE_NOT_IN_V1"
#: Phase 1 (Plan v2.1 §6.1): the consolidated Pre-Trade Risk Gate reasons.
#: These are the ONLY runtime permission vocabulary — UI/bridge/API no
#: longer invent their own refusals, clamps, or fallbacks.
REASON_STRUCTURE_NOT_PERMITTED = "STRUCTURE_NOT_PERMITTED"
REASON_MAX_LOSS_EXCEEDED = "MAX_LOSS_EXCEEDED"
REASON_QUANTITY_CEILING = "QUANTITY_ABOVE_CEILING"
REASON_ORDER_VALUE_EXCEEDED = "ORDER_VALUE_EXCEEDED"
REASON_KILL_SWITCH = "KILL_SWITCH_ENGAGED"
REASON_LEG_COUNT = "MULTI_LEG_NOT_IN_V1"
REASON_LEG_SIDE = "SHORT_OPTION_NOT_IN_V1"
REASON_OPTION_SIDE = "OPTION_SIDE_MISMATCH"
#: Contract validity at the decision (§4.2: halt with an explicit reason, never
#: substitute a contract, a lot size, or a quote the market is not offering).
REASON_NO_EXPIRY = "NO_EXPIRY_ON_CALENDAR"
REASON_EXPIRY_TOO_NEAR = "EXPIRY_BELOW_MIN_DAYS"
REASON_NO_CONTRACT = "NO_CHAIN_CONTRACT"
REASON_NO_STRIKE = "NO_STRIKE_IN_TOLERANCE"
REASON_NO_LOT_SIZE = "NO_CHAIN_LOT_SIZE"
REASON_NO_FRESH_QUOTE = "NO_FRESH_QUOTE"


class StructureNotAllowedError(ValueError):
    """An intent the execution gate refuses. Carries its rejection ``reason``."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class ContractValidityError(ValueError):
    """The contract the entry would trade is not a valid one — refuse it.

    Same shape as :class:`StructureNotAllowedError` so a soak counts rejection
    classes off ``reason`` rather than off log text.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def _option_side(symbol: str) -> str | None:
    """``"CE"``/``"PE"`` when the symbol is suffixed like an index option."""
    upper = str(symbol or "").upper()
    if upper.endswith("CE"):
        return "CE"
    if upper.endswith("PE"):
        return "PE"
    return None


def check_intent(intent: object, allowed: Iterable[str] = V1_ALLOWED_STRUCTURES) -> None:
    """Raise :class:`StructureNotAllowedError` unless ``intent`` passes the gate.

    Fail-closed: an intent that is not a ``TradeIntent`` with a ``legs``
    sequence is rejected rather than let through.
    """
    allowed_set = set(allowed)
    structure_type = str(getattr(intent, "structure_type", "") or "")

    if structure_type not in allowed_set:
        raise StructureNotAllowedError(
            REASON_STRUCTURE_TYPE,
            f"structure_type {structure_type!r} is not executable here "
            f"(allowed: {sorted(allowed_set)})",
        )

    legs = getattr(intent, "legs", None)
    if legs is None:
        raise StructureNotAllowedError(
            REASON_LEG_COUNT, f"{structure_type}: intent exposes no legs to check"
        )
    legs = tuple(legs)

    # The one-leg / BUY / option-side rules are properties of the V1 structures
    # themselves (a long call is one bought contract). A wider profile is a
    # research book: it may hold short legs by construction, so only the
    # whitelist above applies there.
    if not allowed_set <= V1_ALLOWED_STRUCTURES:
        return

    if len(legs) != 1:
        raise StructureNotAllowedError(
            REASON_LEG_COUNT,
            f"{structure_type}: V1 executes one leg, got {len(legs)}",
        )

    leg = legs[0]
    side = str(getattr(leg, "side", "") or "").upper()
    if side != "BUY":
        raise StructureNotAllowedError(
            REASON_LEG_SIDE,
            f"{structure_type}: leg side is {side!r}; V1 buys premium only "
            f"(a short option has unbounded loss)",
        )

    expected = _EXPECTED_SIDE.get(structure_type)
    actual = _option_side(getattr(leg, "trading_symbol", ""))
    if expected is not None and actual is not None and actual != expected:
        raise StructureNotAllowedError(
            REASON_OPTION_SIDE,
            f"{structure_type} requires a {expected} contract, "
            f"leg carries {actual} ({leg.trading_symbol})",
        )


def check_structure_type(
    structure_type: str, allowed: Iterable[str] = V1_ALLOWED_STRUCTURES
) -> None:
    """Gate the bridge's own choice before it builds contracts for it."""
    allowed_set = set(allowed)
    if structure_type not in allowed_set:
        raise StructureNotAllowedError(
            REASON_STRUCTURE_TYPE,
            f"structure_type {structure_type!r} is not executable here "
            f"(allowed: {sorted(allowed_set)})",
        )


# --------------------------------------------------------------------------- #
# Pre-Trade Risk Gate (Phase 1, Plan v2.1 §6.1) — the ONE runtime gate
# --------------------------------------------------------------------------- #


class PreTradeRiskGate:
    """Permission for a specific opening trade, decided outside the strategy.

    The strategy owns *intent* (what it wants); this gate owns *permission*
    (whether this account/mode may do it, at this size, right now). Its
    rulebook is Layer-1 policy (``config/risk_limits.yaml``, Phase 0):
    loaded fail-closed by :func:`backtest.risk_limits.load_risk_limits`,
    so constructing a gate over a broken rulebook raises rather than
    permitting. Legacy module-level ``check_*`` functions above remain the
    broker-boundary leg checks and are unchanged.
    """

    def __init__(self, limits: Any = None, *, mode: str = "paper") -> None:
        if limits is None:
            from backtest.risk_limits import load_risk_limits

            limits = load_risk_limits()  # fail-closed: raises on a bad rulebook
        self.limits = limits
        self.mode = str(mode or "paper").strip().lower()
        self.kill_switch_engaged = bool(
            getattr(limits, "kill_switch_engaged_on_boot", False)
        )

    def permitted_structures(self) -> frozenset[str]:
        """Structures this mode may trade at all (entitlement, §7 layer 2)."""
        return self.limits.permitted_structures(self.mode)

    def check_opening(
        self,
        structure_type: str,
        *,
        quantity: int = 1,
        worst_case_loss_inr: float | None = None,
        order_value_inr: float | None = None,
    ) -> None:
        """Raise :class:`StructureNotAllowedError` unless this opening is permitted.

        ``worst_case_loss_inr``/``order_value_inr`` are checked when the
        caller can state them (an intent that can state neither is refused
        by contract validation upstream, plan §4.3 — the gate never has to
        guess a number here).
        """
        if self.kill_switch_engaged:
            raise StructureNotAllowedError(
                REASON_KILL_SWITCH, "kill switch is engaged; no openings permitted"
            )
        structure = str(structure_type or "").strip().lower()
        if structure not in self.permitted_structures():
            raise StructureNotAllowedError(
                REASON_STRUCTURE_NOT_PERMITTED,
                f"structure {structure!r} is not entitled for mode {self.mode!r} "
                f"(permitted: {sorted(self.permitted_structures())})",
            )
        if int(quantity) < 1 or int(quantity) > int(self.limits.per_runner_max_lots):
            raise StructureNotAllowedError(
                REASON_QUANTITY_CEILING,
                f"quantity {quantity} outside [1, {self.limits.per_runner_max_lots}] "
                "(Layer-1 per-runner ceiling — refused, not clamped)",
            )
        if (
            worst_case_loss_inr is not None
            and float(worst_case_loss_inr) > float(self.limits.per_trade_max_loss_inr)
        ):
            raise StructureNotAllowedError(
                REASON_MAX_LOSS_EXCEEDED,
                f"worst-case loss ₹{float(worst_case_loss_inr):,.0f} exceeds the "
                f"per-trade ceiling ₹{float(self.limits.per_trade_max_loss_inr):,.0f}",
            )
        if (
            order_value_inr is not None
            and float(order_value_inr) > float(self.limits.per_runner_max_order_value_inr)
        ):
            raise StructureNotAllowedError(
                REASON_ORDER_VALUE_EXCEEDED,
                f"order value ₹{float(order_value_inr):,.0f} exceeds the per-runner "
                f"ceiling ₹{float(self.limits.per_runner_max_order_value_inr):,.0f}",
            )
