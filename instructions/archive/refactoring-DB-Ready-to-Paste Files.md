# DB-T1 — Ready-to-Paste Files

---

## 1. Migration file: `db/migrations/002_add_mode_source.sql`

```sql
-- ============================================================
-- 002_add_mode_source.sql
-- Adds `mode` and `source` flags to portfolios table.
--
-- mode:   which risk bucket / semantics a running instance has
--         'paper' | 'live'
-- source: where candle data came from
--         'synthetic' | 'replay' | 'mstock'
--
-- Additive + backfilling. Safe on fresh AND existing DBs.
-- ============================================================

ALTER TABLE portfolios
  ADD COLUMN mode   TEXT NOT NULL DEFAULT 'paper'
    CHECK (mode IN ('paper', 'live'));

ALTER TABLE portfolios
  ADD COLUMN source TEXT NOT NULL DEFAULT 'synthetic'
    CHECK (source IN ('synthetic', 'replay', 'mstock'));

-- Backfill legacy rows (in case a DB had rows before this migration).
UPDATE portfolios
   SET mode   = 'paper',
       source = 'synthetic'
 WHERE mode   IS NULL
    OR source IS NULL;
```

> **Jr engineer note:** If your migration runner applies files by filename order, `002_...sql` sorts after `001_initial_schema.sql` — matching how the existing runner works. If it's NOT filename-sorted, ask your lead where to register this script.

---

## 2. `db/models.py` changes (add to the `Portfolio` model)

```python
# db/models.py  — add these two columns to the Portfolio model/table definition

class Portfolio(Base):                    # or however your base is declared
    __tablename__ = "portfolios"

    # ... existing columns ...

    # NEW — Phase 1 (DB-T1)
    mode   = Column(
        String(10),
        nullable=False,
        default="paper",
        server_default="paper",
    )
    source = Column(
        String(20),
        nullable=False,
        default="synthetic",
        server_default="synthetic",
    )

    # Optional: keep the CHECK constraint visible at the ORM level
    __table_args__ = (
        CheckConstraint(
            "mode IN ('paper', 'live')",
            name="ck_portfolios_mode",
        ),
        CheckConstraint(
            "source IN ('synthetic', 'replay', 'mstock')",
            name="ck_portfolios_source",
        ),
        # ... any existing table args ...
    )
```

> **Jr engineer note:** the `server_default` ensures the DB applies the same default even if ORM isn't the writer; the plain `default` covers ORM-only inserts. Both match the SQL migration.

---

## 3. Test file: `tests/db/test_migrations_002.py`

```python
# tests/db/test_migrations_002.py
import pytest
from sqlalchemy import text

# Adjust import to your actual migration-runner / test-DB fixture.
from db.config import get_engine, get_session


@pytest.fixture(scope="module")
def migrated_engine():
    """Applies 002 migration to a fresh DB (mirror how your runner works)."""
    engine = get_engine("sqlite:///:memory:")   # or your test-DB helper
    # 1) apply 001 (if your runner doesn't auto-run in order, do it here)
    # 2) apply 002_add_mode_source.sql
    with engine.begin() as conn:
        conn.execute(text(_read_migration_002()))
    return engine


def _read_migration_002():
    # Helper: read db/migrations/002_add_mode_source.sql
    path = Path(__file__).parents[2] / "db" / "migrations" / "002_add_mode_source.sql"
    return path.read_text()


def test_columns_exist_with_defaults(migrated_engine):
    with migrated_engine.connect() as conn:
        cols = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='portfolios'"
        )).fetchall()  # or sqlite PRAGMA equivalent
    names = {c[0] for c in cols}
    assert "mode" in names
    assert "source" in names


def test_backfill_legacy_row(migrated_engine):
    # Insert a legacy row WITHOUT mode/source (simulate pre-migration data)
    with migrated_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO portfolios (id) VALUES (:id)"
        ), {"id": "legacy-1"})   # only existing required column
    # After backfill, defaults applied
    with migrated_engine.connect() as conn:
        row = conn.execute(
            text("SELECT mode, source FROM portfolios WHERE id='legacy-1'")
        ).fetchone()
    assert row.mode == "paper"
    assert row.source == "synthetic"


def test_check_constraint_rejects_bad_values(migrated_engine):
    with pytest.raises(Exception):   # IntegrityError
        with migrated_engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO portfolios (id, mode, source) "
                "VALUES ('bad', 'bogus', 'synthetic')"
            ))
```

