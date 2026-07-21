from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse


STATIC_DIR = Path(__file__).resolve().parent / "static"

router = APIRouter(tags=["crm-ui"], include_in_schema=False)


@router.get("/crm", response_class=FileResponse)
def crm_dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "dashboard.html")


@router.get("/crm/products", response_class=FileResponse)
def crm_products() -> FileResponse:
    return FileResponse(STATIC_DIR / "products.html")


@router.get("/crm/products/{product_id}", response_class=FileResponse)
def crm_product_detail(product_id: int) -> FileResponse:
    return FileResponse(STATIC_DIR / "product-detail.html")


@router.get("/crm/orders", response_class=FileResponse)
def crm_orders() -> FileResponse:
    return FileResponse(STATIC_DIR / "orders.html")


@router.get("/crm/suppliers", response_class=FileResponse)
def crm_suppliers() -> FileResponse:
    return FileResponse(STATIC_DIR / "suppliers.html")


@router.get("/crm/monitoring", response_class=FileResponse)
def crm_monitoring() -> FileResponse:
    return FileResponse(STATIC_DIR / "monitoring.html")
