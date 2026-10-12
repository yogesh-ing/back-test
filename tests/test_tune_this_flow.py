"""§6 "Tune This" on the server side: the fields that cross the boundary.

The hand-off itself is frontend and is pinned in ``tests/js/test_tune_this.mjs``;
the audit chain it feeds is pinned in
``tests/optimization/test_tune_this_chain.py``. What is pinned here is the
config parser: the engine and the originating backtest handle travelling into
the stored run, and a hostile handle being refused on the way in.

The handle is deliberately constrained. It is a session id minted by the
Backtest page, and it ends up rendered into an audit trail; a free-form string
in an audit field is a stored-XSS surface, so anything that is not a plausible
id is dropped with a warning rather than stored.
"""

from __future__ import annotations

import pytest

from backtest.optimization.config import ConfigValidationError, parse_config


def _doc(**bt):
    base = {
        "symbol": "INFY",
        "startDate": "2022-01-01",
        "endDate": "2024-01-01",
        "initialCapital": 100_000,
        "timeframe": "1day",
    }
    base.update(bt)
    return {
        "strategyId": "sma_crossover",
        "objectiveFunction": "sharpe",
        "method": "bayesian",
        "parameters": [
            {
                "name": "fast",
                "type": "int",
                "optimize": True,
                "min": 5,
                "max": 50,
                "step": 5,
                "current": 10,
            },
        ],
        "backtestConfig": base,
    }


class TestEngineCarries:
    def test_quick_screen_survives_the_round_trip(self):
        """
        §6: "engine from current result (whichever was used)".

        This is §1.1 one hop downstream. If the engine is dropped here, a result
        screened on approximate fills gets tuned on the canonical driver, and
        the audit trail shows one continuous lineage for what are really two
        different engines.
        """
        cfg = parse_config(_doc(engine="quick_screen"))
        assert cfg.backtest.engine == "quick_screen"
        assert cfg.to_dict()["backtestConfig"]["engine"] == "quick_screen"

    def test_a_canonical_run_still_parses(self):
        assert parse_config(_doc(engine="driver")).backtest.engine == "driver"

    def test_the_default_is_unchanged(self):
        assert parse_config(_doc()).backtest.engine == "driver"


class TestSourceBacktestId:
    def test_it_survives_the_round_trip(self):
        cfg = parse_config(_doc(sourceBacktestId="bt_abc123"))
        assert cfg.backtest.source_backtest_id == "bt_abc123"
        assert cfg.to_dict()["backtestConfig"]["sourceBacktestId"] == "bt_abc123"

    def test_it_is_optional(self):
        cfg = parse_config(_doc())
        assert cfg.backtest.source_backtest_id is None

    def test_the_snake_case_spelling_also_works(self):
        cfg = parse_config(_doc(source_backtest_id="bt_xyz"))
        assert cfg.backtest.source_backtest_id == "bt_xyz"

    @pytest.mark.parametrize(
        "hostile",
        [
            "<script>alert(1)</script>",
            '"><img src=x onerror=alert(1)>',
            "a" * 200,
            "has space",
        ],
    )
    def test_a_value_that_is_not_an_id_is_dropped_with_a_warning(self, hostile):
        """
        It is rendered into the audit trail. Storing an arbitrary string there
        would be a stored-XSS surface, so the constraint is enforced at parse
        time rather than trusted from the browser.
        """
        cfg = parse_config(_doc(sourceBacktestId=hostile))
        assert cfg.backtest.source_backtest_id is None
        assert any("sourceBacktestId" in w for w in cfg.warnings)

    @pytest.mark.parametrize("ok", ["bt_abc123", "BT.2024:01", "a", "A_b-c.d:e" * 7])
    def test_plausible_ids_are_kept(self, ok):
        assert parse_config(_doc(sourceBacktestId=ok)).backtest.source_backtest_id == ok

    def test_an_empty_string_is_simply_absent(self):
        assert parse_config(_doc(sourceBacktestId="")).backtest.source_backtest_id is None


def test_config_validation_error_is_still_raised_for_real_problems():
    with pytest.raises(ConfigValidationError):
        parse_config({**_doc(), "method": "telepathy"})
