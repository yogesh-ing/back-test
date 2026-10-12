"""Remember-session-today — opt-in persistence for broker sessions.

Owner request (2026-09-24): the session lives only in memory, so every app
restart wiped it and forced a fresh TOTP login — painful mid-day when runners
must bind the live chain feed at creation time. This module adds an **opt-in**
"Remember session today" toggle.

Multi-broker PRD (2026-09-28, Phase A): persistence is now **per broker**.
The store file (``.broker_sessions.json``) holds a map of broker name →
``{token, expires_at, saved_at}`` so mStock and Dhan sessions survive a
restart independently:

* **ON + a successful TOTP** → that broker's token + expiry are written into
  the store. On every app boot the session manager seeds ALL saved sessions
  that are not yet expired, so no login is needed. Sessions expire the same
  day, so the file is naturally stale by tomorrow.
* **Toggle OFF at any time** → the file is deleted **immediately** and no
  further saves happen. The NEXT app session then requires fresh login +
  TOTP. The currently-running in-memory sessions are NOT killed (owner
  decision: flipping the toggle mid-day must not disrupt a live watch);
  explicit Logout still ends them (and forgets only that broker's entry).

Legacy migration: the pre-multi-broker single-session file
(``.mstock_remember_session``) is read once as an ``mstock`` entry when the
new store does not exist yet.

Security posture: tokens are stored in plaintext on the local machine —
the same trust level as the pre-existing ``.mstock_session_token`` cache
used by the data scripts. The file lives in the project root (gitignored).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("backtest.brokers.remember_session")

#: Project root = the dir containing src/ (this file is <root>/src/backtest/
#: brokers/remember_session.py → parents[3] = <root>).
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: Multi-broker store (Phase A). One JSON map for all brokers.
STORE_FILE = ".broker_sessions.json"
#: Pre-multi-broker single-session file — read-only migration source.
LEGACY_FILE = ".mstock_remember_session"

_ENV_TOGGLE = "BROKER_REMEMBER_SESSION"


def _project_root() -> Path:
    """Project root, overridable for tests via BROKER_REMEMBER_SESSION_PATH."""
    override = os.getenv("BROKER_REMEMBER_SESSION_PATH")
    return Path(override) if override else _PROJECT_ROOT


def store_path() -> Path:
    """Where the remember-session store lives (test-overridable)."""
    return _project_root() / STORE_FILE


def _legacy_path() -> Path:
    return _project_root() / LEGACY_FILE


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
    """Set the toggle. OFF deletes the saved token store immediately.

    Returns a small status dict for the API layer (``deleted`` reports
    whether a previously saved session store was removed).
    """
    enabled = bool(enabled)
    _write_toggle_file(enabled)
    deleted = False
    if not enabled and delete_saved:
        deleted = delete_saved_session()
    logger.info(
        "remember-session toggle → %s%s", "ON" if enabled else "OFF",
        " (saved sessions deleted)" if deleted else "",
    )
    return {"remember": enabled, "deleted": deleted, "path": str(store_path())}


# ---------------------------------------------------------------------------
# Saved-session store (per-broker map)
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, payload: dict) -> None:
    """Atomic write: temp file in the same directory, then replace."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".broker_sessions_")
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


