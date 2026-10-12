"""Segments & execution routing (PRD-001 Phase B, TEST-001).

* segment loading/validation (unknown broker refused, fail-soft file load);
* routing resolution (segment → broker; explicit override wins);
* paper/live mode isolation (paper ignores broker assignment);
* unknown broker/segment refusal (fail-closed — never silent rerouting);
* session-expired refusal via the ExecutionRouter.
"""

from __future__ import annotations

import textwrap

import pytest

from backtest.brokers.execution_router import BrokerSessionExpired, ExecutionRouter
from backtest.brokers.segments import (
    SegmentError,
    load_segments,
    resolve_execution_broker,
    validate_segment,
)

YAML = textwrap.dedent(
    """
    segments:
      options_index:
        display_name: "Index Options"
        broker: mstock
        mode: live
        allocated_capital: 1200000
        risk:
          daily_loss_limit: 50000
          max_drawdown_pct: 15
      equity_intraday:
        display_name: "Equity Intraday"
        broker: dhan
        mode: live
        allocated_capital: 800000
      swing:
        display_name: "Swing / Positional"
        broker: dhan
        mode: paper
        allocated_capital: 600000
    data:
      primary: mstock
      fallback: dhan
    """
)


@pytest.fixture()
def cfg(tmp_path):
    path = tmp_path / "segments.yaml"
    path.write_text(YAML, encoding="utf-8")
    return load_segments(path)


# ---------------------------------------------------------------------------
# Loading & validation
# ---------------------------------------------------------------------------


def test_segments_load(cfg):
    assert set(cfg.segments) == {"options_index", "equity_intraday", "swing"}
    seg = cfg.get("options_index")
    assert seg.broker == "mstock"
    assert seg.mode == "live"
    assert seg.allocated_capital == 1200000
    assert seg.risk["daily_loss_limit"] == 50000
    assert cfg.data_primary == "mstock"
    assert cfg.data_fallback == "dhan"
    assert cfg.errors == ()


def test_unknown_broker_in_segment_is_an_error(tmp_path):
    path = tmp_path / "segments.yaml"
    path.write_text(
        "segments:\n  bad:\n    broker: zerodha\n    mode: live\n", encoding="utf-8"
    )
    cfg = load_segments(path)
    assert "bad" not in cfg.segments
    assert any("unknown broker" in e for e in cfg.errors)


def test_validate_segment_rejects_bad_mode():
    with pytest.raises(SegmentError):
        validate_segment("x", {"broker": "mstock", "mode": "yolo"})


def test_missing_file_is_empty_config(tmp_path):
    cfg = load_segments(tmp_path / "nope.yaml")
    assert cfg.segments == {}
    assert cfg.errors == ()


def test_segments_for_broker(cfg):
    assert [s.name for s in cfg.for_broker("dhan")] == ["equity_intraday", "swing"]


# ---------------------------------------------------------------------------
# Routing resolution
# ---------------------------------------------------------------------------


def test_segment_routing(cfg):
    """Runner in 'options_index' segment routes to mStock."""
    assert (
        resolve_execution_broker("live", segment="options_index", config=cfg) == "mstock"
    )
    assert (
        resolve_execution_broker("live", segment="equity_intraday", config=cfg) == "dhan"
    )


def test_paper_mode_ignores_broker(cfg):
    """Paper runner uses the paper broker regardless of segment."""
    assert resolve_execution_broker("paper", segment="options_index", config=cfg) is None
    assert (
        resolve_execution_broker("paper", execution_broker="dhan", config=cfg) is None
    )


def test_explicit_execution_broker_wins(cfg):
    assert (
        resolve_execution_broker(
            "live", segment="options_index", execution_broker="dhan", config=cfg
        )
        == "dhan"
    )


def test_unknown_segment_refusal(cfg):
    with pytest.raises(SegmentError):
        resolve_execution_broker("live", segment="nope", config=cfg)


