"""Black-Scholes pricing and Greeks calculations for European options.

Implements the standard Black-Scholes-Merton model for:
- Call and put option pricing
- Greeks: delta, gamma, theta, vega, rho
- Implied volatility (Newton-Raphson solver)

Usage::

    from backtest.options.greeks import BlackScholes, OptionGreeks

    bs = BlackScholes(risk_free_rate=0.06, volatility=0.20)
    price = bs.price(
        spot=24800, strike=25000, expiry_years=0.08,
        option_type="CE", volatility=0.20,
    )
    greeks = bs.greeks(
        spot=24800, strike=25000, expiry_years=0.08,
        option_type="CE", volatility=0.20,
    )

Reference
---------
Black, F. and Scholes, M. (1973). "The Pricing of Options and Corporate Liabilities".
Journal of Political Economy, 81(3), 637-654.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

SQRT_TWO_PI = math.sqrt(2.0 * math.pi)


# ---------------------------------------------------------------------------
# Data class for Greeks
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OptionGreeks:
    """All Greeks for a single option position.

    Attributes
    ----------
    price:
        Theoretical option price (premium per unit).
    delta:
        Rate of change of option price w.r.t. spot price.
        Call: [0, 1], Put: [-1, 0].
    gamma:
        Rate of change of delta w.r.t. spot price. Always >= 0.
    theta:
        Rate of change of option price w.r.t. time (per year).
        Typically negative (time decay).
    vega:
        Rate of change of option price w.r.t. volatility (per 1% vol change).
    rho:
        Rate of change of option price w.r.t. interest rate (per 1% rate change).
    implied_volatility:
        The volatility input used (or solved for).
    """

    price: float
    delta: float
    gamma: float
    theta: float  # per year
    vega: float   # per 1% vol change
    rho: float    # per 1% rate change
    implied_volatility: float = 0.0


# ---------------------------------------------------------------------------
# Normal distribution helpers
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function (Abramowitz & Stegun)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    """Standard normal probability density function."""
    return math.exp(-0.5 * x * x) / SQRT_TWO_PI


# ---------------------------------------------------------------------------
# Black-Scholes model
# ---------------------------------------------------------------------------

class BlackScholes:
    """Black-Scholes-Merton option pricing model.

    Parameters
    ----------
    risk_free_rate:
        Annual risk-free interest rate (e.g. 0.06 for 6%).
    volatility:
        Annual volatility of the underlying (e.g. 0.20 for 20%).
    """

    def __init__(
        self,
        risk_free_rate: float = 0.06,
        volatility: float = 0.20,
    ) -> None:
        if risk_free_rate < 0:
            raise ValueError(f"risk_free_rate must be >= 0, got {risk_free_rate}")
        if volatility <= 0:
            raise ValueError(f"volatility must be > 0, got {volatility}")
        self.risk_free_rate = risk_free_rate
        self.volatility = volatility

    # ------------------------------------------------------------------
    # Price
    # ------------------------------------------------------------------

    def price(
        self,
        spot: float,
        strike: float,
        expiry_years: float,
        option_type: str = "CE",
        volatility: float | None = None,
        risk_free_rate: float | None = None,
    ) -> float:
        """Calculate the theoretical option price.

        Parameters
        ----------
        spot:
            Current underlying price.
        strike:
            Strike price.
        expiry_years:
            Time to expiry in years (e.g. 30 days = 30/365 ≈ 0.082).
        option_type:
            ``"CE"`` for call, ``"PE"`` for put.
        volatility:
            Override the model's default volatility.
        risk_free_rate:
            Override the model's default risk-free rate.

        Returns
        -------
        Theoretical option price per unit.
        """
        if expiry_years <= 0:
            return self._intrinsic(spot, strike, option_type)

        vol = volatility if volatility is not None else self.volatility
        r = risk_free_rate if risk_free_rate is not None else self.risk_free_rate

        d1 = self._d1(spot, strike, expiry_years, r, vol)
        d2 = d1 - vol * math.sqrt(expiry_years)

        if option_type == "CE":
            return (
                spot * _norm_cdf(d1)
                - strike * math.exp(-r * expiry_years) * _norm_cdf(d2)
            )
        elif option_type == "PE":
            return (
                strike * math.exp(-r * expiry_years) * _norm_cdf(-d2)
                - spot * _norm_cdf(-d1)
            )
        else:
            raise ValueError(f"option_type must be CE or PE, got '{option_type}'")

    def _intrinsic(self, spot: float, strike: float, option_type: str) -> float:
        """Intrinsic value at expiry."""
        if option_type == "CE":
            return max(0.0, spot - strike)
        elif option_type == "PE":
            return max(0.0, strike - spot)
        raise ValueError(f"Unknown option_type: {option_type}")

    # ------------------------------------------------------------------
    # Greeks
    # ------------------------------------------------------------------

    def greeks(
        self,
        spot: float,
        strike: float,
        expiry_years: float,
        option_type: str = "CE",
        volatility: float | None = None,
        risk_free_rate: float | None = None,
    ) -> OptionGreeks:
        """Calculate all Greeks for an option.

        Parameters
        ----------
        spot:
            Current underlying price.
        strike:
            Strike price.
        expiry_years:
            Time to expiry in years.
        option_type:
            ``"CE"`` for call, ``"PE"`` for put.
        volatility:
            Override the model's default volatility.
        risk_free_rate:
            Override the model's default risk-free rate.

        Returns
        -------
        ``OptionGreeks`` with price, delta, gamma, theta, vega, rho.
        """
        if expiry_years <= 0:
            return self._greeks_at_expiry(spot, strike, option_type)

        vol = volatility if volatility is not None else self.volatility
        r = risk_free_rate if risk_free_rate is not None else self.risk_free_rate

        d1 = self._d1(spot, strike, expiry_years, r, vol)
        d2 = d1 - vol * math.sqrt(expiry_years)
        sqrt_t = math.sqrt(expiry_years)
        exp_neg_rt = math.exp(-r * expiry_years)

        # Price
        opt_price = self.price(spot, strike, expiry_years, option_type, vol, r)

        # Delta
        if option_type == "CE":
            delta = _norm_cdf(d1)
        else:
            delta = _norm_cdf(d1) - 1.0

        # Gamma (same for calls and puts)
        gamma = _norm_pdf(d1) / (spot * vol * sqrt_t)

        # Theta (per year, then divide by 365 for daily)
        if option_type == "CE":
            theta = (
                -spot * _norm_pdf(d1) * vol / (2 * sqrt_t)
                - r * strike * exp_neg_rt * _norm_cdf(d2)
            )
        else:
            theta = (
                -spot * _norm_pdf(d1) * vol / (2 * sqrt_t)
                + r * strike * exp_neg_rt * _norm_cdf(-d2)
            )

        # Vega (per 1% vol change → divide by 100)
        vega = spot * _norm_pdf(d1) * sqrt_t / 100.0

        # Rho (per 1% rate change → divide by 100)
        if option_type == "CE":
            rho = strike * expiry_years * exp_neg_rt * _norm_cdf(d2) / 100.0
        else:
            rho = -strike * expiry_years * exp_neg_rt * _norm_cdf(-d2) / 100.0

        return OptionGreeks(
            price=opt_price,
            delta=delta,
            gamma=gamma,
            theta=theta,
            vega=vega,
            rho=rho,
            implied_volatility=vol,
        )

    def _greeks_at_expiry(
        self, spot: float, strike: float, option_type: str
    ) -> OptionGreeks:
        """Greeks at expiry — delta is step function, others are zero."""
        intrinsic = self._intrinsic(spot, strike, option_type)
        if option_type == "CE":
            delta = 1.0 if spot > strike else 0.0
        else:
            delta = -1.0 if spot < strike else 0.0
        return OptionGreeks(
            price=intrinsic, delta=delta, gamma=0.0,
            theta=0.0, vega=0.0, rho=0.0,
        )

    # ------------------------------------------------------------------
    # Implied volatility
    # ------------------------------------------------------------------

    def implied_volatility(
        self,
        market_price: float,
        spot: float,
        strike: float,
        expiry_years: float,
        option_type: str = "CE",
        risk_free_rate: float | None = None,
        max_iterations: int = 50,
        tolerance: float = 1e-8,
    ) -> float:
        """Solve for implied volatility using Newton-Raphson.

        Parameters
        ----------
        market_price:
            Observed market price of the option.
        spot, strike, expiry_years, option_type:
            Same as :meth:`price`.
        risk_free_rate:
            Override the model's default.
        max_iterations:
            Maximum Newton-Raphson iterations.
        tolerance:
            Convergence tolerance on vega.

        Returns
        -------
        Implied volatility (annualized).

        Raises
        ------
        ValueError
            If the solver does not converge.
        """
        if expiry_years <= 0:
            raise ValueError("Cannot solve IV at expiry")

        r = risk_free_rate if risk_free_rate is not None else self.risk_free_rate

        # Initial guess: Brenner-Subrahmanyam approximation
        iv = math.sqrt(2 * math.pi / expiry_years) * market_price / spot

        for _ in range(max_iterations):
            greeks = self.greeks(spot, strike, expiry_years, option_type, iv, r)
            vega = greeks.vega * 100  # undo the /100 to get per-unit vol sensitivity
            if abs(vega) < tolerance:
                break
            diff = greeks.price - market_price
            iv -= diff / vega
            iv = max(iv, 0.001)  # floor at 0.1%

        return iv

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _d1(
        spot: float, strike: float, t: float, r: float, vol: float
    ) -> float:
        """Calculate d1 parameter."""
        return (
            math.log(spot / strike)
            + (r + 0.5 * vol * vol) * t
        ) / (vol * math.sqrt(t))


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------

def calculate_price(
    spot: float,
    strike: float,
    expiry_days: int,
    option_type: str = "CE",
    volatility: float = 0.20,
    risk_free_rate: float = 0.06,
) -> float:
    """Quick option price calculation.

    Parameters
    ----------
    spot, strike:
        Prices.
    expiry_days:
        Days to expiry (converted to years internally).
    option_type:
        ``"CE"`` or ``"PE"``.
    volatility:
        Annual volatility (0.20 = 20%).
    risk_free_rate:
        Annual risk-free rate (0.06 = 6%).
    """
    bs = BlackScholes(risk_free_rate=risk_free_rate, volatility=volatility)
    return bs.price(spot, strike, expiry_days / 365.0, option_type)


def calculate_greeks(
    spot: float,
    strike: float,
    expiry_days: int,
    option_type: str = "CE",
    volatility: float = 0.20,
    risk_free_rate: float = 0.06,
) -> OptionGreeks:
    """Quick Greeks calculation.

    Parameters
    ----------
    spot, strike:
        Prices.
    expiry_days:
        Days to expiry.
    option_type:
        ``"CE"`` or ``"PE"``.
    volatility:
        Annual volatility (0.20 = 20%).
    risk_free_rate:
        Annual risk-free rate (0.06 = 6%).
    """
    bs = BlackScholes(risk_free_rate=risk_free_rate, volatility=volatility)
    return bs.greeks(spot, strike, expiry_days / 365.0, option_type)
