"""Domain exceptions for the forward testing simulator.

The hierarchy lets callers choose their granularity: catch
:class:`SimulatorError` to handle anything from this package, or a specific
subclass to react to one failure mode.

:class:`ValidationError` carries a machine-readable ``code`` so the Step 15
risk manager and the Step 19 dashboard can branch on the reason without
string-matching an error message.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "SimulatorError",
    "ValidationError",
    "InsufficientFundsError",
    "PositionError",
    "PositionNotFoundError",
    "DuplicatePositionError",
    "LimitExceededError",
    "ShortSellingNotAllowedError",
    "PortfolioStateError",
]


class SimulatorError(Exception):
    """Base class for every forward-testing simulator error."""


class ValidationError(SimulatorError):
    """A value or operation failed validation.

    Parameters
    ----------
    message:
        Human-readable explanation.
    code:
        Stable machine-readable identifier, e.g. ``"insufficient_funds"``.
        Use this for branching; the message is for humans and may change.
    details:
        Extra structured context (limits, requested values) for logging.
    """

    code: str = "validation_error"

    def __init__(
        self,
        message: str,
        code: str | None = None,
        **details: Any,
    ) -> None:
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


class InsufficientFundsError(ValidationError):
    """Not enough buying power for the requested trade."""

    code = "insufficient_funds"


class PositionError(SimulatorError):
    """Base class for position-related failures."""


class PositionNotFoundError(PositionError):
    """No open position exists for the requested symbol."""


class DuplicatePositionError(PositionError):
    """An open position already exists for this symbol.

    Mirrors the ``uq_positions_one_open_per_symbol`` partial unique index in
    the database, so the in-memory model rejects what the schema would too.
    """


class LimitExceededError(ValidationError):
    """A configured portfolio limit would be breached."""

    code = "limit_exceeded"


class ShortSellingNotAllowedError(ValidationError):
    """A short position was requested but shorting is disabled."""

    code = "short_selling_not_allowed"


class PortfolioStateError(ValidationError):
    """The portfolio's lifecycle state forbids this operation.

    Raised when trading against a ``paused`` or ``stopped`` portfolio.
    """

    code = "portfolio_not_active"