def test_unknown_broker_refusal(cfg):
    """Creating a runner with an unknown broker raises (never reroutes)."""
    with pytest.raises(SegmentError):
        resolve_execution_broker("live", execution_broker="zerodha", config=cfg)


# ---------------------------------------------------------------------------
# RunnerConfig derivation
# ---------------------------------------------------------------------------


def test_runner_config_derives_broker_from_segment(cfg, monkeypatch):
    import backtest.brokers.segments as seg_module

    monkeypatch.setattr(seg_module, "get_segments_config", lambda refresh=False: cfg)
    from backtest.forward.paper_runner import RunnerConfig

    config = RunnerConfig(
        name="r1",
        strategy_name="sma_crossover",
        allocated_capital=100000,
        symbols=["RELIANCE"],
        mode="live",
        source="mstock",
        segment="options_index",
    )
    assert config.execution_broker == "mstock"

    paper = RunnerConfig(
        name="r2",
        strategy_name="sma_crossover",
        allocated_capital=100000,
        symbols=["RELIANCE"],
        mode="paper",
        segment="swing",
    )
    assert paper.execution_broker is None  # paper ignores broker assignment

    with pytest.raises(ValueError):
        RunnerConfig(
            name="r3",
            strategy_name="sma_crossover",
            allocated_capital=100000,
            symbols=["RELIANCE"],
            mode="live",
            segment="does_not_exist",
        )


# ---------------------------------------------------------------------------
# ExecutionRouter
# ---------------------------------------------------------------------------


class _FakeSessions:
    def __init__(self, brokers):
        self._brokers = brokers

    def get_authenticated_broker(self, name):
        return self._brokers.get(name)


class _FakeBroker:
    def __init__(self, name):
        self.broker_name = name
        self.polled = []

    def poll_fill(self, order_id):
        self.polled.append(order_id)
        return {"status": "FILLED", "broker": self.broker_name}


def _cfg_dict(mode, segment=None, execution_broker=None):
    return {"mode": mode, "segment": segment, "execution_broker": execution_broker}


def test_router_order_for_routes_by_segment(cfg):
    mstock, dhan = _FakeBroker("mstock"), _FakeBroker("dhan")
    router = ExecutionRouter(
        session_manager=_FakeSessions({"mstock": mstock, "dhan": dhan}),
        segments=cfg,
        paper_broker="PAPER",
    )
    assert router.order_for(_cfg_dict("live", segment="options_index")) is mstock
    assert router.order_for(_cfg_dict("live", segment="equity_intraday")) is dhan
    # Paper mode → paper broker, broker assignment ignored.
    assert router.order_for(_cfg_dict("paper", segment="options_index")) == "PAPER"


def test_router_expired_session_pauses_not_reroutes(cfg):
    """Expired mStock session → BrokerSessionExpired; Dhan is NOT used."""
    dhan = _FakeBroker("dhan")
    router = ExecutionRouter(
        session_manager=_FakeSessions({"dhan": dhan}),  # mstock absent/expired
        segments=cfg,
    )
    with pytest.raises(BrokerSessionExpired) as exc:
        router.order_for(_cfg_dict("live", segment="options_index"))
    assert exc.value.broker_name == "mstock"


def test_router_poll_fill_per_broker(cfg):
    """Fill poll queries the CORRECT broker's order book."""
    mstock, dhan = _FakeBroker("mstock"), _FakeBroker("dhan")
    router = ExecutionRouter(
        session_manager=_FakeSessions({"mstock": mstock, "dhan": dhan}),
        segments=cfg,
    )
    router.poll_fill("OID-1", "mstock")
    router.poll_fill("OID-2", "dhan")
    assert mstock.polled == ["OID-1"]
    assert dhan.polled == ["OID-2"]

    with pytest.raises(BrokerSessionExpired):
        router.poll_fill("OID-3", "zerodha")
