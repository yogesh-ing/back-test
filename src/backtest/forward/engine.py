"""Main Forward Testing Engine (Step 20).

Ties together all forward-testing components into a single orchestration
loop that can run in live paper-trade mode or backtest replay mode.

The engine is the final integration point for Steps 1-19. Some of those
steps (10-12, 15-19) are not yet fully implemented, so this module provides
minimal but functional placeholders that satisfy the Step 20 contract while
allowing incremental replacement as those steps land.

Design
------
* **Configuration-driven.** All settings come from a single YAML file
  (``config/forward_testing.yaml``) plus env overrides. Validation happens
  on startup so a typo fails fast rather than mid-run.
* **Stateful & recoverable.** Full system state (portfolio, bars, adapter
  state, equity curve) is snapshotted to JSON every N minutes and after each
  trade. On restart, ``initialize_system`` restores it.
* **No lookahead.** The loop processes only completed bars. Signals are
  generated from history including the bar that just closed, but orders are
  executed on the next tick — matching the legacy ``shift(1)`` rule.
* **Graceful shutdown.** SIGINT/SIGTERM handlers save state before exit.
* **Dry-run & backtest modes.** Dry-run generates signals but does not trade.
  Backtest mode replays historical data via a ``DataSource``.

Example config (``config/forward_testing.yaml``):

.. code-block:: yaml

    portfolio:
      initial_capital: 100000
      name: "Forward Test 1"
      base_currency: INR

    strategy:
      name: "sma_crossover"
      parameters:
        fast: 20
        slow: 50

    risk:
      max_position_size: 10000
      max_positions: 5
      max_drawdown_pct: 10
      daily_loss_limit_pct: 2

    execution:
      realism: realistic
      slippage_model: "hybrid"
      commission_model: "zerodha"

    sizing:
      method: risk_based
      risk_per_trade: 0.01
      stop_loss_pct: 0.02

    data:
      provider: "mock"
      symbols: ["INFY", "RELIANCE"]
      timeframe: "1min"

    system:
      loop_interval_seconds: 1
      save_state_interval_minutes: 5
      market: "NSE"
      dry_run: false
      backtest_mode: false
      state_file: "state/forward_state.json"

Usage
-----
>>> from backtest.forward.engine import ForwardTestingEngine
>>> engine = ForwardTestingEngine(config_file="config/forward_testing.yaml")
>>> engine.initialize_system()
>>> engine.start()  # blocks until stopped
"""

from __future__ import annotations

import json
import logging
import signal
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

import pandas as pd

from backtest.simulator.engine_loop import Bar, to_python_scalar
from backtest.simulator.errors import ValidationError
from backtest.simulator.money import ZERO, money

logger = logging.getLogger("backtest.forward.engine")

DEFAULT_FORWARD_CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "forward_testing.yaml"
)
DEFAULT_STATE_FILE = Path("state/forward_state.json")

#: State-file format version (tickets F-04 + #7).
#:
#: * **v1** — the implicit pre-F-04 format: no ``state_version`` field, no
#:   ``mode`` / ``source`` / ``engine_id``. Legacy files still load (warned),
#:   and are migrated to v2 semantics on the next save, with mode/source
#:   derived from the engine's actual config (never hardcoded).
#: * **v2** — adds ``state_version``, ``engine_id`` (the portfolio id),
#:   ``mode`` (paper|live) and ``source`` (synthetic|replay|mstock) mirroring
#:   the ``portfolios`` columns added by migration 002 (DB-T1).
#: * **v3** — current: adds **full resume fidelity**. ``executor`` captures
#:   the executor's in-flight bar-clock queue (pending orders + which of them
#:   are already armed) and ``engine_runtime`` captures ``loop_count``, the
#:   last processed bar timestamp per symbol, and per-symbol processed-bar
#:   counts — so a restored engine continues the SAME loop a never-stopped
#:   engine would have (pending fills keep their exact bar timing; a backtest
#:   replay resumes at the next unprocessed bar instead of re-running).
#:
#: v1/v2 files still load (warned): portfolio + adapter state are restored,
#:  but the executor queue and engine runtime are rebuilt empty (v2) — a
#: resume from an old file is progress-safe but may miss an in-flight order
#: that was armed at teardown. That is the documented cost of NOT bumping
#: before T7; new saves are v3.
STATE_VERSION = 3


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class PortfolioConfig:
    initial_capital: Decimal = Decimal("100000")
    name: str = "Forward Test 1"
    base_currency: str = "INR"
    allow_short: bool = False
    max_open_positions: Optional[int] = None

    def __post_init__(self):
        from backtest.simulator.money import to_decimal

        self.initial_capital = money(self.initial_capital)
        if self.initial_capital <= ZERO:
            raise ValidationError("initial_capital must be positive")
        self.name = str(self.name).strip()
        if not self.name:
            raise ValidationError("portfolio name must not be empty")


@dataclass
class StrategyConfig:
    name: str = "sma_crossover"
    parameters: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.name = str(self.name).strip()
        if not self.name:
            raise ValidationError("strategy name required")


@dataclass
class RiskConfig:
    max_position_size: Optional[Decimal] = None
    max_positions: Optional[int] = None
    max_drawdown_pct: Decimal = Decimal("0.10")
    daily_loss_limit_pct: Decimal = Decimal("0.02")
    max_leverage: Decimal = Decimal("1")
    #: Ticket #9 — per-bucket overrides keyed on the run classification
    #: (paper|live). Canonical defaults live in
    #: ``backtest.simulator.bucket_risk.BUCKET_RISK_LIMITS``; this config only
    #: OVERRIDES a bucket's limit (e.g. ``{"live": {"max_position_pct": 0.05}}``).
    buckets: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self):
        from backtest.simulator.money import to_decimal

        if self.max_position_size is not None:
            self.max_position_size = money(self.max_position_size)
        self.max_drawdown_pct = to_decimal(self.max_drawdown_pct, "max_drawdown_pct")
        self.daily_loss_limit_pct = to_decimal(self.daily_loss_limit_pct, "daily_loss_limit_pct")
        self.max_leverage = to_decimal(self.max_leverage, "max_leverage")
        self.buckets = {
            str(bucket).strip().lower(): dict(values or {})
            for bucket, values in (self.buckets or {}).items()
        }


@dataclass
class ExecutionConfigWrapper:
    realism: str = "realistic"
    slippage_model: str = "hybrid"
    commission_model: str = "zerodha"
    segment: str = "equity_delivery"

    def __post_init__(self):
        self.realism = str(self.realism).strip().lower()
        self.slippage_model = str(self.slippage_model).strip()
        self.commission_model = str(self.commission_model).strip()


@dataclass
class SizingConfigWrapper:
    method: str = "fixed_quantity"
    params: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.method = str(self.method).strip().lower()


@dataclass
class DataConfig:
    provider: str = "mock"
    #: Run classification (ticket P1.2) — resolved by
    #: backtest.data.source_registry.SourceRegistry.
    mode: str = "paper"
    source: str = "synthetic"
    replay_speed: float = 5
    symbols: List[str] = field(default_factory=lambda: ["INFY"])
    timeframe: str = "1min"
    start_date: Optional[str] = None
    end_date: Optional[str] = None

    def __post_init__(self):
        self.provider = str(self.provider).strip().lower()
        self.mode = str(self.mode).strip().lower()
        self.source = str(self.source).strip().lower()
        self.replay_speed = float(self.replay_speed)
        if self.replay_speed <= 0:
            raise ValidationError("replay_speed must be > 0")
        if self.mode not in {"backtest", "paper", "live"}:
            raise ValidationError(f"unknown data mode: {self.mode!r}")
        if self.mode == "paper" and self.source not in {"synthetic", "mstock"}:
            raise ValidationError(
                f"paper mode needs source 'synthetic' or 'mstock', got {self.source!r}"
            )
        self.symbols = [str(s).strip().upper() for s in self.symbols]
        if not self.symbols:
            raise ValidationError("at least one symbol required")


