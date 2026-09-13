"""Expiry selection policies — determines which expiry date to trade.

Different strategies prefer different expiry horizons.  The expiry policy
selects the appropriate expiry date given the current date and available
expiries.

Usage::

    from backtest.options.expiry_policy import NearestExpiryPolicy

    policy = NearestExpiryPolicy()
    expiry = policy.select_expiry(
        available_expiries=[date(2026, 9, 24), date(2026, 10, 29)],
        reference_date=date.today(),
    )

V1 policies: ``NearestExpiry``, ``MonthlyExpiry``, ``FixedDaysExpiry``.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class ExpiryPolicy(Protocol):
    """Protocol for expiry selection strategies."""

    def select_expiry(
        self,
        available_expiries: list[date],
        reference_date: date | None = None,
    ) -> date | None:
        """Select the best expiry from available dates.

        Parameters
        ----------
        available_expiries:
            Sorted list of available expiry dates.
        reference_date:
            The date to anchor selection (defaults to today).

        Returns
        -------
        The selected expiry, or ``None`` if none are suitable.
        """
        ...


class NearestExpiryPolicy:
    """Select the nearest expiry on or after the reference date.

    This is the default policy — it picks the closest available expiry,
    which maximizes theta decay capture and is most liquid.
    """

    def select_expiry(
        self,
        available_expiries: list[date],
        reference_date: date | None = None,
    ) -> date | None:
        ref = reference_date or date.today()
        future = [e for e in sorted(available_expiries) if e >= ref]
        return future[0] if future else None


class WeeklyExpiryPolicy:
    """Select the nearest weekly expiry (next Thursday within 7 days).

    Falls back to nearest monthly if no weekly is available.
    """

    def __init__(self, max_days: int = 7) -> None:
        self.max_days = max_days

    def select_expiry(
        self,
        available_expiries: list[date],
        reference_date: date | None = None,
    ) -> date | None:
        ref = reference_date or date.today()
        cutoff = ref + timedelta(days=self.max_days)
        weekly = [
            e for e in sorted(available_expiries)
            if ref <= e <= cutoff
        ]
        return weekly[0] if weekly else NearestExpiryPolicy().select_expiry(
            available_expiries, reference_date
        )


class FixedDaysExpiryPolicy:
    """Select the expiry closest to N days from now.

    Useful for strategies that have a preferred holding period.

    Parameters
    ----------
    target_days:
        Target number of days until expiry.
    """

    def __init__(self, target_days: int = 30) -> None:
        self.target_days = target_days

    def select_expiry(
        self,
        available_expiries: list[date],
        reference_date: date | None = None,
    ) -> date | None:
        ref = reference_date or date.today()
        target = ref + timedelta(days=self.target_days)
        future = [e for e in sorted(available_expiries) if e >= ref]
        if not future:
            return None
        return min(future, key=lambda e: abs((e - target).days))


class MinimumDaysExpiryPolicy:
    """Select the nearest expiry that is at least N days away.

    Avoids expiries that are too close (high gamma risk, wide spreads).

    Parameters
    ----------
    min_days:
        Minimum number of days until expiry.
    """

    def __init__(self, min_days: int = 7) -> None:
        self.min_days = min_days

    def select_expiry(
        self,
        available_expiries: list[date],
        reference_date: date | None = None,
    ) -> date | None:
        ref = reference_date or date.today()
        min_date = ref + timedelta(days=self.min_days)
        suitable = [e for e in sorted(available_expiries) if e >= min_date]
        return suitable[0] if suitable else None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_expiry_policy(
    policy_type: str = "nearest",
    **kwargs,
) -> ExpiryPolicy:
    """Create an expiry policy by name.

    Parameters
    ----------
    policy_type:
        One of ``"nearest"``, ``"weekly"``, ``"fixed_days"``, ``"minimum_days"``.
    **kwargs:
        Forwarded to the policy constructor.
    """
    policies = {
        "nearest": NearestExpiryPolicy,
        "weekly": WeeklyExpiryPolicy,
        "fixed_days": FixedDaysExpiryPolicy,
        "minimum_days": MinimumDaysExpiryPolicy,
    }
    cls = policies.get(policy_type)
    if cls is None:
        raise ValueError(
            f"Unknown policy_type '{policy_type}'. "
            f"Valid: {sorted(policies.keys())}"
        )
    return cls(**kwargs)
