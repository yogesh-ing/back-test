"""Outbound alert channels — log, Telegram, email (hygiene item #17).

The AlertBroker is in-app information: the widget, the strategies and the
trader at the dashboard see it. This module is the way *out* of the
process — a Telegram message or an email when something breaks and nobody
is watching (overnight breaker trips, stale feeds).

Design constraints (this runs next to a trading loop):

* **Never block the caller.** ``_on_alert`` composes the message and hands
  it to a bounded queue; a daemon worker thread does the network I/O, so a
  dead SMTP host cannot stall a risk tick. ``synchronous=True`` sends
  inline (tests, scripts) and ``flush()`` drains the queue.
* **Never raise.** A channel failure is logged and counted; the broker and
  its other subscribers are unaffected.
* **Route, filter, throttle.** ``config/alerts.yaml`` decides which
  channels see which alert (by type first, then severity), the minimum
  severity, quiet hours (IST by default) and hourly rate limits. Secrets
  come from env vars, never from the YAML.
* **Compose safely.** A template is used only when every ``{placeholder}``
  in it can be filled from the alert data; otherwise a plain
  ``[SEVERITY] type — message`` fallback goes out. A rendered "None" is
  worse than no template.

Fail-soft everywhere: with no config file, no credentials and no network,
the notifier degrades to a log channel and the platform is unaffected.

Env knobs (read by :func:`load_notifier_config` / :func:`start_alert_notifier`):

    ALERT_NOTIFIER=0           master off-switch (default: on)
    ALERT_PROFILE=telegram_only  pick a profile from config/alerts.yaml
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    ALERT_SMTP_HOST, ALERT_SMTP_PORT (587), ALERT_SMTP_USER,
    ALERT_EMAIL_PASSWORD, ALERT_FROM_EMAIL, ALERT_TO_EMAILS (comma-sep),
    ALERT_SMTP_STARTTLS=0      disable STARTTLS

Critical alerts (breaker trips, stale feed) bypass quiet hours when
``quiet_allow_critical`` is set and are exempt from rate limits — throttling
away the one message that mattered would defeat the point.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import smtplib
import string
import threading
import time as _time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from backtest.alerts.broker import get_alert_broker
from backtest.alerts.types import AlertType

logger = logging.getLogger("backtest.alerts.outbound")

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "alerts.yaml"
_WINDOW_S = 3600.0
_MAX_TELEGRAM_CHARS = 4000  # Telegram hard limit is 4096
_LEVEL_RANK = {"debug": 0, "info": 1, "warning": 2, "error": 3, "critical": 4}
_ENV_OFF = {"0", "false", "no", "off"}


def _level_rank(level: Any) -> int:
    return _LEVEL_RANK.get(str(level).strip().lower(), 1)


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = dict(base)
    for key, value in override.items():
        if isinstance(out.get(key), dict) and isinstance(value, Mapping):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def _parse_hhmm(value: Any) -> Tuple[int, int]:
    hour, _, minute = str(value).partition(":")
    return int(hour), int(minute or 0)


def _tzinfo(name: str) -> Optional[Any]:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — no tz database on this host
        # IST has no DST; the fixed offset keeps quiet hours working on hosts
        # without the tzdata package (Windows venvs sometimes lack it).
        if str(name) in ("Asia/Kolkata", "Asia/Calcutta"):
            return timezone(timedelta(hours=5, minutes=30))
        return None


class _Strict(dict):
    def __missing__(self, key: Any) -> Any:
        raise KeyError(key)


def _render_template(template: str, data: Mapping[str, Any]) -> Optional[str]:
    """Fill ``{placeholders}`` from ``data``; None if anything is missing."""
    formatter = string.Formatter()
    try:
        fields = [name for _, name, _, _ in formatter.parse(template) if name]
    except ValueError:
        return None
    for field in fields:
        root = field.split(".")[0].split("[")[0]
        if not root or data.get(root) is None:
            return None
    try:
        return template.format_map(_Strict(data))
    except Exception:  # noqa: BLE001 — any format explosion means "no template"
        return None


_MD_SPECIALS = ("\\", "_", "*", "[", "]", "`")


def _escape_markdown(text: str) -> str:
    """Escape legacy-Markdown specials so identifiers like ``risk_limit_breach``
    display intact instead of turning into italics/broken entities."""
    for ch in _MD_SPECIALS:
        text = text.replace(ch, "\\" + ch)
    return text


@dataclass
class AlertJob:
    """One composed notification, delivered per channel."""

    alert_type: str
    severity: str
    subject: str
    data: Dict[str, Any]
    text: str


class Channel:
    """A delivery target. ``send`` raises on failure; callers isolate."""

    name = "channel"

    def send(self, job: AlertJob) -> None:  # pragma: no cover — interface
        raise NotImplementedError


class LogChannel(Channel):
    """Always-on floor: alerts land in the process log / log file."""

    name = "log"

    def __init__(self, out_logger: Optional[logging.Logger] = None) -> None:
        self._logger = out_logger or logger

    def send(self, job: AlertJob) -> None:
        level = _level_rank(job.severity)
        if level >= 4:
            lvl = logging.CRITICAL
        elif level >= 2:
            lvl = logging.WARNING
        else:
            lvl = logging.INFO
        self._logger.log(lvl, "%s", job.text)


class TelegramChannel(Channel):
    """Bot API ``sendMessage`` over HTTPS (stdlib, injectable opener)."""

    name = "telegram"

    def __init__(
        self,
        token: str,
        chat_id: Any,
        parse_mode: str = "",
        timeout: float = 10.0,
        opener: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.token = token
        self.chat_id = str(chat_id)
        self.parse_mode = parse_mode or ""
        self.timeout = float(timeout)
        self._opener = opener or urllib.request.urlopen

    def send(self, job: AlertJob) -> None:
        text = job.text
        if self.parse_mode:
            text = _escape_markdown(text)
        body: Dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text[:_MAX_TELEGRAM_CHARS],
            "disable_web_page_preview": True,
        }
        if self.parse_mode:
            body["parse_mode"] = self.parse_mode
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        response = self._opener(request, timeout=self.timeout)
        try:
            raw = response.read()
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        try:
            parsed = json.loads(raw.decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001 — non-JSON body: trust the HTTP status
            return
        if isinstance(parsed, dict) and parsed.get("ok") is False:
            raise RuntimeError(
                f"telegram rejected message: {parsed.get('description', parsed)}"
            )


class EmailChannel(Channel):
    """SMTP (STARTTLS) delivery; stdlib only, injectable factory."""

    name = "email"

    def __init__(
        self,
        host: str,
        from_email: str,
        to_emails: Iterable[str],
        port: int = 587,
        user: str = "",
        password: str = "",
        use_starttls: bool = True,
        timeout: float = 10.0,
        smtp_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.from_email = from_email
        self.to_emails = _as_list(to_emails)
        self.use_starttls = bool(use_starttls)
        self.timeout = float(timeout)
        self._smtp_factory = smtp_factory or smtplib.SMTP

    def send(self, job: AlertJob) -> None:
        message = EmailMessage()
        message["Subject"] = f"[{job.severity.upper()}] {job.alert_type.replace('_', ' ')}"
        message["From"] = self.from_email
        message["To"] = ", ".join(self.to_emails)
        lines = [job.text]
        payload = {k: v for k, v in job.data.items() if k != "alert_id"}
        if payload:
            lines.append("")
            lines.append(json.dumps(payload, indent=2, default=str))
        message.set_content("\n".join(lines))

        client = self._smtp_factory(self.host, self.port, timeout=self.timeout)
        try:
            client.ehlo()
            if self.use_starttls:
                client.starttls()
                client.ehlo()
            if self.user and self.password:
                client.login(self.user, self.password)
            client.send_message(message)
        finally:
            try:
                client.quit()
            except Exception:  # noqa: BLE001 — close is best-effort
                pass


def build_channels(
    config: Mapping[str, Any],
    *,
    telegram_opener: Optional[Callable[..., Any]] = None,
    smtp_factory: Optional[Callable[..., Any]] = None,
) -> Dict[str, Channel]:
    """Enabled channels from a merged notifier config (env already applied).

    ``telegram_opener`` / ``smtp_factory`` inject fake transports in tests;
    production callers leave them None (stdlib defaults).
    """
    cfg = config.get("channels") or {}
    channels: Dict[str, Channel] = {}

    log_cfg = cfg.get("log") or {}
    if log_cfg.get("enabled", True):
        channels["log"] = LogChannel()

    tg = cfg.get("telegram") or {}
    if tg.get("enabled") and tg.get("telegram_bot_token") and tg.get("telegram_chat_id"):
        channels["telegram"] = TelegramChannel(
            token=str(tg["telegram_bot_token"]),
            chat_id=tg["telegram_chat_id"],
            parse_mode=str(tg.get("telegram_parse_mode") or ""),
            timeout=float(tg.get("timeout") or 10.0),
            opener=telegram_opener,
        )

    em = cfg.get("email") or {}
    to_emails = _as_list(em.get("to_emails"))
    if em.get("enabled") and em.get("smtp_host") and to_emails:
        channels["email"] = EmailChannel(
            host=str(em["smtp_host"]),
            port=int(em.get("smtp_port") or 587),
            user=str(em.get("smtp_user") or ""),
            password=str(em.get("smtp_password") or ""),
            from_email=str(em.get("from_email") or em.get("smtp_user") or ""),
            to_emails=to_emails,
            use_starttls=bool(em.get("use_starttls", True)),
            timeout=float(em.get("timeout") or 10.0),
            smtp_factory=smtp_factory,
        )

    if not channels:
        # Fail-safe: a misconfigured notifier still records alerts somewhere.
        channels["log"] = LogChannel()
    return channels


def load_notifier_config(
    path: Optional[Any] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Merge ``config/alerts.yaml`` (base profile + active profile) with env.

    Missing/broken YAML is not an error — the caller gets a minimal config
    (log channel only) rather than an exception.
    """
    env_map: Mapping[str, str] = os.environ if env is None else env
    cfg_path = Path(path) if path is not None else _CONFIG_PATH
    data: Dict[str, Any] = {}
    try:
        import yaml

        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        logger.debug("[alerts] %s not found — using defaults", cfg_path)
    except Exception:  # noqa: BLE001 — a broken file must not break alerts
        logger.warning("[alerts] could not read %s — using defaults", cfg_path, exc_info=True)
    if not isinstance(data, dict):
        data = {}

    profile = str(env_map.get("ALERT_PROFILE") or data.get("active_profile") or "default")
    base = data.get("default")
    base = base if isinstance(base, dict) else {}
    named = (data.get("profiles") or {}).get(profile)
    merged = _deep_merge(base, named if isinstance(named, dict) else {})

    channels = merged.get("channels")
    if not isinstance(channels, dict):
        channels = merged["channels"] = {}

    tg = channels.get("telegram")
    if not isinstance(tg, dict):
        tg = channels["telegram"] = {}
    token = env_map.get("TELEGRAM_BOT_TOKEN") or tg.get("telegram_bot_token")
    chat = env_map.get("TELEGRAM_CHAT_ID") or tg.get("telegram_chat_id")
    if token:
        tg["telegram_bot_token"] = token
    if chat:
        tg["telegram_chat_id"] = chat
    if token and chat:
        tg["enabled"] = True

    em = channels.get("email")
    if not isinstance(em, dict):
        em = channels["email"] = {}
    for env_key, field in (
        ("ALERT_SMTP_HOST", "smtp_host"),
        ("ALERT_SMTP_USER", "smtp_user"),
        ("ALERT_EMAIL_PASSWORD", "smtp_password"),
        ("ALERT_FROM_EMAIL", "from_email"),
    ):
        value = env_map.get(env_key)
        if value:
            em[field] = value
    port = env_map.get("ALERT_SMTP_PORT")
    if port:
        try:
            em["smtp_port"] = int(port)
        except (TypeError, ValueError):
            logger.warning("[alerts] ALERT_SMTP_PORT=%r is not a number — ignored", port)
    to_emails = env_map.get("ALERT_TO_EMAILS")
    if to_emails:
        em["to_emails"] = _as_list(to_emails)
    if env_map.get("ALERT_SMTP_STARTTLS") is not None:
        em["use_starttls"] = (
            str(env_map["ALERT_SMTP_STARTTLS"]).strip().lower() not in _ENV_OFF
        )
    if not em.get("from_email") and em.get("smtp_user"):
        em["from_email"] = em["smtp_user"]
    if em.get("smtp_host") and _as_list(em.get("to_emails")):
        em["enabled"] = True

    return merged


