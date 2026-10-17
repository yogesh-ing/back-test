"""Contract validity at the decision — PRD §4.2 / Phase 1 item #16.

The entry may trade a contract the chain published and the market is still
quoting, or it trades nothing. Three habits the V1 paper book had to unlearn:

* an ``expiry_min_days`` calendar with nothing far enough out used to fall back
  to the near expiry and keep trading — a one-bar trade by construction;
* a chain row with no lot size used to be sized at a guessed 50;
* a leg used to fill off whatever the provider's TTL cache happened to hold.

Each halt carries its own reason code so a soak can count the classes.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from backtest.forward.options_bridge import OptionsBridge
from backtest.options.execution_gate import (
    REASON_EXPIRY_TOO_NEAR,
    REASON_NO_CONTRACT,
    REASON_NO_EXPIRY,
    REASON_NO_FRESH_QUOTE,
    REASON_NO_LOT_SIZE,
    REASON_NO_STRIKE,
    ContractValidityError,
)
from backtest.options.quote_providers import SyntheticChainGenerator, SyntheticQuoteProvider
from backtest.strategy.intent import Direction, MarketView

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _NearExpiryCalendar(SyntheticChainGenerator):
    """Offers exactly one expiry, two days out — below every sane minimum."""

    def available_expiries(self, underlying, count=3, reference=None):
        ref = reference or date.today()
        return [ref + timedelta(days=2)]


class _NoLotCalendar(_NearExpiryCalendar):
    """Chain whose contracts carry no lot size (a blank master column)."""

    def generate_chain(self, underlying, expiry=None, strikes_each_side=None,
                       option_type="CE"):
        chain = super().generate_chain(
            underlying,
            expiry=expiry,
            strikes_each_side=strikes_each_side,
            option_type=option_type,
        )
        return {
            strike: replace(contract, lot_size=0) for strike, contract in chain.items()
        }


class _RecordingProvider:
    """Wraps the synthetic provider and logs the reads the gate performs."""

    def __init__(self, inner, quote=None):
        self.inner = inner
        self.quote = quote
        self.calls: list[tuple[str, str]] = []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def invalidate(self, instrument_token=None):
        self.calls.append(("invalidate", str(instrument_token)))
        inner_invalidate = getattr(self.inner, "invalidate", None)
        if callable(inner_invalidate):
            inner_invalidate(instrument_token)

    def get_quote(self, instrument_token):
        self.calls.append(("get_quote", str(instrument_token)))
        if self.quote is not None:
            return dict(self.quote)
        return self.inner.get_quote(instrument_token)


_V2 = {
    "expression_version": 2,
    "type": {"BULLISH": {"structure": "long_call", "strike_selection": "atm"}},
    "quantity": 1,
    "exit": {"stop_loss_pct": 0.5, "min_days_to_expiry": None},
}


def _bridge(expression=None, provider=None) -> OptionsBridge:
    return OptionsBridge(
        capital=1_000_000.0,
        expression=expression or _V2,
        quote_provider=provider or SyntheticQuoteProvider(SyntheticChainGenerator()),
    )


def _open(bridge: OptionsBridge):
    # A complete intent, as a template builds it: the declared rule's
    # fields ride along (structure, strike rule, expiry rule).
    rule = next(iter(bridge._strict_rules.values()))
    return bridge.on_intent(
        {
            "structure": rule.structure,
            "strike_selection": rule.strike_selection,
            "expiry_policy": rule.expiry_policy,
            "expiry_min_days": rule.expiry_min_days,
        },
        _view(),
        "s",
    )


def _view(spot: float = 24_800.0) -> MarketView:
    return MarketView(
        direction=Direction.BULLISH,
        confidence=0.9,
        underlying="NIFTY",
        spot_price=Decimal(str(spot)),
    )


# ---------------------------------------------------------------------------
# No substituted expiry
# ---------------------------------------------------------------------------


def test_a_calendar_below_expiry_min_days_halts_instead_of_substituting():
    """The old fallback traded the too-near expiry, which the exit rule then
    closed on the next bar — a one-bar round trip dressed up as a position."""
    provider = SyntheticQuoteProvider(_NearExpiryCalendar())
    bridge = _bridge(
        {
            **_V2,
            "type": {
                "BULLISH": {
                    "structure": "long_call",
                    "strike_selection": "atm",
                    "expiry_min_days": 7,
                }
            },
        },
        provider,
    )
    bridge.on_bar("NIFTY", 24_800.0, "2026-10-05T09:15:00")

    with pytest.raises(ContractValidityError) as exc:
        bridge._select_expiry(bridge._generator(), "NIFTY")
    assert exc.value.reason == REASON_EXPIRY_TOO_NEAR

    result = _open(bridge)
    assert result is not None and result.get("rejected")
    assert REASON_EXPIRY_TOO_NEAR in result["reason"]
    assert bridge.executed_count == 0, "a halt must not open a substituted contract"


def test_the_knob_is_inert_without_a_bar_clock():
    """Replay anchoring needs a bar: with no bar clock there is no reference
    date to measure minimum days against, so selection stays on the calendar."""
    provider = SyntheticQuoteProvider(_NearExpiryCalendar())
    bridge = _bridge(
        {
            **_V2,
            "type": {
                "BULLISH": {
                    "structure": "long_call",
                    "strike_selection": "atm",
                    "expiry_min_days": 7,
                }
            },
        },
        provider,
    )

    expiry = bridge._select_expiry(bridge._generator(), "NIFTY")
    assert expiry is not None


# ---------------------------------------------------------------------------
# Chain-derived terms, never guessed
# ---------------------------------------------------------------------------


def test_a_contract_with_no_chain_lot_size_is_refused():
    """Lot size comes from the master or the entry stops: guessing it would size
    the position, the premium outlay and every rupee stop off thin air."""
    bridge = _bridge(provider=SyntheticQuoteProvider(_NoLotCalendar()))

    result = _open(bridge)

    assert result is not None and result.get("rejected")
    assert REASON_NO_LOT_SIZE in result["reason"]
    assert bridge.executed_count == 0


def test_a_strike_with_no_contract_on_the_chain_is_refused():
    bridge = _bridge()
    with pytest.raises(ContractValidityError) as exc:
        bridge._validate_entry_contracts([Decimal("24800")], {}, "CE")
    assert exc.value.reason == REASON_NO_CONTRACT


def test_a_calendar_with_no_expiries_at_all_halts():
    class _NoExpiries(SyntheticChainGenerator):
        def available_expiries(self, underlying, count=3, reference=None):
            return []

    bridge = _bridge(provider=SyntheticQuoteProvider(_NoExpiries()))

    result = _open(bridge)

    assert result is not None and result.get("rejected")
    assert REASON_NO_EXPIRY in result["reason"]
    assert bridge.executed_count == 0


def test_an_empty_chain_halts_instead_of_trading_a_missing_strike():
    class _EmptyChain(SyntheticChainGenerator):
        def generate_chain(self, *args, **kwargs):
            return {}

    bridge = _bridge(provider=SyntheticQuoteProvider(_EmptyChain()))

    result = _open(bridge)

    assert result is not None and result.get("rejected")
    assert REASON_NO_STRIKE in result["reason"]
    assert bridge.executed_count == 0


# ---------------------------------------------------------------------------
# Decision-time quote, not a cached one
# ---------------------------------------------------------------------------


def test_the_entry_gate_drops_the_cache_before_reading_every_leg():
    bridge = _bridge()
    spy = _RecordingProvider(bridge.quote_provider)
    bridge.quote_provider = spy

    result = _open(bridge)

    assert result is not None and not result.get("rejected")
    reads = [index for index, (kind, _) in enumerate(spy.calls) if kind == "get_quote"]
    assert reads, "the gate must read the market at decision time"
    first = reads[0]
    kind, token = spy.calls[first - 1]
    assert kind == "invalidate", "the gate reads through the cache, not out of it"
    assert any(t == token for k, t in spy.calls if k == "get_quote")


def test_a_dead_session_answer_is_not_a_quote():
    """A killed broker session answers with a row of zeros; that is the case the
    gate exists for, and it must halt rather than fill at ₹0."""
    provider = SyntheticQuoteProvider(SyntheticChainGenerator())
    bridge = _bridge(provider=_RecordingProvider(provider, quote={"ltp": 0.0}))

    result = _open(bridge)

    assert result is not None and result.get("rejected")
    assert REASON_NO_FRESH_QUOTE in result["reason"]
    assert bridge.executed_count == 0
