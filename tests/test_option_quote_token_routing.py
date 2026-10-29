"""Option quote lookups must try the numeric token (WS depth) before the symbol.

Live regression 2026-10-22: the mStock WebSocket feed was healthy and carrying
5-level depth, but ``CE_instant_buy`` was blocked all day with
``NO_EXECUTION_QUOTE: 51338: no ask to BUY``. Root cause:
``LiveChainProvider.get_quote`` translated the numeric contract token into the
trading symbol and handed only that downstream, so every lookup landed on
mStock's LTP-only HTTP quote endpoint — structurally one-sided, so a BUY could
never find an ask. The socket cache is keyed on the numeric token alone.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

import pytest

from backtest.instruments.base import OptionType
from backtest.instruments.option import OptionContract
from backtest.options.quote_providers import (
    BidAskQuoteProvider,
    LiveChainProvider,
    NoExecutableQuoteError,
)

TOKEN = "51338"
SYMBOL = "NIFTY26OCT25000CE"
DEPTH = {"ltp": 121.5, "bid": 120.0, "ask": 123.0}


def _contract() -> OptionContract:
    return OptionContract(
        instrument_token=TOKEN,
        trading_symbol=SYMBOL,
        underlying="NIFTY",
        expiry=date(2026, 10, 27),
        strike=Decimal(25000),
        option_type=OptionType.CE,
        lot_size=75,
        metadata={},
    )


class RecordingBroker:
    """Answers the numeric token from the WS depth cache, the symbol with LTP only."""

    def __init__(self, *, depth_on_numeric: bool = True) -> None:
        self.keys: list[object] = []
        self.depth_on_numeric = depth_on_numeric

    def get_option_quote(self, instrument_token):
        self.keys.append(instrument_token)
        if str(instrument_token) == TOKEN:
            return dict(DEPTH) if self.depth_on_numeric else {}
        return {"ltp": DEPTH["ltp"], "bid": 0.0, "ask": 0.0}


def _provider(broker: RecordingBroker) -> BidAskQuoteProvider:
    chain = LiveChainProvider(broker)
    chain.register_contract(_contract())
    return BidAskQuoteProvider(chain)


def test_numeric_token_is_tried_first_and_reaches_the_depth_cache():
    broker = RecordingBroker()
    quote = _provider(broker).execution_quote(TOKEN, "BUY")

    assert broker.keys[0] == TOKEN, "the WS depth key must be asked before the symbol"
    assert quote["ltp"] == DEPTH["ask"], "a BUY fills against the ask, not LTP"
    assert quote["bid"] == DEPTH["bid"] and quote["ask"] == DEPTH["ask"]


def test_symbol_ltp_row_is_still_used_when_the_socket_has_no_depth():
    broker = RecordingBroker(depth_on_numeric=False)
    quote = _provider(broker).get_quote(TOKEN)

    assert broker.keys[:2] == [TOKEN, SYMBOL], "the HTTP symbol row stays the fallback"
    assert quote["ltp"] == DEPTH["ltp"]
    assert quote["bid"] == 0.0 and quote["ask"] == 0.0


def test_one_sided_quote_still_fails_closed():
    broker = RecordingBroker(depth_on_numeric=False)

    with pytest.raises(NoExecutableQuoteError):
        _provider(broker).execution_quote(TOKEN, "BUY")


def test_one_sided_http_row_is_logged_for_visibility(caplog):
    broker = RecordingBroker(depth_on_numeric=False)

    with caplog.at_level(logging.INFO, logger="backtest.options.quotes"):
        quote = _provider(broker).get_quote(TOKEN)

    assert quote["ltp"] == DEPTH["ltp"]
    messages = [
        r.getMessage() for r in caplog.records if "QUOTE_ONE_SIDED" in r.getMessage()
    ]
    assert messages, "every one-sided lookup must leave a QUOTE_ONE_SIDED trace"
    assert TOKEN in messages[0] and SYMBOL in messages[0]
