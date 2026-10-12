"""The consolidator — every broker, every mode, one honest P&L statement.

:class:`ConsolidatedPnL` pulls the period's closed trades from every configured
source, classifies them, and answers the four questions a monthly P&L is
actually for:

1. **What did I make before costs?** (``gross_pnl``)
2. **What did the brokers, the exchange and the government take?**
   (itemised ``fees``, plus slippage where the source recorded it)
3. **What is left?** (``net_pnl``)
4. **What will the tax on that be — and which loss can I carry?**
   (``tax``, from :mod:`backtest.reporting.tax`)

It also keeps a running commentary (``warnings``) of everything that made a
number less certain than it looks: estimated cost stacks, trades with no
broker attribution, unclassified instruments, merged duplicates. A P&L report
that cannot say what it does not know is a liability, not a feature.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from backtest.reporting.records import (
    FEE_BASIS_ESTIMATED,
    FEE_BASIS_NONE,
    FEE_BASIS_RECORDED,
    FeeBreakdown,
    Period,
    TradeRecord,
)
from backtest.reporting.sources import (
    SourceBundle,
    resolve_sources,
)
from backtest.reporting.tax import (
    AuditPosition,
    BusinessBase,
    TaxCategory,
    TaxEstimate,
    audit_position,
    estimate_tax,
    fno_turnover,
    load_tax_rules,
    turnover_by_category,
)
from backtest.simulator.money import ZERO

__all__ = ["ConsolidatedPnL", "PnLReport", "CategoryAggregate", "BrokerAggregate"]

logger = logging.getLogger("backtest.reporting.consolidator")


@dataclass
class BrokerAggregate:
    """One broker's line in the report."""

    broker: str
    trades: int = 0
    gross_pnl: Decimal = ZERO
    fees: Decimal = ZERO
    slippage: Decimal = ZERO
    net_pnl: Decimal = ZERO
    share_pct: float = 0.0
    modes: list[str] = field(default_factory=list)
    estimated_fee_trades: int = 0

    def to_dict(self, *, label_key: str = "broker") -> dict[str, Any]:
        """Serialise the row under whatever dimension it was grouped by.

        The same row type backs ``by_broker``/``by_mode``/``by_strategy``; keying
        every one of them as ``broker`` made ``by_mode`` entries claim a broker
        name that is actually a mode.
        """
        return {
            label_key: self.broker,
            "trades": self.trades,
            "gross_pnl": float(self.gross_pnl),
            "fees": float(self.fees),
            "slippage": float(self.slippage),
            "net_pnl": float(self.net_pnl),
            "share_pct": round(self.share_pct, 2),
            "modes": self.modes,
            "estimated_fee_trades": self.estimated_fee_trades,
        }


@dataclass
class CategoryAggregate:
    """One tax category's line: what it made and what it will be taxed on."""

    category: TaxCategory
    trades: int = 0
    gross_pnl: Decimal = ZERO
    fees: Decimal = ZERO
    slippage: Decimal = ZERO
    net_pnl: Decimal = ZERO
    turnover: Decimal = ZERO
    estimated_tax: Decimal = ZERO
    rate: Decimal = ZERO

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "label": self.category.label,
            "treatment": self.category.treatment,
            "loss_rule": self.category.loss_rule,
            "schedule": self.category.itr_schedule,
            "trades": self.trades,
            "gross_pnl": float(self.gross_pnl),
            "fees": float(self.fees),
            "slippage": float(self.slippage),
            "net_pnl": float(self.net_pnl),
            "turnover": float(self.turnover),
            "estimated_tax": float(self.estimated_tax),
            "rate": float(self.rate),
        }


