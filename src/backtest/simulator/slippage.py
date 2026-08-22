"""Slippage simulation for the forward testing simulator.

Slippage is the gap between the price a strategy *decided* on and the price it
actually got. It is the single largest reason a forward test underperforms its
backtest, and unlike commission it is invisible on any statement — it is
already baked into the fill price. Modelling it explicitly is what makes a
forward test worth running.

Five models
-----------

=====================  ====================================================
:class:`ZeroSlippage`  None. The backtest-comparison baseline.
:class:`FixedBpsSlippage`      A flat basis-point haircut.
:class:`SpreadSlippage`        A fraction of the quoted bid-ask spread.
:class:`VolumeImpactSlippage`  Square-root market impact from participation.
:class:`VolatilitySlippage`    A fraction of ATR.
:class:`HybridSlippage`        Spread + impact + volatility, then multipliers.
=====================  ====================================================

Sign convention
---------------
Slippage is expressed as **adverse basis points** and is always non-negative
by default: a buy executes above the reference price, a sell below it. This
matches :attr:`backtest.simulator.fill.Fill.slippage_bps`, where positive
always means "worse".

Limit orders
------------
A limit order cannot fill worse than its limit — that is the entire point of
one. Every estimate is therefore capped against the order's limit price, and
:attr:`SlippageEstimate.capped` records when that happened. Without this the
simulator would happily report a limit buy filling above its limit, which is
impossible and would flatter nothing but the bug.

Defaults
--------
Tuned for NSE large caps: ~5 bps fixed, half the quoted spread, and a
square-root impact coefficient of 100 bps at 100% participation (so a 1%
participation rate costs ~10 bps). Liquidity tiers and time-of-day multipliers
scale these. Override everything in ``config/slippage.yaml``.
"""

from __future__ import annotations

import logging
import statistics as _stats
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from backtest.simulator.enums import OrderSide, OrderType
from backtest.simulator.errors import ValidationError
from backtest.simulator.money import (
    ZERO,
    price as to_price,
    quantize_money,
    quantize_price,
    to_decimal,
)

if TYPE_CHECKING:  # pragma: no cover
    from backtest.simulator.order import Order

__all__ = [
    "SlippageEstimate",
    "SlippageModel",
    "ZeroSlippage",
    "FixedBpsSlippage",
    "SpreadSlippage",
    "VolumeImpactSlippage",
    "VolatilitySlippage",
    "HybridSlippage",
    "SlippageCalculator",
    "SlippageConfig",
    "LiquidityTier",
    "MarketSnapshot",
    "resolve_slippage_model",
    "load_slippage_config",
    "DEFAULT_SLIPPAGE_CONFIG_PATH",
]

logger = logging.getLogger("backtest.simulator.slippage")

_BPS = Decimal("10000")
_HALF = Decimal("0.5")

DEFAULT_SLIPPAGE_CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "slippage.yaml"
)


class LiquidityTier:
    """Symbol liquidity buckets, coarsest tool for scaling slippage."""

    LARGE_CAP = "large_cap"
    MID_CAP = "mid_cap"
    SMALL_CAP = "small_cap"
    ILLIQUID = "illiquid"

    ALL = (LARGE_CAP, MID_CAP, SMALL_CAP, ILLIQUID)

    #: Multipliers applied to the base estimate. A small cap costs roughly
    #: double a large cap to trade; an illiquid name far more.
    DEFAULT_MULTIPLIERS: dict[str, Decimal] = {
        LARGE_CAP: Decimal("1.0"),
        MID_CAP: Decimal("1.5"),
        SMALL_CAP: Decimal("2.5"),
        ILLIQUID: Decimal("5.0"),
    }


