"""Complete fee calculation for the forward testing simulator.

Step 6 supplied the *brokerage* models. This module adds everything else a
real contract note contains — statutory charges, exchange charges and tax —
and the :class:`CommissionCalculator` that assembles them into one
:class:`FeeBreakdown`.

Why the full stack matters
--------------------------
On Indian equity delivery, brokerage is often **zero** while the statutory
charges are not. A ₹1,00,000 delivery buy at a zero-brokerage broker still
costs around ₹115 in STT, stamp duty, exchange and SEBI charges plus GST.
A simulator that models only brokerage would report that trade as free and
systematically overstate every strategy's return.

Two regulatory regimes
----------------------
The plan document specifies US fees (SEC, FINRA TAF) and US brokers, while
this repository trades NSE through mStock. Both are supported:

* :class:`IndiaEquityFees` — STT, exchange transaction charge, SEBI turnover
  fee, IPFT, stamp duty, GST, DP charges. **The default.**
* :class:`USEquityFees` — SEC Section 31 fee and FINRA TAF, both sell-side.
* :class:`NoStatutoryFees` — brokerage only, for isolating its effect.

Mapping onto the database
-------------------------
``fills`` has three cost columns, so the components are grouped:

===================  ==================================================
``commission``       brokerage
``exchange_fees``    exchange transaction charge, IPFT, DP charges
``regulatory_fees``  STT, SEBI turnover fee, stamp duty, GST, SEC, TAF
===================  ==================================================

GST is grouped with regulatory rather than brokerage because it is a tax
remitted to the government, not broker revenue. The full itemisation always
survives in :attr:`FeeBreakdown.components`.

Rates
-----
Indian rates are FY 2024-25 equity values. **They change**, sometimes
mid-year — verify against a current contract note before trusting a
cost-sensitive result, and override in ``config/brokers.yaml``.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, fields
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from backtest.simulator.commission import (
    CommissionModel,
    FlatCommission,
    PaymentForOrderFlowCommission,
    PercentageCommission,
    PerShareCommission,
    TieredCommission,
    ZeroCommission,
    resolve_commission_model,
)
from backtest.simulator.enums import OrderSide
from backtest.simulator.errors import ValidationError
from backtest.simulator.money import ZERO, money
from backtest.simulator.money import price as to_price
from backtest.simulator.money import to_decimal

if TYPE_CHECKING:  # pragma: no cover
    from backtest.simulator.order import Order

__all__ = [
    "TradeSegment",
    "FeeBreakdown",
    "FeeSchedule",
    "NoStatutoryFees",
    "IndiaEquityFees",
    "USEquityFees",
    "BrokerProfile",
    "PAPER_FREE_PROFILE",
    "CommissionCalculator",
    "CurrencyConverter",
    "BROKER_PRESETS",
    "get_broker_preset",
    "load_broker_profile",
    "DEFAULT_BROKER_CONFIG_PATH",
]

logger = logging.getLogger("backtest.simulator.fees")

#: Contract notes are denominated in paise / cents.
_PAISE = Decimal("0.01")

DEFAULT_BROKER_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "brokers.yaml"


def _round(amount: Decimal) -> Decimal:
    """Round a fee to two places, the way a contract note does."""
    return amount.quantize(_PAISE, rounding=ROUND_HALF_UP)


class TradeSegment:
    """Which product is being traded — it changes the statutory rates.

    On Indian equity this is not a detail: delivery pays STT on **both**
    sides at 0.1%, intraday pays it on the **sell only** at 0.025%. Getting
    the segment wrong misprices a round trip by roughly 8x.
    """

    EQUITY_DELIVERY = "equity_delivery"
    EQUITY_INTRADAY = "equity_intraday"
    FUTURES = "futures"
    OPTIONS = "options"

    ALL = (EQUITY_DELIVERY, EQUITY_INTRADAY, FUTURES, OPTIONS)

    @classmethod
    def validate(cls, segment: Any) -> str:
        value = str(segment).strip().lower()
        if value not in cls.ALL:
            raise ValidationError(
                f"unknown trade segment {segment!r}; expected one of {cls.ALL}",
                code="invalid_segment",
            )
        return value


@dataclass(frozen=True)
class FeeBreakdown:
    """Every charge on one execution, itemised.

    ``components`` keeps the full line-by-line detail; the three summary
    properties group it the way the ``fills`` table stores it.
    """

    components: Mapping[str, Decimal] = field(default_factory=dict)
    currency: str = "INR"
    segment: str = TradeSegment.EQUITY_DELIVERY
    broker: str = "generic"

    #: Components that map to the ``fills.exchange_fees`` column.
    EXCHANGE_KEYS = ("exchange_transaction", "ipft", "dp_charges", "ecn_fee")

    #: Components that map to ``fills.regulatory_fees``.
    REGULATORY_KEYS = ("stt", "sebi_turnover", "stamp_duty", "gst", "sec_fee", "finra_taf")

    def get(self, key: str) -> Decimal:
        return self.components.get(key, ZERO)

    @property
    def brokerage(self) -> Decimal:
        """Broker revenue only — maps to ``fills.commission``."""
        return self.get("brokerage")

    @property
    def exchange_fees(self) -> Decimal:
        return _round(sum((self.get(k) for k in self.EXCHANGE_KEYS), ZERO))

    @property
    def regulatory_fees(self) -> Decimal:
        return _round(sum((self.get(k) for k in self.REGULATORY_KEYS), ZERO))

    @property
    def total(self) -> Decimal:
        """Every charge added together."""
        return _round(sum(self.components.values(), ZERO))

    @property
    def taxes(self) -> Decimal:
        """Statutory taxes only — STT, stamp duty and GST."""
        return _round(sum((self.get(k) for k in ("stt", "stamp_duty", "gst")), ZERO))

    def effective_bps(self, trade_value: Any) -> Decimal:
        """Total cost as basis points of the trade value."""
        value = to_decimal(trade_value, "trade_value")
        if value <= ZERO:
            return ZERO
        return (self.total / value * Decimal("10000")).quantize(Decimal("0.0001"))

    def as_fill_kwargs(self) -> dict[str, Decimal]:
        """The three keyword arguments :class:`~backtest.simulator.fill.Fill` takes."""
        return {
            "commission": self.brokerage,
            "exchange_fees": self.exchange_fees,
            "regulatory_fees": self.regulatory_fees,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "broker": self.broker,
            "segment": self.segment,
            "currency": self.currency,
            "components": {k: str(v) for k, v in self.components.items() if v},
            "brokerage": str(self.brokerage),
            "exchange_fees": str(self.exchange_fees),
            "regulatory_fees": str(self.regulatory_fees),
            "total": str(self.total),
        }

    def describe(self) -> str:
        """A contract-note style itemisation, for logs and debugging."""
        lines = [f"{self.broker} · {self.segment} · {self.currency}"]
        width = max((len(k) for k in self.components), default=10)
        for key, value in self.components.items():
            if value:
                lines.append(f"  {key:<{width}}  {value:>12}")
        lines.append(f"  {'TOTAL':<{width}}  {self.total:>12}")
        return "\n".join(lines)

    def __add__(self, other: "FeeBreakdown") -> "FeeBreakdown":
        """Combine two breakdowns — used to total a multi-fill order."""
        if not isinstance(other, FeeBreakdown):  # pragma: no cover - defensive
            return NotImplemented
        merged: dict[str, Decimal] = dict(self.components)
        for key, value in other.components.items():
            merged[key] = merged.get(key, ZERO) + value
        return FeeBreakdown(
            components=merged,
            currency=self.currency,
            segment=self.segment,
            broker=self.broker,
        )


# ---------------------------------------------------------------------------
# Statutory fee schedules
# ---------------------------------------------------------------------------


class FeeSchedule(ABC):
    """Statutory and exchange charges for one market."""

    name: str = "none"
    currency: str = "INR"

    @abstractmethod
    def charges(
        self,
        trade_value: Decimal,
        quantity: Decimal,
        side: OrderSide,
        segment: str,
        brokerage: Decimal,
    ) -> dict[str, Decimal]:
        """Every non-brokerage charge, itemised."""

    def to_dict(self) -> dict[str, Any]:
        return {"schedule": self.name}


class NoStatutoryFees(FeeSchedule):
    """Brokerage only. Useful for isolating the effect of the fee stack."""

    name = "none"

    def charges(self, trade_value, quantity, side, segment, brokerage):
        return {}


@dataclass
class IndiaEquityFees(FeeSchedule):
    """NSE/BSE equity charges (FY 2024-25 rates).

    Rate notes, all as fractions of turnover unless stated:

    ==================  ==========================================
    STT delivery        0.1% on **both** buy and sell
    STT intraday        0.025% on the **sell only**
    Exchange txn (NSE)  0.00297%, both sides
    SEBI turnover       0.0001% (₹10 per crore)
    IPFT (NSE)          0.0001% (₹10 per crore)
    Stamp duty          0.015% delivery / 0.003% intraday, **buy only**
    GST                 18% on brokerage + exchange txn + SEBI + IPFT
    DP charges          flat per sell, delivery only
    ==================  ==========================================

    The buy/sell and delivery/intraday asymmetries are the whole point: a
    delivery round trip pays STT twice, an intraday round trip pays it once
    at a quarter of the rate.

    GST is charged on brokerage and the exchange/SEBI charges, **not** on STT
    or stamp duty — those are taxes themselves and are not taxed again.
    """

    name: str = field(default="india_equity", init=False)
    currency: str = field(default="INR", init=False)

    stt_delivery: Decimal = Decimal("0.001")
    stt_intraday_sell: Decimal = Decimal("0.00025")
    stt_futures_sell: Decimal = Decimal("0.0002")
    stt_options_sell: Decimal = Decimal("0.001")

    exchange_txn_equity: Decimal = Decimal("0.0000297")
    exchange_txn_futures: Decimal = Decimal("0.0000173")
    exchange_txn_options: Decimal = Decimal("0.0003503")

    sebi_turnover: Decimal = Decimal("0.000001")
    ipft: Decimal = Decimal("0.000001")

    stamp_duty_delivery: Decimal = Decimal("0.00015")
    stamp_duty_intraday: Decimal = Decimal("0.00003")
    stamp_duty_futures: Decimal = Decimal("0.00002")
    stamp_duty_options: Decimal = Decimal("0.00003")

    gst_rate: Decimal = Decimal("0.18")
    dp_charges: Decimal = Decimal("15.34")
    """Flat charge per delivery sell. Broker-specific; zero to disable."""

    def __post_init__(self) -> None:
        for name, value in list(vars(self).items()):
            if isinstance(value, (int, float, Decimal)) and name not in ("name", "currency"):
                coerced = to_decimal(value, name)
                if coerced < ZERO:
                    raise ValidationError(f"{name} must not be negative", code="invalid_fee_config")
                setattr(self, name, coerced)

    def _stt(self, value: Decimal, side: OrderSide, segment: str) -> Decimal:
        is_sell = side is OrderSide.SELL
        if segment == TradeSegment.EQUITY_DELIVERY:
            return value * self.stt_delivery  # both sides
        if segment == TradeSegment.EQUITY_INTRADAY:
            return value * self.stt_intraday_sell if is_sell else ZERO
        if segment == TradeSegment.FUTURES:
            return value * self.stt_futures_sell if is_sell else ZERO
        return value * self.stt_options_sell if is_sell else ZERO

    def _exchange_rate(self, segment: str) -> Decimal:
        if segment == TradeSegment.FUTURES:
            return self.exchange_txn_futures
        if segment == TradeSegment.OPTIONS:
            return self.exchange_txn_options
        return self.exchange_txn_equity

    def _stamp_rate(self, segment: str) -> Decimal:
        return {
            TradeSegment.EQUITY_DELIVERY: self.stamp_duty_delivery,
            TradeSegment.EQUITY_INTRADAY: self.stamp_duty_intraday,
            TradeSegment.FUTURES: self.stamp_duty_futures,
            TradeSegment.OPTIONS: self.stamp_duty_options,
        }[segment]

    def charges(self, trade_value, quantity, side, segment, brokerage):
        is_sell = side is OrderSide.SELL
        out: dict[str, Decimal] = {}

        out["stt"] = _round(self._stt(trade_value, side, segment))
        exchange_txn = _round(trade_value * self._exchange_rate(segment))
        out["exchange_transaction"] = exchange_txn
        sebi = _round(trade_value * self.sebi_turnover)
        out["sebi_turnover"] = sebi
        ipft = _round(trade_value * self.ipft)
        out["ipft"] = ipft

        # Stamp duty is a buy-side charge only.
        out["stamp_duty"] = ZERO if is_sell else _round(trade_value * self._stamp_rate(segment))

        # GST applies to brokerage and the exchange/SEBI charges — not to STT
        # or stamp duty, which are themselves taxes.
        out["gst"] = _round((brokerage + exchange_txn + sebi + ipft) * self.gst_rate)

        if is_sell and segment == TradeSegment.EQUITY_DELIVERY and self.dp_charges > ZERO:
            out["dp_charges"] = _round(self.dp_charges)

        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "schedule": self.name,
            "stt_delivery": str(self.stt_delivery),
            "stt_intraday_sell": str(self.stt_intraday_sell),
            "exchange_txn_equity": str(self.exchange_txn_equity),
            "sebi_turnover": str(self.sebi_turnover),
            "stamp_duty_delivery": str(self.stamp_duty_delivery),
            "gst_rate": str(self.gst_rate),
            "dp_charges": str(self.dp_charges),
        }


@dataclass
class USEquityFees(FeeSchedule):
    """US equity statutory charges. Both are **sell-side only**.

    * **SEC Section 31 fee** — a percentage of sale proceeds. The rate is
      reset periodically by the SEC, sometimes more than once a year.
    * **FINRA TAF** — per share sold, capped per trade.

    Included because the plan document specifies them; the Indian schedule is
    the default for this repository.
    """

    name: str = field(default="us_equity", init=False)
    currency: str = field(default="USD", init=False)

    sec_fee_rate: Decimal = Decimal("0.0000278")
    finra_taf_per_share: Decimal = Decimal("0.000166")
    finra_taf_max: Decimal = Decimal("8.30")
    ecn_fee_per_share: Decimal = ZERO
    """Positive removes liquidity; leave zero unless modelling a maker rebate."""

    def __post_init__(self) -> None:
        for name in (
            "sec_fee_rate",
            "finra_taf_per_share",
            "finra_taf_max",
            "ecn_fee_per_share",
        ):
            value = to_decimal(getattr(self, name), name)
            if value < ZERO:
                raise ValidationError(f"{name} must not be negative", code="invalid_fee_config")
            setattr(self, name, value)

    def charges(self, trade_value, quantity, side, segment, brokerage):
        out: dict[str, Decimal] = {}
        if side is OrderSide.SELL:
            out["sec_fee"] = _round(trade_value * self.sec_fee_rate)
            taf = min(quantity * self.finra_taf_per_share, self.finra_taf_max)
            out["finra_taf"] = _round(taf)
        if self.ecn_fee_per_share > ZERO:
            out["ecn_fee"] = _round(quantity * self.ecn_fee_per_share)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "schedule": self.name,
            "sec_fee_rate": str(self.sec_fee_rate),
            "finra_taf_per_share": str(self.finra_taf_per_share),
            "finra_taf_max": str(self.finra_taf_max),
        }


_SCHEDULES: dict[str, type[FeeSchedule]] = {
    "none": NoStatutoryFees,
    "india_equity": IndiaEquityFees,
    "us_equity": USEquityFees,
}


def resolve_fee_schedule(spec: Any) -> FeeSchedule:
    """Build a :class:`FeeSchedule` from an instance, name or dict."""
    if isinstance(spec, FeeSchedule):
        return spec
    if spec is None:
        return NoStatutoryFees()
    if isinstance(spec, str):
        name = spec.strip().lower()
        if name not in _SCHEDULES:
            raise ValidationError(
                f"unknown fee schedule {name!r}; expected one of {sorted(_SCHEDULES)}",
                code="unknown_fee_schedule",
            )
        return _SCHEDULES[name]()
    if isinstance(spec, Mapping):
        payload = dict(spec)
        name = str(payload.pop("schedule", "")).strip().lower()
        if name not in _SCHEDULES:
            raise ValidationError(
                f"unknown fee schedule {name!r}; expected one of {sorted(_SCHEDULES)}",
                code="unknown_fee_schedule",
            )
        try:
            return _SCHEDULES[name](**payload)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ValidationError(
                f"bad configuration for fee schedule {name!r}: {exc}",
                code="invalid_fee_config",
            ) from exc
    raise ValidationError(
        f"cannot build a fee schedule from {type(spec).__name__}",
        code="unknown_fee_schedule",
    )


# ---------------------------------------------------------------------------
# Currency
# ---------------------------------------------------------------------------


@dataclass
class CurrencyConverter:
    """Fixed-rate currency conversion.

    Rates are expressed **per unit of base**: with base ``INR`` and
    ``{"USD": 83}``, one USD is 83 INR. A live FX feed would replace this;
    fixed rates are enough to report a USD-denominated fee in INR.
    """

    base: str = "INR"
    rates: dict[str, Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.base = str(self.base).strip().upper()
        self.rates = {str(k).strip().upper(): to_decimal(v, "rate") for k, v in self.rates.items()}
        self.rates.setdefault(self.base, Decimal("1"))
        for code, rate in self.rates.items():
            if rate <= ZERO:
                raise ValidationError(
                    f"exchange rate for {code} must be positive",
                    code="invalid_fx_rate",
                )

    def convert(self, amount: Any, from_currency: str, to_currency: str) -> Decimal:
        """Convert between two known currencies.

        Raises
        ------
        ValidationError
            If either currency has no rate — guessing an FX rate would
            silently misstate costs.
        """
        src = str(from_currency).strip().upper()
        dst = str(to_currency).strip().upper()
        value = to_decimal(amount, "amount")
        if src == dst:
            return money(value)
        for code in (src, dst):
            if code not in self.rates:
                raise ValidationError(
                    f"no exchange rate for {code}; known: {sorted(self.rates)}",
                    code="unknown_currency",
                )
        return money(value * self.rates[src] / self.rates[dst])


# ---------------------------------------------------------------------------
# Broker profiles
# ---------------------------------------------------------------------------


@dataclass
class BrokerProfile:
    """A broker's complete cost model: brokerage plus statutory charges."""

    name: str = "generic"
    commission_model: CommissionModel = field(default_factory=ZeroCommission)
    fee_schedule: FeeSchedule = field(default_factory=NoStatutoryFees)
    minimum_commission: Decimal | None = None
    """Floor applied to brokerage *after* the model, before taxes."""
    currency: str = "INR"
    default_segment: str = TradeSegment.EQUITY_DELIVERY
    delivery_commission_model: CommissionModel | None = None
    """Many Indian brokers charge nothing on delivery but do on intraday."""

    def __post_init__(self) -> None:
        self.commission_model = resolve_commission_model(self.commission_model)
        self.fee_schedule = resolve_fee_schedule(self.fee_schedule)
        if self.delivery_commission_model is not None:
            self.delivery_commission_model = resolve_commission_model(
                self.delivery_commission_model
            )
        if self.minimum_commission is not None:
            self.minimum_commission = money(self.minimum_commission, "minimum_commission")
            if self.minimum_commission < ZERO:
                raise ValidationError(
                    "minimum_commission must not be negative", code="invalid_fee_config"
                )
        self.currency = str(self.currency).strip().upper()
        self.default_segment = TradeSegment.validate(self.default_segment)

    def model_for(self, segment: str) -> CommissionModel:
        """The brokerage model for a segment, honouring the delivery override."""
        if segment == TradeSegment.EQUITY_DELIVERY and self.delivery_commission_model is not None:
            return self.delivery_commission_model
        return self.commission_model

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "commission_model": self.commission_model.to_dict(),
            "fee_schedule": self.fee_schedule.to_dict(),
            "minimum_commission": (
                str(self.minimum_commission) if self.minimum_commission is not None else None
            ),
            "currency": self.currency,
            "default_segment": self.default_segment,
        }


