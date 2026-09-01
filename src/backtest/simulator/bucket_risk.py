"""Per-bucket risk limits (ticket #9) — canonical map keyed on ``_classify()``.

The ``(mode, source)`` taxonomy (tickets P1.1/T4) gets its accounting teeth:
risk limits resolve from the SAME classification that labels the run
(portfolio, state v3, DB rows), never from a hardcoded global knob.

* The map is the SINGLE source of truth for bucket defaults. Call sites
  IMPORT (T3 pattern); no bucket limit literal is re-declared anywhere.
* ``mode`` keys the exposure/size caps: ``paper`` is free play (permissive —
  the PAPER_FREE_PROFILE intent), ``live`` is real capital (explicit, tight).
* ``source`` gates WHAT can be traded: a live bucket refuses ``synthetic``
  and ``replay`` (fake data trading real money is refused outright). A paper
  bucket accepts any source — paper risk is free play regardless of data
  trust.
* The engine config can OVERRIDE a bucket's limits per field
  (``risk.buckets.<bucket>``); the resolver merges over the canonical
  defaults so nothing is re-declared at call sites.

Layering: this module imports only ``backtest.data.source_tags`` (the
canonical source vocabulary) plus its own simulator siblings — no
``backtest.forward`` / ``backtest.engine`` imports, so both the forward
engine and the canonical backtest runner can use it without a cycle.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, Mapping, Optional

from backtest.data.source_tags import SOURCE_TAG_VALUES
from backtest.simulator.errors import ValidationError
from backtest.simulator.money import ONE, ZERO, money
from backtest.simulator.money import price as to_price
from backtest.simulator.money import to_decimal

logger = logging.getLogger("backtest.simulator.bucket_risk")

__all__ = [
    "PAPER_BUCKET",
    "LIVE_BUCKET",
    "BucketRiskLimits",
    "BUCKET_RISK_LIMITS",
    "BUCKET_RISK_FIELDS",
    "resolve_bucket_risk",
]

#: Bucket keys — the only two values ``_classify`` can produce.
PAPER_BUCKET = "paper"
LIVE_BUCKET = "live"


@dataclass
class BucketRiskLimits:
    """Exposure/size limits for one classification bucket.

    Every numeric limit is optional (``None`` disables it) so a bucket can
    be permissive or tight. ``max_leverage`` is a minimum-1 multiplier for
    buying power. ``allowed_sources`` is the source GATE for the bucket: a
    run whose ``source`` is not in the set is refused before it trades.
    """

    max_position_value: Optional[Decimal] = None
    max_position_pct: Optional[Decimal] = None
    max_gross_exposure_pct: Optional[Decimal] = None
    max_open_positions: Optional[int] = None
    min_trade_value: Optional[Decimal] = None
    max_leverage: Decimal = Decimal("1")
    allowed_sources: frozenset = field(default_factory=lambda: frozenset(SOURCE_TAG_VALUES))

    def __post_init__(self) -> None:
        for name in (
            "max_position_value",
            "max_position_pct",
            "max_gross_exposure_pct",
            "min_trade_value",
        ):
            value = getattr(self, name)
            if value is not None:
                dec = to_decimal(value, name)
                if dec <= ZERO:
                    raise ValidationError(
                        f"{name} must be positive when set",
                        code="invalid_bucket_limit",
                        limit=name,
                    )
                setattr(self, name, dec)

        self.max_leverage = to_decimal(self.max_leverage, "max_leverage")
        if self.max_leverage < ONE:
            raise ValidationError(
                "max_leverage must be >= 1",
                code="invalid_bucket_limit",
                limit="max_leverage",
            )

        if self.max_open_positions is not None:
            try:
                self.max_open_positions = int(self.max_open_positions)
            except (TypeError, ValueError):
                raise ValidationError(
                    "max_open_positions must be an integer",
                    code="invalid_bucket_limit",
                    limit="max_open_positions",
                ) from None
            if self.max_open_positions < 1:
                raise ValidationError(
                    "max_open_positions must be >= 1 when set",
                    code="invalid_bucket_limit",
                    limit="max_open_positions",
                )

        allowed = {str(s).strip().lower() for s in self.allowed_sources}
        unknown = allowed - set(SOURCE_TAG_VALUES)
        if unknown:
            raise ValidationError(
                f"unknown source tag(s) in bucket allowed_sources: {sorted(unknown)}",
                code="invalid_bucket_limit",
                limit="allowed_sources",
            )
        self.allowed_sources = frozenset(allowed)

    def to_dict(self) -> Dict[str, Any]:
        def _s(v: Optional[Decimal]) -> Optional[str]:
            return str(v) if v is not None else None

        return {
            "max_position_value": _s(self.max_position_value),
            "max_position_pct": _s(self.max_position_pct),
            "max_gross_exposure_pct": _s(self.max_gross_exposure_pct),
            "max_open_positions": self.max_open_positions,
            "min_trade_value": _s(self.min_trade_value),
            "max_leverage": str(self.max_leverage),
            "allowed_sources": sorted(self.allowed_sources),
        }

    # -- canonical adapters ------------------------------------------------

    def to_sizing_constraints(self) -> "Any":
        """The sizer constraints that cap entries at bucket size."""
        from backtest.simulator.position_sizing import SizingConstraints

        return SizingConstraints(
            max_position_value=self.max_position_value,
            max_position_pct=self.max_position_pct,
            max_gross_exposure_pct=self.max_gross_exposure_pct,
            min_trade_value=self.min_trade_value,
            max_leverage=self.max_leverage,
            max_open_positions=self.max_open_positions,
        )

    def to_portfolio_limits(self, allow_short: bool = False) -> "Any":
        """The per-trade limits enforced by ``Portfolio.can_open_position``."""
        from backtest.simulator.portfolio import PortfolioLimits

        return PortfolioLimits(
            allow_short=bool(allow_short),
            max_open_positions=self.max_open_positions,
            max_position_value=self.max_position_value,
            max_position_pct=self.max_position_pct,
            max_gross_exposure_pct=self.max_gross_exposure_pct,
            max_leverage=self.max_leverage,
            min_trade_value=self.min_trade_value,
        )

    def to_risk_config(
        self,
        max_drawdown_pct: Any = Decimal("0.10"),
        daily_loss_limit_pct: Any = Decimal("0.02"),
    ) -> "Any":
        """The pre-trade risk-manager config for this bucket.

        Drawdown / daily-loss limits stay CONFIG-level (already explicit on
        the engine); the bucket owns the exposure/size caps and leverage.
        """
        from backtest.simulator.risk_manager import RiskConfig

        return RiskConfig(
            max_position_value=self.max_position_value,
            max_position_pct=self.max_position_pct,
            max_open_positions=self.max_open_positions,
            max_gross_exposure_pct=self.max_gross_exposure_pct,
            max_leverage=self.max_leverage,
            min_order_value=self.min_trade_value,
            max_drawdown_pct=max_drawdown_pct,
            daily_loss_limit_pct=daily_loss_limit_pct,
        )

    def check_exposure(self, portfolio: Any) -> Optional[str]:
        """Check the OPEN book of ``portfolio`` against the bucket caps.

        Returns a human-readable reason string if ANY cap is already
        violated, else ``None``. Duck-typed (``positions``,
        ``calculate_total_equity``, ``calculate_gross_exposure``) so unit
        tests can use a stub and the engine can use the simulator Portfolio.

        This is the risk teeth behind the T8 no-downgrade guard: a run whose
        classification changed is REFUSED here instead of silently trading
        at the wrong size.
        """
        positions = getattr(portfolio, "positions", None) or {}
        reasons = []

        try:
            equity = portfolio.calculate_total_equity()
            equity = money(equity) if equity is not None else ZERO
        except Exception:
            equity = ZERO
            logger.debug("bucket exposure check: equity unavailable", exc_info=True)

        try:
            gross = portfolio.calculate_gross_exposure()
            gross = money(gross) if gross is not None else None
        except Exception:
            gross = None
            logger.debug("bucket exposure check: gross exposure unavailable", exc_info=True)

        if self.max_open_positions is not None and len(positions) > self.max_open_positions:
            reasons.append(
                f"{len(positions)} open positions > bucket max {self.max_open_positions}"
            )

        for symbol, pos in positions.items():
            qty = abs(getattr(pos, "quantity", 0) or 0)
            price = getattr(pos, "current_price", None) or getattr(pos, "average_entry_price", None)
            if qty is None or price is None:
                continue
            try:
                notional = to_price(qty, "quantity") * to_decimal(price, "price")
            except Exception:
                continue
            if self.max_position_value is not None and notional > self.max_position_value:
                reasons.append(
                    f"{symbol} notional {notional} > bucket max_position_value "
                    f"{self.max_position_value}"
                )
            if self.max_position_pct is not None and equity > ZERO:
                pct = notional / equity
                if pct > self.max_position_pct:
                    reasons.append(
                        f"{symbol} {pct:.2%} of equity > bucket max_position_pct "
                        f"{self.max_position_pct:.2%}"
                    )

        if self.max_gross_exposure_pct is not None and equity > ZERO and gross is not None:
            gross_pct = gross / equity
            if gross_pct > self.max_gross_exposure_pct:
                reasons.append(
                    f"gross exposure {gross_pct:.2%} > bucket "
                    f"max_gross_exposure_pct {self.max_gross_exposure_pct:.2%}"
                )

        return "; ".join(reasons) if reasons else None


#: Canonical per-bucket defaults. ``paper`` is free play (no exposure caps —
#: matches the PAPER_FREE_PROFILE intent and the pre-ticket behavior); ``live``
#: is real capital: explicit, tight, and gated to real broker data only.
BUCKET_RISK_LIMITS: Dict[str, BucketRiskLimits] = {
    PAPER_BUCKET: BucketRiskLimits(
        max_position_value=None,
        max_position_pct=None,
        max_gross_exposure_pct=None,
        max_open_positions=None,
        min_trade_value=None,
        max_leverage=Decimal("1"),
        allowed_sources=frozenset(SOURCE_TAG_VALUES),
    ),
    LIVE_BUCKET: BucketRiskLimits(
        max_position_value=money("10000"),
        max_position_pct=Decimal("0.10"),
        max_gross_exposure_pct=Decimal("0.50"),
        max_open_positions=5,
        min_trade_value=money("1000"),
        max_leverage=Decimal("1"),
        #: Real fills need real data; synthetic/replay feeding live fills is
        #: the "trained on fake data, trading real money" hazard.
        allowed_sources=frozenset({"mstock"}),
    ),
}

#: Public override fields (for config ``risk.buckets.<bucket>`` validation).
BUCKET_RISK_FIELDS: frozenset = frozenset(f.name for f in dataclasses.fields(BucketRiskLimits))


def resolve_bucket_risk(
    mode: Any,
    source: Any,
    overrides: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> tuple[str, BucketRiskLimits]:
    """Resolve ``(bucket_key, limits)`` for a run's classification.

    ``mode``/``source`` are ``_classify``'s output (paper|live ×
    synthetic|replay|mstock). ``overrides`` is the engine config's
    ``risk.buckets`` mapping (bucket -> field -> value); it is merged over
    the canonical defaults, so a config can explicitly override a bucket's
    limit without re-declaring the rest.

    Raises :class:`ValidationError` for an unknown mode, an unknown override
    bucket/field, or a run whose ``source`` is not allowed for the bucket
    (e.g. live/synthetic).
    """
    mode = str(mode or "").strip().lower()
    source = str(source or "").strip().lower()

    if mode not in BUCKET_RISK_LIMITS:
        raise ValidationError(
            f"unknown risk bucket {mode!r} (expected paper|live)",
            code="unknown_bucket",
            bucket=mode,
        )

    base = BUCKET_RISK_LIMITS[mode]
    if source not in base.allowed_sources:
        raise ValidationError(
            f"bucket {mode!r} refuses source {source!r} "
            f"(allowed: {sorted(base.allowed_sources)}) — "
            f"a {mode} run must not trade on {source} data",
            code="source_not_allowed_for_bucket",
            bucket=mode,
            source=source,
        )

    bucket_overrides = {}
    if overrides:
        unknown_buckets = set(overrides) - set(BUCKET_RISK_LIMITS)
        if unknown_buckets:
            raise ValidationError(
                f"unknown risk bucket override(s): {sorted(unknown_buckets)} "
                f"(expected paper|live)",
                code="unknown_bucket_override",
            )
        bucket_overrides = overrides.get(mode) or {}

    if bucket_overrides:
        unknown_fields = set(bucket_overrides) - BUCKET_RISK_FIELDS
        if unknown_fields:
            raise ValidationError(
                f"unknown risk limit override(s) for bucket {mode!r}: " f"{sorted(unknown_fields)}",
                code="unknown_limit_override",
            )
        merged = {f.name: getattr(base, f.name) for f in dataclasses.fields(BucketRiskLimits)}
        merged.update(dict(bucket_overrides))
        limits = BucketRiskLimits(**merged)
        logger.info(
            "bucket risk %s/%s: canonical defaults overridden (%s)",
            mode,
            source,
            ", ".join(sorted(bucket_overrides)),
        )
    else:
        limits = base

    return mode, limits
