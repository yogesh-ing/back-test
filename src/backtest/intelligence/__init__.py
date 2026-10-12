"""Portfolio Intelligence — read-only aggregate risk analytics + alert rules.

The platform calculates, informs and broadcasts; strategies decide. Nothing
in this package opens, closes or resizes a position.
"""

from backtest.intelligence.concentration import ConcentrationMonitor
from backtest.intelligence.config import IntelligenceConfig, load_config
from backtest.intelligence.correlation import CorrelationCalculator
from backtest.intelligence.greeks import ExposureLeg, PortfolioGreeksAggregator
from backtest.intelligence.market_activity import MarketActivityMonitor
from backtest.intelligence.regime import MarketRegimeDetector, regime_fit
from backtest.intelligence.service import PortfolioIntelligence

__all__ = [
    "ConcentrationMonitor",
    "CorrelationCalculator",
    "ExposureLeg",
    "IntelligenceConfig",
    "MarketActivityMonitor",
    "MarketRegimeDetector",
    "PortfolioGreeksAggregator",
    "PortfolioIntelligence",
    "load_config",
    "regime_fit",
]
