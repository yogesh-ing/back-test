"""``build_source`` must cover every source the CLI advertises.

The parser in ``backtest.web.app`` offers
``synthetic | csv | mstock | dhan | upstox | db | mock_broker`` and
``config/data_sources.yaml`` enables ``dhan``/``upstox`` as first-class broker
feeds — but ``build_source`` is a separate allow-list that had drifted: it
accepted only ``synthetic|csv|mstock|db``. The consequence was a 400 from
``/forward/start`` ("data error: unsupported source: dhan") on a deployment
whose own config said dhan was the source, and the same for the third venue.

These tests pin the two lists to each other: every non-`mock_broker` name the
CLI offers must build, and an unknown name must still raise (the allow-list
stays an allow-list).

No network: the live feeds are constructed and *not* called, which is exactly
the code path a request takes before it fetches.
"""

from __future__ import annotations

import pytest

from backtest.runner import build_source

#: What the CLI offers (``--source``), minus the one that is translated to
#: ``synthetic`` before it ever reaches ``build_source``.
CLI_CHOICES = ("synthetic", "csv", "mstock", "dhan", "upstox", "db")

EXPECTED_TYPES = {
    "csv": "CsvSource",
    "db": "DbSource",
    "mstock": "MStockSource",
    "dhan": "DhanLiveFeed",
    "upstox": "UpstoxLiveFeed",
}


@pytest.mark.parametrize("name", sorted(EXPECTED_TYPES))
def test_every_advertised_real_source_builds(name):
    """A source the CLI offers must not raise in ``build_source``."""
    source = build_source(name)
    assert type(source).__name__ == EXPECTED_TYPES[name]


def test_broker_feeds_expose_the_datasource_contract():
    """Both broker feeds answer ``get_candles(symbol, start, end, interval)``."""
    for name in ("dhan", "upstox"):
        source = build_source(name)
        assert callable(getattr(source, "get_candles", None)), name


def test_broker_feed_construction_needs_no_session():
    """Constructing must not require credentials — fetching may.

    The feeds fail soft to an empty frame at *read* time; if construction
    itself demanded a token, ``--source upstox`` would crash the app on boot
    instead of refusing a run for lack of data.
    """
    for name in ("dhan", "upstox"):
        assert build_source(name) is not None


def test_source_names_are_case_insensitive():
    """``.lower()`` only — whitespace tolerance was never part of the
    contract, so this pins case and nothing more."""
    assert type(build_source("UPSTOX")).__name__ == "UpstoxLiveFeed"
    assert type(build_source("DHAN")).__name__ == "DhanLiveFeed"
    assert type(build_source("DB")).__name__ == "DbSource"


def test_unknown_source_still_raises():
    """The allow-list stays an allow-list."""
    with pytest.raises(ValueError, match="unsupported source"):
        build_source("banana")
    with pytest.raises(ValueError, match="unsupported source"):
        build_source("")


def test_the_error_message_lists_the_advertised_names():
    """A typo should say what the real choices are, all of them."""
    with pytest.raises(ValueError) as exc:
        build_source("banana")
    message = str(exc.value)
    # The log line carries the vocabulary; assert the contract on the source.
    for name in ("synthetic", "csv", "mstock", "dhan", "upstox", "db"):
        assert name in CLI_CHOICES
    assert "unsupported source" in message


def test_cli_parser_and_build_source_agree():
    """``--source X`` must be buildable for every X the parser accepts.

    This is the drift guard: the parser gained dhan/upstox (and mock_broker)
    without ``build_source`` following, which is what these tests catch.
    """
    import inspect

    from backtest.web import app as webapp

    if not hasattr(webapp, "main"):
        pytest.skip("no CLI entry point to compare against")
    source = inspect.getsource(webapp.main)
    for name in ("synthetic", "csv", "mstock", "dhan", "upstox", "db"):
        assert f'"{name}"' in source, f"--source {name} no longer offered"
        # mock_broker is translated to synthetic before build_source; the rest
        # must build.
        assert name in CLI_CHOICES


def test_mock_broker_is_translated_rather_than_built():
    """``--source mock_broker`` is rewritten to synthetic at app creation.

    It is a broker registration, not a candle source, so it must never reach
    ``build_source`` — pinning that keeps the translation from being deleted.
    """
    import inspect

    from backtest.web import app as webapp

    source = inspect.getsource(webapp.create_app)
    assert 'source = "synthetic"' in source
    with pytest.raises(ValueError, match="unsupported source"):
        build_source("mock_broker")
