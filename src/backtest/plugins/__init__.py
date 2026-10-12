"""Strategy plugin discovery — U6.3, decision D7 (no live-paste of strategy code).

Users author strategies as ordinary Python files and drop them into
``plugins/strategies/`` (repo root). At startup :func:`discover_plugins`
scans that folder and, for each module:

1. **Clean import** — the module is imported in isolation; any exception
   (syntax error, missing dependency, anything) → warning + skip.
2. **Conformance** (:func:`conformance_errors`) — a battery of structural and
   behavioural checks; all must pass or the file's strategies are skipped.
3. **Registration** — survivors are handed to the normal registry.

A broken or malicious plugin can therefore never crash the app, never reach
the trading path, and never silently corrupt another strategy's behaviour.

The C2 boundary is enforced here as a hard rule: a plugin file must not import
engine/broker/feed modules (``backtest.brokers``, ``backtest.forward``,
``backtest.options``, ``backtest.data``, ``requests``, ``urllib``,
``websocket``). Strategies receive data from the engine (architecture §5.2);
anything that reaches out for its own data is refused at load time —
re-imported only if the file changes (mtime check), so normal app restarts
are cheap. The conformance test greps plugin sources for these imports and
the loader refuses files that reach into the engine.

Thread-safety: guarded by a module lock; safe to call from multiple requests.
"""

from __future__ import annotations

import ast
import importlib.util
import logging
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

logger = logging.getLogger("backtest.plugins")

# project root = the dir containing src/, plugins/, templates/
# (__init__.py sits at <root>/src/backtest/plugins/ → parents[3] = <root>)
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
PLUGINS_DIR = _PROJECT_ROOT / "plugins" / "strategies"

#: Modules a strategy file may never import (C2: the engine hands data in).
FORBIDDEN_IMPORTS = frozenset(
    {
        "backtest.brokers",
        "backtest.forward",
        "backtest.options",
        "backtest.data",
        "backtest.live",
        "requests",
        "urllib",
        "urllib3",
        "websocket",
        "websockets",
    }
)


