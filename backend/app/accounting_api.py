from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from .accounting_exports import build_inventory_xlsx, build_inventory_xml
from .accounting_service import (
    build_accounting_report,
    build_capital_position,
    build_inventory_snapshot,
    record_capital_snapshot,
)
from .auth import require_service_token
from .db import get_db
from .workspace_context import current_workspace_id


router = APIRouter(
    prefix="/api/accounting",
    tags=["accounting"],
    dependencies=[Depends(require_service_token)],
)


class CapitalSnapshotWrite(BaseModel):
    cash_balance_kzt: Decimal = Field(
        ge=0,
        le=Decimal("9999999999999999.99"),
        max_digits=18,
        decimal_places=2,
    )
    free_capital_kzt: Decimal = Field(
        ge=0,
        le=Decimal("9999999999999999.99"),
        max_digits=18,
        decimal_places=2,
    )
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def free_capital_is_within_cash(self) -> "CapitalSnapshotWrite":
        if self.free_capital_kzt > self.cash_balance_kzt:
            raise ValueError("free_capital_kzt cannot exceed cash_balance_kzt")
        return self


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


@router.post("/capital-snapshot", status_code=201)
def create_capital_snapshot(
    payload: CapitalSnapshotWrite,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    workspace_id = current_workspace_id()
    record_capital_snapshot(
        db,
        workspace_id=workspace_id,
        cash_balance_kzt=payload.cash_balance_kzt,
        free_capital_kzt=payload.free_capital_kzt,
        note=payload.note,
    )
    inventory = build_inventory_snapshot(
        db,
        workspace_id=workspace_id,
        include_zero=True,
    )
    position = build_capital_position(
        db,
        workspace_id=workspace_id,
        inventory=inventory,
    )
    db.commit()
    return position


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
