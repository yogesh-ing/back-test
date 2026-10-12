"""Consolidated P&L & tax reporting (PRD-002).

One report across every broker account, every mode (paper/live) and every
asset class — with the cost stack and the tax categorisation done honestly,
because those are the two places a trader's self-reported P&L usually lies.

Layout:

* :mod:`backtest.reporting.tax` — Indian tax classification + estimation
  (the rules, the rates, and the traps: F&O is *non-speculative* business
  income, intraday equity is *speculative*, STT is deductible for business
  income but **not** for capital gains).
* :mod:`backtest.reporting.records` — the normalised :class:`TradeRecord`
  every source produces, plus the Indian financial-year clock.
* :mod:`backtest.reporting.sources` — where trades come from: the ``trades``
  DB table, the live in-memory books, or an explicitly-labelled demo book.
* :mod:`backtest.reporting.consolidator` — :class:`ConsolidatedPnL`, the
  service that aggregates sources into a :class:`PnLReport`.
* :mod:`backtest.reporting.reconciliation` — platform P&L vs contract note.
* :mod:`backtest.reporting.exports` — PDF / Excel / CSV annexures built with
  the standard library only (no reportlab / openpyxl requirement).
* :mod:`backtest.reporting.email_report` — the monthly report email.

Design rule that runs through all of it: **a number the platform did not
observe is never presented as if it had been observed.** Estimated statutory
fees, estimated tax and untrusted ``net_pnl`` columns are all flagged in
``PnLReport.warnings`` / ``data_notes`` and rendered as such.
"""

from backtest.reporting.consolidator import ConsolidatedPnL, PnLReport
from backtest.reporting.records import FeeBreakdown, Period, TradeRecord
from backtest.reporting.reconciliation import BrokerReconciliation, ReconciliationResult
from backtest.reporting.tax import (
    TaxCategory,
    TaxEstimate,
    TaxLine,
    TaxRules,
    categorize_trade,
    estimate_tax,
    fno_turnover,
    load_tax_rules,
)

__all__ = [
    "ConsolidatedPnL",
    "PnLReport",
    "TradeRecord",
    "Period",
    "FeeBreakdown",
    "TaxCategory",
    "TaxRules",
    "TaxEstimate",
    "TaxLine",
    "categorize_trade",
    "estimate_tax",
    "fno_turnover",
    "load_tax_rules",
    "BrokerReconciliation",
    "ReconciliationResult",
]
