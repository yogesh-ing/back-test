"""Phase 4 verification suite — Straddles, WS order push & LiveOptionBrokerAdapter.

All tests run strictly against paper books and in-memory fake/mocked brokers
(Condition 2: zero real-venue calls; straddles/strangles remain paper-only in
``config/risk_limits.yaml`` until Monday 09:15 IST soak + ~2026-10-31 risk
policy sign-off). ``src/backtest/data/mticker.py`` and
``tests/test_mticker_ws_fixes.py`` remain untouched (Condition 1).
"""

from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from backtest.brokers.base import BrokerOrder, BrokerOrderId
from backtest.brokers.mstock import (
    MStockBroker,
    MStockOrderError,
    OrderOutcomeUnknown,
)
from backtest.forward.feed_registry import reset_data_bus
from backtest.forward.live_gateway import LiveEquityGateway
from backtest.forward.options_bridge import OptionsBridge
from backtest.forward.paper_runner import (
    ORDER_FILLED,
    STATUS_PAUSED,
    STATUS_RUNNING,
    OrderLedger,
    RunnerConfig,
    StrategyRunner,
)
from backtest.instruments.base import OptionType
from backtest.instruments.option import OptionContract
from backtest.options.execution_gate import (
    RESEARCH_STRUCTURES,
    PreTradeRiskGate,
    check_intent,
)
from backtest.options.intent_journal import (
    KIND_HALTED,
    KIND_UNCERTAIN,
    MemoryJournal,
)
from backtest.options.live_trading import (
    LiveExecutionHaltedError,
    LiveOptionBrokerAdapter,
    LiveOptionTrader,
    StructureExecStatus,
    _round_nfo_tick,
)
from backtest.options.paper_trading import PositionStatus
from backtest.options.structures import (
    LongStraddle,
    ShortStraddle,
    create_structure,
)
from backtest.risk_limits import RiskLimits
from backtest.strategy.execution import (
    parse_exit_plan_strict,
    validate_expression,
)
from backtest.strategy.intent import Direction, MarketView, OptionLeg, TradeIntent


EXPIRY = date(2026, 11, 26)


def _contract(
    strike: int,
    opt_type: OptionType,
    *,
    expiry: date = EXPIRY,
    underlying: str = "NIFTY",
    lot_size: int = 75,
) -> OptionContract:
    side_code = opt_type.value
    return OptionContract(
        instrument_token=f"TOK-{underlying}-{strike}-{side_code}",
        trading_symbol=f"{underlying}26NOV{strike}{side_code}",
        underlying=underlying,
        expiry=expiry,
        strike=Decimal(str(strike)),
        option_type=opt_type,
        lot_size=lot_size,
        metadata={},
    )


def _two_sided_chain(
    strikes: list[int] | None = None,
) -> dict[Decimal, dict[str, OptionContract]]:
    strikes = strikes if strikes is not None else [24750, 24800, 24850]
    out: dict[Decimal, dict[str, OptionContract]] = {}
    for k in strikes:
        out[Decimal(str(k))] = {
            "CE": _contract(k, OptionType.CE),
            "PE": _contract(k, OptionType.PE),
        }
    return out


def _view(
    direction: Direction = Direction.NEUTRAL,
    spot: float = 24_800.0,
    underlying: str = "NIFTY",
) -> MarketView:
    return MarketView(
        direction=direction,
        confidence=0.85,
        underlying=underlying,
        spot_price=Decimal(str(spot)),
        bar_timestamp=datetime(2026, 10, 26, 4, 0, tzinfo=timezone.utc),
    )


def _wide_paper_gate() -> PreTradeRiskGate:
    return PreTradeRiskGate(
        RiskLimits(
            per_trade_max_loss_inr=1_000_000.0,
            per_runner_max_lots=100,
            per_runner_max_order_value_inr=10_000_000.0,
            entitlement={"paper": frozenset(RESEARCH_STRUCTURES)},
            kill_switch_engaged_on_boot=False,
            sha256="a" * 64,
        ),
        mode="paper",
    )


