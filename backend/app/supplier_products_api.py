from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import require_service_token
from .db import get_db
from .suppliers import SupplierProduct, SupplierProductRead

router = APIRouter(
    prefix="/api",
    tags=["suppliers"],
    dependencies=[Depends(require_service_token)],
)


@router.get("/supplier-products", response_model=list[SupplierProductRead])
def list_supplier_products(
    supplier_id: int | None = None,
    external_id: str | None = None,
    db: Session = Depends(get_db),
):
    statement = select(SupplierProduct)
    if supplier_id is not None:
        statement = statement.where(SupplierProduct.supplier_id == supplier_id)
    if external_id is not None:
        statement = statement.where(SupplierProduct.external_id == external_id)
    return list(db.scalars(statement.order_by(SupplierProduct.id)).all())
