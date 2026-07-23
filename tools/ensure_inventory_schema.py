from __future__ import annotations

"""Ensure FIFO inventory tables exist in the deployed database.

The project currently has two historical Alembic version directories. The active
alembic.ini points at ``migrations`` while the FIFO revision was introduced under
``alembic/versions``. This deployment guard is intentionally idempotent and
creates only the two FIFO tables represented by the current SQLAlchemy models.
Alembic remains the long-term migration authority; this guard prevents a code /
schema deployment race from taking Orders Center down.
"""

from sqlalchemy import inspect

from backend.app.db import engine
from backend.app.inventory_models import InventoryAllocation, InventoryBatch


def ensure_inventory_schema() -> tuple[str, ...]:
    created: list[str] = []

    with engine.begin() as connection:
        inspector = inspect(connection)
        existing = set(inspector.get_table_names())

        if InventoryBatch.__tablename__ not in existing:
            InventoryBatch.__table__.create(bind=connection, checkfirst=True)
            created.append(InventoryBatch.__tablename__)

        # Refresh after the parent table is created so the FK target is visible.
        inspector = inspect(connection)
        existing = set(inspector.get_table_names())
        if InventoryAllocation.__tablename__ not in existing:
            InventoryAllocation.__table__.create(bind=connection, checkfirst=True)
            created.append(InventoryAllocation.__tablename__)

    return tuple(created)


def main() -> None:
    created = ensure_inventory_schema()
    if created:
        print(f"Created FIFO inventory tables: {', '.join(created)}")
    else:
        print("FIFO inventory schema is already present")


if __name__ == "__main__":
    main()
