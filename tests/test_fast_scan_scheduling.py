from datetime import UTC, datetime, timedelta

from sqlalchemy import event, select

from backend.app.fast_dumping_models import (
    FastDumpingJob,
    FastDumpingPolicy,
    FastDumpingState,
)
from backend.app.fast_dumping_service import schedule_due_scans
from backend.app.models import Product
from backend.app.workspace_context import workspace_context


def test_due_batch_uses_one_read_and_preserves_guards_and_priority(db_session):
    now = datetime.now(UTC)
    states = {}
    for i in range(25):
        workspace_id = 3 if i == 24 else 1
        with workspace_context(workspace_id):
            product = Product(
                workspace_id=workspace_id,
                name=f"Scheduled {i}",
                kaspi_product_id=f"scheduled-{i}",
                merchant_sku=f"scheduled-{i}",
                sale_enabled=i != 23,
            )
            db_session.add(product)
            db_session.flush()
            policy = FastDumpingPolicy(
                workspace_id=workspace_id,
                product_id=product.id,
                enabled=i != 22,
                pricing_mode="automation" if i == 19 else "manual",
            )
            db_session.add(policy)
            db_session.flush()
            state = FastDumpingState(
                workspace_id=workspace_id,
                policy_id=policy.id,
                product_id=product.id,
                next_scan_at=now + timedelta(hours=1) if i == 21 else now,
                automatic_writes_paused=i == 20,
                last_error_code="old_error",
                last_error_message="Old error",
            )
            db_session.add(state)
            states[i] = state
            db_session.commit()
    db_session.expire_all()
    reads = []

    def record_read(_conn, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith("SELECT"):
            reads.append(statement)

    event.listen(db_session.bind, "before_cursor_execute", record_read)
    try:
        with workspace_context(1):
            assert schedule_due_scans(
                db_session, workspace_id=1, limit=10, now=now,
                recover_inventory_transitions=False,
            ) == 10
    finally:
        event.remove(db_session.bind, "before_cursor_execute", record_read)
    assert len(reads) == 1
    assert states[19].active_job_id is not None  # Automation takes priority.
    assert all(states[i].active_job_id is None for i in range(20, 24))
    with workspace_context(3):
        assert states[24].active_job_id is None
    with workspace_context(1):
        first_ids = set(db_session.scalars(select(FastDumpingJob.id)).all())
        assert len(first_ids) == 10
        assert schedule_due_scans(
            db_session, workspace_id=1, limit=20, now=now,
            recover_inventory_transitions=False,
        ) == 10
        assert schedule_due_scans(
            db_session, workspace_id=1, now=now,
            recover_inventory_transitions=False,
        ) == 0
        jobs = db_session.scalars(select(FastDumpingJob)).all()
        assert len(jobs) == 20
        assert first_ids.issubset({job.id for job in jobs})
        for job in jobs:
            state = db_session.scalar(
                select(FastDumpingState).where(
                    FastDumpingState.product_id == job.product_id
                )
            )
            assert state.active_job_id == job.id
            assert state.status == "queued"
            assert state.next_scan_at is None
            assert state.last_error_code is None
            assert state.last_error_message is None
