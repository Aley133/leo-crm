from sqlalchemy import event

from backend.app.fast_dumping_service import _lock_state, ensure_state
from backend.app.fast_dumping_models import FastDumpingPolicy
from backend.app.fast_dumping_uow import fast_unit_of_work, workspace_row
from backend.app.models import Product
from backend.app.workspace_context import workspace_context


def test_locks_are_reused_only_in_same_transaction_and_scope(db_session):
    with workspace_context(1):
        product = Product(name="Scope", kaspi_product_id="scope")
        db_session.add(product)
        db_session.flush()
        policy = FastDumpingPolicy(product_id=product.id)
        db_session.add(policy)
        db_session.flush()
        state = ensure_state(db_session, policy=policy, workspace_id=1)
        db_session.commit()
        product_id = product.id
        reads = []
        def record(_conn, _cursor, statement, *_args):
            if statement.lstrip().upper().startswith("SELECT"):
                reads.append(statement)
        event.listen(db_session.bind, "before_cursor_execute", record)
        try:
            with fast_unit_of_work(db_session, 1):
                assert workspace_row(db_session, Product, product_id, 3) is None
                assert _lock_state(db_session, workspace_id=1, product_id=product_id) is state
                assert _lock_state(db_session, workspace_id=1, product_id=product_id) is state
                assert len(reads) == 1
                db_session.commit()
                _lock_state(db_session, workspace_id=1, product_id=product_id)
                assert len(reads) == 2
                db_session.rollback()
                _lock_state(db_session, workspace_id=1, product_id=product_id)
                assert len(reads) == 3
            with fast_unit_of_work(db_session, 1):
                _lock_state(db_session, workspace_id=1, product_id=product_id)
                assert len(reads) == 4
        finally:
            event.remove(db_session.bind, "before_cursor_execute", record)
        assert "fast_dumping_unit_of_work" not in db_session.info