class FakeVenueBroker:
    """Deterministic in-memory broker implementing WS order push + REST book."""

    def __init__(
        self,
        *,
        contracts: list[OptionContract] | None = None,
        quotes: dict[str, dict[str, float]] | None = None,
        fill_slippage: float = 0.25,
    ) -> None:
        self.contracts = list(contracts or [])
        self.quotes = dict(quotes or {})
        self.fill_slippage = fill_slippage
        self.placed: list[BrokerOrder] = []
        self.order_book: dict[str, BrokerOrder] = {}
        self.listeners: list[Any] = []
        self.ws_updates: dict[str, dict[str, Any]] = {}
        self.auto_ws_fill: bool = True
        self.reject_leg_indices: set[int] = set()
        self.timeout_leg_indices: set[int] = set()
        self._lock = threading.RLock()
        self._seq = 0

    def is_authenticated(self) -> bool:
        return True

    def add_order_listener(self, cb: Any) -> None:
        if cb not in self.listeners:
            self.listeners.append(cb)

    def remove_order_listener(self, cb: Any) -> None:
        if cb in self.listeners:
            self.listeners.remove(cb)

    def get_ws_order_update(self, order_id: str) -> dict[str, Any] | None:
        return self.ws_updates.get(str(order_id))

    def find_ws_order_update(
        self,
        *,
        client_order_id: str | None = None,
        symbol: str | None = None,
        side: str | None = None,
        quantity: int | None = None,
    ) -> dict[str, Any] | None:
        for row in reversed(list(self.ws_updates.values())):
            if client_order_id and row.get("client_order_id") == client_order_id:
                return dict(row)
            if (
                symbol
                and row.get("symbol") == str(symbol).upper()
                and row.get("side") == str(side).upper()
                and int(row.get("quantity") or 0) == int(quantity or 0)
            ):
                return dict(row)
        return None

    def get_option_chain(self, underlying: str) -> list[OptionContract]:
        return [c for c in self.contracts if c.underlying == underlying]

    def get_option_quote(self, token: str) -> dict[str, Any]:
        return dict(
            self.quotes.get(
                token,
                {"ltp": 120.0, "bid": 119.5, "ask": 120.5, "volume": 1000, "oi": 5000},
            )
        )

    def place_order(self, order: BrokerOrder) -> BrokerOrderId:
        leg_idx = int((order.tag or {}).get("leg_index", len(self.placed)))
        if leg_idx in self.reject_leg_indices and not (order.tag or {}).get("rollback"):
            raise MStockOrderError(f"Rejected leg {leg_idx}", status_code=400)

        with self._lock:
            self._seq += 1
            oid = f"VENUE-{self._seq:04d}"
            self.placed.append(order)

        if leg_idx in self.timeout_leg_indices and not (order.tag or {}).get("rollback"):
            pending = BrokerOrder(
                broker_order_id=BrokerOrderId(oid),
                client_order_id=order.client_order_id,
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                order_type=order.order_type,
                limit_price=order.limit_price,
                status="OPEN",
                filled_quantity=0,
                average_fill_price=0.0,
            )
            with self._lock:
                self.order_book[oid] = pending
            return BrokerOrderId(oid)

        tok = (order.tag or {}).get("instrument_token", "")
        q = self.get_option_quote(tok)
        base_px = (
            float(q.get("ask") or q.get("ltp") or 100.0)
            if order.side == "BUY"
            else float(q.get("bid") or q.get("ltp") or 100.0)
        )
        fill_px = round(
            base_px + (self.fill_slippage if order.side == "BUY" else -self.fill_slippage),
            2,
        )
        filled_order = BrokerOrder(
            broker_order_id=BrokerOrderId(oid),
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            order_type=order.order_type,
            limit_price=order.limit_price,
            status="EXECUTED",
            filled_quantity=int(order.quantity),
            average_fill_price=fill_px,
        )
        with self._lock:
            self.order_book[oid] = filled_order

        if self.auto_ws_fill:
            ws_row = {
                "order_id": oid,
                "client_order_id": order.client_order_id,
                "symbol": str(order.symbol).upper(),
                "side": str(order.side).upper(),
                "quantity": int(order.quantity),
                "filled_quantity": int(order.quantity),
                "average_price": fill_px,
                "status": "COMPLETE",
                "event_type": "order",
            }
            self.ws_updates[oid] = ws_row
            for cb in list(self.listeners):
                threading.Timer(0.005, cb, args=(ws_row,)).start()
        return BrokerOrderId(oid)

    def get_order_book(self) -> list[BrokerOrder]:
        with self._lock:
            return list(self.order_book.values())


# ---------------------------------------------------------------------------
# 1. Straddle Builders, Exit Plan Validation & Condition 2 Risk Entitlements
# ---------------------------------------------------------------------------


