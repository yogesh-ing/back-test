"""Indian tax classification and estimation for a consolidated trade book.

This module is deliberately **not** a tax calculator you can file with. It is
a reality check: it classifies every closed trade into the head of income the
law puts it under, applies the published rates, and tells you the two things
that actually cost traders money:

1. **The category** — because the same ₹2,00,000 of profit is taxed very
   differently depending on which bucket it lands in, and the buckets are
   not intuitive:

   ==========================  ==============================  ====================
   Activity                    Head of income                  Loss rules
   ==========================  ==============================  ====================
   F&O (futures & options on   Non-speculative business        Set off against any
   a recognised exchange)      income (proviso (d) to           income except
                               s.43(5)) — **slab rate**         salary; carry
                                                               8 years
   Intraday equity             Speculative business income      Only against
                               (main limb, s.43(5)) —           speculative gains;
                               **slab rate**                    carry 4 years
   Delivery equity ≤ 12 months Short-term capital gain         Only against
                               (s.111A) — **20%**               capital gains;
                                                               8 years
   Delivery equity > 12 months Long-term capital gain           Only against
                               (s.112A) — **12.5% above         capital gains;
                               ₹1,25,000**                      8 years
   ==========================  ==============================  ====================

   Two of those contradict the PRD this feature came from, which called F&O
   "speculative" and intraday "STCG @ 20%". Both are wrong, and both are
   expensive to get wrong: a ₹2,00,000 intraday profit is ~₹40,000 of tax if
   you mislabel it as STCG and roughly nothing extra if you are below the
   basic exemption limit — while a ₹2,00,000 F&O loss mislabelled as a
   capital loss loses its right to absorb any other income.

2. **The deduction** — for business income (F&O + intraday) every rupee the
   broker took is deductible: brokerage, STT (s.36(1)(xv)), exchange
   transaction charges, SEBI turnover fees, stamp duty and the GST charged on
   brokerage. For **capital gains** the same expenses are treated
   differently: brokerage/stamp duty are allowed in computing the gain, but
   **STT is not deductible** (proviso to s.48) — a subtlety that quietly
   matters on delivery trades, where STT is 0.1% on both legs.

Rates are point-in-time (FY 2026-27, i.e. the tax period 2026-27 under the
Income-tax Act, 2025, whose rates continue to come from the Finance Acts)
and every one of them is overridable in ``config/reporting.yaml`` — see
:func:`load_tax_rules`. Capital-gains rates for listed equity were last
changed by the Finance (No. 2) Act 2024 (STCG 15% → 20%, LTCG 10% → 12.5%,
exemption ₹1,00,000 → ₹1,25,000); **verify them before you file.**
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from backtest.simulator.money import ZERO

__all__ = [
    "TaxCategory",
    "TaxRules",
    "TaxLine",
    "TaxEstimate",
    "AuditPosition",
    "DEFAULT_TAX_RULES",
    "DEFAULT_RULES_PATH",
    "categorize_trade",
    "estimate_tax",
    "fno_turnover",
    "turnover_by_category",
    "audit_position",
    "add_months",
    "is_long_term",
    "load_tax_rules",
    "resolve_tax_rules",
]

logger = logging.getLogger("backtest.reporting.tax")

#: <repo>/config/reporting.yaml (this file is <repo>/src/backtest/reporting/…).
DEFAULT_RULES_PATH = Path(__file__).resolve().parents[3] / "config" / "reporting.yaml"

#: Env var that points at an alternate reporting config file.
RULES_PATH_ENV = "REPORTING_CONFIG_PATH"


def _decimal(value: Any, name: str) -> Decimal:
    """Decimal from str/int/float/Decimal without binary-float surprises."""
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 — a bad config value is a config error
        raise ValueError(f"{name}: not a number: {value!r}") from exc


class TaxCategory(str, Enum):
    """The head of income a closed trade belongs under.

    Values are stable strings (they cross the API boundary); :attr:`label` is
    the human string, :attr:`treatment` explains the rate and :attr:`loss_rule`
    the set-off/carry-forward rule a loss in this bucket obeys.
    """

    FNO_NON_SPECULATIVE = "FNO_NON_SPECULATIVE"
    EQUITY_SPECULATIVE = "EQUITY_SPECULATIVE"
    EQUITY_STCG = "EQUITY_STCG"
    EQUITY_LTCG = "EQUITY_LTCG"
    PAPER_EXCLUDED = "PAPER_EXCLUDED"
    UNCLASSIFIED = "UNCLASSIFIED"

    @property
    def label(self) -> str:
        return _CATEGORY_LABELS[self]

    @property
    def treatment(self) -> str:
        return _CATEGORY_TREATMENTS[self]

    @property
    def loss_rule(self) -> str:
        return _CATEGORY_LOSS_RULES[self]

    @property
    def itr_schedule(self) -> str:
        """Where the number goes in the return (ITR-3, unless stated)."""
        return _CATEGORY_SCHEDULES[self]

    @property
    def is_business_income(self) -> bool:
        return self in _BUSINESS_CATEGORIES

    @property
    def is_capital_gain(self) -> bool:
        return self in _CAPITAL_GAIN_CATEGORIES

    @property
    def is_taxable(self) -> bool:
        return self not in (TaxCategory.PAPER_EXCLUDED, TaxCategory.UNCLASSIFIED)

    @classmethod
    def parse(cls, value: Any) -> "TaxCategory":
        """Parse a category from a value/name, or raise ``ValueError``."""
        if isinstance(value, cls):
            return value
        raw = str(value or "").strip().upper()
        for member in cls:
            if raw == member.value:
                return member
        raise ValueError(f"unknown tax category {value!r}")


_CATEGORY_LABELS: dict[TaxCategory, str] = {
    TaxCategory.FNO_NON_SPECULATIVE: "F&O (non-speculative business)",
    TaxCategory.EQUITY_SPECULATIVE: "Equity intraday (speculative business)",
    TaxCategory.EQUITY_STCG: "Equity delivery ≤12m (STCG)",
    TaxCategory.EQUITY_LTCG: "Equity delivery >12m (LTCG)",
    TaxCategory.PAPER_EXCLUDED: "Paper / simulated (not taxable)",
    TaxCategory.UNCLASSIFIED: "Unclassified — needs review",
}

_CATEGORY_TREATMENTS: dict[TaxCategory, str] = {
    TaxCategory.FNO_NON_SPECULATIVE: "Slab rate on net profit; all trading costs deductible",
    TaxCategory.EQUITY_SPECULATIVE: "Slab rate on net profit; all trading costs deductible",
    TaxCategory.EQUITY_STCG: "20% flat on the gain; STT not deductible",
    TaxCategory.EQUITY_LTCG: "12.5% above the annual exemption; STT not deductible",
    TaxCategory.PAPER_EXCLUDED: "Simulated money — never enters a return",
    TaxCategory.UNCLASSIFIED: "Not estimated — classify the trade first",
}

_CATEGORY_LOSS_RULES: dict[TaxCategory, str] = {
    TaxCategory.FNO_NON_SPECULATIVE: (
        "Set off against any income except salary in the same year; carry forward 8 years"
    ),
    TaxCategory.EQUITY_SPECULATIVE: (
        "Set off only against speculative gains; carry forward 4 years"
    ),
    TaxCategory.EQUITY_STCG: (
        "Set off against capital gains (STCG or LTCG); carry forward 8 years"
    ),
    TaxCategory.EQUITY_LTCG: (
        "Set off only against LTCG; carry forward 8 years"
    ),
    TaxCategory.PAPER_EXCLUDED: "Not a real loss — nothing to set off or carry",
    TaxCategory.UNCLASSIFIED: "Unknown — classify before assuming any set-off",
}

_CATEGORY_SCHEDULES: dict[TaxCategory, str] = {
    TaxCategory.FNO_NON_SPECULATIVE: "ITR-3 Schedule BP (non-speculative business)",
    TaxCategory.EQUITY_SPECULATIVE: "ITR-3 Schedule BP (speculative business)",
    TaxCategory.EQUITY_STCG: "Schedule CG — A3 (STCG at special rates)",
    TaxCategory.EQUITY_LTCG: "Schedule CG — B3 (LTCG at special rates)",
    TaxCategory.PAPER_EXCLUDED: "— (excluded)",
    TaxCategory.UNCLASSIFIED: "— (classify first)",
}

_BUSINESS_CATEGORIES = frozenset(
    {TaxCategory.FNO_NON_SPECULATIVE, TaxCategory.EQUITY_SPECULATIVE}
)
_CAPITAL_GAIN_CATEGORIES = frozenset({TaxCategory.EQUITY_STCG, TaxCategory.EQUITY_LTCG})


@dataclass(frozen=True)
class TaxRules:
    """The rates and thresholds the estimate is built from.

    Every field has a default that matches the published FY 2026-27 position;
    override any of them in ``config/reporting.yaml`` (see
    :func:`load_tax_rules`) when a Finance Act moves a number. The defaults
    are intentionally *conservative-but-not-fictional*: they apply the
    statutory rate plus the 4% health & education cess, and they do **not**
    assume a surcharge (which depends on your total income and is yours to
    set).
    """

    # -- capital gains (listed equity, STT paid) --------------------------
    stcg_rate: Decimal = Decimal("0.20")
    ltcg_rate: Decimal = Decimal("0.125")
    ltcg_exemption: Decimal = Decimal("125000")
    #: Holding period boundary: > this many calendar months is long-term.
    ltcg_holding_months: int = 12

    # -- business income (F&O, intraday equity) ---------------------------
    #: Your marginal slab rate. The platform cannot know it, so it does not
    #: pretend to: set it in the config, or accept the 30% top-slab default
    #: (the PRD's "flat 30% for F&O" — a safe upper bound, not a rule).
    business_slab_rate: Decimal = Decimal("0.30")

    # -- on top of the rate ----------------------------------------------
    cess_rate: Decimal = Decimal("0.04")
    surcharge_rate: Decimal = Decimal("0.00")

    # -- compliance thresholds (informational only) -----------------------
    #: s.44AB tax-audit turnover threshold, all-digital receipts/payments.
    audit_turnover_limit: Decimal = Decimal("10000000")
    #: s.44AB threshold when the 95% digital test is not met.
    audit_turnover_limit_cash: Decimal = Decimal("1000000")
    #: s.44AD presumptive ceiling (F&O included) for digital receipts.
    presumptive_turnover_limit: Decimal = Decimal("3000000")
    #: Deemed profit under s.44AD when opting for presumptive taxation.
    presumptive_profit_rate: Decimal = Decimal("0.06")

    #: Human note rendered next to every estimate (kept in config so the
    #: disclaimer can be changed without a code change).
    disclaimer: str = (
        "Estimate only, from platform-recorded trades and the rates in "
        "config/reporting.yaml. Verify with a chartered accountant before filing."
    )

    def __post_init__(self) -> None:
        """Coerce numeric fields to Decimal and fail loudly on nonsense."""
        for name in (
            "stcg_rate",
            "ltcg_rate",
            "business_slab_rate",
            "cess_rate",
            "surcharge_rate",
            "presumptive_profit_rate",
        ):
            value = _decimal(getattr(self, name), name)
            if value < ZERO:
                raise ValueError(f"{name} must not be negative (got {value})")
            if value > Decimal("1"):
                raise ValueError(f"{name} looks like a percentage, not a fraction: {value}")
            object.__setattr__(self, name, value)
        for name in (
            "ltcg_exemption",
            "audit_turnover_limit",
            "audit_turnover_limit_cash",
            "presumptive_turnover_limit",
        ):
            value = _decimal(getattr(self, name), name)
            if value < ZERO:
                raise ValueError(f"{name} must not be negative (got {value})")
            object.__setattr__(self, name, value)
        if int(self.ltcg_holding_months) < 1:
            raise ValueError("ltcg_holding_months must be at least 1")

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe view (strings for every Decimal, for exact round-trips)."""
        return {
            "stcg_rate": str(self.stcg_rate),
            "ltcg_rate": str(self.ltcg_rate),
            "ltcg_exemption": str(self.ltcg_exemption),
            "ltcg_holding_months": int(self.ltcg_holding_months),
            "business_slab_rate": str(self.business_slab_rate),
            "cess_rate": str(self.cess_rate),
            "surcharge_rate": str(self.surcharge_rate),
            "audit_turnover_limit": str(self.audit_turnover_limit),
            "audit_turnover_limit_cash": str(self.audit_turnover_limit_cash),
            "presumptive_turnover_limit": str(self.presumptive_turnover_limit),
            "presumptive_profit_rate": str(self.presumptive_profit_rate),
            "disclaimer": self.disclaimer,
        }


