from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from backtest.forward.options_bridge import DEFAULT_EXPRESSION as FORWARD_DEFAULT_EXPRESSION
from backtest.replay import (
    NoFreshReplayQuote,
    PointInTimeReplayFeed,
    ReplayChainGenerator,
    ReplayConfig,
    ReplayContract,
    ReplayDataset,
    ReplayFaultPlan,
    ReplayHarness,
    ReplayQuoteProvider,
    SimulatedClock,
)
from backtest.strategy.intent import Direction, MarketView

START = "2026-09-15 09:15"
CONTRACTS = {
    "C1": ReplayContract(
        instrument_token="C1",
        trading_symbol="NIFTY26SEP24800CE",
        underlying="NIFTY",
        expiry=date(2026, 9, 24),
        strike=Decimal("24800"),
        option_type="CE",
        lot_size=75,
    ),
    "C2": ReplayContract(
        instrument_token="C2",
        trading_symbol="NIFTY26SEP24850CE",
        underlying="NIFTY",
        expiry=date(2026, 9, 24),
        strike=Decimal("24850"),
        option_type="CE",
        lot_size=75,
    ),
}


class AlwaysBullish:
    """Small deterministic adapter standing in for the existing signal strategy."""

    def generate_market_view(self, candles):
        if candles.empty:
            return None
        return MarketView(
            direction=Direction.BULLISH,
            confidence=0.9,
            underlying="NIFTY",
            spot_price=Decimal(str(candles["close"].iloc[-1])),
            bar_timestamp=candles.index[-1],
        )


def as_utc(value):
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def make_dataset(
    bars: int = 15,
    *,
    timeframe: str = "1min",
    drop_underlying_rows: set[int] | None = None,
    option_overrides: dict[str, dict[int, dict[str, float]]] | None = None,
    option_missing: dict[str, set[int]] | None = None,
    lot_sizes: dict[str, dict[int, int]] | None = None,
) -> ReplayDataset:
    index = pd.date_range(START, periods=bars, freq="min", tz="Asia/Kolkata")
    underlying = pd.DataFrame(
        {
            "open": [24800.0 + i for i in range(bars)],
            "high": [24802.0 + i for i in range(bars)],
            "low": [24799.0 + i for i in range(bars)],
            "close": [24801.0 + i for i in range(bars)],
            "volume": [1000] * bars,
        },
        index=index,
    )
    if drop_underlying_rows:
        underlying = underlying.drop(index[[*drop_underlying_rows]])

    option_frames = {}
    option_missing = option_missing or {}
    option_overrides = option_overrides or {}
    lot_sizes = lot_sizes or {}
    for token, base_price in (("C1", 100.0), ("C2", 80.0)):
        rows = []
        for i in range(bars):
            override = option_overrides.get(token, {}).get(i, {})
            open_price = override.get("open", base_price)
            close_price = override.get("close", open_price)
            rows.append(
                {
                    "open": open_price,
                    "high": override.get("high", max(open_price, close_price) + 1),
                    "low": override.get("low", min(open_price, close_price) - 1),
                    "close": close_price,
                    "volume": 100,
                    "lot_size": lot_sizes.get(token, {}).get(i, 75),
                }
            )
        frame = pd.DataFrame(rows, index=index)
        if option_missing.get(token):
            frame = frame.drop(index[[*option_missing[token]]])
        option_frames[token] = frame
    return ReplayDataset.from_frames(
        underlying,
        option_frames,
        CONTRACTS,
        underlying_symbol="NIFTY",
        timeframe=timeframe,
    )


def config(**expression_overrides) -> ReplayConfig:
    exit_config = {
        "signal_flip": False,
        "min_days_to_expiry": None,
    }
    exit_config.update(expression_overrides.pop("exit", {}))
    return ReplayConfig(
        strategy_name="directional_options",
        expression={"type": "bull_call_spread", "exit": exit_config},
        **expression_overrides,
    )


def test_replay_config_exports_the_existing_forward_expression_defaults():
    assert ReplayConfig().expression == FORWARD_DEFAULT_EXPRESSION


def run_harness(dataset, *, fault_plan=None, replay_config=None):
    return ReplayHarness(
        dataset,
        replay_config or config(),
        strategy=AlwaysBullish(),
        fault_plan=fault_plan,
    )


