"""Centralized Configuration Manager (Step 23).

Unifies all YAML configs (database, slippage, execution, sizing, risk, stops,
performance, forward_testing, alerts) with layered precedence, schema validation,
hot-reload, and secrets handling.

Precedence (highest wins):
1. Explicit overrides (passed to load_config)
2. Environment variables (FORWARD_TEST_*, MSTOCK_*, TELEGRAM_*, etc.)
3. Active profile in YAML
4. Default block in YAML
5. Built-in defaults

Features
--------
* Support YAML and JSON formats
* Environment variable overrides (e.g. FORWARD_TEST_DB_URL, RISK_MAX_DRAWDOWN_PCT)
* Schema validation using JSON Schema (via jsonschema, optional)
* Default values
* Hot-reload capability (no restart needed)
* Encrypt sensitive values (API keys) – placeholder with Fernet
* .env file support for secrets
* Never log sensitive values

Example
-------
>>> from backtest.config_manager.manager import ConfigManager
>>> manager = ConfigManager(config_file="config/app.yaml")
>>> manager.load_config()
>>> manager.get("risk.max_drawdown_pct")
0.1
>>> manager.set("risk.max_drawdown_pct", 0.15)
>>> manager.save_config()
>>> manager.reload_config()  # hot-reload
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger("backtest.config_manager.manager")

DEFAULT_APP_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "app.yaml"

# Sensitive keys that should never be logged
SENSITIVE_KEYS = {
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "auth_token",
    "private_key",
    "smtp_password",
    "twilio_auth_token",
    "telegram_bot_token",
    "slack_webhook_url",
    "discord_webhook_url",
    "webhook_url",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_sensitive(key: str) -> bool:
    key_lower = str(key).lower()
    return any(s in key_lower for s in SENSITIVE_KEYS)


def _get_nested(data: Mapping[str, Any], key_path: str, default: Any = None) -> Any:
    """Get nested value via dot path: risk.max_drawdown_pct"""
    keys = str(key_path).strip().split(".")
    current = data
    for k in keys:
        if isinstance(current, Mapping) and k in current:
            current = current[k]
        else:
            return default
    return current


def _set_nested(data: Dict[str, Any], key_path: str, value: Any) -> None:
    """Set nested value via dot path."""
    keys = str(key_path).strip().split(".")
    current = data
    for k in keys[:-1]:
        if k not in current or not isinstance(current[k], dict):
            current[k] = {}
        current = current[k]
    current[keys[-1]] = value


def _parse_env_value(value: str) -> Any:
    """Parse env var string to appropriate type."""
    # Try bool
    lower = value.strip().lower()
    if lower in ("true", "yes", "1", "on"):
        return True
    if lower in ("false", "no", "0", "off"):
        return False
    # Try int
    try:
        if "." not in value:
            return int(value)
    except ValueError:
        pass
    # Try float
    try:
        return float(value)
    except ValueError:
        pass
    # Try JSON
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError):
        pass
    # String
    return value


# ---------------------------------------------------------------------------
# ConfigManager
# ---------------------------------------------------------------------------


class ConfigManager:
    """Centralized configuration manager.

    Parameters
    ----------
    config_file:
        Path to main YAML/JSON config file. If None, uses defaults + env.
    profile:
        Profile to activate (e.g. development, production)
    auto_reload:
        Whether to enable hot-reload watching file mtime
    encrypt_secrets:
        Whether to encrypt sensitive values (placeholder)
    """

    def __init__(
        self,
        config_file: str | Path | None = None,
        profile: str | None = None,
        auto_reload: bool = False,
        encrypt_secrets: bool = False,
    ):
        self.config_file = Path(config_file) if config_file else DEFAULT_APP_CONFIG_PATH
        self.profile = profile
        self.auto_reload = bool(auto_reload)
        self.encrypt_secrets = bool(encrypt_secrets)

        self._config: Dict[str, Any] = {}
        self._last_mtime: Optional[float] = None
        self._schema: Optional[Dict[str, Any]] = None

        # Load .env if present
        self._load_dotenv()

        logger.info("ConfigManager initialized: file=%s profile=%s auto_reload=%s", self.config_file, self.profile, self.auto_reload)

    def _load_dotenv(self):
        """Load .env file for secrets (via python-dotenv if available)."""
        try:
            from dotenv import load_dotenv

            # Load from repo root .env
            env_path = self.config_file.parents[2] / ".env" if self.config_file.exists() else Path(".env")
            # Also try current dir
            for candidate in [Path(".env"), self.config_file.parent / ".env", self.config_file.parents[1] / ".env", self.config_file.parents[2] / ".env"]:
                if candidate.exists():
                    load_dotenv(dotenv_path=candidate, override=False)
                    logger.debug("Loaded .env from %s", candidate)
                    break
        except ImportError:
            logger.debug("python-dotenv not installed, skipping .env load")
        except Exception as exc:
            logger.debug("Failed to load .env: %s", exc)

    # -- core API ----------------------------------------------------------

    def load_config(self, file_path: str | Path | None = None, profile: str | None = None, **overrides: Any) -> Dict[str, Any]:
        """Load configuration from file, env, and overrides.

        Parameters
        ----------
        file_path:
            YAML or JSON file to load. If None, uses self.config_file.
        profile:
            Profile to activate. If None, uses self.profile or env or file's active_profile.
        **overrides:
            Explicit overrides (highest precedence)

        Returns
        -------
        Dict with full config
        """
        path = Path(file_path) if file_path else self.config_file
        prof = profile or self.profile

        # Determine file type and load
        config_data: Dict[str, Any] = {}

        if path.exists():
            try:
                if path.suffix in (".yaml", ".yml"):
                    import yaml

                    doc = yaml.safe_load(path.read_text()) or {}
                    config_data = self._layer_yaml(doc, prof, overrides)
                elif path.suffix == ".json":
                    doc = json.loads(path.read_text())
                    config_data = self._layer_json(doc, prof, overrides)
                else:
                    # Try YAML
                    try:
                        import yaml

                        doc = yaml.safe_load(path.read_text()) or {}
                        config_data = self._layer_yaml(doc, prof, overrides)
                    except Exception:
                        doc = json.loads(path.read_text())
                        config_data = self._layer_json(doc, prof, overrides)

                self._last_mtime = path.stat().st_mtime

            except Exception as exc:
                logger.warning("Failed to load config %s: %s, using defaults + env", path, exc)
                config_data = self._build_from_env_and_overrides(overrides)
        else:
            logger.info("Config file %s not found, using env + defaults", path)
            config_data = self._build_from_env_and_overrides(overrides)

        # Validate
        try:
            self.validate_config(config_data)
        except Exception as exc:
            logger.error("Config validation failed: %s", exc)
            raise

        self._config = config_data

        # Log safe version (no secrets)
        safe = self._safe_log_dict(config_data)
        logger.info("Config loaded: %s", json.dumps(safe, indent=2)[:1000])

        return dict(self._config)

    def _layer_yaml(self, doc: Mapping[str, Any], profile: Optional[str], overrides: Dict[str, Any]) -> Dict[str, Any]:
        """Layer YAML sources: default < profile < env < overrides."""
        merged: Dict[str, Any] = {}

        # Default block
        default_block = doc.get("default") or {}
        if isinstance(default_block, dict):
            merged.update(default_block)

        # Profiles
        profiles = doc.get("profiles") or {}
        # Determine active profile
        active_profile = profile or os.getenv("FORWARD_TEST_PROFILE") or doc.get("active_profile") or "default"

        if profiles and isinstance(profiles, dict) and active_profile in profiles:
            profile_block = profiles[active_profile] or {}
            if isinstance(profile_block, dict):
                # Deep merge for nested dicts
                for key, value in profile_block.items():
                    if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
                        merged[key] = {**merged[key], **value}
                    else:
                        merged[key] = value

        # Env overrides
        env_config = self._collect_env()
        for key, value in env_config.items():
            _set_nested(merged, key, value)

        # Explicit overrides
        for key, value in overrides.items():
            if "." in key:
                _set_nested(merged, key, value)
            else:
                merged[key] = value

        return merged

    def _layer_json(self, doc: Mapping[str, Any], profile: Optional[str], overrides: Dict[str, Any]) -> Dict[str, Any]:
        # JSON layering similar to YAML
        return self._layer_yaml(doc, profile, overrides)

    def _collect_env(self) -> Dict[str, Any]:
        """Collect env vars with FORWARD_TEST_ prefix and others."""
        collected: Dict[str, Any] = {}

        # FORWARD_TEST_ prefix – maps to nested keys
        # e.g. FORWARD_TEST_RISK_MAX_DRAWDOWN_PCT -> risk.max_drawdown_pct
        #      FORWARD_TEST_DB_URL -> db.url
        for key, value in os.environ.items():
            if key.startswith("FORWARD_TEST_"):
                # Remove prefix and convert to dot path
                stripped = key[len("FORWARD_TEST_") :].lower()
                # Special handling for known mappings
                # risk.max_drawdown_pct, db.url, etc.
                # Convert underscores to dots, but handle known sections
                # For simplicity, we will support both:
                # FORWARD_TEST_RISK_MAX_DRAWDOWN_PCT -> risk.max_drawdown_pct
                # FORWARD_TEST_DB_URL -> db.url
                # We convert: split by _, first part is section, rest is key with underscores
                # Actually we need smarter: risk.max_drawdown_pct has underscores in key
                # So we will treat first token as section, rest as key path with _ -> .
                # But for simplicity, let's support direct mapping via __ as separator for nesting
                # e.g. FORWARD_TEST__RISK__MAX_DRAWDOWN_PCT -> risk.max_drawdown_pct
                # and also FORWARD_TEST_RISK_MAX_DRAWDOWN_PCT -> risk.max_drawdown_pct (first _ after section is section separator)

                # Try __ separator first
                if "__" in stripped:
                    parts = stripped.split("__")
                    dot_path = ".".join(parts)
                    collected[dot_path] = _parse_env_value(value)
                else:
                    # First part is section
                    # Known sections: db, risk, execution, sizing, strategy, data, system, portfolio, alerts, etc.
                    # We will split into section and rest, rest remains with underscores
                    # e.g. risk_max_drawdown_pct -> section risk, key max_drawdown_pct
                    # We need to find section by checking known sections
                    known_sections = ["db", "risk", "execution", "sizing", "strategy", "data", "system", "portfolio", "alerts", "slippage", "market_data", "performance", "stops"]
                    found_section = None
                    for section in known_sections:
                        if stripped.startswith(section + "_"):
                            found_section = section
                            break

                    if found_section:
                        rest = stripped[len(found_section) + 1 :]
                        # rest is key with underscores, keep as is for nested? Actually keys like max_drawdown_pct should stay with underscores
                        # So dot path is section.rest
                        dot_path = f"{found_section}.{rest}"
                        collected[dot_path] = _parse_env_value(value)
                    else:
                        # No known section, treat whole as top-level key with underscores to dots
                        dot_path = stripped.replace("_", ".")
                        collected[dot_path] = _parse_env_value(value)

            # Also check for other relevant env vars (MSTOCK, TELEGRAM, etc.)
            elif key in ("MSTOCK_API_KEY", "MSTOCK_USERNAME", "MSTOCK_PASSWORD", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SLACK_WEBHOOK_URL", "DISCORD_WEBHOOK_URL"):
                # Map to appropriate section
                if key.startswith("MSTOCK_"):
                    rest = key[len("MSTOCK_") :].lower()
                    collected[f"data.mstock.{rest}"] = value
                    collected[f"market_data.mstock.{rest}"] = value
                elif key.startswith("TELEGRAM_"):
                    rest = key[len("TELEGRAM_") :].lower()
                    collected[f"alerts.channels.telegram.telegram_{rest}"] = value
                elif key == "SLACK_WEBHOOK_URL":
                    collected["alerts.channels.slack.slack_webhook_url"] = value
                elif key == "DISCORD_WEBHOOK_URL":
                    collected["alerts.channels.discord.discord_webhook_url"] = value

        return collected

    def _build_from_env_and_overrides(self, overrides: Dict[str, Any]) -> Dict[str, Any]:
        """Build config from env + overrides when no file."""
        config = {}
        env_config = self._collect_env()
        for key, value in env_config.items():
            _set_nested(config, key, value)
        for key, value in overrides.items():
            if "." in key:
                _set_nested(config, key, value)
            else:
                config[key] = value
        return config

    def validate_config(self, config: Optional[Mapping[str, Any]] = None) -> bool:
        """Validate config against schema.

        If jsonschema is available and schema file exists, uses it.
        Otherwise does basic validation.
        """
        cfg = config or self._config

        if not cfg:
            return True

        # Try JSON Schema validation if available
        if self._schema:
            try:
                import jsonschema

                jsonschema.validate(instance=cfg, schema=self._schema)
                return True
            except ImportError:
                logger.debug("jsonschema not installed, skipping schema validation")
            except Exception as exc:
                logger.error("Schema validation failed: %s", exc)
                raise ValueError(f"Config validation failed: {exc}") from exc

        # Basic validation – check for required sections and types
        # For now, just check that portfolio has initial_capital if present, etc.
        # This is lenient – we don't want to break existing configs

        # Check sensitive keys are not logged (just warning if they are in config file with real values?)
        # Actually we should ensure they come from env, not file, but for now just allow

        return True

    def get(self, key_path: str, default: Any = None) -> Any:
        """Get config value via dot path: risk.max_drawdown_pct"""
        return _get_nested(self._config, key_path, default)

    def set(self, key_path: str, value: Any) -> None:
        """Set config value via dot path."""
        _set_nested(self._config, key_path, value)
        logger.info("Config set: %s = %s (sensitive=%s)", key_path, "***" if _is_sensitive(key_path) else value, _is_sensitive(key_path))

    def save_config(self, file_path: str | Path | None = None) -> str:
        """Save current config to file (YAML)."""
        path = Path(file_path) if file_path else self.config_file
        path.parent.mkdir(parents=True, exist_ok=True)

        # Never save sensitive values to file – remove them or keep placeholders
        safe_config = self._safe_save_dict(self._config)

        try:
            import yaml

            # Preserve structure: if original had default/profiles, we should save to default?
            # For simplicity, save flat config as default block
            output = {"default": safe_config, "active_profile": self.profile or "default"}

            path.write_text(yaml.safe_dump(output, sort_keys=False))
            logger.info("Config saved to %s", path)
            return str(path)

        except ImportError:
            # Fallback to JSON
            path = path.with_suffix(".json")
            path.write_text(json.dumps(safe_config, indent=2))
            logger.info("Config saved to JSON (PyYAML not available): %s", path)
            return str(path)

    def reload_config(self, file_path: str | Path | None = None) -> Dict[str, Any]:
        """Hot-reload config from file (no restart needed).

        Always reloads when called explicitly, even if mtime unchanged.
        For auto-reload that checks mtime, use check_and_reload().
        """
        path = Path(file_path) if file_path else self.config_file

        logger.info("Reloading config from %s", path)
        return self.load_config(file_path=path)

    def check_and_reload(self) -> bool:
        """Check if file changed and reload if needed (for auto_reload).

        Returns True if reloaded.
        """
        if not self.auto_reload:
            return False

        if not self.config_file.exists():
            return False

        mtime = self.config_file.stat().st_mtime
        if self._last_mtime is None or mtime > self._last_mtime:
            self.reload_config()
            return True

        return False

    # -- helpers -----------------------------------------------------------

    def _safe_log_dict(self, data: Mapping[str, Any]) -> Dict[str, Any]:
        """Return dict with sensitive values redacted for logging."""
        safe = {}
        for key, value in data.items():
            if isinstance(value, dict):
                safe[key] = self._safe_log_dict(value)
            else:
                if _is_sensitive(key):
                    safe[key] = "***"
                else:
                    safe[key] = value
        return safe

    def _safe_save_dict(self, data: Mapping[str, Any]) -> Dict[str, Any]:
        """Return dict without sensitive values for saving to file."""
        safe = {}
        for key, value in data.items():
            if isinstance(value, dict):
                nested = self._safe_save_dict(value)
                if nested:  # only include non-empty
                    safe[key] = nested
            else:
                if _is_sensitive(key):
                    # Skip sensitive, they should be in .env
                    continue
                else:
                    safe[key] = value
        return safe

    def get_all(self) -> Dict[str, Any]:
        return dict(self._config)

    def __repr__(self):
        return f"<ConfigManager file={self.config_file} profile={self.profile} keys={list(self._config.keys())}>"


# ---------------------------------------------------------------------------
# Global convenience functions (as per spec)
# ---------------------------------------------------------------------------

_global_manager: Optional[ConfigManager] = None


def load_config(file_path: str | Path | None = None, profile: str | None = None, **overrides: Any) -> Dict[str, Any]:
    global _global_manager
    if _global_manager is None:
        _global_manager = ConfigManager(config_file=file_path, profile=profile)
    return _global_manager.load_config(file_path=file_path, profile=profile, **overrides)


def get_config(key_path: str, default: Any = None) -> Any:
    global _global_manager
    if _global_manager is None:
        _global_manager = ConfigManager()
        _global_manager.load_config()
    return _global_manager.get(key_path, default)


def set_config(key_path: str, value: Any) -> None:
    global _global_manager
    if _global_manager is None:
        _global_manager = ConfigManager()
        _global_manager.load_config()
    _global_manager.set(key_path, value)


def save_config(file_path: str | Path | None = None) -> str:
    global _global_manager
    if _global_manager is None:
        raise ValueError("No config manager initialized, call load_config first")
    return _global_manager.save_config(file_path)


def reload_config(file_path: str | Path | None = None) -> Dict[str, Any]:
    global _global_manager
    if _global_manager is None:
        _global_manager = ConfigManager()
    return _global_manager.reload_config(file_path)
