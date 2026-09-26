"""EMA Reversion + Point of Balance (POB) — aggressive OTM option buying.

Spec (user request, 2026-09-25) — "Dr. Devendra's aggressive high-gamma
strategy" ("EMA Reversion + Point of Balance + Premium Chart Fibonacci").

The rules, translated to what this engine can actually see
---------------------------------------------------------
The strategy only ever receives the underlying's 1-minute bar buffer
(``generate_market_view`` gets ~500 bars ≈ 2 trading days), so:

* **Daily 5 EMA** — reconstructed by grouping the buffer's bars into
  trading days and taking each day's last close (today excluded), then
  EMA(5) over those daily closes. The daily close's distance from this
  EMA is the mean-reversion filter: a close far *below* the EMA biases
  the day bullish (reversion up toward the EMA), far *above* biases
  bearish — "price tends to touch the 5 EMA".
* **30-minute Point of Balance** — the day's FIRST completed 30-minute
  candle (resampled from 1-min bars, left-labelled). Fibonacci levels
  0.44 (lower) and 0.50 (upper) of that candle's range are the
  equilibrium band:
    - close **above the 50% level** → buyers in control → BULLISH (buy call)
    - close **below the 0.44 level** → sellers in control → BEARISH (buy put)
    - **inside the band** → hold the previous state (hysteresis — no
      flip-flopping in the no-man's-land between the levels)
  The state machine replays the day's 30-minute closes deterministically
  (pure function of the buffer — passes the conformance determinism check).
  Entries become possible once the first 30-min candle is COMPLETE; before
  that the strategy is silent, exactly like waiting for the first candle.
* **Premium-chart 50% hold / candle-low stop** — the strategy cannot see
  the option's own chart (contract seam: strategies emit views, the
  expression layer trades structures). The premium-level rules are
  approximated on the spawn expression's exit policy:
    - tight stop → ``exit.stop_loss_pct`` (₹1–₹3 stop on a ₹20–₹30
      premium ≈ 8–12% of premium),
    - minimum 1:2 target → ``exit.take_profit_pct`` (2.0 = 200% of premium),
    - "close below the 50% level" → ``exit.signal_flip = true`` (an
      opposite-direction view closes the position; ``reenter`` optionally
      books the reverse side next bar).
  The 5-EMA touch target / Renko-Supertrend trail has no engine
  equivalent — the take-profit knob is the stand-in (documented gotcha).

Contract adaptation
-------------------
Directional views only — the runner's expression maps BULLISH → long_call
and BEARISH → long_put (OTM via ``strike_selection``), which is the
aggressive OTM-buying shape the spec asks for. Deterministic: no clock,
no randomness, pure function of the candles.
"""

from __future__ import annotations

import pandas as pd

from backtest.strategy.base import Strategy
from backtest.strategy.intent import Direction, MarketView

# Fibonacci levels of the first 30-minute candle's range.
POB_LOWER = 0.44  # below → sellers in control
POB_UPPER = 0.50  # above → buyers in control


