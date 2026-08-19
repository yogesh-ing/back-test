from __future__ import annotations

import pandas as pd

from backtest.live.mstock import _candles_to_frame


def test_candles_to_frame_accepts_mstock_array_rows():
    frame = _candles_to_frame([
        ["2024-03-20T05:30:00+05:30", 47.33, 47.54, 46.80, 47.21, 12585],
    ])

    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert isinstance(frame.index, pd.DatetimeIndex)
    assert frame.index.tz is None
    assert frame.iloc[0].to_dict() == {
        "open": 47.33,
        "high": 47.54,
        "low": 46.80,
        "close": 47.21,
        "volume": 12585.0,
    }
