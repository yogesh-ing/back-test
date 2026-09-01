"""Tests for Step 11: Data Quality Validator."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backtest.live.data_validator import DataValidator, ValidatorConfig


def test_validator_init():
    validator = DataValidator()
    assert validator.config.strictness == "normal"

    strict = DataValidator(config={"strictness": "strict"})
    assert strict.config.strictness == "strict"
    assert strict.config.spike_threshold_std == 2.0

    lenient = DataValidator(config={"strictness": "lenient"})
    assert lenient.config.strictness == "lenient"


def test_validate_bar_valid():
    validator = DataValidator()

    result = validator.validate_bar(
        {"symbol": "INFY", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000}
    )
    assert result.valid is True

    result = validator.validate_bar(
        {"symbol": "INFY", "open": 100, "high": 100, "low": 100, "close": 100, "volume": 0}
    )
    assert result.valid is True


def test_validate_bar_invalid_missing_field():
    validator = DataValidator()

    result = validator.validate_bar(
        {"symbol": "INFY", "open": 100, "high": 101, "low": 99}
    )  # missing close
    assert result.valid is False
    assert result.code == "missing_field"


def test_validate_bar_invalid_price():
    validator = DataValidator()

    result = validator.validate_bar(
        {"symbol": "INFY", "open": 0, "high": 101, "low": 99, "close": 100, "volume": 1000}
    )
    assert result.valid is False
    assert "price" in result.code

    result = validator.validate_bar(
        {"symbol": "INFY", "open": -10, "high": 101, "low": 99, "close": 100, "volume": 1000}
    )
    assert result.valid is False


def test_validate_ohlc_relationship():
    validator = DataValidator()

    # Valid
    result = validator.validate_ohlc_relationship(100, 101, 99, 100)
    assert result.valid is True

    # Invalid: high < low
    result = validator.validate_ohlc_relationship(100, 98, 99, 100)
    assert result.valid is False
    assert result.code == "ohlc_high_low"

    # Invalid: low > high
    result = validator.validate_ohlc_relationship(100, 101, 102, 100)
    assert result.valid is False

    # Invalid: high < open
    result = validator.validate_ohlc_relationship(100, 99, 98, 100)
    assert result.valid is False


def test_validate_tick_valid():
    validator = DataValidator()

    result = validator.validate_tick(
        {"symbol": "INFY", "bid": 99, "ask": 101, "last": 100, "volume": 100}
    )
    assert result.valid is True


def test_validate_tick_bid_gt_ask():
    validator = DataValidator(config={"check_bid_ask": True})

    result = validator.validate_tick({"symbol": "INFY", "bid": 102, "ask": 101, "last": 100})
    assert result.valid is False
    assert result.code == "bid_gt_ask"


def test_validate_tick_last_outside_spread_strict():
    validator = DataValidator(config={"strictness": "strict", "check_last_between_bid_ask": True})

    # Last outside bid/ask with tolerance
    result = validator.validate_tick({"symbol": "INFY", "bid": 99, "ask": 101, "last": 105})
    assert result.valid is False
    assert result.code == "last_outside_spread"


def test_validate_tick_negative_volume():
    validator = DataValidator()

    result = validator.validate_tick(
        {"symbol": "INFY", "bid": 99, "ask": 101, "last": 100, "volume": -10}
    )
    assert result.valid is False
    assert "volume" in result.code


def test_check_for_spikes():
    validator = DataValidator(config={"spike_window": 10, "spike_min_history": 5})

    # Build history
    for price in [100, 101, 100, 101, 100, 101, 100, 101, 100, 101]:
        validator._price_history["INFY"].append(price)

    # Normal price should pass
    result = validator.check_for_spikes(100.5, "INFY", threshold=3.0)
    assert result.valid is True

    # Spike price should fail
    result = validator.check_for_spikes(200, "INFY", threshold=3.0)
    assert result.valid is False
    assert result.code == "price_spike"
    assert "z=" in result.reason


def test_check_for_gaps():
    validator = DataValidator()

    ts1 = datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)
    ts2 = datetime(2024, 1, 2, 10, 1, tzinfo=timezone.utc)
    ts3 = datetime(2024, 1, 2, 10, 10, tzinfo=timezone.utc)

    # Small gap should pass
    result = validator.check_for_gaps(ts2, ts1, max_gap_seconds=300)
    assert result.valid is True

    # Large gap should fail
    result = validator.check_for_gaps(ts3, ts1, max_gap_seconds=300)
    assert result.valid is False
    assert result.code == "time_gap"

    # Regression should fail
    result = validator.check_for_gaps(ts1, ts2, max_gap_seconds=300)
    assert result.valid is False
    assert result.code == "timestamp_regression"


def test_check_volume_anomaly():
    validator = DataValidator(config={"volume_window": 10})

    # Build history
    for vol in [1000, 1100, 1000, 1100, 1000, 1100, 1000, 1100]:
        validator._volume_history["INFY"].append(vol)

    # Normal volume should pass
    result = validator.check_volume_anomaly(1050, "INFY", threshold=5.0)
    assert result.valid is True

    # Anomalous volume should fail
    result = validator.check_volume_anomaly(10000, "INFY", threshold=5.0)
    assert result.valid is False
    assert result.code == "volume_spike"


def test_timestamp_chronological():
    validator = DataValidator(config={"check_chronological": True})

    ts1 = datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)
    ts2 = datetime(2024, 1, 2, 10, 1, tzinfo=timezone.utc)

    # First tick
    result = validator.validate_tick({"symbol": "INFY", "timestamp": ts1, "last": 100})
    assert result.valid is True

    # Second tick after first – should pass
    result = validator.validate_tick({"symbol": "INFY", "timestamp": ts2, "last": 101})
    assert result.valid is True

    # Regression – before last – should fail
    result = validator.validate_tick({"symbol": "INFY", "timestamp": ts1, "last": 100})
    assert result.valid is False
    assert result.code == "timestamp_regression"


def test_future_timestamp():
    validator = DataValidator(
        config={"allow_future_timestamp": False, "future_tolerance_seconds": 60}
    )

    future = datetime.now(timezone.utc) + timedelta(hours=1)
    result = validator.validate_tick({"symbol": "INFY", "timestamp": future, "last": 100})
    assert result.valid is False
    assert result.code == "future_timestamp"


def test_gap_detection_in_bar():
    validator = DataValidator(config={"gap_detection_enabled": True, "max_gap_seconds": 60})

    ts1 = datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)
    ts2 = datetime(2024, 1, 2, 10, 5, tzinfo=timezone.utc)  # 5 min gap > 60 sec

    result = validator.validate_bar(
        {
            "symbol": "INFY",
            "timestamp": ts1,
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100,
            "volume": 1000,
        }
    )
    assert result.valid is True

    result = validator.validate_bar(
        {
            "symbol": "INFY",
            "timestamp": ts2,
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100,
            "volume": 1000,
        }
    )
    assert result.valid is False
    assert result.code == "time_gap"


def test_consecutive_failures_alert():
    validator = DataValidator(config={"max_consecutive_failures_before_alert": 3})

    # 3 failures for same symbol
    for _ in range(3):
        validator.validate_bar(
            {"symbol": "INFY", "open": 0, "high": 101, "low": 99, "close": 100, "volume": 1000}
        )

    assert validator._consecutive_failures["INFY"] == 3
    assert validator.get_stats()["failed_checks"] == 3


def test_generic_validate():
    validator = DataValidator()

    # Single bar
    assert (
        validator.validate(
            {"symbol": "INFY", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000}
        )
        is True
    )

    # Single tick
    assert validator.validate({"symbol": "INFY", "bid": 99, "ask": 101, "last": 100}) is True

    # Mapping
    assert (
        validator.validate(
            {
                "INFY": {
                    "symbol": "INFY",
                    "open": 100,
                    "high": 101,
                    "low": 99,
                    "close": 100,
                    "volume": 1000,
                }
            }
        )
        is True
    )

    # Invalid mapping
    assert (
        validator.validate(
            {
                "INFY": {
                    "symbol": "INFY",
                    "open": 0,
                    "high": 101,
                    "low": 99,
                    "close": 100,
                    "volume": 1000,
                }
            }
        )
        is False
    )

    # None
    assert validator.validate(None) is False


def test_stats_and_reset():
    validator = DataValidator()

    validator.validate_bar(
        {"symbol": "INFY", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000}
    )
    validator.validate_bar(
        {"symbol": "INFY", "open": 0, "high": 101, "low": 99, "close": 100, "volume": 1000}
    )

    stats = validator.get_stats()
    assert stats["total_checks"] == 2
    assert stats["failed_checks"] == 1
    assert stats["failure_rate"] == 0.5

    validator.reset()
    stats = validator.get_stats()
    assert stats["total_checks"] == 0
    assert stats["failed_checks"] == 0
