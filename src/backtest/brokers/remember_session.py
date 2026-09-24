"""Remember-session-today — opt-in persistence for the mStock broker session.

Owner request (2026-09-24): the session lives only in memory, so every app
restart wiped it and forced a fresh TOTP login — painful mid-day when runners
must bind the live chain feed at creation time. This module adds an **opt-in**
"Remember session today" toggle:

* **ON + a successful TOTP** → the raw token + expiry are written to a
  gitignored file (``.mstock_remember_session``). On every app boot the
  session manager seeds itself from that file when it exists and is not
  expired, so no login is needed. Because mStock sessions expire the same
  day (~17:40 IST), the file is naturally stale by tomorrow.
* **Toggle OFF at any time** → the file is deleted **immediately** and no
  further saves happen. The NEXT app session then requires fresh login +
  TOTP. The currently-running in-memory session is NOT killed (owner
  decision: flipping the toggle mid-day must not disrupt a live watch);
  explicit Logout still ends it.

Security posture: the token is stored in plaintext on the local machine —
the same trust level as the pre-existing ``.mstock_session_token`` cache
used by the data scripts. The file lives in the project root (gitignored)
and is written with restrictive semantics (no group/world bits on POSIX).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("backtest.brokers.remember_session")

#: Project root = the dir containing src/ (this file is <root>/src/backtest/
#: brokers/remember_session.py → parents[3] = <root>).
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PATH = _PROJECT_ROOT / ".mstock_remember_session"

_ENV_TOGGLE = "BROKER_REMEMBER_SESSION"


def _project_root() -> Path:
    """Project root, overridable for tests via BROKER_REMEMBER_SESSION_PATH."""
    override = os.getenv("BROKER_REMEMBER_SESSION_PATH")
    return Path(override) if override else _PROJECT_ROOT


def store_path() -> Path:
    """Where the remember-session file lives (test-overridable)."""
    return _project_root() / DEFAULT_PATH.name


# ---------------------------------------------------------------------------
# Toggle state (runtime, per process) + file persistence
# ---------------------------------------------------------------------------

_TOGGLE_FILE = "broker_remember_session.json"


def _toggle_file() -> Path:
    return _project_root() / _TOGGLE_FILE


def _read_toggle_file() -> Optional[bool]:
    """Persisted toggle choice, or ``None`` when absent/unreadable.

    The choice survives restarts so the UI checkbox reflects what the user
    last selected rather than a hardcoded default.
    """
    path = _toggle_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return bool(data.get("remember"))
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001 — a corrupt toggle file is just a default
        logger.warning("remember-session toggle file unreadable: %s", path)
        return None


def _write_toggle_file(value: bool) -> None:
    path = _toggle_file()
    try:
        path.write_text(
            json.dumps({"remember": bool(value), "updated": _now().isoformat()}),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001 — a toggle save must never break auth
        logger.warning("remember-session toggle save failed: %s", path)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_toggle() -> bool:
    """Whether "Remember session today" is currently ON.

    Order: explicit env var (tests/ops) → persisted toggle file → default OFF
    (opt-in, never remember unless the user asked).
    """
    env = os.getenv(_ENV_TOGGLE)
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    persisted = _read_toggle_file()
    if persisted is not None:
        return persisted
    return False


def set_toggle(enabled: bool, *, delete_saved: bool = True) -> dict[str, Any]:
    """Set the toggle. OFF deletes the saved token file immediately.

    Returns a small status dict for the API layer (``deleted`` reports
    whether a previously saved session file was removed).
    """
    enabled = bool(enabled)
    _write_toggle_file(enabled)
    deleted = False
    if not enabled and delete_saved:
        deleted = delete_saved_session()
    logger.info(
        "remember-session toggle → %s%s", "ON" if enabled else "OFF",
        " (saved session deleted)" if deleted else "",
    )
    return {"remember": enabled, "deleted": deleted, "path": str(store_path())}


# ---------------------------------------------------------------------------
# Saved-session file
# ---------------------------------------------------------------------------


def save_session(token: str, expires_at: datetime, broker: str = "mstock") -> bool:
    """Persist the live session for restoration on the next boot.

    Only called when the toggle is ON. Failures are logged, never raised —
    a save problem must never break the login that produced the token.
    """
    if not token:
        return False
    path = store_path()
    try:
        payload = {
            "broker": broker,
            "token": token,
            "expires_at": expires_at.isoformat(),
            "saved_at": _now().isoformat(),
        }
        # Atomic write: temp file in the same directory, then replace.
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".mstock_remember_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        logger.info("remember-session: %s session saved (expires %s)", broker, expires_at.isoformat())
        return True
    except Exception:  # noqa: BLE001 — persistence must never break auth
        logger.warning("remember-session save failed: %s", path, exc_info=True)
        return False


def load_session() -> Optional[dict[str, Any]]:
    """The saved session payload, or ``None`` when absent/expired/corrupt.

    An expired or corrupt file is deleted on read so it can never be
    resurrected by a later boot.
    """
    path = store_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001
        logger.warning("remember-session file corrupt — removing: %s", path)
        delete_saved_session()
        return None

    try:
        expires_at = datetime.fromisoformat(str(data.get("expires_at")))
    except (TypeError, ValueError):
        logger.warning("remember-session file has a bad expiry — removing: %s", path)
        delete_saved_session()
        return None

    if _now() >= expires_at:
        logger.info("remember-session: saved %s session already expired — removing", data.get("broker"))
        delete_saved_session()
        return None

    token = data.get("token")
    if not token:
        return None
    return {
        "broker": str(data.get("broker", "mstock")),
        "token": str(token),
        "expires_at": expires_at,
    }


def delete_saved_session() -> bool:
    """Remove the saved-session file. Returns True when a file was removed."""
    path = store_path()
    try:
        path.unlink(missing_ok=True)
        return True
    except Exception:  # noqa: BLE001 — a delete failure must never break auth
        logger.warning("remember-session delete failed: %s", path, exc_info=True)
        return False


def has_saved_session() -> bool:
    """True when a restorable saved session exists (UI display helper)."""
    return load_session() is not None
