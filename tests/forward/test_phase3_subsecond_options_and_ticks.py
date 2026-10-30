"""Phase 3 tests: sub-second ``OptionsBridge.on_tick()``, tick-cadence entries,
``LiveMinuteBarBuilder``, ``HotStrikeRing`` leg pinning, and option ``session_date`` roll.

Covers:
  - Phase 2 reviewer nit: public ``MStockBroker.ws_subscribed_tokens()`` +
    tick-listener survival across TOTP re-auth.
  - Recon Finding F3 + Caveat Q-3 + OPT-5: sub-second ``OptionsBridge.on_tick()``
    protective exits without advancing ``bars_in_trade`` or firing ``signal_flip``;
    stale/failed mark suppression; ``HotStrikeRing`` 1:1 leg pin/unpin; ``>= 20`` min
    intraday EOD square-off floor.
  - Caveats Q-2, Q-4, Q-7: ``evaluation_cadence="tick"`` on ``CE_instant_buy`` /
    ``PE_instant_buy`` / ``immediate_strangle``, strict ``_forming_bar`` isolation,
    atomic ``_entry_in_flight`` lock, ``reentry_cooldown_seconds``, and ``09:15``
    stabilization + ``max_bid_ask_spread_pct`` guard.
  - Caveat Q-5: ``LiveMinuteBarBuilder`` genuine 1-min OHLCV bars + REST ``_last_ts``
    dedupe, ``MStockLiveFeed.latest_bar("NIFTY")`` intraday-first routing, and
    ``PortfolioManager._on_ws_tick_batch`` spot + held-leg dispatch.
  - Caveat OPT-6 + Recon Findings F4/F5: IST ``session_date`` stamping on closed
    ``StructurePosition``s, midnight roll, legacy snapshot backfill, and
    ``gross_pnl`` / ``net_pnl`` / ``commission`` reconciliation.
"""

from __future__ import annotations

import threading
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from backtest.brokers.mstock import MStockBroker
from backtest.data.mstock_live_feed import MStockLiveFeed
from backtest.data.mstock_ws_wrapper import MStockWebSocketWrapper
from backtest.forward.feed_registry import (
    LiveMinuteBarBuilder,
    MStockBarFeed,
    reset_data_bus,
)
from backtest.forward.options_bridge import EXIT_INTRADAY_SQUAREOFF, OptionsBridge
from backtest.forward.paper_runner import (
    OrderLedger,
    RunnerConfig,
    StrategyRunner,
    infer_evaluation_cadence,
)
from backtest.forward.portfolio_manager import PortfolioManager
from backtest.forward.state_store import capture_runner, restore_runner
from backtest.instruments.base import ExerciseType, SettlementType
from backtest.instruments.option import OptionContract
from backtest.options.quote_providers import (
    BidAskQuoteProvider,
    HotStrikeRing,
    LiveChainProvider,
)
from backtest.plugins import discover_plugins
from backtest.strategy.registry import get_strategy

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture(autouse=True)
def _testing_profile(monkeypatch):
    monkeypatch.setenv("BACKTEST_DATA_PROFILE", "testing")
    monkeypatch.delenv("MSTOCK_MAX_BID_ASK_SPREAD_PCT", raising=False)
    monkeypatch.delenv("OPTIONS_INTRADAY_SQUAREOFF_MINUTES", raising=False)
    discover_plugins()
    reset_data_bus()
    yield
    reset_data_bus()


def _make_chain_contracts(
    underlying: str = "NIFTY",
    center: int = 24500,
    step: int = 50,
    half_steps: int = 15,
    expiry: date | None = None,
    lot_size: int = 65,
) -> list[OptionContract]:
    exp = expiry or (date.today() + timedelta(days=5))
    contracts: list[OptionContract] = []
    tok_seq = 50000
    for i in range(-half_steps, half_steps + 1):
        strike = center + i * step
        for opt_type in ("CE", "PE"):
            tok_seq += 1
            contracts.append(
                OptionContract(
                    instrument_token=str(tok_seq),
                    trading_symbol=f"{underlying}{exp:%d%b}{strike}{opt_type}".upper(),
                    underlying=underlying,
                    exchange="NSE",
                    segment="NFO",
                    expiry=exp,
                    strike=Decimal(str(strike)),
                    option_type=opt_type,
                    lot_size=lot_size,
                    tick_size=Decimal("0.05"),
                    contract_type=ExerciseType.EUROPEAN,
                    settlement_type=SettlementType.CASH,
                )
            )
    return contracts


