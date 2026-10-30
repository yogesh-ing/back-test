"""PnL Diary & Institutional Edge Attribution package."""

from backtest.diary.attribution import (
    DEFAULT_INLINE_TRADE_LIMIT,
    build_attribution_tree,
    compute_rollup_metrics,
    format_public_trade_row,
    make_instance_key,
)
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
from backtest.diary.regimes import (
    CANONICAL_EXIT_BUCKETS,
    NSE_SESSION_REGIMES,
    build_regime_matrices,
    canonical_exit_reason,
    classify_ist_session_regime,
    validate_regime_clock,
    validate_regime_group_by,
)
from backtest.diary.service import (
    VALID_DIARY_MODES,
    DiaryService,
    parse_ist_date_range,
    validate_diary_mode,
)

__all__ = [
    "CANONICAL_EXIT_BUCKETS",
    "DEFAULT_INLINE_TRADE_LIMIT",
    "DiaryService",
    "NSE_SESSION_REGIMES",
    "RECONCILE_TOLERANCE_INR",
    "VALID_DIARY_MODES",
    "build_attribution_tree",
    "build_regime_matrices",
    "canonical_exit_reason",
    "canonical_trade_signature",
    "classify_ist_session_regime",
    "compute_rollup_metrics",
    "dedupe_normalized_trades",
    "extract_instance_prefix",
    "format_holding_duration",
    "format_public_trade_row",
    "make_instance_key",
    "normalize_db_trade_row",
    "normalize_memory_trade_dict",
    "parse_ist_date_range",
    "reconcile_trade_amounts",
    "single_commission_decimal",
    "validate_diary_mode",
    "validate_regime_clock",
    "validate_regime_group_by",
]