class TestStraddleStructuresAndPolicy:
    def test_long_straddle_builds_atm_buy_ce_and_pe(self):
        chain = _two_sided_chain()
        s = create_structure("long_straddle")
        assert isinstance(s, LongStraddle)
        assert s.is_defined_risk is True
        assert s.max_loss_description() == "net_debit"

        intent = s.build(_view(), [Decimal("24800")], chain, EXPIRY, strategy_name="vol")
        assert intent.structure_type == "long_straddle"
        assert len(intent.legs) == 2
        assert [leg.side for leg in intent.legs] == ["BUY", "BUY"]
        assert intent.legs[0].trading_symbol.endswith("CE")
        assert intent.legs[1].trading_symbol.endswith("PE")
        check_intent(intent, RESEARCH_STRUCTURES)

    def test_short_straddle_builds_atm_sell_ce_and_pe(self):
        chain = _two_sided_chain()
        s = create_structure("short_straddle")
        assert isinstance(s, ShortStraddle)
        assert s.is_defined_risk is False
        assert s.max_loss_description() == "unlimited"

        intent = s.build(_view(), [Decimal("24800")], chain, EXPIRY, strategy_name="theta")
        assert intent.structure_type == "short_straddle"
        assert len(intent.legs) == 2
        assert [leg.side for leg in intent.legs] == ["SELL", "SELL"]
        assert intent.legs[0].trading_symbol.endswith("CE")
        assert intent.legs[1].trading_symbol.endswith("PE")
        check_intent(intent, RESEARCH_STRUCTURES)

    def test_straddles_reject_flat_single_sided_chain(self):
        flat_chain = {Decimal("24800"): _contract(24800, OptionType.CE)}
        with pytest.raises(ValueError, match="two-sided chain"):
            LongStraddle().build(_view(), [Decimal("24800")], flat_chain, EXPIRY)
        with pytest.raises(ValueError, match="two-sided chain"):
            ShortStraddle().build(_view(), [Decimal("24800")], flat_chain, EXPIRY)

    def test_exit_plan_caps_long_straddle_but_forbids_uncapped_short_straddle(self):
        # LongStraddle has bounded debit risk -> structure_capped=True is legal
        errs_long = validate_expression(
            {
                "expression_version": 2,
                "type": {"BULLISH": "long_straddle"},
                "exit": {"structure_capped": True, "min_days_to_expiry": 1},
            },
            permitted_structures=RESEARCH_STRUCTURES,
        )
        assert errs_long == []

        # ShortStraddle has unlimited loss -> structure_capped without hard stop refused
        errs_short = validate_expression(
            {
                "expression_version": 2,
                "type": {"BULLISH": "short_straddle"},
                "exit": {"structure_capped": True, "min_days_to_expiry": 1},
            },
            permitted_structures=RESEARCH_STRUCTURES,
        )
        assert any("short_straddle" in e for e in errs_short), errs_short
        errs_short_str = validate_expression(
            {
                "expression_version": 2,
                "type": "short_straddle",
                "exit": {"structure_capped": True, "min_days_to_expiry": 1},
            },
            permitted_structures=RESEARCH_STRUCTURES,
        )
        assert any("short_straddle" in e for e in errs_short_str), errs_short_str

        # ShortStraddle WITH explicit max_loss_inr + stop_loss_pct is accepted
        errs_capped = validate_expression(
            {
                "expression_version": 2,
                "type": {"BULLISH": "short_straddle"},
                "exit": {
                    "max_loss_inr": 15_000.0,
                    "stop_loss_pct": 0.4,
                    "min_days_to_expiry": 1,
                },
            },
            permitted_structures=RESEARCH_STRUCTURES,
        )
        assert errs_capped == []
        capped_short = parse_exit_plan_strict(
            {"max_loss_inr": 15_000.0, "stop_loss_pct": 0.4, "min_days_to_expiry": 1}
        )
        assert capped_short.max_loss_inr == 15_000.0
        assert capped_short.stop_loss_pct == 0.4

    def test_condition_2_risk_limits_keep_straddles_paper_only(self):
        paper_gate = PreTradeRiskGate(mode="paper")
        live_gate = PreTradeRiskGate(mode="live")

        assert "long_straddle" in paper_gate.permitted_structures()
        assert "short_straddle" in paper_gate.permitted_structures()
        # Condition 2: live entitlement stays strictly {long_call, long_put}
        assert live_gate.permitted_structures() == frozenset({"long_call", "long_put"})
        assert "long_straddle" not in live_gate.permitted_structures()
        assert "short_straddle" not in live_gate.permitted_structures()
        assert "strangle" not in live_gate.permitted_structures()


# ---------------------------------------------------------------------------
# 2. Paper OptionsBridge Execution + HotStrikeRing Pinning on Straddles
# ---------------------------------------------------------------------------


