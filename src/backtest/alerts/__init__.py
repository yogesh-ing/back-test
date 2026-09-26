"""Portfolio Intelligence alerts — information only, never actions.

Platform role: calculate + inform + broadcast.
Strategy role: subscribe + decide + execute (through the engine).
Trader role:   monitor + override.

    from backtest.alerts import AlertType

    class MyStrategy(Strategy):
        def __init__(self, **params):
            super().__init__(**params)
            self.subscribe_to_alerts([AlertType.PORTFOLIO_GAMMA_CRITICAL])

        def on_alert(self, alert_type, alert_data):
            ...  # decide: pause entries, request an exit, or ignore

See ``docs/ALERTS-GUIDE.md`` and ``docs/STRATEGY-ALERTS.md``.
"""

from backtest.alerts.broker import AlertBroker, get_alert_broker, reset_alert_broker
from backtest.alerts.catalog import CATALOG, context_for
from backtest.alerts.types import (
    AUDIENCE,
    EVENT_ALERT_TYPES,
    SEVERITY_RANK,
    Alert,
    AlertType,
    Severity,
)

__all__ = [
    "AUDIENCE",
    "Alert",
    "AlertBroker",
    "AlertType",
    "CATALOG",
    "EVENT_ALERT_TYPES",
    "SEVERITY_RANK",
    "Severity",
    "context_for",
    "get_alert_broker",
    "reset_alert_broker",
]
