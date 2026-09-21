"""Unit tests for mStock option chain methods (mocked HTTP).

Tests cover:
- get_option_chain() parsing instrument master CSV
- _parse_option_contract() edge cases
- get_option_quote() response handling
- Error handling for API failures
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from backtest.brokers.mstock import MStockBroker, MStockOrderError
from backtest.instruments.base import ExerciseType, SettlementType
from backtest.instruments.option import OptionContract


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_broker_with_session() -> MStockBroker:
    """Create a broker with an active (mocked) session."""
    broker = MStockBroker()
    broker._session_token = "test_token_123"
    broker._expires_at = datetime(2099, 1, 1)
    return broker


# Sample instrument master CSV — matches mStock's REAL schema
# (verified live 2026-09-18: segment=OPTIDX, instrument_token, tradingsymbol,
#  explicit expiry date column, lot 65 for NIFTY).
_SAMPLE_CSV = (
    "tradingsymbol,instrument_token,segment,instrument_type,lot_size,tick_size,expiry,strike\n"
    "NIFTY26SEP24500CE,51201,OPTIDX,CE,65,0.05,2026-09-24,24500.00\n"
    "NIFTY26SEP24500PE,51202,OPTIDX,PE,65,0.05,2026-09-24,24500.00\n"
    "NIFTY26SEP25000CE,51203,OPTIDX,CE,65,0.05,2026-09-24,25000.00\n"
    "BANKNIFTY26SEP51000CE,51204,OPTIDX,CE,35,0.05,2026-09-24,51000.00\n"
    "NIFTY 50,26000,IDX,IN,1,0.05,,0.00\n"
)


# ---------------------------------------------------------------------------
# _parse_option_contract tests
# ---------------------------------------------------------------------------

class TestParseOptionContract:
    def _row(self, tradingsymbol: str, token: str, itype: str,
             lot: str = "65", strike: str = "24500.00") -> dict:
        return {
            "tradingsymbol": tradingsymbol,
            "instrument_token": token,
            "segment": "OPTIDX",
            "instrument_type": itype,
            "lot_size": lot,
            "tick_size": "0.05",
            "expiry": "2026-09-24T00:00:00",
            "strike": strike,
        }

    def test_parse_nifty_call(self):
        row = self._row("NIFTY26SEP24500CE", "51201", "CE")
        contract = MStockBroker._parse_option_contract(row)
        assert contract is not None
        assert contract.underlying == "NIFTY"
        assert contract.strike == Decimal("24500")
        assert contract.option_type == "CE"
        assert contract.expiry.year == 2026
        assert contract.expiry.month == 9
        assert contract.expiry.day == 24
        assert contract.lot_size == 65

    def test_parse_nifty_put(self):
        row = self._row("NIFTY26SEP24500PE", "51202", "PE")
        contract = MStockBroker._parse_option_contract(row)
        assert contract is not None
        assert contract.option_type == "PE"

    def test_parse_banknifty(self):
        row = self._row("BANKNIFTY26SEP51000CE", "51204", "CE", lot="35",
                        strike="51000.00")
        contract = MStockBroker._parse_option_contract(row)
        assert contract is not None
        assert contract.underlying == "BANKNIFTY"
        assert contract.strike == Decimal("51000")
        assert contract.lot_size == 35

    def test_parse_equity_returns_none(self):
        row = {
            "tradingsymbol": "RELIANCE26SEPEQ",
            "instrument_token": "2885",
            "segment": "NSE",
            "instrument_type": "EQ",
            "lot_size": "1",
            "tick_size": "0.05",
        }
        assert MStockBroker._parse_option_contract(row) is None

    def test_parse_invalid_symbol_returns_none(self):
        row = {"tradingsymbol": "INVALID", "instrument_token": "X"}
        assert MStockBroker._parse_option_contract(row) is None

    def test_parse_contract_is_valid(self):
        row = self._row("NIFTY26SEP24500CE", "51201", "CE")
        contract = MStockBroker._parse_option_contract(row)
        assert contract is not None
        assert contract.is_valid()


# ---------------------------------------------------------------------------
# get_option_chain tests (mocked HTTP)
# ---------------------------------------------------------------------------

class TestGetOptionChain:
    @patch("backtest.brokers.mstock.requests.get")
    def test_returns_nifty_contracts(self, mock_get):
        resp = MagicMock()
        resp.text = _SAMPLE_CSV
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp

        broker = _make_broker_with_session()
        contracts = broker.get_option_chain("NIFTY")

        # Should get 3 NIFTY options (24500CE, 24500PE, 25000CE), not BANKNIFTY or RELIANCE
        assert len(contracts) == 3
        assert all(c.underlying == "NIFTY" for c in contracts)

    @patch("backtest.brokers.mstock.requests.get")
    def test_returns_banknifty_contracts(self, mock_get):
        resp = MagicMock()
        resp.text = _SAMPLE_CSV
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp

        broker = _make_broker_with_session()
        contracts = broker.get_option_chain("BANKNIFTY")

        assert len(contracts) == 1
        assert contracts[0].underlying == "BANKNIFTY"

    @patch("backtest.brokers.mstock.requests.get")
    def test_filters_non_option_instruments(self, mock_get):
        resp = MagicMock()
        resp.text = _SAMPLE_CSV
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp

        broker = _make_broker_with_session()
        all_contracts = broker.get_option_chain("NIFTY")
        # Should not include RELIANCE equity
        symbols = [c.trading_symbol for c in all_contracts]
        assert "RELIANCE24DECEQ" not in symbols

    @patch("backtest.brokers.mstock.requests.get")
    def test_empty_csv_returns_empty(self, mock_get):
        resp = MagicMock()
        resp.text = "header1,header2\n"
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp

        broker = _make_broker_with_session()
        assert broker.get_option_chain("NIFTY") == []

    @patch("backtest.brokers.mstock.requests.get")
    def test_network_error_returns_empty(self, mock_get):
        import requests
        mock_get.side_effect = requests.ConnectionError("timeout")

        broker = _make_broker_with_session()
        assert broker.get_option_chain("NIFTY") == []

    def test_no_session_raises(self):
        broker = MStockBroker()
        with pytest.raises(MStockOrderError, match="no active"):
            broker.get_option_chain("NIFTY")


# ---------------------------------------------------------------------------
# get_option_quote tests (mocked HTTP)
# ---------------------------------------------------------------------------

class TestGetOptionQuote:
    @patch("backtest.brokers.mstock.requests.get")
    def test_returns_quote_data(self, mock_get):
        resp = MagicMock()
        resp.json.return_value = {"data": {"ltp": 150.5, "bid": 150.0, "ask": 151.0}}
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp

        broker = _make_broker_with_session()
        quote = broker.get_option_quote("NFO_NIFTY26SEP24500CE")

        assert quote["ltp"] == 150.5
        assert quote["bid"] == 150.0

    @patch("backtest.brokers.mstock.time.sleep")  # retry backoff: keep test fast
    @patch("backtest.brokers.mstock.requests.get")
    def test_network_error_returns_empty(self, mock_get, mock_sleep):
        import requests
        mock_get.side_effect = requests.ConnectionError("timeout")

        broker = _make_broker_with_session()
        quote = broker.get_option_quote("NFO_NIFTY26SEP24500CE")
        assert quote == {}
        # transient-failure retry: two attempts before giving up
        assert mock_get.call_count == 2


# ---------------------------------------------------------------------------
# get_option_chain_data tests (mocked HTTP)
# ---------------------------------------------------------------------------

class TestGetOptionChainData:
    @patch("backtest.brokers.mstock.requests.get")
    def test_returns_raw_data(self, mock_get):
        resp = MagicMock()
        resp.json.return_value = {"status": "success", "data": []}
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp

        broker = _make_broker_with_session()
        data = broker.get_option_chain_data("NIFTY", "24DEC", "token123")
        assert data["status"] == "success"