class _LiveWSBrokerStub:
    """Deterministic in-memory WS broker stub exposing public Phase 2/3 seams."""

    broker_name = "mstock"

    def __init__(self, contracts: list[OptionContract]) -> None:
        self._contracts = list(contracts)
        self._ws_feed = object()
        self._subscribed: set[int] = set()
        self._pins: dict[int, int] = {}
        self._quotes: dict[str, dict] = {}
        self._reinit_listeners = []
        self._tick_listeners = []
        now_mono = time.monotonic()
        now_utc = datetime.now(timezone.utc).isoformat()
        for c in contracts:
            dist = abs(float(c.strike) - 24500.0)
            mid = max(15.0, 120.0 - dist * 0.15)
            self._quotes[str(c.instrument_token)] = {
                "ltp": round(mid, 2),
                "bid": round(mid - 0.15, 2),
                "ask": round(mid + 0.15, 2),
                "recv_mono": now_mono,
                "recv_utc": now_utc,
                "source": "ws",
            }

    def ws_subscribed_tokens(self) -> set[int]:
        return set(self._subscribed)

    def add_ws_reinit_listener(self, cb) -> None:
        self._reinit_listeners.append(cb)

    def add_tick_listener(self, cb) -> None:
        if cb not in self._tick_listeners:
            self._tick_listeners.append(cb)

    def remove_tick_listener(self, cb) -> None:
        if cb in self._tick_listeners:
            self._tick_listeners.remove(cb)

    def emit_tick_batch(self, batch: dict[str, dict]) -> None:
        for tok, q in batch.items():
            self._quotes[str(tok)] = dict(q)
        for cb in list(self._tick_listeners):
            cb(batch)

    def subscribe_tokens_ws(self, tokens, mode: str = "full", **kwargs) -> list[int]:
        added = [int(t) for t in tokens]
        self._subscribed.update(added)
        return added

    def unsubscribe_tokens_ws(self, tokens) -> list[int]:
        removed = []
        for raw in tokens:
            t = int(raw)
            if self._pins.get(t, 0) <= 0 and t in self._subscribed:
                self._subscribed.discard(t)
                removed.append(t)
        return removed

    def pin_ws_tokens(self, tokens, mode: str = "full") -> list[int]:
        out = []
        for raw in tokens:
            t = int(raw)
            self._pins[t] = self._pins.get(t, 0) + 1
            self._subscribed.add(t)
            out.append(t)
        return out

    def unpin_ws_tokens(self, tokens) -> list[int]:
        out = []
        for raw in tokens:
            t = int(raw)
            cur = self._pins.get(t, 0)
            if cur <= 1:
                self._pins.pop(t, None)
                out.append(t)
            else:
                self._pins[t] = cur - 1
        return out

    def get_option_chain(self, underlying: str, expiry=None):
        return [c for c in self._contracts if c.underlying == underlying.upper()]

    def get_ws_quote(self, token: int | str, max_age_seconds: float = 30.0):
        row = self._quotes.get(str(token))
        return dict(row) if row is not None else None

    def get_option_quote(self, symbol: str, numeric_token: int | str | None = None):
        if numeric_token is not None:
            row = self._quotes.get(str(numeric_token))
            if row is not None:
                return dict(row)
        row = self._quotes.get(str(symbol))
        if row is not None:
            return dict(row)
        for c in self._contracts:
            if c.trading_symbol == symbol:
                found = self._quotes.get(str(c.instrument_token))
                if found is not None:
                    return dict(found)
        return {}

    def get_option_quotes_bulk(self, symbols):
        return {}


# ---------------------------------------------------------------------------
# 1. Phase 2 Reviewer Nit + Tick Listener Re-Auth Survival
# ---------------------------------------------------------------------------


class TestPhase2AccessorNitAndTickBusPersistence:
    def test_ws_subscribed_tokens_public_accessor_repairs_ring_without_private_dict(self):
        broker = MStockBroker()
        broker._ws_subscribed_tokens.update({26000, 50001})
        snap = broker.ws_subscribed_tokens()
        assert snap == {26000, 50001}
        snap.add(99999)
        assert 99999 not in broker.ws_subscribed_tokens()

        # Stub has ONLY public ws_subscribed_tokens() — NO _ws_subscribed_tokens attr!
        contracts = _make_chain_contracts()
        stub = _LiveWSBrokerStub(contracts)
        assert "_ws_subscribed_tokens" not in stub.__dict__
        ring = HotStrikeRing(stub, half_width=2)
        first = ring.recenter("NIFTY", 24500.0, contracts)
        assert len(first["subscribed"]) == 10

        # Simulate broker socket losing subscriptions while ATM stays at 24500
        stub._subscribed.clear()
        repaired = ring.recenter("NIFTY", 24508.0, contracts)
        assert len(repaired["subscribed"]) == 10
        assert len(stub.ws_subscribed_tokens()) == 10

    def test_broker_tick_listeners_survive_totp_reinit(self, monkeypatch):
        class _FakeMTicker:
            def __init__(self, *a, **kw):
                self.on_ticks = None
                self.on_connect = None
                self.on_close = None
                self.on_open = None

            def send_login_after_connect(self):
                pass

            def subscribe(self, tokens):
                pass

            def set_mode(self, mode, tokens):
                pass

            def connect(self, threaded=True):
                if self.on_connect:
                    self.on_connect(self, {})
                if self.on_open:
                    self.on_open(self)

            def close(self):
                pass

        monkeypatch.setenv("MSTOCK_API_KEY", "test-key")
        import backtest.data.mticker as mticker_mod

        monkeypatch.setattr(mticker_mod, "MTicker", _FakeMTicker)

        broker = MStockBroker()
        received: list[dict[str, dict]] = []
        done = threading.Event()

        def _listener(batch: dict[str, dict]) -> None:
            received.append(batch)
            done.set()

        # Register BEFORE first WS init
        broker.add_tick_listener(_listener)
        broker._init_websocket_feed("token-1")
        assert isinstance(broker._ws_feed, MStockWebSocketWrapper)

        # Re-init WS (simulating TOTP re-auth mid-session)
        broker._init_websocket_feed("token-2")
        try:
            broker._ws_feed._coalesce_s = 0.01
            broker._ws_feed._ticker.on_ticks(
                broker._ws_feed._ticker,
                [{"instrument_token": 26000, "last_price": 24555.0, "mode": "full"}],
            )
            assert done.wait(timeout=2.0)
            assert received[-1]["26000"]["ltp"] == 24555.0
        finally:
            broker._ws_feed.close()


# ---------------------------------------------------------------------------
# 2. Sub-Second OptionsBridge.on_tick() (Fix F3, Caveat Q-3, OPT-5)
# ---------------------------------------------------------------------------