DEFAULT_TAX_RULES = TaxRules()


def add_months(anchor: date, months: int) -> date:
    """``anchor`` shifted by whole calendar months, clamped to month end.

    2026-01-31 + 1 month → 2026-02-28. Used for the 12-month long-term test,
    because "365 days" is wrong across leap years and the Act counts months.
    """
    month_index = anchor.month - 1 + int(months)
    year = anchor.year + month_index // 12
    month = month_index % 12 + 1
    day = anchor.day
    while day > 0:  # clamp 31 → 30/28 as needed
        try:
            return date(year, month, day)
        except ValueError:
            day -= 1
    return date(year, month, 1)  # pragma: no cover - unreachable for day >= 1


def is_long_term(entry: date | None, exit_: date | None, rules: TaxRules | None = None) -> bool:
    """True when a delivery position was held **more than** 12 months.

    Exactly 12 months is *short*-term for listed securities: the law says
    "held for a period of more than twelve months".
    """
    if entry is None or exit_ is None:
        return False
    active = rules or DEFAULT_TAX_RULES
    return exit_ > add_months(entry, int(active.ltcg_holding_months))


def categorize_trade(
    *,
    segment: str | None,
    mode: str | None = None,
    entry_date: date | None = None,
    exit_date: date | None = None,
    rules: TaxRules | None = None,
) -> TaxCategory:
    """Classify one closed trade into its head of income.

    ``segment`` is the fee engine's vocabulary (``options``, ``futures``,
    ``equity_intraday``, ``equity_delivery``) because that is the same string
    the statutory rates are keyed off — one classification, one place.

    Fail-closed: an unknown segment or an unparseable mode returns
    :attr:`TaxCategory.UNCLASSIFIED` and is **never** silently taxed as if it
    were something else. The report lists those trades so they get fixed.
    """
    from backtest.simulator.fees import TradeSegment

    raw_mode = str(mode or "").strip().lower()
    if raw_mode == "paper":
        return TaxCategory.PAPER_EXCLUDED
    try:
        seg = TradeSegment.validate(segment) if segment is not None else None
    except Exception:  # noqa: BLE001 — unknown segment is a classification miss
        return TaxCategory.UNCLASSIFIED
    if seg is None:
        return TaxCategory.UNCLASSIFIED

    if seg in (TradeSegment.FUTURES, TradeSegment.OPTIONS):
        return TaxCategory.FNO_NON_SPECULATIVE
    if seg == TradeSegment.EQUITY_INTRADAY:
        return TaxCategory.EQUITY_SPECULATIVE
    if seg == TradeSegment.EQUITY_DELIVERY:
        active = rules or DEFAULT_TAX_RULES
        if is_long_term(entry_date, exit_date, active):
            return TaxCategory.EQUITY_LTCG
        return TaxCategory.EQUITY_STCG
    return TaxCategory.UNCLASSIFIED  # pragma: no cover - validate() covers the set


