"""One number-formatting system per page (finding F9).

The templates used to mix ``"{:,.0f}".format(...)`` (locale-less, so western
grouping) with JavaScript that grouped en-IN, so a portfolio row read
1,234,567 while the risk strip beside it read 12,34,567. Both halves now go
through one formatter — ``formatting.py`` on the server, ``Money`` in
``components/currency.js`` — and the ratchet below keeps the old pattern from
coming back.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "backtest" / "web"
TEMPLATES = WEB / "templates"
SCRIPTS = WEB / "static" / "js"

#: Frozen baseline. Every numeric format in the workspace now names its locale,
#: so this must stay empty: add a number back only by routing it through Money
#: (components/currency.js), never by listing it here.
LOCALELESS_JS_ALLOWLIST: dict[str, int] = {}

LOCALELESS_JS = re.compile(r"\.toLocaleString\(\)|\.toLocaleString\(undefined")
TEMPLATE_PY_FORMAT = re.compile(r'"\{:,')


@pytest.fixture
def app(monkeypatch):
    from backtest.forward.portfolio_manager import (
        get_portfolio_manager,
        reset_portfolio_manager,
    )
    from backtest.web.app import create_app

    monkeypatch.setenv("ALLOW_LIVE_ORDERS", "0")
    monkeypatch.setenv("BACKTEST_DATA_PROFILE", "testing")
    monkeypatch.setenv("FORWARD_TEST_DB_PROFILE", "testing")
    monkeypatch.setenv("FORWARD_TEST_DB_URL", "sqlite:///:memory:")
    reset_portfolio_manager(auto_start_feed=False)
    application = create_app(source="synthetic", log_level="ERROR", TESTING=True)
    yield application
    get_portfolio_manager().shutdown()


# ------------------------------------------------------------------ server half


def test_money_groups_lakhs_and_keeps_the_sign_outside_the_symbol():
    from backtest.web.formatting import format_money

    assert format_money(1234567) == "₹12,34,567"
    assert format_money(-1234567) == "-₹12,34,567"
    assert format_money(999) == "₹999"
    assert format_money(100000) == "₹1,00,000"
    assert format_money(1234.5, dp=2) == "₹1,234.50"


def test_small_numbers_and_garbage_do_not_become_thousand_separators():
    from backtest.web.formatting import format_count, format_money

    assert format_count(0) == "0"
    assert format_count(42) == "42"
    assert format_count(1234567) == "12,34,567"
    assert format_money("not a number") == "₹0"
    assert format_money(None) == "₹0"


def test_a_non_lakh_locale_groups_western():
    from backtest.web.formatting import format_count, format_money

    assert format_money(1234567, locale="en-US", symbol="$") == "$1,234,567"
    assert format_count(1234567, locale="en-US") == "1,234,567"


# --------------------------------------------------------------------- wiring


def test_the_filters_read_the_same_config_as_the_browser_helper(app):
    from backtest.web.formatting import format_money

    assert app.jinja_env.filters["money"](-1234567) == format_money(
        -1234567,
        symbol=app.config["CURRENCY_SYMBOL"],
        locale=app.config["CURRENCY_LOCALE"],
        dp=0,
    )
    assert app.jinja_env.filters["count"](1234567) == "12,34,567"


def test_the_template_and_the_browser_helper_agree_on_one_number(app):
    """The whole point of the slice: a server row and a client row, same digits."""
    rendered = app.jinja_env.from_string("{{ value | money }}").render(value=1234567)
    assert rendered == "₹12,34,567"
    currency_js = (SCRIPTS / "components" / "currency.js").read_text(encoding="utf-8")
    assert "en-IN" in currency_js, "Money must default to the same locale as the server"


# -------------------------------------------------------------------- ratchet


def test_no_template_formats_a_number_with_a_locale_less_python_format():
    hits = [
        f"{path.relative_to(ROOT).as_posix()}:{line_number}"
        for path in TEMPLATES.rglob("*.html")
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        )
        if TEMPLATE_PY_FORMAT.search(line)
    ]
    assert not hits, (
        'Templates must render money through {{ value | money }} and counts '
        f'through {{ value | count }}; found locale-less grouping at: {hits}'
    )


def test_no_script_formats_a_number_without_the_house_locale():
    counts: dict[str, int] = {}
    for path in SCRIPTS.rglob("*.js"):
        key = path.relative_to(SCRIPTS).as_posix()
        found = len(LOCALELESS_JS.findall(path.read_text(encoding="utf-8")))
        if found:
            counts[key] = found
    assert counts == LOCALELESS_JS_ALLOWLIST, (
        "Use Money.format/signed/num/count (components/currency.js) — a bare "
        f"toLocaleString() follows the visitor's locale: {counts}"
    )
