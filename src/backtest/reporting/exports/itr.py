"""ITR annexures: what to hand a chartered accountant, and what is *not* true.

Three schedules plus a guidance sheet, built from the consolidated report:

* **Capital gains** — one row per delivery trade in the shape Schedule CG wants
  (A3 for STCG at special rates, B3 for LTCG), with the transfer expenses
  computed *without* STT, because the Act does not allow it.
* **PGBP statement** — the P&L statement a business-income trader attaches to
  ITR-3, split into non-speculative (F&O) and speculative (intraday equity),
  with turnover on the ICAI absolute-P&L basis and the deductible cost heads
  the law actually allows.
* **STT summary** — how much STT was paid, per head, and whether it is
  deductible (it is, for business income; it is not, for capital gains).
* **Guidance** — the form/schedule mapping and the corrections to the common
  myths. This is where PRD-002's "ITR-4 ready" idea gets fixed: F&O and equity
  intraday are business income → **ITR-3** (ITR-4 only under the s.44AD
  presumptive scheme, which is unavailable if you also have capital gains), and
  a delivery book on its own is ITR-2.

Every sheet carries the estimate disclaimer, and every number is traceable to
the trade rows in the main export.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from backtest.reporting.consolidator import PnLReport
from backtest.reporting.exports.xlsx import Workbook
from backtest.reporting.tax import TaxCategory
from backtest.simulator.money import ZERO

__all__ = [
    "capital_gains_schedule",
    "fno_pnl_statement",
    "stt_summary",
    "turnover_sheet",
    "itr_guidance",
    "ITR_ANNEXURES",
    "build_workbook",
]

logger = logging.getLogger("backtest.reporting.exports.itr")

_DISCLAIMER = (
    "ESTIMATE — generated from platform-recorded trades with the rates in config/reporting.yaml. "
    "This is not tax advice: verify every figure against broker contract notes and your "
    "chartered accountant (CA) before filing."
)


def _money(value: Decimal | float | int) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01")))


def _capital_gain_trades(report: PnLReport) -> list[Any]:
    return [
        trade
        for trade in report.trades
        if trade.tax_category in (TaxCategory.EQUITY_STCG, TaxCategory.EQUITY_LTCG)
    ]


def capital_gains_schedule(report: PnLReport) -> list[list[Any]]:
    """Schedule CG rows (A3 short-term / B3 long-term) from delivery trades."""
    rows: list[list[Any]] = [
        [
            "Sr.",
            "Description of asset",
            "Date of acquisition",
            "Date of sale",
            "Sale consideration",
            "Cost of acquisition",
            "Expenses on transfer (excl. STT)",
            "Gain",
            "Type",
            "Schedule",
        ]
    ]
    short_total = Decimal("0")
    long_total = Decimal("0")
    for index, trade in enumerate(_capital_gain_trades(report), start=1):
        sale = Decimal(str(trade.quantity)) * Decimal(str(trade.exit_price))
        cost = Decimal(str(trade.quantity)) * Decimal(str(trade.entry_price))
        # Everything the broker charged, minus STT: the proviso to s.48 bars
        # an STT deduction against a capital gain, and every rupee of it
        # matters on delivery where STT is 0.1% on both legs.
        transfer_costs = trade.total_costs - trade.fees.get("stt")
        gain = sale - cost - transfer_costs
        is_long = trade.tax_category == TaxCategory.EQUITY_LTCG
        if is_long:
            long_total += gain
        else:
            short_total += gain
        rows.append(
            [
                index,
                f"{trade.symbol} ({float(trade.quantity):,.0f} @ {float(trade.entry_price):,.2f})",
                trade.entry_date.isoformat() if trade.entry_date else "—",
                trade.exit_date.isoformat() if trade.exit_date else "—",
                _money(sale),
                _money(cost),
                _money(transfer_costs),
                _money(gain),
                "Long-term" if is_long else "Short-term",
                "B3" if is_long else "A3",
            ]
        )
    rows.append([])
    rows.append(["", "Total short-term (Schedule CG A3)", "", "", "", "", "", _money(short_total)])
    rows.append(["", "Total long-term (Schedule CG B3)", "", "", "", "", "", _money(long_total)])
    if report.tax.ltcg_exemption_used > ZERO:
        rows.append(
            [
                "",
                "LTCG exemption applied (s.112A)",
                "",
                "",
                "",
                "",
                "",
                _money(-report.tax.ltcg_exemption_used),
            ]
        )
    rows.append([])
    rows.append(
        [
            "",
            "Note: STT paid on these trades is NOT deductible against a capital gain "
            "(proviso to s.48). Brokerage and other transfer expenses are included above.",
        ]
    )
    rows.append(["", _DISCLAIMER])
    return rows


def fno_pnl_statement(report: PnLReport) -> list[list[Any]]:
    """The PGBP statement for ITR-3, split non-speculative / speculative."""
    rows: list[list[Any]] = [
        ["Particulars", "Non-speculative (F&O)", "Speculative (intraday equity)"]
    ]
    blocks = (
        (TaxCategory.FNO_NON_SPECULATIVE, "Non-speculative business (F&O)"),
        (TaxCategory.EQUITY_SPECULATIVE, "Speculative business (intraday equity)"),
    )
    aggregates: dict[TaxCategory, dict[str, Decimal]] = {}
    for category, _label in blocks:
        trades = [t for t in report.trades if t.tax_category == category]
        aggregates[category] = {
            "turnover": sum((abs(t.gross_pnl) for t in trades), ZERO),
            "gross": sum((t.gross_pnl for t in trades), ZERO),
            "brokerage": sum((t.fees.get("brokerage") for t in trades), ZERO),
            "stt": sum((t.fees.get("stt") for t in trades), ZERO),
            "exchange": sum(
                (
                    t.fees.get("exchange_transaction")
                    + t.fees.get("sebi_turnover")
                    + t.fees.get("ipft")
                    + t.fees.get("dp_charges")
                    for t in trades
                ),
                ZERO,
            ),
            "stamp": sum((t.fees.get("stamp_duty") for t in trades), ZERO),
            "gst": sum((t.fees.get("gst") for t in trades), ZERO),
            "slippage": sum((t.slippage for t in trades), ZERO),
            "net": sum((t.net_pnl for t in trades), ZERO),
            "trades": Decimal(len(trades)),
        }

    def pair(label: str, key: str | None = None) -> list[Any]:
        field = key or label
        return [
            label,
            _money(aggregates[TaxCategory.FNO_NON_SPECULATIVE][field]),
            _money(aggregates[TaxCategory.EQUITY_SPECULATIVE][field]),
        ]

    rows.append(pair("Trades", "trades"))
    rows.append(pair("Turnover (absolute P&L, ICAI basis)", "turnover"))
    rows.append(pair("Gross profit / (loss)", "gross"))
    rows.append(["Less: deductible expenses", "", ""])
    rows.append(pair("  Brokerage", "brokerage"))
    rows.append(pair("  STT / CTT (s.36(1)(xv))", "stt"))
    rows.append(pair("  Exchange / SEBI / IPFT charges", "exchange"))
    rows.append(pair("  Stamp duty", "stamp"))
    rows.append(pair("  GST on brokerage and charges", "gst"))
    rows.append(pair("  Slippage (recorded, embedded in prices)", "slippage"))
    rows.append([])
    rows.append(pair("Net profit / (loss) for PGBP", "net"))
    rows.append([])
    rows.append(
        [
            "Speculative and non-speculative books are reported separately: a speculative loss "
            "can only absorb speculative profit, while a non-speculative loss can absorb most "
            "other income except salary.",
        ]
    )
    rows.append(["", _DISCLAIMER])
    return rows


def stt_summary(report: PnLReport) -> list[list[Any]]:
    """STT paid per head and whether it is deductible."""
    rows: list[list[Any]] = [
        ["Category", "STT / CTT paid", "Deductible?", "Where it goes"],
    ]
    deductibility = {
        TaxCategory.FNO_NON_SPECULATIVE: ("Yes — s.36(1)(xv)", "Deduct from business income"),
        TaxCategory.EQUITY_SPECULATIVE: ("Yes — s.36(1)(xv)", "Deduct from business income"),
        TaxCategory.EQUITY_STCG: ("No — proviso to s.48", "Not deductible against the gain"),
        TaxCategory.EQUITY_LTCG: ("No — proviso to s.48", "Not deductible against the gain"),
        TaxCategory.PAPER_EXCLUDED: ("n/a", "Simulated book"),
    }
    total = Decimal("0")
    for category, (deductible, where) in deductibility.items():
        trades = [t for t in report.trades if t.tax_category == category]
        if not trades:
            continue
        stt = sum((t.fees.get("stt") for t in trades), ZERO)
        total += stt
        rows.append([category.label, _money(stt), deductible, where])
    rows.append([])
    rows.append(["Total STT / CTT paid", _money(total)])
    rows.append([])
    rows.append(["", _DISCLAIMER])
    return rows


def turnover_sheet(report: PnLReport) -> list[list[Any]]:
    """Turnover by head plus the s.44AB / s.44AD read-out."""
    rows: list[list[Any]] = [["Head", "Turnover (absolute P&L)", "Measured against"]]
    for category in TaxCategory:
        if category in (TaxCategory.PAPER_EXCLUDED, TaxCategory.UNCLASSIFIED):
            continue
        trades = [t for t in report.trades if t.tax_category == category]
        if not trades:
            continue
        measured = (
            "s.44AB audit threshold"
            if category == TaxCategory.FNO_NON_SPECULATIVE
            else "Reported separately (no audit threshold of its own)"
        )
        rows.append(
            [
                category.label,
                _money(sum((abs(t.gross_pnl) for t in trades), ZERO)),
                measured,
            ]
        )
    rows.append([])
    audit = report.audit
    if audit is not None:
        rows.append(["F&O turnover", _money(audit.fno_turnover)])
        rows.append(["Audit threshold used", _money(audit.audit_threshold)])
        rows.append(
            [
                "Audit flag",
                "REVIEW — turnover above threshold"
                if audit.audit_required
                else "below threshold",
            ]
        )
        rows.append(
            [
                "Presumptive s.44AD",
                f"eligible up to {_money(audit.presumptive_profit_required)} deemed profit"
                if audit.presumptive_eligible
                else "turnover above the presumptive ceiling",
            ]
        )
        rows.append(["", audit.note])
    rows.append([])
    rows.append(["", _DISCLAIMER])
    return rows


def itr_guidance(report: PnLReport) -> list[list[Any]]:
    """Which form, which schedules, and the myths this report refuses to repeat."""
    has_business = any(t.tax_category.is_business_income for t in report.trades)
    has_capital = any(t.tax_category.is_capital_gain for t in report.trades)
    if has_business and has_capital:
        form = "ITR-3"
        why = (
            "business income (F&O / intraday) plus capital gains cannot be reported on ITR-2, "
            "and the s.44AD presumptive route (ITR-4) cannot carry capital gains either"
        )
    elif has_business:
        form = "ITR-3 (ITR-4 only if you deliberately opt for s.44AD presumptive taxation)"
        why = (
            "F&O and intraday equity are business income; books and a P&L are expected even "
            "when no audit is due"
        )
    elif has_capital:
        form = "ITR-2"
        why = "capital gains with no business income"
    else:
        form = "n/a"
        why = "no taxable trades were found in this period"

    rows: list[list[Any]] = [["Item", "Answer"]]
    rows.append(["Recommended form", form])
    rows.append(["Why", why])
    if has_capital:
        rows.append(
            [
                "Capital gains schedule",
                "Schedule CG — A3 (STCG at 20% on listed equity with STT) and B3 (LTCG at 12.5% "
                "above the Rs 1,25,000 annual exemption). Use the 'Capital gains' sheet.",
            ]
        )
    if has_business:
        rows.append(
            [
                "Business schedule",
                "Schedule BP (non-speculative for F&O, speculative for intraday equity) with the "
                "P&L, balance sheet and the ITR-3 'Part A - P&L' figures. Use the 'PGBP' sheet.",
            ]
        )
        rows.append(
            [
                "Books of account",
                "Required from the start of the year in which turnover crosses Rs 25 lakh or "
                "income crosses Rs 2.5 lakh (s.44AA).",
            ]
        )
        rows.append(
            [
                "Advance tax",
                "Applies to business income (quarterly instalments); if capital gains are the "
                "only income in a quarter, the s.234C proviso may relieve the interest.",
            ]
        )
    if report.tax.carry_forward:
        rows.append(
            [
                "Losses to carry",
                "; ".join(
                    f"{entry['label']}: Rs {entry['amount']:,.2f} ({entry['rule']})"
                    for entry in report.tax.carry_forward
                ),
            ]
        )
    rows.append([])
    rows.append(["Common mistake", "Why it matters here"])
    rows.append(
        [
            "Treating F&O as capital gains",
            "F&O is non-speculative BUSINESS income (proviso (d) to s.43(5)) — filing it under "
            "Schedule CG overpays at flat rates on profits and, far worse, converts an "
            "8-year loss carry-forward against ordinary income into an 8-year capital-loss carry.",
        ]
    )
    rows.append(
        [
            "Treating intraday equity as STCG",
            "Intraday equity is SPECULATIVE business income at slab rates, not s.111A STCG; its "
            "losses can only absorb speculative profits and lapse after 4 years.",
        ]
    )
    rows.append(
        [
            "Claiming STT against capital gains",
            "Only business income may deduct STT (s.36(1)(xv)); the proviso to s.48 bars it "
            "against a capital gain.",
        ]
    )
    rows.append([])
    rows.append(["", _DISCLAIMER])
    return rows


#: Annexure name → builder used by the API/UI.
ITR_ANNEXURES = {
    "capital_gains": ("Capital gains (Schedule CG)", capital_gains_schedule),
    "pgbp": ("PGBP statement (ITR-3)", fno_pnl_statement),
    "stt": ("STT summary", stt_summary),
    "turnover": ("Turnover & audit", turnover_sheet),
    "guidance": ("Filing guidance", itr_guidance),
}


def build_workbook(report: PnLReport, annexures: list[str] | None = None) -> Workbook:
    """Assemble the requested annexures into one workbook."""
    workbook = Workbook()
    selected = annexures or list(ITR_ANNEXURES)
    for key in selected:
        entry = ITR_ANNEXURES.get(key)
        if entry is None:
            logger.warning("unknown ITR annexure %r requested", key)
            continue
        title, builder = entry
        workbook.add_sheet(title, builder(report))
    if not workbook.sheets:
        workbook.add_sheet("ITR annexures", [["No annexure selected"]])
    return workbook