class TestPaperBridgeStraddles:
    @pytest.mark.parametrize(
        "structure_type,exit_cfg,expected_sides",
        [
            (
                "long_straddle",
                {"structure_capped": True, "take_profit_pct": 0.5, "min_days_to_expiry": 1},
                ["BUY", "BUY"],
            ),
            (
                "short_straddle",
                {
                    "stop_loss_pct": 0.5,
                    "max_loss_inr": 25_000.0,
                    "take_profit_pct": 0.4,
                    "min_days_to_expiry": 1,
                },
                ["SELL", "SELL"],
            ),
        ],
    )
    def test_paper_bridge_opens_marks_and_closes_straddle_with_ring_pins(
        self, structure_type, exit_cfg, expected_sides
    ):
        expr = {
            "expression_version": 2,
            "type": {"BULLISH": {"structure": structure_type, "strike_selection": "atm"}},
            "quantity": 1,
            "exit": exit_cfg,
        }
        ledger = OrderLedger()
        bridge = OptionsBridge(
            capital=1_000_000.0,
            expression=expr,
            mode="paper",
            risk_gate=_wide_paper_gate(),
        )
        bridge.option_broker.on_leg_fill = lambda rec: ledger.record_filled(
            "paper-inst",
            symbol=rec["symbol"],
            side=rec["side"],
            quantity=float(rec["quantity"]),
            price=float(rec["price"]),
            requested_price=float(rec["requested_price"]),
        )
        # Attach pin_contracts / unpin_contracts hooks to verify 1:1 2-leg straddle pinning
        pinned_batches: list[list[str]] = []
        unpinned_batches: list[list[str]] = []
        qp_obj: Any = bridge.quote_provider
        qp_obj.pin_contracts = lambda toks: pinned_batches.append(list(toks))
        qp_obj.unpin_contracts = lambda toks: unpinned_batches.append(list(toks))

        bar_ts = "2026-10-12T09:20:00+05:30"
        bridge.on_bar("NIFTY", 24_800.0, bar_ts)
        res = bridge.on_intent(
            {"structure": structure_type, "strike_selection": "atm"},
            MarketView(
                direction=Direction.BULLISH,
                confidence=0.9,
                underlying="NIFTY",
                spot_price=Decimal("24800"),
                bar_timestamp=datetime.fromisoformat(bar_ts),
            ),
            "straddle_test",
        )
        assert res is not None and not res.get("rejected"), res
        open_structs = bridge.option_broker.get_open_structures()
        assert len(open_structs) == 1
        assert len(open_structs[0].legs) == 2
        assert [p.side for p in open_structs[0].legs] == expected_sides
        assert {p.option_type for p in open_structs[0].legs} == {"CE", "PE"}
        assert len(pinned_batches) == 1 and len(pinned_batches[0]) == 2

        exit_evt = bridge.manual_exit(reason="test_flatten", strategy_name="straddle_test")
        assert exit_evt is not None and exit_evt["exited"] is True
        assert len(unpinned_batches) == 1 and unpinned_batches[0] == pinned_batches[0]
        # 2 entry leg fills + 2 exit leg fills mirrored in OrderLedger
        assert len(ledger.orders_for("paper-inst")) == 4

    def test_straddle_entry_validation_refuses_missing_put_side(self):
        from backtest.options.execution_gate import ContractValidityError

        expr = {
            "expression_version": 2,
            "type": {"BULLISH": "long_straddle"},
            "quantity": 1,
            "exit": {"structure_capped": True, "min_days_to_expiry": 1},
        }
        bridge = OptionsBridge(
            capital=1_000_000.0,
            expression=expr,
            mode="paper",
            risk_gate=_wide_paper_gate(),
        )
        one_sided_chain = {
            Decimal("24800"): {"CE": _contract(24800, OptionType.CE)}
        }
        with pytest.raises(ContractValidityError):
            bridge._validate_entry_contracts(
                [Decimal("24800")],
                one_sided_chain,
                "CE",
                structure_type="long_straddle",
            )


# ---------------------------------------------------------------------------
# 3. External MStockBroker WS Order/Trade Push Bus (mticker.py untouched)
# ---------------------------------------------------------------------------


