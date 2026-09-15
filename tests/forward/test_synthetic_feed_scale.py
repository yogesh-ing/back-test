"""Task D1 — index-scale synthetic spot.

Before D1, ``SyntheticFeed`` seeded **every** symbol at ``rng.uniform(80, 450)``.
A "NIFTY forward test" therefore ran at ~₹392: the option chain is priced off
the bar close, so the generator produced ``NIFTY2609 400 CE`` on a 50-point grid
that collapses to two usable strikes, and everything downstream inherited the
scale — premium, margin, and the index-scaled strategy defaults.

The knock-on was worse than the strikes: ``directional_options`` ships
``scale_points: 100`` / ``min_confidence: 0.3``, so on a ₹392 spot the
close-to-EMA distance never reached the confidence floor and the strategy
emitted **no views at all** — an API-created option runner sat flat forever, with
nothing for the B3 trade log or the C2 columns to show. The demo only traded
after overriding the strategy params by hand.

These tests pin the fix:

* index symbols start inside their real band; equity symbols keep the historic
  small-cap band **with byte-identical draws** (so existing runs/demos are
  unaffected),
* the band always contains the chain generator's own default spot for that
  index (kept honest across the two modules),
* chains built off a feed-driven spot land on the real strike grid with real lot
  sizes,
* an option runner created with **default** strategy parameters now opens and
  closes structures on its own.

See ``docs/OPTIONS-FORWARD-TESTING.md`` → task D1.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from backtest.forward.feed import (
    EQUITY_BAND,
    INDEX_BANDS,
    SyntheticFeed,
    _seed_for,
    base_price_for,
)
from backtest.forward.portfolio_manager import PortfolioManager
from backtest.forward.paper_runner import RunnerConfig
from backtest.forward.risk_supervisor import GlobalRiskConfig
from backtest.options.quote_providers import SyntheticChainGenerator

INDEX_SYMBOLS = ["NIFTY", "BANKNIFTY", "nifty", "banknifty"]
EQUITY_SYMBOLS = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "SBIN", "BTC/USD", "ETH/USD"]


def _seeded_base(symbol: str) -> float:
    return base_price_for(symbol, random.Random(_seed_for(symbol)))


# ---------------------------------------------------------------------------
# The band itself
# ---------------------------------------------------------------------------


class TestBasePrice:
    def test_index_symbols_start_at_index_levels(self):
        for symbol in INDEX_SYMBOLS:
            base = _seeded_base(symbol)
            low, high = INDEX_BANDS[symbol.upper()]
            assert low <= base <= high, f"{symbol} seeded at ₹{base:,.2f}"

    def test_a_nifty_runner_starts_near_25k_not_400(self):
        # The literal bug from the review: strikes like "NIFTY2609 400 CE".
        nifty = _seeded_base("NIFTY")
        assert 24_000 < nifty < 26_000
        # ...and well clear of the old equity band.
        assert not (EQUITY_BAND[0] <= nifty <= EQUITY_BAND[1])

    def test_banknifty_is_higher_than_nifty(self):
        assert _seeded_base("BANKNIFTY") > _seeded_base("NIFTY")

    def test_equity_symbols_keep_the_legacy_band(self):
        for symbol in EQUITY_SYMBOLS:
            base = _seeded_base(symbol)
            assert EQUITY_BAND[0] <= base <= EQUITY_BAND[1], f"{symbol} moved to ₹{base:,.2f}"

    def test_equity_draws_are_byte_for_byte_unchanged(self):
        """The old code was ``rng.uniform(80, 450)`` on the same seeded RNG."""
        for symbol in EQUITY_SYMBOLS:
            legacy = round(random.Random(_seed_for(symbol)).uniform(80, 450), 2)
            assert _seeded_base(symbol) == legacy

    def test_bases_are_deterministic_and_symbol_specific(self):
        assert _seeded_base("NIFTY") == _seeded_base("NIFTY")
        assert _seeded_base("NIFTY") != _seeded_base("BANKNIFTY")

    def test_band_contains_the_generators_own_default_spot(self):
        """The two modules must agree; a drift here re-introduces the bug."""
        generator = SyntheticChainGenerator()
        for underlying, default_spot in generator.DEFAULT_SPOTS.items():
            if underlying not in INDEX_BANDS:
                continue
            low, high = INDEX_BANDS[underlying]
            assert low <= default_spot <= high, (
                f"{underlying}: feed band ₹{low:,.0f}-{high:,.0f} excludes the "
                f"chain generator's default spot ₹{default_spot:,.0f}"
            )

    def test_unknown_symbols_and_empty_names_fall_back_to_equity(self):
        assert EQUITY_BAND[0] <= _seeded_base("") <= EQUITY_BAND[1]
        assert EQUITY_BAND[0] <= _seeded_base("SOMETHING-NEW") <= EQUITY_BAND[1]


# ---------------------------------------------------------------------------
# The feed
# ---------------------------------------------------------------------------


class TestFeedSubscription:
    def _feed(self, symbols):
        feed = SyntheticFeed(on_bar=lambda *_: None, warmup_bars=5)
        feed.add_symbols(symbols)
        return feed

    def test_feed_seeds_index_symbols_at_index_levels(self):
        feed = self._feed(["NIFTY", "BANKNIFTY", "RELIANCE"])
        assert 24_500 <= feed._state["NIFTY"]["close"] <= 25_500
        assert 51_000 <= feed._state["BANKNIFTY"]["close"] <= 53_000
        assert 80 <= feed._state["RELIANCE"]["close"] <= 450

    def test_an_index_walk_stays_in_a_sane_band(self):
        """Twenty warmup bars of ±0.6% must not walk NIFTY into another regime."""
        feed = self._feed(["NIFTY"])
        feed.warmup()
        assert 20_000 < feed._state["NIFTY"]["close"] < 30_000

    def test_emitted_index_bars_carry_index_prices(self):
        bars: list[tuple] = []
        feed = SyntheticFeed(on_bar=lambda sym, bar: bars.append((sym, bar)), warmup_bars=3)
        feed.add_symbols(["NIFTY"])
        feed.warmup()
        assert bars
        for _, bar in bars:
            assert 20_000 < bar["close"] < 30_000
            assert bar["high"] >= bar["low"] > 0


# ---------------------------------------------------------------------------
# Downstream: the chain the index spot builds
# ---------------------------------------------------------------------------


class TestChainScale:
    @pytest.fixture()
    def nifty_spot(self):
        feed = SyntheticFeed(on_bar=lambda *_: None, warmup_bars=10)
        feed.add_symbols(["NIFTY"])
        feed.warmup()
        return feed._state["NIFTY"]["close"]

    def test_strikes_land_on_the_nifty_grid_around_spot(self, nifty_spot):
        generator = SyntheticChainGenerator()
        generator.set_spot("NIFTY", nifty_spot)
        chain = generator.generate_chain("NIFTY", option_type="CE")

        assert chain, "no chain generated"
        assert all(int(strike) % 50 == 0 for strike in chain), "strikes off the 50-pt grid"

        atm = min(chain, key=lambda strike: abs(float(strike) - nifty_spot))
        assert abs(float(atm) - nifty_spot) <= 25  # nearest grid line

    def test_contracts_are_index_contracts(self, nifty_spot):
        generator = SyntheticChainGenerator()
        generator.set_spot("NIFTY", nifty_spot)
        chain = generator.generate_chain("NIFTY", option_type="CE")
        contract = chain[min(chain, key=lambda s: abs(float(s) - nifty_spot))]

        assert contract.lot_size == SyntheticChainGenerator.LOT_SIZES["NIFTY"] == 75
        # The old bug produced "NIFTY2609" + a ~3 digit strike (e.g. 400CE).
        assert contract.trading_symbol.startswith("NIFTY26")
        assert len(contract.trading_symbol) > len("NIFTY2609400CE")

    def test_premiums_are_life_sized(self, nifty_spot):
        generator = SyntheticChainGenerator()
        generator.set_spot("NIFTY", nifty_spot)
        chain = generator.generate_chain("NIFTY", option_type="CE")
        atm = min(chain, key=lambda strike: abs(float(strike) - nifty_spot))
        premium = generator.price_contract(chain[atm], "CE")
        # An ATM NIFTY option is worth hundreds of rupees, not a few.
        assert 20 < premium < 3_000, f"ATM premium ₹{premium:,.2f} is not life-sized"

    def test_banknifty_uses_the_100_point_grid(self):
        feed = SyntheticFeed(on_bar=lambda *_: None, warmup_bars=5)
        feed.add_symbols(["BANKNIFTY"])
        feed.warmup()
        generator = SyntheticChainGenerator()
        generator.set_spot("BANKNIFTY", feed._state["BANKNIFTY"]["close"])
        chain = generator.generate_chain("BANKNIFTY", option_type="PE")
        assert chain
        assert all(int(strike) % 100 == 0 for strike in chain)


# ---------------------------------------------------------------------------
# The knock-on, at the strategy level
# ---------------------------------------------------------------------------


def test_directional_options_defaults_are_reachable_at_index_scale():
    """The shipped parameters, on an index-scale frame, produce a view.

    ``scale_points: 100`` / ``min_confidence: 0.3`` are sized for an index: at
    the old ₹392 spot an entire rally was a fraction of a point, so the
    confidence floor was never reached and the strategy returned ``None`` —
    which is *why* a default-param option runner never traded. This is the fast
    unit-level counterpart to the manager test below.
    """
    import pandas as pd

    from backtest.strategies.option_directional import DirectionalOptions

    strategy = DirectionalOptions()  # no overrides — the shipped defaults
    closes = [24_800 + i * 40 for i in range(30)]  # a steady index rally
    frame = pd.DataFrame(
        {
            "close": closes,
            "open": closes,
            "high": closes,
            "low": closes,
            "volume": [1] * len(closes),
        }
    )

    view = strategy.generate_market_view(frame)

    assert view is not None, "index-scale defaults produced no view"
    assert float(view.spot_price) == closes[-1]
    assert view.confidence >= float(strategy.min_confidence)


# ---------------------------------------------------------------------------
# End to end: a default-param option runner now trades
# ---------------------------------------------------------------------------


def _run_manager_ticks(manager: PortfolioManager, count: int, start_offset: int = 0) -> None:
    """Emit distinct, increasing bars (equal timestamps are de-duplicated)."""
    base = datetime.now(timezone.utc) + timedelta(seconds=1)
    for i in range(start_offset, start_offset + count):
        manager.tick(ts=base + timedelta(days=i))


@pytest.fixture()
def manager():
    mgr = PortfolioManager(
        risk_config=GlobalRiskConfig(daily_loss_limit=1_000_000, max_drawdown_pct=0.9),
        tick_seconds=1.0,
        warmup_bars=30,
        auto_start_feed=False,
    )
    yield mgr
    mgr.shutdown()


def test_default_param_option_runner_opens_structures_on_index_scale(manager):
    """The D1 knock-on: no param overrides, and it still trades.

    ``directional_options``' 100-point confidence scale only means anything at
    index scale — this is the regression that kept an API-created option runner
    flat forever.
    """
    instance_id = manager.add_runner(
        RunnerConfig(
            name="NIFTY-DEFAULT",
            strategy_name="directional_options",
            allocated_capital=500_000,
            symbols=["NIFTY"],
            timeframe="1day",
            mode="paper",
            source="synthetic",
            instrument={
                "type": "option",
                "expression": {
                    "type": "bull_call_spread",
                    "strike_selection": "atm",
                    "quantity": 3,
                    "exit": {"min_days_to_expiry": 2},
                },
            },
        )
    )
    runner = manager.get_runner(instance_id)
    assert runner is not None

    manager.feed.warmup()
    _run_manager_ticks(manager, 40)
    _run_manager_ticks(manager, 40, start_offset=40)

    summary = runner.options_summary()
    assert summary is not None
    assert summary["executed_count"] >= 1, "default params still never trade"
    assert summary["closed_structures"] >= 1, "structures opened but never closed"

    # Entries happened at index strikes, in index-sized lots.
    spot = manager.feed._state["NIFTY"]["close"]
    assert 20_000 < spot < 30_000
    trades = [t for t in runner.closed_trades if t.get("kind") == "option"]
    assert trades
    for trade in trades:
        strikes = [float(s) for s in trade["strikes"]]
        assert strikes, "structure recorded without strikes"
        assert all(strike % 50 == 0 for strike in strikes)
        assert all(abs(strike - spot) < 2_000 for strike in strikes)
        assert trade["units"] == trade["qty"] * 75  # NIFTY lot size
        assert trade["exit_reason"]


def test_equity_runner_prices_are_unchanged(manager):
    """Existing small-cap behaviour, end to end through the manager."""
    runners = []
    for i, symbol in enumerate(("RELIANCE", "TCS")):
        instance_id = manager.add_runner(
            RunnerConfig(
                name=f"EQUITY-{i}",
                strategy_name="sma_crossover",
                allocated_capital=100_000,
                symbols=[symbol],
                timeframe="1day",
                mode="paper",
                source="synthetic",
            )
        )
        runners.append((symbol, manager.get_runner(instance_id)))

    manager.feed.warmup()
    _run_manager_ticks(manager, 20)

    for symbol, runner in runners:
        bars = runner._bars[symbol]
        assert bars, f"{symbol} received no bars"
        for bar in bars:
            assert 80 <= bar["close"] <= 450, f"{symbol} bar at ₹{bar['close']:,.2f}"
