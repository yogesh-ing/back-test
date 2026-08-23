"""Tests for Step 21: Alert & Notification System (including Telegram)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest

from backtest.alerts.manager import AlertManager, AlertConfig, AlertLevel, AlertChannel, ChannelConfig


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_alert_level_validation():
    assert AlertLevel.validate("info") == "info"
    assert AlertLevel.validate("CRITICAL") == "critical"
    with pytest.raises(Exception):
        AlertLevel.validate("invalid")


def test_alert_channel_validation():
    assert AlertChannel.validate("telegram") == "telegram"
    assert AlertChannel.validate("TELEGRAM") == "telegram"
    with pytest.raises(Exception):
        AlertChannel.validate("invalid_channel")


def test_alert_config_defaults():
    cfg = AlertConfig()
    assert cfg.enabled is True
    assert cfg.min_level == "info"
    assert "trade_executed" in cfg.templates


def test_channel_config():
    cfg = ChannelConfig(enabled=True, telegram_bot_token="123:ABC", telegram_chat_id="123456")
    assert cfg.enabled is True
    assert cfg.telegram_bot_token == "123:ABC"


# ---------------------------------------------------------------------------
# AlertManager - core
# ---------------------------------------------------------------------------


def test_alert_manager_init():
    manager = AlertManager(config={"min_level": "info"})
    assert manager.config.min_level == "info"
    assert len(manager._senders) == 7  # 7 channels


def test_send_alert_log_only():
    manager = AlertManager(config={"min_level": "debug", "channels": {"log": {"enabled": True}}, "routing": {"info": ["log"]}})

    record = manager.send_alert(level="info", message="Test message", channels=["log"])

    assert record.level == "info"
    assert record.message == "Test message"
    assert "log" in record.success_channels
    assert len(record.failed_channels) == 0


def test_send_alert_min_level_filter():
    manager = AlertManager(config={"min_level": "warning"})

    # info below warning should be skipped (no channels)
    record = manager.send_alert(level="info", message="Info message", channels=["log"])
    assert len(record.channels) == 0  # skipped due to min level

    # warning should pass
    record2 = manager.send_alert(level="warning", message="Warning message", channels=["log"])
    assert len(record2.success_channels) == 1


def test_send_alert_with_routing():
    cfg = AlertConfig(
        min_level="info",
        channels={"log": ChannelConfig(enabled=True), "telegram": ChannelConfig(enabled=True, telegram_bot_token="token", telegram_chat_id="123")},
        routing={"info": ["log"], "error": ["telegram", "log"]},
    )
    manager = AlertManager(config=cfg)

    # info should route to log only
    record = manager.send_alert(level="info", message="Info")
    assert "log" in record.channels
    assert "telegram" not in record.channels

    # error should route to telegram + log
    record2 = manager.send_alert(level="error", message="Error")
    assert "log" in record2.channels
    # telegram will fail because token is fake, but should be in channels list
    assert "telegram" in record2.channels


def test_send_alert_by_type_routing():
    cfg = AlertConfig(
        min_level="info",
        channels={"log": ChannelConfig(enabled=True), "telegram": ChannelConfig(enabled=True, telegram_bot_token="t", telegram_chat_id="c")},
        routing={"trade_executed": ["telegram", "log"]},
    )
    manager = AlertManager(config=cfg)

    record = manager.send_alert(level="info", message="Trade", alert_type="trade_executed")
    assert "telegram" in record.channels
    assert "log" in record.channels


def test_template_rendering():
    cfg = AlertConfig(
        min_level="info",
        channels={"log": ChannelConfig(enabled=True)},
        templates={"trade_executed": "Trade {symbol} {side} {quantity} @ {price}"},
    )
    manager = AlertManager(config=cfg)

    # Context with placeholders
    record = manager.send_alert(
        level="info", message="Trade executed", alert_type="trade_executed", context={"symbol": "INFY", "side": "BUY", "quantity": 100, "price": 1500}
    )

    # Should use template
    assert "INFY" in record.message
    assert "BUY" in record.message
    assert "100" in record.message


def test_quiet_hours():
    cfg = AlertConfig(quiet_hours_enabled=False)
    manager = AlertManager(config=cfg)
    assert manager._is_quiet_hours() is False

    # Enable quiet hours and mock time to be inside quiet hours
    cfg2 = AlertConfig(quiet_hours_enabled=True, quiet_start="00:00", quiet_end="23:59", quiet_allow_critical=True)
    manager2 = AlertManager(config=cfg2)

    # Should be in quiet hours (entire day)
    assert manager2._is_quiet_hours() is True

    # Non-critical should be suppressed
    record = manager2.send_alert(level="info", message="Info during quiet", channels=["log"])
    assert "quiet_hours" in str(record.context.get("suppressed", ""))

    # Critical should pass even in quiet hours
    record2 = manager2.send_alert(level="critical", message="Critical during quiet", channels=["log"])
    assert len(record2.success_channels) == 1


def test_rate_limiting():
    cfg = AlertConfig(rate_limit_enabled=True, max_alerts_per_hour=2)
    manager = AlertManager(config=cfg)

    # Send 2 alerts – should pass
    for _ in range(2):
        record = manager.send_alert(level="info", message="Test", channels=["log"])
        assert len(record.success_channels) == 1

    # 3rd should be rate limited
    record3 = manager.send_alert(level="info", message="Test 3", channels=["log"])
    assert len(record3.success_channels) == 0
    assert manager._stats["rate_limited"] == 1

    # Different channel should not be limited
    record4 = manager.send_alert(level="info", message="Test", channels=["telegram"])
    # Telegram not configured, will fail, but not rate limited for telegram
    # Actually telegram bucket is separate, so it should not be rate limited
    assert manager._is_rate_limited("telegram") is False


# ---------------------------------------------------------------------------
# Channel senders (mocked)
# ---------------------------------------------------------------------------


def test_log_channel_sender():
    from backtest.alerts.manager import LogChannelSender

    sender = LogChannelSender()
    config = ChannelConfig(enabled=True, log_file="/tmp/test_alerts.log")

    ok, reason = sender.send("Test log message", config, context={"level": "info"})
    assert ok is True


def test_email_channel_sender_not_configured():
    from backtest.alerts.manager import EmailChannelSender

    sender = EmailChannelSender()
    config = ChannelConfig(enabled=False)

    ok, reason = sender.send("Test", config)
    assert ok is False
    assert "not configured" in reason


@patch("smtplib.SMTP")
def test_email_channel_sender_mocked(mock_smtp):
    from backtest.alerts.manager import EmailChannelSender

    sender = EmailChannelSender()
    config = ChannelConfig(
        enabled=True,
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="user@example.com",
        smtp_password="pass",
        from_email="from@example.com",
        to_emails=["to@example.com"],
    )

    mock_server = Mock()
    mock_smtp.return_value.__enter__.return_value = mock_server

    ok, reason = sender.send("Test email", config, context={"level": "info", "alert_type": "test"})

    assert ok is True
    assert mock_server.send_message.called


def test_sms_channel_sender_mocked():
    from backtest.alerts.manager import SMSChannelSender

    sender = SMSChannelSender()
    config = ChannelConfig(enabled=True, twilio_account_sid="AC123", twilio_to_numbers=["+1234567890"])

    ok, reason = sender.send("Test SMS", config)
    assert ok is True
    assert "mock" in reason


@patch("requests.post")
def test_slack_channel_sender_mocked(mock_post):
    from backtest.alerts.manager import SlackChannelSender

    mock_post.return_value.raise_for_status = Mock()
    mock_post.return_value.status_code = 200

    sender = SlackChannelSender()
    config = ChannelConfig(enabled=True, slack_webhook_url="https://hooks.slack.com/test")

    ok, reason = sender.send("Test Slack", config)

    assert ok is True
    assert mock_post.called


@patch("requests.post")
def test_discord_channel_sender_mocked(mock_post):
    from backtest.alerts.manager import DiscordChannelSender

    mock_post.return_value.raise_for_status = Mock()
    mock_post.return_value.status_code = 200

    sender = DiscordChannelSender()
    config = ChannelConfig(enabled=True, discord_webhook_url="https://discord.com/api/webhooks/test")

    ok, reason = sender.send("Test Discord", config)

    assert ok is True


@patch("requests.post")
def test_telegram_channel_sender_mocked(mock_post):
    from backtest.alerts.manager import TelegramChannelSender

    # Mock successful Telegram API response
    mock_response = Mock()
    mock_response.raise_for_status = Mock()
    mock_response.json.return_value = {"ok": True, "result": {"message_id": 123}}
    mock_post.return_value = mock_response

    sender = TelegramChannelSender()
    config = ChannelConfig(enabled=True, telegram_bot_token="123456:ABC-DEF", telegram_chat_id="123456789")

    ok, reason = sender.send("Test Telegram message", config)

    assert ok is True
    assert mock_post.called
    # Check URL contains bot token
    called_url = mock_post.call_args[0][0]
    assert "123456:ABC-DEF" in called_url
    # Check payload
    called_json = mock_post.call_args[1]["json"]
    assert called_json["chat_id"] == "123456789"
    assert called_json["text"] == "Test Telegram message"


def test_telegram_channel_sender_not_configured():
    from backtest.alerts.manager import TelegramChannelSender

    sender = TelegramChannelSender()
    config = ChannelConfig(enabled=False)

    ok, reason = sender.send("Test", config)
    assert ok is False
    assert "not configured" in reason


@patch("requests.post")
def test_telegram_channel_sender_api_error(mock_post):
    from backtest.alerts.manager import TelegramChannelSender

    mock_response = Mock()
    mock_response.raise_for_status = Mock()
    mock_response.json.return_value = {"ok": False, "description": "Invalid token"}
    mock_post.return_value = mock_response

    sender = TelegramChannelSender()
    config = ChannelConfig(enabled=True, telegram_bot_token="bad_token", telegram_chat_id="123")

    ok, reason = sender.send("Test", config)

    assert ok is False
    assert "API error" in reason


# ---------------------------------------------------------------------------
# Convenience methods
# ---------------------------------------------------------------------------


def test_alert_on_trade():
    manager = AlertManager(config={"min_level": "info", "channels": {"log": {"enabled": True}}})

    class MockTrade:
        symbol = "INFY"
        side = "BUY"
        quantity = 100
        fill_price = 1500
        realized_pnl = 500
        reason = "SMA crossover"

    record = manager.alert_on_trade(MockTrade())

    assert record.alert_type == "trade_executed"
    assert "INFY" in record.message
    assert len(record.success_channels) >= 1


def test_alert_on_error():
    manager = AlertManager(config={"min_level": "info", "channels": {"log": {"enabled": True}}})

    record = manager.alert_on_error(Exception("Test error"), component="test_component")

    assert record.alert_type == "system_error"
    assert record.level == "error"
    assert "test_component" in record.message


def test_alert_on_limit_breach():
    manager = AlertManager(config={"min_level": "info", "channels": {"log": {"enabled": True}}})

    record = manager.alert_on_limit_breach(limit_type="max_drawdown", symbol="INFY", reason="Drawdown 15% > 10%")

    assert record.alert_type == "risk_limit_breach"
    assert record.level == "warning"
    assert "max_drawdown" in record.message


def test_alert_on_stop_loss():
    manager = AlertManager(config={"min_level": "info", "channels": {"log": {"enabled": True}}})

    record = manager.alert_on_stop_loss(symbol="INFY", quantity=100, price=1450, pnl=-500)

    assert record.alert_type == "stop_loss_hit"
    assert "INFY" in record.message


def test_alert_on_take_profit():
    manager = AlertManager(config={"min_level": "info", "channels": {"log": {"enabled": True}}})

    record = manager.alert_on_take_profit(symbol="INFY", quantity=100, price=1550, pnl=500)

    assert record.alert_type == "take_profit_hit"
    assert "INFY" in record.message


def test_daily_summary():
    manager = AlertManager(config={"min_level": "info", "channels": {"log": {"enabled": True}}})

    record = manager.send_daily_summary(equity=105000, pnl=5000, trades=10, win_rate=60.0)

    assert record.alert_type == "daily_summary"
    assert "105000" in record.message


# ---------------------------------------------------------------------------
# History and stats
# ---------------------------------------------------------------------------


def test_history():
    manager = AlertManager(config={"min_level": "debug", "channels": {"log": {"enabled": True}}, "max_history": 5})

    for i in range(10):
        manager.send_alert(level="info", message=f"Message {i}", channels=["log"])

    # History should be capped at 5
    assert len(manager._history) == 5

    history = manager.get_history(limit=3)
    assert len(history) == 3

    # Filter by level
    manager.send_alert(level="error", message="Error message", channels=["log"])
    error_history = manager.get_history(level="error")
    assert len(error_history) >= 1
    assert all(r.level == "error" for r in error_history)


def test_stats():
    manager = AlertManager(config={"min_level": "info", "channels": {"log": {"enabled": True}}})

    manager.send_alert(level="info", message="Test", channels=["log"])
    manager.send_alert(level="info", message="Test2", channels=["log"])

    stats = manager.get_stats()
    assert stats["sent"] == 2
    assert stats["history_count"] == 2


def test_clear_history():
    manager = AlertManager(config={"min_level": "info", "channels": {"log": {"enabled": True}}})

    manager.send_alert(level="info", message="Test", channels=["log"])
    assert len(manager._history) == 1

    manager.clear_history()
    assert len(manager._history) == 0
    assert manager.get_stats()["sent"] == 0


def test_configure_alerts():
    manager = AlertManager()

    new_config = AlertConfig(min_level="error", channels={"log": ChannelConfig(enabled=True)})
    manager.configure_alerts(new_config)

    assert manager.config.min_level == "error"

    # From dict
    manager.configure_alerts({"min_level": "warning", "channels": {"log": {"enabled": True}}})
    assert manager.config.min_level == "warning"


# ---------------------------------------------------------------------------
# Integration with other components
# ---------------------------------------------------------------------------


def test_integration_with_risk_manager():
    from backtest.simulator.portfolio import Portfolio
    from backtest.simulator.risk_manager import RiskManager, RiskConfig

    portfolio = Portfolio(name="risk_alert_test", initial_capital=100000)
    risk_config = RiskConfig(restricted_symbols={"BAD"})
    risk_manager = RiskManager(portfolio, risk_config)

    alert_manager = AlertManager(config={"min_level": "info", "channels": {"log": {"enabled": True}}})

    # Wire alert manager as callback to risk manager
    alerts = []
    risk_manager.add_alert_callback(lambda level, msg, details: alerts.append((level, msg)))
    alert_manager.add_alert_callback = lambda level, msg, details: None  # placeholder

    # Actually risk manager already has alert callbacks – we test that it calls them
    # For integration, we can make risk manager's alert trigger alert manager

    def risk_alert_callback(level, message, details):
        alert_manager.send_alert(level=level, message=message, alert_type="risk_limit_breach", context=details)

    risk_manager.add_alert_callback(risk_alert_callback)

    from backtest.simulator.order import Order

    order = Order(symbol="BAD", side="buy", quantity=10, order_type="market")
    order.submit()

    result = risk_manager.validate_order(order, current_price=100)
    assert not result.allowed
    # Alert should have been sent via callback
    assert len(alert_manager.get_history()) >= 1


def test_telegram_preferred():
    """Test that Telegram is preferred and works with all other channels."""

    cfg = AlertConfig(
        min_level="info",
        channels={
            "log": ChannelConfig(enabled=True),
            "telegram": ChannelConfig(enabled=True, telegram_bot_token="test_token", telegram_chat_id="123"),
            "email": ChannelConfig(enabled=True, smtp_host="smtp.example.com", to_emails=["test@example.com"]),
        },
        routing={
            "info": ["telegram", "log"],
            "critical": ["telegram", "email", "log"],
        },
    )

    manager = AlertManager(config=cfg)

    # Mock Telegram and Email senders
    with patch("requests.post") as mock_post, patch("smtplib.SMTP") as mock_smtp:
        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.json.return_value = {"ok": True}
        mock_post.return_value = mock_response

        mock_server = Mock()
        mock_smtp.return_value.__enter__.return_value = mock_server

        # Info should go to Telegram + Log (Telegram mocked, will succeed)
        record = manager.send_alert(level="info", message="Test info", alert_type="trade_executed")

        assert "telegram" in record.channels
        assert "log" in record.channels

        # Critical should go to Telegram + Email + Log
        record2 = manager.send_alert(level="critical", message="Critical error")

        assert "telegram" in record2.channels
        assert "email" in record2.channels
        assert "log" in record2.channels
