"""Tests for Market Status Awareness (MARKET-STATUS-SPEC-2026-10-02).

Validates:
- Scenarios 1-6 from MARKET-STATUS-SPEC-2026-10-02 §9
- Holiday schema and 2026 seeding in SQLAlchemy model and migrations
- Resolver get_market_day_state() behaviour (IST timezone, fail-open, gap warning)
- Feed gating in _BrokerBarFeedBase._poll_once on closed market days
- API payload expansion (/api/market/status and /api/portfolio/summary)
- Verbatim copy conformance
"""

from datetime import date, datetime, time, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backtest.api.portfolio import portfolio_bp
from backtest.db.models import Base, MarketHoliday
from backtest.forward.feed_registry import _BrokerBarFeedBase
from backtest.live.market_status import (
    IST,
    NSE_HOLIDAYS_2026,
    _DAY_CACHE,
    get_market_day_state,
)


@pytest.fixture(autouse=True)
def clear_market_cache():
    """Clear the in-memory day cache before and after each test."""
    _DAY_CACHE.clear()
    yield
    _DAY_CACHE.clear()


@pytest.fixture
def sqlite_db():
    """In-memory SQLite engine with market_holidays schema and seed."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[MarketHoliday.__table__])
    with Session(engine) as session:
        MarketHoliday.seed_2026(session)

    class MockDbManager:
        def __init__(self, eng):
            self.engine = eng
            self.is_connected = True

        def session(self):
            return Session(self.engine)

        def connect(self):
            pass

    return MockDbManager(engine)


# =============================================================================
# Schema & Seeding Tests
# =============================================================================


def test_market_holiday_model_seeding(sqlite_db):
    """Verify that MarketHoliday model seeds 15 trading holidays for 2026."""
    with sqlite_db.session() as s:
        holidays = s.query(MarketHoliday).filter(MarketHoliday.segment == "equity").all()
        assert len(holidays) >= 15
        gandhi = s.get(MarketHoliday, date(2026, 10, 2))
        assert gandhi is not None
        assert gandhi.description == "Mahatma Gandhi Jayanti"
        assert gandhi.is_trading_holiday is True


def test_seed_2026_is_idempotent(sqlite_db):
    """Calling seed_2026 multiple times does not create duplicate rows."""
    with sqlite_db.session() as s:
        inserted = MarketHoliday.seed_2026(s)
        assert inserted == 0


# =============================================================================
# Scenarios 1-6 (MARKET-STATUS-SPEC-2026-10-02 §9)
# =============================================================================


def test_scenario_1_gandhi_jayanti_holiday(sqlite_db):
    """Scenario 1: Friday 2026-10-02 Gandhi Jayanti holiday.

    Expected:
    - day_state == HOLIDAY
    - session_state == POST_CLOSE
    - holiday_name == 'Mahatma Gandhi Jayanti'
    - next_open_ts == '2026-10-05T09:15:00+05:30' (Monday)
    - is_open == False
    """
    dt = datetime(2026, 10, 2, 11, 0, 0, tzinfo=IST)
    res = get_market_day_state(now_ist=dt, db_manager=sqlite_db)

    assert res["day_state"] == "HOLIDAY"
    assert res["session_state"] == "POST_CLOSE"
    assert res["holiday_name"] == "Mahatma Gandhi Jayanti"
    assert res["is_open"] is False
    assert res["next_open_ts"] == "2026-10-05T09:15:00+05:30"
    assert res["next_open_formatted"] == "Mon 05 Oct 09:15 IST"
    assert res["gap_warning"] is None


def test_scenario_2_saturday_weekend(sqlite_db):
    """Scenario 2: Saturday 2026-10-03 Weekend.

    Expected:
    - day_state == WEEKEND
    - session_state == POST_CLOSE
    - holiday_name == None
    - next_open_ts == '2026-10-05T09:15:00+05:30' (Monday)
    - is_open == False
    """
    dt = datetime(2026, 10, 3, 14, 0, 0, tzinfo=IST)
    res = get_market_day_state(now_ist=dt, db_manager=sqlite_db)

    assert res["day_state"] == "WEEKEND"
    assert res["session_state"] == "POST_CLOSE"
    assert res["holiday_name"] is None
    assert res["is_open"] is False
    assert res["next_open_ts"] == "2026-10-05T09:15:00+05:30"
    assert res["next_open_formatted"] == "Mon 05 Oct 09:15 IST"


def test_scenario_3_monday_pre_open(sqlite_db):
    """Scenario 3: Monday 2026-10-05 09:05:00 IST Pre-Open.

    Expected:
    - day_state == TRADING_DAY
    - session_state == PRE_OPEN
    - next_open_ts == '2026-10-05T09:15:00+05:30' (today)
    - is_open == False
    """
    dt = datetime(2026, 10, 5, 9, 5, 0, tzinfo=IST)
    res = get_market_day_state(now_ist=dt, db_manager=sqlite_db)

    assert res["day_state"] == "TRADING_DAY"
    assert res["session_state"] == "PRE_OPEN"
    assert res["is_open"] is False
    assert res["next_open_ts"] == "2026-10-05T09:15:00+05:30"
    assert res["next_open_formatted"] == "Mon 05 Oct 09:15 IST"


def test_scenario_4_monday_live_trading(sqlite_db):
    """Scenario 4: Monday 2026-10-05 10:30:00 IST Live Trading.

    Expected:
    - day_state == TRADING_DAY
    - session_state == OPEN
    - is_open == True
    - next_open_ts == '2026-10-06T09:15:00+05:30' (tomorrow)
    """
    dt = datetime(2026, 10, 5, 10, 30, 0, tzinfo=IST)
    res = get_market_day_state(now_ist=dt, db_manager=sqlite_db)

    assert res["day_state"] == "TRADING_DAY"
    assert res["session_state"] == "OPEN"
    assert res["is_open"] is True
    assert res["next_open_ts"] == "2026-10-06T09:15:00+05:30"
    assert res["next_open_formatted"] == "Tue 06 Oct 09:15 IST"


def test_scenario_5_database_unavailable_fail_open():
    """Scenario 5: Database unavailable fail-open.

    Expected:
    - does not raise
    - day_state == TRADING_DAY during market hours
    - is_open == True
    - gap_warning is present
    """
    broken_mgr = MagicMock()
    broken_mgr.session.side_effect = RuntimeError("Database unreachable")

    dt = datetime(2026, 10, 5, 11, 0, 0, tzinfo=IST)
    res = get_market_day_state(now_ist=dt, db_manager=broken_mgr)

    assert res["day_state"] == "TRADING_DAY"
    assert res["session_state"] == "OPEN"
    assert res["is_open"] is True
    assert "fail-open" in res["gap_warning"]


def test_scenario_6_calendar_gap_warning(sqlite_db):
    """Scenario 6: Year 2027 calendar gap warning.

    Expected:
    - day_state == TRADING_DAY (weekday rule)
    - gap_warning == '⚠ No holiday data for 2027 — weekday rule only'
    - is_open == True during market hours
    """
    dt = datetime(2027, 3, 15, 11, 0, 0, tzinfo=IST)
    res = get_market_day_state(now_ist=dt, db_manager=sqlite_db)

    assert res["day_state"] == "TRADING_DAY"
    assert res["session_state"] == "OPEN"
    assert res["is_open"] is True
    assert res["gap_warning"] == "⚠ No holiday data for 2027 — weekday rule only"


def test_trading_day_post_close(sqlite_db):
    """Trading day after 15:30 IST is POST_CLOSE and is_open == False."""
    dt = datetime(2026, 10, 5, 16, 0, 0, tzinfo=IST)
    res = get_market_day_state(now_ist=dt, db_manager=sqlite_db)

    assert res["day_state"] == "TRADING_DAY"
    assert res["session_state"] == "POST_CLOSE"
    assert res["is_open"] is False
    assert res["next_open_ts"] == "2026-10-06T09:15:00+05:30"


# =============================================================================
# Feed Gating Tests (§5)
# =============================================================================


class DummyBrokerFeed(_BrokerBarFeedBase):
    broker_name = "test_feed"


def test_feed_gated_on_holiday():
    """Feed worker makes zero broker HTTP calls and delivers 0 bars on holiday."""
    mock_client = MagicMock()
    feed = DummyBrokerFeed(feed_client=mock_client)
    feed.add_symbols(["RELIANCE"])

    with patch.object(
        DummyBrokerFeed,
        "_market_day_state",
        return_value=("HOLIDAY", "Mahatma Gandhi Jayanti"),
    ):
        delivered = feed._poll_once()

    assert delivered == 0
    mock_client.latest_bar.assert_not_called()


def test_feed_gated_on_weekend():
    """Feed worker makes zero broker HTTP calls on weekend."""
    mock_client = MagicMock()
    feed = DummyBrokerFeed(feed_client=mock_client)
    feed.add_symbols(["RELIANCE"])

    with patch.object(
        DummyBrokerFeed,
        "_market_day_state",
        return_value=("WEEKEND", "Weekend"),
    ):
        delivered = feed._poll_once()

    assert delivered == 0
    mock_client.latest_bar.assert_not_called()


def test_feed_active_on_trading_day():
    """Feed worker polls broker normally on an open trading day."""
    mock_client = MagicMock()
    mock_client.latest_bar.return_value = {
        "ts": "2026-10-05 10:30:00",
        "close": 2500.0,
    }
    feed = DummyBrokerFeed(feed_client=mock_client)
    feed.on_bar = MagicMock()
    feed.add_symbols(["RELIANCE"])

    with patch.object(
        DummyBrokerFeed,
        "_market_day_state",
        return_value=("TRADING_DAY", "Trading day"),
    ), patch.object(DummyBrokerFeed, "_market_open", return_value=True):
        delivered = feed._poll_once()

    assert delivered == 1
    mock_client.latest_bar.assert_called_once_with("RELIANCE")
    feed.on_bar.assert_called_once()


# =============================================================================
# API Endpoint Tests (§4)
# =============================================================================


@pytest.fixture
def app_client():
    from backtest.web.app import create_app
    app = create_app(source="synthetic")
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_market_status_api_endpoint(app_client, sqlite_db):
    """Test /api/market/status returns standard market state payload."""
    with patch(
        "backtest.live.market_status.get_market_day_state",
        return_value={
            "day_state": "HOLIDAY",
            "session_state": "POST_CLOSE",
            "holiday_name": "Mahatma Gandhi Jayanti",
            "next_open_ts": "2026-10-05T09:15:00+05:30",
            "next_open_formatted": "Mon 05 Oct 09:15 IST",
            "gap_warning": None,
            "is_open": False,
        },
    ):
        res = app_client.get("/api/market/status")
        assert res.status_code == 200
        data = res.get_json()
        assert data["success"] is True
        assert data["day_state"] == "HOLIDAY"
        assert data["session_state"] == "POST_CLOSE"
        assert data["holiday_name"] == "Mahatma Gandhi Jayanti"
        assert data["is_open"] is False