@dataclass
class SystemConfig:
    loop_interval_seconds: float = 1.0
    save_state_interval_minutes: float = 5.0
    market: str = "NSE"
    dry_run: bool = False
    backtest_mode: bool = False
    state_file: str = str(DEFAULT_STATE_FILE)
    log_level: str = "INFO"
    max_errors_before_pause: int = 5
    heartbeat_interval_seconds: float = 60.0

    def __post_init__(self):
        self.loop_interval_seconds = float(self.loop_interval_seconds)
        if self.loop_interval_seconds < 0:
            raise ValidationError("loop_interval_seconds must be >=0")
        self.save_state_interval_minutes = float(self.save_state_interval_minutes)
        self.market = str(self.market).strip().upper()
        self.log_level = str(self.log_level).strip().upper()


@dataclass
class ForwardTestingConfig:
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfigWrapper = field(default_factory=ExecutionConfigWrapper)
    sizing: SizingConfigWrapper = field(default_factory=SizingConfigWrapper)
    data: DataConfig = field(default_factory=DataConfig)
    system: SystemConfig = field(default_factory=SystemConfig)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ForwardTestingConfig":
        def _get(section: str, default: Any = None):
            return data.get(section, default) or {}

        return cls(
            portfolio=PortfolioConfig(**_get("portfolio")),
            strategy=StrategyConfig(**_get("strategy")),
            risk=RiskConfig(**_get("risk")),
            execution=ExecutionConfigWrapper(**_get("execution")),
            sizing=SizingConfigWrapper(**_get("sizing")),
            data=DataConfig(**_get("data")),
            system=SystemConfig(**_get("system")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "portfolio": {
                "initial_capital": str(self.portfolio.initial_capital),
                "name": self.portfolio.name,
                "base_currency": self.portfolio.base_currency,
                "allow_short": self.portfolio.allow_short,
            },
            "strategy": {
                "name": self.strategy.name,
                "parameters": dict(self.strategy.parameters),
            },
            "risk": {
                "max_position_size": (
                    str(self.risk.max_position_size) if self.risk.max_position_size else None
                ),
                "max_positions": self.risk.max_positions,
                "max_drawdown_pct": str(self.risk.max_drawdown_pct),
                "daily_loss_limit_pct": str(self.risk.daily_loss_limit_pct),
                "buckets": {
                    bucket: {
                        field: (
                            str(value)
                            if isinstance(value, Decimal)
                            else sorted(value) if isinstance(value, (set, frozenset)) else value
                        )
                        for field, value in values.items()
                    }
                    for bucket, values in self.risk.buckets.items()
                },
            },
            "execution": {
                "realism": self.execution.realism,
                "slippage_model": self.execution.slippage_model,
                "commission_model": self.execution.commission_model,
            },
            "sizing": {
                "method": self.sizing.method,
                "params": dict(self.sizing.params),
            },
            "data": {
                "provider": self.data.provider,
                "symbols": list(self.data.symbols),
                "timeframe": self.data.timeframe,
            },
            "system": {
                "loop_interval_seconds": self.system.loop_interval_seconds,
                "save_state_interval_minutes": self.system.save_state_interval_minutes,
                "market": self.system.market,
                "dry_run": self.system.dry_run,
                "backtest_mode": self.system.backtest_mode,
                "state_file": self.system.state_file,
            },
        }


def load_forward_config(path: str | Path | None = None) -> ForwardTestingConfig:
    """Load config from YAML, with defaults if file missing."""
    config_path = Path(path) if path else DEFAULT_FORWARD_CONFIG_PATH

    if path is not None and not config_path.exists():
        raise ValidationError(f"forward testing config not found: {config_path}")

    if not config_path.exists():
        logger.info("No config file at %s, using defaults", config_path)
        return ForwardTestingConfig()

    try:
        import yaml

        doc = yaml.safe_load(config_path.read_text()) or {}
    except ModuleNotFoundError as exc:
        raise ValidationError("PyYAML required to load config") from exc
    except Exception as exc:
        raise ValidationError(f"could not parse {config_path}: {exc}") from exc

    return ForwardTestingConfig.from_dict(doc)


# ---------------------------------------------------------------------------
# Placeholders for Steps 10-12, 15-19 (minimal but functional)
# ---------------------------------------------------------------------------


class MockMarketDataHandler:
    """Minimal market data handler (Step 10 placeholder).

    In backtest mode, replays historical data from a DataSource.
    In live mode, returns mock ticks.
    """

    def __init__(self, symbols: List[str], provider: str = "mock", data_source: Any = None):
        self.symbols = symbols
        self.provider = provider
        self.data_source = data_source
        self._subscribed = set(symbols)
        self._latest: Dict[str, Dict[str, Any]] = {}
        self._connected = False

    def connect(self):
        self._connected = True
        logger.info("Market data handler connected (provider=%s)", self.provider)

    def disconnect(self):
        self._connected = False
        logger.info("Market data handler disconnected")

    def subscribe_symbols(self, symbols: List[str]):
        self._subscribed.update([s.upper() for s in symbols])

    def get_latest_data(self) -> Dict[str, Dict[str, Any]]:
        # In real implementation, this would fetch from broker API
        # For now, return latest mock or empty
        return dict(self._latest)

    def get_current_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        return self._latest.get(symbol.upper())

    def inject_bar(self, bar: Dict[str, Any]):
        """For testing: inject a bar as if it came from live feed."""
        symbol = str(bar.get("symbol", "")).upper()
        if symbol:
            self._latest[symbol] = bar


class MockDataValidator:
    """Minimal data quality validator (Step 11 placeholder)."""

    def validate(self, market_data: Any) -> bool:
        if not market_data:
            return False
        # Basic OHLC sanity
        if isinstance(market_data, dict):
            # single symbol dict or mapping symbol->bar
            if "symbol" in market_data and "close" in market_data:
                return self._validate_bar(market_data)
            # mapping
            for bar in market_data.values():
                if isinstance(bar, dict) and not self._validate_bar(bar):
                    return False
        return True

    def _validate_bar(self, bar: Dict[str, Any]) -> bool:
        try:
            if bar.get("close") is None:
                return False
            close = float(bar["close"])
            if close <= 0:
                return False
            # OHLC consistency
            high = bar.get("high")
            low = bar.get("low")
            if high is not None and low is not None:
                if float(high) < float(low):
                    return False
                if float(high) < close or float(low) > close:
                    return False
            return True
        except Exception:
            return False


class MockTimeManager:
    """Minimal time manager (Step 12 placeholder)."""

    def __init__(self, market: str = "NSE"):
        self.market = market

    def is_market_open(self, symbol: str | None = None) -> bool:
        # For backtest, always open
        return True

    def get_current_time(self) -> datetime:
        return datetime.now(timezone.utc)


class MockRiskManager:
    """Minimal risk manager (Step 15 placeholder).

    Checks portfolio limits, drawdown, daily loss.
    """

    def __init__(self, portfolio: Any, risk_config: RiskConfig):
        self.portfolio = portfolio
        self.config = risk_config
        self._daily_pnl = Decimal("0")
        self._peak_equity = None

    def validate_order(self, order: Any) -> tuple[bool, str]:
        # Check portfolio can_open_position for BUY
        try:
            if hasattr(order, "symbol") and hasattr(order, "quantity"):
                # For BUY orders opening new positions
                if not self.portfolio.has_position(order.symbol):
                    check = self.portfolio.can_open_position(
                        order.symbol, order.quantity, order.limit_price or 100
                    )
                    if not check:
                        return False, f"{check.code}: {check.reason}"
            # Drawdown check
            if self.config.max_drawdown_pct:
                try:
                    drawdown = self.portfolio.current_drawdown()
                    if drawdown > self.config.max_drawdown_pct:
                        return False, f"max_drawdown {drawdown} > {self.config.max_drawdown_pct}"
                except Exception:
                    pass
            return True, "ok"
        except Exception as exc:
            return False, f"risk check error: {exc}"

    def validate_orders(self, orders: List[Any]) -> List[Any]:
        approved = []
        for order in orders:
            ok, reason = self.validate_order(order)
            if ok:
                approved.append(order)
            else:
                logger.info("Risk rejected order %s: %s", getattr(order, "symbol", "?"), reason)
        return approved

    def check_drawdown_limits(self, portfolio: Any) -> bool:
        try:
            dd = portfolio.current_drawdown()
            return dd <= self.config.max_drawdown_pct
        except Exception:
            return True


class MockStopManager:
    """Minimal stop manager (Step 16 placeholder)."""

    def __init__(self, portfolio: Any):
        self.portfolio = portfolio
        self._stops: Dict[str, Dict[str, Any]] = {}

    def add_stop_loss(self, symbol: str, stop_price: Any):
        self._stops[symbol] = {"stop_price": stop_price, "type": "fixed"}

    def check_stops(self, market_data: Any) -> List[Any]:
        # No automatic stops in placeholder
        return []


class MockPerformanceCalculator:
    """Minimal performance tracker (Step 17 placeholder)."""

    def __init__(self, portfolio: Any):
        self.portfolio = portfolio
        self.equity_curve: List[Dict[str, Any]] = []
        self.metrics: Dict[str, Any] = {}

    def update_metrics(self, portfolio: Any = None):
        pf = portfolio or self.portfolio
        try:
            equity = pf.calculate_total_equity()
            self.equity_curve.append(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "equity": str(equity),
                    "cash": str(pf.current_cash),
                }
            )
            # Simple metrics
            self.metrics = {
                "total_equity": equity,
                "cash": pf.current_cash,
                "open_positions": len(pf.positions),
                "total_return": pf.total_return if hasattr(pf, "total_return") else ZERO,
            }
        except Exception as exc:
            logger.debug("performance update failed: %s", exc)

    def get_metrics(self) -> Dict[str, Any]:
        return dict(self.metrics)


