"""PRD Task 6.1 — Strategy contract + auto-discovery tests."""

import pytest

from backtest.strategy import registry
from backtest.strategy.base import Strategy, StrategyContractError


@pytest.fixture(autouse=True)
def _restore_registry():
    """Keep test-defined strategies from leaking into the global registry.

    Discovery is triggered at setup so the snapshot always contains the real
    strategies — clearing without this desyncs ``_REGISTRY`` from
    ``sys.modules`` (cached modules won't re-run ``__init_subclass__``).
    """
    registry._discover()
    snap = dict(registry._REGISTRY)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(snap)


def _make_valid(name="contract_valid"):
    class _S(Strategy):
        pass

    _S.name = name
    _S.description = "d"
    _S.version = "1.0"
    _S.author = "tester"
    _S.params = {
        "period": {
            "default": 14,
            "min": 5,
            "max": 50,
            "type": "int",
            "label": "Period",
            "tooltip": "lookback",
        }
    }

    def generate_signals(self, candles):
        import pandas as pd

        return pd.Series(1, index=candles.index, dtype=int)

    _S.generate_signals = generate_signals
    return _S


# --- validation -------------------------------------------------------------


def test_valid_strategy_passes_validation():
    cls = _make_valid()
    cls.validate()  # must not raise
    assert cls.param_schema()["period"]["default"] == 14


def test_missing_name_fails():
    class NoName(Strategy):
        params = {"p": 1}

        def generate_signals(self, candles): ...

    with pytest.raises(StrategyContractError, match="name"):
        NoName.validate()


def test_params_not_a_dict_fails():
    class BadParams(Strategy):
        name = "bad_params"
        params = "not a dict"

        def generate_signals(self, candles): ...

    with pytest.raises(StrategyContractError, match="params"):
        BadParams.validate()


def test_schema_missing_default_fails():
    class NoDefault(Strategy):
        name = "no_default"
        params = {"p": {"min": 1, "max": 10, "type": "int"}}

        def generate_signals(self, candles): ...

    with pytest.raises(StrategyContractError, match="default"):
        NoDefault.validate()


def test_schema_bad_type_fails():
    class BadType(Strategy):
        name = "bad_type"
        params = {"p": {"default": 1, "type": "percentage"}}

        def generate_signals(self, candles): ...

    with pytest.raises(StrategyContractError, match="invalid type"):
        BadType.validate()


def test_schema_default_out_of_range_fails():
    class OutOfRange(Strategy):
        name = "out_of_range"
        params = {"p": {"default": 100, "min": 1, "max": 10, "type": "int"}}

        def generate_signals(self, candles): ...

    with pytest.raises(StrategyContractError, match="> max"):
        OutOfRange.validate()


def test_missing_signal_method_fails():
    class NoSignals(Strategy):
        name = "no_signals"
        params = {"p": 1}

    with pytest.raises(StrategyContractError, match="generate_signals"):
        NoSignals.validate()


# --- schema / defaults ------------------------------------------------------


def test_flat_form_yields_full_schema():
    class Flat(Strategy):
        name = "flat"
        params = {"period": 14, "vol": 1.5, "on": True, "label": "x"}

        def generate_signals(self, candles): ...

    schema = Flat.param_schema()
    assert schema["period"] == {
        "default": 14,
        "type": "int",
        "min": None,
        "max": None,
        "label": "Period",
        "tooltip": "",
    }
    assert schema["vol"]["type"] == "float"
    assert schema["on"]["type"] == "bool"
    assert Flat.default_params() == {"period": 14, "vol": 1.5, "on": True, "label": "x"}


def test_schema_form_override_is_coerced():
    cls = _make_valid()
    inst = cls(period="30")  # string override coerced to declared int
    assert inst.period == 30 and isinstance(inst.period, int)


def test_unknown_override_raises():
    cls = _make_valid()
    with pytest.raises(ValueError, match="unknown"):
        cls(bogus=1)


# --- catalogue API + auto-discovery ----------------------------------------


def test_get_all_returns_valid_catalogue():
    catalogue = registry.get_all()
    names = {c["name"] for c in catalogue}
    assert {"sma_crossover", "rsi_reversion", "buy_and_hold", "donchian_breakout"} <= names
    for entry in catalogue:
        assert set(entry) == {"name", "description", "version", "author", "params"}
        assert entry["params"] == registry.get_params(entry["name"])


def test_get_params_unknown_raises():
    with pytest.raises(KeyError):
        registry.get_params("does_not_exist")


def test_get_all_skips_invalid_strategies(caplog):
    class Invalid(Strategy):
        name = "catalogue_invalid"
        params = {"p": {"min": 5, "max": 1, "type": "int", "default": 3}}  # min>max

        def generate_signals(self, candles): ...

    names = {c["name"] for c in registry.get_all()}
    assert "catalogue_invalid" not in names
    assert any("catalogue_invalid" in rec.getMessage() for rec in caplog.records)


def test_discovery_survives_broken_module(monkeypatch):
    """A module that raises on import is skipped, not fatal (Task 1.2)."""
    real_import = registry.importlib.import_module

    def flaky(full_name):
        if full_name.endswith(".rsi_reversion"):
            raise RuntimeError("boom")
        return real_import(full_name)

    monkeypatch.setattr(registry.importlib, "import_module", flaky)
    registry._discover()  # must not raise
    assert "sma_crossover" in registry._REGISTRY


# --- Task 6.6: migrated strategies carry full metadata + schemas ------------


def test_migrated_strategies_have_full_metadata():
    expected = {"sma_crossover", "rsi_reversion", "buy_and_hold", "donchian_breakout"}
    catalogue = {s["name"]: s for s in registry.get_all()}
    assert expected <= set(catalogue)
    for name, entry in catalogue.items():
        if name not in expected:
            continue
        # metadata populated
        assert entry["description"], f"{name} missing description"
        assert entry["version"], f"{name} missing version"
        assert entry["author"], f"{name} missing author"
        # every declared param is a full schema
        for pname, spec in entry["params"].items():
            assert {"default", "min", "max", "type", "label", "tooltip"} <= set(
                spec
            ), f"{name}.{pname} schema incomplete"
            assert spec["label"], f"{name}.{pname} missing label"


def test_migrated_strategies_validate_and_run():
    import pandas as pd

    from backtest.data.synthetic import SyntheticSource
    from backtest.runner import run_on_candles

    candles = SyntheticSource().get_candles("DEMO", "2021-01-01", "2024-01-01", "day")
    for name in ["sma_crossover", "rsi_reversion", "buy_and_hold", "donchian_breakout"]:
        cls = registry.get_strategy(name)
        cls.validate()  # must not raise
        result = run_on_candles(candles, name, {})
        assert isinstance(result.metrics["num_trades"], int)
        assert len(result.equity) == len(candles)
