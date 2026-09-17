"""F-12 — live equity fills: the idempotent, reconciled order path.

Before this, a ``mode='live'`` runner on the multi-runner forward path
filled through PaperBroker — a live runner could silently paper-trade.
These tests pin the seam:

* **Fail-closed arming** — the gateway refuses to exist without
  ``confirm_live=True`` + ``ALLOW_LIVE_ORDERS`` + an authenticated broker
  (same invariant as the LiveOptionTrader gate);
* **Idempotent placement** — one ``place_order`` per ledger order (coid
  rides to the venue), placement failure cancels locally, a restart
  re-arms POLLING via the P2.4 state file instead of re-placing;
* **Delta-only fills** — the broker reports cumulative filled quantity;
  repeated polls can never double-book (partial fills apply in steps);
* **Shared accounting** — fills land through the SAME ``ledger.apply_fill``
  paper uses, into the runner's own portfolio, at the broker's ACTUAL
  price with zero simulated fees;
* **Reconciliation** — rejected/expired orders at the venue are cancelled
  locally with a WARNING, never silently.
"""

from __future__ import annotations

import logging

import pytest

from backtest.brokers.base import BrokerOrder
from backtest.forward.feed_registry import reset_data_bus
from backtest.forward.live_gateway import LiveEquityGateway, live_orders_allowed
from backtest.forward.paper_runner import (
    ORDER_PENDING,
    SIDE_BUY,
    RunnerConfig,
    StrategyRunner,
)
from backtest.forward.portfolio_manager import PortfolioManager
from backtest.forward.risk_supervisor import GlobalRiskConfig


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeLiveBroker:
    """Deterministic venue: scripted cumulative fills + an order book."""

    def __init__(self, authed: bool = True) -> None:
        self.authed = authed
        self.placed: list[BrokerOrder] = []
        self.fills: dict[str, dict] = {}  # broker_order_id -> cumulative row
        self.book: list[BrokerOrder] = []
        self.fail_place = False
        self._seq = 0

    def is_authenticated(self) -> bool:
        return self.authed

    def place_order(self, order: BrokerOrder) -> str:
        if self.fail_place:
            raise RuntimeError("venue down")
        self._seq += 1
        boid = f"B{self._seq}"
        order.broker_order_id = boid
        self.placed.append(order)
        return boid

    def poll_fill(self, broker_order_id):
        return self.fills.get(str(broker_order_id))

    def get_order_book(self):
        return self.book

    def set_fill(self, boid, quantity, price=101.5):
        self.fills[boid] = {
            "symbol": "RELIANCE",
            "transaction_type": "BUY",
            "quantity": quantity,
            "price": price,
        }


def _ledger():
    from backtest.forward.paper_runner import OrderLedger

    return OrderLedger()


def _runner(ledger, name="LIVE-R"):
    config = RunnerConfig(
        name=name,
        strategy_name="sma_crossover",
        allocated_capital=100_000,
        symbols=["RELIANCE"],
        timeframe="1day",
        mode="live",
        source="synthetic",
    )
    return StrategyRunner(config, ledger=ledger)


def _gateway(broker=None, ledger=None, runner=None):
    ledger = ledger or _ledger()
    runner = runner or _runner(ledger)
    broker = broker or FakeLiveBroker()
    gw = LiveEquityGateway(ledger, broker, confirm_live=True)
    return gw, ledger, runner, broker


@pytest.fixture(autouse=True)
def _armed_env(monkeypatch):
    monkeypatch.setenv("ALLOW_LIVE_ORDERS", "1")
    reset_data_bus()
    yield
    reset_data_bus()


# ---------------------------------------------------------------------------
# Arming — fail-closed
# ---------------------------------------------------------------------------


class TestArming:
    def test_refuses_without_confirm_flag(self, ledger_and_runner):
        ledger, runner = ledger_and_runner
        with pytest.raises(ValueError, match="confirm_live"):
            LiveEquityGateway(ledger, FakeLiveBroker(), confirm_live=False)

    def test_refuses_without_env(self, monkeypatch, ledger_and_runner):
        ledger, runner = ledger_and_runner
        monkeypatch.delenv("ALLOW_LIVE_ORDERS", raising=False)
        assert live_orders_allowed() is False
        with pytest.raises(ValueError, match="ALLOW_LIVE_ORDERS"):
            LiveEquityGateway(ledger, FakeLiveBroker(), confirm_live=True)

    def test_refuses_unauthenticated_broker(self, ledger_and_runner):
        ledger, runner = ledger_and_runner
        with pytest.raises(ValueError, match="authenticated"):
            LiveEquityGateway(ledger, FakeLiveBroker(authed=False), confirm_live=True)

    def test_armed_env_values(self, monkeypatch):
        for value in ("1", "true", "YES", " True "):
            monkeypatch.setenv("ALLOW_LIVE_ORDERS", value)
            assert live_orders_allowed() is True
        for value in ("0", "no", "", "off"):
            monkeypatch.setenv("ALLOW_LIVE_ORDERS", value)
            assert live_orders_allowed() is False

    @pytest.fixture
    def ledger_and_runner(self):
        ledger = _ledger()
        runner = _runner(ledger)
        return ledger, runner