class TestSubSecondOptionsBridgeOnTick:
    def _build_live_strangle_bridge(
        self,
        contracts: list[OptionContract],
        *,
        extra_expr: dict | None = None,
    ) -> tuple[OptionsBridge, _LiveWSBrokerStub, BidAskQuoteProvider]:
        stub = _LiveWSBrokerStub(contracts)
        lcp = LiveChainProvider(stub)
        lcp.set_spot("NIFTY", 24500.0)
        provider = BidAskQuoteProvider(lcp)
        spec = get_strategy("immediate_strangle").execution_spec().to_expression()
        if extra_expr:
            spec = {**spec, **extra_expr}
        bridge = OptionsBridge(
            capital=500_000,
            expression=spec,
            mode="paper",
            quote_provider=provider,
        )
        return bridge, stub, provider

    def test_f3_strangle_stop_triggers_subsecond_on_tick_without_bar_close(self):
        """Reproduces 2026-10-23 Trade #9: the declared stop exits intra-bar, not at bar close.

        BUG-02 (QA 2026-10-28) moved the strangle bracket from ±₹100 to
        ₹2,000 stop / ₹1,000 target, because ₹100 sat inside the ₹193 round-trip
        fee — so this test now proves BOTH halves of the fix: a ₹117 quote bounce
        rides through instead of stopping out, and the tick path still catches the
        real move (₹2,067) long before the bar-close blowout.
        """
        contracts = _make_chain_contracts(center=24500, half_steps=20, lot_size=65)
        bridge, stub, _ = self._build_live_strangle_bridge(contracts)
        strat = get_strategy("immediate_strangle")()

        # Bar 1 at 13:33:00 opens short strangle
        bridge.on_bar("NIFTY", 24500.0, "2026-10-24 13:33:00")
        bar_idx_at_open = bridge._bar_index
        bar_row = {
            "ts": "2026-10-24 13:33:00",
            "open": 24500,
            "high": 24500,
            "low": 24500,
            "close": 24500,
            "volume": 100,
        }
        view = strat.generate_market_view(StrategyRunner._bars_to_frame([bar_row]))
        intent = strat.build_opening_intent(view)
        opened = bridge.on_intent(intent, view, "immediate_strangle")
        assert opened and opened.get("structure_id")

        structure = bridge.option_broker.get_open_structures()[0]
        ce_leg = [leg for leg in structure.legs if leg.option_type == "CE"][0]
        # Tighten spread near flat first, then widen CE ask by +1.8 pts
        # (65 units * 1.80 = ₹117) — inside the fee-aware stop, so it rides.
        for leg in structure.legs:
            entry_px = float(leg.entry_price)
            stub._quotes[leg.instrument_token].update(
                {"bid": entry_px - 0.1, "ask": entry_px + 0.1, "ltp": entry_px}
            )
        bridge.on_tick("NIFTY", 24505.0, "2026-10-24T08:03:10+00:00")
        assert bridge._has_open_structure()

        # Intra-bar spike: CE ask rises +31.80 pts (65 units * 31.80 = ₹2,067
        # loss > the ₹2,000 stop), catching the move BEFORE the bar close, where
        # a +73 pt spot move would have booked a far larger loss.
        ce_entry = float(ce_leg.entry_price)
        stub._quotes[ce_leg.instrument_token].update(
            {"bid": ce_entry + 31.6, "ask": ce_entry + 31.8, "ltp": ce_entry + 31.7}
        )
        t0 = time.perf_counter()
        bridge.on_tick("NIFTY", 24520.0, "2026-10-24T08:03:12+00:00")
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        assert elapsed_ms < 250.0
        assert not bridge._has_open_structure()
        assert bridge._bar_index == bar_idx_at_open  # bar counter NEVER advanced on tick!
        exit_ev = bridge.pop_exit_event()
        assert exit_ev is not None
        assert exit_ev["reason"] == "stop_loss"
        assert -2400.0 < exit_ev["pnl"] <= -2000.0  # bounded near -₹2,067, not the blowout

    def test_on_tick_never_advances_bars_in_trade_or_triggers_time_stop(self):
        """Caveat Q-3: 25 intra-bar ticks must not advance bars_in_trade or trip max_bars."""
        contracts = _make_chain_contracts(center=24500, half_steps=15)
        stub = _LiveWSBrokerStub(contracts)
        lcp = LiveChainProvider(stub)
        lcp.set_spot("NIFTY", 24500.0)
        provider = BidAskQuoteProvider(lcp)

        spec = get_strategy("banknifty_ema_momentum").execution_spec().to_expression()
        spec["exit"] = {
            **spec["exit"],
            "max_bars": 2,
            "signal_flip": True,
        }
        bridge = OptionsBridge(
            capital=500_000,
            expression=spec,
            mode="paper",
            quote_provider=provider,
        )
        bridge.on_bar("NIFTY", 24500.0, "2026-10-24 10:00:00")
        from backtest.strategy.intent import Direction, MarketView

        view = MarketView(
            direction=Direction.BULLISH,
            confidence=1.0,
            underlying="NIFTY",
            spot_price=24500.0,
            bar_timestamp="2026-10-24 10:00:00",
        )
        res = bridge.on_intent(
            {"structure": "long_call", "strike_selection": "atm", "quantity": 1},
            view,
            "banknifty_ema_momentum",
        )
        assert res and res.get("structure_id")

        for i in range(25):
            bridge.on_tick("NIFTY", 24500.0 + (i % 3), f"2026-10-24T04:30:{i:02d}+00:00")

        assert bridge._has_open_structure()
        assert bridge.bars_in_trade == 0
        assert bridge.pop_exit_event() is None

    def test_on_tick_suppresses_stops_when_mark_untrusted(self):
        """OPT-5 / PRD §4.2: failed/one-sided leg depth must suppress price-driven stops."""
        contracts = _make_chain_contracts(center=24500, half_steps=20)
        bridge, stub, _ = self._build_live_strangle_bridge(contracts)
        strat = get_strategy("immediate_strangle")()
        bridge.on_bar("NIFTY", 24500.0, "2026-10-24 10:00:00")
        row = {
            "ts": "2026-10-24 10:00:00",
            "open": 24500,
            "high": 24500,
            "low": 24500,
            "close": 24500,
            "volume": 100,
        }
        df = StrategyRunner._bars_to_frame([row])
        view = strat.generate_market_view(df)
        bridge.on_intent(strat.build_opening_intent(view), view, "immediate_strangle")
        structure = bridge.option_broker.get_open_structures()[0]

        # Arm an aggressive manual stop AND corrupt one leg's ask to 0 (one-sided book)
        bridge.set_manual_rule(stop_loss=0.01)
        leg0 = structure.legs[0]
        stub._quotes[leg0.instrument_token]["ask"] = 0.0
        bridge.on_tick("NIFTY", 24600.0, "2026-10-24T04:30:15+00:00")

        assert structure.mtm_failures >= 1
        assert bridge._has_open_structure()
        assert bridge.pop_exit_event() is None

    def test_open_structure_pins_and_unpins_legs_1_to_1_on_hot_strike_ring(self):
        contracts = _make_chain_contracts(center=24500, half_steps=20)
        bridge, stub, _ = self._build_live_strangle_bridge(contracts)
        strat = get_strategy("immediate_strangle")()
        bridge.on_bar("NIFTY", 24500.0, "2026-10-24 10:00:00")
        row = {
            "ts": "2026-10-24 10:00:00",
            "open": 24500,
            "high": 24500,
            "low": 24500,
            "close": 24500,
            "volume": 100,
        }
        df = StrategyRunner._bars_to_frame([row])
        view = strat.generate_market_view(df)
        bridge.on_intent(strat.build_opening_intent(view), view, "immediate_strangle")

        structure = bridge.option_broker.get_open_structures()[0]
        leg_tokens = [int(leg.instrument_token) for leg in structure.legs]
        for tok in leg_tokens:
            assert stub._pins.get(tok) == 1
            assert tok in stub.ws_subscribed_tokens()

        # Move spot +600 pts away: pinned open legs MUST stay subscribed on WS
        bridge.on_tick("NIFTY", 25100.0, "2026-10-24T04:35:00+00:00")
        for tok in leg_tokens:
            assert tok in stub.ws_subscribed_tokens()

        # Flatten closes structure -> pins drop 1-for-1 to zero
        assert bridge.flatten_all() == 1
        for tok in leg_tokens:
            assert stub._pins.get(tok, 0) == 0

    def test_intraday_squareoff_enforces_20min_rms_floor_and_closes_at_1510_ist(self):
        contracts = _make_chain_contracts(center=24500, half_steps=20)
        # Requesting 10m is clamped up to 20m (15:10 IST) per OPT-5
        bridge, stub, _ = self._build_live_strangle_bridge(
            contracts,
            extra_expr={"intraday_squareoff_minutes_before": 10},
        )
        assert bridge.intraday_squareoff_minutes_before == 20

        strat = get_strategy("immediate_strangle")()
        bridge.on_bar("NIFTY", 24500.0, "2026-10-24 14:30:00")
        df = StrategyRunner._bars_to_frame(
            [
                {
                    "ts": "2026-10-24 14:30:00",
                    "open": 24500,
                    "high": 24500,
                    "low": 24500,
                    "close": 24500,
                    "volume": 100,
                }
            ]
        )
        view = strat.generate_market_view(df)
        bridge.on_intent(strat.build_opening_intent(view), view, "immediate_strangle")
        for leg in bridge.option_broker.get_open_structures()[0].legs:
            px = float(leg.entry_price)
            stub._quotes[leg.instrument_token].update({"bid": px - 0.05, "ask": px + 0.05})

        # 15:09:30 IST (09:39:30 UTC) -> still open
        bridge.on_tick("NIFTY", 24500.0, "2026-10-24T09:39:30+00:00")
        assert bridge._has_open_structure()

        # 15:10:01 IST (09:40:01 UTC) -> squared off ahead of 15:15 RMS window!
        bridge.on_tick("NIFTY", 24500.0, "2026-10-24T09:40:01+00:00")
        assert not bridge._has_open_structure()
        ev = bridge.pop_exit_event()
        assert ev is not None and ev["reason"] == EXIT_INTRADAY_SQUAREOFF


