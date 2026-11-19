"""Tests for the model pricing feed (plan V3 Phase 1).

The hand-calc oracle is Appendix A recomputed with the corrected conventions:
lot 75 (broker master), tick-snapped quotes, per-band spread floor, VWAP
expiry settlement and exercise STT. Numbers were derived independently of
the implementation (textbook BS via math.erf + fee rates by hand):

    Entry 2026-06-18 15:30 IST, NIFTY 22000 CE, expiry 2026-06-25,
    IV 20% + 2.5 pts premium, r 6.5%, T = 7/365:
        mid 287.20  ask 287.90  bid 286.50
        buy 75 @ 287.90 -> turnover 21,592.50, fees 33.22
    Early exit 5 bars later (spot 22500, T = 2/365):
        mid 522.30  bid 521.00
        sell 75 @ 521.00 -> turnover 39,075.00, fees 98.46 (STT 58.61)
        net P&L +17,350.82
    Hold to expiry (VWAP settlement 22500, intrinsic 500):
        exercise charges on 37,500 -> 75.45 (STT 56.25)
        net P&L +15,798.83
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from backtest.options.model_pricing import (
    IV_WINDOWS,
    SOURCE_TAG_MODEL_BS,
    ModelBSFeed,
    ModelPricingConfig,
    OptionPricingFeed,
    compute_realized_vol,
    settlement_price,
)
from backtest.simulator import CommissionCalculator, IndiaEquityFees, TradeSegment
from backtest.timekeeping import EXCHANGE_TZ

D = Decimal
TICK = D("0.05")

ENTRY_BAR = datetime(2026, 6, 18, 15, 30, tzinfo=EXCHANGE_TZ)
EXIT_BAR = datetime(2026, 6, 23, 15, 30, tzinfo=EXCHANGE_TZ)  # T = 2/365
EXPIRY = date(2026, 6, 25)

ORACLE_CONFIG = ModelPricingConfig(
    iv_source="constant", iv_constant=0.20, iv_premium=0.025
)


def _feed(**kw) -> ModelBSFeed:
    return ModelBSFeed(config=ORACLE_CONFIG, **kw)


def _quote(feed, spot=22000.0, strike=22000.0, option_type="CE", bar=ENTRY_BAR):
    return feed.quote(
        underlying="NIFTY",
        strike=strike,
        expiry=EXPIRY,
        option_type=option_type,
        spot=spot,
        bar_time=bar,
    )


# ===========================================================================
# Black-Scholes core
# ===========================================================================


class TestBSPrices:
    def test_textbook_atm_one_year(self):
        # S=K=100, T=1, r=5%, vol=20%, no skew/premium: call 10.4506, put 5.5735.
        cfg = ModelPricingConfig(
            iv_source="constant", iv_constant=0.20, iv_premium=0.0,
            skew_pts=0.0, risk_free=0.05,
        )
        feed = ModelBSFeed(cfg)
        bar = datetime(2026, 6, 18, 15, 30, tzinfo=EXCHANGE_TZ)
        call = feed.quote(
            underlying="X", strike=100.0, expiry=date(2027, 6, 18),
            option_type="CE", spot=100.0, bar_time=bar,
        )
        put = feed.quote(
            underlying="X", strike=100.0, expiry=date(2027, 6, 18),
            option_type="PE", spot=100.0, bar_time=bar,
        )
        assert abs(float(call.mid) - 10.45) <= 0.05
        assert abs(float(put.mid) - 5.57) <= 0.05

    def test_at_expiry_collapses_to_intrinsic(self):
        q = _quote(_feed(), spot=22500.0, bar=datetime(2026, 6, 25, 15, 30, tzinfo=EXCHANGE_TZ))
        assert q.mid == D("500.00")
        assert q.delta == 1.0
        assert q.gamma == q.theta == q.vega == 0.0

    def test_otm_at_expiry_is_zero(self):
        q = _quote(_feed(), spot=21900.0, bar=datetime(2026, 6, 25, 15, 30, tzinfo=EXCHANGE_TZ))
        assert q.mid == q.bid == q.ask == D("0")

    def test_deep_itm_price_near_intrinsic_plus_carry(self):
        q = _quote(_feed(), spot=24000.0, strike=22000.0)
        assert float(q.mid) > 2000.0
        assert q.delta > 0.99

    def test_deep_otm_price_small_but_positive(self):
        q = _quote(_feed(), spot=22000.0, strike=24000.0)
        assert D("0") < q.mid < D("10")

    def test_zero_iv_gives_intrinsic(self):
        # base iv = -0.025 + premium 0.025 = 0 -> the iv<=0 branch.
        cfg = ModelPricingConfig(iv_source="realized_20", iv_premium=0.025)
        iv_bars = pd.Series([-0.025], index=[ENTRY_BAR])
        feed = ModelBSFeed(cfg, iv_bars=iv_bars)
        q = _quote(feed, spot=22500.0)
        assert q.mid == D("500.00")  # intrinsic, no time value


# ===========================================================================
# IV: premium, skew, realized-vol source
# ===========================================================================


class TestIVModel:
    def test_iv_premium_added_to_base(self):
        q = _quote(_feed())
        assert q.iv == pytest.approx(0.225)

    def test_put_skew_lifts_put_iv_only(self):
        feed = _feed()
        call = _quote(feed, option_type="CE")
        put = _quote(feed, option_type="PE")
        assert put.iv == pytest.approx(call.iv + 0.03)

    def test_realized_vol_series_drives_iv(self):
        cfg = ModelPricingConfig(iv_source="realized_20", iv_premium=0.025)
        iv_bars = pd.Series([0.18], index=[ENTRY_BAR])
        q = _quote(ModelBSFeed(cfg, iv_bars=iv_bars))
        assert q.iv == pytest.approx(0.18 + 0.025)

    def test_missing_iv_falls_back_to_constant(self):
        cfg = ModelPricingConfig(iv_source="realized_20", iv_premium=0.025)
        iv_bars = pd.Series([0.18], index=[EXIT_BAR])  # nothing at/before entry
        q = _quote(ModelBSFeed(cfg, iv_bars=iv_bars))
        assert q.iv == pytest.approx(0.15 + 0.025)  # default iv_constant 0.15

    def test_nan_iv_falls_back_to_constant(self):
        cfg = ModelPricingConfig(iv_source="realized_20", iv_premium=0.025)
        iv_bars = pd.Series([float("nan")], index=[ENTRY_BAR])
        q = _quote(ModelBSFeed(cfg, iv_bars=iv_bars))
        assert q.iv == pytest.approx(0.15 + 0.025)

    def test_constant_source_ignores_iv_bars(self):
        iv_bars = pd.Series([0.18], index=[ENTRY_BAR])
        q = _quote(_feed(iv_bars=iv_bars))  # ORACLE_CONFIG is constant 0.20
        assert q.iv == pytest.approx(0.225)

    def test_iv_source_vocabulary_validated(self):
        with pytest.raises(ValueError, match="iv_source"):
            ModelPricingConfig(iv_source="magic")
        assert IV_WINDOWS["realized_20"] == 20
        assert IV_WINDOWS["constant"] is None


class TestRealizedVol:
    def test_known_alternating_series(self):
        # Alternating +1%/-1% returns: the last window's std is analytic.
        prices = [100.0]
        for i in range(40):
            prices.append(prices[-1] * (1.01 if i % 2 == 0 else 1 / 1.01))
        closes = pd.Series(prices)
        rv = compute_realized_vol(closes, window=20, periods_per_year=252.0)
        rets = np.log(closes / closes.shift(1)).iloc[1:]
        expected = rets.iloc[-20:].std() * np.sqrt(252.0)
        assert rv.iloc[-1] == pytest.approx(expected)
        assert rv.iloc[-1] > 0.10  # ~1%/bar daily-scaled is sizeable

    def test_warmup_bars_are_nan(self):
        closes = pd.Series(np.linspace(100, 110, 30))
        rv = compute_realized_vol(closes, window=20)
        assert rv.iloc[:20].isna().all()
        assert not np.isnan(rv.iloc[-1])

    def test_annualization_uses_periods_per_year(self):
        closes = pd.Series(np.linspace(100, 110, 60))
        daily = compute_realized_vol(closes, window=20, periods_per_year=252.0)
        minute = compute_realized_vol(closes, window=20, periods_per_year=94500.0)
        ratio = minute.iloc[-1] / daily.iloc[-1]
        assert ratio == pytest.approx(np.sqrt(94500.0 / 252.0))

    def test_window_below_two_rejected(self):
        with pytest.raises(ValueError, match="window"):
            compute_realized_vol(pd.Series([1.0, 2.0]), window=1)


# ===========================================================================
# Spread: percentage vs per-band floor
# ===========================================================================


class TestSpread:
    def test_percentage_spread_when_above_floor(self):
        q = _quote(_feed())  # mid 287.20 -> 0.5% = 1.436 > 0.50 floor
        assert q.ask - q.bid == D("1.40")  # tick-snapped half-spreads
        assert q.ask > q.mid > q.bid

    def test_floor_binds_for_cheap_options(self):
        # 400 pts OTM with 1 day left: premium tens of rupees, pct spread
        # below the band floor.
        feed = ModelBSFeed(
            ModelPricingConfig(iv_source="constant", iv_constant=0.20, iv_premium=0.025)
        )
        q = feed.quote(
            underlying="NIFTY", strike=22400.0, expiry=EXPIRY, option_type="CE",
            spot=22000.0, bar_time=datetime(2026, 6, 24, 15, 30, tzinfo=EXCHANGE_TZ),
        )
        assert D("0") < q.mid < D("100")
        assert q.ask - q.bid >= D("2.00")  # band 8 -> beyond-max floor

    def test_band_floors_widen_with_distance(self):
        feed = _feed()
        atm = _quote(feed, strike=22000.0)
        b2 = _quote(feed, strike=22100.0)
        b3 = _quote(feed, strike=22150.0)
        b5 = _quote(feed, strike=22250.0)
        assert atm.ask - atm.bid == D("1.40")  # pct dominates ATM
        assert b2.ask - b2.bid >= D("1.00")
        assert b3.ask - b3.bid >= D("2.00")
        assert b5.ask - b5.bid >= D("2.00")  # beyond max band uses max floor

    def test_custom_bands_respected(self):
        cfg = ModelPricingConfig(
            iv_source="constant", iv_constant=0.20, iv_premium=0.025,
            spread_pct=0.0, spread_floor=0.50, spread_floor_bands={1: 1.00},
        )
        feed = ModelBSFeed(cfg)
        q = _quote(feed, strike=22050.0)
        assert q.ask - q.bid == D("1.00")


# ===========================================================================
# VWAP settlement
# ===========================================================================


class TestSettlement:
    def test_last_30_bars_only(self):
        # 40 bars: first 10 at 22000, last 30 at 22500 -> window excludes the 10.
        closes = pd.Series([22000.0] * 10 + [22500.0] * 30)
        assert settlement_price(closes) == pytest.approx(22500.0)

    def test_fewer_than_30_uses_all(self):
        closes = pd.Series([22000.0] * 5 + [22600.0] * 10)
        assert settlement_price(closes) == pytest.approx((22000 * 5 + 22600 * 10) / 15)

    def test_fewer_than_5_uses_close(self):
        closes = pd.Series([22000.0, 22100.0, 22900.0])
        assert settlement_price(closes) == pytest.approx(22900.0)

    def test_volume_weighting_when_volume_present(self):
        idx = pd.date_range("2026-06-25 15:00", periods=30, freq="min")
        closes = pd.Series([22400.0] * 29 + [22600.0], index=idx)
        volumes = pd.Series([1.0] * 29 + [9.0], index=idx)
        expected = (22400 * 29 + 22600 * 9) / 38
        assert settlement_price(closes, volumes) == pytest.approx(expected)

    def test_zero_volume_degrades_to_equal_weight(self):
        closes = pd.Series([22400.0] * 29 + [22600.0])
        volumes = pd.Series([0.0] * 30)
        assert settlement_price(closes, volumes) == pytest.approx((22400 * 29 + 22600) / 30)

    def test_empty_session_rejected(self):
        with pytest.raises(ValueError, match="empty"):
            settlement_price(pd.Series([], dtype=float))

    def test_feed_settlement_is_tick_snapped(self):
        closes = pd.Series([22500.017] * 30)
        assert _feed().expiry_settlement(closes) == D("22500.00")


# ===========================================================================
# Feed contract
# ===========================================================================


class TestFeedContract:
    def test_satisfies_protocol(self):
        assert isinstance(_feed(), OptionPricingFeed)

    def test_feed_tag_and_not_real(self):
        feed = _feed()
        assert feed.source_tag == SOURCE_TAG_MODEL_BS == "model_bs"
        assert feed.data_source_real is False
        assert _quote(feed).source_tag == "model_bs"

    def test_quote_carries_bar_time(self):
        q = _quote(_feed())
        assert q.bar_time == ENTRY_BAR

    def test_set_reference_pins_quotes(self):
        feed = _feed()
        feed.set_reference(ENTRY_BAR)
        q = feed.quote(
            underlying="NIFTY", strike=22000.0, expiry=EXPIRY,
            option_type="CE", spot=22000.0,
        )
        assert q.bar_time == ENTRY_BAR

    def test_naive_bar_time_reads_as_exchange_clock(self):
        feed = _feed()
        q = feed.quote(
            underlying="NIFTY", strike=22000.0, expiry=EXPIRY,
            option_type="CE", spot=22000.0,
            bar_time=datetime(2026, 6, 18, 15, 30),  # naive = IST
        )
        assert q.bar_time == ENTRY_BAR
        assert q.mid == D("287.20")

    def test_no_reference_and_no_bar_time_raises(self):
        with pytest.raises(ValueError, match="reference"):
            _feed().quote(
                underlying="NIFTY", strike=22000.0, expiry=EXPIRY,
                option_type="CE", spot=22000.0,
            )

    def test_money_is_decimal_on_tick(self):
        q = _quote(_feed())
        for value in (q.mid, q.bid, q.ask):
            assert isinstance(value, Decimal)
            assert (value / TICK) == (value / TICK).to_integral_value()

    def test_bad_option_type_rejected(self):
        with pytest.raises(ValueError, match="option_type"):
            _quote(_feed(), option_type="XX")


# ===========================================================================
# Appendix A — corrected hand-calc oracle (lot 75, VWAP, exercise STT)
# ===========================================================================


class TestAppendixAOracle:
    def test_entry_quote_matches_hand_calc(self):
        q = _quote(_feed())
        assert q.mid == D("287.20")
        assert q.ask == D("287.90")
        assert q.bid == D("286.50")

    def test_early_exit_quote_matches_hand_calc(self):
        q = _quote(_feed(), spot=22500.0, bar=EXIT_BAR)
        assert q.mid == D("522.30")
        assert q.bid == D("521.00")

    def test_early_exit_round_trip_pnl(self):
        feed = _feed()
        entry = _quote(feed)
        exit_ = _quote(feed, spot=22500.0, bar=EXIT_BAR)
        calc = CommissionCalculator.for_broker("mstock")
        buy_fees = calc.calculate(
            quantity=75, fill_price=entry.ask, side="buy", segment=TradeSegment.OPTIONS
        )
        sell_fees = calc.calculate(
            quantity=75, fill_price=exit_.bid, side="sell", segment=TradeSegment.OPTIONS
        )
        assert buy_fees.total == D("33.22")
        assert sell_fees.total == D("98.46")
        assert sell_fees.get("stt") == D("58.61")
        pnl = (exit_.bid - entry.ask) * 75 - buy_fees.total - sell_fees.total
        assert pnl == D("17350.82")

    def test_hold_to_expiry_pnl_with_vwap_and_exercise_stt(self):
        feed = _feed()
        entry = _quote(feed)
        calc = CommissionCalculator.for_broker("mstock")
        buy_fees = calc.calculate(
            quantity=75, fill_price=entry.ask, side="buy", segment=TradeSegment.OPTIONS
        )
        # Expiry session: 30 one-minute bars around 22500 -> VWAP 22500.
        session = pd.Series([22500.0] * 30)
        settle = feed.expiry_settlement(session)
        assert settle == D("22500.00")
        intrinsic = max(D("0"), settle - D("22000"))
        settlement_value = intrinsic * 75
        exercise = IndiaEquityFees().exercise_charges(
            settlement_value=settlement_value, quantity=D("75"), brokerage=D("20")
        )
        exercise_total = sum(exercise.values(), D("0"))
        assert exercise["stt"] == D("56.25")
        assert exercise_total == D("75.45")
        pnl = (intrinsic - entry.ask) * 75 - buy_fees.total - exercise_total
        assert pnl == D("15798.83")