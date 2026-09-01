"""Portfolio risk supervisor (PRD Phase 3 / Task 3.2).

A background guard that inspects aggregate portfolio performance on every
feed tick (≈ every second) and trips circuit breakers that override
individual strategy decisions:

* **Global daily loss limit** — summed daily PnL across ALL runners breaches
  the configured absolute loss → halt (pause or flatten).
* **Global max drawdown** — aggregate peak-to-trough equity drop exceeds the
  configured fraction → halt.
* **Correlation / concentration warning (V1 telemetry)** — 3+ runners hold
  LONG positions in the same correlation group (e.g. major crypto) → flag a
  High Concentration warning.

The supervisor is deliberately cheap (one pass over runner states, O(n)) so
the < 500 ms halt requirement (Task 7.2) holds for 50+ runners.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backtest.data.universe import CORRELATION_GROUPS, correlation_group_for

logger = logging.getLogger("backtest.forward.risk")

HALT_PAUSE = "PAUSE_AND_HOLD"  # Mode A: stop new entries, let SL/TP ride
HALT_FLATTEN = "EMERGENCY_FLATTEN"  # Mode B: cancel/exit everything

STATE_NORMAL = "NORMAL"
STATE_HALTED = "CIRCUIT_BREAKER_HALT"


@dataclass
class GlobalRiskConfig:
    """Portfolio-level circuit-breaker limits.

    Defaults are deliberately permissive so the demo feed (a volatile
    synthetic random-walk) does not halt on its own; production/tight limits
    are injected via :class:`PortfolioManager(risk_config=...)` or env config.
    """

    daily_loss_limit: float = 250_000.0  # absolute account currency
    max_drawdown_pct: float = 0.25  # fraction of peak equity
    max_leverage: float = 1.0  # V1 telemetry
    breach_mode: str = HALT_PAUSE  # daily-loss response mode
    correlation_warning_threshold: int = 3

    def __post_init__(self) -> None:
        if self.daily_loss_limit <= 0:
            raise ValueError("daily_loss_limit must be positive")
        if not 0 < self.max_drawdown_pct < 1:
            raise ValueError("max_drawdown_pct must be between 0 and 1")
        if self.breach_mode not in (HALT_PAUSE, HALT_FLATTEN):
            raise ValueError(f"breach_mode must be {HALT_PAUSE} or {HALT_FLATTEN}")


@dataclass
class RiskReport:
    state: str = STATE_NORMAL
    halted: bool = False
    halt_reason: Optional[str] = None
    halt_mode: Optional[str] = None
    breaches: List[str] = field(default_factory=list)
    warnings: List[Dict[str, Any]] = field(default_factory=list)
    daily_pnl: float = 0.0
    equity: float = 0.0
    peak_equity: float = 0.0
    drawdown_pct: float = 0.0
    checked_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "halt_mode": self.halt_mode,
            "breaches": list(self.breaches),
            "warnings": list(self.warnings),
            "daily_pnl": round(self.daily_pnl, 2),
            "equity": round(self.equity, 2),
            "peak_equity": round(self.peak_equity, 2),
            "drawdown_pct": round(self.drawdown_pct, 4),
            "checked_at": self.checked_at,
        }


class RiskSupervisor:
    """Stateless evaluator — peak equity / day anchors live on the manager."""

    def __init__(self, config: Optional[GlobalRiskConfig] = None) -> None:
        self.config = config or GlobalRiskConfig()

    # -- rule checks ------------------------------------------------------

    def check_portfolio_daily_loss(self, daily_pnl: float) -> Optional[str]:
        if daily_pnl <= -abs(self.config.daily_loss_limit):
            return (
                f"Global daily loss {daily_pnl:,.2f} breached limit "
                f"-{abs(self.config.daily_loss_limit):,.2f}"
            )
        return None

    def check_portfolio_max_drawdown(self, equity: float, peak_equity: float) -> Optional[str]:
        if peak_equity <= 0:
            return None
        dd = (peak_equity - equity) / peak_equity
        if dd >= self.config.max_drawdown_pct:
            return (
                f"Portfolio drawdown {dd:.2%} breached max "
                f"{self.config.max_drawdown_pct:.2%} (equity {equity:,.2f} "
                f"vs peak {peak_equity:,.2f})"
            )
        return None

    def check_correlation_concentration(self, runners: List[Any]) -> List[Dict[str, Any]]:
        """Flag correlation groups with >= threshold concurrent LONG positions."""
        group_longs: Dict[str, set] = {}
        for runner in runners:
            _ = runner.get_state()  # unused; call kept unchanged (F841, ticket #11)
            for pos in runner.positions.values():
                if pos["side"] != "LONG":
                    continue
                group = correlation_group_for(pos["symbol"])
                if group is None:
                    continue
                group_longs.setdefault(group, set()).add(pos["symbol"])

        warnings: List[Dict[str, Any]] = []
        for gid, symbols in group_longs.items():
            threshold = CORRELATION_GROUPS.get(gid, {}).get(
                "threshold", self.config.correlation_warning_threshold
            )
            if len(symbols) >= threshold:
                meta = CORRELATION_GROUPS.get(gid, {})
                warnings.append(
                    {
                        "kind": "HIGH_CONCENTRATION",
                        "group": gid,
                        "label": meta.get("label", gid),
                        "symbols": sorted(symbols),
                        "count": len(symbols),
                        "threshold": threshold,
                        "message": (
                            f"High concentration: {len(symbols)} LONG positions in "
                            f"correlated {meta.get('label', gid)} instruments "
                            f"({', '.join(sorted(symbols))})"
                        ),
                    }
                )
        return warnings

    # -- one-shot evaluation ----------------------------------------------

    def evaluate(
        self,
        runners: List[Any],
        total_equity: float,
        peak_equity: float,
        daily_pnl: float,
        already_halted: bool = False,
    ) -> RiskReport:
        report = RiskReport(
            daily_pnl=daily_pnl,
            equity=total_equity,
            peak_equity=peak_equity,
            drawdown_pct=((peak_equity - total_equity) / peak_equity) if peak_equity > 0 else 0.0,
            checked_at=datetime.now(timezone.utc).isoformat(),
        )

        dd_breach = self.check_portfolio_max_drawdown(total_equity, peak_equity)
        loss_breach = self.check_portfolio_daily_loss(daily_pnl)

        if dd_breach:
            report.breaches.append(dd_breach)
        if loss_breach:
            report.breaches.append(loss_breach)

        if report.breaches:
            report.halted = True
            report.state = STATE_HALTED
            # Drawdown is the harder stop → always flatten; daily loss uses
            # the configured response mode.
            if dd_breach:
                report.halt_reason = dd_breach
                report.halt_mode = HALT_FLATTEN
            else:
                report.halt_reason = loss_breach
                report.halt_mode = self.config.breach_mode
            # Log once — the manager latches and keeps calling evaluate, so
            # avoid re-logging the same breach on every subsequent tick.
            if not already_halted:
                logger.critical(
                    "CIRCUIT BREAKER: %s (mode=%s)", report.halt_reason, report.halt_mode
                )
        elif already_halted:
            # Stay latched until an explicit reset.
            report.halted = True
            report.state = STATE_HALTED

        report.warnings = self.check_correlation_concentration(runners)
        return report
