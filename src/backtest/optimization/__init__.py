"""Parameter Optimization Engine.

Systematic search over a strategy's parameter space (grid / random /
Bayesian / genetic), constraint filtering, parallel backtests, walk-forward
validation, sensitivity + robustness analysis, presets and an audited
apply-to-runner flow. See ``docs/OPTIMIZATION-ENGINE.md``.

Layers (each importable on its own):

* :mod:`.config`       — parse/validate the PRD ``OptimizationConfig``
* :mod:`.scoring`      — objective functions + constraint checks
* :mod:`.evaluator`    — one backtest → standardized metrics; cached pool
* :mod:`.methods`      — search methods over a discrete grid
* :mod:`.walk_forward` — train/test splits, degradation, overfit verdict
* :mod:`.analysis`     — heatmaps, sensitivity, robustness, warning signs
* :mod:`.store`        — persistence (optimization_* tables)
* :mod:`.service`      — job lifecycle, progress, apply/rollback, presets
"""

from backtest.optimization.config import (
    ConfigValidationError,
    OptimizationConfig,
    ParameterSpec,
    default_space,
    parse_config,
)

__all__ = [
    "ConfigValidationError",
    "OptimizationConfig",
    "ParameterSpec",
    "default_space",
    "parse_config",
]
