import pytest
from fastapi import HTTPException

from backend.app.main import app
from backend.app.models import Product
from backend.app.product_supplier_binding_api import (
    ManualSupplierBindingCreate,
    ManualSupplierBindingResult,
)
from backend.app.workspace_auth import WorkspacePrincipal
from backend.app.workspace_models import Workspace
from backend.app import workspace_supplier_binding_api


def _principal(workspace: Workspace) -> WorkspacePrincipal:
    return WorkspacePrincipal(
        user_id=100,
        username="workspace-owner",
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
    )


def test_workspace_supplier_binding_route_is_registered() -> None:
    paths = {route.path for route in app.routes}
    assert "/api/workspace/products/{product_id}/supplier-bindings/manual" in paths


def test_foreign_product_cannot_receive_supplier_binding(db_session) -> None:
    owner = Workspace(name="Owner", slug="binding-owner")
    foreign = Workspace(name="Foreign", slug="binding-foreign")
    db_session.add_all([owner, foreign])
    db_session.flush()
    product = Product(
        workspace_id=foreign.id,
        kaspi_product_id="FOREIGN-BINDING-1",
        merchant_sku="FOREIGN-BINDING-1",
        name="Foreign product",
    )
    db_session.add(product)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        workspace_supplier_binding_api.create_workspace_manual_supplier_binding(
            product_id=product.id,
            payload=ManualSupplierBindingCreate(url="https://www.ozon.ru/product/item-123456/"),
            principal=_principal(owner),
            db=db_session,
        )

    assert exc.value.status_code == 404


def test_owned_product_is_delegated_to_existing_binding_pipeline(db_session, monkeypatch) -> None:
    owner = Workspace(name="Owner two", slug="binding-owner-two")
    db_session.add(owner)
    db_session.flush()
    product = Product(
        workspace_id=owner.id,
        kaspi_product_id="OWN-BINDING-1",
        merchant_sku="OWN-BINDING-1",
        name="Owned product",
    )
    db_session.add(product)
    db_session.commit()

    expected = ManualSupplierBindingResult(
        product_id=product.id,
        supplier_code="ozon",
        supplier_product_id=11,
        binding_id=12,
        monitor_target_id=13,
        job_id=None,
        created_supplier_product=True,
        created_binding=True,
        queued_initial_check=False,
    )
    called = {}

    def fake_create_manual_supplier_binding(*, product_id, payload, db):
        called["product_id"] = product_id
        called["db"] = db
        return expected

    monkeypatch.setattr(
        workspace_supplier_binding_api,
        "create_manual_supplier_binding",
        fake_create_manual_supplier_binding,
    )

    result = workspace_supplier_binding_api.create_workspace_manual_supplier_binding(
        product_id=product.id,
        payload=ManualSupplierBindingCreate(url="https://www.ozon.ru/product/item-123456/"),
        principal=_principal(owner),
        db=db_session,
    )

    assert result == expected
    assert called == {"product_id": product.id, "db": db_session}
