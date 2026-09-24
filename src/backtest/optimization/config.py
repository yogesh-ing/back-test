"""Optimization config: parsing + validation of the PRD ``OptimizationConfig``.

Accepts the PRD's camelCase JSON (``strategyId``, ``objectiveFunction``,
``backtestConfig``, ``walkForward.trainPeriodDays`` ...) and the snake_case
equivalents, and normalises both into frozen dataclasses. Every problem the
user can fix is raised as :class:`ConfigValidationError` carrying *all*
field errors at once, so the setup page can highlight every bad input in a
single round trip.

Units (single source of truth for the whole engine)
---------------------------------------------------
* ``max_drawdown`` constraint values are **percent magnitudes**: ``15`` means
  "drawdown no worse than -15 %" (what the setup page shows). A PRD-style
  negative decimal (``-0.15``) is accepted and converted.
* ``win_rate`` constraint values are percentages (``45`` = 45 %); decimals
  ``<= 1`` are treated as fractions and converted.
* ``min_trades`` counts round trips (the engine's ``num_trades``).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from backtest.strategy.registry import get_strategy

OBJECTIVES = ("sharpe", "sortino", "calmar", "total_return", "profit_factor", "expectancy")
METHODS = ("grid", "random", "bayesian", "genetic")
ENGINES = ("driver", "quick_screen", "options")
CONSTRAINT_METRICS = (
    "max_drawdown",
    "min_trades",
    "win_rate",
    "sharpe",
    "profit_factor",
    "total_return",
)
OPERATORS = ("<", "<=", ">", ">=")

#: PRD guardrail: "Don't optimize >8 parameters (overfitting risk)".
MAX_OPTIMIZED_PARAMS = 8
#: Hard ceiling on an exhaustive grid (use random/bayesian beyond this).
MAX_GRID_COMBINATIONS = 50_000
#: Hard ceiling on evaluations for the sampling methods.
MAX_EVALUATIONS = 5_000
#: PRD guardrail: "Don't use <6 months data (insufficient sample)".
MIN_RECOMMENDED_DAYS = 182

#: Option-engine knobs that can be optimized next to strategy params
#: (``engine.<name>``). ``delta_target`` is the Greeks-based strike selector.
OPTION_ENGINE_PARAMS: dict[str, dict[str, Any]] = {
    "engine.delta_target": {
        "type": "float", "min": 0.05, "max": 0.95, "default": 0.35,
        "label": "Strike delta target", "suggested_step": 0.05,
        "tooltip": "Delta selector: target |delta| of the bought leg (forces selector=delta).",
    },
    "engine.spread_threshold": {
        "type": "float", "min": 0.0, "max": 1.0, "default": 0.7,
        "label": "Outright-vs-spread conviction", "suggested_step": 0.05,
        "tooltip": "Conviction at/above which an outright option is bought instead of a spread.",
    },
    "engine.max_open_structures": {
        "type": "int", "min": 1, "max": 10, "default": 1,
        "label": "Max open structures", "suggested_step": 1,
        "tooltip": "Concurrent open option structures (PRD risk.max_positions).",
    },
}


class ConfigValidationError(ValueError):
    """Raised with a ``{field: message}`` map of every validation problem."""

    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = dict(errors)
        joined = "; ".join(f"{k}: {v}" for k, v in self.errors.items())
        super().__init__(joined or "invalid optimization config")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


def _decimals(value: float) -> int:
    """Number of decimal places needed to represent ``value`` exactly."""
    try:
        exp = Decimal(str(value)).normalize().as_tuple().exponent
    except InvalidOperation:  # pragma: no cover - defensive
        return 6
    return max(0, -int(exp)) if isinstance(exp, int) else 6


@dataclass(frozen=True)
class ParameterSpec:
    """One tunable parameter: bounds, step, current value, optimize flag."""

    name: str
    type: str  # 'int' | 'float'
    min: float
    max: float
    step: float
    current: float
    optimize: bool = True

    @property
    def precision(self) -> int:
        return max(_decimals(self.step), _decimals(self.min))

    def values(self) -> list[float | int]:
        """Inclusive grid ``min, min+step, ... <= max`` (Decimal-exact)."""
        if not self.optimize:
            return [self.cast(self.current)]
        lo, st, hi = Decimal(str(self.min)), Decimal(str(self.step)), Decimal(str(self.max))
        count = int((hi - lo) / st) + 1
        out: list[float | int] = []
        for i in range(max(count, 1)):
            v = lo + st * i
            if v > hi:
                break
            out.append(self.cast(float(v)))
        return out

    def size(self) -> int:
        if not self.optimize:
            return 1
        lo, st, hi = Decimal(str(self.min)), Decimal(str(self.step)), Decimal(str(self.max))
        return int((hi - lo) / st) + 1

    def cast(self, value: float) -> float | int:
        if self.type == "int":
            return int(round(value))
        return round(float(value), self.precision)

    def snap(self, value: float) -> float | int:
        """Nearest grid value (clamped to bounds)."""
        if not self.optimize:
            return self.cast(self.current)
        n = self.size()
        idx = int(round((float(value) - self.min) / self.step)) if self.step else 0
        idx = min(max(idx, 0), n - 1)
        return self.cast(float(Decimal(str(self.min)) + Decimal(str(self.step)) * idx))

    def index_of(self, value: float) -> int:
        if not self.optimize:
            return 0
        idx = int(round((float(value) - self.min) / self.step)) if self.step else 0
        return min(max(idx, 0), self.size() - 1)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Constraint:
    metric: str
    operator: str
    value: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BacktestSettings:
    symbol: str
    start_date: str
    end_date: str
    initial_capital: float
    timeframe: str = "1day"
    engine: str = "driver"
    mode: str = "paper"  # provenance tag: which execution data the run models
    source: str | None = None  # data source override (defaults to the app's)
    selector_type: str = "atm"  # options engine only

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WalkForwardSettings:
    enabled: bool = False
    train_period_days: int = 60
    test_period_days: int = 30
    step_days: int = 30
    #: Evaluation budget per split (full grid used when it fits).
    max_evals_per_split: int = 150
    #: Average (train - test) objective degradation above which the run is
    #: flagged as overfit (PRD rule of thumb: 0.3 Sharpe).
    overfit_threshold: float = 0.3

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MethodSettings:
    """Budgets for the sampling methods (ignored by grid)."""

    n_samples: int | None = None  # random: default 20 % of the grid
    n_calls: int = 60  # bayesian
    n_initial: int | None = None  # bayesian random warm-up points
    population: int = 20  # genetic
    generations: int = 10  # genetic
    seed: int = 42

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OptimizationConfig:
    strategy_id: str
    objective: str
    parameters: tuple[ParameterSpec, ...]
    constraints: tuple[Constraint, ...]
    method: str
    backtest: BacktestSettings
    walk_forward: WalkForwardSettings
    method_settings: MethodSettings = field(default_factory=MethodSettings)
    bucket_id: str | None = None
    warnings: tuple[str, ...] = ()

    # -- derived ------------------------------------------------------------

    @property
    def optimized(self) -> tuple[ParameterSpec, ...]:
        return tuple(p for p in self.parameters if p.optimize)

    @property
    def fixed_params(self) -> dict[str, Any]:
        return {p.name: p.cast(p.current) for p in self.parameters if not p.optimize}

    @property
    def baseline_params(self) -> dict[str, Any]:
        return {p.name: p.cast(p.current) for p in self.parameters}

    def grid_size(self) -> int:
        total = 1
        for p in self.optimized:
            total *= p.size()
        return total

    def planned_evaluations(self) -> int:
        """How many backtests the main search will run (excl. WF/sensitivity)."""
        grid = self.grid_size()
        ms = self.method_settings
        if self.method == "grid":
            return grid
        if self.method == "random":
            return min(grid, ms.n_samples or default_random_samples(grid))
        if self.method == "bayesian":
            return min(grid, ms.n_calls)
        return min(grid, ms.population * ms.generations)

    def to_dict(self) -> dict[str, Any]:
        """Canonical camelCase document (what is stored and echoed to the UI)."""
        return {
            "strategyId": self.strategy_id,
            "objectiveFunction": self.objective,
            "parameters": [p.to_dict() for p in self.parameters],
            "constraints": [c.to_dict() for c in self.constraints],
            "method": self.method,
            "methodSettings": self.method_settings.to_dict(),
            "backtestConfig": {
                "symbol": self.backtest.symbol,
                "startDate": self.backtest.start_date,
                "endDate": self.backtest.end_date,
                "initialCapital": self.backtest.initial_capital,
                "timeframe": self.backtest.timeframe,
                "engine": self.backtest.engine,
                "mode": self.backtest.mode,
                "source": self.backtest.source,
                "selectorType": self.backtest.selector_type,
            },
            "walkForward": {
                "enabled": self.walk_forward.enabled,
                "trainPeriodDays": self.walk_forward.train_period_days,
                "testPeriodDays": self.walk_forward.test_period_days,
                "stepDays": self.walk_forward.step_days,
                "maxEvalsPerSplit": self.walk_forward.max_evals_per_split,
                "overfitThreshold": self.walk_forward.overfit_threshold,
            },
            "bucketId": self.bucket_id,
        }


def default_random_samples(grid: int) -> int:
    """PRD: random search ≈ 20 % of the grid (at least 10, capped)."""
    return int(min(MAX_EVALUATIONS, max(10, math.ceil(grid * 0.2))))


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _pick(doc: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if isinstance(doc, dict) and key in doc and doc[key] is not None:
            return doc[key]
    return default


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _parse_date(value: Any) -> str | None:
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    if not value:
        return None
    text = str(value).strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def strategy_param_schema(strategy_id: str, engine: str | None = None) -> dict[str, dict]:
    """The strategy's declared schema (+ option-engine knobs when relevant)."""
    cls = get_strategy(strategy_id)
    schema = dict(cls.param_schema())
    if engine == "options" or (engine is None and is_option_strategy(strategy_id)):
        schema.update({k: dict(v) for k, v in OPTION_ENGINE_PARAMS.items()})
    return schema


