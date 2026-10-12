"""``POST /api/backtest/run-many`` comparison block (PRD backTest-enhance §4).

The maths itself is pinned in ``tests/test_engine_comparison.py``. What is
pinned HERE is the wiring, and the wiring is where a comparison goes wrong
quietly: a slot that silently drops out of the matrix, a badge that claims one
symbol when four ran, or a generalization run that quietly ignores the symbols
it was given.
"""

from __future__ import annotations

import pytest

from backtest.web.app import create_app

SHARED = {
    "symbol": "INFY",
    "from_date": "2022-01-01",
    "to_date": "2024-01-01",
    "capital": 100_000,
}


@pytest.fixture()
def client():
    return create_app(source="synthetic").test_client()


#: Params per strategy. Sending one strategy's parameters to another is an
#: invalid request, not a comparison, so each gets its own.
_PARAMS = {
    "sma_crossover": {"fast": 10, "slow": 30},
    "rsi_reversion": {"period": 14},
    "macd_trend": {},
    "donchian_breakout": {},
}


def _slot(idx: int, strategy: str = "sma_crossover", **over) -> dict:
    return {
        "id": idx,
        "strategy": strategy,
        "timeframe": "1day",
        "params": dict(_PARAMS.get(strategy, {})),
        **over,
    }


def _post(client, slots, shared=None):
    resp = client.post(
        "/api/backtest/run-many",
        json={"shared": {**SHARED, **(shared or {})}, "slots": slots},
    )
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


class TestComparisonBlock:
    def test_every_successful_slot_appears_in_the_matrix(self, client):
        data = _post(client, [_slot(1), _slot(2, "rsi_reversion"), _slot(3, "macd_trend")])
        corr = data["comparison"]["correlation"]
        assert corr["available"] is True
        assert len(corr["labels"]) == 3
        assert len(corr["matrix"]) == 3
        assert corr["aligned_bars"] > 0

    def test_matrix_is_square_and_symmetric(self, client):
        """A heatmap that is not symmetric would be showing an ordered thing."""
        corr = _post(client, [_slot(1), _slot(2, "rsi_reversion"), _slot(3, "macd_trend")])[
            "comparison"
        ]["correlation"]
        for i in range(3):
            for j in range(3):
                assert corr["matrix"][i][j] == corr["matrix"][j][i]
                assert corr["matrix"][i][i] == 1.0

    def test_significance_covers_every_pair(self, client):
        sig = _post(client, [_slot(1), _slot(2, "rsi_reversion"), _slot(3, "macd_trend")])[
            "comparison"
        ]["significance"]
        assert sig["available"] is True
        assert len(sig["comparisons"]) == 3  # C(3,2)
        assert sig["simulations"] >= 1000
        assert sig["paired"] is True

    def test_two_slots_on_one_strategy_keep_distinct_labels(self, client):
        """
        A parameter sweep is the main use of this page, and both slots are
        ``sma_crossover``. Keyed on the bare strategy name they would collapse
        into a single matrix row and the heatmap would lose a column.
        """
        corr = _post(
            client,
            [_slot(1, params={"fast": 10, "slow": 30}), _slot(2, params={"fast": 20, "slow": 60})],
        )["comparison"]
        assert len(corr["labels_by_slot"]) == 2
        assert len(set(corr["labels_by_slot"].values())) == 2

    def test_failed_slot_is_excluded_but_named(self, client):
        """§4.1/§4.3: a slot that failed is not in the maths and not hidden."""
        comp = _post(client, [_slot(1), _slot(2, "no_such_strategy"), _slot(3, "rsi_reversion")])[
            "comparison"
        ]
        assert len(comp["correlation"]["labels"]) == 2
        excluded = comp["excluded"]
        assert len(excluded) == 1
        assert excluded[0]["slot"] == "2"
        assert "unknown strategy" in excluded[0]["reason"]

    def test_all_slots_failing_still_returns_a_block(self, client):
        """The page needs to say "nothing to compare", not blow up."""
        comp = _post(client, [_slot(1, "no_such_strategy"), _slot(2, "also_missing")])["comparison"]
        assert comp["correlation"]["available"] is False
        assert comp["significance"]["available"] is False
        assert len(comp["excluded"]) == 2

    def test_single_slot_has_nothing_to_compare(self, client):
        comp = _post(client, [_slot(1)])["comparison"]
        assert comp["correlation"]["available"] is False
        assert comp["significance"]["available"] is False

    def test_annualisation_comes_from_the_shared_timeframe(self, client):
        """Sharpe must be scaled by the bar count, not hard-coded to 252."""
        comp = _post(client, [_slot(1), _slot(2, "rsi_reversion")])["comparison"]
        assert comp["periods_per_year"] == 252.0


