"""Quote providers for the options layer (Gap-Analysis G2.1 / G4.3).

Three implementations of the broker's ``QuoteProvider`` protocol
(``get_quote(instrument_token) -> {"ltp": ..., "bid": ..., "ask": ...}``):

* :class:`SyntheticQuoteProvider` — prices contracts with Black-Scholes off a
  simulated spot that the caller can move. Zero credentials, zero network;
  the default for the ``/options`` dashboard so P&L and Greeks respond to a
  market instead of a hardcoded ₹100.
* :class:`LiveQuoteProvider` — fetches real LTP from an authenticated
  mStock session, with a TTL cache so UI polling cannot spam the API.
* :class:`CachedQuoteProvider` — generic TTL wrapper around any provider
  (the rate-limit mitigation from the Gap PRD).

Also here: :class:`SyntheticChainGenerator` — builds a deterministic
NIFTY/BANKNIFTY option chain (Gap G4.3, the "mock broker" role) so the whole
expression → execution pipeline runs without mStock credentials.

All monetary math is Decimal.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from backtest.instruments.option import OptionContract
from backtest.instruments.base import OptionType

logger = logging.getLogger("backtest.options.quotes")

__all__ = [
    "SyntheticQuoteProvider",
    "LiveQuoteProvider",
    "CachedQuoteProvider",
    "SyntheticChainGenerator",
    "bs_price",
]


# ---------------------------------------------------------------------------
# Black-Scholes (no external deps — math.erf for the normal CDF)
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(
    spot: float,
    strike: float,
    years_to_expiry: float,
    vol: float,
    option_type: str,
    risk_free: float = 0.065,
) -> float:
    """European Black-Scholes price. ``option_type`` is ``"CE"`` or ``"PE"``."""
    if years_to_expiry <= 0:
        intrinsic = max(0.0, spot - strike) if option_type == "CE" else max(0.0, strike - spot)
        return intrinsic
    if vol <= 0:
        intrinsic = max(0.0, spot - strike) if option_type == "CE" else max(0.0, strike - spot)
        return intrinsic
    sqrt_t = math.sqrt(years_to_expiry)
    drift = (risk_free + vol * vol / 2.0) * years_to_expiry
    d1 = (math.log(spot / strike) + drift) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t
    disc = math.exp(-risk_free * years_to_expiry)
    if option_type == "CE":
        return spot * _norm_cdf(d1) - strike * disc * _norm_cdf(d2)
    return strike * disc * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


# ---------------------------------------------------------------------------
# Synthetic chain generator (G4.3 — the "mock broker" role)
# ---------------------------------------------------------------------------

class SyntheticChainGenerator:
    """Deterministic NIFTY/BANKNIFTY option chain — no credentials needed.

    Generates strikes around spot at realistic intervals (NIFTY: 50 pts,
    BANKNIFTY: 100 pts) for the next monthly expiry, and prices them with
    Black-Scholes so premiums differ per strike/expiry.
    """

    LOT_SIZES = {"NIFTY": 75, "BANKNIFTY": 35}
    STRIKE_STEPS = {"NIFTY": 50, "BANKNIFTY": 100}
    DEFAULT_SPOTS = {"NIFTY": 24800.0, "BANKNIFTY": 52000.0}
    VOL = {"NIFTY": 0.12, "BANKNIFTY": 0.15}

    def __init__(self, spot: float | None = None, strikes_each_side: int = 10) -> None:
        self.spots: dict[str, float] = dict(self.DEFAULT_SPOTS)
        self.strikes_each_side = int(strikes_each_side)
        if spot is not None:
            self.set_spot("NIFTY", spot)

    def set_spot(self, underlying: str, spot: float) -> None:
        self.spots[underlying] = float(spot)

    def get_spot(self, underlying: str) -> float:
        return self.spots.get(underlying, self.DEFAULT_SPOTS.get(underlying, 100.0))

    def next_monthly_expiry(self, reference: date | None = None) -> date:
        """Last Thursday of the month of ``reference`` (next month if passed).

        Month arithmetic goes through :meth:`_first_of_month`, which rolls the
        year over: the old ``ref.month + 2`` form raised
        ``month must be in 1..12`` for a November reference and skipped January
        for a December one — reachable as soon as the expiry calendar follows
        the replay clock (forward testing task B1) instead of ``date.today()``.
        """
        ref = reference or date.today()
        expiry = self._last_thursday_of(ref.year, ref.month)
        if expiry < ref:
            # This month's expiry has passed — the nearest remaining one is
            # NEXT month's (the old two-month jump skipped a whole expiry).
            first_next = self._first_of_month(ref, months_ahead=1)
            expiry = self._last_thursday_of(first_next.year, first_next.month)
        return expiry

    @staticmethod
    def _first_of_month(ref: date, months_ahead: int = 1) -> date:
        """First day of the month ``months_ahead`` from ``ref`` (year-safe)."""
        index = ref.year * 12 + (ref.month - 1) + months_ahead
        return date(index // 12, index % 12 + 1, 1)

    @classmethod
    def _last_thursday_of(cls, year: int, month: int) -> date:
        """Last Thursday of a calendar month (NSE monthly expiry convention)."""
        last_day = cls._first_of_month(date(year, month, 1), months_ahead=1) - timedelta(days=1)
        offset = (last_day.weekday() - 3) % 7  # Thursday == 3
        return last_day - timedelta(days=offset)

    def generate_chain(
        self,
        underlying: str,
        expiry: date | None = None,
        strikes_each_side: int | None = None,
        option_type: str = "CE",
    ) -> dict[Decimal, OptionContract]:
        """Build ``{strike: contract}`` for one expiry (V1 chain shape).

        ``option_type`` selects which side of the chain is materialised —
        V1 chains are flat (one contract per strike), so a put-side
        structure requests ``"PE"`` and gets put contracts.
        """
        step = self.STRIKE_STEPS.get(underlying, 50)
        spot = self.get_spot(underlying)
        atm = round(spot / step) * step
        expiry = expiry or self.next_monthly_expiry()
        lot_size = self.LOT_SIZES.get(underlying, 50)
        vol = self.VOL.get(underlying, 0.13)
        n_each = self.strikes_each_side if strikes_each_side is None else int(strikes_each_side)

        if option_type not in ("CE", "PE"):
            raise ValueError(f"option_type must be CE or PE, got {option_type!r}")

        chain: dict[Decimal, OptionContract] = {}
        for i in range(-n_each, n_each + 1):
            strike_val = atm + i * step
            strike = Decimal(str(strike_val))
            token = f"MOCK-{underlying}-{strike_val}-{option_type}"
            symbol = f"{underlying}{expiry.strftime('%y%m')}{strike_val}{option_type}"
            chain[strike] = OptionContract(
                instrument_token=token,
                trading_symbol=symbol,
                underlying=underlying,
                expiry=expiry,
                strike=strike,
                option_type=OptionType.CE if option_type == "CE" else OptionType.PE,
                lot_size=lot_size,
                metadata={"synthetic": True, "vol": vol, "spot_at_gen": spot},
            )
        return chain

    def available_expiries(
        self,
        underlying: str,
        count: int = 3,
        reference: date | None = None,
    ) -> list[date]:
        """The next ``count`` monthly expiries from ``reference`` (default today).

        ``reference`` matters for replay: a forward test iterating historical
        bars must select the expiry that was current **on the bar**, not the
        one that is current on the wall clock. Otherwise every bar after the
        wall-clock expiry is treated as "already expired", which silently
        blocks new entries forever.
        """
        expiries: list[date] = []
        ref = reference or date.today()
        for _ in range(count):
            expiries.append(self.next_monthly_expiry(ref))
            ref = expiries[-1] + timedelta(days=1)
        return expiries

    def price_contract(
        self,
        contract: OptionContract,
        option_type: str = "CE",
        reference: datetime | None = None,
    ) -> float:
        """Black-Scholes price for a generated contract at the current spot."""
        spot = self.get_spot(contract.underlying)
        vol = float(contract.metadata.get("vol", 0.13))
        ref = reference or datetime.now()
        expiry_dt = datetime.combine(contract.expiry or ref.date(), datetime.min.time())
        years = max((expiry_dt - ref).total_seconds(), 0.0) / (365.0 * 24 * 3600)
        return bs_price(spot, float(contract.strike), years, vol, option_type)


# ---------------------------------------------------------------------------
# SyntheticQuoteProvider (G2.1 — replaces FakeQuoteProvider in the web app)
# ---------------------------------------------------------------------------

class SyntheticQuoteProvider:
    """Black-Scholes quotes off a simulated spot — realistic, credential-free.

    Maintains a token → contract registry (populated by
    :meth:`register_chain`). MTM P&L and Greeks then respond to spot moves
    via :meth:`set_spot`, which is exactly what makes the dashboard feel
    alive without a live feed.
    """

    def __init__(self, chain_generator: SyntheticChainGenerator | None = None) -> None:
        self.generator = chain_generator or SyntheticChainGenerator()
        self._contracts: dict[str, OptionContract] = {}
        self.spread = 0.5  # bid/ask half-spread in ₹
        # Pricing clock (A6 debt, pulled forward): when set, every quote is
        # priced as of this reference instead of the wall clock. The options
        # backtest driver sets it per bar — otherwise ``price_contract``
        # computes NEGATIVE time-to-expiry against historical bars and every
        # option collapses to intrinsic value.
        self._reference: datetime | None = None

    def set_reference(self, reference: datetime | None) -> None:
        """Pin quote pricing to a reference time (``None`` = wall clock)."""
        self._reference = reference

    @property
    def source_name(self) -> str:
        return "synthetic:bs"

    # -- registry -------------------------------------------------------

    def register_chain(self, chain: dict[Decimal, OptionContract]) -> None:
        """Register contracts so ``get_quote`` can price their tokens."""
        for contract in chain.values():
            self._contracts[contract.instrument_token] = contract

    def register_contract(self, contract: OptionContract) -> None:
        self._contracts[contract.instrument_token] = contract

    # -- spot control ----------------------------------------------------

    def set_spot(self, underlying: str, spot: float) -> None:
        self.generator.set_spot(underlying, spot)

    def get_spot(self, underlying: str) -> float:
        return self.generator.get_spot(underlying)

    # -- QuoteProvider protocol ------------------------------------------

    def get_quote(self, instrument_token: str) -> dict[str, Any]:
        contract = self._contracts.get(instrument_token)
        if contract is None:
            logger.warning("SyntheticQuoteProvider: unknown token %s", instrument_token)
            return {"ltp": 0.0, "bid": 0.0, "ask": 0.0}
        option_type = (
            contract.option_type.value
            if hasattr(contract.option_type, "value")
            else str(contract.option_type)
        )
        ltp = self.generator.price_contract(contract, option_type, reference=self._reference)
        return {
            "ltp": round(ltp, 2),
            "bid": round(max(ltp - self.spread, 0.05), 2),
            "ask": round(ltp + self.spread, 2),
            "volume": 0,
            "oi": 0,
            "synthetic": True,
        }


# ---------------------------------------------------------------------------
# LiveQuoteProvider (G2.1 — real mStock LTP with TTL cache)
# ---------------------------------------------------------------------------

class LiveQuoteProvider:
    """Fetch real LTP from an authenticated mStock session.

    The mStock order client exposes ``get_latest()`` for quotes; this
    provider wraps it, normalises to the protocol's dict shape, and caches
    per token for ``cache_ttl_seconds`` so dashboard polling cannot spam
    the broker API (the rate-limit mitigation from the Gap PRD).
    """

    def __init__(self, broker: Any, cache_ttl_seconds: int = 5) -> None:
        self.broker = broker
        self.cache_ttl = int(cache_ttl_seconds)
        self._cache: dict[str, tuple[dict[str, Any], float]] = {}

    @property
    def source_name(self) -> str:
        return "live:mstock"

    def get_quote(self, instrument_token: str) -> dict[str, Any]:
        now = time.monotonic()
        cached = self._cache.get(instrument_token)
        if cached is not None:
            quote, ts = cached
            if now - ts < self.cache_ttl:
                return quote

        quote = self._fetch(instrument_token)
        self._cache[instrument_token] = (quote, now)
        return quote

    def _fetch(self, instrument_token: str) -> dict[str, Any]:
        try:
            raw = self.broker.get_option_quote(instrument_token)
        except Exception as exc:  # noqa: BLE001 — quotes must never crash the UI
            logger.error("LiveQuoteProvider fetch failed for %s: %s", instrument_token, exc)
            return {"ltp": 0.0, "bid": 0.0, "ask": 0.0, "error": str(exc)}

        if not raw:
            return {"ltp": 0.0, "bid": 0.0, "ask": 0.0}

        def _f(key: str) -> float:
            try:
                return float(raw.get(key, 0.0) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        return {
            "ltp": _f("ltp") or _f("last_price") or _f("last_price"),
            "bid": _f("bid") or _f("best_bid"),
            "ask": _f("ask") or _f("best_ask"),
            "volume": _f("volume"),
            "oi": _f("oi"),
        }


# ---------------------------------------------------------------------------
# CachedQuoteProvider — generic TTL wrapper (rate-limit mitigation)
# ---------------------------------------------------------------------------

class CachedQuoteProvider:
    """Wrap any quote provider with a per-token TTL cache."""

    def __init__(self, inner: Any, ttl: int = 60) -> None:
        self.inner = inner
        self.ttl = int(ttl)
        self._cache: dict[str, tuple[dict[str, Any], float]] = {}

    @property
    def source_name(self) -> str:
        return getattr(self.inner, "source_name", "unknown")

    def clear(self) -> None:
        """Drop every cached quote (e.g. after a synthetic spot change —
        cached prices would otherwise mask the move for up to ``ttl`` s)."""
        self._cache.clear()

    def get_quote(self, instrument_token: str) -> dict[str, Any]:
        now = time.monotonic()
        cached = self._cache.get(instrument_token)
        if cached is not None:
            quote, ts = cached
            if now - ts < self.ttl:
                return quote
        quote = self.inner.get_quote(instrument_token)
        self._cache[instrument_token] = (quote, now)
        return quote