def is_option_strategy(strategy_id: str) -> bool:
    from backtest.strategy.registry import signal_kind

    try:
        return signal_kind(get_strategy(strategy_id)) == "option"
    except Exception:  # noqa: BLE001 - unknown strategies handled by the caller
        return False


def suggested_step(spec: dict[str, Any]) -> float:
    """A sensible default step for a numeric schema entry."""
    if spec.get("suggested_step"):
        return float(spec["suggested_step"])
    lo, hi = spec.get("min"), spec.get("max")
    if spec.get("type") == "int":
        if lo is not None and hi is not None:
            return float(max(1, int(round((hi - lo) / 20))))
        return 1.0
    if lo is not None and hi is not None and hi > lo:
        raw = (hi - lo) / 20
        magnitude = 10 ** math.floor(math.log10(raw))
        for mult in (1, 2, 2.5, 5, 10):
            if raw <= mult * magnitude:
                return float(round(mult * magnitude, 10))
    return 0.1


def _nice_step(raw: float) -> float:
    """Round ``raw`` up to 1/2/2.5/5 × 10^k."""
    if raw <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(raw))
    for mult in (1, 2, 2.5, 5, 10):
        if raw <= mult * magnitude + 1e-12:
            return float(round(mult * magnitude, 10))
    return float(10 * magnitude)  # pragma: no cover


