"""PRD path alias: ``from backtest.alerts.alert_broker import AlertBroker``.

The implementation lives in :mod:`backtest.alerts.broker`.
"""

from backtest.alerts.broker import AlertBroker, get_alert_broker, reset_alert_broker
from backtest.alerts.types import Alert, AlertType, Severity

#: PRD-style module-level singleton accessor.
alert_broker = get_alert_broker

__all__ = [
    "Alert",
    "AlertBroker",
    "AlertType",
    "Severity",
    "alert_broker",
    "get_alert_broker",
    "reset_alert_broker",
]
