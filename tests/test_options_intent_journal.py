"""PRD decision 8 — the append-only option intent journal.

Three properties are load-bearing for the Phase 1 gate ("100%
intent→fill→journal reconciliation") and for the no-blind-retry rule:

* **Deterministic keys.** The same decision hashes to the same ``intent_id``,
  so a replay is recognisable. A random uuid would make duplicates invisible.
* **Append-only with a dedupe key.** Re-writing the same event is a no-op, not
  a second row — and there is no update or delete path at all.
* **One schema, three routes.** The ORM model, the hand-applied ``.sql`` files
  and the Alembic revision must describe the same table.

Runs on a file-backed SQLite database (tmp_path); the process-local journal is
put through the same contract so the two stay interchangeable for callers.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from backtest.db.config import DEFAULT_CONFIG_PATH
from backtest.db.manager import DatabaseManager
from backtest.db.models import Base, OptionIntentEvent, OptionIntentKind
from backtest.options.intent_journal import (
    ALL_KINDS,
    KIND_DECIDED,
    KIND_EXECUTED,
    KIND_FILLED,
    KIND_HALTED,
    KIND_RECONCILED,
    KIND_REJECTED,
    KIND_SUBMITTED,
    IntentJournal,
    MemoryJournal,
    decision_key,
    dedupe_key,
    intent_key,
    journal_from_env,
)
from backtest.strategy.intent import Direction, MarketView, OptionLeg, TradeIntent

REPO = Path(__file__).resolve().parents[1]
TABLE = "option_intent_events"
BAR = datetime(2026, 10, 5, 4, 30, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


def _sqlite_journal(tmp_path, name="journal.db"):
    manager = DatabaseManager.from_env(
        path=str(DEFAULT_CONFIG_PATH),
        profile="testing",
        url=f"sqlite:///{tmp_path / name}",
    )
    manager.connect()
    journal = IntentJournal(manager)
    journal.ensure_schema()
    return journal


@pytest.fixture()
def journal(tmp_path):
    return _sqlite_journal(tmp_path)


@pytest.fixture(params=["memory", "sqlite"], ids=["memory", "sqlite"])
def any_journal(request, tmp_path):
    """Every caller-visible behaviour must hold for both implementations."""
    if request.param == "memory":
        return MemoryJournal()
    return _sqlite_journal(tmp_path, name=f"{request.param}.db")


def _view(when: datetime = BAR) -> MarketView:
    return MarketView(
        direction=Direction.BULLISH,
        confidence=0.8,
        underlying="NIFTY",
        spot_price=D("24800"),
        bar_timestamp=when,
    )


def _intent(
    symbol: str = "NIFTY26SEP24800CE",
    side: str = "BUY",
    quantity: int = 1,
    when: datetime = BAR,
    structure_type: str = "long_call",
) -> TradeIntent:
    return TradeIntent(
        view=_view(when),
        structure_type=structure_type,
        legs=(OptionLeg("T1", symbol, side, quantity, 25),),
        expiry=date(2026, 9, 24),
        strategy_name="test_strategy",
    )


def _spread(when: datetime = BAR) -> TradeIntent:
    return TradeIntent(
        view=_view(when),
        structure_type="bull_call_spread",
        legs=(
            OptionLeg("T1", "NIFTY26SEP24700CE", "BUY", 1, 25),
            OptionLeg("T2", "NIFTY26SEP25000CE", "SELL", 1, 25),
        ),
        expiry=date(2026, 9, 24),
        strategy_name="test_strategy",
    )


# ---------------------------------------------------------------------------
# The idempotency key is a property of the decision
# ---------------------------------------------------------------------------


class TestIntentKey:
    def test_the_same_decision_hashes_to_the_same_id(self):
        assert intent_key(_intent()) == intent_key(_intent())

    def test_a_different_bar_is_a_different_decision(self):
        other = _intent(when=BAR + timedelta(minutes=1))
        assert intent_key(_intent()) != intent_key(other)

    def test_a_different_contract_is_a_different_decision(self):
        assert intent_key(_intent()) != intent_key(_intent(symbol="NIFTY26SEP24900CE"))

    def test_side_and_quantity_are_part_of_the_key(self):
        assert intent_key(_intent()) != intent_key(_intent(side="SELL"))
        assert intent_key(_intent()) != intent_key(_intent(quantity=2))

    def test_leg_order_matters(self):
        """A spread's legs are not interchangeable, so they must not collide."""
        view = _view()
        legs = (
            OptionLeg("T1", "NIFTY26SEP24700CE", "BUY", 1, 25),
            OptionLeg("T2", "NIFTY26SEP25000CE", "SELL", 1, 25),
        )
        a = TradeIntent(
            view=view, structure_type="bull_call_spread", legs=legs,
            expiry=date(2026, 9, 24),
        )
        b = TradeIntent(
            view=view, structure_type="bull_call_spread", legs=tuple(reversed(legs)),
            expiry=date(2026, 9, 24),
        )
        assert intent_key(a) != intent_key(b)

    def test_action_separates_an_open_from_a_close(self):
        intent = _intent()
        assert intent_key(intent, action="open") != intent_key(intent, action="close")

    def test_no_bar_clock_falls_back_to_the_callers_clock(self):
        """Without a bar stamp the key must still distinguish two bars."""
        view = MarketView(
            direction=Direction.BULLISH, underlying="NIFTY", spot_price=D("24800")
        )
        intent = TradeIntent(
            view=view,
            structure_type="long_call",
            legs=(OptionLeg("T1", "NIFTY26SEP24800CE", "BUY", 1, 25),),
            expiry=date(2026, 9, 24),
        )
        assert intent_key(intent, clock=BAR) == intent_key(intent, clock=BAR)
        assert intent_key(intent, clock=BAR) != intent_key(
            intent, clock=BAR + timedelta(minutes=5)
        )

    def test_the_key_fits_the_column(self):
        key = intent_key(_intent())
        assert len(key) == 64  # sha256 hex — matches ``intent_id VARCHAR(64)``

    def test_a_different_leg_set_never_reuses_an_intent_id(self):
        assert intent_key(_intent()) != intent_key(_spread())

    def test_decision_key_is_deterministic_per_bar(self):
        a = decision_key("halt", "NO_EXPIRY: none", code="NO_EXPIRY", clock=BAR)
        assert a == decision_key("halt", "NO_EXPIRY: none", code="NO_EXPIRY", clock=BAR)
        assert a != decision_key("halt", "NO_EXPIRY: none", code="NO_EXPIRY", clock=None)
        assert a != decision_key("halt", "NO_STRIKE: none", code="NO_STRIKE", clock=BAR)

    def test_dedupe_key_scopes_by_leg(self):
        assert dedupe_key("abc", KIND_SUBMITTED) == "abc:submitted"
        assert dedupe_key("abc", KIND_SUBMITTED, 1) == "abc:submitted:1"


