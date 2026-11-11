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

from collections.abc import Iterable, Sequence
import inspect
import logging
import math
import os
import statistics
import threading
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Optional

from backtest.data.feed_depth_recorder import record_price_used, record_sanity_flag
from backtest.instruments.option import OptionContract
from backtest.instruments.base import OptionType
from backtest.timekeeping import EXCHANGE_TZ, now_utc, session_date

logger = logging.getLogger("backtest.options.quotes")

__all__ = [
    "SyntheticQuoteProvider",
    "LiveQuoteProvider",
    "LiveChainProvider",
    "HotStrikeRing",
    "CachedQuoteProvider",
    "BidAskQuoteProvider",
    "DisconnectedQuoteProvider",
    "NoExecutableQuoteError",
    "SyntheticChainGenerator",
    "PAIR_PARITY_MAX_DEVIATION",
    "PAIR_PARITY_MIN_STRIKES",
    "PAIR_PARITY_REASON",
    "PARITY_SANITY_ENV",
    "CROSS_BROKER_REASON",
    "CROSS_BROKER_SANITY_ENV",
    "CROSS_BROKER_MAX_DEVIATION",
    "CROSS_BROKER_MAX_REL",
    "CROSS_BROKER_PEER_MAX_AGE_S",
    "QUOTE_SANITY_ENV",
    "bs_price",
    "parity_deviation",
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

#: Smallest quotable option premium (NSE tick). A provider must never emit
#: 0.00 for a live contract: downstream, 0 means "quote failed".
MIN_QUOTE_TICK = 0.05


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

    def __init__(
        self,
        spot: float | None = None,
        strikes_each_side: int = 10,
        reference: datetime | None = None,
    ) -> None:
        self.spots: dict[str, float] = dict(self.DEFAULT_SPOTS)
        self.strikes_each_side = int(strikes_each_side)
        if spot is not None:
            self.set_spot("NIFTY", spot)
        # A6 determinism hardening (2026-09-28): pinnable clock. When set,
        # expiry selection AND pricing read this instead of the wall clock,
        # so a whole run is reproducible on any calendar date. ``None``
        # keeps V1 behaviour (wall clock) byte-identical.
        self._reference: datetime | None = reference

    def set_reference(self, reference: datetime | None) -> None:
        """Pin the generator's clock (expiry selection + pricing fallbacks).

        Mirrors :meth:`SyntheticQuoteProvider.set_reference`. ``None`` unpins
        back to the wall clock. Precedence at every call site: explicit
        ``reference`` argument > this pin > wall clock.

        .. warning:: In the forward stack ONE generator is shared per
           underlying across runners (``ChainBus``). Pin only in single-book
           contexts — tests and the options backtest driver. The shared bus
           must keep passing explicit per-bar references instead.
        """
        self._reference = reference

    # -- A6: the ONLY wall-clock fallbacks in this class ------------------
    # Every method that used to call ``date.today()`` / ``datetime.now()``
    # inline now routes through these two, so "who can smuggle the wall
    # clock into a backtest?" has exactly one audit point.

    def _reference_datetime(self, reference: datetime | None = None) -> datetime:
        """Explicit argument > pinned reference > wall clock (audit point)."""
        if reference is not None:
            return reference
        if self._reference is not None:
            return self._reference
        return now_utc()

    def _reference_date(self, reference: date | None = None) -> date:
        """Date twin of :meth:`_reference_datetime` (same precedence)."""
        if reference is not None:
            return reference
        if self._reference is not None:
            return session_date(self._reference)
        return date.today()

    def set_spot(self, underlying: str, spot: float) -> None:
        self.spots[underlying] = float(spot)

    def get_spot(self, underlying: str) -> float:
        return self.spots.get(underlying, self.DEFAULT_SPOTS.get(underlying, 100.0))

    def next_monthly_expiry(self, reference: date | None = None) -> date:
        """NSE monthly expiry for the month of ``reference`` (next month if passed).

        Last Tuesday from September 2025 onward, last Thursday before that —
        delegated to :func:`backtest.instruments.expiry_calendar.monthly_expiry`
        so the forward chain generator and the option backtest driver can never
        drift apart on the convention (the bug that produced a 2026-10-29
        "expiry" instead of the real 2026-10-27).

        Month arithmetic goes through :meth:`_first_of_month`, which rolls the
        year over: the old ``ref.month + 2`` form raised
        ``month must be in 1..12`` for a November reference and skipped January
        for a December one — reachable as soon as the expiry calendar follows
        the replay clock (forward testing task B1) instead of ``date.today()``.
        """
        from backtest.instruments.expiry_calendar import monthly_expiry

        ref = self._reference_date(reference)
        expiry = monthly_expiry(ref.year, ref.month)
        if expiry < ref:
            # This month's expiry has passed — the nearest remaining one is
            # NEXT month's (the old two-month jump skipped a whole expiry).
            first_next = self._first_of_month(ref, months_ahead=1)
            expiry = monthly_expiry(first_next.year, first_next.month)
        return expiry

    @staticmethod
    def _first_of_month(ref: date, months_ahead: int = 1) -> date:
        """First day of the month ``months_ahead`` from ``ref`` (year-safe)."""
        index = ref.year * 12 + (ref.month - 1) + months_ahead
        return date(index // 12, index % 12 + 1, 1)

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
        """The next ``count`` monthly expiries from ``reference``
        (default: the pinned reference if set, else today).

        ``reference`` matters for replay: a forward test iterating historical
        bars must select the expiry that was current **on the bar**, not the
        one that is current on the wall clock. Otherwise every bar after the
        wall-clock expiry is treated as "already expired", which silently
        blocks new entries forever.
        """
        expiries: list[date] = []
        ref = self._reference_date(reference)
        for _ in range(count):
            expiries.append(self.next_monthly_expiry(ref))
            ref = expiries[-1] + timedelta(days=1)
        return expiries

    #: Minimum time-to-expiry used for *pricing* (half a trading day). The
    #: pricing reference can legitimately sit at or after the expiry midnight
    #: — a wall-clock entry on expiry day, or replay bars that reach the
    #: expiry before settlement fires — and zero/negative time collapses
    #: Black-Scholes to pure intrinsic, pricing every ATM contract ₹0. That
    #: trips the runner's fail-closed ``ltp=0`` guard and blocks ALL entries
    #: (31 date-brittle test failures on 2026-09-24, a monthly expiry day).
    #: Clamping keeps a real, strike-dependent premium smile (a flat premium
    #: floor would flatten spreads to net ₹0); settlement math is untouched —
    #: it prices off the spot intrinsic, not this method.
    MIN_PRICING_YEARS = 0.5 / 365.0

    def price_contract(
        self,
        contract: OptionContract,
        option_type: str = "CE",
        reference: datetime | None = None,
    ) -> float:
        """Black-Scholes price for a generated contract at the current spot.

        Time-to-expiry is floored at :attr:`MIN_PRICING_YEARS` so contracts on
        (or nominally past) expiry day keep a tradeable premium instead of
        collapsing to ₹0 intrinsic.
        """
        spot = self.get_spot(contract.underlying)
        vol = float(contract.metadata.get("vol", 0.13))
        ref = self._reference_datetime(reference)
        # One clock family inside the pricer. Callers hand in either a naive
        # wall clock or — since the 2026-10-27 clock fix — an aware UTC bar
        # instant, and subtracting either from a naive midnight raises
        # "can't subtract offset-naive and offset-aware datetimes". Exchange
        # time is the common ground, and it is what a NSE expiry midnight
        # actually means.
        exchange_ref = (
            ref.astimezone(EXCHANGE_TZ)
            if ref.tzinfo is not None
            else ref.replace(tzinfo=EXCHANGE_TZ)
        )
        expiry_dt = datetime.combine(
            contract.expiry or exchange_ref.date(),
            datetime.min.time(),
            tzinfo=EXCHANGE_TZ,
        )
        years = (expiry_dt - exchange_ref).total_seconds() / (365.0 * 24 * 3600)
        return bs_price(
            spot,
            float(contract.strike),
            max(years, self.MIN_PRICING_YEARS),
            vol,
            option_type,
        )


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

    def register_chain(self, chain: Any) -> None:
        """Register contracts so ``get_quote`` can price their tokens.

        Accepts a ``{strike: contract}`` dict or a plain iterable of
        contracts (the bridge's flattened two-sided chains).
        """
        contracts = chain.values() if hasattr(chain, "values") else chain
        for contract in contracts:
            self._contracts[str(contract.instrument_token)] = contract

    def register_contract(self, contract: OptionContract) -> None:
        self._contracts[str(contract.instrument_token)] = contract

    def invalidate(self, instrument_token: str | None = None) -> None:
        """No-op cache invalidation hook (protocol symmetry)."""
        return None

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
            return {"ltp": 0.0, "bid": 0.0, "ask": 0.0, "error": "unknown token"}
        option_type = (
            contract.option_type.value
            if hasattr(contract.option_type, "value")
            else str(contract.option_type)
        )
        ltp = self.generator.price_contract(contract, option_type, reference=self._reference)
        # An exchange-traded premium can never be 0.00 — NSE's tick is 5 paise.
        # Rounding a deep-OTM contract to zero told the MTM fail-closed guard
        # "this quote failed" (0 = no quote, an impossible price), so a crash
        # froze the mark instead of marking the leg down: stops never fired.
        ltp = max(round(ltp, 2), MIN_QUOTE_TICK)
        return {
            "ltp": ltp,
            "bid": round(max(ltp - self.spread, MIN_QUOTE_TICK), 2),
            "ask": round(ltp + self.spread, 2),
            "volume": 0,
            "oi": 0,
            "synthetic": True,
        }


# ---------------------------------------------------------------------------
# LiveQuoteProvider (G2.1 — real broker LTP with TTL cache)
# ---------------------------------------------------------------------------


def broker_label(broker: Any) -> str:
    """B4 provenance: a row's label is the name of the broker that produced it.

    Every real broker carries ``broker_name`` (:mod:`backtest.brokers.base`,
    default ``"unnamed"``, overridden to ``mstock``/``dhan``/``upstox``).
    A duck-typed client that carries none is honestly ``"unnamed"`` rather
    than wearing a broker's name it cannot back — the 2026-11 sweep that
    removed the hard-coded ``"live:mstock"`` stamps.
    """
    return str(getattr(broker, "broker_name", "") or "unnamed")


class LiveQuoteProvider:
    """Fetch real LTP from an authenticated broker session.

    The mStock order client exposes ``get_latest()`` for quotes; this
    provider wraps it, normalises to the protocol's dict shape, and caches
    per token for ``cache_ttl_seconds`` so dashboard polling cannot spam
    the broker API (the rate-limit mitigation from the Gap PRD).
    """

    def __init__(self, broker: Any, cache_ttl_seconds: int = 5) -> None:
        self.broker = broker
        self.cache_ttl = int(cache_ttl_seconds)
        self._cache: dict[str, tuple[dict[str, Any], float]] = {}
        self._contracts: dict[str, Any] = {}

    @property
    def source_name(self) -> str:
        return f"live:{broker_label(self.broker)}"

    def register_contract(self, contract: Any) -> None:
        """Remember token → contract so numeric tokens translate to trading_symbol."""
        token = str(getattr(contract, "instrument_token", "") or "")
        if token:
            self._contracts[token] = contract

    def register_chain(self, chain: Any) -> None:
        """Remember token → contract for every contract in ``chain``."""
        contracts = chain.values() if hasattr(chain, "values") else chain
        for contract in contracts:
            self.register_contract(contract)

    def clear(self) -> None:
        """Drop all cached quotes."""
        self._cache.clear()

    def invalidate(self, instrument_token: str | None = None) -> None:
        """Evict one token (and its mapped symbol) or all tokens from cache."""
        if instrument_token is None:
            self._cache.clear()
            return
        key = str(instrument_token)
        self._cache.pop(key, None)
        contract = self._contracts.get(key)
        if contract is not None and getattr(contract, "trading_symbol", None):
            self._cache.pop(str(contract.trading_symbol), None)

    def get_quote(
        self, instrument_token: str, *, numeric_token: Any = None
    ) -> dict[str, Any]:
        key = str(instrument_token)
        contract = self._contracts.get(key)
        if numeric_token in (None, ""):
            # The registered contract knows its own token in the venue's format;
            # a digit-shaped key is only a fallback for a leg that was restored
            # before its contract was registered. Testing the key for digits
            # first would throw away a key-shaped venue's tokens.
            if contract is not None:
                numeric_token = getattr(contract, "instrument_token", None) or None
            elif key.isdigit():
                numeric_token = key
        if contract is not None and getattr(contract, "trading_symbol", None):
            key = str(contract.trading_symbol)

        now = time.monotonic()
        ws_feed = getattr(self.broker, "__dict__", {}).get("_ws_feed")
        ws_active = numeric_token not in (None, "") and ws_feed is not None
        if not ws_active:
            cached = self._cache.get(key)
            if cached is not None:
                quote, ts = cached
                if now - ts < self.cache_ttl:
                    return quote

        quote = self._fetch(
            key,
            numeric_token=numeric_token,
            fallback_cache_entry=self._cache.get(key) if ws_active else None,
            now_mono=now,
        )
        # Bypass the 5-second HTTP rate-limit cache for live WebSocket depth
        # (Plan §3.1.3): WS quotes live in memory (<10 µs) and must never lag
        # sub-second MTM or SL/TP behind a 5s TTL. Also never cache failures
        # or ltp<=0 rows (live-session lesson 2026-09-21).
        if quote.get("source") == "ws":
            self._cache.pop(key, None)
            return quote
        if float(quote.get("ltp", 0) or 0) > 0 and not quote.get("error"):
            self._cache[key] = (quote, now)
        return quote

    def _fetch(
        self,
        instrument_token: str,
        *,
        numeric_token: Any = None,
        fallback_cache_entry: tuple[dict[str, Any], float] | None = None,
        now_mono: float | None = None,
    ) -> dict[str, Any]:
        # mStock's WebSocket depth cache is keyed on the NUMERIC instrument
        # token and carries real bid/ask. The HTTP quote endpoint only accepts
        # the trading symbol and answers LTP-only — one-sided, so execution
        # fails closed (2026-10-22: CE blocked all day on a healthy socket
        # because the symbol was the only key ever tried). Ask the numeric
        # token first; keep the symbol row as the LTP fallback.
        has_numeric = numeric_token not in (None, "")
        keys: list[Any] = []
        if has_numeric:
            keys.append(numeric_token)
        if not has_numeric or str(instrument_token) != str(numeric_token):
            keys.append(instrument_token)

        raw: dict[str, Any] = {}
        failure: str | None = None
        for idx, key in enumerate(keys):
            # If the numeric WS lookup missed and we still hold a valid HTTP
            # fallback row inside cache_ttl, serve it instead of hammering HTTP.
            if (
                idx > 0
                and has_numeric
                and not raw
                and fallback_cache_entry is not None
            ):
                cached_q, cached_ts = fallback_cache_entry
                ref_mono = time.monotonic() if now_mono is None else now_mono
                if ref_mono - cached_ts < self.cache_ttl:
                    return cached_q

            try:
                row = self.broker.get_option_quote(key) or {}
            except Exception as exc:  # noqa: BLE001 — quotes must never crash the UI
                failure = str(exc)
                continue
            if row.get("bid") or row.get("ask") or idx == len(keys) - 1:
                raw = row
                failure = None
                break
            raw = raw or row

        if failure is not None and not raw:
            return {"ltp": 0.0, "bid": 0.0, "ask": 0.0, "error": failure}

        if not raw:
            return {"ltp": 0.0, "bid": 0.0, "ask": 0.0, "error": "empty quote row"}

        def _f(key: str) -> float:
            try:
                return float(raw.get(key, 0.0) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        ltp = _f("ltp") or _f("last_price")
        out: dict[str, Any] = {
            "ltp": ltp,
            "bid": _f("bid") or _f("best_bid"),
            "ask": _f("ask") or _f("best_ask"),
            "volume": _f("volume"),
            "oi": _f("oi"),
        }
        if raw.get("source"):
            out["source"] = str(raw["source"])
        if raw.get("recv_mono") is not None:
            try:
                out["recv_mono"] = float(raw["recv_mono"])
            except (TypeError, ValueError):
                pass
        if raw.get("error"):
            out["error"] = str(raw["error"])
        elif ltp <= 0:
            out["error"] = "zero or missing ltp"
        if not out["bid"] or not out["ask"]:
            # One-sided / depth-miss visibility: every NO_EXECUTION_QUOTE
            # upstream starts here, so name the key tried and what came back.
            logger.info(
                "QUOTE_ONE_SIDED key=%s numeric=%s ltp=%s bid=%s ask=%s error=%s",
                instrument_token,
                numeric_token,
                out["ltp"],
                out["bid"],
                out["ask"],
                out.get("error"),
            )
        return out


# ---------------------------------------------------------------------------
# CachedQuoteProvider — generic TTL wrapper (rate-limit mitigation)
# ---------------------------------------------------------------------------

class CachedQuoteProvider:
    """Wrap any quote provider with a per-token TTL cache.

    Preserves ``numeric_token`` routing for WebSocket depth caches, never
    caches zero/error rows, and bypasses TTL caching when the inner provider
    serves a zero-latency ``source="ws"`` quote.
    """

    def __init__(self, inner: Any, ttl: int = 60) -> None:
        self.inner = inner
        self.ttl = int(ttl)
        self._cache: dict[str, tuple[dict[str, Any], float]] = {}
        self._inner_accepts_numeric = self._check_accepts_numeric(inner)

    @staticmethod
    def _check_accepts_numeric(inner: Any) -> bool:
        fn = getattr(inner, "get_quote", None)
        if not callable(fn):
            return False
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            return False
        return "numeric_token" in sig.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in sig.parameters.values()
        )

    def _has_active_ws(self) -> bool:
        cur: Any = self.inner
        for _ in range(4):
            if cur is None:
                return False
            cur_dict = getattr(cur, "__dict__", {})
            broker = cur_dict.get("broker")
            if broker is not None:
                b_dict = getattr(broker, "__dict__", {})
                ws_feed = b_dict.get("_ws_feed")
                if ws_feed is not None:
                    return True
            cur = cur_dict.get("inner") or cur_dict.get("_quotes")
        return False

    @property
    def source_name(self) -> str:
        return getattr(self.inner, "source_name", "unknown")

    def clear(self) -> None:
        """Drop every cached quote (e.g. after a synthetic spot change —
        cached prices would otherwise mask the move for up to ``ttl`` s)."""
        self._cache.clear()
        inner_clear = getattr(self.inner, "clear", None)
        if callable(inner_clear):
            inner_clear()

    def invalidate(self, instrument_token: str | None = None) -> None:
        """Evict one token or all tokens from this and any inner cache."""
        if instrument_token is None:
            self._cache.clear()
        else:
            self._cache.pop(str(instrument_token), None)
        inner_inv = getattr(self.inner, "invalidate", None)
        if callable(inner_inv):
            inner_inv(instrument_token)

    def register_contract(self, contract: Any) -> None:
        """Delegate contract registration to the wrapped provider (R3)."""
        reg = getattr(self.inner, "register_contract", None)
        if callable(reg):
            reg(contract)

    def register_chain(self, chain: Any) -> None:
        """Delegate chain registration to the wrapped provider."""
        reg = getattr(self.inner, "register_chain", None)
        if callable(reg):
            reg(chain)

    def get_quote(
        self, instrument_token: str, *, numeric_token: Any = None
    ) -> dict[str, Any]:
        cache_key = str(instrument_token)
        now = time.monotonic()
        if not self._has_active_ws():
            cached = self._cache.get(cache_key)
            if cached is not None:
                quote, ts = cached
                if now - ts < self.ttl:
                    return quote

        if numeric_token is not None and self._inner_accepts_numeric:
            quote = self.inner.get_quote(instrument_token, numeric_token=numeric_token)
        else:
            quote = self.inner.get_quote(instrument_token)

        if not isinstance(quote, dict):
            return {"ltp": 0.0, "bid": 0.0, "ask": 0.0, "error": "invalid quote"}

        if quote.get("source") == "ws":
            self._cache.pop(cache_key, None)
            return quote

        if float(quote.get("ltp", 0) or 0) > 0 and not quote.get("error"):
            self._cache[cache_key] = (quote, now)
        return quote


# ---------------------------------------------------------------------------
# BidAskQuoteProvider (PRD §4.1 — buy at ask, sell at bid, mark at the other side)
# ---------------------------------------------------------------------------


class NoExecutableQuoteError(ValueError):
    """No crossable price for one leg.

    Carries the PRD's stable reason code so a soak can count the class, and
    subclasses ``ValueError`` because the execution seam treats it as a client
    rejection (400 at the API, ``{"rejected": True}`` at the bridge) rather
    than a server fault.
    """

    reason = "NO_EXECUTION_QUOTE"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.reason}: {detail}")


class UntrustedOptionQuoteError(NoExecutableQuoteError):
    """A nonzero side price that no counterparty could have quoted.

    The 2026-11-03 fabricated straddle needed exactly this class: legs priced at
    ₹0.05 and ₹4 on contracts worth ~₹340 passed ``price <= 0`` and were booked
    as +₹71,639 of profit. Same refusal shape as its parent, different reason
    code so the two failure modes stay countable apart.
    """

    reason = "UNTRUSTED_QUOTE"


#: ``off`` restores the pre-incident seam, ``shadow`` logs without refusing,
#: ``enforce`` refuses. Default is enforce — the gate exists because the number
#: that reached the ledger was neither zero nor missing, merely impossible.
QUOTE_SANITY_ENV = "OPTION_QUOTE_SANITY"
QUOTE_SANITY_MODES = ("off", "shadow", "enforce")
#: NSE index-option tick grid; a contract that says otherwise wins.
NSE_OPTION_TICK_SIZE = 0.05
#: How far a top-of-book may sit from the last traded price in the *same packet*.
#: Multiplicative and deliberately generous: an illiquid strike routinely prints
#: a book several times away from a stale LTP, and refusing that would turn a
#: calibration question into a mass exit refusal. What it cannot tolerate is an
#: order-of-magnitude disagreement — the ₹0.05 ask against a ₹340 LTP that the
#: 2026-11-03 straddle booked as profit.
LTP_DISTANCE_MAX_RATIO = 5.0

#: Pair parity (root-cause doc §8, item B). For one expiry, ``CE − PE + strike`` is
#: the cost of a synthetic long future, so it must print the SAME number at every
#: strike — put-call parity, no model and no spot needed. A leg whose own implied
#: forward dissents from the median of its siblings has a bad price on that leg,
#: even when it sits comfortably next to its own last trade: the ×1.345 PE-book
#: injection replayed on the 2026-11-08 capture
#: (``tools/out/demo_parity_catches_consistent_error.py``) passes both gates above
#: and is invisible to them by construction — every PE print moved together with
#: its own LTP.
#:
#: This band is measured, not picked. Replaying one full session of the
#: cross-broker witness through :func:`parity_deviation`
#: (``tools/out/measure_parity_band_20261109.py``, 2026-11-09, ~3,400 leg-reads per
#: broker per side, median 12 paired strikes) gives |deviation| p50 ₹0.88 / p95
#: ₹2.30 / p99 ₹3.25 on mStock asks and p95 ₹3.25 / p99 ₹4.90 on Upstox asks, while
#: the whole Upstox session — the broker with no known defect — never printed a
#: deviation above ₹7.25 on either side. ₹10 is the round number just clear of that
#: clean ceiling, and it caught exactly five mStock legs all session, the largest
#: ₹780.30 and ₹108.43 away from their own expiry's forward. One more session of
#: ``sanity_flag`` / ``parity_scan`` capture rows decides whether it moves;
#: ``LTP_DISTANCE_MAX_RATIO`` stays untouched — tightening it was ruled out, and
#: this gate exists so it does not have to be.
PAIR_PARITY_MAX_DEVIATION = 10.0
#: Paired strikes, other than the leg's own, before the median means anything.
#: Below this the ring is too thin (or the chain one-sided) and parity abstains.
PAIR_PARITY_MIN_STRIKES = 4
#: Reason code, counted in :attr:`BidAskQuoteProvider.sanity_stats`.
PAIR_PARITY_REASON = "pair_parity"
#: Parity runs in ``shadow`` by default — it is a witness first, a gate only after
#: the operator sees a session's hit count. Separate from ``OPTION_QUOTE_SANITY``
#: so the two proven gates can enforce while this one measures.
PARITY_SANITY_ENV = "OPTION_PARITY_SANITY"

#: Second-opinion gate: the same side of the book, at the other broker, right now.
#: Reason code, counted in :attr:`BidAskQuoteProvider.sanity_stats`.
CROSS_BROKER_REASON = "cross_broker_l1"
#: Its own mode, default ``shadow`` — a gate that depends on a second broker being
#: logged in must prove its hit rate before it can hold an exit open.
CROSS_BROKER_SANITY_ENV = "OPTION_CROSS_BROKER_SANITY"
#: Band ``dev > max(CROSS_BROKER_MAX_DEVIATION, CROSS_BROKER_MAX_REL * peer)``,
#: measured on one session of ``shadow_diff`` rows
#: (``tools/out/measure_cross_broker_band.py``, 2026-11-09: 5,119 pairs where both
#: brokers were <= 10 s fresh). The disagreement floor is tight — p50 ₹0.20, p99
#: ₹3.60, and the largest legitimate gap all session ₹7.45 on a ₹475 leg — and it
#: is flat in rupees rather than proportional, which is why the absolute term
#: carries the band and the percentage term only keeps it honest above ₹500. That
#: rule flags exactly ONE pair in 5,119, and it is the phantom the incident needs:
#: ``NIFTY26NOV25200PE`` offered at ₹302.00 while Upstox said ₹407.85 and both
#: brokers' own last trade said ₹408.75 (ages 0.81 s / 1.09 s) — a ₹105.85 hole
#: that passes the x5 ``ltp_distance`` test, because the bad number is 0.74x of a
#: correct LTP, not a multiple of it.
CROSS_BROKER_MAX_DEVIATION = 10.0
CROSS_BROKER_MAX_REL = 0.02
#: A peer older than this is a cached book, not a witness. Same ceiling the band
#: was measured at, and it is load-bearing: on 2026-11-10, with neither broker
#: logged in, every row on the tape was mStock zeros against an Upstox ladder
#: ~28 h old. Without an age condition that comparison is 1522 "disagreements"
#: and a gate would refuse the whole session.
CROSS_BROKER_PEER_MAX_AGE_S = 10.0


def parity_deviation(
    legs: Iterable[tuple[float, str, float]], strike: float
) -> Optional[tuple[float, float, int]]:
    """``(this strike's implied forward, the median of the others, how many)``.

    ``legs`` is ``(strike, "CE"|"PE", price)`` for ONE expiry, every leg read from
    the same ladder slot — mixing a bid with an ask would compare two different
    spreads against each other. ``None`` when this strike has no pair on the other
    side, or when fewer than :data:`PAIR_PARITY_MIN_STRIKES` other strikes carry
    both sides, which is the ring being thin rather than a price being wrong.

    Pure: no clock, no broker, no I/O — so the same function answers the seam and
    the capture analysis that has to set the band.
    """
    sides: dict[float, dict[str, float]] = {}
    for leg_strike, option_type, price in legs:
        if leg_strike <= 0 or price <= 0 or option_type not in ("CE", "PE"):
            continue
        sides.setdefault(leg_strike, {})[option_type] = price
    implied = {
        leg_strike: book["CE"] - book["PE"] + leg_strike
        for leg_strike, book in sides.items()
        if "CE" in book and "PE" in book
    }
    own = implied.get(strike)
    if own is None:
        return None
    others = [value for leg_strike, value in implied.items() if leg_strike != strike]
    if len(others) < PAIR_PARITY_MIN_STRIKES:
        return None
    return own, float(statistics.median(others)), len(others)


class BidAskQuoteProvider:
    """Execution realism wrapper: market orders cross the spread (PRD §4.1).

    Wraps any protocol provider. ``get_quote`` passes through UNCHANGED — the
    dashboard, Greeks and chain snapshots keep showing LTP, which is what an
    operator reads as "where the market is". The realism lives in two
    side-aware methods the paper broker calls:

    * :meth:`execution_quote` — the price an order actually pays: the ASK for a
      BUY, the BID for a SELL.
    * :meth:`mark_quote` — the conservative mark for a held leg: what closing
      it *now* would fetch (bid if long, ask if short), never LTP.

    **Fail-closed** (§4.1): when the side the order needs is missing or zero —
    venues do sometimes return a one-sided book, and a dead session returns a
    row of zeros — there is no fill and no mark. The method raises
    :class:`NoExecutableQuoteError` (or, for marks, flags the quote as failed)
    instead of substituting LTP, because pricing a leg off a number nobody was
    willing to trade at is exactly the phantom fill this wrapper exists to
    remove. A structure that cannot be priced stays unopened; a held structure
    keeps its last honest mark and the staleness halt deals with it.

    Zero is not the only impossible number. On 2026-11-03 the seam took a ₹0.05
    ask on a leg worth ~₹340 because ``price <= 0`` was the whole test, so the
    same fail-closed rule now runs two more gates before a side price is
    believed — see :meth:`_sanity_problems`: the price must sit near the LTP that
    arrived in the same packet, and it must not be under the contract's own
    intrinsic value. Mode is ``$OPTION_QUOTE_SANITY`` (``off`` / ``shadow`` /
    ``enforce``, default ``enforce``); refusals count into
    :attr:`sanity_stats` and log as ``quote_sanity``.

    A third proof, ``pair_parity`` — the leg's strike must agree with the other
    strikes of its expiry about where the forward is — runs under its own
    ``$OPTION_PARITY_SANITY`` mode and defaults to ``shadow``, because its band
    is still being calibrated from the capture file rather than derived from an
    incident. A fourth, ``cross_broker_l1``, asks the *other broker*'s book for the
    same side of the same contract right now — the only proof that survives a bad
    price agreeing with its own last trade, which is the shape of the 2026-11-08
    ₹493.65 offer; it answers to ``$OPTION_CROSS_BROKER_SANITY`` (default
    ``shadow``) and abstains whenever no second broker is mirrored, so it can
    never be the only defence. Every flag, shadow or enforced, is also written to
    the capture (``kind=sanity_flag``) so the next session's tape counts what a
    gate would have done.

    Note ``OptionPaperBroker.slippage_pct`` still applies ON TOP: the
    spread is where the market stands, slippage models your own impact.
    Construct the broker with ``slippage_pct=0`` for pure-spread fills.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        #: Refusals by reason code, so a soak can count the classes apart from
        #: ``NO_EXECUTION_QUOTE``. Read by the runner's diagnostics, never by a
        #: pricing path.
        self.sanity_stats: dict[str, int] = {}

    # -- quote sanity (root-cause doc §8b/§8d) ------------------------------

    @staticmethod
    def _sanity_mode() -> str:
        mode = str(os.getenv(QUOTE_SANITY_ENV, "enforce")).strip().lower()
        return mode if mode in QUOTE_SANITY_MODES else "enforce"

    def _contract_for(self, instrument_token: str) -> Any:
        """The contract behind a token, if the wrapped provider knows it.

        Same lookup order the paper broker uses for tick size: an explicit
        finder, then the private registry. ``None`` means "no opinion", and the
        intrinsic gate abstains rather than guessing a strike.
        """
        for name in ("get_contract", "contract_for", "find_contract"):
            finder = getattr(self.inner, name, None)
            if callable(finder):
                try:
                    found = finder(instrument_token)
                except Exception:  # noqa: BLE001 — a lookup must never price a leg
                    found = None
                if found is not None:
                    return found
        registry = getattr(self.inner, "_contracts", None)
        if isinstance(registry, dict):
            return registry.get(str(instrument_token))
        return None

    def _spot_for(self, contract: Any) -> float:
        """The underlying's live spot, or 0.0 when the provider cannot say.

        Deliberate deviation from fail-closed: an unusable spot disables only
        the intrinsic gate, it does not refuse the price. Refusing on a missing
        spot would turn a spot-cache hiccup into a mass exit refusal across
        every held book — a different outage than the one being fixed.
        """
        getter = getattr(self.inner, "get_spot", None)
        if not callable(getter):
            return 0.0
        underlying = str(getattr(contract, "underlying", "") or "")
        if not underlying:
            return 0.0
        try:
            return max(0.0, float(getter(underlying)))
        except Exception:  # noqa: BLE001 — abstain, never invent a spot
            return 0.0

    def _sanity_problems(
        self, quote: dict[str, Any], key: str, price: float, instrument_token: str,
        *, parity_enabled: bool = True, corroboration_enabled: bool = True,
    ) -> list[tuple[str, str]]:
        """Every ``(reason, detail)`` this side price fails, most precise first.

        Independent impossibility proofs, all read from data already in hand.
        Returning all of them rather than the first is what keeps a witness in
        ``shadow`` from hiding a gate in ``enforce`` — see :meth:`_check_sanity`:

        * ``intrinsic_floor`` — an ITM option is worth at least its intrinsic
          (minus one tick for grid rounding). ₹0.05 on a ₹340-strike leg is not
          a cheap quote, it is not a quote. Model-free, so it needs no opinion
          about volatility or time value. It fires only when the row's own LTP
          corroborates the intrinsic: when the LTP is itself under intrinsic the
          whole row is merely lagging a moved spot, which is a staleness problem
          the age gates own — refusing it would lock an exit rather than a fake.
        * ``ltp_distance`` — the same packet carried an LTP for this very
          instrument; a book a multiple away from it is a decoding artefact or a
          row whose price moved on.
        * ``pair_parity`` — this leg's own strike disagrees with the other strikes
          of the same expiry about where the forward is. Its own mode
          (:data:`PARITY_SANITY_ENV`, shadow by default) decides whether it runs at
          all; see :meth:`_parity_problem`.
        * ``cross_broker_l1`` — the other broker's book disagrees about this exact
          side right now. See :meth:`_corroboration_problem`.
        """
        problems: list[tuple[str, str]] = []
        try:
            ltp = float(quote.get("ltp") or 0.0)
        except (TypeError, ValueError):
            ltp = 0.0

        contract = self._contract_for(instrument_token)

        if contract is not None:
            tick = NSE_OPTION_TICK_SIZE
            try:
                raw_tick = float(getattr(contract, "tick_size", 0) or 0)
                if raw_tick > 0:
                    tick = raw_tick
            except (TypeError, ValueError):
                pass
            try:
                strike = float(getattr(contract, "strike", 0) or 0)
            except (TypeError, ValueError):
                strike = 0.0
            option_type = getattr(contract, "option_type", "")
            option_type = str(getattr(option_type, "value", option_type)).upper()
            spot = self._spot_for(contract)
            if strike > 0 and spot > 0 and option_type in ("CE", "PE"):
                intrinsic = (
                    max(0.0, spot - strike)
                    if option_type == "CE"
                    else max(0.0, strike - spot)
                )
                corroborated = ltp <= 0 or ltp >= intrinsic - tick
                if intrinsic > 0 and corroborated and price < intrinsic - tick:
                    problems.append((
                        "intrinsic_floor",
                        f"{key}={price:.2f} < intrinsic={intrinsic:.2f} "
                        f"({option_type} strike={strike:.0f} spot={spot:.2f})",
                    ))

        if ltp > 0 and price > 0:
            if price * LTP_DISTANCE_MAX_RATIO < ltp or price > ltp * LTP_DISTANCE_MAX_RATIO:
                problems.append((
                    "ltp_distance",
                    f"{key}={price:.2f} vs ltp={ltp:.2f} "
                    f"(outside x{LTP_DISTANCE_MAX_RATIO} of its own last trade)",
                ))
        if parity_enabled and contract is not None:
            problem = self._parity_problem(contract, key, price, instrument_token)
            if problem is not None:
                problems.append(problem)
        if corroboration_enabled:
            problem = self._corroboration_problem(key, price, instrument_token)
            if problem is not None:
                problems.append(problem)
        return problems

    @staticmethod
    def _parity_mode() -> str:
        """``off`` / ``shadow`` / ``enforce`` for the parity witness alone.

        Default ``shadow``: the band is a placeholder until a session of
        ``sanity_flag`` capture rows says where the real noise floor is, and a
        witness that refuses exits on its first day of calibration is worse than
        no witness. The two proven gates keep their own
        :data:`QUOTE_SANITY_ENV` (default ``enforce``).
        """
        mode = str(os.getenv(PARITY_SANITY_ENV, "shadow")).strip().lower()
        return mode if mode in QUOTE_SANITY_MODES else "shadow"

    def _parity_problem(
        self, contract: Any, key: str, price: float, instrument_token: str
    ) -> tuple[str, str] | None:
        """``pair_parity`` when this leg dissents from its own expiry's forward.

        For one expiry, ``CE − PE + strike`` is the cost of a synthetic long future
        and must print the same number at every strike. The reference is the median
        of the *ring's other strikes* — not an external spot or a model — so it
        absorbs carry and interest, survives a market-wide move (every strike
        dissents together, so the median moves with them), and needs no threshold
        about where NIFTY "should" be. What it cannot survive is one leg, or one
        whole side of the book, priced wrong in a way that agrees with its own LTP:
        exactly the shape :meth:`_sanity_problems`'s per-row gates are blind to.

        Peers come from the wrapped provider read-only
        (:meth:`LiveChainProvider.parity_siblings`); a provider that cannot name
        them, a ring too thin to have an opinion, or a missing pair on this strike
        all abstain. Never raises: a witness must not become an outage.
        """
        if key not in ("bid", "ask"):
            return None
        getter = getattr(self.inner, "parity_siblings", None)
        if not callable(getter):
            return None
        try:
            siblings = getter(instrument_token) or []
            strike = float(getattr(contract, "strike", 0) or 0)
            option_type = getattr(contract, "option_type", "")
            option_type = str(getattr(option_type, "value", option_type)).upper()
            legs: list[tuple[float, str, float]] = [(strike, option_type, price)]
            for sibling, row in siblings:
                legs.append((
                    float(getattr(sibling, "strike", 0) or 0),
                    str(getattr(
                        getattr(sibling, "option_type", ""), "value",
                        getattr(sibling, "option_type", ""),
                    )).upper(),
                    float((row or {}).get(key) or 0.0),
                ))
            result = parity_deviation(legs, strike)
        except Exception:  # noqa: BLE001 — a witness must not reach the trading path
            logger.debug("pair_parity aborted", exc_info=True)
            return None
        if result is None or strike <= 0:
            return None
        own, reference, peers = result
        deviation = own - reference
        if abs(deviation) <= PAIR_PARITY_MAX_DEVIATION:
            return None
        return (
            PAIR_PARITY_REASON,
            f"{key}={price:.2f} on {option_type} {strike:.0f} implies forward "
            f"{own:.2f} vs {reference:.2f} median of {peers} paired strikes "
            f"(dev={deviation:+.2f}, band ±{PAIR_PARITY_MAX_DEVIATION:.2f})",
        )

    @staticmethod
    def _corroboration_mode() -> str:
        """``off`` / ``shadow`` / ``enforce`` for the second-broker witness alone.

        Default ``shadow``, for a reason parity does not have: this gate can only
        answer when the operator is logged into the second broker. On a
        single-broker day an enforcing version abstains always; on a two-broker day
        it is the only thing between a bad offer and a stopped structure. The
        capture decides — ``sanity_flag`` rows with ``reason=cross_broker_l1`` are
        the vote, and they count what enforcement *would* have refused.
        """
        mode = str(os.getenv(CROSS_BROKER_SANITY_ENV, "shadow")).strip().lower()
        return mode if mode in QUOTE_SANITY_MODES else "shadow"

    def _corroboration_problem(
        self, key: str, price: float, instrument_token: str
    ) -> tuple[str, str] | None:
        """``cross_broker_l1`` when the other broker's book disagrees on this side.

        Every other proof here reads one broker's packet and asks whether it is
        self-consistent, which is exactly why the 2026-11-08 offer of ₹493.65 on a
        ₹367.00 put passed: 1.34x its own (correct) last trade, inside the x5 band,
        and an OTM put has no intrinsic floor to breach. This asks a question the
        bad row cannot answer, because the bad row is not an input to it.

        Deliberately narrow, since a second opinion that overrules the venue you
        trade on is a fresh way to lose a fill:

        * the peer must be FRESH (:data:`CROSS_BROKER_PEER_MAX_AGE_S`) — the
          2026-11-10 tape is mStock zeros against an Upstox ladder ~28 h old, and a
          gate with no age condition reads that as 1,522 disagreements and refuses
          the session;
        * only the same side is compared; asking whether a bid agrees with the
          other broker's ask would flag normal spread;
        * ``None`` — abstain, never refuse — when no mirror is running, the token
          is unmapped on the peer's terms, or the peer side is missing or
          unreadable. Absence of a witness must not look like evidence of a
          problem, so a broker logout costs corroboration, not trading.

        Blind spot: both brokers publishing the same junk, or the *peer* being the
        wrong one. Neither is detectable from L1 agreement and neither is what the
        2026-11-08 incident looked like; the mirror's own ladder check
        (``ladder_status``) is what covers the second.
        """
        getter = getattr(self.inner, "peer_l1", None)
        if not callable(getter):
            return None
        try:
            peer = getter(instrument_token) or {}
            peer_price = float(peer.get(key) or 0.0)
            age_s = float(peer.get("age_s"))
        except (TypeError, ValueError):
            return None
        if peer_price <= 0 or not 0.0 <= age_s <= CROSS_BROKER_PEER_MAX_AGE_S:
            return None
        band = max(CROSS_BROKER_MAX_DEVIATION, CROSS_BROKER_MAX_REL * peer_price)
        deviation = abs(price - peer_price)
        if deviation <= band:
            return None
        return (
            CROSS_BROKER_REASON,
            f"{key}={price:.2f} vs {peer.get('broker', 'peer')} {key}={peer_price:.2f} "
            f"(dev={deviation:.2f} > band {band:.2f}, peer age {age_s:.1f}s)",
        )

    def _check_sanity(
        self, quote: dict[str, Any], key: str, price: float, instrument_token: str
    ) -> str | None:
        """Run the gates; return the reason when the price must be refused.

        ``shadow`` records and logs but lets the trade through — that is the
        mode to run while the calibration of a bound is being watched. Each
        reason answers to its own env, so parity can measure while the two proven
        gates enforce.
        """
        general = self._sanity_mode()
        parity = self._parity_mode()
        corroboration = self._corroboration_mode()
        if general == "off" and parity == "off" and corroboration == "off":
            return None
        modes = {PAIR_PARITY_REASON: parity, CROSS_BROKER_REASON: corroboration}
        problems = self._sanity_problems(
            quote, key, price, instrument_token,
            parity_enabled=parity != "off",
            corroboration_enabled=corroboration != "off",
        )
        # A gate that is switched off contributes nothing, even when it disagrees:
        # ``OPTION_QUOTE_SANITY=off`` must restore the pre-2026-11-03 seam exactly.
        problems = [p for p in problems if modes.get(p[0], general) != "off"]
        if not problems:
            return None
        # An enforcing gate outranks a witnessing one whatever the check order, so
        # turning on ``cross_broker_l1`` cannot be neutralised by parity still
        # sitting in ``shadow``. Among shadows, the most precise reason is logged.
        enforcing = [p for p in problems if modes.get(p[0], general) == "enforce"]
        reason, detail = enforcing[0] if enforcing else problems[0]
        mode = modes.get(reason, general)
        self.sanity_stats[reason] = self.sanity_stats.get(reason, 0) + 1
        logger.warning(
            "quote_sanity mode=%s reason=%s token=%s side=%s detail=%s",
            mode,
            reason,
            instrument_token,
            key,
            detail,
        )
        record_sanity_flag(
            quote,
            broker=str(getattr(self, "source_name", "unknown")),
            token=instrument_token,
            reason=reason,
            mode=mode,
            field=key,
            price=price,
            detail=detail,
        )
        if mode == "enforce":
            return f"{reason} ({detail})"
        return None

    # -- QuoteProvider protocol (unchanged passthrough) -------------------

    def get_quote(
        self, instrument_token: str, *, numeric_token: Any = None
    ) -> dict[str, Any]:
        if numeric_token is not None:
            fn = getattr(self.inner, "get_quote", None)
            if callable(fn):
                try:
                    return fn(instrument_token, numeric_token=numeric_token)
                except TypeError:
                    pass
        return self.inner.get_quote(instrument_token)

    def execution_quote(self, instrument_token: str, side: str) -> dict[str, Any]:
        """Side-aware fill quote: ``ltp`` becomes ask (BUY) / bid (SELL)."""
        normalized = str(side).upper()
        quote = dict(self.inner.get_quote(instrument_token) or {})
        key = "ask" if normalized == "BUY" else "bid"
        return self._side_price(quote, key, normalized, instrument_token)

    def mark_quote(self, instrument_token: str, position_side: str) -> dict[str, Any]:
        """Conservative mark for a held leg: bid if long, ask if short.

        A long position is worth what a buyer would pay for it — the bid. The
        returned dict keeps ``ltp`` as the mark so every existing consumer
        (unrealized P&L, exit rules, dashboards reading the book) reads the
        conservative number without knowing about this wrapper.
        """
        quote = dict(self.inner.get_quote(instrument_token) or {})
        held = str(position_side).upper()
        key = "bid" if held == "BUY" else "ask"
        price = float(quote.get(key) or 0.0)
        if price <= 0 or bool(quote.get("error")):
            reason = f"no {key} quote to mark {instrument_token} (held {held})"
        else:
            reason = self._check_sanity(quote, key, price, instrument_token)
        if reason is not None:
            # Do NOT invent a mark: label the row failed so the broker's
            # fail-closed MTM keeps the last known price and counts the
            # failure (the same path a dead broker session takes).
            quote["error"] = quote.get("error") or reason
            return quote
        quote["ltp"] = price
        quote["mark_basis"] = key
        return quote

    def _side_price(
        self, quote: dict[str, Any], key: str, side: str, instrument_token: str
    ) -> dict[str, Any]:
        price = float(quote.get(key) or 0.0)
        if price <= 0 or bool(quote.get("error")):
            raise NoExecutableQuoteError(
                f"{instrument_token}: no {key} to {side} against "
                f"(quote={quote.get('error') or 'one-sided or zero ' + key})"
            )
        reason = self._check_sanity(quote, key, price, instrument_token)
        if reason is not None:
            raise UntrustedOptionQuoteError(f"{instrument_token}: {reason}")
        # An order row keeps only the price; this writes down the whole ladder it came
        # from, so a disputed fill can be settled from a file (ENABLE_FEED). Recorded
        # before ``ltp`` is overwritten with the side price below: the pair of them is
        # the finding — an offer of ₹493.65 against a last trade of ₹367.00 — and a
        # witness that reads the fill price back as the last trade cannot show it.
        self._record_price_used(quote, key, side, instrument_token)
        quote["ltp"] = price
        quote["execution_side"] = side
        return quote

    def _record_price_used(
        self, quote: dict[str, Any], key: str, side: str, instrument_token: str
    ) -> None:
        """Hand the packet a fill was priced from to the capture, with the name on it."""
        row = dict(quote)
        if not row.get("symbol") and not row.get("trading_symbol"):
            contract = self._contract_for(instrument_token)
            if contract is not None:
                symbol = getattr(contract, "trading_symbol", None) or getattr(
                    contract, "symbol", None
                )
                if symbol:
                    row["symbol"] = str(symbol)
        record_price_used(
            row,
            broker=str(getattr(self, "source_name", "unknown")),
            token=instrument_token,
            side=side,
            field=key,
        )

    def register_contract(self, contract: Any) -> None:
        """Delegate contract registration to the wrapped provider (R3)."""
        reg = getattr(self.inner, "register_contract", None)
        if callable(reg):
            reg(contract)

    def invalidate(self, instrument_token: str | None = None) -> None:
        """Delegate cache invalidation to the wrapped provider."""
        inv = getattr(self.inner, "invalidate", None)
        if callable(inv):
            inv(instrument_token)

    # -- delegation so the wrapper can sit anywhere in the chain ----------

    @property
    def source_name(self) -> str:
        inner_name = str(getattr(self.inner, "source_name", "unknown"))
        return f"{inner_name}+bidask"

    @property
    def generator(self) -> Any:
        return getattr(self.inner, "generator", None)

    def __getattr__(self, name: str) -> Any:
        # set_reference / set_spot / get_spot / register_chain /
        # register_contract / get_quotes_bulk … — whatever the inner
        # provider offers, offer too (only called for names not found on
        # the wrapper itself).
        return getattr(self.inner, name)


# ---------------------------------------------------------------------------
# DisconnectedQuoteProvider (Sprint A1 — the frozen-mark stub)
# ---------------------------------------------------------------------------

class DisconnectedQuoteProvider:
    """The quote feed of a book whose broker session is not authenticated.

    An option runner restored before the operator logged in used to fail
    construction and disappear into ``PortfolioManager._parked_snapshots``: its
    open structures, strikes, lots and manual SL/TP levels went invisible, and
    with them the risk they carry. This provider is what lets the book be built
    anyway. It answers **every** quote with an explicit failure, so

    * held legs keep their last honest mark — the fail-closed MTM refuses a
      zero/``error`` row and flags staleness instead of re-pricing the leg off a
      number nobody quoted;
    * a new entry cannot fill: :meth:`execution_quote` raises
      :class:`NoExecutableQuoteError`, and the bridge refuses the intent itself
      with the ``WAITING_FOR_FEED`` code before it ever prices one.

    It never generates a chain, a premium or a candle. There is deliberately no
    ``generator`` attribute, so :meth:`OptionsBridge._generator` refuses rather
    than fitting a synthetic chain behind a book that cannot price — fabricating
    a mark is the one thing worse than showing a stale one.
    """

    #: Why every quote fails, and the code the bridge refuses an entry with.
    reason = "WAITING_FOR_FEED"
    #: Read by the bridge (keep this provider off the synthetic chain surface)
    #: and by the runner (name its ``feed_state``).
    disconnected = True

    def __init__(self, source: str = "", underlying: str = "") -> None:
        self.source = str(source or "").strip().lower()
        self.underlying = str(underlying or "").strip().upper()
        self._contracts: dict[str, Any] = {}

    @property
    def source_name(self) -> str:
        return f"disconnected:{self.source}" if self.source else "disconnected"

    @property
    def detail(self) -> str:
        """The operator-facing reason: what is missing and what it costs."""
        where = f" on {self.underlying}" if self.underlying else ""
        return (
            f"{self.reason}: no authenticated {self.source or 'broker'} session{where} "
            "— marks stay frozen at their last honest value and new entries are "
            "refused until the broker logs in"
        )

    def _unavailable(self) -> dict[str, Any]:
        return {
            "ltp": 0.0,
            "bid": 0.0,
            "ask": 0.0,
            "volume": 0,
            "oi": 0,
            "error": self.detail,
            "disconnected": True,
        }

    # -- QuoteProvider protocol ------------------------------------------

    def get_quote(
        self, instrument_token: str, *, numeric_token: Any = None
    ) -> dict[str, Any]:
        return self._unavailable()

    def get_quotes_bulk(self, tokens: list[str]) -> dict[str, dict[str, Any]]:
        return {str(token): self._unavailable() for token in (tokens or [])}

    def execution_quote(self, instrument_token: str, side: str) -> dict[str, Any]:
        raise NoExecutableQuoteError(
            f"{instrument_token}: cannot {str(side).upper()} while disconnected "
            f"({self.detail})"
        )

    def mark_quote(self, instrument_token: str, position_side: str) -> dict[str, Any]:
        # Failed row, not a fabricated one: the caller keeps the last honest
        # mark and counts the failure (the same path a dead session takes).
        return self._unavailable()

    # -- registry (protocol symmetry; nothing here can price) -------------

    def register_contract(self, contract: Any) -> None:
        token = str(getattr(contract, "instrument_token", "") or "")
        if token:
            self._contracts[token] = contract

    def register_chain(self, chain: Any) -> None:
        contracts = chain.values() if hasattr(chain, "values") else chain
        for contract in contracts:
            self.register_contract(contract)

    def invalidate(self, instrument_token: str | None = None) -> None:
        """No cache to evict — every answer is already "unavailable"."""
        return None


# ---------------------------------------------------------------------------
# Subscription tokens — one key format per venue, one code path for both
# ---------------------------------------------------------------------------

#: What a subscription token can be: mStock's numeric instrument token, or an
#: Upstox-style opaque instrument key (``NSE_FO|NIFTY|…``).
RingToken = int | str


def normalize_token(raw: Any) -> RingToken:
    """A token as the ring stores it — the venue's own format, never a guess.

    A pure-digit spelling becomes ``int`` so the mStock ring is bit-for-bit what
    it was (the broker, its ``_ws_subscribed_tokens`` set and the pinned test
    tables all speak ``int``); anything else is kept verbatim as an opaque key,
    because a venue that names its contracts with strings has no numeric form to
    fall back to. Empty/None has no token and must be skipped by the caller.
    """
    text = "" if raw is None else str(raw).strip()
    if not text:
        return ""
    if text.isdigit():
        try:
            return int(text)
        except (OverflowError, ValueError):  # pragma: no cover — defensive
            return text
    return text


def token_text(token: Any) -> str:
    """The registry spelling of a token — how a token looks up its contract."""
    return "" if token is None else str(token).strip()


def token_sort_key(token: Any) -> tuple[int, int, str]:
    """Order tokens without comparing an ``int`` to a ``str``.

    A ring that mixes venues (a migrated book whose held legs are numeric while
    its fresh legs are keys) must still sort: ``sorted(mixed)`` raises
    ``TypeError`` and would take the recenter — and every subscription behind it
    — down with it.
    """
    text = token_text(token)
    return (0, int(text), "") if text.isdigit() else (1, 0, text)


# ---------------------------------------------------------------------------
# HotStrikeRing (Plan §3.2 — ATM ± 6 CE/PE MODE_FULL pre-subscription)
# ---------------------------------------------------------------------------


class HotStrikeRing:
    """Maintains pre-subscribed ``ATM ± 6`` CE & PE option tokens in ``MODE_FULL``.

    * Default strike steps: ``NIFTY=50``, ``BANKNIFTY=100``, ``FINNIFTY=50``,
      ``MIDCPNIFTY=25``, ``SENSEX=100`` (inferred from chain strikes otherwise).
    * Inner subscription band: ``ATM ± half_width`` (default ``6`` → 13 strikes ×
      CE/PE = ``26`` tokens per underlying), plus optional OTM strangle wings
      registered via :meth:`register_wing_distance`.
    * Hysteresis eviction band: ``ATM ± evict_half_width`` (default ``8``).
      Re-centering occurs only when ``ATM`` moves by ``>= 1`` strike step; tokens
      inside ``ATM ± 8`` remain subscribed to avoid churn on minor oscillations,
      and pinned open-position tokens are **never** evicted.
    * Tokens are stored in the venue's own spelling (:func:`normalize_token`):
      ``int`` for a numeric broker, opaque ``str`` for a key-shaped one. The ring
      never converts between the two — translation to the wire format belongs to
      ``subscribe_tokens_ws`` on the broker that owns the socket.
    """

    STRIKE_STEPS: dict[str, int] = {
        "NIFTY": 50,
        "BANKNIFTY": 100,
        "FINNIFTY": 50,
        "MIDCPNIFTY": 25,
        "SENSEX": 100,
    }

    def __init__(
        self,
        broker: Any,
        *,
        half_width: int | None = None,
        evict_half_width: int | None = None,
        ring_mode: str | None = None,
        max_ring_tokens: int | None = None,
        on_contract: Callable[[Any], None] | None = None,
    ) -> None:
        resolved_half = (
            int(os.getenv("MSTOCK_HOT_RING_HALF_WIDTH", "6"))
            if half_width is None
            else int(half_width)
        )
        resolved_evict = (
            max(resolved_half, int(os.getenv("MSTOCK_HOT_RING_EVICT_HALF_WIDTH", "8")))
            if evict_half_width is None
            else int(evict_half_width)
        )
        if resolved_evict < resolved_half:
            raise ValueError("evict_half_width must be >= half_width")
        resolved_mode = (
            os.getenv("MSTOCK_HOT_RING_MODE", "full")
            if ring_mode is None
            else str(ring_mode)
        ).strip().lower() or "full"
        resolved_max = (
            int(os.getenv("MSTOCK_HOT_RING_MAX_TOKENS", "64"))
            if max_ring_tokens is None
            else int(max_ring_tokens)
        )

        self.broker = broker
        self.half_width = resolved_half
        self.evict_half_width = resolved_evict
        self.ring_mode = resolved_mode
        self.max_ring_tokens = max(0, resolved_max)
        self._on_contract = on_contract
        self._lock = threading.RLock()
        self._center_strike: dict[str, float] = {}
        self._step_by_underlying: dict[str, float] = {}
        self._expiry_by_underlying: dict[str, date] = {}
        self._ring_tokens: dict[str, dict[int, float]] = {}  # underlying -> {token: strike}
        self._wing_tokens_by_underlying: dict[str, set[int]] = {}
        self._wing_distances_pct: dict[str, set[float]] = {}
        self._dirty_wings: set[str] = set()
        #: underlyings already reported by :meth:`recenter` as an empty ring.
        self._empty_ring_warned: set[str] = set()
        self._pinned_tokens: dict[int, int] = {}

        add_reinit = getattr(broker, "add_ws_reinit_listener", None)
        if callable(add_reinit):
            try:
                add_reinit(self._on_broker_ws_reinit)
            except Exception:  # noqa: BLE001
                logger.debug("Could not attach ws reinit listener", exc_info=True)

    def _broker_subscribed_snapshot(self) -> set[RingToken] | None:
        """What the socket believes it holds, in the ring's own spelling.

        Normalized through :func:`normalize_token` so the membership tests below
        compare like with like: a broker that stores ``26001`` while the ring
        holds ``"26001"`` (or the other way round) would report every token as
        missing and re-subscribe the whole ring on every bar.
        """
        accessor = getattr(self.broker, "ws_subscribed_tokens", None)
        if callable(accessor):
            try:
                snap = accessor()
            except Exception:  # noqa: BLE001
                snap = None
            if isinstance(snap, (set, frozenset)):
                return {normalize_token(tok) for tok in snap}
        raw = getattr(self.broker, "__dict__", {}).get("_ws_subscribed_tokens")
        if isinstance(raw, (set, frozenset)):
            return {normalize_token(tok) for tok in raw}
        return None

    def _on_broker_ws_reinit(self) -> None:
        """Re-apply active ring and pinned subscriptions after a TOTP WS re-init."""
        with self._lock:
            ring_all: list[RingToken] = sorted(
                {tok for m in self._ring_tokens.values() for tok in m},
                key=token_sort_key,
            )
            pinned_all: list[RingToken] = sorted(
                (tok for tok, cnt in self._pinned_tokens.items() if cnt > 0),
                key=token_sort_key,
            )
        broker_sub = self._broker_subscribed_snapshot()
        if broker_sub is not None:
            missing_ring = [t for t in ring_all if t not in broker_sub]
            missing_pinned = [t for t in pinned_all if t not in broker_sub]
        else:
            missing_ring = ring_all
            missing_pinned = pinned_all
        if missing_ring:
            self._subscribe_ws(missing_ring, mode=self.ring_mode)
        if missing_pinned:
            self._subscribe_ws(missing_pinned, mode="full")

    def register_wing_distance(self, underlying: str, distance_pct: float) -> None:
        """Include ``±distance_pct`` OTM strangle strikes in the pre-subscribed ring."""
        dist = abs(float(distance_pct))
        if dist <= 0.0:
            return
        key = str(underlying).upper()
        with self._lock:
            wings = self._wing_distances_pct.setdefault(key, set())
            if dist not in wings:
                wings.add(dist)
                self._dirty_wings.add(key)

    def pin_tokens(self, tokens: Iterable[Any]) -> list[RingToken]:
        """Protect held open-leg tokens from ever being evicted by re-centering."""
        pinned: list[RingToken] = []
        with self._lock:
            for raw in tokens:
                tok = normalize_token(raw)
                # A numeric token must be positive; a key-shaped one is opaque
                # and its content is the broker's problem, not ours.
                if not tok or (isinstance(tok, int) and tok <= 0):
                    continue
                self._pinned_tokens[tok] = self._pinned_tokens.get(tok, 0) + 1
                pinned.append(tok)
        pin_fn = getattr(self.broker, "pin_ws_tokens", None)
        if pinned and callable(pin_fn):
            pin_fn(pinned, mode="full")
        elif pinned:
            self._subscribe_ws(sorted(set(pinned), key=token_sort_key), mode="full")
        return pinned

    def unpin_tokens(self, tokens: Iterable[Any]) -> list[RingToken]:
        """Release pin refcount 1-for-1 on both ring and broker; evict stale OOTM legs."""
        decremented: list[RingToken] = []
        zeroed: list[RingToken] = []
        stale_evict: list[RingToken] = []
        with self._lock:
            for raw in tokens:
                tok = normalize_token(raw)
                cur = self._pinned_tokens.get(tok, 0)
                if cur <= 0:
                    continue
                decremented.append(tok)
                if cur == 1:
                    self._pinned_tokens.pop(tok, None)
                    zeroed.append(tok)
                else:
                    self._pinned_tokens[tok] = cur - 1

            # Any ring token whose pin hit 0 after spot already moved outside
            # ATM ± evict_half_width is evicted and unsubscribed immediately.
            for tok in zeroed:
                evicted_from_ring = False
                still_needed = False
                for u_key, tok_map in self._ring_tokens.items():
                    if tok not in tok_map:
                        continue
                    center = self._center_strike.get(u_key)
                    step = self._step_by_underlying.get(u_key, 50.0)
                    wings = self._wing_tokens_by_underlying.get(u_key, set())
                    outer_r = self.evict_half_width * step + 1e-6
                    if (
                        center is not None
                        and abs(tok_map[tok] - center) > outer_r
                        and tok not in wings
                    ):
                        tok_map.pop(tok, None)
                        evicted_from_ring = True
                    else:
                        still_needed = True
                if evicted_from_ring and not still_needed:
                    stale_evict.append(tok)

        unpin_fn = getattr(self.broker, "unpin_ws_tokens", None)
        if decremented and callable(unpin_fn):
            unpin_fn(decremented)
        if stale_evict:
            self._unsubscribe_ws(sorted(set(stale_evict), key=token_sort_key))
        return zeroed

    def subscribed_tokens(self, underlying: str | None = None) -> set[RingToken]:
        """Return currently tracked HotStrikeRing tokens (venue's own spelling)."""
        with self._lock:
            if underlying is not None:
                return set(self._ring_tokens.get(str(underlying).upper(), {}).keys())
            out: set[RingToken] = set()
            for tok_map in self._ring_tokens.values():
                out.update(tok_map.keys())
            return out

    def center_strike(self, underlying: str) -> float | None:
        with self._lock:
            return self._center_strike.get(str(underlying).upper())

    @classmethod
    def _infer_step(cls, underlying: str, strikes: Sequence[float]) -> float:
        preset = cls.STRIKE_STEPS.get(underlying)
        if preset:
            return float(preset)
        unique_sorted = sorted({float(s) for s in strikes if float(s) > 0})
        diffs = [
            round(unique_sorted[i + 1] - unique_sorted[i], 4)
            for i in range(len(unique_sorted) - 1)
            if unique_sorted[i + 1] > unique_sorted[i]
        ]
        return float(min(diffs)) if diffs else 50.0

    def _subscribe_ws(
        self, tokens: Sequence[RingToken], *, mode: str | None = None
    ) -> list[Any]:
        """Hand tokens to the broker that owns the socket — it does the translation.

        The ring deliberately passes its keys through: ``MStockBroker.subscribe_tokens_ws``
        wants ``int``, an Upstox feed wants ``NSE_FO|…``, and a second conversion here
        would be a second place the two venues can disagree.
        """
        if not tokens:
            return []
        use_mode = self.ring_mode if mode is None else mode
        batch_fn = getattr(self.broker, "subscribe_tokens_ws", None)
        if callable(batch_fn):
            return list(batch_fn(tokens, mode=use_mode))
        single_fn = getattr(self.broker, "subscribe_to_instrument", None)
        if callable(single_fn):
            ok = [tok for tok in tokens if single_fn(tok, mode=use_mode)]
            return ok
        return []

    def _unsubscribe_ws(self, tokens: Sequence[RingToken]) -> list[Any]:
        if not tokens:
            return []
        unsub_fn = getattr(self.broker, "unsubscribe_tokens_ws", None)
        if callable(unsub_fn):
            return list(unsub_fn(tokens))
        return []

    def recenter(
        self,
        underlying: str,
        spot: float,
        contracts: Sequence[Any],
        *,
        reference_date: date | None = None,
        force: bool = False,
    ) -> dict[str, list[Any]]:
        """Center ``ATM ± 6`` CE/PE subscriptions around ``spot`` with hysteresis.

        Returns the broker's own answer — ``{"subscribed": [...], "unsubscribed":
        [...]}`` and nothing else. Several pinned tests compare that dict by
        equality, so an extra diagnostic key here is a behaviour change; a ring
        that ends up empty is reported by log line instead (see below).
        """
        key = str(underlying).upper()
        spot_val = float(spot)
        if spot_val <= 0 or not contracts:
            return {"subscribed": [], "unsubscribed": []}

        ref = reference_date or date.today()
        future_expiries = sorted(
            {
                c.expiry
                for c in contracts
                if getattr(c, "expiry", None) is not None and c.expiry >= ref
            }
        )
        if not future_expiries:
            return {"subscribed": [], "unsubscribed": []}
        nearest_expiry = future_expiries[0]

        expiry_contracts = [c for c in contracts if getattr(c, "expiry", None) == nearest_expiry]
        if not expiry_contracts:
            return {"subscribed": [], "unsubscribed": []}

        step = self._infer_step(key, [float(c.strike) for c in expiry_contracts])
        new_atm = round(spot_val / step) * step
        broker_sub = self._broker_subscribed_snapshot()

        with self._lock:
            prev_atm = self._center_strike.get(key)
            prev_expiry = self._expiry_by_underlying.get(key)
            wings_dirty = key in self._dirty_wings
            current_existing = self._ring_tokens.get(key, {})
            missing_on_socket = bool(
                broker_sub is not None
                and current_existing
                and any(tok not in broker_sub for tok in current_existing)
            )
            if (
                not force
                and not wings_dirty
                and not missing_on_socket
                and prev_atm is not None
                and prev_expiry == nearest_expiry
                and abs(new_atm - prev_atm) < (step - 1e-6)
            ):
                return {"subscribed": [], "unsubscribed": []}

            wing_pcts = set(self._wing_distances_pct.get(key, set()))
            self._dirty_wings.discard(key)

        inner_radius = self.half_width * step + 1e-6
        outer_radius = self.evict_half_width * step + 1e-6

        desired_tokens: dict[RingToken, float] = {}
        wing_tokens: set[RingToken] = set()
        contracts_to_register: list[Any] = []
        tokenless = 0

        for c in expiry_contracts:
            tok = normalize_token(getattr(c, "instrument_token", ""))
            if not tok:
                tokenless += 1
                continue
            strike = float(c.strike)
            raw_type = str(getattr(c.option_type, "value", c.option_type)).upper()
            is_ce = raw_type.startswith("C")

            in_inner = abs(strike - new_atm) <= inner_radius
            in_wing = False
            for pct in wing_pcts:
                target = (
                    spot_val * (1.0 + pct / 100.0)
                    if is_ce
                    else spot_val * (1.0 - pct / 100.0)
                )
                if abs(strike - target) <= (step + 1e-6):
                    in_wing = True
                    break

            if in_inner or in_wing:
                desired_tokens[tok] = strike
                contracts_to_register.append(c)
                if in_wing:
                    wing_tokens.add(tok)

        if not desired_tokens:
            # The silent zero-subscription path. A ring that selects nothing is how
            # a venue whose token spelling this code could not read ends up trading
            # with no depth at all — every leg then answers from LTP-only HTTP and
            # the fill seam refuses without ever saying why. It has to be a line in
            # the log, because the return dict is compared by equality in pinned
            # tests and cannot gain a diagnostic key. Once per underlying until the
            # ring fills: a book with no tokens recenters every bar and would drown
            # the log in the one message that matters.
            with self._lock:
                first = key not in self._empty_ring_warned
                if first:
                    self._empty_ring_warned.add(key)
            if first:
                logger.warning(
                    "HOT_RING_EMPTY underlying=%s expiry=%s contracts=%d tokenless=%d "
                    "atm=%.2f step=%.2f — nothing subscribed, so this underlying has no "
                    "WebSocket depth",
                    key, nearest_expiry, len(expiry_contracts), tokenless, new_atm, step,
                )
        else:
            with self._lock:
                self._empty_ring_warned.discard(key)

        if self._on_contract is not None:
            for c in contracts_to_register:
                self._on_contract(c)

        with self._lock:
            # Enforce global non-pinned ring token budget across underlyings
            # (closest strikes to new_atm take priority if capped).
            if self.max_ring_tokens > 0:
                other_count = sum(
                    1
                    for u_k, m in self._ring_tokens.items()
                    if u_k != key
                    for tok in m
                    if self._pinned_tokens.get(tok, 0) <= 0
                )
                budget = max(0, self.max_ring_tokens - other_count)
                if len(desired_tokens) > budget:
                    prioritized = sorted(
                        desired_tokens,
                        key=lambda t: (abs(desired_tokens[t] - new_atm), token_sort_key(t)),
                    )[:budget]
                    keep_set = set(prioritized)
                    desired_tokens = {
                        t: s for t, s in desired_tokens.items() if t in keep_set
                    }
                    wing_tokens &= keep_set

            current_map = dict(self._ring_tokens.get(key, {}))
            expiry_changed = prev_expiry is not None and prev_expiry != nearest_expiry
            to_subscribe = sorted(
                (
                    tok
                    for tok in desired_tokens
                    if tok not in current_map
                    or (broker_sub is not None and tok not in broker_sub)
                ),
                key=token_sort_key,
            )

            to_evict: list[RingToken] = []
            next_map: dict[RingToken, float] = {}
            for tok, old_strike in current_map.items():
                if tok in desired_tokens:
                    next_map[tok] = desired_tokens[tok]
                    continue
                pinned = self._pinned_tokens.get(tok, 0) > 0
                outside_hysteresis = expiry_changed or (
                    abs(old_strike - new_atm) > outer_radius
                )
                if outside_hysteresis and tok not in wing_tokens and not pinned:
                    to_evict.append(tok)
                else:
                    # Inside hysteresis band (ATM ± 8) or pinned: retain!
                    next_map[tok] = old_strike
                    if broker_sub is not None and tok not in broker_sub:
                        to_subscribe.append(tok)

            for tok, strike in desired_tokens.items():
                next_map[tok] = strike

            to_subscribe = sorted(set(to_subscribe), key=token_sort_key)
            self._ring_tokens[key] = next_map
            self._wing_tokens_by_underlying[key] = wing_tokens
            self._center_strike[key] = new_atm
            self._step_by_underlying[key] = step
            self._expiry_by_underlying[key] = nearest_expiry

        subscribed = self._subscribe_ws(to_subscribe, mode=self.ring_mode)
        unsubscribed = self._unsubscribe_ws(sorted(to_evict, key=token_sort_key))
        return {"subscribed": subscribed, "unsubscribed": unsubscribed}

    def release_underlying(self, underlying: str) -> list[Any]:
        """Unsubscribe all non-pinned ring tokens for ``underlying`` on bus eviction."""
        key = str(underlying).upper()
        with self._lock:
            current_map = self._ring_tokens.pop(key, {})
            self._wing_tokens_by_underlying.pop(key, None)
            self._center_strike.pop(key, None)
            self._step_by_underlying.pop(key, None)
            self._expiry_by_underlying.pop(key, None)
            self._wing_distances_pct.pop(key, None)
            self._dirty_wings.discard(key)
            self._empty_ring_warned.discard(key)
            removable = sorted(
                (tok for tok in current_map if self._pinned_tokens.get(tok, 0) <= 0),
                key=token_sort_key,
            )
            no_rings_left = not self._ring_tokens and not self._pinned_tokens
        if no_rings_left:
            rem_reinit = getattr(self.broker, "remove_ws_reinit_listener", None)
            if callable(rem_reinit):
                try:
                    rem_reinit(self._on_broker_ws_reinit)
                except Exception:  # noqa: BLE001
                    pass
        return self._unsubscribe_ws(removable)


# ---------------------------------------------------------------------------
# LiveChainProvider (P1.1 — real mStock chain behind the synthetic duck type)
# ---------------------------------------------------------------------------


class LiveChainProvider:
    """Real mStock option chain + LTP behind the *generator* duck type.

    P1.1 — the last synthetic gap in the runner path. When an option runner
    runs with ``source="mstock"`` and an authenticated session, this class
    replaces the ``SyntheticChainGenerator`` + ``SyntheticQuoteProvider``
    pair inside the runner's :class:`~backtest.forward.options_bridge.OptionsBridge`
    so chains, strikes and MTM come from the broker, not Black-Scholes.

    One object, two surfaces (the bridge holds it as its ``quote_provider``
    and reaches the generator surface through ``.generator``):

    * **Quote surface** (:class:`~backtest.options.paper_trading.OptionPaperBroker`
      contract) — ``get_quote(token)`` returns real LTP/bid/ask through a TTL
      cache; ``register_chain(chain)`` keeps the token → contract registry.
    * **Generator surface** (``OptionsBridge`` contract) —
      ``generate_chain``/``available_expiries`` serve the *contract terms*
      (strike/expiry/lot/token/symbol) fetched from the instrument master —
      ONE API call per underlying per ``chain_ttl_seconds`` no matter how many
      runners share it (the §5.2 rate-limit rule); ``get_spot``/``set_spot``
      carry the real underlying spot pushed in from the runner's bars
      (``OptionsBridge._sync_market`` calls ``set_spot`` already — no bridge
      change needed); ``price_contract`` returns the live LTP.

    Fail semantics: quotes and chain fetches are error-soft (log + last-known
    or zero-quote), matching :class:`LiveQuoteProvider`. ``get_spot`` raises
    ``ValueError`` until the first real spot arrives — no real spot means no
    strike selection, i.e. **no trading until a real bar shows up** (never a
    silent fall-back to synthetic scale). ``set_reference`` is accepted and
    ignored: the live clock is the market's clock.
    """

    #: Underlying → starting spot while no real bar has arrived. Used ONLY
    #: to answer ``get_spot`` before the first bar; a runner that trades on
    #: it would be trading a guess — so ``require_live_spot`` (default True)
    #: makes ``get_spot`` raise instead. Keep for research/read-only use.
    DEFAULT_SPOTS = dict(SyntheticChainGenerator.DEFAULT_SPOTS)

    def __init__(
        self, broker: Any, quote_ttl_seconds: int = 5, chain_ttl_seconds: int = 900,
        require_live_spot: bool = True,
    ) -> None:
        self.broker = broker
        self._quotes = LiveQuoteProvider(broker, cache_ttl_seconds=quote_ttl_seconds)
        self._chain_ttl = float(chain_ttl_seconds)
        self._require_live_spot = bool(require_live_spot)
        self._chain_cache: dict[str, tuple[list[Any], float]] = {}
        self._contracts: dict[str, Any] = {}  # instrument_token → OptionContract
        self._spots: dict[str, float] = {}
        self.hot_ring = HotStrikeRing(broker, on_contract=self.register_contract)
        #: ``hot_ring``'s lock is private; peer-ladder reads have their own.
        self._ring_lock = threading.Lock()
        self._warming_underlyings: set[str] = set()
        #: underlying → (monotonic stamp, peer rows) for :meth:`ring_book`.
        self._parity_cache: dict[str, tuple[float, list]] = {}

    # -- identity -----------------------------------------------------------

    @property
    def source_name(self) -> str:
        return f"live:{broker_label(self.broker)}"

    @property
    def generator(self) -> "LiveChainProvider":
        """The generator surface is this object (``OptionsBridge._generator``)."""
        return self

    def __repr__(self) -> str:  # pragma: no cover — debug helper
        return f"<LiveChainProvider underlying_contracts={len(self._contracts)}>"

    # -- quote surface (OptionPaperBroker) -----------------------------------

    def register_chain(self, chain: Any) -> None:
        """Remember token → contract for every contract in ``chain``.

        Accepts a ``{strike: contract}`` dict or a plain iterable of
        contracts (the bridge's flattened two-sided chains).
        """
        contracts = chain.values() if hasattr(chain, "values") else chain
        for contract in contracts:
            self.register_contract(contract)

    def register_contract(self, contract: Any) -> None:
        """Remember token → contract for one contract (R3/R5 restart recovery).

        Restored open structures carry real numeric mStock tokens on their legs,
        while the fresh process's ``LiveChainProvider`` starts with an empty
        registry. Registering each restored leg here restores the
        ``instrument_token → trading_symbol`` translation for ``get_quote``.
        """
        token = str(getattr(contract, "instrument_token", "") or "")
        if token:
            self._contracts[token] = contract
        self._quotes.register_contract(contract)

    def invalidate(self, instrument_token: str | None = None) -> None:
        """Evict cached quote(s) so a manual/watchdog refresh fetches live LTP."""
        if instrument_token is None:
            self._quotes.clear()
            return
        key = str(instrument_token)
        self._quotes.invalidate(key)
        contract = self._contracts.get(key)
        if contract is not None and getattr(contract, "trading_symbol", None):
            self._quotes.invalidate(str(contract.trading_symbol))

    def contract_for(self, instrument_token: str) -> Any:
        """The registered terms for one token, or None.

        Read-only by design: it exists so the shadow cross-broker witness
        (:mod:`backtest.data.cross_broker_shadow`) can name the contract behind a
        ring token. No trading path may branch on it.
        """
        return self._contracts.get(str(instrument_token))

    #: How long a peer-ladder read may be reused. The parity reference is a median
    #: over two dozen legs; re-reading them for every mark of every book would make
    #: the witness cost the trading path it exists to observe. The leg under test
    #: never comes from here — only its peers, whose median a half-second of lag
    #: cannot move by anything close to the band.
    PARITY_READ_TTL_S = 0.5
    #: Peer freshness, matching the ring's own WS read window
    #: (``MStockBroker._try_ws_quote``): a peer the feed stopped printing drops out
    #: of the median instead of anchoring it to an old book.
    PARITY_QUOTE_MAX_AGE_S = 10.0

    def ring_book(self, underlying: str) -> list[tuple[Any, dict[str, Any]]]:
        """Every contract the hot ring is quoting for ``underlying``, with its row.

        Read-only, like :meth:`contract_for`: the peer set for ``pair_parity`` and
        for the shadow scan that calibrates its band. Only tokens already in the
        ring are read, and only through the broker's WebSocket cache — a peer that
        is not subscribed is skipped rather than fetched, so building a reference
        can never spend an HTTP quote request or trigger a subscription.
        """
        key = str(underlying).upper()
        now = time.monotonic()
        with self._ring_lock:
            hit = self._parity_cache.get(key)
            if hit is not None and now - hit[0] < self.PARITY_READ_TTL_S:
                return list(hit[1])
        getter = getattr(self.broker, "get_ws_quote", None)
        out: list[tuple[Any, dict[str, Any]]] = []
        if callable(getter):
            for token in sorted(self.hot_ring.subscribed_tokens(key), key=token_sort_key):
                contract = self._contracts.get(token_text(token))
                if contract is None:
                    continue
                try:
                    # The token goes to the broker exactly as the ring holds it:
                    # ``int`` for mStock's cache, an opaque key for a key-shaped
                    # venue. ``int(token)`` here used to raise — and be swallowed
                    # by the ``except`` below — so a key-shaped peer silently
                    # vanished from the parity reference instead of failing loudly.
                    row = getter(token, max_age_seconds=self.PARITY_QUOTE_MAX_AGE_S) or {}
                except (TypeError, ValueError):
                    continue
                except Exception:  # noqa: BLE001 — a peer is allowed to be missing
                    continue
                if row:
                    out.append((contract, row))
        with self._ring_lock:
            self._parity_cache[key] = (now, out)
        return list(out)

    def parity_siblings(self, instrument_token: str) -> list[tuple[Any, dict[str, Any]]]:
        """The ring's other contracts that share this leg's expiry.

        Read-only (:meth:`ring_book` says why); feeds the seam's ``pair_parity``
        check. A leg whose terms are unknown, or whose expiry has no peers in the
        ring, gets an empty list and the check abstains.
        """
        contract = self._contracts.get(str(instrument_token))
        expiry = getattr(contract, "expiry", None) if contract is not None else None
        underlying = (
            str(getattr(contract, "underlying", "") or "").upper() if contract else ""
        )
        if expiry is None or not underlying:
            return []
        own = str(getattr(contract, "instrument_token", instrument_token))
        return [
            (peer, row)
            for peer, row in self.ring_book(underlying)
            if getattr(peer, "expiry", None) == expiry
            and str(getattr(peer, "instrument_token", "")) != own
        ]

    def peer_l1(self, instrument_token: str) -> Optional[dict[str, Any]]:
        """The second broker's L1 for this token, when a mirror is running.

        Read-only, same rule as :meth:`ring_book`: it asks the shadow cross-broker
        witness (:mod:`backtest.data.cross_broker_shadow`) what its socket holds,
        so a corroboration check can never spend an HTTP quote request, open a
        subscription, or reach a broker the book is not already mirrored onto.
        ``None`` means "no second opinion" — no mirror for this underlying, the
        token unmapped on the peer's terms, or the import unavailable — and the
        caller abstains rather than refusing.
        """
        contract = self._contracts.get(str(instrument_token))
        underlying = str(getattr(contract, "underlying", "") or "").upper()
        if not underlying:
            return None
        try:
            from backtest.data import cross_broker_shadow

            mirror = cross_broker_shadow.active().get(underlying)
            return mirror.l1_for(str(instrument_token)) if mirror is not None else None
        except Exception:  # noqa: BLE001 — a missing witness must not break quoting
            logger.debug("peer_l1 lookup failed for %s", instrument_token, exc_info=True)
            return None

    def get_quote(self, instrument_token: str) -> dict[str, Any]:
        """Real L1 quote (TTL-cached) for one contract token.

        mStock's quote endpoint keys on the TRADING SYMBOL, not the numeric
        token (verified live 2026-09-18: ``NFO:NIFTY26SEP23300CE`` → 200 with
        ``last_price``; ``NFO:73923`` → "Invalid symbol"). Translate through
        the registered chain contracts; unknown keys pass through unchanged.

        The numeric token is passed down as well: it is the only key the
        WebSocket depth cache understands, and that cache is the only source
        of a two-sided quote (the symbol route is LTP-only).
        """
        key = str(instrument_token)
        contract = self._contracts.get(key)
        # The contract's own token is the WS cache key whatever the venue spells
        # it as; the digit test is only for a token that has no registered
        # contract at all (a restored leg before the chain reload).
        if contract is not None:
            numeric = getattr(contract, "instrument_token", None) or None
        else:
            numeric = key if key.isdigit() else None
        if contract is not None and getattr(contract, "trading_symbol", None):
            key = str(contract.trading_symbol)
        return self._quotes.get_quote(key, numeric_token=numeric)

    def get_quotes_bulk(self, tokens: list[str]) -> dict[str, dict[str, Any]]:
        """Quotes for many tokens (each individually error-soft)."""
        return {token: self.get_quote(token) for token in tokens}

    # -- HotStrikeRing lifecycle (Plan §3.2) ---------------------------------

    def has_ws_ring_support(self) -> bool:
        """True when ``self.broker`` has an initialized WebSocket feed."""
        ws_feed = getattr(self.broker, "__dict__", {}).get("_ws_feed")
        if ws_feed is None:
            return False
        return callable(
            getattr(self.broker, "subscribe_tokens_ws", None)
            or getattr(self.broker, "subscribe_to_instrument", None)
        )

    def _infer_spot_from_ws(self, underlying: str) -> float | None:
        key = str(underlying).upper()
        get_ws = getattr(self.broker, "get_ws_quote", None)
        if not callable(get_ws):
            return None
        # The broker that owns the book knows its own index spelling: mStock's
        # numeric UNDERLYING_INDEX_TOKENS, Upstox's NSE_INDEX|… keys via its
        # resolver. Never a second key table here (impl plan Phase 1 item 5) —
        # and an underlying the venue cannot name makes spot abstain, because a
        # stale or invented center re-strikes the whole ring.
        resolver = getattr(self.broker, "underlying_index_key", None)
        if callable(resolver):
            tok = resolver(key)
        else:
            index_map = getattr(
                self.broker,
                "UNDERLYING_INDEX_TOKENS",
                {"NIFTY": 26000, "BANKNIFTY": 26009, "MIDCPNIFTY": 26013, "FINNIFTY": 26037},
            )
            tok = index_map.get(key)
        if tok is None:
            return None
        try:
            q = get_ws(tok, max_age_seconds=30.0) or {}
            ltp = float(q.get("ltp", 0.0) or 0.0)
            if ltp > 0.0:
                return ltp
        except Exception:  # noqa: BLE001
            return None
        return None

    def register_wing_distance(self, underlying: str, distance_pct: float) -> None:
        """Register strangle OTM wing distance (%) for ``HotStrikeRing``."""
        key = str(underlying).upper()
        self.hot_ring.register_wing_distance(key, distance_pct)
        spot = self._spots.get(key)
        cached = self._chain_cache.get(key)
        if spot is not None and cached is not None and self.has_ws_ring_support():
            self.hot_ring.recenter(key, spot, cached[0])

    def pin_contracts(self, contracts_or_tokens: Iterable[Any]) -> list[int]:
        """Pin held open-position contracts/tokens so ring hysteresis never evicts them."""
        tokens: list[int | str] = []
        for item in contracts_or_tokens:
            if hasattr(item, "instrument_token"):
                self.register_contract(item)
                tokens.append(getattr(item, "instrument_token"))
            else:
                tokens.append(item)
        return self.hot_ring.pin_tokens(tokens)

    def unpin_contracts(self, contracts_or_tokens: Iterable[Any]) -> list[int]:
        """Release pin protection for closed option contracts/tokens."""
        tokens: list[int | str] = [
            getattr(item, "instrument_token")
            if hasattr(item, "instrument_token")
            else item
            for item in contracts_or_tokens
        ]
        return self.hot_ring.unpin_tokens(tokens)

    def warm_chain_and_ring(
        self,
        underlying: str,
        spot: float | None = None,
        *,
        wing_distance_pct: float | None = None,
        async_warm: bool = False,
    ) -> dict[str, list[int]]:
        """Pre-warm option chain terms and ``ATM ± 6`` WS depth subscriptions."""
        key = str(underlying).upper()
        if wing_distance_pct is not None and wing_distance_pct > 0:
            self.hot_ring.register_wing_distance(key, wing_distance_pct)

        def _run() -> dict[str, list[int]]:
            try:
                contracts = self._fetch_chain(key)
                resolved_spot = (
                    float(spot)
                    if spot is not None and float(spot) > 0
                    else self._spots.get(key) or self._infer_spot_from_ws(key)
                )
                if resolved_spot is not None and resolved_spot > 0:
                    self._spots.setdefault(key, resolved_spot)
                    if self.has_ws_ring_support() and contracts:
                        return self.hot_ring.recenter(key, resolved_spot, contracts)
                return {"subscribed": [], "unsubscribed": []}
            finally:
                with self._ring_lock:
                    self._warming_underlyings.discard(key)

        if async_warm:
            with self._ring_lock:
                if key in self._warming_underlyings:
                    return {"subscribed": [], "unsubscribed": []}
                self._warming_underlyings.add(key)
            threading.Thread(
                target=_run,
                name=f"hot-ring-warm-{key.lower()}",
                daemon=True,
            ).start()
            return {"subscribed": [], "unsubscribed": []}

        with self._ring_lock:
            self._warming_underlyings.add(key)
        return _run()

    def release_hot_ring(self, underlying: str) -> list[int]:
        """Release pre-subscribed WS ring tokens for ``underlying``."""
        return self.hot_ring.release_underlying(underlying)

    # -- generator surface (OptionsBridge) ------------------------------------

    def set_spot(self, underlying: str, spot: float) -> None:
        """Record the real underlying spot and re-center ``HotStrikeRing`` if needed."""
        key = str(underlying).upper()
        spot_val = float(spot)
        self._spots[key] = spot_val
        if not self.has_ws_ring_support():
            return
        cached = self._chain_cache.get(key)
        if cached is not None:
            contracts, ts = cached
            if time.monotonic() - ts < self._chain_ttl and contracts:
                self.hot_ring.recenter(key, spot_val, contracts)
                return
        self.warm_chain_and_ring(key, spot_val, async_warm=True)

    def get_spot(self, underlying: str) -> float:
        """The real spot. Raises before the first real bar (fail-loud)."""
        key = str(underlying).upper()
        if key in self._spots:
            return self._spots[key]
        if self._require_live_spot:
            raise ValueError(
                f"no live spot yet for {underlying} — waiting for the first "
                "mStock bar (synthetic scale is never substituted)"
            )
        return self.DEFAULT_SPOTS.get(key, 100.0)

    def set_reference(self, reference: datetime | None) -> None:
        """Accepted for API parity — live pricing uses the market clock."""

    def _fetch_chain(self, underlying: str) -> list[Any]:
        """All contracts for ``underlying`` (instrument master, TTL-cached)."""
        now = time.monotonic()
        cached = self._chain_cache.get(underlying)
        if cached is not None:
            contracts, ts = cached
            if now - ts < self._chain_ttl:
                return contracts
        try:
            contracts = list(self.broker.get_option_chain(underlying))
        except Exception as exc:  # noqa: BLE001 — a chain hiccup must not kill the bar
            logger.error("LiveChainProvider chain fetch failed for %s: %s", underlying, exc)
            return cached[0] if cached is not None else []
        self._chain_cache[underlying] = (contracts, now)
        return contracts

    @staticmethod
    def _expiry_code(expiry: date) -> str:
        """mStock chain-data expiry code — ``"25DEC"`` style (``%d%b`` upper)."""
        return expiry.strftime("%d%b").upper()

    def available_expiries(
        self,
        underlying: str,
        count: int = 3,
        reference: date | None = None,
    ) -> list[date]:
        """Distinct real expiries for ``underlying`` (nearest-first)."""
        ref = reference or date.today()
        expiries = sorted(
            {
                c.expiry
                for c in self._fetch_chain(underlying)
                if getattr(c, "expiry", None) is not None and c.expiry >= ref
            }
        )
        return expiries[: max(int(count), 0)]

    def generate_chain(
        self,
        underlying: str,
        expiry: date | None = None,
        strikes_each_side: int | None = None,
        option_type: str = "CE",
    ) -> dict[Decimal, Any]:
        """``{strike: contract}`` from REAL contracts for one expiry + side.

        Mirrors ``SyntheticChainGenerator.generate_chain`` (the V1 chain
        shape): one contract per strike, filtered to ``expiry`` (default:
        nearest available) and ``option_type``. Raises ``ValueError`` when
        the broker serves no matching contracts — the bridge surfaces it as
        a soft rejection rather than trading an empty chain.
        """
        if option_type not in ("CE", "PE"):
            raise ValueError(f"option_type must be CE or PE, got {option_type!r}")
        contracts = self._fetch_chain(underlying)
        expiries = sorted({c.expiry for c in contracts if getattr(c, "expiry", None)})
        if expiry is None:
            future = [e for e in expiries if e >= date.today()]
            if not future:
                raise ValueError(f"no option contracts available for {underlying}")
            expiry = future[0]
        chain: dict[Decimal, Any] = {}
        for c in contracts:
            if c.expiry != expiry:
                continue
            raw_type = str(getattr(c.option_type, "value", c.option_type)).upper()
            type_code = "CE" if raw_type.startswith("C") else "PE"
            if type_code != option_type:
                continue
            chain[c.strike] = c
        if not chain:
            raise ValueError(
                f"no {option_type} contracts for {underlying} expiry {expiry} "
                f"({len(contracts)} contracts in master)"
            )
        return chain

    def price_contract(
        self,
        contract: Any,
        option_type: str = "CE",
        reference: datetime | None = None,
    ) -> float:
        """Live LTP for ``contract`` (0.0 when the quote is unavailable).

        Tries the trading symbol first (mStock keys quotes on the symbol),
        then the token — duck-typed brokers in tests key on either.
        """
        symbol = getattr(contract, "trading_symbol", None)
        token = str(getattr(contract, "instrument_token", ""))
        if symbol:
            quote = self._quotes.get_quote(
                str(symbol),
                # This IS the contract's own token — the format is the venue's
                # business, so it is handed over verbatim rather than validated
                # for digits first.
                numeric_token=token or None,
            )
            if float(quote.get("ltp", 0) or 0) > 0:
                return float(quote["ltp"])
        if token:
            quote = self.get_quote(token)
            if float(quote.get("ltp", 0) or 0) > 0:
                return float(quote["ltp"])
        return 0.0
