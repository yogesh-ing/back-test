"""Symbol universe / pool registry (PRD Phase 2 / Task 2.1).

Resolves universe aliases such as ``NIFTY_50`` or ``TOP_10_CRYPTO`` into
concrete symbol lists, so a pool-mode :class:`StrategyRunner` can be spawned
with a single alias instead of 50 tickers.

A universe is a curated basket; symbols are returned in a stable order so
signal ranking and tests are deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

# ---------------------------------------------------------------------------
# Curated universes
# ---------------------------------------------------------------------------

# NIFTY 50 constituents (NSE). Equity symbols are stored as plain tickers the
# same way the rest of the app uses them (e.g. "RELIANCE").
NIFTY_50: List[str] = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "BHARTIARTL", "INFY",
    "ITC", "SBIN", "LT", "HINDUNILVR", "BAJFINANCE", "KOTAKBANK",
    "AXISBANK", "MARUTI", "SUNPHARMA", "ASIANPAINT", "TITAN", "BAJAJFINSV",
    "NESTLEIND", "ULTRACEMCO", "ONGC", "NTPC", "POWERGRID", "M&M",
    "TATAMOTORS", "TATASTEEL", "WIPRO", "HCLTECH", "ADANIENT", "ADANIPORTS",
    "COALINDIA", "GRASIM", "JSWSTEEL", "TECHM", "HDFCLIFE", "DRREDDY",
    "CIPLA", "BRITANNIA", "DIVISLAB", "EICHERMOT", "BAJAJ-AUTO", "HEROMOTOCO",
    "APOLLOHOSP", "BPCL", "INDUSINDBK", "SBILIFE", "TATACONSUM", "HINDALCO",
    "TRIDENT", "LTIM",
]

# Top crypto pairs (USD-quoted).
TOP_10_CRYPTO: List[str] = [
    "BTC/USD", "ETH/USD", "SOL/USD", "BNB/USD", "XRP/USD",
    "DOGE/USD", "ADA/USD", "AVAX/USD", "LINK/USD", "DOT/USD",
]

TOP_20_CRYPTO: List[str] = TOP_10_CRYPTO + [
    "MATIC/USD", "LTC/USD", "UNI/USD", "ATOM/USD", "ETC/USD",
    "FIL/USD", "NEAR/USD", "APT/USD", "ARB/USD", "OP/USD",
]


@dataclass(frozen=True)
class Universe:
    """A named, curated basket of symbols."""

    uid: str
    label: str
    symbols: List[str] = field(default_factory=list)
    sector: str = "custom"
    correlation_group: str | None = None

    def __post_init__(self) -> None:
        if not self.symbols:
            raise ValueError(f"universe {self.uid!r} must contain symbols")

    @property
    def size(self) -> int:
        return len(self.symbols)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_UNIVERSES: Dict[str, Universe] = {}


def register(universe: Universe) -> Universe:
    """Register a universe by its (upper-cased) alias."""
    key = universe.uid.strip().upper()
    _UNIVERSES[key] = universe
    return universe


def register_universe(
    uid: str,
    symbols: List[str],
    label: str | None = None,
    sector: str = "custom",
    correlation_group: str | None = None,
) -> Universe:
    """Convenience: register a universe from an alias + symbol list."""
    universe = Universe(
        uid=uid.strip().upper(),
        label=label or uid,
        symbols=[str(s).upper() for s in symbols],
        sector=sector,
        correlation_group=correlation_group,
    )
    return register(universe)


def get_universe(uid: str) -> Universe:
    """Return the :class:`Universe` for an alias.

    Raises ``KeyError`` for unknown aliases.
    """
    key = str(uid).strip().upper()
    if key not in _UNIVERSES:
        known = ", ".join(sorted(_UNIVERSES))
        raise KeyError(f"unknown universe: {uid}. Available: {known}")
    return _UNIVERSES[key]


def get_universe_symbols(uid: str) -> List[str]:
    """Resolve a universe alias to a concrete, stable-order symbol list."""
    return list(get_universe(uid).symbols)


def list_universes() -> List[Dict]:
    """Catalogue of universes as plain dicts (powers the spawn modal)."""
    return [
        {
            "id": u.uid,
            "label": u.label,
            "size": u.size,
            "sector": u.sector,
            "correlation_group": u.correlation_group,
            "symbols": list(u.symbols),
        }
        for u in sorted(_UNIVERSES.values(), key=lambda x: x.uid)
    ]


def is_universe(uid: str) -> bool:
    """True when ``uid`` is a registered universe alias."""
    return str(uid).strip().upper() in _UNIVERSES


# ---------------------------------------------------------------------------
# Built-in universes
# ---------------------------------------------------------------------------

register_universe(
    "NIFTY_50", NIFTY_50, label="NIFTY 50 Pool", sector="equity",
)
register_universe(
    "NIFTY50", NIFTY_50, label="NIFTY 50 Pool", sector="equity",
)
register_universe(
    "TOP_10_CRYPTO", TOP_10_CRYPTO, label="Top 10 Crypto",
    sector="crypto", correlation_group="crypto",
)
register_universe(
    "TOP_20_CRYPTO", TOP_20_CRYPTO, label="Top 20 Crypto",
    sector="crypto", correlation_group="crypto",
)

# Correlation groups for the Phase-3 concentration warning. A runner holding
# >= ``threshold`` LONG positions inside one group raises a High Concentration
# flag on the dashboard.
CORRELATION_GROUPS: Dict[str, Dict] = {
    "crypto": {
        "label": "Major Crypto",
        "symbols": set(TOP_20_CRYPTO),
        "threshold": 3,
    },
}


def correlation_group_for(symbol: str) -> str | None:
    """Return the correlation group id a symbol belongs to, if any."""
    sym = str(symbol).upper()
    for gid, group in CORRELATION_GROUPS.items():
        if sym in group["symbols"]:
            return gid
    return None