@dataclass
class TaxLine:
    """One row of the tax estimate: one category's base, rate and tax."""

    category: TaxCategory
    net_pnl: Decimal
    #: The amount the rate is applied to — see :func:`estimate_tax` for why
    #: this is not always ``net_pnl``.
    taxable_base: Decimal
    rate: Decimal
    tax: Decimal
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "label": self.category.label,
            "net_pnl": float(self.net_pnl),
            "taxable_base": float(self.taxable_base),
            "rate": float(self.rate),
            "tax": float(self.tax),
            "treatment": self.category.treatment,
            "loss_rule": self.category.loss_rule,
            "schedule": self.category.itr_schedule,
            "note": self.note,
        }


@dataclass
class TaxEstimate:
    """Tax on the period's realised book, plus what could not be estimated."""

    lines: list[TaxLine] = field(default_factory=list)
    subtotal: Decimal = ZERO
    cess: Decimal = ZERO
    surcharge: Decimal = ZERO
    total: Decimal = ZERO
    #: Categories with a *negative* net result — money you may carry forward.
    carry_forward: list[dict[str, Any]] = field(default_factory=list)
    #: Categories whose base exceeded the LTCG exemption (for transparency).
    ltcg_exemption_used: Decimal = ZERO
    notes: list[str] = field(default_factory=list)
    rules: TaxRules = DEFAULT_TAX_RULES

    def to_dict(self) -> dict[str, Any]:
        return {
            "lines": [line.to_dict() for line in self.lines],
            "subtotal": float(self.subtotal),
            "cess": float(self.cess),
            "surcharge": float(self.surcharge),
            "total": float(self.total),
            "carry_forward": self.carry_forward,
            "ltcg_exemption_used": float(self.ltcg_exemption_used),
            "notes": self.notes,
            "rules": self.rules.as_dict(),
            "effective_rate": (
                float(self.total / self._taxable_total()) if self._taxable_total() > ZERO else 0.0
            ),
        }

    def _taxable_total(self) -> Decimal:
        return sum((line.taxable_base for line in self.lines if line.taxable_base > ZERO), ZERO)