# ---------------------------------------------------------------------------
# Placement — one place per intent, failures cancel locally
# ---------------------------------------------------------------------------


class TestPlacement:
    def test_submit_places_and_stamps_but_does_not_fill(self):
        gw, ledger, runner, broker = _gateway()
        coid = gw.submit_market(runner.instance_id, "RELIANCE", SIDE_BUY, 10, 101.0)
        assert isinstance(coid, str) and coid.startswith("PRT-")
        # the venue saw the ledger's coid (idempotency key)
        assert broker.placed[0].client_order_id == coid
        assert broker.placed[0].quantity == 10
        # ledger order exists, PENDING, stamped with the venue id
        order = ledger.get_order(coid)
        assert order.status == ORDER_PENDING
        assert order.broker_order_id == "B1"
        # nothing traded yet — the portfolio is untouched
        assert gw.working_count() == 1
        assert not runner.portfolio.positions

    def test_place_failure_cancels_locally(self):
        gw, ledger, runner, broker = _gateway()
        broker.fail_place = True
        with pytest.raises(RuntimeError, match="venue down"):
            gw.submit_market(runner.instance_id, "RELIANCE", SIDE_BUY, 10, 101.0)
        assert gw.working_count() == 0
        assert ledger.order_count == 1  # the order existed...
        placed = ledger.orders_for(runner.instance_id)[0]
        assert ledger.get_order(placed.client_order_id).status == "CANCELLED"


# ---------------------------------------------------------------------------
# Fills — delta-only, shared accounting
# ---------------------------------------------------------------------------


class TestFills:
    def test_fill_applies_once_and_is_idempotent(self):
        gw, ledger, runner, broker = _gateway()
        coid = gw.submit_market(runner.instance_id, "RELIANCE", SIDE_BUY, 10, 101.0)
        broker.set_fill("B1", quantity=10, price=101.5)

        assert gw.poll_pending() == 1
        pos = runner.portfolio.positions["RELIANCE"]
        assert float(pos.quantity) == 10
        assert float(pos.average_entry_price) == 101.5
        assert ledger.fill_count == 1

        # re-poll: the venue still reports 10 cumulative — ZERO new booking
        assert gw.poll_pending() == 0
        assert ledger.fill_count == 1
        assert float(runner.portfolio.positions["RELIANCE"].quantity) == 10

        # the fill routed through the shared ledger path
        drained = ledger.drain_pending_fills(runner.instance_id)
        assert len(drained) == 1
        assert drained[0].client_order_id == coid
        assert drained[0].price == 101.5

    def test_partial_fills_apply_in_steps(self):
        gw, ledger, runner, broker = _gateway()
        gw.submit_market(runner.instance_id, "RELIANCE", SIDE_BUY, 10, 101.0)

        broker.set_fill("B1", quantity=4, price=101.0)
        assert gw.poll_pending() == 1
        assert float(runner.portfolio.positions["RELIANCE"].quantity) == 4
        assert gw.working_count() == 1  # order still working

        broker.set_fill("B1", quantity=10, price=101.4)  # cumulative 10
        assert gw.poll_pending() == 1
        pos = runner.portfolio.positions["RELIANCE"]
        assert float(pos.quantity) == 10
        assert gw.working_count() == 0  # fully filled → leaves the working set

    def test_fill_without_local_order_is_refused_not_booked(self, caplog):
        gw, ledger, runner, broker = _gateway()
        gw.submit_market(runner.instance_id, "RELIANCE", SIDE_BUY, 10, 101.0)
        coid = gw.working_coids()[0]
        # simulate the ledger losing the order (e.g. trim) while the venue filled
        state = gw._working[coid]
        ledger._orders.pop(coid)
        ledger._routing.pop(coid)
        broker.set_fill(state["broker_order_id"], quantity=10, price=101.5)
        with caplog.at_level(logging.ERROR):
            gw.poll_pending()
        assert any("REFUSING to book" in r.message for r in caplog.records)
        assert not runner.portfolio.positions


