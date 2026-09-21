"""GAP-2 (2026-09-21): option-runner symbol validation.

The frontend used to validate ``body.symbol`` before reading it from the
form, so EVERY option deployment failed with "pick NIFTY or BANKNIFTY".
Fixes verified here, synthetically:

1. Backend: an option runner on NIFTY/BANKNIFTY spawns; on any other symbol
   it is rejected with the same message (defense in depth).
2. Frontend: the spawn body builder reads the symbol field into the body
   BEFORE the option validation (source-level assertion, no browser needed).
"""

from __future__ import annotations

import inspect

import pytest

from backtest.forward.risk_supervisor import GlobalRiskConfig


@pytest.fixture
def client():
    from backtest.forward.portfolio_manager import get_portfolio_manager, reset_portfolio_manager
    from backtest.web.app import create_app

    reset_portfolio_manager(
        risk_config=GlobalRiskConfig(daily_loss_limit=100_000, max_drawdown_pct=0.50),
        tick_seconds=1.0,
        warmup_bars=15,
        auto_start_feed=False,
    )
    app = create_app(source="synthetic")
    with app.test_client() as c:
        yield c
    get_portfolio_manager().shutdown()


class TestBackendOptionSymbolValidation:
    def _post(self, client, symbol, instrument=None):
        return client.post(
            "/api/portfolio/runner/create",
            json={
                "name": "gap2-test",
                "strategy": "vwap_ema_rsi",
                "timeframe": "1min",
                "allocated_capital": 100_000,
                "mode": "paper",
                "source": "synthetic",
                "target_type": "SINGLE_SYMBOL",
                "symbol": symbol,
                "instrument": instrument
                or {"type": "option", "expression": {"type": {"BULLISH": "bull_call_spread"}}},
            },
        )

    def test_option_runner_on_nifty_is_accepted(self, client):
        resp = self._post(client, "NIFTY")
        assert resp.status_code in (200, 201), resp.get_json()
        assert resp.get_json()["success"] is True

    def test_option_runner_on_reliance_is_rejected(self, client):
        resp = self._post(client, "RELIANCE")
        assert resp.status_code == 400
        body = resp.get_json()
        assert "pick NIFTY or BANKNIFTY" in body.get("error", "")

    def test_option_runner_on_lowercase_nifty_is_accepted(self, client):
        resp = self._post(client, "nifty")
        assert resp.status_code in (200, 201), resp.get_json()

    def test_equity_runner_on_reliance_still_accepted(self, client):
        resp = self._post(
            client,
            "RELIANCE",
            instrument={"type": "equity"},
        )
        assert resp.status_code in (200, 201), resp.get_json()


class TestFrontendSymbolWiring:
    def test_symbol_is_read_into_body_before_option_check(self):
        """Source-level guard: the regression was reading symbol AFTER validation."""
        import re
        from pathlib import Path

        src = Path("src/backtest/web/static/js/portfolio.js").read_text(encoding="utf-8")

        body_read = src.index('const body = {')
        symbol_read = src.index('symbol: $("spawn-symbol").value.trim()', body_read)
        option_check = src.index('OPTION_INDEXES.includes(symbolUpper)', symbol_read)
        assert symbol_read < option_check, (
            "portfolio.js must read spawn-symbol into the body BEFORE the "
            "option-index validation (GAP-2 regression)"
        )
