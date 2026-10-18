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

from typing import Iterable

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