# ---------------------------------------------------------------------------
# Market data normalisation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketSnapshot:
    """The market inputs a slippage model needs, normalised.

    Built from the Step 10 quote dict, or from a bare price. Missing fields
    are ``None`` rather than guessed, so a model can decide whether it has
    enough information to run (see :meth:`SlippageModel.calculate`).
    """

    last: Decimal
    bid: Decimal | None = None
    ask: Decimal | None = None
    volume: Decimal | None = None
    avg_volume: Decimal | None = None
    atr: Decimal | None = None
    timestamp: datetime | None = None

    @property
    def mid(self) -> Decimal:
        """Mid price when both sides are quoted, otherwise the last trade."""
        if self.bid is not None and self.ask is not None:
            return quantize_price((self.bid + self.ask) / 2)
        return self.last

    @property
    def spread(self) -> Decimal | None:
        """Absolute quoted spread, or ``None`` if not two-sided."""
        if self.bid is None or self.ask is None:
            return None
        return quantize_price(self.ask - self.bid)

    @property
    def spread_bps(self) -> Decimal | None:
        """Quoted spread in basis points of the mid."""
        spread = self.spread
        if spread is None or self.mid <= ZERO:
            return None
        return (spread / self.mid * _BPS).quantize(Decimal("0.000001"))

    @classmethod
    def from_market_data(cls, market_data: Mapping[str, Any] | Any) -> "MarketSnapshot":
        """Normalise a quote dict (or bare price) into a snapshot.

        Recognises the Step 10 keys plus common aliases: ``adv`` and
        ``average_volume`` for ``avg_volume``, ``close``/``price`` for
        ``last``.
        """
        if market_data is None:
            raise ValidationError("market data is required", code="missing_market_data")

        if not isinstance(market_data, Mapping):
            last = to_price(market_data, "price")
            return cls(last=last)

        def num(*keys: str) -> Decimal | None:
            for key in keys:
                value = market_data.get(key)
                if value is not None:
                    return to_decimal(value, key)
            return None

        last = num("last", "close", "price")
        bid = num("bid")
        ask = num("ask")
        if last is None:
            last = bid if bid is not None else ask
        if last is None:
            raise ValidationError(
                "market data must contain at least one of last/close/price/bid/ask",
                code="missing_market_data",
            )
        if bid is not None and ask is not None and bid > ask:
            raise ValidationError(
                "crossed quote: bid is above ask",
                code="crossed_quote",
                bid=str(bid),
                ask=str(ask),
            )

        ts = market_data.get("timestamp") or market_data.get("ts")
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)

        return cls(
            last=to_price(last, "last"),
            bid=to_price(bid, "bid") if bid is not None else None,
            ask=to_price(ask, "ask") if ask is not None else None,
            volume=num("volume"),
            avg_volume=num("avg_volume", "average_volume", "adv"),
            atr=num("atr"),
            timestamp=ts,
        )


@dataclass(frozen=True)
class SlippageEstimate:
    """The result of a slippage calculation.

    ``bps`` is adverse basis points; ``executed_price`` is the reference price
    moved against the order by that amount.
    """

    bps: Decimal
    reference_price: Decimal
    executed_price: Decimal
    side: OrderSide
    quantity: Decimal
    model: str = "unknown"
    components: Mapping[str, Decimal] = field(default_factory=dict)
    """Per-factor bps breakdown, for attribution in Step 22."""
    capped: bool = False
    """True when a limit price prevented the full estimate being applied."""
    symbol: str | None = None

    @property
    def per_share(self) -> Decimal:
        """Adverse price move per share."""
        return quantize_price(abs(self.executed_price - self.reference_price))

    @property
    def amount(self) -> Decimal:
        """Total adverse cost across the whole order."""
        return quantize_money(self.per_share * self.quantity)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "model": self.model,
            "side": self.side.value,
            "quantity": str(self.quantity),
            "bps": str(self.bps),
            "reference_price": str(self.reference_price),
            "executed_price": str(self.executed_price),
            "per_share": str(self.per_share),
            "amount": str(self.amount),
            "capped": self.capped,
            "components": {k: str(v) for k, v in self.components.items()},
        }


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class SlippageModel(ABC):
    """Base class. Subclasses return adverse slippage in basis points."""

    name: str = "slippage"

    @abstractmethod
    def bps(
        self,
        snapshot: MarketSnapshot,
        quantity: Decimal,
        side: OrderSide,
        order_type: OrderType = OrderType.MARKET,
    ) -> tuple[Decimal, dict[str, Decimal]]:
        """Adverse basis points, plus a per-component breakdown."""

    def to_dict(self) -> dict[str, Any]:
        return {"model": self.name}

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<{type(self).__name__}>"


class ZeroSlippage(SlippageModel):
    """No slippage — the backtest-comparison baseline.

    Run the same strategy with this and with a realistic model; the difference
    is the execution cost the backtest was hiding.
    """

    name = "zero"

    def bps(self, snapshot, quantity, side, order_type=OrderType.MARKET):
        return ZERO, {}


