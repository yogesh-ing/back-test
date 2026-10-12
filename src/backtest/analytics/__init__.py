"""Cross-broker analytics primitives (PRD-003).

Two modules live here:

* :mod:`backtest.analytics.stats` — dependency-free significance tests
  (Mann-Whitney U, chi-square on a 2x2 table). ``scipy`` is deliberately
  NOT a dependency of this project, so the tests the PRD asks for are
  implemented against ``math`` instead of being stubbed out.
* :mod:`backtest.analytics.cross_broker` — the broker-aware aggregation
  service behind ``/api/analytics/cross-broker/*``.

Both are import-light: nothing here touches Flask, the database or the
portfolio manager at import time.
"""

from backtest.analytics.cross_broker import CrossBrokerAnalyticsService
from backtest.analytics.stats import (
    chi_square_2x2,
    mann_whitney_u,
    normal_sf,
    rate_significance_block,
    significance_block,
)

__all__ = [
    "CrossBrokerAnalyticsService",
    "chi_square_2x2",
    "mann_whitney_u",
    "normal_sf",
    "rate_significance_block",
    "significance_block",
]
