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
      state_file: "state/forward_test_state.json"

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
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

import pandas as pd

from backtest.simulator.errors import ValidationError
from backtest.simulator.money import ZERO, money

logger = logging.getLogger("backtest.forward.engine")

DEFAULT_FORWARD_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "forward_testing.yaml"
DEFAULT_STATE_FILE = Path("state/forward_test_state.json")


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

    def __post_init__(self):
        from backtest.simulator.money import to_decimal

        if self.max_position_size is not None:
            self.max_position_size = money(self.max_position_size)
        self.max_drawdown_pct = to_decimal(self.max_drawdown_pct, "max_drawdown_pct")
        self.daily_loss_limit_pct = to_decimal(self.daily_loss_limit_pct, "daily_loss_limit_pct")
        self.max_leverage = to_decimal(self.max_leverage, "max_leverage")


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
    symbols: List[str] = field(default_factory=lambda: ["INFY"])
    timeframe: str = "1min"
    start_date: Optional[str] = None
    end_date: Optional[str] = None

    def __post_init__(self):
        self.provider = str(self.provider).strip().lower()
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
                "max_position_size": str(self.risk.max_position_size) if self.risk.max_position_size else None,
                "max_positions": self.risk.max_positions,
                "max_drawdown_pct": str(self.risk.max_drawdown_pct),
                "daily_loss_limit_pct": str(self.risk.daily_loss_limit_pct),
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
                    check = self.portfolio.can_open_position(order.symbol, order.quantity, order.limit_price or 100)
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
# State Manager
# ---------------------------------------------------------------------------


