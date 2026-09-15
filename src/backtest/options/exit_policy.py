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

Rule order (first match wins — risk before opinion)::

    stop loss → take profit → days-to-expiry → time stop → signal flip → signal neutral

Configuration lives in the runner's expression block::

    instrument = {
        "type": "option",
        "expression": {
            "type": {"BULLISH": "bull_call_spread", "BEARISH": "bear_put_spread"},
            "exit": {
                "signal_flip": true,          # close when the view turns
                "neutral_bars": 3,            # close after N bars with no view
                "stop_loss_pct": 0.5,         # 50% of the net premium
                "take_profit_pct": 1.0,       # +100% of the net premium
                "stop_loss_points": 2000.0,   # ...or absolute ₹ of structure P&L
                "take_profit_points": 3000.0,
                "max_bars": 10,               # time stop, in bars
                "min_days_to_expiry": 1,      # square off the day before expiry
                "reenter": false,             # flip = close only, or close + reverse
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
#: Held for ``max_bars`` bars.
EXIT_TIME_STOP = "time_stop"
#: Squared off ``min_days_to_expiry`` before expiry (the established
#: pre-expiry market-close reason — see ``options/expiry.py``).
EXIT_DTE = "auto_square_off"

#: Reasons that are purely mechanical (no view needed) — a pool runner that
#: never routes views to the bridge can still honour these.
RISK_REASONS = frozenset({EXIT_STOP_LOSS, EXIT_TAKE_PROFIT, EXIT_TIME_STOP, EXIT_DTE})

#: Every reason a policy can emit.
EXIT_REASONS = frozenset(
    {
        EXIT_SIGNAL_FLIP,
        EXIT_SIGNAL_NEUTRAL,
        EXIT_STOP_LOSS,
        EXIT_TAKE_PROFIT,
        EXIT_TIME_STOP,
        EXIT_DTE,
    }
)

_KNOWN_KEYS = frozenset(
    {
        "signal_flip",
        "neutral_bars",
        "stop_loss_pct",
        "take_profit_pct",
        "stop_loss_points",
        "take_profit_points",
        "max_bars",
        "min_days_to_expiry",
        "reenter",
    }
)


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
    settlement). Stops/targets are opt-in — a wrong default there would
    silently cap every trade.
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
    #: Time stop — maximum bars in the trade.
    max_bars: Optional[int] = None
    #: Square off this many days before expiry (``None`` rides to settlement).
    min_days_to_expiry: Optional[int] = 1
    #: After a flip-triggered close, immediately open the opposite structure.
    reenter: bool = False

    @classmethod
    def from_expression(cls, expression: Any) -> "ExitConfig":
        """Build from ``expression["exit"]``; tolerant of junk by design.

        A malformed exit block must never stop a runner from spawning — bad
        keys are warned about and skipped, valid ones still apply.
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
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialisable form (state payloads / UI)."""
        return {
            "signal_flip": self.signal_flip,
            "neutral_bars": self.neutral_bars,
            "stop_loss_pct": self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
            "stop_loss_points": self.stop_loss_points,
            "take_profit_points": self.take_profit_points,
            "max_bars": self.max_bars,
            "min_days_to_expiry": self.min_days_to_expiry,
            "reenter": self.reenter,
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

        if (
            cfg.min_days_to_expiry is not None
            and bar_date is not None
            and expiry is not None
            and (expiry - bar_date).days <= cfg.min_days_to_expiry
        ):
            days = (expiry - bar_date).days
            return ExitDecision(
                EXIT_DTE, f"{days}d to expiry ≤ {cfg.min_days_to_expiry}d"
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
