from __future__ import annotations

from abc import ABC
from typing import Any

import pandas as pd


class Strategy(ABC):
    name: str = ""
    params: dict[str, Any] = {}
    stop_loss: float | None = None
    take_profit: float | None = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if getattr(cls, "name", ""):
            from .registry import register

            register(cls)

    def __init__(self, **overrides: Any) -> None:
        known = set(self.params)
        unknown = set(overrides) - known
        if unknown:
            raise ValueError(f"unknown strategy params: {sorted(unknown)}")

        for key, value in self.params.items():
            setattr(self, key, value)
        for key, value in overrides.items():
            if key in self.params:
                setattr(self, key, value)

    def p(self, key: str) -> Any:
        if key not in self.params:
            raise KeyError(key)
        return getattr(self, key)

    def entries(self, candles: pd.DataFrame) -> pd.Series:
        raise NotImplementedError

    def exits(self, candles: pd.DataFrame) -> pd.Series | None:
        return None

    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        if self._uses_entries_model():
            return self._signals_from_entries_exits(candles)
        raise NotImplementedError

    def _uses_entries_model(self) -> bool:
        return type(self).entries is not Strategy.entries

    def _signals_from_entries_exits(self, candles: pd.DataFrame) -> pd.Series:
        entries = self.entries(candles)
        exits = self.exits(candles)

        signals = pd.Series(0, index=candles.index, dtype=int)
        held = False
        for idx in candles.index:
            if entries.get(idx, False):
                held = True
                signals.loc[idx] = 1
            elif exits is not None and exits.get(idx, False):
                held = False
                signals.loc[idx] = 0
            else:
                signals.loc[idx] = 1 if held else 0
        return signals
