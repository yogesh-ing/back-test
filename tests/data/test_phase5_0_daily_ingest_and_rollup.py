"""Phase 5.0 verification suite — 1day equity ingest, 1min rollup & day-by-day audit.

Covers the three hard production constraints for Phase 5.0:
1. TimescaleDB hypertable chunk safety: strictly ``ON CONFLICT DO NOTHING``
   (zero ``UPDATE`` SQL statements ever emitted against ``market_data_cache``).
2. Pilot-first rate & DB pool protection: ``BulkIngestGatedError`` blocks >10
   symbols or >120 calendar days unless ``allow_bulk=True``; 502-storm circuit
   breaker + checkpoint JSON halts cleanly without pretending completion.
3. Day-by-day verification: silent-``None``/empty HTTP 200 responses, thin
   ``1min`` sessions (<338 bars), and single missing trading days fail
   ``verify_daily_coverage`` with exact missing ``YYYY-MM-DD`` dates while
   respecting weekends and NSE holidays (e.g. 2026-10-02, 2026-10-20).
4. End-to-end ``DbSource.get_candles_bulk(..., interval="1day")`` +
   ``WarmupService.warmup_runner`` compatibility for ``TARGET_POOL`` 1day swing
   runners.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, event, text

from backtest.data.daily_bar_ingest import (
    DEFAULT_MIN_INTRADAY_BARS,
    IST_ZONE,
    PILOT_MAX_CALENDAR_DAYS,
    PILOT_MAX_SYMBOLS,
    BulkIngestGatedError,
    daily_session_open_utc,
    expected_nse_trading_days,
    ingest_mstock_daily,
    persist_daily_bars,
    resolve_daily_session_date,
    rollup_1min_to_1day,
    verify_daily_coverage,
)
from backtest.data.db_source import DbSource
from backtest.db.models import MarketDataCache, MarketHoliday
from backtest.forward.paper_runner import OrderLedger, RunnerConfig, StrategyRunner
from backtest.forward.warmup import WarmupService

ROOT = Path(__file__).resolve().parents[2]
CLI_PATH = ROOT / "tools" / "daily_equity_ingest.py"

# Fixed reference clock: Saturday 2026-10-24 12:00 UTC (17:30 IST — all prior week sealed)
FIXED_NOW_UTC = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)


def _load_cli_module() -> Any:
    spec = importlib.util.spec_from_file_location("daily_equity_ingest_cli", CLI_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mdc_engine(tmp_path):
    """Isolated SQLite engine with MarketDataCache + MarketHoliday schema & SQL spy."""
    db_file = tmp_path / "mdc_phase5_0.sqlite"
    eng = create_engine(f"sqlite:///{db_file}")
    MarketDataCache.__table__.create(eng)
    MarketHoliday.ensure_schema(eng, seed=True)

    executed_sql: list[str] = []

    @event.listens_for(eng, "before_cursor_execute")
    def _spy_sql(conn, cursor, statement, parameters, context, executemany):
        executed_sql.append(str(statement))

    eng.executed_sql = executed_sql  # type: ignore[attr-defined]
    yield eng
    eng.dispose()


def _seed_1min_session(
    engine: Any,
    symbol: str,
    sess_date: date,
    *,
    bars: int = 375,
    base_price: float = 1000.0,
    exchange: str = "NSE",
    add_off_hours_noise: bool = False,
) -> None:
    """Insert ``bars`` regular 1-minute candles (09:15..15:29 IST) into market_data_cache."""
    start_ist = datetime(
        sess_date.year, sess_date.month, sess_date.day, 9, 15, tzinfo=IST_ZONE
    )
    rows: list[dict[str, Any]] = []
    now_iso = FIXED_NOW_UTC.isoformat()

    if add_off_hours_noise:
        # Pre-open 09:05 & post-close 15:35 stray ticks — rollup MUST ignore them!
        for noisy_dt, bad_high, bad_low in (
            (start_ist - timedelta(minutes=10), base_price + 999.0, base_price - 500.0),
            (start_ist + timedelta(minutes=380), base_price + 888.0, base_price - 400.0),
        ):
            rows.append(
                {
                    "symbol": symbol,
                    "exchange": exchange,
                    "timeframe": "1min",
                    "ts": noisy_dt.astimezone(timezone.utc).isoformat(),
                    "open": base_price,
                    "high": bad_high,
                    "low": bad_low,
                    "close": base_price,
                    "volume": 99_999.0,
                    "source": "mstock",
                    "ingested_at": now_iso,
                }
            )

    for i in range(bars):
        dt_utc = (start_ist + timedelta(minutes=i)).astimezone(timezone.utc)
        px_open = base_price + float(i) * 0.10
        px_close = px_open + 0.25
        px_high = px_close + 0.50
        px_low = px_open - 0.50
        rows.append(
            {
                "symbol": symbol,
                "exchange": exchange,
                "timeframe": "1min",
                "ts": dt_utc.isoformat(),
                "open": round(px_open, 2),
                "high": round(px_high, 2),
                "low": round(px_low, 2),
                "close": round(px_close, 2),
                "volume": 100.0,
                "source": "mstock",
                "ingested_at": now_iso,
            }
        )

    sql = text(
        """
        INSERT INTO market_data_cache
            (symbol, exchange, timeframe, ts, open, high, low, close, volume, source, ingested_at)
        VALUES
            (:symbol, :exchange, :timeframe, :ts, :open, :high, :low, :close, :volume,
             :source, :ingested_at)
        """
    )
    with engine.begin() as conn:
        conn.execute(sql, rows)


# ---------------------------------------------------------------------------
# 1. Canonical 1day Session Timestamp & Zero-UPDATE Hypertable Safety
# ---------------------------------------------------------------------------


class TestSessionClockAndHypertableSafety:
    def test_daily_session_open_utc_prevents_utc_midnight_day_shift(self):
        # mStock daily endpoint often stamps "2026-10-23T00:00:00+05:30"
        # Naive UTC conversion would land on 2026-10-22 18:30Z (WRONG day!).
        sess_d = resolve_daily_session_date("2026-10-23T00:00:00+05:30")
        assert sess_d == date(2026, 10, 23)

        ts_utc = daily_session_open_utc(sess_d)
        assert ts_utc == datetime(2026, 10, 23, 3, 45, 0, tzinfo=timezone.utc)
        # Same calendar date in BOTH UTC and IST!
        assert ts_utc.date() == date(2026, 10, 23)
        assert ts_utc.astimezone(IST_ZONE).strftime("%Y-%m-%d %H:%M:%S") == (
            "2026-10-23 09:15:00"
        )
        assert resolve_daily_session_date(ts_utc) == date(2026, 10, 23)
        assert resolve_daily_session_date("2026-10-23") == date(2026, 10, 23)

    def test_persist_daily_bars_is_insert_only_and_never_executes_update(
        self, mdc_engine
    ):
        mdc_engine.executed_sql.clear()
        bars = [
            {
                "ts": "2026-10-21T00:00:00+05:30",
                "open": 1400.0,
                "high": 1425.0,
                "low": 1395.0,
                "close": 1418.0,
                "volume": 500_000,
            },
            # Invalid OHLC (high < open) must be rejected before SQL
            {
                "ts": "2026-10-22T00:00:00+05:30",
                "open": 1400.0,
                "high": 1390.0,
                "low": 1380.0,
                "close": 1395.0,
                "volume": 1000,
            },
        ]
        first = persist_daily_bars(mdc_engine, "RELIANCE", bars, source="mstock")
        assert first.inserted == 1
        assert first.skipped_conflict == 0
        assert first.rejected_invalid_ohlc == 1

        # Re-inserting the same day with modified values MUST hit ON CONFLICT DO NOTHING
        bars_re = [
            {
                "ts": "2026-10-21",
                "open": 9999.0,
                "high": 9999.0,
                "low": 9999.0,
                "close": 9999.0,
                "volume": 1,
            }
        ]
        second = persist_daily_bars(mdc_engine, "RELIANCE", bars_re, source="mstock")
        assert second.inserted == 0
        assert second.skipped_conflict == 1

        # Assert ZERO SQL UPDATE statements were ever emitted (Timescale chunk safety)
        assert not any(
            "UPDATE" in stmt.upper() for stmt in mdc_engine.executed_sql
        ), mdc_engine.executed_sql

        # Explicit replace_window uses DELETE + INSERT (still zero UPDATE!)
        mdc_engine.executed_sql.clear()
        replaced = persist_daily_bars(
            mdc_engine,
            "RELIANCE",
            bars_re,
            source="mstock",
            replace_window=(date(2026, 10, 21), date(2026, 10, 21)),
        )
        assert replaced.deleted_for_replace == 1
        assert replaced.inserted == 1
        assert not any("UPDATE" in stmt.upper() for stmt in mdc_engine.executed_sql)


# ---------------------------------------------------------------------------
# 2. Local 1min -> 1day Rollup + Intraday Completeness Quorum
# ---------------------------------------------------------------------------


class TestIntradayToDailyRollup:
    def test_rollup_aggregates_regular_session_ignores_noise_and_skips_partial_days(
        self, mdc_engine
    ):
        # Week of Oct 19-23, 2026:
        # - 2026-10-19 (Mon): full 375 bars + off-hours noise -> rolled up cleanly
        # - 2026-10-20 (Tue): NSE Dussehra holiday (0 bars)
        # - 2026-10-21 (Wed): ONLY 80 bars (feed dropped mid-morning!) -> MUST skip!
        # - 2026-10-22 (Thu): full 375 bars -> rolled up cleanly
        # - 2026-10-23 (Fri): full 375 bars -> rolled up cleanly
        _seed_1min_session(
            mdc_engine,
            "TCS",
            date(2026, 10, 19),
            bars=375,
            base_price=3500.0,
            add_off_hours_noise=True,
        )
        _seed_1min_session(
            mdc_engine, "TCS", date(2026, 10, 21), bars=80, base_price=3520.0
        )
        _seed_1min_session(
            mdc_engine, "TCS", date(2026, 10, 22), bars=375, base_price=3550.0
        )
        _seed_1min_session(
            mdc_engine, "TCS", date(2026, 10, 23), bars=375, base_price=3580.0
        )

        mdc_engine.executed_sql.clear()
        rep = rollup_1min_to_1day(
            mdc_engine,
            ["TCS"],
            "2026-10-19",
            "2026-10-23",
            min_intraday_bars=DEFAULT_MIN_INTRADAY_BARS,
            now_utc=FIXED_NOW_UTC,
        )
        assert rep.total_inserted == 3
        sym_rep = rep.symbols[0]
        assert sym_rep.rolled_dates == ["2026-10-19", "2026-10-22", "2026-10-23"]
        # Partial 80-bar day explicitly quarantined, NOT fabricated into 1day!
        assert sym_rep.partial_intraday_days == {"2026-10-21": 80}
        assert not any("UPDATE" in s.upper() for s in mdc_engine.executed_sql)

        # Verify exact OHLCV math on 2026-10-19 (off-hours +999/-500 noise ignored!)
        src = DbSource.__new__(DbSource)
        src._engine = mdc_engine
        frames = src.get_candles_bulk(["TCS"], "2026-10-19", "2026-10-19", interval="1day")
        df_oct19 = frames["TCS"]
        assert len(df_oct19) == 1
        row = df_oct19.iloc[0]
        assert float(row["open"]) == pytest.approx(3500.0)
        expected_last_open = 3500.0 + 374 * 0.10
        assert float(row["close"]) == pytest.approx(expected_last_open + 0.25)
        assert float(row["high"]) == pytest.approx(expected_last_open + 0.75)
        assert float(row["low"]) == pytest.approx(3499.50)
        assert float(row["volume"]) == pytest.approx(375 * 100.0)

        # Day-by-day verifier MUST flag 2026-10-21 as missing while ignoring 2026-10-20 holiday!
        audit = verify_daily_coverage(
            mdc_engine,
            ["TCS"],
            "2026-10-19",
            "2026-10-23",
            now_utc=FIXED_NOW_UTC,
        )
        assert audit.expected_trading_dates == [
            "2026-10-19",
            "2026-10-21",
            "2026-10-22",
            "2026-10-23",
        ]
        assert audit.passed is False
        assert audit.symbols[0].missing_dates == ["2026-10-21"]

    def test_rollup_skips_unclosed_today_before_1535_ist(self, mdc_engine):
        # Mid-session Friday 2026-10-23 14:55 IST (09:25 UTC)
        mid_session_utc = datetime(2026, 10, 23, 9, 25, tzinfo=timezone.utc)
        _seed_1min_session(mdc_engine, "INFY", date(2026, 10, 22), bars=375, base_price=1800.0)
        _seed_1min_session(mdc_engine, "INFY", date(2026, 10, 23), bars=340, base_price=1810.0)

        rep = rollup_1min_to_1day(
            mdc_engine,
            ["INFY"],
            "2026-10-22",
            "2026-10-23",
            now_utc=mid_session_utc,
        )
        assert rep.symbols[0].rolled_dates == ["2026-10-22"]


# ---------------------------------------------------------------------------
# 3. Pilot Gate, 502 Storm Circuit Breaker, Coverage Skip & Anti-Silent-None
# ---------------------------------------------------------------------------


class TestPilotMStockIngestGuards:
    def test_pilot_gate_blocks_excess_symbols_or_multi_year_span_without_allow_bulk(
        self, mdc_engine
    ):
        eleven_syms = [f"SYM{i}" for i in range(PILOT_MAX_SYMBOLS + 1)]
        with pytest.raises(BulkIngestGatedError, match="symbols"):
            ingest_mstock_daily(
                mdc_engine,
                eleven_syms,
                "2026-10-01",
                "2026-10-23",
                now_utc=FIXED_NOW_UTC,
            )

        too_wide_start = (
            date(2026, 10, 23) - timedelta(days=PILOT_MAX_CALENDAR_DAYS + 5)
        ).isoformat()
        with pytest.raises(BulkIngestGatedError, match="calendar days"):
            ingest_mstock_daily(
                mdc_engine,
                ["RELIANCE"],
                too_wide_start,
                "2026-10-23",
                now_utc=FIXED_NOW_UTC,
            )

    def test_pilot_fetch_uses_end_exclusive_to_skips_covered_windows_and_verifies_100pct(
        self, mdc_engine, tmp_path
    ):
        calls: list[tuple[str, dict[str, str]]] = []
        exp_days = expected_nse_trading_days(
            "2026-10-01", "2026-10-23", engine=mdc_engine, now_utc=FIXED_NOW_UTC
        )
        # Note: 2026-10-02 (Gandhi Jayanti) & 2026-10-20 (Dussehra) excluded automatically!
        assert date(2026, 10, 2) not in exp_days
        assert date(2026, 10, 20) not in exp_days

        def fake_get(url: str, *, headers: dict, params: dict, timeout: int):
            calls.append((url, dict(params)))
            candles = [
                [
                    f"{d.isoformat()}T00:00:00+05:30",
                    2500.0,
                    2530.0,
                    2490.0,
                    2515.0,
                    120_000,
                ]
                for d in exp_days
            ]
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"status": "success", "data": {"candles": candles}},
            )

        ckpt = tmp_path / "pilot_ckpt.json"
        rep1 = ingest_mstock_daily(
            mdc_engine,
            ["RELIANCE", "HDFCBANK"],
            "2026-10-01",
            "2026-10-23",
            api_key="k",
            access_token="t",
            checkpoint_path=ckpt,
            http_get=fake_get,
            token_resolver=lambda sym: (f"TOK-{sym}", "NSE"),
            sleep_fn=lambda _s: None,
            now_utc=FIXED_NOW_UTC,
        )
        assert rep1.all_windows_clean is True
        assert rep1.http_requests == 2
        assert rep1.total_inserted == len(exp_days) * 2
        # Verify mStock end-exclusive `to` = 2026-10-24 (2026-10-23 + 1 day)
        assert calls[0][1] == {"from": "2026-10-01", "to": "2026-10-24"}
        assert ckpt.is_file()
        saved_ckpt = json.loads(ckpt.read_text(encoding="utf-8"))
        assert saved_ckpt["all_windows_clean"] is True

        # Second run over the same range MUST skip covered windows (0 HTTP calls!)
        calls.clear()
        rep2 = ingest_mstock_daily(
            mdc_engine,
            ["RELIANCE", "HDFCBANK"],
            "2026-10-01",
            "2026-10-23",
            api_key="k",
            access_token="t",
            http_get=fake_get,
            token_resolver=lambda sym: (f"TOK-{sym}", "NSE"),
            sleep_fn=lambda _s: None,
            now_utc=FIXED_NOW_UTC,
        )
        assert rep2.http_requests == 0
        assert len(calls) == 0

        audit = verify_daily_coverage(
            mdc_engine,
            ["RELIANCE", "HDFCBANK"],
            "2026-10-01",
            "2026-10-23",
            now_utc=FIXED_NOW_UTC,
        )
        assert audit.passed is True
        assert all(s.coverage_pct == 100.0 for s in audit.symbols)

    def test_502_storm_trips_circuit_breaker_and_never_claims_clean(
        self, mdc_engine, tmp_path
    ):
        def storm_502(url: str, *, headers: dict, params: dict, timeout: int):
            return SimpleNamespace(status_code=502, json=lambda: {})

        ckpt = tmp_path / "storm_ckpt.json"
        rep = ingest_mstock_daily(
            mdc_engine,
            ["RELIANCE", "TCS", "INFY", "SBIN"],
            "2026-10-01",
            "2026-10-23",
            api_key="k",
            access_token="t",
            max_retries_per_window=2,
            max_consecutive_5xx=2,
            checkpoint_path=ckpt,
            http_get=storm_502,
            token_resolver=lambda sym: ("1234", "NSE"),
            sleep_fn=lambda _s: None,
            now_utc=FIXED_NOW_UTC,
        )
        assert rep.circuit_breaker_tripped is True
        assert rep.all_windows_clean is False
        # Breaker tripped after 2 symbols (2 retries each = 4 HTTP calls);
        # INFY & SBIN skipped without hammering mStock further!
        assert rep.http_requests == 4
        assert "circuit breaker" in rep.symbols[2].error
        assert "circuit breaker" in rep.symbols[3].error

    def test_silent_empty_response_on_trading_days_is_flagged_not_ignored(
        self, mdc_engine
    ):
        def silent_empty(url: str, *, headers: dict, params: dict, timeout: int):
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"status": "success", "data": None},
            )

        rep = ingest_mstock_daily(
            mdc_engine,
            ["ICICIBANK"],
            "2026-10-19",
            "2026-10-23",
            api_key="k",
            access_token="t",
            http_get=silent_empty,
            token_resolver=lambda sym: ("4963", "NSE"),
            sleep_fn=lambda _s: None,
            now_utc=FIXED_NOW_UTC,
        )
        assert rep.all_windows_clean is False
        assert len(rep.symbols[0].silent_empty_windows) == 1
        assert rep.symbols[0].silent_empty_windows[0]["expected_days"] == 4


# ---------------------------------------------------------------------------
# 4. End-to-End TARGET_POOL 1day Swing Runner Warmup & CLI Exit Codes
# ---------------------------------------------------------------------------


class TestSwingPoolWarmupAndCli:
    def test_persisted_1day_bars_satisfy_pool_runner_warmup_in_single_db_query(
        self, mdc_engine
    ):
        pool_syms = ["RELIANCE", "TCS", "INFY"]
        trading_days = expected_nse_trading_days(
            "2026-07-01", "2026-10-23", engine=mdc_engine, now_utc=FIXED_NOW_UTC
        )
        assert len(trading_days) >= 65

        for sym_idx, sym in enumerate(pool_syms):
            bars = [
                {
                    "session_date": d,
                    "open": 1000.0 + sym_idx * 100 + i,
                    "high": 1010.0 + sym_idx * 100 + i,
                    "low": 995.0 + sym_idx * 100 + i,
                    "close": 1005.0 + sym_idx * 100 + i,
                    "volume": 250_000.0,
                }
                for i, d in enumerate(trading_days)
            ]
            persist_daily_bars(mdc_engine, sym, bars, source="rollup_1min")

        db_src = DbSource.__new__(DbSource)
        db_src._engine = mdc_engine
        svc = WarmupService(db_source=db_src, now_fn=lambda: FIXED_NOW_UTC)

        cfg = RunnerConfig(
            name="SWING-POOL-1D",
            strategy_name="sma_crossover",
            strategy_params={"fast": 5, "slow": 15},
            allocated_capital=500_000.0,
            symbols=pool_syms,
            target_type="SYMBOL_UNIVERSE",
            timeframe="1day",
            mode="paper",
            source="mstock",
        )
        runner = StrategyRunner(cfg, ledger=OrderLedger())
        report = svc.warmup_runner(runner)

        assert report.db_queries == 1
        assert report.intraday_http_calls == 0
        assert report.db_symbols_hit == 3
        assert runner.warmup_status == "READY"
        for sym in pool_syms:
            assert len(runner._bars[sym]) >= 45

    def test_cli_rollup_verify_and_pilot_gate_exit_codes(self, mdc_engine, tmp_path):
        cli = _load_cli_module()

        # Seed 3 of 4 expected days in Oct 19-23 (leave 2026-10-21 missing)
        for d in (date(2026, 10, 19), date(2026, 10, 22), date(2026, 10, 23)):
            _seed_1min_session(mdc_engine, "RELIANCE", d, bars=375, base_price=1400.0)

        out_json = tmp_path / "rollup_report.json"
        # Rollup --apply runs post-rollup day-by-day verify -> exits 1 because Oct 21 is missing!
        rc_gap = cli.main(
            [
                "rollup",
                "--symbols",
                "RELIANCE",
                "--from",
                "2026-10-19",
                "--to",
                "2026-10-23",
                "--apply",
                "--json-out",
                str(out_json),
            ],
            engine=mdc_engine,
            now_utc=FIXED_NOW_UTC,
        )
        assert rc_gap == 1
        payload = json.loads(out_json.read_text(encoding="utf-8"))
        assert payload["audit"]["passed"] is False
        assert payload["audit"]["symbols"][0]["missing_dates"] == ["2026-10-21"]

        # Seed the missing Oct 21 session -> nightly --apply now passes with exit code 0!
        _seed_1min_session(mdc_engine, "RELIANCE", date(2026, 10, 21), bars=375, base_price=1410.0)
        rc_ok = cli.main(
            [
                "nightly",
                "--symbols",
                "RELIANCE",
                "--from",
                "2026-10-19",
                "--to",
                "2026-10-23",
                "--apply",
            ],
            engine=mdc_engine,
            now_utc=FIXED_NOW_UTC,
        )
        assert rc_ok == 0

        # Standalone verify returns 0 once all 4 expected sessions exist
        rc_verify = cli.main(
            [
                "verify",
                "--symbols",
                "RELIANCE",
                "--from",
                "2026-10-19",
                "--to",
                "2026-10-23",
            ],
            engine=mdc_engine,
            now_utc=FIXED_NOW_UTC,
        )
        assert rc_verify == 0

        # Bulk NIFTY_50 (50 symbols > 10 pilot ceiling) without --allow-bulk returns exit code 3!
        rc_gated = cli.main(
            [
                "fetch",
                "--universe",
                "NIFTY_50",
                "--from",
                "2026-10-01",
                "--to",
                "2026-10-23",
                "--apply",
            ],
            engine=mdc_engine,
            now_utc=FIXED_NOW_UTC,
        )
        assert rc_gated == 3