> **Jr engineer note:** the exact introspection query (pandas/SQLAlchemy inspector, sqlite PRAGMA, information_schema) depends on whether your runner uses **PostgreSQL (Timescale)** or **SQLite** for dev tests. Adjust `test_columns_exist_with_defaults` to your DB. The **backfill and CHECK-constraint tests are the important ones** — they prove the migration is safe and correct.

---

# DB-T3 — Live-order `broker_order_id` wiring

## `simulator/fill_providers.py` (new file — created in Phase 3)

```python
# simulator/fill_providers.py
from abc import ABC, abstractmethod

from simulator.fill import Fill
from simulator.fees import FeeModel      # adjust to real import path
from simulator.slippage import SlippageModel


class FillProvider(ABC):
    """Decides WHERE a fill comes from. This is the ONLY paper/live split."""
    @abstractmethod
    def produce_fill(self, order, fill_bar) -> Fill:
        ...


class SimulatedFillProvider(FillProvider):
    """Paper / synthetic / backtest — fills at next-bar open with fee+slippage."""
    def __init__(self, fee_model: FeeModel, slippage_model: SlippageModel):
        self.fee_model = fee_model
        self.slippage_model = slippage_model

    def produce_fill(self, order, fill_bar) -> Fill:
        price = self.slippage_model.apply(fill_bar.open)
        fee   = self.fee_model.compute(price, order.quantity)
        return Fill(
            order=order,
            price=price,
            quantity=order.quantity,
            fee=fee,
            broker_order_id=None,          # paper → NULL
        )


class BrokerFillProvider(FillProvider):
    """LIVE — real broker. Places the order and maps the real fill back."""
    def __init__(self, broker):
        self.broker = broker              # a BrokerOrderBase instance

    def produce_fill(self, order, fill_bar) -> Fill:
        broker_ref = self.broker.place_order(order)          # real network call
        real_fill  = self.broker.poll_fill(broker_ref)       # wait for execution
        # Map the broker's real fill into our immutable Fee-aware Fill
        return Fill(
            order=order,
            price=real_fill.price,
            quantity=real_fill.quantity,
            fee=real_fill.fee,                                # broker-reported (or compute)
            broker_order_id=broker_ref.order_id,              # ← THE point of DB-T3
        )
```

## `simulator/order.py` — ensure persistence writes `broker_order_id`

```python
# simulator/order.py  — the Order row persistence
class Order:
    # ... existing ...

    broker_order_id: str | None = None   # NEW field

    def persist(self, session):
        """Write/update the order row. Now includes broker_order_id."""
        row = session.query(OrderRow).filter_by(id=self.id).first()
        if row is None:
            row = OrderRow(id=self.id, ...)
            session.add(row)
        row.broker_order_id = self.broker_order_id   # NEW — saved here
        # ... existing fields ...
        session.commit()
```

> **Jr engineer note:** `broker_order_id` is `None` for every paper/synthetic/backtest fill (SimulatedFillProvider sets it to None). It's **only** populated in live mode when BrokerFillProvider returns a real id. This is exactly the "same trade, different mode → never confused" rule from the design.

---

## `db/models.py` — confirm `broker_order_id` exists on the `orders` model

```python
# db/models.py  — the OrderRow / orders table (should already exist, verify)
class OrderRow(Base):
    __tablename__ = "orders"

    # ... existing ...

    broker_order_id = Column(String(64), nullable=True)   # ← confirm present
    # paper → NULL, live → broker's real id (populated by BrokerFillProvider)
```

---

## Test: live fill populates `broker_order_id`

