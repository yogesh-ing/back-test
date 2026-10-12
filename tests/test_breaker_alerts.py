"""Breaker → alert wiring (STATUS item 17, Task 5).

Circuit-breaker trips must reach the alert broker — that is what the widget
and the outbound notifier (Telegram/email) read. These tests pin the
contract end to end:

* a fresh bucket trip raises ONE critical ``risk_limit_breach`` alert with
  subject ``bucket:<mode>`` and the breaker context in ``data``;
* the master (combined-book) halt raises its ``portfolio`` alert only when
  no bucket tripped in the same pass — one incident, one notification;
* repeated evaluations while halted do not re-raise (the breaker latches);
* a reset resolves exactly the scopes it releases;
* manager → broker → notifier reaches a (fake) Telegram venue using the
  shipped ``config/alerts.yaml`` template and routing.

Core cases run on the paper bucket; bucket-independence cases use the live
bucket through the armed test venue (F-12).
"""

from __future__ import annotations

import json

import pytest

from backtest.alerts.broker import get_alert_broker, reset_alert_broker
from backtest.alerts.notifier import (
    AlertNotifier,
    _escape_markdown,
    load_notifier_config,
    stop_alert_notifier,
)
from backtest.alerts.types import AlertType
from backtest.forward.paper_runner import (
    SIDE_BUY,
    SIDE_SELL,
    TARGET_SINGLE,
    RunnerConfig,
)
from backtest.forward.portfolio_manager import PortfolioManager
from backtest.forward.risk_supervisor import HALT_PAUSE, GlobalRiskConfig

BREACH = AlertType.RISK_LIMIT_BREACH.value


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _arm_live_orders(monkeypatch):
    """Live-bucket cases trade through the armed test venue (F-12)."""
    monkeypatch.setenv("ALLOW_LIVE_ORDERS", "1")


@pytest.fixture(autouse=True)
def _fresh_alert_state():
    """Each test gets its own alert broker / notifier singletons."""
    stop_alert_notifier()
    reset_alert_broker()
    yield
    stop_alert_notifier()
    reset_alert_broker()


@pytest.fixture()
def manager():
    from live_test_support import ARMED_KWARGS

    mgr = PortfolioManager(
        **ARMED_KWARGS,
        risk_config=GlobalRiskConfig(
            daily_loss_limit=5_000,
            max_drawdown_pct=0.10,
            breach_mode=HALT_PAUSE,
        ),
        auto_start_feed=False,
    )
    yield mgr
    mgr.shutdown()


class FakeResponse:
    def read(self) -> bytes:
        return b'{"ok": true}'

    def close(self) -> None:
        pass


class FakeOpener:
    """Stands in for urllib's opener; records the JSON body per call."""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, request, timeout=None):
        self.calls.append(json.loads(request.data.decode("utf-8")))
        return FakeResponse()


def _paper_config(name="P1", capital=100_000, symbols=None):
    return RunnerConfig(
        name=name,
        strategy_name="rsi_reversion",
        allocated_capital=capital,
        target_type=TARGET_SINGLE,
        symbols=symbols or ["AAA"],
        timeframe="1hour",
        mode="paper",
    )


def _live_config(name="L1", capital=200_000, symbols=None):
    return RunnerConfig(
        name=name,
        strategy_name="sma_crossover",
        allocated_capital=capital,
        target_type=TARGET_SINGLE,
        symbols=symbols or ["BBB"],
        timeframe="1hour",
        mode="live",
    )


def _bar(symbol="AAA", ts="2026-09-02 10:00:00"):
    return {"ts": ts, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000}


def _lose(mgr, runner_id, symbol, qty=1000, entry=100.0, exit_px=80.0):
    """Realize a loss on one runner via a buy-then-sell round trip."""
    mgr.broker.submit_market(runner_id, symbol, SIDE_BUY, qty, entry)
    mgr.broker.submit_market(runner_id, symbol, SIDE_SELL, qty, exit_px)


def _single_breach(mgr):
    """One paper runner realizing −8,000 → daily-loss trip (+ master latch).

    −8,000 breaches the 5k daily limit but stays under the 10% drawdown
    stop (8% of 100k), so the trip is the daily-loss breaker in its
    configured PAUSE mode — not the always-flatten drawdown stop.
    """
    pid = mgr.add_runner(_paper_config(), start=False)
    mgr._on_bar("AAA", _bar("AAA"))
    _lose(mgr, pid, "AAA", exit_px=92.0)
    mgr._evaluate_risk()
    return pid


def _breaker_alerts():
    return [a for a in get_alert_broker().open_alerts() if a.alert_type == BREACH]


# ---------------------------------------------------------------------------
# Bucket trip → scoped alert
# ---------------------------------------------------------------------------