# ---------------------------------------------------------------------------
# 3. Tick-Cadence Entries & Entry Guards (Q-2, Q-4, Q-7)
# ---------------------------------------------------------------------------


class TestTickCadenceEntriesAndGuards:
    def test_evaluation_cadence_inference_for_instant_structure_and_indicator_strategies(self):
        assert infer_evaluation_cadence(get_strategy("CE_instant_buy")()) == "tick"
        assert infer_evaluation_cadence(get_strategy("PE_instant_buy")()) == "tick"
        assert infer_evaluation_cadence(get_strategy("immediate_strangle")()) == "tick"
        assert infer_evaluation_cadence(get_strategy("banknifty_ema_momentum")()) == "bar_close"
        assert infer_evaluation_cadence(get_strategy("ema_pullback")()) == "bar_close"

    @pytest.mark.parametrize("strategy_name", ["CE_instant_buy", "immediate_strangle"])
    def test_instant_and_structure_runners_enter_on_first_tick_without_mutating_bars(
        self, strategy_name: str
    ):
        runner = StrategyRunner(
            RunnerConfig(
                name=f"TICK-{strategy_name}",
                strategy_name=strategy_name,
                allocated_capital=500_000,
                symbols=["NIFTY"],
                timeframe="1hour",
                mode="paper",
                source="synthetic",
                instrument={
                    "type": "option",
                    "expression": get_strategy(strategy_name).execution_spec().to_expression(),
                },
            ),
            ledger=OrderLedger(),
        )
        runner.start()
        assert runner.evaluation_cadence == "tick"
        assert len(runner._bars["NIFTY"]) == 0

        res = runner.on_tick("NIFTY", price=24500.0, ts="2026-10-24T04:44:23+00:00")
        assert res is not None and res.get("structure_id")
        assert runner.options_bridge.executed_count == 1
        # Caveat Q-2: _bars remains strictly closed timeframe bars (0 bars yet)!
        assert len(runner._bars["NIFTY"]) == 0
        assert runner._forming_bar["NIFTY"]["close"] == 24500.0

    def test_bar_close_strategy_never_enters_intrabar_on_ticks(self):
        spec = get_strategy("banknifty_ema_momentum").execution_spec().to_expression()
        runner = StrategyRunner(
            RunnerConfig(
                name="EMA-OPT",
                strategy_name="banknifty_ema_momentum",
                allocated_capital=500_000,
                symbols=["BANKNIFTY"],
                timeframe="15min",
                mode="paper",
                source="synthetic",
                instrument={"type": "option", "expression": spec},
            ),
            ledger=OrderLedger(),
        )
        runner.start()
        seed = [
            {
                "ts": f"2026-10-23 10:{i:02d}:00",
                "open": 51000.0 + i * 10,
                "high": 51020.0 + i * 10,
                "low": 50990.0 + i * 10,
                "close": 51010.0 + i * 10,
                "volume": 1000.0,
            }
            for i in range(40)
        ]
        runner.seed_history({"BANKNIFTY": seed}, target_bars=40, quorum_bars=30)
        assert runner.evaluation_cadence == "bar_close"

        for step in range(5):
            out = runner.on_tick(
                "BANKNIFTY",
                price=51600.0 + step * 25,
                ts=f"2026-10-24T04:45:1{step}+00:00",
            )
            assert out is None

        assert runner.options_bridge.executed_count == 0
        assert len(runner._bars["BANKNIFTY"]) == 40

    def test_atomic_entry_in_flight_prevents_concurrent_tick_double_fill(self):
        runner = StrategyRunner(
            RunnerConfig(
                name="RACE",
                strategy_name="CE_instant_buy",
                allocated_capital=500_000,
                symbols=["NIFTY"],
                timeframe="1min",
                mode="paper",
                source="synthetic",
                instrument={
                    "type": "option",
                    "expression": get_strategy("CE_instant_buy").execution_spec().to_expression(),
                },
            ),
            ledger=OrderLedger(),
        )
        runner.start()

        # Verify _try_begin_entry is strictly single-flight across threads
        entered_first = threading.Event()
        release_first = threading.Event()
        second_attempt_result: list[bool] = []

        def _holder() -> None:
            assert runner._try_begin_entry() is True
            entered_first.set()
            release_first.wait(timeout=2.0)
            runner._end_entry()

        t = threading.Thread(target=_holder)
        t.start()
        assert entered_first.wait(timeout=2.0)
        second_attempt_result.append(runner._try_begin_entry())
        # While in-flight, a tick entry attempt cleanly no-ops without blocking
        in_flight_res = runner._process_option_tick_entry(
            "NIFTY", 24500.0, "2026-10-24T04:45:10+00:00"
        )
        assert in_flight_res is None
        release_first.set()
        t.join(timeout=2.0)

        assert second_attempt_result == [False]
        assert runner.options_bridge.executed_count == 0
        # Once released, normal tick entry succeeds cleanly
        res = runner.on_tick("NIFTY", price=24500.0, ts="2026-10-24T04:45:11+00:00")
        assert res is not None and runner.options_bridge.executed_count == 1

    def test_reentry_cooldown_blocks_machine_gun_reentry_after_tick_stopout(self):
        expr = {
            **get_strategy("immediate_strangle").execution_spec().to_expression(),
            "reentry_cooldown_seconds": 60.0,
        }
        runner = StrategyRunner(
            RunnerConfig(
                name="COOLDOWN",
                strategy_name="immediate_strangle",
                allocated_capital=500_000,
                symbols=["NIFTY"],
                timeframe="1hour",
                mode="paper",
                source="synthetic",
                instrument={"type": "option", "expression": expr},
            ),
            ledger=OrderLedger(),
        )
        runner.start()
        runner.on_tick("NIFTY", price=24500.0, ts="2026-10-24T04:50:00+00:00")
        assert runner.options_bridge.executed_count == 1

        # Trigger stop-loss intra-bar via large spot jump
        out = runner.on_tick("NIFTY", price=25200.0, ts="2026-10-24T04:50:05+00:00")
        assert out == {"exited": True}
        assert runner.options_bridge.closed_count == 1

        # Immediate subsequent ticks within 60s cooldown MUST NOT re-enter
        for sec in (6, 7, 15, 30):
            runner.on_tick("NIFTY", price=25200.0, ts=f"2026-10-24T04:50:{sec:02d}+00:00")
        assert runner.options_bridge.executed_count == 1

        # Simulate cooldown elapsed -> still blocked INSIDE the same 1hour bar.
        # BUG-01 (QA 2026-10-28): the cooldown was the only latch, so a runner
        # declared on "1hour" could re-open 6 times a minute. One entry per
        # timeframe bucket now, and the bucket is spent by the deploy entry.
        runner.options_bridge._last_exit_mono = time.monotonic() - 61.0
        same_bucket = runner.on_tick("NIFTY", price=25200.0, ts="2026-10-24T04:51:10+00:00")
        assert same_bucket is None
        assert runner.options_bridge.executed_count == 1

        # Next genuine hourly boundary — 11:00 IST = 05:30 UTC — with the feed
        # still live (ticks 20 s apart, as a real WS stream delivers them).
        runner.on_tick("NIFTY", price=25200.0, ts="2026-10-24T05:29:50+00:00")
        assert runner.options_bridge.executed_count == 1
        reentered = runner.on_tick("NIFTY", price=25200.0, ts="2026-10-24T05:30:10+00:00")
        assert reentered is not None and reentered.get("structure_id")
        assert runner.options_bridge.executed_count == 2

    def test_0915_opening_stabilization_and_spread_sanity_guard(self):
        contracts = _make_chain_contracts(center=24500, half_steps=10)
        stub = _LiveWSBrokerStub(contracts)
        lcp = LiveChainProvider(stub)
        lcp.set_spot("NIFTY", 24500.0)
        provider = BidAskQuoteProvider(lcp)
        expr = {
            **get_strategy("CE_instant_buy").execution_spec().to_expression(),
            "market_open_stabilization_seconds": 5.0,
            "max_bid_ask_spread_pct": 0.25,
        }
        bridge = OptionsBridge(
            capital=500_000,
            expression=expr,
            mode="paper",
            quote_provider=provider,
        )
        strat = get_strategy("CE_instant_buy")()
        df = StrategyRunner._bars_to_frame(
            [
                {
                    "ts": "2026-10-24 09:15:01",
                    "open": 24500,
                    "high": 24500,
                    "low": 24500,
                    "close": 24500,
                    "volume": 10,
                }
            ]
        )
        view = strat.generate_market_view(df)
        intent = strat.build_opening_intent(view)

        # 09:15:02 IST (03:45:02 UTC) -> inside 5s stabilization window -> suppressed!
        assert (
            bridge.on_intent(
                intent,
                view,
                "CE_instant_buy",
                from_tick=True,
                tick_ts="2026-10-24T03:45:02+00:00",
            )
            is None
        )
        assert bridge.executed_count == 0

        # 09:15:06 IST with wild opening spread (bid=20, ask=60 => 100% spread > 25%) -> rejected!
        atm_ce = [c for c in contracts if c.strike == Decimal("24500") and c.option_type == "CE"][0]
        stub._quotes[atm_ce.instrument_token].update({"bid": 20.0, "ask": 60.0, "ltp": 40.0})
        wide_res = bridge.on_intent(
            intent,
            view,
            "CE_instant_buy",
            from_tick=True,
            tick_ts="2026-10-24T03:45:06+00:00",
        )
        assert wide_res is not None and wide_res.get("rejected") is True
        assert bridge.executed_count == 0

        # Tight spread arrives at 09:15:07 IST -> fills cleanly!
        stub._quotes[atm_ce.instrument_token].update({"bid": 119.5, "ask": 120.5, "ltp": 120.0})
        ok_res = bridge.on_intent(
            intent,
            view,
            "CE_instant_buy",
            from_tick=True,
            tick_ts="2026-10-24T03:45:07+00:00",
        )
        assert ok_res is not None and ok_res.get("structure_id")
        assert bridge.executed_count == 1