@dataclass
class PnLReport:
    """A consolidated P&L statement for one period."""

    period: Period
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    trades: list[TradeRecord] = field(default_factory=list)
    include_paper: bool = True
    brokers: list[str] | None = None

    gross_pnl: Decimal = ZERO
    fees: FeeBreakdown = field(default_factory=FeeBreakdown)
    slippage: Decimal = ZERO
    net_pnl: Decimal = ZERO

    by_broker: list[BrokerAggregate] = field(default_factory=list)
    by_mode: list[BrokerAggregate] = field(default_factory=list)
    by_category: list[CategoryAggregate] = field(default_factory=list)
    by_strategy: list[BrokerAggregate] = field(default_factory=list)

    tax: TaxEstimate = field(default_factory=TaxEstimate)
    turnover: dict[str, float] = field(default_factory=dict)
    audit: AuditPosition | None = None

    trade_count: int = 0
    winners: int = 0
    losers: int = 0
    breakeven: int = 0
    win_rate: float = 0.0
    avg_win: Decimal = ZERO
    avg_loss: Decimal = ZERO
    profit_factor: float | None = None
    best_trade: Decimal = ZERO
    worst_trade: Decimal = ZERO

    cost_basis: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    data_notes: list[str] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    discrepancies: list[dict[str, Any]] = field(default_factory=list)
    #: True when any part of this report came from the demo book.
    demo: bool = False

    # -- derived -----------------------------------------------------------

    @property
    def total_costs(self) -> Decimal:
        return _money(self.fees.total + self.slippage)

    @property
    def net_after_tax(self) -> Decimal:
        return _money(self.net_pnl - self.tax.total)

    @property
    def cost_drag_pct(self) -> float:
        """Costs + tax as a share of gross profit (the 'reality check' number)."""
        if self.gross_pnl <= ZERO:
            return 0.0
        return float((self.total_costs + self.tax.total) / self.gross_pnl * Decimal("100"))

    @property
    def live_net_pnl(self) -> Decimal:
        return _money(
            sum((t.net_pnl for t in self.trades if t.mode == "live"), ZERO)
        )

    @property
    def paper_net_pnl(self) -> Decimal:
        return _money(sum((t.net_pnl for t in self.trades if t.mode == "paper"), ZERO))

    def fee_rows(self) -> list[dict[str, Any]]:
        """Aggregated fee components in contract-note order."""
        return _fee_rows(self.fees)

    def summary(self) -> dict[str, Any]:
        """The headline ladder: gross → costs → net → tax → after tax."""
        return {
            "gross_pnl": float(self.gross_pnl),
            "fees": float(self.fees.total),
            "slippage": float(self.slippage),
            "total_costs": float(self.total_costs),
            "net_pnl": float(self.net_pnl),
            "tax": float(self.tax.total),
            "net_after_tax": float(self.net_after_tax),
            "cost_drag_pct": round(self.cost_drag_pct, 2),
        }

    def to_dict(self, *, include_trades: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "period": self.period.to_dict(),
            "generated_at": self.generated_at.isoformat(),
            "include_paper": self.include_paper,
            "brokers": self.brokers,
            "demo": self.demo,
            "summary": self.summary(),
            "fee_rows": self.fee_rows(),
            "by_broker": [row.to_dict() for row in self.by_broker],
            "by_mode": [row.to_dict(label_key="mode") for row in self.by_mode],
            "by_category": [row.to_dict() for row in self.by_category],
            "by_strategy": [row.to_dict(label_key="strategy") for row in self.by_strategy],
            "tax": self.tax.to_dict(),
            "turnover": self.turnover,
            "audit": self.audit.to_dict() if self.audit else None,
            "stats": {
                "trade_count": self.trade_count,
                "winners": self.winners,
                "losers": self.losers,
                "breakeven": self.breakeven,
                "win_rate": round(self.win_rate, 4),
                "avg_win": float(self.avg_win),
                "avg_loss": float(self.avg_loss),
                "profit_factor": self.profit_factor,
                "best_trade": float(self.best_trade),
                "worst_trade": float(self.worst_trade),
                "live_net_pnl": float(self.live_net_pnl),
                "paper_net_pnl": float(self.paper_net_pnl),
            },
            "cost_basis": self.cost_basis,
            "warnings": self.warnings,
            "data_notes": self.data_notes,
            "sources": self.sources,
            "discrepancies": self.discrepancies,
        }
        if include_trades:
            payload["trades"] = [trade.to_dict() for trade in self.trades]
        return payload


