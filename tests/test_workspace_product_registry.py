from sqlalchemy import select

from backend.app.main import app
from backend.app.models import Product
from backend.app.workspace_models import Workspace


def test_same_kaspi_product_id_isolated_between_workspaces(db_session) -> None:
    first_workspace = Workspace(name="First products", slug="first-products")
    second_workspace = Workspace(name="Second products", slug="second-products")
    db_session.add_all([first_workspace, second_workspace])
    db_session.flush()

    first = Product(
        workspace_id=first_workspace.id,
        kaspi_product_id="100500",
        merchant_sku="first-sku",
        name="First product",
        status="active",
    )
    second = Product(
        workspace_id=second_workspace.id,
        kaspi_product_id="100500",
        merchant_sku="second-sku",
        name="Second product",
        status="active",
    )
    db_session.add_all([first, second])
    db_session.commit()

    assert first.id != second.id
    assert db_session.scalar(
        select(Product).where(
            Product.workspace_id == first_workspace.id,
            Product.kaspi_product_id == "100500",
        )
    ) is first
    assert db_session.scalar(
        select(Product).where(
            Product.workspace_id == second_workspace.id,
            Product.kaspi_product_id == "100500",
        )
    ) is second


def test_product_workspace_is_required() -> None:
    assert Product.__table__.c.workspace_id.nullable is False
    constraint_names = {constraint.name for constraint in Product.__table__.constraints}
    assert "uq_products_workspace_kaspi_product_id" in constraint_names


def test_workspace_product_routes_are_registered() -> None:
    paths = {route.path for route in app.routes}
    assert "/api/workspace/products" in paths
    assert "/api/workspace/products/{product_id}" in paths
