from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, RedirectResponse


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


@router.get("/crm/revenue", response_class=FileResponse)
def crm_revenue() -> FileResponse:
    return FileResponse(STATIC_DIR / "revenue.html")


@router.get("/crm/accounting", response_class=FileResponse)
def crm_accounting() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "accounting.html",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/crm/dumping", response_class=RedirectResponse)
def crm_dumping() -> RedirectResponse:
    return RedirectResponse("/crm/fast-dumping", status_code=307)


@router.get("/crm/fast-dumping", response_class=FileResponse)
def crm_fast_dumping() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "fast-dumping.html",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/crm/product-test", response_class=FileResponse)
def crm_product_test() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "product-test.html",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/crm/add-product", response_class=FileResponse)
def crm_add_product() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "add-product.html",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/crm/suppliers", response_class=FileResponse)
def crm_suppliers() -> FileResponse:
    return FileResponse(STATIC_DIR / "suppliers.html")


@router.get("/crm/monitoring", response_class=FileResponse)
def crm_monitoring() -> FileResponse:
    return FileResponse(STATIC_DIR / "monitoring.html")


@router.get("/crm/full-automation", response_class=RedirectResponse)
def full_automation_page():
    return RedirectResponse("/crm/fast-dumping", status_code=307)


@router.get("/crm/preorder", response_class=FileResponse)
def preorder_page():
    return FileResponse(STATIC_DIR / "preorder.html", headers={"Cache-Control": "no-store"})