class ConsolidatedPnL:
    """Builds a :class:`PnLReport` from every configured trade source."""

    def __init__(
        self,
        *,
        manager: Any = None,
        db_manager: Any = None,
        fee_calculator: Any = None,
        sources: Sequence[Any] | None = None,
    ) -> None:
        self._manager = manager
        self._db_manager = db_manager
        self._fee_calculator = fee_calculator
        self._sources = list(sources) if sources else None

    # -- public API --------------------------------------------------------

    def generate_report(
        self,
        from_date: date | str | None = None,
        to_date: date | str | None = None,
        *,
        include_paper: bool = True,
        brokers: Sequence[str] | None = None,
        demo: bool = False,
        include_trades: bool = True,
    ) -> PnLReport:
        """Consolidate the period's closed trades into a report.

        Parameters
        ----------
        from_date / to_date:
            Inclusive bounds (``YYYY-MM-DD`` or :class:`~datetime.date`). Omit
            both for financial-year-to-date — the window a trader actually
            needs mid-year.
        include_paper:
            Whether simulated books take part. They are **always** excluded
            from the tax estimate, whatever this says.
        brokers:
            Optional venue filter (``["mstock", "dhan"]``); ``None`` = all.
        demo:
            Serve the labelled demo book (previews/tests only).
        """
        rules = load_tax_rules()
        period = Period.parse(from_date, to_date)
        bundle = self._resolved_sources(demo)
        records, source_warnings = bundle.fetch(period)
        report = PnLReport(
            period=period,
            include_paper=include_paper,
            brokers=list(brokers) if brokers else None,
            warnings=list(source_warnings),
            sources=bundle.describe(),
            demo=any(t.source == "demo" for t in records),
        )

        records, merge_note = _dedupe(records)
        if merge_note:
            report.data_notes.append(merge_note)

        selected = _select(records, include_paper=include_paper, brokers=brokers, report=report)
        for trade in selected:
            trade.classify(rules)

        utc_min = datetime.min.replace(tzinfo=timezone.utc)

        def _sort_key(t: Any) -> tuple:
            et = t.exit_time
            if et is None:
                return (utc_min, t.symbol)
            return (et if et.tzinfo is not None else et.replace(tzinfo=timezone.utc), t.symbol)

        report.trades = sorted(selected, key=_sort_key)
        self._aggregate(report, rules)
        self._collect_caveats(report, records, selected)
        if not include_trades:
            report.trades = []
        return report

    # -- internals ---------------------------------------------------------

    def _resolved_sources(self, demo: bool) -> SourceBundle:
        if self._sources is not None:
            return SourceBundle(sources=list(self._sources))
        return resolve_sources(
            manager=self._manager,
            db_manager=self._db_manager,
            fee_calculator=self._fee_calculator,
            demo=demo,
        )

    def _aggregate(self, report: PnLReport, rules: Any) -> None:
        trades = report.trades
        report.gross_pnl = _money(sum((t.gross_pnl for t in trades), ZERO))
        report.fees = _sum_fees(trades)
        report.slippage = _money(sum((t.slippage for t in trades), ZERO))
        report.net_pnl = _money(sum((t.net_pnl for t in trades), ZERO))
        report.trade_count = len(trades)

        pnls = [t.net_pnl for t in trades]
        wins = [p for p in pnls if p > ZERO]
        losses = [p for p in pnls if p < ZERO]
        report.winners = len(wins)
        report.losers = len(losses)
        report.breakeven = len(pnls) - len(wins) - len(losses)
        report.win_rate = (len(wins) / len(pnls)) if pnls else 0.0
        report.avg_win = _money(sum(wins, ZERO) / len(wins)) if wins else ZERO
        report.avg_loss = _money(sum(losses, ZERO) / len(losses)) if losses else ZERO
        gross_profit = sum(wins, ZERO)
        gross_loss = abs(sum(losses, ZERO))
        report.profit_factor = (
            float(gross_profit / gross_loss) if gross_loss > ZERO else None
        )
        report.best_trade = max(pnls) if pnls else ZERO
        report.worst_trade = min(pnls) if pnls else ZERO

        report.by_broker = _group(trades, key=lambda t: t.broker or "unattributed")
        report.by_mode = _group(trades, key=lambda t: t.mode or "paper")
        report.by_strategy = _group(trades, key=lambda t: t.strategy or "(no strategy)")
        _apply_shares(report.by_broker)
        _apply_shares(report.by_mode)
        _apply_shares(report.by_strategy)

        bases = _category_bases(trades)
        report.tax = estimate_tax(bases, rules)
        tax_by_category = {line.category: line for line in report.tax.lines}
        turnover = turnover_by_category(trades)
        report.turnover = {
            category.value: float(amount) for category, amount in turnover.items()
        }
        report.by_category = _category_rows(trades, bases, tax_by_category, turnover)
        report.audit = audit_position(fno_turnover(trades), rules=rules)
        report.cost_basis = _cost_basis(trades)

    def _collect_caveats(
        self,
        report: PnLReport,
        all_records: list[TradeRecord],
        selected: list[TradeRecord],
    ) -> None:
        """Say out loud everything that made a number less certain."""
        estimated = [t for t in selected if t.fees_basis == FEE_BASIS_ESTIMATED]
        if estimated:
            report.warnings.append(
                f"{len(estimated)} of {len(selected)} trades carry an ESTIMATED fee "
                "stack (the source did not record the statutory charges) — the cost "
                "line is modelled, not observed"
            )
        unattributed = [t for t in selected if t.broker == "unattributed"]
        if unattributed:
            report.warnings.append(
                f"{len(unattributed)} trade(s) have no broker attribution — they are "
                "reported under 'unattributed' rather than assigned to a broker"
            )
        unclassified = [t for t in selected if t.tax_category == TaxCategory.UNCLASSIFIED]
        if unclassified:
            report.warnings.append(
                f"{len(unclassified)} trade(s) could not be classified for tax "
                "(unknown segment) and are excluded from the estimate"
            )
        residuals = _money(sum((t.ladder_residual for t in selected), ZERO))
        if residuals != ZERO:
            report.warnings.append(
                f"reported net P&L and the cost ladder disagree by ₹{residuals:,.2f} "
                "across the period — see the per-trade 'ladder_residual' column"
            )
        simulated = [t for t in selected if t.tag == "SIMULATED DATA"]
        if simulated:
            report.warnings.append(
                f"{len(simulated)} trade(s) come from synthetic/replay data and must "
                "never be read as real fills"
            )
        if report.demo:
            report.warnings.append(
                "DEMO BOOK: this report contains explicitly-labelled sample trades "
                "and is not a record of any real account"
            )
        excluded = [t for t in all_records if t not in selected]
        if excluded:
            paper = sum(1 for t in excluded if t.mode == "paper")
            broker_filtered = len(excluded) - paper
            bits = []
            if paper:
                bits.append(f"{paper} paper trade(s) excluded by request")
            if broker_filtered:
                bits.append(f"{broker_filtered} excluded by the broker filter")
            report.data_notes.append("; ".join(bits))
        if not selected:
            report.data_notes.append(
                "no closed trades in this period — check the date range, the "
                "include-paper toggle and whether live runners have closed anything"
            )


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def _select(
    records: Iterable[TradeRecord],
    *,
    include_paper: bool,
    brokers: Sequence[str] | None,
    report: PnLReport,
) -> list[TradeRecord]:
    wanted = {str(b).strip().lower() for b in (brokers or []) if str(b).strip()}
    out: list[TradeRecord] = []
    for trade in records:
        if not include_paper and trade.mode == "paper":
            continue
        if wanted and trade.broker.lower() not in wanted:
            continue
        out.append(trade)
    if wanted:
        missing = sorted(wanted - {t.broker.lower() for t in out})
        if missing:
            report.data_notes.append(
                "broker filter matched nothing for: " + ", ".join(missing)
            )
    return out