# ---------------------------------------------------------------------------
# State classification helpers (ticket F-04)
# ---------------------------------------------------------------------------


def _ts_key(ts: Any) -> str:
    """Canonical JSON-safe key for a bar timestamp (ticket #7).

    Both the live dedupe (:meth:`ForwardTestingEngine._new_bars`) and the
    persisted ``engine_runtime.last_bar_ts`` use this encoding, so a restored
    engine compares the same shape an in-memory engine does.
    """
    if ts is None:
        return ""
    try:
        return pd.Timestamp(ts).isoformat()
    except Exception:  # noqa: BLE001 - non-date stamps survive as strings
        return str(ts)


def _ts_from_key(key: Any) -> Any:
    """Inverse of :func:`_ts_key` for restoring a saved timestamp."""
    if key in (None, ""):
        return None
    try:
        return pd.Timestamp(key)
    except Exception:  # noqa: BLE001
        return key


def _normalize_mode(mode: Any, default: Any = None) -> str:
    """Coerce + validate a state ``mode`` against ``db.models.PortfolioMode``.

    Uses the ORM enum (the same vocabulary as the ``portfolios.mode`` CHECK)
    rather than re-declaring ``'paper'``/``'live'`` here.
    """
    from backtest.db.models import PortfolioMode

    fallback = default or PortfolioMode.PAPER.value
    if mode is None:
        return fallback
    mode = str(mode).strip().lower()
    valid = {m.value for m in PortfolioMode}
    if mode not in valid:
        logger.warning(
            "state mode %r is not one of %s; falling back to %r",
            mode,
            sorted(valid),
            fallback,
        )
        return fallback
    return mode


def _normalize_source(source: Any, default: Any = None) -> str:
    """Coerce + validate a state ``source`` against the canonical SOURCE_TAGS.

    The single set of tag strings lives in ``backtest.data.source_tags`` — the
    state module never re-declares ``synthetic``/``replay``/``mstock``.
    """
    from backtest.data.source_tags import DEFAULT_SOURCE_TAG, SOURCE_TAG_VALUES

    fallback = default or DEFAULT_SOURCE_TAG
    if source is None:
        return fallback
    source = str(source).strip().lower()
    if source not in SOURCE_TAG_VALUES:
        logger.warning(
            "state source %r is not one of %s; falling back to %r",
            source,
            sorted(SOURCE_TAG_VALUES),
            fallback,
        )
        return fallback
    return source


def _classify(engine: Any) -> tuple[str, str]:
    """(mode, source) for an engine — from its ACTUAL config, duck-typed.

    ``config.data.mode`` is ``backtest|paper|live``; the state/portfolio
    vocabulary is ``paper|live`` (migration 002), and a backtest replay runs
    simulated fills, so it classifies as the ``paper`` bucket (ticket P1.1).
    Missing values fall back through the canonical normalize helpers (the
    ORM's ``PortfolioMode`` / ``SOURCE_TAGS`` default) — never hardcoded here.
    """
    data = getattr(getattr(engine, "config", None), "data", None)
    mode = getattr(data, "mode", None)
    source = getattr(data, "source", None)
    if str(mode).strip().lower() == "backtest":
        mode = "paper"
    return _normalize_mode(mode), _normalize_source(source)


# ---------------------------------------------------------------------------
# State Manager
# ---------------------------------------------------------------------------


