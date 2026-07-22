from decimal import Decimal

from sqlalchemy import select

from backend.app.fixed_procurement_source_api import (
    FixedProcurementSourceUpsert,
    FixedSourceType,
    upsert_fixed_procurement_source,
)
from backend.app.models import Product
from backend.app.monitoring import MonitorTarget, SupplierOfferState
from backend.app.suppliers import ProductBinding, Supplier, SupplierProduct


def _product(db_session) -> Product:
    product = Product(
        kaspi_product_id="854792406",
        merchant_sku="854792406",
        name="Органайзер пластик",
        status="active",
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)
    return product


def test_offline_source_keeps_fixed_price_without_monitor_target(db_session) -> None:
    product = _product(db_session)

    result = upsert_fixed_procurement_source(
        product.id,
        FixedProcurementSourceUpsert(
            source_type=FixedSourceType.OFFLINE,
            source_name="Аптека рядом",
            price=Decimal("1250.00"),
            delivery_days=0,
            is_primary=True,
        ),
        db_session,
    )

    supplier = db_session.get(Supplier, result.supplier_id)
    supplier_product = db_session.get(SupplierProduct, result.supplier_product_id)
    binding = db_session.get(ProductBinding, result.binding_id)
    state = db_session.scalar(
        select(SupplierOfferState).where(
            SupplierOfferState.supplier_product_id == result.supplier_product_id
        )
    )

    assert supplier is not None and supplier.code.startswith("offline-")
    assert supplier_product is not None
    assert supplier_product.current_price == Decimal("1250.00")
    assert binding is not None and binding.is_primary is True
    assert state is not None and state.price == Decimal("1250.00")
    assert db_session.scalar(select(MonitorTarget)) is None


def test_fixed_source_price_is_editable_and_preserves_identity(db_session) -> None:
    product = _product(db_session)
    payload = FixedProcurementSourceUpsert(
        source_type=FixedSourceType.PRODUCTION,
        source_name="Собственное производство",
        price=Decimal("700.00"),
        delivery_days=1,
        is_primary=True,
    )
    first = upsert_fixed_procurement_source(product.id, payload, db_session)

    second = upsert_fixed_procurement_source(
        product.id,
        payload.model_copy(update={"price": Decimal("850.00")}),
        db_session,
    )

    assert second.supplier_id == first.supplier_id
    assert second.supplier_product_id == first.supplier_product_id
    state = db_session.scalar(
        select(SupplierOfferState).where(
            SupplierOfferState.supplier_product_id == second.supplier_product_id
        )
    )
    assert state is not None
    assert state.old_price == Decimal("700.00")
    assert state.price == Decimal("850.00")
    assert state.adapter_schema_version == "fixed-source-v1"
