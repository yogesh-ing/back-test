"""Data Quality Validator for forward testing (Step 11).

Validates market data ticks and bars for sanity, spikes, gaps, volume anomalies,
and statistical outliers.

Validation rules
----------------
* Price within reasonable range (not zero, not negative)
* OHLC consistency (high >= open/close/low, low <= open/close)
* Volume non-negative
* Timestamp chronological
* Bid <= Ask
* Last price between bid and ask (usually, with tolerance)
* Spike detection via Z-score and rolling std dev
* Gap detection via timestamp diff
* Volume anomaly via avg volume comparison

Handling bad data
-----------------
* Logs validation failures
* Configurable strictness: strict, normal, lenient
* Options: reject, interpolate, or allow with warning
* Alert on repeated failures
* Statistics on data quality

Example
-------
>>> from backtest.live.data_validator import DataValidator
>>> validator = DataValidator()
>>> validator.validate_bar({"open":100,"high":101,"low":99,"close":100,"volume":1000})
True
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import pandas as pd

logger = logging.getLogger("backtest.live.data_validator")

DEFAULT_VALIDATOR_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "data_quality.yaml"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class ValidationRule:
    """Single validation rule config."""

    enabled: bool = True
    threshold: Optional[float] = None
    action: str = "reject"  # reject, warn, interpolate


@dataclass
class ValidatorConfig:
    """Configuration for DataValidator.

    Loaded from YAML or defaults.
    """

    # Strictness level: strict, normal, lenient
    strictness: str = "normal"

    # Price checks
    min_price: float = 0.01
    max_price: float = 1_000_000.0
    allow_zero_volume: bool = True

    # OHLC checks
    check_ohlc_consistency: bool = True
    check_bid_ask: bool = True
    check_last_between_bid_ask: bool = False  # lenient by default, often last is outside spread
    bid_ask_tolerance_pct: float = 0.01  # 1% tolerance for last outside bid/ask

    # Spike detection
    spike_detection_enabled: bool = True
    spike_threshold_std: float = 3.0  # Z-score threshold
    spike_window: int = 20  # rolling window for std dev
    spike_min_history: int = 10

    # Gap detection
    gap_detection_enabled: bool = True
    max_gap_seconds: float = 300.0  # 5 minutes for intraday
    max_gap_seconds_daily: float = 86400.0 * 3  # 3 days for daily bars

    # Volume anomaly
    volume_anomaly_enabled: bool = True
    volume_threshold_multiplier: float = 5.0  # 5x avg volume
    volume_window: int = 20

    # Timestamp
    check_chronological: bool = True
    allow_future_timestamp: bool = False
    future_tolerance_seconds: float = 60.0

    # Handling
    on_failure: str = "reject"  # reject, warn, interpolate
    max_consecutive_failures_before_alert: int = 10

    def __post_init__(self):
        self.strictness = str(self.strictness).strip().lower()
        if self.strictness not in ("strict", "normal", "lenient"):
            self.strictness = "normal"

        # Adjust thresholds based on strictness
        if self.strictness == "strict":
            self.spike_threshold_std = 2.0
            self.volume_threshold_multiplier = 3.0
            self.check_last_between_bid_ask = True
        elif self.strictness == "lenient":
            self.spike_threshold_std = 4.0
            self.volume_threshold_multiplier = 10.0
            self.check_last_between_bid_ask = False
            self.check_bid_ask = False


def load_validator_config(path: str | Path | None = None) -> ValidatorConfig:
    """Load validator config from YAML, fallback to defaults."""
    config_path = Path(path) if path else DEFAULT_VALIDATOR_CONFIG_PATH

    if not config_path.exists():
        return ValidatorConfig()

    try:
        import yaml

        doc = yaml.safe_load(config_path.read_text()) or {}
        # Flatten: support both top-level and nested
        if "validation" in doc:
            doc = doc["validation"]
        known = set(ValidatorConfig.__dataclass_fields__.keys())
        filtered = {k: v for k, v in doc.items() if k in known}
        return ValidatorConfig(**filtered)
    except Exception as exc:
        logger.warning("Failed to load validator config %s: %s, using defaults", config_path, exc)
        return ValidatorConfig()


# ---------------------------------------------------------------------------
# Validation result
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    valid: bool
    reason: str = ""
    code: str = "ok"
    details: Dict[str, Any] = field(default_factory=dict)
    should_interpolate: bool = False

    def __bool__(self):
        return self.valid

    def to_dict(self):
        return {
            "valid": self.valid,
            "reason": self.reason,
            "code": self.code,
            "details": dict(self.details),
            "should_interpolate": self.should_interpolate,
        }


# ---------------------------------------------------------------------------
# DataValidator
# ---------------------------------------------------------------------------


class DataValidator:
    """Validates market data ticks and bars.

    Parameters
    ----------
    config:
        ValidatorConfig or dict. If None, uses defaults.
    symbols:
        Optional list of symbols to track history for (for spike/gap detection).
        If None, tracks all symbols.

    Example
    -------
    >>> validator = DataValidator()
    >>> result = validator.validate_bar({"open":100,"high":101,"low":99,"close":100,"volume":1000})
    >>> result.valid
    True
    """

    def __init__(
        self,
        config: Optional[ValidatorConfig | Mapping[str, Any]] = None,
        symbols: Optional[List[str]] = None,
    ):
        if config is None:
            self.config = ValidatorConfig()
        elif isinstance(config, dict):
            self.config = ValidatorConfig(**config)
        else:
            self.config = config

        self.symbols = [str(s).upper() for s in (symbols or [])]

        # Per-symbol history for statistical checks
        self._price_history: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=self.config.spike_window * 2)
        )
        self._volume_history: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=self.config.volume_window * 2)
        )
        self._last_timestamp: Dict[str, datetime] = {}
        self._consecutive_failures: Dict[str, int] = defaultdict(int)
        self._total_checks: int = 0
        self._failed_checks: int = 0
        self._failure_by_code: Dict[str, int] = defaultdict(int)

        logger.info(
            "DataValidator initialized: strictness=%s spike_thr=%.1f vol_mult=%.1f",
            self.config.strictness,
            self.config.spike_threshold_std,
            self.config.volume_threshold_multiplier,
        )

    # -- public API --------------------------------------------------------

    def validate_tick(self, tick_data: Mapping[str, Any]) -> ValidationResult:
        """Validate a single tick: {symbol, timestamp, bid, ask, last, volume}."""
        self._total_checks += 1

        if not isinstance(tick_data, Mapping):
            return self._fail(
                "invalid_type", "tick must be a mapping", {"type": str(type(tick_data))}
            )

        symbol = str(tick_data.get("symbol", "")).upper()
        if not symbol:
            return self._fail("missing_symbol", "symbol is required")

        # Price checks
        for key in ("bid", "ask", "last"):
            val = tick_data.get(key)
            if val is not None:
                try:
                    price = float(val)
                    if price <= 0:
                        return self._fail(
                            "invalid_price",
                            f"{key} must be positive, got {price}",
                            {"symbol": symbol, key: price},
                        )
                    if price < self.config.min_price or price > self.config.max_price:
                        return self._fail(
                            "price_out_of_range",
                            f"{key} {price} out of range "
                            f"[{self.config.min_price}, {self.config.max_price}]",
                            {"symbol": symbol},
                        )
                except (ValueError, TypeError):
                    return self._fail(
                        "invalid_price", f"{key} must be numeric, got {val}", {"symbol": symbol}
                    )

        # Bid <= Ask
        if self.config.check_bid_ask:
            bid = tick_data.get("bid")
            ask = tick_data.get("ask")
            if bid is not None and ask is not None:
                try:
                    if float(bid) > float(ask):
                        return self._fail(
                            "bid_gt_ask",
                            f"bid {bid} > ask {ask}",
                            {"symbol": symbol, "bid": bid, "ask": ask},
                        )
                except (ValueError, TypeError):
                    pass

        # Last between bid/ask (with tolerance)
        if self.config.check_last_between_bid_ask:
            bid = tick_data.get("bid")
            ask = tick_data.get("ask")
            last = tick_data.get("last")
            if bid is not None and ask is not None and last is not None:
                try:
                    b = float(bid)
                    a = float(ask)
                    last_px = float(last)
                    tolerance = (a - b) * self.config.bid_ask_tolerance_pct if a > b else 0
                    if not (b - tolerance <= last_px <= a + tolerance):
                        return self._fail(
                            "last_outside_spread",
                            f"last {last_px} outside bid/ask [{b}, {a}] with tolerance {tolerance}",
                            {"symbol": symbol},
                        )
                except (ValueError, TypeError):
                    pass

        # Volume non-negative
        vol = tick_data.get("volume")
        if vol is not None:
            try:
                v = float(vol)
                if v < 0:
                    return self._fail(
                        "negative_volume",
                        f"volume must be non-negative, got {v}",
                        {"symbol": symbol},
                    )
            except (ValueError, TypeError):
                return self._fail(
                    "invalid_volume", f"volume must be numeric, got {vol}", {"symbol": symbol}
                )

        # Timestamp chronological
        ts_result = self._check_timestamp(tick_data, symbol)
        if not ts_result.valid:
            return ts_result

        # Spike detection
        if self.config.spike_detection_enabled:
            last = tick_data.get("last")
            if last is not None:
                spike_result = self.check_for_spikes(
                    last, symbol, threshold=self.config.spike_threshold_std
                )
                if not spike_result.valid:
                    return spike_result

        # Volume anomaly
        if self.config.volume_anomaly_enabled and vol is not None:
            vol_result = self.check_volume_anomaly(
                vol, symbol, threshold=self.config.volume_threshold_multiplier
            )
            if not vol_result.valid:
                # Volume anomaly is usually warning, not rejection, depending on strictness
                if self.config.strictness == "strict":
                    return vol_result
                else:
                    logger.warning("Volume anomaly for %s: %s", symbol, vol_result.reason)

        # Gap detection
        if self.config.gap_detection_enabled:
            gap_result = self._check_gap(tick_data, symbol)
            if not gap_result.valid:
                return gap_result

        # If we reach here, valid – update history
        self._update_history(tick_data, symbol)
        self._consecutive_failures[symbol] = 0

        return ValidationResult(valid=True, reason="ok", code="ok")

    def validate_bar(self, bar_data: Mapping[str, Any]) -> ValidationResult:
        """Validate a single OHLCV bar: {symbol, timestamp, open, high, low, close, volume}."""
        self._total_checks += 1

        if not isinstance(bar_data, Mapping):
            return self._fail("invalid_type", "bar must be a mapping")

        symbol = str(bar_data.get("symbol", "")).upper()

        # Check required fields
        for key in ("open", "high", "low", "close"):
            val = bar_data.get(key)
            if val is None:
                return self._fail("missing_field", f"{key} is required", {"symbol": symbol})
            try:
                price = float(val)
                if price <= 0:
                    return self._fail(
                        "invalid_price",
                        f"{key} must be positive, got {price}",
                        {"symbol": symbol, key: price},
                    )
                if price < self.config.min_price or price > self.config.max_price:
                    return self._fail(
                        "price_out_of_range", f"{key} {price} out of range", {"symbol": symbol}
                    )
            except (ValueError, TypeError):
                return self._fail(
                    "invalid_price", f"{key} must be numeric, got {val}", {"symbol": symbol}
                )

        # OHLC relationship
        ohlc_result = self.validate_ohlc_relationship(
            bar_data.get("open"), bar_data.get("high"), bar_data.get("low"), bar_data.get("close")
        )
        if not ohlc_result.valid:
            # Add symbol to details
            ohlc_result.details["symbol"] = symbol
            return ohlc_result

        # Volume
        vol = bar_data.get("volume")
        if vol is not None:
            try:
                v = float(vol)
                if v < 0:
                    return self._fail(
                        "negative_volume",
                        f"volume must be non-negative, got {v}",
                        {"symbol": symbol},
                    )
                if not self.config.allow_zero_volume and v == 0:
                    return self._fail("zero_volume", "zero volume not allowed", {"symbol": symbol})
            except (ValueError, TypeError):
                return self._fail(
                    "invalid_volume", f"volume must be numeric, got {vol}", {"symbol": symbol}
                )

        # Timestamp
        if symbol:
            ts_result = self._check_timestamp(bar_data, symbol)
            if not ts_result.valid:
                return ts_result

            # Gap
            if self.config.gap_detection_enabled:
                gap_result = self._check_gap(bar_data, symbol)
                if not gap_result.valid:
                    return gap_result

            # Spike on close
            if self.config.spike_detection_enabled:
                close = bar_data.get("close")
                if close is not None:
                    spike_result = self.check_for_spikes(
                        close, symbol, threshold=self.config.spike_threshold_std
                    )
                    if not spike_result.valid:
                        return spike_result

            # Volume anomaly
            if self.config.volume_anomaly_enabled and vol is not None:
                vol_result = self.check_volume_anomaly(
                    vol, symbol, threshold=self.config.volume_threshold_multiplier
                )
                if not vol_result.valid and self.config.strictness == "strict":
                    return vol_result

        if symbol:
            self._update_history(bar_data, symbol)
            self._consecutive_failures[symbol] = 0

        return ValidationResult(valid=True, reason="ok", code="ok")

    def validate(self, market_data: Any) -> bool:
        """Generic validate that accepts tick, bar, or dict of symbol->bar.

        Returns bool for compatibility with engine's placeholder.
        """
        if market_data is None:
            return False

        if isinstance(market_data, dict):
            if "symbol" in market_data and ("close" in market_data or "last" in market_data):
                # Single tick or bar
                if "open" in market_data:
                    result = self.validate_bar(market_data)
                else:
                    result = self.validate_tick(market_data)
                return result.valid
            else:
                # Mapping symbol->bar
                for bar in market_data.values():
                    if isinstance(bar, dict):
                        if "open" in bar:
                            if not self.validate_bar(bar).valid:
                                return False
                        else:
                            if not self.validate_tick(bar).valid:
                                return False
                return True

        return False

    # -- specific checks ---------------------------------------------------

    def validate_ohlc_relationship(
        self, open: Any, high: Any, low: Any, close: Any
    ) -> ValidationResult:
        """Validate OHLC consistency: high >= all, low <= all."""
        if not self.config.check_ohlc_consistency:
            return ValidationResult(valid=True)

        try:
            o = float(open)
            h = float(high)
            lo = float(low)
            c = float(close)

            if h < o or h < c or h < lo:
                return self._fail(
                    "ohlc_high_low",
                    f"high {h} must be >= open {o}, low {lo}, close {c}",
                    {"open": o, "high": h, "low": lo, "close": c},
                )

            if lo > o or lo > c or lo > h:
                return self._fail(
                    "ohlc_low_high",
                    f"low {lo} must be <= open {o}, high {h}, close {c}",
                    {"open": o, "high": h, "low": lo, "close": c},
                )

            return ValidationResult(valid=True)

        except (ValueError, TypeError) as exc:
            return self._fail(
                "invalid_ohlc",
                f"OHLC must be numeric: {exc}",
                {"open": open, "high": high, "low": low, "close": close},
            )

    def check_for_spikes(self, price: Any, symbol: str, threshold: float = 3.0) -> ValidationResult:
        """Check if price is a spike vs rolling history (Z-score)."""
        symbol = str(symbol).upper()

        try:
            p = float(price)
        except (ValueError, TypeError):
            return self._fail(
                "invalid_price", f"price must be numeric, got {price}", {"symbol": symbol}
            )

        history = self._price_history.get(symbol)
        if history is None or len(history) < self.config.spike_min_history:
            # Not enough history, cannot detect spike
            return ValidationResult(valid=True)

        # Calculate mean and std from history
        prices = list(history)
        mean = sum(prices) / len(prices)
        # std dev
        variance = sum((x - mean) ** 2 for x in prices) / len(prices)
        std = math.sqrt(variance) if variance > 0 else 0

        if std == 0:
            # No variance, check if price is same
            if abs(p - mean) > 0.01:  # small tolerance
                # If price differs from flat history, could be spike
                # But with zero std, we cannot compute Z-score, so allow
                return ValidationResult(valid=True)
            return ValidationResult(valid=True)

        z_score = abs(p - mean) / std if std != 0 else 0

        if z_score > threshold:
            return self._fail(
                "price_spike",
                f"price spike detected for {symbol}: {p} vs mean {mean:.2f} "
                f"std {std:.2f} z={z_score:.2f} > {threshold}",
                {
                    "symbol": symbol,
                    "price": p,
                    "mean": mean,
                    "std": std,
                    "z_score": z_score,
                    "threshold": threshold,
                },
            )

        return ValidationResult(valid=True)

    def check_for_gaps(
        self, timestamp: Any, last_timestamp: Any, max_gap_seconds: float = 300.0
    ) -> ValidationResult:
        """Check for gaps between timestamps."""
        try:
            # Parse timestamps
            if isinstance(timestamp, datetime):
                ts = timestamp
            else:
                ts = pd.to_datetime(timestamp, utc=True).to_pydatetime()

            if isinstance(last_timestamp, datetime):
                last_ts = last_timestamp
            else:
                last_ts = pd.to_datetime(last_timestamp, utc=True).to_pydatetime()

            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=timezone.utc)

            gap = (ts - last_ts).total_seconds()

            if gap < 0:
                return self._fail(
                    "timestamp_regression",
                    f"timestamp {ts} before last {last_ts}",
                    {"gap_seconds": gap},
                )

            if gap > max_gap_seconds:
                return self._fail(
                    "time_gap",
                    f"gap {gap:.1f}s > max {max_gap_seconds}s",
                    {"gap_seconds": gap, "max_gap": max_gap_seconds},
                )

            return ValidationResult(valid=True)

        except Exception as exc:
            return self._fail(
                "invalid_timestamp",
                f"timestamp parse error: {exc}",
                {"timestamp": str(timestamp), "last_timestamp": str(last_timestamp)},
            )

    def check_volume_anomaly(
        self, volume: Any, symbol: str, threshold: float = 5.0
    ) -> ValidationResult:
        """Check if volume is anomalous vs avg."""
        symbol = str(symbol).upper()

        try:
            vol = float(volume)
            if vol < 0:
                return self._fail("negative_volume", f"volume negative {vol}", {"symbol": symbol})
        except (ValueError, TypeError):
            return self._fail(
                "invalid_volume", f"volume must be numeric, got {volume}", {"symbol": symbol}
            )

        history = self._volume_history.get(symbol)
        if history is None or len(history) < 5:
            return ValidationResult(valid=True)

        vols = list(history)
        avg_vol = sum(vols) / len(vols) if vols else 0

        if avg_vol == 0:
            return ValidationResult(valid=True)

        if vol > avg_vol * threshold:
            return self._fail(
                "volume_spike",
                f"volume anomaly for {symbol}: {vol} > {threshold}x avg {avg_vol:.1f}",
                {"symbol": symbol, "volume": vol, "avg_volume": avg_vol, "threshold": threshold},
            )

        return ValidationResult(valid=True)

    # -- internal ----------------------------------------------------------

    def _check_timestamp(self, data: Mapping[str, Any], symbol: str) -> ValidationResult:
        """Check timestamp is chronological and not future."""
        if not self.config.check_chronological and not self.config.allow_future_timestamp:
            return ValidationResult(valid=True)

        ts_raw = data.get("timestamp") or data.get("ts") or data.get("time")
        if ts_raw is None:
            # No timestamp, skip check
            return ValidationResult(valid=True)

        try:
            if isinstance(ts_raw, datetime):
                ts = ts_raw
            else:
                ts = pd.to_datetime(ts_raw, utc=True).to_pydatetime()

            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            # Future check
            if not self.config.allow_future_timestamp:
                now = datetime.now(timezone.utc)
                future_gap = (ts - now).total_seconds()
                if future_gap > self.config.future_tolerance_seconds:
                    return self._fail(
                        "future_timestamp",
                        f"timestamp {ts} is {future_gap:.1f}s in future",
                        {"symbol": symbol, "timestamp": str(ts)},
                    )

            # Chronological check
            if self.config.check_chronological:
                last_ts = self._last_timestamp.get(symbol)
                if last_ts is not None:
                    if ts < last_ts:
                        return self._fail(
                            "timestamp_regression",
                            f"timestamp {ts} before last {last_ts} for {symbol}",
                            {
                                "symbol": symbol,
                                "timestamp": str(ts),
                                "last_timestamp": str(last_ts),
                            },
                        )

            return ValidationResult(valid=True)

        except Exception as exc:
            return self._fail(
                "invalid_timestamp",
                f"timestamp parse error: {exc}",
                {"symbol": symbol, "raw": str(ts_raw)},
            )

    def _check_gap(self, data: Mapping[str, Any], symbol: str) -> ValidationResult:
        """Check gap based on last timestamp."""
        ts_raw = data.get("timestamp") or data.get("ts") or data.get("time")
        if ts_raw is None:
            return ValidationResult(valid=True)

        last_ts = self._last_timestamp.get(symbol)
        if last_ts is None:
            return ValidationResult(valid=True)

        try:
            if isinstance(ts_raw, datetime):
                ts = ts_raw
            else:
                ts = pd.to_datetime(ts_raw, utc=True).to_pydatetime()

            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            gap = (ts - last_ts).total_seconds()

            # Determine max gap based on timeframe
            timeframe = str(data.get("timeframe", "")).lower()
            max_gap = (
                self.config.max_gap_seconds_daily
                if timeframe in ("day", "1day", "d")
                else self.config.max_gap_seconds
            )

            if gap > max_gap:
                return self._fail(
                    "time_gap",
                    f"gap {gap:.1f}s > max {max_gap}s for {symbol}",
                    {"symbol": symbol, "gap_seconds": gap, "max_gap": max_gap},
                )

            return ValidationResult(valid=True)

        except Exception:
            return ValidationResult(valid=True)

    def _update_history(self, data: Mapping[str, Any], symbol: str):
        """Update price and volume history."""
        # Price: prefer close, then last, then price
        price = data.get("close") or data.get("last") or data.get("price")
        if price is not None:
            try:
                self._price_history[symbol].append(float(price))
            except (ValueError, TypeError):
                pass

        vol = data.get("volume")
        if vol is not None:
            try:
                self._volume_history[symbol].append(float(vol))
            except (ValueError, TypeError):
                pass

        # Timestamp
        ts_raw = data.get("timestamp") or data.get("ts") or data.get("time")
        if ts_raw is not None:
            try:
                if isinstance(ts_raw, datetime):
                    ts = ts_raw
                else:
                    ts = pd.to_datetime(ts_raw, utc=True).to_pydatetime()
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                self._last_timestamp[symbol] = ts
            except Exception:
                pass

    def _fail(
        self, code: str, reason: str, details: Optional[Dict[str, Any]] = None
    ) -> ValidationResult:
        """Create failure result and update stats."""
        self._failed_checks += 1
        self._failure_by_code[code] += 1

        # Extract symbol for consecutive failure tracking
        symbol = ""
        if details and "symbol" in details:
            symbol = str(details["symbol"]).upper()
            self._consecutive_failures[symbol] += 1

            if (
                self._consecutive_failures[symbol]
                >= self.config.max_consecutive_failures_before_alert
            ):
                logger.warning(
                    "Repeated validation failures for %s: %s consecutive, last code=%s reason=%s",
                    symbol,
                    self._consecutive_failures[symbol],
                    code,
                    reason,
                )

        logger.debug("Validation failed [%s] %s details=%s", code, reason, details)

        should_interp = self.config.on_failure == "interpolate"

        return ValidationResult(
            valid=False,
            reason=reason,
            code=code,
            details=details or {},
            should_interpolate=should_interp,
        )

    # -- stats -------------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        return {
            "total_checks": self._total_checks,
            "failed_checks": self._failed_checks,
            "failure_rate": (
                round(self._failed_checks / self._total_checks, 4) if self._total_checks else 0
            ),
            "failures_by_code": dict(self._failure_by_code),
            "consecutive_failures": dict(self._consecutive_failures),
        }

    def reset(self):
        self._price_history.clear()
        self._volume_history.clear()
        self._last_timestamp.clear()
        self._consecutive_failures.clear()
        self._total_checks = 0
        self._failed_checks = 0
        self._failure_by_code.clear()
        logger.info("Validator reset")

    def __repr__(self):
        return (
            f"<DataValidator strictness={self.config.strictness} "
            f"checks={self._total_checks} failed={self._failed_checks}>"
        )