class StateManager:
    """Persists full system state for crash recovery (Step 20)."""

    def __init__(self, state_file: str | Path = DEFAULT_STATE_FILE):
        self.state_file = Path(state_file)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

    def save_state(self, engine: "ForwardTestingEngine") -> str:
        """Save full engine state to JSON (format v3, tickets F-04 + #7).

        The payload mirrors the ``portfolios`` classification added by
        migration 002: ``mode`` (paper|live) + ``source``
        (synthetic|replay|mstock) are derived from the engine's ACTUAL config
        (via :func:`_classify`) — never hardcoded, and the ``source`` strings
        come from the canonical :data:`backtest.data.source_tags.SOURCE_TAGS`.

        v3 adds the **resume fidelity** fields: ``executor`` (in-flight
        pending/armed orders) and ``engine_runtime`` (loop count, last bar
        timestamp per symbol, per-symbol processed-bar counts) — see
        :data:`STATE_VERSION`.
        """
        try:
            mode, source = _classify(engine)
            executor_state = {}
            if hasattr(engine, "executor") and hasattr(engine.executor, "get_state"):
                try:
                    executor_state = engine.executor.get_state()
                except Exception as exc:
                    logger.warning("executor state capture failed: %s", exc)
            payload = {
                "state_version": STATE_VERSION,
                "engine_id": getattr(getattr(engine, "portfolio", None), "portfolio_id", None),
                "mode": mode,
                "source": source,
                "portfolio": (
                    engine.portfolio.to_dict() if hasattr(engine.portfolio, "to_dict") else {}
                ),
                "adapter": (
                    engine.adapter.get_state() if hasattr(engine.adapter, "get_state") else {}
                ),
                "executor": executor_state,
                "engine_runtime": {
                    "loop_count": getattr(engine, "_loop_count", 0),
                    "last_bar_ts": {
                        str(sym): _ts_key(ts)
                        for sym, ts in getattr(engine, "_last_bar_ts", {}).items()
                    },
                    "processed_bars": dict(getattr(engine, "_processed_bars", {})),
                },
                "performance": (
                    engine.performance.equity_curve
                    if hasattr(engine.performance, "equity_curve")
                    else []
                ),
                "config": engine.config.to_dict(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "loop_count": engine._loop_count,
            }
            # Atomic write via temp file
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.replace(self.state_file)
            logger.info(
                "State saved to %s (v%s %s/%s, loop %s)",
                self.state_file,
                STATE_VERSION,
                mode,
                source,
                engine._loop_count,
            )
            return str(self.state_file)
        except Exception as exc:
            logger.exception("Failed to save state: %s", exc)
            raise

    def load_state(
        self,
        path: str | Path | None = None,
        engine: "ForwardTestingEngine | None" = None,
    ) -> Optional[Dict[str, Any]]:
        """Load a state file (ticket F-04).

        ``engine`` is optional: it supplies the ACTUAL run config so legacy
        files can be filled with the right mode/source instead of a hardcoded
        default. When omitted (e.g. a standalone ``StateManager``), the
        helper's own safe defaults apply.

        Accepts:

        * **v1 / legacy** — missing ``state_version`` (the pre-F-04 format).
          It is accepted as-is (no hard failure) and normalized to v2 in place:
          ``mode``/``source`` are derived from the ACTUAL engine config at the
          moment of load (never hardcoded; a warn + fallback if the file holds
          an invalid value), then the migrated file is rewritten on the next
          :meth:`save_state`. No separate on-disk migration on load — loading
          must stay read-only, so the file keeps surviving restarts.
        * **v2** — current; invalid ``mode``/``source`` values are warned and
          replaced with the engine-derived value but the file is touched only
          on the next save.

        ``state_version`` values above the current format are refused (the
        file was written by a newer product; a warn + ``None``).
        """
        file_path = Path(path) if path else self.state_file
        if not file_path.exists():
            logger.info("No state file at %s", file_path)
            return None
        try:
            data = json.loads(file_path.read_text())
        except Exception as exc:
            logger.warning("Failed to load state from %s: %s", file_path, exc)
            return None

        if not isinstance(data, dict):
            logger.warning("State file %s is not a JSON object; ignoring", file_path)
            return None

        version = data.get("state_version", 1)
        try:
            version = int(version)
        except (TypeError, ValueError):
            logger.warning(
                "State file %s has non-integer state_version %r; treating as v1", file_path, version
            )
            version = 1

        if version > STATE_VERSION:
            logger.warning(
                "State file %s is format v%s but this build supports <= v%s; refusing to load",
                file_path,
                version,
                STATE_VERSION,
            )
            return None

        if version < STATE_VERSION:
            logger.warning(
                "State file %s is legacy format v%s; migrating to v%s semantics in memory "
                "(kept read-only on disk; rewritten on next save)",
                file_path,
                version,
                STATE_VERSION,
            )

        # Normalize the F-04 classification in memory (never mutates this
        # object's expectation of the file — the rewrite happens on save).
        mode, source = _classify(engine)
        data["mode"] = _normalize_mode(data.get("mode"), default=mode)
        data["source"] = _normalize_source(data.get("source"), default=source)
        data["state_version"] = STATE_VERSION

        logger.info(
            "State loaded from %s (v%s, loop %s)",
            file_path,
            data["state_version"],
            data.get("loop_count", "?"),
        )
        return data

    def should_save(self, last_save: datetime, interval_minutes: float) -> bool:
        if interval_minutes <= 0:
            return False
        return datetime.now(timezone.utc) - last_save >= timedelta(minutes=interval_minutes)


# ---------------------------------------------------------------------------
# Main Engine
# ---------------------------------------------------------------------------


class ForwardTestingEngine:
    """Main orchestration engine (Step 20).

    Parameters
    ----------
    config_file:
        Path to YAML config. If None, uses defaults and env.
    config_dict:
        Dict config overrides (takes precedence over file).
    db_manager:
        Optional DatabaseManager. If None, created from env.
    data_source:
        Optional DataSource for backtest replay mode.
    portfolio:
        Optional existing Portfolio instance.
    strategy:
        Optional existing Strategy instance.
    """

    def __init__(
        self,
        config_file: str | Path | None = None,
        config_dict: Optional[Mapping[str, Any]] = None,
        db_manager: Any = None,
        data_source: Any = None,
        portfolio: Any = None,
        strategy: Any = None,
        broker: Any = None,
    ):
        # Load config
        if config_file:
            self.config = load_forward_config(config_file)
        else:
            self.config = load_forward_config()

        if config_dict:
            # Merge overrides
            merged = self.config.to_dict()
            # Deep merge simple: update top-level sections
            for section, values in config_dict.items():
                if section in merged and isinstance(values, dict):
                    merged[section].update(values)
                else:
                    merged[section] = values
            self.config = ForwardTestingConfig.from_dict(merged)

        self.db_manager = db_manager
        self.data_source = data_source
        self._provided_portfolio = portfolio
        self._provided_strategy = strategy
        #: Duck-typed live broker (ticket #8) — drives ``BrokerFillProvider``
        #: for ``config.data.mode == 'live'``. Injected fake brokers keep the
        #: live path deterministic; when None, a live run defaults to the
        #: repo's :class:`MStockBroker` (construction is safe — no auth,
        #: ``place_order`` fails cleanly without a session).
        self._broker = broker

        # Runtime state
        self.portfolio: Any = None
        self.strategy: Any = None
        self.adapter: Any = None
        self.executor: Any = None
        self.sizer: Any = None
        self.data_handler: Any = None
        self.validator: Any = None
        self.time_manager: Any = None
        self.risk_manager: Any = None
        self.stop_manager: Any = None
        self.performance: Any = None
        self.trade_analyzer: Any = None
        self.state_manager: StateManager = StateManager(self.config.system.state_file)
        #: Resolved bucket classification + limits (ticket #9) — set by
        #: ``initialize_system`` from ``_classify``, the single keying point.
        self._bucket_key: Optional[str] = None
        self._bucket_limits: Any = None

        self._running = False
        self._paused = False
        self._loop_count = 0
        self._error_count = 0
        self._last_save = datetime.now(timezone.utc)
        self._last_heartbeat = datetime.now(timezone.utc)
        #: Last processed bar timestamp per symbol — a poll can return the
        #: same completed bar repeatedly; the executor bar clock must only
        #: advance on NEW bars (ticket F-01).
        self._last_bar_ts: Dict[str, Any] = {}
        #: Bars already replayed per symbol in backtest mode (ticket #7):
        #: a restored engine resumes at the next unprocessed bar instead of
        #: re-running the full history (which would double-fill positions).
        self._processed_bars: Dict[str, int] = {}

        # Lifecycle hooks
        self._hooks: Dict[str, List[Callable]] = {
            "on_start": [],
            "on_market_open": [],
            "on_market_close": [],
            "on_stop": [],
            "on_error": [],
        }

        # Setup logging
        self._setup_logging()

        # Signal handlers
        self._setup_signal_handlers()

        logger.info("ForwardTestingEngine initialized: config=%s", config_file or "defaults")

    # -- lifecycle hooks ---------------------------------------------------

    def add_hook(self, event: str, callback: Callable):
        if event not in self._hooks:
            raise ValidationError(f"unknown hook event {event}; expected {list(self._hooks)}")
        self._hooks[event].append(callback)

    def _fire_hook(self, event: str, *args, **kwargs):
        for cb in self._hooks.get(event, []):
            try:
                cb(*args, **kwargs)
            except Exception:
                logger.exception("Hook %s failed", event)

    def on_start(self):
        self._fire_hook("on_start", self)

    def on_market_open(self):
        self._fire_hook("on_market_open", self)

    def on_market_close(self):
        self._fire_hook("on_market_close", self)

    def on_stop(self):
        self._fire_hook("on_stop", self)

    def on_error(self, exc: Exception):
        self._fire_hook("on_error", self, exc)

    # -- setup -------------------------------------------------------------

    def _setup_logging(self):
        # Delegates to the project-wide setup so the engine, the web app and the
        # simulator all share one format/handler (see backtest.logging_config).
        from backtest.logging_config import configure_logging

        configure_logging(self.config.system.log_level)
        logging.getLogger(__name__).debug(
            "engine logging at %s", logging.getLevelName(logging.getLogger().level)
        )

    def _setup_signal_handlers(self):
        def _handle_signal(signum, frame):
            logger.warning("Received signal %s, stopping gracefully", signum)
            self.stop()

        try:
            signal.signal(signal.SIGINT, _handle_signal)
            signal.signal(signal.SIGTERM, _handle_signal)
        except Exception:
            # Signal handlers may not work in all environments (e.g. tests)
            pass

    # -- canonical fill path (ticket F-01) --------------------------------

    @staticmethod
    def _bar_ts(bar: Mapping[str, Any]) -> Any:
        return bar.get("timestamp") or bar.get("ts") or bar.get("time") or bar.get("datetime")

    def _new_bars(self, market_data: Mapping[str, Any]) -> Dict[str, Any]:
        """Keep only bars newer than the last processed one, per symbol.

        A live poll can return the same completed bar repeatedly. The
        executor's bar clock (``step``) must advance only on NEW bars —
        stepping the same bar twice would let an armed order fill at the
        SIGNAL bar's open, which is exactly the leak F-01 removes.
        Bars without a timestamp are treated as new each poll (no dedupe
        information available).
        """
        new_bars: Dict[str, Any] = {}
        for sym, bar in market_data.items():
            sym = str(sym).strip().upper()
            if not isinstance(bar, Mapping):
                continue
            ts = self._bar_ts(bar)
            key = _ts_key(ts)
            # Canonical key (ticket #7): a restored engine's saved timestamp
            # compares equal to the live one regardless of the in-memory type
            # (datetime vs pd.Timestamp vs string).
            if ts is None or self._last_bar_ts.get(sym) != key:
                new_bars[sym] = bar
                if ts is not None:
                    self._last_bar_ts[sym] = key
        return new_bars

    @staticmethod
    def _to_executor_bar(symbol: str, bar: Mapping[str, Any]) -> Bar:
        """The minimal bar view the executor needs — ``open`` is the anchor.

        ``close`` is deliberately not used by the executor's fill logic; the
        bar's timestamp is passed through for session checks.
        """
        close = bar.get("close")
        if close is None:
            close = bar.get("last") or bar.get("price")
        open_price = bar.get("open")
        if open_price is None:
            open_price = close
        return Bar(
            open=to_python_scalar(open_price),
            close=to_python_scalar(close),
            volume=to_python_scalar(bar.get("volume")),
            timestamp=to_python_scalar(ForwardTestingEngine._bar_ts(bar)),
        )

    def _submit_orders(self, signals: Iterable[Any], market_data: Mapping[str, Any]) -> List[Any]:
        """Signal → Order → ``executor.submit`` (arms for the NEXT bar).

        Same sequence the canonical ``run_engine_loop`` uses: while bar ``t``
        is the latest known data, the order is submitted; the executor arms
        it on the next ``step`` and only fills at the bar AFTER that, at its
        open.
        """
        created: List[Any] = []
        if self.executor is None:
            logger.warning(
                "no executor; %d signal(s) created without fill path",
                len(signals) if signals else 0,
            )
            return created
        for order in self.adapter.create_orders(signals, market_data=market_data):
            # Ticket #9 — risk teeth for the LIVE bucket: every real-fill
            # order passes the bucket pre-trade check before it reaches the
            # executor. Paper stays free play (its bucket caps are permissive
            # anyway, and sizing already ran through the same bucket caps).
            if self._bucket_key == "live" and self.risk_manager is not None:
                if not self._bucket_risk_allows(order, market_data):
                    continue
            self.executor.submit(order)
            created.append(order)
        return created

    def _bucket_risk_allows(
        self, order: Any, market_data: Optional[Mapping[str, Any]] = None
    ) -> bool:
        """Run the bucket risk manager's check for one live order.

        Duck-typed: the real ``RiskManager`` returns a ``RiskCheckResult``
        (``allowed``), the mock fallback returns a ``(bool, reason)`` tuple.
        """
        price = self._order_price(order, market_data)
        try:
            result = self.risk_manager.validate_order(order, current_price=price)
        except TypeError:  # mock fallback takes only the order
            result = self.risk_manager.validate_order(order)
        if hasattr(result, "allowed"):
            allowed = bool(result.allowed)
            reason = getattr(result, "reason", "")
        elif isinstance(result, tuple) and result:
            allowed = bool(result[0])
            reason = result[1] if len(result) > 1 else ""
        else:
            allowed = bool(result)
            reason = ""
        if not allowed:
            logger.warning(
                "Risk rejected live order %s %s x%s: %s",
                getattr(order, "symbol", "?"),
                getattr(order, "side", "?"),
                getattr(order, "quantity", "?"),
                reason,
            )
        return allowed

    def _order_price(self, order: Any, market_data: Optional[Mapping[str, Any]] = None) -> Any:
        """Best price for the bucket risk check: market data > limit > last bar."""
        symbol = str(getattr(order, "symbol", "") or "").strip().upper()
        if symbol and isinstance(market_data, Mapping):
            md = market_data.get(symbol)
            if isinstance(md, Mapping):
                for key in ("close", "last", "price", "ask", "bid"):
                    if md.get(key):
                        return md[key]
            for key in ("close", "last", "price"):
                if market_data.get(key):
                    return market_data[key]
        limit = getattr(order, "limit_price", None)
        if limit:
            return limit
        if symbol:
            try:
                candles = self.adapter._bars.get(symbol)
                if candles is not None and not candles.empty:
                    return float(candles["close"].iloc[-1])
            except Exception:
                pass
        return 100  # same fallback the strategy adapter uses

    def initialize_system(self):
        """Initialize all components, restore state if available."""
        logger.info("Initializing system...")

        # DB
        if self.db_manager is None:
            try:
                from backtest.db.manager import DatabaseManager

                self.db_manager = DatabaseManager.from_env()
                self.db_manager.connect()
                # Create tables if needed
                from backtest.db.models import Base

                Base.metadata.create_all(self.db_manager.engine)
                logger.info("Database connected")
            except Exception as exc:
                logger.warning("DB connection failed, running without DB: %s", exc)
                self.db_manager = None

        # Ticket #9 — risk limits resolve from the SAME classification that
        # labels the run (mode/source), at the same point ``_classify``
        # resolves them. No hardcoded global knob: the canonical bucket map
        # (backtest.simulator.bucket_risk) owns the defaults and this config
        # (risk.buckets) only overrides them.
        run_mode, run_source = _classify(self)
        try:
            from backtest.simulator.bucket_risk import resolve_bucket_risk

            self._bucket_key, self._bucket_limits = resolve_bucket_risk(
                run_mode,
                run_source,
                overrides=self.config.risk.buckets,
            )
        except Exception as exc:
            # A live/synthetic run etc. must be refused BEFORE any trading —
            # the source gate is part of the risk limits, not a soft warning.
            logger.error("Bucket risk resolution refused the run: %s", exc)
            raise

        # Portfolio
        if self._provided_portfolio is not None:
            self.portfolio = self._provided_portfolio
        else:
            # Try to restore from state file
            state = self.state_manager.load_state(engine=self)
            if state is None:
                pass
            elif "portfolio" not in state or not state["portfolio"]:
                logger.warning("State file exists but has no usable portfolio; creating fresh")
            else:
                # New-format classification (mode/source on the state file) is
                # advisory here — the engine's live config always wins, and the
                # portfolio row already carries the authoritative classification.
                if state.get("mode") not in (None, run_mode) or state.get("source") not in (
                    None,
                    run_source,
                ):
                    logger.warning(
                        "State classification (%s/%s) != engine classification (%s/%s); "
                        "engine config wins, state will be rewritten on next save",
                        state.get("mode"),
                        state.get("source"),
                        run_mode,
                        run_source,
                    )
                try:
                    from backtest.simulator.portfolio import Portfolio

                    self.portfolio = Portfolio.from_dict(state["portfolio"])
                    logger.info("Portfolio restored from state file: %s", self.portfolio.name)
                except Exception as exc:
                    logger.warning("Failed to restore portfolio from state: %s", exc)

            if self.portfolio is None:
                # Try to load from DB if name exists?
                # For now, create new
                from backtest.simulator.portfolio import Portfolio

                # Ticket #9 — the bucket (not a global default) sizes the
                # fresh portfolio's per-trade limits.
                limits = self._bucket_limits.to_portfolio_limits(
                    allow_short=self.config.portfolio.allow_short,
                )
                # Ticket #8 — a FRESH run classifies itself from the real
                # config (live/mstock) instead of the constructor defaults
                # (paper/synthetic); the DB row then matches state + config.
                self.portfolio = Portfolio(
                    name=self.config.portfolio.name,
                    initial_capital=self.config.portfolio.initial_capital,
                    limits=limits,
                    mode=run_mode,
                    source=run_source,
                )
                logger.info(
                    "New portfolio created: %s %s (%s/%s)",
                    self.portfolio.name,
                    self.portfolio.initial_capital,
                    self.portfolio.mode,
                    self.portfolio.source,
                )

        # Ticket #8 — no silent classification downgrade. The engine's
        # config is authoritative for the CURRENT run; the guard only ever
        # upgrades paper→live when the run is live (a live run must never
        # resurrect as paper) and never claims live for a paper run.
        guard_fired = False
        if run_mode == "live" and getattr(self.portfolio, "mode", "paper") != "live":
            logger.warning(
                "Portfolio %s is classified %s/%s but the engine config is LIVE — "
                "upgrading to live/%s so state/DB can never show a live run "
                "reclassified as paper",
                self.portfolio.name,
                getattr(self.portfolio, "mode", "?"),
                getattr(self.portfolio, "source", "?"),
                run_source,
            )
            self.portfolio.mode = "live"
            self.portfolio.source = run_source
            guard_fired = True
        elif run_mode == "paper" and getattr(self.portfolio, "mode", "paper") == "live":
            logger.warning(
                "Portfolio %s was restored classified live/%s but the engine config is "
                "PAPER — engine config wins, reclassifying to paper/%s (a paper run "
                "must never claim live). This usually means the state file belongs "
                "to a different run; the accounting restores unchanged.",
                self.portfolio.name,
                getattr(self.portfolio, "source", "?"),
                run_source,
            )
            self.portfolio.mode = "paper"
            self.portfolio.source = run_source
            guard_fired = True

        # Ticket #9 — the bucket that trades is the bucket that was
        # risk-limited: ALWAYS re-key the portfolio's per-trade limits from
        # the resolved bucket (fresh, restored or provided). Explicit config
        # values that used to cap positions stay effective as a fallback when
        # the bucket leaves a limit open.
        allow_short = bool(
            getattr(getattr(self.portfolio, "limits", None), "allow_short", None)
            if getattr(self.portfolio, "limits", None) is not None
            else self.config.portfolio.allow_short
        )
        # Bucket caps, then the explicit config-level position cap as a
        # fallback (preserves the pre-T9 config behavior when the bucket
        # leaves the limit open).
        self.portfolio.limits = self._bucket_limits.to_portfolio_limits(
            allow_short=allow_short,
        )
        if self.portfolio.limits.max_open_positions is None:
            self.portfolio.limits.max_open_positions = (
                self.config.portfolio.max_open_positions or self.config.risk.max_positions
            )
        logger.info(
            "Bucket risk %s/%s: %s",
            self._bucket_key,
            run_source,
            ", ".join(
                f"{k}={v}"
                for k, v in self._bucket_limits.to_dict().items()
                if k != "allowed_sources"
            ),
        )

        # Risk teeth on the T8 guard: if the classification had to be
        # CHANGED (mis-classified restore), the OPEN book must satisfy the
        # target bucket's caps — otherwise refuse the run instead of
        # silently trading at the wrong size.
        if guard_fired:
            violation = self._bucket_limits.check_exposure(self.portfolio)
            if violation:
                raise ValidationError(
                    f"RISK REFUSAL — mis-classified {run_mode} run: {violation}. "
                    "The open book violates the bucket risk caps; refusing to run "
                    "at the wrong size instead of silently trading under mismatched "
                    "limits. Close/reclassify the portfolio or override the bucket "
                    "limits explicitly (risk.buckets)."
                )

        # Strategy
        if self._provided_strategy is not None:
            self.strategy = self._provided_strategy
        else:
            try:
                from backtest.strategy.registry import get_strategy

                StratCls = get_strategy(self.config.strategy.name)
                self.strategy = StratCls(**self.config.strategy.parameters)
                logger.info(
                    "Strategy loaded: %s %s", self.strategy.name, self.config.strategy.parameters
                )
            except Exception as exc:
                logger.error("Failed to load strategy %s: %s", self.config.strategy.name, exc)
                raise

        # Position sizer
        try:
            from backtest.simulator.position_sizing import PositionSizer, SizingConfig

            # Build sizing config from engine config
            sizing_dict = {
                "method": self.config.sizing.method,
            }
            sizing_dict.update(self.config.sizing.params)

            # If sizing params contain risk_per_trade etc, pass through
            # Ticket #9 — the bucket's exposure/size caps become the sizer's
            # hard constraints, so the size that reaches the executor is
            # already bucket-limited (paper: permissive; live: tight).
            cfg = SizingConfig(
                method=sizing_dict.get("method", "fixed_quantity"),
                fixed_quantity=sizing_dict.get("fixed_quantity", 100),
                fixed_dollar_amount=sizing_dict.get("fixed_dollar_amount", 10000),
                percentage=sizing_dict.get("percentage", 0.05),
                risk_per_trade=sizing_dict.get("risk_per_trade", 0.01),
                stop_loss_pct=sizing_dict.get("stop_loss_pct", 0.02),
                atr=sizing_dict.get("atr"),
                risk_amount=sizing_dict.get("risk_amount"),
                win_rate=sizing_dict.get("win_rate"),
                avg_win=sizing_dict.get("avg_win"),
                avg_loss=sizing_dict.get("avg_loss"),
                kelly_fraction=sizing_dict.get("kelly_fraction", 0.5),
                constraints=self._bucket_limits.to_sizing_constraints(),
            )
            self.sizer = PositionSizer(cfg)
            logger.info(
                "Position sizer initialized: %s (bucket %s caps)",
                cfg.method,
                self._bucket_key,
            )
        except Exception as exc:
            logger.warning("Failed to init sizer, using fixed 100: %s", exc)
            from backtest.simulator.position_sizing import PositionSizer, SizingConfig

            self.sizer = PositionSizer(SizingConfig(method="fixed_quantity", fixed_quantity=100))

        # Execution
        try:
            from backtest.simulator.execution import ExecutionConfig, OrderExecutor
            from backtest.simulator.fees import CommissionCalculator
            from backtest.simulator.slippage import SlippageCalculator

            exec_cfg = ExecutionConfig()
            # Try to load from file
            try:
                from backtest.simulator.execution import load_execution_config

                exec_cfg = load_execution_config(profile=self.config.execution.realism)
            except Exception:
                pass

            slippage = SlippageCalculator()
            fees = CommissionCalculator()

            # Ticket #8 — the live fill seam: in live mode the executor sends
            # orders to a REAL broker (BrokerFillProvider) instead of
            # simulating fills, so a live run can never silently paper-trade.
            # Everything above the fill (portfolio/positions/risk/metrics)
            # stays the shared path; only the provider differs.
            run_mode, _ = _classify(self)
            if run_mode == "live":
                from backtest.simulator.fill_providers import BrokerFillProvider

                fill_provider = BrokerFillProvider(broker=self._resolve_live_broker())
                self.executor = OrderExecutor(
                    config=exec_cfg,
                    slippage=slippage,
                    fees=fees,
                    portfolio=self.portfolio,
                    fill_provider=fill_provider,
                )
                logger.info(
                    "Order executor initialized: %s — LIVE (broker %s)",
                    exec_cfg.realism,
                    type(fill_provider.broker).__name__,
                )
            else:
                self.executor = OrderExecutor(
                    config=exec_cfg,
                    slippage=slippage,
                    fees=fees,
                    portfolio=self.portfolio,
                )
                logger.info("Order executor initialized: %s", exec_cfg.realism)
        except Exception as exc:
            logger.warning("Failed to init executor: %s", exc)
            self.executor = None

        # Strategy adapter — signal source only (ticket F-01). It produces
        # signals + Order objects; the engine owns the executor's bar clock
        # (submit → step) so fills land at the NEXT bar's open.
        try:
            from backtest.forward.strategy_adapter import StrategyAdapter

            self.adapter = StrategyAdapter(
                strategy=self.strategy,
                portfolio=self.portfolio,
                symbols=self.config.data.symbols,
                dry_run=self.config.system.dry_run,
                position_sizer=self.sizer,
                db_manager=self.db_manager,
                allow_short=self.config.portfolio.allow_short,
            )
            # Restore adapter state if available
            state = self.state_manager.load_state(engine=self)
            if state and "adapter" in state and state["adapter"]:
                try:
                    self.adapter.load_state(state["adapter"])
                    logger.info("Adapter state restored")
                except Exception as exc:
                    logger.warning("Failed to restore adapter state: %s", exc)

            logger.info("Strategy adapter initialized")
        except Exception as exc:
            logger.error("Failed to init adapter: %s", exc)
            raise

        # Data handler – try real implementation first, fallback to mock placeholder
        try:
            from backtest.live.data_validator import DataValidator as RealDataValidator
            from backtest.live.market_data_handler import MarketDataHandler as RealMarketDataHandler
            from backtest.live.time_manager import TimeManager as RealTimeManager

            self.data_handler = RealMarketDataHandler(
                symbols=self.config.data.symbols,
                provider=self.config.data.provider,
                db_manager=self.db_manager,
                validator=RealDataValidator(),
                time_manager=RealTimeManager(market=self.config.system.market),
                timeframe=self.config.data.timeframe,
                timeframes=[self.config.data.timeframe, "5min", "15min", "1day"],
            )
            self.validator = self.data_handler.validator
            self.time_manager = self.data_handler.time_manager
            logger.info("Using real MarketDataHandler (Steps 10-12)")
        except Exception as exc:
            logger.warning("Failed to init real MarketDataHandler, using mock placeholder: %s", exc)
            self.data_handler = MockMarketDataHandler(
                symbols=self.config.data.symbols,
                provider=self.config.data.provider,
                data_source=self.data_source,
            )
            self.validator = MockDataValidator()
            self.time_manager = MockTimeManager(market=self.config.system.market)

        self.data_handler.connect()

        # Validators and managers (if real handler already has validator/time_manager, keep them)
        if not hasattr(self, "validator") or self.validator is None:
            self.validator = MockDataValidator()
        if not hasattr(self, "time_manager") or self.time_manager is None:
            self.time_manager = MockTimeManager(market=self.config.system.market)

        # Risk manager – try real implementation
        try:
            from backtest.simulator.risk_manager import RiskConfig as RealRiskConfig
            from backtest.simulator.risk_manager import RiskManager as RealRiskManager

            # Ticket #9 — the pre-trade risk config is built from the bucket
            # caps (keyed on the classification), with drawdown/daily-loss
            # staying config-level (already explicit on the engine).
            real_risk_cfg = self._bucket_limits.to_risk_config(
                max_drawdown_pct=self.config.risk.max_drawdown_pct,
                daily_loss_limit_pct=self.config.risk.daily_loss_limit_pct,
            )
            self.risk_manager = RealRiskManager(portfolio=self.portfolio, config=real_risk_cfg)
            logger.info("Using real RiskManager (Step 15, bucket %s)", self._bucket_key)
        except Exception as exc:
            logger.warning("Failed to init real RiskManager, using mock: %s", exc)
            self.risk_manager = MockRiskManager(
                portfolio=self.portfolio, risk_config=self.config.risk
            )

        # Stop manager – try real implementation
        try:
            from backtest.simulator.stop_manager import StopManager as RealStopManager

            self.stop_manager = RealStopManager(
                portfolio=self.portfolio, backtest_mode=self.config.system.backtest_mode
            )
            logger.info("Using real StopManager (Step 16)")
        except Exception as exc:
            logger.warning("Failed to init real StopManager, using mock: %s", exc)
            self.stop_manager = MockStopManager(portfolio=self.portfolio)

        # Performance calculator – try real implementation
        try:
            from backtest.simulator.performance import PerformanceCalculator as RealPerfCalc

            self.performance = RealPerfCalc(portfolio=self.portfolio, db_manager=self.db_manager)
            logger.info("Using real PerformanceCalculator (Step 17)")
        except Exception as exc:
            logger.warning("Failed to init real PerformanceCalculator, using mock: %s", exc)
            self.performance = MockPerformanceCalculator(portfolio=self.portfolio)

        # Trade analyzer – Step 18
        try:
            from backtest.simulator.trade_analyzer import TradeAnalyzer as RealTradeAnalyzer

            self.trade_analyzer = RealTradeAnalyzer(portfolio=self.portfolio)
            logger.info("Using real TradeAnalyzer (Step 18)")
        except Exception as exc:
            logger.warning("Failed to init TradeAnalyzer: %s", exc)
            self.trade_analyzer = None

        self._loop_count = 0
        self._error_count = 0

        # Restore the engine runtime + in-flight execution queue (ticket #7).
        # Fresh v3 files resume EXACTLY where a never-stopped engine would
        # be: loop counter, per-symbol processed bar counts, last bar
        # timestamps, and the executor's pending/armed orders. v1/v2 files
        # have none of these — they load (warned) and resume with an empty
        # queue, which is documented in STATE_VERSION.
        try:
            state = self.state_manager.load_state(engine=self)
        except Exception as exc:
            logger.warning("State read skipped: %s", exc)
            state = None
        if state:
            runtime = state.get("engine_runtime") or {}
            self._loop_count = int(runtime.get("loop_count", 0) or 0)
            self._error_count = 0
            self._last_bar_ts = {
                str(sym): _ts_from_key(ts) for sym, ts in (runtime.get("last_bar_ts") or {}).items()
            }
            self._processed_bars = {
                str(sym): int(count) for sym, count in (runtime.get("processed_bars") or {}).items()
            }
            if state.get("executor") and self.executor is not None:
                try:
                    # Share the portfolio's order objects so fills update the
                    # same graph (sync_orders sees the executor's transitions).
                    self.executor.restore_state(
                        state["executor"], orders=self.portfolio.pending_orders
                    )
                    logger.info(
                        "Executor restored: %d in-flight order(s), %d armed",
                        len(self.portfolio.pending_orders),
                        len(state["executor"].get("armed", [])),
                    )
                except Exception as exc:
                    logger.warning("Failed to restore executor state: %s", exc)
            if self._loop_count:
                logger.info(
                    "Resumed runtime: loop_count=%s processed=%s",
                    self._loop_count,
                    self._processed_bars,
                )

        logger.info(
            "System initialized: portfolio=%s strategy=%s symbols=%s",
            self.portfolio.name,
            self.strategy.name,
            self.config.data.symbols,
        )

    def _resolve_live_broker(self) -> Any:
        """The broker for live orders (ticket #8).

        Returns the injected ``broker`` when given (tests pass a
        deterministic fake); otherwise the repo's live broker
        (:class:`MStockBroker`) — constructed without auth; every live call
        is guarded by its session requirement, so an unauthenticated broker
        fails cleanly instead of half-sending.
        """
        if self._broker is not None:
            return self._broker
        from backtest.brokers.mstock import MStockBroker

        return MStockBroker()

    # -- control -----------------------------------------------------------

    def start(self):
        """Start the engine (blocks until stopped)."""
        if self.portfolio is None:
            self.initialize_system()

        self._running = True
        self._paused = False
        logger.warning(
            "Engine starting: %s (dry_run=%s backtest=%s)",
            self.portfolio.name,
            self.config.system.dry_run,
            self.config.system.backtest_mode,
        )

        self.on_start()

        try:
            if self.config.system.backtest_mode and self.data_source is not None:
                self._run_backtest_mode()
            else:
                self.run_loop()
        except Exception as exc:
            logger.exception("Engine crashed: %s", exc)
            self.on_error(exc)
            raise
        finally:
            self.stop()

    def stop(self):
        """Stop the engine gracefully."""
        if not self._running:
            return

        self._running = False
        logger.warning("Engine stopping...")

        try:
            # Save state
            self.state_manager.save_state(self)
            # Save portfolio to DB if available
            if self.db_manager is not None:
                try:
                    self.portfolio.save_to_db(self.db_manager)
                    logger.info("Portfolio saved to DB")
                except Exception as exc:
                    logger.warning("Failed to save portfolio to DB: %s", exc)
        except Exception as exc:
            logger.exception("Error during stop: %s", exc)

        self.on_stop()
        logger.warning("Engine stopped")

    def pause(self):
        self._paused = True
        if hasattr(self.portfolio, "pause"):
            self.portfolio.pause()
        logger.warning("Engine paused")

    def resume(self):
        if hasattr(self.portfolio, "status") and self.portfolio.status == "stopped":
            raise ValidationError("Cannot resume a stopped portfolio")
        self._paused = False
        if hasattr(self.portfolio, "resume"):
            try:
                self.portfolio.resume()
            except Exception:
                pass
        self._error_count = 0
        logger.info("Engine resumed")

    # -- main loop ---------------------------------------------------------

    def run_loop(self):
        """Main event loop for live trading."""
        logger.info(
            "Entering main loop: interval=%s save_interval=%s",
            self.config.system.loop_interval_seconds,
            self.config.system.save_state_interval_minutes,
        )

        while self._running:
            if self._paused:
                time.sleep(0.5)
                continue

            loop_start = time.perf_counter()

            try:
                # Get market data
                market_data = self.data_handler.get_latest_data()

                # Validate data
                if not self.validator.validate(market_data):
                    logger.debug("Data validation failed, skipping tick")
                    time.sleep(self.config.system.loop_interval_seconds)
                    continue

                # Check and update stops
                self.stop_manager.check_stops(market_data)

                # Canonical bar-clock sequence (ticket F-01) — the same
                # submit → step(next-open) path PaperRunner uses:
                #   bar t: signal → Order → executor.submit (arms)
                #   bar t+1: executor.step fills at t+1's OPEN
                # Only NEW bars advance the clock (a poll may repeat the
                # latest completed bar).
                new_bars = self._new_bars(market_data if isinstance(market_data, dict) else {})
                if new_bars:
                    for sym, bar in new_bars.items():
                        if "symbol" not in bar:
                            bar["symbol"] = sym
                        sigs = self.adapter.on_bar_close(bar)
                        if sigs:
                            self._submit_orders(sigs, bar)

                    # Fill orders armed on earlier bars at each new bar's open.
                    self.executor.step(
                        {sym: self._to_executor_bar(sym, bar) for sym, bar in new_bars.items()}
                    )
                    self.portfolio.sync_orders()

                    # Mark to market at the close of the bars just processed.
                    prices: Dict[str, Any] = {}
                    for sym, bar in new_bars.items():
                        price = bar.get("close") or bar.get("last") or bar.get("price")
                        if price is not None:
                            prices[sym] = price
                    if prices:
                        self.portfolio.update_prices(prices)

                # Update performance metrics
                self.performance.update_metrics(self.portfolio)

                # Save state periodically
                if self.state_manager.should_save(
                    self._last_save, self.config.system.save_state_interval_minutes
                ):
                    self.state_manager.save_state(self)
                    self._last_save = datetime.now(timezone.utc)

                # Heartbeat
                if (
                    datetime.now(timezone.utc) - self._last_heartbeat
                ).total_seconds() >= self.config.system.heartbeat_interval_seconds:
                    self._log_heartbeat()
                    self._last_heartbeat = datetime.now(timezone.utc)

                # Reset error count on success
                self._error_count = 0

                # Track loop time
                loop_time = time.perf_counter() - loop_start
                if loop_time > 1.0:
                    logger.warning("Slow loop: %.3fs", loop_time)

            except Exception as exc:
                self._error_count += 1
                logger.exception("Error in main loop (count %s): %s", self._error_count, exc)
                self.on_error(exc)

                if self._error_count >= self.config.system.max_errors_before_pause:
                    logger.error("Too many errors (%s), pausing", self._error_count)
                    self.pause()
                    # Could add alert here

            # Sleep until next iteration
            if self.config.system.loop_interval_seconds > 0:
                elapsed = time.perf_counter() - loop_start
                sleep_time = max(0, self.config.system.loop_interval_seconds - elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)

            self._loop_count += 1

    def _run_backtest_mode(self):
        """Replay historical data for backtesting."""
        logger.info("Running in backtest mode")

        if self.data_source is None:
            logger.warning("Backtest mode requires data_source, falling back to live loop")
            self.run_loop()
            return

        # For each symbol, get candles and replay
        for symbol in self.config.data.symbols:
            try:
                # data_source.get_candles signature: symbol, start, end, interval
                start = self.config.data.start_date or "2024-01-01"
                end = self.config.data.end_date or "2024-12-31"
                timeframe = self.config.data.timeframe or "day"

                candles = self.data_source.get_candles(symbol, start, end, timeframe)
                # Resume (ticket #7): skip bars already processed in a prior
                # run. Without the offset a restored engine re-replays the
                # whole history — signals would re-fire against a portfolio
                # that already holds the positions (double exposure).
                offset = int(self._processed_bars.get(symbol, 0) or 0)
                remaining = candles.iloc[offset:]
                logger.info(
                    "Replaying %s bars for %s (%s already processed%s)",
                    len(remaining),
                    symbol,
                    offset,
                    ", resuming" if offset else "",
                )

                for pos, (idx, row) in enumerate(remaining.iterrows()):
                    if not self._running:
                        break
                    if self._paused:
                        while self._paused and self._running:
                            time.sleep(0.5)

                    bar = {
                        "symbol": symbol,
                        "timestamp": idx,
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": int(row["volume"]),
                        "timeframe": timeframe,
                    }

                    # Inject into data handler for consistency
                    self.data_handler.inject_bar(bar)

                    # Process as in live loop
                    if not self.validator.validate(bar):
                        continue

                    # Canonical bar-clock sequence (ticket F-01): signal on
                    # this bar → Order → executor.submit (arms); orders armed
                    # on EARLIER bars fill at THIS bar's open; then mark to
                    # market at this bar's close. Same as PaperRunner.
                    sigs = self.adapter.on_bar_close(bar)
                    if sigs:
                        self._submit_orders(sigs, bar)
                    self.executor.step({symbol: self._to_executor_bar(symbol, bar)})
                    self.portfolio.sync_orders()
                    self.portfolio.update_prices({symbol: bar["close"]})
                    self.stop_manager.check_stops({symbol: bar})
                    self.performance.update_metrics(self.portfolio)

                    self._loop_count += 1
                    self._processed_bars[symbol] = offset + pos + 1

                    # Save state periodically
                    if self.state_manager.should_save(
                        self._last_save, self.config.system.save_state_interval_minutes
                    ):
                        self.state_manager.save_state(self)
                        self._last_save = datetime.now(timezone.utc)

                    # Small sleep to avoid spinning too fast in backtest
                    if self.config.system.loop_interval_seconds > 0:
                        time.sleep(self.config.system.loop_interval_seconds * 0.1)

            except Exception as exc:
                logger.exception("Backtest failed for %s: %s", symbol, exc)
                self.on_error(exc)

        logger.info("Backtest replay finished: %s loops", self._loop_count)

    def _log_heartbeat(self):
        try:
            equity = self.portfolio.calculate_total_equity()
            exposure = (
                self.portfolio.get_current_exposure()
                if hasattr(self.portfolio, "get_current_exposure")
                else {}
            )
            metrics = self.performance.get_metrics()

            logger.info(
                "Heartbeat loop=%s equity=%s cash=%s positions=%s exposure=%s errors=%s",
                self._loop_count,
                equity,
                self.portfolio.current_cash,
                len(self.portfolio.positions),
                exposure.get("gross_exposure_pct", "?"),
                self._error_count,
            )
        except Exception as exc:
            logger.debug("Heartbeat failed: %s", exc)

    # -- misc --------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        return {
            "running": self._running,
            "paused": self._paused,
            "loop_count": self._loop_count,
            "error_count": self._error_count,
            "portfolio": self.portfolio.summary() if hasattr(self.portfolio, "summary") else {},
            "performance": self.performance.get_metrics(),
            "config": self.config.to_dict(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def __repr__(self):
        return f"<ForwardTestingEngine portfolio={getattr(self.portfolio, 'name', '?')} running={self._running} loops={self._loop_count}>"


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Forward Testing Engine (Step 20)")
    parser.add_argument("--config", type=str, default=None, help="Path to forward_testing.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Run in dry-run mode (no trades)")
    parser.add_argument("--backtest", action="store_true", help="Run in backtest replay mode")
    parser.add_argument("--symbols", type=str, nargs="*", help="Override symbols")
    args = parser.parse_args()

    config_overrides = {}
    if args.dry_run:
        config_overrides["system"] = {"dry_run": True}
    if args.backtest:
        if "system" not in config_overrides:
            config_overrides["system"] = {}
        config_overrides["system"]["backtest_mode"] = True
    if args.symbols:
        config_overrides["data"] = {"symbols": args.symbols}

    engine = ForwardTestingEngine(config_file=args.config, config_dict=config_overrides or None)
    engine.initialize_system()
    engine.start()


if __name__ == "__main__":
    main()
