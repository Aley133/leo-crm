from __future__ import annotations

"""Ensure FIFO inventory tables exist in the deployed database."""

from backend.app.db import engine
from backend.app.inventory_schema import ensure_inventory_schema


def main() -> None:
    created = ensure_inventory_schema(engine)
    if created:
        print(f"Created FIFO inventory tables: {', '.join(created)}")
    else:
        print("FIFO inventory schema is already present")


if __name__ == "__main__":
    main()
