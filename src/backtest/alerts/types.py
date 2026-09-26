"""Alert vocabulary for the Portfolio Intelligence layer.

``AlertType`` and ``Severity`` are ``str`` enums on purpose: a strategy's
``on_alert(alert_type, data)`` callback receives a plain string, and
``"portfolio_gamma_critical" == AlertType.PORTFOLIO_GAMMA_CRITICAL`` is
``True`` for a ``str`` enum — so strategy code can compare either way.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class AlertType(str, Enum):
    """Every alert the platform can raise (PRD alert taxonomy)."""

    PORTFOLIO_GAMMA_CRITICAL = "portfolio_gamma_critical"
    PORTFOLIO_DELTA_WARNING = "portfolio_delta_warning"
    CONCENTRATION_HIGH = "concentration_high"
    STRIKE_CLUSTERING = "strike_clustering"
    VIX_REGIME_CHANGE = "vix_regime_change"
    OI_ANOMALY = "oi_anomaly"
    CORRELATION_SPIKE = "correlation_spike"
    LIQUIDITY_DRY_UP = "liquidity_dry_up"
    DATA_FEED_STALE = "data_feed_stale"

    def __str__(self) -> str:  # "portfolio_gamma_critical", not "AlertType.X"
        return self.value


class Severity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"

    def __str__(self) -> str:
        return self.value


#: Ordering used for sorting and for "did this alert escalate?".
SEVERITY_RANK: Dict[str, int] = {"info": 0, "warning": 1, "critical": 2}

#: Alerts that describe a *state* (auto-resolve when the condition clears)
#: versus alerts that describe an *event* (regime flipped, OI spiked) — the
#: latter have nothing to "clear", so they expire after a TTL instead.
EVENT_ALERT_TYPES = frozenset(
    {
        AlertType.VIX_REGIME_CHANGE.value,
        AlertType.OI_ANOMALY.value,
        AlertType.LIQUIDITY_DRY_UP.value,
    }
)

#: Who the PRD says each alert is *primarily* for (display only). Every alert
#: is shown to the trader (the widget is the audit surface) and any strategy
#: may subscribe to any type — ``strategies`` marks the types the PRD
#: expects strategies to react to.
AUDIENCE: Dict[str, tuple] = {
    AlertType.PORTFOLIO_GAMMA_CRITICAL.value: ("trader", "strategies"),
    AlertType.PORTFOLIO_DELTA_WARNING.value: ("trader",),
    AlertType.CONCENTRATION_HIGH.value: ("trader",),
    AlertType.STRIKE_CLUSTERING.value: ("trader",),
    AlertType.VIX_REGIME_CHANGE.value: ("trader", "strategies"),
    AlertType.OI_ANOMALY.value: ("strategies",),
    AlertType.CORRELATION_SPIKE.value: ("trader",),
    AlertType.LIQUIDITY_DRY_UP.value: ("strategies",),
    AlertType.DATA_FEED_STALE.value: ("trader",),
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: Optional[datetime]) -> Optional[str]:
    return ts.isoformat() if ts is not None else None


@dataclass
class Alert:
    """One alert instance.

    ``key`` is the dedupe identity (``type:subject``) — while an alert with
    the same key is unresolved, a re-detection *updates* it instead of
    creating a new one, so a condition that stays true for an hour is one
    alert, not 3,600.
    """

    alert_type: str
    severity: str
    message: str
    data: Dict[str, Any] = field(default_factory=dict)
    subject: str = ""
    alert_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    resolved_at: Optional[datetime] = None
    dismissed_at: Optional[datetime] = None
    dismissed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    #: ``[{"instance_id", "runner", "strategy", "ok", "error"?}]``
    notified_strategies: List[Dict[str, Any]] = field(default_factory=list)
    #: How many times the condition has been re-observed while open.
    occurrences: int = 1

    @property
    def key(self) -> str:
        return f"{self.alert_type}:{self.subject}"

    @property
    def resolved(self) -> bool:
        return self.resolved_at is not None

    @property
    def status(self) -> str:
        if self.resolved_at is not None:
            return "resolved"
        if self.reviewed_at is not None:
            return "reviewed"
        if self.dismissed_at is not None:
            return "dismissed"
        return "active"

    @property
    def visible(self) -> bool:
        """Shown in the widget: unresolved and not dismissed/reviewed."""
        return self.status == "active"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "alert_type": str(self.alert_type),
            "severity": str(self.severity),
            "message": self.message,
            "subject": self.subject,
            "key": self.key,
            "data": self.data,
            "status": self.status,
            "resolved": self.resolved,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "resolved_at": _iso(self.resolved_at),
            "dismissed_at": _iso(self.dismissed_at),
            "dismissed_by": self.dismissed_by,
            "reviewed_at": _iso(self.reviewed_at),
            "notified_strategies": list(self.notified_strategies),
            "occurrences": self.occurrences,
            "audience": list(AUDIENCE.get(str(self.alert_type), ("trader",))),
            "is_event": str(self.alert_type) in EVENT_ALERT_TYPES,
        }
