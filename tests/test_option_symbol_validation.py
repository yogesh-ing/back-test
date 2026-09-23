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
        # 2026-09-22: message now names the allowed set from the (strategy-
        # driven) eligibility check — sorted alphabetically.
        assert "pick BANKNIFTY/NIFTY" in body.get("error", "")

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


class TestLotsPerLeg:
    """Owner rule (2026-09-22): lot size is USER-SELECTED at instance creation,

    defaults to 1, is clamped server-side, and is IMMUTABLE afterwards — to
    change size the user spawns a new instance (or risk management halts the
    runner). No in-place lot mutation exists by design.
    """

    def _post(self, client, symbol, instrument=None):
        return TestBackendOptionSymbolValidation()._post(client, symbol, instrument)

    def test_default_is_one_lot(self):
        from backtest.forward.options_bridge import DEFAULT_EXPRESSION

        assert int(DEFAULT_EXPRESSION.get("quantity", 1)) == 1

    def test_user_chosen_lots_are_accepted(self, client):
        resp = self._post(
            client,
            "NIFTY",
            instrument={
                "type": "option",
                "expression": {
                    "type": {"BULLISH": "bull_call_spread"},
                    "quantity": 3,
                },
            },
        )
        assert resp.status_code in (200, 201), resp.get_json()
        expr = resp.get_json()["runner"]["instrument"]["expression"]
        assert int(expr["quantity"]) == 3

    def test_lots_are_clamped_to_1_100(self, client):
        resp = self._post(
            client,
            "NIFTY",
            instrument={
                "type": "option",
                "expression": {"type": {"BULLISH": "bull_call_spread"}, "quantity": 9999},
            },
        )
        assert resp.status_code in (200, 201), resp.get_json()
        expr = resp.get_json()["runner"]["instrument"]["expression"]
        assert int(expr["quantity"]) == 100

    def test_bad_lots_fall_back_to_one(self, client):
        resp = self._post(
            client,
            "NIFTY",
            instrument={
                "type": "option",
                "expression": {"type": {"BULLISH": "bull_call_spread"}, "quantity": "abc"},
            },
        )
        assert resp.status_code in (200, 201), resp.get_json()
        expr = resp.get_json()["runner"]["instrument"]["expression"]
        assert int(expr["quantity"]) == 1

    def test_no_in_place_lot_mutation_endpoint_exists(self):
        """The API has create + control only — no update/patch of a runner's
        sizing. Changing lots = spawn a new instance (owner rule)."""
        from pathlib import Path

        src = Path("src/backtest/api/portfolio.py").read_text(encoding="utf-8")
        assert "@portfolio_bp.patch" not in src.lower()
        assert "@portfolio_bp.put" not in src.lower()


class TestFrontendSymbolWiring:
    def test_symbol_is_read_into_body_before_option_check(self):
        """Source-level guard: the regression was reading symbol AFTER validation.

        2026-09-22: the symbol read now prefers the restricted-instrument
        dropdown (spawn-symbol-select) when it is visible — the guard tracks
        the IIFE that picks whichever element is active."""
        import re
        from pathlib import Path

        src = Path("src/backtest/web/static/js/portfolio.js").read_text(encoding="utf-8")

        body_read = src.index('const body = {')
        symbol_read = src.index('symbol: (() => {', body_read)
        option_check = src.index('OPTION_INDEXES.includes(symbolUpper)', symbol_read)
        assert symbol_read < option_check, (
            "portfolio.js must read the instrument (dropdown or free text) into "
            "the body BEFORE the option-index validation (GAP-2 regression)"
        )

    def test_restricted_strategy_gets_dropdown_not_free_text(self):
        """Source-level guard (2026-09-22): a strategy with eligible_instruments
        renders the instrument dropdown and hides free text — no typing."""
        from pathlib import Path

        src = Path("src/backtest/web/static/js/portfolio.js").read_text(encoding="utf-8")
        assert "eligible_instruments" in src, (
            "spawn form must honour strategy eligible_instruments"
        )
        assert "spawn-symbol-select" in src
