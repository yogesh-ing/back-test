"""Task 5.3 — Security Verification Tests.

Automated security verification for the broker authentication system:
  • Credential handling (passwords, TOTP, API keys)
  • Session token security
  • API security (method restrictions, input validation, error handling)
  • Forward test security gates
  • Browser security (no sensitive data in responses)
  • Logging security (no sensitive data in logs)
"""

import pytest
import json
import re
from unittest.mock import Mock, patch
from backtest.web.app import create_app
from backtest.brokers.session_manager import get_session_manager, reset_default_manager
from backtest.brokers.base import BrokerAuthBase, STATUS_AUTHENTICATED, STATUS_UNAUTHENTICATED, STATUS_EXPIRING_SOON, STATUS_EXPIRED


class SecurityTestBroker(BrokerAuthBase):
    """Test broker for security verification."""
    
    broker_name = "security_test"
    
    def __init__(self):
        self._status = STATUS_UNAUTHENTICATED
        self._token = None
    
    def login(self, username, password):
        """Login method for testing."""
        self._status = STATUS_AUTHENTICATED
        self._token = "security-test-token-12345"
        return {"success": True, "message": "Authenticated"}
    
    def authenticate(self, credentials):
        self._status = STATUS_AUTHENTICATED
        self._token = "security-test-token-12345"
        return {"success": True, "message": "Authenticated"}
    
    def verify_totp(self, code):
        return {"success": True, "message": "TOTP verified"}
    
    def get_session_status(self):
        return {"status": self._status, "expires_at": None}
    
    def get_session_token(self):
        return self._token if self._status == STATUS_AUTHENTICATED else None
    
    def logout(self):
        self._status = STATUS_UNAUTHENTICATED
        self._token = None


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
        broker = SecurityTestBroker()
        manager = get_session_manager()
        manager.set_broker(broker)
        yield broker
        reset_default_manager()


class TestCredentialSecurity:
    """Test credential handling security."""
    
    def test_password_never_in_login_response(self, client, broker):
        """Verify password is never included in login response."""
        password = "super_secret_password_123"
        response = client.post('/api/broker/login', json={
            'username': 'test_user',
            'password': password
        })
        
        response_text = response.get_data(as_text=True)
        response_json = response.get_json()
        
        # Password should not appear in response
        assert password not in response_text
        assert password not in json.dumps(response_json)
        assert 'password' not in response_json
    
    def test_totp_code_never_in_verify_response(self, client, broker):
        """Verify TOTP code is never included in verification response."""
        broker.authenticate({"username": "test", "password": "test"})
        
        totp_code = "987654"
        response = client.post('/api/broker/verify-totp', json={
            'code': totp_code
        })
        
        response_text = response.get_data(as_text=True)
        response_json = response.get_json()
        
        # TOTP code should not appear in response
        assert totp_code not in response_text
        assert totp_code not in json.dumps(response_json)
        assert 'code' not in response_json
    
    def test_api_key_not_in_status_response(self, client, broker):
        """Verify API key is never included in status response."""
        broker.authenticate({"username": "test", "password": "test"})
        
        response = client.get('/api/broker/status')
        response_text = response.get_data(as_text=True)
        response_json = response.get_json()
        
        # API key should not appear in response
        assert 'api_key' not in response_json
        assert 'apiKey' not in response_json
        assert 'MSTOCK_API_KEY' not in response_text


class TestSessionTokenSecurity:
    """Test session token security."""
    
    def test_session_token_never_in_login_response(self, client, broker):
        """Verify session token is never sent to browser in login response."""
        response = client.post('/api/broker/login', json={
            'username': 'test_user',
            'password': 'test_password'
        })
        
        response_text = response.get_data(as_text=True)
        response_json = response.get_json()
        
        # Token should not appear in response
        assert 'token' not in response_json
        assert 'session_token' not in response_json
        assert 'security-test-token-12345' not in response_text
    
    def test_session_token_never_in_totp_response(self, client, broker):
        """Verify session token is never sent to browser in TOTP response."""
        broker.authenticate({"username": "test", "password": "test"})
        
        response = client.post('/api/broker/verify-totp', json={
            'code': '123456'
        })
        
        response_text = response.get_data(as_text=True)
        response_json = response.get_json()
        
        # Token should not appear in response
        assert 'token' not in response_json
        assert 'session_token' not in response_json
        assert 'security-test-token-12345' not in response_text
    
    def test_session_token_never_in_status_response(self, client, broker):
        """Verify session token is never sent to browser in status response."""
        broker.authenticate({"username": "test", "password": "test"})
        
        response = client.get('/api/broker/status')
        response_text = response.get_data(as_text=True)
        response_json = response.get_json()
        
        # Token should not appear in response
        assert 'token' not in response_json
        assert 'session_token' not in response_json
        assert 'security-test-token-12345' not in response_text
    
    def test_session_token_never_in_logout_response(self, client, broker):
        """Verify session token is never sent to browser in logout response."""
        broker.authenticate({"username": "test", "password": "test"})
        
        response = client.post('/api/broker/logout')
        response_text = response.get_data(as_text=True)
        response_json = response.get_json()
        
        # Token should not appear in response
        assert 'token' not in response_json
        assert 'session_token' not in response_json
        assert 'security-test-token-12345' not in response_text


