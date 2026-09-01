"""FrameSource — a DataSource adapter over an already-fetched candle frame.

Canonical home (ticket #6): every bar-replay run that already holds its
candles (walk-forward buckets, forward-session replay, the canonical
:func:`backtest.engine.backtest_runner.run_backtest`) wraps them in this one
adapter instead of re-declaring a private clone. ``backtest.forward.paper_runner``
keeps ``_FrameSource`` as a compatibility alias.
"""

from __future__ import annotations

import pandas as pd


class FrameSource:
    """DataSource adapter over an already-fetched candle frame."""

    def __init__(self, candles: pd.DataFrame) -> None:
        self._candles = candles

    def get_candles(self, symbol: str, start: str, end: str, interval: str = "1day") -> pd.DataFrame:
        return self._candles

    def __repr__(self) -> str:
        return f"<FrameSource {len(self._candles)} bars>"


__all__ = ["FrameSource"]