@dataclass
class FixedBpsSlippage(SlippageModel):
    """A flat haircut in basis points, regardless of conditions.

    Crude but predictable, and a reasonable first approximation for liquid
    names traded in small size. 5 bps is a sane NSE large-cap default.
    """

    bps_value: Decimal = Decimal("5")
    name: str = field(default="fixed", init=False)

    def __post_init__(self) -> None:
        self.bps_value = to_decimal(self.bps_value, "bps_value")
        if self.bps_value < ZERO:
            raise ValidationError(
                "slippage bps must not be negative", code="invalid_slippage_config"
            )

    def bps(self, snapshot, quantity, side, order_type=OrderType.MARKET):
        return self.bps_value, {"fixed": self.bps_value}

    def to_dict(self) -> dict[str, Any]:
        return {"model": self.name, "bps": str(self.bps_value)}


@dataclass
class SpreadSlippage(SlippageModel):
    """A fraction of the quoted bid-ask spread.

    A taker crossing from the mid pays half the spread, so ``0.5`` is the
    theoretical default. Values above that model queue-position losses and
    quote fade.

    Falls back to ``fallback_bps`` when the snapshot is not two-sided — a
    daily-bar feed has no quotes, and silently returning zero there would make
    the model look free.
    """

    spread_fraction: Decimal = Decimal("0.5")
    fallback_bps: Decimal = Decimal("5")
    name: str = field(default="spread", init=False)

    def __post_init__(self) -> None:
        self.spread_fraction = to_decimal(self.spread_fraction, "spread_fraction")
        self.fallback_bps = to_decimal(self.fallback_bps, "fallback_bps")
        if self.spread_fraction < ZERO:
            raise ValidationError(
                "spread_fraction must not be negative", code="invalid_slippage_config"
            )
        if self.fallback_bps < ZERO:
            raise ValidationError(
                "fallback_bps must not be negative", code="invalid_slippage_config"
            )

    def bps(self, snapshot, quantity, side, order_type=OrderType.MARKET):
        spread_bps = snapshot.spread_bps
        if spread_bps is None:
            return self.fallback_bps, {"spread_fallback": self.fallback_bps}
        value = (spread_bps * self.spread_fraction).quantize(Decimal("0.000001"))
        return value, {"spread": value}

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.name,
            "spread_fraction": str(self.spread_fraction),
            "fallback_bps": str(self.fallback_bps),
        }


@dataclass
class VolumeImpactSlippage(SlippageModel):
    """Square-root market impact from participation rate.

    Implements the widely-replicated square-root law::

        impact_bps = coefficient_bps * sqrt(quantity / average_volume)

    ``coefficient_bps`` is the cost of consuming an entire day's volume, so
    the default of 100 means 1% participation costs ~10 bps and 25% costs
    ~50 bps. The square root matters: impact grows *sub*-linearly, so
    doubling size does not double cost — which is exactly why splitting a
    large order helps but only up to a point.

    Returns zero when average volume is unknown, since participation is
    undefined; use the hybrid model if you want a floor.
    """

    coefficient_bps: Decimal = Decimal("100")
    max_participation: Decimal = Decimal("1")
    name: str = field(default="volume", init=False)

    def __post_init__(self) -> None:
        self.coefficient_bps = to_decimal(self.coefficient_bps, "coefficient_bps")
        self.max_participation = to_decimal(self.max_participation, "max_participation")
        if self.coefficient_bps < ZERO:
            raise ValidationError(
                "coefficient_bps must not be negative", code="invalid_slippage_config"
            )
        if self.max_participation <= ZERO:
            raise ValidationError(
                "max_participation must be positive", code="invalid_slippage_config"
            )

    def impact_bps(self, quantity: Any, avg_volume: Any) -> Decimal:
        """Impact for a given size against a given average volume."""
        qty = abs(to_decimal(quantity, "quantity"))
        adv = to_decimal(avg_volume, "avg_volume")
        if adv <= ZERO or qty <= ZERO:
            return ZERO
        participation = min(qty / adv, self.max_participation)
        # Decimal.sqrt keeps the whole pipeline off binary floats.
        return (self.coefficient_bps * participation.sqrt()).quantize(
            Decimal("0.000001")
        )

    def bps(self, snapshot, quantity, side, order_type=OrderType.MARKET):
        if snapshot.avg_volume is None:
            return ZERO, {}
        value = self.impact_bps(quantity, snapshot.avg_volume)
        return value, {"impact": value}

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.name,
            "coefficient_bps": str(self.coefficient_bps),
            "max_participation": str(self.max_participation),
        }