def test_feed_is_point_in_time_and_missing_rows_are_not_carried_forward():
    dataset = make_dataset(
        bars=3,
        option_missing={"C1": {1}},
        option_overrides={"C1": {0: {"close": 101}, 2: {"close": 103}}},
    )
    clock = SimulatedClock(dataset.bar_close(dataset.underlying.index[0]))
    feed = PointInTimeReplayFeed(dataset, clock)
    provider = ReplayQuoteProvider(feed)

    first = feed.advance(0)
    assert first.bar_close > first.bar_open
    assert provider.get_quote("C1")["ltp"] == Decimal("101.0")

    feed.advance(1)
    assert not feed.available_quote("C1")
    with pytest.raises(NoFreshReplayQuote):
        provider.get_quote("C1")

    feed.advance(2)
    assert provider.get_quote("C1")["ltp"] == Decimal("103.0")


def test_chain_expiry_filter_rolls_forward_without_using_expired_contracts():
    contracts = {
        "C1": ReplayContract(
            "C1",
            "NIFTY26SEP24800CE",
            "NIFTY",
            date(2026, 9, 17),
            Decimal("24800"),
            "CE",
            75,
        ),
        "C2": ReplayContract(
            "C2",
            "NIFTY26SEP24850CE",
            "NIFTY",
            date(2026, 9, 24),
            Decimal("24850"),
            "CE",
            75,
        ),
    }
    dataset = ReplayDataset.from_frames(
        make_dataset(bars=1).underlying,
        make_dataset(bars=1).option_frames,
        contracts,
        underlying_symbol="NIFTY",
    )
    feed = PointInTimeReplayFeed(
        dataset,
        SimulatedClock(dataset.bar_close(dataset.underlying.index[0])),
    )
    feed.advance(0)
    chain = ReplayChainGenerator(feed)

    assert chain.available_expiries("NIFTY", reference=date(2026, 9, 16)) == [
        date(2026, 9, 17),
        date(2026, 9, 24),
    ]
    assert chain.available_expiries("NIFTY", reference=date(2026, 9, 18)) == [date(2026, 9, 24)]


def test_replay_fills_on_the_next_bar_open_and_is_deterministic():
    dataset = make_dataset()
    harness = run_harness(dataset)

    first = harness.run()
    second = harness.run()
    fill = next(event for event in first.journal if event["kind"] == "fill")
    intent = next(event for event in first.journal if event["kind"] == "intent")

    assert fill["intent_bar_index"] + 1 == fill["fill_bar_index"]
    assert fill["intent_bar_index"] == intent["bar_index"]
    assert fill["intent_ts"] == intent["ts"]
    assert fill["ts"] == fill["intent_ts"]  # T close equals T+1 open on contiguous bars
    assert fill["quote_reads"]
    assert {read["phase"] for read in fill["quote_reads"]} == {"open"}
    assert all(
        as_utc(read["quote_timestamp"]) >= as_utc(fill["intent_ts"]) for read in fill["quote_reads"]
    )
    assert first.to_dict() == second.to_dict()
    assert first.run_id == second.run_id
    assert first.summary["db_writes"] == 0
    assert first.summary["ledger_invariants_passed"] is True


def test_no_lookahead_matches_every_closed_bar_prefix():
    result = run_harness(make_dataset()).verify_no_lookahead()

    assert result["passed"] is True
    assert result["checkpoints"] == 15
    assert result["method"] == "full replay vs every closed-bar truncation"


def test_missing_fill_quote_rejects_instead_of_using_a_previous_price():
    dataset = make_dataset(bars=13)
    missing_token = "C1"
    fill_bar_open = dataset.underlying.index[12].isoformat()
    plan = ReplayFaultPlan(drop_quotes={(missing_token, fill_bar_open)})

    result = run_harness(dataset, fault_plan=plan).run()

    assert result.summary["fills"] == 0
    assert result.summary["rejections"] >= 1
    assert any(
        "STRIKE_NO_FRESH_QUOTE" in str(event.get("reason"))
        for event in result.journal
        if event["kind"] == "rejected"
    )


def test_missing_contract_at_fill_window_is_journaled():
    dataset = make_dataset(bars=13)
    fill_bar_open = dataset.underlying.index[12].isoformat()
    plan = ReplayFaultPlan(drop_contracts={(fill_bar_open, "2026-09-24", "24800", "CE")})

    result = run_harness(dataset, fault_plan=plan).run()

    assert result.summary["fills"] == 0
    assert any(
        "STRIKE_NO_FRESH_QUOTE" in str(event.get("reason"))
        for event in result.journal
        if event["kind"] == "rejected"
    )


def test_held_position_halts_on_missing_leg_quote_and_retains_last_mark():
    dataset = make_dataset(bars=15)
    stale_bar_open = dataset.underlying.index[13].isoformat()
    plan = ReplayFaultPlan(drop_quotes={("C1", stale_bar_open)})

    result = run_harness(dataset, fault_plan=plan).run()

    halt = next(event for event in result.journal if event["kind"] == "feed_halt")
    assert halt["bar_index"] == 14
    assert halt["dataset_index"] == 13
    assert halt["code"] == "STRIKE_NO_FRESH_QUOTE"
    assert result.summary["feed_halts"] == 1
    assert result.summary["fills"] == 1
    assert result.summary["open_structures"] == 1
    assert result.summary["ledger_invariants_passed"] is True