def _india_discount(name: str, flat: Decimal = Decimal("20")) -> BrokerProfile:
    """The standard Indian discount-broker shape.

    Delivery free, intraday capped at 0.03% or a flat fee, whichever is
    lower — the model Zerodha popularised and most competitors copied.
    """
    return BrokerProfile(
        name=name,
        commission_model=PercentageCommission(rate=Decimal("0.0003"), maximum=flat),
        delivery_commission_model=ZeroCommission(),
        fee_schedule=IndiaEquityFees(),
        currency="INR",
        default_segment=TradeSegment.EQUITY_DELIVERY,
    )


#: Deterministic zero-cost profile for simulated runs (command-center paper
#: buckets, walk-forward buckets, canonical backtests). V1 paper trading had
#: no costs — the simulator reproduces that exactly: no commission, no
#: statutory fees, and (via :func:`backtest.simulator.execution.free_executor`)
#: no slippage, price improvement or market-hours gate.
PAPER_FREE_PROFILE = BrokerProfile(
    name="paper_free",
    commission_model=ZeroCommission(),
    fee_schedule=NoStatutoryFees(),
)


#: Ready-made broker cost models. Rates are indicative — verify before use.
BROKER_PRESETS: dict[str, Any] = {
    # ---- India ----
    "zerodha": lambda: _india_discount("zerodha"),
    "upstox": lambda: _india_discount("upstox"),
    "mstock": lambda: BrokerProfile(
        # mStock's headline is a flat-fee plan with no percentage component.
        name="mstock",
        commission_model=FlatCommission(per_trade=Decimal("20")),
        delivery_commission_model=ZeroCommission(),
        fee_schedule=IndiaEquityFees(),
        currency="INR",
    ),
    "india_full_service": lambda: BrokerProfile(
        name="india_full_service",
        commission_model=PercentageCommission(rate=Decimal("0.005"), maximum=None),
        fee_schedule=IndiaEquityFees(),
        minimum_commission=Decimal("25"),
        currency="INR",
    ),
    "india_zero": lambda: BrokerProfile(
        # Zero brokerage, full statutory stack. Shows that "free" trading
        # still costs roughly 12 bps on a delivery round trip.
        name="india_zero",
        commission_model=ZeroCommission(),
        fee_schedule=IndiaEquityFees(),
        currency="INR",
    ),
    # ---- United States (named by the plan document) ----
    "ibkr": lambda: BrokerProfile(
        name="ibkr",
        commission_model=PerShareCommission(
            per_share=Decimal("0.005"), minimum=Decimal("1"), maximum=None
        ),
        fee_schedule=USEquityFees(),
        currency="USD",
    ),
    "td_ameritrade": lambda: BrokerProfile(
        name="td_ameritrade",
        commission_model=ZeroCommission(),
        fee_schedule=USEquityFees(),
        currency="USD",
    ),
    "robinhood": lambda: BrokerProfile(
        # Zero commission funded by payment for order flow. The cost does not
        # vanish — it reappears as worse fills, which Step 7's slippage models
        # are where you should represent it.
        name="robinhood",
        commission_model=PaymentForOrderFlowCommission(),
        fee_schedule=USEquityFees(),
        currency="USD",
    ),
    "generic_discount": lambda: BrokerProfile(
        name="generic_discount",
        commission_model=PercentageCommission(rate=Decimal("0.0003"), maximum=Decimal("20")),
        fee_schedule=NoStatutoryFees(),
        currency="INR",
    ),
    "zero": lambda: BrokerProfile(
        name="zero",
        commission_model=ZeroCommission(),
        fee_schedule=NoStatutoryFees(),
        currency="INR",
    ),
}