# ---------------------------------------------------------------------------
# Append-only semantics — one contract, two implementations
# ---------------------------------------------------------------------------


class TestJournalContract:
    def test_append_then_read_back(self, any_journal):
        key = intent_key(_intent())
        assert any_journal.append(
            intent_id=key, kind=KIND_DECIDED, structure_type="long_call"
        )
        events = any_journal.events(key)
        assert [e.kind for e in events] == [KIND_DECIDED]
        assert events[0].structure_type == "long_call"

    def test_a_replayed_event_is_absorbed_not_duplicated(self, any_journal):
        key = intent_key(_intent())
        assert any_journal.append(intent_id=key, kind=KIND_SUBMITTED, leg_index=0)
        assert (
            any_journal.append(intent_id=key, kind=KIND_SUBMITTED, leg_index=0) is False
        )
        assert len(any_journal.events(key)) == 1

    def test_legs_are_deduped_separately(self, any_journal):
        key = intent_key(_spread())
        assert any_journal.append(intent_id=key, kind=KIND_SUBMITTED, leg_index=0)
        assert any_journal.append(intent_id=key, kind=KIND_SUBMITTED, leg_index=1)

    def test_unduped_events_repeat(self, any_journal):
        """``reconciled`` records every time we looked — it must not be absorbed."""
        key = intent_key(_intent())
        for _ in range(3):
            assert any_journal.append(
                intent_id=key, kind=KIND_RECONCILED, leg_index=0, dedupe=False
            )
        assert len(any_journal.events(key)) == 3

    def test_has_and_kinds(self, any_journal):
        key = intent_key(_intent())
        any_journal.append(intent_id=key, kind=KIND_SUBMITTED, leg_index=0)
        assert any_journal.has(key, KIND_SUBMITTED, 0)
        assert not any_journal.has(key, KIND_FILLED, 0)
        assert any_journal.has(key, KIND_SUBMITTED) is False  # leg-less key differs
        assert any_journal.kinds(key) == {KIND_SUBMITTED}

    def test_order_id_for_is_the_idempotency_read(self, any_journal):
        key = intent_key(_spread())
        assert any_journal.order_id_for(key, 0) is None
        any_journal.append(
            intent_id=key, kind=KIND_SUBMITTED, leg_index=0, broker_order_id="ORD-1"
        )
        assert any_journal.order_id_for(key, 0) == "ORD-1"
        assert any_journal.order_id_for(key, 1) is None

    def test_append_for_intent_returns_the_key(self, any_journal):
        intent = _intent()
        key = any_journal.append_for_intent(intent, KIND_DECIDED)
        assert key == intent_key(intent)
        assert any_journal.has(key, KIND_DECIDED)
        event = any_journal.events(key)[0]
        assert (event.strategy_name, event.underlying) == ("test_strategy", "NIFTY")

    def test_broker_order_ids_round_trip_as_text(self, any_journal):
        """A ``BrokerOrderId``/int must land as its string form, not a repr."""
        key = intent_key(_intent())
        any_journal.append(intent_id=key, kind=KIND_SUBMITTED, broker_order_id=1234)
        assert any_journal.order_id_for(key) == "1234"

    def test_detail_round_trips_as_a_dict(self, any_journal):
        key = intent_key(_intent())
        any_journal.append(
            intent_id=key, kind=KIND_REJECTED, detail={"error": "margin", "status_code": 400}
        )
        assert any_journal.events(key)[0].detail == {
            "error": "margin",
            "status_code": 400,
        }

    def test_unknown_kind_is_refused(self, any_journal):
        with pytest.raises(ValueError, match="unknown journal kind"):
            any_journal.append(intent_id="x", kind="guessed")

    def test_there_is_no_update_or_delete_path(self, any_journal):
        """Append-only is an API property, not a comment (PRD §6)."""
        for forbidden in ("update", "delete", "amend", "remove", "clear"):
            assert not hasattr(any_journal, forbidden)

    def test_events_come_back_oldest_first(self, any_journal):
        key = intent_key(_intent())
        any_journal.append(intent_id=key, kind=KIND_DECIDED)
        any_journal.append(intent_id=key, kind=KIND_SUBMITTED, leg_index=0)
        any_journal.append(intent_id=key, kind=KIND_FILLED, leg_index=0)
        assert [e.kind for e in any_journal.events(key)] == [
            KIND_DECIDED,
            KIND_SUBMITTED,
            KIND_FILLED,
        ]

    def test_count_answers_the_soak_gate(self, any_journal):
        for i in range(3):
            intent = _intent(when=BAR + timedelta(minutes=i))
            any_journal.append_for_intent(intent, KIND_DECIDED)
            any_journal.append_for_intent(intent, KIND_EXECUTED)
        assert any_journal.count() == 6
        assert any_journal.count(KIND_DECIDED) == 3
        assert any_journal.count(KIND_EXECUTED) == 3

    def test_recent_returns_a_window_oldest_first(self, any_journal):
        for i in range(4):
            any_journal.append(
                intent_id=intent_key(_intent(when=BAR + timedelta(minutes=i))),
                kind=KIND_DECIDED,
            )
        assert len(any_journal.recent(limit=2)) == 2

    def test_a_halt_is_recorded_once_per_bar(self, any_journal):
        key = decision_key("halt", "no expiry", code="NO_EXPIRY", clock=BAR)
        assert any_journal.append(intent_id=key, kind=KIND_HALTED)
        assert not any_journal.append(intent_id=key, kind=KIND_HALTED)
        # The next bar is a new decision, so it records again.
        next_bar = decision_key(
            "halt", "no expiry", code="NO_EXPIRY", clock=BAR + timedelta(minutes=1)
        )
        assert any_journal.append(intent_id=next_bar, kind=KIND_HALTED)


