"""Serialize an options model backtest result for the web layer (plan V3 §3).

The equity :class:`BacktestAdapter` is unchanged. This adapter rides the same
payload *shape* (config/metrics/equity/drawdown/trades) so the existing charts
render, and adds the options-only blocks: ``greeks_curve``, per-structure
``trade_legs``, ``pricing_feed``, ``model_params`` and a ``disclaimer``.

A model run is never certifiable: ``certifiable`` is ``False`` and
``certification_reason`` is ``"model_pricing"`` here at the source, so every
downstream door (readiness, optimize, runner-create, UI) reads the same fact
rather than re-deriving it.
"""

from __future__ import annotations

import math
from typing import Any

from backtest.engine.option_backtest_driver import BacktestResult

__all__ = ["OptionsBacktestAdapter", "MODEL_DISCLAIMER", "CHAIN_DISCLAIMER"]

MODEL_DISCLAIMER = (
    "Model-priced sandbox: option premiums are Black-Scholes estimates off the "
    "underlying candles, not historical market quotes. Use this to compare "
    "structures, strikes and exits — not as a measurement of historical option "
    "P&L. It cannot be certified or deployed to paper/live."
)

CHAIN_DISCLAIMER = (
    "Recorded-chain replay: option premiums are captured broker quotes, screened "
    "for put-call parity and junk prints before storage. This is a measurement of "
    "historical option P&L on the stored sessions, certifiable subject to the "
    "readiness checks. Coverage is limited to what was captured — gaps are refused, "
    "never filled by a model."
)


def _f(value: Any, places: int = 2) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), places)
    except (TypeError, ValueError):
        return None


class OptionsBacktestAdapter:
    """One :class:`BacktestResult` → the JSON payload the Backtest tab renders."""

    def __init__(self, result: BacktestResult, *, config_info: dict[str, Any]) -> None:
        self._result = result
        self._info = dict(config_info)

    # -- series ----------------------------------------------------------

    def to_equity(self) -> list[dict[str, Any]]:
        return [
            {"date": p.timestamp.isoformat(), "equity": _f(p.equity)}
            for p in self._result.equity_curve
        ]

    def to_drawdown(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        peak = None
        for p in self._result.equity_curve:
            equity = float(p.equity)
            peak = equity if peak is None else max(peak, equity)
            dd = (equity - peak) / peak * 100.0 if peak > 0 else 0.0
            out.append({"date": p.timestamp.isoformat(), "drawdown_pct": round(dd, 4)})
        return out

    def _sharpe(self) -> float | None:
        equity = [float(p.equity) for p in self._result.equity_curve]
        if len(equity) < 3:
            return None
        returns = [
            (equity[i] - equity[i - 1]) / equity[i - 1]
            for i in range(1, len(equity))
            if equity[i - 1] > 0
        ]
        if len(returns) < 2:
            return None
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        std = math.sqrt(var)
        if std == 0:
            return None
        return round(mean / std * math.sqrt(252.0), 4)

    # -- metrics ---------------------------------------------------------

    def to_metrics(self) -> dict[str, Any]:
        m = dict(self._result.metrics)
        capital = float(self._info.get("capital") or 0)
        final = _f(m.get("final_equity"))
        if capital > 0 and final is not None:
            m["total_return_pct"] = round((final - capital) / capital * 100.0, 4)
        else:
            m["total_return_pct"] = None
        dd = m.get("max_drawdown")
        m["max_drawdown_pct"] = round(float(dd) * 100.0, 4) if dd is not None else None
        wr = m.get("win_rate")
        m["win_rate_pct"] = round(float(wr) * 100.0, 2) if wr is not None else None
        m["final_equity"] = final
        m["sharpe"] = self._sharpe()
        m["strategy"] = self._info.get("strategy", "")
        m["symbol"] = self._info.get("symbol", "")
        return m

    # -- full payload ----------------------------------------------------

    def to_all(self) -> dict[str, Any]:
        info = self._info
        capital = float(info.get("capital") or 0)
        config = {
            "strategy": info.get("strategy", ""),
            "symbol": info.get("symbol", ""),
            "capital": _f(capital),
            "bars": len(self._result.equity_curve),
            "strategy_params": info.get("strategy_params") or {},
            "timeframe": info.get("timeframe", "1D"),
            "from_date": info.get("from_date"),
            "to_date": info.get("to_date"),
            "engine": "options",
            "pricing_feed": self._result.pricing_feed,
            "model_params": info.get("model_params") or {},
            "exits": info.get("exits") or {},
        }
        is_model = str(self._result.pricing_feed or "").strip().lower() == "model_bs"
        return {
            "config": config,
            "metrics": self.to_metrics(),
            "equity": self.to_equity(),
            "drawdown": self.to_drawdown(),
            "trades": [r.to_dict() for r in self._result.trade_log],
            "greeks_curve": self._result.greeks_curve,
            "pricing_feed": self._result.pricing_feed,
            "model_params": info.get("model_params") or {},
            "disclaimer": MODEL_DISCLAIMER if is_model else CHAIN_DISCLAIMER,
            "certifiable": not is_model,
            "certification_reason": "model_pricing" if is_model else None,
        }
