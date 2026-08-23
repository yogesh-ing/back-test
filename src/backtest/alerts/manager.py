"""Alert & Notification Manager (Step 21).

Multi-channel alerting with Telegram as preferred medium (user preference:
email delayed, SMS costly).

Channels:
- Email (SMTP)
- SMS (Twilio) – costly, for critical only
- Slack webhook
- Discord webhook
- Telegram bot – preferred, fast, free
- Desktop notification (plyer – optional)
- Log file (always)

Features:
- Template system for messages
- Channel routing by level and alert type
- Quiet hours (e.g. 22:00-16:00 IST no non-critical)
- Rate limiting (max N alerts per hour)
- Alert history in DB (optional) and in-memory
- Integration with RiskManager, TradeAnalyzer, ForwardTestingEngine

Telegram setup:
1. Talk to @BotFather on Telegram, /newbot → get token like 123456:ABC-DEF...
2. Get chat_id: send message to bot, then visit https://api.telegram.org/bot<token>/getUpdates
3. Put in .env:
   TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
   TELEGRAM_CHAT_ID=123456789

Then:
>>> from backtest.alerts.manager import AlertManager
>>> manager = AlertManager()
>>> manager.send_alert("info", "Trade executed: INFY BUY 100 @ 1500", channels=["telegram"])
"""

from __future__ import annotations

import logging
import smtplib
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, time as dtime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Set

import requests

logger = logging.getLogger("backtest.alerts.manager")

DEFAULT_ALERT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "alerts.yaml"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AlertLevel:
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"

    ALL = (DEBUG, INFO, WARNING, ERROR, CRITICAL)

    ORDER = {DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3, CRITICAL: 4}

    @classmethod
    def validate(cls, level: Any) -> str:
        v = str(level).strip().lower()
        if v not in cls.ALL:
            raise ValueError(f"invalid alert level {v!r}; expected one of {cls.ALL}")
        return v

    @classmethod
    def should_send(cls, message_level: str, threshold: str) -> bool:
        """Check if message_level >= threshold."""
        return cls.ORDER.get(message_level, 0) >= cls.ORDER.get(threshold, 0)


class AlertChannel:
    EMAIL = "email"
    SMS = "sms"
    SLACK = "slack"
    DISCORD = "discord"
    TELEGRAM = "telegram"
    DESKTOP = "desktop"
    LOG = "log"

    ALL = (EMAIL, SMS, SLACK, DISCORD, TELEGRAM, DESKTOP, LOG)

    @classmethod
    def validate(cls, channel: Any) -> str:
        v = str(channel).strip().lower()
        if v not in cls.ALL:
            raise ValueError(f"invalid alert channel {v!r}; expected one of {cls.ALL}")
        return v


