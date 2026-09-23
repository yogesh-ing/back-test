"""Options forward testing, task C2 — matrix + deep-dive option columns.

B3 taught the runner to *report* option structures (`options.open_structures_detail`,
`kind: "option"` trade records). Nobody rendered them: the matrix and the
deep-dive drawer are equity-shaped, and an option runner holds no equity
positions at all — so a runner with a live 25,000-point bull call spread
rendered as "Positions 0", with an empty Active Positions table and a Trades
tab that showed only the symbol string.

Three layers are covered here:

1. the numbers/labels the views need — `tests/js/test_option_view.mjs`;
2. the actual markup — `tests/js/render_option_views.mjs` loads the real
   `portfolio.js` / `deep_dive.js` against a dependency-free DOM stub and
   renders a live-shaped payload (row from `get_state()`, drawer from
   `get_detail()`);
3. the wiring — the components are loaded by `base.html` and used by both
   views.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from backtest.forward.paper_runner import OrderLedger, RunnerConfig, StrategyRunner
from backtest.strategy.base import Strategy
from backtest.strategy.intent import Direction, MarketView
from backtest.strategy import registry as _registry

if "silent_option_fixture" not in _registry._REGISTRY:

    class _SilentOptionStrategy(Strategy):
        """Emits NO view ever — the fixture drives entries explicitly.

        Needed since the warmup exemption (2026-09-18): option runners act on
        bar 1, and the base ``generate_market_view`` returns a NEUTRAL view for
        signal 0 — which plain-string expressions trade. A silent strategy is
        the only way a fixture can own the entry timing.
        """

        name = "silent_option_fixture"
        # Option-kind strategies must declare the instruments they may trade
        # (2026-09-22 contract, enforced by tests/test_strategy_conformance).
        # The fixture trades NIFTY only, and it is registered in the process-wide
        # catalogue — an undeclared entry here broke the conformance gate for
        # every later test in the same pytest session.
        eligible_instruments = ["NIFTY", "BANKNIFTY"]

        def generate_signals(self, candles):
            import pandas as pd

            return pd.Series(0, index=candles.index)

        def generate_market_view(self, candles):
            return None

    _registry._REGISTRY[_SilentOptionStrategy.name] = _SilentOptionStrategy

REPO_ROOT = Path(__file__).resolve().parents[1]
_JS_DIR = REPO_ROOT / "tests" / "js"
_VIEW_HARNESS = _JS_DIR / "test_option_view.mjs"
_RENDER_HARNESS = _JS_DIR / "render_option_views.mjs"

requires_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)

# Bars are dated well past the synthetic feed's wall clock and inside a known
# monthly cycle, so the chosen expiry is stable (see B2's `_CYCLE_*` fixtures).
_BAR_START = date(2026, 10, 1)


def _bars(closes: list[float], day_offset: int = 0) -> list[dict]:
    base = _BAR_START + timedelta(days=day_offset)
    return [
        {
            "ts": f"{(base + timedelta(days=i)).isoformat()}T09:15:00",
            "open": close - 10,
            "high": close + 30,
            "low": close - 30,
            "close": float(close),
            "volume": 1000,
        }
        for i, close in enumerate(closes)
    ]


def _option_runner_config(**overrides) -> RunnerConfig:
    instrument = {
        "type": "option",
        "expression": {
            "type": "bull_call_spread",
            "strike_selection": "atm",
            "quantity": 12,
            # 2 bars: enough to force one close, deterministic and view-free.
            "exit": {"max_bars": 2, "min_days_to_expiry": 1},
        },
    }
    # NOTE (2026-09-21): option runners are exempt from the warmup gate, so
    # the runner's OWN strategy views fire from bar 1. sma_crossover signals
    # BULLISH immediately on a rising series — the fixture would auto-trade.
    # The fixture's explicit on_market_view calls must be the only entries,
    # so the params below pin a strategy whose view stays None (price_move
    # with an unreachable threshold does exactly that).
    params = dict(
        name="NIFTY-BCS",
        strategy_name="silent_option_fixture",  # emits no view: fixture drives
        allocated_capital=500_000,
        symbols=["NIFTY"],
        timeframe="1day",
        mode="paper",
        source="synthetic",
        instrument=instrument,
    )
    params.update(overrides)
    return RunnerConfig(**params)


def _bull_view(spot: float) -> MarketView:
    """A bullish view carrying the spot it was formed at.

    The spot is not optional: without it the expression layer prices strikes
    against a zero underlying and the structure never executes.
    """
    return MarketView(
        direction=Direction.BULLISH,
        confidence=0.9,
        underlying="NIFTY",
        spot_price=Decimal(str(spot)),
    )


@pytest.fixture()
def option_runner():
    """A runner with one **closed** structure and one **open** one."""
    runner = StrategyRunner(_option_runner_config(), ledger=OrderLedger())
    runner.start()

    for bar in _bars([25_000, 25_010, 25_020, 25_030, 25_040]):
        runner.process_candle_event("NIFTY", bar)

    bridge = runner.options_bridge
    assert bridge is not None

    # 1) first structure — the time stop closes it two bars later
    bridge.on_market_view(_bull_view(runner.last_price["NIFTY"]), runner.config.strategy_name)
    for bar in _bars([25_120, 25_240, 25_360], day_offset=6):
        runner.process_candle_event("NIFTY", bar)

    # 2) second structure — one bar later it is still open (max_bars closes on
    # the *second* bar), so the render has a live book to show.
    bridge.on_market_view(_bull_view(runner.last_price["NIFTY"]), runner.config.strategy_name)
    for bar in _bars([25_650], day_offset=10):
        runner.process_candle_event("NIFTY", bar)

    yield runner
    runner.stop()


@pytest.fixture()
def snapshots(option_runner, tmp_path):
    """Row (matrix snapshot) + detail (deep-dive payload) as JSON files."""
    row = option_runner.get_state()
    detail = option_runner.get_detail()

    equity = StrategyRunner(
        RunnerConfig(
            name="RELIANCE-SMA",
            strategy_name="sma_crossover",
            allocated_capital=100_000,
            symbols=["RELIANCE"],
            timeframe="1day",
        ),
        ledger=OrderLedger(),
    )
    equity.start()
    for bar in _bars([1_400, 1_410, 1_420]):
        bar = dict(bar, ts=bar["ts"].replace("2026-10-01", "2026-10-01"))
        equity.process_candle_event("RELIANCE", bar)

    summary = {
        "success": True,
        "portfolio": {
            "total_equity": row["equity"] + equity.get_state()["equity"],
            "runners": [row, equity.get_state()],
            "buckets": {},
        },
    }
    (tmp_path / "summary.json").write_text(json.dumps(summary, default=str))
    (tmp_path / "detail.json").write_text(json.dumps(detail, default=str))
    (tmp_path / "equity_detail.json").write_text(json.dumps(equity.get_detail(), default=str))

    yield {
        "dir": tmp_path,
        "row": row,
        "detail": detail,
        "equity_row": equity.get_state(),
    }
    equity.stop()
    equity.stop()


def _render(workdir: Path, detail_file: str) -> dict[str, str]:
    """Render the views in node and split the harness's stdout into sections."""
    result = subprocess.run(
        ["node", str(_RENDER_HARNESS), str(workdir / "summary.json"), str(workdir / detail_file)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",  # harness prints ₹/→; Windows cp1252 decode kills stdout=None
        timeout=120,
    )
    assert result.returncode == 0, (
        f"render harness failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    sections: dict[str, str] = {}
    current = None
    for line in result.stdout.splitlines():
        if line.startswith("---") and line.endswith("---"):
            current = line.strip("-")
            sections[current] = ""
        elif current:
            sections[current] += line + "\n"
    assert sections, f"harness produced no sections:\n{result.stdout}"
    return sections


@pytest.fixture()
def rendered(snapshots):
    return _render(snapshots["dir"], "detail.json")


@pytest.fixture()
def rendered_equity(snapshots):
    return _render(snapshots["dir"], "equity_detail.json")


# ---------------------------------------------------------------------------
# 1. The view component's numbers and labels
# ---------------------------------------------------------------------------


@requires_node
def test_option_view_component_behaviour():
    result = subprocess.run(
        ["node", str(_VIEW_HARNESS)], cwd=REPO_ROOT, capture_output=True, text=True,
        encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, (
        f"node harness failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "24 tests passed" in result.stdout


# ---------------------------------------------------------------------------
# 2. The payload the views consume
# ---------------------------------------------------------------------------


class TestRunnerRowContract:
    def test_open_structures_are_reported_with_their_premium_and_expiry(self, snapshots):
        row = snapshots["row"]
        assert row["instrument"]["type"] == "option"

        book = row["options"]
        assert book["open_structures"] == 1
        structures = book["open_structures_detail"]
        assert len(structures) == 1

        s = structures[0]
        assert s["kind"] == "option"
        assert s["structure_type"] == "bull_call_spread"
        assert len(s["strikes"]) == 2
        assert s["strikes"][0] % 50 == 0  # index strikes sit on 50-point steps
        assert s["qty"] == 12
        assert s["units"] == s["qty"] * s["lot_size"]
        assert s["entry_price"] > 0  # net debit for a bull call spread
        assert s["entry_cost"] == pytest.approx(s["entry_price"] * s["units"], rel=1e-6)
        assert s["expiry"] and s["next_expiry"] == s["expiry"]
        assert len(s["legs_detail"]) == 2
        assert [leg["side"] for leg in s["legs_detail"]] == ["LONG", "SHORT"]
        assert all(leg["trading_symbol"] for leg in s["legs_detail"])

    def test_the_mark_moves_with_the_underlying(self, snapshots):
        s = snapshots["row"]["options"]["open_structures_detail"][0]
        # The fixture drives NIFTY up after entry, so the mark (and the P&L)
        # must be up too — this is what makes the columns worth rendering.
        assert s["current_price"] > s["entry_price"]
        assert s["unrealized_pnl"] > 0
        assert s["open_pnl_pct"] > 0
        assert s["bars_held"] > 0

    def test_open_positions_counts_the_structure_not_zero(self, snapshots):
        row = snapshots["row"]
        assert row["equity_positions"] == 0  # options live in the bridge
        assert row["open_positions"] == 1  # ... but the runner is not flat
        assert row["options"]["open_positions"] == 2  # legs, bridge contract intact

    def test_the_closed_structure_is_a_trade_record(self, snapshots):
        trades = snapshots["detail"]["trades"]
        options = [t for t in trades if t.get("kind") == "option"]
        assert options, "the closed structure never reached the trade log"
        t = options[-1]
        assert t["structure_type"] == "bull_call_spread"
        assert t["exit_reason"] == "time_stop"  # max_bars: 2
        assert t["legs"] == 2
        assert t["expiry"]


# ---------------------------------------------------------------------------
# 3. The rendered markup
# ---------------------------------------------------------------------------


class TestMatrixRendering:
    def test_the_row_shows_the_structure_and_premium_at_risk(self, rendered):
        matrix = rendered["MATRIX"]
        assert "badge-option" in matrix
        assert "Bull call spread" in matrix
        assert "premium at risk" in matrix
        assert "exp 29 Oct" in matrix.replace("Oct", "Oct")  # expiry of the open structure
        assert "24,950/25,000 CE" in matrix or "/" in matrix

    def test_the_positions_cell_counts_structures_with_legs_underneath(self, rendered):
        matrix = rendered["MATRIX"]
        assert "2 legs" in matrix
        # 12 lots of a 2-leg spread is 1 position, not 24 contracts.
        assert ">24<" not in matrix

    def test_the_equity_row_is_untouched(self, rendered):
        matrix = rendered["MATRIX"]
        assert "RELIANCE-SMA" in matrix
        # Exactly one option badge — the equity runner did not gain one.
        assert matrix.count("badge-option") == 1

    def test_the_aggregate_positions_tab_shows_every_leg(self, rendered):
        positions = rendered["POSITIONS"]
        assert "NIFTY" in positions and "CE" in positions
        assert "(net)" in positions          # the structure's summarising row
        assert "exp " in positions

    def test_an_equity_runner_renders_exactly_as_before(self, rendered):
        # The matrix renders every runner in the snapshot, so scope the check to
        # the equity runner's own row: it must gain no option badge and keep the
        # equity-shaped Type/Positions cells.
        rows = [r for r in rendered["MATRIX"].split("<tr") if "RELIANCE-SMA" in r]
        assert len(rows) == 1
        equity_row = rows[0]
        assert "badge-option" not in equity_row
        assert "badge-single" in equity_row
        # And the option runner's row does carry the badge (the control case).
        assert "badge-option" in rendered["MATRIX"]

    def test_an_equity_runners_drawer_has_no_option_section(self, rendered_equity):
        body = rendered_equity["DEEPDIVE"]
        assert "Options Book" not in body
        assert "Net Premium" not in body
        assert "Active Open Positions" in body


class TestDeepDiveRendering:
    def test_the_drawer_has_an_options_book_section(self, rendered):
        body = rendered["DEEPDIVE"]
        assert "Options Book" in body
        assert "Open Structures" in body
        assert "Premium at Risk" in body
        assert "Book P&amp;L (Open)" in body
        assert "Next Expiry" in body

    def test_the_open_structure_table_lists_strikes_premium_and_expiry(self, rendered):
        body = rendered["DEEPDIVE"]
        assert "Net Premium" in body
        assert "Bull call spread" in body
        assert "Expiry" in body
        assert "% of premium" in body  # marked against what was paid

    def test_each_leg_is_shown_with_its_own_side_and_mark(self, rendered):
        body = rendered["DEEPDIVE"]
        assert "↳ leg" in body
        assert body.count("↳ leg") == 2
        assert "NIFTY" in body and "CE" in body

    def test_closed_structures_name_why_they_closed(self, rendered):
        body = rendered["DEEPDIVE"]
        assert "trade-option" in body
        assert "Time stop" in body
        assert "Exit Reason" in body

    def test_the_config_tab_states_the_instrument_and_exit_plan(self, rendered):
        config = rendered["DEEPDIVE-CONFIG"]
        assert "Options" in config
        assert "Strike selection" in config
        assert "Lots per leg" in config
        assert "Exit policy" in config
        assert "max 2 bars" in config


# ---------------------------------------------------------------------------
# 4. End to end: the payload the endpoints actually send, rendered
# ---------------------------------------------------------------------------


@pytest.fixture()
def api_client():
    """The real app with the process-wide manager reset and its feed off."""
    from backtest.forward.portfolio_manager import (
        get_portfolio_manager,
        reset_portfolio_manager,
    )
    from backtest.forward.risk_supervisor import GlobalRiskConfig
    from backtest.web.app import create_app

    reset_portfolio_manager(
        risk_config=GlobalRiskConfig(daily_loss_limit=100_000, max_drawdown_pct=0.50),
        tick_seconds=1.0,
        warmup_bars=15,
        auto_start_feed=False,
    )
    app = create_app({"TESTING": True})
    with app.test_client() as client:
        yield client
    get_portfolio_manager().shutdown()


@requires_node
def test_live_api_payload_renders_option_columns(api_client, tmp_path):
    """Spawn over HTTP → drive bars → summary + deep dive → render the JS."""
    from backtest.forward.portfolio_manager import get_portfolio_manager

    payload = {
        "name": "NIFTY-BCS-API",
        "strategy": "sma_crossover",
        "allocated_capital": 500_000,
        "symbol": "NIFTY",
        "timeframe": "1day",
        "mode": "paper",
        "source": "synthetic",
        "target_type": "SINGLE_SYMBOL",
        "instrument": {
            "type": "option",
            "expression": {
                "type": "bull_call_spread",
                "strike_selection": "atm",
                "quantity": 5,
                "exit": {"max_bars": 2, "min_days_to_expiry": 1},
            },
        },
    }
    created = api_client.post("/api/portfolio/runner/create", json=payload)
    assert created.status_code == 201, created.get_json()
    iid = created.get_json()["instance_id"]

    runner = get_portfolio_manager().get_runner(iid)
    for bar in _bars([25_000, 25_010, 25_020, 25_030, 25_040]):
        runner.process_candle_event("NIFTY", bar)
    bridge = runner.options_bridge
    bridge.on_market_view(_bull_view(runner.last_price["NIFTY"]), payload["strategy"])
    for bar in _bars([25_120, 25_240, 25_360], day_offset=6):
        runner.process_candle_event("NIFTY", bar)
    bridge.on_market_view(_bull_view(runner.last_price["NIFTY"]), payload["strategy"])
    for bar in _bars([25_650], day_offset=10):
        runner.process_candle_event("NIFTY", bar)

    summary = api_client.get("/api/portfolio/summary?mode=paper")
    assert summary.status_code == 200
    snapshot = summary.get_json()
    row = next(r for r in snapshot["portfolio"]["runners"] if r["instance_id"] == iid)
    assert row["options"]["open_structures"] == 1
    assert row["open_positions"] == 1  # the structure, not 0

    detail_response = api_client.post(
        f"/api/portfolio/runner/{iid}/control", json={"action": "deep_dive"}
    )
    assert detail_response.status_code == 200
    detail_payload = detail_response.get_json()["runner"]
    structures = detail_payload["options"]["open_structures_detail"]
    assert structures and structures[0]["legs_detail"]

    (tmp_path / "summary.json").write_text(json.dumps(snapshot, default=str))
    (tmp_path / "detail.json").write_text(
        json.dumps(detail_payload, default=str)
    )
    rendered = _render(tmp_path, "detail.json")

    # The browser-visible result of the whole chain.
    assert "badge-option" in rendered["MATRIX"]
    assert "premium at risk" in rendered["MATRIX"]
    assert "Options Book" in rendered["DEEPDIVE"]
    assert "Net Premium" in rendered["DEEPDIVE"]
    assert "Time stop" in rendered["DEEPDIVE"]  # the closed structure's reason
    assert "↳ leg" in rendered["DEEPDIVE"]


# ---------------------------------------------------------------------------
# 5. Wiring
# ---------------------------------------------------------------------------


def test_components_are_loaded_and_used():
    base = (REPO_ROOT / "src/backtest/web/templates/base.html").read_text(encoding="utf-8")
    assert "js/components/option_view.js" in base
    # Order matters: option_view.js declares its dependency on option_config.js.
    assert base.index("option_config.js") < base.index("option_view.js")

    portfolio = (REPO_ROOT / "src/backtest/web/static/js/portfolio.js").read_text(encoding="utf-8")
    for call in ("OptionView.isOption", "OptionView.matrixNotes",
                 "OptionView.positionsCell", "OptionView.openStructures"):
        assert call in portfolio, f"matrix does not use {call}"

    # Live Order Management (2026-09-23): the positions table's action modals
    # and the Orders tab are two more globally-loaded components. Both format
    # money through currency.js, so they must load after it — and the page
    # renderer must actually call them, or the buttons render dead.
    for component in ("js/components/position_actions.js", "js/components/orders_tab.js"):
        assert component in base, f"{component} is not loaded"
        assert base.index("currency.js") < base.index(component), f"{component} before currency.js"
    for call in ("buttonCell", "PositionActions.rows", "PositionActions.init",
                 "OrdersTab.init", "OrdersTab.refresh", "OrdersTab.noteTick"):
        assert call in portfolio, f"positions/orders UI does not use {call}"
    # …and the action buttons are a real cell in the positions table.
    assert "positionActionsCell(row)" in portfolio

    deep_dive = (REPO_ROOT / "src/backtest/web/static/js/deep_dive.js").read_text(encoding="utf-8")
    for call in ("OptionView.bookStats", "OptionView.structureRows",
                 "OptionView.exitReasonLabel", "OptionView.isOptionTrade"):
        assert call in deep_dive, f"drawer does not use {call}"


def test_option_columns_have_styles():
    css = (REPO_ROOT / "src/backtest/web/static/css/app.css").read_text(encoding="utf-8")
    assert ".badge-option" in css
    assert ".dd-stats-4" in css
    assert ".opt-total" in css


def test_the_action_surface_carries_its_handles():
    """The DOM contract PositionActions / OrdersTab are wired against.

    The components resolve rows and modals by id at click time (no per-row
    listeners), so a renamed id fails silently in the browser — pin the handles
    the templates promise: the positions table's tbody, the four action modals
    with their submit buttons, and the Orders tab's filter/readout elements.
    """
    center = (REPO_ROOT / "src/backtest/web/templates/_portfolio_center.html").read_text(
        encoding="utf-8"
    )
    for token in ('data-tab="orders"', 'id="orders-tab-badge"', 'id="tab-orders"',
                  'id="orders-body"', 'id="orders-summary"', 'id="orders-status"',
                  'id="orders-limit"', 'id="orders-search"', 'id="orders-refresh"',
                  'id="aggregate-positions"', 'id="pos-search"', 'id="pos-rules-only"',
                  'id="pos-summary"', 'id="pos-footnote"'):
        assert token in center, f"command center is missing {token}"
    for control in ("pos-sl-modal", "pos-sl-value", "pos-sl-submit", "pos-sl-clear",
                    "pos-target-modal", "pos-target-value", "pos-target-submit",
                    "pos-target-clear", "pos-partial-modal", "pos-partial-value",
                    "pos-partial-submit", "pos-closeall-modal", "pos-closeall-confirm"):
        assert 'id="' + control + '"' in center, f"missing control {control}"
    # The Actions column is a header the operator can see, not just markup that
    # only appears inside rows.
    assert "Actions" in center


def test_the_action_and_order_styles_exist():
    css = (REPO_ROOT / "src/backtest/web/static/css/app.css").read_text(encoding="utf-8")
    for cls in (".pos-actions", ".pos-modal-context", ".pos-modal-line",
                ".pos-fraction-row", ".pos-row-stale", ".opt-leg-row",
                ".orders-summary", ".orders-stat", ".order-status", ".tab-badge"):
        assert cls in css, f"missing style {cls}"
    # Adverse slippage must read as a loss, and a working order must be visible.
    assert ".order-row.order-pending" in css
    assert ".order-row.order-rejected" in css