# ---------------------------------------------------------------------------
# 4. LiveMinuteBarBuilder & PortfolioManager WS Tick Fan-Out (Q-5)
# ---------------------------------------------------------------------------


class TestLiveMinuteBarBuilderAndPortfolioTickFanout:
    def test_live_minute_bar_builder_constructs_true_minute_ohlcv_and_dedupes_rest(
        self, monkeypatch
    ):
        builder = LiveMinuteBarBuilder()
        s1, f1 = builder.ingest_tick(
            "NIFTY", 24500.0, "2026-10-24T04:45:05+00:00", cumulative_volume=10_000
        )
        assert s1 == []
        assert f1 == {
            "ts": "2026-10-24 10:15:00",
            "open": 24500.0,
            "high": 24500.0,
            "low": 24500.0,
            "close": 24500.0,
            "volume": 0.0,
        }
        builder.ingest_tick("NIFTY", 24525.0, "2026-10-24T04:45:25+00:00", cumulative_volume=10_300)
        _, f3 = builder.ingest_tick(
            "NIFTY", 24490.0, "2026-10-24T04:45:40+00:00", cumulative_volume=10_450
        )
        assert f3["high"] == 24525.0 and f3["low"] == 24490.0 and f3["volume"] == 450.0
        builder.ingest_tick("NIFTY", 24515.0, "2026-10-24T04:45:58+00:00", cumulative_volume=10_600)

        # Crossing into 10:16 IST seals 10:15:00 with genuine per-minute OHLCV!
        fake_client = MagicMock(spec=["latest_bar", "emits_minute_bars"])
        fake_client.emits_minute_bars = True
        fake_client.latest_bar.return_value = {
            "ts": "2026-10-24 10:15:00",
            "open": 24000.0,  # bogus session open from REST fallback
            "high": 24900.0,
            "low": 23900.0,
            "close": 24515.0,
            "volume": 0,
        }
        feed = MStockBarFeed(feed_client=fake_client)
        feed.add_symbols(["NIFTY"])
        delivered: list[dict] = []
        feed.on_bar = lambda sym, b: delivered.append(dict(b))
        feed.bar_builder = builder

        emitted, forming_next = feed.ingest_ws_tick(
            "NIFTY",
            {"ltp": 24520.0, "volume": 10_650, "recv_utc": "2026-10-24T04:46:01+00:00"},
            emit_tick_end=False,
        )
        assert len(emitted) == 1
        assert delivered[0]["ts"] == "2026-10-24 10:15:00"
        assert delivered[0]["open"] == 24500.0
        assert delivered[0]["high"] == 24525.0
        assert delivered[0]["low"] == 24490.0
        assert delivered[0]["close"] == 24515.0
        assert delivered[0]["volume"] == 600.0
        assert forming_next["ts"] == "2026-10-24 10:16:00"
        assert forming_next["volume"] == 50.0

        # Subsequent REST poll for 10:15:00 is deduped via shared _last_ts!
        monkeypatch.setattr(MStockBarFeed, "_market_open", staticmethod(lambda: True))
        assert feed._poll_once() == 0
        assert len(delivered) == 1

    def test_mstock_live_feed_prefers_intraday_minute_over_full_day_quote_ohlc(self, monkeypatch):
        monkeypatch.setenv("MSTOCK_API_KEY", "test-api-key")
        monkeypatch.setenv("MSTOCK_BASE_URL", "https://api.mstock.test")
        monkeypatch.setattr("backtest.live.auth.get_session_token", lambda: "sess")

        import backtest.data.mstock_live_feed as feed_mod

        def _never_scriptmaster(*a, **kw):
            raise AssertionError("scriptmaster must never be hit for NIFTY")

        monkeypatch.setattr(feed_mod, "_resolve_scriptmaster_row", _never_scriptmaster)
        urls: list[str] = []

        class _IntradayResp:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "data": {
                        "candles": [
                            ["2026-10-24 13:33:00", 24510.0, 24522.0, 24508.0, 24519.0, 12500]
                        ]
                    }
                }

        def _fake_get(url, *a, **kw):
            urls.append(url)
            assert "/instruments/intraday/1/26000/minute" in url
            return _IntradayResp()

        monkeypatch.setattr("requests.get", _fake_get)
        feed = MStockLiveFeed(base_url="https://api.mstock.test")
        bar = feed.latest_bar("NIFTY")
        assert bar is not None
        assert bar["open"] == 24510.0 and bar["close"] == 24519.0
        assert all("/quote/ohlc" not in u for u in urls)

    def test_portfolio_manager_ws_tick_batch_dispatches_spot_and_held_option_leg_ticks(
        self, monkeypatch, tmp_path
    ):
        contracts = _make_chain_contracts(center=24500, half_steps=20, lot_size=65)
        stub = _LiveWSBrokerStub(contracts)

        import backtest.forward.feed_registry as fr_mod

        monkeypatch.setattr(fr_mod, "_default_quote_broker", lambda source=None: stub)

        mgr = PortfolioManager(
            auto_start_feed=False,
            mstock_feed_client=MagicMock(spec=[]),
            enable_broker_warmup=False,
            async_warmup=False,
            live_broker=stub,
            # Ticket 2.4: isolated by construction, not by the pytest DB
            # firewall — this book writes no ledger rows, cannot reap the real
            # one on boot, and its snapshot lands in this test's own directory.
            state_path=tmp_path / "portfolio_state.json",
            persist_trades=False,
        )
        try:
            spec = get_strategy("immediate_strangle").execution_spec().to_expression()
            instance_id = mgr.add_runner(
                RunnerConfig(
                    name="LIVE-STRANGLE",
                    strategy_name="immediate_strangle",
                    allocated_capital=500_000,
                    symbols=["NIFTY"],
                    timeframe="1hour",
                    mode="paper",
                    source="mstock",
                    instrument={"type": "option", "expression": spec},
                ),
                start=True,
            )
            runner = mgr._runners[instance_id]
            assert mgr._attached_ws_broker is stub

            # 1) Index spot tick ("26000") opens immediate_strangle intra-bar!
            stub.emit_tick_batch(
                {
                    "26000": {
                        "ltp": 24500.0,
                        "volume": 1000,
                        "recv_utc": "2026-10-24T04:45:10+00:00",
                        "source": "ws",
                    }
                }
            )
            assert runner.options_bridge.executed_count == 1
            open_struct = runner.options_bridge.option_broker.get_open_structures()[0]
            ce_leg = [leg for leg in open_struct.legs if leg.option_type == "CE"][0]

            # Normalize mark near flat first
            for leg in open_struct.legs:
                px = float(leg.entry_price)
                stub._quotes[leg.instrument_token].update(
                    {"bid": px - 0.1, "ask": px + 0.1, "ltp": px}
                )

            # 2) Option-leg-only tick (NO "26000" index tick in batch!) spikes CE ask -> stops out!
            # +32 pts on 65 units = ₹2,080 clears the fee-aware ₹2,000 stop (BUG-02).
            ce_px = float(ce_leg.entry_price)
            widened_quote = {
                "ltp": ce_px + 31.9,
                "bid": ce_px + 31.7,
                "ask": ce_px + 32.0,
                "recv_mono": time.monotonic(),
                "recv_utc": "2026-10-24T04:45:12+00:00",
                "source": "ws",
            }
            stub.emit_tick_batch({ce_leg.instrument_token: widened_quote})

            assert not runner.options_bridge._has_open_structure()
            assert runner.options_bridge.closed_count == 1
            assert runner.options_bridge.last_exit["reason"] == "stop_loss"
        finally:
            mgr.shutdown()


