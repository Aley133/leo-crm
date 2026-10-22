from datetime import UTC, datetime

from sqlalchemy.orm import sessionmaker
from backend.app.inventory_models import InventoryBatch

from backend.app import fast_dumping_maintenance as maintenance
from backend.app.fast_dumping_models import FastDumpingPolicy, FastDumpingState
from backend.app.fast_dumping_service import claim_job, ensure_state
from backend.app.models import Product
from backend.app.product_test_models import ProductTestItem  # noqa: F401
from backend.app.workspace_context import workspace_context
from backend.app.workspace_models import Workspace


def test_background_prepares_both_workspace_queues_without_claim_side_effects(db_session, monkeypatch):
    for workspace_id in (1, 3):
        with workspace_context(workspace_id):
            db_session.add(Workspace(id=workspace_id, name=f"Shop {workspace_id}", slug=f"shop-{workspace_id}", is_active=True))
            product = Product(workspace_id=workspace_id, name="Ready", kaspi_product_id=f"ready-{workspace_id}", sale_enabled=True)
            db_session.add(product)
            db_session.flush()
            db_session.add(InventoryBatch(workspace_id=workspace_id, product_id=product.id, received_at=datetime.now(UTC),
                quantity_received=2, quantity_remaining=2, unit_cost=10))
            policy = FastDumpingPolicy(workspace_id=workspace_id, product_id=product.id, enabled=True)
            db_session.add(policy)
            db_session.flush()
            ensure_state(db_session, policy=policy, workspace_id=workspace_id)
            db_session.commit()
    factory = sessionmaker(bind=db_session.bind, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(maintenance, "SessionLocal", factory)
    monkeypatch.setattr(maintenance, "_pool_has_room", lambda: True)
    maintenance.run_maintenance_cycle()
    assert maintenance.STATUS["workspaces"] == 2
    def forbidden(*args, **kwargs):
        raise AssertionError("claim ran maintenance")
    monkeypatch.setattr("backend.app.fast_dumping_service.maintain_workspace_queue", forbidden)
    for workspace_id in (1, 3):
        with factory() as db, workspace_context(workspace_id):
            claimed = claim_job(db, workspace_id=workspace_id, agent_id="agent", maintain_queue=False)
            assert claimed is not None and claimed.workspace_id == workspace_id
            assert claimed.status == "leased_scan"
            db.commit()


def test_busy_pool_defers_maintenance_without_opening_session(monkeypatch):
    monkeypatch.setattr(maintenance, "_pool_has_room", lambda: False)
    def forbidden():
        raise AssertionError("maintenance acquired a saturated pool")
    monkeypatch.setattr(maintenance, "SessionLocal", forbidden)
    maintenance.run_maintenance_cycle()
    assert maintenance.STATUS["status"] == "deferred_pool_busy"