def get_broker_preset(name: str) -> BrokerProfile:
    """Instantiate a named preset from :data:`BROKER_PRESETS`."""
    key = str(name).strip().lower()
    if key not in BROKER_PRESETS:
        raise ValidationError(
            f"unknown broker preset {key!r}; expected one of {sorted(BROKER_PRESETS)}",
            code="unknown_broker",
        )
    return BROKER_PRESETS[key]()


def load_broker_profile(path: str | Path | None = None, broker: str | None = None) -> BrokerProfile:
    """Load a broker profile from ``config/brokers.yaml``.

    Falls back to the named preset when the file has no matching entry, so
    the presets work with no configuration at all.
    """
    config_path = Path(path) if path else DEFAULT_BROKER_CONFIG_PATH
    if path is not None and not config_path.exists():
        raise ValidationError(f"broker config not found: {config_path}", code="config_not_found")
    if not config_path.exists():
        return get_broker_preset(broker or "zerodha")

    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise ValidationError(
            f"{config_path} exists but PyYAML is not installed", code="missing_pyyaml"
        ) from exc
    try:
        document = yaml.safe_load(config_path.read_text()) or {}
    except Exception as exc:
        raise ValidationError(
            f"could not parse {config_path}: {exc}", code="invalid_fee_config"
        ) from exc

    brokers = document.get("brokers") or {}
    chosen = broker or document.get("active_broker") or "zerodha"
    if chosen not in brokers:
        return get_broker_preset(chosen)

    payload = dict(brokers[chosen] or {})
    payload.setdefault("name", chosen)
    known = {f.name for f in fields(BrokerProfile)}
    unknown = set(payload) - known
    if unknown:
        raise ValidationError(
            f"unknown broker config keys for {chosen!r}: {sorted(unknown)}",
            code="invalid_fee_config",
        )
    return BrokerProfile(**payload)


