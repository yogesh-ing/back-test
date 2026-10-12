"""Database migration: populate strategy_metadata table from existing strategies.

This script discovers all registered strategies and inserts their metadata
into the new strategy_metadata table for persistent storage and querying.

Usage:
    cd src && python -m backtest.pine.migrate_strategy_metadata [db_url]
"""

from __future__ import annotations

import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backtest.db.models import StrategyMetadata, Base
from backtest.plugins import discover_plugins
from backtest.strategy.registry import get_all


def migrate_strategy_metadata(db_url: str = "sqlite:///backtest.db"):
    """Migrate strategy metadata into the database."""
    engine = create_engine(db_url)

    # Ensure table exists
    Base.metadata.create_all(engine, tables=[StrategyMetadata.__table__])

    discover_plugins()
    strategies = get_all()

    with Session(engine) as session:
        inserted = 0
        updated = 0

        for strategy_info in strategies:
            name = strategy_info["name"]
            description = strategy_info.get("description", "")
            version = strategy_info.get("version", "")
            author = strategy_info.get("author", "")
            signal_kind_val = strategy_info.get("signal_kind", "equity")
            eligible = strategy_info.get("eligible_instruments")

            # Check if already exists
            existing = (
                session.query(StrategyMetadata)
                .filter_by(strategy_name=name)
                .first()
            )

            if existing:
                # Update existing
                existing.description = description or existing.description
                existing.version = version or existing.version
                existing.author = author or existing.author
                existing.signal_kind = signal_kind_val
                if eligible:
                    existing.eligible_instruments = ",".join(eligible)
                updated += 1
                print(f"[OK] Updated '{name}'")
            else:
                # Insert new
                meta = StrategyMetadata(
                    strategy_name=name,
                    description=description or f"{name} strategy",
                    version=version or "1.0",
                    author=author or "",
                    signal_kind=signal_kind_val,
                    eligible_instruments=",".join(eligible) if eligible else None,
                )
                session.add(meta)
                inserted += 1
                print(f"[OK] Inserted '{name}'")

        session.commit()

    print("\nMigration complete:")
    print(f"  Inserted: {inserted}")
    print(f"  Updated: {updated}")
    print(f"  Total: {len(strategies)}")


if __name__ == "__main__":
    db_url = sys.argv[1] if len(sys.argv) > 1 else "sqlite:///backtest.db"
    migrate_strategy_metadata(db_url)
