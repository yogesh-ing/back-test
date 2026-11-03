"""Sprint D / Ticket D2 — Unified Positions Blotter & Carry-Horizon Badges.

Verifies ``PortfolioManager.list_positions()`` surfaces open option structures
(including ``WAITING_FOR_FEED`` runners restored before broker login) alongside
equity positions, carrying ``carry_policy`` (``intraday`` | ``positional``),
``intraday_squareoff_minutes_before``, ``prev_close_mark``, ``net_open_pnl``,
``prior_close_net_pnl``, and ``today_pnl``.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from backtest.forward.paper_runner import FEED_STATE_WAITING, RunnerConfig
from backtest.forward.portfolio_manager import PortfolioManager
from backtest.options.paper_trading import OptionPosition, StructurePosition
from backtest.timekeeping import EXCHANGE_TZ


def _make_option_runner_cfg(
    *, name: str, instance_id: str, carry_policy: str
) -> RunnerConfig:
    return RunnerConfig(
        name=name,
        strategy_name="sma_crossover",
        symbols=["NIFTY"],
        allocated_capital=200_000.0,
        mode="paper",
        source="synthetic",
        instance_id=instance_id,
        instrument={
            "type": "option",
            "expression": {
                "expression_version": 2,
                "type": {
                    "BULLISH": {"structure": "long_call", "strike_selection": "atm"},
                    "BEARISH": {"structure": "long_put", "strike_selection": "atm"},
                },
                "quantity": 1,
                "carry_policy": carry_policy,
                "exit": {
                    "stop_loss_points": 1_000_000.0,
                    "signal_flip": False,
                    "min_days_to_expiry": None,
                },
            },
        },
    )


def test_list_positions_surfaces_carry_policy_prev_close_and_waiting_for_feed() -> None:
    mgr = PortfolioManager(auto_start_feed=False, persist_trades=False)
    r_pos = mgr.get_runner(
        mgr.add_runner(
            _make_option_runner_cfg(
                name="Positional Carry Book",
                instance_id="posbook001",
                carry_policy="positional",
            )
        )
    )
    r_intra = mgr.get_runner(
        mgr.add_runner(
            _make_option_runner_cfg(
                name="Intraday Carry Book",
                instance_id="intrabook001",
                carry_policy="intraday",
            )
        )
    )

    # 1. Carried-overnight positional structure on a WAITING_FOR_FEED runner:
    # Entry 100.00 x 75, comm 20.00; yesterday 15:30 close 110.00 -> prior_net = +730.00
    # Today live/frozen mark 118.00 -> gross +1350.00, net_open = +1330.00, today = +600.00
    leg_pos = OptionPosition(
        position_id="leg-pos-1",
        structure_id="struct-pos-1",
        strategy_name="sma_crossover",
        underlying="NIFTY",
        strike=Decimal("24000"),
        expiry=date(2026, 11, 26),
        option_type="CE",
        side="BUY",
        quantity=1,
        lot_size=75,
        entry_price=Decimal("100.00"),
        current_price=Decimal("118.00"),
        prev_close_mark=Decimal("110.00"),
        unrealized_pnl=Decimal("1350.00"),
        commission=Decimal("20.00"),
        opened_at=datetime(2026, 11, 2, 10, 0, tzinfo=EXCHANGE_TZ),
        trading_symbol="NIFTY26NOV24000CE",
    )
    struct_pos = StructurePosition(
        structure_id="struct-pos-1",
        structure_type="long_call",
        strategy_name="sma_crossover",
        underlying="NIFTY",
        expiry=date(2026, 11, 26),
        legs=[leg_pos],
        opened_at=datetime(2026, 11, 2, 10, 0, tzinfo=EXCHANGE_TZ),
        entry_session_date="2026-11-02",
        last_mark_session="2026-11-03",
        last_mark_net_pnl=Decimal("1330.00"),
        prior_close_net_pnl=Decimal("730.00"),
    )
    r_pos.options_bridge.option_broker._structures["struct-pos-1"] = struct_pos
    r_pos.options_bridge.option_broker._positions["leg-pos-1"] = leg_pos
    r_pos.options_bridge.open_structure_id = "struct-pos-1"
    r_pos.config.source = "mstock"
    r_pos.feed_state = FEED_STATE_WAITING
    r_pos.options_bridge.quote_provider.disconnected = True
    r_pos.pause()

    # 2. Same-day intraday structure:
    today_str = datetime.now(EXCHANGE_TZ).date().isoformat()
    leg_in = OptionPosition(
        position_id="leg-in-1",
        structure_id="struct-in-1",
        strategy_name="sma_crossover",
        underlying="NIFTY",
        strike=Decimal("24100"),
        expiry=date(2026, 11, 5),
        option_type="PE",
        side="BUY",
        quantity=1,
        lot_size=75,
        entry_price=Decimal("80.00"),
        current_price=Decimal("84.00"),
        unrealized_pnl=Decimal("300.00"),
        commission=Decimal("20.00"),
        opened_at=datetime.now(EXCHANGE_TZ),
        trading_symbol="NIFTY26NOV24100PE",
    )
    struct_in = StructurePosition(
        structure_id="struct-in-1",
        structure_type="long_put",
        strategy_name="sma_crossover",
        underlying="NIFTY",
        expiry=date(2026, 11, 5),
        legs=[leg_in],
        opened_at=datetime.now(EXCHANGE_TZ),
        entry_session_date=today_str,
    )
    r_intra.options_bridge.option_broker._structures["struct-in-1"] = struct_in
    r_intra.options_bridge.option_broker._positions["leg-in-1"] = leg_in
    r_intra.options_bridge.open_structure_id = "struct-in-1"

    rows = {r["instance_id"]: r for r in mgr.list_positions()}
    assert set(rows.keys()) == {"posbook001", "intrabook001"}

    pos_row = rows["posbook001"]
    assert pos_row["feed_state"] == FEED_STATE_WAITING
    assert pos_row["carry_policy"] == "positional"
    assert pos_row["carried_overnight"] is True
    assert pos_row["prev_close_mark"] == pytest.approx(110.0)
    assert pos_row["unrealized_pnl"] == pytest.approx(1350.0)
    assert pos_row["net_open_pnl"] == pytest.approx(1330.0)
    assert pos_row["prior_close_net_pnl"] == pytest.approx(730.0)
    assert pos_row["today_pnl"] == pytest.approx(600.0)
    assert pos_row["legs"][0]["prev_close_mark"] == pytest.approx(110.0)

    intra_row = rows["intrabook001"]
    assert intra_row["carry_policy"] == "intraday"
    assert intra_row["intraday_squareoff_minutes_before"] == 20
    assert intra_row["carried_overnight"] is False
    assert intra_row["prev_close_mark"] is None
    assert intra_row["net_open_pnl"] == pytest.approx(280.0)
    assert intra_row["today_pnl"] == pytest.approx(280.0)
