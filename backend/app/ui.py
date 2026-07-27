from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, RedirectResponse


STATIC_DIR = Path(__file__).resolve().parent / "static"
NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

router = APIRouter(tags=["crm-ui"], include_in_schema=False)


def _file(name: str) -> FileResponse:
    return FileResponse(STATIC_DIR / name, headers=NO_STORE_HEADERS)


@router.get("/login", response_class=FileResponse)
def login_page() -> FileResponse:
    return _file("auth.html")


@router.get("/crm/account", response_class=FileResponse)
def crm_account() -> FileResponse:
    return _file("account.html")


@router.get("/crm", response_class=FileResponse)
def crm_gateway() -> FileResponse:
    # Browser-side gateway is required because the workspace session is stored in
    # localStorage and is therefore not available to the server request.
    return _file("crm-gateway.html")


@router.get("/crm/workspace", response_class=RedirectResponse)
def crm_workspace_root() -> RedirectResponse:
    return RedirectResponse("/crm/workspace/orders", status_code=307, headers=NO_STORE_HEADERS)


@router.get("/crm/legacy", response_class=FileResponse)
def crm_legacy_dashboard() -> FileResponse:
    return _file("dashboard.html")


@router.get("/crm/products", response_class=FileResponse)
def crm_products() -> FileResponse:
    return _file("products.html")


@router.get("/crm/products/{product_id}", response_class=FileResponse)
def crm_product_detail(product_id: int) -> FileResponse:
    return _file("product-detail.html")


@router.get("/crm/orders", response_class=FileResponse)
def crm_orders() -> FileResponse:
    return _file("orders.html")


@router.get("/crm/workspace/orders", response_class=FileResponse)
def crm_workspace_orders() -> FileResponse:
    return _file("workspace-orders.html")


@router.get("/crm/revenue", response_class=FileResponse)
def crm_revenue() -> FileResponse:
    return _file("revenue.html")


@router.get("/crm/dumping", response_class=FileResponse)
def crm_dumping() -> FileResponse:
    return _file("dumping.html")


@router.get("/crm/suppliers", response_class=FileResponse)
def crm_suppliers() -> FileResponse:
    return _file("suppliers.html")


@router.get("/crm/monitoring", response_class=FileResponse)
def crm_monitoring() -> FileResponse:
    return _file("monitoring.html")
