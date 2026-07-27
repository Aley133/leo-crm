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
    return FileResponse(STATIC_DIR / "crm-gateway.html", headers=NO_STORE_HEADERS)


@router.get("/crm/workspace", response_class=RedirectResponse)
def crm_workspace_root() -> RedirectResponse:
    return RedirectResponse("/crm/workspace/orders", status_code=307, headers=NO_STORE_HEADERS)


@router.get("/crm/legacy", response_class=FileResponse)
def crm_legacy_dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "dashboard.html", headers=NO_STORE_HEADERS)


@router.get("/crm/products", response_class=FileResponse)
def crm_products() -> FileResponse:
    return FileResponse(STATIC_DIR / "products.html", headers=NO_STORE_HEADERS)


@router.get("/crm/products/{product_id}", response_class=FileResponse)
def crm_product_detail(product_id: int) -> FileResponse:
    return FileResponse(STATIC_DIR / "product-detail.html", headers=NO_STORE_HEADERS)


@router.get("/crm/orders", response_class=FileResponse)
def crm_orders() -> FileResponse:
    return FileResponse(STATIC_DIR / "orders.html", headers=NO_STORE_HEADERS)


@router.get("/crm/workspace/orders", response_class=FileResponse)
def crm_workspace_orders() -> FileResponse:
    return FileResponse(STATIC_DIR / "workspace-orders.html", headers=NO_STORE_HEADERS)


@router.get("/crm/revenue", response_class=FileResponse)
def crm_revenue() -> FileResponse:
    return FileResponse(STATIC_DIR / "revenue.html", headers=NO_STORE_HEADERS)


@router.get("/crm/dumping", response_class=FileResponse)
def crm_dumping() -> FileResponse:
    return FileResponse(STATIC_DIR / "dumping.html", headers=NO_STORE_HEADERS)


@router.get("/crm/suppliers", response_class=FileResponse)
def crm_suppliers() -> FileResponse:
    return FileResponse(STATIC_DIR / "suppliers.html", headers=NO_STORE_HEADERS)


@router.get("/crm/monitoring", response_class=FileResponse)
def crm_monitoring() -> FileResponse:
    return FileResponse(STATIC_DIR / "monitoring.html", headers=NO_STORE_HEADERS)