@dataclass
class VolatilitySlippage(SlippageModel):
    """A fraction of ATR, so slippage widens when the market is moving.

    Volatile markets have wider spreads and faster quote fade. ``atr_fraction``
    of 0.1 charges 10% of one ATR unit — around 10 bps for a stock with a 1%
    ATR.

    Optionally scales with participation too, since size hurts more in a fast
    market than a quiet one.
    """

    atr_fraction: Decimal = Decimal("0.1")
    size_scaling: bool = True
    name: str = field(default="volatility", init=False)

    def __post_init__(self) -> None:
        self.atr_fraction = to_decimal(self.atr_fraction, "atr_fraction")
        if self.atr_fraction < ZERO:
            raise ValidationError(
                "atr_fraction must not be negative", code="invalid_slippage_config"
            )

    def volatility_bps(
        self, atr: Any, price: Any, quantity: Any = None, avg_volume: Any = None
    ) -> Decimal:
        """ATR-derived slippage in bps, optionally scaled by participation."""
        atr_value = to_decimal(atr, "atr")
        px = to_decimal(price, "price")
        if atr_value <= ZERO or px <= ZERO:
            return ZERO
        base = atr_value / px * _BPS * self.atr_fraction
        if self.size_scaling and quantity is not None and avg_volume is not None:
            adv = to_decimal(avg_volume, "avg_volume")
            if adv > ZERO:
                participation = min(
                    abs(to_decimal(quantity, "quantity")) / adv, Decimal("1")
                )
                # (1 + sqrt(p)) so a tiny order is unaffected and a full-day
                # order pays double.
                base *= Decimal("1") + participation.sqrt()
        return base.quantize(Decimal("0.000001"))

    def bps(self, snapshot, quantity, side, order_type=OrderType.MARKET):
        if snapshot.atr is None:
            return ZERO, {}
        value = self.volatility_bps(
            snapshot.atr, snapshot.mid, quantity, snapshot.avg_volume
        )
        return value, {"volatility": value}

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.name,
            "atr_fraction": str(self.atr_fraction),
            "size_scaling": self.size_scaling,
        }


@dataclass
class HybridSlippage(SlippageModel):
    """Spread + market impact + volatility, each independently weighted.

    The realistic default. The three components capture different costs —
    crossing the spread, moving the market, and paying for uncertainty — and
    they add rather than substitute. ``floor_bps`` guarantees a minimum so a
    sparse snapshot cannot make execution look free.
    """

    spread: SpreadSlippage = field(default_factory=SpreadSlippage)
    volume: VolumeImpactSlippage = field(default_factory=VolumeImpactSlippage)
    volatility: VolatilitySlippage = field(default_factory=VolatilitySlippage)
    spread_weight: Decimal = Decimal("1")
    volume_weight: Decimal = Decimal("1")
    volatility_weight: Decimal = Decimal("1")
    floor_bps: Decimal = Decimal("1")
    cap_bps: Decimal | None = Decimal("500")
    name: str = field(default="hybrid", init=False)

    def __post_init__(self) -> None:
        for attr in ("spread_weight", "volume_weight", "volatility_weight", "floor_bps"):
            setattr(self, attr, to_decimal(getattr(self, attr), attr))
            if getattr(self, attr) < ZERO:
                raise ValidationError(
                    f"{attr} must not be negative", code="invalid_slippage_config"
                )
        if self.cap_bps is not None:
            self.cap_bps = to_decimal(self.cap_bps, "cap_bps")
            if self.cap_bps < self.floor_bps:
                raise ValidationError(
                    "cap_bps must be >= floor_bps", code="invalid_slippage_config"
                )

    def bps(self, snapshot, quantity, side, order_type=OrderType.MARKET):
        components: dict[str, Decimal] = {}
        total = ZERO

        spread_bps, _ = self.spread.bps(snapshot, quantity, side, order_type)
        weighted = spread_bps * self.spread_weight
        components["spread"] = weighted.quantize(Decimal("0.000001"))
        total += weighted

        impact_bps, _ = self.volume.bps(snapshot, quantity, side, order_type)
        weighted = impact_bps * self.volume_weight
        if weighted:
            components["impact"] = weighted.quantize(Decimal("0.000001"))
        total += weighted

        vol_bps, _ = self.volatility.bps(snapshot, quantity, side, order_type)
        weighted = vol_bps * self.volatility_weight
        if weighted:
            components["volatility"] = weighted.quantize(Decimal("0.000001"))
        total += weighted

        total = max(total, self.floor_bps)
        if self.cap_bps is not None:
            total = min(total, self.cap_bps)
        return total.quantize(Decimal("0.000001")), components

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.name,
            "spread": self.spread.to_dict(),
            "volume": self.volume.to_dict(),
            "volatility": self.volatility.to_dict(),
            "spread_weight": str(self.spread_weight),
            "volume_weight": str(self.volume_weight),
            "volatility_weight": str(self.volatility_weight),
            "floor_bps": str(self.floor_bps),
            "cap_bps": str(self.cap_bps) if self.cap_bps is not None else None,
        }


