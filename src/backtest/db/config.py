"""Database configuration loading for the forward testing simulator.

Resolves settings from three sources, most specific first:

1. explicit keyword arguments to :func:`load_config`
2. ``FORWARD_TEST_DB_*`` environment variables (where secrets belong)
3. ``config/database.yaml`` — the active profile, then the ``default`` block

The split matters: the YAML file is committed and describes *shape* (pool
sizes, timeouts, logging), while the environment supplies *secrets* (the
connection URL). Nothing here ever writes credentials to disk.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

__all__ = [
    "DatabaseConfig",
    "ConfigError",
    "load_config",
    "DEFAULT_CONFIG_PATH",
    "ENV_PREFIX",
]

ENV_PREFIX = "FORWARD_TEST_DB"

#: ``config/database.yaml`` relative to the repository root.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "database.yaml"


class ConfigError(RuntimeError):
    """Raised when configuration is missing, malformed, or self-contradictory."""


def _as_bool(value: Any) -> bool:
    """Parse a boolean from YAML or from an environment string."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off", ""}:
        return False
    raise ConfigError(f"cannot interpret {value!r} as a boolean")


def _as_int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc


def _as_float(value: Any, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc


@dataclass(frozen=True)
class DatabaseConfig:
    """Immutable, validated database settings.

    Frozen so a manager cannot be reconfigured behind the caller's back once
    its engine is built. Use :meth:`with_overrides` to derive a variant.
    """

    url: str

    # ---- pool ----
    pool_min_size: int = 5
    pool_max_size: int = 20
    pool_timeout: int = 30
    pool_recycle: int = 1800
    pool_pre_ping: bool = True

    # ---- retries (transient faults only) ----
    retry_attempts: int = 3
    retry_base_delay: float = 0.5
    retry_max_delay: float = 5.0

    # ---- logging ----
    echo: bool = False
    log_queries: bool = False
    slow_query_ms: int = 500

    # ---- timeouts ----
    connect_timeout: int = 10
    statement_timeout_ms: int = 30000
    application_name: str = "forward_test"

    #: Which YAML profile produced this config; for diagnostics only.
    profile: str = "default"

    _source: Mapping[str, str] = field(default_factory=dict, repr=False, compare=False)

    # -- derived -----------------------------------------------------------

    @property
    def dialect(self) -> str:
        """Backend name: ``postgresql``, ``sqlite``, ...

        Taken from the URL scheme, ignoring any ``+driver`` suffix.
        """
        scheme = urlsplit(self.url).scheme
        return scheme.split("+", 1)[0].lower()

    @property
    def is_sqlite(self) -> bool:
        return self.dialect == "sqlite"

    @property
    def is_postgres(self) -> bool:
        return self.dialect in {"postgresql", "postgres"}

    @property
    def is_memory_sqlite(self) -> bool:
        """True for an in-memory SQLite database, which needs special pooling."""
        return self.is_sqlite and (":memory:" in self.url or self.url.endswith("sqlite://"))

    @property
    def max_overflow(self) -> int:
        """Connections allowed beyond ``pool_min_size``, up to ``pool_max_size``."""
        return max(0, self.pool_max_size - self.pool_min_size)

    @property
    def safe_url(self) -> str:
        """The URL with any password replaced by ``***``.

        Always use this in logs and error messages.
        """
        parts = urlsplit(self.url)
        if not parts.password:
            return self.url
        userinfo = parts.username or ""
        if parts.password:
            userinfo += ":***"
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        netloc = f"{userinfo}@{host}" if userinfo else host
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))

    # -- helpers -----------------------------------------------------------

    def with_overrides(self, **changes: Any) -> "DatabaseConfig":
        """Return a copy with ``changes`` applied and re-validated."""
        return replace(self, **changes).validated()

    def validated(self) -> "DatabaseConfig":
        """Check internal consistency, raising :class:`ConfigError` on problems."""
        if not self.url or not str(self.url).strip():
            raise ConfigError(
                "No database URL configured. Set FORWARD_TEST_DB_URL, e.g.\n"
                "  export FORWARD_TEST_DB_URL="
                "postgresql+psycopg2://user:pass@localhost:5432/forward_test\n"
                "or, for local development:\n"
                "  export FORWARD_TEST_DB_URL=sqlite:///forward_test.db"
            )
        if not urlsplit(self.url).scheme:
            raise ConfigError(
                f"malformed database URL {self.safe_url!r}: missing scheme "
                "(expected something like 'postgresql+psycopg2://...')"
            )
        if self.pool_min_size < 1:
            raise ConfigError(f"pool_min_size must be >= 1, got {self.pool_min_size}")
        if self.pool_max_size < self.pool_min_size:
            raise ConfigError(
                f"pool_max_size ({self.pool_max_size}) must be >= "
                f"pool_min_size ({self.pool_min_size})"
            )
        if self.retry_attempts < 1:
            raise ConfigError(f"retry_attempts must be >= 1, got {self.retry_attempts}")
        if self.retry_base_delay < 0 or self.retry_max_delay < 0:
            raise ConfigError("retry delays must be non-negative")
        if self.retry_max_delay < self.retry_base_delay:
            raise ConfigError(
                f"retry_max_delay ({self.retry_max_delay}) must be >= "
                f"retry_base_delay ({self.retry_base_delay})"
            )
        for name in ("pool_timeout", "pool_recycle", "connect_timeout", "slow_query_ms"):
            if getattr(self, name) < 0:
                raise ConfigError(f"{name} must be non-negative, got {getattr(self, name)}")
        return self

    def describe(self) -> dict[str, Any]:
        """A log-safe summary. Never contains the password."""
        return {
            "url": self.safe_url,
            "dialect": self.dialect,
            "profile": self.profile,
            "pool_min_size": self.pool_min_size,
            "pool_max_size": self.pool_max_size,
            "pool_pre_ping": self.pool_pre_ping,
            "retry_attempts": self.retry_attempts,
            "log_queries": self.log_queries,
            "slow_query_ms": self.slow_query_ms,
        }


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------

