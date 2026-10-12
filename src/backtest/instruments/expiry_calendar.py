"""Expiry calendar for NIFTY and BANKNIFTY index options.

NSE monthly F&O contracts expire on the **last Tuesday** of the contract
month, effective September 2025 (circular dated 2025-06-25; the first
Tuesday expiry was 2025-09-02). Before that they expired on the **last
Thursday**. If the computed expiry is a trading holiday, it shifts to the
preceding trading day — V1 implements the weekday rule without holiday
awareness (holidays require a separate exchange calendar).

:func:`monthly_expiry` is the single canonical rule and is date-aware: it
returns the last Tuesday for contract months from September 2025 onward and
the last Thursday for earlier months, so historical backtests over older
data stay correct while the forward path uses the current convention.

Typical usage::

    from backtest.instruments.expiry_calendar import ExpiryCalendar

    cal = ExpiryCalendar()
    next_expiry = cal.next_expiry("NIFTY")
    all_expiries = cal.expiries_for_year(2026, "NIFTY")
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

# Underlyings that follow the NSE index-option expiry rule
_INDEX_UNDERLYINGS: frozenset[str] = frozenset({"NIFTY", "BANKNIFTY"})

#: NSE moved monthly expiry from the last Thursday to the last Tuesday of the
#: contract month, effective September 2025. Contract months on/after this date
#: use the Tuesday rule; earlier months keep the historical Thursday rule.
TUESDAY_EXPIRY_EFFECTIVE: date = date(2025, 9, 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Last day in ``year``/``month`` whose ``weekday()`` == ``weekday``.

    ``weekday()``: Monday=0 … Tuesday=1 … Thursday=3 … Sunday=6.
    """
    last_day = calendar.monthrange(year, month)[1]
    d = date(year, month, last_day)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def last_thursday(year: int, month: int) -> date:
    """Return the last Thursday of the given month/year.

    The historical NSE monthly-expiry convention (contracts before September
    2025). Retained as a pure helper; :func:`monthly_expiry` is the rule the
    calendar actually applies.
    """
    return _last_weekday(year, month, 3)


def last_tuesday(year: int, month: int) -> date:
    """Return the last Tuesday of the given month/year.

    The current NSE monthly-expiry convention (contracts from September 2025).
    """
    return _last_weekday(year, month, 1)


def monthly_expiry(year: int, month: int) -> date:
    """The NSE monthly expiry for a contract month (date-aware).

    Last Tuesday from :data:`TUESDAY_EXPIRY_EFFECTIVE` (September 2025) onward;
    last Thursday for earlier months. This is the single source of truth — the
    synthetic chain generator and the option backtest driver both delegate here
    so the convention can never drift between the forward and backtest paths.
    """
    if date(year, month, 1) >= TUESDAY_EXPIRY_EFFECTIVE:
        return last_tuesday(year, month)
    return last_thursday(year, month)


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

        For V1 this is always a single date (the monthly expiry).  A future
        weekly-expansion would return multiple dates here.
        """
        self._check_underlying(underlying)
        return [monthly_expiry(year, month)]

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
            exp = monthly_expiry(y, m)
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
            exp = monthly_expiry(y, m)
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
