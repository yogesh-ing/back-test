"""Model-driven option pricing feed — synthetic Black-Scholes, not market data.

This is the ``model_bs`` side of the pricing-feed abstraction (plan V3 §3).
The backtest driver depends only on :class:`OptionPricingFeed`; this module
provides the model implementation that prices structures off *real index
candles* with Black-Scholes, realized volatility and a configurable
spread/skew. A future ``ChainHistoryFeed`` (recorded broker chains) will
implement the same protocol, so a run can switch from model to real premiums
without touching the driver.

Every quote carries ``source_tag="model_bs"`` and the feed declares
``data_source_real = False`` — a result that cannot state its feed is a bug.

Honesty rules baked in here:

* IV = realized vol + ``iv_premium`` (default 2.5 vol pts, uncalibrated until
  Upstox IV data lands) + put skew. Results should be re-run at 0/2/4 pts to
  show sensitivity; nothing here pretends the premium is known.
* Spread = max(``spread_pct`` × premium, per-strike-band floor). A flat
  percentage alone quotes deep-OTM strikes at untradeably tight spreads.
* Expiry settlement is the VWAP of the last 30 one-minute bars of the expiry
  session (all available bars if fewer than 30, the close if fewer than 5).
  Index bars carry no volume, so without a volume series this degenerates to
  an equal-weighted average — stated, not hidden.
* Prices are float BS math quantized to the NSE tick (₹0.05) at the boundary;
  downstream brokers and the fee stack are Decimal-exact.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping

import numpy as np
import pandas as pd

from backtest.options.greeks import BlackScholes
from backtest.options.pricing_feed import LegQuote, OptionPricingFeed
from backtest.options.quote_providers import MIN_QUOTE_TICK, bs_price
from backtest.timekeeping import EXCHANGE_TZ, from_market_clock

__all__ = [
    "SOURCE_TAG_MODEL_BS",
    "OptionPricingFeed",
    "LegQuote",
    "ModelPricingConfig",
    "ModelBSFeed",
    "compute_realized_vol",
    "settlement_price",
    "IV_WINDOWS",
    "SETTLEMENT_WINDOW_BARS",
    "SETTLEMENT_MIN_BARS",
]

SOURCE_TAG_MODEL_BS = "model_bs"

_TICK = Decimal(str(MIN_QUOTE_TICK))
_ZERO = Decimal("0")

#: NSE index options settle at the 15:30 IST close on expiry day.
EXPIRY_SETTLE_TIME = time(15, 30)

#: Expiry settlement window: last 30 one-minute bars; fewer than 30 → use all
#: available; fewer than 5 → fall back to the final close.
SETTLEMENT_WINDOW_BARS = 30
SETTLEMENT_MIN_BARS = 5

#: ``iv_source`` vocabulary → rolling window in bars. ``constant`` has no window.
IV_WINDOWS: dict[str, int | None] = {
    "realized_10": 10,
    "realized_20": 20,
    "realized_50": 50,
    "constant": None,
}

_SECONDS_PER_YEAR = 365.0 * 86400.0


def _quantize(value: float) -> Decimal:
    # quantize(Decimal("0.05")) only matches the exponent (2dp) — it does NOT
    # snap to tick multiples. Divide, round to whole ticks, multiply back.
    ticks = (Decimal(value) / _TICK).to_integral_value(rounding=ROUND_HALF_UP)
    return ticks * _TICK


def _aware_exchange(bar_time: datetime) -> datetime:
    if bar_time.tzinfo is None:
        # Naive bar stamps are exchange time — the declared door, not a guess.
        return from_market_clock(bar_time)
    return bar_time.astimezone(EXCHANGE_TZ)


@dataclass
class ModelPricingConfig:
    """Knobs for the model feed. Defaults are the plan-V3 pinned values.

    ``iv_premium`` is in absolute vol units (0.025 = 2.5 vol pts) added on
    top of realized vol — the cushion for the fact that implied trades above
    realized. Uncalibrated until Upstox IV data is available; every result
    should show the 0/2/4-pt sensitivity.

    ``spread_floor_bands`` maps strike-distance-from-ATM (in strike steps) to
    a minimum full spread in rupees; band 0 (ATM) uses ``spread_floor``, and
    distances beyond the largest configured band use that band's floor.
    """

    iv_source: str = "realized_20"
    iv_constant: float = 0.15
    iv_premium: float = 0.025
    skew_pts: float = 0.03
    spread_pct: float = 0.005
    spread_floor: float = 0.50
    spread_floor_bands: Mapping[int, float] = field(
        default_factory=lambda: {1: 0.50, 2: 1.00, 3: 2.00}
    )
    risk_free: float = 0.065

    def __post_init__(self) -> None:
        if self.iv_source not in IV_WINDOWS:
            raise ValueError(
                f"iv_source must be one of {sorted(IV_WINDOWS)}, got {self.iv_source!r}"
            )
        if self.iv_constant <= 0:
            raise ValueError("iv_constant must be > 0")
        if self.iv_premium < 0 or self.skew_pts < 0:
            raise ValueError("iv_premium and skew_pts must be >= 0")
        if self.spread_pct < 0 or self.spread_floor < 0:
            raise ValueError("spread_pct and spread_floor must be >= 0")
        if any(v < 0 for v in self.spread_floor_bands.values()):
            raise ValueError("spread_floor_bands values must be >= 0")

    @property
    def iv_window(self) -> int | None:
        return IV_WINDOWS[self.iv_source]


def compute_realized_vol(
    closes: pd.Series,
    window: int = 20,
    periods_per_year: float = 252.0,
) -> pd.Series:
    """Annualized rolling realized vol from log returns.

    ``window`` is in bars and ``periods_per_year`` must match the bar
    timeframe (daily → 252, 1-min NSE → 94,500); annualizing 1-min returns
    with √252 understates vol by ~19x. The first ``window`` bars are NaN —
    the feed falls back to ``iv_constant`` there.
    """
    if window < 2:
        raise ValueError("window must be >= 2")
    closes = closes.astype(float)
    log_ret = np.log(closes / closes.shift(1))
    return log_ret.rolling(window, min_periods=window).std() * math.sqrt(periods_per_year)


def settlement_price(
    session_closes: pd.Series,
    volumes: pd.Series | None = None,
) -> float:
    """Expiry-session settlement: VWAP of the last 30 one-minute bars.

    Fewer than 30 bars → all available bars; fewer than 5 → the final close.
    With no (or zero-sum) volume — index bars carry none — this is an
    equal-weighted average of the same window.
    """
    closes = pd.Series(session_closes).astype(float)
    if len(closes) == 0:
        raise ValueError("empty expiry session: no settlement price")
    if len(closes) < SETTLEMENT_MIN_BARS:
        return float(closes.iloc[-1])
    window = closes.iloc[-SETTLEMENT_WINDOW_BARS:]
    if volumes is not None:
        vol = pd.Series(volumes).reindex(window.index).fillna(0.0).astype(float)
        total = float(vol.sum())
        if total > 0:
            return float((window * vol).sum() / total)
    return float(window.mean())


class ModelBSFeed:
    """Black-Scholes feed over real index candles. ``data_source_real`` is
    always False — the premiums are model output, never market observations.

    ``iv_bars`` is the annualized realized-vol series (see
    :func:`compute_realized_vol`) indexed by bar time; the quote at a bar uses
    the last value at or before it. Missing/NaN IV falls back to
    ``iv_constant``. ``strike_step`` is the underlying's strike interval
    (NIFTY 50, BANKNIFTY 100) and drives the per-band spread floor.
    """

    source_tag = SOURCE_TAG_MODEL_BS
    data_source_real = False

    def __init__(
        self,
        config: ModelPricingConfig | None = None,
        iv_bars: pd.Series | None = None,
        strike_step: int = 50,
    ) -> None:
        self.config = config or ModelPricingConfig()
        self.iv_bars = iv_bars
        self.strike_step = int(strike_step)
        if self.strike_step <= 0:
            raise ValueError("strike_step must be > 0")
        self._reference: datetime | None = None
        self._bs = BlackScholes(
            risk_free_rate=self.config.risk_free,
            volatility=self.config.iv_constant,
        )

    def set_reference(self, bar_time: datetime) -> None:
        self._reference = _aware_exchange(bar_time)

    def _base_iv(self, bar_time: datetime) -> float:
        cfg = self.config
        if cfg.iv_window is None or self.iv_bars is None or len(self.iv_bars) == 0:
            return cfg.iv_constant
        value = self.iv_bars.asof(bar_time)
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return cfg.iv_constant
        return float(value)

    def _spread_floor(self, spot: float, strike: float) -> float:
        cfg = self.config
        band = int(round(abs(spot - strike) / self.strike_step))
        if band == 0:
            return cfg.spread_floor
        if band in cfg.spread_floor_bands:
            return cfg.spread_floor_bands[band]
        if cfg.spread_floor_bands:
            return cfg.spread_floor_bands[max(cfg.spread_floor_bands)]
        return cfg.spread_floor

    def quote(
        self,
        *,
        underlying: str,
        strike: float,
        expiry: date,
        option_type: str,
        spot: float,
        bar_time: datetime | None = None,
    ) -> LegQuote:
        if option_type not in ("CE", "PE"):
            raise ValueError(f"option_type must be CE or PE, got {option_type!r}")
        moment = bar_time if bar_time is not None else self._reference
        if moment is None:
            raise ValueError("no bar_time given and no reference pinned")
        moment = _aware_exchange(moment)
        cfg = self.config

        iv = self._base_iv(moment) + cfg.iv_premium
        if option_type == "PE":
            iv += cfg.skew_pts

        expiry_dt = datetime.combine(expiry, EXPIRY_SETTLE_TIME, tzinfo=EXCHANGE_TZ)
        years = max((expiry_dt - moment).total_seconds() / _SECONDS_PER_YEAR, 0.0)

        if years <= 0 or iv <= 0:
            intrinsic = (
                max(0.0, spot - strike) if option_type == "CE" else max(0.0, strike - spot)
            )
            price = intrinsic
            delta = (
                (1.0 if spot > strike else 0.0)
                if option_type == "CE"
                else (-1.0 if spot < strike else 0.0)
            )
            gamma = theta = vega = 0.0
        else:
            price = bs_price(spot, strike, years, iv, option_type, cfg.risk_free)
            greeks = self._bs.greeks(spot, strike, years, option_type, volatility=iv)
            delta, gamma, theta, vega = (
                greeks.delta,
                greeks.gamma,
                greeks.theta,
                greeks.vega,
            )

        mid = _quantize(price)
        if mid <= _ZERO:
            bid = ask = _ZERO
        else:
            half = max(cfg.spread_pct * float(mid), self._spread_floor(spot, strike)) / 2.0
            bid = max(_ZERO, _quantize(float(mid) - half))
            ask = _quantize(float(mid) + half)

        return LegQuote(
            mid=mid,
            bid=bid,
            ask=ask,
            iv=iv,
            delta=delta,
            gamma=gamma,
            theta=theta,
            vega=vega,
            source_tag=self.source_tag,
            bar_time=moment,
        )

    def expiry_settlement(
        self,
        session_closes: pd.Series,
        volumes: pd.Series | None = None,
    ) -> Decimal:
        return _quantize(settlement_price(session_closes, volumes))