def _dedupe(records: Sequence[TradeRecord]) -> tuple[list[TradeRecord], str]:
    """Collapse the same trade seen by two sources (memory + DB).

    Natural key: symbol + exit time + quantity + gross P&L. The in-memory
    record wins because it is the book's own view of the trade (and carries
    the richer cost detail for live option structures).
    """
    best: dict[tuple[Any, ...], TradeRecord] = {}
    order: list[tuple[Any, ...]] = []
    for trade in records:
        key = (
            trade.symbol,
            trade.exit_time.isoformat() if trade.exit_time else None,
            str(trade.quantity),
            str(_money(trade.gross_pnl)),
        )
        current = best.get(key)
        if current is None:
            best[key] = trade
            order.append(key)
            continue
        if current.source == "db" and trade.source == "memory":
            best[key] = trade
    merged = len(records) - len(order)
    note = (
        f"{merged} duplicate record(s) merged across sources (memory book wins)"
        if merged
        else ""
    )
    return [best[key] for key in order], note


def _sum_fees(trades: Iterable[TradeRecord]) -> FeeBreakdown:
    total = FeeBreakdown()
    for trade in trades:
        total = total + trade.fees
    return total


def _fee_rows(fees: FeeBreakdown) -> list[dict[str, Any]]:
    from backtest.reporting.records import FEE_DISPLAY_ORDER, FEE_LABELS

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key in FEE_DISPLAY_ORDER:
        seen.add(key)
        value = fees.get(key)
        if value:
            rows.append({"key": key, "label": FEE_LABELS.get(key, key), "amount": float(value)})
    for key, value in fees.components.items():
        if key in seen or FeeBreakdown._is_leg_key(key):  # noqa: SLF001
            continue
        if isinstance(value, Decimal) and value:
            rows.append({"key": key, "label": FEE_LABELS.get(key, key), "amount": float(value)})
    return rows


