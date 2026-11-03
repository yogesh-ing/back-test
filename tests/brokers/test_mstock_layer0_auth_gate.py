"""Sprint C / Ticket C2 — Layer-0 mStock Auth Gate & Instant 401/403 Kill-Latch.

Institutional Market-Data & Session Security Standard:
  1. Zero Packets Before Login: when ``SessionManager.is_authenticated("mstock")``
     is ``False``, ``MStockBarFeed`` and ``MStockLiveFeed`` emit 0 HTTP requests,
     0 retries, never read stale ``.mstock_session_token`` files on disk, and
     never invoke interactive CLI ``login()`` / ``input()`` from daemon threads.
  2. Instant 401/403 Kill-Latch: the very first HTTP 401/403 from any mStock
     TypeA endpoint immediately flips ``mstock`` to ``STATUS_EXPIRED`` in
     ``BrokerSessionManager``, clears the token, aborts remaining symbol polls in
     0 ms without sleeping, and triggers Ticket A4's market-hours-gated CRITICAL
     ``broker_session_expired`` alert.
  3. Upstox Parked: zero cross-talk into ``upstox``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from backtest.alerts.broker import get_alert_broker, reset_alert_broker
from backtest.brokers.base import STATUS_AUTHENTICATED, STATUS_EXPIRED, STATUS_UNAUTHENTICATED
from backtest.brokers.mstock import MStockBroker, MStockOrderError
from backtest.brokers.session_manager import get_session_manager, reset_default_manager
from backtest.data.mstock_live_feed import MStockLiveFeed
from backtest.forward.feed_registry import MStockBarFeed


@pytest.fixture(autouse=True)
def _clean_singletons(monkeypatch):
    monkeypatch.setenv("MSTOCK_API_KEY", "test-mstock-api-key")
    monkeypatch.setenv("MSTOCK_BASE_URL", "https://api.mstock.test")
    reset_default_manager()
    reset_alert_broker()
    yield
    reset_default_manager()
    reset_alert_broker()


def _seed_live_mstock_session() -> tuple[MStockBroker, object]:
    mgr = get_session_manager()
    broker = MStockBroker()
    # Keep WebSocket init inert in unit tests:
    broker._init_websocket_feed = lambda _tok: None  # type: ignore[method-assign]
    broker.restore_session(
        "valid-mstock-jwt-token-123456",
        datetime.now() + timedelta(hours=4),
    )
    mgr._sessions["mstock"] = broker
    mgr._ui_active = "mstock"
    mgr._last_observed_status["mstock"] = STATUS_AUTHENTICATED
    assert mgr.is_authenticated("mstock") is True
    return broker, mgr


class TestLayer0ZeroPacketGate:
    def test_logged_out_bar_feed_and_live_feed_emit_zero_http_and_ignore_stale_cache_file(
        self, tmp_path, monkeypatch
    ):
        # Plant a realistic-looking stale `.mstock_session_token` file in CWD:
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".mstock_session_token").write_text(
            "stale-disk-token-from-yesterday-999999", encoding="utf-8"
        )

        http_calls: list[str] = []
        cli_login_calls: list[str] = []

        monkeypatch.setattr(
            "requests.get",
            lambda url, *a, **k: http_calls.append(url) or pytest.fail(f"unexpected GET {url}"),
        )
        monkeypatch.setattr(
            "requests.post",
            lambda url, *a, **k: http_calls.append(url) or pytest.fail(f"unexpected POST {url}"),
        )
        monkeypatch.setattr(
            "backtest.live.auth.login",
            lambda *a, **k: cli_login_calls.append("login") or pytest.fail("CLI login called!"),
        )

        # 1. Direct MStockLiveFeed calls while logged out -> 0 HTTP packets, 0 CLI calls
        live_feed = MStockLiveFeed(base_url="https://api.mstock.test")
        assert live_feed.latest_bar("NIFTY") is None
        assert live_feed.latest_bar("RELIANCE") is None
        candles = live_feed.get_candles("NIFTY", "2026-11-02", "2026-11-03", "1min")
        assert candles.empty

        # 2. MStockBarFeed._poll_once across 5 subscribed symbols -> 0 HTTP packets
        bar_feed = MStockBarFeed(feed_client=live_feed, poll_interval_s=60.0)
        bar_feed.add_symbols(["NIFTY", "BANKNIFTY", "RELIANCE", "TCS", "INFY"])
        monkeypatch.setattr(bar_feed, "_market_open", lambda: True)

        assert bar_feed._poll_once() == 0
        assert http_calls == []
        assert cli_login_calls == []
        # Upstox stayed completely un-instantiated / parked:
        assert "upstox" not in get_session_manager()._sessions


class TestInstant401KillLatch:
    @pytest.mark.parametrize("status_code", [401, 403])
    def test_first_401_or_403_trips_session_expired_aborts_sweep_and_pages_in_market_hours(
        self, status_code, monkeypatch
    ):
        broker, mgr = _seed_live_mstock_session()

        # Active NSE market hours -> Ticket A4 CRITICAL alert must fire immediately!
        monkeypatch.setattr(
            "backtest.live.market_status.get_market_day_state",
            lambda *_a, **_k: {"session_state": "OPEN", "is_open": True},
        )

        http_urls: list[str] = []
        sleeps: list[float] = []
        monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

        class _AuthRejectedResp:
            ok = False

            def __init__(self, code: int):
                self.status_code = code

            def raise_for_status(self):
                raise RuntimeError(f"HTTP {self.status_code}")

            def json(self):
                return {"status": "error", "message": "Token expired / invalid"}

        def fake_get(url, *a, **k):
            http_urls.append(url)
            return _AuthRejectedResp(status_code)

        monkeypatch.setattr("requests.get", fake_get)

        live_feed = MStockLiveFeed(base_url="https://api.mstock.test")
        bar_feed = MStockBarFeed(feed_client=live_feed, poll_interval_s=60.0)
        bar_feed.add_symbols(["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"])
        monkeypatch.setattr(bar_feed, "_market_open", lambda: True)

        # Symbol #1 ("NIFTY" / first in set) hits instruments/intraday -> 401/403:
        # Must immediately trip kill-latch, skip quote/historical fallbacks AND
        # abort symbols #2..#5 with 0 retry sleeps!
        delivered = bar_feed._poll_once()
        assert delivered == 0
        assert len(http_urls) == 1, f"expected 1 HTTP packet before kill-latch, got {http_urls}"
        assert sleeps == [], "401/403 must never sleep/retry"

        assert mgr.is_authenticated("mstock") is False
        assert mgr.get_active_session_token("mstock") is None
        assert broker.get_session_status()["status"] == STATUS_EXPIRED
        assert mgr.get_all_status()["mstock"]["status"] == STATUS_EXPIRED
        assert mgr.consume_expiry_events() == [{"broker": "mstock", "kind": "expired"}]

        # Ticket A4 CRITICAL session-expired alert opened immediately:
        open_keys = {a.key: a for a in get_alert_broker().open_alerts()}
        assert "broker_session_expired:mstock" in open_keys
        assert open_keys["broker_session_expired:mstock"].severity == "critical"

        # Subsequent sweep sends 0 packets at Layer 0:
        assert bar_feed._poll_once() == 0
        assert len(http_urls) == 1

    def test_mstock_broker_order_and_option_quote_401_latch_expired_and_hold_alert_post_close(
        self, monkeypatch
    ):
        broker, mgr = _seed_live_mstock_session()

        # Outside market hours (`POST_CLOSE`): session still flips to `expired`
        # immediately, while Ticket A4 holds back operator paging until 09:00 IST.
        monkeypatch.setattr(
            "backtest.live.market_status.get_market_day_state",
            lambda *_a, **_k: {"session_state": "POST_CLOSE", "is_open": False},
        )
        sleeps: list[float] = []
        monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

        class _UnauthResp:
            ok = False
            status_code = 401

            def json(self):
                return {"status": "error", "message": "Invalid token"}

            def raise_for_status(self):
                raise RuntimeError("401")

        calls = 0

        def fake_get(*a, **k):
            nonlocal calls
            calls += 1
            return _UnauthResp()

        monkeypatch.setattr("requests.get", fake_get)

        # 1. Option HTTP quote fallback hits 401 -> 0 retries, immediate STATUS_EXPIRED
        quote = broker.get_option_quote("NIFTY26NOV24500CE")
        assert quote == {}
        assert calls == 1
        assert sleeps == []
        assert broker.get_session_status()["status"] == STATUS_EXPIRED
        assert mgr.is_authenticated("mstock") is False
        assert get_alert_broker().open_alerts() == []  # held back post-close

        # 2. Subsequent order call fails closed in RAM with 0 network packets
        with pytest.raises(MStockOrderError, match="no active mStock session"):
            broker.get_order_book()
        assert calls == 1

        # 3. Explicit logout clears forced-expired state back to unauthenticated
        mgr.logout("mstock")
        assert broker.get_session_status()["status"] == STATUS_UNAUTHENTICATED
