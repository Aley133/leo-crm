from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse


STATIC_DIR = Path(__file__).resolve().parent / "static"

router = APIRouter(tags=["crm-ui"], include_in_schema=False)


@router.get("/crm", response_class=FileResponse)
def crm_dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "dashboard.html")
