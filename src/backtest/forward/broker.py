from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TradeRecord:
    strategy: str
    symbol: str
    side: str
    price: float
    quantity: float
    when: str
    pnl: float = 0.0
    bars_held: int = 0


class SimulatedBroker:
    def __init__(self, cost_rate: float = 0.0008, stop_loss: float | None = None, take_profit: float | None = None, fill_reference: str = "close") -> None:
        self.cost_rate = float(cost_rate)
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        self.fill_reference = fill_reference
        self.trades: list[TradeRecord] = []

    def step(self, desired: float, bar: dict[str, Any], held: float, prev_close: float, entry_price: float | None, blocked: bool) -> dict[str, Any]:
        """
        Execute one bar of simulated trading.
        
        Invariant: net = held × (end/prev_close − 1) − costs, matching engine.
        
        Args:
            desired: target position from strategy signal (already shifted by caller)
            bar: current OHLCV bar
            held: position entering this bar (from previous bar)
            prev_close: close price from previous bar
            entry_price: entry price if position is open
            blocked: whether re-entry is blocked after forced exit
        """
        # Unblock when desired returns to 0
        want = 0 if blocked else desired
        
        prev_held = held
        # Execute position change at prev_close
        if want != held:
            if want != 0:
                entry_price = prev_close
            else:
                entry_price = None
            held = want
        
        # Entry/exit turnover cost
        turnover = abs(held - prev_held)
        bar_cost = turnover * self.cost_rate
        
        # Default fill price is close, unless intrabar stop/target triggers
        end = float(bar.get(self.fill_reference, bar.get("close", prev_close)))
        
        # Check intrabar stop/target (only if position is held)
        exit_triggered = False
        if held != 0 and entry_price is not None:
            if held > 0:
                # Long: stop below, target above
                stop = entry_price * (1 - self.stop_loss) if self.stop_loss is not None else None
                target = entry_price * (1 + self.take_profit) if self.take_profit is not None else None
                if stop is not None and float(bar.get("low", end)) <= stop:
                    end = stop
                    exit_triggered = True
                elif target is not None and float(bar.get("high", end)) >= target:
                    end = target
                    exit_triggered = True
            else:
                # Short: stop above, target below (inverted)
                stop = entry_price * (1 + self.stop_loss) if self.stop_loss is not None else None
                target = entry_price * (1 - self.take_profit) if self.take_profit is not None else None
                if stop is not None and float(bar.get("high", end)) >= stop:
                    end = stop
                    exit_triggered = True
                elif target is not None and float(bar.get("low", end)) <= target:
                    end = target
                    exit_triggered = True
        
        # On forced exit: add exit cost, go flat, block re-entry
        if exit_triggered:
            bar_cost += abs(held) * self.cost_rate
            held = 0.0
            blocked = True
            entry_price = None
        
        # Per-bar return: held position × pct change
        r = held * (end / prev_close - 1.0) if prev_close else 0.0
        net = r - bar_cost
        
        return {
            "held": held,
            "entry_price": entry_price,
            "blocked": blocked,
            "net": net,
            "end": end,
            "turnover": turnover,
            "cost": bar_cost,
        }
