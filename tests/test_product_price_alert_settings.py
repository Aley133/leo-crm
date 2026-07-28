from sqlalchemy.orm import Session

from backend.app.models import Product
from backend.app.product_detail_api import (
    ProductPriceAlertUpdate,
    update_product_price_alert,
)


def _product(session: Session) -> Product:
    product = Product(
        kaspi_product_id="ALERT-SETTINGS-001",
        merchant_sku="SKU-ALERT-001",
        name="Карточка для Telegram",
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    return product


def test_product_price_alert_is_disabled_by_default_and_can_be_enabled(
    db_session: Session,
) -> None:
    product = _product(db_session)
    assert product.sudden_price_alert_enabled is False

    result = update_product_price_alert(
        product.id,
        ProductPriceAlertUpdate(enabled=True),
        db_session,
    )

    assert result.enabled is True
    db_session.refresh(product)
    assert product.sudden_price_alert_enabled is True