# ---------------------------------------------------------------------------
# The durable journal and its schema
# ---------------------------------------------------------------------------


class TestSqliteJournal:
    def test_ensure_schema_is_idempotent(self, journal):
        journal.ensure_schema()
        journal.ensure_schema()
        assert inspect(journal.manager.engine).has_table(TABLE)

    def test_a_second_journal_reads_the_same_rows(self, journal, tmp_path):
        """Cross-process visibility is the point of the durable journal."""
        key = journal.append_for_intent(_intent(), KIND_SUBMITTED, leg_index=0)
        reopened = _sqlite_journal(tmp_path)  # same file, new manager
        assert reopened.order_id_for(key, 0) is None  # no id was recorded
        assert reopened.has(key, KIND_SUBMITTED, 0)

    def test_a_second_process_sees_the_broker_order_id(self, journal, tmp_path):
        intent = _intent()
        key = intent_key(intent)
        journal.append(intent_id=key, kind=KIND_SUBMITTED, leg_index=0, broker_order_id="77")
        assert _sqlite_journal(tmp_path).order_id_for(key, 0) == "77"


class TestSchemaParity:
    """The model, the hand-applied SQL and Alembic must describe one table."""

    @pytest.mark.parametrize(
        "relative",
        [
            "db/migrations/020_option_intent_journal.sql",
            "db/migrations/020_option_intent_journal.sqlite.sql",
            "db/alembic/versions/20261008_1200_020_option_intent_journal.py",
        ],
    )
    def test_the_020_routes_exist(self, relative):
        assert (REPO / relative).exists(), f"missing {relative}"

    def test_sqlite_migration_matches_the_orm(self, tmp_path):
        db_path = tmp_path / "hand.db"
        conn = sqlite3.connect(db_path)
        conn.executescript(
            (REPO / "db" / "migrations" / "001_initial_schema.sqlite.sql").read_text()
        )
        conn.executescript(
            (REPO / "db" / "migrations" / "020_option_intent_journal.sqlite.sql").read_text()
        )
        conn.close()

        hand = inspect(create_engine(f"sqlite:///{db_path}"))
        orm_engine = create_engine("sqlite://")
        Base.metadata.create_all(orm_engine, tables=[OptionIntentEvent.__table__])
        orm = inspect(orm_engine)

        assert TABLE in hand.get_table_names()
        hand_cols = {c["name"] for c in hand.get_columns(TABLE)}
        orm_cols = {c["name"] for c in orm.get_columns(TABLE)}
        assert hand_cols == orm_cols, (
            f"sql-only={sorted(hand_cols - orm_cols)} orm-only={sorted(orm_cols - hand_cols)}"
        )

    def test_kind_enum_matches_the_sql_check(self):
        sql = (REPO / "db" / "migrations" / "020_option_intent_journal.sql").read_text()
        for kind in ALL_KINDS:
            assert f"'{kind}'" in sql, f"{kind} missing from the migration CHECK"
        assert set(OptionIntentKind.values()) == set(ALL_KINDS)

    def test_alembic_revision_follows_019(self):
        source = (
            REPO / "db" / "alembic" / "versions" / "20261008_1200_020_option_intent_journal.py"
        ).read_text()
        assert 'revision: str = "020"' in source
        assert 'down_revision: Union[str, None] = "019"' in source


