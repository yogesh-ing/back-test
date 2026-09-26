"""Normalized inputs and outputs for the portfolio intelligence layer.

Everything below the collector speaks these types only. The collector is the
single place that knows what a runner, an options bridge or the dashboard book
looks like; the analytics (Greeks, concentration, correlation, regime) are
pure functions over :class:`MonitorPosition` / :class:`StrategyBook` lists, so
they are deterministic and testable without a running portfolio.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"

#: Ordering used for escalation and sorting (higher = worse).
SEVERITY_RANK: Dict[str, int] = {
    SEVERITY_INFO: 0,
    SEVERITY_WARNING: 1,
    SEVERITY_CRITICAL: 2,
}

INSTRUMENT_OPTION = "option"
INSTRUMENT_EQUITY = "equity"

SIDE_LONG = "LONG"
SIDE_SHORT = "SHORT"

#: Where the implied vol behind a leg's Greeks came from — surfaced in the UI
#: so an operator can tell a market-implied Greek from a defaulted one.
IV_SOURCE_CONTRACT = "contract"  # the chain contract's own vol metadata
IV_SOURCE_IMPLIED = "implied"  # solved from the leg's current premium
IV_SOURCE_DEFAULT = "default"  # config fallback — lowest confidence


def clean_float(value: Any, digits: Optional[int] = None) -> Optional[float]:
    """JSON-safe float: NaN/inf/None → ``None``; optional rounding."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return round(out, digits) if digits is not None else out


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MonitorPosition:
    """One open exposure: a single option leg or an equity position.

    Quantities are in **units** (lots × lot size for options, shares for
    equity) and always positive; direction lives in ``side``. Prices are per
    unit: the option premium for a leg, the share price for equity.
    """

    position_id: str
    strategy_id: str  # runner instance_id, or "dashboard" for the manual book
    strategy_name: str  # human label shown in the UI
    strategy_kind: str  # registry strategy (e.g. "rsi_reversion") or "manual"
    book: str  # "runner" | "dashboard"
    mode: str  # "paper" | "live"
    instrument_type: str  # INSTRUMENT_OPTION | INSTRUMENT_EQUITY
    symbol: str
    underlying: str
    side: str  # SIDE_LONG | SIDE_SHORT
    units: float
    underlying_price: float
    current_price: float
    entry_price: float = 0.0
    lot_size: int = 1
    option_type: Optional[str] = None  # "CE" | "PE"
    strike: Optional[float] = None
    expiry: Optional[date] = None
    dte_days: Optional[float] = None  # calendar days on the book's own clock
    iv: Optional[float] = None  # annualised decimal (0.15 = 15%)
    iv_source: Optional[str] = None
    structure_id: Optional[str] = None
    structure_type: Optional[str] = None

    @property
    def direction(self) -> float:
        return 1.0 if self.side == SIDE_LONG else -1.0

    @property
    def is_option(self) -> bool:
        return self.instrument_type == INSTRUMENT_OPTION

    @property
    def lots(self) -> float:
        return self.units / self.lot_size if self.lot_size else self.units

    @property
    def notional(self) -> float:
        """Gross underlying notional — units × underlying price.

        For an option this is the contract notional (the exchange/SEBI
        convention for exposure), not the premium: a short straddle's premium
        is small, the underlying it is exposed to is not.
        """
        return abs(self.units) * abs(self.underlying_price)


@dataclass
class StrategyBook:
    """Book-level facts about one strategy (runner) or the manual book."""

    strategy_id: str
    strategy_name: str
    strategy_kind: str
    book: str
    mode: str
    status: str = "RUNNING"
    instrument_type: str = INSTRUMENT_EQUITY
    structure_type: Optional[str] = None  # option expression type, if any
    symbols: List[str] = field(default_factory=list)
    capital: float = 0.0
    equity: float = 0.0
    margin_used: float = 0.0
    #: Session P&L (flow-aware — the runner's own day anchor).
    daily_pnl: float = 0.0
    #: Chronological ``(ts_iso, equity)`` points — feeds P&L correlation.
    equity_curve: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class PortfolioInputs:
    """Everything the analytics need, captured once per evaluation."""

    positions: List[MonitorPosition]
    strategies: List[StrategyBook]
    total_capital: float
    total_equity: float
    mode: str  # "all" | "paper" | "live"
    as_of: str
    daily_pnl: float = 0.0
    daily_loss_limit: Optional[float] = None  # supervisor's absolute limit
    #: underlying → recent OHLC bars (dicts with ts/open/high/low/close) —
    #: the freshest bars any runner has seen, used for regime detection.
    bars: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)

    @property
    def capital_base(self) -> float:
        """Denominator for ratio limits: equity if known, else capital."""
        return self.total_equity if self.total_equity > 0 else self.total_capital


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


@dataclass
class Alert:
    """One monitor finding.

    ``key`` identifies the *condition* (e.g. ``greeks:gamma:portfolio``) so the
    alert book can tell "still true" from "new" across evaluations; the
    message and value may change while the key stays put.
    """

    key: str
    category: str  # greeks | scenario | concentration | correlation | regime
    severity: str
    title: str
    message: str
    recommendation: Optional[str] = None
    metric: Optional[str] = None
    value: Optional[float] = None
    threshold: Optional[float] = None
    subject: Optional[str] = None  # strategy / underlying / pair it concerns
    context: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["value"] = clean_float(self.value, 4)
        out["threshold"] = clean_float(self.threshold, 4)
        return out


def sort_alerts(alerts: List[Alert]) -> List[Alert]:
    """Worst first, then by category/key for a stable order."""
    return sorted(
        alerts,
        key=lambda a: (-SEVERITY_RANK.get(a.severity, 0), a.category, a.key),
    )
