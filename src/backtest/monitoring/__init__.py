"""Real-time portfolio intelligence — the risk layer above individual strategies.

Individual strategies have their own limits; this package watches what those
limits cannot see: the **combined** book.

* :mod:`.greeks`         — portfolio Greeks in ₹, full-revaluation scenarios,
                            Greek-limit alerts with concrete hedges (PRD §1.1)
* :mod:`.concentration`  — exposure by underlying / correlated group, strategy
                            stacking, strike clustering (PRD §1.2)
* :mod:`.correlation`    — strategy P&L correlation, effective diversification
                            (PRD §1.2)
* :mod:`.regime`         — volatility regime + strategy regime-fit (PRD §1.3)
* :mod:`.alerts`         — alert lifecycle (raise / escalate / resolve / ack)
* :mod:`.collector`      — the only module that reads the command center
* :mod:`.service`        — :class:`PortfolioMonitor`, the orchestrator

Docs: ``docs/PORTFOLIO-INTELLIGENCE.md``.
"""

from backtest.monitoring.alerts import AlertBook
from backtest.monitoring.concentration import ConcentrationMonitor
from backtest.monitoring.config import MonitorConfig, load_monitor_config
from backtest.monitoring.correlation import CorrelationMonitor
from backtest.monitoring.greeks import PortfolioGreeksAggregator
from backtest.monitoring.models import Alert, MonitorPosition, PortfolioInputs, StrategyBook
from backtest.monitoring.regime import MarketRegimeDetector
from backtest.monitoring.service import PortfolioMonitor

__all__ = [
    "Alert",
    "AlertBook",
    "ConcentrationMonitor",
    "CorrelationMonitor",
    "MarketRegimeDetector",
    "MonitorConfig",
    "MonitorPosition",
    "PortfolioGreeksAggregator",
    "PortfolioInputs",
    "PortfolioMonitor",
    "StrategyBook",
    "load_monitor_config",
]