def estimate_tax(
    by_category: Mapping[TaxCategory, "BusinessBase"],
    rules: TaxRules | None = None,
) -> TaxEstimate:
    """Estimate tax for each category of the period's book.

    ``by_category`` maps a category to a :class:`BusinessBase` — the gross
    P&L, the deductible cost stack and the net result for that bucket.
    (It is a plain object rather than three parallel dicts so the taxable
    base can differ per category without callers re-deriving it.)

    Two deliberate decisions:

    * **Business income is taxed on the net** (gross minus the whole fee
      stack, STT included — s.36(1)(xv)). Taxing the gross would overstate
      the bill by the exact amount the brokers took.
    * **Capital gains are taxed on the gain computed without STT** but with
      the rest of the transfer cost (proviso to s.48). Applying the full fee
      stack here would quietly claim a deduction the Act does not give; the
      per-line ``note`` says so.

    Losses never produce a negative tax (a refund is not what an estimate
    does) — they produce a ``carry_forward`` entry with the correct window.
    """
    active = rules or DEFAULT_TAX_RULES
    lines: list[TaxLine] = []
    carry_forward: list[dict[str, Any]] = []
    notes: list[str] = []
    ltcg_used = ZERO

    for category in TaxCategory:
        base = by_category.get(category)
        if base is None:
            continue
        net = _decimal(base.net, "net")
        taxable_amount = max(_decimal(base.taxable_base, "taxable_base"), ZERO)
        if category == TaxCategory.PAPER_EXCLUDED:
            lines.append(
                TaxLine(
                    category=category,
                    net_pnl=net,
                    taxable_base=ZERO,
                    rate=ZERO,
                    tax=ZERO,
                    note="Simulated book — excluded from the estimate.",
                )
            )
            continue
        if not category.is_taxable:
            notes.append(
                f"{len(getattr(base, 'trade_ids', []) or [])} trade(s) are UNCLASSIFIED "
                "and were left out of the estimate — classify them first."
            )
            continue

        if net <= ZERO:
            lines.append(
                TaxLine(
                    category=category,
                    net_pnl=net,
                    taxable_base=ZERO,
                    rate=ZERO,
                    tax=ZERO,
                    note="Loss — no tax this period." if net < ZERO else "Flat.",
                )
            )
            if net < ZERO:
                carry_forward.append(
                    {
                        "category": category.value,
                        "label": category.label,
                        "amount": float(abs(net)),
                        "rule": category.loss_rule,
                        "its_schedule": category.itr_schedule,
                    }
                )
            continue

        if category == TaxCategory.EQUITY_LTCG:
            taxable = max(taxable_amount - active.ltcg_exemption, ZERO)
            exemption_left = active.ltcg_exemption - taxable_amount
            ltcg_used += min(taxable_amount, active.ltcg_exemption)
            rate = active.ltcg_rate
            note = (
                f"₹{active.ltcg_exemption:,.0f} annual exemption applied"
                if exemption_left >= ZERO
                else f"₹{active.ltcg_exemption:,.0f} annual exemption fully used"
            )
        elif category == TaxCategory.EQUITY_STCG:
            taxable, rate = taxable_amount, active.stcg_rate
            note = "STT paid on the trade is not deductible against a capital gain."
        else:  # business income
            taxable, rate = taxable_amount, active.business_slab_rate
            note = (
                "Slab rate assumed — set business_slab_rate in config/reporting.yaml "
                "to your actual marginal rate."
            )

        lines.append(
            TaxLine(
                category=category,
                net_pnl=net,
                taxable_base=taxable,
                rate=rate,
                tax=_money(taxable * rate),
                note=note,
            )
        )

    subtotal = _money(sum((line.tax for line in lines), ZERO))
    surcharge = _money(subtotal * active.surcharge_rate)
    cess = _money((subtotal + surcharge) * active.cess_rate)
    total = _money(subtotal + surcharge + cess)
    if active.business_slab_rate > ZERO and any(
        line.category.is_business_income and line.taxable_base > ZERO for line in lines
    ):
        notes.append(
            "Business-income tax is a slab estimate, not a rule: it ignores the "
            "basic exemption limit, other income and the s.87A rebate."
        )
    if total > ZERO:
        notes.append(
            "Advance tax applies to business income (s.234B/234C interest if underpaid); "
            "capital gains get the s.234C(1) proviso relief when they are the only "
            "income in a quarter."
        )
    return TaxEstimate(
        lines=lines,
        subtotal=subtotal,
        cess=cess,
        surcharge=surcharge,
        total=total,
        carry_forward=carry_forward,
        ltcg_exemption_used=_money(ltcg_used),
        notes=notes,
        rules=active,
    )