class AlertNotifier:
    """Broker subscriber that fans alerts out to the configured channels."""

    subscriber_id = "alert-notifier"

    def __init__(
        self,
        broker: Any,
        config: Optional[Mapping[str, Any]] = None,
        *,
        synchronous: bool = False,
        clock: Callable[[], float] = _time.monotonic,
        now: Optional[Callable[[], datetime]] = None,
        max_queue: int = 1000,
        telegram_opener: Optional[Callable[..., Any]] = None,
        smtp_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._broker = broker
        cfg: Dict[str, Any] = dict(config or {})
        self.min_level = str(cfg.get("min_level", "info"))
        self.routing: Dict[str, List[str]] = {
            str(k): [str(v) for v in (vals or [])]
            for k, vals in (cfg.get("routing") or {}).items()
        }
        self.templates: Dict[str, str] = dict(cfg.get("templates") or {})
        self.quiet_hours_enabled = bool(cfg.get("quiet_hours_enabled", False))
        self.quiet_start = str(cfg.get("quiet_start", "22:00"))
        self.quiet_end = str(cfg.get("quiet_end", "07:00"))
        self.quiet_timezone = str(cfg.get("quiet_timezone", "Asia/Kolkata"))
        self.quiet_allow_critical = bool(cfg.get("quiet_allow_critical", True))
        self.rate_limit_enabled = bool(cfg.get("rate_limit_enabled", False))
        self.max_per_hour = int(cfg.get("max_alerts_per_hour") or 0)
        self.max_per_hour_per_channel = {
            str(k): int(v)
            for k, v in (cfg.get("max_alerts_per_hour_per_channel") or {}).items()
        }
        self.channels = build_channels(
            cfg, telegram_opener=telegram_opener, smtp_factory=smtp_factory
        )
        self.synchronous = bool(synchronous)
        self._clock = clock
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._queue: "queue.Queue[Optional[Tuple[Channel, AlertJob]]]" = queue.Queue(
            maxsize=max_queue
        )
        self._thread: Optional[threading.Thread] = None
        self._rate: Dict[str, List[float]] = {}
        self._rate_lock = threading.Lock()
        self.running = False
        self.sent = 0
        self.failures = 0
        self.dropped = 0

    # -- lifecycle --------------------------------------------------------

    def start(self) -> "AlertNotifier":
        if self.running:
            return self
        self.running = True
        try:
            self._broker.unsubscribe(self.subscriber_id)
        except Exception:  # noqa: BLE001 — dropping stale subscriptions is harmless
            pass
        count = 0
        for alert_type in AlertType:
            try:
                self._broker.subscribe(
                    alert_type.value,
                    self._on_alert,
                    subscriber_id=self.subscriber_id,
                    meta={"runner": "AlertNotifier"},
                )
                count += 1
            except Exception:  # noqa: BLE001
                logger.exception("notifier subscribe failed for %s", alert_type.value)
        if not self.synchronous:
            self._thread = threading.Thread(
                target=self._worker, name="alert-notifier", daemon=True
            )
            self._thread.start()
        logger.info(
            "[alerts] outbound notifier active: channels=%s (min_level=%s, %d types)",
            sorted(self.channels),
            self.min_level,
            count,
        )
        return self

    def stop(self, timeout: float = 2.0) -> None:
        if not self.running:
            return
        self.running = False
        try:
            self._broker.unsubscribe(self.subscriber_id)
        except Exception:  # noqa: BLE001
            pass
        if self._thread is not None:
            try:
                self._queue.put_nowait(None)
            except queue.Full:  # pragma: no cover — worker keeps draining
                pass
            self._thread.join(timeout=timeout)
            self._thread = None
        logger.info("[alerts] outbound notifier stopped")

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until every queued message has been attempted."""
        if self.synchronous:
            return True
        deadline = _time.monotonic() + float(timeout)
        while self._queue.unfinished_tasks and _time.monotonic() < deadline:
            _time.sleep(0.01)
        return self._queue.unfinished_tasks == 0

    # -- delivery ---------------------------------------------------------

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                channel, job = item
                self._deliver(channel, job)
            finally:
                self._queue.task_done()

    def _deliver(self, channel: Channel, job: AlertJob) -> None:
        try:
            channel.send(job)
            self.sent += 1
        except Exception:  # noqa: BLE001 — one dead channel never stops the others
            self.failures += 1
            logger.exception(
                "[alerts] %s delivery failed (%s)", channel.name, job.alert_type
            )

    # -- broker callback --------------------------------------------------

    def _on_alert(self, alert_type: str, payload: Dict[str, Any]) -> None:
        try:
            if not self.running:
                return
            severity = str(payload.get("severity", "info"))
            if _level_rank(severity) < _level_rank(self.min_level):
                return
            message = str(payload.get("message", "") or "")
            alert_type = str(alert_type)
            job = AlertJob(
                alert_type=alert_type,
                severity=severity,
                subject=str(payload.get("subject", "") or ""),
                data={k: v for k, v in payload.items() if k not in ("message", "alert_id")},
                text=self._compose(alert_type, severity, message, payload),
            )
            channels = self._channels_for(alert_type, severity)
            if not channels:
                return
            when = self._now()
            if self._quiet_blocks(severity, when):
                channels = [c for c in channels if isinstance(c, LogChannel)]
                if not channels:
                    logger.debug("[alerts] quiet hours: %s held back", alert_type)
                    return
            if self.rate_limit_enabled and _level_rank(severity) < _LEVEL_RANK["critical"]:
                if not self._rate_allow("__all__", self.max_per_hour):
                    self.dropped += 1
                    logger.warning("[alerts] rate limit hit — %s suppressed", alert_type)
                    return
                channels = [
                    c
                    for c in channels
                    if self._rate_allow(
                        f"channel:{c.name}",
                        self.max_per_hour_per_channel.get(c.name, 0),
                    )
                ]
                if not channels:
                    self.dropped += 1
                    return
            for channel in channels:
                self._enqueue(channel, job)
        except Exception:  # noqa: BLE001 — the broker must never see a raise
            self.failures += 1
            logger.exception("[alerts] notifier failed to process %s", alert_type)

    def _enqueue(self, channel: Channel, job: AlertJob) -> None:
        if self.synchronous:
            self._deliver(channel, job)
            return
        try:
            self._queue.put_nowait((channel, job))
        except queue.Full:
            self.dropped += 1
            logger.warning("[alerts] queue full — %s dropped", job.alert_type)

    # -- policy -----------------------------------------------------------

    def _channels_for(self, alert_type: str, severity: str) -> List[Channel]:
        names = self.routing.get(alert_type) or self.routing.get(severity) or []
        out: List[Channel] = []
        for name in names:
            channel = self.channels.get(str(name))
            if channel is not None and channel not in out:
                out.append(channel)
        if not out and "log" in self.channels:
            # The log channel is the floor: an alert that passed min_level is
            # never silently dropped, even when its routing names only
            # channels this deployment has not enabled.
            out = [self.channels["log"]]
        return out

    def _rate_allow(self, key: str, limit: int) -> bool:
        if not self.rate_limit_enabled or limit <= 0:
            return True
        now = self._clock()
        with self._rate_lock:
            stamps = [s for s in self._rate.get(key, []) if s > now - _WINDOW_S]
            if len(stamps) >= limit:
                self._rate[key] = stamps
                return False
            stamps.append(now)
            self._rate[key] = stamps
            return True

    def _quiet_blocks(self, severity: str, when: datetime) -> bool:
        if not self.quiet_hours_enabled:
            return False
        if self.quiet_allow_critical and _level_rank(severity) >= _LEVEL_RANK["critical"]:
            return False
        try:
            start_h, start_m = _parse_hhmm(self.quiet_start)
            end_h, end_m = _parse_hhmm(self.quiet_end)
        except (TypeError, ValueError):
            return False
        tz = _tzinfo(self.quiet_timezone)
        local = when.astimezone(tz) if tz else when
        minutes = local.hour * 60 + local.minute
        start = start_h * 60 + start_m
        end = end_h * 60 + end_m
        if start == end:
            return False
        if start < end:
            return start <= minutes < end
        return minutes >= start or minutes < end

    def _compose(
        self, alert_type: str, severity: str, message: str, data: Mapping[str, Any]
    ) -> str:
        template = self.templates.get(alert_type)
        if template:
            rendered = _render_template(str(template), data)
            if rendered:
                return rendered
        head = f"[{severity.upper()}] {alert_type.replace('_', ' ')}"
        return f"{head} — {message}" if message else head


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

_NOTIFIER: Optional[AlertNotifier] = None
_NOTIFIER_LOCK = threading.Lock()


def start_alert_notifier(
    broker: Any = None,
    config: Optional[Mapping[str, Any]] = None,
    env: Optional[Mapping[str, str]] = None,
    *,
    synchronous: bool = False,
    path: Optional[Any] = None,
) -> Optional[AlertNotifier]:
    """Create + start the process-wide notifier (idempotent).

    Returns None when disabled via ``ALERT_NOTIFIER=0``. Safe to call from
    any boot path: repeated calls return the already-running instance.
    """
    global _NOTIFIER
    with _NOTIFIER_LOCK:
        if _NOTIFIER is not None and _NOTIFIER.running:
            return _NOTIFIER
        env_map: Mapping[str, str] = os.environ if env is None else env
        if str(env_map.get("ALERT_NOTIFIER", "")).strip().lower() in _ENV_OFF:
            logger.info("[alerts] outbound notifier disabled by ALERT_NOTIFIER")
            return None
        if config is None:
            config = load_notifier_config(path=path, env=env_map)
        notifier = AlertNotifier(
            broker if broker is not None else get_alert_broker(),
            config,
            synchronous=synchronous,
        )
        notifier.start()
        _NOTIFIER = notifier
        return notifier


def get_notifier() -> Optional[AlertNotifier]:
    """The process-wide notifier, or None when not started."""
    return _NOTIFIER


def stop_alert_notifier() -> None:
    """Tear down the process-wide notifier (tests / restart)."""
    global _NOTIFIER
    with _NOTIFIER_LOCK:
        if _NOTIFIER is not None:
            _NOTIFIER.stop()
            _NOTIFIER = None
