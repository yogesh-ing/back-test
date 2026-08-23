"""Tests for the market data handler (Step 10).

The important behaviours under test:

* **normalization** — every broker dialect (mStock ``ltp``, generic
  ``last_price``, bar-shaped ``ohlcv``) collapses into one standard tick,
  and bad payloads are rejected loudly, not silently zero-filled.
* **boundary alignment** — bars are floored in the *exchange* timezone
  (IST), anchored at the NSE session open, because UTC-floored hourly bars
  would open at half past the local hour.
* **gaps and late data** — a gap is counted, optionally filled with flagged
  synthetic bars; a late tick may add volume but never rewrites the close.
* **reconnection** — a transient poll failure reconnects with exponential
  backoff and retries; exhaustion raises instead of hanging.
* **idempotent persistence** — replaying the same closed bars writes no
  duplicate ``market_data_cache`` rows.
"""

from __future__ import annotations

import json
from datetime import datetime, time as dtime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from backtest.db.manager import DatabaseManager
from backtest.db.models import Base, MarketDataCache
from backtest.marketdata import (
    Bar,
    BarAggregator,
    DataFeed,
    FeedConnectionError,
    FeedError,
    INTRADAY_MINUTES,
    MarketDataConfig,
    MarketDataError,
    MarketDataHandler,
    MockFeed,
    MStockFeed,
    NormalizationError,
    Tick,
    Timeframe,
    align_to_boundary,
    load_marketdata_config,
    next_boundary,
    normalize_tick,
    parse_timestamp,
)
from backtest.db.models import Timeframe as DbTimeframe

D = Decimal
IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
ANCHOR = dtime(9, 15)


def ist(hour: int, minute: int, second: int = 0, day: int = 20) -> datetime:
    return datetime(2026, 8, day, hour, minute, second, tzinfo=IST)


def tick(
    hour: int,
    minute: int,
    second: int = 0,
    last: str = "100",
    vol: int = 10,
    sym: str = "INFY",
    day: int = 20,
    **kw,
) -> Tick:
    return Tick(
        symbol=sym,
        timestamp=ist(hour, minute, second, day=day),
        last=D(last),
        volume=vol,
        **kw,
    )


def payload(ts: str = "2026-08-20T09:15:05+05:30", **kw) -> dict:
    base = {"symbol": "INFY", "timestamp": ts, "last": 100.5, "volume": 10}
    base.update(kw)
    return base


@pytest.fixture()
def db():
    manager = DatabaseManager.from_env(profile="testing", url="sqlite:///:memory:")
    manager.connect()
    Base.metadata.create_all(manager.engine)
    yield manager
    manager.disconnect()


# ===========================================================================
# Timestamp parsing
# ===========================================================================


class TestParseTimestamp:
    def test_aware_datetime_converted_to_utc(self):
        result = parse_timestamp(ist(9, 15))
        assert result.tzinfo == UTC
        assert result == ist(9, 15)

    def test_naive_datetime_uses_naive_tz(self):
        result = parse_timestamp(datetime(2026, 8, 20, 9, 15), naive_tz="Asia/Kolkata")
        assert result == ist(9, 15)

    def test_naive_default_is_utc(self):
        result = parse_timestamp(datetime(2026, 8, 20, 9, 15))
        assert result == datetime(2026, 8, 20, 9, 15, tzinfo=UTC)

    def test_epoch_seconds(self):
        epoch = ist(9, 15).timestamp()
        assert parse_timestamp(epoch) == ist(9, 15)

    def test_epoch_milliseconds(self):
        epoch_ms = int(ist(9, 15).timestamp() * 1000)
        assert parse_timestamp(epoch_ms) == ist(9, 15)

    def test_iso_string(self):
        assert parse_timestamp("2026-08-20T09:15:00+05:30") == ist(9, 15)

    def test_iso_string_with_z_suffix(self):
        result = parse_timestamp("2026-08-20T03:45:00Z")
        assert result == ist(9, 15)

    def test_naive_iso_string_uses_naive_tz(self):
        assert parse_timestamp("2026-08-20 09:15:00", naive_tz="Asia/Kolkata") == ist(9, 15)

    def test_none_rejected(self):
        with pytest.raises(NormalizationError) as exc:
            parse_timestamp(None)
        assert exc.value.code == "missing_timestamp"

    def test_garbage_string_rejected(self):
        with pytest.raises(NormalizationError) as exc:
            parse_timestamp("not-a-time")
        assert exc.value.code == "bad_timestamp"

    def test_bool_rejected(self):
        with pytest.raises(NormalizationError):
            parse_timestamp(True)

    def test_negative_epoch_rejected(self):
        with pytest.raises(NormalizationError):
            parse_timestamp(-5)


# ===========================================================================
# Tick normalization
# ===========================================================================