```python
# tests/simulator/test_fill_providers.py  (add)
def test_broker_fill_sets_broker_order_id():
    class FakeBroker:
        def place_order(self, order):
            return BrokerOrderRef(order_id="BROKER-1234")
        def poll_fill(self, ref):
            return RealFill(price=101.5, quantity=10, fee=2.0)

    provider = BrokerFillProvider(FakeBroker())
    order = Order(quantity=10)
    fill = provider.produce_fill(order, fill_bar)

    assert fill.broker_order_id == "BROKER-1234"
    # and in paper mode:
    sim_provider = SimulatedFillProvider(fee_model, slippage_model)
    sim_fill = sim_provider.produce_fill(order, fill_bar)
    assert sim_fill.broker_order_id is None
```

---

# DB-T4 — Remove phantom `forward_test_*` references

## Files to edit/delete

**Delete (or strip):** `forward/live_engine.py` — remove every write to `forward_test_state` / `forward_test_trades` / `forward_test_equity`.

**Do a sweep:**
```bash
grep -rn "forward_test_state\|forward_test_trades\|forward_test_equity" src/ db/ docs/ tests/
```
Delete or comment every hit. These tables have **no DDL anywhere** — pure dead code.

**Check for stray DDL/docs:** if any `*.sql` or doctest defines them, remove. The spec'd tables (`equity_curve`, `trades`) already hold this data.

---

# DB-T5 — Single DB-URL authority

## `db/config.py` — the only place that resolves the connection string

```python
# db/config.py — SINGLE authority for DB connection URL
import os
from pathlib import Path

def resolve_db_url() -> str:
    """The ONLY function that decides where the DB lives.
    Order: env override → config file → dev default."""
    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        return env_url

    # optional: read from a single config file
    config_path = Path(__file__).resolve().parents[1] / "config" / "app.yaml"
    if config_path.exists():
        # ... parse and return db url from config ...
        pass

    # dev default (SQLite) — change to your actual default
    return "sqlite:///data/dev.db"

def get_engine():
    from sqlalchemy import create_engine
    return create_engine(resolve_db_url())
```

> **Jr engineer note:** `db_source.py`, `live_engine.py`, and anything else that currently builds its own URL must import `resolve_db_url()` / `get_engine()` from `db/config.py`. After this, exactly **one** file owns the URL.

---

# DB-T6 — Fix timeframe naming against schema CHECK constraints

## Pick a canonical set (recommended here):

| Canonical | Maps from (inconsistent) |
|---|---|
| `1m`, `5m`, `15m`, `30m` | `minute`, `5min`, `M5` |
| `1h`, `4h` | `1H`, `4hour`, `hourly`, `H4` |
| `1d` | `1D`, `day`, `daily` |

## `db/models.py` — align CHECK constraint to canonical set

```python
CheckConstraint(
    "timeframe IN ('1m','5m','15m','30m','1h','4h','1d')",
    name="ck_orders_timeframe",   # name to your actual table/column
)
```

## Sweep code for old names

```bash
grep -rn "'1H'\|\"4hour\"\|'minute'\|'hourly'\|'M5'\|'H4'" src/ config/ tests/
```
Replace every trade-timeframe string with the canonical value so nothing violates the CHECK constraint.

**Acceptance:** no timeframe string in code/config that the schema CHECK rejects.

---

# Full DB Task Board (ready to hand to a Jr engineer)

| ID | Task | Files | Git-ish "Done" check |
|---|---|---|---|
| DB-T1 | `mode`/`source` cols | `002_add_mode_source.sql`, `db/models.py` | migration applies, legacy rows backfilled, bad values rejected |
| DB-T2 | verify core tables | (read-only) `db/models.py` | no schema change needed |
| DB-T3 | populate `broker_order_id` | `fill_providers.py`, `order.py`, `models.py` | live fill saves broker id; paper keeps `NULL` |
| DB-T4 | drop phantom `forward_test_*` | `live_engine.py` + sweep | grep returns nothing |
| DB-T5 | unify DB-URL | `db/config.py` + callers | exactly 1 file resolves URL |
| DB-T6 | canonical timeframe | `db/models.py` + code sweep | no violating timeframe string |

**Order:** DB-T1 → DB-T2 → DB-T4 → DB-T3 → DB-T5 → DB-T6

Everything is additive or cleanup-first, so you can apply DB-T1 and DB-T4 anytime without breaking the running system. DB-T3 is the only behavior change and it's live-mode-only (won't affect paper).