@dataclass
class BusinessBase:
    """One category's gross / costs / net — the input to :func:`estimate_tax`.

    ``taxable_base`` is the amount the rate is applied to and is *not* always
    ``net``: for capital gains it is the gain computed **without** STT
    (proviso to s.48), for business income it is the net after every cost
    (s.36(1)(xv)). The consolidator computes it; :func:`estimate_tax` only
    consumes it, so the deduction rule lives in exactly one place.
    """

    gross: Decimal = ZERO
    #: Everything the broker, exchange and government took.
    costs: Decimal = ZERO
    #: Costs the Act lets you deduct for THIS category (STT excluded for CG).
    deductible_costs: Decimal = ZERO
    #: Gross minus the deductible costs — the rate's base.
    taxable_base: Decimal = ZERO
    #: The money actually realised (full cost stack deducted).
    net: Decimal = ZERO
    trade_ids: list[str] = field(default_factory=list)


def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# Turnover & compliance flags
# ---------------------------------------------------------------------------


def fno_turnover(trades: Iterable[Any]) -> Decimal:
    """F&O turnover under the ICAI absolute-P&L method.

    Turnover is the sum of |profit| and |loss| of every F&O trade — *not* the
    contract value and not the premium collected. This is the number the
    s.44AB audit threshold is compared against (₹1 crore, or ₹10 crore when
    95%+ of receipts and payments are digital, which F&O always is), so
    getting it wrong either way has a cost: overstate it and you buy an
    unnecessary audit, understate it and you invite a s.271B penalty.

    Only trades whose category is :attr:`TaxCategory.FNO_NON_SPECULATIVE`
    count. Both ``tax_category`` and ``segment`` are consulted so the helper
    works on :class:`~backtest.reporting.records.TradeRecord` objects and on
    raw dicts.
    """
    total = ZERO
    for trade in trades:
        category = getattr(trade, "tax_category", None)
        # UNCLASSIFIED means "not classified yet", not "definitely not F&O" —
        # the segment still knows the answer, so fall back to it.
        if category is not None and category not in (
            TaxCategory.FNO_NON_SPECULATIVE,
            TaxCategory.UNCLASSIFIED,
        ):
            continue
        if category in (None, TaxCategory.UNCLASSIFIED):
            segment = getattr(trade, "segment", None) or _dict_get(trade, "segment")
            if str(segment or "").strip().lower() not in ("options", "futures"):
                continue
        gross = getattr(trade, "gross_pnl", None)
        if gross is None:
            gross = _dict_get(trade, "gross_pnl")
        total += abs(_decimal(gross or 0, "gross_pnl"))
    return _money(total)