class TestNormalizeTick:
    def test_standard_fields(self):
        t = normalize_tick(payload(bid=100.4, ask=100.6, open=99, high=101, low=98.5, close=100.5))
        assert t.symbol == "INFY"
        assert t.last == D("100.5")
        assert t.bid == D("100.4")
        assert t.ask == D("100.6")
        assert t.volume == 10
        assert t.open == D("99")
        assert t.high == D("101")
        assert t.low == D("98.5")
        assert t.close == D("100.5")

    def test_prices_are_decimal(self):
        t = normalize_tick(payload())
        assert isinstance(t.last, Decimal)

    def test_float_avoids_binary_expansion(self):
        t = normalize_tick(payload(last=0.1))
        assert t.last == D("0.1")

    def test_mstock_dialect(self):
        raw = {"tradingsymbol": "infy", "ltt": "2026-08-20T09:15:00+05:30",
               "ltp": "1500.50", "ltq": 25, "bp": 1500.4, "ap": 1500.6}
        t = normalize_tick(raw)
        assert t.symbol == "INFY"
        assert t.last == D("1500.50")
        assert t.volume == 25
        assert t.bid == D("1500.4")
        assert t.ask == D("1500.6")

    def test_short_ohlcv_dialect(self):
        raw = {"symbol": "TCS", "t": "2026-08-20T09:15:00+05:30",
               "o": 100, "h": 102, "l": 99, "c": 101, "v": 5000}
        t = normalize_tick(raw)
        assert t.open == D("100")
        assert t.close == D("101")
        assert t.last == D("101")  # bar-shaped payloads fall back to close
        assert t.volume == 5000

    def test_symbol_argument_overrides_payload(self):
        t = normalize_tick(payload(), symbol="tcs")
        assert t.symbol == "TCS"

    def test_symbol_normalized_upper(self):
        t = normalize_tick(payload(symbol="  infy "))
        assert t.symbol == "INFY"

    def test_missing_symbol_rejected(self):
        raw = payload()
        del raw["symbol"]
        with pytest.raises(NormalizationError) as exc:
            normalize_tick(raw)
        assert exc.value.code == "missing_symbol"

    def test_missing_timestamp_rejected(self):
        raw = payload()
        del raw["timestamp"]
        with pytest.raises(NormalizationError) as exc:
            normalize_tick(raw)
        assert exc.value.code == "missing_timestamp"

    def test_missing_price_rejected(self):
        raw = {"symbol": "INFY", "timestamp": "2026-08-20T09:15:00+05:30", "volume": 5}
        with pytest.raises(NormalizationError) as exc:
            normalize_tick(raw)
        assert exc.value.code == "missing_price"

    def test_zero_price_rejected(self):
        with pytest.raises(NormalizationError) as exc:
            normalize_tick(payload(last=0))
        assert exc.value.code == "non_positive_price"

    def test_negative_price_rejected(self):
        with pytest.raises(NormalizationError):
            normalize_tick(payload(last=-5))

    def test_crossed_quote_rejected(self):
        with pytest.raises(NormalizationError) as exc:
            normalize_tick(payload(bid=101, ask=100))
        assert exc.value.code == "crossed_quote"

    def test_bid_equal_ask_allowed(self):
        t = normalize_tick(payload(bid=100, ask=100))
        assert t.bid == t.ask == D("100")

    def test_negative_volume_rejected(self):
        with pytest.raises(NormalizationError) as exc:
            normalize_tick(payload(volume=-1))
        assert exc.value.code == "negative_volume"

    def test_missing_volume_defaults_to_zero(self):
        raw = payload()
        del raw["volume"]
        assert normalize_tick(raw).volume == 0

    def test_non_mapping_payload_rejected(self):
        with pytest.raises(NormalizationError) as exc:
            normalize_tick([1, 2, 3])  # type: ignore[arg-type]
        assert exc.value.code == "bad_payload"

    def test_naive_timestamp_uses_feed_timezone(self):
        t = normalize_tick(payload(ts="2026-08-20 09:15:00"), naive_tz="Asia/Kolkata")
        assert t.timestamp == ist(9, 15)

    def test_source_recorded(self):
        assert normalize_tick(payload(), source="mstock").source == "mstock"

    def test_tick_is_immutable(self):
        t = normalize_tick(payload())
        with pytest.raises(AttributeError):
            t.last = D("1")  # type: ignore[misc]

    def test_to_dict_is_json_safe(self):
        t = normalize_tick(payload(bid=100.4, ask=100.6))
        parsed = json.loads(json.dumps(t.to_dict()))
        assert parsed["last"] == "100.5"
        assert parsed["symbol"] == "INFY"


# ===========================================================================
# Boundary alignment
# ===========================================================================


