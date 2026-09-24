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
    return OptimizationService(store, workers=1, loader=synthetic_loader,
                               manager_getter=lambda: fake_manager)


@pytest.fixture()
def sma_doc():
    return _sma_doc()
