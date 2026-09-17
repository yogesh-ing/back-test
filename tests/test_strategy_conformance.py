"""U6.3 — strategy conformance battery + plugin discovery (D7 close-out).

Every strategy — built-in, template or user drop-in — must pass ONE battery
(``backtest.plugins.conformance_errors``): unique non-empty name, the
``Strategy.validate()`` contract, signal-kind consistency (``option`` claimed
⇔ ``generate_market_view`` really overridden), metadata, param UI labels,
output shape and determinism (same candles twice → identical output,
value-compared not repr).

The loader applies the same battery to ``plugins/strategies/*.py`` at startup
and *skips with a warning* anything that fails — a broken plugin can never
crash the app, reach the trading path, or leave a half-valid class registered
(the loader POPS the ``__init_subclass__`` auto-registration of failures).

Pinned here:

* all six built-ins pass the battery and classify correctly;
* both shipped templates conform and never import engine modules;
* good equity/option drop-ins register with the right ``signal_kind``;
* bad plugins (syntax error, broker/HTTP imports, nondeterminism, wrong
  output shape, missing hook, duplicate name, missing dir) are skipped with a
  warning and leave the built-ins untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backtest.plugins import (
    PLUGINS_DIR,
    _check_allowed_imports,
    _import_module_from_path,
    conformance_errors,
    discover_plugins,
    plugin_strategy_names,
)
from backtest.strategy.registry import _REGISTRY, get_strategy, list_strategies, signal_kind

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = PROJECT_ROOT / "templates"

EQUITY_PLUGIN = '''\
import pandas as pd
from backtest.strategy.base import Strategy


class GoodEquity(Strategy):
    name = "{name}"
    description = "drop-in equity test strategy"
    version = "1.0"
    author = "tests"
    params = {{"period": {{"default": 5, "min": 2, "max": 50, "type": "int", "label": "Period"}}}}

    def generate_signals(self, candles):
        ema = candles["close"].ewm(span=int(self.period), adjust=False).mean()
        return (candles["close"] > ema).astype(int)
'''

OPTION_PLUGIN = '''\
import pandas as pd
from backtest.strategy.base import Strategy
from backtest.strategy.intent import Direction, MarketView


class GoodOption(Strategy):
    name = "{name}"
    description = "drop-in option test strategy"
    version = "1.0"
    author = "tests"
    params = {{"period": {{"default": 5, "min": 2, "max": 50, "type": "int", "label": "Period"}}}}

    def entries(self, candles):
        return candles["close"] > candles["close"].rolling(int(self.period)).mean()

    def generate_market_view(self, candles):
        return MarketView(
            direction=Direction.BULLISH,
            confidence=0.6,
            underlying="NIFTY",
            spot_price=float(candles["close"].iloc[-1]),
        )
'''


@pytest.fixture()
def plugin_env(tmp_path, monkeypatch):
    """Point the loader at an empty per-test plugin dir; undo registry pollution."""
    strategies = tmp_path / "strategies"
    strategies.mkdir()
    monkeypatch.setattr("backtest.plugins.PLUGINS_DIR", strategies)
    before = set(_REGISTRY)
    plugin_names_before = set(plugin_strategy_names())
    yield strategies
    for name in set(_REGISTRY) - before:
        _REGISTRY.pop(name, None)
    from backtest.plugins import _REGISTERED_FROM_PLUGINS

    _REGISTERED_FROM_PLUGINS.difference_update(plugin_strategy_names() - plugin_names_before)


def _write(dir_: Path, filename: str, source: str) -> Path:
    path = dir_ / filename
    path.write_text(source, encoding="utf-8")
    return path


def _load(dir_: Path, filename: str):
    """Force a fresh scan and return the registered names."""
    return discover_plugins(force=True)


# ---------------------------------------------------------------------------
# Built-ins
# ---------------------------------------------------------------------------


class TestBuiltIns:
    # The six canonical built-ins (exact-name check, NOT registry size: any
    # test module that defines a Strategy subclass pollutes the global
    # registry, so counting entries is order-dependent).
    BUILTINS = (
        "buy_and_hold",
        "donchian_breakout",
        "price_move",
        "rsi_reversion",
        "sma_crossover",
        "directional_options",
    )

    def test_all_six_builtins_pass_the_battery(self):
        registered = set(list_strategies())
        for name in self.BUILTINS:
            assert name in registered, f"built-in {name} not registered"
            res = conformance_errors(get_strategy(name))
            assert res.ok, f"{name}: {res.errors}"

    def test_builtin_signal_kinds(self):
        assert signal_kind(get_strategy("directional_options")) == "option"
        equity = (
            "buy_and_hold", "donchian_breakout", "price_move", "rsi_reversion", "sma_crossover",
        )
        for name in equity:
            assert signal_kind(get_strategy(name)) == "equity", name

    def test_catalogue_entries_carry_params_and_kind(self):
        from backtest.strategy.registry import get_all

        catalogue = {entry["name"]: entry for entry in get_all()}
        assert set(catalogue) == set(list_strategies())
        for entry in catalogue.values():
            assert entry["signal_kind"] in ("equity", "option")
            for spec in entry["params"].values():
                assert spec.get("label"), entry["name"]


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


class TestTemplates:
    @pytest.mark.parametrize(
        ("filename", "kind"),
        [
            ("equity_strategy_template.py", "equity"),
            ("option_strategy_template.py", "option"),
        ],
    )
    def test_template_conforms_and_classifies(self, filename, kind):
        module = _import_module_from_path(f"_tpl_test_{filename[:-3]}", TEMPLATES_DIR / filename)
        try:
            classes = [
                getattr(module, a)
                for a in dir(module)
                if isinstance(getattr(module, a), type)
                and str(getattr(getattr(module, a), "name", "")).startswith("my_")
            ]
            assert len(classes) == 1, f"{filename} must define exactly one template strategy"
            res = conformance_errors(classes[0])
            assert res.ok, res.errors
            assert signal_kind(classes[0]) == kind
        finally:
            tpl_cls = getattr(module, "MyEmaTrend", None) or getattr(module, "MyOptionMomentum")
            name = getattr(tpl_cls, "name", "")
            if name in _REGISTRY:
                _REGISTRY.pop(name)

    def test_templates_never_import_engine_modules(self):
        for filename in ("equity_strategy_template.py", "option_strategy_template.py"):
            source = (TEMPLATES_DIR / filename).read_text(encoding="utf-8")
            assert _check_allowed_imports(source) is None, filename


# ---------------------------------------------------------------------------
# Good drop-ins
# ---------------------------------------------------------------------------


class TestGoodDropIns:
    def test_equity_dropin_registers_with_equity_kind(self, plugin_env):
        _write(plugin_env, "good_equity.py", EQUITY_PLUGIN.format(name="drop_eq_test"))
        assert _load(plugin_env, "good_equity.py") == ["drop_eq_test"]
        assert "drop_eq_test" in _REGISTRY
        assert signal_kind(get_strategy("drop_eq_test")) == "equity"
        assert "drop_eq_test" in plugin_strategy_names()

    def test_option_dropin_registers_with_option_kind(self, plugin_env):
        _write(plugin_env, "good_option.py", OPTION_PLUGIN.format(name="drop_opt_test"))
        assert _load(plugin_env, "good_option.py") == ["drop_opt_test"]
        assert signal_kind(get_strategy("drop_opt_test")) == "option"

    def test_good_and_bad_files_in_one_scan_bad_never_poisons_good(self, plugin_env):
        _write(plugin_env, "aa_bad.py", "def broken(:\n")  # syntax error, sorts first
        _write(plugin_env, "zz_good.py", EQUITY_PLUGIN.format(name="survivor_test"))
        registered = _load(plugin_env, "*")
        assert registered == ["survivor_test"]
        assert "survivor_test" in _REGISTRY


# ---------------------------------------------------------------------------
# Bad plugins — skipped with a warning, built-ins untouched
# ---------------------------------------------------------------------------


def _builtin_snapshot() -> dict:
    return {name: _REGISTRY[name] for name in _REGISTRY}


class TestBadPlugins:
    def test_syntax_error_is_skipped(self, plugin_env):
        before = _builtin_snapshot()
        _write(plugin_env, "broken.py", "def broken(:\n    pass")
        assert discover_plugins(force=True) == []
        assert _builtin_snapshot() == before

    def test_broker_and_http_imports_are_refused(self, plugin_env):
        before = _builtin_snapshot()
        _write(
            plugin_env,
            "reaches_out.py",
            "from backtest.brokers.base import BrokerOrder\n"
            "import requests\n" + EQUITY_PLUGIN.format(name="sneaky_test"),
        )
        assert discover_plugins(force=True) == []
        assert "sneaky_test" not in _REGISTRY
        assert _builtin_snapshot() == before

    def test_forbidden_submodule_import_detected(self):
        # Submodule of a forbidden package is caught by the same rule.
        assert _check_allowed_imports("from backtest.data.base import normalize_candles\n")
        assert _check_allowed_imports("import urllib.parse\n")
        assert _check_allowed_imports("from backtest.forward.feed_registry import get_chain_bus\n")
        # Innocent imports pass.
        assert _check_allowed_imports("import pandas as pd\nimport math\n") is None

    def test_nondeterministic_strategy_is_skipped_and_unregistered(self, plugin_env):
        _write(
            plugin_env,
            "flake.py",
            "import random\nimport pandas as pd\n"
            "from backtest.strategy.base import Strategy\n"
            "class Flake(Strategy):\n"
            '    name = "flake_test"\n'
            '    description = "random"\n    version = "1"\n    author = "t"\n'
            "    params = {}\n"
            "    def generate_signals(self, candles):\n"
            "        return pd.Series(random.random(), index=candles.index)\n",
        )
        assert discover_plugins(force=True) == []
        assert "flake_test" not in _REGISTRY, "failed plugin must be popped back out"

    def test_wrong_output_shape_is_skipped(self, plugin_env):
        _write(
            plugin_env,
            "shapeless.py",
            "import pandas as pd\n"
            "from backtest.strategy.base import Strategy\n"
            "class Shapeless(Strategy):\n"
            '    name = "shapeless_test"\n'
            '    description = "wrong length"\n    version = "1"\n    author = "t"\n'
            "    params = {}\n"
            "    def generate_signals(self, candles):\n"
            "        return pd.Series([1])\n",
        )
        assert discover_plugins(force=True) == []
        assert "shapeless_test" not in _REGISTRY

    def test_missing_signal_hook_is_skipped(self, plugin_env):
        _write(
            plugin_env,
            "hookless.py",
            "from backtest.strategy.base import Strategy\n"
            "class Hookless(Strategy):\n"
            '    name = "hookless_test"\n'
            '    description = "no hook"\n    version = "1"\n    author = "t"\n'
            "    params = {}\n",
        )
        assert discover_plugins(force=True) == []
        assert "hookless_test" not in _REGISTRY

    def test_duplicate_name_vs_builtin_skipped_builtin_untouched(self, plugin_env):
        builtin = get_strategy("sma_crossover")
        _write(plugin_env, "impostor.py", EQUITY_PLUGIN.format(name="sma_crossover"))
        assert discover_plugins(force=True) == []
        assert get_strategy("sma_crossover") is builtin, "built-in must survive the impostor"

    def test_signal_kind_is_derived_not_declared(self):
        """A strategy can never CLAIM option-kind: it is derived from the override.

        ``signal_kind`` compares ``generate_market_view`` against the base
        implementation, so the "claims option but doesn't override" lie is
        structurally impossible — pin that property.
        """
        from backtest.strategy.base import Strategy

        class QuietEquity(Strategy):
            name = "quiet_equity_test"
            description = "entries-only"
            version = "1"
            author = "t"
            params = {}

            def entries(self, candles):
                return candles["close"] > candles["close"].shift(1)

        try:
            # Even an explicit copy of the base impl is still the base impl.
            QuietEquity.generate_market_view = Strategy.generate_market_view
            assert signal_kind(QuietEquity) == "equity"
            res = conformance_errors(QuietEquity)
            assert res.ok, res.errors
        finally:
            _REGISTRY.pop("quiet_equity_test", None)

    def test_missing_plugin_dir_is_a_noop(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backtest.plugins.PLUGINS_DIR", tmp_path / "nope" / "deeper")
        assert discover_plugins(force=True) == []

    def test_discovery_never_raises_on_garbage_dir(self, tmp_path, monkeypatch):
        garbage = tmp_path / "strategies"
        garbage.mkdir()
        (garbage / "binary.py").write_bytes(b"\x00\x01\x02\xff not python")
        monkeypatch.setattr("backtest.plugins.PLUGINS_DIR", garbage)
        before = _builtin_snapshot()
        assert discover_plugins(force=True) == []  # skipped, not raised
        assert _builtin_snapshot() == before


# ---------------------------------------------------------------------------
# The real plugin folder (repo root) — present, empty or valid, never fatal
# ---------------------------------------------------------------------------


class TestRepoPluginDir:
    def test_create_app_survives_whatever_is_in_plugins(self):
        # discover_plugins is called inside create_app; it must never break boot.
        from backtest.web.app import create_app

        app = create_app()
        assert any(r.endpoint == "index" for r in app.url_map.iter_rules())

    def test_default_plugins_dir_points_at_repo_root(self):
        assert PLUGINS_DIR == PROJECT_ROOT / "plugins" / "strategies"
