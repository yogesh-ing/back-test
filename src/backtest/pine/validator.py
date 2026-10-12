"""Validator for converted strategies.

Runs backtests on generated strategies to ensure they produce valid results
before allowing them to be saved as plugins.
"""

from __future__ import annotations

import ast
from datetime import date, timedelta
from pathlib import Path
from tempfile import gettempdir
from typing import Tuple

from backtest.data.sources_policy import default_backtest_source


class ConvertedStrategyValidator:
    """Validate converted strategy before allowing save.

    Rules:
    1. Must pass syntax validation
    2. Must run backtest successfully
    3. Performance metrics are reported, never used as a conversion gate
    """

    def __init__(self, backtest_engine=None, min_sharpe: float = 0.5):
        self.engine = backtest_engine
        self.min_sharpe = min_sharpe

    def validate(
        self,
        strategy_code: str,
        strategy_name: str,
        symbol: str = "NIFTY",
        days: int = 30,
    ) -> Tuple[bool, dict]:
        """Validate strategy by running backtest.

        Args:
            strategy_code: Generated Python strategy code
            strategy_name: Name of the strategy
            symbol: Symbol to test on (default: NIFTY)
            days: Number of days for validation backtest

        Returns:
            Tuple of (is_valid, result_dict)

            result_dict contains:
            - validation_status: "PASS" | "FAIL"
            - backtest_metrics: dict of metrics (if available)
            - rejection_reason: str (if failed)
        """
        # 1. Save as temporary plugin. The generated code carries non-ASCII
        # arrows/dashes in its comments, so the write must name its encoding —
        # the platform default is cp1252 on Windows and refuses those chars.
        try:
            ast.parse(strategy_code)
        except SyntaxError as exc:
            return False, {
                "validation_status": "FAIL",
                "rejection_reason": f"Invalid Python syntax: {exc}",
                "backtest_metrics": {},
            }
        temp_path = Path(gettempdir()) / f"{strategy_name}_temp.py"
        temp_path.write_text(strategy_code, encoding="utf-8")

        # 2. Run backtest (if engine available)
        if self.engine is None:
            # No engine — skip backtest validation, just check syntax
            return True, {
                "validation_status": "SYNTAX_ONLY",
                "backtest_metrics": {},
                "note": "No backtest engine provided — backtest not run",
            }

        try:
            from_date = date.today() - timedelta(days=days)
            to_date = date.today()

            result = self.engine.run_backtest(
                strategy=strategy_name,
                symbol=symbol,
                from_date=from_date,
                to_date=to_date,
                initial_capital=100000,
                # A quick validation run still honours the data policy: the
                # deployment's historical source (db), or synthetic only when
                # the policy enables it. Never a literal.
                source=default_backtest_source(),
            )

        except Exception as e:
            return False, {
                "validation_status": "FAIL",
                "rejection_reason": f"Backtest failed: {e}",
                "backtest_metrics": {},
            }

        # 4. Passed
        return True, {
            "validation_status": "PASS",
            "backtest_metrics": result.get("metrics", {}),
        }