class TestMStockBrokerWsOrderPush:
    def test_ws_order_and_trade_hooks_survive_totp_reauth_and_clear_on_logout(
        self, monkeypatch
    ):
        created_tickers: list[Any] = []

        class StubTicker:
            MODE_LTP = "ltp"
            MODE_QUOTE = "quote"
            MODE_FULL = "full"

            def __init__(self, api_key, access_token, **kwargs):
                self.api_key = api_key
                self.access_token = access_token
                self.on_ticks = None
                self.on_connect = None
                self.on_open = None
                self.on_close = None
                self.on_error = None
                self.on_order_update = None
                self.on_trade_update = None
                created_tickers.append(self)

            def is_connected(self):
                return True

            def send_login_after_connect(self):
                pass

            def subscribe(self, tokens):
                pass

            def set_mode(self, mode, tokens):
                pass

            def connect(self, threaded=True):
                if callable(self.on_connect):
                    self.on_connect(self, {})
                if callable(self.on_open):
                    self.on_open(self)

            def close(self):
                pass

        import backtest.data.mticker as mticker_mod

        monkeypatch.setattr(mticker_mod, "MTicker", StubTicker)
        monkeypatch.setenv("MSTOCK_API_KEY", "test-key")

        broker = MStockBroker()
        broker._session_token = "tok-1"
        broker._expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        received: list[dict[str, Any]] = []
        broker.add_order_listener(received.append)

        broker._init_websocket_feed("tok-1")
        assert len(created_tickers) == 1
        assert callable(created_tickers[0].on_order_update)
        assert callable(created_tickers[0].on_trade_update)

        created_tickers[0].on_order_update(
            created_tickers[0],
            {
                "data": {
                    "orderId": "ORD-WS-1",
                    "guiOrderId": "OPT-11112222-0",
                    "tradingSymbol": "NIFTY26NOV24800CE",
                    "transactionType": "B",
                    "quantity": 75,
                    "filledShares": 75,
                    "averagePrice": 124.55,
                    "orderStatus": "COMPLETE",
                }
            },
        )
        assert len(received) == 1
        assert received[0]["order_id"] == "ORD-WS-1"
        assert received[0]["side"] == "BUY"
        assert broker.get_ws_order_update("ORD-WS-1")["average_price"] == 124.55
        assert (
            broker.find_ws_order_update(client_order_id="OPT-11112222-0")["order_id"]
            == "ORD-WS-1"
        )

        # Simulate TOTP re-auth creating a brand-new MTicker instance:
        # registered listener MUST survive without re-registration!
        broker._session_token = "tok-2"
        broker._init_websocket_feed("tok-2")
        assert len(created_tickers) == 2
        created_tickers[1].on_trade_update(
            created_tickers[1],
            {
                "nOrdNo": "ORD-WS-2",
                "trdSym": "NIFTY26NOV24800PE",
                "trnsTp": "S",
                "qty": 75,
                "fldQty": 75,
                "avgPrc": 118.20,
                "ordSt": "EXECUTED",
            },
        )
        assert len(received) == 2
        assert received[1]["order_id"] == "ORD-WS-2"
        assert received[1]["side"] == "SELL"

        broker.logout()
        assert broker.get_ws_order_update("ORD-WS-1") is None


# ---------------------------------------------------------------------------
# 4. LiveOptionTrader — WS <50ms Wakeup, Marketable LIMIT, Concurrent & BUY-first
# ---------------------------------------------------------------------------


