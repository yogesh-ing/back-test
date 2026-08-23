"""Tests for Step 23: Configuration Manager."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from backtest.config_manager.manager import ConfigManager, load_config, get_config, set_config


def test_config_manager_init():
    manager = ConfigManager()
    assert manager.config_file is not None


def test_load_config_defaults():
    with tempfile.TemporaryDirectory() as tmpdir:
        # No file, should use env + defaults
        config_file = Path(tmpdir) / "nonexistent.yaml"
        manager = ConfigManager(config_file=config_file)
        cfg = manager.load_config()

        assert isinstance(cfg, dict)


def test_load_config_yaml():
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_path = Path(tmpdir) / "app.yaml"
        yaml_path.write_text(
            """
default:
  portfolio:
    initial_capital: 100000
    name: "Test"
  risk:
    max_drawdown_pct: 0.1
profiles:
  production:
    portfolio:
      initial_capital: 1000000
"""
        )

        manager = ConfigManager(config_file=yaml_path)
        cfg = manager.load_config()

        assert cfg["portfolio"]["initial_capital"] == 100000
        assert cfg["risk"]["max_drawdown_pct"] == 0.1

        # Load production profile
        cfg_prod = manager.load_config(profile="production")
        assert cfg_prod["portfolio"]["initial_capital"] == 1000000


def test_load_config_json():
    with tempfile.TemporaryDirectory() as tmpdir:
        json_path = Path(tmpdir) / "app.json"
        json_path.write_text(json.dumps({"default": {"portfolio": {"name": "JsonTest"}}}))

        manager = ConfigManager(config_file=json_path)
        cfg = manager.load_config()

        assert cfg["portfolio"]["name"] == "JsonTest"


def test_env_overrides():
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_path = Path(tmpdir) / "app.yaml"
        yaml_path.write_text(
            """
default:
  risk:
    max_drawdown_pct: 0.1
"""
        )

        # Set env var
        os.environ["FORWARD_TEST_RISK_MAX_DRAWDOWN_PCT"] = "0.2"

        manager = ConfigManager(config_file=yaml_path)
        cfg = manager.load_config()

        assert float(cfg["risk"]["max_drawdown_pct"]) == 0.2

        # Cleanup
        del os.environ["FORWARD_TEST_RISK_MAX_DRAWDOWN_PCT"]


def test_get_set():
    manager = ConfigManager()
    manager._config = {"risk": {"max_drawdown_pct": 0.1}, "portfolio": {"name": "Test"}}

    assert manager.get("risk.max_drawdown_pct") == 0.1
    assert manager.get("portfolio.name") == "Test"
    assert manager.get("nonexistent.key", default="default") == "default"

    manager.set("risk.max_drawdown_pct", 0.15)
    assert manager.get("risk.max_drawdown_pct") == 0.15

    manager.set("new.nested.key", "value")
    assert manager.get("new.nested.key") == "value"


def test_save_config():
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_path = Path(tmpdir) / "app.yaml"
        yaml_path.write_text("default:\n  portfolio:\n    name: Test\n")

        manager = ConfigManager(config_file=yaml_path)
        manager.load_config()
        manager.set("risk.max_drawdown_pct", 0.15)

        # Save to new file
        new_path = Path(tmpdir) / "saved.yaml"
        saved = manager.save_config(file_path=new_path)

        assert Path(saved).exists()

        # Sensitive values should not be saved
        manager.set("alerts.channels.telegram.telegram_bot_token", "secret_token")
        manager.set("portfolio.name", "SafeTest")

        saved2 = manager.save_config(file_path=new_path)
        content = Path(saved2).read_text()
        assert "secret_token" not in content
        assert "SafeTest" in content


def test_reload_config():
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_path = Path(tmpdir) / "app.yaml"
        yaml_path.write_text("default:\n  portfolio:\n    name: Original\n")

        manager = ConfigManager(config_file=yaml_path)
        cfg1 = manager.load_config()
        assert cfg1["portfolio"]["name"] == "Original"

        # Modify file
        yaml_path.write_text("default:\n  portfolio:\n    name: Modified\n")

        cfg2 = manager.reload_config()
        assert cfg2["portfolio"]["name"] == "Modified"


def test_check_and_reload():
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_path = Path(tmpdir) / "app.yaml"
        yaml_path.write_text("default:\n  portfolio:\n    name: Test\n")

        manager = ConfigManager(config_file=yaml_path, auto_reload=True)
        manager.load_config()

        # No change, should not reload
        assert manager.check_and_reload() is False

        # Modify file
        import time

        time.sleep(0.1)
        yaml_path.write_text("default:\n  portfolio:\n    name: NewName\n")

        assert manager.check_and_reload() is True
        assert manager.get("portfolio.name") == "NewName"


def test_safe_log_dict():
    manager = ConfigManager()
    manager._config = {
        "portfolio": {"name": "Test"},
        "alerts": {"channels": {"telegram": {"telegram_bot_token": "secret123", "chat_id": "123"}}},
        "db": {"password": "dbpass"},
    }

    safe = manager._safe_log_dict(manager._config)

    assert safe["portfolio"]["name"] == "Test"
    assert safe["alerts"]["channels"]["telegram"]["telegram_bot_token"] == "***"
    assert safe["db"]["password"] == "***"


def test_global_convenience_functions():
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_path = Path(tmpdir) / "app.yaml"
        yaml_path.write_text("default:\n  portfolio:\n    name: GlobalTest\n")

        cfg = load_config(file_path=yaml_path)
        assert cfg["portfolio"]["name"] == "GlobalTest"

        name = get_config("portfolio.name")
        assert name == "GlobalTest"

        set_config("portfolio.name", "NewGlobal")
        assert get_config("portfolio.name") == "NewGlobal"


def test_env_parsing():
    from backtest.config_manager.manager import _parse_env_value

    assert _parse_env_value("true") is True
    assert _parse_env_value("false") is False
    assert _parse_env_value("123") == 123
    assert _parse_env_value("123.45") == 123.45
    assert _parse_env_value("hello") == "hello"
    assert _parse_env_value('{"key": "value"}') == {"key": "value"}


def test_telegram_env_override():
    os.environ["TELEGRAM_BOT_TOKEN"] = "test_token_123"
    os.environ["TELEGRAM_CHAT_ID"] = "123456"

    manager = ConfigManager()
    cfg = manager.load_config()

    # Should have telegram config from env
    assert manager.get("alerts.channels.telegram.telegram_bot_token") == "test_token_123"
    assert manager.get("alerts.channels.telegram.telegram_chat_id") == "123456"

    del os.environ["TELEGRAM_BOT_TOKEN"]
    del os.environ["TELEGRAM_CHAT_ID"]
