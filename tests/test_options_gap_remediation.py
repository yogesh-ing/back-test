"""Gap-Analysis remediation tests (G1–G4).

Covers:
- G2.1: SyntheticQuoteProvider (BS pricing, spot response, per-type pricing),
        CachedQuoteProvider (TTL + clear), LiveQuoteProvider (mStock wrapper)
- G4.3: SyntheticChainGenerator (shape, per-type chains, spot-driven ATM)
- G1.1: POST /api/options/trade — open/close via the dashboard driver
- G2.2: provider selection (synthetic default, source tag in summary)
- G2.3: quote_source badge data + spot-control endpoint (cache invalidation)
- G4.1: statutory fees cash-bearing (open + close), equity reconciliation
- G3.1: DirectionalOptions strategy — directional views + registry presence
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal as D
from unittest.mock import MagicMock

import pandas as pd
import pytest

from backtest.instruments.base import OptionType
from backtest.options.paper_trading import (
    FakeQuoteProvider,
    OptionPaperBroker,
    PositionStatus,
)
from backtest.options.quote_providers import (
    CachedQuoteProvider,
    LiveQuoteProvider,
    SyntheticChainGenerator,
    SyntheticQuoteProvider,
)
from backtest.strategy.intent import Direction, MarketView
from backtest.web.app import create_app
from backtest.web.options_api import (
    get_option_broker,
    reset_option_state,
)


D_ = D


# ===========================================================================
# G4.3 — SyntheticChainGenerator
# ===========================================================================


class TestSyntheticChainGenerator:
    def test_chain_shape(self):
        gen = SyntheticChainGenerator(spot=24850.0)
        chain = gen.generate_chain("NIFTY")
        assert len(chain) == 2 * gen.strikes_each_side + 1
        strikes = sorted(chain.keys())
        assert strikes[len(strikes) // 2] == D_(24850)  # ATM lands on spot
        for c in chain.values():
            assert c.option_type == OptionType.CE
            assert c.lot_size == 75
            assert c.underlying == "NIFTY"

    def test_put_side_chain(self):
        gen = SyntheticChainGenerator(spot=52000.0)
        chain = gen.generate_chain("BANKNIFTY", option_type="PE")
        assert all(c.option_type == OptionType.PE for c in chain.values())
        assert all("PE" in c.trading_symbol for c in chain.values())

    def test_invalid_option_type_rejected(self):
        gen = SyntheticChainGenerator()
        with pytest.raises(ValueError):
            gen.generate_chain("NIFTY", option_type="XX")

    def test_premiums_differ_by_strike(self):
        gen = SyntheticChainGenerator(spot=24800.0)
        chain = gen.generate_chain("NIFTY", strikes_each_side=5)
        prices = [gen.price_contract(c, "CE") for c in chain.values()]
        assert len(set(prices)) > 1  # not the hardcoded ₹100 world
        # OTM call (higher strike) is cheaper than ITM call
        atm = gen.price_contract(chain[D_(24800)], "CE")
        otm = gen.price_contract(chain[D_(25000)], "CE")  # 4 steps OTM
        assert otm < atm

    def test_expiry_progression(self):
        gen = SyntheticChainGenerator()
        expiries = gen.available_expiries("NIFTY", count=3)
        assert expiries == sorted(expiries)
        assert len(set(expiries)) == 3


# ===========================================================================
# G2.1 — Quote providers
# ===========================================================================


class TestSyntheticQuoteProvider:
    def _provider(self):
        p = SyntheticQuoteProvider()
        p.register_chain(p.generator.generate_chain("NIFTY", option_type="CE"))
        p.register_chain(p.generator.generate_chain("NIFTY", option_type="PE"))
        return p

    def test_quote_shape_and_source(self):
        p = self._provider()
        q = p.get_quote("MOCK-NIFTY-24800-CE")
        assert q["ltp"] > 0 and q["ask"] > q["bid"]
        assert p.source_name == "synthetic:bs"

    def test_spot_move_changes_prices(self):
        p = self._provider()
        before = p.get_quote("MOCK-NIFTY-24800-CE")["ltp"]
        p.set_spot("NIFTY", 25400.0)
        after = p.get_quote("MOCK-NIFTY-24800-CE")["ltp"]
        assert after > before

    def test_call_vs_put_same_strike(self):
        p = self._provider()
        call = p.get_quote("MOCK-NIFTY-24800-CE")["ltp"]
        put = p.get_quote("MOCK-NIFTY-24800-PE")["ltp"]
        assert call > 0 and put > 0

    def test_unknown_token_zero(self):
        p = self._provider()
        assert p.get_quote("NOPE")["ltp"] == 0.0


class TestCachedQuoteProvider:
    def test_ttl_caches(self):
        inner = MagicMock()
        inner.get_quote.return_value = {"ltp": 1.0}
        cached = CachedQuoteProvider(inner, ttl=60)
        cached.get_quote("A")
        cached.get_quote("A")
        assert inner.get_quote.call_count == 1

    def test_clear_forces_refetch(self):
        inner = MagicMock()
        inner.get_quote.return_value = {"ltp": 1.0}
        cached = CachedQuoteProvider(inner, ttl=60)
        cached.get_quote("A")
        cached.clear()
        cached.get_quote("A")
        assert inner.get_quote.call_count == 2

    def test_source_name_delegates(self):
        inner = MagicMock()
        inner.source_name = "live:mstock"
        assert CachedQuoteProvider(inner).source_name == "live:mstock"


class TestLiveQuoteProvider:
    def _broker(self, ltp=150.0):
        broker = MagicMock()
        broker.get_option_quote.return_value = {
            "ltp": ltp, "bid": ltp - 1, "ask": ltp + 1,
        }
        return broker

    def test_uses_get_option_quote(self):
        provider = LiveQuoteProvider(self._broker())
        quote = provider.get_quote("TOK")
        assert quote["ltp"] == 150.0
        assert provider.source_name == "live:mstock"

    def test_ttl_cache(self):
        broker = self._broker()
        provider = LiveQuoteProvider(broker, cache_ttl_seconds=60)
        provider.get_quote("TOK")
        provider.get_quote("TOK")
        assert broker.get_option_quote.call_count == 1

    def test_broker_failure_degrades(self):
        broker = MagicMock()
        broker.get_option_quote.side_effect = RuntimeError("session dead")
        quote = LiveQuoteProvider(broker).get_quote("TOK")
        assert quote["ltp"] == 0.0
        assert "error" in quote


# ===========================================================================
# G4.1 — statutory fees are cash-bearing
# ===========================================================================


class TestCashBearingFees:
    def _intent(self, legs_meta):
        from backtest.strategy.intent import OptionLeg, TradeIntent

        legs = tuple(
            OptionLeg(tok, tok, side, 1, 75) for tok, side in legs_meta
        )
        strikes = {tok: str(24000 + i * 100) for i, (tok, _) in enumerate(legs_meta)}
        return TradeIntent(
            view=MarketView(direction=Direction.BULLISH, underlying="NIFTY", spot_price=D_(24800)),
            structure_type="long_call" if len(legs) == 1 else "bull_call_spread",
            legs=legs,
            expiry=date(2026, 10, 29),
            metadata={"option_type": "CE", "strikes": strikes},
        )

    def test_open_debits_statutory_fees(self):
        from backtest.simulator import CommissionCalculator

        broker = OptionPaperBroker(
            capital=100_000.0, slippage_pct=0.0, commission_per_lot=0.0,
            fee_calculator=CommissionCalculator.for_broker("mstock"),
        )
        quotes = FakeQuoteProvider(default_price=120.50)
        broker.execute_structure(self._intent([("L1", "BUY")]), quotes)

        # The Phase 8 contract-note anchor: 75 x 120.50 buy = Rs 27.63.
        assert broker.total_statutory_fees_paid == D_("27.63")
        assert broker.available_cash == D_("100000") - D_("9037.50") - D_("27.63")
        # Equity nets the fees too.
        assert broker.total_equity == D_("100000") - D_("27.63")

    def test_close_debits_exit_fees(self):
        from backtest.simulator import CommissionCalculator

        broker = OptionPaperBroker(
            capital=100_000.0, slippage_pct=0.0, commission_per_lot=0.0,
            fee_calculator=CommissionCalculator.for_broker("mstock"),
        )
        quotes = FakeQuoteProvider(default_price=120.50)
        positions = broker.execute_structure(self._intent([("L1", "BUY")]), quotes)

        cash_before_close = broker.available_cash
        quotes.set_price("L1", 130.00)
        broker.close_structure(positions[0].structure_id, quotes)

        # Exit side: brokerage + sell-side STT on 75 x 130 = 9.75 STT + 20 brokerage + surcharges.
        assert broker.total_statutory_fees_paid > D_("27.63")
        assert broker.available_cash < cash_before_close + D_("9750.00")

    def test_no_calculator_stays_free(self):
        broker = OptionPaperBroker(capital=100_000.0, slippage_pct=0.0, commission_per_lot=0.0)
        quotes = FakeQuoteProvider(default_price=120.50)
        broker.execute_structure(self._intent([("L1", "BUY")]), quotes)
        assert broker.total_statutory_fees_paid == D_("0")
        assert broker.available_cash == D_("100000") - D_("9037.50")

    def test_insufficient_margin_includes_fees(self):
        from backtest.simulator import CommissionCalculator
        from backtest.options.paper_trading import InsufficientMarginError

        broker = OptionPaperBroker(
            capital=9_050.0, slippage_pct=0.0, commission_per_lot=0.0,
            fee_calculator=CommissionCalculator.for_broker("mstock"),
        )
        quotes = FakeQuoteProvider(default_price=120.50)
        # Premium 9037.50 alone fits in 9050, but + 27.63 fees does not.
        with pytest.raises(InsufficientMarginError):
            broker.execute_structure(self._intent([("L1", "BUY")]), quotes)


# ===========================================================================
# G3.1 — DirectionalOptions strategy
# ===========================================================================


class TestDirectionalOptionsStrategy:
    def _candles(self, closes):
        idx = pd.date_range("2026-09-01", periods=len(closes))
        return pd.DataFrame({"close": closes}, index=idx)

    def test_bullish_view(self):
        from backtest.strategies.option_directional import DirectionalOptions

        s = DirectionalOptions()
        candles = self._candles([24800 - i * 30 for i in range(12)][::-1])
        view = s.generate_market_view(candles)
        assert view is not None and view.direction == Direction.BULLISH
        assert view.underlying == "NIFTY"
        assert view.spot_price == D_("24800")

    def test_bearish_view(self):
        from backtest.strategies.option_directional import DirectionalOptions

        s = DirectionalOptions()
        view = s.generate_market_view(self._candles([24800 - i * 30 for i in range(12)]))
        assert view.direction == Direction.BEARISH

    def test_flat_market_no_trade(self):
        from backtest.strategies.option_directional import DirectionalOptions

        s = DirectionalOptions()
        assert s.generate_market_view(self._candles([24800.0] * 12)) is None

    def test_registered_in_strategy_registry(self):
        from backtest.strategy.registry import list_strategies

        assert "directional_options" in list_strategies()

    def test_view_feeds_expression_layer(self):
        """Full G3.1 proof: strategy view → selector → structure → intent."""
        from backtest.strategies.option_directional import DirectionalOptions
        from backtest.options.selector import ATMSelector
        from backtest.options.structures import LongCall
        from backtest.options.expiry_policy import NearestExpiryPolicy
        from backtest.options.quote_providers import SyntheticChainGenerator

        s = DirectionalOptions()
        view = s.generate_market_view(self._candles([24800 - i * 30 for i in range(12)][::-1]))
        gen = SyntheticChainGenerator(spot=float(view.spot_price))
        chain = gen.generate_chain("NIFTY")
        expiry = NearestExpiryPolicy().select_expiry(gen.available_expiries("NIFTY"))
        strike = ATMSelector().pick_strike(view.spot_price, sorted(chain.keys()), view.direction)
        intent = LongCall().build(view, [strike], chain, expiry, "directional_options")

        assert intent.structure_type == "long_call"
        assert intent.legs[0].side == "BUY"
        assert intent.view.metadata["strategy"] == "directional_options"


# ===========================================================================
# G1.1 / G2.2 / G2.3 — web API
# ===========================================================================


@pytest.fixture()
def app(monkeypatch):
    # The dashboard book persists to the configured DB when available
    # (Gap G4.2); tests run hermetically without it.
    monkeypatch.setenv("OPTIONS_PERSISTENCE", "off")
    reset_option_state()
    app = create_app(source="synthetic")
    app.config["TESTING"] = True
    yield app
    reset_option_state()


@pytest.fixture()
def client(app):
    return app.test_client()


class TestTradeDriverApi:
    # -- dashboard wiring helpers (pinned-clock tests) -----------------------

    @staticmethod
    def _dashboard_quote_provider():
        from backtest.web.options_api import get_quote_provider

        try:
            return get_quote_provider()
        except Exception:  # noqa: BLE001 — helper must never break the test
            return None

    @classmethod
    def _dashboard_generator(cls):
        quotes = cls._dashboard_quote_provider()
        return getattr(quotes, "generator", None) or getattr(
            getattr(quotes, "inner", None), "generator", None
        )

    def test_open_long_call_201(self, client):
        # Pin the pricing clock 30 days out: on an expiry DAY (e.g.
        # 2026-09-24) the wall-clock ATM premium is a legitimate ~₹45, which
        # would break the premium sanity check below. Date-brittle failure,
        # fixed 2026-09-24.
        generator = self._dashboard_generator()
        if generator is not None:
            expiry = generator.next_monthly_expiry()
            provider = self._dashboard_quote_provider()
            # set_reference lives on the inner synthetic provider, not the
            # TTL cache wrapper.
            inner = getattr(provider, "inner", provider)
            if inner is not None and hasattr(inner, "set_reference"):
                inner.set_reference(
                    datetime.combine(expiry - timedelta(days=7), datetime.min.time())
                )
        resp = client.post(
            "/api/options/trade",
            json={"underlying": "NIFTY", "structure_type": "long_call", "quantity": 1},
        )
        assert resp.status_code == 201
        out = resp.get_json()
        assert out["structure_id"]
        assert len(out["strikes"]) == 1
        assert out["quote_source"] == "synthetic:bs"
        pos = out["positions"][0]
        assert pos["side"] == "BUY" and pos["strike"] == "24800"
        assert float(pos["entry_price"]) > 100  # BS-priced, not ₹100 fake

    def test_open_spread_two_legs(self, client):
        resp = client.post(
            "/api/options/trade",
            json={"underlying": "NIFTY", "structure_type": "bull_call_spread", "quantity": 1},
        )
        assert resp.status_code == 201
        out = resp.get_json()
        assert len(out["positions"]) == 2
        assert {p["side"] for p in out["positions"]} == {"BUY", "SELL"}

    def test_open_put_side_uses_pe_chain(self, client):
        resp = client.post(
            "/api/options/trade",
            json={"underlying": "NIFTY", "structure_type": "long_put", "quantity": 1},
        )
        assert resp.status_code == 201
        assert all(p["option_type"] == "PE" for p in resp.get_json()["positions"])

    def test_quantity_scaling_unit(self):
        """_scale_intent multiplies lots per leg (API-level scaling is capped
        by the risk policy, so this checks the mechanism directly)."""
        from backtest.strategy.intent import MarketView, OptionLeg, TradeIntent
        from backtest.web.options_api import _scale_intent

        intent = TradeIntent(
            view=MarketView(direction=Direction.BULLISH, underlying="NIFTY"),
            structure_type="long_call",
            legs=(OptionLeg("T", "SYM", "BUY", 1, 75),),
            expiry=date(2026, 10, 29),
        )
        scaled = _scale_intent(intent, 3)
        assert scaled.legs[0].quantity == 3
        assert scaled.legs[0].lot_size == 75

    def test_oversized_quantity_rejected_by_risk_policy(self, client):
        """3 lots of ATM premium ≈ 5% of capital > the 2% per-trade loss cap.

        The pricing clock is pinned 30 days out — on expiry day the ATM
        premium collapses and 3 lots no longer breach the cap (date-brittle,
        fixed 2026-09-24).
        """
        generator = self._dashboard_generator()
        if generator is not None:
            expiry = generator.next_monthly_expiry()
            provider = self._dashboard_quote_provider()
            inner = getattr(provider, "inner", provider)
            if inner is not None and hasattr(inner, "set_reference"):
                inner.set_reference(
                    datetime.combine(expiry - timedelta(days=7), datetime.min.time())
                )
        resp = client.post(
            "/api/options/trade",
            json={"underlying": "NIFTY", "structure_type": "long_call", "quantity": 3},
        )
        assert resp.status_code == 400
        assert resp.get_json().get("rejected") is True

    def test_invalid_structure_400(self, client):
        resp = client.post("/api/options/trade", json={"structure_type": "iron_condor"})
        assert resp.status_code == 400

    def test_bad_quantity_400(self, client):
        resp = client.post(
            "/api/options/trade",
            json={"underlying": "NIFTY", "structure_type": "long_call", "quantity": 0},
        )
        assert resp.status_code == 400

    def test_orders_appear_in_summary(self, client):
        client.post(
            "/api/options/trade",
            json={"underlying": "NIFTY", "structure_type": "long_call"},
        )
        data = client.get("/api/options/summary").get_json()
        assert data["open_position_count"] == 1
        assert data["open_structure_count"] == 1


class TestQuoteSourceBadge:
    def test_summary_carries_source(self, client):
        data = client.get("/api/options/summary").get_json()
        assert data["quote_source"] == "synthetic:bs"

    def test_summary_carries_fee_fields(self, client):
        data = client.get("/api/options/summary").get_json()
        assert "statutory_fees" in data and "total_costs" in data

    def test_close_reports_source(self, client):
        client.post("/api/options/trade", json={"structure_type": "long_call"})
        data = client.get("/api/options/summary").get_json()
        sid = data["structures"][0]["structure_id"]
        resp = client.post(f"/api/options/structures/{sid}/close")
        assert resp.get_json()["quote_source"] == "synthetic:bs"


class TestSpotControl:
    def test_set_spot_and_mtm_response(self, client):
        client.post("/api/options/trade", json={"structure_type": "long_call"})
        resp = client.post("/api/options/spot", json={"underlying": "NIFTY", "spot": 25400})
        assert resp.status_code == 200
        data = client.get("/api/options/summary").get_json()
        pos = data["positions"][0]
        assert pos["unrealized_pnl"] > 0  # call gains after +600pt rally

    def test_spot_requires_number(self, client):
        resp = client.post("/api/options/spot", json={"underlying": "NIFTY"})
        assert resp.status_code == 400

    def test_put_loses_after_rally(self, client):
        client.post("/api/options/trade", json={"structure_type": "long_put"})
        client.post("/api/options/spot", json={"underlying": "NIFTY", "spot": 25400})
        data = client.get("/api/options/summary").get_json()
        assert data["positions"][0]["unrealized_pnl"] < 0


class TestMtmOnSummary:
    def test_summary_refreshes_mtm(self, client):
        """The dashboard poll must refresh unrealized P&L (gap found in QA)."""
        client.post("/api/options/trade", json={"structure_type": "long_call"})
        before = client.get("/api/options/summary").get_json()["positions"][0]["unrealized_pnl"]
        client.post("/api/options/spot", json={"underlying": "NIFTY", "spot": 25100})
        after = client.get("/api/options/summary").get_json()["positions"][0]["unrealized_pnl"]
        assert after != before