class EmaReversionPob(Strategy):
    """Daily-5-EMA reversion bias + first-30-min-candle 50% breakout trigger."""

    name = "ema_reversion_pob"
    eligible_instruments = ["NIFTY", "BANKNIFTY"]
    description = (
        "EMA Reversion + Point of Balance: bias from the daily 5-EMA "
        "mean-reversion gap, trigger from the first 30-minute candle's "
        "0.44/0.50 Fibonacci band. Bullish above the 50% level (long call), "
        "bearish below the 0.44 level (long put); holds state inside the "
        "band. Pair with a tight premium-percentage stop and 1:2+ target "
        "in the runner expression (aggressive OTM buying)."
    )
    version = "1.0"
    author = "Trading Bot"

    params = {
        "ema_period": {
            "default": 5,
            "min": 2,
            "max": 50,
            "type": "int",
            "label": "Daily EMA Period",
            "tooltip": "EMA length over reconstructed daily closes (spec: 5).",
        },
        "ema_gap_pct": {
            "default": 0.0,
            "min": 0.0,
            "max": 10.0,
            "type": "float",
            "label": "Min EMA Gap (%)",
            "tooltip": (
                "Only trade when the last daily close is at least this far "
                "(% of the EMA) from the daily EMA — 'price far away from "
                "the 5 EMA'. 0 disables the filter."
            ),
        },
        "require_ema_alignment": {
            "default": False,
            "type": "bool",
            "label": "Require EMA Alignment",
            "tooltip": (
                "True: the POB trigger must agree with the reversion bias "
                "(close below EMA → only longs, above → only shorts). "
                "False (spec default): the POB trigger alone decides."
            ),
        },
        "pob_lower": {
            "default": POB_LOWER,
            "min": 0.0,
            "max": 0.49,
            "type": "float",
            "label": "POB Lower Fib",
            "tooltip": "Close below this fraction of the first 30-min candle's range → bearish.",
        },
        "pob_upper": {
            "default": POB_UPPER,
            "min": 0.50,
            "max": 1.0,
            "type": "float",
            "label": "POB Upper Fib",
            "tooltip": "Close above this fraction of the first 30-min candle's range → bullish.",
        },
        "scale_points": {
            "default": 50.0,
            "min": 1.0,
            "max": 100000.0,
            "type": "float",
            "label": "Confidence Scale (pts)",
            "tooltip": (
                "Distance beyond the POB level (index points) that maps to "
                "full confidence. ~50 for NIFTY, ~100 for BANKNIFTY."
            ),
        },
        "min_confidence": {
            "default": 0.35,
            "min": 0.0,
            "max": 1.0,
            "type": "float",
            "label": "Min Confidence",
            "tooltip": "Views below this conviction are dropped (None returned).",
        },
        "underlying": {
            "default": "NIFTY",
            "type": "str",
            "label": "Underlying",
            "tooltip": "Index the view applies to (NIFTY / BANKNIFTY).",
        },
    }

    # ------------------------------------------------------------------ #
    # Building blocks (pure functions of the candle frame)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _daily_closes(candles: pd.DataFrame) -> pd.Series:
        """One close per completed trading day in the buffer (today dropped).

        The rolling buffer only holds ~2 days of 1-min bars, which is enough
        for EMA(5) to have a stable seed once a couple of days have passed;
        with fewer days the EMA degrades gracefully to what is available.
        """
        if candles.empty:
            return pd.Series(dtype=float)
        idx = candles.index
        day_key = pd.Series(idx.date, index=candles.index)
        daily = candles["close"].groupby(day_key).last()
        today = idx[-1].date()
        return daily.drop(index=today, errors="ignore").astype(float)

    def _daily_ema_bias(self, candles: pd.DataFrame) -> dict:
        """Reversion bias + gap filter from the daily 5 EMA.

        Close below the EMA → the market is expected to revert UP toward it
        (bullish bias); above → bearish. ``ema_gap_pct`` turns the
        'significantly far away' condition into a hard filter (0 = off).
        """
        out = {"bias": None, "ema": None, "close": None, "gap_pct": None}
        daily = self._daily_closes(candles)
        if daily.empty:
            return out
        ema = daily.ewm(span=max(2, int(self.ema_period)), adjust=False).mean()
        last_close = float(daily.iloc[-1])
        last_ema = float(ema.iloc[-1])
        gap_pct = (last_ema - last_close) / last_ema * 100.0 if last_ema else 0.0
        out.update(ema=last_ema, close=last_close, gap_pct=gap_pct)
        if float(self.ema_gap_pct) > 0 and abs(gap_pct) < float(self.ema_gap_pct):
            return out  # too close to the EMA — no 'far away' edge
        out["bias"] = Direction.BULLISH if last_close < last_ema else Direction.BEARISH
        return out

    @staticmethod
    def _first_30min_levels(candles: pd.DataFrame) -> dict | None:
        """Today's FIRST COMPLETED 30-minute candle and its 0.44/0.50 levels.

        ``None`` until the first window has fully formed (30 minutes of bars
        exist for the day) — the spec waits for the first 30-min candle.
        """
        if candles.empty:
            return None
        day = candles.index[-1].date()
        day_mask = pd.Series(candles.index.date, index=candles.index) == day
        day_df = candles.loc[day_mask]
        if day_df.empty:
            return None
        window_start = day_df.index[0].floor("30min")
        window_end = window_start + pd.Timedelta(minutes=30)
        first = day_df.loc[(day_df.index >= window_start) & (day_df.index < window_end)]
        if first.empty or day_df.index[-1] < window_end:
            return None  # first 30-min candle not complete yet — stand aside
        hi = float(first["high"].max())
        lo = float(first["low"].min())
        rng = hi - lo
        return {
            "high": hi,
            "low": lo,
            "upper": lo + rng * 0.50,
            "lower": lo + rng * 0.44,
            "start": window_start,
        }

    @staticmethod
    def _pob_state(day_df: pd.DataFrame, levels: dict, lower: float, upper: float):
        """Replay the day's 30-min closes through the band state machine.

        Above the 0.50 level → bullish; below the 0.44 level → bearish;
        inside the band → hold the previous state (hysteresis). Returns the
        final direction (``None`` if the day never left the band) plus the
        last decisive excursion size in points.
        """
        closes = day_df["close"].resample("30min", label="left", closed="left").last().dropna()
        state = None
        excursion = 0.0
        for _, close in closes.items():
            if close > levels["upper"]:
                state = Direction.BULLISH
                excursion = float(close - levels["upper"])
            elif close < levels["lower"]:
                state = Direction.BEARISH
                excursion = float(levels["lower"] - close)
            # else: inside the band — hold previous state
        return state, excursion

    # ------------------------------------------------------------------ #
    # Contract surface
    # ------------------------------------------------------------------ #

    def entries(self, candles: pd.DataFrame) -> pd.Series:
        """Equity-path fallback: long while close is above the daily EMA."""
        daily = self._daily_closes(candles)
        ema = (
            daily.ewm(span=max(2, int(self.ema_period)), adjust=False).mean()
            if not daily.empty
            else None
        )
        bias_up = bool(ema is not None and not daily.empty and daily.iloc[-1] >= ema.iloc[-1])
        return pd.Series(bias_up, index=candles.index)

    def generate_market_view(self, candles: pd.DataFrame) -> MarketView | None:
        if candles is None or candles.empty or "close" not in candles.columns:
            return None

        spot = float(candles["close"].iloc[-1])
        if spot <= 0:
            return None

        levels = self._first_30min_levels(candles)
        if levels is None:
            return None  # still inside the first 30-min candle — no trigger yet

        day = candles.index[-1].date()
        day_mask = pd.Series(candles.index.date, index=candles.index) == day
        day_df = candles.loc[day_mask]
        state, excursion = self._pob_state(
            day_df, levels, float(self.pob_lower), float(self.pob_upper)
        )
        if state is None:
            return None  # price never left the equilibrium band today

        ema_info = self._daily_ema_bias(candles)
        if ema_info["bias"] is not None:
            if bool(self.require_ema_alignment) and state != ema_info["bias"]:
                return None  # trigger fights the reversion bias — stand aside
        elif bool(self.require_ema_alignment) or float(self.ema_gap_pct) > 0:
            return None  # alignment/gap demanded but no daily data to judge it

        raw_conf = excursion / max(float(self.scale_points), 1e-9)
        if raw_conf < float(self.min_confidence):
            return None
        confidence = min(1.0, max(float(self.min_confidence), raw_conf))

        return MarketView(
            direction=state,
            confidence=confidence,
            underlying=str(self.underlying).upper(),
            spot_price=spot,
            bar_timestamp=candles.index[-1],
            metadata={
                "strategy": self.name,
                "pob_upper": levels["upper"],
                "pob_lower": levels["lower"],
                "first30_high": levels["high"],
                "first30_low": levels["low"],
                "daily_ema": ema_info["ema"],
                "daily_close": ema_info["close"],
                "ema_gap_pct": ema_info["gap_pct"],
            },
        )