# ---------------------------------------------------------------------------
# Calculator
# ---------------------------------------------------------------------------


class CommissionCalculator:
    """Computes the complete cost of an execution.

    Tracks monthly traded volume so tiered brokerage can price off it, and
    accumulates totals for end-of-run reporting.

    Examples
    --------
    >>> calc = CommissionCalculator.for_broker("zerodha")     # doctest: +SKIP
    >>> fees = calc.calculate(quantity=100, price=1500, side="buy")
    >>> fees.total                                             # doctest: +SKIP
    Decimal('165.28')
    """

    def __init__(
        self,
        broker: BrokerProfile | str | None = None,
        converter: CurrencyConverter | None = None,
        report_currency: str | None = None,
        record: bool = True,
    ) -> None:
        if isinstance(broker, str):
            broker = get_broker_preset(broker)
        self.broker = broker or get_broker_preset("zerodha")
        self.converter = converter or CurrencyConverter(base=self.broker.currency)
        self.report_currency = (report_currency or self.broker.currency).upper()
        self.record = record

        self._monthly_volume: dict[tuple[int, int], Decimal] = {}
        self._history: list[FeeBreakdown] = []

    # -- construction ------------------------------------------------------

    @classmethod
    def for_broker(cls, name: str, **kwargs: Any) -> "CommissionCalculator":
        """Build from a named preset."""
        return cls(broker=get_broker_preset(name), **kwargs)

    @classmethod
    def from_config(
        cls, path: str | Path | None = None, broker: str | None = None, **kwargs: Any
    ) -> "CommissionCalculator":
        """Build from ``config/brokers.yaml``."""
        return cls(broker=load_broker_profile(path, broker), **kwargs)

    def switch_broker(self, broker: BrokerProfile | str) -> BrokerProfile:
        """Change broker mid-run, keeping volume history.

        Useful for re-costing the same strategy against a different broker
        without rebuilding the calculator.
        """
        self.broker = get_broker_preset(broker) if isinstance(broker, str) else broker
        logger.info("switched broker to %s", self.broker.name)
        return self.broker

    # -- volume tracking ---------------------------------------------------

    def record_volume(self, trade_value: Any, when: datetime | date | None = None) -> Decimal:
        """Add to the running monthly volume and return the new total."""
        value = abs(to_decimal(trade_value, "trade_value"))
        stamp = when or datetime.now()
        key = (stamp.year, stamp.month)
        self._monthly_volume[key] = self._monthly_volume.get(key, ZERO) + value
        return self._monthly_volume[key]

    def monthly_volume(self, when: datetime | date | None = None) -> Decimal:
        """Volume traded in the given month (current month by default)."""
        stamp = when or datetime.now()
        return self._monthly_volume.get((stamp.year, stamp.month), ZERO)

    # -- spec-named helpers ------------------------------------------------

    def calculate_commission(
        self,
        order: "Order | None" = None,
        fill_price: Any = None,
        *,
        quantity: Any = None,
        side: Any = OrderSide.BUY,
        segment: str | None = None,
        when: datetime | None = None,
    ) -> Decimal:
        """Brokerage only, before statutory charges.

        A tiered model prices off **monthly volume** when one has been
        recorded, otherwise off this trade's value — so tiering works whether
        or not the caller tracks volume.
        """
        if order is not None:
            quantity = quantity if quantity is not None else order.quantity
            side = order.side
            fill_price = fill_price if fill_price is not None else order.average_fill_price

        if quantity is None or fill_price is None:
            raise ValidationError(
                "provide an order, or explicit quantity and fill_price",
                code="missing_order_context",
            )

        qty = abs(to_price(quantity, "quantity"))
        px = to_price(fill_price, "fill_price")
        side = OrderSide.parse(side)
        segment = TradeSegment.validate(segment or self.broker.default_segment)

        model = self.broker.model_for(segment)
        if isinstance(model, TieredCommission):
            volume = self.monthly_volume(when)
            if volume > ZERO:
                rate = model.rate_for(volume + qty * px)
                brokerage = qty * px * rate
                if model.minimum is not None:
                    brokerage = max(brokerage, model.minimum)
                if model.maximum is not None:
                    brokerage = min(brokerage, model.maximum)
            else:
                brokerage = model.calculate(qty, px, side)
        else:
            brokerage = model.calculate(qty, px, side)

        if self.broker.minimum_commission is not None:
            brokerage = max(brokerage, self.broker.minimum_commission)
        return _round(brokerage)

    def calculate_regulatory_fees(
        self,
        trade_value: Any,
        side: Any = OrderSide.BUY,
        segment: str | None = None,
        brokerage: Any = ZERO,
    ) -> Decimal:
        """Statutory charges: STT, SEBI, stamp duty, GST, SEC, TAF."""
        value = abs(to_decimal(trade_value, "trade_value"))
        segment = TradeSegment.validate(segment or self.broker.default_segment)
        charges = self.broker.fee_schedule.charges(
            value, ZERO, OrderSide.parse(side), segment, money(brokerage)
        )
        return _round(
            sum(
                (v for k, v in charges.items() if k in FeeBreakdown.REGULATORY_KEYS),
                ZERO,
            )
        )

    def calculate_exchange_fees(
        self,
        quantity: Any = ZERO,
        trade_value: Any = ZERO,
        side: Any = OrderSide.BUY,
        segment: str | None = None,
    ) -> Decimal:
        """Exchange transaction charge, IPFT and DP charges.

        Indian exchange charges are value-based, so ``trade_value`` matters
        more than ``quantity`` here; both are accepted because US ECN fees
        are per share.
        """
        segment = TradeSegment.validate(segment or self.broker.default_segment)
        charges = self.broker.fee_schedule.charges(
            abs(to_decimal(trade_value, "trade_value")),
            abs(to_decimal(quantity, "quantity")),
            OrderSide.parse(side),
            segment,
            ZERO,
        )
        return _round(sum((v for k, v in charges.items() if k in FeeBreakdown.EXCHANGE_KEYS), ZERO))

    # -- main entry point --------------------------------------------------

    def calculate(
        self,
        order: "Order | None" = None,
        fill_price: Any = None,
        *,
        quantity: Any = None,
        side: Any = None,
        segment: str | None = None,
        when: datetime | None = None,
        track_volume: bool = True,
    ) -> FeeBreakdown:
        """The complete itemised cost of one execution.

        Parameters
        ----------
        order:
            An :class:`~backtest.simulator.order.Order`; its side and quantity
            are used unless overridden.
        segment:
            Overrides the broker's default. **Set this correctly** — delivery
            and intraday STT differ by roughly 8x on a round trip.
        track_volume:
            Add this trade to the monthly volume used for tiered pricing.

        Returns
        -------
        FeeBreakdown
            Itemised, with ``as_fill_kwargs()`` ready to pass to
            :class:`~backtest.simulator.fill.Fill`.
        """
        if order is not None:
            quantity = quantity if quantity is not None else order.quantity
            side = side if side is not None else order.side
            fill_price = fill_price if fill_price is not None else order.average_fill_price
        if quantity is None or fill_price is None or side is None:
            raise ValidationError(
                "provide an order, or explicit quantity, fill_price and side",
                code="missing_order_context",
            )

        qty = abs(to_price(quantity, "quantity"))
        px = to_price(fill_price, "fill_price")
        if px <= ZERO:
            raise ValidationError("fill_price must be positive", code="invalid_price")
        side = OrderSide.parse(side)
        segment = TradeSegment.validate(segment or self.broker.default_segment)
        trade_value = qty * px

        brokerage = self.calculate_commission(
            quantity=qty, fill_price=px, side=side, segment=segment, when=when
        )
        components: dict[str, Decimal] = {"brokerage": brokerage}
        components.update(
            self.broker.fee_schedule.charges(trade_value, qty, side, segment, brokerage)
        )

        breakdown = FeeBreakdown(
            components=components,
            currency=self.broker.currency,
            segment=segment,
            broker=self.broker.name,
        )

        if self.report_currency != self.broker.currency:
            converted = {
                k: self.converter.convert(v, self.broker.currency, self.report_currency)
                for k, v in components.items()
            }
            breakdown = FeeBreakdown(
                components=converted,
                currency=self.report_currency,
                segment=segment,
                broker=self.broker.name,
            )

        if track_volume:
            self.record_volume(trade_value, when)
        if self.record:
            self._history.append(breakdown)

        logger.debug(
            "fees %s %s %s @ %s [%s] -> %s (%s bps)",
            self.broker.name,
            side,
            qty,
            px,
            segment,
            breakdown.total,
            breakdown.effective_bps(trade_value),
        )
        return breakdown

    # -- reporting ---------------------------------------------------------

    def get_total_fees(self) -> Decimal:
        """Every fee charged since construction (or the last :meth:`reset`)."""
        return _round(sum((b.total for b in self._history), ZERO))

    @property
    def history(self) -> tuple[FeeBreakdown, ...]:
        return tuple(self._history)

    def reset(self) -> None:
        """Clear fee history and monthly volume."""
        self._history.clear()
        self._monthly_volume.clear()

    def statistics(self) -> dict[str, Any]:
        """Aggregate cost report for the run."""
        if not self._history:
            return {"count": 0, "total": ZERO}
        totals: dict[str, Decimal] = {}
        for breakdown in self._history:
            for key, value in breakdown.components.items():
                totals[key] = totals.get(key, ZERO) + value
        return {
            "count": len(self._history),
            "broker": self.broker.name,
            "currency": self.report_currency,
            "total": self.get_total_fees(),
            "brokerage": _round(sum((b.brokerage for b in self._history), ZERO)),
            "exchange_fees": _round(sum((b.exchange_fees for b in self._history), ZERO)),
            "regulatory_fees": _round(sum((b.regulatory_fees for b in self._history), ZERO)),
            "taxes": _round(sum((b.taxes for b in self._history), ZERO)),
            "components": {k: _round(v) for k, v in sorted(totals.items()) if v},
            "monthly_volume": dict(self._monthly_volume),
        }

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<CommissionCalculator {self.broker.name} n={len(self._history)}>"