_MODEL_REGISTRY: dict[str, type[SlippageModel]] = {
    "zero": ZeroSlippage,
    "none": ZeroSlippage,
    "fixed": FixedBpsSlippage,
    "spread": SpreadSlippage,
    "volume": VolumeImpactSlippage,
    "volume_based": VolumeImpactSlippage,
    "volatility": VolatilitySlippage,
    "hybrid": HybridSlippage,
}


def resolve_slippage_model(spec: Any) -> SlippageModel:
    """Build a :class:`SlippageModel` from a model, name, dict or number.

    A bare number is treated as fixed basis points, which makes
    ``resolve_slippage_model(5)`` a convenient shorthand.
    """
    if isinstance(spec, SlippageModel):
        return spec
    if spec is None:
        return ZeroSlippage()

    if isinstance(spec, str):
        name = spec.strip().lower()
        if name not in _MODEL_REGISTRY:
            raise ValidationError(
                f"unknown slippage model {name!r}; "
                f"expected one of {sorted(set(_MODEL_REGISTRY))}",
                code="unknown_slippage_model",
            )
        return _MODEL_REGISTRY[name]()

    if isinstance(spec, Mapping):
        payload = dict(spec)
        name = str(payload.pop("model", "")).strip().lower()
        if name not in _MODEL_REGISTRY:
            raise ValidationError(
                f"unknown slippage model {name!r}; "
                f"expected one of {sorted(set(_MODEL_REGISTRY))}",
                code="unknown_slippage_model",
            )
        cls = _MODEL_REGISTRY[name]
        if cls is HybridSlippage:
            for key in ("spread", "volume", "volatility"):
                if isinstance(payload.get(key), Mapping):
                    payload[key] = resolve_slippage_model(
                        {**payload[key], "model": payload[key].get("model", key)}
                    )
        if cls is FixedBpsSlippage and "bps" in payload:
            payload["bps_value"] = payload.pop("bps")
        try:
            return cls(**payload)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ValidationError(
                f"bad configuration for slippage model {name!r}: {exc}",
                code="invalid_slippage_config",
            ) from exc

    if isinstance(spec, (int, float, Decimal)):
        return FixedBpsSlippage(bps_value=spec)

    raise ValidationError(
        f"cannot build a slippage model from {type(spec).__name__}",
        code="unknown_slippage_model",
    )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class SlippageConfig:
    """Tunables for :class:`SlippageCalculator`.

    Session times default to NSE (09:15–15:30 IST). The open and close
    multipliers reflect the well-documented intraday U-shape in spreads and
    volatility: the first and last half-hour are materially more expensive
    than midday.
    """

    model: SlippageModel = field(default_factory=HybridSlippage)

    session_open: dtime = dtime(9, 15)
    session_close: dtime = dtime(15, 30)
    session_timezone: str = "Asia/Kolkata"

    open_window_minutes: int = 30
    open_multiplier: Decimal = Decimal("2.0")
    close_window_minutes: int = 30
    close_multiplier: Decimal = Decimal("1.5")

    tier_multipliers: dict[str, Decimal] = field(
        default_factory=lambda: dict(LiquidityTier.DEFAULT_MULTIPLIERS)
    )
    symbol_tiers: dict[str, str] = field(default_factory=dict)
    symbol_overrides: dict[str, Decimal] = field(default_factory=dict)
    """Per-symbol multiplier applied last, for names you know are awkward."""

    default_tier: str = LiquidityTier.LARGE_CAP
    max_bps: Decimal = Decimal("1000")
    """Absolute safety ceiling. A 10% haircut is almost certainly a bug."""

    def __post_init__(self) -> None:
        self.model = resolve_slippage_model(self.model)
        for attr in ("open_multiplier", "close_multiplier", "max_bps"):
            setattr(self, attr, to_decimal(getattr(self, attr), attr))
            if getattr(self, attr) < ZERO:
                raise ValidationError(
                    f"{attr} must not be negative", code="invalid_slippage_config"
                )
        if self.open_window_minutes < 0 or self.close_window_minutes < 0:
            raise ValidationError(
                "session windows must not be negative", code="invalid_slippage_config"
            )
        self.tier_multipliers = {
            str(k): to_decimal(v, "tier multiplier")
            for k, v in self.tier_multipliers.items()
        }
        self.symbol_tiers = {
            str(k).strip().upper(): str(v) for k, v in self.symbol_tiers.items()
        }
        self.symbol_overrides = {
            str(k).strip().upper(): to_decimal(v, "symbol override")
            for k, v in self.symbol_overrides.items()
        }
        if self.default_tier not in self.tier_multipliers:
            raise ValidationError(
                f"default_tier {self.default_tier!r} has no multiplier",
                code="invalid_slippage_config",
            )
        for symbol, tier in self.symbol_tiers.items():
            if tier not in self.tier_multipliers:
                raise ValidationError(
                    f"symbol {symbol} maps to unknown tier {tier!r}",
                    code="invalid_slippage_config",
                    known=sorted(self.tier_multipliers),
                )

    def tier_for(self, symbol: str | None) -> str:
        if not symbol:
            return self.default_tier
        return self.symbol_tiers.get(str(symbol).strip().upper(), self.default_tier)

    def tier_multiplier(self, symbol: str | None) -> Decimal:
        return self.tier_multipliers.get(self.tier_for(symbol), Decimal("1"))

    def symbol_multiplier(self, symbol: str | None) -> Decimal:
        if not symbol:
            return Decimal("1")
        return self.symbol_overrides.get(str(symbol).strip().upper(), Decimal("1"))


