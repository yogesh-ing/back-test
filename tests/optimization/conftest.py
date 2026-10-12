"""Fixtures: in-memory SQLite store, fake runner manager, service."""

from __future__ import annotations

import logging

import pytest

from backtest.db import DatabaseManager
from backtest.optimization.service import OptimizationService
from backtest.optimization.store import OptimizationStore

from .support import FakeManager, synthetic_loader
from .support import sma_doc as _sma_doc


@pytest.fixture(autouse=True)
def _quiet_engine_logs():
    logger = logging.getLogger("backtest")
    old = logger.level
    logger.setLevel(logging.WARNING)
    yield
    logger.setLevel(old)


@pytest.fixture()
def store():
    # explicit URL: hermetic even when FORWARD_TEST_DB_URL points at a real DB
    manager = DatabaseManager.from_env(profile="testing", url="sqlite:///:memory:")
    manager.connect()
    st = OptimizationStore(manager)
    st.ensure_schema()
    yield st
    manager.disconnect()


@pytest.fixture()
def fake_manager():
    return FakeManager()


@pytest.fixture()
def service(store, fake_manager):
    # Named on purpose: a service with no default source used to have the
    # provenance layer quietly write "synthetic" for it. Here that IS the
    # truth - synthetic_loader generates the candles - so say it outright.
    return OptimizationService(store, workers=1, loader=synthetic_loader,
                               manager_getter=lambda: fake_manager,
                               default_source="synthetic")


@pytest.fixture()
def sma_doc():
    return _sma_doc()


@pytest.fixture()
def unacknowledged():
    """An optimize submission with the synthetic tick deliberately NOT given.

    Separate from ``sma_doc`` (which arrives ticked) so that tests about the
    gate have to ask for this by name — the absence of an acknowledgement is
    the thing under test, and it should never be an accident.
    """
    from .support import unacknowledged_sma_doc

    return unacknowledged_sma_doc()
