"""Commission models for the forward testing simulator.

Introduced in Step 6 so :class:`~backtest.simulator.fill.Fill` can price its
own costs. Step 8 extends this with the full NSE fee stack (STT, stamp duty,
SEBI turnover fee, GST); everything here is the broker-agnostic base.

Why this is a strategy object rather than a number
--------------------------------------------------
Commission structure changes which strategies are viable. A per-share model
punishes small-priced, high-quantity trades; a percentage model punishes large
notionals; a flat model punishes frequent small trades. Making the model
pluggable means a backtest can be re-costed against a different broker without
touching strategy code — and the Step 22 comparison tool can quantify exactly
how much of a live/backtest divergence is fees.

All models return a **non-negative** commission. A rebate would be a negative
fee, which the schema's ``ck_fills_fees_nonneg`` constraint forbids; model
maker rebates in Step 8's fee stack instead.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Sequence

from backtest.simulator.enums import OrderSide
from backtest.simulator.errors import ValidationError
from backtest.simulator.money import ZERO, money, price as to_price, to_decimal

__all__ = [
    "CommissionModel",
    "ZeroCommission",
    "FlatCommission",
    "PerShareCommission",
    "PercentageCommission",
    "TieredCommission",
    "PaymentForOrderFlowCommission",
    "resolve_commission_model",
]


class CommissionModel(ABC):
    """Base class for commission calculation.

    Subclasses implement :meth:`calculate`. Instances must be stateless and
    reusable across fills — the Step 20 engine holds one for the whole run.
    """

    name: str = "commission"

    @abstractmethod
    def calculate(self, quantity: Any, price: Any, side: Any = OrderSide.BUY) -> Decimal:
        """Commission for one execution, as a non-negative amount."""

    def _clamp(
        self,
        amount: Decimal,
        minimum: Decimal | None,
        maximum: Decimal | None,
    ) -> Decimal:
        """Apply per-trade floor and cap, then guarantee non-negativity."""
        if minimum is not None:
            amount = max(amount, minimum)
        if maximum is not None:
            amount = min(amount, maximum)
        return money(max(amount, ZERO))

    @staticmethod
    def _inputs(quantity: Any, price: Any) -> tuple[Decimal, Decimal]:
        qty = abs(to_price(quantity, "quantity"))
        px = to_price(price, "price")
        if px < ZERO:
            raise ValidationError("price must not be negative", code="invalid_price")
        return qty, px

    def to_dict(self) -> dict[str, Any]:
        return {"model": self.name}

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<{type(self).__name__}>"


class ZeroCommission(CommissionModel):
    """No commission. Useful for isolating slippage in an A/B comparison."""

    name = "zero"

    def calculate(self, quantity: Any, price: Any, side: Any = OrderSide.BUY) -> Decimal:
        return ZERO


@dataclass
class FlatCommission(CommissionModel):
    """A fixed charge per execution, regardless of size."""

    per_trade: Decimal = Decimal("20")
    name: str = field(default="flat", init=False)

    def __post_init__(self) -> None:
        self.per_trade = money(self.per_trade, "per_trade")
        if self.per_trade < ZERO:
            raise ValidationError(
                "per_trade must not be negative", code="invalid_commission_config"
            )

    def calculate(self, quantity: Any, price: Any, side: Any = OrderSide.BUY) -> Decimal:
        self._inputs(quantity, price)
        return self.per_trade

    def to_dict(self) -> dict[str, Any]:
        return {"model": self.name, "per_trade": str(self.per_trade)}


@dataclass
class PerShareCommission(CommissionModel):
    """Charge proportional to share count, with an optional floor and cap.

    The Interactive Brokers style. Punishes low-priced, high-quantity trades:
    100,000 shares at ₹2 costs the same as 100,000 shares at ₹2,000.
    """

    per_share: Decimal = Decimal("0.005")
    minimum: Decimal | None = Decimal("1")
    maximum: Decimal | None = None
    name: str = field(default="per_share", init=False)

    def __post_init__(self) -> None:
        self.per_share = to_decimal(self.per_share, "per_share")
        if self.per_share < ZERO:
            raise ValidationError(
                "per_share must not be negative", code="invalid_commission_config"
            )
        if self.minimum is not None:
            self.minimum = money(self.minimum, "minimum")
        if self.maximum is not None:
            self.maximum = money(self.maximum, "maximum")
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.maximum < self.minimum
        ):
            raise ValidationError(
                "maximum must be >= minimum", code="invalid_commission_config"
            )

    def calculate(self, quantity: Any, price: Any, side: Any = OrderSide.BUY) -> Decimal:
        qty, _ = self._inputs(quantity, price)
        return self._clamp(qty * self.per_share, self.minimum, self.maximum)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.name,
            "per_share": str(self.per_share),
            "minimum": str(self.minimum) if self.minimum is not None else None,
            "maximum": str(self.maximum) if self.maximum is not None else None,
        }


@dataclass
class PercentageCommission(CommissionModel):
    """Charge as a fraction of traded value.

    The Indian discount-broker default. ``rate`` is fractional: ``0.0003`` is
    0.03%, matching the backtest engine's default commission. ``maximum``
    models the common "₹20 or 0.03%, whichever is lower" cap.
    """

    rate: Decimal = Decimal("0.0003")
    minimum: Decimal | None = None
    maximum: Decimal | None = Decimal("20")
    name: str = field(default="percentage", init=False)

    def __post_init__(self) -> None:
        self.rate = to_decimal(self.rate, "rate")
        if self.rate < ZERO:
            raise ValidationError(
                "rate must not be negative", code="invalid_commission_config"
            )
        if self.rate > Decimal("1"):
            # 100% commission is always a units mistake — 0.03 meaning "0.03%"
            # rather than 3%. Catch it here instead of after a confusing run.
            raise ValidationError(
                "rate is fractional, not a percentage: use 0.0003 for 0.03%",
                code="invalid_commission_config",
                rate=str(self.rate),
            )
        if self.minimum is not None:
            self.minimum = money(self.minimum, "minimum")
        if self.maximum is not None:
            self.maximum = money(self.maximum, "maximum")

    def calculate(self, quantity: Any, price: Any, side: Any = OrderSide.BUY) -> Decimal:
        qty, px = self._inputs(quantity, price)
        return self._clamp(qty * px * self.rate, self.minimum, self.maximum)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.name,
            "rate": str(self.rate),
            "minimum": str(self.minimum) if self.minimum is not None else None,
            "maximum": str(self.maximum) if self.maximum is not None else None,
        }


@dataclass
class TieredCommission(CommissionModel):
    """Rate selected by trade value, cheaper as size grows.

    ``tiers`` is a sequence of ``(threshold, rate)`` pairs. The applicable rate
    is the one with the **highest threshold not exceeding** the trade value, so
    the first tier must start at zero.

    This is a *selected-rate* model (the whole trade is charged at one rate),
    not a *marginal* model. Retail brokers overwhelmingly use the former;
    document loudly if you ever switch, because the numbers differ.

    Examples
    --------
    >>> model = TieredCommission([(0, Decimal("0.0005")),
    ...                           (100000, Decimal("0.0003")),
    ...                           (1000000, Decimal("0.0001"))])
    >>> model.calculate(100, 500)          # 50,000 -> tier 0
    Decimal('25.0000')
    """

    tiers: Sequence[tuple[Any, Any]] = ()
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    name: str = field(default="tiered", init=False)

    def __post_init__(self) -> None:
        if not self.tiers:
            raise ValidationError(
                "tiered commission requires at least one tier",
                code="invalid_commission_config",
            )
        parsed = [
            (to_decimal(threshold, "tier threshold"), to_decimal(rate, "tier rate"))
            for threshold, rate in self.tiers
        ]
        parsed.sort(key=lambda pair: pair[0])

        if parsed[0][0] != ZERO:
            raise ValidationError(
                "the first tier threshold must be 0 so every trade matches a tier",
                code="invalid_commission_config",
                first_threshold=str(parsed[0][0]),
            )
        for threshold, rate in parsed:
            if threshold < ZERO:
                raise ValidationError(
                    "tier thresholds must not be negative",
                    code="invalid_commission_config",
                )
            if rate < ZERO or rate > Decimal("1"):
                raise ValidationError(
                    "tier rates are fractional and must be between 0 and 1",
                    code="invalid_commission_config",
                    rate=str(rate),
                )
        if len({threshold for threshold, _ in parsed}) != len(parsed):
            raise ValidationError(
                "tier thresholds must be unique", code="invalid_commission_config"
            )

        self.tiers = parsed
        if self.minimum is not None:
            self.minimum = money(self.minimum, "minimum")
        if self.maximum is not None:
            self.maximum = money(self.maximum, "maximum")

    def rate_for(self, trade_value: Any) -> Decimal:
        """The rate that applies to ``trade_value``."""
        value = abs(to_decimal(trade_value, "trade_value"))
        applicable = self.tiers[0][1]
        for threshold, rate in self.tiers:
            if value >= threshold:
                applicable = rate
            else:
                break
        return applicable

    def calculate(self, quantity: Any, price: Any, side: Any = OrderSide.BUY) -> Decimal:
        qty, px = self._inputs(quantity, price)
        value = qty * px
        return self._clamp(value * self.rate_for(value), self.minimum, self.maximum)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.name,
            "tiers": [[str(t), str(r)] for t, r in self.tiers],
            "minimum": str(self.minimum) if self.minimum is not None else None,
            "maximum": str(self.maximum) if self.maximum is not None else None,
        }


#: Name -> constructor, for building a model from configuration.
@dataclass
class PaymentForOrderFlowCommission(CommissionModel):
    """Zero commission, funded by payment for order flow.

    The Robinhood model. Commission really is zero — but the cost does not
    vanish, it reappears as systematically worse fills, because the broker is
    paid to route your order to a market maker rather than to the best venue.

    Reporting this as "free" is the single most misleading thing a fee model
    can do. :attr:`implied_slippage_bps` records the cost that *should* be
    charged as slippage instead, so it is visible rather than silently absent.
    Wire it into :mod:`backtest.simulator.slippage` — this class deliberately
    does **not** return it as a fee, because it is not one.
    """

    implied_slippage_bps: Decimal = Decimal("2.5")
    name: str = field(default="pfof", init=False)

    def __post_init__(self) -> None:
        self.implied_slippage_bps = to_decimal(
            self.implied_slippage_bps, "implied_slippage_bps"
        )
        if self.implied_slippage_bps < ZERO:
            raise ValidationError(
                "implied_slippage_bps must not be negative",
                code="invalid_commission_config",
            )

    def calculate(self, quantity: Any, price: Any, side: Any = OrderSide.BUY) -> Decimal:
        self._inputs(quantity, price)
        return ZERO

    def hidden_cost(self, quantity: Any, price: Any) -> Decimal:
        """What the 'free' trade actually costs, via worse fills."""
        qty, px = self._inputs(quantity, price)
        return money(qty * px * self.implied_slippage_bps / Decimal("10000"))

    def to_dict(self) -> dict[str, Any]:
        return {"model": self.name, "implied_slippage_bps": str(self.implied_slippage_bps)}


_REGISTRY: dict[str, type[CommissionModel]] = {
    "zero": ZeroCommission,
    "pfof": PaymentForOrderFlowCommission,
    "flat": FlatCommission,
    "per_share": PerShareCommission,
    "percentage": PercentageCommission,
    "tiered": TieredCommission,
}


def resolve_commission_model(spec: Any) -> CommissionModel:
    """Build a :class:`CommissionModel` from a model, name, dict or number.

    Accepts, in order of preference:

    * a :class:`CommissionModel` instance — returned unchanged
    * ``None`` — :class:`ZeroCommission`
    * a name such as ``"percentage"`` — the model's defaults
    * a dict such as ``{"model": "percentage", "rate": "0.0005"}``
    * a bare number — treated as a flat per-trade charge

    Raises
    ------
    ValidationError
        For an unknown model name or bad keyword arguments.
    """
    if isinstance(spec, CommissionModel):
        return spec
    if spec is None:
        return ZeroCommission()

    if isinstance(spec, dict):
        payload = dict(spec)
        name = str(payload.pop("model", "")).strip().lower()
        if name not in _REGISTRY:
            raise ValidationError(
                f"unknown commission model {name!r}; expected one of {sorted(_REGISTRY)}",
                code="unknown_commission_model",
            )
        payload = {k: v for k, v in payload.items() if v is not None or k in {"minimum", "maximum"}}
        try:
            return _REGISTRY[name](**payload)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ValidationError(
                f"bad configuration for commission model {name!r}: {exc}",
                code="invalid_commission_config",
            ) from exc

    if isinstance(spec, str):
        name = spec.strip().lower()
        if name not in _REGISTRY:
            raise ValidationError(
                f"unknown commission model {name!r}; expected one of {sorted(_REGISTRY)}",
                code="unknown_commission_model",
            )
        if name == "tiered":
            raise ValidationError(
                "the tiered model needs explicit tiers; pass a dict",
                code="invalid_commission_config",
            )
        return _REGISTRY[name]()

    if isinstance(spec, (int, float, Decimal)):
        return FlatCommission(per_trade=spec)

    raise ValidationError(
        f"cannot build a commission model from {type(spec).__name__}",
        code="unknown_commission_model",
    )