def _group(trades: Sequence[TradeRecord], *, key: Any) -> list[BrokerAggregate]:
    buckets: dict[str, BrokerAggregate] = {}
    for trade in trades:
        label = str(key(trade) or "unattributed")
        row = buckets.get(label)
        if row is None:
            row = buckets[label] = BrokerAggregate(broker=label)
        row.trades += 1
        row.gross_pnl += trade.gross_pnl
        row.fees += trade.fees_total
        row.slippage += trade.slippage_total
        row.net_pnl += trade.net_pnl
        if trade.mode not in row.modes:
            row.modes.append(trade.mode)
        if trade.fees_basis == FEE_BASIS_ESTIMATED:
            row.estimated_fee_trades += 1
    rows = []
    for row in buckets.values():
        row.gross_pnl = _money(row.gross_pnl)
        row.fees = _money(row.fees)
        row.slippage = _money(row.slippage)
        row.net_pnl = _money(row.net_pnl)
        rows.append(row)
    rows.sort(key=lambda r: r.net_pnl, reverse=True)
    return rows


def _apply_shares(rows: Sequence[BrokerAggregate]) -> None:
    positive = sum((r.net_pnl for r in rows if r.net_pnl > ZERO), ZERO)
    if positive <= ZERO:
        return
    for row in rows:
        row.share_pct = float(max(row.net_pnl, ZERO) / positive * Decimal("100"))


def _category_bases(trades: Sequence[TradeRecord]) -> dict[TaxCategory, BusinessBase]:
    bases: dict[TaxCategory, BusinessBase] = {}
    for trade in trades:
        base = bases.setdefault(trade.tax_category, BusinessBase())
        base.gross += trade.gross_pnl
        base.costs += trade.total_costs
        base.net += trade.net_pnl
        # STT is deductible against business income (s.36(1)(xv)) but NOT
        # against a capital gain (proviso to s.48) — the one deduction that
        # flips with the category, so it is applied here and nowhere else.
        deductible = trade.total_costs
        if trade.tax_category.is_capital_gain:
            deductible -= trade.fees.get("stt")
        base.deductible_costs += deductible
        base.trade_ids.append(trade.trade_id)
    for base in bases.values():
        base.gross = _money(base.gross)
        base.costs = _money(base.costs)
        base.deductible_costs = _money(base.deductible_costs)
        base.net = _money(base.net)
        base.taxable_base = _money(base.gross - base.deductible_costs)
    return bases


def _category_rows(
    trades: Sequence[TradeRecord],
    bases: Mapping[TaxCategory, BusinessBase],
    tax_by_category: Mapping[TaxCategory, Any],
    turnover: Mapping[TaxCategory, Decimal],
) -> list[CategoryAggregate]:
    counts: dict[TaxCategory, int] = {}
    for trade in trades:
        counts[trade.tax_category] = counts.get(trade.tax_category, 0) + 1
    rows: list[CategoryAggregate] = []
    for category in TaxCategory:
        base = bases.get(category)
        if base is None:
            continue
        line = tax_by_category.get(category)
        rows.append(
            CategoryAggregate(
                category=category,
                trades=counts.get(category, 0),
                gross_pnl=base.gross,
                fees=_money(sum(
                    (t.fees_total for t in trades if t.tax_category == category), ZERO
                )),
                slippage=_money(sum(
                    (t.slippage_total for t in trades if t.tax_category == category), ZERO
                )),
                net_pnl=base.net,
                turnover=turnover.get(category, ZERO),
                estimated_tax=line.tax if line is not None else ZERO,
                rate=line.rate if line is not None else ZERO,
            )
        )
    rows.sort(key=lambda r: r.net_pnl, reverse=True)
    return rows


def _cost_basis(trades: Sequence[TradeRecord]) -> dict[str, int]:
    counts = {FEE_BASIS_RECORDED: 0, FEE_BASIS_ESTIMATED: 0, FEE_BASIS_NONE: 0}
    for trade in trades:
        counts[trade.fees_basis] = counts.get(trade.fees_basis, 0) + 1
    return counts


def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"))