def default_space(strategy_id: str, engine: str | None = None,
                  target_points: int = 10) -> list[dict[str, Any]]:
    """Setup-page defaults: every numeric param, a range around its default.

    The suggested range is the default ±50 % (at least ±3 steps' worth for
    small integers), clipped to the declared bounds, with a "nice" step that
    gives roughly ``target_points`` values — a first run explores the
    neighbourhood of what the strategy currently uses rather than the whole
    legal range. The default always lies on the grid.
    """
    rows: list[dict[str, Any]] = []
    for name, spec in strategy_param_schema(strategy_id, engine).items():
        ptype = spec.get("type")
        if ptype not in ("int", "float"):
            continue
        default = spec.get("default")
        if default is None:
            continue
        lo_decl, hi_decl = spec.get("min"), spec.get("max")
        base = float(default)
        span = abs(base) * 0.5
        if ptype == "int":
            span = max(span, 3.0)
        elif span == 0:
            span = float(spec.get("suggested_step") or 0.1) * 5
        lo, hi = base - span, base + span
        if lo_decl is not None:
            lo = max(lo, float(lo_decl))
        if hi_decl is not None:
            hi = min(hi, float(hi_decl))
        if spec.get("suggested_step"):
            step = float(spec["suggested_step"])
        else:
            step = _nice_step((hi - lo) / max(target_points - 1, 1))
        if ptype == "int":
            step = float(max(1, int(round(step))))
        # snap the range onto a grid through the default
        n_lo = math.floor(round((base - lo) / step, 9))
        n_hi = math.floor(round((hi - base) / step, 9))
        lo, hi = base - n_lo * step, base + n_hi * step
        if ptype == "int":
            lo, hi, step = int(round(lo)), int(round(hi)), int(step)
        else:
            prec = max(_decimals(step), _decimals(base), 2)
            lo, hi = round(lo, prec), round(hi, prec)
        rows.append(
            {
                "name": name,
                "label": spec.get("label") or name,
                "tooltip": spec.get("tooltip", ""),
                "type": ptype,
                "current": default,
                "min": lo,
                "max": hi,
                "step": step,
                "bound_min": lo_decl,
                "bound_max": hi_decl,
                "optimize": not name.startswith("engine."),
                "engine_param": name.startswith("engine."),
            }
        )
    if rows and not any(r["optimize"] for r in rows):
        # no strategy-level numeric params (e.g. atm_instant_buy): the option
        # engine knobs are the only thing to tune — pre-select them.
        for r in rows:
            r["optimize"] = True
    return rows


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------