# ---------------------------------------------------------------------------
# 5. Option session_date Roll & Commission Reconciliation (OPT-6, F4, F5)
# ---------------------------------------------------------------------------


class TestOptionSessionDateRollAndCommissionAccounting:
    def _open_and_close_option(
        self, runner: StrategyRunner, close_dt: datetime, exit_spot: float = 24300.0
    ):
        runner.on_tick("NIFTY", price=24500.0, ts="2026-10-24T04:45:10+00:00")
        bridge = runner.options_bridge
        assert bridge._has_open_structure()
        sid = bridge.open_structure_id
        bridge._sync_market("NIFTY", exit_spot, close_dt.isoformat())
        bridge.option_broker.close_structure(
            sid,
            bridge.quote_provider,
            timestamp=close_dt,
            reason="stop_loss",
        )
        bridge.open_structure_id = None
        bridge.last_unrealized_pnl = Decimal("0")
        return bridge.option_broker.get_structure(sid)

    def test_closed_option_stamps_ist_session_date_rolls_at_midnight_and_survives_restart(self):
        runner = StrategyRunner(
            RunnerConfig(
                name="OPT-ROLL",
                strategy_name="CE_instant_buy",
                allocated_capital=500_000,
                daily_loss_limit_pct=0.05,
                max_drawdown_pct=0.0,
                symbols=["NIFTY"],
                timeframe="1min",
                mode="paper",
                source="synthetic",
                instrument={
                    "type": "option",
                    "expression": get_strategy("CE_instant_buy").execution_spec().to_expression(),
                },
            ),
            ledger=OrderLedger(),
        )
        runner.start()

        ist_today = datetime.now(IST).date()
        # 1) Close from yesterday (23:30 IST yesterday) -> excluded from today's daily_pnl!
        yesterday_close = datetime.combine(
            ist_today - timedelta(days=1), dt_time(23, 30), tzinfo=IST
        )
        s_prev = self._open_and_close_option(runner, yesterday_close, exit_spot=24200.0)
        assert s_prev.session_date == (ist_today - timedelta(days=1)).isoformat()
        assert runner.daily_pnl() == pytest.approx(0.0, abs=1e-9)

        # 2) Close at 00:20 IST today (18:50 UTC yesterday!) -> stamps TODAY's IST session_date!
        runner.options_bridge._last_exit_mono = None
        after_midnight = datetime.combine(ist_today, dt_time(0, 20), tzinfo=IST)
        s_today = self._open_and_close_option(runner, after_midnight, exit_spot=24350.0)
        assert s_today.session_date == ist_today.isoformat()
        expected_net = s_today.gross_pnl - s_today.total_commission - s_today.statutory_fees
        assert s_today.net_pnl == expected_net
        assert runner.daily_pnl() == pytest.approx(float(s_today.net_pnl), rel=1e-6)

        # 3) Snapshot -> strip session_date on yesterday's trade (legacy Friday state!) -> restore
        snap = capture_runner(runner)
        snap["bridge"]["broker"]["structures"][0].pop("session_date", None)

        revived = StrategyRunner(runner.config, ledger=OrderLedger())
        restore_runner(revived, snap)
        # Legacy Friday close backfills yesterday's session_date; today's loss survives restart!
        assert revived.daily_pnl() == pytest.approx(float(s_today.net_pnl), rel=1e-6)

    def test_f4_trade_record_and_bridge_summary_expose_gross_and_net_pnl(self):
        runner = StrategyRunner(
            RunnerConfig(
                name="OPT-F4",
                strategy_name="CE_instant_buy",
                allocated_capital=500_000,
                symbols=["NIFTY"],
                timeframe="1min",
                mode="paper",
                source="synthetic",
                instrument={
                    "type": "option",
                    "expression": get_strategy("CE_instant_buy").execution_spec().to_expression(),
                },
            ),
            ledger=OrderLedger(),
        )
        runner.start()
        ist_now = datetime.now(IST)
        closed = self._open_and_close_option(runner, ist_now, exit_spot=24650.0)

        trades = runner.closed_option_trades
        assert len(trades) == 1
        rec = trades[0]
        assert rec["session_date"] == ist_now.date().isoformat()
        assert rec["gross_pnl"] == pytest.approx(float(closed.gross_pnl), abs=0.01)
        assert rec["commission"] == pytest.approx(float(closed.total_commission), abs=0.01)
        assert rec["statutory_fees"] == pytest.approx(float(closed.statutory_fees), abs=0.01)
        assert rec["net_pnl"] == pytest.approx(float(closed.net_pnl), abs=0.01)
        assert rec["pnl"] == rec["gross_pnl"]

        summary = runner.options_bridge.summary()
        assert summary["gross_realized_pnl"] == pytest.approx(float(closed.gross_pnl), abs=0.01)
        assert summary["net_realized_pnl"] == pytest.approx(float(closed.net_pnl), abs=0.01)
        assert summary["commission_paid"] == pytest.approx(float(closed.total_commission), abs=0.01)

    def test_open_option_daily_pnl_nets_entry_costs_and_reconciles_with_equity(self):
        """While a structure is open, daily_pnl() must net entry brokerage + statutory fees."""
        runner = StrategyRunner(
            RunnerConfig(
                name="OPT-OPEN-NET",
                strategy_name="CE_instant_buy",
                allocated_capital=500_000,
                symbols=["NIFTY"],
                timeframe="1min",
                mode="paper",
                source="synthetic",
                instrument={
                    "type": "option",
                    "expression": get_strategy("CE_instant_buy").execution_spec().to_expression(),
                },
            ),
            ledger=OrderLedger(),
        )
        runner.start()
        baseline = runner._day_start_equity
        ist_now = datetime.now(IST)
        runner.process_candle_event(
            "NIFTY",
            {
                "ts": ist_now.strftime("%Y-%m-%d %H:%M:%S"),
                "open": 24500.0,
                "high": 24510.0,
                "low": 24490.0,
                "close": 24500.0,
                "volume": 1000,
            },
        )
        open_structures = runner.options_bridge.option_broker.get_open_structures()
        assert len(open_structures) == 1
        entry_costs = float(
            open_structures[0].total_commission + open_structures[0].statutory_fees
        )
        assert entry_costs > 0.0
        assert runner.daily_pnl() == pytest.approx(runner.equity() - baseline, abs=1e-6)

    def test_normalize_bar_rejects_missing_or_nat_ts_across_pandas_versions(
        self, monkeypatch
    ):
        """Pandas 2.x NaT.strftime returns 'NaT' — _normalize_bar must return None."""
        import backtest.forward.feed_registry as fr_mod

        assert MStockBarFeed._normalize_bar({"close": 10.0}) is None
        assert MStockBarFeed._normalize_bar({"ts": "", "close": 10.0}) is None
        assert MStockBarFeed._normalize_bar({"ts": "   ", "close": 10.0}) is None
        assert MStockBarFeed._normalize_bar({"ts": None, "close": 10.0}) is None

        # Simulate Pandas 2.x NaT where strftime() returns literal "NaT" instead of raising
        class _FakePandas2NaT:
            def strftime(self, _fmt: str) -> str:
                return "NaT"

        orig_isna = fr_mod.pd.isna
        monkeypatch.setattr(
            fr_mod.pd,
            "to_datetime",
            lambda *_a, **_kw: _FakePandas2NaT(),
        )
        monkeypatch.setattr(
            fr_mod.pd,
            "isna",
            lambda v: True if isinstance(v, _FakePandas2NaT) else orig_isna(v),
        )
        assert MStockBarFeed._normalize_bar({"ts": "2026-10-24 10:00:00", "close": 10.0}) is None
