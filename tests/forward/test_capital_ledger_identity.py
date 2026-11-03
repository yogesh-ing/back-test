"""Ticket 2.1 — the capital ledger has exactly one source (BUG-03).

Two phantom-money defects measured on the live box 2026-10-30:

* ``total_capital`` was an accumulator kept alive by ``+=`` on spawn and ``-=``
  on delete, and was re-adopted verbatim from the state file. Any path that
  removed a runner without paying the ``-=`` (refused restore, re-spawn) left
  it drifting away from the money actually on the books.
* The singleton ``dashboard_book`` was folded into every summary. Its
  ``equity`` is capital + P&L, so ₹10,00,000 of capital the ledger never
  carried appeared in ``total_equity`` — on scoped paper/real-money calls too,
  where the book belongs to neither bucket — and ``total_equity -
  total_capital`` stopped meaning P&L.
"""

from __future__ import annotations

import pytest

from backtest.forward.paper_runner import TARGET_SINGLE, RunnerConfig
from backtest.forward.portfolio_manager import PortfolioManager
from backtest.forward.risk_supervisor import GlobalRiskConfig


@pytest.fixture(autouse=True)
def _arm_orders(monkeypatch):
    """Real-money-BUCKET accounting needs an armed gateway (see
    live_test_support)."""
    monkeypatch.setenv("ALLOW_LIVE_ORDERS", "1")


def _paper(name="P1", capital=100_000, symbol="AAA"):
    return RunnerConfig(
        name=name,
        strategy_name="rsi_reversion",
        allocated_capital=capital,
        target_type=TARGET_SINGLE,
        symbols=[symbol],
        timeframe="1hour",
        mode="paper",
    )


def _armed(name="M1", capital=200_000, symbol="BBB"):
    return RunnerConfig(
        name=name,
        strategy_name="sma_crossover",
        allocated_capital=capital,
        target_type=TARGET_SINGLE,
        symbols=[symbol],
        timeframe="1hour",
        mode="live",
    )


def _dashboard(capital, pnl, positions=1, margin=0.0):
    """A dashboard book whose equity follows the ledger convention."""
    return {
        "exists": True,
        "open_positions": positions,
        "open_structures": 0,
        "equity": round(capital + pnl, 2),
        "realized_pnl": pnl,
        "unrealized_pnl": 0.0,
        "capital": capital,
        "margin_used": margin,
        "positions": [],
        "structures": [],
    }


@pytest.fixture
def manager():
    from live_test_support import ARMED_KWARGS

    mgr = PortfolioManager(
        **ARMED_KWARGS,
        risk_config=GlobalRiskConfig(daily_loss_limit=100_000, max_drawdown_pct=0.50),
        auto_start_feed=False,
    )
    yield mgr
    mgr.shutdown()


# ---------------------------------------------------------------------------
# total_capital is derived, never accumulated
# ---------------------------------------------------------------------------


class TestCapitalIsDerived:
    def test_summary_ignores_hand_drifted_scalar(self, manager):
        """A drifted accumulator cannot survive into the ledger."""
        manager.add_runner(_paper(capital=100_000), start=False)
        manager.add_runner(_armed(capital=200_000), start=False)
        manager.total_capital = 8_250_000.0  # the shape of the live phantom

        assert manager.get_portfolio_summary()["total_capital"] == pytest.approx(300_000.0)

    def test_add_runner_resyncs_the_scalar(self, manager):
        manager.add_runner(_paper(capital=100_000), start=False)
        assert manager.total_capital == pytest.approx(100_000.0)
        manager.add_runner(_paper(name="P2"), start=False)
        assert manager.total_capital == pytest.approx(200_000.0)

    def test_remove_runner_leaves_no_accumulator_residue(self, manager):
        pid = manager.add_runner(_paper(capital=100_000), start=False)
        manager.add_runner(_armed(capital=200_000), start=False)
        manager.remove_runner(pid)

        assert manager.total_capital == pytest.approx(200_000.0)
        assert manager.get_portfolio_summary()["total_capital"] == pytest.approx(200_000.0)

    def test_restore_rebuilds_capital_and_drops_saved_scalar(self, manager):
        """The saved ``manager.total_capital`` is a ghost; the runners are it."""
        pid = manager.add_runner(_paper(capital=100_000), start=False)
        payload = manager._snapshot_payload()
        manager.remove_runner(pid)

        payload["manager"]["total_capital"] = 8_250_000.0
        restored = manager._apply_state_payload(payload)

        assert restored == 1
        assert manager.total_capital == pytest.approx(100_000.0)

    def test_unscoped_capital_is_the_buckets_summed(self, manager):
        manager.add_runner(_paper(capital=100_000), start=False)
        manager.add_runner(_armed(capital=200_000), start=False)

        buckets = manager.get_bucket_aggregates()
        combined = manager.get_portfolio_summary()

        assert combined["total_capital"] == pytest.approx(
            sum(b["capital"] for b in buckets.values())
        )


