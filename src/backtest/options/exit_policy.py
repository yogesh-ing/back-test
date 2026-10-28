"""Exit rules for option structures — forward testing task B1.

The forward bridge could open a structure but never leave one: while a
structure was open it ignored every later view, so the only exits were a
human on the dashboard (which holds a different book) or expiry — and expiry
only ran on the backtest/dashboard paths, not in the runner loop.

This module is the missing half of the position lifecycle. It is a pure
evaluator: give it the current view, the structure's mark and age, and it
returns the reason to close (or ``None`` to keep holding). The bridge owns
the bookkeeping (bar clock, neutral counter, closing the structure), so the
rules stay unit-testable without a broker.

Rule order (first match wins — the exit precedence in
`docs/OPTION-STRATEGY-BUILDER-ARCHITECT-PRD.md` §2 "Risk & exits", protective
before opinion)::

    risk cap → days-to-expiry square-off → stop loss → take profit
    → time stop → signal flip → signal neutral

The tier above all of these, an emergency flatten from a breaker or an
operator, bypasses this policy — the runner owns it.

The cap is the platform's authority and the square-off is a scheduled close, so
neither may be pre-empted by the strategy's own, softer stop/target.
See :data:`PRICE_MEASURED_REASONS` for the tiers a bad mark must not drive.

Configuration lives in the runner's expression block::

    instrument = {
        "type": "option",
        "expression": {
            "type": {"BULLISH": "bull_call_spread", "BEARISH": "bear_put_spread"},
            "exit": {
                "signal_flip": true,          # close when the view turns
                "neutral_bars": 3,            # close after N bars with no view
                "max_loss_inr": 5000,         # per-trade risk cap (₹ of P&L)
                "stop_loss_pct": 0.5,         # 50% of the net premium
                "take_profit_pct": 1.0,       # +100% of the net premium
                "stop_loss_points": 2000.0,   # ...or absolute ₹ of structure P&L
                "take_profit_points": 3000.0,
                "max_bars": 10,               # time stop, in bars
                "min_days_to_expiry": 1,      # square off the day before expiry
                "reenter": false,             # flip = close only, or close + reverse
                "max_reentries_per_day": 2,   # only when reenter is true
            },
        },
    }

Every key is optional. Percent rules are measured against the structure's
**net premium** (its max loss for all four V1 structures) and are skipped for
credit structures, where a percentage of a negative base has no meaning.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Optional

logger = logging.getLogger("backtest.options.exit_policy")

# ---------------------------------------------------------------------------
# exit_reason vocabulary (mirrors StructurePosition.exit_reason and the
# options dashboard's trade log)
# ---------------------------------------------------------------------------

#: A human closed it (dashboard button) — never produced by this policy.
EXIT_MANUAL = "manual"
#: The strategy's view turned against the open structure.
EXIT_SIGNAL_FLIP = "signal_flip"
#: The strategy went quiet (no view / neutral) for ``neutral_bars`` bars.
EXIT_SIGNAL_NEUTRAL = "signal_neutral"
#: Structure mark hit the stop.
EXIT_STOP_LOSS = "stop_loss"
#: Structure mark hit the target.
EXIT_TAKE_PROFIT = "take_profit"
#: The trade's P&L breached the per-trade risk cap (`max_loss_inr`).
EXIT_RISK_CAP = "risk_cap"
#: Held for ``max_bars`` bars.
EXIT_TIME_STOP = "time_stop"
#: The structure mark hit a stop the OPERATOR set from the dashboard
#: (Live Order Management). Never produced by the config policy — the bridge
#: owns it, because a human's level beats the config's.
EXIT_MANUAL_STOP = "manual_stop_loss"
#: The structure mark hit an operator-set target.
EXIT_MANUAL_TARGET = "manual_target"
#: Squared off ``min_days_to_expiry`` before expiry (the established
#: pre-expiry market-close reason — see ``options/expiry.py``).
EXIT_DTE = "auto_square_off"

#: Reasons that are purely mechanical (no view needed) — a pool runner that
#: never routes views to the bridge can still honour these. Operator-set
#: levels are mechanical too: they must fire on a viewless bar.
RISK_REASONS = frozenset(
    {
        EXIT_RISK_CAP,
        EXIT_STOP_LOSS,
        EXIT_TAKE_PROFIT,
        EXIT_TIME_STOP,
        EXIT_DTE,
        EXIT_MANUAL_STOP,
        EXIT_MANUAL_TARGET,
    }
)

#: The reasons whose trigger is read off the structure's mark. A stale or
#: failed mark can only produce a wrong answer here, so the bridge suppresses
#: exactly this set while it cannot trust the price — the calendar and
#: time-based tiers keep firing.
PRICE_MEASURED_REASONS = frozenset(
    {EXIT_RISK_CAP, EXIT_STOP_LOSS, EXIT_TAKE_PROFIT}
)

#: Every reason a policy can emit (plus the two operator-set levels the
#: bridge emits directly).
EXIT_REASONS = frozenset(
    {
        EXIT_SIGNAL_FLIP,
        EXIT_SIGNAL_NEUTRAL,
        EXIT_RISK_CAP,
        EXIT_STOP_LOSS,
        EXIT_TAKE_PROFIT,
        EXIT_TIME_STOP,
        EXIT_DTE,
        EXIT_MANUAL_STOP,
        EXIT_MANUAL_TARGET,
    }
)

_KNOWN_KEYS = frozenset(
    {
        "signal_flip",
        "neutral_bars",
        "max_loss_inr",
        "stop_loss_pct",
        "take_profit_pct",
        "stop_loss_points",
        "take_profit_points",
        "max_bars",
        "min_days_to_expiry",
        "reenter",
        "max_reentries_per_day",
    }
)


#: Fee-floor multiplier (BUG-02, QA 2026-10-28). A declared take-profit that
#: clears fewer than ``multiplier`` round-trip fees makes every "win" a net
#: loss — the ±₹100 short-strangle bracket against the ₹193 fee the engine
#: itself charges is the case that motivated this. The bridge refuses such an
#: entry at the pre-trade gate; the multiplier and the wording live here so the
#: economics sit with the exit rules and the check is unit-testable without a
#: broker or a quote feed.
FEE_FLOOR_MULTIPLIER: float = 1.2


def take_profit_fee_floor_violation(
    take_profit_inr: Optional[float],
    round_trip_fees_inr: Optional[float],
    *,
    multiplier: float = FEE_FLOOR_MULTIPLIER,
) -> Optional[str]:
    """Return a refusal reason when the take-profit is inside the round-trip fee.

    ``take_profit_inr`` is the structure's declared favourable bracket in ₹ of
    P&L (``take_profit_points``, or ``take_profit_pct`` × the premium base);
    ``round_trip_fees_inr`` is the engine's own entry + exit fee estimate for
    the same structure. Returns ``None`` when either number is unknown/
    non-positive — the caller must not refuse on a number it cannot state — and
    a human-readable reason when ``take_profit_inr <= multiplier × fees``.

    This is pure: it prices nothing and fabricates no market data. The caller
    passes a fee estimate it derived from real quotes and the fee calculator.
    """
    if take_profit_inr is None or round_trip_fees_inr is None:
        return None
    try:
        target = float(take_profit_inr)
        fees = float(round_trip_fees_inr)
    except (TypeError, ValueError):
        return None
    if target <= 0 or fees <= 0:
        return None
    floor = fees * float(multiplier)
    if target <= floor:
        return (
            f"take-profit ₹{target:,.2f} clears only ≤ 1.2× the round-trip fee "
            f"₹{fees:,.2f} (floor ₹{floor:,.2f}) — every target would be a net "
            f"loss (BUG-02)"
        )
    return None


def _positive(value: Any, key: str) -> Optional[float]:
    """Coerce to a strictly positive float, warning (not raising) on junk.

    A zero/negative stop or target is meaningless and would fire on every
    bar, so it degrades to "disabled" instead of arming a hair trigger.
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        logger.warning("[exit-policy] %s=%r is not a number — ignored", key, value)
        return None
    if number <= 0:
        logger.warning("[exit-policy] %s=%r must be > 0 — ignored", key, value)
        return None
    return number


