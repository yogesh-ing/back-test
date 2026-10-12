"""The normalised trade record + the Indian financial-year clock.

Every source (the ``trades`` table, the live in-memory books, the explicitly
labelled demo book) is reduced to one :class:`TradeRecord` so the consolidator
never branches on where a number came from. The record keeps **three** P&L
figures rather than one, because collapsing them is exactly how a P&L report
starts lying:

``gross_pnl``
    Price P&L at the prices the trades actually executed at, before fees.
``net_pnl``
    What the money did. Taken from the source when the source really knows it
    (an option structure's realised P&L already nets its commission); computed
    as ``gross - fees - slippage`` otherwise.
``fees`` + ``slippage``
    The cost stack, itemised, plus a flag saying whether it was **recorded**
    (the venue or the platform wrote it down) or **estimated** (the fee engine
    re-derived it from the trade's own prices). Estimated costs are legal —
    they are just never displayed as if they were observed.

Where a source reports both a net and a cost stack that do not add up, the
difference is kept as ``ladder_residual`` and surfaced in the report instead of
being smoothed away.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping

from backtest.reporting.tax import TaxCategory, TaxRules, categorize_trade
from backtest.simulator.enums import OrderSide
from backtest.simulator.fees import FeeBreakdown, TradeSegment
from backtest.simulator.money import ZERO, money

__all__ = [
    "TradeRecord",
    "Period",
    "FeeBreakdown",
    "estimate_round_trip_fees",
    "instrument_from_symbol",
    "IST",
    "FEE_BASIS_RECORDED",
    "FEE_BASIS_ESTIMATED",
    "FEE_BASIS_NONE",
]

logger = logging.getLogger("backtest.reporting.records")

#: Cost stack was observed (venue fill, contract note, platform ledger).
FEE_BASIS_RECORDED = "recorded"
#: Cost stack was re-derived by the fee engine from the trade's own prices.
FEE_BASIS_ESTIMATED = "estimated"
#: No costs applied at all — deterministic zero-cost paper books.
FEE_BASIS_NONE = "none"

#: Indian market clock — naive bar/exit stamps are IST (same convention as
#: :mod:`backtest.api.analytics_service`).
IST = timedelta(hours=5, minutes=30)

#: Component display order for the report's cost table (contract-note order).
FEE_DISPLAY_ORDER = (
    "brokerage",
    "stt",
    "exchange_transaction",
    "sebi_turnover",
    "ipft",
    "stamp_duty",
    "gst",
    "dp_charges",
)

FEE_LABELS = {
    "brokerage": "Brokerage",
    "stt": "STT / CTT",
    "exchange_transaction": "Exchange transaction charges",
    "sebi_turnover": "SEBI turnover fee",
    "ipft": "IPFT",
    "stamp_duty": "Stamp duty",
    "gst": "GST",
    "dp_charges": "DP charges",
    "ecn_fee": "ECN fee",
    "sec_fee": "SEC fee",
    "finra_taf": "FINRA TAF",
}


def instrument_from_symbol(symbol: str | None) -> str:
    """Best-effort instrument tag for a trade that did not carry one.

    Used only when a source has no explicit instrument type; the caller marks
    the record's ``fees_basis``/``notes`` accordingly. Option contracts end in
    a strike + CE/PE or carry a ``FUT`` marker; anything else is treated as
    equity, which is the platform's default assumption.
    """
    text = str(symbol or "").strip().upper()
    if not text:
        return "equity"
    if text.endswith(("CE", "PE")):
        return "options"
    if text.endswith("FUT") or "FUT" in text:
        return "futures"
    return "equity"


def _normalize_aware_dt(value: Any) -> datetime | None:
    """Normalize any datetime/date/string to a timezone-aware UTC datetime.

    Avoids offset-naive and offset-aware datetime comparison errors across
    heterogeneous sources (persisted DB rows, in-memory runners, demo book).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass
class TradeRecord:
    """One closed round trip, normalised across every source.

    ``segment`` uses the fee engine's vocabulary (:class:`TradeSegment`) so the
    statutory rates and the tax category are keyed off the same string.
    """

    trade_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    broker: str = ""
    mode: str = "paper"
    segment: str = TradeSegment.EQUITY_DELIVERY
    symbol: str = ""
    strategy: str = ""
    direction: str = "long"
    quantity: Decimal = ZERO
    entry_price: Decimal = ZERO
    exit_price: Decimal = ZERO
    entry_time: datetime | None = None
    exit_time: datetime | None = None
    gross_pnl: Decimal = ZERO
    fees: FeeBreakdown = field(default_factory=FeeBreakdown)
    slippage: Decimal = ZERO
    #: The source's own net, when it has one; ``None`` when it does not.
    net_pnl_recorded: Decimal | None = None
    #: True only when ``net_pnl_recorded`` already includes every cost the
    #: source knows about (option structures do; the ``trades`` table echoes
    #: its own gross column and therefore does not).
    net_authoritative: bool = False
    fees_basis: str = FEE_BASIS_RECORDED
    exit_reason: str = ""
    #: Provenance: ``db`` | ``memory`` | ``demo`` | … (never hidden).
    source: str = "db"
    #: Free-form marker rendered next to the row (e.g. ``SIMULATED``).
    tag: str = ""
    tax_category: TaxCategory = TaxCategory.UNCLASSIFIED
    notes: list[str] = field(default_factory=list)
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    # -- derived -----------------------------------------------------------

    def __post_init__(self) -> None:
        self.quantity = _dec(self.quantity)
        self.entry_price = _dec(self.entry_price)
        self.exit_price = _dec(self.exit_price)
        self.gross_pnl = money(_dec(self.gross_pnl))
        self.slippage = money(_dec(self.slippage))
        self.entry_time = _normalize_aware_dt(self.entry_time)
        self.exit_time = _normalize_aware_dt(self.exit_time)
        self.mode = str(self.mode or "paper").strip().lower()
        self.broker = str(self.broker or "").strip().lower()
        try:
            self.segment = TradeSegment.validate(self.segment)
        except Exception:  # noqa: BLE001 — keep the raw string, flag it later
            self.notes.append(f"unknown segment {self.segment!r} — classified UNCLASSIFIED")
            self.segment = str(self.segment)

    @property
    def entry_date(self) -> date | None:
        return _as_date(self.entry_time)

    @property
    def exit_date(self) -> date | None:
        return _as_date(self.exit_time)

    @property
    def holding_days(self) -> int:
        if self.entry_date is None or self.exit_date is None:
            return 0
        return max((self.exit_date - self.entry_date).days, 0)

    @property
    def holding_minutes(self) -> int:
        if self.entry_time is None or self.exit_time is None:
            return 0
        return max(int((self.exit_time - self.entry_time).total_seconds() // 60), 0)

    @property
    def fees_total(self) -> Decimal:
        return money(self.fees.total)

    @property
    def slippage_total(self) -> Decimal:
        return money(self.slippage)

    @property
    def total_costs(self) -> Decimal:
        return money(self.fees_total + self.slippage_total)

    @property
    def net_pnl(self) -> Decimal:
        """Realised money: recorded when authoritative, else derived."""
        if self.net_authoritative and self.net_pnl_recorded is not None:
            return money(self.net_pnl_recorded)
        return money(self.gross_pnl - self.total_costs)

    @property
    def ladder_residual(self) -> Decimal:
        """``gross - costs - net`` when the source reported its own net.

        Non-zero means the record's internal arithmetic disagrees with itself;
        the report sums and lists these instead of hiding them.
        """
        if not self.net_authoritative or self.net_pnl_recorded is None:
            return ZERO
        return money(self.gross_pnl - self.total_costs - self.net_pnl_recorded)

    @property
    def is_win(self) -> bool:
        return self.net_pnl > ZERO

    def classify(self, rules: TaxRules | None = None) -> TaxCategory:
        """Set and return this trade's tax category."""
        self.tax_category = categorize_trade(
            segment=self.segment,
            mode=self.mode,
            entry_date=self.entry_date,
            exit_date=self.exit_date,
            rules=rules,
        )
        return self.tax_category

    def fee_rows(self) -> list[dict[str, Any]]:
        """Component rows in contract-note order, zeros dropped."""
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for key in FEE_DISPLAY_ORDER:
            value = self.fees.get(key)
            seen.add(key)
            if value:
                rows.append({"key": key, "label": FEE_LABELS.get(key, key), "amount": float(value)})
        for key, value in self.fees.components.items():
            if key in seen or FeeBreakdown._is_leg_key(key):  # noqa: SLF001 — same package intent
                continue
            if isinstance(value, Decimal) and value:
                rows.append({"key": key, "label": FEE_LABELS.get(key, key), "amount": float(value)})
        return rows

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "broker": self.broker,
            "mode": self.mode,
            "segment": self.segment,
            "symbol": self.symbol,
            "strategy": self.strategy,
            "direction": self.direction,
            "quantity": float(self.quantity),
            "entry_price": float(self.entry_price),
            "exit_price": float(self.exit_price),
            "entry_date": self.entry_date.isoformat() if self.entry_date else None,
            "exit_date": self.exit_date.isoformat() if self.exit_date else None,
            "entry_time": self.entry_time.isoformat() if self.entry_time else None,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "holding_days": self.holding_days,
            "holding_minutes": self.holding_minutes,
            "gross_pnl": float(self.gross_pnl),
            "fees": float(self.fees_total),
            "fee_rows": self.fee_rows(),
            "slippage": float(self.slippage_total),
            "net_pnl": float(self.net_pnl),
            "net_pnl_recorded": (
                float(self.net_pnl_recorded) if self.net_pnl_recorded is not None else None
            ),
            "net_authoritative": self.net_authoritative,
            "ladder_residual": float(self.ladder_residual),
            "fees_basis": self.fees_basis,
            "tax_category": self.tax_category.value,
            "tax_category_label": self.tax_category.label,
            "exit_reason": self.exit_reason,
            "source": self.source,
            "tag": self.tag,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Fee estimation (for records whose source did not store the statutory stack)
# ---------------------------------------------------------------------------


def estimate_round_trip_fees(
    *,
    segment: str,
    broker: str | None,
    quantity: Decimal,
    entry_price: Decimal,
    exit_price: Decimal,
    direction: str = "long",
    when: datetime | None = None,
    calculator: Any = None,
) -> FeeBreakdown:
    """Re-derive the full cost of a round trip from its own prices.

    Both legs are priced with the broker's real schedule — including the
    asymmetries that matter (STT is sell-side for intraday/futures/options and
    both-side for delivery; stamp duty is buy-side only; DP charges land on the
    delivery sell) — so an estimated stack is at least shaped like a contract
    note. The caller marks the record ``estimated``; nothing here pretends the
    venue confirmed it.

    ``calculator`` lets a caller pass a configured
    :class:`~backtest.simulator.fees.CommissionCalculator` (e.g. one already
    loaded from the settings-panel profile store). Otherwise the broker name is
    resolved through the normal preset/YAML/DB chain, defaulting to
    ``india_zero`` — zero brokerage plus the full statutory stack, the
    conservative shape for a broker we do not have a profile for.
    """
    seg = TradeSegment.validate(segment)
    calc = calculator or _calculator_for(broker)

    qty = abs(_dec(quantity))
    if qty <= ZERO:
        return FeeBreakdown(components={}, segment=seg, broker=getattr(calc.broker, "name", ""))
    is_long = str(direction or "long").strip().lower() != "short"
    open_side = OrderSide.BUY if is_long else OrderSide.SELL
    close_side = OrderSide.SELL if is_long else OrderSide.BUY

    total = FeeBreakdown(
        components={}, currency="INR", segment=seg, broker=getattr(calc.broker, "name", "")
    )
    for side, price in ((open_side, entry_price), (close_side, exit_price)):
        px = _dec(price)
        if px <= ZERO:
            continue
        try:
            total = total + calc.calculate(
                quantity=qty, fill_price=px, side=side, segment=seg, when=when, track_volume=False
            )
        except Exception as exc:  # noqa: BLE001 — never let one leg block the report
            logger.warning("fee estimate failed for %s %s (%s)", seg, side, exc)
    return FeeBreakdown(
        components=dict(total.components),
        currency=total.currency,
        segment=seg,
        broker=total.broker,
    )


#: One calculator per broker name, resolved through the normal
#: DB → config/brokers.yaml → preset chain and cached for the process.
_CALCULATORS: dict[str, Any] = {}
_WARNED_BROKERS: set[str] = set()


def _calculator_for(broker: str | None) -> Any:
    """A :class:`CommissionCalculator` for ``broker`` (statutory-only fallback).

    Resolution follows the platform's own precedence — the settings panel's DB
    profile first, then ``config/brokers.yaml``, then a built-in preset. A
    broker nobody has modelled gets ``india_zero`` (zero brokerage, full
    statutory stack) rather than an invented commission, and the fallback is
    logged once per name so a report full of ₹0 brokerage is explainable.
    """
    from backtest.simulator.fees import CommissionCalculator, load_broker_profile

    name = str(broker or "").strip().lower() or "india_zero"
    cached = _CALCULATORS.get(name)
    if cached is not None:
        return cached
    try:
        profile = load_broker_profile(broker=name)
        calc = CommissionCalculator(broker=profile)
    except Exception as exc:  # noqa: BLE001 — unknown broker → statutory-only model
        if name not in _WARNED_BROKERS:
            _WARNED_BROKERS.add(name)
            logger.warning(
                "no fee model for broker %r (%s) — estimating the statutory stack only "
                "(zero brokerage); add a profile in config/brokers.yaml or the Cost panel",
                name,
                exc,
            )
        calc = CommissionCalculator.for_broker("india_zero")
    _CALCULATORS[name] = calc
    return calc


# ---------------------------------------------------------------------------
# Period — the Indian financial year clock
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Period:
    """A reporting window, defaulting to the Indian financial year.

    The FY runs 1 April → 31 March, so a "this year" report in June is FY
    2026-27 H1-to-date, not the calendar year. Getting this wrong is how
    capital-gains exemptions and turnover limits get measured against the
    wrong twelve months.
    """

    start: date
    end: date
    label: str = ""

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"period end {self.end} precedes start {self.start}")
        if not self.label:
            object.__setattr__(self, "label", self.default_label())

    # -- constructors ------------------------------------------------------

    @classmethod
    def financial_year(cls, anchor: date | datetime | None = None) -> "Period":
        """The FY containing ``anchor`` (defaults to today)."""
        today = _as_date(anchor) or date.today()
        year = today.year if today.month >= 4 else today.year - 1
        return cls(date(year, 4, 1), date(year + 1, 3, 31), f"FY {year}-{str(year + 1)[2:]}")

    @classmethod
    def financial_year_to_date(cls, anchor: date | datetime | None = None) -> "Period":
        """The FY containing ``anchor``, truncated at ``anchor``."""
        today = _as_date(anchor) or date.today()
        fy = cls.financial_year(today)
        if today >= fy.end or today < fy.start:
            return fy
        return cls(fy.start, today, f"{fy.label} (to date)")

    @classmethod
    def month(cls, anchor: date | datetime | None = None) -> "Period":
        """Whole calendar month containing ``anchor``."""
        today = _as_date(anchor) or date.today()
        start = today.replace(day=1)
        end = _add_months(start, 1).replace(day=1) - timedelta(days=1)
        return cls(start, end, start.strftime("%B %Y"))

    @classmethod
    def previous_month(cls, anchor: date | datetime | None = None) -> "Period":
        """The calendar month before the one containing ``anchor``.

        The monthly email runs on the 1st and reports the month that just
        closed — reporting "this month" on the 1st would mail an empty file.
        """
        today = _as_date(anchor) or date.today()
        first = today.replace(day=1)
        return cls.month(first - timedelta(days=1))

    @classmethod
    def parse(
        cls,
        start: str | date | None,
        end: str | date | None,
        *,
        default: "Period | None" = None,
    ) -> "Period":
        """Parse ``YYYY-MM-DD`` strings (or dates); ``None`` → ``default``/FYTD.

        An *unparseable* string is a :class:`ValueError`, not a fallback: a
        typo'd date silently becoming "the financial year" would file the wrong
        twelve months' numbers.
        """
        fallback = default or cls.financial_year_to_date()
        return cls(
            _bound(start, "from_date") or fallback.start,
            _bound(end, "to_date") or fallback.end,
            "",
        )

    # -- queries -----------------------------------------------------------

    def contains(self, value: date | datetime | None) -> bool:
        day = _as_date(value)
        return day is not None and self.start <= day <= self.end

    def days(self) -> int:
        return (self.end - self.start).days + 1

    def default_label(self) -> str:
        return f"{self.start.strftime('%d-%b-%Y')} → {self.end.strftime('%d-%b-%Y')}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "label": self.label,
            "days": self.days(),
        }

    def __str__(self) -> str:  # pragma: no cover - display only
        return self.label


def _add_months(anchor: date, months: int) -> date:
    from backtest.reporting.tax import add_months

    return add_months(anchor, months)


def _bound(value: Any, name: str) -> date | None:
    """Parse one period bound; a non-empty value that does not parse is fatal."""
    if value is None or isinstance(value, (date, datetime)):
        return _as_date(value)
    if str(value).strip() == "":
        return None
    parsed = _as_date(value)
    if parsed is None:
        raise ValueError(f"{name} is not a date: {value!r} (expected YYYY-MM-DD)")
    return parsed


def _as_date(value: Any) -> date | None:
    """date/datetime/ISO string → date, guessing IST for naive datetimes.

    Bar and exit stamps in this platform are IST wall-clock; treating a naive
    stamp as UTC shifts a whole day of trades at the month boundary, which is
    exactly where a period report is read.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    normalised = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    return parsed.date()


def _dec(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return ZERO
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 — a non-numeric cell is a data problem, not a crash
        logger.warning("non-numeric value in trade record: %r", value)
        return ZERO