def _parse_time(value: Any, field_name: str) -> dtime:
    if isinstance(value, dtime):
        return value
    text = str(value).strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise ValidationError(
        f"{field_name} must look like HH:MM, got {value!r}",
        code="invalid_slippage_config",
    )


def load_slippage_config(
    path: str | Path | None = None, profile: str | None = None
) -> SlippageConfig:
    """Load :class:`SlippageConfig` from YAML.

    The file may hold a ``default`` block and named ``profiles`` in the same
    shape as ``config/database.yaml``. A missing default file is not an
    error — built-in defaults are used.
    """
    config_path = Path(path) if path else DEFAULT_SLIPPAGE_CONFIG_PATH
    if path is not None and not config_path.exists():
        raise ValidationError(
            f"slippage config not found: {config_path}", code="config_not_found"
        )
    if not config_path.exists():
        return SlippageConfig()

    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on env
        raise ValidationError(
            f"{config_path} exists but PyYAML is not installed", code="missing_pyyaml"
        ) from exc

    try:
        document = yaml.safe_load(config_path.read_text()) or {}
    except Exception as exc:
        raise ValidationError(
            f"could not parse {config_path}: {exc}", code="invalid_slippage_config"
        ) from exc

    merged: dict[str, Any] = dict(document.get("default") or {})
    profiles = document.get("profiles") or {}
    chosen = profile or document.get("active_profile")
    if chosen:
        if chosen not in profiles:
            raise ValidationError(
                f"unknown slippage profile {chosen!r}; available: {sorted(profiles)}",
                code="unknown_slippage_profile",
            )
        merged.update(profiles[chosen] or {})

    if "session_open" in merged:
        merged["session_open"] = _parse_time(merged["session_open"], "session_open")
    if "session_close" in merged:
        merged["session_close"] = _parse_time(merged["session_close"], "session_close")

    known = {f for f in SlippageConfig.__dataclass_fields__}
    unknown = set(merged) - known
    if unknown:
        raise ValidationError(
            f"unknown slippage config keys: {sorted(unknown)}",
            code="invalid_slippage_config",
        )
    return SlippageConfig(**merged)


# ---------------------------------------------------------------------------
# Calculator
# ---------------------------------------------------------------------------


