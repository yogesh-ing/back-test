"""Sprint 1 invariant gate — `docs/BUG-FIX-PLAN-2026-10-28.md` §4.

The five properties the 28-Oct live-paper QA run violated, each asserted once,
in one file, in seconds:

1. **One entry per timeframe bucket** (BUG-01) — an "1hour" runner entered 205
   times in a session because only a cooldown stood between ticks and entries.
2. **Every option fill on the ₹0.05 grid** (BUG-06) — 733 of 910 booked prices
   were ones no exchange could ever have printed.
3. **Fee-honest round trips on both books** (BUG-26 / BUG-04) — a cost stack of
   zero made a scalper look profitable at exactly the frequency the venue taxes
   it, and the closed trade could not say why it had closed.
4. **An intraday option book squares off on the session clock** (BUG-39) — the
   square-off used to need a tick to arrive at 15:10; an illiquid strike stops
   printing, so three monthly legs rode overnight.
5. **No entry outside the session window** (BUG-18) — a bar stamped 15:30:30 IST
   opened a structure in a session that had already ended.

Not duplicated here: the single-Alembic-head check (§4 item 5) is already
enforced more strictly by ``tests/db/test_migrations_009_013.py``, which pins
the head revision by name rather than its count, and the pytest DB firewall is
Sprint 2 / Ticket 2.4.

No broker session, no server, no production database.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backtest.forward.options_bridge import (
    CARRY_INTRADAY,
    EXIT_INTRADAY_SQUAREOFF,
    OptionsBridge,
)
from backtest.forward.paper_runner import OrderLedger, RunnerConfig, StrategyRunner
from backtest.forward.trade_persistence import _map_exit_reason
from backtest.options.execution_gate import RESEARCH_STRUCTURES
from backtest.options.paper_trading import NSE_OPTION_TICK_SIZE, OptionPaperBroker
from backtest.options.quote_providers import (
    BidAskQuoteProvider,
    SyntheticChainGenerator,
    SyntheticQuoteProvider,
)
from backtest.plugins import discover_plugins
from backtest.simulator import fees as fees_module
from backtest.simulator.fees import CommissionCalculator
from backtest.strategy.intent import Direction, MarketView, OptionLeg, TradeIntent
from options_support import wide_gate

IST = timezone(timedelta(hours=5, minutes=30))
EXPIRY = date(2026, 12, 24)
TICK = NSE_OPTION_TICK_SIZE

#: The exit reasons the ``trades`` table accepts — a closed row outside this
#: set is a CHECK-constraint rejection waiting to happen.
DB_EXIT_REASONS = {
    "signal",
    "stop_loss",
    "take_profit",
    "trailing_stop",
    "time_stop",
    "risk_limit",
    "manual",
    "eod_flat",
}

V2 = {
    "expression_version": 2,
    "type": {
        "BULLISH": {"structure": "long_call", "strike_selection": "atm"},
        "BEARISH": {"structure": "long_put", "strike_selection": "atm"},
    },
    "quantity": 1,
    "exit": {"stop_loss_pct": 0.30, "signal_flip": False, "min_days_to_expiry": 2},
}


@pytest.fixture(autouse=True)
def _deterministic_environment(monkeypatch):
    """Testing data profile (synthetic chains allowed) + yaml/preset fees only.

    The fee engine normally resolves through the settings-panel DB row; pinned
    out here so this gate measures the cost model, not whatever the deployed
    panel says today, and never opens a database connection.
    """
    monkeypatch.setenv("BACKTEST_DATA_PROFILE", "testing")
    monkeypatch.delenv("OPTIONS_INTRADAY_SQUAREOFF_MINUTES", raising=False)
    monkeypatch.delenv("EOD_SQUAREOFF_SWEEP", raising=False)
    monkeypatch.setattr(fees_module, "_load_db_broker_profile", lambda broker: None)
    discover_plugins()


def _ist(hour: int, minute: int = 0, second: int = 0) -> datetime:
    """An exchange instant, as the aware UTC value the app stores."""
    return datetime(2026, 10, 5, hour, minute, second, tzinfo=IST).astimezone(timezone.utc)


def _ist_text(hour: int, minute: int = 0, second: int = 0) -> str:
    return datetime(2026, 10, 5, hour, minute, second).strftime("%Y-%m-%d %H:%M:%S")


def _bridge(expression=None, timeframe="5min", **kwargs) -> OptionsBridge:
    return OptionsBridge(
        capital=2_000_000.0,
        expression={**V2, **(expression or {})},
        quote_provider=SyntheticQuoteProvider(SyntheticChainGenerator()),
        risk_gate=wide_gate(),
        timeframe=timeframe,
        **kwargs,
    )


def _open_structure(bridge: OptionsBridge, hour=10, minute=0, second=0, spot=24_500.0):
    ts = _ist_text(hour, minute, second)
    bridge.on_bar("NIFTY", spot, ts)
    view = MarketView(
        direction=Direction.BULLISH,
        confidence=1.0,
        underlying="NIFTY",
        spot_price=Decimal(str(spot)),
        bar_timestamp=ts,
    )
    return bridge.on_intent(
        {"structure": "long_call", "strike_selection": "atm", "quantity": 1},
        view,
        "invariants",
    )


# --------------------------------------------------------------------------- #
# 1. One entry per timeframe bucket (BUG-01)
# --------------------------------------------------------------------------- #


def test_tick_churn_cannot_take_more_than_one_entry_per_timeframe_bucket():
    """60 ticks over 10 minutes into a 15-minute runner that stops out at +3s.

    The stop is followed by every tick in the same bucket, cooldown released —
    the latch, not the cooldown, is what has to hold.
    """
    from backtest.strategy.registry import get_strategy

    expression = get_strategy("immediate_strangle").execution_spec().to_expression()
    runner = StrategyRunner(
        RunnerConfig(
            name="LATCH",
            strategy_name="immediate_strangle",
            allocated_capital=500_000,
            symbols=["NIFTY"],
            timeframe="15min",
            mode="paper",
            source="synthetic",
            instrument={"type": "option", "expression": expression},
        ),
        ledger=OrderLedger(),
    )
    runner.start()

    # 09:45:05 IST — inside the 09:45–10:00 bucket, so all 10 minutes of churn
    # below stay in the bucket the entry already spent.
    assert runner.on_tick("NIFTY", price=24500.0, ts="2026-10-24T04:15:05+00:00")
    assert runner.options_bridge.executed_count == 1

    # Stop-out 3 s later (a ₹700 spot jump is outside the declared bracket).
    assert runner.on_tick("NIFTY", price=25200.0, ts="2026-10-24T04:15:08+00:00")
    assert runner.options_bridge.closed_count == 1
    # Not the cooldown holding the door: pretend it elapsed.
    runner.options_bridge._last_exit_mono -= 61.0

    churn_start = datetime(2026, 10, 24, 4, 15, 15, tzinfo=timezone.utc)
    for step in range(60):
        stamp = (churn_start + timedelta(seconds=step * 10)).isoformat()
        runner.on_tick("NIFTY", price=25200.0, ts=stamp)

    assert runner.options_bridge.executed_count == 1, "off-boundary re-entry"

    # The next genuine 15-minute bucket (10:00 IST = 04:30 UTC) trades again.
    assert runner.on_tick("NIFTY", price=25200.0, ts="2026-10-24T04:30:05+00:00")
    assert runner.options_bridge.executed_count == 2


# --------------------------------------------------------------------------- #
# 2. Every option fill on the exchange grid (BUG-06)
# --------------------------------------------------------------------------- #


class _DepthStub:
    """Two-sided rows like the live WS depth cache, wrapped by ``BidAskQuoteProvider``."""

    source_name = "live:mstock"

    def __init__(self, rows: dict[str, dict]) -> None:
        self.rows = {str(k): dict(v) for k, v in rows.items()}

    def get_quote(self, instrument_token: str) -> dict:
        return dict(self.rows[str(instrument_token)])


class _MidStub:
    """A one-number feed (replay / OHLCV / LTP-only): no side to cross."""

    source_name = "replay:ohlcv"

    def __init__(self, mids: dict[str, Decimal]) -> None:
        self.mids = {str(k): v for k, v in mids.items()}

    def get_quote(self, instrument_token: str) -> dict:
        mid = self.mids[str(instrument_token)]
        return {"ltp": float(mid), "bid": 0.0, "ask": 0.0}


def _provider(rows: dict[str, dict]):
    return BidAskQuoteProvider(_DepthStub(rows))


def _intent(token: str, side: str, lots: int = 1, lot_size: int = 65) -> TradeIntent:
    leg = OptionLeg(
        instrument_token=token,
        trading_symbol=f"NIFTY26DEC24500{'CE' if side == 'BUY' else 'PE'}",
        side=side,
        quantity=lots,
        lot_size=lot_size,
    )
    view = MarketView(
        direction=Direction.BULLISH if side == "BUY" else Direction.NEUTRAL,
        confidence=0.7,
        underlying="NIFTY",
        spot_price=Decimal("24500"),
    )
    return TradeIntent(
        view=view,
        structure_type="long_call" if side == "BUY" else "short_straddle",
        legs=(leg,),
        expiry=EXPIRY,
        strategy_name="invariants",
        metadata={"option_type": "CE" if side == "BUY" else "PE", "strike": "24500"},
    )


def _broker() -> OptionPaperBroker:
    return OptionPaperBroker(
        capital=5_000_000.0,
        allowed_structures=RESEARCH_STRUCTURES,
        fee_calculator=CommissionCalculator.for_broker("zerodha"),
    )


def on_grid(price) -> bool:
    return Decimal(price) % TICK == Decimal("0")


def test_every_option_fill_lands_on_the_tick_grid():
    """>=50 entries and exits through the book: no booked price may leave ₹0.05."""
    rng = random.Random(20261029)
    checked = 0
    for index in range(30):
        side = "BUY" if index % 2 == 0 else "SELL"
        mid = Decimal(rng.randrange(500, 150_000)) / Decimal(100)
        price = mid + Decimal(str(rng.choice(["0.01", "0.02", "0.03", "0.04", "0.00"])))
        token = f"T{index:03d}"

        if index % 3 == 0:
            provider = _MidStub({token: price})
        else:
            provider = _provider(
                {
                    token: {
                        "ltp": float(mid),
                        "bid": float(price - Decimal("0.35")),
                        "ask": float(price),
                    }
                }
            )

        broker = _broker()
        positions = broker.execute_structure(_intent(token, side), provider)
        leg = positions[0]
        assert on_grid(leg.entry_price), (index, side, leg.entry_price)
        checked += 1

        moved = price + (Decimal("1.5") if side == "BUY" else Decimal("-1.2"))
        if isinstance(provider, _MidStub):
            provider.mids[token] = moved
        else:
            provider.inner.rows[token].update(
                {"ltp": float(moved), "bid": float(moved - Decimal("0.35")), "ask": float(moved)}
            )
        broker.close_structure(leg.structure_id, provider)
        assert on_grid(leg.current_price), (index, side, leg.current_price)
        checked += 1

    assert checked >= 50


# --------------------------------------------------------------------------- #
# 3. Fee-honest round trips on both books (BUG-26 / BUG-04)
# --------------------------------------------------------------------------- #


def _bars(closes, symbol="RELIANCE"):
    base = date(2026, 9, 10)
    return [
        {
            "ts": f"{(base + timedelta(days=i)).isoformat()}T09:15:00",
            "open": close - 1,
            "high": close + 5,
            "low": close - 5,
            "close": float(close),
            "volume": 1000,
        }
        for i, close in enumerate(closes)
    ]


def test_equity_round_trip_pays_its_costs_and_says_why_it_closed():
    runner = StrategyRunner(
        RunnerConfig(
            name="EQ",
            strategy_name="buy_and_hold",
            allocated_capital=1_000_000,
            symbols=["RELIANCE"],
            timeframe="1min",
            mode="paper",
            source="synthetic",
        ),
        ledger=OrderLedger(),
    )
    runner.start()
    for bar in _bars([100.0] * 14):
        runner.process_candle_event("RELIANCE", bar)
    assert runner.positions, "fixture failed to open a position"

    runner.last_price["RELIANCE"] = 110.0
    runner.close_position("RELIANCE", reason="manual_target")
    closed = [p for p in runner.portfolio.closed_positions if p.symbol == "RELIANCE"]
    assert closed
    position = closed[-1]

    assert position.commission_total > 0
    assert (
        position.brokerage_total + position.statutory_fees_total
        == position.commission_total
    )
    assert position.net_pnl == position.realized_pnl - position.commission_total
    assert position.exit_reason, "a closed trade that cannot say why it closed"
    assert _map_exit_reason(position.exit_reason) in DB_EXIT_REASONS


def test_option_round_trip_nets_its_own_cost_stack():
    bridge = _bridge(fee_calculator=CommissionCalculator.for_broker("zerodha"))
    assert _open_structure(bridge).get("structure_id")
    structure_id = bridge.open_structure_id

    bridge.option_broker.close_structure(
        structure_id, bridge.quote_provider, reason="manual_target"
    )

    closed = [
        s
        for s in bridge.option_broker.get_closed_structures()
        if s.structure_id == structure_id
    ]
    assert closed, "close did not land in closed_structures"
    structure = closed[-1]

    assert structure.total_commission > 0
    assert structure.statutory_fees > 0
    assert structure.net_pnl == (
        structure.total_realized_pnl - structure.total_commission - structure.statutory_fees
    )
    assert _map_exit_reason(structure.exit_reason) in DB_EXIT_REASONS


# --------------------------------------------------------------------------- #
# 4. Intraday books square off on the session clock (BUG-39)
# --------------------------------------------------------------------------- #


def test_intraday_option_book_is_flat_at_1510_with_no_tick_in_between():
    bridge = _bridge(timeframe="15min")
    assert bridge.carry_policy == CARRY_INTRADAY
    assert _open_structure(bridge).get("structure_id")

    event = bridge.eod_squareoff(_ist(15, 10, 5))
    assert event is not None and event["reason"] == EXIT_INTRADAY_SQUAREOFF
    assert bridge._has_open_structure() is False
    # The trades table has to be able to store this exit as what it was.
    assert _map_exit_reason(EXIT_INTRADAY_SQUAREOFF) == "eod_flat"


def test_a_deliberate_positional_book_is_left_open():
    bridge = _bridge({"carry_policy": "positional"}, timeframe="15min")
    assert _open_structure(bridge).get("structure_id")
    assert bridge.eod_squareoff(_ist(15, 25, 0)) is None
    assert bridge._has_open_structure() is True


# --------------------------------------------------------------------------- #
# 5. No entry outside the session window (BUG-18)
# --------------------------------------------------------------------------- #


class _LiveLabelledProvider(SyntheticQuoteProvider):
    """Synthetic pricing wearing the label the window reads (``live:*``).

    Only a broker feed's stamps *are* the exchange clock, so only they are
    governed; this lets the rule be exercised without a broker session.
    """

    @property
    def source_name(self) -> str:  # noqa: D401 - label only
        return "live:testing"


def _live_bridge(**kwargs) -> OptionsBridge:
    return OptionsBridge(
        capital=2_000_000.0,
        expression=dict(V2),
        quote_provider=_LiveLabelledProvider(SyntheticChainGenerator()),
        risk_gate=wide_gate(),
        **kwargs,
    )


@pytest.mark.parametrize(
    "hour,minute,second,fragment",
    [
        (9, 14, 59, "before the 09:15:00 open"),
        (15, 30, 30, "after the 15:30:00 close"),
        (15, 25, 1, "after the 15:25 entry cut-off"),
    ],
)
def test_a_live_bar_outside_the_session_window_cannot_open_a_position(
    hour, minute, second, fragment
):
    bridge = _live_bridge(timeframe="1day")
    result = _open_structure(bridge, hour=hour, minute=minute, second=second)
    assert result.get("code") == "OUTSIDE_ENTRY_WINDOW"
    assert fragment in result["reason"]
    assert bridge._has_open_structure() is False


def test_a_live_bar_inside_the_window_still_trades():
    bridge = _live_bridge(timeframe="1day")
    assert _open_structure(bridge, hour=10).get("structure_id")
