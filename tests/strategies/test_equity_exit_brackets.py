"""Editable SL/TP brackets on the equity signal path (2026-11-18).

The engine opens on 0→1 and closes on 1→0 in ``generate_signals`` for both
backtest and paper, so the bracket must live in the signal state machine.
These tests prove: defaults, editability, stop/target exits that fire while
the signal exit would still hold, and (sma) no instant re-entry after a
bracket exit.
"""

import numpy as np
import pandas as pd
from backtest.strategies.ema_pullback import EmaPullback
from backtest.strategies.sma_crossover import SmaCrossover


def _frame(closes):
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="D")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.005 for c in closes],
            "low": [c * 0.995 for c in closes],
            "close": closes,
            "volume": [1000] * len(closes),
        },
        index=idx,
    )


def _uptrend_then(dip_bars, shock_pct, tail=12):
    """Steep uptrend (slow EMA lags far below), a pullback deep enough to
    touch the fast EMA (entry), then one shock bar of ``shock_pct`` percent
    measured from the last dip close, then flat bars."""
    closes = [100.0 * 1.01**i for i in range(80)]
    last = closes[-1]
    for _ in range(dip_bars):
        last *= 0.991
        closes.append(last)
    closes.append(last * (1.0 + shock_pct / 100.0))
    closes.extend([closes[-1]] * tail)
    return _frame(closes)


def _first_entry(sig):
    arr = np.asarray(sig)
    hits = np.where((arr[1:] == 1) & (arr[:-1] == 0))[0]
    return int(hits[0]) + 1 if len(hits) else None


# ---------------------------------------------------------------------------
# ema_pullback — defaults 2% / 3%
# ---------------------------------------------------------------------------


def test_ema_pullback_declares_editable_bracket_defaults():
    assert EmaPullback.params["stop_loss_pct"]["default"] == 2.0
    assert EmaPullback.params["take_profit_pct"]["default"] == 3.0
    EmaPullback.validate()
    s = EmaPullback(stop_loss_pct=5.0, take_profit_pct=9.0)
    assert s.stop_loss_pct == 5.0 and s.take_profit_pct == 9.0


def test_ema_pullback_stop_fires_while_trend_still_up():
    candles = _uptrend_then(dip_bars=6, shock_pct=-2.5)
    bracket = np.asarray(EmaPullback().generate_signals(candles))
    no_bracket = np.asarray(
        EmaPullback(stop_loss_pct=0, take_profit_pct=0, stretch_pct=10.0).generate_signals(candles)
    )
    e = _first_entry(no_bracket)
    assert e is not None, "scenario must produce an entry"
    exits = np.where((bracket[e:] == 0) & (no_bracket[e:] == 1))[0]
    assert len(exits), "bracket must exit on a bar where the signal exit still holds"
    x = e + int(exits[0])
    entry_price = float(candles["close"].iloc[e])
    slow_ema = candles["close"].ewm(span=50, adjust=False).mean()
    assert float(candles["close"].iloc[x]) <= entry_price * 0.98
    assert float(candles["close"].iloc[x]) > float(slow_ema.iloc[x]), (
        "exit must be the stop, not the trend break"
    )


def test_ema_pullback_target_fires_before_the_stretch_exit():
    candles = _uptrend_then(dip_bars=6, shock_pct=5.0)
    bracket = np.asarray(EmaPullback(stretch_pct=10.0).generate_signals(candles))
    no_bracket = np.asarray(
        EmaPullback(stop_loss_pct=0, take_profit_pct=0, stretch_pct=10.0).generate_signals(candles)
    )
    e = _first_entry(no_bracket)
    assert e is not None
    exits = np.where((bracket[e:] == 0) & (no_bracket[e:] == 1))[0]
    assert len(exits), "+5% bar must hit the 3% target"
    x = e + int(exits[0])
    assert float(candles["close"].iloc[x]) >= float(candles["close"].iloc[e]) * 1.03


# ---------------------------------------------------------------------------
# sma_crossover — defaults 1.5% / 4%
# ---------------------------------------------------------------------------


def _sma_trend_then(shock_pct, tail=12):
    # gentle 0.5%/bar run-up so the cumulative gain from entry stays under
    # the 4% target — the shock bar is the first bracket event
    closes = [100.0 * 1.005**i for i in range(10)]
    closes.append(closes[-1] * (1.0 + shock_pct / 100.0))
    closes.extend([closes[-1]] * tail)
    return _frame(closes)


def test_sma_crossover_declares_editable_bracket_defaults():
    assert SmaCrossover.params["stop_loss_pct"]["default"] == 1.5
    assert SmaCrossover.params["take_profit_pct"]["default"] == 4.0
    SmaCrossover.validate()
    s = SmaCrossover(stop_loss_pct=2.5, take_profit_pct=6.0)
    assert s.stop_loss_pct == 2.5 and s.take_profit_pct == 6.0


def test_sma_stop_fires_while_fast_still_above_slow():
    candles = _sma_trend_then(shock_pct=-2.0)
    bracket = np.asarray(SmaCrossover(fast=3, slow=10).generate_signals(candles))
    no_bracket = np.asarray(
        SmaCrossover(fast=3, slow=10, stop_loss_pct=0, take_profit_pct=0).generate_signals(candles)
    )
    e = _first_entry(no_bracket)
    assert e is not None
    x = e + 1  # the shock bar
    assert no_bracket[x] == 1, "one down bar must not cross the SMAs"
    assert bracket[x] == 0, "-2% from entry must hit the 1.5% stop"
    assert not bracket[x + 1 :].any(), (
        "after a stop-out with the trend still up, no re-entry until a fresh cross"
    )


def test_sma_target_fires_and_waits_for_a_fresh_cross():
    candles = _sma_trend_then(shock_pct=5.0)
    bracket = np.asarray(SmaCrossover(fast=3, slow=10).generate_signals(candles))
    no_bracket = np.asarray(
        SmaCrossover(fast=3, slow=10, stop_loss_pct=0, take_profit_pct=0).generate_signals(candles)
    )
    e = _first_entry(no_bracket)
    assert e is not None
    x = e + 1
    assert no_bracket[x] == 1
    assert bracket[x] == 0, "+5% from entry must hit the 4% target"
    assert not bracket[x + 1 :].any()


def test_sma_with_bracket_off_is_the_old_level_signal():
    rng = np.random.default_rng(7)
    closes = 100.0 + np.cumsum(rng.normal(0, 1.5, 300))
    candles = _frame(list(closes))
    old = (
        candles["close"].rolling(20).mean() > candles["close"].rolling(50).mean()
    ).astype(int)
    new = SmaCrossover(stop_loss_pct=0, take_profit_pct=0).generate_signals(candles)
    assert list(new) == list(old)