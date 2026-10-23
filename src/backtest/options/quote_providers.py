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
import threading
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable

from backtest.instruments.option import OptionContract
from backtest.instruments.base import OptionType

logger = logging.getLogger("backtest.options.quotes")

__all__ = [
    "SyntheticQuoteProvider",
    "LiveQuoteProvider",
    "LiveChainProvider",
    "HotStrikeRing",
    "CachedQuoteProvider",
    "BidAskQuoteProvider",
    "NoExecutableQuoteError",
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
        return datetime.now()

    def _reference_date(self, reference: date | None = None) -> date:
        """Date twin of :meth:`_reference_datetime` (same precedence)."""
        if reference is not None:
            return reference
        if self._reference is not None:
            return self._reference.date()
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
        expiry_dt = datetime.combine(contract.expiry or ref.date(), datetime.min.time())
        years = (expiry_dt - ref).total_seconds() / (365.0 * 24 * 3600)
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
        self._contracts: dict[str, Any] = {}

    @property
    def source_name(self) -> str:
        return "live:mstock"

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
            if key.isdigit():
                numeric_token = key
            elif contract is not None:
                numeric_token = getattr(contract, "instrument_token", None)
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

    Note ``OptionPaperBroker.slippage_pct`` still applies ON TOP: the
    spread is where the market stands, slippage models your own impact.
    Construct the broker with ``slippage_pct=0`` for pure-spread fills.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner

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
            # Do NOT invent a mark: label the row failed so the broker's
            # fail-closed MTM keeps the last known price and counts the
            # failure (the same path a dead broker session takes).
            quote["error"] = quote.get("error") or (
                f"no {key} quote to mark {instrument_token} (held {held})"
            )
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
        quote["ltp"] = price
        quote["execution_side"] = side
        return quote

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
        half_width: int = 6,
        evict_half_width: int = 8,
        on_contract: Callable[[Any], None] | None = None,
    ) -> None:
        if evict_half_width < half_width:
            raise ValueError("evict_half_width must be >= half_width")
        self.broker = broker
        self.half_width = int(half_width)
        self.evict_half_width = int(evict_half_width)
        self._on_contract = on_contract
        self._lock = threading.RLock()
        self._center_strike: dict[str, float] = {}
        self._expiry_by_underlying: dict[str, date] = {}
        self._ring_tokens: dict[str, dict[int, float]] = {}  # underlying -> {token: strike}
        self._wing_distances_pct: dict[str, set[float]] = {}
        self._dirty_wings: set[str] = set()
        self._pinned_tokens: dict[int, int] = {}

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

    def pin_tokens(self, tokens: Iterable[int | str]) -> list[int]:
        """Protect held open-leg tokens from ever being evicted by re-centering."""
        pinned: list[int] = []
        with self._lock:
            for raw in tokens:
                try:
                    tok = int(str(raw).strip())
                except (TypeError, ValueError):
                    continue
                if tok <= 0:
                    continue
                self._pinned_tokens[tok] = self._pinned_tokens.get(tok, 0) + 1
                pinned.append(tok)
        pin_fn = getattr(self.broker, "pin_ws_tokens", None)
        if pinned and callable(pin_fn):
            pin_fn(pinned, mode="full")
        elif pinned:
            self._subscribe_ws(pinned)
        return pinned

    def unpin_tokens(self, tokens: Iterable[int | str]) -> list[int]:
        """Release pin refcount for closed option leg tokens."""
        released: list[int] = []
        with self._lock:
            for raw in tokens:
                try:
                    tok = int(str(raw).strip())
                except (TypeError, ValueError):
                    continue
                cur = self._pinned_tokens.get(tok, 0)
                if cur <= 1:
                    if tok in self._pinned_tokens:
                        self._pinned_tokens.pop(tok, None)
                        released.append(tok)
                else:
                    self._pinned_tokens[tok] = cur - 1
        unpin_fn = getattr(self.broker, "unpin_ws_tokens", None)
        if released and callable(unpin_fn):
            unpin_fn(released)
        return released

    def subscribed_tokens(self, underlying: str | None = None) -> set[int]:
        """Return currently tracked HotStrikeRing numeric tokens."""
        with self._lock:
            if underlying is not None:
                return set(self._ring_tokens.get(str(underlying).upper(), {}).keys())
            out: set[int] = set()
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

    def _subscribe_ws(self, tokens: list[int]) -> list[int]:
        if not tokens:
            return []
        batch_fn = getattr(self.broker, "subscribe_tokens_ws", None)
        if callable(batch_fn):
            return list(batch_fn(tokens, mode="full"))
        single_fn = getattr(self.broker, "subscribe_to_instrument", None)
        if callable(single_fn):
            ok = [tok for tok in tokens if single_fn(tok, mode="full")]
            return ok
        return []

    def _unsubscribe_ws(self, tokens: list[int]) -> list[int]:
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
    ) -> dict[str, list[int]]:
        """Center ``ATM ± 6`` CE/PE subscriptions around ``spot`` with hysteresis."""
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

        with self._lock:
            prev_atm = self._center_strike.get(key)
            prev_expiry = self._expiry_by_underlying.get(key)
            wings_dirty = key in self._dirty_wings
            if (
                not force
                and not wings_dirty
                and prev_atm is not None
                and prev_expiry == nearest_expiry
                and abs(new_atm - prev_atm) < (step - 1e-6)
            ):
                return {"subscribed": [], "unsubscribed": []}

            wing_pcts = set(self._wing_distances_pct.get(key, set()))
            self._dirty_wings.discard(key)

        inner_radius = self.half_width * step + 1e-6
        outer_radius = self.evict_half_width * step + 1e-6

        desired_tokens: dict[int, float] = {}
        wing_tokens: set[int] = set()
        contracts_to_register: list[Any] = []

        for c in expiry_contracts:
            raw_tok = str(getattr(c, "instrument_token", "") or "").strip()
            if not raw_tok.isdigit():
                continue
            tok = int(raw_tok)
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

        if self._on_contract is not None:
            for c in contracts_to_register:
                self._on_contract(c)

        with self._lock:
            current_map = dict(self._ring_tokens.get(key, {}))
            expiry_changed = prev_expiry is not None and prev_expiry != nearest_expiry
            to_subscribe = sorted(tok for tok in desired_tokens if tok not in current_map)

            to_evict: list[int] = []
            next_map: dict[int, float] = {}
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

            for tok, strike in desired_tokens.items():
                next_map[tok] = strike

            self._ring_tokens[key] = next_map
            self._center_strike[key] = new_atm
            self._expiry_by_underlying[key] = nearest_expiry

        subscribed = self._subscribe_ws(to_subscribe)
        unsubscribed = self._unsubscribe_ws(sorted(to_evict))
        return {"subscribed": subscribed, "unsubscribed": unsubscribed}

    def release_underlying(self, underlying: str) -> list[int]:
        """Unsubscribe all non-pinned ring tokens for ``underlying`` on bus eviction."""
        key = str(underlying).upper()
        with self._lock:
            current_map = self._ring_tokens.pop(key, {})
            self._center_strike.pop(key, None)
            self._expiry_by_underlying.pop(key, None)
            self._wing_distances_pct.pop(key, None)
            self._dirty_wings.discard(key)
            removable = sorted(
                tok for tok in current_map if self._pinned_tokens.get(tok, 0) <= 0
            )
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
        self,
        broker: Any,
        quote_ttl_seconds: int = 5,
        chain_ttl_seconds: int = 900,
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
        self._ring_lock = threading.Lock()
        self._warming_underlyings: set[str] = set()

    # -- identity -----------------------------------------------------------

    @property
    def source_name(self) -> str:
        return "live:mstock"

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
        numeric = key if key.isdigit() else getattr(contract, "instrument_token", None)
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
        index_map = getattr(
            self.broker,
            "UNDERLYING_INDEX_TOKENS",
            {"NIFTY": 26000, "BANKNIFTY": 26009, "MIDCPNIFTY": 26013, "FINNIFTY": 26037},
        )
        tok = index_map.get(str(underlying).upper())
        get_ws = getattr(self.broker, "get_ws_quote", None)
        if tok is not None and callable(get_ws):
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
                numeric_token=token if token.isdigit() else None,
            )
            if float(quote.get("ltp", 0) or 0) > 0:
                return float(quote["ltp"])
        if token:
            quote = self.get_quote(token)
            if float(quote.get("ltp", 0) or 0) > 0:
                return float(quote["ltp"])
        return 0.0