class _ForbiddenImportVisitor(ast.NodeVisitor):
    """AST walk that records every import a module performs."""

    def __init__(self) -> None:
        self.imported: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802 (ast API)
        for alias in node.names:
            self.imported.add(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        if node.module:
            self.imported.add(node.module)
        for alias in node.names:  # relative imports: `from . import x`
            if node.level > 0:
                base = node.module or ""
                self.imported.add(f"{base}.{alias.name}".strip("."))


def _check_allowed_imports(source: str) -> Optional[str]:
    """Return a refusal reason when the file imports forbidden modules."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return f"cannot parse imports: {exc}"
    visitor = _ForbiddenImportVisitor()
    visitor.visit(tree)
    for imported in visitor.imported:
        for forbidden in FORBIDDEN_IMPORTS:
            # Exact match or submodule: backtest.brokers AND backtest.brokers.base.
            if imported == forbidden or imported.startswith(forbidden + "."):
                return (
                    f"imports forbidden module '{imported}' — strategies receive "
                    "data from the engine (C2) and must not fetch their own"
                )
    return None


# ---------------------------------------------------------------------------
# Conformance — shared by the loader and tests/test_strategy_conformance.py
# ---------------------------------------------------------------------------


@dataclass
class ConformanceResult:
    """Everything the conformance battery learned about one strategy class."""

    cls: type
    errors: List[str] = field(default_factory=list)
    checks_run: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def conformance_errors(cls: type, *, run_behaviour: bool = True) -> ConformanceResult:
    """Run the full conformance battery against one strategy class.

    Returns every failure reason (empty list = conformant). Used by the
    plugin loader (skip-and-log) and by ``tests/test_strategy_conformance.py``
    (assert-empty) so both enforce exactly the same contract.
    """
    from backtest.strategy.base import Strategy

    res = ConformanceResult(cls=cls)

    def check(label: str, fn: Callable[[], None]) -> None:
        res.checks_run.append(label)
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — any failure is a conformance failure
            res.errors.append(str(exc))

    # 1. identity -----------------------------------------------------------
    def _name_ok() -> None:
        if not isinstance(getattr(cls, "name", ""), str) or not cls.name.strip():
            raise AssertionError(f"{cls.__name__}: missing non-empty 'name'")

    check("unique-non-empty-name", _name_ok)

    # 2. structural contract (validate raises StrategyContractError) --------
    check("strategy-contract", cls.validate)

    # 3. signal_kind declared correctly --------------------------------------
    def _kind_ok() -> None:
        from backtest.strategy.registry import signal_kind

        kind = signal_kind(cls)
        if kind == "option" and cls.generate_market_view is Strategy.generate_market_view:
            raise AssertionError("option kind claimed but generate_market_view not overridden")

    check("signal-kind-consistent", _kind_ok)

    # 4. metadata present ----------------------------------------------------
    def _meta_ok() -> None:
        for attr in ("description", "version", "author"):
            if not str(getattr(cls, attr, "") or "").strip():
                raise AssertionError(f"missing '{attr}' metadata")

    check("metadata-present", _meta_ok)

    # 5. params schema sanity (validate covers schema; check UI-friendliness)
    def _params_ui_ok() -> None:
        schema = cls.param_schema()
        for pname, entry in schema.items():
            if not entry.get("label"):
                raise AssertionError(f"param '{pname}' has no label for the spawn form")

    check("params-ui-labels", _params_ui_ok)

    # 6–8. behavioural (need candles; skipped for abstract/abstract-ish cases)
    if run_behaviour and not getattr(cls, "__abstractmethods__", None):
        candles = _candles()
        kind = None
        try:
            from backtest.strategy.registry import signal_kind

            kind = signal_kind(cls)
        except Exception:  # noqa: BLE001 — already reported by _kind_ok
            pass

        if kind == "option":
            check("option-view-returns", lambda: _check_option_view(cls, candles))
        else:
            check("equity-signals-returns", lambda: _check_equity_signals(cls, candles))
        check("deterministic", lambda: _check_determinism(cls, candles, kind))

    return res


def _candles(n: int = 120) -> "Any":
    """A deterministic OHLCV frame (slow sine drift + noise-free wiggle)."""
    import math

    import pandas as pd

    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    close = [25_000.0 + 800.0 * math.sin(i / 9.0) + 3.0 * i for i in range(n)]
    frame = pd.DataFrame({"close": close}, index=idx)
    frame["open"] = frame["close"].shift(1).fillna(frame["close"])
    frame["high"] = frame[["open", "close"]].max(axis=1) * 1.001
    frame["low"] = frame[["open", "close"]].min(axis=1) * 0.999
    frame["volume"] = 1_000_000.0
    return frame[["open", "high", "low", "close", "volume"]]


def _fresh(cls: type) -> Any:
    """A default-params instance (deterministic — no clock, no randomness)."""
    return cls()


def _check_equity_signals(cls: type, candles: Any) -> None:
    out = _fresh(cls).generate_signals(candles)
    if out is None or len(out) != len(candles):
        raise AssertionError("generate_signals must return a Series aligned to candles")


def _check_option_view(cls: type, candles: Any) -> None:
    view = _fresh(cls).generate_market_view(candles)
    if view is not None and getattr(view, "direction", None) is None:
        raise AssertionError("generate_market_view returned a view without direction")


def _check_determinism(cls: type, candles: Any, kind: Optional[str]) -> None:
    """Same candles twice → identical output (backtest ≡ forward, D-rule)."""
    if kind == "option":
        a = _fresh(cls).generate_market_view(candles)
        b = _fresh(cls).generate_market_view(candles)
    else:
        a = _fresh(cls).generate_signals(candles)
        b = _fresh(cls).generate_signals(candles)

    def _sig(value: Any) -> str:
        # Series → full value dump (a pd.Series has no meaningful __dict__
        # signature); MarketView → its dataclass repr; None → "None".
        if isinstance(value, pd.Series):
            return f"{value.dtype}:{value.tolist()}"
        return repr(
            getattr(value, "__dict__", value)
            if not hasattr(value, "__dataclass_fields__")
            else value
        )

    if _sig(a) != _sig(b):
        raise AssertionError(
            "non-deterministic: same candles produced different outputs — "
            "remove clock/random reads"
        )


# ---------------------------------------------------------------------------
# Discovery + registration
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()
_LAST_SCAN: Dict[str, float] = {}  # file → mtime, for re-import-on-change
_REGISTERED_FROM_PLUGINS: set[str] = set()


def _import_module_from_path(modname: str, path: Path) -> Optional[Any]:
    """Import a file as a top-level module (fresh bytecode, isolated)."""
    spec = importlib.util.spec_from_file_location(modname, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    # Registered in sys.modules so dataclasses / __init_subclass__ machinery
    # inside the module behaves like a normal import.
    sys.modules[modname] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(modname, None)
        raise
    return module


def _vet_plugin_classes(module: Any) -> List[str]:
    """Vet every Strategy subclass defined in ``module``; keep conformant ones.

    ``Strategy.__init_subclass__`` auto-registers a class the moment its
    ``name`` is set — i.e. during ``exec_module``, BEFORE the loader can vet
    it. So the loader's job is the inverse of the built-in flow: run the
    conformance battery on each class the module defines and **pop** the
    failures back out of the registry. Net effect: only conformant plugin
    strategies remain registered, and a broken file can never leave a
    half-valid class behind.
    """
    from backtest.strategy.base import Strategy
    from backtest.strategy.registry import _REGISTRY

    registered: List[str] = []
    for attr_name in sorted(dir(module)):
        if attr_name.startswith("_"):
            continue
        cls = getattr(module, attr_name)
        if not isinstance(cls, type) or not issubclass(cls, Strategy):
            continue
        if cls.__module__ != module.__name__:
            continue  # imported Strategy itself, not a definition here

        name = getattr(cls, "name", "")
        result = conformance_errors(cls)
        if not result.ok:
            logger.warning(
                "plugin %s: strategy %s skipped — failed conformance: %s",
                module.__name__,
                name or "<unnamed>",
                "; ".join(result.errors),
            )
            # Undo the __init_subclass__ auto-registration (a no-op when the
            # name was never registered, e.g. empty name).
            holder = _REGISTRY.get(name)
            if holder is cls:
                _REGISTRY.pop(name, None)
            continue
        if name in _REGISTRY and _REGISTRY[name] is not cls:
            # Two classes claiming one name inside one module: first wins.
            logger.warning(
                "plugin %s: duplicate strategy name %r — keeping the first, " "skipping %s",
                module.__name__,
                name,
                cls.__name__,
            )
            continue
        _REGISTERED_FROM_PLUGINS.add(name)
        registered.append(name)
    return registered


def discover_plugins(force: bool = False) -> List[str]:
    """Scan ``plugins/strategies/``, register conformant strategies, return names.

    - Missing folder → empty list (feature simply inactive).
    - File failing import or conformance → warning + skip, app continues.
    - ``force=False`` re-imports only files whose mtime changed.
    """
    with _LOCK:
        if not PLUGINS_DIR.is_dir():
            logger.debug("plugin dir missing (%s) — no plugins", PLUGINS_DIR)
            return []

        registered: List[str] = []
        for path in sorted(PLUGINS_DIR.glob("*.py")):
            if path.name.startswith("_"):
                continue
            modname = f"_plugin_{path.stem}"
            try:
                mtime = path.stat().st_mtime
                if not force and modname in sys.modules and _LAST_SCAN.get(modname) == mtime:
                    continue  # unchanged since last scan
                source = path.read_text(encoding="utf-8")
                refusal = _check_allowed_imports(source)
                if refusal:
                    logger.warning("plugin %s refused: %s", path.name, refusal)
                    _LAST_SCAN[modname] = mtime  # don't re-spam every scan
                    continue
                module = _import_module_from_path(modname, path)
                _LAST_SCAN[modname] = mtime
                registered.extend(_vet_plugin_classes(module))
            except Exception as exc:  # noqa: BLE001 — a bad plugin never crashes the app
                logger.warning("plugin %s skipped: %s: %s", path.name, exc.__class__.__name__, exc)
                logger.debug("plugin %s import failed", path.name, exc_info=True)
                sys.modules.pop(modname, None)
        return registered


def plugin_strategy_names() -> set[str]:
    """Names currently registered from the plugins folder (for tests/telemetry)."""
    return set(_REGISTERED_FROM_PLUGINS)