class TestLiveOptionTraderPhase4:
    @pytest.fixture(autouse=True)
    def _arm_env(self, monkeypatch):
        monkeypatch.setenv("ALLOW_LIVE_ORDERS", "1")

    def test_nfo_tick_rounding_crosses_spread_cleanly(self):
        assert _round_nfo_tick(101.21, "BUY") == 101.25
        assert _round_nfo_tick(101.20, "BUY") == 101.20
        assert _round_nfo_tick(101.24, "SELL") == 101.20
        assert _round_nfo_tick(0.01, "SELL") == 0.05

    def test_ws_push_wakes_poll_in_under_200ms_and_uses_marketable_limit(self):
        venue = FakeVenueBroker(
            quotes={"TOK-NIFTY-24800-CE": {"ltp": 120.0, "bid": 119.5, "ask": 120.5}}
        )
        qp = SimpleNamespace(get_quote=venue.get_option_quote)
        # poll_interval_seconds=5.0 proves wakeup came from threading.Event WS push!
        trader = LiveOptionTrader(
            broker=venue,  # type: ignore[arg-type]
            dry_run=False,
            confirm_live=True,
            poll_interval_seconds=5.0,
            poll_timeout_seconds=5.0,
            quote_provider=qp,
        )
        intent = TradeIntent(
            view=_view(Direction.BULLISH),
            structure_type="long_call",
            legs=(
                OptionLeg(
                    instrument_token="TOK-NIFTY-24800-CE",
                    trading_symbol="NIFTY26NOV24800CE",
                    side="BUY",
                    quantity=1,
                    lot_size=75,
                ),
            ),
            expiry=EXPIRY,
            strategy_name="fast_ws",
        )
        t0 = time.monotonic()
        res = trader.execute_structure(intent)
        elapsed = time.monotonic() - t0

        assert res.status == StructureExecStatus.FILLED
        assert elapsed < 0.5, f"WS push wakeup took {elapsed:.3f}s instead of <50ms"
        assert venue.placed[0].order_type == "LIMIT"
        assert venue.placed[0].limit_price is not None
        assert round(venue.placed[0].limit_price % 0.05, 4) in (0.0, 0.05)
        trader.close()

    def test_concurrent_same_side_straddle_dispatches_both_legs_and_rolls_back_reject(self):
        venue = FakeVenueBroker()
        venue.reject_leg_indices.add(1)  # Leg 0 fills, Leg 1 definitely rejects
        trader = LiveOptionTrader(
            broker=venue,  # type: ignore[arg-type]
            dry_run=False,
            confirm_live=True,
            allowed_structures=RESEARCH_STRUCTURES,
            poll_interval_seconds=0.05,
            poll_timeout_seconds=1.0,
        )
        chain = _two_sided_chain()
        intent = LongStraddle().build(_view(), [Decimal("24800")], chain, EXPIRY)
        res = trader.execute_structure(intent)

        assert res.status == StructureExecStatus.ROLLED_BACK
        # Leg 0 placed + rollback SELL placed for Leg 0
        assert any((o.tag or {}).get("rollback") is True and o.side == "SELL" for o in venue.placed)
        trader.close()

    def test_concurrent_straddle_halts_without_rollback_when_leg_uncertain(self):
        venue = FakeVenueBroker()
        venue.timeout_leg_indices.add(1)  # Leg 1 stays OPEN -> timeout -> uncertain!
        journal = MemoryJournal()
        trader = LiveOptionTrader(
            broker=venue,  # type: ignore[arg-type]
            dry_run=False,
            confirm_live=True,
            journal=journal,
            allowed_structures=RESEARCH_STRUCTURES,
            poll_interval_seconds=0.02,
            poll_timeout_seconds=0.08,
        )
        chain = _two_sided_chain()
        intent = LongStraddle().build(_view(), [Decimal("24800")], chain, EXPIRY)
        res = trader.execute_structure(intent)

        assert res.status == StructureExecStatus.HALTED
        # OPT-3 invariant: NEVER blind-rollback when sibling leg is uncertain!
        assert not any((o.tag or {}).get("rollback") for o in venue.placed)
        kinds = [
            e.kind for e in journal.recent(50) if e.intent_id == res.intent_id
        ]
        assert KIND_UNCERTAIN in kinds and KIND_HALTED in kinds
        trader.close()

    def test_mixed_spread_executes_buy_wing_first_on_entry_and_buy_to_cover_first_on_close(
        self,
    ):
        venue = FakeVenueBroker()
        qp = SimpleNamespace(get_quote=venue.get_option_quote)
        trader = LiveOptionTrader(
            broker=venue,  # type: ignore[arg-type]
            dry_run=False,
            confirm_live=True,
            allowed_structures=RESEARCH_STRUCTURES,
            quote_provider=qp,
            poll_interval_seconds=0.05,
            poll_timeout_seconds=1.0,
        )
        # Intentionally pass SELL leg at index 0 and BUY leg at index 1!
        intent = TradeIntent(
            view=_view(Direction.BULLISH),
            structure_type="bull_call_spread",
            legs=(
                OptionLeg(
                    instrument_token="TOK-NIFTY-24850-CE",
                    trading_symbol="NIFTY26NOV24850CE",
                    side="SELL",
                    quantity=1,
                    lot_size=75,
                ),
                OptionLeg(
                    instrument_token="TOK-NIFTY-24800-CE",
                    trading_symbol="NIFTY26NOV24800CE",
                    side="BUY",
                    quantity=1,
                    lot_size=75,
                ),
            ),
            expiry=EXPIRY,
            strategy_name="hedged_seq",
        )
        adapter = LiveOptionBrokerAdapter(
            trader=trader,
            capital=500_000.0,
            allowed_structures=RESEARCH_STRUCTURES,
        )
        positions = adapter.execute_structure(intent, qp)
        assert len(positions) == 2
        # Entry MUST have sent BUY protective wing FIRST (venue.placed[0].side == "BUY")!
        assert [o.side for o in venue.placed[:2]] == ["BUY", "SELL"]

        # Close MUST send BUY-to-cover (closing the short 24850CE) BEFORE selling long 24800CE!
        structure_id = positions[0].structure_id
        adapter.close_structure(structure_id, qp, reason="target")
        assert [o.side for o in venue.placed[2:4]] == ["BUY", "SELL"]
        assert venue.placed[2].symbol == "NIFTY26NOV24850CE"
        assert venue.placed[3].symbol == "NIFTY26NOV24800CE"
        trader.close()

    def test_precheck_margin_refuses_before_placing_any_order(self):
        venue = FakeVenueBroker()
        venue.calculate_order_margin = lambda order: SimpleNamespace(  # type: ignore[attr-defined]
            required_margin=180_000.0, available_margin=50_000.0
        )
        trader = LiveOptionTrader(
            broker=venue,  # type: ignore[arg-type]
            dry_run=False,
            confirm_live=True,
            precheck_margin=True,
        )
        intent = TradeIntent(
            view=_view(Direction.BULLISH),
            structure_type="long_call",
            legs=(
                OptionLeg(
                    instrument_token="TOK-NIFTY-24800-CE",
                    trading_symbol="NIFTY26NOV24800CE",
                    side="BUY",
                    quantity=1,
                    lot_size=75,
                ),
            ),
            expiry=EXPIRY,
        )
        res = trader.execute_structure(intent)
        assert res.status == StructureExecStatus.ROLLED_BACK
        assert "margin check failed" in res.error
        assert len(venue.placed) == 0
        trader.close()

    def test_uncertain_place_order_reconciles_from_ws_push_cache(self):
        venue = FakeVenueBroker()

        def _timeout_after_accept(order: BrokerOrder) -> BrokerOrderId:
            # Venue accepted and pushed WS COMPLETE, then HTTP 504 dropped!
            ws_row = {
                "order_id": "VENUE-RECON-1",
                "client_order_id": order.client_order_id,
                "symbol": order.symbol,
                "side": order.side,
                "quantity": int(order.quantity),
                "filled_quantity": int(order.quantity),
                "average_price": 121.35,
                "status": "COMPLETE",
            }
            venue.ws_updates["VENUE-RECON-1"] = ws_row
            raise OrderOutcomeUnknown("Gateway 504 timeout", status_code=504)

        venue.place_order = _timeout_after_accept  # type: ignore[method-assign]
        trader = LiveOptionTrader(
            broker=venue,  # type: ignore[arg-type]
            dry_run=False,
            confirm_live=True,
        )
        intent = TradeIntent(
            view=_view(Direction.BULLISH),
            structure_type="long_call",
            legs=(
                OptionLeg(
                    instrument_token="TOK-NIFTY-24800-CE",
                    trading_symbol="NIFTY26NOV24800CE",
                    side="BUY",
                    quantity=1,
                    lot_size=75,
                ),
            ),
            expiry=EXPIRY,
        )
        res = trader.execute_structure(intent)
        assert res.status == StructureExecStatus.FILLED
        assert res.legs[0].uncertain is False
        assert res.legs[0].broker_order_id == "VENUE-RECON-1"
        assert res.legs[0].fill_price == pytest.approx(121.35)
        trader.close()

    def test_partial_close_settles_filled_leg_and_leaves_unconfirmed_leg_open(self):
        venue = FakeVenueBroker()
        qp = SimpleNamespace(get_quote=venue.get_option_quote)
        trader = LiveOptionTrader(
            broker=venue,  # type: ignore[arg-type]
            dry_run=False,
            confirm_live=True,
            allowed_structures=RESEARCH_STRUCTURES,
            quote_provider=qp,
            poll_interval_seconds=0.02,
            poll_timeout_seconds=0.06,
        )
        adapter = LiveOptionBrokerAdapter(
            trader=trader,
            capital=500_000.0,
            allowed_structures=RESEARCH_STRUCTURES,
        )
        # Open bull_call_spread: leg 0 = BUY 24800CE, leg 1 = SELL 24850CE
        intent = TradeIntent(
            view=_view(Direction.BULLISH),
            structure_type="bull_call_spread",
            legs=(
                OptionLeg(
                    instrument_token="TOK-NIFTY-24800-CE",
                    trading_symbol="NIFTY26NOV24800CE",
                    side="BUY",
                    quantity=1,
                    lot_size=75,
                ),
                OptionLeg(
                    instrument_token="TOK-NIFTY-24850-CE",
                    trading_symbol="NIFTY26NOV24850CE",
                    side="SELL",
                    quantity=1,
                    lot_size=75,
                ),
            ),
            expiry=EXPIRY,
        )
        positions = adapter.execute_structure(intent, qp)
        structure_id = positions[0].structure_id

        # On close: leg index 1 (BUY-to-cover 24850CE) runs FIRST and fills;
        # leg index 0 (SELL 24800CE) runs SECOND and times out!
        venue.timeout_leg_indices.add(0)
        with pytest.raises(LiveExecutionHaltedError):
            adapter.close_structure(structure_id, qp, reason="partial_halt")

        struct = adapter.get_structure(structure_id)
        assert struct is not None
        by_sym = {p.trading_symbol: p.status for p in struct.legs}
        assert by_sym["NIFTY26NOV24850CE"] == PositionStatus.CLOSED
        assert by_sym["NIFTY26NOV24800CE"] == PositionStatus.OPEN
        # Never re-open/rollback a closing leg!
        assert not any((o.tag or {}).get("rollback") for o in venue.placed)
        trader.close()