class TestGeneralization:
    def test_each_slot_runs_its_own_symbol(self, client):
        """
        §4.2: Test Generalization is one strategy across several symbols. If
        the per-slot symbol were ignored, every slot would return the same
        numbers and the whole mode would be a duplicate of Compare Strategies.
        """
        slots = [
            _slot(1, symbol="INFY"),
            _slot(2, symbol="TCS"),
            _slot(3, symbol="RELIANCE"),
        ]
        data = _post(client, slots)
        # The first bars of every curve are flat at the starting capital, so a
        # prefix is identical by construction — compare the whole curve.
        equities = {sid: tuple(r["equity"]["values"]) for sid, r in data["results"].items()}
        assert len(set(equities.values())) == 3, "slots ran identical data"
        for sid, r in data["results"].items():
            assert r["config"]["symbol"] == {"1": "INFY", "2": "TCS", "3": "RELIANCE"}[sid]

    def test_provenance_names_every_symbol(self, client):
        """A badge naming the shared symbol would be wrong for a multi-symbol run."""
        prov = _post(client, [_slot(1, symbol="INFY"), _slot(2, symbol="TCS")])["provenance"]
        assert prov["comparison_mode"] == "generalization"
        assert prov["symbols_used"] == ["INFY", "TCS"]
        assert "INFY" in prov["symbol"] and "TCS" in prov["symbol"]

    def test_single_symbol_run_is_still_reported_as_strategies_mode(self, client):
        """Distinguishing on the data, not on a client-sent flag, keeps it honest."""
        prov = _post(client, [_slot(1), _slot(2, "rsi_reversion")])["provenance"]
        assert prov["comparison_mode"] == "strategies"
        assert prov["symbols_used"] == ["INFY"]

    def test_slot_without_a_symbol_falls_back_to_the_shared_one(self, client):
        data = _post(client, [_slot(1, symbol="TCS"), _slot(2)])
        assert data["results"]["1"]["config"]["symbol"] == "TCS"
        assert data["results"]["2"]["config"]["symbol"] == "INFY"
        assert data["provenance"]["symbols_used"] == ["INFY", "TCS"]

    def test_dates_capital_and_engine_stay_shared(self, client):
        """§4.2 varies the SYMBOL and nothing else."""
        # The engine is chosen per slot (that is what run-many reads); the
        # shared block only carries the conditions.
        slots = [
            _slot(1, symbol="INFY", mode="quick_screen"),
            _slot(2, symbol="TCS", mode="quick_screen"),
        ]
        data = _post(client, slots, shared={"capital": 250_000})
        assert data["provenance"]["comparison_mode"] == "generalization"
        assert data["provenance"]["engine_tier"] != "canonical"
        for r in data["results"].values():
            assert r["config"]["capital"] == 250_000
            assert r["config"]["from_date"] == data["results"]["1"]["config"]["from_date"]
            assert r["config"]["to_date"] == data["results"]["1"]["config"]["to_date"]

    def test_generalization_matrix_is_usually_diversifying(self, client):
        """
        Different symbols are different businesses, so their daily returns
        should not move together. If the mode reported a 0.9 correlation it
        would mean the per-slot symbol was never applied.
        """
        corr = _post(
            client, [_slot(1, symbol="INFY"), _slot(2, symbol="TCS"), _slot(3, symbol="RELIANCE")]
        )["comparison"]["correlation"]
        assert corr["high_correlation_pairs"] == []


class TestComparePageWiring:
    """The page must ship the controls §4.2/§4.3/§4.4/§4.5 depend on."""

    def test_page_offers_both_comparison_modes(self, client):
        html = client.get("/compare").get_data(as_text=True)
        assert 'data-mode="strategies"' in html
        assert 'data-mode="generalization"' in html

    def test_timeframe_is_a_shared_control_not_a_per_slot_one(self, client):
        """§4.1: a 5-minute and a daily run cannot be compared side by side."""
        html = client.get("/compare").get_data(as_text=True)
        assert 'id="sharedTimeframe"' in html
        assert 'class="slot-tf"' not in html

    def test_comparison_panels_and_script_are_served(self, client):
        html = client.get("/compare").get_data(as_text=True)
        assert 'id="comparePanels"' in html
        assert "js/compare/comparison_panels.js" in html
        assert client.get("/static/js/compare/comparison_panels.js").status_code == 200

    def test_equity_pane_explains_the_index(self, client):
        """§4.5: a reader seeing a y-axis of 100-200 should be told why."""
        html = client.get("/compare").get_data(as_text=True)
        assert "indexed to 100" in html