# ---------------------------------------------------------------------------
# Reconciliation — the honesty check
# ---------------------------------------------------------------------------


class TestReconcile:
    def _working(self):
        gw, ledger, runner, broker = _gateway()
        coid = gw.submit_market(runner.instance_id, "RELIANCE", SIDE_BUY, 10, 101.0)
        return gw, ledger, runner, broker, coid

    def test_rejected_at_venue_cancels_locally(self, caplog):
        gw, ledger, runner, broker, coid = self._working()
        broker.book = [
            BrokerOrder(broker_order_id="B1", status="REJECTED", filled_quantity=0)
        ]
        with caplog.at_level(logging.WARNING):
            summary = gw.reconcile()
        assert summary["rejected"] == 1
        assert gw.working_count() == 0
        assert ledger.get_order(coid).status == "CANCELLED"
        assert any("REJECTED" in r.message for r in caplog.records)

    def test_expired_at_venue_cancels_locally(self):
        gw, ledger, runner, broker, coid = self._working()
        broker.book = []  # day order purged at the venue
        summary = gw.reconcile()
        assert summary["unknown_at_broker"] == 1
        assert gw.working_count() == 0
        assert ledger.get_order(coid).status == "CANCELLED"

    def test_divergent_fill_warns_and_stays_working_for_repoll(self, caplog):
        gw, ledger, runner, broker, coid = self._working()
        broker.set_fill("B1", quantity=10, price=101.5)
        broker.book = [BrokerOrder(broker_order_id="B1", status="COMPLETE", filled_quantity=10)]
        with caplog.at_level(logging.WARNING):
            summary = gw.reconcile()
        assert summary["divergent"] == 1
        # the poll (not reconcile) owns booking — order stays working for it
        assert gw.working_count() == 1
        assert any("forcing re-poll" in r.message for r in caplog.records)
        assert gw.poll_pending() == 1  # the delta lands on the next pump


# ---------------------------------------------------------------------------
# Manager wiring
# ---------------------------------------------------------------------------


def _manager(broker=None, state_path=None, confirm=False):
    return PortfolioManager(
        risk_config=GlobalRiskConfig(daily_loss_limit=1_000_000, max_drawdown_pct=0.9),
        tick_seconds=1.0,
        warmup_bars=5,
        auto_start_feed=False,
        state_path=state_path,
        live_broker=broker or FakeLiveBroker(),
        confirm_live_orders=confirm,
    )


