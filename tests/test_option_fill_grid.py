"""BUG-06 — every booked option fill lands on the exchange ₹0.05 grid.

Measured on 2026-10-28: 910/910 option orders quoted an exchange-legal price,
but 733 (80.5 %) of the prices the paper book actually booked were off the ₹0.05
NSE option grid — ``_apply_slippage()`` multiplied an already-crossed bid/ask by
±0.1 % and quantised to paise. Two defects in one line: a fill no venue could
print, and a cost charged twice (the spread *and* modelled impact on top).

These tests pin the replacement contract:

1. ``quantize_nse_tick`` rounds ADVERSELY onto the instrument's own grid
   (BUY up, SELL down) and every option entry/exit passes through it;
2. a leg that filled against a live two-sided book pays **exactly** the side it
   crossed — no percentage on top — while mid/synthetic quotes keep the historic
   ``slippage_pct`` model, still quantised onto the grid;
3. residual impact on a crossed book is counted in WHOLE ticks, never in paise;
4. the grid is the contract's, so a paise-ticking instrument is not dragged onto
   ₹0.05 (the equity paths are option-only code and stay untouched).
"""

from __future__ import annotations

import random
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from backtest.options.execution_gate import RESEARCH_STRUCTURES
from backtest.options.paper_trading import (
    NSE_OPTION_TICK_SIZE,
    TICK_SIZE_METADATA_KEY,
    OptionPaperBroker,
    quantize_nse_tick,
)
from backtest.options.quote_providers import BidAskQuoteProvider
from backtest.strategy.intent import Direction, MarketView, OptionLeg, TradeIntent

TICK = NSE_OPTION_TICK_SIZE
EXPIRY = date(2026, 12, 24)


def on_grid(price: Decimal, tick: Decimal = TICK) -> bool:
    """The exchange test: a price the venue can actually print."""
    return Decimal(price) % tick == Decimal("0")


# ---------------------------------------------------------------------------
# Provider doubles — one that behaves like the live WS depth cache, one that
# only ever answers a mid.
# ---------------------------------------------------------------------------


class _DepthStub:
    """Stands in for ``LiveQuoteProvider``: real two-sided bid/ask rows.

    Wrapped in :class:`BidAskQuoteProvider` its ``source_name`` composes to
    ``"live:mstock+bidask"`` — the marker the broker reads to know the spread
    was crossed by the seam and must not be charged again.
    """

    source_name = "live:mstock"

    def __init__(self, rows: dict[str, dict], contracts: dict | None = None) -> None:
        self.rows = {str(k): dict(v) for k, v in rows.items()}
        self._contracts = contracts or {}

    def get_quote(self, instrument_token: str) -> dict:
        return dict(self.rows[str(instrument_token)])


class _MidStub:
    """A one-number feed (replay / OHLCV / HTTP LTP-only): no side to cross."""

    source_name = "replay:ohlcv"

    def __init__(self, mids: dict[str, Decimal]) -> None:
        self.mids = {str(k): v for k, v in mids.items()}

    def get_quote(self, instrument_token: str) -> dict:
        mid = self.mids[str(instrument_token)]
        return {"ltp": float(mid), "bid": 0.0, "ask": 0.0}


def _live_depth(rows: dict[str, dict], contracts: dict | None = None):
    return BidAskQuoteProvider(_DepthStub(rows, contracts))


def _intent(token: str, symbol: str, side: str, lots: int = 1, lot_size: int = 65):
    leg = OptionLeg(
        instrument_token=token,
        trading_symbol=symbol,
        side=side,
        quantity=lots,
        lot_size=lot_size,
    )
    structure = "long_call" if side == "BUY" else "short_straddle"
    view = MarketView(
        direction=Direction.BULLISH if side == "BUY" else Direction.NEUTRAL,
        confidence=0.7,
        underlying="NIFTY",
        spot_price=Decimal("24500"),
    )
    return TradeIntent(
        view=view,
        structure_type=structure,
        legs=(leg,),
        expiry=EXPIRY,
        strategy_name="grid_test",
        metadata={"option_type": "CE", "strike": "24500"},
    )