class TestAPIMethodSecurity:
    """Test API method restrictions."""
    
    def test_login_requires_post(self, client):
        """Verify login endpoint only accepts POST."""
        response = client.get('/api/broker/login')
        assert response.status_code == 405  # Method Not Allowed
        
        response = client.put('/api/broker/login')
        assert response.status_code == 405
        
        response = client.delete('/api/broker/login')
        assert response.status_code == 405
    
    def test_verify_totp_requires_post(self, client):
        """Verify TOTP endpoint only accepts POST."""
        response = client.get('/api/broker/verify-totp')
        assert response.status_code == 405
        
        response = client.put('/api/broker/verify-totp')
        assert response.status_code == 405
    
    def test_status_requires_get(self, client):
        """Verify status endpoint only accepts GET."""
        response = client.post('/api/broker/status')
        assert response.status_code == 405
        
        response = client.put('/api/broker/status')
        assert response.status_code == 405
    
    def test_logout_requires_post(self, client):
        """Verify logout endpoint only accepts POST."""
        response = client.get('/api/broker/logout')
        assert response.status_code == 405
        
        response = client.put('/api/broker/logout')
        assert response.status_code == 405


class TestInputValidation:
    """Test input validation security."""
    
    def test_login_validates_username_required(self, client):
        """Verify login validates username is required."""
        response = client.post('/api/broker/login', json={
            'password': 'test_password'
        })
        assert response.status_code == 400
    
    def test_login_validates_password_required(self, client):
        """Verify login validates password is required."""
        response = client.post('/api/broker/login', json={
            'username': 'test_user'
        })
        assert response.status_code == 400
    
    def test_login_validates_json_content_type(self, client):
        """Verify login validates JSON content type."""
        response = client.post('/api/broker/login',
            data='username=test&password=test',
            content_type='application/x-www-form-urlencoded')
        assert response.status_code == 400 or response.status_code == 415
    
    def test_login_validates_json_structure(self, client):
        """Verify login validates JSON structure."""
        response = client.post('/api/broker/login',
            data='invalid json',
            content_type='application/json')
        assert response.status_code == 400
    
    def test_totp_validates_code_required(self, client, broker):
        """Verify TOTP validates code is required."""
        broker.authenticate({"username": "test", "password": "test"})
        
        response = client.post('/api/broker/verify-totp', json={})
        assert response.status_code == 400
    
    def test_totp_validates_code_format(self, client, broker):
        """Verify TOTP validates code format (6 digits)."""
        broker.authenticate({"username": "test", "password": "test"})
        
        # Too short
        response = client.post('/api/broker/verify-totp', json={'code': '123'})
        assert response.status_code == 400
        
        # Non-numeric
        response = client.post('/api/broker/verify-totp', json={'code': 'abcdef'})
        assert response.status_code == 400
        
        # Too long
        response = client.post('/api/broker/verify-totp', json={'code': '1234567'})
        assert response.status_code == 400


class TestErrorHandlingSecurity:
    """Test error handling security."""
    
    def test_login_error_no_stack_trace(self, client):
        """Verify login errors don't include stack traces."""
        response = client.post('/api/broker/login', json={
            'username': 'test',
            'password': 'wrong'
        })
        
        response_text = response.get_data(as_text=True)
        
        # Stack trace indicators should not appear
        assert 'Traceback' not in response_text
        assert 'File "' not in response_text
        assert 'raise ' not in response_text
    
    def test_totp_error_no_stack_trace(self, client, broker):
        """Verify TOTP errors don't include stack traces."""
        broker.authenticate({"username": "test", "password": "test"})
        
        response = client.post('/api/broker/verify-totp', json={
            'code': '000000'
        })
        
        response_text = response.get_data(as_text=True)
        
        # Stack trace indicators should not appear
        assert 'Traceback' not in response_text
        assert 'File "' not in response_text
    
    def test_generic_error_messages(self, client):
        """Verify error messages are generic (don't reveal details)."""
        response = client.post('/api/broker/login', json={
            'username': 'nonexistent_user',
            'password': 'wrong_password'
        })
        
        response_json = response.get_json()
        
        # Error message should be generic
        error_msg = response_json.get('message', '').lower()
        assert 'user not found' not in error_msg
        assert 'invalid password' not in error_msg
        # Should say something like "Invalid credentials" instead


