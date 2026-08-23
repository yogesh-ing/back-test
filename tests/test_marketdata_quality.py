"""Tests for the data quality validator (Step 11).

The important behaviours under test:

* **structural violations always reject** — no strictness level accepts a
  negative price or an impossible OHLC bar.
* **rejected data never enters the rolling windows** — one accepted spike
  would widen the standard deviation enough to hide the next one.
* **regime reset** — a genuine gap-up keeps looking like a spike; after
  ``alert_threshold`` consecutive rejections the validator alerts and
  starts learning the new level instead of rejecting forever.
* **repair substitutes, never invents** — interpolation uses the previous
  observed price and only for price-level errors; bars are never repaired.
* **handler integration** — rejected ticks reach neither buffers nor the
  aggregator; repaired ticks flow through with the substituted price.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from backtest.marketdata import (
    Action,
    BadDataPolicy,
    Bar,
    DataValidator,
    MarketDataConfig,
    MarketDataHandler,
    MockFeed,
    QualityConfig,
    Severity,
    Strictness,
    Tick,
    load_quality_config,
)

D = Decimal
IST = ZoneInfo("Asia/Kolkata")


def ist(hour: int, minute: int, second: int = 0, day: int = 20) -> datetime:
    return datetime(2026, 8, day, hour, minute, second, tzinfo=IST)


def tick(
    last: str = "100",
    vol: int = 10,
    sym: str = "INFY",
    second: int = 0,
    minute: int = 15,
    hour: int = 9,
    **kw,
) -> Tick:
    return Tick(
        symbol=sym,
        timestamp=ist(hour, minute, second),
        last=D(last),
        volume=vol,
        **kw,
    )


def bar(
    o: str = "100", h: str = "102", l: str = "99", c: str = "101",  # noqa: E741
    vol: int = 1000, sym: str = "INFY", tf: str = "1min", minute: int = 15,
) -> Bar:
    return Bar(
        symbol=sym, timeframe=tf, ts=ist(9, minute),
        open=D(o), high=D(h), low=D(l), close=D(c),
        volume=vol, tick_count=5, complete=True,
    )


def validator(**kw) -> DataValidator:
    kw.setdefault("spike_min_samples", 5)
    kw.setdefault("spike_window", 20)
    kw.setdefault("volume_min_samples", 5)
    return DataValidator(config=QualityConfig(**kw))


def warm_up(v: DataValidator, n: int = 10, sym: str = "INFY", last: str = "100", vol: int = 10):
    """Feed n clean ticks so the rolling windows have samples."""
    for i in range(n):
        base = D(last) + D(i % 3) * D("0.1")  # tiny variance, not flat
        result = v.validate_tick(tick(last=str(base), vol=vol, sym=sym, second=i))
        assert result.ok
    return v


# ===========================================================================
# OHLC relationship
# ===========================================================================


class TestOhlcRelationship:
    def test_consistent_ohlc_passes(self):
        assert validator().validate_ohlc_relationship(100, 102, 99, 101) == []

    def test_high_below_low(self):
        problems = validator().validate_ohlc_relationship(100, 98, 99, 100)
        assert any("high" in p and "low" in p for p in problems)

    def test_high_below_open(self):
        assert validator().validate_ohlc_relationship(105, 102, 99, 101)

    def test_high_below_close(self):
        assert validator().validate_ohlc_relationship(100, 102, 99, 103)

    def test_low_above_open(self):
        assert validator().validate_ohlc_relationship(98, 102, 99, 101)

    def test_low_above_close(self):
        assert validator().validate_ohlc_relationship(100, 102, 99, 98.5)

    def test_non_positive_prices_reported(self):
        problems = validator().validate_ohlc_relationship(0, 0, 0, 0)
        assert any("not positive" in p for p in problems)

    def test_messages_are_detailed(self):
        problems = validator().validate_ohlc_relationship(100, 98, 99, 100)
        assert all(any(ch.isdigit() for ch in p) for p in problems)  # values included

    def test_doji_bar_is_fine(self):
        assert validator().validate_ohlc_relationship(100, 100, 100, 100) == []


# ===========================================================================
# Spike detection
# ===========================================================================


class TestSpikes:
    def test_no_spike_without_enough_samples(self):
        v = validator()
        v.validate_tick(tick(last="100"))
        is_spike, zscore = v.check_for_spikes(500, "INFY")
        assert not is_spike and zscore is None

    def test_normal_price_is_not_a_spike(self):
        v = warm_up(validator())
        is_spike, zscore = v.check_for_spikes("100.1", "INFY")
        assert not is_spike
        assert abs(zscore) < 3

    def test_far_price_is_a_spike(self):
        v = warm_up(validator())
        is_spike, zscore = v.check_for_spikes("150", "INFY")
        assert is_spike
        assert zscore > 3

    def test_downward_spike_detected(self):
        v = warm_up(validator())
        is_spike, zscore = v.check_for_spikes("50", "INFY")
        assert is_spike
        assert zscore < -3

    def test_threshold_override(self):
        v = warm_up(validator())
        _, zscore = v.check_for_spikes("100.5", "INFY")
        assert v.check_for_spikes("100.5", "INFY", threshold=abs(zscore) - 0.1)[0]
        assert not v.check_for_spikes("100.5", "INFY", threshold=abs(zscore) + 0.1)[0]

    def test_flat_window_uses_percent_move(self):
        v = validator()
        for i in range(10):
            v.validate_tick(tick(last="100", second=i))  # zero variance
        assert not v.check_for_spikes("100", "INFY")[0]
        is_spike, zscore = v.check_for_spikes("150", "INFY")  # 50% move
        assert is_spike and zscore is None

    def test_unknown_symbol_never_spikes(self):
        assert validator().check_for_spikes(100, "WIPRO") == (False, None)

    def test_spike_tick_rejected_at_normal_strictness(self):
        v = warm_up(validator())
        result = v.validate_tick(tick(last="150", second=30))
        assert result.rejected
        assert result.issues[0].code == "price_spike"
        assert result.issues[0].severity is Severity.ERROR

    def test_spike_only_warns_when_lenient(self):
        v = warm_up(validator(strictness="lenient", spike_zscore_threshold=3.0))
        result = v.validate_tick(tick(last="150", second=30))
        assert result.action is Action.ACCEPTED
        assert result.issues[0].code == "price_spike"
        assert result.issues[0].severity is Severity.WARNING

    def test_rejected_spike_does_not_enter_the_window(self):
        v = warm_up(validator())
        v.validate_tick(tick(last="150", second=30))  # rejected
        # If 150 had entered the window the next 150 would not be a spike.
        assert v.check_for_spikes("150", "INFY")[0]

    def test_accepted_prices_update_the_window(self):
        v = warm_up(validator())
        for i in range(20):  # window slides to the new level
            v.validate_tick(tick(last=str(100 + i * 0.05), second=30 + i))
        assert not v.check_for_spikes("101", "INFY")[0]


# ===========================================================================
# Gap detection
# ===========================================================================


class TestGaps:
    def test_no_gap_without_history(self):
        assert validator().check_for_gaps(ist(9, 15), None) == (False, 0.0)

    def test_gap_detected(self):
        v = validator(max_gap_seconds=60)
        is_gap, seconds = v.check_for_gaps(ist(9, 20), ist(9, 15))
        assert is_gap and seconds == 300.0

    def test_within_limit_is_not_a_gap(self):
        v = validator(max_gap_seconds=300)
        assert v.check_for_gaps(ist(9, 17), ist(9, 15)) == (False, 120.0)

    def test_limit_override(self):
        v = validator(max_gap_seconds=300)
        assert v.check_for_gaps(ist(9, 17), ist(9, 15), max_gap_seconds=60)[0]

    def test_gap_issue_on_tick_stream(self):
        v = validator(max_gap_seconds=60)
        v.validate_tick(tick(second=0))
        result = v.validate_tick(tick(minute=25, second=0, last="100.1"))
        codes = [i.code for i in result.issues]
        assert "data_gap" in codes
        assert result.action is Action.ACCEPTED  # warning at normal strictness

    def test_gap_rejects_when_strict(self):
        v = validator(max_gap_seconds=60, strictness="strict")
        v.validate_tick(tick(second=0))
        result = v.validate_tick(tick(minute=25, second=0, last="100.1"))
        assert result.rejected


# ===========================================================================
# Volume anomaly
# ===========================================================================


class TestVolumeAnomaly:
    def test_explicit_average(self):
        is_anomaly, ratio = validator().check_volume_anomaly(600, avg_volume=100)
        assert is_anomaly and ratio == 6.0

    def test_below_threshold_is_fine(self):
        assert validator().check_volume_anomaly(400, avg_volume=100) == (False, 4.0)

    def test_threshold_override(self):
        assert validator().check_volume_anomaly(300, avg_volume=100, threshold=2.0)[0]

    def test_rolling_average_from_symbol(self):
        v = warm_up(validator(), vol=100)
        is_anomaly, ratio = v.check_volume_anomaly(1000, symbol="INFY")
        assert is_anomaly and ratio == pytest.approx(10.0)

    def test_needs_avg_or_symbol(self):
        with pytest.raises(ValueError, match="avg_volume or symbol"):
            validator().check_volume_anomaly(100)

    def test_no_anomaly_without_enough_samples(self):
        v = validator()
        v.validate_tick(tick(vol=100))
        assert v.check_volume_anomaly(10_000, symbol="INFY") == (False, None)

    def test_zero_average_never_divides(self):
        assert validator().check_volume_anomaly(100, avg_volume=0) == (False, None)

    def test_anomaly_is_warning_at_normal_strictness(self):
        v = warm_up(validator(), vol=100)
        result = v.validate_tick(tick(vol=5000, second=30, last="100.1"))
        assert result.action is Action.ACCEPTED
        assert any(i.code == "volume_anomaly" and i.severity is Severity.WARNING
                   for i in result.issues)

    def test_anomaly_rejects_when_strict(self):
        v = warm_up(validator(strictness="strict"), vol=100)
        result = v.validate_tick(tick(vol=5000, second=30, last="100.1"))
        assert result.rejected


# ===========================================================================
# Tick validation rules
# ===========================================================================


class TestTickRules:
    def test_clean_tick_accepted_with_no_issues(self):
        result = validator().validate_tick(tick(bid=D("99.9"), ask=D("100.1")))
        assert result.action is Action.ACCEPTED
        assert result.issues == []
        assert result.tick.last == D("100")

    def test_out_of_range_price_rejected(self):
        result = validator(max_price=1000).validate_tick(tick(last="5000"))
        assert result.rejected
        assert result.issues[0].code == "price_out_of_range"

    def test_out_of_range_only_warns_when_lenient(self):
        result = validator(max_price=1000, strictness="lenient").validate_tick(tick(last="5000"))
        assert result.action is Action.ACCEPTED

    def test_below_min_price_rejected(self):
        result = validator(min_price="1").validate_tick(tick(last="0.5"))
        assert result.rejected

    def test_crossed_quote_rejected_at_every_strictness(self):
        for level in ("lenient", "normal", "strict"):
            result = validator(strictness=level).validate_tick(
                tick(bid=D("101"), ask=D("100"))
            )
            assert result.rejected, level
            assert result.issues[0].code == "crossed_quote"

    def test_last_outside_spread_warns_at_normal(self):
        result = validator().validate_tick(tick(last="102", bid=D("99.9"), ask=D("100.1")))
        assert result.action is Action.ACCEPTED
        assert any(i.code == "last_outside_spread" for i in result.issues)

    def test_last_outside_spread_rejects_when_strict(self):
        result = validator(strictness="strict").validate_tick(
            tick(last="102", bid=D("99.9"), ask=D("100.1"))
        )
        assert result.rejected

    def test_out_of_order_tick_warns_at_normal(self):
        v = validator()
        v.validate_tick(tick(second=30))
        result = v.validate_tick(tick(second=10, last="100.1"))
        assert result.action is Action.ACCEPTED
        assert any(i.code == "out_of_order" for i in result.issues)

    def test_out_of_order_rejects_when_strict(self):
        v = validator(strictness="strict")
        v.validate_tick(tick(second=30))
        assert v.validate_tick(tick(second=10, last="100.1")).rejected

    def test_out_of_order_does_not_rewind_last_seen(self):
        v = validator()
        v.validate_tick(tick(second=30))
        v.validate_tick(tick(second=10, last="100.1"))  # accepted with warning
        result = v.validate_tick(tick(second=20, last="100.2"))
        assert any(i.code == "out_of_order" for i in result.issues)  # still older than :30

    def test_equal_timestamps_are_chronological(self):
        v = validator()
        v.validate_tick(tick(second=30))
        result = v.validate_tick(tick(second=30, last="100.1"))
        assert not any(i.code == "out_of_order" for i in result.issues)

    def test_multiple_issues_reported_together(self):
        v = warm_up(validator(max_gap_seconds=60), vol=100)
        result = v.validate_tick(tick(minute=30, vol=5000, last="100.1"))
        codes = {i.code for i in result.issues}
        assert "data_gap" in codes and "volume_anomaly" in codes

    def test_symbols_do_not_share_state(self):
        v = warm_up(validator(), sym="INFY")
        # TCS has no window yet — 3000 is not a spike for it.
        result = v.validate_tick(tick(last="3000", sym="TCS"))
        assert result.action is Action.ACCEPTED

    def test_issue_to_dict_is_json_safe(self):
        v = warm_up(validator())
        result = v.validate_tick(tick(last="150", second=30))
        parsed = json.loads(json.dumps(result.to_dict()))
        assert parsed["action"] == "rejected"
        assert parsed["issues"][0]["code"] == "price_spike"


# ===========================================================================
# Repair (interpolation)
# ===========================================================================


class TestRepair:
    def test_spike_repaired_with_previous_price(self):
        v = warm_up(validator(on_bad_data="repair"))
        result = v.validate_tick(tick(last="500", second=30))
        assert result.repaired
        assert result.tick.last != D("500")  # spike price replaced
        assert result.tick.symbol == "INFY"

    def test_repaired_price_is_the_previous_observation(self):
        v = validator(on_bad_data="repair", spike_min_samples=2, spike_window=5)
        v.validate_tick(tick(last="100", second=0))
        v.validate_tick(tick(last="101", second=1))
        v.validate_tick(tick(last="100", second=2))
        result = v.validate_tick(tick(last="9999", second=3))
        assert result.repaired
        assert result.tick.last == D("100")  # the most recent accepted price

    def test_repaired_tick_enters_the_window(self):
        v = warm_up(validator(on_bad_data="repair"))
        before = v.report()["totals"]["checked"]
        result = v.validate_tick(tick(last="500", second=30))
        assert result.repaired
        assert v.report()["totals"]["repaired"] == 1
        assert v.report()["totals"]["checked"] == before + 1

    def test_original_issues_still_reported_on_repair(self):
        v = warm_up(validator(on_bad_data="repair"))
        result = v.validate_tick(tick(last="500", second=30))
        assert any(i.code == "price_spike" for i in result.issues)

    def test_no_repair_without_history(self):
        v = validator(on_bad_data="repair")
        result = v.validate_tick(tick(last="-5"))
        assert result.rejected  # nothing to interpolate from

    def test_non_price_errors_are_not_repaired(self):
        v = warm_up(validator(on_bad_data="repair"))
        result = v.validate_tick(tick(bid=D("101"), ask=D("100"), second=30))
        assert result.rejected  # crossed quote is not price-level repairable

    def test_reject_policy_never_repairs(self):
        v = warm_up(validator(on_bad_data="reject"))
        assert v.validate_tick(tick(last="500", second=30)).rejected


# ===========================================================================
# Alerts and regime reset
# ===========================================================================


class TestAlerts:
    def test_alert_fires_after_threshold_consecutive_rejects(self):
        alerts = []
        v = warm_up(validator(alert_threshold=3))
        v.on_alert(lambda sym, count, issues: alerts.append((sym, count)))
        for i in range(3):
            v.validate_tick(tick(last="500", second=30 + i))
        assert alerts == [("INFY", 3)]

    def test_accepted_tick_resets_the_streak(self):
        alerts = []
        v = warm_up(validator(alert_threshold=3))
        v.on_alert(lambda *a: alerts.append(a))
        v.validate_tick(tick(last="500", second=30))
        v.validate_tick(tick(last="500", second=31))
        v.validate_tick(tick(last="100.1", second=32))  # clean — resets
        v.validate_tick(tick(last="500", second=33))
        v.validate_tick(tick(last="500", second=34))
        assert alerts == []

    def test_regime_reset_accepts_the_new_level(self):
        v = warm_up(validator(alert_threshold=3))
        for i in range(3):
            assert v.validate_tick(tick(last="500", second=30 + i)).rejected
        # Window was reset — the "spike" level is now just data.
        assert v.validate_tick(tick(last="500", second=40)).action is Action.ACCEPTED

    def test_alert_callback_exception_isolated(self):
        seen = []

        def broken(*a):
            raise RuntimeError("pager down")

        v = warm_up(validator(alert_threshold=2))
        v.on_alert(broken)
        v.on_alert(lambda sym, count, issues: seen.append(sym))
        v.validate_tick(tick(last="500", second=30))
        v.validate_tick(tick(last="500", second=31))
        assert seen == ["INFY"]

    def test_alert_callback_removable(self):
        alerts = []
        v = warm_up(validator(alert_threshold=2))
        cb = v.on_alert(lambda *a: alerts.append(a))
        v.remove_alert_callback(cb)
        v.validate_tick(tick(last="500", second=30))
        v.validate_tick(tick(last="500", second=31))
        assert alerts == []

    def test_alerts_are_per_symbol(self):
        alerts = []
        v = warm_up(warm_up(validator(alert_threshold=3)), sym="TCS", last="3000")
        v.on_alert(lambda sym, count, issues: alerts.append(sym))
        v.validate_tick(tick(last="500", second=30))  # INFY reject 1
        v.validate_tick(tick(last="9000", sym="TCS", second=30))  # TCS reject 1
        v.validate_tick(tick(last="500", second=31))  # INFY reject 2
        assert alerts == []  # neither symbol reached 3

    def test_non_callable_alert_rejected(self):
        with pytest.raises(ValueError, match="callable"):
            validator().on_alert("page me")  # type: ignore[arg-type]


# ===========================================================================
# Bar validation
# ===========================================================================


class TestValidateBar:
    def test_clean_bar_accepted(self):
        result = validator().validate_bar(bar())
        assert result.action is Action.ACCEPTED
        assert result.bar is not None

    def test_impossible_ohlc_rejected_at_every_strictness(self):
        for level in ("lenient", "normal", "strict"):
            result = validator(strictness=level).validate_bar(bar(h="98"))
            assert result.rejected, level
            assert result.issues[0].code == "ohlc_inconsistent"

    def test_negative_volume_rejected(self):
        assert validator().validate_bar(bar(vol=-1)).rejected

    def test_bars_must_advance_in_time(self):
        v = validator()
        v.validate_bar(bar(minute=16))
        result = v.validate_bar(bar(minute=15))
        assert any(i.code == "out_of_order" for i in result.issues)

    def test_duplicate_bar_ts_flagged(self):
        v = validator()
        v.validate_bar(bar(minute=15))
        result = v.validate_bar(bar(minute=15))
        assert any(i.code == "out_of_order" for i in result.issues)

    def test_missing_bars_flagged_as_gap(self):
        v = validator()
        v.validate_bar(bar(minute=15))
        result = v.validate_bar(bar(minute=20))  # 5 minutes on a 1min stream
        assert any(i.code == "data_gap" for i in result.issues)
        assert result.action is Action.ACCEPTED  # warning at normal

    def test_adjacent_bars_are_not_a_gap(self):
        v = validator()
        v.validate_bar(bar(minute=15))
        result = v.validate_bar(bar(minute=16))
        assert result.issues == []

    def test_bar_chronology_is_per_timeframe(self):
        v = validator()
        v.validate_bar(bar(minute=16, tf="1min"))
        result = v.validate_bar(bar(minute=15, tf="5min"))
        assert result.issues == []  # different timeframe, no conflict

    def test_bars_are_never_repaired(self):
        v = validator(on_bad_data="repair")
        v.validate_bar(bar())
        assert v.validate_bar(bar(h="98", minute=16)).rejected

    def test_rejected_bar_does_not_advance_chronology(self):
        v = validator()
        v.validate_bar(bar(minute=15))
        v.validate_bar(bar(h="98", minute=16))  # rejected
        result = v.validate_bar(bar(minute=16))  # same ts as the reject
        assert not any(i.code == "out_of_order" for i in result.issues)


# ===========================================================================
# Cross-source comparison
# ===========================================================================


class TestCompareSources:
    def test_agreeing_sources_pass(self):
        a = tick(last="100")
        b = tick(last="100.1")
        assert validator().compare_sources(a, b) is None

    def test_diverging_sources_flagged(self):
        a = tick(last="100")
        b = tick(last="103")
        issue = validator().compare_sources(a, b)
        assert issue is not None
        assert issue.code == "source_divergence"
        assert "%" in issue.message

    def test_tolerance_is_configurable(self):
        a, b = tick(last="100"), tick(last="101")
        assert validator(source_divergence_pct=2.0).compare_sources(a, b) is None
        assert validator(source_divergence_pct=0.5).compare_sources(a, b) is not None


# ===========================================================================
# Statistics
# ===========================================================================


class TestStatistics:
    def test_pristine_validator_scores_one(self):
        assert validator().quality_score == 1.0

    def test_report_counts_outcomes(self):
        v = warm_up(validator())  # 10 accepted
        v.validate_tick(tick(last="500", second=30))  # rejected
        report = v.report()
        assert report["totals"]["checked"] == 11
        assert report["totals"]["accepted"] == 10
        assert report["totals"]["rejected"] == 1
        assert report["by_code"]["price_spike"] == 1

    def test_quality_score_reflects_rejections(self):
        v = warm_up(validator())
        v.validate_tick(tick(last="500", second=30))
        assert v.quality_score == pytest.approx(10 / 11)

    def test_repaired_counts_as_usable(self):
        v = warm_up(validator(on_bad_data="repair"))
        v.validate_tick(tick(last="500", second=30))
        assert v.quality_score == 1.0

    def test_per_symbol_breakdown(self):
        v = warm_up(validator())
        v.validate_tick(tick(last="3000", sym="TCS"))
        report = v.report()
        assert report["by_symbol"]["INFY"]["checked"] == 10
        assert report["by_symbol"]["TCS"]["quality_score"] == 1.0

    def test_warnings_counted(self):
        v = validator(max_gap_seconds=60)
        v.validate_tick(tick(second=0))
        v.validate_tick(tick(minute=25, last="100.1"))
        assert v.report()["totals"]["warnings"] >= 1

    def test_reset_statistics_keeps_windows(self):
        v = warm_up(validator())
        v.reset_statistics()
        assert v.report()["totals"]["checked"] == 0
        # Window survives: a spike is still recognised as one.
        assert v.check_for_spikes("500", "INFY")[0]

    def test_report_is_json_safe(self):
        v = warm_up(validator())
        v.validate_tick(tick(last="500", second=30))
        json.dumps(v.report())


# ===========================================================================
# Config
# ===========================================================================


class TestQualityConfig:
    def test_defaults(self):
        config = QualityConfig()
        assert config.strictness is Strictness.NORMAL
        assert config.on_bad_data is BadDataPolicy.REJECT
        assert config.spike_zscore_threshold == 3.0

    def test_strings_coerce_to_enums(self):
        config = QualityConfig(strictness="strict", on_bad_data="repair")
        assert config.strictness is Strictness.STRICT
        assert config.on_bad_data is BadDataPolicy.REPAIR

    def test_load_default_file(self):
        config = load_quality_config()
        assert config.strictness is Strictness.NORMAL
        assert config.alert_threshold == 5

    def test_lenient_profile(self):
        config = load_quality_config(profile="lenient")
        assert config.strictness is Strictness.LENIENT
        assert config.spike_zscore_threshold == 5.0

    def test_strict_profile(self):
        config = load_quality_config(profile="strict")
        assert config.strictness is Strictness.STRICT
        assert config.max_gap_seconds == 120

    def test_repair_profile(self):
        assert load_quality_config(profile="repair").on_bad_data is BadDataPolicy.REPAIR

    def test_unknown_profile_rejected(self):
        with pytest.raises(ValueError, match="unknown quality profile"):
            load_quality_config(profile="nonexistent")

    def test_unknown_keys_rejected(self, tmp_path):
        doc = tmp_path / "q.yaml"
        doc.write_text("default:\n  not_a_rule: 1\n")
        with pytest.raises(ValueError, match="unknown quality config keys"):
            load_quality_config(path=doc)

    @pytest.mark.parametrize(
        "kw",
        [dict(strictness="paranoid"), dict(on_bad_data="ignore"),
         dict(min_price=0), dict(max_price="0.005"),
         dict(spike_zscore_threshold=0), dict(spike_window=1),
         dict(spike_min_samples=1), dict(spike_min_samples=100),
         dict(max_gap_seconds=0), dict(volume_anomaly_multiple=1.0),
         dict(volume_min_samples=0), dict(alert_threshold=0),
         dict(source_divergence_pct=0)],
    )
    def test_invalid_config_rejected(self, kw):
        with pytest.raises((ValueError, KeyError)):
            QualityConfig(**kw)


# ===========================================================================
# Handler integration
# ===========================================================================


def payload(ts: str, last: float | str = 100, vol: int = 10, sym: str = "INFY") -> dict:
    return {"symbol": sym, "timestamp": ts, "last": last, "volume": vol}


def integrated_handler(v: DataValidator) -> MarketDataHandler:
    config = MarketDataConfig(
        timeframes=("1min",),
        reconnect_backoff_seconds=0.0,
        reconnect_backoff_cap_seconds=0.0,
    )
    feed = MockFeed(naive_tz="Asia/Kolkata")
    h = MarketDataHandler(config=config, feed=feed, validator=v)
    h.subscribe_symbols("INFY")
    return h


class TestHandlerIntegration:
    def _warm(self, h: MarketDataHandler, n: int = 10):
        for i in range(n):
            h.ingest([payload(f"2026-08-20 09:15:{i:02d}", last=100 + (i % 3) * 0.1)])

    def test_rejected_tick_never_reaches_buffers(self):
        h = integrated_handler(validator())
        self._warm(h)
        h.ingest([payload("2026-08-20 09:15:30", last=500)])
        assert h.stats["quality_rejected"] == 1
        assert h.get_current_quote("INFY").last != D("500")
        assert all(t.last != D("500") for t in h.get_recent_ticks("INFY"))

    def test_rejected_tick_never_reaches_the_aggregator(self):
        h = integrated_handler(validator())
        self._warm(h)
        h.ingest([payload("2026-08-20 09:15:30", last=500)])
        assert h.get_current_bar("INFY", "1min").high < D("500")

    def test_repaired_tick_flows_through_with_substitute(self):
        h = integrated_handler(validator(on_bad_data="repair"))
        self._warm(h)
        h.ingest([payload("2026-08-20 09:15:30", last=500)])
        assert h.stats["quality_repaired"] == 1
        quote = h.get_current_quote("INFY")
        assert quote.last != D("500")  # interpolated, not the spike

    def test_clean_data_unaffected(self):
        h = integrated_handler(validator())
        self._warm(h)
        assert h.stats["quality_rejected"] == 0
        assert h.stats["ticks_received"] == 10

    def test_handler_without_validator_accepts_spikes(self):
        config = MarketDataConfig(timeframes=("1min",))
        h = MarketDataHandler(config=config, feed=MockFeed(naive_tz="Asia/Kolkata"))
        h.subscribe_symbols("INFY")
        self._warm(h)
        h.ingest([payload("2026-08-20 09:15:30", last=500)])
        assert h.get_current_quote("INFY").last == D("500")

    def test_attach_validator_after_construction(self):
        config = MarketDataConfig(timeframes=("1min",))
        h = MarketDataHandler(config=config, feed=MockFeed(naive_tz="Asia/Kolkata"))
        h.subscribe_symbols("INFY")
        h.attach_validator(validator())
        self._warm(h)
        h.ingest([payload("2026-08-20 09:15:30", last=500)])
        assert h.stats["quality_rejected"] == 1

    def test_attach_validator_type_checked(self):
        h = MarketDataHandler(config=MarketDataConfig(timeframes=("1min",)))
        with pytest.raises(TypeError, match="DataValidator"):
            h.attach_validator("not a validator")  # type: ignore[arg-type]

    def test_validator_report_visible_alongside_handler_stats(self):
        v = validator()
        h = integrated_handler(v)
        self._warm(h)
        h.ingest([payload("2026-08-20 09:15:30", last=500)])
        assert v.report()["totals"]["rejected"] == 1
        assert h.stats["quality_rejected"] == 1

    def test_alerts_fire_through_the_integrated_pipeline(self):
        alerts = []
        v = validator(alert_threshold=2)
        v.on_alert(lambda sym, count, issues: alerts.append((sym, count)))
        h = integrated_handler(v)
        self._warm(h)
        h.ingest([payload("2026-08-20 09:15:30", last=500)])
        h.ingest([payload("2026-08-20 09:15:31", last=500)])
        assert alerts == [("INFY", 2)]
