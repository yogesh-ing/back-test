"""Tests for the recorded-chain feed and its adapter refusal path (Phase 4).

The feed must replay recorded premiums verbatim (no BS guess), refuse a
missing/illiquid strike with ChainQuoteUnavailable, and — through the adapter —
hand the broker a true zero quote, never a tick-floored ₹0.05 phantom.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pandas as pd
import pytest

from backtest.instruments.base import OptionType
from backtest.instruments.option import OptionContract
from backtest.options.chain_history_feed import (
    ChainHistoryFeed,
    ChainQuoteUnavailable,
    InMemoryChainReader,
)
from backtest.options.feed_adapter import FeedQuoteAdapter
from backtest.options.pricing_feed import OptionPricingFeed
from backtest.options.quote_providers import NoExecutableQuoteError
from backtest.timekeeping import EXCHANGE_TZ

EXP = date(2024, 1, 25)
BAR = datetime(2024, 1, 25, 10, 0, tzinfo=EXCHANGE_TZ)
LATER = datetime(2024, 1, 25, 10, 5, tzinfo=EXCHANGE_TZ)


def row(strike=25000, otype="CE", *, bar_ts=BAR, bid=149.0, ask=151.0, ltp=150.0,
        iv=0.16, delta=0.5, gamma=0.001, theta=-12.0, vega=8.0, oi=5000, volume=1000):
    return {
        "underlying": "NIFTY", "expiry": EXP, "strike": float(strike),
        "option_type": otype, "bar_ts": bar_ts, "bid": bid, "ask": ask, "ltp": ltp,
        "iv": iv, "delta": delta, "gamma": gamma, "theta": theta, "vega": vega,
        "oi": oi, "volume": volume, "spot": 25000.0,
    }


def feed(rows, **kw):
    return ChainHistoryFeed(InMemoryChainReader(rows), **kw)


def test_protocol_conformance_and_tags():
    f = feed([row()])
    assert isinstance(f, OptionPricingFeed)
    assert f.source_tag == "chain_history"
    assert f.data_source_real is True


def test_quote_replays_recorded_premiums_verbatim():
    f = feed([row(bid=149.0, ask=151.0, iv=0.18, delta=0.52, theta=-13.5)])
    f.set_reference(BAR)
    q = f.quote(underlying="NIFTY", strike=25000, expiry=EXP, option_type="CE", spot=25000.0)
    assert q.bid == Decimal("149.00")
    assert q.ask == Decimal("151.00")
    assert q.mid == Decimal("150.00")
    assert q.iv == 0.18
    assert q.delta == 0.52
    assert q.theta == -13.5
    assert q.source_tag == "chain_history"
    assert q.bar_time == BAR


def test_asof_returns_last_row_at_or_before_bar():
    rows = [row(bar_ts=BAR, bid=100.0, ask=102.0), row(bar_ts=LATER, bid=200.0, ask=202.0)]
    f = feed(rows)
    f.set_reference(BAR)
    q = f.quote(underlying="NIFTY", strike=25000, expiry=EXP, option_type="CE", spot=25000.0)
    assert q.bid == Decimal("100.00")
    f.set_reference(LATER)
    q2 = f.quote(underlying="NIFTY", strike=25000, expiry=EXP, option_type="CE", spot=25000.0)
    assert q2.bid == Decimal("200.00")


def test_bar_before_first_row_refuses():
    f = feed([row(bar_ts=LATER)])
    f.set_reference(BAR)
    with pytest.raises(ChainQuoteUnavailable):
        f.quote(underlying="NIFTY", strike=25000, expiry=EXP, option_type="CE", spot=25000.0)


def test_missing_strike_refuses():
    f = feed([row(strike=25000)])
    f.set_reference(BAR)
    with pytest.raises(ChainQuoteUnavailable) as ei:
        f.quote(underlying="NIFTY", strike=25100, expiry=EXP, option_type="CE", spot=25000.0)
    assert ei.value.reason == "NO_CHAIN_DATA"
    assert isinstance(ei.value, NoExecutableQuoteError)


@pytest.mark.parametrize("bad", [
    {"bid": 0.0, "ask": 0.0},          # zero book
    {"bid": 151.0, "ask": 149.0},      # crossed book
    {"ltp": 0.0},                       # zero last trade
    {"oi": 0, "volume": 0},            # dead strike (liquidity gate)
    {"bid": None, "ask": None},        # incomplete book
])
def test_illiquid_rows_refused(bad):
    f = feed([row(**bad)])
    f.set_reference(BAR)
    with pytest.raises(ChainQuoteUnavailable):
        f.quote(underlying="NIFTY", strike=25000, expiry=EXP, option_type="CE", spot=25000.0)


def test_liquidity_gate_off_allows_zero_oi_book():
    f = feed([row(oi=0, volume=0)], liquidity_gate=False)
    f.set_reference(BAR)
    q = f.quote(underlying="NIFTY", strike=25000, expiry=EXP, option_type="CE", spot=25000.0)
    assert q.bid == Decimal("149.00")


def test_absent_greeks_read_zero_not_bs_guess():
    r = row()
    for k in ("iv", "delta", "gamma", "theta", "vega"):
        r[k] = None
    f = feed([r])
    f.set_reference(BAR)
    q = f.quote(underlying="NIFTY", strike=25000, expiry=EXP, option_type="CE", spot=25000.0)
    assert (q.iv, q.delta, q.gamma, q.theta, q.vega) == (0.0, 0.0, 0.0, 0.0, 0.0)


def test_expiry_settlement_vwap_of_last_bars():
    f = feed([row()])
    closes = pd.Series([100.0] * 40, dtype=float)
    settle = f.expiry_settlement(closes)
    assert settle == Decimal("100.00")


def test_invalid_option_type_raises_valueerror():
    f = feed([row()])
    f.set_reference(BAR)
    with pytest.raises(ValueError):
        f.quote(underlying="NIFTY", strike=25000, expiry=EXP, option_type="XX", spot=25000.0)


# --- adapter refusal path: true zero, never a tick-floored phantom ---------


def _adapter(f):
    adapter = FeedQuoteAdapter(f, "NIFTY")
    adapter.set_reference(BAR)
    adapter.set_spot("NIFTY", 25000.0)
    contract = OptionContract(
        instrument_token="FEED-NIFTY-25000-CE",
        trading_symbol="NIFTY240125000CE",
        underlying="NIFTY", expiry=EXP, strike=Decimal("25000"),
        option_type=OptionType.CE, lot_size=75, metadata={},
    )
    adapter.register_contract(contract)
    return adapter, contract.instrument_token


def test_adapter_refusal_returns_zero_not_tick_floor():
    adapter, token = _adapter(feed([]))  # no recorded rows → refusal
    quote = adapter.get_quote(token)
    assert quote["ltp"] == 0.0
    assert quote["bid"] == 0.0
    assert quote["ask"] == 0.0
    assert quote["error"] == "no_chain_data"
    assert quote["pricing_feed"] == "chain_history"
    assert quote["model"] is False
    # The phantom-price guard reads fill_price<=0; a ₹0.05 floor would defeat it.
    assert quote["ltp"] < 0.05


def test_adapter_success_path_floors_and_carries_greeks():
    adapter, token = _adapter(feed([row(bid=149.0, ask=151.0, delta=0.52)]))
    quote = adapter.get_quote(token)
    assert quote["ltp"] == 150.0
    assert quote["bid"] == 149.0
    assert quote["ask"] == 151.0
    assert quote["delta"] == 0.52
    assert quote["pricing_feed"] == "chain_history"
    assert "error" not in quote


def test_adapter_unknown_token_still_zero_error():
    adapter, _ = _adapter(feed([row()]))
    quote = adapter.get_quote("NOT-REGISTERED")
    assert quote["ltp"] == 0.0
    assert quote["error"] == "unknown token"