def turnover_by_category(trades: Iterable[Any]) -> dict[TaxCategory, Decimal]:
    """Absolute-P&L turnover per category (what each head is measured on).

    Records that have not been classified yet are classified on the spot from
    their segment/mode/holding period: silently dropping them would understate
    turnover, and turnover is what the audit threshold is measured on.
    """
    totals: dict[TaxCategory, Decimal] = {}
    for trade in trades:
        category = getattr(trade, "tax_category", None)
        if category is None or category is TaxCategory.UNCLASSIFIED:
            category = _category_from_record(trade)
        if category is None or category is TaxCategory.UNCLASSIFIED:
            continue
        gross = getattr(trade, "gross_pnl", None)
        if gross is None:
            gross = _dict_get(trade, "gross_pnl")
        totals[category] = totals.get(category, ZERO) + abs(_decimal(gross or 0, "gross_pnl"))
    return {key: _money(value) for key, value in totals.items()}


def _category_from_record(trade: Any) -> TaxCategory | None:
    """Best-effort classification for a record that has not been through it."""
    segment = getattr(trade, "segment", None) or _dict_get(trade, "segment")
    if not segment:
        return None
    try:
        return categorize_trade(
            segment=segment,
            mode=getattr(trade, "mode", None) or _dict_get(trade, "mode"),
            entry_date=_record_date(trade, "entry_date", "entry_time"),
            exit_date=_record_date(trade, "exit_date", "exit_time"),
        )
    except Exception:  # noqa: BLE001 — a record we cannot classify is simply skipped
        logger.warning("could not classify record %r for turnover", trade, exc_info=True)
        return None


