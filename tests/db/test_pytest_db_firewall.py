"""Ticket 2.4 / BUG-37: a pytest run must never resolve the live paper database.

The suite has historically written fixture portfolios and closed trades into
``forward_test`` (27 orphan rows, ₹82.5L of phantom ``sum(initial_capital)``),
because the live URL is what ``FORWARD_TEST_DB_URL`` holds on a developer box.
These tests pin the gate that stops that — including the cases it must *not*
fire on, so migration tests keep their scratch database and the app keeps its URL.
"""

from __future__ import annotations

import pytest

from backtest.db.config import (
    PYTEST_SANDBOX_URL,
    ConfigError,
    load_config,
    pytest_db_firewall,
)

LIVE_URL = "postgresql+psycopg2://ft_app:secret@localhost:5432/forward_test"
SCRATCH_URL = "postgresql+psycopg2://ft_app:secret@localhost:5432/forward_test_scratch"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Isolate the variables the firewall reads, for every case in this file."""
    monkeypatch.delenv("FORWARD_TEST_DB_URL", raising=False)
    monkeypatch.delenv("ALLOW_LIVE_DB_IN_PYTEST", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/db/test_pytest_db_firewall.py::x (call)")


class TestFromEnvironment:
    def test_live_url_from_env_is_redirected(self, monkeypatch):
        monkeypatch.setenv("FORWARD_TEST_DB_URL", LIVE_URL)
        cfg = load_config()
        assert cfg.url == PYTEST_SANDBOX_URL
        assert cfg.is_memory_sqlite

    def test_redirection_is_attributable_in_provenance(self, monkeypatch):
        monkeypatch.setenv("FORWARD_TEST_DB_URL", LIVE_URL)
        source = load_config()._source
        assert source["url"].startswith("pytest-firewall(from env)")

    def test_escape_hatch_admits_the_live_url(self, monkeypatch):
        monkeypatch.setenv("FORWARD_TEST_DB_URL", LIVE_URL)
        monkeypatch.setenv("ALLOW_LIVE_DB_IN_PYTEST", "1")
        assert load_config().url == LIVE_URL


class TestFromExplicitArgument:
    """An explicit ``url=`` is the easiest way to forget the gate, so it is covered."""

    def test_live_url_passed_as_argument_is_redirected(self):
        cfg = load_config(url=LIVE_URL)
        assert cfg.url == PYTEST_SANDBOX_URL

    def test_scratch_database_is_left_alone(self):
        """Migration tests create their own database on the same server."""
        assert load_config(url=SCRATCH_URL).url == SCRATCH_URL

    def test_sqlite_is_left_alone(self):
        assert load_config(url="sqlite:///x.db").url == "sqlite:///x.db"


class TestOutsidePytest:
    def test_the_application_keeps_its_live_url(self, monkeypatch):
        """Inert unless pytest is running — this must not touch the deployed app."""
        monkeypatch.delenv("PYTEST_CURRENT_TEST")
        assert pytest_db_firewall(LIVE_URL) == LIVE_URL

    def test_missing_url_still_explains_how_to_fix_it(self, tmp_path):
        empty = tmp_path / "empty.yaml"
        empty.write_text("profiles:\n  dev: {}\n")
        with pytest.raises(ConfigError, match="FORWARD_TEST_DB_URL"):
            load_config(path=str(empty), profile="dev")


class TestLiveNameRecognition:
    @pytest.mark.parametrize(
        "url, expected",
        [
            (LIVE_URL, "forward_test"),
            ("postgresql://u@h:5432/forward_test", "forward_test"),
            ("postgres://u@h:5432/forward_test", "forward_test"),
            (SCRATCH_URL, None),
            ("postgresql://u@h:5432/postgres", None),
            ("sqlite:///:memory:", None),
            ("not-a-url-at-all", None),
            ("", None),
        ],
    )
    def test_only_the_live_ledger_names_are_caught(self, url, expected):
        from backtest.db.config import _live_db_name

        assert _live_db_name(url) == expected