class TestBucketTripAlert:
    def test_paper_trip_raises_scoped_critical_alert(self, manager):
        _single_breach(manager)

        alerts = _breaker_alerts()
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.severity == "critical"
        assert alert.subject == "bucket:paper"
        assert alert.data["scope"] == "bucket"
        assert alert.data["bucket"] == "paper"
        assert alert.data["symbol"] == "PAPER"
        assert alert.data["limit_type"] == "portfolio_daily_loss"
        assert alert.data["halt_mode"] == HALT_PAUSE
        assert alert.data["daily_pnl"] <= -5_000  # the breached limit
        assert "PAPER halted" in alert.message

    def test_master_latch_suppressed_when_bucket_alerted(self, manager):
        """One incident → one notification: the bucket alert covers it."""
        _single_breach(manager)

        assert manager.halted is True  # master still latches for safety
        assert [a.subject for a in _breaker_alerts()] == ["bucket:paper"]

    def test_repeated_evaluation_does_not_duplicate(self, manager):
        _single_breach(manager)
        before = _breaker_alerts()[0].occurrences

        manager._evaluate_risk()
        manager._evaluate_risk()

        alerts = _breaker_alerts()
        assert len(alerts) == 1
        assert alerts[0].occurrences == before == 1


# ---------------------------------------------------------------------------
# Combined-book breach with no bucket trip → portfolio alert
# ---------------------------------------------------------------------------


class TestMasterOnlyAlert:
    def test_combined_breach_without_bucket_trips_alerts_portfolio(self, manager):
        pid = manager.add_runner(_paper_config(symbols=["AAA"]), start=False)
        lid = manager.add_runner(_live_config(symbols=["BBB"]), start=False)
        manager._on_bar("AAA", _bar("AAA"))
        manager._on_bar("BBB", _bar("BBB"))
        # −3,000 per bucket: below the 5k per-bucket limit, combined −6k trips
        _lose(manager, pid, "AAA", exit_px=97.0)
        _lose(manager, lid, "BBB", exit_px=97.0)
        manager._evaluate_risk()

        assert manager._bucket_halted["paper"] is False
        assert manager._bucket_halted["live"] is False
        assert manager.halted is True
        alerts = _breaker_alerts()
        assert len(alerts) == 1
        assert alerts[0].subject == "portfolio"
        assert alerts[0].data["scope"] == "portfolio"
        assert alerts[0].data["bucket"] is None


# ---------------------------------------------------------------------------
# Reset resolves exactly the released scopes
# ---------------------------------------------------------------------------


class TestResetResolves:
    def test_scoped_reset_clears_only_that_bucket(self, manager):
        pid = manager.add_runner(_paper_config(symbols=["AAA"]), start=False)
        lid = manager.add_runner(_live_config(symbols=["BBB"]), start=False)
        manager._on_bar("AAA", _bar("AAA"))
        manager._on_bar("BBB", _bar("BBB"))
        _lose(manager, pid, "AAA", exit_px=92.0)
        manager._evaluate_risk()  # paper trips
        _lose(manager, lid, "BBB", exit_px=92.0)
        manager._evaluate_risk()  # live trips

        assert sorted(a.subject for a in _breaker_alerts()) == [
            "bucket:live",
            "bucket:paper",
        ]

        manager.reset_circuit_breaker("paper")
        assert [a.subject for a in _breaker_alerts()] == ["bucket:live"]

        manager.reset_circuit_breaker("live")
        assert _breaker_alerts() == []
        assert manager.halted is False

    def test_master_reset_clears_every_scope(self, manager):
        _single_breach(manager)

        manager.reset_circuit_breaker()

        assert _breaker_alerts() == []
        assert manager.halted is False
        assert manager._bucket_halted["paper"] is False


# ---------------------------------------------------------------------------
# End-to-end: manager → broker → notifier → (fake) Telegram
# ---------------------------------------------------------------------------


class TestEndToEndNotification:
    def test_trip_reaches_telegram_through_shipped_config(self, manager):
        opener = FakeOpener()
        config = load_notifier_config(
            env={"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "42"}
        )
        notifier = AlertNotifier(
            get_alert_broker(),
            config,
            synchronous=True,
            telegram_opener=opener,
        ).start()
        try:
            _single_breach(manager)

            assert len(opener.calls) == 1
            body = opener.calls[0]
            assert body["chat_id"] == "42"
            assert body["parse_mode"] == "Markdown"
            text = body["text"]
            # Shipped template (config/alerts.yaml) rendered with breaker data;
            # identifiers arrive Markdown-escaped (parse_mode is set there).
            assert "Risk limit breached" in text
            assert "PAPER" in text
            assert _escape_markdown("portfolio_daily_loss") in text
            assert notifier.failures == 0
            assert notifier.sent == 2  # telegram + log floor

            # Latch: another evaluation pass is not a new notification.
            manager._evaluate_risk()
            assert len(opener.calls) == 1
        finally:
            notifier.stop()
