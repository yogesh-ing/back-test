"""Expiry calendar for NIFTY and BANKNIFTY index options.

Indian index options expire on the **last Thursday** of each month.
If the last Thursday is a trading holiday, the expiry shifts to the
preceding trading day.  V1 implements the simple last-Thursday rule
without holiday awareness (holidays require a separate exchange calendar).

Typical usage::

    from backtest.instruments.expiry_calendar import ExpiryCalendar

    cal = ExpiryCalendar()
    next_expiry = cal.next_expiry("NIFTY")
    all_expiries = cal.expiries_for_year(2026, "NIFTY")
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

# Underlyings that follow the last-Thursday expiry rule
_INDEX_UNDERLYINGS: frozenset[str] = frozenset({"NIFTY", "BANKNIFTY"})


def last_thursday(year: int, month: int) -> date:
    """Return the last Thursday of the given month/year.

    NSE index options always expire on the last Thursday.  If the
    calendar month has no Thursday (impossible in practice), raises
    ``ValueError``.
    """
    # Start from the last day of the month and walk backwards
    last_day = calendar.monthrange(year, month)[1]
    d = date(year, month, last_day)
    # weekday(): Monday=0 … Thursday=3 … Sunday=6
    days_since_thursday = (d.weekday() - 3) % 7
    return d - timedelta(days=days_since_thursday)


class ExpiryCalendar:
    """Generate expiry dates for index options.

    Parameters
    ----------
    underlyings:
        Which underlyings to support.  Defaults to ``("NIFTY", "BANKNIFTY")``.
    """

    def __init__(
        self,
        underlyings: tuple[str, ...] = ("NIFTY", "BANKNIFTY"),
    ) -> None:
        self._underlyings = frozenset(underlyings)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def expiries_for_month(
        self, year: int, month: int, underlying: str = "NIFTY"
    ) -> list[date]:
        """Return the expiry date(s) for a given month.

        For V1 this is always a single date (last Thursday).  A future
        weekly-expansion would return multiple dates here.
        """
        self._check_underlying(underlying)
        return [last_thursday(year, month)]

    def expiries_for_year(
        self, year: int, underlying: str = "NIFTY"
    ) -> list[date]:
        """Return all 12 monthly expiry dates for a year, oldest first."""
        self._check_underlying(underlying)
        return [
            d
            for month in range(1, 13)
            for d in self.expiries_for_month(year, month, underlying)
        ]

    def next_expiry(
        self,
        underlying: str = "NIFTY",
        from_date: date | None = None,
    ) -> date | None:
        """Return the nearest future expiry on or after ``from_date``.

        If ``from_date`` is ``None``, defaults to ``date.today()``.
        Returns ``None`` if no expiry is found within 12 months.
        """
        self._check_underlying(underlying)
        ref = from_date or date.today()
        # Check current month forward through current month + 12
        for offset in range(13):
            y = ref.year + (ref.month + offset - 1) // 12
            m = (ref.month + offset - 1) % 12 + 1
            exp = last_thursday(y, m)
            if exp >= ref:
                return exp
        return None

    def expiries_between(
        self,
        start: date,
        end: date,
        underlying: str = "NIFTY",
    ) -> list[date]:
        """Return all expiry dates within ``[start, end]`` inclusive."""
        self._check_underlying(underlying)
        result: list[date] = []
        y, m = start.year, start.month
        while date(y, m, 1) <= end:
            exp = last_thursday(y, m)
            if start <= exp <= end:
                result.append(exp)
            m += 1
            if m > 12:
                m = 1
                y += 1
        return result

    def format_expiry(self, d: date) -> str:
        """Format a date as the mStock expiry string: ``YYMON`` (e.g. ``26SEP``)."""
        return d.strftime("%y%b").upper()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _check_underlying(self, underlying: str) -> None:
        if underlying not in self._underlyings:
            raise ValueError(
                f"Unknown underlying '{underlying}'. "
                f"Supported: {sorted(self._underlyings)}"
            )
