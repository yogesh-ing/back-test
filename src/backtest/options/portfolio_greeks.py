"""Portfolio-level Greeks aggregation.

Aggregates Greeks across multiple option positions to give a portfolio-level
view of risk exposure.

Usage::

    from backtest.options.portfolio_greeks import PortfolioGreeksCalculator
    from backtest.options.paper_trading import OptionPosition

    calc = PortfolioGreeksCalculator(risk_free_rate=0.06)
    portfolio_greeks = calc.calculate(positions)
    print(portfolio_greeks)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from backtest.options.greeks import BlackScholes
from backtest.options.paper_trading import OptionPosition

logger = logging.getLogger("backtest.options.portfolio_greeks")


@dataclass
class PortfolioGreeks:
    """Aggregated Greeks for a portfolio of option positions.

    All values are net (long positions add, short positions subtract).
    """

    total_delta: float = 0.0
    total_gamma: float = 0.0
    total_theta: float = 0.0  # per year
    total_vega: float = 0.0   # per 1% vol change
    total_rho: float = 0.0    # per 1% rate change
    total_value: float = 0.0  # sum of option values

    # Per-underlying breakdown
    by_underlying: dict[str, dict[str, float]] = field(default_factory=dict)

    # Per-position detail
    positions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_delta": round(self.total_delta, 4),
            "total_gamma": round(self.total_gamma, 6),
            "total_theta": round(self.total_theta, 2),
            "total_vega": round(self.total_vega, 2),
            "total_rho": round(self.total_rho, 4),
            "total_value": round(self.total_value, 2),
            "by_underlying": self.by_underlying,
        }


class PortfolioGreeksCalculator:
    """Calculate portfolio-level Greeks from a list of option positions.

    Parameters
    ----------
    risk_free_rate:
        Annual risk-free rate for Black-Scholes calculations.
    default_volatility:
        Fallback volatility when position doesn't carry one.
    """

    def __init__(
        self,
        risk_free_rate: float = 0.06,
        default_volatility: float = 0.20,
    ) -> None:
        self.bs = BlackScholes(
            risk_free_rate=risk_free_rate,
            volatility=default_volatility,
        )
        self.default_volatility = default_volatility

    def calculate(
        self,
        positions: list[OptionPosition],
        spot_prices: dict[str, float] | None = None,
        volatilities: dict[str, float] | None = None,
    ) -> PortfolioGreeks:
        """Calculate aggregated portfolio Greeks.

        Parameters
        ----------
        positions:
            List of open ``OptionPosition`` objects.
        spot_prices:
            Current spot prices by underlying (e.g. ``{"NIFTY": 24800}``).
            If ``None``, uses the position's ``current_price`` field.
        volatilities:
            Current volatilities by underlying. If ``None``, uses default.

        Returns
        -------
        ``PortfolioGreeks`` with aggregated values.
        """
        result = PortfolioGreeks()
        spot = spot_prices or {}
        vols = volatilities or {}

        for pos in positions:
            if pos.status.value != "open":
                continue

            # Get spot price for this underlying.  Without an explicit spot
            # the strike is the best ATM approximation — the option's own
            # premium (current_price) is NOT the underlying price.
            s = spot.get(pos.underlying, float(pos.strike))
            if s <= 0 or float(pos.strike) <= 0:
                logger.warning(
                    "[greeks] skipping %s: spot/strike undefined",
                    pos.trading_symbol,
                )
                continue

            # Get volatility
            vol = vols.get(pos.underlying, self.default_volatility)

            # Calculate days to expiry
            from datetime import date as date_type
            if pos.expiry is not None:
                days_to_expiry = max((pos.expiry - date_type.today()).days, 0)
            else:
                days_to_expiry = 30  # default fallback

            expiry_years = days_to_expiry / 365.0

            # Calculate Greeks for this position
            greeks = self.bs.greeks(
                spot=s,
                strike=float(pos.strike),
                expiry_years=expiry_years,
                option_type=pos.option_type,
                volatility=vol,
            )

            # Scale by quantity and direction
            qty = float(pos.total_quantity)
            direction = 1.0 if pos.is_long else -1.0

            pos_delta = greeks.delta * qty * direction
            pos_gamma = greeks.gamma * qty * direction
            pos_theta = greeks.theta * qty * direction
            pos_vega = greeks.vega * qty * direction
            pos_rho = greeks.rho * qty * direction
            pos_value = greeks.price * qty * direction

            # Aggregate
            result.total_delta += pos_delta
            result.total_gamma += pos_gamma
            result.total_theta += pos_theta
            result.total_vega += pos_vega
            result.total_rho += pos_rho
            result.total_value += pos_value

            # Per-underlying
            u = pos.underlying
            if u not in result.by_underlying:
                result.by_underlying[u] = {
                    "delta": 0.0, "gamma": 0.0, "theta": 0.0,
                    "vega": 0.0, "rho": 0.0, "value": 0.0,
                }
            result.by_underlying[u]["delta"] += pos_delta
            result.by_underlying[u]["gamma"] += pos_gamma
            result.by_underlying[u]["theta"] += pos_theta
            result.by_underlying[u]["vega"] += pos_vega
            result.by_underlying[u]["rho"] += pos_rho
            result.by_underlying[u]["value"] += pos_value

            # Per-position detail
            result.positions.append({
                "symbol": pos.trading_symbol,
                "underlying": pos.underlying,
                "side": pos.side,
                "quantity": pos.quantity,
                "lot_size": pos.lot_size,
                "strike": str(pos.strike),
                "option_type": pos.option_type,
                "expiry": str(pos.expiry),
                "delta": round(pos_delta, 4),
                "gamma": round(pos_gamma, 6),
                "theta": round(pos_theta, 2),
                "vega": round(pos_vega, 2),
                "rho": round(pos_rho, 4),
                "value": round(pos_value, 2),
            })

        # Round by-underlying values
        for u in result.by_underlying:
            for k in result.by_underlying[u]:
                result.by_underlying[u][k] = round(result.by_underlying[u][k], 4)

        return result