def _record_date(trade: Any, date_key: str, stamp_key: str) -> Any:
    value = getattr(trade, date_key, None) or _dict_get(trade, date_key)
    if value is None:
        value = getattr(trade, stamp_key, None) or _dict_get(trade, stamp_key)
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


@dataclass
class AuditPosition:
    """Informational s.44AB / s.44AD read-out for the period."""

    fno_turnover: Decimal
    turnover_all_digital: bool
    audit_threshold: Decimal
    audit_required: bool
    presumptive_eligible: bool
    presumptive_profit_required: Decimal
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "fno_turnover": float(self.fno_turnover),
            "turnover_all_digital": self.turnover_all_digital,
            "audit_threshold": float(self.audit_threshold),
            "audit_required": self.audit_required,
            "presumptive_eligible": self.presumptive_eligible,
            "presumptive_profit_required": float(self.presumptive_profit_required),
            "note": self.note,
        }


def audit_position(
    turnover: Decimal,
    *,
    all_digital: bool = True,
    rules: TaxRules | None = None,
) -> AuditPosition:
    """Compare F&O turnover against the audit / presumptive thresholds.

    Informational: a period view cannot know about prior-year s.44AD opt-ins
    or your other income, both of which change the answer. The note says so
    rather than quietly implying "no audit needed".
    """
    active = rules or DEFAULT_TAX_RULES
    limit = active.audit_turnover_limit if all_digital else active.audit_turnover_limit_cash
    required = turnover > limit
    presumptive = turnover <= active.presumptive_turnover_limit and all_digital
    note = (
        f"Digital receipts/payments assumed, so the s.44AB threshold is "
        f"₹{limit:,.0f}. This ignores a prior-year s.44AD opt-in (which can "
        f"trigger an audit at a lower turnover) and your other income."
    )
    return AuditPosition(
        fno_turnover=_money(turnover),
        turnover_all_digital=all_digital,
        audit_threshold=_money(limit),
        audit_required=required,
        presumptive_eligible=presumptive,
        presumptive_profit_required=_money(
            (turnover * active.presumptive_profit_rate) if presumptive else ZERO
        ),
        note=note,
    )


def _dict_get(mapping: Any, key: str) -> Any:
    return mapping.get(key) if isinstance(mapping, Mapping) else None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def resolve_tax_rules(data: Mapping[str, Any] | None) -> TaxRules:
    """Build :class:`TaxRules` from a mapping, ignoring unknown keys noisily."""
    if not data:
        return DEFAULT_TAX_RULES
    known = {f for f in TaxRules.__dataclass_fields__}
    payload = {k: v for k, v in data.items() if k in known}
    unknown = sorted(set(data) - known)
    if unknown:
        logger.warning("ignoring unknown tax config keys: %s", ", ".join(unknown))
    return TaxRules(**payload)


def load_tax_rules(path: str | Path | None = None) -> TaxRules:
    """Load tax rules from YAML; fall back to defaults on any problem.

    Fail-soft on purpose — a typo in a config file must not take down the
    reporting page; it logs and reports the defaults (which are the published
    rates anyway). ``REPORTING_CONFIG_PATH`` overrides the file location.
    """
    candidate = Path(path) if path else Path(os.getenv(RULES_PATH_ENV) or DEFAULT_RULES_PATH)
    if not candidate.exists():
        return DEFAULT_TAX_RULES
    try:
        import yaml

        document = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
        block = document.get("tax") if isinstance(document, Mapping) else None
        return resolve_tax_rules(block if isinstance(block, Mapping) else document)
    except Exception as exc:  # noqa: BLE001 — config problems must not break the page
        logger.warning("could not load tax rules from %s (%s) — using defaults", candidate, exc)
        return DEFAULT_TAX_RULES