class TestManagerWiring:
    def test_live_runner_refused_when_disarmed(self):
        mgr = _manager(confirm=False)  # broker fine, but not confirmed
        with pytest.raises(ValueError, match="confirm_live"):
            mgr.add_runner(
                RunnerConfig(
                    name="L", strategy_name="sma_crossover", allocated_capital=50_000,
                    symbols=["RELIANCE"], timeframe="1day", mode="live", source="synthetic",
                )
            )
        assert len(mgr._runners) == 0

    def test_live_runner_gets_the_gateway_paper_unchanged(self):
        mgr = _manager(confirm=True)
        live_id = mgr.add_runner(
            RunnerConfig(
                name="L", strategy_name="sma_crossover", allocated_capital=50_000,
                symbols=["RELIANCE"], timeframe="1day", mode="live", source="synthetic",
            )
        )
        paper_id = mgr.add_runner(
            RunnerConfig(
                name="P", strategy_name="sma_crossover", allocated_capital=50_000,
                symbols=["INFY"], timeframe="1day", mode="paper", source="synthetic",
            )
        )
        from backtest.forward.paper_runner import PaperBroker

        assert isinstance(mgr.get_runner(live_id).broker, LiveEquityGateway)
        assert isinstance(mgr.get_runner(paper_id).broker, PaperBroker)
        mgr.shutdown()

    def test_unauthenticated_broker_refuses_live_runner(self):
        mgr = _manager(broker=FakeLiveBroker(authed=False), confirm=True)
        with pytest.raises(ValueError, match="authenticated"):
            mgr.add_runner(
                RunnerConfig(
                    name="L", strategy_name="sma_crossover", allocated_capital=50_000,
                    symbols=["RELIANCE"], timeframe="1day", mode="live", source="synthetic",
                )
            )

    def test_live_emission_end_to_end_through_manager_tick(self):
        broker = FakeLiveBroker()
        mgr = _manager(broker=broker, confirm=True)
        live_id = mgr.add_runner(
            RunnerConfig(
                name="L", strategy_name="sma_crossover", allocated_capital=50_000,
                symbols=["RELIANCE"], timeframe="1day", mode="live", source="synthetic",
            ),
            start=False,
        )
        runner = mgr.get_runner(live_id)
        # the live emission path: gateway returns a coid, no fake fill
        runner._emit_entry("RELIANCE", 101.0, ts="2026-09-17T10:00:00+00:00")
        assert len(broker.placed) == 1
        assert not runner.portfolio.positions  # nothing filled yet

        # the venue fills; the manager's tick pump books it
        broker.set_fill("B1", quantity=10, price=101.5)
        mgr.tick(ts=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
        pos = runner.portfolio.positions["RELIANCE"]
        assert float(pos.quantity) == 10
        assert runner.positions["RELIANCE"]["qty"] == 10  # the exit path sees it
        mgr.shutdown()


# ---------------------------------------------------------------------------
# Restart survival — re-arm polling, never re-place (P2.4 × F-12)
# ---------------------------------------------------------------------------


class TestRestartReArm:
    def test_working_order_survives_restart_and_fills_exactly_once(self, tmp_path):
        broker = FakeLiveBroker()
        state = tmp_path / "state.json"
        mgr1 = _manager(broker=broker, state_path=state, confirm=True)
        live_id = mgr1.add_runner(
            RunnerConfig(
                name="L", strategy_name="sma_crossover", allocated_capital=50_000,
                symbols=["RELIANCE"], timeframe="1day", mode="live", source="synthetic",
            ),
            start=False,
        )
        mgr1.get_runner(live_id)._emit_entry("RELIANCE", 101.0, ts="2026-09-17T10:00:00+00:00")
        assert len(broker.placed) == 1
        mgr1.shutdown()  # final poll finds no fill; state has the working order

        # the venue fills BETWEEN the two sessions
        broker.set_fill("B1", quantity=10, price=101.5)

        mgr2 = _manager(broker=broker, state_path=state, confirm=True)
        try:
            restored = mgr2.get_runner(live_id)
            gw = restored.broker
            assert isinstance(gw, LiveEquityGateway)
            assert gw.working_count() == 1  # re-armed, NOT re-placed
            assert len(broker.placed) == 1  # still exactly one venue order

            mgr2.tick(ts=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
            assert float(restored.portfolio.positions["RELIANCE"].quantity) == 10
            assert len(broker.placed) == 1  # and still one after the fill
        finally:
            mgr2.shutdown()

    def test_disarmed_boot_skips_live_runner_but_boots(self, tmp_path, monkeypatch, caplog):
        broker = FakeLiveBroker()
        state = tmp_path / "state.json"
        mgr1 = _manager(broker=broker, state_path=state, confirm=True)
        mgr1.add_runner(
            RunnerConfig(
                name="L", strategy_name="sma_crossover", allocated_capital=50_000,
                symbols=["RELIANCE"], timeframe="1day", mode="live", source="synthetic",
            ),
            start=False,
        )
        mgr1.add_runner(
            RunnerConfig(
                name="P", strategy_name="sma_crossover", allocated_capital=50_000,
                symbols=["INFY"], timeframe="1day", mode="paper", source="synthetic",
            ),
            start=False,
        )
        mgr1.shutdown()

        # next boot without the env: live runner skipped, boot lives on
        monkeypatch.delenv("ALLOW_LIVE_ORDERS", raising=False)
        with caplog.at_level(logging.WARNING):
            mgr2 = PortfolioManager(
                risk_config=GlobalRiskConfig(daily_loss_limit=1_000_000, max_drawdown_pct=0.9),
                tick_seconds=1.0, warmup_bars=5, auto_start_feed=False,
                state_path=state, live_broker=broker, confirm_live_orders=True,
            )
        try:
            assert len(mgr2._runners) == 1  # the paper runner
            assert any("refused on restore" in r.message for r in caplog.records)
        finally:
            mgr2.shutdown()


# ---------------------------------------------------------------------------
# Gateway state round-trip (unit level)
# ---------------------------------------------------------------------------


class TestGatewayState:
    def test_snapshot_restore_round_trip(self):
        gw, ledger, runner, broker = _gateway()
        gw.submit_market(runner.instance_id, "RELIANCE", SIDE_BUY, 10, 101.0)
        payload = gw.snapshot_state()
        assert len(payload["working"]) == 1

        gw2 = LiveEquityGateway(ledger, broker, confirm_live=True)
        gw2.restore_state(payload)
        assert gw2.working_count() == 1
        assert gw2.working_coids() == gw.working_coids()
        # and the re-armed order still fills through the SAME ledger
        broker.set_fill("B1", quantity=10, price=101.5)
        assert gw2.poll_pending() == 1
        assert float(runner.portfolio.positions["RELIANCE"].quantity) == 10