class TestForwardTestSecurity:
    """Test forward test security gates."""
    
    def test_forward_start_requires_authentication(self, client):
        """Verify forward start requires authentication."""
        response = client.post('/api/forward/start', json={
            'strategy': 'sma_crossover',
            'symbol': 'DEMO',
            'timeframe': '1D',
            'from_date': '2024-01-01',
            'to_date': '2024-12-31',
            'capital': 10000
        })
        
        assert response.status_code == 403
        response_json = response.get_json()
        assert response_json['error'] == 'broker_not_authenticated'
    
    def test_forward_start_blocked_with_expired_session(self, client, broker):
        """Verify forward start is blocked with expired session."""
        broker.authenticate({"username": "test", "password": "test"})
        broker.logout()  # Simulate expiry
        
        response = client.post('/api/forward/start', json={
            'strategy': 'sma_crossover',
            'symbol': 'DEMO',
            'timeframe': '1D',
            'from_date': '2024-01-01',
            'to_date': '2024-12-31',
            'capital': 10000
        })
        
        assert response.status_code == 403
    
    def test_forward_start_allowed_when_authenticated(self, client, broker):
        """Verify forward start is allowed when authenticated."""
        broker.authenticate({"username": "test", "password": "test"})
        
        response = client.post('/api/forward/start', json={
            'strategy': 'sma_crossover',
            'symbol': 'DEMO',
            'timeframe': '1D',
            'from_date': '2024-01-01',
            'to_date': '2024-12-31',
            'capital': 10000
        })
        
        assert response.status_code == 200


class TestBrowserSecurity:
    """Test browser-side security."""
    
    def test_no_sensitive_data_in_any_response(self, client, broker):
        """Comprehensive test: no sensitive data in any API response."""
        sensitive_data = [
            'super_secret_password',
            'test_api_key_12345',
            'security-test-token-12345',
            'private_key_data',
        ]
        
        # Test all endpoints
        endpoints = [
            ('POST', '/api/broker/login', {'username': 'test', 'password': 'super_secret_password'}),
            ('POST', '/api/broker/verify-totp', {'code': '123456'}),
            ('GET', '/api/broker/status', None),
            ('POST', '/api/broker/logout', None),
        ]
        
        broker.authenticate({"username": "test", "password": "super_secret_password"})
        
        for method, endpoint, data in endpoints:
            if method == 'GET':
                response = client.get(endpoint)
            else:
                response = client.post(endpoint, json=data)
            
            response_text = response.get_data(as_text=True)
            
            for sensitive in sensitive_data:
                assert sensitive not in response_text, \
                    f"Sensitive data '{sensitive}' found in {method} {endpoint} response"
    
    def test_response_content_type_is_json(self, client):
        """Verify all API responses have correct content type."""
        endpoints = [
            ('GET', '/api/broker/status'),
            ('POST', '/api/broker/login'),
            ('POST', '/api/broker/verify-totp'),
            ('POST', '/api/broker/logout'),
        ]
        
        for method, endpoint in endpoints:
            if method == 'GET':
                response = client.get(endpoint)
            else:
                response = client.post(endpoint, json={})
            
            assert 'application/json' in response.content_type


class TestLoggingSecurity:
    """Test logging security (no sensitive data in logs)."""
    
    def test_login_does_not_log_password(self, client, caplog):
        """Verify login doesn't log passwords."""
        import logging
        
        with caplog.at_level(logging.DEBUG):
            client.post('/api/broker/login', json={
                'username': 'test_user',
                'password': 'super_secret_password'
            })
        
        log_text = caplog.text.lower()
        assert 'super_secret_password' not in log_text
        assert 'password' not in log_text or 'password field' in log_text
    
    def test_totp_does_not_log_code(self, client, broker, caplog):
        """Verify TOTP verification doesn't log codes."""
        import logging
        
        broker.authenticate({"username": "test", "password": "test"})
        
        with caplog.at_level(logging.DEBUG):
            client.post('/api/broker/verify-totp', json={
                'code': '987654'
            })
        
        log_text = caplog.text
        assert '987654' not in log_text


class TestSQLInjectionSecurity:
    """Test SQL injection prevention."""
    
    def test_login_prevents_sql_injection(self, client, broker):
        """Verify login prevents SQL injection attempts.
        
        Note: Our stub broker always returns success for testing, so we verify
        that injection attempts don't cause unexpected behavior or expose data.
        In production, the real broker would reject these attempts.
        """
        injection_attempts = [
            'admin" OR "1"="1',
            "admin' OR '1'='1",
            'admin"; DROP TABLE users;--',
            "admin' UNION SELECT * FROM users--",
        ]
        
        for attempt in injection_attempts:
            response = client.post('/api/broker/login', json={
                'username': attempt,
                'password': 'test'
            })
            
            # Verify response is valid JSON (no syntax errors from injection)
            assert response.status_code == 200
            data = response.get_json()
            
            # Verify response structure is correct (no data leakage)
            assert 'success' in data
            assert 'token' not in data  # No token leakage
            assert 'session_token' not in data
            
            # Verify the injection attempt doesn't appear in response
            response_text = response.get_data(as_text=True)
            assert attempt not in response_text


class TestXSSSecurity:
    """Test XSS prevention."""
    
    def test_login_prevents_xss(self, client):
        """Verify login prevents XSS attempts."""
        xss_attempts = [
            '<script>alert("XSS")</script>',
            '<img src=x onerror=alert("XSS")>',
            'javascript:alert("XSS")',
            '<svg onload=alert("XSS")>',
        ]
        
        for attempt in xss_attempts:
            response = client.post('/api/broker/login', json={
                'username': attempt,
                'password': 'test'
            })
            
            response_text = response.get_data(as_text=True)
            
            # XSS attempt should not be reflected in response
            assert attempt not in response_text


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
