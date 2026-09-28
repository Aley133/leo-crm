from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from .accounting_exports import build_inventory_xlsx, build_inventory_xml
from .accounting_service import build_accounting_report, build_inventory_snapshot
from .auth import require_service_token
from .db import get_db
from .workspace_context import current_workspace_id


router = APIRouter(
    prefix="/api/accounting",
    tags=["accounting"],
    dependencies=[Depends(require_service_token)],
)


@router.get("/report")
def accounting_report(
    days: int = Query(default=30, ge=0, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return build_accounting_report(
        db,
        workspace_id=current_workspace_id(),
        days=days,
    )


@router.get("/inventory/export")
def export_inventory(
    format: Literal["xml", "xlsx"] = Query(default="xlsx"),
    include_zero: bool = Query(default=True),
    db: Session = Depends(get_db),
) -> Response:
    workspace_id = current_workspace_id()
    generated_at = datetime.now(UTC)
    snapshot = build_inventory_snapshot(
        db,
        workspace_id=workspace_id,
        include_zero=include_zero,
    )
    snapshot.pop("owner_by_product", None)
    snapshot.pop("products_by_id", None)
    date_marker = generated_at.strftime("%Y%m%d")
    common_headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if format == "xml":
        content = build_inventory_xml(snapshot, generated_at=generated_at)
        return Response(
            content=content,
            media_type="application/xml",
            headers={
                **common_headers,
                "Content-Disposition": (
                    f'attachment; filename="leo-inventory-workspace-{workspace_id}-{date_marker}.xml"'
                ),
            },
        )

    content = build_inventory_xlsx(snapshot, generated_at=generated_at)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            **common_headers,
            "Content-Disposition": (
                f'attachment; filename="leo-inventory-workspace-{workspace_id}-{date_marker}.xlsx"'
            ),
        },
    )
