"""Task 5.1 — Full Authentication Flow Integration Test.

This test verifies the complete authentication flow from unauthenticated → 
credentials → TOTP → authenticated → forward start → logout → re-auth.

It exercises all components together:
  • Nav pill status indicator (Task 3.1)
  • Auth modal flow (Task 3.2)
  • Session expiry handling (Task 3.3)
  • Client-side forward start gate (Task 4.1)
  • Server-side forward start guard (Task 4.2)

Run with: pytest tests/manual/test_auth_flow_integration.py -v
"""

from datetime import datetime, timedelta
from typing import Any

import pytest

from backtest.brokers.base import STATUS_AUTHENTICATED, STATUS_UNAUTHENTICATED, BrokerAuthBase
from backtest.brokers.session_manager import (
    BrokerSessionManager,
    get_session_manager,
    reset_default_manager,
)
from backtest.web.app import create_app


class _TestStubBroker(BrokerAuthBase):
    """Stub broker for integration testing."""

    broker_name = "test_stub"
    broker_display_name = "Test Stub Broker"

    def __init__(self) -> None:
        self._status = STATUS_UNAUTHENTICATED
        self._expires_at: str | None = None
        self.login_result: dict[str, Any] = {
            "success": True,
            "message": "Credentials verified",
            "requires_totp": True,
        }

    def login(self, username: str, password: str) -> dict[str, Any]:
        return dict(self.login_result)

    def verify_totp(self, totp_code: str) -> dict[str, Any]:
        self._status = STATUS_AUTHENTICATED
        self._expires_at = (datetime.now() + timedelta(hours=2)).isoformat()
        return {
            "success": True,
            "message": "session established",
            "expires_at": self._expires_at,
        }

    def get_session_status(self) -> dict[str, Any]:
        return {
            "status": self._status,
            "expires_at": self._expires_at,
            "broker": self.broker_name,
        }

    def get_session_token(self) -> str | None:
        return "test-token-12345" if self._status == STATUS_AUTHENTICATED else None

    def logout(self) -> None:
        self._status = STATUS_UNAUTHENTICATED
        self._expires_at = None


