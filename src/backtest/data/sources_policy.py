"""Which market-data sources this deployment is allowed to run on.

Backed by ``config/data_sources.yaml``; see that file for the full rationale.
The short version: a run on synthetic candles can return a robustness score of
8/10 and read as certified, on prices that were never traded.

    from backtest.data.sources_policy import source_policy
    source_policy().is_enabled("db")     # -> True
    source_policy().is_enabled("synthetic")  # -> False
    source_policy().describe()           # -> [{name, enabled, label, certifiable}]

**This is a policy layer, not a removal.** ``SyntheticSource`` still exists and
still generates candles; the CLI flag still parses; the test fixtures still
build synthetic series on purpose. What changed is that the *app* will not
serve a backtest or an optimization on a disabled source, and will say why
instead of returning results that look real.

Since 2026-09-30 the policy is enforced at **every** source decision, not just
the run endpoints: :func:`require_enabled` guards
:attr:`backtest.data.source_registry.SourceRegistry` and the forward runner's
feed routing, and :func:`synthetic_enabled` is what an option runner asks
before it is allowed to substitute a Black-Scholes chain for the broker's.
With synthetic off there is no fallback path left — the runner refuses to
start, it does not trade a guess.

A missing or broken config is not fatal — it falls back to
:data:`FALLBACK_SOURCES`, which is the conservative choice: synthetic off, real
sources on. A typo in a YAML file should not be the thing that quietly re-enables
generated data.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Mapping

from backtest.data.source_tags import APP_SOURCE_TAGS

logger = logging.getLogger("backtest")

__all__ = [
    "SourceSpec",
    "SourcePolicy",
    "SourceDisabledError",
    "source_policy",
    "require_enabled",
    "require_synthetic",
    "synthetic_enabled",
    "default_backtest_source",
    "default_broker_source",
    "default_source_tag",
    "synthetic_chain_generator",
    "policy_key",
    "SOURCE_KEY_ALIASES",
    "POLICY_VOCABULARY",
    "CHAIN_SOURCES",
    "reset_source_policy",
    "FALLBACK_SOURCES",
    "PROFILE_ENV",
]

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "data_sources.yaml"
PROFILE_ENV = "BACKTEST_DATA_PROFILE"

#: Used when the file is missing, unreadable, or does not describe a source.
#: Synthetic is off here too: a broken file must not be the thing that quietly
#: re-enables generated data.
FALLBACK_SOURCES: dict[str, dict[str, Any]] = {
    "synthetic": {"enabled": False, "label": "Synthetic (random walk)", "certifiable": False},
    "db": {"enabled": True, "label": "Real Data (PostgreSQL)", "certifiable": True},
    "mstock": {"enabled": True, "label": "Broker (mstock)", "certifiable": True},
    "dhan": {"enabled": True, "label": "Broker (dhan)", "certifiable": True},
    "csv": {"enabled": True, "label": "CSV files", "certifiable": False},
}


#: Sources that can carry a real *option chain* (not just candles) into a
#: backtest. Deliberately empty as of 2026-09-30: the DB holds bars, the broker
#: holds live chains, and neither is historical option-chain storage. Backtests
#: and compare-backtests are therefore equity-only, which is the operator's
#: stated rule — options belong to forward testing and the portfolio, where the
#: chain comes from mStock live.
CHAIN_SOURCES: tuple[str, ...] = ()

#: Runner/feed source name -> the key that describes it in this policy.
#:
#: The app speaks two vocabularies and they are not the same size. A runner's
#: ``config.source`` is a *feed* name (``mstock``, ``dhan``, ``replay``,
#: ``synthetic``); this file keys on *data sources* (``db``, ``mstock``,
#: ``synthetic``, ``csv``). ``replay`` means "historical DB bars replayed at a
#: clock", so it is ``db`` as far as provenance goes; ``dhan`` is a second
#: broker feed and gets its own row. Without this map, checking a runner's
#: source against the policy would report ``replay``/``dhan`` as unknown and
#: refuse a perfectly real broker runner — a false alarm that would get the
#: control switched back off again.
SOURCE_KEY_ALIASES: dict[str, str] = {
    "synthetic": "synthetic",
    "generated": "synthetic",
    "replay": "db",
    "historical": "db",
    "db": "db",
    "postgres": "db",
    "csv": "csv",
    "mstock": "mstock",
    "dhan": "dhan",
}

#: Every source name the policy can meaningfully describe. The vocabulary the
#: config file is cross-checked against (:func:`_warn_about_vocabulary`) — a
#: row here that no profile describes is a silent hole in the control.
POLICY_VOCABULARY: frozenset[str] = frozenset(SOURCE_KEY_ALIASES.values())


def policy_key(name: str) -> str | None:
    """The policy key for a source/feed name, or ``None`` if unrecognized."""
    raw = str(name or "").strip().lower()
    return SOURCE_KEY_ALIASES.get(raw)


class SourceDisabledError(ValueError):
    """A run/runner asked for a source this deployment disables.

    ``ValueError`` on purpose: the API layer already maps that onto a 4xx with
    the message shown verbatim, so the operator reads *which* source is off and
    *how* to enable it instead of getting a stack trace.
    """


class SourceSpec:
    """One row of the policy: is it allowed, and what does it mean."""

    __slots__ = ("name", "enabled", "label", "certifiable", "note")

    def __init__(
        self,
        name: str,
        enabled: bool = False,
        label: str = "",
        certifiable: bool = False,
        note: str = "",
    ) -> None:
        self.name = name
        self.enabled = bool(enabled)
        self.label = label or name
        self.certifiable = bool(certifiable)
        self.note = note

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "label": self.label,
            "certifiable": self.certifiable,
            "note": self.note,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<SourceSpec {self.name} enabled={self.enabled}>"


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in (over or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        logger.warning("[data-policy] %s not found — using built-in defaults", path)
        return {}
    except Exception:  # noqa: BLE001 — a broken file must not break startup
        logger.warning(
            "[data-policy] could not read %s — using built-in defaults", path, exc_info=True
        )
        return {}
    return data if isinstance(data, dict) else {}


class SourcePolicy:
    """Resolved policy for this process."""

    def __init__(self, sources: Mapping[str, SourceSpec], profile: str = "default") -> None:
        self._sources: dict[str, SourceSpec] = dict(sources)
        self.profile = profile

    # -- queries -----------------------------------------------------------
    def is_enabled(self, name: str) -> bool:
        spec = self._sources.get(str(name or "").strip().lower())
        return bool(spec and spec.enabled)

    def option_backtest_allowed(self) -> bool:
        """May an options chain be priced at all in a backtest?

        PRD 2026-09-30: the deployment's data sources are **DB candles only** —
        ``market_data_cache`` holds no historical option chains, so a backtested
        option P&L can only ever be Black-Scholes off a generated chain. The
        option paths therefore require a source that is *both* enabled and
        explicitly allowed to carry chains, and no deployment today satisfies
        that. When a real chain source is added to the vocabulary, list its name
        in ``CHAIN_SOURCES`` and enable it; the backtest gate opens with it.
        """
        return any(self.is_enabled(name) for name in CHAIN_SOURCES)

    def is_certifiable(self, name: str) -> bool:
        spec = self._sources.get(str(name or "").strip().lower())
        return bool(spec and spec.enabled and spec.certifiable)

    def get(self, name: str) -> SourceSpec | None:
        return self._sources.get(str(name or "").strip().lower())

    def enabled_names(self) -> list[str]:
        return [n for n, s in self._sources.items() if s.enabled]

    def describe(self) -> list[dict[str, Any]]:
        """Rows for the UI. Disabled sources are included, flagged, not hidden —
        a source that silently vanishes is impossible to debug."""
        return [self._sources[n].as_dict() for n in sorted(self._sources)]

    # -- the refusal -------------------------------------------------------
    def refusal_for(self, name: str) -> str | None:
        """None when the source may be used, else a message a user can act on."""
        raw = str(name or "").strip().lower()
        spec = self._sources.get(raw)
        if spec is None:
            return (
                f"Unknown data source '{name}'. Known sources: "
                f"{', '.join(sorted(self._sources)) or 'none'}."
            )
        if not spec.enabled:
            return (
                f"Data source '{spec.label}' is disabled in config/data_sources.yaml. "
                f"{spec.note or 'It is not certification-grade.'} "
                f"Enabled sources: {', '.join(self.enabled_names()) or 'none'}."
            )
        return None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<SourcePolicy profile={self.profile} enabled={self.enabled_names()}>"


_POLICY: SourcePolicy | None = None


def build_policy(
    path: Path | None = None, profile: str | None = None, env: Mapping[str, str] | None = None
) -> SourcePolicy:
    """Resolve the policy from disk. Exposed separately so tests can build one
    from a fixture file without touching the process-wide cache."""
    env = os.environ if env is None else env
    cfg_path = Path(path) if path is not None else _CONFIG_PATH
    data = _load_yaml(cfg_path)

    base = data.get("default")
    base = base if isinstance(base, dict) else {}
    name = profile or env.get(PROFILE_ENV) or data.get("active_profile") or "default"
    named = (data.get("profiles") or {}).get(str(name))
    merged = _deep_merge(base, named if isinstance(named, dict) else {})

    raw_sources = merged.get("sources")
    if not isinstance(raw_sources, dict) or not raw_sources:
        logger.warning("[data-policy] no sources described — using built-in defaults")
        raw_sources = FALLBACK_SOURCES

    specs: dict[str, SourceSpec] = {}
    for key, value in raw_sources.items():
        value = value if isinstance(value, dict) else {"enabled": value}
        fallback = FALLBACK_SOURCES.get(key, {})
        specs[str(key)] = SourceSpec(
            name=str(key),
            enabled=value.get("enabled", fallback.get("enabled", False)),
            label=value.get("label", fallback.get("label", str(key))),
            certifiable=value.get("certifiable", fallback.get("certifiable", False)),
            note=str(value.get("note", "") or "").strip(),
        )
    _warn_about_vocabulary(specs)
    return SourcePolicy(specs, profile=str(name))


def _warn_about_vocabulary(specs: Mapping[str, SourceSpec]) -> None:
    """Cross-check the config against the app's source vocabulary.

    A misspelled key is the failure mode worth catching: ``postgress:
    enabled: true`` reads as real data, matches nothing, and silently leaves
    the real source disabled.
    """
    known = POLICY_VOCABULARY | set(APP_SOURCE_TAGS)
    unknown = sorted(set(specs) - known)
    if unknown:
        logger.warning(
            "[data-policy] %s describes unknown source(s) %s — the app's vocabulary is %s. "
            "They will never match a running source.",
            _CONFIG_PATH.name,
            ", ".join(unknown),
            ", ".join(sorted(known)),
        )
    missing = sorted(known - set(specs))
    if missing:
        # Not fatal: an unlisted source falls back to FALLBACK_SOURCES, which
        # is the conservative answer. Worth saying out loud, though.
        logger.warning(
            "[data-policy] %s does not describe %s — using built-in defaults for them.",
            _CONFIG_PATH.name,
            ", ".join(missing),
        )


def source_policy() -> SourcePolicy:
    """The process-wide policy, resolved once."""
    global _POLICY
    if _POLICY is None:
        _POLICY = build_policy()
    return _POLICY


def reset_source_policy() -> None:
    """Drop the cache — for tests, and for anything that edits the config."""
    global _POLICY
    _POLICY = None


# -- the enforcement helpers (2026-09-30) ------------------------------------
# The policy used to be consulted only at the backtest/optimize request
# boundary (``api.data_guard``). Every OTHER place that picks a data source —
# the (mode, source) registry, a forward runner's bar feed, an option runner's
# chain provider — still had its own hard-coded ``or "synthetic"`` default or
# its own labelled fallback, so a deployment with synthetic switched OFF could
# still end up pricing and filling on generated candles. These two helpers are
# the single check those call sites share.


def require_enabled(name: str, *, where: str = "") -> str:
    """Return ``name`` (normalised) when the policy allows it.

    Raises :class:`SourceDisabledError` carrying the policy's own refusal
    sentence otherwise. ``where`` names the call site in the message when the
    refusal needs extra context (e.g. "this runner asked for mStock but no
    session is authenticated").

    Feed names are resolved through :data:`SOURCE_KEY_ALIASES` first, so a
    runner saying ``replay`` is judged as the ``db`` source it actually bars
    from. A name with no alias is refused rather than waved through — an
    unrecognised source is exactly the thing this policy exists to catch.
    """
    key = str(name or "").strip().lower()
    resolved = policy_key(key)
    if resolved is None:
        raise SourceDisabledError(
            f"Unknown data source '{name}' — config/data_sources.yaml describes: "
            f"{', '.join(sorted(SOURCE_KEY_ALIASES))}. "
            f"Nothing runs on a source the policy cannot name. [{where}]".strip()
        )
    refusal = source_policy().refusal_for(resolved)
    if refusal is not None:
        raise SourceDisabledError(f"{refusal}{f' [{where}]' if where else ''}")
    return key


def synthetic_enabled() -> bool:
    """Is the synthetic source allowed for this process?

    Named for the one question the fallback sites actually ask — they do not
    branch on db/mstock, they branch on "may I substitute generated data at
    all?". Default deployments say no; ``tests/conftest.py`` (and
    ``BACKTEST_DATA_PROFILE=testing``) say yes.
    """
    return source_policy().is_enabled("synthetic")


def require_synthetic(where: str = "") -> None:
    """Refuse, in words, to substitute generated data at this call site.

    :func:`require_enabled` judges a source the caller has *chosen*; this one
    guards the places that were about to *fall back* to synthetic without
    anyone choosing it (an option runner with no broker session, an equity
    runner whose source string was not ``mstock``). Those sites only ever need
    the yes/no on synthetic, and gating the real sources here instead would
    break the test suite's ability to build a ``DbSource`` for an unreachable
    database — availability is not the same question as legitimacy.

    Raises :class:`SourceDisabledError` (a ``ValueError``) when the policy has
    synthetic off.
    """
    if synthetic_enabled():
        return
    raise SourceDisabledError(
        "Synthetic (generated) data is disabled in config/data_sources.yaml, so there is "
        "no fallback source to use here. Run this on a real source — mstock/dhan for live "
        "and paper, db for backtests — and authenticate the broker first. The test suite "
        "opts back into synthetic through the policy's own 'testing' profile "
        "(BACKTEST_DATA_PROFILE=testing)."
        + (f" [{where}]" if where else "")
    )


# -- "nothing was named" answers, so no call site needs its own literal ------
#
# Every default that used to read ``or "synthetic"`` is replaced by one of
# these two. Both put synthetic FIRST and only when the policy enables it, so
# the test profile keeps its source-less convenience while a deployment with
# the flag off can never be answered with a generated feed. The order of the
# real candidates mirrors ``backtest.web.app.resolve_source`` — db first for
# historical runs, the broker feeds first for trading.

def _first_enabled(candidates: tuple[str, ...]) -> str:
    policy = source_policy()
    for candidate in candidates:
        if policy.is_enabled(candidate):
            return candidate
    return ""


def default_backtest_source() -> str:
    """The source a *historical* run (backtest, compare, optimization, CLI)
    gets when nothing names one — the database, unless the policy says
    generated data is fine."""
    return _first_enabled(("synthetic", "db", "csv"))


def default_broker_source() -> str:
    """The source a *trading* path (runner, forward run, live papertrade) gets
    when nothing names one — the broker feed, never an unnamed synthetic one."""
    return _first_enabled(("synthetic", "mstock", "dhan"))


def default_source_tag() -> str:
    """The taxonomy tag (``synthetic|replay|mstock``) for the default feed.

    For the places that only ever *label* a run — a ledger row, a persisted
    portfolio row, the source dropdown's pre-selection. Those defaults used to
    read the literal ``"synthetic"`` for every run, which mislabelled a real
    mStock book as generated data and left a deployment with synthetic off
    naming a feed it had switched off. ``replay`` is the fallback when the
    policy enables nothing at all: an unrecorded provenance is historical
    data, never a claim about a disabled feed.
    """
    from backtest.data.source_tags import app_source_tag

    return app_source_tag(default_broker_source(), default="replay")


def synthetic_chain_generator(where: str = "") -> Any | None:
    """A generated option-chain feed, or ``None`` when the policy forbids one.

    Both execution engines used to say ``self.chain_generator or
    SyntheticChainGenerator()`` — a constructor that asked the policy nothing,
    so a live-labelled run with no broker session priced itself on
    Black-Scholes strikes and expiries the market never listed. Callers treat
    ``None`` as "there is no chain feed here" and refuse the order; the label
    they carry says why.

    ``where`` only names the call site in the refusal log line.
    """
    if synthetic_enabled():
        from backtest.options.quote_providers import SyntheticChainGenerator

        return SyntheticChainGenerator()
    logger.warning(
        "[data-policy] no chain feed for %s: synthetic is disabled in "
        "config/data_sources.yaml and there is no broker chain to use instead.",
        where or "this run",
    )
    return None