def parse_config(doc: dict[str, Any], *, default_source: str | None = None) -> OptimizationConfig:
    """Validate a raw config document; raise :class:`ConfigValidationError`."""
    if not isinstance(doc, dict):
        raise ConfigValidationError({"config": "must be a JSON object"})
    errors: dict[str, str] = {}
    warnings: list[str] = []

    # -- strategy -------------------------------------------------------------
    strategy_id = str(_pick(doc, "strategyId", "strategy_id", "strategy", default="")).strip()
    strategy_cls = None
    if not strategy_id:
        errors["strategyId"] = "strategy is required"
    else:
        try:
            strategy_cls = get_strategy(strategy_id)
        except KeyError:
            errors["strategyId"] = f"unknown strategy: {strategy_id}"

    # -- objective / method ---------------------------------------------------
    objective = str(_pick(doc, "objectiveFunction", "objective_function", "objective",
                          default="sharpe")).strip().lower()
    if objective not in OBJECTIVES:
        errors["objectiveFunction"] = f"must be one of {', '.join(OBJECTIVES)}"
    method = str(_pick(doc, "method", default="grid")).strip().lower()
    if method not in METHODS:
        errors["method"] = f"must be one of {', '.join(METHODS)}"

    # -- backtest settings ----------------------------------------------------
    bt = _pick(doc, "backtestConfig", "backtest_config", "backtest", default={}) or {}
    engine = str(_pick(bt, "engine", default="")).strip().lower()
    if not engine:
        engine = "options" if (strategy_cls and is_option_strategy(strategy_id)) else "driver"
    if engine not in ENGINES:
        errors["backtestConfig.engine"] = f"must be one of {', '.join(ENGINES)}"
    start = _parse_date(_pick(bt, "startDate", "start_date", "from_date", "from"))
    end = _parse_date(_pick(bt, "endDate", "end_date", "to_date", "to"))
    if not start:
        errors["backtestConfig.startDate"] = "start date is required (YYYY-MM-DD)"
    if not end:
        errors["backtestConfig.endDate"] = "end date is required (YYYY-MM-DD)"
    if start and end and start >= end:
        errors["backtestConfig.endDate"] = "end date must be after start date"
    capital = _num(_pick(bt, "initialCapital", "initial_capital", "capital", default=100_000))
    if capital is None or capital <= 0:
        errors["backtestConfig.initialCapital"] = "initial capital must be a positive number"
    symbol = str(_pick(bt, "symbol", default="") or "").strip().upper()
    if not symbol:
        symbol = "NIFTY" if engine == "options" else "DEMO"
    timeframe = str(_pick(bt, "timeframe", default="1day")).strip()
    from backtest.engine.backtest_runner import resolve_interval

    timeframe = resolve_interval(timeframe, log_prefix="[optimize]")
    mode = str(_pick(bt, "mode", default="paper")).strip().lower()
    if mode not in ("paper", "live"):
        errors["backtestConfig.mode"] = "mode must be paper or live"
    source = _pick(bt, "source", default=None) or default_source
    selector_type = str(_pick(bt, "selectorType", "selector_type", default="atm")).strip().lower()
    if selector_type not in ("atm", "delta", "fixed_distance"):
        errors["backtestConfig.selectorType"] = "selector must be atm, delta or fixed_distance"

    if start and end and start < end:
        days = (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days
        if days < MIN_RECOMMENDED_DAYS:
            warnings.append(
                f"Only {days} days of data — the PRD recommends at least 6 months "
                "to avoid fitting noise."
            )

    # -- parameters -------------------------------------------------------------
    raw_params = _pick(doc, "parameters", "param_space", default=[]) or []
    if isinstance(raw_params, dict):  # {name: {...}} form
        raw_params = [{"name": k, **(v or {})} for k, v in raw_params.items()]
    schema: dict[str, dict] = {}
    if strategy_cls is not None:
        schema = strategy_param_schema(strategy_id, engine)
    params: list[ParameterSpec] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_params if isinstance(raw_params, list) else []):
        key = f"parameters[{i}]"
        if not isinstance(raw, dict):
            errors[key] = "must be an object"
            continue
        name = str(raw.get("name", "")).strip()
        if not name:
            errors[key] = "name is required"
            continue
        key = f"parameters.{name}"
        if name in seen:
            errors[key] = "duplicate parameter"
            continue
        seen.add(name)
        spec = schema.get(name)
        if strategy_cls is not None and spec is None:
            errors[key] = f"'{name}' is not a parameter of {strategy_id}"
            continue
        ptype = str(raw.get("type") or (spec or {}).get("type") or "float").lower()
        if spec and spec.get("type") in ("int", "float"):
            ptype = spec["type"]
        if ptype not in ("int", "float"):
            errors[key] = f"only int/float parameters can be optimized (got {ptype})"
            continue
        optimize = bool(raw.get("optimize", True))
        lo, hi, step = _num(raw.get("min")), _num(raw.get("max")), _num(raw.get("step"))
        current = _num(raw.get("current"))
        if current is None and spec is not None:
            current = _num(spec.get("default"))
        if optimize:
            if lo is None or hi is None or step is None:
                errors[key] = "min, max and step are required"
                continue
            if step <= 0:
                errors[key] = "step must be > 0"
                continue
            if lo > hi:
                errors[key] = "min must be <= max"
                continue
            if ptype == "int" and not all(float(v).is_integer() for v in (lo, hi, step)):
                errors[key] = "integer parameter needs integer min/max/step"
                continue
            if spec:
                b_lo, b_hi = spec.get("min"), spec.get("max")
                if b_lo is not None and lo < b_lo:
                    errors[key] = f"min {lo:g} is below the strategy's allowed minimum {b_lo}"
                    continue
                if b_hi is not None and hi > b_hi:
                    errors[key] = f"max {hi:g} is above the strategy's allowed maximum {b_hi}"
                    continue
        else:
            lo = hi = current if current is not None else 0.0
            step = step or 1.0
        if current is None:
            current = lo if lo is not None else 0.0
        params.append(
            ParameterSpec(
                name=name, type=ptype, min=float(lo), max=float(hi), step=float(step),
                current=float(current), optimize=optimize,
            )
        )
    optimized = [p for p in params if p.optimize]
    if not raw_params:
        errors["parameters"] = "select at least one parameter to optimize"
    elif not optimized and not any(k.startswith("parameters") for k in errors):
        errors["parameters"] = "select at least one parameter to optimize"
    elif len(optimized) > MAX_OPTIMIZED_PARAMS:
        errors["parameters"] = (
            f"{len(optimized)} parameters selected — optimize at most {MAX_OPTIMIZED_PARAMS} "
            "(overfitting risk)"
        )
    elif len(optimized) > 5 and method == "grid":
        warnings.append("More than 5 parameters on a grid — Bayesian search is usually faster.")

    # -- constraints ------------------------------------------------------------
    constraints: list[Constraint] = []
    raw_constraints = _pick(doc, "constraints", default=[]) or []
    for i, raw in enumerate(raw_constraints if isinstance(raw_constraints, list) else []):
        key = f"constraints[{i}]"
        if not isinstance(raw, dict):
            errors[key] = "must be an object"
            continue
        if raw.get("enabled") is False:
            continue
        metric = str(raw.get("metric", "")).strip().lower()
        if metric == "total_trades":
            metric = "min_trades"
        op = str(raw.get("operator", "")).strip()
        value = _num(raw.get("value"))
        if metric not in CONSTRAINT_METRICS:
            errors[key] = f"metric must be one of {', '.join(CONSTRAINT_METRICS)}"
            continue
        if op not in OPERATORS:
            errors[key] = f"operator must be one of {' '.join(OPERATORS)}"
            continue
        if value is None:
            errors[key] = "value must be a number"
            continue
        if metric == "max_drawdown":
            # Stored as a percent MAGNITUDE compared with the operator as
            # written: "max_drawdown < 15" = never worse than -15 %.
            # The PRD's signed form ("> -0.15") says the same thing about the
            # negative drawdown, so a negative value flips the operator.
            if value < 0:
                op = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}[op]
            value = abs(value) * (100.0 if abs(value) <= 1 else 1.0)
        elif metric == "win_rate" and 0 < value <= 1:
            value = value * 100.0
        constraints.append(Constraint(metric=metric, operator=op, value=float(value)))

    # -- method settings ---------------------------------------------------------
    ms_doc = _pick(doc, "methodSettings", "method_settings", default={}) or {}

    def _int_setting(key_camel: str, key_snake: str, default: int | None,
                     lo: int, hi: int) -> int | None:
        raw = _pick(ms_doc, key_camel, key_snake, default=None)
        if raw is None:
            raw = _pick(doc, key_camel, key_snake, default=None)
        if raw is None:
            return default
        value = _num(raw)
        if value is None or not float(value).is_integer() or not lo <= value <= hi:
            errors[f"methodSettings.{key_camel}"] = f"must be an integer in [{lo}, {hi}]"
            return default
        return int(value)

    method_settings = MethodSettings(
        n_samples=_int_setting("nSamples", "n_samples", None, 1, MAX_EVALUATIONS),
        n_calls=_int_setting("nCalls", "n_calls", 60, 5, 1000) or 60,
        n_initial=_int_setting("nInitial", "n_initial", None, 2, 200),
        population=_int_setting("population", "population", 20, 4, 200) or 20,
        generations=_int_setting("generations", "generations", 10, 1, 200) or 10,
        seed=_int_setting("seed", "seed", 42, 0, 2**31 - 1) or 42,
    )

    # -- walk-forward -------------------------------------------------------------
    wf_doc = _pick(doc, "walkForward", "walk_forward", default={}) or {}
    wf_enabled = bool(_pick(wf_doc, "enabled", default=False))

    def _wf_int(camel: str, snake: str, default: int, lo: int, hi: int) -> int:
        raw = _pick(wf_doc, camel, snake, default=default)
        value = _num(raw)
        if value is None or not float(value).is_integer() or not lo <= value <= hi:
            if wf_enabled:
                errors[f"walkForward.{camel}"] = f"must be an integer in [{lo}, {hi}]"
            return default
        return int(value)

    threshold = _num(_pick(wf_doc, "overfitThreshold", "overfit_threshold", default=0.3))
    walk_forward = WalkForwardSettings(
        enabled=wf_enabled,
        train_period_days=_wf_int("trainPeriodDays", "train_period_days", 60, 5, 3650),
        test_period_days=_wf_int("testPeriodDays", "test_period_days", 30, 2, 3650),
        step_days=_wf_int("stepDays", "step_days", 30, 1, 3650),
        max_evals_per_split=_wf_int("maxEvalsPerSplit", "max_evals_per_split", 150, 5, 5000),
        overfit_threshold=threshold if threshold is not None and threshold >= 0 else 0.3,
    )
    if wf_enabled and start and end and start < end:
        days = (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days + 1
        if walk_forward.train_period_days + walk_forward.test_period_days > days:
            errors["walkForward.trainPeriodDays"] = (
                f"train + test ({walk_forward.train_period_days + walk_forward.test_period_days}"
                f" days) exceeds the backtest period ({days} days)"
            )
    if not wf_enabled:
        warnings.append("Walk-forward validation is off — overfitting will not be detected.")

    if errors:
        raise ConfigValidationError(errors)

    cfg = OptimizationConfig(
        strategy_id=strategy_id,
        objective=objective,
        parameters=tuple(params),
        constraints=tuple(constraints),
        method=method,
        backtest=BacktestSettings(
            symbol=symbol, start_date=str(start), end_date=str(end),
            initial_capital=float(capital or 0), timeframe=timeframe, engine=engine,
            mode=mode, source=source, selector_type=selector_type,
        ),
        walk_forward=walk_forward,
        method_settings=method_settings,
        bucket_id=_pick(doc, "bucketId", "bucket_id", default=None),
        warnings=tuple(warnings),
    )
    grid = cfg.grid_size()
    if method == "grid" and grid > MAX_GRID_COMBINATIONS:
        raise ConfigValidationError(
            {
                "method": (
                    f"grid has {grid:,} combinations (max {MAX_GRID_COMBINATIONS:,}) — "
                    "widen the steps or use random/bayesian/genetic search"
                )
            }
        )
    return cfg


def params_key(params: dict[str, Any], names: Iterable[str] | None = None) -> tuple:
    """Hashable identity of a parameter set (stable across dict orderings)."""
    keys = sorted(names) if names is not None else sorted(params)
    return tuple((k, params.get(k)) for k in keys)
