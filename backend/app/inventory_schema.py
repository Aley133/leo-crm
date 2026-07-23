from __future__ import annotations

from sqlalchemy import Engine, inspect

from .inventory_models import InventoryAllocation, InventoryBatch


def ensure_inventory_schema(engine: Engine) -> tuple[str, ...]:
    """Create missing FIFO tables without modifying existing inventory data."""

    created: list[str] = []
    with engine.begin() as connection:
        existing = set(inspect(connection).get_table_names())
        if InventoryBatch.__tablename__ not in existing:
            InventoryBatch.__table__.create(bind=connection, checkfirst=True)
            created.append(InventoryBatch.__tablename__)

        existing = set(inspect(connection).get_table_names())
        if InventoryAllocation.__tablename__ not in existing:
            InventoryAllocation.__table__.create(bind=connection, checkfirst=True)
            created.append(InventoryAllocation.__tablename__)

    return tuple(created)