class TestFullAuthenticationFlow:
    """End-to-end authentication flow test (Task 5.1)."""

    @pytest.fixture
    def stub(self):
        """Create stub broker."""
        return _TestStubBroker()

    @pytest.fixture
    def app(self, stub):
        """Create application for testing with stub broker."""
        reset_default_manager()
        get_session_manager().set_broker(stub)
        app = create_app(testing=True, source="synthetic")
        app.config["TESTING"] = True
        yield app
        reset_default_manager()

    @pytest.fixture
    def client(self, app):
        """Create test client."""
        return app.test_client()

    def test_step1_initial_unauthenticated_state(self, client):
        """Step 1: Verify initial state is unauthenticated."""
        # Check broker status
        response = client.get("/api/broker/status")
        assert response.status_code == 200
        data = response.get_json()

        assert data["status"] == "unauthenticated"
        assert data["broker"] == "test_stub"
        assert "token" not in data
        assert "session_token" not in data

        # Verify LIVE forward start is blocked (paper needs no session — T10)
        response = client.post(
            "/api/forward/start",
            json={
                "strategy": "sma_crossover",
                "symbol": "DEMO",
                "mode": "live",
                "source": "mstock",
                "timeframe": "1D",
                "from_date": "2024-01-01",
                "to_date": "2024-12-31",
                "capital": 10000,
            },
        )
        assert response.status_code == 403
        data = response.get_json()
        assert data["error"] == "broker_not_authenticated"

    def test_step2_login_with_credentials(self, client, stub):
        """Step 2: Login with credentials (Step 1 of auth flow)."""
        response = client.post(
            "/api/broker/login", json={"username": "test_user", "password": "test_password"}
        )
        assert response.status_code == 200
        data = response.get_json()

        assert data["success"] is True
        assert data["requires_totp"] is True
        assert "token" not in data
        assert "session_token" not in data
        assert "test_password" not in str(data)  # Password not echoed

    def test_step3_verify_totp_completes_authentication(self, client, stub):
        """Step 3: Verify TOTP completes authentication (Step 2 of auth flow)."""
        # First, login with credentials
        login_resp = client.post(
            "/api/broker/login", json={"username": "test_user", "password": "test_password"}
        )
        assert login_resp.status_code == 200

        # Then verify TOTP
        totp_resp = client.post("/api/broker/verify-totp", json={"totp_code": "123456"})
        assert totp_resp.status_code == 200
        data = totp_resp.get_json()

        assert data["success"] is True
        assert "token" not in data
        assert "session_token" not in data

        # Verify session is now authenticated
        status_resp = client.get("/api/broker/status")
        status_data = status_resp.get_json()
        assert status_data["status"] == "authenticated"
        assert "token" not in status_data

        # Verify forward start now succeeds
        forward_resp = client.post(
            "/api/forward/start",
            json={
                "strategy": "sma_crossover",
                "symbol": "DEMO",
                "timeframe": "1D",
                "from_date": "2024-01-01",
                "to_date": "2024-12-31",
                "capital": 10000,
            },
        )
        assert forward_resp.status_code == 200
        forward_data = forward_resp.get_json()
        assert forward_data["status"] == "running"

    def test_step4_logout_clears_session(self, client, stub):
        """Step 4: Logout clears session and re-enables the gate."""
        # Authenticate first
        client.post(
            "/api/broker/login", json={"username": "test_user", "password": "test_password"}
        )
        client.post("/api/broker/verify-totp", json={"totp_code": "123456"})

        # Verify authenticated
        status_resp = client.get("/api/broker/status")
        assert status_resp.get_json()["status"] == "authenticated"

        # Logout
        logout_resp = client.post("/api/broker/logout")
        assert logout_resp.status_code == 200
        assert logout_resp.get_json()["success"] is True

        # Verify unauthenticated
        status_resp = client.get("/api/broker/status")
        status_data = status_resp.get_json()
        assert status_data["status"] == "unauthenticated"

        # Verify LIVE forward start is blocked again (T10: paper is not)
        forward_resp = client.post(
            "/api/forward/start",
            json={
                "strategy": "sma_crossover",
                "symbol": "DEMO",
                "mode": "live",
                "source": "mstock",
                "timeframe": "1D",
                "from_date": "2024-01-01",
                "to_date": "2024-12-31",
                "capital": 10000,
            },
        )
        assert forward_resp.status_code == 403

    def test_step5_reauthentication_flow(self, client, stub):
        """Step 5: Re-authentication flow works cleanly."""
        # First authentication
        client.post(
            "/api/broker/login", json={"username": "test_user", "password": "test_password"}
        )
        client.post("/api/broker/verify-totp", json={"totp_code": "123456"})

        # Logout
        client.post("/api/broker/logout")

        # Re-authenticate with different credentials
        login_resp = client.post(
            "/api/broker/login", json={"username": "another_user", "password": "another_password"}
        )
        assert login_resp.status_code == 200
        assert login_resp.get_json()["success"] is True

        totp_resp = client.post("/api/broker/verify-totp", json={"totp_code": "654321"})
        assert totp_resp.status_code == 200
        assert totp_resp.get_json()["success"] is True

        # Verify authenticated again
        status_resp = client.get("/api/broker/status")
        assert status_resp.get_json()["status"] == "authenticated"

        # Verify forward start works
        forward_resp = client.post(
            "/api/forward/start",
            json={
                "strategy": "sma_crossover",
                "symbol": "DEMO",
                "timeframe": "1D",
                "from_date": "2024-01-01",
                "to_date": "2024-12-31",
                "capital": 10000,
            },
        )
        assert forward_resp.status_code == 200

    def test_step6_security_no_token_in_responses(self, client, stub):
        """Step 6: Security verification — token never appears in responses."""
        # Collect all responses
        responses = []

        # Login
        login_resp = client.post(
            "/api/broker/login", json={"username": "test_user", "password": "test_password"}
        )
        responses.append(login_resp.get_json())

        # TOTP
        totp_resp = client.post("/api/broker/verify-totp", json={"totp_code": "123456"})
        responses.append(totp_resp.get_json())

        # Status
        status_resp = client.get("/api/broker/status")
        responses.append(status_resp.get_json())

        # Logout
        logout_resp = client.post("/api/broker/logout")
        responses.append(logout_resp.get_json())

        # Verify no token in any response
        for data in responses:
            assert "token" not in data
            assert "session_token" not in data

        # Verify passwords not echoed
        login_data = responses[0]
        assert "test_password" not in str(login_data)

    def test_complete_flow_all_steps(self, client, stub):
        """Complete flow test: all 7 steps in sequence."""
        # Step 1: Initial unauthenticated state
        status = client.get("/api/broker/status").get_json()
        assert status["status"] == "unauthenticated"

        forward = client.post(
            "/api/forward/start",
            json={
                "strategy": "sma_crossover",
                "symbol": "DEMO",
                "mode": "live",
                "source": "mstock",
                "timeframe": "1D",
                "from_date": "2024-01-01",
                "to_date": "2024-12-31",
                "capital": 10000,
            },
        )
        assert forward.status_code == 403

        # Step 2: Login with credentials
        login = client.post(
            "/api/broker/login", json={"username": "test_user", "password": "test_password"}
        )
        assert login.get_json()["success"] is True
        assert login.get_json()["requires_totp"] is True

        # Step 3: Verify TOTP
        totp = client.post("/api/broker/verify-totp", json={"totp_code": "123456"})
        assert totp.get_json()["success"] is True

        # Step 4: Verify authenticated
        status = client.get("/api/broker/status").get_json()
        assert status["status"] == "authenticated"

        # Step 5: Forward start succeeds
        forward = client.post(
            "/api/forward/start",
            json={
                "strategy": "sma_crossover",
                "symbol": "DEMO",
                "timeframe": "1D",
                "from_date": "2024-01-01",
                "to_date": "2024-12-31",
                "capital": 10000,
            },
        )
        assert forward.status_code == 200

        # Step 6: Logout
        logout = client.post("/api/broker/logout")
        assert logout.get_json()["success"] is True

        status = client.get("/api/broker/status").get_json()
        assert status["status"] == "unauthenticated"

        # Step 7: Re-authentication
        login = client.post(
            "/api/broker/login", json={"username": "test_user", "password": "test_password"}
        )
        assert login.get_json()["success"] is True

        totp = client.post("/api/broker/verify-totp", json={"totp_code": "654321"})
        assert totp.get_json()["success"] is True

        status = client.get("/api/broker/status").get_json()
        assert status["status"] == "authenticated"

        forward = client.post(
            "/api/forward/start",
            json={
                "strategy": "sma_crossover",
                "symbol": "DEMO",
                "timeframe": "1D",
                "from_date": "2024-01-01",
                "to_date": "2024-12-31",
                "capital": 10000,
            },
        )
        assert forward.status_code == 200


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
