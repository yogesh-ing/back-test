"""Ticket 2.3 — capital flows must not read as trading (BUG-33).

The flow-adjusted anchors are what keep the *card* honest: ``daily_pnl`` is now
the bucket's session move (``Σ runner.daily_pnl()``), while ``day_start`` /
``peak_equity`` are the percentage denominator and the drawdown baseline. The
anchor was taken once, at the session's first bar, and never moved afterwards —
so a runner spawned mid-session added its allocated capital to equity while the
anchor stayed behind, and the bucket reported a huge phantom *gain*. That
direction is the dangerous one: the daily-loss limit gets spent absorbing the
phantom, so a real loss the size of the new book trips nothing. Deleting a book
moved equity the other way and read as a loss against a peak the flow had just
inflated.

The anchors (manager baseline, bucket, live segment) and the high-water marks
now move with the money: in at ``allocated_capital``, out at the book's
``equity()``. An anchor still at 0 means the session baseline has not been
taken yet — the first bar already includes the flow, so those keys are skipped,
which is also what keeps a boot restore from double-counting itself.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backtest.forward.paper_runner import SIDE_BUY, SIDE_SELL, TARGET_SINGLE, RunnerConfig
from backtest.forward.portfolio_manager import PortfolioManager
from backtest.forward.risk_supervisor import GlobalRiskConfig


@pytest.fixture(autouse=True)
def _arm_orders(monkeypatch):
    monkeypatch.setenv("ALLOW_LIVE_ORDERS", "1")


def _book(name, capital, symbol, mode="paper"):
    return RunnerConfig(
        name=name,
        strategy_name="rsi_reversion" if mode == "paper" else "sma_crossover",
        allocated_capital=capital,
        target_type=TARGET_SINGLE,
        symbols=[symbol],
        timeframe="1hour",
        mode=mode,
    )


def _bar(symbol="AAA", price=100):
    return {
        "ts": "2026-09-02 10:00:00",
        "open": price, "high": price + 1, "low": price - 1,
        "close": price, "volume": 1000,
    }


def _id_for(manager, name):
    return next(r.instance_id for r in manager._runners.values() if r.config.name == name)


def _manager(daily_loss_limit=100_000, max_drawdown_pct=0.90):
    from live_test_support import ARMED_KWARGS

    return PortfolioManager(
        **ARMED_KWARGS,
        risk_config=GlobalRiskConfig(
            daily_loss_limit=daily_loss_limit, max_drawdown_pct=max_drawdown_pct
        ),
        auto_start_feed=False,
    )


def _fuse_daily_pnl(manager, mode):
    """The number the bucket breaker is fed — the bucket's session move.

    Since the daily-pnl continuation it is ``Σ runner.daily_pnl()`` (what the
    card shows), not ``equity - day_start``, which was the bucket's whole life
    against an anchor taken at its first bar. The anchors below still matter:
    they are the percentage denominator and the reported day baseline, and a
    flow must move them or a spawn reads as a gain on the card.
    """
    return manager._bucket_daily_pnl(mode)



@pytest.fixture
def manager():
    mgr = _manager()
    yield mgr
    mgr.shutdown()


# ---------------------------------------------------------------------------
# Acceptance: the plan's ₹63L booted + ₹39L spawned mid-day, ₹0 traded
# ---------------------------------------------------------------------------


class TestSpawnIsNotProfit:
    def test_mid_day_spawn_moves_the_anchor(self, manager):
        small = 6_300_000.0
        big = 3_900_000.0
        manager.add_runner(_book("Boot", small, "AAA"), start=False)
        manager._on_bar("AAA", _bar())
        assert manager._bucket_day_start["paper"] == pytest.approx(small)

        manager.add_runner(_book("Late", big, "BBB"), start=False)

        assert manager._bucket_day_start["paper"] == pytest.approx(small + big)
        assert manager._day_start_equity == pytest.approx(small + big)
        assert _fuse_daily_pnl(manager, "paper") == pytest.approx(0.0)

        summary = manager.get_portfolio_summary(mode="paper")
        assert summary["daily_pnl"] == pytest.approx(0.0)
        assert summary["daily_pnl_pct"] == 0.0
        assert manager.get_bucket_aggregates()["paper"]["daily_pnl_pct"] == 0.0

    def test_phantom_gain_cannot_shield_a_real_loss(self):
        """A ₹39L spawn must not buy the bucket a ₹39L loss cushion."""
        mgr = _manager(daily_loss_limit=50_000)
        try:
            pid = mgr.add_runner(_book("Boot", 500_000, "AAA"), start=False)
            mgr._on_bar("AAA", _bar())
            mgr.add_runner(_book("Late", 3_900_000, "BBB"), start=False)

            # ~₹70,000 of real trading loss on the original book, over the
            # ₹50,000 limit — but small next to the book that arrived mid-session.
            mgr.broker.submit_market(pid, "AAA", SIDE_BUY, 1000, 100.0)
            mgr.broker.submit_market(pid, "AAA", SIDE_SELL, 1000, 30.0)

            assert _fuse_daily_pnl(mgr, "paper") < -50_000
            mgr._evaluate_risk()
            assert mgr._bucket_halted["paper"] is True
        finally:
            mgr.shutdown()


# ---------------------------------------------------------------------------
# Removal: the survivors' measured P&L must not jump
# ---------------------------------------------------------------------------


class TestRemovalIsNotLoss:
    def test_deleting_a_book_leaves_the_remaining_pnl_alone(self, manager):
        pid_a = manager.add_runner(_book("Keep", 500_000, "AAA"), start=False)
        manager._on_bar("AAA", _bar())
        manager.add_runner(_book("Drop", 1_000_000, "BBB"), start=False)
        manager.broker.submit_market(pid_a, "AAA", SIDE_BUY, 1000, 100.0)
        manager.broker.submit_market(pid_a, "AAA", SIDE_SELL, 1000, 90.0)

        before = _fuse_daily_pnl(manager, "paper")
        anchor_before = manager._bucket_day_start["paper"]
        peak_before = manager._bucket_peak["paper"]

        assert manager.remove_runner(_id_for(manager, "Drop")) is True

        assert _fuse_daily_pnl(manager, "paper") == pytest.approx(before, abs=1.0)
        assert manager._bucket_day_start["paper"] == pytest.approx(
            anchor_before - 1_000_000.0, abs=1.0
        )
        # The peak moved with the money too, so no drawdown appears out of thin
        # air: what remains is the surviving book's own loss on its own baseline.
        assert manager._bucket_peak["paper"] == pytest.approx(peak_before - 1_000_000.0, abs=1.0)
        assert manager._bucket_drawdown("paper") == pytest.approx(-before / 500_000.0, abs=0.005)

    def test_delete_then_respawn_does_not_double_count(self, manager):
        pid = manager.add_runner(_book("Boot", 500_000, "AAA"), start=False)
        manager._on_bar("AAA", _bar())
        payload = manager._snapshot_payload()

        assert manager.remove_runner(pid) is True
        assert manager._day_start_equity == pytest.approx(0.0)

        manager._apply_state_payload(payload)
        assert manager._day_start_equity == pytest.approx(500_000.0)
        assert manager._bucket_day_start["paper"] == pytest.approx(500_000.0)
        assert _fuse_daily_pnl(manager, "paper") == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Segments, and the percentage denominator
# ---------------------------------------------------------------------------


class TestFlowAcrossScopes:
    def test_flow_reaches_the_bucket_and_the_live_segment(self, manager):
        """One flow must move every anchor the money sits inside."""
        runner = SimpleNamespace(
            config=SimpleNamespace(mode="live", segment="TestSeg", name="Seg")
        )
        key = manager._segment_key("testseg")
        manager._bucket_day_start[key] = 500_000.0
        manager._bucket_peak[key] = 500_000.0
        manager._bucket_day_start["live"] = 800_000.0
        manager._bucket_peak["live"] = 800_000.0

        manager._record_capital_flow(runner, 250_000.0)

        assert manager._bucket_day_start[key] == pytest.approx(750_000.0)
        assert manager._bucket_peak[key] == pytest.approx(750_000.0)
        assert manager._bucket_day_start["live"] == pytest.approx(1_050_000.0)
        assert manager._bucket_peak["live"] == pytest.approx(1_050_000.0)
        # No session baseline taken yet → nothing to adjust, no invented anchor.
        assert manager._bucket_day_start["paper"] == 0.0

    def test_summary_percentage_never_divides_by_a_stale_anchor(self, manager):
        """A ₹1 anchor against a ₹5L book cannot report a 500000% day."""
        pid = manager.add_runner(_book("A", 500_000, "AAA"), start=False)
        manager._on_bar("AAA", _bar())
        manager.broker.submit_market(pid, "AAA", SIDE_BUY, 100, 100.0)
        manager.broker.submit_market(pid, "AAA", SIDE_SELL, 100, 110.0)
        manager._day_start_equity = 1.0
        manager._bucket_day_start["paper"] = 1.0

        summary = manager.get_portfolio_summary(mode="paper")
        assert summary["daily_pnl_pct"] == pytest.approx(1000.0 / 500_000.0, abs=1e-4)
