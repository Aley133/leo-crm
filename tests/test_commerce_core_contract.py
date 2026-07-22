from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_commerce_core_is_registered_as_separate_application_boundary() -> None:
    main = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    api = (ROOT / "backend" / "app" / "commerce" / "api.py").read_text(encoding="utf-8")

    assert "from .commerce.api import router as commerce_router" in main
    assert "app.include_router(commerce_router)" in main
    assert 'prefix="/api/commerce"' in api
    assert '@router.get("/orders"' in api
    assert "dependencies=[Depends(require_service_token)]" in api


def test_commerce_core_reuses_normalized_orders_and_purchases() -> None:
    repository = (
        ROOT / "backend" / "app" / "commerce" / "repository.py"
    ).read_text(encoding="utf-8")

    assert "MarketplaceOrder" in repository
    assert "MarketplaceOrderLine" in repository
    assert "PurchaseRequest" in repository
    assert "PurchaseRequestLine" in repository
    # Kaspi operational stages are normalized before Commerce Core. The repository
    # must not read the removed Browser Agent Snapshot subsystem.
    assert "KaspiSellerOrderSnapshotRecord" not in repository
    assert "requests." not in repository


def test_commerce_domain_does_not_depend_on_fastapi_or_sqlalchemy() -> None:
    domain = (ROOT / "backend" / "app" / "commerce" / "domain.py").read_text(
        encoding="utf-8"
    )
    service = (ROOT / "backend" / "app" / "commerce" / "service.py").read_text(
        encoding="utf-8"
    )

    assert "fastapi" not in domain
    assert "sqlalchemy" not in domain
    assert "fastapi" not in service
    assert "sqlalchemy" not in service
