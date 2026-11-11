"""Item D — a protective exit may not fill at a price nobody quoted.

The 2026-11-08 Muhurat session lost ₹11,794 on a short straddle because the
close asked the provider for a price and booked whatever the packet carried:
the put was carried at a ₹367.00 mark and bought back at ₹493.65, a move the
venue's own tape says cannot happen — no legitimate same-contract ask move in
the 2026-11-09 dual capture exceeded ₹11.25 inside five seconds. Refusing a bad
*entry* quote does not help once the structure is open, so the exit seam now
bounds the fill against the last mark this book trusted for that leg and leaves
the structure alone when the number is impossible.

The collar width, ``max(₹25, 5 % × mark)``, is the measurement
``tools/out/measure_exit_collar_band.py`` took off that capture, so the tables
below are the tape: changing the collar means re-measuring it.

See ``docs/OPTION-JUNK-ASK-INCIDENT-2026-11-08.md`` §5 D.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from backtest.data.feed_depth_recorder import reset_recorder

from backtest.forward.options_bridge import OptionsBridge
from backtest.options.execution_gate import RESEARCH_STRUCTURES
from backtest.options.intent_journal import KIND_HALTED
from backtest.options.paper_trading import (
    EXIT_COLLAR_MAX_REL,
    EXIT_COLLAR_MIN_RS,
    EXIT_COLLAR_SANITY_ENV,
    OptionPaperBroker,
    OptionPosition,
    PositionStatus,
    ProtectedExitRefusedError,
)
from backtest.strategy.intent import Direction, MarketView, OptionLeg, TradeIntent

CE_TOKEN, PE_TOKEN = "64441", "64442"
CE_SYMBOL, PE_SYMBOL = "NIFTY26NOV24400CE", "NIFTY26NOV24400PE"
EXPIRY = date(2026, 11, 20)
#: 10:00 IST on a Monday — inside the session, so mark ages are real.
SESSION_1000 = datetime(2026, 11, 9, 4, 30, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# A venue book, not a model
# ---------------------------------------------------------------------------

class VenueBook:
    """Two-sided depth shaped like what ``BidAskQuoteProvider`` hands the seam.

    The collar binds only a fill that came off a venue's own book, so the
    fixture has to prove that: ``source_name`` carries the ``+bidask`` marker
    and ``execution_quote`` stamps ``execution_side`` the way the seam does.
    """

    source_name = "live:mstock+bidask"

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, float]] = {}
        self.exit_calls: list[tuple[str, str]] = []

    def set(self, token: str, bid: float, ask: float) -> None:
        self.rows[token] = {"bid": bid, "ask": ask, "ltp": round((bid + ask) / 2, 2)}

    def fail(self, token: str, message: str) -> None:
        self.rows[token] = {"error": message}

    def register_contract(self, contract) -> None:
        return None

    def get_quote(self, token: str) -> dict:
        return dict(self.rows.get(token) or {"error": "unknown token"})

    def _side_price(self, token: str, side: str) -> dict:
        row = dict(self.rows.get(token) or {})
        if not row or row.get("error"):
            return row
        row["ltp"] = row["ask"] if side.upper() == "BUY" else row["bid"]
        return row

    def execution_quote(self, token: str, side: str) -> dict:
        self.exit_calls.append((token, side))
        row = self._side_price(token, side)
        if row:
            row["execution_side"] = side
        return row

    def mark_quote(self, token: str, position_side: str) -> dict:
        """Conservative mark, same rule as the seam: bid if long, ask if short."""
        row = dict(self.rows.get(token) or {})
        if not row or row.get("error"):
            return row
        key = "bid" if str(position_side).upper() == "BUY" else "ask"
        row["ltp"] = row[key]
        row["mark_basis"] = key
        return row


class ModelBook:
    """A Black-Scholes style book: one number, no spread, no venue marker."""

    source_name = "synthetic"

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, float]] = {}

    def set(self, token: str, ltp: float) -> None:
        self.rows[token] = {"ltp": ltp}

    def get_quote(self, token: str) -> dict:
        return dict(self.rows.get(token) or {})

    def register_contract(self, contract) -> None:
        return None


def _view(direction: Direction) -> MarketView:
    return MarketView(
        direction=direction,
        confidence=0.9,
        underlying="NIFTY",
        spot_price=Decimal("24800"),
    )


def _straddle_intent() -> TradeIntent:
    """The instrument that broke: a short straddle, sold at 337.10 / 320.85."""
    return TradeIntent(
        view=_view(Direction.NEUTRAL),
        structure_type="short_straddle",
        legs=(
            OptionLeg(
                instrument_token=CE_TOKEN,
                trading_symbol=CE_SYMBOL,
                side="SELL",
                quantity=1,
                lot_size=25,
            ),
            OptionLeg(
                instrument_token=PE_TOKEN,
                trading_symbol=PE_SYMBOL,
                side="SELL",
                quantity=1,
                lot_size=25,
            ),
        ),
        expiry=EXPIRY,
        strategy_name="junk_ask_probe",
        metadata={"option_type": "CE"},
    )


def _long_put_intent() -> TradeIntent:
    return TradeIntent(
        view=_view(Direction.BEARISH),
        structure_type="long_put",
        legs=(
            OptionLeg(
                instrument_token=PE_TOKEN,
                trading_symbol=PE_SYMBOL,
                side="BUY",
                quantity=1,
                lot_size=25,
            ),
        ),
        expiry=EXPIRY,
        strategy_name="junk_ask_probe",
        metadata={"option_type": "PE", "strike": "24400"},
    )


def _broker() -> tuple[OptionPaperBroker, list[dict]]:
    fills: list[dict] = []
    broker = OptionPaperBroker(
        capital=1_000_000.0,
        allowed_structures=RESEARCH_STRUCTURES,
        on_leg_fill=fills.append,
    )
    return broker, fills


def _exit_fills(fills: list[dict]) -> list[dict]:
    return [record for record in fills if record.get("phase") == "exit"]


# ---------------------------------------------------------------------------
# The collar arithmetic — the measured table
# ---------------------------------------------------------------------------

def _leg(mark: float, marked_at: datetime = SESSION_1000) -> OptionPosition:
    return OptionPosition(
        instrument_token=PE_TOKEN,
        trading_symbol=PE_SYMBOL,
        side="SELL",
        quantity=1,
        lot_size=25,
        entry_price=Decimal(str(mark)),
        current_price=Decimal(str(mark)),
        opened_at=marked_at - timedelta(minutes=5),
        last_updated=marked_at,
    )


@pytest.mark.parametrize(
    "mark, fill",
    [
        (468.05, 479.30),  # ₹11.25 — the largest legitimate 5 s move on mStock
        (296.05, 305.40),  # ₹9.35
        (424.10, 415.90),  # ₹8.20 the other way
        (487.65, 495.10),  # ₹7.45
        (367.00, 371.00),  # an ordinary ₹4
        (439.00, 442.50),  # ₹3.50 — Upstox's whole 5 s tail
    ],
)
def test_every_move_the_tape_called_legitimate_still_fills(mark, fill):
    problem = OptionPaperBroker._collar_problem(
        _leg(mark), Decimal(str(fill)), SESSION_1000
    )
    assert problem is None, problem


@pytest.mark.parametrize(
    "mark, fill",
    [
        (367.00, 493.65),     # 2026-11-08: the exit that cost ₹11,794
        (408.55, 302.00),     # the phantom offer, ₹106.55
        (332.75, 1642.42),    # ₹1,309.67 — a level no counterparty printed
        (5.65, 127.95),       # 2026-11-05: the fake take-profit, favourable side
    ],
)
def test_no_measured_junk_price_becomes_a_fill(mark, fill):
    problem = OptionPaperBroker._collar_problem(
        _leg(mark), Decimal(str(fill)), SESSION_1000
    )
    assert problem is not None, f"{mark} -> {fill} was allowed through"


def test_the_band_is_the_rupee_floor_or_five_percent_whichever_is_wider():
    assert EXIT_COLLAR_MIN_RS == Decimal("25")
    assert EXIT_COLLAR_MAX_REL == Decimal("0.05")
    # A ₹500 mark: 5 % = ₹25, so the two terms meet exactly there.
    assert OptionPaperBroker._collar_problem(
        _leg(500.0), Decimal("524.95"), SESSION_1000
    ) is None
    assert OptionPaperBroker._collar_problem(
        _leg(500.0), Decimal("525.05"), SESSION_1000
    ) is not None
    # A ₹40 contract is bounded by the ₹25 floor, not by 5 %. Deliberately
    # permissive: the capture only covers ₹250-500 premiums, so the
    # low-premium tail was never measured and gets no invented bound.
    assert OptionPaperBroker._collar_problem(
        _leg(40.0), Decimal("64.00"), SESSION_1000
    ) is None
    assert OptionPaperBroker._collar_problem(
        _leg(40.0), Decimal("66.00"), SESSION_1000
    ) is not None


def test_a_reference_older_than_the_measured_window_bounds_nothing():
    """This is a five-second rule; an overnight gap is not a bad packet."""
    stale = _leg(367.00, marked_at=SESSION_1000 - timedelta(seconds=30))
    assert OptionPaperBroker._collar_problem(
        stale, Decimal("493.65"), SESSION_1000
    ) is None
    fresh = _leg(367.00, marked_at=SESSION_1000 - timedelta(seconds=5))
    assert OptionPaperBroker._collar_problem(
        fresh, Decimal("493.65"), SESSION_1000
    ) is not None


def test_a_leg_that_was_never_marked_bounds_nothing():
    unmarked = _leg(367.00)
    unmarked.current_price = Decimal("0")
    assert OptionPaperBroker._collar_problem(
        unmarked, Decimal("493.65"), SESSION_1000
    ) is None


# ---------------------------------------------------------------------------
# close_structure — a refusal must leave no trace in the book
# ---------------------------------------------------------------------------

def test_the_muhurat_exit_is_refused_and_the_book_is_untouched(monkeypatch):
    monkeypatch.setenv(EXIT_COLLAR_SANITY_ENV, "enforce")
    broker, fills = _broker()
    quotes = VenueBook()
    quotes.set(CE_TOKEN, bid=336.90, ask=337.10)
    quotes.set(PE_TOKEN, bid=320.85, ask=321.05)
    positions = broker.execute_structure(_straddle_intent(), quotes)
    structure_id = positions[0].structure_id

    # A bar of healthy marks, then the packet that ended the session. A short
    # leg is marked on the ask — the same level the cover buys at.
    quotes.set(PE_TOKEN, bid=366.75, ask=367.00)
    broker.update_mtm(quotes)
    cash_before = broker.available_cash
    fees_before = broker.get_structure(structure_id).statutory_fees
    assert positions[1].current_price == Decimal("367.00")

    quotes.set(PE_TOKEN, bid=493.00, ask=493.65)
    with pytest.raises(ProtectedExitRefusedError) as exc:
        broker.close_structure(structure_id, quotes, reason="stop_loss")

    assert exc.value.code == "outside_collar"
    assert exc.value.structure_id == structure_id
    assert "₹493.65" in str(exc.value) and "₹367.00" in str(exc.value)
    structure = broker.get_structure(structure_id)
    assert structure.closed_at is None
    assert structure.exit_reason is None
    assert all(p.status == PositionStatus.OPEN for p in positions)
    assert broker.available_cash == cash_before
    assert structure.statutory_fees == fees_before
    assert _exit_fills(fills) == []
    assert broker.exit_guard_stats["refused"] == 1


def test_a_legitimate_move_closes_normally_after_the_guard_landed(monkeypatch):
    monkeypatch.setenv(EXIT_COLLAR_SANITY_ENV, "enforce")
    broker, fills = _broker()
    quotes = VenueBook()
    quotes.set(CE_TOKEN, bid=336.90, ask=337.10)
    quotes.set(PE_TOKEN, bid=320.85, ask=321.05)
    positions = broker.execute_structure(_straddle_intent(), quotes)

    quotes.set(CE_TOKEN, bid=300.00, ask=302.00)
    quotes.set(PE_TOKEN, bid=300.00, ask=302.00)
    broker.update_mtm(quotes)
    realized = broker.close_structure(
        positions[0].structure_id, quotes, reason="take_profit"
    )

    assert realized > 0
    assert all(p.status == PositionStatus.CLOSED for p in positions)
    assert len(_exit_fills(fills)) == 2
    assert broker.exit_guard_stats["refused"] == 0


def test_a_favourable_impossible_price_is_refused_too(monkeypatch):
    """2026-11-05 sold a ₹5.65 leg back at ₹127.95 and booked the profit."""
    monkeypatch.setenv(EXIT_COLLAR_SANITY_ENV, "enforce")
    broker, fills = _broker()
    quotes = VenueBook()
    quotes.set(PE_TOKEN, bid=5.60, ask=5.65)
    positions = broker.execute_structure(_long_put_intent(), quotes)
    quotes.set(PE_TOKEN, bid=5.60, ask=6.00)
    broker.update_mtm(quotes)

    quotes.set(PE_TOKEN, bid=127.95, ask=128.00)
    with pytest.raises(ProtectedExitRefusedError) as exc:
        broker.close_structure(positions[0].structure_id, quotes, reason="take_profit")

    assert exc.value.code == "outside_collar"
    assert _exit_fills(fills) == []


def test_leg_two_is_priced_before_leg_one_moves(monkeypatch):
    """Price-all-then-close-all: a strangle cannot half-exit on one bad leg."""
    monkeypatch.setenv(EXIT_COLLAR_SANITY_ENV, "enforce")
    broker, fills = _broker()
    quotes = VenueBook()
    quotes.set(CE_TOKEN, bid=336.90, ask=337.10)
    quotes.set(PE_TOKEN, bid=320.85, ask=321.05)
    positions = broker.execute_structure(_straddle_intent(), quotes)
    structure_id = positions[0].structure_id
    cash_before = broker.available_cash
    quotes.exit_calls.clear()

    quotes.set(CE_TOKEN, bid=316.90, ask=318.90)   # a real ₹18 move: inside the band
    quotes.set(PE_TOKEN, bid=493.00, ask=493.65)   # junk, and it is leg two
    with pytest.raises(ProtectedExitRefusedError):
        broker.close_structure(structure_id, quotes, reason="stop_loss")

    assert all(p.status == PositionStatus.OPEN for p in positions)
    assert broker.available_cash == cash_before
    assert _exit_fills(fills) == []
    assert ("64441", "BUY") in quotes.exit_calls, "both legs were priced first"


def test_a_provider_that_answers_with_nothing_refuses_instead_of_inventing(monkeypatch):
    """This fallback used to book the last mark as a fill — a price nobody quoted."""
    monkeypatch.setenv(EXIT_COLLAR_SANITY_ENV, "enforce")
    broker, fills = _broker()
    quotes = VenueBook()
    quotes.set(PE_TOKEN, bid=320.85, ask=321.05)
    positions = broker.execute_structure(_long_put_intent(), quotes)
    cash_before = broker.available_cash

    quotes.fail(PE_TOKEN, "broker session expired")
    with pytest.raises(ProtectedExitRefusedError) as exc:
        broker.close_structure(positions[0].structure_id, quotes, reason="stop_loss")

    assert exc.value.code == "no_quote"
    assert positions[0].status == PositionStatus.OPEN
    assert broker.available_cash == cash_before
    assert _exit_fills(fills) == []
    assert broker.exit_guard_stats["no_quote"] == 1


def test_cash_settlement_at_expiry_keeps_its_own_path(monkeypatch):
    """An expired contract has no book left to bound; refusing would strand it."""
    monkeypatch.setenv(EXIT_COLLAR_SANITY_ENV, "enforce")
    broker, _ = _broker()
    quotes = VenueBook()
    quotes.set(PE_TOKEN, bid=320.85, ask=321.05)
    positions = broker.execute_structure(_long_put_intent(), quotes)
    positions[0].current_price = Decimal("300.00")

    quotes.fail(PE_TOKEN, "contract expired")
    broker.close_structure(
        positions[0].structure_id, quotes, reason="expiry_settlement"
    )

    assert positions[0].status == PositionStatus.CLOSED
    assert broker.exit_guard_stats == {"refused": 0, "shadow": 0, "no_quote": 0}


def test_a_model_price_is_not_bounded_because_no_venue_quoted_it(monkeypatch):
    """Replay and synthetic books re-price by construction; the guard is for feeds."""
    monkeypatch.setenv(EXIT_COLLAR_SANITY_ENV, "enforce")
    broker, _ = _broker()
    quotes = ModelBook()
    quotes.set(PE_TOKEN, ltp=100.0)
    positions = broker.execute_structure(_long_put_intent(), quotes)
    quotes.set(PE_TOKEN, ltp=130.0)

    broker.close_structure(positions[0].structure_id, quotes)

    assert positions[0].status == PositionStatus.CLOSED
    assert broker.exit_guard_stats == {"refused": 0, "shadow": 0, "no_quote": 0}


def test_shadow_mode_flags_the_fill_and_takes_it(monkeypatch, caplog):
    monkeypatch.setenv(EXIT_COLLAR_SANITY_ENV, "shadow")
    broker, fills = _broker()
    quotes = VenueBook()
    quotes.set(PE_TOKEN, bid=320.85, ask=321.05)
    positions = broker.execute_structure(_long_put_intent(), quotes)
    quotes.set(PE_TOKEN, bid=367.00, ask=369.00)
    broker.update_mtm(quotes)
    quotes.set(PE_TOKEN, bid=493.65, ask=495.00)

    with caplog.at_level(logging.WARNING, logger="backtest.options.paper_trading"):
        broker.close_structure(positions[0].structure_id, quotes, reason="stop_loss")

    assert positions[0].status == PositionStatus.CLOSED
    assert len(_exit_fills(fills)) == 1
    assert "EXIT_COLLAR_SHADOW" in caplog.text
    assert "outside" not in caplog.text  # not the refusal line
    assert broker.exit_guard_stats["shadow"] == 1
    assert broker.exit_guard_stats["refused"] == 0


def test_off_mode_never_consults_the_collar(monkeypatch):
    monkeypatch.setenv(EXIT_COLLAR_SANITY_ENV, "off")
    broker, _ = _broker()
    quotes = VenueBook()
    quotes.set(PE_TOKEN, bid=320.85, ask=321.05)
    positions = broker.execute_structure(_long_put_intent(), quotes)
    quotes.set(PE_TOKEN, bid=367.00, ask=369.00)
    broker.update_mtm(quotes)
    quotes.set(PE_TOKEN, bid=493.65, ask=495.00)

    broker.close_structure(positions[0].structure_id, quotes, reason="stop_loss")

    assert positions[0].status == PositionStatus.CLOSED
    assert broker.exit_guard_stats == {"refused": 0, "shadow": 0, "no_quote": 0}


# ---------------------------------------------------------------------------
# The bridge: refuse loudly, retry next bar, spend nothing
# ---------------------------------------------------------------------------

class _Journal:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def append(self, **event) -> None:
        self.rows.append(event)


class _Alerts:
    def __init__(self) -> None:
        self.raised: list[tuple] = []

    def raise_alert(self, alert_type, severity, message, subject="", data=None):
        self.raised.append(
            (alert_type, severity, message, subject, data or {})
        )


class _RefusingBroker:
    """Stands in for the paper broker: the exit guard said no, nothing moved."""

    is_live_adapter = False
    total_unrealized_pnl = Decimal("0")

    def __init__(self, code: str = "outside_collar") -> None:
        self.code = code
        self.calls = 0

    def get_structure(self, structure_id: str):
        return SimpleNamespace(
            structure_id=structure_id,
            structure_type="short_straddle",
            underlying="NIFTY",
            legs=(),
        )

    def close_structure(self, *args, **kwargs):
        self.calls += 1
        raise ProtectedExitRefusedError(
            "NIFTY26NOV24400PE: exit fill ₹493.65 is ₹126.65 from the last "
            "trusted mark ₹367.00 (collar ₹25.00, mark age 0.0s)",
            structure_id="struct_refused",
            instrument_token=PE_TOKEN,
            code=self.code,
        )


def _bridge(code: str = "outside_collar", **overrides) -> OptionsBridge:
    kwargs: dict = dict(
        capital=1_000_000.0,
        expression={
            "expression_version": 2,
            "type": "long_call",
            "quantity": 1,
            "exit": {"structure_capped": True},
        },
        allowed_structures=RESEARCH_STRUCTURES,
    )
    kwargs.update(overrides)
    bridge = OptionsBridge(**kwargs)
    bridge.open_structure_id = "struct_refused"
    bridge.option_broker = _RefusingBroker(code)
    return bridge


def _decision():
    return SimpleNamespace(reason="stop_loss", detail="stop -11,794 ≤ -2,000")


def test_a_refused_exit_journals_and_alerts_once_not_once_per_bar(monkeypatch):
    alerts = _Alerts()
    import backtest.alerts.broker as alerts_module

    monkeypatch.setattr(alerts_module, "get_alert_broker", lambda: alerts)
    journal = _Journal()
    bridge = _bridge(intent_journal=journal)

    first = bridge._exit_structure(_decision(), "2026-11-09T09:45:00", "junk_ask_probe")
    second = bridge._exit_structure(_decision(), "2026-11-09T09:46:00", "junk_ask_probe")

    assert first is None and second is None
    assert bridge.option_broker.calls == 2, "the exit is retried every bar"
    halted = [row for row in journal.rows if row["kind"] == KIND_HALTED]
    assert len(halted) == 1, "a standing refusal must not spam the journal"
    assert halted[0]["detail"]["code"] == "exit_outside_collar_refused"
    assert len(alerts.raised) == 1
    assert alerts.raised[0][1] == "critical"
    assert "₹493.65" in alerts.raised[0][2]


def test_a_refused_exit_spends_nothing_and_keeps_the_structure(monkeypatch):
    import backtest.alerts.broker as alerts_module

    monkeypatch.setattr(alerts_module, "get_alert_broker", lambda: _Alerts())
    bridge = _bridge(intent_journal=_Journal())

    bridge._exit_structure(_decision(), "2026-11-09T09:45:00", "junk_ask_probe")

    assert bridge.open_structure_id == "struct_refused"
    assert bridge.closed_count == 0
    assert bridge.last_exit is None
    assert list(bridge._exit_events) == []


def test_a_refused_paper_close_does_not_pause_the_runner(monkeypatch):
    """No order reached the venue, so there is nothing to reconcile — unlike a
    live close whose outcome is uncertain, which does halt."""
    import backtest.alerts.broker as alerts_module

    monkeypatch.setattr(alerts_module, "get_alert_broker", lambda: _Alerts())
    halts: list[str] = []
    bridge = _bridge(on_halt=halts.append, intent_journal=_Journal())

    bridge._exit_structure(_decision(), "2026-11-09T09:45:00", "junk_ask_probe")

    assert halts == []
    assert bridge.open_structure_id == "struct_refused"


def test_a_no_quote_refusal_gets_its_own_code(monkeypatch):
    import backtest.alerts.broker as alerts_module

    monkeypatch.setattr(alerts_module, "get_alert_broker", lambda: _Alerts())
    journal = _Journal()
    bridge = _bridge(intent_journal=journal)
    bridge.option_broker = _RefusingBroker(code="no_quote")

    bridge._exit_structure(_decision(), "2026-11-09T09:45:00", "junk_ask_probe")

    halted = [row for row in journal.rows if row["kind"] == KIND_HALTED]
    assert halted[0]["detail"]["code"] == "exit_no_quote_refused"


# ---------------------------------------------------------------------------
# Phase 3 item 4 — the peer-mark journal (the column Phase 4's S4/S5/S7 read)
# ---------------------------------------------------------------------------

def _peer_mark_rows(tmp_path: Path) -> list[dict]:
    reset_recorder()  # close the recorder first — joins its writer thread
    rows: list[dict] = []
    for path in sorted(Path(tmp_path).glob("feed_*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("kind") == "peer_mark":
                rows.append(row)
    return rows


def _with_feed_capture(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ENABLE_FEED", "true")
    monkeypatch.setenv("ENABLE_FEED_DIR", str(tmp_path))
    reset_recorder()


def test_shadow_evaluations_journal_the_peer_mark_next_to_our_mark(monkeypatch, tmp_path):
    """Every shadow collar evaluation lands our mark beside the peer's L1 — the
    trip that costs money and the fine leg both, so S5's coverage denominator
    is the evaluations themselves (§7 Q2)."""
    monkeypatch.setenv(EXIT_COLLAR_SANITY_ENV, "shadow")
    _with_feed_capture(monkeypatch, tmp_path)
    try:
        broker, fills = _broker()
        quotes = VenueBook()
        quotes.set(CE_TOKEN, bid=336.90, ask=337.10)
        quotes.set(PE_TOKEN, bid=320.85, ask=321.05)
        positions = broker.execute_structure(_straddle_intent(), quotes)
        structure_id = positions[0].structure_id
        quotes.set(PE_TOKEN, bid=366.75, ask=367.00)
        broker.update_mtm(quotes)
        quotes.peer_l1 = lambda token: {
            "broker": "upstox", "ltp": 367.5, "bid": 367.3,
            "ask": 367.8, "age_s": 0.4,
        }
        quotes.set(PE_TOKEN, bid=493.00, ask=493.65)  # the trip
        broker.close_structure(structure_id, quotes, reason="stop_loss")

        rows = _peer_mark_rows(tmp_path)
        assert len(rows) == 2, rows  # one per closed leg — 100% of evaluations
        decisions = {row["decision"] for row in rows}
        assert decisions == {"ok", "trip"}
        trip = next(row for row in rows if row["decision"] == "trip")
        assert "exit fill" in trip["problem"] and "493.65" in trip["problem"]
        assert trip["mark"] == 367.00
        assert float(trip["fill_price"]) == 493.65
        for row in rows:
            assert row["mode"] == "shadow"
            assert row["side"] == "BUY"  # a short leg covers by buying
            assert row["peer"] and row["peer"]["broker"] == "upstox"
            assert row["peer"]["bid"] == 367.3  # the peer's own level
            assert "mark_age_s" in row
    finally:
        reset_recorder()


def test_enforce_mode_journals_nothing(monkeypatch, tmp_path):
    """The journal exists to make the SHADOW decision attributable before the
    flip to enforce — an enforce refusal raises, and writes no peer marks."""
    monkeypatch.setenv(EXIT_COLLAR_SANITY_ENV, "enforce")
    _with_feed_capture(monkeypatch, tmp_path)
    try:
        broker, _ = _broker()
        quotes = VenueBook()
        quotes.set(CE_TOKEN, bid=336.90, ask=337.10)
        quotes.set(PE_TOKEN, bid=320.85, ask=321.05)
        positions = broker.execute_structure(_straddle_intent(), quotes)
        structure_id = positions[0].structure_id
        quotes.set(PE_TOKEN, bid=366.75, ask=367.00)
        broker.update_mtm(quotes)
        quotes.set(PE_TOKEN, bid=493.00, ask=493.65)
        with pytest.raises(ProtectedExitRefusedError):
            broker.close_structure(structure_id, quotes, reason="stop_loss")
        assert _peer_mark_rows(tmp_path) == []
    finally:
        reset_recorder()


def test_the_peer_column_is_null_when_no_witness_runs(monkeypatch, tmp_path):
    """Absence of a witness is not evidence: the row still journals our mark
    with ``peer: null``, so S4's split never mistakes silence for agreement."""
    _with_feed_capture(monkeypatch, tmp_path)
    try:
        OptionPaperBroker._journal_peer_mark(
            _leg(367.00), "buy", Decimal("368.00"), SESSION_1000, None,
            SimpleNamespace(),  # no peer_l1 at all
        )
        rows = _peer_mark_rows(tmp_path)
        assert len(rows) == 1
        assert rows[0]["decision"] == "ok"
        assert rows[0]["mark"] == 367.00
        assert rows[0]["peer"] is None
    finally:
        reset_recorder()