def _count(value: Any, key: str, minimum: int = 1) -> Optional[int]:
    """Coerce to an int ``>= minimum``; ``minimum=0`` lets ``0`` mean
    "the boundary itself" (e.g. square off *on* expiry day)."""
    if value is None:
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        logger.warning("[exit-policy] %s=%r is not a number — ignored", key, value)
        return None
    if number < minimum:
        logger.warning(
            "[exit-policy] %s=%r must be >= %d — ignored", key, value, minimum
        )
        return None
    return number


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


@dataclass(frozen=True)
class ExitConfig:
    """When to leave an option structure.

    Defaults are deliberately conservative: close on a view flip (the strategy
    changed its mind) and square off one day before expiry (don't ride into
    settlement). Stops, targets and the risk cap are opt-in — a wrong default
    there would silently cap every trade.
    """

    #: Close when the view flips against the structure's direction.
    signal_flip: bool = True
    #: Close after this many consecutive bars with no view / a neutral view.
    #: ``0`` disables it (hold until another rule fires).
    neutral_bars: int = 0
    #: Stop / target as a fraction of the net premium paid (its max loss).
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    #: Stop / target in absolute structure P&L (₹).
    stop_loss_points: Optional[float] = None
    take_profit_points: Optional[float] = None
    #: Per-trade risk cap in ₹ of structure P&L. The platform's hard ceiling:
    #: it is checked before the calendar tier and before any strategy stop, so
    #: a mis-set stop can never widen the worst loss above this.
    max_loss_inr: Optional[float] = None
    #: Time stop — maximum bars in the trade.
    max_bars: Optional[int] = None
    #: Square off this many days before expiry (``None`` rides to settlement).
    min_days_to_expiry: Optional[int] = 1
    #: After a flip-triggered close, immediately open the opposite structure.
    reenter: bool = False
    #: Flip re-entries allowed per day. Only meaningful with ``reenter`` —
    #: same-bar re-entry is impossible regardless (churn finding, U2.2).
    max_reentries_per_day: int = 2

    @classmethod
    def from_expression(cls, expression: Any) -> "ExitConfig":
        """Build from ``expression["exit"]``; tolerant of junk by design.

        LEGACY-FENCED (Phase 4): the trading path never calls this — the
        bridge parses exit plans strictly (``parse_exit_plan_strict``)
        and refuses junk at construction. This tolerant parser survives
        only for loading legacy-shaped data outside the trading path
        (state files and tactical-eval of pre-v2 configs). Do not wire it
        back into a spawn or trading path.
        """
        if expression is None:
            return cls()
        if not isinstance(expression, dict):
            logger.warning(
                "[exit-policy] expression['exit'] must be a dict, got %r — using defaults",
                type(expression).__name__,
            )
            return cls()

        unknown = set(expression) - _KNOWN_KEYS
        if unknown:
            logger.warning(
                "[exit-policy] unknown exit keys ignored: %s", ", ".join(sorted(unknown))
            )

        defaults = cls()
        return cls(
            signal_flip=_as_bool(expression.get("signal_flip"), defaults.signal_flip),
            neutral_bars=_count(expression.get("neutral_bars"), "neutral_bars", 0) or 0,
            max_loss_inr=_positive(expression.get("max_loss_inr"), "max_loss_inr"),
            stop_loss_pct=_positive(expression.get("stop_loss_pct"), "stop_loss_pct"),
            take_profit_pct=_positive(expression.get("take_profit_pct"), "take_profit_pct"),
            stop_loss_points=_positive(
                expression.get("stop_loss_points"), "stop_loss_points"
            ),
            take_profit_points=_positive(
                expression.get("take_profit_points"), "take_profit_points"
            ),
            max_bars=_count(expression.get("max_bars"), "max_bars", 1),
            # ``min_days_to_expiry`` has a non-None default, so an *omitted*
            # key must keep it (1 day). Only an explicit ``null`` means "ride
            # into settlement" — otherwise adding any other exit key would
            # silently disable the square-off.
            min_days_to_expiry=(
                defaults.min_days_to_expiry
                if "min_days_to_expiry" not in expression
                else _count(expression.get("min_days_to_expiry"), "min_days_to_expiry", 0)
            ),
            reenter=_as_bool(expression.get("reenter"), defaults.reenter),
            # ``0`` is a real setting (never re-enter), so an omitted key keeps
            # the default while an explicit 0 must not read as "disabled".
            max_reentries_per_day=(
                defaults.max_reentries_per_day
                if "max_reentries_per_day" not in expression
                else _count(
                    expression.get("max_reentries_per_day"),
                    "max_reentries_per_day",
                    0,
                )
                or 0
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialisable form (state payloads / UI)."""
        return {
            "signal_flip": self.signal_flip,
            "neutral_bars": self.neutral_bars,
            "max_loss_inr": self.max_loss_inr,
            "stop_loss_pct": self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
            "stop_loss_points": self.stop_loss_points,
            "take_profit_points": self.take_profit_points,
            "max_bars": self.max_bars,
            "min_days_to_expiry": self.min_days_to_expiry,
            "reenter": self.reenter,
            "max_reentries_per_day": self.max_reentries_per_day,
        }


@dataclass(frozen=True)
class ExitDecision:
    """A verdict from :meth:`ExitPolicy.evaluate`."""

    reason: str
    detail: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return True


@dataclass
class ExitPolicy:
    """Stateless rule evaluator (the bridge owns all counters)."""

    config: ExitConfig = field(default_factory=ExitConfig)

    def should_reenter(self, closed_direction: Any, view: Any) -> bool:
        """Whether a flip-close should immediately open the reverse structure.

        Requires ``reenter`` **and** a genuinely directional view that differs
        from the structure just closed — otherwise a flip out of a bullish
        spread would re-open another bullish one.
        """
        if not self.config.reenter:
            return False
        if view is None:
            return False
        new_direction = getattr(view, "direction", None)
        if new_direction is None or _is_neutral(new_direction):
            return False
        return closed_direction is None or new_direction != closed_direction

    def evaluate(
        self,
        *,
        view: Any = None,
        structure_direction: Any = None,
        unrealized_pnl: Decimal = Decimal("0"),
        basis: Decimal = Decimal("0"),
        bars_held: int = 0,
        bars_without_view: int = 0,
        bar_date: Optional[date] = None,
        expiry: Optional[date] = None,
    ) -> Optional[ExitDecision]:
        """Return the exit decision for one bar, or ``None`` to keep holding."""
        pnl = Decimal(str(unrealized_pnl or 0))
        premium = Decimal(str(basis or 0))
        cfg = self.config

        # -- risk first: these fire regardless of what the strategy thinks ----
        decision = self._risk_decision(pnl, premium, bars_held, bar_date, expiry)
        if decision is not None:
            return decision

        # -- then opinion: the strategy changed its mind / went quiet --------
        if view is None:
            return self._neutral_decision(bars_without_view)

        direction = getattr(view, "direction", None)
        if _is_neutral(direction):
            return self._neutral_decision(bars_without_view)

        if cfg.signal_flip and structure_direction is not None and direction != structure_direction:
            return ExitDecision(
                EXIT_SIGNAL_FLIP,
                f"view flipped {_label(structure_direction)} → {_label(direction)}",
            )
        return None

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _risk_decision(
        self,
        pnl: Decimal,
        premium: Decimal,
        bars_held: int,
        bar_date: Optional[date],
        expiry: Optional[date],
    ) -> Optional[ExitDecision]:
        cfg = self.config

        # -- the platform's hard ceiling first, whatever the strategy set ------
        if cfg.max_loss_inr is not None and pnl <= -Decimal(str(cfg.max_loss_inr)):
            return ExitDecision(
                EXIT_RISK_CAP, f"loss {pnl:,.0f} ≤ cap -{cfg.max_loss_inr:,.0f}"
            )

        # -- then the scheduled square-off: it does not depend on the mark ----
        if (
            cfg.min_days_to_expiry is not None
            and bar_date is not None
            and expiry is not None
            and (expiry - bar_date).days <= cfg.min_days_to_expiry
        ):
            days = (expiry - bar_date).days
            return ExitDecision(EXIT_DTE, f"{days}d to expiry ≤ {cfg.min_days_to_expiry}d")

        if cfg.stop_loss_points is not None and pnl <= -Decimal(str(cfg.stop_loss_points)):
            return ExitDecision(
                EXIT_STOP_LOSS, f"stop {pnl:,.0f} ≤ -{cfg.stop_loss_points:,.0f}"
            )
        if (
            cfg.stop_loss_pct is not None
            and premium > 0
            and pnl <= -(premium * Decimal(str(cfg.stop_loss_pct)))
        ):
            return ExitDecision(
                EXIT_STOP_LOSS,
                f"stop {pnl:,.0f} ≤ -{cfg.stop_loss_pct:.0%} of premium {premium:,.0f}",
            )

        if cfg.take_profit_points is not None and pnl >= Decimal(str(cfg.take_profit_points)):
            return ExitDecision(
                EXIT_TAKE_PROFIT, f"target {pnl:,.0f} ≥ {cfg.take_profit_points:,.0f}"
            )
        if (
            cfg.take_profit_pct is not None
            and premium > 0
            and pnl >= premium * Decimal(str(cfg.take_profit_pct))
        ):
            return ExitDecision(
                EXIT_TAKE_PROFIT,
                f"target {pnl:,.0f} ≥ {cfg.take_profit_pct:.0%} of premium {premium:,.0f}",
            )

        if cfg.max_bars is not None and bars_held >= cfg.max_bars:
            return ExitDecision(EXIT_TIME_STOP, f"held {bars_held} bars ≥ {cfg.max_bars}")

        return None

    def _neutral_decision(self, bars_without_view: int) -> Optional[ExitDecision]:
        if self.config.neutral_bars and bars_without_view >= self.config.neutral_bars:
            return ExitDecision(
                EXIT_SIGNAL_NEUTRAL,
                f"no view for {bars_without_view} bars ≥ {self.config.neutral_bars}",
            )
        return None


def _is_neutral(direction: Any) -> bool:
    """True for ``None`` and for the NEUTRAL member of the Direction enum."""
    if direction is None:
        return True
    return str(getattr(direction, "value", direction)).lower() == "neutral"


def _label(direction: Any) -> str:
    return str(getattr(direction, "value", direction))
