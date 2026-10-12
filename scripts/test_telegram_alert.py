"""Live Telegram notification test — broker → notifier → Telegram API.

Usage (from the repo root):

    python scripts/test_telegram_alert.py                 # info test alert
    python scripts/test_telegram_alert.py --severity critical
    python scripts/test_telegram_alert.py --type risk_limit_breach --message "custom text"

Credentials: put TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env or the shell
environment before running. The script loads .env itself, so no other setup
is needed. It does not modify any config file.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _load_env() -> None:
    """Populate os.environ from .env (no third-party dependency)."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--severity", default="info", choices=["info", "warning", "error", "critical"]
    )
    parser.add_argument(
        "--type",
        default="risk_limit_breach",
        help="Alert type (default: risk_limit_breach, which routes to telegram in config/alerts.yaml)",
    )
    parser.add_argument("--message", default="Live test of the Telegram alert pipeline")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    _load_env()

    if not (os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")):
        print(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set.\n"
            "Add them to .env (or export in the shell) and re-run.",
            file=sys.stderr,
        )
        return 2

    from backtest.alerts.broker import get_alert_broker
    from backtest.alerts.notifier import load_notifier_config, start_alert_notifier

    alert_type = str(args.type)

    config = load_notifier_config()
    tg_cfg = config.get("channels", {}).get("telegram", {})
    if not (tg_cfg.get("enabled") or os.environ.get("TELEGRAM_BOT_TOKEN")):
        print(
            "Telegram channel is not enabled "
            "(set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID).",
            file=sys.stderr,
        )
        return 2

    broker = get_alert_broker()
    notifier = start_alert_notifier(synchronous=True)
    if notifier is None:
        print("Outbound notifier disabled (ALERT_NOTIFIER=0).", file=sys.stderr)
        return 2

    print(f"channels: {sorted(notifier.channels)}  (config/alerts.yaml + env)")
    print(f"sending: [{args.severity.upper()}] {alert_type} — {args.message}")

    broker.raise_alert(
        alert_type,
        args.severity,
        args.message,
        data={"source": "scripts/test_telegram_alert.py", "env": "live-test"},
        subject="live-test",
    )

    print(f"sent={notifier.sent}  failures={notifier.failures}")
    if notifier.failures:
        print("Delivery failed — check the traceback above.", file=sys.stderr)
        return 1
    if notifier.sent == 0:
        print(
            "Nothing was sent: this alert type is not routed to telegram in "
            "config/alerts.yaml (type routing wins over severity routing). "
            "Re-run with --type risk_limit_breach.",
            file=sys.stderr,
        )
        return 1
    print("OK — check your Telegram chat for the message.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
