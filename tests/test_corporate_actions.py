"""Corporate-action adjustment policy + split-suspect gate (review §3.3, 12c).

The landmine being pinned: mStock TypeA daily history is RAW — one unadjusted
10:1 split is a −90% "drawdown" that poisons every metric. These tests pin:

* factor arithmetic (split / bonus / dividend) and back-adjustment semantics
  (pre-ex-date prices scale, latest bars stay traded, volume inverts);
* the ±40% split-suspect gate with known-vs-unexplained annotation;
* read-time-only application (storage frames are never mutated);
* CSV / rows / DB loaders, config policy parsing (default OFF), and the
  SourceRegistry wiring (enabled → AdjustedSource, disabled → plain DbSource).
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from backtest.data.corporate_actions import (
    AdjustedSource,
    CorporateAction,
    CorporateActionCalendar,
    bonus_action,
    calendar_from_config,
    calendar_from_csv,
    calendar_from_db,
    calendar_from_rows,
    daily_return_outliers,
    dividend_action,
    split_action,
)
from backtest.data.frame_source import FrameSource
from backtest.data.source_registry import SourceRegistry


def _frame(dates, closes, volumes=None, base=None):
    base = base or 1.0
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates])
    n = len(closes)
    data = {
        "open": [c * base for c in closes],
        "high": [c * base * 1.02 for c in closes],
        "low": [c * base * 0.98 for c in closes],
        "close": list(closes),
        "volume": list(volumes) if volumes else [1000] * n,
    }
    return pd.DataFrame(data, index=idx)


# ---------------------------------------------------------------------------
# Factor arithmetic
# ---------------------------------------------------------------------------


class TestFactors:
    def test_split_face_arithmetic(self):
        assert split_action("RELIANCE", date(2024, 9, 5), 10, 1).adjustment_factor == 0.1
        assert split_action("X", date(2024, 9, 5), 10, 5).adjustment_factor == 0.5

    def test_bonus_arithmetic(self):
        assert bonus_action("X", date(2024, 9, 5), 1, 1).adjustment_factor == 0.5
        assert bonus_action("X", date(2024, 9, 5), 1, 2).adjustment_factor == pytest.approx(2 / 3)

    def test_dividend_is_price_neutral_by_default(self):
        action = dividend_action("X", date(2024, 9, 5), 12.5)
        assert action.adjustment_factor == 1.0
        assert action.amount == 12.5

    def test_invalid_inputs_rejected(self):
        with pytest.raises(ValueError, match="face values"):
            split_action("X", date(2024, 9, 5), 0, 1)
        with pytest.raises(ValueError, match="factor must be > 0"):
            CorporateAction("X", date(2024, 9, 5), "split", 0.0)
        with pytest.raises(ValueError, match="kind must be one of"):
            CorporateAction("X", date(2024, 9, 5), "merger", 0.5)
        with pytest.raises(ValueError, match="symbol"):
            CorporateAction("  ", date(2024, 9, 5), "split", 0.5)

    def test_symbol_normalised_and_datetime_date(self):
        action = CorporateAction(" aapl ", date(2024, 9, 5), "split", 0.5)
        assert action.symbol == "AAPL"


# ---------------------------------------------------------------------------
# Back-adjustment semantics
# ---------------------------------------------------------------------------


class TestAdjustCandles:
    def test_ten_to_one_split_latest_raw(self):
        cal = CorporateActionCalendar().add(split_action("ABC", date(2026, 1, 5), 10, 1))
        dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(8)]
        df = _frame(dates, [2500] * 4 + [250] * 4, volumes=[100] * 8)

        out = cal.adjust_candles(df, "ABC")

        assert float(out["close"].iloc[0]) == pytest.approx(250.0)  # pre-ex × 0.1
        assert float(out["volume"].iloc[0]) == pytest.approx(1000.0)  # volume ÷ 0.1
        assert float(out["close"].iloc[-1]) == pytest.approx(250.0)  # latest raw
        assert float(out["close"].iloc[3]) == pytest.approx(250.0)  # last pre-ex bar
        # the raw input frame is untouched — storage stays raw
        assert float(df["close"].iloc[0]) == 2500.0
        assert float(df["volume"].iloc[0]) == 100.0

    def test_ex_date_bar_itself_is_raw(self):
        cal = CorporateActionCalendar().add(split_action("ABC", date(2026, 1, 5), 10, 1))
        dates = [date(2026, 1, 3), date(2026, 1, 5), date(2026, 1, 6)]
        df = _frame(dates, [100.0, 10.0, 10.2])
        out = cal.adjust_candles(df, "ABC")
        assert float(out["close"].iloc[0]) == pytest.approx(10.0)
        assert float(out["close"].iloc[1]) == pytest.approx(10.0)  # ex-date: raw
        assert float(out["close"].iloc[2]) == pytest.approx(10.2)

    def test_ohlc_stays_consistent(self):
        cal = CorporateActionCalendar().add(split_action("ABC", date(2026, 1, 5), 10, 1))
        dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(6)]
        df = _frame(dates, [2500.0] * 6, base=1.0)
        out = cal.adjust_candles(df, "ABC")
        assert (out["high"] >= out["low"]).all()
        assert (out["high"] >= out["open"]).all()
        assert (out["low"] <= out["close"]).all()

    def test_multiple_actions_cumulate(self):
        cal = CorporateActionCalendar()
        cal.add(split_action("ABC", date(2026, 1, 3), 10, 1))  # ×0.1 before Jan 3
        cal.add(bonus_action("ABC", date(2026, 1, 6), 1, 1))  # ×0.5 before Jan 6
        dates = [date(2026, 1, 1), date(2026, 1, 4), date(2026, 1, 7)]
        df = _frame(dates, [1000.0, 100.0, 50.0])
        out = cal.adjust_candles(df, "ABC")
        assert float(out["close"].iloc[0]) == pytest.approx(1000.0 * 0.1 * 0.5)
        assert float(out["close"].iloc[1]) == pytest.approx(100.0 * 0.5)
        assert float(out["close"].iloc[2]) == pytest.approx(50.0)

    def test_no_actions_returns_input_untouched(self):
        cal = CorporateActionCalendar()
        df = _frame([date(2026, 1, 1), date(2026, 1, 2)], [10.0, 11.0])
        assert cal.adjust_candles(df, "ABC") is df

    def test_dividend_only_adjusts_nothing(self):
        cal = CorporateActionCalendar().add(dividend_action("ABC", date(2026, 1, 4), 25.0))
        dates = [date(2026, 1, 1), date(2026, 1, 6)]
        df = _frame(dates, [100.0, 80.0])
        out = cal.adjust_candles(df, "ABC")
        assert float(out["close"].iloc[0]) == pytest.approx(100.0)

    def test_tz_aware_index(self):
        cal = CorporateActionCalendar().add(split_action("ABC", date(2026, 1, 5), 10, 1))
        tz = "Asia/Kolkata"
        idx = pd.DatetimeIndex(
            [pd.Timestamp("2026-01-02 09:15", tz=tz), pd.Timestamp("2026-01-06 09:15", tz=tz)]
        )
        df = pd.DataFrame(
            {"open": [2500.0, 250.0], "high": [2510.0, 255.0], "low": [2490.0, 248.0],
             "close": [2500.0, 250.0], "volume": [100.0, 1000.0]},
            index=idx,
        )
        out = cal.adjust_candles(df, "ABC")
        assert float(out["close"].iloc[0]) == pytest.approx(250.0)
        assert float(out["close"].iloc[1]) == pytest.approx(250.0)

    def test_factor_for_scalar(self):
        cal = CorporateActionCalendar().add(split_action("ABC", date(2026, 1, 5), 10, 1))
        assert cal.factor_for("ABC", date(2026, 1, 1)) == 0.1
        assert cal.factor_for("ABC", date(2026, 1, 5)) == 1.0
        from datetime import datetime as dt

        assert cal.factor_for("ABC", dt(2026, 1, 4, 15, 30)) == 0.1


# ---------------------------------------------------------------------------
# The ±threshold gate
# ---------------------------------------------------------------------------


class TestSplitSuspects:
    def test_unadjusted_split_is_flagged_with_implied_factor(self):
        dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(5)]
        df = _frame(dates, [2500.0] * 4 + [250.0])
        suspects = daily_return_outliers(df, threshold_pct=40.0)
        assert len(suspects) == 1
        rec = suspects[0]
        assert rec["return_pct"] == pytest.approx(-90.0)
        assert rec["implied_factor"] == pytest.approx(0.1)

    def test_threshold_boundary(self):
        dates = [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3), date(2026, 1, 4)]
        df = _frame(dates, [100.0, 139.0, 141.0, 141.0])
        assert daily_return_outliers(df, threshold_pct=40.0) == []

    def test_short_frame_is_silent(self):
        assert daily_return_outliers(_frame([date(2026, 1, 1)], [10.0])) == []
        assert daily_return_outliers(None) == []

    def test_calendar_annotates_known_vs_unexplained(self):
        cal = CorporateActionCalendar().add(split_action("ABC", date(2026, 1, 5), 10, 1))
        dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(5)]
        df = _frame(dates, [2500.0] * 4 + [250.0])
        suspects = cal.suspects(df, "ABC", 40.0)
        assert suspects[0]["known_action"] is True
        assert "split" in suspects[0]["nearby_action"]

        dates2 = [date(2026, 3, 1) + timedelta(days=i) for i in range(5)]
        df2 = _frame(dates2, [500.0] * 4 + [100.0])  # -80% nowhere near an action
        suspects2 = cal.suspects(df2, "ABC", 40.0)
        assert suspects2[0]["known_action"] is False
        assert suspects2[0]["nearby_action"] is None


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


class TestLoaders:
    def test_rows_round_trip(self):
        cal = calendar_from_rows(
            [
                {
                    "symbol": "abc", "ex_date": "2026-01-05",
                    "kind": "split", "adjustment_factor": 0.1,
                },
                {"symbol": "XYZ", "ex_date": date(2026, 2, 1), "kind": "dividend", "amount": 12.5},
            ]
        )
        assert cal.symbols() == ["ABC", "XYZ"]
        assert cal.actions_for("ABC")[0].adjustment_factor == 0.1
        assert cal.actions_for("XYZ")[0].amount == 12.5

    def test_csv_round_trip(self, tmp_path):
        path = tmp_path / "actions.csv"
        path.write_text(
            "symbol,ex_date,kind,adjustment_factor,amount,note\n"
            "ABC,2026-01-05,split,0.1,,10:1 split\n"
            "XYZ,2026-02-01,dividend,1.0,12.5,interim\n"
        )
        cal = calendar_from_csv(path)
        assert len(cal) == 2
        assert cal.actions_for("ABC")[0].note == "10:1 split"

    def test_bad_row_raises_with_index(self):
        rows = [
            {"symbol": "ABC", "ex_date": "2026-01-05", "kind": "split", "adjustment_factor": 0.1},
            {"symbol": "BAD", "ex_date": "2026-01-06", "kind": "merger", "adjustment_factor": 1.0},
        ]
        with pytest.raises(ValueError, match="row 1"):
            calendar_from_rows(rows)

    def test_duplicate_event_rejected(self):
        cal = CorporateActionCalendar().add(split_action("ABC", date(2026, 1, 5), 10, 1))
        with pytest.raises(ValueError, match="duplicate"):
            cal.add(CorporateAction("ABC", date(2026, 1, 5), "split", 0.1))

    def test_db_round_trip(self, tmp_path):
        from backtest.db.manager import DatabaseManager
        from backtest.db.models import CorporateActionRecord

        manager = DatabaseManager.from_env(url=f"sqlite:///{tmp_path}/ca.db")
        CorporateActionRecord.ensure_schema(manager)
        with manager.session() as session:
            session.add(CorporateActionRecord(
                symbol="ABC", ex_date=date(2026, 1, 5), kind="split",
                factor=0.1, note="10:1",
            ))
            session.add(CorporateActionRecord(
                symbol="XYZ", ex_date=date(2026, 2, 1), kind="dividend",
                factor=1.0, amount=12.5,
            ))

        cal = calendar_from_db(manager)
        assert cal.symbols() == ["ABC", "XYZ"]
        assert cal.actions_for("ABC")[0].adjustment_factor == pytest.approx(0.1)
        # record → domain mapping
        with manager.session() as session:
            row = session.query(CorporateActionRecord).filter_by(symbol="ABC").one()
            assert row.to_action().symbol == "ABC"
            assert row.to_action().adjustment_factor == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# Policy config + AdjustedSource
# ---------------------------------------------------------------------------


class TestPolicyConfig:
    def _yaml(self, tmp_path, enabled, csv_name="actions.csv", with_csv=True):
        csv_path = tmp_path / csv_name
        if with_csv and enabled:
            csv_path.write_text(
                "symbol,ex_date,kind,adjustment_factor,amount,note\n"
                "ABC,2026-01-05,split,0.1,,10:1\n"
            )
        cfg = tmp_path / "data_quality.yaml"
        cfg.write_text(
            "daily_bar:\n"
            "  split_suspect_return_pct: 40.0\n"
            "  corporate_actions:\n"
            f"    enabled: {'true' if enabled else 'false'}\n"
            f"    actions_csv: {csv_path}\n"
        )
        return cfg

    def test_disabled_policy_is_none(self, tmp_path):
        assert calendar_from_config(self._yaml(tmp_path, enabled=False)) is None

    def test_missing_file_is_none(self, tmp_path):
        assert calendar_from_config(tmp_path / "nope.yaml") is None

    def test_enabled_policy_loads_csv(self, tmp_path):
        cal = calendar_from_config(self._yaml(tmp_path, enabled=True))
        assert cal is not None
        assert cal.actions_for("ABC")[0].adjustment_factor == pytest.approx(0.1)

    def test_enabled_but_csv_missing_is_none_with_warning(self, tmp_path, caplog):
        cal = calendar_from_config(self._yaml(tmp_path, enabled=True, with_csv=False))
        assert cal is None
        assert any("no actions file" in r.message for r in caplog.records)

    def test_inline_actions_supported(self, tmp_path):
        cfg = tmp_path / "data_quality.yaml"
        cfg.write_text(
            "daily_bar:\n"
            "  corporate_actions:\n"
            "    enabled: true\n"
            "    actions:\n"
            "      - symbol: ABC\n"
            "        ex_date: '2026-01-05'\n"
            "        kind: split\n"
            "        adjustment_factor: 0.1\n"
        )
        cal = calendar_from_config(cfg)
        assert cal is not None and len(cal) == 1


class TestAdjustedSource:
    def test_wraps_and_adjusts_without_mutating_inner(self, tmp_path):
        dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(6)]
        raw = _frame(dates, [2500.0] * 3 + [250.0] * 3)
        inner = FrameSource(raw)
        cal = CorporateActionCalendar().add(split_action("ABC", date(2026, 1, 4), 10, 1))
        src = AdjustedSource(inner, cal)
        out = src.get_candles("ABC", "", "")
        assert float(out["close"].iloc[0]) == pytest.approx(250.0)
        assert float(out["close"].iloc[-1]) == pytest.approx(250.0)
        assert float(inner._candles["close"].iloc[0]) == 2500.0  # storage raw

    def test_empty_calendar_is_passthrough(self):
        raw = _frame([date(2026, 1, 1), date(2026, 1, 2)], [10.0, 10.5])
        src = AdjustedSource(FrameSource(raw), CorporateActionCalendar())
        out = src.get_candles("ABC", "", "")
        assert float(out["close"].iloc[0]) == 10.0

    def test_unexplained_suspect_warns(self, caplog):
        dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(5)]
        raw = _frame(dates, [2500.0] * 4 + [250.0])
        src = AdjustedSource(FrameSource(raw), CorporateActionCalendar())
        src.get_candles("ABC", "", "")
        assert any(
            r.levelname == "WARNING" and "NO known corporate action" in r.message
            for r in caplog.records
        )

    def test_known_suspect_does_not_warn(self, caplog):
        dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(5)]
        raw = _frame(dates, [2500.0] * 4 + [250.0])
        cal = CorporateActionCalendar().add(split_action("ABC", date(2026, 1, 5), 10, 1))
        src = AdjustedSource(FrameSource(raw), cal)
        src.get_candles("ABC", "", "")
        assert not any(r.levelname == "WARNING" for r in caplog.records)


# ---------------------------------------------------------------------------
# Registry wiring (the single place sources are built)
# ---------------------------------------------------------------------------


class TestRegistryWiring:
    def test_disabled_policy_returns_plain_dbsource(self, tmp_path):
        import backtest.data.corporate_actions as ca_mod

        cfg = tmp_path / "dq.yaml"
        cfg.write_text("daily_bar:\n  corporate_actions:\n    enabled: false\n")
        original = ca_mod.DEFAULT_POLICY_PATH
        ca_mod.DEFAULT_POLICY_PATH = cfg
        try:
            source = SourceRegistry().get_source("backtest")
            assert type(source).__name__ == "DbSource"
        finally:
            ca_mod.DEFAULT_POLICY_PATH = original

    def test_enabled_policy_wraps_backtest_source(self, tmp_path):
        import backtest.data.corporate_actions as ca_mod

        actions = tmp_path / "actions.csv"
        actions.write_text(
            "symbol,ex_date,kind,adjustment_factor,amount,note\n"
            "ABC,2026-01-05,split,0.1,,\n"
        )
        cfg = tmp_path / "dq.yaml"
        cfg.write_text(
            "daily_bar:\n  corporate_actions:\n    enabled: true\n"
            f"    actions_csv: {actions}\n"
        )
        original = ca_mod.DEFAULT_POLICY_PATH
        ca_mod.DEFAULT_POLICY_PATH = cfg
        try:
            source = SourceRegistry().get_source("backtest")
            assert isinstance(source, AdjustedSource)
            assert source.calendar.actions_for("ABC")[0].adjustment_factor == pytest.approx(0.1)
        finally:
            ca_mod.DEFAULT_POLICY_PATH = original

    def test_paper_synthetic_paths_unaffected(self):
        source = SourceRegistry().get_source("paper", "synthetic")
        assert type(source).__name__ == "SyntheticSource"
