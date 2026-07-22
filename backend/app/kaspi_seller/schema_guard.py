from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .snapshot_models import KaspiSellerOrderSnapshotRecord
from .timeline_models import KaspiSellerOrderTimelineEvent


_REQUIRED_TABLES = {
    KaspiSellerOrderSnapshotRecord.__tablename__,
    KaspiSellerOrderTimelineEvent.__tablename__,
}


def ensure_kaspi_seller_storage_schema(db: Session) -> bool:
    """Ensure Snapshot/Timeline tables exist in the database used by this request.

    Alembic remains the authoritative migration mechanism. This guard exists for
    production environments where the migration revision was stamped previously
    while one or both physical tables were absent. It performs no work once the
    schema is healthy.
    """

    bind = db.get_bind()
    engine = bind if isinstance(bind, Engine) else bind.engine
    existing = set(inspect(engine).get_table_names())
    missing = _REQUIRED_TABLES - existing
    if not missing:
        return False

    # Create in dependency order because timeline rows reference snapshots.
    KaspiSellerOrderSnapshotRecord.__table__.create(bind=engine, checkfirst=True)
    KaspiSellerOrderTimelineEvent.__table__.create(bind=engine, checkfirst=True)
    return True
