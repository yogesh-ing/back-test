"""Explicit exchange-time clock for deterministic historical replay."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd


class ReplayClockError(ValueError):
    """Invalid or non-monotonic simulated time."""


class SimulatedClock:
    """A monotonic UTC clock controlled only by replay bars.

    There is intentionally no wall-clock fallback. Calling :meth:`now` before
    the first replay timestamp is an error, making accidental live-time reads
    visible instead of silently contaminating historical results.
    """

    def __init__(self, initial: Any | None = None, naive_timezone: str = "Asia/Kolkata") -> None:
        self.naive_timezone = naive_timezone
        self._now: datetime | None = None
        if initial is not None:
            self.set(initial)

    @property
    def current(self) -> datetime:
        if self._now is None:
            raise ReplayClockError("simulated clock has not been initialized")
        return self._now

    def now(self) -> datetime:
        """Callable clock interface compatible with forward-engine injection."""
        return self.current

    def set(self, value: Any) -> datetime:
        stamp = self._coerce(value)
        if self._now is not None and stamp < self._now:
            raise ReplayClockError(f"clock cannot move backwards: {self._now} → {stamp}")
        self._now = stamp
        return stamp

    def advance_to(self, value: Any) -> datetime:
        """Advance to a closed-bar timestamp; equal-time events are allowed."""
        return self.set(value)

    def _coerce(self, value: Any) -> datetime:
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize(self.naive_timezone)
        stamp = stamp.tz_convert("UTC")
        return stamp.to_pydatetime().astimezone(timezone.utc)


__all__ = ["ReplayClockError", "SimulatedClock"]
