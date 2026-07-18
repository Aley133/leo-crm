from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from .auth import require_service_token
from .db import Base, get_db
from .monitoring import BindingStatus


class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SupplierProduct(Base):
    __tablename__ = "supplier_products"
    __table_args__ = (UniqueConstraint("supplier_id", "external_id", name="uq_supplier_product_external"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(1000))
    url: Mapped[str] = mapped_column(String(2000))
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    delivery_days: Mapped[int | None] = mapped_column(nullable=True)
    in_stock: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ProductBinding(Base):
    __tablename__ = "product_bindings"
    __table_args__ = (UniqueConstraint("product_id", "supplier_product_id", name="uq_product_binding"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    supplier_product_id: Mapped[int] = mapped_column(ForeignKey("supplier_products.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(
        String(32), default=BindingStatus.CANDIDATE.value, server_default="candidate", index=True
    )
    decision_source: Mapped[str] = mapped_column(String(32), default="manual", server_default="manual")
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    confidence_score: Mapped[int | None] = mapped_column(nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=100, server_default="100")
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_mismatch_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SupplierCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9_-]+$")
    name: str = Field(min_length=2, max_length=255)


class SupplierRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str
    name: str
    is_active: bool
    created_at: datetime


class SupplierProductCreate(BaseModel):
    supplier_id: int
    external_id: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=1000)
    url: HttpUrl
    current_price: Decimal | None = Field(default=None, ge=0)
    delivery_days: int | None = Field(default=None, ge=0, le=365)
    in_stock: bool | None = None


class SupplierProductRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    supplier_id: int
    external_id: str
    title: str
    url: str
    current_price: Decimal | None
    delivery_days: int | None
    in_stock: bool | None
    last_checked_at: datetime | None
    created_at: datetime
    updated_at: datetime


class BindingCreate(BaseModel):
    product_id: int
    supplier_product_id: int
    status: BindingStatus = BindingStatus.CANDIDATE
    decision_source: str = Field(default="manual", pattern=r"^(automatic|manual|imported)$")
    is_primary: bool = False
    confidence_score: int | None = Field(default=None, ge=0, le=100)
    priority: int = Field(default=100, ge=0, le=10_000)


class BindingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    product_id: int
    supplier_product_id: int
    status: str
    decision_source: str
    is_primary: bool
    confidence_score: int | None
    priority: int
    confirmed_at: datetime | None
    last_validated_at: datetime | None
    last_mismatch_reason: str | None
    created_at: datetime
    updated_at: datetime


router = APIRouter(
    prefix="/api",
    tags=["suppliers"],
    dependencies=[Depends(require_service_token)],
)


@router.get("/suppliers", response_model=list[SupplierRead])
def list_suppliers(db: Session = Depends(get_db)):
    return list(db.scalars(select(Supplier).order_by(Supplier.id)).all())


@router.post("/suppliers", response_model=SupplierRead, status_code=status.HTTP_201_CREATED)
def create_supplier(payload: SupplierCreate, db: Session = Depends(get_db)):
    supplier = Supplier(**payload.model_dump())
    db.add(supplier)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Supplier code already exists") from exc
    db.refresh(supplier)
    return supplier


@router.post("/supplier-products", response_model=SupplierProductRead, status_code=status.HTTP_201_CREATED)
def create_supplier_product(payload: SupplierProductCreate, db: Session = Depends(get_db)):
    if db.get(Supplier, payload.supplier_id) is None:
        raise HTTPException(status_code=404, detail="Supplier not found")
    item = SupplierProduct(**payload.model_dump(mode="json"))
    db.add(item)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Supplier product already exists") from exc
    db.refresh(item)
    return item


@router.post("/product-bindings", response_model=BindingRead, status_code=status.HTTP_201_CREATED)
def create_binding(payload: BindingCreate, db: Session = Depends(get_db)):
    from .models import Product

    if db.get(Product, payload.product_id) is None:
        raise HTTPException(status_code=404, detail="Product not found")
    if db.get(SupplierProduct, payload.supplier_product_id) is None:
        raise HTTPException(status_code=404, detail="Supplier product not found")

    values = payload.model_dump(mode="json")
    if values["status"] in {BindingStatus.CONFIRMED.value, BindingStatus.ACTIVE.value}:
        values["confirmed_at"] = func.now()

    binding = ProductBinding(**values)
    db.add(binding)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Binding already exists") from exc
    db.refresh(binding)
    return binding