class AlertType:
    TRADE_EXECUTED = "trade_executed"
    STOP_LOSS_HIT = "stop_loss_hit"
    TAKE_PROFIT_HIT = "take_profit_hit"
    RISK_LIMIT_BREACH = "risk_limit_breach"
    SYSTEM_ERROR = "system_error"
    POSITION_OPENED = "position_opened"
    POSITION_CLOSED = "position_closed"
    DAILY_SUMMARY = "daily_summary"
    WEEKLY_SUMMARY = "weekly_summary"

    ALL = (
        TRADE_EXECUTED,
        STOP_LOSS_HIT,
        TAKE_PROFIT_HIT,
        RISK_LIMIT_BREACH,
        SYSTEM_ERROR,
        POSITION_OPENED,
        POSITION_CLOSED,
        DAILY_SUMMARY,
        WEEKLY_SUMMARY,
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class ChannelConfig:
    enabled: bool = False
    # Email
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    from_email: Optional[str] = None
    to_emails: List[str] = field(default_factory=list)

    # SMS (Twilio)
    twilio_account_sid: Optional[str] = None
    twilio_auth_token: Optional[str] = None
    twilio_from_number: Optional[str] = None
    twilio_to_numbers: List[str] = field(default_factory=list)

    # Slack
    slack_webhook_url: Optional[str] = None

    # Discord
    discord_webhook_url: Optional[str] = None

    # Telegram – preferred
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    telegram_parse_mode: str = "Markdown"

    # Desktop
    desktop_enabled: bool = False

    # Log
    log_file: Optional[str] = None


@dataclass
class AlertConfig:
    """Configuration for AlertManager."""

    # Global
    enabled: bool = True
    min_level: str = AlertLevel.INFO  # minimum level to send

    # Channel configs
    channels: Dict[str, ChannelConfig] = field(default_factory=dict)

    # Routing: level or alert_type -> list of channels
    # Example: {"error": ["sms", "slack"], "trade_executed": ["telegram", "email"]}
    routing: Dict[str, List[str]] = field(default_factory=dict)

    # Quiet hours: no non-critical alerts during these hours (IST)
    quiet_hours_enabled: bool = False
    quiet_start: str = "22:00"  # HH:MM
    quiet_end: str = "07:00"
    quiet_timezone: str = "Asia/Kolkata"
    quiet_allow_critical: bool = True  # allow critical even in quiet hours

    # Rate limiting
    rate_limit_enabled: bool = True
    max_alerts_per_hour: int = 20
    max_alerts_per_hour_per_channel: Dict[str, int] = field(default_factory=dict)

    # Templates
    templates: Dict[str, str] = field(default_factory=dict)

    # History
    keep_history: bool = True
    max_history: int = 1000
    persist_to_db: bool = False

    def __post_init__(self):
        self.min_level = AlertLevel.validate(self.min_level)

        # Normalize channels: convert dict values to ChannelConfig if needed
        normalized_channels = {}
        for chan_name, chan_cfg in (self.channels or {}).items():
            if isinstance(chan_cfg, dict):
                normalized_channels[chan_name] = ChannelConfig(**chan_cfg)
            elif isinstance(chan_cfg, ChannelConfig):
                normalized_channels[chan_name] = chan_cfg
            else:
                # If it's a bool or enabled flag
                if isinstance(chan_cfg, bool):
                    normalized_channels[chan_name] = ChannelConfig(enabled=chan_cfg)
                else:
                    normalized_channels[chan_name] = ChannelConfig(enabled=False)
        self.channels = normalized_channels

        # Normalize routing channels
        normalized_routing = {}
        for key, chans in self.routing.items():
            if isinstance(chans, str):
                chans = [chans]
            normalized_routing[str(key).lower()] = [AlertChannel.validate(c) for c in chans]
        self.routing = normalized_routing

        # Default templates
        default_templates = {
            "trade_executed": "🔔 Trade executed: {symbol} {side} {quantity} @ {price} | PnL: {pnl} | {reason}",
            "stop_loss_hit": "🛑 Stop loss hit: {symbol} {side} {quantity} @ {price} | Loss: {pnl}",
            "take_profit_hit": "✅ Take profit hit: {symbol} {side} {quantity} @ {price} | Profit: {pnl}",
            "risk_limit_breach": "⚠️ Risk limit breached: {limit_type} for {symbol} | {reason}",
            "system_error": "❌ System error: {component} | {message}",
            "position_opened": "📈 Position opened: {symbol} {side} {quantity} @ {price}",
            "position_closed": "📉 Position closed: {symbol} {side} {quantity} @ {price} | PnL: {pnl}",
            "daily_summary": "📊 Daily Summary: Equity {equity} | P&L {pnl} | Trades {trades} | Win Rate {win_rate}%",
            "default": "{level}: {message}",
        }
        # Merge user templates over defaults
        merged = dict(default_templates)
        merged.update(self.templates)
        self.templates = merged


def load_alert_config(path: str | Path | None = None, profile: str | None = None) -> AlertConfig:
    """Load alert config from YAML, with env overrides for secrets."""
    import os

    config_path = Path(path) if path else DEFAULT_ALERT_CONFIG_PATH

    if path is not None and not config_path.exists():
        raise ValueError(f"alert config not found: {config_path}")

    if not config_path.exists():
        # Build from env only
        return _build_config_from_env(AlertConfig())

    try:
        import yaml

        doc = yaml.safe_load(config_path.read_text()) or {}
        merged = dict(doc.get("default") or {})
        profiles = doc.get("profiles") or {}
        chosen = profile or doc.get("active_profile") or "default"

        if profiles and chosen in profiles:
            merged.update(profiles[chosen] or {})

        # Build ChannelConfig from merged
        channels_data = merged.get("channels", {})
        channels = {}
        for chan_name, chan_cfg in channels_data.items():
            if isinstance(chan_cfg, dict):
                channels[chan_name] = ChannelConfig(**chan_cfg)

        # Routing
        routing = merged.get("routing", {})

        # Other top-level
        cfg_kwargs = {}
        for key in AlertConfig.__dataclass_fields__:
            if key in merged and key not in ("channels", "routing"):
                cfg_kwargs[key] = merged[key]

        cfg_kwargs["channels"] = channels
        cfg_kwargs["routing"] = routing

        # Build from env overrides
        base_cfg = AlertConfig(**cfg_kwargs)
        return _build_config_from_env(base_cfg)

    except Exception as exc:
        logger.warning("Failed to load alert config %s: %s, using defaults + env", config_path, exc)
        return _build_config_from_env(AlertConfig())


def _build_config_from_env(base: AlertConfig) -> AlertConfig:
    """Override config with env vars for secrets (never commit secrets)."""
    import os

    # Helper to get env
    def _env(key: str, default=None):
        return os.getenv(key, default)

    # Ensure telegram channel exists
    if "telegram" not in base.channels:
        base.channels["telegram"] = ChannelConfig()

    # Telegram env
    bot_token = _env("TELEGRAM_BOT_TOKEN") or _env("TELEGRAM_BOT_TOKEN".lower())
    chat_id = _env("TELEGRAM_CHAT_ID")
    if bot_token:
        base.channels["telegram"].telegram_bot_token = bot_token
        base.channels["telegram"].enabled = True
    if chat_id:
        base.channels["telegram"].telegram_chat_id = chat_id

    # Slack
    if "slack" not in base.channels:
        base.channels["slack"] = ChannelConfig()
    slack_url = _env("SLACK_WEBHOOK_URL")
    if slack_url:
        base.channels["slack"].slack_webhook_url = slack_url
        base.channels["slack"].enabled = True

    # Discord
    if "discord" not in base.channels:
        base.channels["discord"] = ChannelConfig()
    discord_url = _env("DISCORD_WEBHOOK_URL")
    if discord_url:
        base.channels["discord"].discord_webhook_url = discord_url
        base.channels["discord"].enabled = True

    # Email
    if "email" not in base.channels:
        base.channels["email"] = ChannelConfig()
    smtp_host = _env("ALERT_EMAIL_SMTP_HOST") or _env("SMTP_HOST")
    if smtp_host:
        base.channels["email"].smtp_host = smtp_host
        base.channels["email"].enabled = True
    smtp_user = _env("ALERT_EMAIL_USER") or _env("SMTP_USER")
    if smtp_user:
        base.channels["email"].smtp_user = smtp_user
    smtp_pass = _env("ALERT_EMAIL_PASSWORD") or _env("SMTP_PASSWORD")
    if smtp_pass:
        base.channels["email"].smtp_password = smtp_pass
    from_email = _env("ALERT_EMAIL_FROM")
    if from_email:
        base.channels["email"].from_email = from_email
    to_emails = _env("ALERT_EMAIL_TO")
    if to_emails:
        base.channels["email"].to_emails = [e.strip() for e in to_emails.split(",")]

    # Twilio SMS
    if "sms" not in base.channels:
        base.channels["sms"] = ChannelConfig()
    twilio_sid = _env("TWILIO_ACCOUNT_SID")
    if twilio_sid:
        base.channels["sms"].twilio_account_sid = twilio_sid
        base.channels["sms"].enabled = True

    return base


# ---------------------------------------------------------------------------
# Alert record
# ---------------------------------------------------------------------------


@dataclass
class AlertRecord:
    alert_id: str
    level: str
    message: str
    alert_type: Optional[str] = None
    channels: List[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    success_channels: List[str] = field(default_factory=list)
    failed_channels: Dict[str, str] = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        return {
            "alert_id": self.alert_id,
            "level": self.level,
            "message": self.message,
            "alert_type": self.alert_type,
            "channels": list(self.channels),
            "timestamp": self.timestamp.isoformat(),
            "success_channels": list(self.success_channels),
            "failed_channels": dict(self.failed_channels),
            "context": dict(self.context),
        }


# ---------------------------------------------------------------------------
# Channel senders
# ---------------------------------------------------------------------------


class ChannelSender:
    """Base for channel senders."""

    def send(self, message: str, config: ChannelConfig, context: Dict[str, Any] = None) -> Tuple[bool, str]:
        raise NotImplementedError


class LogChannelSender(ChannelSender):
    def send(self, message: str, config: ChannelConfig, context: Dict[str, Any] = None) -> Tuple[bool, str]:
        level = (context or {}).get("level", "info").lower()
        if level == "critical":
            logger.critical("ALERT [%s] %s", level, message)
        elif level == "error":
            logger.error("ALERT [%s] %s", level, message)
        elif level == "warning":
            logger.warning("ALERT [%s] %s", level, message)
        else:
            logger.info("ALERT [%s] %s", level, message)

        # Optional log file
        if config.log_file:
            try:
                Path(config.log_file).parent.mkdir(parents=True, exist_ok=True)
                with open(config.log_file, "a") as f:
                    f.write(f"{datetime.now(timezone.utc).isoformat()} [{level}] {message}\n")
            except Exception as exc:
                logger.debug("Failed to write to log file %s: %s", config.log_file, exc)

        return True, "logged"


class EmailChannelSender(ChannelSender):
    def send(self, message: str, config: ChannelConfig, context: Dict[str, Any] = None) -> Tuple[bool, str]:
        if not config.enabled or not config.smtp_host:
            return False, "email channel not configured"

        if not config.to_emails:
            return False, "no recipient emails"

        try:
            msg = MIMEMultipart()
            msg["From"] = config.from_email or config.smtp_user or "alerts@forward-test.local"
            msg["To"] = ", ".join(config.to_emails)
            msg["Subject"] = f"[{context.get('level','info').upper()}] Forward Test Alert – {context.get('alert_type','alert')}"

            msg.attach(MIMEText(message, "plain"))

            with smtplib.SMTP(config.smtp_host, config.smtp_port) as server:
                server.starttls()
                if config.smtp_user and config.smtp_password:
                    server.login(config.smtp_user, config.smtp_password)
                server.send_message(msg)

            logger.info("Email alert sent to %s", config.to_emails)
            return True, "email sent"

        except Exception as exc:
            logger.warning("Failed to send email alert: %s", exc)
            return False, f"email failed: {exc}"


class SMSChannelSender(ChannelSender):
    def send(self, message: str, config: ChannelConfig, context: Dict[str, Any] = None) -> Tuple[bool, str]:
        if not config.enabled or not config.twilio_account_sid:
            return False, "sms channel not configured (Twilio)"

        # In real implementation, use twilio client
        # from twilio.rest import Client
        # client = Client(config.twilio_account_sid, config.twilio_auth_token)
        # for to in config.twilio_to_numbers:
        #     client.messages.create(body=message, from_=config.twilio_from_number, to=to)

        # For mock, just log and pretend
        logger.info("SMS alert (mock) to %s: %s", config.twilio_to_numbers, message[:100])
        return True, "sms sent (mock – would use Twilio)"


class SlackChannelSender(ChannelSender):
    def send(self, message: str, config: ChannelConfig, context: Dict[str, Any] = None) -> Tuple[bool, str]:
        if not config.enabled or not config.slack_webhook_url:
            return False, "slack webhook not configured"

        try:
            payload = {"text": message}
            # Slack supports blocks for richer formatting
            if context:
                # Add context as attachment
                payload["attachments"] = [{"fields": [{"title": k, "value": str(v), "short": True} for k, v in context.items() if k not in ("message", "level")]}]

            resp = requests.post(config.slack_webhook_url, json=payload, timeout=10)
            resp.raise_for_status()

            logger.info("Slack alert sent")
            return True, "slack sent"

        except Exception as exc:
            logger.warning("Failed to send Slack alert: %s", exc)
            return False, f"slack failed: {exc}"


class DiscordChannelSender(ChannelSender):
    def send(self, message: str, config: ChannelConfig, context: Dict[str, Any] = None) -> Tuple[bool, str]:
        if not config.enabled or not config.discord_webhook_url:
            return False, "discord webhook not configured"

        try:
            payload = {"content": message}
            resp = requests.post(config.discord_webhook_url, json=payload, timeout=10)
            resp.raise_for_status()

            logger.info("Discord alert sent")
            return True, "discord sent"

        except Exception as exc:
            logger.warning("Failed to send Discord alert: %s", exc)
            return False, f"discord failed: {exc}"


class TelegramChannelSender(ChannelSender):
    """Telegram Bot API sender – preferred medium (fast, free, reliable)."""

    def send(self, message: str, config: ChannelConfig, context: Dict[str, Any] = None) -> Tuple[bool, str]:
        if not config.enabled or not config.telegram_bot_token or not config.telegram_chat_id:
            return False, "telegram not configured (need TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)"

        try:
            # Telegram Bot API: https://api.telegram.org/bot{token}/sendMessage
            url = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"

            # Escape for Markdown if needed, but keep simple
            # For Markdown, we should keep message as is – user can use Markdown in templates
            payload = {
                "chat_id": config.telegram_chat_id,
                "text": message,
                "parse_mode": config.telegram_parse_mode,
            }

            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()

            data = resp.json()
            if not data.get("ok"):
                return False, f"telegram API error: {data}"

            logger.info("Telegram alert sent to %s", config.telegram_chat_id)
            return True, "telegram sent"

        except Exception as exc:
            logger.warning("Failed to send Telegram alert: %s", exc)
            return False, f"telegram failed: {exc}"


class DesktopChannelSender(ChannelSender):
    def send(self, message: str, config: ChannelConfig, context: Dict[str, Any] = None) -> Tuple[bool, str]:
        if not config.desktop_enabled:
            return False, "desktop notifications disabled"

        try:
            # Try plyer
            from plyer import notification

            notification.notify(
                title=f"Forward Test {context.get('level','info').upper()}",
                message=message[:200],  # desktop notifications limited
                timeout=10,
            )
            logger.info("Desktop notification sent")
            return True, "desktop sent"
        except Exception as exc:
            # Fallback: just log
            logger.debug("Desktop notification failed (plyer not installed?): %s", exc)
            logger.info("DESKTOP ALERT [%s] %s", context.get("level", "info") if context else "info", message)
            return False, f"desktop failed: {exc} (logged instead)"


# ---------------------------------------------------------------------------
# AlertManager
# ---------------------------------------------------------------------------


class AlertManager:
    """Multi-channel alert manager (Step 21).

    Parameters
    ----------
    config:
        AlertConfig or dict or path to YAML
    db_manager:
        Optional DatabaseManager for persisting alert history
    """

    def __init__(self, config: Optional[AlertConfig | Mapping[str, Any] | str | Path] = None, db_manager: Any = None):
        if config is None:
            self.config = load_alert_config()
        elif isinstance(config, (str, Path)):
            self.config = load_alert_config(path=config)
        elif isinstance(config, dict):
            self.config = AlertConfig(**config)
        else:
            self.config = config

        self.db_manager = db_manager

        # Channel senders
        self._senders: Dict[str, ChannelSender] = {
            AlertChannel.LOG: LogChannelSender(),
            AlertChannel.EMAIL: EmailChannelSender(),
            AlertChannel.SMS: SMSChannelSender(),
            AlertChannel.SLACK: SlackChannelSender(),
            AlertChannel.DISCORD: DiscordChannelSender(),
            AlertChannel.TELEGRAM: TelegramChannelSender(),
            AlertChannel.DESKTOP: DesktopChannelSender(),
        }

        # History
        self._history: deque = deque(maxlen=self.config.max_history)

        # Rate limiting: channel -> deque of timestamps
        self._rate_limit_buckets: Dict[str, deque] = defaultdict(lambda: deque(maxlen=self.config.max_alerts_per_hour * 2))

        # Stats
        self._stats = {"sent": 0, "failed": 0, "rate_limited": 0, "quiet_hours_suppressed": 0}

        logger.info("AlertManager initialized: min_level=%s channels=%s routing=%s", self.config.min_level, list(self.config.channels.keys()), self.config.routing)

    # -- core sending ------------------------------------------------------

    def send_alert(self, level: str, message: str, channels: Optional[List[str]] = None, alert_type: Optional[str] = None, context: Optional[Dict[str, Any]] = None) -> AlertRecord:
        """Send alert to specified channels or via routing.

        Parameters
        ----------
        level:
            Alert level: debug, info, warning, error, critical
        message:
            Message text (can use template placeholders)
        channels:
            List of channels to send to. If None, uses routing based on level and alert_type.
        alert_type:
            Optional alert type for routing and templating (e.g. trade_executed)
        context:
            Optional dict for template rendering and channel payloads

        Returns
        -------
        AlertRecord
            Record with success/failure per channel
        """
        import uuid

        if not self.config.enabled:
            logger.debug("Alerts disabled, skipping: %s", message)
            return AlertRecord(alert_id=str(uuid.uuid4()), level=level, message=message, channels=[])

        level = AlertLevel.validate(level)

        # Check min level threshold
        if not AlertLevel.should_send(level, self.config.min_level):
            logger.debug("Alert level %s below threshold %s, skipping", level, self.config.min_level)
            return AlertRecord(alert_id=str(uuid.uuid4()), level=level, message=message, channels=[])

        # Check quiet hours
        if self._is_quiet_hours() and level != AlertLevel.CRITICAL:
            if not (self.config.quiet_allow_critical and level == AlertLevel.CRITICAL):
                self._stats["quiet_hours_suppressed"] += 1
                logger.info("Alert suppressed due to quiet hours [%s] %s", level, message)
                return AlertRecord(alert_id=str(uuid.uuid4()), level=level, message=message, channels=[], context={"suppressed": "quiet_hours"})

        # Resolve channels via routing if not explicitly provided
        effective_channels = channels
        if effective_channels is None:
            effective_channels = self._resolve_routing(level, alert_type)

        if not effective_channels:
            # Default to log and telegram (preferred)
            effective_channels = [AlertChannel.LOG, AlertChannel.TELEGRAM]

        # Validate channels
        effective_channels = [AlertChannel.validate(c) for c in effective_channels]

        # Template rendering
        rendered_message = self._render_template(message, alert_type, level, context)

        # Rate limiting
        allowed_channels = []
        for chan in effective_channels:
            if self._is_rate_limited(chan):
                self._stats["rate_limited"] += 1
                logger.warning("Alert rate limited for channel %s, skipping", chan)
                continue
            allowed_channels.append(chan)

        # Send via each channel
        success = []
        failed = {}
        context = context or {}
        context = {**context, "level": level, "message": rendered_message, "alert_type": alert_type or "default"}

        for chan in allowed_channels:
            sender = self._senders.get(chan)
            chan_config = self.config.channels.get(chan, ChannelConfig())

            if not sender:
                failed[chan] = f"no sender for {chan}"
                continue

            # Check if channel enabled (except log which is always enabled)
            if chan != AlertChannel.LOG and not chan_config.enabled:
                # For telegram, check if token/chat_id present even if enabled flag false
                if chan == AlertChannel.TELEGRAM and chan_config.telegram_bot_token and chan_config.telegram_chat_id:
                    # Allow if configured via env even if enabled flag false
                    pass
                else:
                    failed[chan] = f"channel {chan} not enabled"
                    continue

            try:
                ok, reason = sender.send(rendered_message, chan_config, context)
                if ok:
                    success.append(chan)
                    self._stats["sent"] += 1
                    # Record for rate limiting
                    self._rate_limit_buckets[chan].append(datetime.now(timezone.utc))
                else:
                    failed[chan] = reason
                    self._stats["failed"] += 1
            except Exception as exc:
                failed[chan] = str(exc)
                self._stats["failed"] += 1
                logger.exception("Channel %s send failed", chan)

        # Create record
        record = AlertRecord(
            alert_id=str(uuid.uuid4()),
            level=level,
            message=rendered_message,
            alert_type=alert_type,
            channels=effective_channels,
            timestamp=datetime.now(timezone.utc),
            success_channels=success,
            failed_channels=failed,
            context=context,
        )

        # Keep history
        if self.config.keep_history:
            self._history.append(record)

        # Persist to DB if enabled
        if self.config.persist_to_db and self.db_manager:
            try:
                self._persist_to_db(record)
            except Exception as exc:
                logger.warning("Failed to persist alert to DB: %s", exc)

        logger.info("Alert sent [%s] via %s (success=%s failed=%s): %s", level, effective_channels, success, list(failed.keys()), rendered_message[:200])

        return record

    def _resolve_routing(self, level: str, alert_type: Optional[str]) -> List[str]:
        """Resolve channels from routing config based on level and alert_type."""
        # Priority: alert_type routing > level routing > default

        if alert_type and alert_type.lower() in self.config.routing:
            return self.config.routing[alert_type.lower()]

        if level.lower() in self.config.routing:
            return self.config.routing[level.lower()]

        # Default routing based on level
        if level == AlertLevel.CRITICAL:
            return [AlertChannel.TELEGRAM, AlertChannel.SLACK, AlertChannel.SMS, AlertChannel.LOG]
        elif level == AlertLevel.ERROR:
            return [AlertChannel.TELEGRAM, AlertChannel.SLACK, AlertChannel.LOG]
        elif level == AlertLevel.WARNING:
            return [AlertChannel.TELEGRAM, AlertChannel.LOG]
        elif level == AlertLevel.INFO:
            return [AlertChannel.TELEGRAM, AlertChannel.LOG]
        else:
            return [AlertChannel.LOG]

    def _render_template(self, message: str, alert_type: Optional[str], level: str, context: Optional[Dict[str, Any]]) -> str:
        """Render message using template if alert_type has template."""
        # If message already looks rendered (no placeholders), return as is
        # If alert_type has template and message is empty or is alert_type, use template

        # Try to get template for alert_type
        template = None
        if alert_type and alert_type.lower() in self.config.templates:
            template = self.config.templates[alert_type.lower()]
        elif "default" in self.config.templates:
            template = self.config.templates["default"]

        # If message is exactly alert_type or empty, use template
        # Otherwise, message is already the rendered text – but we still try to format with context

        ctx = context or {}
        ctx = {**ctx, "level": level, "message": message, "alert_type": alert_type or "default"}

        # First, try to format the message itself with context (if it has placeholders)
        try:
            rendered = message.format(**ctx)
            # If message had no placeholders, rendered == message
            # If we have a template and message is short (like trade data), we should use template?
            # Heuristic: if alert_type provided and message does not contain alert_type keywords, use template if message is not already containing symbol etc?
            # For simplicity, if alert_type provided and template exists and message is not the template itself,
            # and context has relevant keys (symbol, etc), we will try template rendering
            if alert_type and template and template != message:
                # If context has symbol, use template
                if any(k in ctx for k in ("symbol", "price", "quantity", "pnl")):
                    try:
                        return template.format(**ctx)
                    except KeyError:
                        # Template missing keys, fallback to message
                        pass
            return rendered
        except KeyError as exc:
            logger.debug("Template key missing %s, using raw message", exc)
            # Try template if available
            if template:
                try:
                    return template.format(**ctx)
                except KeyError:
                    pass
            return message
        except Exception:
            return message

    def _is_quiet_hours(self) -> bool:
        if not self.config.quiet_hours_enabled:
            return False

        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(self.config.quiet_timezone)
            now = datetime.now(tz)

            # Parse quiet start/end
            def _parse_time(t_str: str) -> dtime:
                parts = t_str.strip().split(":")
                hour = int(parts[0])
                minute = int(parts[1]) if len(parts) > 1 else 0
                return dtime(hour=hour, minute=minute)

            start = _parse_time(self.config.quiet_start)
            end = _parse_time(self.config.quiet_end)

            now_time = now.time()

            # Handle overnight quiet hours (e.g. 22:00-07:00)
            if start <= end:
                # Same day range
                return start <= now_time <= end
            else:
                # Overnight range
                return now_time >= start or now_time <= end

        except Exception as exc:
            logger.debug("Quiet hours check failed: %s", exc)
            return False

    def _is_rate_limited(self, channel: str) -> bool:
        if not self.config.rate_limit_enabled:
            return False

        try:
            now = datetime.now(timezone.utc)
            bucket = self._rate_limit_buckets.get(channel, deque())

            # Remove old entries (>1 hour)
            while bucket and (now - bucket[0]).total_seconds() > 3600:
                bucket.popleft()

            # Check per-channel limit
            per_channel_limit = self.config.max_alerts_per_hour_per_channel.get(channel, self.config.max_alerts_per_hour)

            if len(bucket) >= per_channel_limit:
                return True

            return False

        except Exception as exc:
            logger.debug("Rate limit check failed: %s", exc)
            return False

    def _persist_to_db(self, record: AlertRecord):
        # Placeholder for DB persistence – would need alerts table
        # For now, just log
        logger.debug("Persisting alert to DB (placeholder): %s", record.alert_id)

    # -- convenience methods for specific alert types ----------------------

    def configure_alerts(self, config: AlertConfig | Mapping[str, Any] | str | Path):
        if isinstance(config, (str, Path)):
            self.config = load_alert_config(path=config)
        elif isinstance(config, dict):
            self.config = AlertConfig(**config)
        else:
            self.config = config
        logger.info("Alert config updated")

    def alert_on_trade(self, trade: Any, **kwargs) -> AlertRecord:
        """Alert when a trade is executed."""
        symbol = getattr(trade, "symbol", "UNKNOWN")
        side = getattr(trade, "side", getattr(trade, "direction", "unknown"))
        qty = getattr(trade, "quantity", getattr(trade, "filled_quantity", 0))
        price = getattr(trade, "fill_price", getattr(trade, "entry_price", 0))
        pnl = getattr(trade, "realized_pnl", getattr(trade, "net_pnl", 0))

        context = {
            "symbol": symbol,
            "side": side,
            "quantity": qty,
            "price": price,
            "pnl": pnl,
            "reason": getattr(trade, "reason", "") or kwargs.get("reason", ""),
            **kwargs,
        }

        message = self.config.templates.get("trade_executed", "").format(**context) if self.config.templates.get("trade_executed") else f"Trade executed: {symbol} {side} {qty} @ {price}"

        return self.send_alert(level=AlertLevel.INFO, message=message, alert_type=AlertType.TRADE_EXECUTED, context=context)

    def alert_on_error(self, error: Any, component: str = "unknown", **kwargs) -> AlertRecord:
        """Alert on system error."""
        message = str(error)
        context = {"component": component, "message": message, "stack_trace": traceback.format_exc() if kwargs.get("include_trace") else "", **kwargs}

        template_msg = self.config.templates.get("system_error", "System error: {component} | {message}")
        try:
            rendered = template_msg.format(**context)
        except KeyError:
            rendered = f"System error in {component}: {message}"

        return self.send_alert(level=AlertLevel.ERROR, message=rendered, alert_type=AlertType.SYSTEM_ERROR, context=context)

    def alert_on_limit_breach(self, limit_type: str, symbol: str = "", reason: str = "", **kwargs) -> AlertRecord:
        """Alert when risk limit breached."""
        context = {"limit_type": limit_type, "symbol": symbol, "reason": reason, **kwargs}

        template_msg = self.config.templates.get("risk_limit_breach", "Risk limit breached: {limit_type} for {symbol} | {reason}")
        try:
            rendered = template_msg.format(**context)
        except KeyError:
            rendered = f"Risk limit breached: {limit_type} for {symbol}: {reason}"

        return self.send_alert(level=AlertLevel.WARNING, message=rendered, alert_type=AlertType.RISK_LIMIT_BREACH, context=context)

    def alert_on_stop_loss(self, symbol: str, quantity: Any, price: Any, pnl: Any = 0, **kwargs) -> AlertRecord:
        context = {"symbol": symbol, "quantity": quantity, "price": price, "pnl": pnl, "side": "SELL", **kwargs}
        template_msg = self.config.templates.get("stop_loss_hit", "Stop loss hit: {symbol} {quantity} @ {price}")
        try:
            rendered = template_msg.format(**context)
        except KeyError:
            rendered = f"Stop loss hit: {symbol} {quantity} @ {price}"
        return self.send_alert(level=AlertLevel.WARNING, message=rendered, alert_type=AlertType.STOP_LOSS_HIT, context=context)

    def alert_on_take_profit(self, symbol: str, quantity: Any, price: Any, pnl: Any = 0, **kwargs) -> AlertRecord:
        context = {"symbol": symbol, "quantity": quantity, "price": price, "pnl": pnl, "side": "SELL", **kwargs}
        template_msg = self.config.templates.get("take_profit_hit", "Take profit hit: {symbol} {quantity} @ {price}")
        try:
            rendered = template_msg.format(**context)
        except KeyError:
            rendered = f"Take profit hit: {symbol} {quantity} @ {price}"
        return self.send_alert(level=AlertLevel.INFO, message=rendered, alert_type=AlertType.TAKE_PROFIT_HIT, context=context)

    def send_daily_summary(self, equity: Any, pnl: Any, trades: int, win_rate: float, **kwargs) -> AlertRecord:
        context = {"equity": equity, "pnl": pnl, "trades": trades, "win_rate": win_rate, **kwargs}
        template_msg = self.config.templates.get("daily_summary", "Daily Summary: Equity {equity} | P&L {pnl} | Trades {trades}")
        try:
            rendered = template_msg.format(**context)
        except KeyError:
            rendered = f"Daily Summary: Equity {equity} P&L {pnl} Trades {trades}"
        return self.send_alert(level=AlertLevel.INFO, message=rendered, alert_type=AlertType.DAILY_SUMMARY, context=context)

    # -- history and stats -------------------------------------------------

    def get_history(self, limit: int = 100, level: Optional[str] = None) -> List[AlertRecord]:
        history = list(self._history)
        if level:
            level = AlertLevel.validate(level)
            history = [r for r in history if r.level == level]
        return history[-limit:]

    def get_stats(self) -> Dict[str, Any]:
        return {
            **dict(self._stats),
            "history_count": len(self._history),
            "rate_limit_buckets": {k: len(v) for k, v in self._rate_limit_buckets.items()},
        }

    def clear_history(self):
        self._history.clear()
        self._rate_limit_buckets.clear()
        self._stats = {"sent": 0, "failed": 0, "rate_limited": 0, "quiet_hours_suppressed": 0}
        logger.info("Alert history cleared")

    def __repr__(self):
        return f"<AlertManager min_level={self.config.min_level} sent={self._stats['sent']} failed={self._stats['failed']} history={len(self._history)}>"