def _parse_expiry(raw: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    # A broker can hand back a naive expiry: mStock computes its session
    # expiry from a local clock (datetime.now(), no tzinfo) and the store
    # keeps that ISO string verbatim. The store clock here (_now) is aware
    # UTC, so comparing a naive expiry against it raises
    # ``TypeError: can't compare offset-naive and offset-aware datetimes``
    # in _prune() on every save. Re-attach the local zone (a naive value is
    # presumed system-local time) so both operands are aware and the expiry
    # check stays correct instead of crashing. Already-aware values pass
    # through untouched.
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def _read_raw_store() -> Optional[Dict[str, dict]]:
    """The raw ``{broker: entry}`` map from disk, or ``None`` when unusable.

    A corrupt store is deleted on read so it can never be resurrected by a
    later boot. Falls back to the legacy single-session file (as an
    ``mstock`` entry) when the new store does not exist.
    """
    path = store_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        sessions = data.get("sessions")
        if not isinstance(sessions, dict):
            raise ValueError("missing sessions map")
        return {str(k): v for k, v in sessions.items() if isinstance(v, dict)}
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        logger.warning("remember-session store corrupt — removing: %s", path)
        delete_saved_session()
        return None

    # Legacy migration path (single-session file → mstock entry).
    legacy = _legacy_path()
    try:
        data = json.loads(legacy.read_text(encoding="utf-8"))
        broker = str(data.get("broker", "mstock"))
        return {broker: {k: data.get(k) for k in ("token", "expires_at", "saved_at")}}
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001
        logger.warning("legacy remember-session file corrupt — removing: %s", legacy)
        try:
            legacy.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def _write_store(sessions: Dict[str, dict]) -> None:
    """Persist the pruned session map; an empty map deletes the file."""
    path = store_path()
    if not sessions:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return
    _atomic_write(path, {"sessions": sessions, "updated": _now().isoformat()})


def save_session(token: str, expires_at: datetime, broker: str = "mstock") -> bool:
    """Persist one broker's live session for restoration on the next boot.

    Only called when the toggle is ON. Failures are logged, never raised —
    a save problem must never break the login that produced the token.
    Other brokers' entries are preserved (per-broker independence).
    """
    if not token:
        return False
    try:
        sessions = _read_raw_store() or {}
        sessions[str(broker)] = {
            "token": token,
            "expires_at": expires_at.isoformat(),
            "saved_at": _now().isoformat(),
        }
        _write_store(_prune(sessions))
        logger.info(
            "remember-session: %s session saved (expires %s)", broker, expires_at.isoformat()
        )
        return True
    except Exception:  # noqa: BLE001 — persistence must never break auth
        logger.warning("remember-session save failed: %s", store_path(), exc_info=True)
        return False


def _prune(sessions: Dict[str, dict]) -> Dict[str, dict]:
    """Drop expired / malformed entries (they can never be restored)."""
    keep: Dict[str, dict] = {}
    for broker, entry in sessions.items():
        expires_at = _parse_expiry(entry.get("expires_at"))
        token = entry.get("token")
        if expires_at is None or not token:
            logger.info("remember-session: dropping malformed %s entry", broker)
            continue
        if _now() >= expires_at:
            logger.info("remember-session: saved %s session already expired — dropping", broker)
            continue
        keep[broker] = entry
    return keep


def load_sessions() -> Dict[str, dict[str, Any]]:
    """All restorable sessions: ``{broker: {token, expires_at(datetime)}}``.

    Expired/corrupt entries are removed on read (the store on disk is
    rewritten pruned; an empty store is deleted).
    """
    raw = _read_raw_store()
    if raw is None:
        return {}
    pruned = _prune(raw)
    if pruned != raw or not pruned:
        try:
            _write_store(pruned)
            # Pruning rewrote the new store — the legacy file (if any) is
            # now stale either way.
            _legacy_path().unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 — cleanup is best-effort
            pass
    return {
        broker: {
            "broker": broker,
            "token": str(entry["token"]),
            "expires_at": _parse_expiry(entry["expires_at"]),
        }
        for broker, entry in pruned.items()
    }


def load_session(broker: str | None = None) -> Optional[dict[str, Any]]:
    """One saved session payload, or ``None`` when absent/expired/corrupt.

    ``broker=None`` keeps the legacy single-session semantics: it returns
    the sole saved entry when exactly one broker is remembered (the common
    single-broker deployment), else ``None`` (callers that understand
    multi-broker use :func:`load_sessions`).
    """
    sessions = load_sessions()
    if broker is not None:
        return sessions.get(str(broker))
    if len(sessions) == 1:
        return next(iter(sessions.values()))
    return None


def delete_saved_session(broker: str | None = None) -> bool:
    """Forget saved session(s). Returns True when something was removed.

    ``broker=None`` removes the whole store (legacy behaviour — used by the
    toggle-OFF path); a broker name removes only that broker's entry,
    leaving other brokers' remembered sessions intact.
    """
    path = store_path()
    try:
        if broker is None:
            existed = path.exists() or _legacy_path().exists()
            path.unlink(missing_ok=True)
            _legacy_path().unlink(missing_ok=True)
            return existed or True
        sessions = _read_raw_store() or {}
        if str(broker) not in sessions:
            return False
        sessions.pop(str(broker), None)
        _write_store(_prune(sessions))
        _legacy_path().unlink(missing_ok=True)
        return True
    except Exception:  # noqa: BLE001 — a delete failure must never break auth
        logger.warning("remember-session delete failed: %s", path, exc_info=True)
        return False


def has_saved_session(broker: str | None = None) -> bool:
    """True when a restorable saved session exists (UI display helper)."""
    if broker is not None:
        return load_session(broker) is not None
    return bool(load_sessions())