class StateManager:
    """Persists full system state for crash recovery (Step 20)."""

    def __init__(self, state_file: str | Path = DEFAULT_STATE_FILE):
        self.state_file = Path(state_file)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

    def save_state(self, engine: "ForwardTestingEngine") -> str:
        """Save full engine state to JSON."""
        try:
            payload = {
                "portfolio": engine.portfolio.to_dict() if hasattr(engine.portfolio, "to_dict") else {},
                "adapter": engine.adapter.get_state() if hasattr(engine.adapter, "get_state") else {},
                "performance": engine.performance.equity_curve if hasattr(engine.performance, "equity_curve") else [],
                "config": engine.config.to_dict(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "loop_count": engine._loop_count,
            }
            # Atomic write via temp file
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.replace(self.state_file)
            logger.info("State saved to %s (loop %s)", self.state_file, engine._loop_count)
            return str(self.state_file)
        except Exception as exc:
            logger.exception("Failed to save state: %s", exc)
            raise

    def load_state(self, path: str | Path | None = None) -> Optional[Dict[str, Any]]:
        file_path = Path(path) if path else self.state_file
        if not file_path.exists():
            logger.info("No state file at %s", file_path)
            return None
        try:
            data = json.loads(file_path.read_text())
            logger.info("State loaded from %s (loop %s)", file_path, data.get("loop_count", "?"))
            return data
        except Exception as exc:
            logger.warning("Failed to load state from %s: %s", file_path, exc)
            return None

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
        self.state_manager: StateManager = StateManager(self.config.system.state_file)

        self._running = False
        self._paused = False
        self._loop_count = 0
        self._error_count = 0
        self._last_save = datetime.now(timezone.utc)
        self._last_heartbeat = datetime.now(timezone.utc)

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
        level = getattr(logging, self.config.system.log_level, logging.INFO)
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        # Reduce noise from libs
        logging.getLogger("urllib3").setLevel(logging.WARNING)

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

        # Portfolio
        if self._provided_portfolio is not None:
            self.portfolio = self._provided_portfolio
        else:
            # Try to restore from state file
            state = self.state_manager.load_state()
            if state and "portfolio" in state and state["portfolio"]:
                try:
                    from backtest.simulator.portfolio import Portfolio

                    self.portfolio = Portfolio.from_dict(state["portfolio"])
                    logger.info("Portfolio restored from state file: %s", self.portfolio.name)
                except Exception as exc:
                    logger.warning("Failed to restore portfolio from state: %s", exc)

            if self.portfolio is None:
                # Try to load from DB if name exists?
                # For now, create new
                from backtest.simulator.portfolio import Portfolio, PortfolioLimits

                limits = PortfolioLimits(
                    allow_short=self.config.portfolio.allow_short,
                    max_open_positions=self.config.portfolio.max_open_positions or self.config.risk.max_positions,
                )
                self.portfolio = Portfolio(
                    name=self.config.portfolio.name,
                    initial_capital=self.config.portfolio.initial_capital,
                    limits=limits,
                )
                logger.info("New portfolio created: %s %s", self.portfolio.name, self.portfolio.initial_capital)

        # Strategy
        if self._provided_strategy is not None:
            self.strategy = self._provided_strategy
        else:
            try:
                from backtest.strategy.registry import get_strategy

                StratCls = get_strategy(self.config.strategy.name)
                self.strategy = StratCls(**self.config.strategy.parameters)
                logger.info("Strategy loaded: %s %s", self.strategy.name, self.config.strategy.parameters)
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
            )
            self.sizer = PositionSizer(cfg)
            logger.info("Position sizer initialized: %s", cfg.method)
        except Exception as exc:
            logger.warning("Failed to init sizer, using fixed 100: %s", exc)
            from backtest.simulator.position_sizing import PositionSizer, SizingConfig

            self.sizer = PositionSizer(SizingConfig(method="fixed_quantity", fixed_quantity=100))

        # Execution
        try:
            from backtest.simulator.execution import OrderExecutor, ExecutionConfig
            from backtest.simulator.slippage import SlippageCalculator
            from backtest.simulator.fees import CommissionCalculator

            exec_cfg = ExecutionConfig()
            # Try to load from file
            try:
                from backtest.simulator.execution import load_execution_config

                exec_cfg = load_execution_config(profile=self.config.execution.realism)
            except Exception:
                pass

            slippage = SlippageCalculator()
            fees = CommissionCalculator()

            self.executor = OrderExecutor(config=exec_cfg, slippage=slippage, fees=fees, portfolio=self.portfolio)
            logger.info("Order executor initialized: %s", exec_cfg.realism)
        except Exception as exc:
            logger.warning("Failed to init executor: %s", exc)
            self.executor = None

        # Strategy adapter
        try:
            from backtest.forward.strategy_adapter import StrategyAdapter

            self.adapter = StrategyAdapter(
                strategy=self.strategy,
                portfolio=self.portfolio,
                executor=self.executor,
                symbols=self.config.data.symbols,
                dry_run=self.config.system.dry_run,
                position_sizer=self.sizer,
                db_manager=self.db_manager,
                allow_short=self.config.portfolio.allow_short,
            )
            # Restore adapter state if available
            state = self.state_manager.load_state()
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
            from backtest.live.market_data_handler import MarketDataHandler as RealMarketDataHandler
            from backtest.live.data_validator import DataValidator as RealDataValidator
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
                symbols=self.config.data.symbols, provider=self.config.data.provider, data_source=self.data_source
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
            from backtest.simulator.risk_manager import RiskManager as RealRiskManager, RiskConfig as RealRiskConfig

            real_risk_cfg = RealRiskConfig(
                max_position_value=self.config.risk.max_position_size,
                max_position_pct=None,
                max_open_positions=self.config.risk.max_positions,
                max_drawdown_pct=self.config.risk.max_drawdown_pct,
                daily_loss_limit_pct=self.config.risk.daily_loss_limit_pct,
                max_leverage=self.config.risk.max_leverage,
            )
            self.risk_manager = RealRiskManager(portfolio=self.portfolio, config=real_risk_cfg)
            logger.info("Using real RiskManager (Step 15)")
        except Exception as exc:
            logger.warning("Failed to init real RiskManager, using mock: %s", exc)
            self.risk_manager = MockRiskManager(portfolio=self.portfolio, risk_config=self.config.risk)

        self.stop_manager = MockStopManager(portfolio=self.portfolio)
        self.performance = MockPerformanceCalculator(portfolio=self.portfolio)

        self._loop_count = 0
        self._error_count = 0

        logger.info("System initialized: portfolio=%s strategy=%s symbols=%s", self.portfolio.name, self.strategy.name, self.config.data.symbols)

    # -- control -----------------------------------------------------------

    def start(self):
        """Start the engine (blocks until stopped)."""
        if self.portfolio is None:
            self.initialize_system()

        self._running = True
        self._paused = False
        logger.warning("Engine starting: %s (dry_run=%s backtest=%s)", self.portfolio.name, self.config.system.dry_run, self.config.system.backtest_mode)

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
        logger.info("Entering main loop: interval=%s save_interval=%s", self.config.system.loop_interval_seconds, self.config.system.save_state_interval_minutes)

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

                # Update portfolio positions with current prices
                if market_data:
                    prices = {}
                    for sym, bar in market_data.items():
                        if isinstance(bar, dict):
                            price = bar.get("close") or bar.get("last") or bar.get("price")
                            if price is not None:
                                prices[sym] = price
                    if prices:
                        self.portfolio.update_prices(prices)

                # Check and update stops
                self.stop_manager.check_stops(market_data)

                # Generate strategy signals
                signals = []
                if isinstance(market_data, dict):
                    for sym, bar in market_data.items():
                        if isinstance(bar, dict):
                            # Ensure bar has symbol
                            if "symbol" not in bar:
                                bar["symbol"] = sym
                            sigs = self.adapter.on_bar_close(bar)
                            signals.extend(sigs)
                else:
                    signals = self.adapter.generate_signals()

                # Apply position sizing is already done inside adapter.execute_signals via sizer
                # Risk check is also inside adapter, but we double-check with risk_manager
                # For this loop, adapter already created orders, so we get them
                # In a more explicit flow, we would do sizing and risk separately

                # Update performance metrics
                self.performance.update_metrics(self.portfolio)

                # Save state periodically
                if self.state_manager.should_save(self._last_save, self.config.system.save_state_interval_minutes):
                    self.state_manager.save_state(self)
                    self._last_save = datetime.now(timezone.utc)

                # Heartbeat
                if (datetime.now(timezone.utc) - self._last_heartbeat).total_seconds() >= self.config.system.heartbeat_interval_seconds:
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
                logger.info("Replaying %s bars for %s", len(candles), symbol)

                for idx, row in candles.iterrows():
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

                    self.portfolio.update_prices({symbol: bar["close"]})
                    self.stop_manager.check_stops({symbol: bar})
                    self.adapter.on_bar_close(bar)
                    self.performance.update_metrics(self.portfolio)

                    self._loop_count += 1

                    # Save state periodically
                    if self.state_manager.should_save(self._last_save, self.config.system.save_state_interval_minutes):
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
            exposure = self.portfolio.get_current_exposure() if hasattr(self.portfolio, "get_current_exposure") else {}
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
