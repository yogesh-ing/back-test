from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StrategyAccount:
    cash: float = 0.0
    position: float = 0.0
    entry_price: float | None = None
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    equity_history: list[float] = field(default_factory=list)
    blocked: bool = False


class Portfolio:
    def __init__(self, allocations: dict[str, float] | None = None) -> None:
        self.allocations: dict[str, float] = {}
        self.accounts: dict[str, StrategyAccount] = {}
        if allocations:
            for name, capital in allocations.items():
                self.allocate(name, float(capital))

    def allocate(self, strategy: str, capital: float) -> StrategyAccount:
        self.allocations[strategy] = float(capital)
        account = self.accounts.setdefault(strategy, StrategyAccount(cash=float(capital)))
        account.cash = float(capital)
        account.position = 0.0
        account.entry_price = None
        account.realized_pnl = 0.0
        account.unrealized_pnl = 0.0
        account.blocked = False
        account.equity_history = []
        return account

    def mark_to_market(self, prices: dict[str, float]) -> None:
        for strategy, account in self.accounts.items():
            price = float(prices.get(strategy, prices.get("close", 0.0) or 0.0))
            if account.position != 0 and account.entry_price is not None:
                account.unrealized_pnl = (price - account.entry_price) * account.position
            else:
                account.unrealized_pnl = 0.0
            value = account.cash + account.position * price + account.realized_pnl + account.unrealized_pnl
            account.equity_history.append(value)

    def equity(self) -> float:
        total = 0.0
        for strategy, account in self.accounts.items():
            price = 0.0
            if account.position and account.entry_price is not None:
                price = account.entry_price
            total += account.cash + account.position * price + account.realized_pnl + account.unrealized_pnl
        return total

    def snapshot(self) -> dict[str, Any]:
        return {
            "allocations": dict(self.allocations),
            "accounts": {
                name: {
                    "cash": account.cash,
                    "position": account.position,
                    "entry_price": account.entry_price,
                    "realized_pnl": account.realized_pnl,
                    "unrealized_pnl": account.unrealized_pnl,
                    "blocked": account.blocked,
                    "equity_history": account.equity_history,
                }
                for name, account in self.accounts.items()
            },
        }

    @classmethod
    def load_from_snapshot(cls, snapshot: dict[str, Any]) -> "Portfolio":
        portfolio = cls(snapshot.get("allocations", {}))
        for name, payload in snapshot.get("accounts", {}).items():
            account = StrategyAccount(
                cash=float(payload.get("cash", 0.0)),
                position=float(payload.get("position", 0.0)),
                entry_price=payload.get("entry_price"),
                realized_pnl=float(payload.get("realized_pnl", 0.0)),
                unrealized_pnl=float(payload.get("unrealized_pnl", 0.0)),
                blocked=bool(payload.get("blocked", False)),
                equity_history=list(payload.get("equity_history", [])),
            )
            portfolio.accounts[name] = account
        return portfolio
