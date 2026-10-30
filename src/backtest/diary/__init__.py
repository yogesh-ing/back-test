"""PnL Diary & Institutional Edge Attribution package.

Slice 0 ships :mod:`backtest.diary.ledger` — the canonical trade-row
normalization, single-commission sum (E2), ``Gross - Commission = Net``
reconciliation check (B2), and cross-restart/cross-store deduplication (B1)
shared by :class:`~backtest.forward.trade_persistence.LiveTradePersister`,
the Portfolio trade-history API, and the ``/diary`` endpoints.
"""

from backtest.diary.ledger import (
    RECONCILE_TOLERANCE_INR,
    canonical_trade_signature,
    dedupe_normalized_trades,
    extract_instance_prefix,
    format_holding_duration,
    normalize_db_trade_row,
    normalize_memory_trade_dict,
    reconcile_trade_amounts,
    single_commission_decimal,
)

__all__ = [
    "RECONCILE_TOLERANCE_INR",
    "canonical_trade_signature",
    "dedupe_normalized_trades",
    "extract_instance_prefix",
    "format_holding_duration",
    "normalize_db_trade_row",
    "normalize_memory_trade_dict",
    "reconcile_trade_amounts",
    "single_commission_decimal",
]