def test_lot_size_change_on_held_contract_halts_and_preserves_last_mark():
    dataset = make_dataset(
        bars=15,
        lot_sizes={"C1": {13: 50}},
    )

    result = run_harness(dataset).run()

    halt = next(
        event
        for event in result.journal
        if event["kind"] == "feed_halt" and event.get("code") == "LOT_SIZE_CHANGED"
    )
    assert halt["dataset_index"] == 13
    assert halt["contracts"][0]["position_lot_size"] == 75
    assert halt["contracts"][0]["feed_lot_size"] == 50
    assert result.summary["fills"] == 1
    assert result.summary["open_structures"] == 1


def test_stale_feed_window_is_journaled_and_suppresses_strategy_decisions():
    dataset = make_dataset(bars=15)
    plan = ReplayFaultPlan(
        stale_windows=[
            (
                dataset.underlying.index[5].isoformat(),
                dataset.underlying.index[6].isoformat(),
            )
        ]
    )

    result = run_harness(dataset, fault_plan=plan).run()

    halts = [
        event
        for event in result.journal
        if event["kind"] == "feed_halt" and event.get("code") == "STALE_FEED"
    ]
    assert [event["dataset_index"] for event in halts] == [5, 6]
    assert [event["bar_index"] for event in halts] == [6, 7]
    assert not any(
        event.get("bar_index") in {6, 7}
        for event in result.journal
        if event["kind"] in {"decision", "intent"}
    )


def test_lot_size_change_at_fill_is_a_rejection_not_a_resized_order():
    dataset = make_dataset(bars=13)
    fill_bar_open = dataset.underlying.index[12].isoformat()
    plan = ReplayFaultPlan(lot_size_overrides={("C1", fill_bar_open): 50})

    result = run_harness(dataset, fault_plan=plan).run()

    assert result.summary["fills"] == 0
    assert any(
        "LOT_SIZE_CHANGED" in str(event.get("reason"))
        for event in result.journal
        if event["kind"] == "rejected"
    )


def test_stop_wins_when_stop_and_target_are_touched_in_one_candle():
    dataset = make_dataset(
        bars=14,
        option_overrides={
            "C1": {13: {"open": 100, "high": 110, "low": 95, "close": 100}},
            "C2": {13: {"open": 80, "high": 85, "low": 70, "close": 80}},
        },
    )
    replay_config = config(exit={"stop_loss_points": 500, "take_profit_points": 500})

    result = run_harness(dataset, replay_config=replay_config).run()

    exit_event = next(event for event in result.journal if event["kind"] == "exit")
    assert exit_event["reason"] == "stop_loss"
    assert "same-candle ambiguity" in exit_event["detail"]
    assert "stop assumed before target" in exit_event["detail"]
    assert any(read["phase"] == "ohlc" for read in exit_event["quote_reads"])
    assert result.summary["exits"] == 1


def test_resampling_uses_complete_nse_session_bars_only():
    session = pd.date_range(START, periods=375, freq="min", tz="Asia/Kolkata")
    frame = pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1},
        index=session,
    )
    contract = ReplayContract(
        "C", "NIFTY26SEP24800CE", "NIFTY", date(2026, 9, 24), Decimal("24800"), "CE", 75
    )
    dataset = ReplayDataset.from_frames(
        frame,
        {"C": frame},
        {"C": contract},
        underlying_symbol="NIFTY",
        timeframe="15min",
    )
    local_index = dataset.underlying.index.tz_convert("Asia/Kolkata")

    assert len(dataset.underlying) == 25
    assert local_index[0].strftime("%H:%M") == "09:15"
    assert local_index[-1].strftime("%H:%M") == "15:15"
    assert (local_index[-1] + pd.Timedelta(minutes=15)).strftime("%H:%M") == "15:30"

    incomplete = make_dataset(
        bars=16,
        timeframe="5min",
        drop_underlying_rows={2, 15},
    )
    incomplete_local = incomplete.underlying.index.tz_convert("Asia/Kolkata")
    assert len(incomplete.underlying) == 2
    assert [stamp.strftime("%H:%M") for stamp in incomplete_local] == ["09:20", "09:25"]


