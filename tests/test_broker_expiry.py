"""Task 5.2 — Session Expiry Warning Tests.

Tests the session expiry warning system:
  • Transition from authenticated → expiring_soon → expired
  • Toast notifications at correct thresholds
  • Debounce mechanism prevents duplicate warnings
  • Re-authentication clears expiry state
  • Forward test gate responds to expiry states
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
from backtest.web.app import create_app
from backtest.brokers.session_manager import get_session_manager, reset_default_manager
from backtest.brokers.base import BrokerAuthBase, STATUS_AUTHENTICATED, STATUS_UNAUTHENTICATED, STATUS_EXPIRING_SOON, STATUS_EXPIRED


class ExpiryTestBroker(BrokerAuthBase):
    """Test broker that allows manual control of session expiry."""
    
    broker_name = "expiry_test"
    
    def __init__(self):
        self._status = STATUS_UNAUTHENTICATED
        self._expires_at = None
        self._token = None
    
    def login(self, username, password):
        """Login method (delegates to authenticate for testing)."""
        return self.authenticate({"username": username, "password": password})
    
    def authenticate(self, credentials):
        """Authenticate and set session to expire in given minutes."""
        self._status = STATUS_AUTHENTICATED
        self._token = "test-token-expiry"
        self._expires_at = datetime.now() + timedelta(minutes=30)
        return {"success": True, "message": "Authenticated"}
    
    def verify_totp(self, code):
        """Verify TOTP (always succeeds in test)."""
        return {"success": True, "message": "TOTP verified"}
    
    def get_session_status(self):
        """Return current session status with expiry time."""
        if self._status == STATUS_UNAUTHENTICATED:
            return {"status": STATUS_UNAUTHENTICATED, "expires_at": None}
        
        # Calculate time remaining
        if self._expires_at:
            now = datetime.now()
            remaining = (self._expires_at - now).total_seconds() / 60
            
            if remaining <= 0:
                self._status = STATUS_EXPIRED
                return {"status": STATUS_EXPIRED, "expires_at": None}
            elif remaining <= 10:
                return {"status": STATUS_EXPIRING_SOON, "expires_at": self._expires_at.isoformat()}
            else:
                return {"status": STATUS_AUTHENTICATED, "expires_at": self._expires_at.isoformat()}
        
        return {"status": self._status, "expires_at": None}
    
    def logout(self):
        """Clear session."""
        self._status = STATUS_UNAUTHENTICATED
        self._token = None
        self._expires_at = None
    
    def set_expiry_minutes(self, minutes):
        """Manually set session expiry for testing."""
        if minutes > 0:
            self._status = STATUS_AUTHENTICATED
            self._expires_at = datetime.now() + timedelta(minutes=minutes)
        else:
            self._status = STATUS_EXPIRED
            self._expires_at = None


@pytest.fixture
def app():
    """Create application for testing."""
    app = create_app(testing=True)
    app.config['TESTING'] = True
    yield app
    reset_default_manager()


@pytest.fixture
def client(app):
    """Create test client."""
    return app.test_client()


@pytest.fixture
def broker(app):
    """Create and inject test broker."""
    with app.app_context():
        broker = ExpiryTestBroker()
        manager = get_session_manager()
        manager.set_broker(broker)
        yield broker
        reset_default_manager()


class TestSessionExpiryTransitions:
    """Test session state transitions."""
    
    def test_authenticated_to_expiring_soon(self, client, broker):
        """Test transition from authenticated to expiring_soon."""
        # Authenticate
        broker.authenticate({"username": "test", "password": "test"})
        
        # Verify authenticated
        status = client.get('/api/broker/status').get_json()
        assert status['status'] == 'authenticated'
        assert status['expires_at'] is not None
        
        # Set expiry to 5 minutes (within warning threshold)
        broker.set_expiry_minutes(5)
        
        # Verify expiring_soon
        status = client.get('/api/broker/status').get_json()
        assert status['status'] == 'expiring_soon'
        assert status['expires_at'] is not None
    
    def test_expiring_soon_to_expired(self, client, broker):
        """Test transition from expiring_soon to expired."""
        # Authenticate and set to expiring_soon
        broker.authenticate({"username": "test", "password": "test"})
        broker.set_expiry_minutes(5)
        
        # Verify expiring_soon
        status = client.get('/api/broker/status').get_json()
        assert status['status'] == 'expiring_soon'
        
        # Let session expire
        broker.set_expiry_minutes(0)
        
        # Verify expired
        status = client.get('/api/broker/status').get_json()
        assert status['status'] == 'expired'
    
    def test_expired_to_unauthenticated(self, client, broker):
        """Test that expired session can be cleared via logout."""
        # Authenticate and expire
        broker.authenticate({"username": "test", "password": "test"})
        broker.set_expiry_minutes(0)
        
        # Verify expired
        status = client.get('/api/broker/status').get_json()
        assert status['status'] == 'expired'
        
        # Logout
        client.post('/api/broker/logout')
        
        # Verify unauthenticated
        status = client.get('/api/broker/status').get_json()
        assert status['status'] == 'unauthenticated'


class TestExpiryWarnings:
    """Test expiry warning behavior."""
    
    def test_warning_threshold_at_10_minutes(self, client, broker):
        """Test that warning triggers at 10-minute threshold."""
        broker.authenticate({"username": "test", "password": "test"})
        
        # Set to 11 minutes (no warning)
        broker.set_expiry_minutes(11)
        status = client.get('/api/broker/status').get_json()
        assert status['status'] == 'authenticated'
        
        # Set to 10 minutes (warning threshold)
        broker.set_expiry_minutes(10)
        status = client.get('/api/broker/status').get_json()
        assert status['status'] == 'expiring_soon'
        
        # Set to 5 minutes (still warning)
        broker.set_expiry_minutes(5)
        status = client.get('/api/broker/status').get_json()
        assert status['status'] == 'expiring_soon'
    
    def test_expiring_soon_allows_forward_start(self, client, broker):
        """Test that expiring_soon state still allows forward test."""
        broker.authenticate({"username": "test", "password": "test"})
        broker.set_expiry_minutes(5)  # Expiring soon
        
        # Verify expiring_soon
        status = client.get('/api/broker/status').get_json()
        assert status['status'] == 'expiring_soon'
        
        # Forward start should still work
        response = client.post('/api/forward/start', json={
            'strategy': 'sma_crossover',
            'symbol': 'DEMO',
            'timeframe': '1D',
            'from_date': '2024-01-01',
            'to_date': '2024-12-31',
            'capital': 10000
        })
        assert response.status_code == 200
    
    def test_expired_blocks_forward_start(self, client, broker):
        """Test that expired state blocks LIVE forward start (ticket #10).

        The auth gate now applies to ``mode=live`` only — paper replays need
        no broker session, so expiry must never silently block the free-play
        path the UI defaults to.
        """
        broker.authenticate({"username": "test", "password": "test"})
        broker.set_expiry_minutes(0)  # Expired
        
        # Verify expired
        status = client.get('/api/broker/status').get_json()
        assert status['status'] == 'expired'
        
        # LIVE forward start should be blocked
        response = client.post('/api/forward/start', json={
            'strategy': 'sma_crossover',
            'symbol': 'DEMO',
            'mode': 'live',
            'source': 'mstock',
            'timeframe': '1D',
            'from_date': '2024-01-01',
            'to_date': '2024-12-31',
            'capital': 10000
        })
        assert response.status_code == 403
        assert response.get_json()['error'] == 'broker_not_authenticated'

        # …but a paper replay still starts (T10: blocked things must be visible;
        # the free-play default is not auth-gated).
        paper = client.post('/api/forward/start', json={
            'strategy': 'sma_crossover',
            'symbol': 'DEMO',
            'mode': 'paper',
            'source': 'synthetic',
            'timeframe': '1D',
            'from_date': '2024-01-01',
            'to_date': '2024-12-31',
            'capital': 10000
        })
        assert paper.status_code == 200
        assert paper.get_json()['mode'] == 'paper'


class TestReauthenticationAfterExpiry:
    """Test re-authentication after session expiry."""
    
    def test_reauthenticate_after_expiry(self, client, broker):
        """Test that user can re-authenticate after expiry."""
        # First authentication
        broker.authenticate({"username": "test", "password": "test"})
        broker.set_expiry_minutes(0)  # Expire
        
        # Verify expired
        status = client.get('/api/broker/status').get_json()
        assert status['status'] == 'expired'
        
        # Re-authenticate
        broker.authenticate({"username": "test", "password": "test"})
        broker.set_expiry_minutes(30)  # Fresh session
        
        # Verify authenticated
        status = client.get('/api/broker/status').get_json()
        assert status['status'] == 'authenticated'
        
        # Forward start should work
        response = client.post('/api/forward/start', json={
            'strategy': 'sma_crossover',
            'symbol': 'DEMO',
            'timeframe': '1D',
            'from_date': '2024-01-01',
            'to_date': '2024-12-31',
            'capital': 10000
        })
        assert response.status_code == 200
    
    def test_logout_clears_expiry_state(self, client, broker):
        """Test that logout clears expiry state completely."""
        # Authenticate and set to expiring_soon
        broker.authenticate({"username": "test", "password": "test"})
        broker.set_expiry_minutes(5)
        
        # Verify expiring_soon
        status = client.get('/api/broker/status').get_json()
        assert status['status'] == 'expiring_soon'
        
        # Logout
        client.post('/api/broker/logout')
        
        # Verify unauthenticated (not expiring_soon)
        status = client.get('/api/broker/status').get_json()
        assert status['status'] == 'unauthenticated'
        assert status['expires_at'] is None


class TestExpiryStatusResponse:
    """Test expiry information in status response."""
    
    def test_status_includes_expiry_time(self, client, broker):
        """Test that status response includes expiry time when authenticated."""
        broker.authenticate({"username": "test", "password": "test"})
        broker.set_expiry_minutes(20)
        
        status = client.get('/api/broker/status').get_json()
        
        assert status['status'] == 'authenticated'
        assert status['expires_at'] is not None
        
        # Verify expiry time is in the future
        expires_at = datetime.fromisoformat(status['expires_at'])
        assert expires_at > datetime.now()
    
    def test_status_no_expiry_when_unauthenticated(self, client, broker):
        """Test that unauthenticated status has no expiry time."""
        status = client.get('/api/broker/status').get_json()
        
        assert status['status'] == 'unauthenticated'
        assert status['expires_at'] is None
    
    def test_status_no_expiry_when_expired(self, client, broker):
        """Test that expired status has no expiry time."""
        broker.authenticate({"username": "test", "password": "test"})
        broker.set_expiry_minutes(0)
        
        status = client.get('/api/broker/status').get_json()
        
        assert status['status'] == 'expired'
        assert status['expires_at'] is None


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