def _broker(fills: list, **kwargs) -> OptionPaperBroker:
    return OptionPaperBroker(
        capital=5_000_000.0,
        allowed_structures=RESEARCH_STRUCTURES,
        on_leg_fill=fills.append,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. the grid helper itself
# ---------------------------------------------------------------------------


class TestQuantizeNSEGrid:
    @pytest.mark.parametrize(
        "raw,expected_buy,expected_sell",
        [
            ("178.08", "178.10", "178.05"),  # the 28-Oct row from the QA report
            ("206.24", "206.25", "206.20"),
            ("910", "910.00", "910.00"),  # already legal: unchanged
            ("0.01", "0.05", "0.00"),
            ("41.508", "41.55", "41.50"),
        ],
    )
    def test_rounds_adversely_onto_the_grid(self, raw, expected_buy, expected_sell):
        assert quantize_nse_tick(Decimal(raw), "BUY") == Decimal(expected_buy)
        assert quantize_nse_tick(Decimal(raw), "SELL") == Decimal(expected_sell)
        assert on_grid(quantize_nse_tick(Decimal(raw), "BUY"))
        assert on_grid(quantize_nse_tick(Decimal(raw), "SELL"))

    def test_adverse_direction_is_never_generous(self):
        """A BUY never fills below the quote, a SELL never fills above it."""
        for cents in range(0, 100):
            price = Decimal(f"100.{cents:02d}")
            assert quantize_nse_tick(price, "BUY") >= price
            assert quantize_nse_tick(price, "SELL") <= price

    def test_honours_the_instruments_own_tick(self):
        """A paise-ticking instrument is NOT dragged onto ₹0.05.

        This is why the helper takes a tick and why only the option book calls
        it — equity fills keep trading in paise.
        """
        assert quantize_nse_tick(Decimal("178.06"), "BUY", Decimal("0.01")) == Decimal(
            "178.06"
        )
        assert quantize_nse_tick(Decimal("178.06"), "SELL", Decimal("0.01")) == Decimal(
            "178.06"
        )
        assert quantize_nse_tick(Decimal("2.31"), "BUY", Decimal("0.05")) == Decimal(
            "2.35"
        )

    def test_garbage_in_stays_harmless(self):
        assert quantize_nse_tick(None, "BUY") == Decimal("0")
        assert quantize_nse_tick("not-a-price", "SELL") == Decimal("0")


# ---------------------------------------------------------------------------
# 2. the double-count is gone on live two-sided depth
# ---------------------------------------------------------------------------


class TestNoSpreadPlusBpsDoubleCount:
    def test_live_depth_buy_pays_exactly_the_ask(self):
        fills: list[dict] = []
        broker = _broker(fills)  # default slippage_pct=0.001 still configured
        provider = _live_depth(
            {"50041": {"ltp": 910.02, "bid": 910.00, "ask": 910.05}}
        )
        positions = broker.execute_structure(
            _intent("50041", "NIFTY26DEC24500CE", "BUY"), provider
        )
        assert positions[0].entry_price == Decimal("910.05")
        assert on_grid(positions[0].entry_price)
        assert positions[0].metadata["fill_basis"] == "live_depth"
        # the Orders tab can stop reporting the model constant as "slippage"
        assert fills[0]["price"] == fills[0]["requested_price"] == Decimal("910.05")

    def test_live_depth_sell_receives_exactly_the_bid(self):
        fills: list[dict] = []
        broker = _broker(fills)
        provider = _live_depth({"50042": {"ltp": 178.07, "bid": 178.05, "ask": 178.40}})
        positions = broker.execute_structure(
            _intent("50042", "NIFTY26DEC24500PE", "SELL"), provider
        )
        assert positions[0].entry_price == Decimal("178.05")
        assert on_grid(positions[0].entry_price)

    def test_mid_quote_buy_still_pays_slippage_and_lands_on_grid(self):
        """No counterparty was crossed, so the cost model still applies."""
        fills: list[dict] = []
        broker = _broker(fills)
        provider = _MidStub({"50041": Decimal("910.00")})
        positions = broker.execute_structure(
            _intent("50041", "NIFTY26DEC24500CE", "BUY"), provider
        )
        mid = Decimal("910.00")
        expected_raw = mid * Decimal("1.001")  # 910.91 → already on grid
        assert expected_raw == Decimal("910.910")
        assert positions[0].entry_price == Decimal("910.95")
        assert positions[0].entry_price > mid  # slippage charged, not dropped
        assert on_grid(positions[0].entry_price)
        assert positions[0].metadata["fill_basis"] == "model"

    def test_mid_quote_buy_rounds_up_when_bps_land_off_grid(self):
        fills: list[dict] = []
        broker = _broker(fills)
        provider = _MidStub({"50041": Decimal("145.35")})
        positions = broker.execute_structure(
            _intent("50041", "NIFTY26DEC24500CE", "BUY"), provider
        )
        # 145.35 × 1.001 = 145.49535 → adverse ceil onto the grid, never .145.50 paise
        assert positions[0].entry_price == Decimal("145.50")
        assert on_grid(positions[0].entry_price)

    def test_synthetic_book_is_not_live_depth_and_keeps_the_bps_model(self):
        """A modelled bid/ask is not a venue book: 910/910 must stay honest."""

        class _Synthetic(_DepthStub):
            source_name = "synthetic:bs"

        broker = _broker([])
        provider = BidAskQuoteProvider(
            _Synthetic({"50041": {"ltp": 910.02, "bid": 910.00, "ask": 910.05}})
        )
        assert provider.source_name == "synthetic:bs+bidask"
        positions = broker.execute_structure(
            _intent("50041", "NIFTY26DEC24500CE", "BUY"), provider
        )
        assert positions[0].entry_price > Decimal("910.05")  # bps on top of the ask
        assert positions[0].metadata["fill_basis"] == "model"
        assert on_grid(positions[0].entry_price)

    def test_one_sided_book_never_invents_a_side(self):
        """No ask to buy against → the structure stays unopened, as before."""
        from backtest.options.quote_providers import NoExecutableQuoteError

        broker = _broker([])
        provider = _live_depth({"50041": {"ltp": 910.0, "bid": 909.5, "ask": 0.0}})
        with pytest.raises(NoExecutableQuoteError):
            broker.execute_structure(_intent("50041", "NIFTY26DEC24500CE", "BUY"), provider)
        assert broker.get_open_structures() == []


# ---------------------------------------------------------------------------
# 3. residual impact is counted in whole ticks
# ---------------------------------------------------------------------------


class TestResidualTicks:
    def test_order_bigger_than_the_published_top_of_book_costs_whole_ticks(self):
        fills: list[dict] = []
        broker = _broker(fills, slippage_ticks=0)
        ask = 910.05
        provider = _live_depth(
            {
                "50041": {
                    "ltp": 910.02,
                    "bid": 910.00,
                    "ask": ask,
                    "depth": {
                        "ask": [{"price": ask, "quantity": 30}],
                        "bid": [{"price": 910.0, "quantity": 50}],
                    },
                }
            }
        )
        # 45 units against a visible 30: one more book to eat → exactly +1 tick
        positions = broker.execute_structure(
            _intent("50041", "NIFTY26DEC24500CE", "BUY", lots=1, lot_size=45), provider
        )
        assert positions[0].entry_price == Decimal("910.10")
        assert on_grid(positions[0].entry_price)

    def test_walking_the_book_costs_whole_ticks_not_paise(self):
        """65 units over a 30-unit top book = two more books = two ticks."""
        broker = _broker([])
        ask = 910.05
        provider = _live_depth(
            {
                "50041": {
                    "ltp": 910.02,
                    "bid": 910.00,
                    "ask": ask,
                    "depth": {"ask": [{"price": ask, "quantity": 30}], "bid": []},
                }
            }
        )
        positions = broker.execute_structure(
            _intent("50041", "NIFTY26DEC24500CE", "BUY", lots=1, lot_size=65), provider
        )
        paid = positions[0].entry_price
        assert paid == Decimal("910.15")
        ticks = (paid - Decimal(str(ask))) / TICK
        assert ticks == ticks.to_integral_value()  # integer ticks, never 0.01 steps
        assert on_grid(paid)

    def test_size_inside_the_top_of_book_costs_no_extra_ticks(self):
        broker = _broker([])
        ask = 910.05
        provider = _live_depth(
            {
                "50041": {
                    "ltp": 910.02,
                    "bid": 910.00,
                    "ask": ask,
                    "depth": {"ask": [{"price": ask, "quantity": 500}], "bid": []},
                }
            }
        )
        positions = broker.execute_structure(
            _intent("50041", "NIFTY26DEC24500CE", "BUY", lots=1, lot_size=65), provider
        )
        assert positions[0].entry_price == Decimal("910.05")

    def test_configured_tick_residual_stays_on_grid(self):
        broker = _broker([], slippage_ticks=2)
        provider = _live_depth({"50041": {"ltp": 910.02, "bid": 910.00, "ask": 910.05}})
        positions = broker.execute_structure(
            _intent("50041", "NIFTY26DEC24500CE", "BUY"), provider
        )
        assert positions[0].entry_price == Decimal("910.15")
        assert on_grid(positions[0].entry_price)


# ---------------------------------------------------------------------------
# 4. the per-instrument grid, and the exits that inherit it
# ---------------------------------------------------------------------------


class TestInstrumentGrid:
    def test_contract_tick_wins_over_the_default(self):
        broker = _broker([])
        provider = _live_depth(
            {"50041": {"ltp": 178.06, "bid": 178.01, "ask": 178.06}},
            contracts={"50041": SimpleNamespace(tick_size=Decimal("0.01"))},
        )
        positions = broker.execute_structure(
            _intent("50041", "NIFTY26DEC24500CE", "BUY"), provider
        )
        assert positions[0].entry_price == Decimal("178.06")
        assert positions[0].metadata[TICK_SIZE_METADATA_KEY] == "0.01"

    def test_exit_fill_uses_the_legs_own_recorded_grid(self):
        broker = _broker([])
        provider = _live_depth({"50041": {"ltp": 910.02, "bid": 910.00, "ask": 910.05}})
        positions = broker.execute_structure(
            _intent("50041", "NIFTY26DEC24500CE", "BUY"), provider
        )
        provider.inner.rows["50041"].update(
            {"ltp": 913.33, "bid": 913.35, "ask": 913.40}
        )
        broker.close_structure(positions[0].structure_id, provider)
        # sold into a legal bid: exactly that bid, no bps on top of the spread
        assert positions[0].current_price == Decimal("913.35")
        assert on_grid(positions[0].current_price)

    def test_exit_from_an_off_grid_bid_rounds_adversely(self):
        """The fail-closed/stale-mark paths can hand us a paise price — the
        grid still wins, and always in the exchange's favour."""
        broker = _broker([])
        provider = _live_depth({"50041": {"ltp": 910.02, "bid": 910.00, "ask": 910.05}})
        positions = broker.execute_structure(
            _intent("50041", "NIFTY26DEC24500CE", "BUY"), provider
        )
        provider.inner.rows["50041"].update(
            {"ltp": 913.33, "bid": 913.31, "ask": 913.37}
        )
        broker.close_structure(positions[0].structure_id, provider)
        assert positions[0].current_price == Decimal("913.30")  # rounded down, not up
        assert on_grid(positions[0].current_price)


# ---------------------------------------------------------------------------
# 5. the headline invariant, randomised
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", [20261028, 910_05])
def test_every_randomised_option_fill_lands_on_the_grid(seed):
    """≥50 BUY/SELL fills across prices: no booked price may leave the grid."""
    rng = random.Random(seed)

    checked = 0
    for index in range(60):
        side = "BUY" if index % 2 == 0 else "SELL"
        lots = rng.randint(1, 3)
        lot_size = rng.choice([25, 65])
        mid = Decimal(rng.randrange(500, 150_000)) / Decimal(100)  # ₹5.00 – ₹1500.00
        off_grid = mid + Decimal(str(rng.choice(["0.01", "0.02", "0.03", "0.04", "0.00"])))
        ask = off_grid
        bid = off_grid - Decimal("0.35")
        token = f"T{index:03d}"
        fills: list[dict] = []
        broker = _broker(fills)

        if index % 3 == 0:
            provider = _MidStub({token: off_grid})  # mid / replay pricing
        else:
            provider = _live_depth(
                {token: {"ltp": float(mid), "bid": float(bid), "ask": float(ask)}}
            )

        symbol = f"NIFTY26DEC24500{'CE' if side == 'BUY' else 'PE'}"
        intent = _intent(token, symbol, side, lots, lot_size)
        positions = broker.execute_structure(intent, provider)
        leg = positions[0]
        assert on_grid(leg.entry_price), (index, side, provider, leg.entry_price)
        assert leg.entry_price >= Decimal("0.05")
        checked += 1

        # move the book and close: the exit is a fill too
        new_mid = off_grid + (Decimal("1.5") if side == "BUY" else Decimal("-1.2"))
        if isinstance(provider, _MidStub):
            provider.mids[token] = new_mid
        else:
            provider.inner.rows[token].update(
                {
                    "ltp": float(new_mid),
                    "bid": float(new_mid - Decimal("0.35")),
                    "ask": float(new_mid),
                }
            )
        broker.close_structure(leg.structure_id, provider)
        assert on_grid(leg.current_price), (index, side, leg.current_price)
        checked += 1

        # whatever reached the ledger obeys the same rule
        for record in fills:
            assert on_grid(Decimal(record["price"])), record

    assert checked >= 120  # 60 entries + 60 exits, well past the 50-fill bar
