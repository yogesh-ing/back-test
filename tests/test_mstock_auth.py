"""Acceptance tests for mStock auth (Card 07 live bring-up, deferred from this build)."""

import importlib
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_env_file_is_loaded_for_live_auth(tmp_path, monkeypatch):
    """Credentials in .env at the project root should populate the runtime environment."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        "MSTOCK_USERNAME=liveuser\n"
        "MSTOCK_PASSWORD=livepass\n"
        "MSTOCK_TOTP=123456\n"
        "MSTOCK_AUTH_MODE=totp\n"
        "MSTOCK_CHECKSUM=JBSWY3DPEHPK3PXP\n"
        "MSTOCK_BASE_URL=https://api.mstock.trade\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("MSTOCK_USERNAME", raising=False)
    monkeypatch.delenv("MSTOCK_PASSWORD", raising=False)
    monkeypatch.delenv("MSTOCK_TOTP", raising=False)
    monkeypatch.delenv("MSTOCK_AUTH_MODE", raising=False)
    monkeypatch.delenv("MSTOCK_CHECKSUM", raising=False)
    monkeypatch.delenv("MSTOCK_BASE_URL", raising=False)

    old_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        import backtest
        importlib.reload(backtest)
        assert os.getenv("MSTOCK_USERNAME") == "liveuser"
        assert os.getenv("MSTOCK_PASSWORD") == "livepass"
        assert os.getenv("MSTOCK_AUTH_MODE") == "totp"
    finally:
        os.chdir(old_cwd)


def test_get_auth_code_prompts_for_user_totp(monkeypatch):
    """TOTP must be entered by the user at runtime; never generated from the checksum alone."""
    monkeypatch.setenv("MSTOCK_AUTH_MODE", "totp")
    monkeypatch.delenv("MSTOCK_TOTP", raising=False)

    from backtest.live.auth import get_auth_code

    monkeypatch.setattr("builtins.input", lambda prompt="": "123456")
    assert get_auth_code() == "123456"


def test_login_sends_sdk_headers(monkeypatch):
    """Login must include the mStock API version header required by the TypeA service."""
    captured = {}

    class DummyResponse:
        status_code = 200
        headers = {"Content-Type": "application/json; charset=utf-8"}

        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "success"}

    def fake_post(url, data=None, headers=None, timeout=None):
        captured["url"] = url
        captured["data"] = data
        captured["headers"] = headers
        captured["timeout"] = timeout
        return DummyResponse()

    monkeypatch.setenv("MSTOCK_USERNAME", "user")
    monkeypatch.setenv("MSTOCK_PASSWORD", "pass")
    monkeypatch.setattr("requests.post", fake_post)

    from backtest.live.auth import login

    assert login() == {"status": "success"}
    assert captured["headers"]["X-Mirae-Version"] == "1"
    assert captured["headers"]["Content-Type"] == "application/x-www-form-urlencoded"


def test_totp_session_uses_login_then_prompt_then_verify_totp(monkeypatch, tmp_path):
    """Real flow from the SDK example: login first, prompt for TOTP, then verify it."""
    monkeypatch.setenv("MSTOCK_AUTH_MODE", "totp")
    monkeypatch.setenv("MSTOCK_USERNAME", "user")
    monkeypatch.setenv("MSTOCK_PASSWORD", "pass")
    monkeypatch.setenv("MSTOCK_API_KEY", "api-key")
    monkeypatch.setenv("MSTOCK_CHECKSUM", "W")
    monkeypatch.delenv("MSTOCK_TOTP", raising=False)
    monkeypatch.delenv("MSTOCK_OTP", raising=False)

    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    cache = Path(tmp_path / ".mstock_session_token")
    if cache.exists():
        cache.unlink()

    try:
        from backtest.live import auth as live_auth

        calls = []

        def fake_login():
            calls.append("login")
            return {"status": "success"}

        def fake_verify_totp(code):
            calls.append(("verify_totp", code))
            return {"token": "access-token"}

        def fake_input(prompt=""):
            calls.append(f"prompt:{prompt}")
            return "123456"

        monkeypatch.setattr(live_auth, "login", fake_login)
        monkeypatch.setattr(live_auth, "verify_totp", fake_verify_totp)
        monkeypatch.setattr("builtins.input", fake_input)

        assert live_auth.get_session_token() == "access-token"
        assert calls[0] == "login"
        assert calls[1].startswith("prompt:Enter TOTP")
        assert calls[2] == ("verify_totp", "123456")
    finally:
        os.chdir(old_cwd)


@pytest.mark.skip(reason="Card 07 live bring-up deferred (requires mStock credentials and auth testing)")
def test_totp_auth_calls_verify_totp():
    """Test 16: auth_mode='totp' ⇒ calls verify_totp with the preset code (not generate_session)."""
    # This test is for Card 07 live bring-up (deferred).
    # Mock the auth module if it doesn't exist yet.
    try:
        from backtest.live.auth import verify_totp, generate_session
    except ImportError:
        pytest.skip("Live auth module not yet built (Card 07)")
        return
    
    with patch("backtest.live.auth.verify_totp") as mock_verify:
        with patch("backtest.live.auth.generate_session") as mock_gen:
            mock_verify.return_value = {"token": "mock_token"}
            
            # Simulate auth_mode="totp" flow
            result = verify_totp("preset_code_123")
            
            mock_verify.assert_called_once_with("preset_code_123")
            mock_gen.assert_not_called()


@pytest.mark.skip(reason="Card 07 live bring-up deferred (requires mStock credentials and auth testing)")
def test_otp_auth_calls_generate_session():
    """Test 17: auth_mode='otp' ⇒ calls generate_session with the preset code."""
    try:
        from backtest.live.auth import verify_totp, generate_session
    except ImportError:
        pytest.skip("Live auth module not yet built (Card 07)")
        return
    
    with patch("backtest.live.auth.generate_session") as mock_gen:
        with patch("backtest.live.auth.verify_totp") as mock_verify:
            mock_gen.return_value = {"token": "mock_token"}
            
            # Simulate auth_mode="otp" flow
            result = generate_session("preset_code_456")
            
            mock_gen.assert_called_once_with("preset_code_456")
            mock_verify.assert_not_called()


@pytest.mark.skip(reason="Card 07 live bring-up deferred (requires mStock credentials and auth testing)")
def test_mstock_totp_env_used():
    """Test 18: MSTOCK_TOTP env used when no explicit code is passed."""
    try:
        from backtest.live.auth import get_auth_code
    except ImportError:
        pytest.skip("Live auth module not yet built (Card 07)")
        return
    
    with patch.dict(os.environ, {"MSTOCK_TOTP": "env_code_789"}):
        code = get_auth_code(explicit_code=None)
        assert code == "env_code_789"