# ---------------------------------------------------------------------------
# The dashboard book folds in where it belongs — or not at all
# ---------------------------------------------------------------------------


class TestDashboardFold:
    def test_scoped_summary_never_folds_dashboard(self, manager, monkeypatch):
        """The singleton is neither the paper nor the real-money bucket."""
        manager.add_runner(_paper(capital=100_000), start=False)
        monkeypatch.setattr(
            manager, "get_dashboard_book_summary",
            lambda: _dashboard(capital=1_000_000.0, pnl=50_000.0),
        )

        paper = manager.get_portfolio_summary(mode="paper")
        assert paper["total_equity"] == pytest.approx(100_000.0)
        assert paper["total_capital"] == pytest.approx(100_000.0)
        assert paper["realized_pnl"] == pytest.approx(0.0)
        assert paper["open_positions"] == 0

    def test_idle_dashboard_adds_nothing(self, manager, monkeypatch):
        """An empty book carrying ₹10L of capital is not money at risk."""
        manager.add_runner(_paper(capital=100_000), start=False)
        monkeypatch.setattr(
            manager, "get_dashboard_book_summary",
            lambda: _dashboard(capital=1_000_000.0, pnl=0.0, positions=0),
        )

        combined = manager.get_portfolio_summary()
        assert combined["total_equity"] == pytest.approx(100_000.0)
        assert combined["total_capital"] == pytest.approx(100_000.0)

    def test_in_use_dashboard_folds_its_own_capital(self, manager, monkeypatch):
        """equity - capital must stay exactly P&L after the fold."""
        manager.add_runner(_paper(capital=100_000), start=False)
        monkeypatch.setattr(
            manager, "get_dashboard_book_summary",
            lambda: _dashboard(capital=1_000_000.0, pnl=50_000.0),
        )

        combined = manager.get_portfolio_summary()
        assert combined["total_capital"] == pytest.approx(1_100_000.0)
        assert combined["total_equity"] == pytest.approx(1_150_000.0)
        assert combined["total_equity"] - combined["total_capital"] == pytest.approx(50_000.0)
        assert combined["realized_pnl"] == pytest.approx(50_000.0)

    def test_folded_margin_counts_as_deployed(self, manager, monkeypatch):
        manager.add_runner(_paper(capital=100_000), start=False)
        monkeypatch.setattr(
            manager, "get_dashboard_book_summary",
            lambda: _dashboard(capital=1_000_000.0, pnl=0.0, margin=200_000.0),
        )

        combined = manager.get_portfolio_summary()
        assert combined["deployed_capital"] == pytest.approx(200_000.0)
        assert combined["deployed_pct"] == pytest.approx(
            combined["deployed_capital"] / combined["total_capital"], abs=1e-4
        )

    def test_dashboard_gone_leaves_ledger_untouched(self, manager, monkeypatch):
        manager.add_runner(_paper(capital=100_000), start=False)
        monkeypatch.setattr(
            manager, "get_dashboard_book_summary",
            lambda: {"exists": False, "open_positions": 0, "equity": 0.0,
                     "realized_pnl": 0.0, "capital": 0.0,
                     "positions": [], "structures": []},
        )

        combined = manager.get_portfolio_summary()
        assert combined["total_capital"] == pytest.approx(100_000.0)
        assert combined["total_equity"] == pytest.approx(100_000.0)
