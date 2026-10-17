"""Structured strategy descriptions ("strategy cards").

Contract (2026-10-17, user-approved):
- Every registered strategy carries both the short ``description`` and a
  structured ``description_sections`` card with all canonical sections.
- ``GET /api/strategies`` exposes the normalised card so the backtest page
  can render it above the performance metrics.
- ``Strategy.validate()`` rejects malformed cards (unknown keys, empty
  sections, non-dict).
- The backtest page renders the card from the API payload.
"""

from __future__ import annotations

import itertools
import re
from pathlib import Path

import pytest

from backtest.strategy.base import (
    DESCRIPTION_SECTION_KEYS,
    Strategy,
    StrategyContractError,
)

ROOT = Path(__file__).resolve().parents[1]


_counter = itertools.count()


def _dummy(**attrs):
    from backtest.strategy.registry import _REGISTRY

    name = f"desc_dummy_{next(_counter)}"
    base = {
        "name": name,
        "description": "dummy strategy for card validation tests",
        "params": {},
        "generate_signals": lambda self, candles: candles,
    }
    base.update(attrs)
    cls = type(f"DescDummy{name}", (Strategy,), base)
    # __init_subclass__ auto-registers; this is a fixture, not a strategy.
    _REGISTRY.pop(name, None)
    return cls


class TestCardValidation:
    def test_valid_card_passes(self):
        card = {key: f"{key} text" for key in DESCRIPTION_SECTION_KEYS}
        _dummy(description_sections=card).validate()

    def test_no_card_is_allowed_by_validate(self):
        # Presence is a conformance rule (below), not a validate() rule —
        # fixture/legacy strategies must stay constructible.
        _dummy(description_sections=None).validate()

    def test_unknown_section_rejected(self):
        _bad = {key: "text" for key in DESCRIPTION_SECTION_KEYS}
        _bad["marketing"] = "buy now"
        with pytest.raises(StrategyContractError, match="unknown description"):
            _dummy(description_sections=_bad).validate()

    def test_empty_section_rejected(self):
        card = {key: "text" for key in DESCRIPTION_SECTION_KEYS}
        card["entry"] = "   "
        with pytest.raises(StrategyContractError, match="entry"):
            _dummy(description_sections=card).validate()

    def test_non_dict_rejected(self):
        with pytest.raises(StrategyContractError, match="must be a dict"):
            _dummy(description_sections="one long blob").validate()

    def test_description_card_normalises(self):
        card = {
            "parameters": "  params  ",
            "overview": " summary ",
            "not_a_section": "dropped",
        }
        normalised = _dummy(description_sections=card).description_card()
        assert list(normalised) == ["overview", "parameters"]  # canonical order
        assert normalised["overview"] == "summary"
        assert normalised["parameters"] == "params"

    def test_description_card_none_without_sections(self):
        assert _dummy(description_sections=None).description_card() is None


def _real_strategy_names() -> list[str]:
    """Registered strategies that ship with the app: classes whose source
    file lives in one of the two real strategy roots. Anything else is a
    fixture another test module registered into the global registry."""
    import inspect

    from backtest.strategy.registry import get_strategy, list_strategies

    roots = (ROOT / "src" / "backtest" / "strategies", ROOT / "plugins" / "strategies")
    names = []
    for name in list_strategies():
        try:
            path = Path(inspect.getfile(get_strategy(name))).resolve()
        except (TypeError, OSError):
            continue
        if any(path.is_relative_to(root.resolve()) for root in roots):
            names.append(name)
    return names


class TestRegisteredCards:
    """Conformance: no strategy registers without a complete card."""

    def test_every_registered_strategy_has_a_complete_card(self):
        from backtest.strategy.registry import get_strategy

        names = _real_strategy_names()
        assert names, "registry is empty — discovery failed?"
        for name in names:
            card = get_strategy(name).description_card()
            assert card is not None, f"{name}: missing description_sections"
            assert list(card) == list(DESCRIPTION_SECTION_KEYS), (
                f"{name}: card sections {list(card)} != "
                f"{list(DESCRIPTION_SECTION_KEYS)}"
            )
            for key, text in card.items():
                assert len(text) >= 40, (
                    f"{name}:{key} is too thin ({len(text)} chars) — "
                    "a card section should actually explain, not stub"
                )

    def test_catalogue_exposes_cards(self):
        from backtest.strategy.registry import get_all

        real = set(_real_strategy_names())
        for entry in get_all():
            if entry["name"] not in real:
                continue  # test fixture registered by another module
            sections = entry.get("description_sections")
            assert sections, f"{entry['name']}: catalogue omits the card"
            assert sections["overview"], entry["name"]


class TestStrategiesApi:
    @pytest.fixture()
    def client(self):
        from backtest.web.app import create_app

        return create_app(source="synthetic").test_client()

    def test_api_returns_description_sections(self, client):
        payload = client.get("/api/strategies").get_json()
        by_name = {s["name"]: s for s in payload}
        assert "sma_crossover" in by_name
        sections = by_name["sma_crossover"]["description_sections"]
        assert set(sections) == set(DESCRIPTION_SECTION_KEYS)
        assert "fast SMA" in sections["entry"]


class TestBacktestPageContract:
    def test_card_markup_sits_above_metrics(self):
        html = (ROOT / "src/backtest/web/templates/backtest.html").read_text()
        assert 'id="strategyCard"' in html
        assert 'id="strategyCardOverview"' in html
        assert 'id="strategyCardSections"' in html
        assert 'id="strategyCardToggle"' in html
        assert html.index('id="strategyCard"') < html.index('id="metricsCards"')

    def test_js_renders_card_from_api_payload(self):
        js = (ROOT / "src/backtest/web/static/js/backtest.js").read_text()
        assert "description_sections" in js
        assert "renderStrategyCard" in js
        assert "exits_risk" in js  # section keys wired into the renderer
        # updates on selection change AND on initial selection
        assert js.count("renderStrategyCard(") >= 4  # def + 3 call sites

    def test_card_styles_exist(self):
        css = (ROOT / "src/backtest/web/static/css/workspace.css").read_text()
        assert ".strategy-card" in css
        assert re.search(r"\.strategy-card-sections\s*\{", css)