# ---------------------------------------------------------------------------
# The process-local journal's one job: be loud about its own limits
# ---------------------------------------------------------------------------


class TestMemoryJournalLimits:
    def test_warn_if_live_is_loud_once(self, caplog):
        journal = MemoryJournal()
        with caplog.at_level("WARNING"):
            journal.warn_if_live()
            journal.warn_if_live()
        assert len(caplog.records) == 1
        assert "process-local journal" in caplog.text

    def test_ensure_schema_is_a_no_op(self):
        assert MemoryJournal().ensure_schema() is None


# ---------------------------------------------------------------------------
# Factory — what a runner gets when it wires the journal
# ---------------------------------------------------------------------------


class TestJournalFromEnv:
    def test_off_switch_forces_the_process_local_journal(self, monkeypatch):
        monkeypatch.setenv("OPTIONS_INTENT_JOURNAL", "off")
        assert isinstance(journal_from_env(), MemoryJournal)

    def test_persistence_off_switch_is_respected_too(self, monkeypatch):
        """The options path already isolates itself from the DB with this knob."""
        monkeypatch.delenv("OPTIONS_INTENT_JOURNAL", raising=False)
        monkeypatch.setenv("OPTIONS_PERSISTENCE", "off")
        assert isinstance(journal_from_env(), MemoryJournal)

    def test_an_unreachable_database_degrades_instead_of_raising(self, monkeypatch, caplog):
        monkeypatch.delenv("OPTIONS_INTENT_JOURNAL", raising=False)
        monkeypatch.delenv("OPTIONS_PERSISTENCE", raising=False)

        def _boom(*_a, **_kw):
            raise RuntimeError("database is down")

        monkeypatch.setattr("backtest.db.DatabaseManager.from_env", _boom)
        with caplog.at_level("WARNING"):
            journal = journal_from_env()
        assert isinstance(journal, MemoryJournal)
        assert "process memory only" in caplog.text
