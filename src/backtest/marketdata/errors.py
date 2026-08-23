"""Domain exceptions for the live market data layer (Step 10).

Mirrors the style of :mod:`backtest.simulator.errors`: catch
:class:`MarketDataError` for anything from this package, or a specific
subclass to react to one failure mode. :class:`NormalizationError` carries a
machine-readable ``code`` so the Step 11 validator and Step 19 dashboard can
branch on the reason without string-matching messages.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "MarketDataError",
    "NormalizationError",
    "FeedError",
    "FeedConnectionError",
    "TimeSyncError",
]


class MarketDataError(Exception):
    """Base class for every market data error."""


class NormalizationError(MarketDataError):
    """A broker payload could not be normalized into a valid tick.

    Parameters
    ----------
    message:
        Human-readable explanation.
    code:
        Stable machine-readable identifier, e.g. ``"crossed_quote"``.
    details:
        Extra structured context for logging.
    """

    code: str = "normalization_error"

    def __init__(self, message: str, code: str | None = None, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details: dict[str, Any] = details

    def __str__(self) -> str:
        if self.details:
            extra = ", ".join(f"{k}={v}" for k, v in sorted(self.details.items()))
            return f"{self.message} ({extra})"
        return self.message


class FeedError(MarketDataError):
    """A transient feed failure — the poll failed but the feed may recover."""


class FeedConnectionError(FeedError):
    """The feed cannot be (re)connected. Terminal until intervention."""


class TimeSyncError(MarketDataError):
    """Clock synchronisation (NTP) failed."""
