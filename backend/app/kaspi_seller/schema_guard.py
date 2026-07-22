from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .snapshot_models import KaspiSellerOrderSnapshotRecord
from .timeline_models import KaspiSellerOrderTimelineEvent


_REQUIRED_TABLES = {
    KaspiSellerOrderSnapshotRecord.__tablename__,
    KaspiSellerOrderTimelineEvent.__tablename__,
}


def ensure_kaspi_seller_storage_schema(db: Session) -> bool:
    """Repair production schema drift before Snapshot writes.

    Unit tests intentionally use lightweight fake sessions without a SQLAlchemy
    bind. Such sessions do not represent a physical database and therefore need
    no schema repair.
    """

    get_bind = getattr(db, "get_bind", None)
    if not callable(get_bind):
        return False

    bind = get_bind()
    engine = bind if isinstance(bind, Engine) else bind.engine
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    missing = _REQUIRED_TABLES - existing
    changed = False

    if missing:
        KaspiSellerOrderSnapshotRecord.__table__.create(bind=engine, checkfirst=True)
        KaspiSellerOrderTimelineEvent.__table__.create(bind=engine, checkfirst=True)
        changed = True
        inspector = inspect(engine)

    snapshot_table = KaspiSellerOrderSnapshotRecord.__tablename__
    if snapshot_table in set(inspector.get_table_names()):
        columns = {column["name"] for column in inspector.get_columns(snapshot_table)}
        if "marketplace_account_id" not in columns:
            db.execute(
                text(
                    "ALTER TABLE kaspi_seller_order_snapshots "
                    "ADD COLUMN marketplace_account_id INTEGER NULL"
                )
            )
            db.flush()
            changed = True

    return changed
