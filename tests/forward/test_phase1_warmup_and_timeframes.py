"""Phase 1 tests — Unified Warmup, Shared DB Pool, ScriptMaster Cache & Timeframe Aggregation.

Covers:
1. Canonical IST timestamp normalization (truncated mStock '+05', UTC timestamptz, epoch).
2. Backtest-parity IST bucket alignment & TimeframeBarAggregator (5m..1d + 15:30 EOD).
3. Strategy lookback warmup inference (EMA 3x IIR convergence, 600-bar cap, 1day VCP, options).
4. DbSource shared DatabaseManager.from_env() pool (POOL-3) + single-query get_candles_bulk().
5. Session-scoped ScriptMasterCache (1 download across N equity symbols) + fetch_intraday_bars().
6. Silent StrategyRunner.seed_history() (Caveat Q-6: zero phantom orders) + option spot sync.
7. PortfolioManager Finding F1 fix (1min live feed -> 15min single & pool runners), mid-session
   partial-bucket priming, SessionBarRecorder reuse, and duplicate-runner guard.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pandas as pd
import pytest

from backtest.data.db_source import DbSource, base_stored_timeframe
from backtest.data.mstock_live_feed import (
    MStockLiveFeed,
    clear_scriptmaster_cache,
)
from backtest.forward.paper_runner import (
    MAX_BARS_PER_SYMBOL,
    TARGET_POOL,
    TARGET_SINGLE,
    OrderLedger,
    PaperBroker,
    RunnerConfig,
    StrategyRunner,
)
from backtest.forward.portfolio_manager import PortfolioManager
from backtest.forward.warmup import (
    SessionBarRecorder,
    TimeframeBarAggregator,
    WarmupService,
    bucket_start_ts,
    infer_warmup_bars,
    normalize_ist_bar_ts,
)
from backtest.strategy.base import BaseStrategy


# ---------------------------------------------------------------------------
# Dummy strategies for deterministic testing
# ---------------------------------------------------------------------------


class _AlwaysBuyStrategy(BaseStrategy):
    """Strategy that emits BUY on every evaluate call to prove seed_history is silent."""

    params = {"ema_fast": 9, "ema_slow": 21}

    def __init__(self, ema_fast: int = 9, ema_slow: int = 21) -> None:
        super().__init__()
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.eval_calls = 0

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        self.eval_calls += 1
        signals = pd.Series(0, index=data.index, dtype=int)
        if len(signals) > 0:
            signals.iloc[-1] = 1
        return signals


class _PoolSpyStrategy(BaseStrategy):
    """Tracks pool scan invocations to verify coarse-timeframe tick_end gating."""

    params = {"sma_slow": 50}

    def __init__(self, sma_slow: int = 50) -> None:
        super().__init__()
        self.sma_slow = sma_slow
        self.eval_count = 0

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        self.eval_count += 1
        return pd.Series(0, index=data.index, dtype=int)


# ---------------------------------------------------------------------------
# 1. Timestamp normalization & bucket floor parity
# ---------------------------------------------------------------------------


class TestTimestampAndBuckets:
    def test_normalize_ist_handles_truncated_mstock_offset_and_utc_db(self) -> None:
        # mStock intraday candles ship truncated '+05' for IST (+05:30)
        assert (
            normalize_ist_bar_ts("2026-10-23T09:15:00+05")
            == "2026-10-23 09:15:00"
        )
        # Full IST offset
        assert (
            normalize_ist_bar_ts("2026-10-23T14:45:00+05:30")
            == "2026-10-23 14:45:00"
        )
        # PostgreSQL timestamptz in UTC (03:45 UTC == 09:15 IST)
        utc_ts = pd.Timestamp("2026-10-23 03:45:00+00:00")
        assert normalize_ist_bar_ts(utc_ts) == "2026-10-23 09:15:00"
        # Naive IST string
        assert (
            normalize_ist_bar_ts("2026-10-23 12:30:00")
            == "2026-10-23 12:30:00"
        )

    def test_bucket_start_ts_matches_dbsource_resample_floors(self) -> None:
        assert bucket_start_ts("2026-10-23 09:15:00", "5min") == "2026-10-23 09:15:00"
        assert bucket_start_ts("2026-10-23 09:19:00", "5min") == "2026-10-23 09:15:00"
        assert bucket_start_ts("2026-10-23 09:29:00", "15min") == "2026-10-23 09:15:00"
        assert bucket_start_ts("2026-10-23 09:30:00", "15min") == "2026-10-23 09:30:00"
        # DbSource._resample('1h') floors 09:15..09:59 to 09:00
        assert bucket_start_ts("2026-10-23 09:15:00", "1hour") == "2026-10-23 09:00:00"
        assert bucket_start_ts("2026-10-23 09:59:00", "1hour") == "2026-10-23 09:00:00"
        assert bucket_start_ts("2026-10-23 11:45:00", "4hour") == "2026-10-23 08:00:00"
        assert bucket_start_ts("2026-10-23 12:15:00", "4hour") == "2026-10-23 12:00:00"
        assert bucket_start_ts("2026-10-23 14:10:00", "1day") == "2026-10-23 00:00:00"


# ---------------------------------------------------------------------------
# 2. TimeframeBarAggregator
# ---------------------------------------------------------------------------


class TestTimeframeBarAggregator:
    def test_15min_seals_exact_ohlcv_and_ignores_1530_straggler(self) -> None:
        agg = TimeframeBarAggregator()
        # Feed 09:15 .. 09:29 (15 bars) — none should seal yet
        for idx in range(15):
            minute = 15 + idx
            bar = {
                "ts": f"2026-10-23 09:{minute:02d}:00",
                "open": 100.0 + idx,
                "high": 105.0 + idx,
                "low": 99.0 - idx,
                "close": 101.0 + idx,
                "volume": 10.0,
            }
            assert agg.ingest_minute_bar("r1", "BANKNIFTY", "15min", bar) == []

        # 09:30 arrives -> seals 09:15 bucket with exact OHLCV
        sealed = agg.ingest_minute_bar(
            "r1",
            "BANKNIFTY",
            "15min",
            {
                "ts": "2026-10-23 09:30:00",
                "open": 200.0,
                "high": 201.0,
                "low": 199.0,
                "close": 200.5,
                "volume": 50.0,
            },
        )
        assert len(sealed) == 1
        assert sealed[0] == {
            "ts": "2026-10-23 09:15:00",
            "open": 100.0,
            "high": 119.0,
            "low": 85.0,
            "close": 115.0,
            "volume": 150.0,
        }

        # End-of-day: 15:15..15:29 sealed by 15:30 without starting a new 15:30 candle
        agg2 = TimeframeBarAggregator()
        agg2.ingest_minute_bar(
            "r2",
            "NIFTY",
            "15min",
            {
                "ts": "2026-10-23 15:15:00",
                "open": 24000.0,
                "high": 24020.0,
                "low": 23990.0,
                "close": 24010.0,
                "volume": 100.0,
            },
        )
        eod_sealed = agg2.ingest_minute_bar(
            "r2",
            "NIFTY",
            "15min",
            {
                "ts": "2026-10-23 15:30:00",
                "open": 24012.0,
                "high": 24015.0,
                "low": 24010.0,
                "close": 24014.0,
                "volume": 5.0,
            },
        )
        assert len(eod_sealed) == 1
        assert eod_sealed[0]["ts"] == "2026-10-23 15:15:00"
        assert ("r2", "NIFTY") not in agg2._forming

    def test_1hour_and_1day_flush_at_1530_session_close(self) -> None:
        agg = TimeframeBarAggregator()
        agg.ingest_minute_bar(
            "r1",
            "RELIANCE",
            "1hour",
            {
                "ts": "2026-10-23 15:00:00",
                "open": 2900.0,
                "high": 2915.0,
                "low": 2895.0,
                "close": 2910.0,
                "volume": 1000.0,
            },
        )
        sealed_1h = agg.ingest_minute_bar(
            "r1",
            "RELIANCE",
            "1hour",
            {
                "ts": "2026-10-23 15:30:00",
                "open": 2910.0,
                "high": 2912.0,
                "low": 2908.0,
                "close": 2911.0,
                "volume": 50.0,
            },
        )
        assert len(sealed_1h) == 1
        assert sealed_1h[0]["ts"] == "2026-10-23 15:00:00"
        assert sealed_1h[0]["close"] == 2911.0

        agg.ingest_minute_bar(
            "r2",
            "RELIANCE",
            "1day",
            {
                "ts": "2026-10-23 09:15:00",
                "open": 2880.0,
                "high": 2920.0,
                "low": 2875.0,
                "close": 2905.0,
                "volume": 5000.0,
            },
        )
        sealed_1d = agg.ingest_minute_bar(
            "r2",
            "RELIANCE",
            "1day",
            {
                "ts": "2026-10-23 15:30:00",
                "open": 2905.0,
                "high": 2925.0,
                "low": 2900.0,
                "close": 2922.0,
                "volume": 200.0,
            },
        )
        assert len(sealed_1d) == 1
        assert sealed_1d[0] == {
            "ts": "2026-10-23 00:00:00",
            "open": 2880.0,
            "high": 2925.0,
            "low": 2875.0,
            "close": 2922.0,
            "volume": 5200.0,
        }


# ---------------------------------------------------------------------------
# 3. Lookback inference & 600-bar buffer cap
# ---------------------------------------------------------------------------


class TestWarmupInference:
    def test_max_bars_per_symbol_is_600(self) -> None:
        assert MAX_BARS_PER_SYMBOL == 600

    def test_ema_convergence_3x_and_vcp_daily_and_instant_option_zero(self) -> None:
        strat_ema21 = _AlwaysBuyStrategy(ema_fast=9, ema_slow=21)
        assert infer_warmup_bars(strat_ema21, "15min") == 63

        strat_ema200 = _AlwaysBuyStrategy(ema_fast=50, ema_slow=200)
        assert infer_warmup_bars(strat_ema200, "15min") == 600

        strat_daily_vcp = _PoolSpyStrategy(sma_slow=50)
        assert infer_warmup_bars(strat_daily_vcp, "1day") >= 260

        class _InstantLongOption:
            name = "instant_long_option"

        assert infer_warmup_bars(_InstantLongOption(), "5min") == 0


# ---------------------------------------------------------------------------
# 4. Shared DB Pool (POOL-3) & Bulk SQL Read
# ---------------------------------------------------------------------------


class TestDbSourceSharedPoolAndBulk:
    def test_get_engine_delegates_to_database_manager_singleton(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_engine = object()
        fake_mgr = MagicMock()
        fake_mgr.engine = fake_engine
        monkeypatch.setattr(
            "backtest.db.DatabaseManager.from_env",
            classmethod(lambda cls: fake_mgr),
        )
        src = DbSource.__new__(DbSource)
        src._engine = None
        assert src._get_engine() is fake_engine

    def test_get_engine_never_falls_back_to_private_create_engine(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "backtest.db.DatabaseManager.from_env",
            classmethod(lambda cls, **kw: (_ for _ in ()).throw(ConnectionError("db down"))),
        )
        src = DbSource.__new__(DbSource)
        src._engine = None
        with pytest.raises(ConnectionError, match="db down"):
            src._get_engine()

    def test_get_candles_bulk_fetches_all_symbols_in_single_ts_bounded_query(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert base_stored_timeframe("15min") == "1min"
        assert base_stored_timeframe("1hour") == "1min"
        assert base_stored_timeframe("1day") == "1day"
        assert base_stored_timeframe("1week") == "1day"

        src = DbSource.__new__(DbSource)
        fake_engine = MagicMock()
        src._engine = fake_engine

        # Return interleaved UTC timestamptz 1min rows for two symbols
        raw_df = pd.DataFrame(
            [
                {
                    "symbol": "RELIANCE",
                    "ts": pd.Timestamp("2026-10-23 03:45:00+00:00"),
                    "open": 2900.0,
                    "high": 2910.0,
                    "low": 2895.0,
                    "close": 2905.0,
                    "volume": 1000,
                },
                {
                    "symbol": "TCS",
                    "ts": pd.Timestamp("2026-10-23 03:45:00+00:00"),
                    "open": 4100.0,
                    "high": 4120.0,
                    "low": 4090.0,
                    "close": 4115.0,
                    "volume": 800,
                },
            ]
        )
        calls: List[Any] = []

        def _fake_read_sql(stmt: Any, conn: Any, params: Any = None) -> pd.DataFrame:
            calls.append((str(stmt), params))
            return raw_df.copy()

        monkeypatch.setattr(pd, "read_sql", _fake_read_sql)
        res = src.get_candles_bulk(
            ["RELIANCE", "TCS"], "2026-10-20", "2026-10-23", interval="15min"
        )

        # Zero unbounded discovery queries — engine.connect() never touched!
        fake_engine.connect.assert_not_called()
        assert len(calls) == 1
        sql_text, params = calls[0]
        assert "GROUP BY" not in sql_text.upper()
        assert "WHERE timeframe = :timeframe" in sql_text
        assert "ts >= :start" in sql_text and "ts < :end" in sql_text
        assert "ORDER BY ts ASC" in sql_text
        assert params["timeframe"] == "1min"
        assert set(res.keys()) == {"RELIANCE", "TCS"}
        assert str(res["RELIANCE"].index[0]) == "2026-10-23 09:15:00"
        assert float(res["TCS"]["close"].iloc[0]) == 4115.0


# ---------------------------------------------------------------------------
# 5. Session-scoped ScriptMasterCache & MStockLiveFeed.fetch_intraday_bars
# ---------------------------------------------------------------------------


class TestScriptMasterCacheAndIntraday:
    def test_scriptmaster_downloaded_once_across_pool_of_equities(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clear_scriptmaster_cache()
        csv_body = (
            "instrument_token,exchange_token,tradingsymbol,name,last_price,"
            "expiry,strike,tick_size,lot_size,instrument_type,segment,exchange\n"
            "2885,2885,RELIANCE-EQ,RELIANCE,0,,0,0.05,1,EQ,EQ,NSE\n"
            "11536,11536,TCS-EQ,TCS,0,,0,0.05,1,EQ,EQ,NSE\n"
            "1594,1594,INFY-EQ,INFY,0,,0,0.05,1,EQ,EQ,NSE\n"
        )
        resp_sm = MagicMock()
        resp_sm.status_code = 200
        resp_sm.text = csv_body
        resp_sm.raise_for_status.return_value = None

        resp_intra = MagicMock()
        resp_intra.status_code = 200
        resp_intra.raise_for_status.return_value = None
        resp_intra.json.return_value = {
            "status": "true",
            "data": {
                "candles": [
                    ["2026-10-23T09:15:00+05", 2900.0, 2905.0, 2898.0, 2903.0, 1200],
                    ["2026-10-23T09:16:00+05", 2903.0, 2908.0, 2901.0, 2907.0, 950],
                ]
            },
        }

        sm_calls = 0

        def _fake_requests_get(url: str, **kwargs: Any) -> Any:
            nonlocal sm_calls
            if "scriptmaster" in url:
                sm_calls += 1
                return resp_sm
            return resp_intra

        monkeypatch.setattr("backtest.data.mstock_live_feed.requests.get", _fake_requests_get)
        monkeypatch.setenv("MSTOCK_API_KEY", "test_key")
        monkeypatch.setattr("backtest.live.auth.get_session_token", lambda: "session_tok_1")

        feed = MStockLiveFeed()
        assert feed.emits_minute_bars is True

        for sym in ("RELIANCE", "TCS", "INFY"):
            rows = feed.fetch_intraday_bars(sym)
            assert len(rows) == 2
            assert rows[0]["ts"] == "2026-10-23 09:15:00"
            assert rows[1]["ts"] == "2026-10-23 09:16:00"

        # 3 distinct equities -> exactly 1 ScriptMaster download!
        assert sm_calls == 1

        # Index uses hardcoded token (26000) without touching ScriptMaster
        idx_rows = feed.fetch_intraday_bars("NIFTY")
        assert len(idx_rows) == 2
        assert sm_calls == 1
        clear_scriptmaster_cache()


# ---------------------------------------------------------------------------
# 6. Silent seed_history (Caveat Q-6)
# ---------------------------------------------------------------------------


class TestSilentSeedHistory:
    def test_seed_history_populates_buffer_with_zero_orders(self) -> None:
        ledger = OrderLedger()
        broker = PaperBroker(ledger)
        strat = _AlwaysBuyStrategy(ema_fast=9, ema_slow=21)
        cfg = RunnerConfig(
            name="WarmupSilentTest",
            strategy_name="sma_crossover",
            symbols=["RELIANCE"],
            timeframe="15min",
            allocated_capital=200_000.0,
            source="mstock",
        )
        runner = StrategyRunner(cfg, ledger=ledger, broker=broker, strategy=strat)
        runner.start()

        history = [
            {
                "ts": f"2026-10-22 {9 + (i // 4):02d}:{(i % 4) * 15:02d}:00",
                "open": 2800.0 + i,
                "high": 2805.0 + i,
                "low": 2795.0 + i,
                "close": 2802.0 + i,
                "volume": 500.0,
            }
            for i in range(24)
        ]
        seeded = runner.seed_history({"RELIANCE": history})
        assert seeded == 24
        assert strat.eval_calls == 0
        assert len(runner.positions) == 0
        assert len(runner.closed_trades) == 0
        assert ledger.order_count == 0
        assert ledger.fill_count == 0
        assert runner.warmup_status == "READY"
        assert runner.warmup_bars_seeded == 24
        assert runner.last_price["RELIANCE"] == 2825.0
        state = runner.get_state()
        assert state["warmup_status"] == "READY"
        assert state["warmup_bars_seeded"] == 24


# ---------------------------------------------------------------------------
# 7. End-to-End PortfolioManager: Finding F1 fix, Mid-Session Warmup & Dup Guard
# ---------------------------------------------------------------------------


class TestPortfolioManagerWarmupAndAggregation:
    def test_live_1min_bars_aggregate_into_15min_runner_fixing_finding_f1(
        self,
    ) -> None:
        mgr = PortfolioManager(auto_start_feed=False, enable_broker_warmup=False)
        strat = _AlwaysBuyStrategy(ema_fast=9, ema_slow=21)
        cfg = RunnerConfig(
            name="BNF-15m",
            strategy_name="sma_crossover",
            symbols=["BANKNIFTY"],
            timeframe="15min",
            allocated_capital=300_000.0,
            source="mstock",
        )
        rid = mgr.add_runner(cfg, start=True, strategy=strat)
        runner = mgr.get_runner(rid)
        assert runner is not None

        # Pre-seed 20 historical 15min bars silently so MIN_WARMUP_BARS is satisfied
        hist = [
            {
                "ts": f"2026-10-22 {10 + (i // 4):02d}:{(i % 4) * 15:02d}:00",
                "open": 51000.0 + i * 10,
                "high": 51020.0 + i * 10,
                "low": 50990.0 + i * 10,
                "close": 51010.0 + i * 10,
                "volume": 100.0,
            }
            for i in range(20)
        ]
        runner.seed_history({"BANKNIFTY": hist})
        assert strat.eval_calls == 0

        # Feed 15 live 1min bars (09:15 .. 09:29) stamped with _timeframe='1min'
        for m in range(15, 30):
            mgr._on_bar(
                "BANKNIFTY",
                {
                    "ts": f"2026-10-23 09:{m:02d}:00",
                    "open": 51500.0,
                    "high": 51550.0,
                    "low": 51480.0,
                    "close": 51500.0 + m,
                    "volume": 10.0,
                    "_source": "mstock",
                    "_timeframe": "1min",
                },
            )
        # MTM updated on every minute, but ZERO premature 15min candles processed!
        assert runner.last_price["BANKNIFTY"] == 51529.0
        assert runner.bars_processed == 0
        assert strat.eval_calls == 0

        # Now 09:30 arrives -> seals 09:15..09:29 and triggers strategy ONCE!
        mgr._on_bar(
            "BANKNIFTY",
            {
                "ts": "2026-10-23 09:30:00",
                "open": 51530.0,
                "high": 51540.0,
                "low": 51525.0,
                "close": 51535.0,
                "volume": 12.0,
                "_source": "mstock",
                "_timeframe": "1min",
            },
        )
        assert runner.bars_processed == 1
        assert strat.eval_calls == 1
        assert runner._last_bar_ts["BANKNIFTY"] == "2026-10-23 09:15:00"
        # SessionBarRecorder also captured all 16 minute bars for future runners
        assert len(mgr.session_bar_recorder.get_bars("mstock", "BANKNIFTY")) == 16

    def test_pool_runner_skips_on_tick_end_until_coarse_candle_seals(self) -> None:
        mgr = PortfolioManager(auto_start_feed=False, enable_broker_warmup=False)
        spy = _PoolSpyStrategy(sma_slow=20)
        cfg = RunnerConfig(
            name="SwingPool-15m",
            strategy_name="sma_crossover",
            symbols=["RELIANCE", "TCS"],
            timeframe="15min",
            target_type=TARGET_POOL,
            allocated_capital=500_000.0,
            source="mstock",
        )
        rid = mgr.add_runner(cfg, start=True, strategy=spy)
        runner = mgr.get_runner(rid)
        assert runner is not None

        hist = [
            {
                "ts": f"2026-10-22 {10 + (i // 4):02d}:{(i % 4) * 15:02d}:00",
                "open": 1000.0 + i,
                "high": 1005.0 + i,
                "low": 995.0 + i,
                "close": 1002.0 + i,
                "volume": 100.0,
            }
            for i in range(15)
        ]
        runner.seed_history({"RELIANCE": hist, "TCS": hist})

        # Unsealed 09:15 minute + _on_tick_end -> pool MUST NOT evaluate
        for sym in ("RELIANCE", "TCS"):
            mgr._on_bar(
                sym,
                {
                    "ts": "2026-10-23 09:15:00",
                    "open": 1050.0,
                    "high": 1055.0,
                    "low": 1048.0,
                    "close": 1052.0,
                    "volume": 50.0,
                    "_source": "mstock",
                    "_timeframe": "1min",
                },
            )
        mgr._on_tick_end("2026-10-23 09:15:00")
        assert spy.eval_count == 0

        # 09:30 seals 09:15 15min bar -> _on_tick_end runs pool scan once per symbol
        for sym in ("RELIANCE", "TCS"):
            mgr._on_bar(
                sym,
                {
                    "ts": "2026-10-23 09:30:00",
                    "open": 1060.0,
                    "high": 1065.0,
                    "low": 1058.0,
                    "close": 1062.0,
                    "volume": 60.0,
                    "_source": "mstock",
                    "_timeframe": "1min",
                },
            )
        mgr._on_tick_end("2026-10-23 09:30:00")
        assert spy.eval_count == 2

    def test_mid_session_warmup_stitches_db_and_primes_partial_bucket(self) -> None:
        # Simulate spawning at 13:05 IST on 2026-10-23 (07:35 UTC)
        fixed_now = datetime(2026, 10, 23, 7, 35, 0, tzinfo=timezone.utc)
        fake_db = MagicMock()
        # Historical 15min rows across previous sessions (>= 48 quorum for target=60)
        db_idx = pd.date_range("2026-10-20 09:15:00", periods=60, freq="15min")
        db_df = pd.DataFrame(
            {
                "open": 2900.0,
                "high": 2905.0,
                "low": 2895.0,
                "close": 2902.0,
                "volume": 100.0,
            },
            index=db_idx,
        )
        fake_db.get_candles_bulk.return_value = {"RELIANCE": db_df}

        fake_client = MagicMock()
        fake_client.emits_minute_bars = True
        # Today's session 12:45..13:04 (12:45..12:59 seals 12:45; 13:00..13:04 is partial!)
        intraday_bars: List[Dict[str, Any]] = []
        for m in range(45, 60):
            intraday_bars.append(
                {
                    "ts": f"2026-10-23 12:{m:02d}:00",
                    "open": 2910.0,
                    "high": 2915.0,
                    "low": 2908.0,
                    "close": 2912.0,
                    "volume": 20.0,
                }
            )
        for m in range(0, 5):
            intraday_bars.append(
                {
                    "ts": f"2026-10-23 13:{m:02d}:00",
                    "open": 2920.0 if m == 0 else 2922.0,
                    "high": 2940.0 if m == 2 else 2925.0,
                    "low": 2918.0,
                    "close": 2924.0,
                    "volume": 100.0,
                }
            )
        fake_client.fetch_intraday_bars.return_value = intraday_bars

        recorder = SessionBarRecorder()
        ws = WarmupService(
            db_source=fake_db,
            session_recorder=recorder,
            now_fn=lambda: fixed_now,
            sleep_fn=lambda _: None,
        )
        mgr = PortfolioManager(
            auto_start_feed=False,
            mstock_feed_client=fake_client,
            warmup_service=ws,
            enable_broker_warmup=True,
        )
        cfg = RunnerConfig(
            name="MidSessionReliance",
            strategy_name="sma_crossover",
            symbols=["RELIANCE"],
            timeframe="15min",
            target_type=TARGET_SINGLE,
            allocated_capital=250_000.0,
            source="mstock",
        )
        rid = mgr.add_runner(cfg, start=True)
        runner = mgr.get_runner(rid)
        assert runner is not None
        assert fake_db.get_candles_bulk.call_count == 1
        assert runner.warmup_status == "READY"
        assert runner.warmup_reason is None
        # Last seeded bar is 12:45 (unsealed 13:00..13:04 held in bar_aggregator!)
        assert runner._last_bar_ts["RELIANCE"] == "2026-10-23 12:45:00"

        # Feed remaining 13:05..13:14 live bars + 13:15 sealer
        for m in range(5, 16):
            mgr._on_bar(
                "RELIANCE",
                {
                    "ts": f"2026-10-23 13:{m:02d}:00",
                    "open": 2925.0,
                    "high": 2930.0,
                    "low": 2921.0,
                    "close": 2928.0,
                    "volume": 100.0,
                    "_source": "mstock",
                    "_timeframe": "1min",
                },
            )
        # Sealed 13:00 bar preserved 13:00 open (2920.0), 13:02 high (2940.0), and 15x100 volume!
        last_bar = runner._bars["RELIANCE"][-1]
        assert last_bar["ts"] == "2026-10-23 13:00:00"
        assert last_bar["open"] == 2920.0
        assert last_bar["high"] == 2940.0
        assert last_bar["low"] == 2918.0
        assert last_bar["volume"] == 1500.0

    def test_db_timeout_intraday_only_marks_ema200_degraded_and_blocks_unconverged_entry(
        self,
    ) -> None:
        """Regression for live DB audit: QueryCanceled + 1 session of 1min bars (~25 15m bars)
        must NEVER stamp READY or fire EMA(200) entries (target=600, quorum=480)."""
        fixed_now = datetime(2026, 10, 23, 9, 55, 0, tzinfo=timezone.utc)  # 15:25 IST
        fake_db = MagicMock()
        fake_db.get_candles_bulk.side_effect = RuntimeError("QueryCanceled: statement timeout")

        # 360 1min bars from 09:15..15:14 -> 24 sealed 15min bars (>= 12, but << 480 quorum!)
        start_dt = pd.Timestamp("2026-10-23 09:15:00")
        intraday_bars = [
            {
                "ts": (start_dt + pd.Timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S"),
                "open": 51000.0,
                "high": 51010.0,
                "low": 50990.0,
                "close": 51005.0,
                "volume": 50.0,
            }
            for i in range(360)
        ]
        fake_client = MagicMock()
        fake_client.emits_minute_bars = True
        fake_client.fetch_intraday_bars.return_value = intraday_bars

        ws = WarmupService(
            db_source=fake_db,
            session_recorder=SessionBarRecorder(),
            now_fn=lambda: fixed_now,
            sleep_fn=lambda _: None,
        )
        mgr = PortfolioManager(
            auto_start_feed=False,
            mstock_feed_client=fake_client,
            warmup_service=ws,
            enable_broker_warmup=True,
        )
        strat = _AlwaysBuyStrategy(ema_fast=50, ema_slow=200)
        cfg = RunnerConfig(
            name="BNF-EMA200",
            strategy_name="sma_crossover",
            symbols=["BANKNIFTY"],
            timeframe="15min",
            allocated_capital=300_000.0,
            source="mstock",
        )
        rid = mgr.add_runner(cfg, start=True, strategy=strat)
        runner = mgr.get_runner(rid)
        assert runner is not None
        assert fake_db.get_candles_bulk.call_count == 1
        state = runner.get_state()
        assert state["warmup_status"] == "DEGRADED"
        assert state["warmup_target_bars"] == 600
        assert state["warmup_quorum_bars"] == 480
        assert state["warmup_min_symbol_bars"] == 24
        assert state["warmup_degraded_symbols"] == ["BANKNIFTY"]
        assert "QueryCanceled" in (state["warmup_reason"] or "")
        assert "24/600" in (state["warmup_reason"] or "")

        # Feed 15:15 and 15:30 to seal the 25th 15min bar: MTM marks, but EMA(200) MUST NOT fire!
        for hm in ("15:15:00", "15:30:00"):
            mgr._on_bar(
                "BANKNIFTY",
                {
                    "ts": f"2026-10-23 {hm}",
                    "open": 51100.0,
                    "high": 51120.0,
                    "low": 51090.0,
                    "close": 51115.0,
                    "volume": 100.0,
                    "_source": "mstock",
                    "_timeframe": "1min",
                },
            )
        assert runner.last_price["BANKNIFTY"] == 51115.0
        assert runner.bars_processed == 1
        assert strat.eval_calls == 0

    def test_non_auto_start_feed_async_warmup_returns_immediately(self) -> None:
        """Ensure async_warmup runs off the request thread even when auto_start_feed=False."""
        fake_db = MagicMock()

        def _slow_bulk(*args: Any, **kwargs: Any) -> Dict[str, pd.DataFrame]:
            time.sleep(0.18)
            idx = pd.date_range("2026-10-20 09:15:00", periods=60, freq="15min")
            return {
                "RELIANCE": pd.DataFrame(
                    {
                        "open": 2900.0,
                        "high": 2905.0,
                        "low": 2895.0,
                        "close": 2902.0,
                        "volume": 100.0,
                    },
                    index=idx,
                )
            }

        fake_db.get_candles_bulk.side_effect = _slow_bulk
        ws = WarmupService(
            db_source=fake_db,
            session_recorder=SessionBarRecorder(),
            now_fn=lambda: datetime(2026, 10, 23, 3, 30, 0, tzinfo=timezone.utc),
            sleep_fn=lambda _: None,
        )
        mgr = PortfolioManager(
            auto_start_feed=False,
            warmup_service=ws,
            enable_broker_warmup=True,
            async_warmup=True,
        )
        cfg = RunnerConfig(
            name="AsyncSpawnReliance",
            strategy_name="sma_crossover",
            symbols=["RELIANCE"],
            timeframe="15min",
            allocated_capital=200_000.0,
            source="mstock",
        )
        t0 = time.monotonic()
        rid = mgr.add_runner(cfg, start=True)
        elapsed_spawn = time.monotonic() - t0
        runner = mgr.get_runner(rid)
        assert runner is not None
        assert elapsed_spawn < 0.08
        assert runner.warmup_status in ("WARMING_UP", "READY")

        deadline = time.monotonic() + 1.5
        while runner.warmup_status == "WARMING_UP" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert runner.warmup_status == "READY"
        assert runner.warmup_bars_seeded == 60

    def test_ui_runner_card_and_deep_dive_surface_warmup_degraded(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        portfolio_js = (repo / "src/backtest/web/static/js/portfolio.js").read_text()
        deep_dive_js = (repo / "src/backtest/web/static/js/deep_dive.js").read_text()
        assert "warmupSubHtml" in portfolio_js
        assert "badge-warmup-degraded" in portfolio_js
        assert "warmup_reason" in portfolio_js
        assert "WARMUP DEGRADED" in deep_dive_js

    def test_duplicate_runner_warning_and_strict_reject_mode(self) -> None:
        mgr_warn = PortfolioManager(auto_start_feed=False, enable_broker_warmup=False)
        cfg1 = RunnerConfig(
            name="DupA",
            strategy_name="sma_crossover",
            symbols=["NIFTY"],
            timeframe="5min",
            allocated_capital=100_000.0,
            source="mstock",
        )
        cfg2 = RunnerConfig(
            name="DupB",
            strategy_name="sma_crossover",
            symbols=["NIFTY"],
            timeframe="5min",
            allocated_capital=100_000.0,
            source="mstock",
        )
        r1 = mgr_warn.add_runner(cfg1, start=True)
        r2 = mgr_warn.add_runner(cfg2, start=True)
        runner2 = mgr_warn.get_runner(r2)
        assert runner2 is not None
        assert runner2.duplicate_of == r1
        assert runner2.get_state()["duplicate_of"] == r1

        mgr_strict = PortfolioManager(
            auto_start_feed=False,
            enable_broker_warmup=False,
            reject_duplicate_runners=True,
        )
        mgr_strict.add_runner(cfg1, start=True)
        with pytest.raises(ValueError, match="duplicate active runner"):
            mgr_strict.add_runner(cfg2, start=True)