class TestAlignment:
    @pytest.mark.parametrize(
        "tf, minute, expected_minute",
        [("1min", 17, 17), ("3min", 17, 15), ("5min", 17, 15),
         ("15min", 44, 30), ("30min", 44, 30)],
    )
    def test_intraday_floor_from_midnight(self, tf, minute, expected_minute):
        result = align_to_boundary(ist(10, minute, 33), tf)
        assert result == ist(10, expected_minute)

    def test_exact_boundary_maps_to_itself(self):
        assert align_to_boundary(ist(10, 15), "5min") == ist(10, 15)

    def test_seconds_are_floored(self):
        assert align_to_boundary(ist(9, 15, 59), "1min") == ist(9, 15)

    def test_hourly_without_anchor_floors_to_the_hour(self):
        assert align_to_boundary(ist(10, 20), "60min") == ist(10, 0)

    def test_hourly_with_nse_anchor(self):
        # Real NSE hourly candles run 09:15-10:15, not 09:00-10:00.
        assert align_to_boundary(ist(10, 14), "60min", anchor=ANCHOR) == ist(9, 15)
        assert align_to_boundary(ist(10, 15), "60min", anchor=ANCHOR) == ist(10, 15)

    def test_pre_open_tick_falls_back_to_midnight_flooring(self):
        assert align_to_boundary(ist(9, 14), "60min", anchor=ANCHOR) == ist(9, 0)

    def test_anchor_matters_for_30min(self):
        assert align_to_boundary(ist(9, 16), "30min", anchor=ANCHOR) == ist(9, 15)
        assert align_to_boundary(ist(9, 16), "30min") == ist(9, 0)

    def test_1hour_is_alias_of_60min(self):
        assert align_to_boundary(ist(10, 20), "1hour") == align_to_boundary(ist(10, 20), "60min")

    def test_day_aligns_to_local_midnight(self):
        result = align_to_boundary(ist(15, 29), "day")
        assert result == datetime(2026, 8, 20, tzinfo=IST)

    def test_day_alignment_happens_in_exchange_tz_not_utc(self):
        # 2026-08-20 20:00 UTC is already 2026-08-21 01:30 in IST.
        late_utc = datetime(2026, 8, 20, 20, 0, tzinfo=UTC)
        assert align_to_boundary(late_utc, "day") == datetime(2026, 8, 21, tzinfo=IST)

    def test_week_aligns_to_monday(self):
        # 2026-08-20 is a Thursday.
        assert align_to_boundary(ist(11, 0), "week") == datetime(2026, 8, 17, tzinfo=IST)

    def test_month_aligns_to_first(self):
        assert align_to_boundary(ist(11, 0), "month") == datetime(2026, 8, 1, tzinfo=IST)

    def test_utc_input_converted_before_flooring(self):
        # 04:00 UTC == 09:30 IST; a UTC-floored 5min bar would also be 09:30,
        # but an hourly one would not — check the interesting case.
        four_utc = datetime(2026, 8, 20, 4, 0, tzinfo=UTC)
        assert align_to_boundary(four_utc, "60min") == ist(9, 0)

    def test_other_timezone_supported(self):
        # 14:30 UTC is 10:30 in New York during DST.
        ts = datetime(2026, 8, 20, 14, 30, tzinfo=UTC)
        result = align_to_boundary(ts, "60min", tz="America/New_York")
        assert result == datetime(2026, 8, 20, 10, 0, tzinfo=ZoneInfo("America/New_York"))

    def test_naive_timestamp_rejected(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            align_to_boundary(datetime(2026, 8, 20, 10, 0), "5min")

    def test_unknown_timeframe_rejected(self):
        with pytest.raises(ValueError, match="unknown timeframe"):
            align_to_boundary(ist(10, 0), "7min")

    def test_timeframe_enum_accepted(self):
        assert align_to_boundary(ist(10, 17), Timeframe.M5) == ist(10, 15)

    def test_timeframe_values_match_db_schema(self):
        assert {tf.value for tf in Timeframe} == {tf.value for tf in DbTimeframe}


class TestNextBoundary:
    def test_intraday(self):
        assert next_boundary(ist(9, 15), "5min") == ist(9, 20)

    def test_hourly_with_anchor(self):
        assert next_boundary(ist(9, 15), "60min", anchor=ANCHOR) == ist(10, 15)

    def test_pre_open_hourly_snaps_to_anchor(self):
        # The bar after the 09:00 pre-open stub is the 09:15 session bar.
        assert next_boundary(ist(9, 0), "60min", anchor=ANCHOR) == ist(9, 15)

    def test_day(self):
        start = datetime(2026, 8, 20, tzinfo=IST)
        assert next_boundary(start, "day") == datetime(2026, 8, 21, tzinfo=IST)

    def test_week(self):
        start = datetime(2026, 8, 17, tzinfo=IST)
        assert next_boundary(start, "week") == datetime(2026, 8, 24, tzinfo=IST)

    def test_month_rollover(self):
        start = datetime(2026, 8, 1, tzinfo=IST)
        assert next_boundary(start, "month") == datetime(2026, 9, 1, tzinfo=IST)

    def test_month_rollover_across_year(self):
        start = datetime(2026, 12, 1, tzinfo=IST)
        assert next_boundary(start, "month") == datetime(2027, 1, 1, tzinfo=IST)


# ===========================================================================
# Bar aggregation
# ===========================================================================


def aggregator(**kw) -> BarAggregator:
    kw.setdefault("timeframes", ["1min"])
    kw.setdefault("anchor", ANCHOR)
    return BarAggregator(**kw)


class TestBarAggregatorBasics:
    def test_first_tick_opens_a_bar(self):
        agg = aggregator()
        closed = agg.add_tick(tick(9, 15, 5))
        assert closed == []
        bar = agg.current_bar("INFY", "1min")
        assert bar is not None
        assert bar.ts == ist(9, 15)
        assert bar.open == bar.high == bar.low == bar.close == D("100")
        assert bar.volume == 10
        assert bar.tick_count == 1
        assert not bar.complete

    def test_ticks_fold_into_the_bar(self):
        agg = aggregator()
        agg.add_tick(tick(9, 15, 5, last="100"))
        agg.add_tick(tick(9, 15, 20, last="103", vol=5))
        agg.add_tick(tick(9, 15, 40, last="99", vol=2))
        bar = agg.current_bar("INFY", "1min")
        assert bar.open == D("100")
        assert bar.high == D("103")
        assert bar.low == D("99")
        assert bar.close == D("99")
        assert bar.volume == 17
        assert bar.tick_count == 3

    def test_boundary_cross_closes_the_bar(self):
        agg = aggregator()
        agg.add_tick(tick(9, 15, 5))
        closed = agg.add_tick(tick(9, 16, 2, last="101"))
        assert len(closed) == 1
        assert closed[0].ts == ist(9, 15)
        assert closed[0].complete
        assert agg.current_bar("INFY", "1min").ts == ist(9, 16)

    def test_closed_bar_rejects_further_updates(self):
        agg = aggregator()
        agg.add_tick(tick(9, 15, 5))
        closed = agg.add_tick(tick(9, 16, 2))
        with pytest.raises(ValueError, match="completed bar"):
            closed[0].update(tick(9, 16, 30))

    def test_multiple_timeframes_build_simultaneously(self):
        agg = aggregator(timeframes=["1min", "5min"])
        agg.add_tick(tick(9, 15, 5))
        closed = agg.add_tick(tick(9, 16, 2))
        assert [b.timeframe for b in closed] == ["1min"]  # 5min still building
        assert agg.current_bar("INFY", "5min").ts == ist(9, 15)
        assert agg.current_bar("INFY", "5min").tick_count == 2

    def test_five_minute_bar_closes_at_its_own_boundary(self):
        agg = aggregator(timeframes=["5min"])
        agg.add_tick(tick(9, 15, 5))
        agg.add_tick(tick(9, 18, 0, last="105"))
        closed = agg.add_tick(tick(9, 20, 1, last="104"))
        assert len(closed) == 1
        assert closed[0].ts == ist(9, 15)
        assert closed[0].high == D("105")

    def test_hourly_bars_follow_the_nse_anchor(self):
        agg = aggregator(timeframes=["60min"])
        agg.add_tick(tick(9, 20))
        closed = agg.add_tick(tick(10, 20))
        assert closed[0].ts == ist(9, 15)
        assert agg.current_bar("INFY", "60min").ts == ist(10, 15)

    def test_day_bars(self):
        agg = aggregator(timeframes=["day"])
        agg.add_tick(tick(9, 15, day=20))
        agg.add_tick(tick(15, 29, day=20, last="110"))
        closed = agg.add_tick(tick(9, 15, day=21))
        assert len(closed) == 1
        assert closed[0].ts == datetime(2026, 8, 20, tzinfo=IST)
        assert closed[0].close == D("110")

    def test_symbols_are_independent(self):
        agg = aggregator()
        agg.add_tick(tick(9, 15, 5, sym="INFY", last="100"))
        agg.add_tick(tick(9, 15, 10, sym="TCS", last="3000"))
        assert agg.current_bar("INFY", "1min").close == D("100")
        assert agg.current_bar("TCS", "1min").close == D("3000")

    def test_bid_ask_carried_onto_the_bar(self):
        agg = aggregator()
        agg.add_tick(tick(9, 15, 5, bid=D("99.5"), ask=D("100.5")))
        bar = agg.current_bar("INFY", "1min")
        assert bar.bid == D("99.5")
        assert bar.ask == D("100.5")

    def test_bar_to_dict_is_json_safe(self):
        agg = aggregator()
        agg.add_tick(tick(9, 15, 5))
        parsed = json.loads(json.dumps(agg.current_bar("INFY", "1min").to_dict()))
        assert parsed["open"] == "100"
        assert parsed["timeframe"] == "1min"
        assert parsed["complete"] is False

    def test_requires_at_least_one_timeframe(self):
        with pytest.raises(ValueError, match="at least one timeframe"):
            BarAggregator(timeframes=[])

    def test_bad_timezone_fails_at_construction(self):
        with pytest.raises(Exception):
            BarAggregator(timeframes=["1min"], tz="Mars/Olympus")

    def test_duplicate_timeframes_deduplicated(self):
        agg = BarAggregator(timeframes=["1min", "1min", Timeframe.M1])
        assert agg.timeframes == ("1min",)


class TestGaps:
    def test_gap_detected_but_not_filled_by_default(self):
        agg = aggregator()
        agg.add_tick(tick(9, 15, 5))
        closed = agg.add_tick(tick(9, 18, 0))
        assert len(closed) == 1  # only the real bar
        assert agg.stats.gaps_detected == 1
        assert agg.stats.synthetic_bars == 0

    def test_gap_filled_with_flat_synthetic_bars(self):
        agg = aggregator(fill_gaps=True)
        agg.add_tick(tick(9, 15, 5, last="100"))
        closed = agg.add_tick(tick(9, 18, 0, last="105"))
        assert [b.ts for b in closed] == [ist(9, 15), ist(9, 16), ist(9, 17)]
        for synth in closed[1:]:
            assert synth.synthetic
            assert synth.complete
            assert synth.open == synth.high == synth.low == synth.close == D("100")
            assert synth.volume == 0
            assert synth.tick_count == 0
        assert agg.stats.synthetic_bars == 2

    def test_adjacent_bars_are_not_a_gap(self):
        agg = aggregator(fill_gaps=True)
        agg.add_tick(tick(9, 15, 5))
        agg.add_tick(tick(9, 16, 5))
        assert agg.stats.gaps_detected == 0
        assert agg.stats.synthetic_bars == 0

    def test_huge_gap_is_not_filled(self):
        agg = aggregator(fill_gaps=True, max_gap_bars=2)
        agg.add_tick(tick(9, 15, 5))
        closed = agg.add_tick(tick(9, 30, 0))
        assert len(closed) == 1  # 14 missing periods > 2 → skip filling
        assert agg.stats.gaps_detected == 1
        assert agg.stats.synthetic_bars == 0

    def test_synthetic_bars_fire_the_bar_closed_callback(self):
        seen = []
        agg = aggregator(fill_gaps=True)
        agg.on_bar_closed(seen.append)
        agg.add_tick(tick(9, 15, 5))
        agg.add_tick(tick(9, 17, 0))
        assert [b.ts for b in seen] == [ist(9, 15), ist(9, 16)]


class TestLateTicks:
    def test_late_tick_within_grace_adds_volume_and_extremes(self):
        agg = aggregator(late_grace_seconds=60)
        agg.add_tick(tick(9, 15, 55))
        agg.add_tick(tick(9, 16, 10, last="101"))  # opens the 09:16 bar
        agg.add_tick(tick(9, 15, 40, last="200", vol=7))  # 20s late
        bar = agg.current_bar("INFY", "1min")
        assert bar.high == D("200")  # extreme extended
        assert bar.close == D("101")  # close NOT rewritten
        assert bar.volume == 17
        assert agg.stats.late_applied == 1

    def test_late_tick_beyond_grace_dropped(self):
        agg = aggregator(late_grace_seconds=10)
        agg.add_tick(tick(9, 16, 10))
        closed = agg.add_tick(tick(9, 15, 40, last="200"))
        assert closed == []
        assert agg.current_bar("INFY", "1min").high == D("100")
        assert agg.stats.late_dropped == 1

    def test_zero_grace_drops_every_late_tick(self):
        agg = aggregator(late_grace_seconds=0)
        agg.add_tick(tick(9, 16, 0))
        agg.add_tick(tick(9, 15, 59))
        assert agg.stats.late_dropped == 1


class TestForceClose:
    def test_closes_in_progress_bars(self):
        agg = aggregator(timeframes=["1min", "5min"])
        agg.add_tick(tick(9, 15, 5))
        closed = agg.force_close()
        assert len(closed) == 2
        assert all(b.complete for b in closed)
        assert agg.current_bar("INFY", "1min") is None

    def test_filter_by_timeframe(self):
        agg = aggregator(timeframes=["1min", "5min"])
        agg.add_tick(tick(9, 15, 5))
        closed = agg.force_close(timeframe="1min")
        assert [b.timeframe for b in closed] == ["1min"]
        assert agg.current_bar("INFY", "5min") is not None

    def test_filter_by_symbol(self):
        agg = aggregator()
        agg.add_tick(tick(9, 15, 5, sym="INFY"))
        agg.add_tick(tick(9, 15, 6, sym="TCS", last="3000"))
        closed = agg.force_close(symbol="tcs")
        assert [b.symbol for b in closed] == ["TCS"]
        assert agg.current_bar("INFY", "1min") is not None

    def test_empty_aggregator_returns_nothing(self):
        assert aggregator().force_close() == []


class TestAggregatorCallbacks:
    def test_callback_fired_once_per_closed_bar(self):
        seen = []
        agg = aggregator()
        agg.on_bar_closed(seen.append)
        agg.add_tick(tick(9, 15, 5))
        agg.add_tick(tick(9, 16, 5))
        agg.add_tick(tick(9, 17, 5))
        assert [b.ts for b in seen] == [ist(9, 15), ist(9, 16)]

    def test_failing_callback_does_not_break_aggregation(self):
        seen = []

        def broken(bar):
            raise RuntimeError("observer bug")

        agg = aggregator()
        agg.on_bar_closed(broken)
        agg.on_bar_closed(seen.append)
        closed = agg.add_tick(tick(9, 16, 0)) or agg.add_tick(tick(9, 17, 0))
        assert len(closed) == 1
        assert len(seen) == 1  # the healthy observer still ran

    def test_callback_removable(self):
        seen = []
        agg = aggregator()
        handle = agg.on_bar_closed(seen.append)
        agg.remove_bar_callback(handle)
        agg.add_tick(tick(9, 15, 5))
        agg.add_tick(tick(9, 16, 5))
        assert seen == []

    def test_non_callable_rejected(self):
        with pytest.raises(ValueError, match="callable"):
            aggregator().on_bar_closed("not a function")  # type: ignore[arg-type]


# ===========================================================================
# Feeds
# ===========================================================================


class TestMockFeed:
    def test_abstract_base_cannot_be_instantiated(self):
        with pytest.raises(TypeError):
            DataFeed()  # type: ignore[abstract]

    def test_connect_disconnect_cycle(self):
        feed = MockFeed()
        assert not feed.is_connected
        feed.connect()
        assert feed.is_connected
        feed.disconnect()
        assert not feed.is_connected

    def test_poll_before_connect_raises(self):
        with pytest.raises(FeedConnectionError):
            MockFeed().poll(["INFY"])

    def test_batches_returned_fifo(self):
        feed = MockFeed()
        feed.connect()
        feed.push(payload(last=1))
        feed.push(payload(last=2))
        assert feed.poll(["INFY"])[0]["last"] == 1
        assert feed.poll(["INFY"])[0]["last"] == 2

    def test_empty_queue_returns_empty_batch(self):
        feed = MockFeed()
        feed.connect()
        assert feed.poll(["INFY"]) == []

    def test_scripted_poll_failures(self):
        feed = MockFeed()
        feed.connect()
        feed.fail_next_polls(1)
        with pytest.raises(FeedError):
            feed.poll(["INFY"])
        assert feed.poll(["INFY"]) == []  # recovered

    def test_scripted_connect_failures(self):
        feed = MockFeed(connect_failures=2)
        with pytest.raises(FeedConnectionError):
            feed.connect()
        with pytest.raises(FeedConnectionError):
            feed.connect()
        feed.connect()
        assert feed.is_connected

    def test_default_backfill_unsupported(self):
        with pytest.raises(NotImplementedError):
            MockFeed().backfill("INFY", "2026-01-01", "2026-02-01")


class _StubMStockClient:
    def __init__(self, latest=None, bars=None, error=None):
        self.latest = latest or {}
        self.bars = bars or []
        self.error = error
        self.get_bars_calls = []

    def get_latest(self, symbol):
        if self.error is not None:
            raise self.error
        return self.latest.get(symbol, {"ltp": 100, "ltt": "2026-08-20T09:15:00+05:30"})

    def get_bars(self, symbol, start, end, interval="day"):
        self.get_bars_calls.append((symbol, start, end, interval))
        return self.bars


class TestMStockFeed:
    def test_injected_client_counts_as_connected(self):
        feed = MStockFeed(client=_StubMStockClient())
        assert feed.is_connected

    def test_poll_injects_the_symbol(self):
        feed = MStockFeed(client=_StubMStockClient())
        payloads = feed.poll(["INFY", "TCS"])
        assert [p["symbol"] for p in payloads] == ["INFY", "TCS"]

    def test_poll_preserves_payload_symbol_when_present(self):
        client = _StubMStockClient(latest={"INFY": {"symbol": "INFY-EQ", "ltp": 100, "ltt": 1755660300}})
        feed = MStockFeed(client=client)
        assert feed.poll(["INFY"])[0]["symbol"] == "INFY-EQ"

    def test_client_errors_wrapped_as_feed_errors(self):
        feed = MStockFeed(client=_StubMStockClient(error=RuntimeError("HTTP 502")))
        with pytest.raises(FeedError, match="HTTP 502"):
            feed.poll(["INFY"])

    def test_poll_while_disconnected_raises(self):
        feed = MStockFeed(client=_StubMStockClient())
        feed.disconnect()
        with pytest.raises(FeedConnectionError):
            feed.poll(["INFY"])

    def test_naive_timestamps_assumed_ist(self):
        assert MStockFeed.naive_tz == "Asia/Kolkata"

    def test_backfill_maps_timeframe_to_mstock_interval(self):
        client = _StubMStockClient(bars=[{"t": "2026-08-20", "o": 1, "h": 2, "l": 1, "c": 2, "v": 10}])
        feed = MStockFeed(client=client)
        feed.backfill("INFY", "2026-08-01", "2026-08-20", timeframe="1hour")
        assert client.get_bars_calls[0][3] == "60minute"

    def test_backfill_normalizes_tuple_candles(self):
        client = _StubMStockClient(bars=[("2026-08-20T09:15:00", 1, 2, 0.5, 1.5, 100)])
        feed = MStockFeed(client=client)
        result = feed.backfill("INFY", "2026-08-01", "2026-08-20")
        assert result[0]["open"] == 1
        assert result[0]["symbol"] == "INFY"

    def test_backfill_unknown_timeframe_rejected(self):
        feed = MStockFeed(client=_StubMStockClient())
        with pytest.raises(ValueError, match="no mStock interval"):
            feed.backfill("INFY", "2026-08-01", "2026-08-20", timeframe="week")


# ===========================================================================
# Config
# ===========================================================================


class TestConfig:
    def test_defaults_are_nse(self):
        config = MarketDataConfig()
        assert config.timezone == "Asia/Kolkata"
        assert config.exchange == "NSE"
        assert config.session_anchor == ANCHOR

    def test_load_default_file(self):
        config = load_marketdata_config()
        assert config.timeframes == ("1min", "5min", "15min", "60min", "day")
        assert config.session_anchor == ANCHOR
        assert config.fill_gaps is False

    def test_replay_profile_fills_gaps(self):
        config = load_marketdata_config(profile="replay")
        assert config.fill_gaps is True
        assert config.max_gap_bars == 64

    def test_testing_profile_shrinks_buffers(self):
        config = load_marketdata_config(profile="testing")
        assert config.tick_buffer_size == 50
        assert config.reconnect_backoff_seconds == 0.0

    def test_unknown_profile_rejected(self):
        with pytest.raises(ValueError, match="unknown marketdata profile"):
            load_marketdata_config(profile="nonexistent")

    def test_unknown_keys_rejected(self, tmp_path):
        bad = tmp_path / "md.yaml"
        bad.write_text("default:\n  not_a_setting: 1\n")
        with pytest.raises(ValueError, match="unknown marketdata config keys"):
            load_marketdata_config(path=bad)

    def test_bad_timeframe_rejected(self, tmp_path):
        bad = tmp_path / "md.yaml"
        bad.write_text("default:\n  timeframes: [2min]\n")
        with pytest.raises(ValueError, match="unknown timeframe"):
            load_marketdata_config(path=bad)

    def test_null_anchor_disables_anchoring(self, tmp_path):
        doc = tmp_path / "md.yaml"
        doc.write_text("default:\n  session_anchor: null\n")
        assert load_marketdata_config(path=doc).session_anchor is None

    def test_bad_anchor_rejected(self, tmp_path):
        doc = tmp_path / "md.yaml"
        doc.write_text("default:\n  session_anchor: sometimes\n")
        with pytest.raises(ValueError, match="session_anchor"):
            load_marketdata_config(path=doc)

    @pytest.mark.parametrize(
        "kw",
        [dict(timeframes=()), dict(timezone="Mars/Olympus"), dict(tick_buffer_size=0),
         dict(bar_buffer_size=0), dict(late_grace_seconds=-1), dict(max_gap_bars=-1),
         dict(max_reconnect_attempts=0),
         dict(reconnect_backoff_seconds=5.0, reconnect_backoff_cap_seconds=1.0)],
    )
    def test_invalid_config_rejected(self, kw):
        with pytest.raises(Exception):
            MarketDataConfig(**kw)


# ===========================================================================
# Handler
# ===========================================================================


def handler(**kw) -> MarketDataHandler:
    config = kw.pop("config", None) or MarketDataConfig(
        timeframes=("1min",),
        reconnect_backoff_seconds=0.0,
        reconnect_backoff_cap_seconds=0.0,
        max_reconnect_attempts=3,
    )
    feed = kw.pop("feed", None)
    if feed is None:
        feed = MockFeed(naive_tz="Asia/Kolkata")
    h = MarketDataHandler(config=config, feed=feed, **kw)
    return h


class TestHandlerSubscriptions:
    def test_subscribe_and_unsubscribe(self):
        h = handler()
        h.subscribe_symbols(["infy", "tcs"])
        assert h.subscribed_symbols == ("INFY", "TCS")
        h.unsubscribe_symbols("tcs")
        assert h.subscribed_symbols == ("INFY",)

    def test_subscribe_is_idempotent(self):
        h = handler()
        h.subscribe_symbols("INFY")
        h.subscribe_symbols(["INFY"])
        assert h.subscribed_symbols == ("INFY",)

    def test_unsubscribe_unknown_symbol_is_a_noop(self):
        h = handler()
        h.unsubscribe_symbols("WIPRO")
        assert h.subscribed_symbols == ()

    def test_empty_subscription_rejected(self):
        with pytest.raises(ValueError, match="no symbols"):
            handler().subscribe_symbols([])

    def test_only_subscribed_symbols_are_polled(self):
        feed = MockFeed(naive_tz="Asia/Kolkata")
        h = handler(feed=feed)
        h.subscribe_symbols(["TCS", "INFY"])
        h.poll_once()
        assert feed.polled_symbols == [("INFY", "TCS")]

    def test_poll_without_subscriptions_skips_the_feed(self):
        feed = MockFeed(naive_tz="Asia/Kolkata")
        h = handler(feed=feed)
        assert h.poll_once() == []
        assert feed.poll_calls == 0


class TestHandlerIngestion:
    def test_poll_once_returns_normalized_ticks(self):
        feed = MockFeed(naive_tz="Asia/Kolkata")
        feed.push(payload())
        h = handler(feed=feed)
        h.subscribe_symbols("INFY")
        ticks = h.poll_once()
        assert len(ticks) == 1
        assert ticks[0].last == D("100.5")
        assert ticks[0].source == "mock"

    def test_quote_updated_to_latest(self):
        feed = MockFeed(naive_tz="Asia/Kolkata")
        feed.push(payload(last=100))
        feed.push(payload(ts="2026-08-20T09:15:06+05:30", last=101))
        h = handler(feed=feed)
        h.subscribe_symbols("INFY")
        h.poll_once()
        h.poll_once()
        assert h.get_current_quote("infy").last == D("101")

    def test_unknown_symbol_quote_is_none(self):
        assert handler().get_current_quote("WIPRO") is None

    def test_invalid_payload_does_not_break_the_batch(self):
        feed = MockFeed(naive_tz="Asia/Kolkata")
        feed.push(payload(last=-1), payload(symbol="TCS", last=200))
        h = handler(feed=feed)
        h.subscribe_symbols(["INFY", "TCS"])
        ticks = h.poll_once()
        assert [t.symbol for t in ticks] == ["TCS"]
        assert h.stats["invalid_payloads"] == 1

    def test_unsubscribed_payloads_ignored(self):
        feed = MockFeed(naive_tz="Asia/Kolkata")
        feed.push(payload(symbol="WIPRO"))
        h = handler(feed=feed)
        h.subscribe_symbols("INFY")
        assert h.poll_once() == []
        assert h.stats["ignored_unsubscribed"] == 1

    def test_poll_without_feed_raises(self):
        h = MarketDataHandler(config=MarketDataConfig(timeframes=("1min",)))
        h.subscribe_symbols("INFY")
        with pytest.raises(MarketDataError, match="no feed"):
            h.poll_once()

    def test_feed_type_checked(self):
        with pytest.raises(TypeError, match="DataFeed"):
            MarketDataHandler(feed="not a feed")  # type: ignore[arg-type]

    def test_naive_feed_timestamps_use_feed_timezone(self):
        feed = MockFeed(naive_tz="Asia/Kolkata")
        feed.push(payload(ts="2026-08-20 09:15:05"))
        h = handler(feed=feed)
        h.subscribe_symbols("INFY")
        ticks = h.poll_once()
        assert ticks[0].timestamp == ist(9, 15, 5)

    def test_ingest_directly_for_replay(self):
        h = handler()
        h.subscribe_symbols("INFY")
        ticks = h.ingest([payload()])
        assert len(ticks) == 1


class TestHandlerObservers:
    def test_tick_callbacks_fire(self):
        seen = []
        feed = MockFeed(naive_tz="Asia/Kolkata")
        feed.push(payload())
        h = handler(feed=feed)
        h.subscribe_symbols("INFY")
        h.on_tick_received(seen.append)
        h.poll_once()
        assert len(seen) == 1
        assert seen[0].symbol == "INFY"

    def test_tick_callback_removable(self):
        seen = []
        feed = MockFeed(naive_tz="Asia/Kolkata")
        feed.push(payload())
        h = handler(feed=feed)
        h.subscribe_symbols("INFY")
        cb = h.on_tick_received(seen.append)
        h.remove_tick_callback(cb)
        h.poll_once()
        assert seen == []

    def test_failing_tick_callback_isolated(self):
        seen = []

        def broken(t):
            raise RuntimeError("observer bug")

        feed = MockFeed(naive_tz="Asia/Kolkata")
        feed.push(payload())
        h = handler(feed=feed)
        h.subscribe_symbols("INFY")
        h.on_tick_received(broken)
        h.on_tick_received(seen.append)
        ticks = h.poll_once()
        assert len(ticks) == 1
        assert len(seen) == 1

    def test_bar_closed_callbacks_via_handler(self):
        seen = []
        h = handler()
        h.subscribe_symbols("INFY")
        h.on_bar_closed(seen.append)
        h.ingest([payload(ts="2026-08-20 09:15:05")])
        h.ingest([payload(ts="2026-08-20 09:16:05")])
        assert [b.ts for b in seen] == [ist(9, 15)]

    def test_non_callable_tick_callback_rejected(self):
        with pytest.raises(ValueError, match="callable"):
            handler().on_tick_received(42)  # type: ignore[arg-type]


class TestHandlerBarsAndBuffers:
    def test_get_current_bar_returns_in_progress(self):
        h = handler()
        h.subscribe_symbols("INFY")
        h.ingest([payload(ts="2026-08-20 09:15:05")])
        bar = h.get_current_bar("INFY", "1min")
        assert bar is not None and not bar.complete

    def test_get_current_bar_falls_back_to_latest_closed(self):
        h = handler()
        h.subscribe_symbols("INFY")
        h.ingest([payload(ts="2026-08-20 09:15:05")])
        h.flush_bars()
        bar = h.get_current_bar("INFY", "1min")
        assert bar is not None and bar.complete

    def test_get_current_bar_none_when_no_data(self):
        assert handler().get_current_bar("INFY", "1min") is None

    def test_recent_bars_oldest_first(self):
        h = handler()
        h.subscribe_symbols("INFY")
        for minute in (15, 16, 17):
            h.ingest([payload(ts=f"2026-08-20 09:{minute}:05", last=minute)])
        bars = h.get_recent_bars("INFY", "1min")
        assert [b.ts for b in bars] == [ist(9, 15), ist(9, 16)]

    def test_recent_bars_respects_count(self):
        h = handler()
        h.subscribe_symbols("INFY")
        for minute in range(15, 25):
            h.ingest([payload(ts=f"2026-08-20 09:{minute}:05")])
        assert len(h.get_recent_bars("INFY", "1min", count=3)) == 3

    def test_bar_buffer_is_bounded(self):
        config = MarketDataConfig(timeframes=("1min",), bar_buffer_size=3)
        h = handler(config=config)
        h.subscribe_symbols("INFY")
        for minute in range(15, 30):
            h.ingest([payload(ts=f"2026-08-20 09:{minute}:05")])
        assert len(h.get_recent_bars("INFY", "1min", count=100)) == 3

    def test_tick_buffer_is_bounded(self):
        config = MarketDataConfig(timeframes=("1min",), tick_buffer_size=5)
        h = handler(config=config)
        h.subscribe_symbols("INFY")
        for second in range(10, 30):
            h.ingest([payload(ts=f"2026-08-20 09:15:{second}")])
        assert len(h.get_recent_ticks("INFY", count=100)) == 5

    def test_recent_ticks_oldest_first(self):
        h = handler()
        h.subscribe_symbols("INFY")
        h.ingest([payload(ts="2026-08-20 09:15:05", last=1)])
        h.ingest([payload(ts="2026-08-20 09:15:06", last=2)])
        ticks = h.get_recent_ticks("INFY")
        assert [t.last for t in ticks] == [D("1"), D("2")]

    def test_recent_queries_validate_count(self):
        with pytest.raises(ValueError):
            handler().get_recent_bars("INFY", "1min", count=0)
        with pytest.raises(ValueError):
            handler().get_recent_ticks("INFY", count=0)

    def test_flush_bars_emits_through_observers(self):
        seen = []
        h = handler()
        h.subscribe_symbols("INFY")
        h.on_bar_closed(seen.append)
        h.ingest([payload(ts="2026-08-20 09:15:05")])
        closed = h.flush_bars()
        assert len(closed) == 1
        assert seen == closed

    def test_stats_merge_handler_and_aggregator_counters(self):
        h = handler()
        h.subscribe_symbols("INFY")
        h.ingest([payload(ts="2026-08-20 09:15:05")])
        h.ingest([payload(ts="2026-08-20 09:16:05")])
        stats = h.stats
        assert stats["ticks_received"] == 2
        assert stats["bars_closed"] == 1
        assert stats["pending_db_bars"] == 1


class TestReconnection:
    def test_transient_poll_failure_reconnects_and_retries(self):
        feed = MockFeed(naive_tz="Asia/Kolkata")
        h = handler(feed=feed)
        h.subscribe_symbols("INFY")
        feed.fail_next_polls(1)
        feed.push(payload())
        ticks = h.poll_once()
        assert len(ticks) == 1
        assert h.stats["poll_errors"] == 1
        assert h.stats["reconnects"] == 1

    def test_connect_failures_backed_off_then_succeed(self):
        sleeps = []
        feed = MockFeed(naive_tz="Asia/Kolkata")
        config = MarketDataConfig(
            timeframes=("1min",),
            max_reconnect_attempts=3,
            reconnect_backoff_seconds=1.0,
            reconnect_backoff_cap_seconds=4.0,
        )
        h = MarketDataHandler(config=config)
        h._sleep = sleeps.append
        feed.connect()  # pre-connect so attach does not reconnect
        h.connect_to_feed(feed)
        h.subscribe_symbols("INFY")
        feed.fail_next_polls(1)
        feed._connect_failures = 2  # two failed reconnects, third succeeds
        feed.push(payload())
        ticks = h.poll_once()
        assert len(ticks) == 1
        assert sleeps == [1.0, 2.0]  # exponential backoff

    def test_reconnect_exhaustion_raises(self):
        sleeps = []
        feed = MockFeed(naive_tz="Asia/Kolkata")
        config = MarketDataConfig(
            timeframes=("1min",),
            max_reconnect_attempts=3,
            reconnect_backoff_seconds=1.0,
            reconnect_backoff_cap_seconds=4.0,
        )
        h = MarketDataHandler(config=config)
        h._sleep = sleeps.append
        feed.connect()
        h.connect_to_feed(feed)
        h.subscribe_symbols("INFY")
        feed.fail_next_polls(1)
        feed._connect_failures = 99
        with pytest.raises(FeedConnectionError, match="after 3 attempt"):
            h.poll_once()
        assert sleeps == [1.0, 2.0]  # no sleep after the final attempt

    def test_backoff_is_capped(self):
        sleeps = []
        feed = MockFeed(naive_tz="Asia/Kolkata", connect_failures=99)
        config = MarketDataConfig(
            timeframes=("1min",),
            max_reconnect_attempts=5,
            reconnect_backoff_seconds=1.0,
            reconnect_backoff_cap_seconds=2.0,
        )
        h = MarketDataHandler(config=config)
        h._sleep = sleeps.append
        with pytest.raises(FeedConnectionError):
            h.connect_to_feed(feed)
        assert sleeps == [1.0, 2.0, 2.0, 2.0]

    def test_connect_to_feed_retries_initial_connection(self):
        feed = MockFeed(naive_tz="Asia/Kolkata", connect_failures=1)
        h = handler(feed=feed)  # config allows 3 attempts, zero backoff
        assert feed.is_connected
        assert feed.connect_calls == 2

    def test_disconnect_is_safe_without_feed(self):
        MarketDataHandler().disconnect()  # must not raise


# ===========================================================================
# Persistence — market_data_cache
# ===========================================================================


class TestPersistence:
    def _closed_bars_handler(self):
        h = handler()
        h.subscribe_symbols(["INFY", "TCS"])
        h.ingest([payload(ts="2026-08-20 09:15:05", last="100.5", volume=10)])
        h.ingest([payload(ts="2026-08-20 09:15:30", symbol="TCS", last="3000", volume=7)])
        h.ingest([payload(ts="2026-08-20 09:16:05", last="101")])
        h.ingest([payload(ts="2026-08-20 09:16:30", symbol="TCS", last="3001")])
        return h  # one closed 1min bar per symbol

    def test_closed_bars_written(self, db):
        h = self._closed_bars_handler()
        written = h.persist_closed_bars(db)
        assert written == 2
        with db.session() as session:
            rows = session.query(MarketDataCache).order_by(MarketDataCache.symbol).all()
            assert [r.symbol for r in rows] == ["INFY", "TCS"]
            assert rows[0].timeframe == "1min"
            assert rows[0].exchange == "NSE"
            assert rows[0].source == "mock"

    def test_ohlcv_round_trips(self, db):
        h = self._closed_bars_handler()
        h.persist_closed_bars(db)
        with db.session() as session:
            row = session.query(MarketDataCache).filter_by(symbol="INFY").one()
            assert Decimal(str(row.open)) == D("100.5")
            assert Decimal(str(row.close)) == D("100.5")
            assert int(row.volume) == 10

    def test_persist_is_idempotent(self, db):
        h = self._closed_bars_handler()
        assert h.persist_closed_bars(db) == 2
        assert h.persist_closed_bars(db) == 0  # pending drained

    def test_replay_does_not_duplicate_rows(self, db):
        h = self._closed_bars_handler()
        h.persist_closed_bars(db)
        h2 = self._closed_bars_handler()  # same bars again (restart/replay)
        assert h2.persist_closed_bars(db) == 0
        with db.session() as session:
            assert session.query(MarketDataCache).count() == 2

    def test_pending_cleared_after_success(self, db):
        h = self._closed_bars_handler()
        h.persist_closed_bars(db)
        assert h.stats["pending_db_bars"] == 0

    def test_synthetic_bars_never_persisted(self, db):
        config = MarketDataConfig(timeframes=("1min",), fill_gaps=True)
        h = handler(config=config)
        h.subscribe_symbols("INFY")
        h.ingest([payload(ts="2026-08-20 09:15:05")])
        h.ingest([payload(ts="2026-08-20 09:18:05")])  # gap → 2 synthetic bars
        assert h.stats["synthetic_bars"] == 2
        written = h.persist_closed_bars(db)
        assert written == 1  # only the real 09:15 bar
        with db.session() as session:
            assert session.query(MarketDataCache).count() == 1

    def test_flushed_bars_are_persistable(self, db):
        h = handler()
        h.subscribe_symbols("INFY")
        h.ingest([payload(ts="2026-08-20 09:15:05")])
        h.flush_bars()
        assert h.persist_closed_bars(db) == 1

    def test_nothing_pending_writes_nothing(self, db):
        assert handler().persist_closed_bars(db) == 0


# ===========================================================================
# Layering drift guard
# ===========================================================================


def test_marketdata_does_not_import_engine_or_forward():
    """Layering rule: marketdata/ stays free of the older subsystems.

    Same AST-based guard as the simulator's — prose that merely *mentions*
    ``backtest.engine`` must not trip the check. ``backtest.live`` is the
    one allowed edge (the mStock feed), ``backtest.db`` the other
    (persistence through DatabaseManager only).
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "backtest" / "marketdata"
    forbidden = ("backtest.engine", "backtest.forward")
    offenders: list[str] = []

    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if any(name == f or name.startswith(f + ".") for f in forbidden):
                    offenders.append(f"{path.name}: {name}")

    assert not offenders, f"marketdata/ must not import engine/ or forward/: {offenders}"


# ===========================================================================
# End-to-end: poll → bars → persistence
# ===========================================================================


class TestEndToEnd:
    def test_full_pipeline(self, db):
        feed = MockFeed(naive_tz="Asia/Kolkata")
        # Two polls inside the 09:15 bar, one that rolls into 09:16.
        feed.push(payload(ts="2026-08-20 09:15:05", last=100, volume=10))
        feed.push(payload(ts="2026-08-20 09:15:35", last=102, volume=5))
        feed.push(payload(ts="2026-08-20 09:16:05", last=101, volume=3))

        config = MarketDataConfig(
            timeframes=("1min", "5min"),
            reconnect_backoff_seconds=0.0,
            reconnect_backoff_cap_seconds=0.0,
        )
        h = MarketDataHandler(config=config, feed=feed)
        h.subscribe_symbols("INFY")
        closed_bars = []
        h.on_bar_closed(closed_bars.append)

        for _ in range(3):
            h.poll_once()

        # The 09:15 1min bar closed; the 5min bar is still building.
        assert [b.timeframe for b in closed_bars] == ["1min"]
        bar = closed_bars[0]
        assert bar.ts == ist(9, 15)
        assert bar.open == D("100")
        assert bar.high == D("102")
        assert bar.close == D("102")
        assert bar.volume == 15
        assert h.get_current_bar("INFY", "5min").tick_count == 3
        assert h.get_current_quote("INFY").last == D("101")

        # Shutdown: flush and persist everything.
        h.flush_bars()
        assert h.persist_closed_bars(db) == 3  # 1min ×2 + 5min ×1
        with db.session() as session:
            assert session.query(MarketDataCache).count() == 3