#: field name -> parser, for every key readable from YAML or the environment.
_FIELD_PARSERS: dict[str, Any] = {
    "url": lambda v, n: str(v),
    "pool_min_size": _as_int,
    "pool_max_size": _as_int,
    "pool_timeout": _as_int,
    "pool_recycle": _as_int,
    "pool_pre_ping": lambda v, n: _as_bool(v),
    "retry_attempts": _as_int,
    "retry_base_delay": _as_float,
    "retry_max_delay": _as_float,
    "echo": lambda v, n: _as_bool(v),
    "log_queries": lambda v, n: _as_bool(v),
    "slow_query_ms": _as_int,
    "connect_timeout": _as_int,
    "statement_timeout_ms": _as_int,
    "application_name": lambda v, n: str(v),
}


def _read_yaml(path: Path) -> dict[str, Any]:
    """Parse a YAML config file, tolerating its absence."""
    if not path.exists():
        return {}
    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on env
        raise ConfigError(
            f"{path} exists but PyYAML is not installed. "
            "Run: pip install pyyaml  (or delete the file to use env vars only)"
        ) from exc

    try:
        loaded = yaml.safe_load(path.read_text()) or {}
    except Exception as exc:
        raise ConfigError(f"could not parse {path}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    return loaded


def _collect_env() -> dict[str, Any]:
    """Read every recognised ``FORWARD_TEST_DB_*`` variable."""
    found: dict[str, Any] = {}
    for name in _FIELD_PARSERS:
        raw = os.getenv(f"{ENV_PREFIX}_{name.upper()}")
        if raw is not None and raw != "":
            found[name] = raw
    return found


def load_config(
    path: str | Path | None = None,
    profile: str | None = None,
    **overrides: Any,
) -> DatabaseConfig:
    """Build a validated :class:`DatabaseConfig`.

    Parameters
    ----------
    path:
        YAML file to read. Defaults to ``$FORWARD_TEST_DB_CONFIG`` if set,
        otherwise ``config/database.yaml``. A missing file is not an error —
        environment variables alone are enough.
    profile:
        Profile to activate. Defaults to ``$FORWARD_TEST_DB_PROFILE``, then
        the file's ``active_profile``, then ``development``.
    **overrides:
        Highest-precedence values, e.g. ``load_config(url="sqlite://")``.

    Raises
    ------
    ConfigError
        If the file is malformed, the profile is unknown, or the resulting
        settings are invalid (including a missing URL).

    Examples
    --------
    >>> cfg = load_config(url="sqlite:///:memory:")   # doctest: +SKIP
    >>> cfg.dialect                                    # doctest: +SKIP
    'sqlite'
    """
    if path is None:
        env_path = os.getenv(f"{ENV_PREFIX}_CONFIG")
        config_path = Path(env_path) if env_path else DEFAULT_CONFIG_PATH
    else:
        config_path = Path(path)

    if path is not None and not config_path.exists():
        # An explicitly named file that does not exist is a mistake worth
        # reporting; the implicit default being absent is fine.
        raise ConfigError(f"config file not found: {config_path}")

    document = _read_yaml(config_path)
    profiles = document.get("profiles") or {}
    if not isinstance(profiles, dict):
        raise ConfigError(f"'profiles' in {config_path} must be a mapping")

    chosen = (
        profile
        or os.getenv(f"{ENV_PREFIX}_PROFILE")
        or document.get("active_profile")
        or "development"
    )
    if profiles and chosen not in profiles:
        raise ConfigError(
            f"unknown profile {chosen!r} in {config_path}. "
            f"Available: {sorted(profiles)}"
        )

    # Layer the sources, least specific first, tracking provenance so
    # misconfiguration is easy to debug.
    merged: dict[str, Any] = {}
    source: dict[str, str] = {}

    def layer(values: Mapping[str, Any], origin: str) -> None:
        for key, value in (values or {}).items():
            if key not in _FIELD_PARSERS:
                continue  # ignore unknown keys so the YAML can carry comments/extras
            merged[key] = value
            source[key] = origin

    layer(document.get("default") or {}, f"{config_path.name}:default")
    layer(profiles.get(chosen) or {}, f"{config_path.name}:{chosen}")
    layer(_collect_env(), "env")
    layer({k: v for k, v in overrides.items() if v is not None}, "argument")

    unknown = set(overrides) - set(_FIELD_PARSERS) - {"profile"}
    if unknown:
        raise ConfigError(f"unknown configuration keys: {sorted(unknown)}")

    parsed: dict[str, Any] = {}
    for key, raw in merged.items():
        parsed[key] = _FIELD_PARSERS[key](raw, key)

    if "url" not in parsed:
        raise ConfigError(
            f"no database URL for profile {chosen!r}.\n"
            f"Set it in the environment:\n"
            f"  export {ENV_PREFIX}_URL=postgresql+psycopg2://user:pass@host:5432/forward_test\n"
            f"or add a 'url:' key under profiles.{chosen} in {config_path}."
        )

    return DatabaseConfig(profile=chosen, _source=source, **parsed).validated()