class SlippageCalculator:
    """Applies a slippage model, with tier, time-of-day and order-type rules.

    Also accumulates every estimate so a run can report what execution
    actually cost — the input to the Step 22 backtest comparison.

    Examples
    --------
    >>> calc = SlippageCalculator(model="fixed")            # doctest: +SKIP
    >>> est = calc.calculate_slippage(order, {"bid": 99, "ask": 101})
    >>> est.executed_price                                   # doctest: +SKIP
    Decimal('101.05050000')
    """

    def __init__(
        self,
        config: SlippageConfig | None = None,
        model: Any = None,
        record: bool = True,
    ) -> None:
        self.config = config or SlippageConfig()
        if model is not None:
            self.config.model = resolve_slippage_model(model)
        self.record = record
        self._history: list[SlippageEstimate] = []

    @classmethod
    def from_config(
        cls, path: str | Path | None = None, profile: str | None = None
    ) -> "SlippageCalculator":
        """Build from ``config/slippage.yaml``."""
        return cls(config=load_slippage_config(path, profile))

    @classmethod
    def disabled(cls) -> "SlippageCalculator":
        """A calculator that applies no slippage — the backtest baseline."""
        return cls(config=SlippageConfig(model=ZeroSlippage()))

    @property
    def model(self) -> SlippageModel:
        return self.config.model

    # -- factor helpers (named by the Step 7 specification) ----------------

    def estimate_market_impact(self, order_size: Any, avg_volume: Any) -> Decimal:
        """Square-root market impact in bps for a given participation."""
        model = self.config.model
        impact = (
            model.volume
            if isinstance(model, HybridSlippage)
            else model
            if isinstance(model, VolumeImpactSlippage)
            else VolumeImpactSlippage()
        )
        return impact.impact_bps(order_size, avg_volume)

    def get_effective_spread(
        self, bid: Any, ask: Any, time_of_day: datetime | None = None
    ) -> Decimal:
        """Quoted spread in bps, scaled by the time-of-day multiplier."""
        bid_d = to_price(bid, "bid")
        ask_d = to_price(ask, "ask")
        if bid_d > ask_d:
            raise ValidationError(
                "crossed quote: bid is above ask", code="crossed_quote"
            )
        mid = (bid_d + ask_d) / 2
        if mid <= ZERO:
            return ZERO
        spread_bps = (ask_d - bid_d) / mid * _BPS
        return (spread_bps * self.time_multiplier(time_of_day)).quantize(
            Decimal("0.000001")
        )

    def calculate_volatility_adjustment(
        self, atr: Any, order_size: Any = None, price: Any = None, avg_volume: Any = None
    ) -> Decimal:
        """ATR-derived slippage in bps."""
        model = self.config.model
        vol = (
            model.volatility
            if isinstance(model, HybridSlippage)
            else model
            if isinstance(model, VolatilitySlippage)
            else VolatilitySlippage()
        )
        return vol.volatility_bps(atr, price or Decimal("1"), order_size, avg_volume)

    def time_multiplier(self, when: datetime | None) -> Decimal:
        """Multiplier for the intraday U-shape in execution cost.

        The first and last window of the session are more expensive: spreads
        are wider at the open while price discovery happens, and at the close
        while everyone rebalances. Outside the session the open multiplier is
        used, since anything trading then is illiquid by definition.
        """
        if when is None:
            return Decimal("1")

        cfg = self.config
        try:
            from zoneinfo import ZoneInfo

            local = (
                when.astimezone(ZoneInfo(cfg.session_timezone))
                if when.tzinfo is not None
                else when
            )
        except Exception:  # pragma: no cover - missing tzdata
            logger.debug("timezone %s unavailable; using naive time", cfg.session_timezone)
            local = when

        minutes = local.hour * 60 + local.minute
        open_min = cfg.session_open.hour * 60 + cfg.session_open.minute
        close_min = cfg.session_close.hour * 60 + cfg.session_close.minute

        if minutes < open_min or minutes > close_min:
            return cfg.open_multiplier
        if minutes < open_min + cfg.open_window_minutes:
            return cfg.open_multiplier
        if minutes > close_min - cfg.close_window_minutes:
            return cfg.close_multiplier
        return Decimal("1")

    # -- main entry point --------------------------------------------------

    def calculate_slippage(
        self,
        order: "Order | None" = None,
        market_data: Mapping[str, Any] | Any = None,
        model_type: Any = None,
        *,
        symbol: str | None = None,
        side: Any = None,
        quantity: Any = None,
        order_type: Any = OrderType.MARKET,
        reference_price: Any = None,
    ) -> SlippageEstimate:
        """Estimate slippage for an order (or explicit parameters).

        Parameters
        ----------
        order:
            An :class:`~backtest.simulator.order.Order`. Its symbol, side,
            quantity and type are used, and a limit price caps the result.
        model_type:
            Override the configured model for this one call — handy for A/B
            comparisons within a single run.

        Returns
        -------
        SlippageEstimate
            Including the executed price and a per-component breakdown.

        Raises
        ------
        ValidationError
            If neither an order nor explicit side/quantity are supplied, or
            the market data is unusable.
        """
        if order is not None:
            symbol = symbol or order.symbol
            side = side if side is not None else order.side
            quantity = quantity if quantity is not None else order.quantity
            order_type = order.order_type
        if side is None or quantity is None:
            raise ValidationError(
                "provide an order, or explicit side and quantity",
                code="missing_order_context",
            )

        side = OrderSide.parse(side)
        order_type = OrderType.parse(order_type)
        qty = abs(to_price(quantity, "quantity"))
        snapshot = MarketSnapshot.from_market_data(market_data)

        reference = (
            to_price(reference_price, "reference_price")
            if reference_price is not None
            else (snapshot.ask if side is OrderSide.BUY else snapshot.bid)
            or snapshot.last
        )
        if reference <= ZERO:
            raise ValidationError(
                "reference price must be positive", code="invalid_price"
            )

        model = (
            resolve_slippage_model(model_type)
            if model_type is not None
            else self.config.model
        )
        base_bps, components = model.bps(snapshot, qty, side, order_type)

        multipliers: dict[str, Decimal] = {}
        time_mult = self.time_multiplier(snapshot.timestamp)
        if time_mult != Decimal("1"):
            multipliers["time_of_day"] = time_mult
        tier_mult = self.config.tier_multiplier(symbol)
        if tier_mult != Decimal("1"):
            multipliers["liquidity_tier"] = tier_mult
        symbol_mult = self.config.symbol_multiplier(symbol)
        if symbol_mult != Decimal("1"):
            multipliers["symbol_override"] = symbol_mult

        total_bps = base_bps * time_mult * tier_mult * symbol_mult
        total_bps = min(max(total_bps, ZERO), self.config.max_bps)
        total_bps = total_bps.quantize(Decimal("0.000001"))

        executed = quantize_price(
            reference * (Decimal("1") + total_bps / _BPS * side.sign)
        )

        # A limit order cannot fill worse than its limit — that is what makes
        # it a limit order. Cap and record it.
        capped = False
        limit = getattr(order, "limit_price", None) if order is not None else None
        if limit is not None and order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT):
            if side is OrderSide.BUY and executed > limit:
                executed, capped = limit, True
            elif side is OrderSide.SELL and executed < limit:
                executed, capped = limit, True
            if capped:
                total_bps = (
                    abs(executed - reference) / reference * _BPS
                ).quantize(Decimal("0.000001"))

        breakdown = {**components, **multipliers}
        estimate = SlippageEstimate(
            bps=total_bps,
            reference_price=reference,
            executed_price=executed,
            side=side,
            quantity=qty,
            model=model.name,
            components=breakdown,
            capped=capped,
            symbol=symbol,
        )

        if self.record:
            self._history.append(estimate)
        logger.debug(
            "slippage %s %s %s: %s bps -> %s (from %s)%s",
            symbol, side, qty, total_bps, executed, reference,
            " [capped at limit]" if capped else "",
        )
        return estimate

    def apply(
        self,
        order: "Order | None" = None,
        market_data: Mapping[str, Any] | Any = None,
        **kwargs: Any,
    ) -> Decimal:
        """Convenience wrapper returning just the executed price."""
        return self.calculate_slippage(order, market_data, **kwargs).executed_price

    # -- statistics --------------------------------------------------------

    @property
    def history(self) -> Sequence[SlippageEstimate]:
        """Every estimate produced, in order."""
        return tuple(self._history)

    def reset(self) -> None:
        """Clear recorded history."""
        self._history.clear()

    def statistics(self) -> dict[str, Any]:
        """Summary of applied slippage across the run.

        Returns counts, mean/median/p95 basis points and total currency cost —
        the numbers Step 22 compares against the backtest.
        """
        if not self._history:
            return {"count": 0}

        bps_values = [float(e.bps) for e in self._history]
        amounts = [e.amount for e in self._history]
        ordered = sorted(bps_values)
        p95_index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))

        by_symbol: dict[str, dict[str, Any]] = {}
        for estimate in self._history:
            key = estimate.symbol or "?"
            bucket = by_symbol.setdefault(key, {"count": 0, "total_amount": ZERO, "bps": []})
            bucket["count"] += 1
            bucket["total_amount"] = quantize_money(bucket["total_amount"] + estimate.amount)
            bucket["bps"].append(float(estimate.bps))
        for bucket in by_symbol.values():
            bucket["mean_bps"] = round(_stats.fmean(bucket.pop("bps")), 4)

        return {
            "count": len(self._history),
            "mean_bps": round(_stats.fmean(bps_values), 4),
            "median_bps": round(_stats.median(bps_values), 4),
            "max_bps": round(max(bps_values), 4),
            "min_bps": round(min(bps_values), 4),
            "p95_bps": round(ordered[p95_index], 4),
            "total_amount": quantize_money(sum(amounts, ZERO)),
            "capped_count": sum(1 for e in self._history if e.capped),
            "by_symbol": by_symbol,
        }

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<SlippageCalculator {self.config.model.name} n={len(self._history)}>"