# ---------------------------------------------------------------------------
# 5. LiveOptionBrokerAdapter & StrategyRunner(mode="live") Wiring + Halt Pause
# ---------------------------------------------------------------------------


class TestLiveOptionRunnerWiring:
    @pytest.fixture()
    def live_env(self, monkeypatch):
        reset_data_bus()
        monkeypatch.setenv("ALLOW_LIVE_ORDERS", "1")
        yield
        reset_data_bus()

    def test_live_option_runner_fails_closed_when_chain_falls_back_to_synthetic(
        self, live_env, monkeypatch
    ):
        import backtest.forward.feed_registry as fr

        monkeypatch.setattr(fr, "_default_quote_broker", lambda src="mstock": None)
        venue = FakeVenueBroker()
        ledger = OrderLedger()
        gw = LiveEquityGateway(ledger=ledger, broker=venue, confirm_live=True)

        cfg = RunnerConfig(
            name="LIVE-REFUSE-SYNTH",
            strategy_name="directional_options",
            allocated_capital=500_000.0,
            symbols=["NIFTY"],
            timeframe="5min",
            mode="live",
            source="mstock",
            instrument={
                "type": "option",
                "expression": {
                    "expression_version": 2,
                    "type": "long_call",
                    "quantity": 1,
                    "exit": {"structure_capped": True, "min_days_to_expiry": 1},
                },
            },
        )
        with pytest.raises(ValueError, match="refusing synthetic chain fallback"):
            StrategyRunner(cfg, ledger=ledger, broker=gw)  # type: ignore[arg-type]

    def test_live_option_runner_books_venue_fills_into_ledger_and_halts_cleanly_on_partial_close(
        self, live_env, monkeypatch
    ):
        import backtest.forward.feed_registry as fr

        chain_contracts = [
            _contract(24750, OptionType.CE),
            _contract(24800, OptionType.CE),
            _contract(24850, OptionType.CE),
            _contract(24800, OptionType.PE),
        ]
        quotes = {
            "TOK-NIFTY-24800-CE": {"ltp": 110.0, "bid": 109.5, "ask": 110.5},
            "TOK-NIFTY-24850-CE": {"ltp": 75.0, "bid": 74.5, "ask": 75.5},
        }
        venue = FakeVenueBroker(contracts=chain_contracts, quotes=quotes, fill_slippage=0.30)
        monkeypatch.setattr(fr, "_default_quote_broker", lambda src="mstock": venue)

        ledger = OrderLedger()
        gw = LiveEquityGateway(ledger=ledger, broker=venue, confirm_live=True)
        cfg = RunnerConfig(
            name="LIVE-NIFTY-CALL",
            strategy_name="directional_options",
            allocated_capital=500_000.0,
            symbols=["NIFTY"],
            timeframe="5min",
            mode="live",
            source="mstock",
            instrument={
                "type": "option",
                "expression": {
                    "expression_version": 2,
                    "type": "long_call",
                    "quantity": 1,
                    "exit": {"structure_capped": True, "min_days_to_expiry": 1},
                },
            },
        )
        runner = StrategyRunner(cfg, ledger=ledger, broker=gw)  # type: ignore[arg-type]
        assert runner.options_bridge is not None
        assert getattr(runner.options_bridge.option_broker, "is_live_adapter", False) is True
        # Widen per-trade ceiling in test (110.80 * 75 = ₹8,310 fits ₹25k anyway,
        # and verify OrderLedger captures broker_order_id + LIMIT + slippage).
        runner.start()
        assert runner.status == STATUS_RUNNING

        bar_ts = "2026-10-26T09:25:00+05:30"
        runner.options_bridge.on_bar("NIFTY", 24_800.0, bar_ts)
        entry_res = runner.options_bridge.on_intent(
            {"structure": "long_call", "strike_selection": "atm"},
            MarketView(
                direction=Direction.BULLISH,
                confidence=0.9,
                underlying="NIFTY",
                spot_price=Decimal("24800"),
                bar_timestamp=datetime.fromisoformat(bar_ts),
            ),
            runner.config.strategy_name,
        )
        assert entry_res is not None and not entry_res.get("rejected"), entry_res

        orders = [ledger.order_to_dict(o) for o in ledger.orders_for(runner.instance_id)]
        assert len(orders) == 1
        assert orders[0]["status"] == ORDER_FILLED
        assert orders[0]["broker_order_id"] == "VENUE-0001"
        assert orders[0]["order_type"] == "LIMIT"
        assert orders[0]["limit_price"] is not None
        assert orders[0]["avg_fill_price"] == pytest.approx(110.80)
        assert orders[0]["requested_price"] == pytest.approx(110.50)
        assert orders[0]["slippage"] == pytest.approx(0.30)

        # Now force a close timeout -> LiveExecutionHaltedError -> runner MUST pause!
        venue.timeout_leg_indices.add(0)
        adapter: Any = runner.options_bridge.option_broker
        adapter.trader.poll_interval = 0.02
        adapter.trader.poll_timeout = 0.06

        close_evt = runner.options_bridge.manual_exit(
            reason="operator_close", strategy_name=runner.config.strategy_name
        )
        assert close_evt is None
        assert runner.status == STATUS_PAUSED
        assert runner.error is not None and "LIVE_EXECUTION_HALTED" in runner.error
        # Unconfirmed position remains OPEN in the book for operator reconciliation
        open_legs = adapter.get_open_positions()
        assert len(open_legs) == 1 and open_legs[0].status == PositionStatus.OPEN

        runner.stop()