def test_date_and_time_vendor_columns_are_combined_deterministically():
    timestamps = pd.date_range(START, periods=2, freq="min", tz="Asia/Kolkata")
    values = {
        "date": [stamp.strftime("%Y-%m-%d") for stamp in timestamps],
        "time": [stamp.strftime("%H:%M:%S") for stamp in timestamps],
        "open": [100, 101],
        "high": [101, 102],
        "low": [99, 100],
        "close": [100, 101],
    }
    underlying = pd.DataFrame(values)
    options = pd.DataFrame(values)
    contract = ReplayContract(
        "C", "NIFTY26SEP24800CE", "NIFTY", date(2026, 9, 24), Decimal("24800"), "CE", 75
    )

    dataset = ReplayDataset.from_frames(
        underlying,
        {"C": options},
        {"C": contract},
        underlying_symbol="NIFTY",
    )

    assert len(dataset.underlying) == 2
    assert dataset.underlying.index[0].tz_convert("Asia/Kolkata").strftime("%H:%M") == "09:15"


def test_vendor_close_timestamps_are_normalized_to_candle_open():
    opened = make_dataset(bars=2)
    closed_underlying = opened.underlying.copy()
    closed_underlying.index = closed_underlying.index + pd.Timedelta(minutes=1)
    closed_options = {}
    for token, frame in opened.option_frames.items():
        closed_options[token] = frame.copy()
        closed_options[token].index = closed_options[token].index + pd.Timedelta(minutes=1)

    normalized = ReplayDataset.from_frames(
        closed_underlying,
        closed_options,
        opened.contracts,
        underlying_symbol="NIFTY",
        timestamp_kind="close",
    )

    assert normalized.underlying.index.equals(opened.underlying.index)
    assert normalized.fingerprint != opened.fingerprint


def test_csv_contract_metadata_and_lot_size_history_are_preserved(tmp_path):
    timestamps = pd.date_range(START, periods=3, freq="min", tz="Asia/Kolkata")
    underlying_path = tmp_path / "underlying.csv"
    option_path = tmp_path / "option.csv"
    pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [24800, 24801, 24802],
            "high": [24802, 24803, 24804],
            "low": [24799, 24800, 24801],
            "close": [24801, 24802, 24803],
            "volume": [1000, 1000, 1000],
        }
    ).to_csv(underlying_path, index=False)
    pd.DataFrame(
        {
            "timestamp": timestamps,
            "instrument_token": ["C1"] * 3,
            "trading_symbol": ["NIFTY26SEP24800CE"] * 3,
            "underlying": ["NIFTY"] * 3,
            "expiry": ["2026-09-24"] * 3,
            "strike": [24800] * 3,
            "option_type": ["CE"] * 3,
            "lot_size": [75, 75, 50],
            "open": [100, 101, 102],
            "high": [101, 102, 103],
            "low": [99, 100, 101],
            "close": [100, 101, 102],
            "volume": [100, 100, 100],
        }
    ).to_csv(option_path, index=False)

    dataset = ReplayDataset.from_csv(
        underlying_path,
        [option_path],
        underlying_symbol="NIFTY",
    )

    assert dataset.contracts["C1"].lot_size == 75
    assert dataset.option_frames["C1"]["lot_size"].tolist() == [75, 75, 50]
    assert set(dataset.source_fingerprints) == {
        "underlying.csv",
        "option:C1:option.csv",
    }
    changed_metadata = ReplayDataset.from_csv(
        underlying_path,
        [option_path],
        underlying_symbol="NIFTY",
        contract_metadata={"C1": {"expiry": "2026-10-29"}},
    )
    assert changed_metadata.fingerprint != dataset.fingerprint


def test_export_is_labeled_replay_and_discloses_execution_limits(tmp_path):
    result = run_harness(make_dataset()).run()
    artifact = result.export(tmp_path / "evidence.json")
    payload = json.loads(artifact.read_text())

    assert payload["label"] == "REPLAY"
    assert payload["data_fingerprint"] == result.data_fingerprint
    assert payload["summary"]["db_writes"] == 0
    assert payload["fault_plan"]["drop_quotes"] == []
    assert any("bid/ask" in item.lower() for item in payload["limitations"])
    assert any("live soak remains mandatory" in item.lower() for item in payload["limitations"])
    assert "sharpe" not in payload
    assert artifact.exists()


def test_injected_broker_reject_is_preserved_as_failure_evidence():
    dataset = make_dataset(bars=13)
    reject_bar_open = dataset.underlying.index[12].isoformat()
    plan = ReplayFaultPlan(reject_entry_bars={reject_bar_open})

    result = run_harness(dataset, fault_plan=plan).run()

    assert result.summary["fills"] == 0
    assert any(
        "INJECTED_BROKER_REJECT" in str(event.get("reason"))
        for event in result.journal
        if event["kind"] == "rejected"
    )
