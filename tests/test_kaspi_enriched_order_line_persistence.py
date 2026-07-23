from __future__ import annotations

from sqlalchemy import select

from backend.app.marketplace_import import import_kaspi_order
from backend.app.models import MarketplaceAccount, MarketplaceOrder, MarketplaceProvider


def _account(db_session) -> MarketplaceAccount:
    account = MarketplaceAccount(
        provider=MarketplaceProvider.KASPI.value,
        external_account_id="partner-enrichment",
        display_name="Kaspi enrichment test",
    )
    db_session.add(account)
    db_session.flush()
    return account


def _payload(*, line_id: str, title: str, sku: str | None, product_id: str | None) -> dict:
    attributes = {
        "code": "1007942113",
        "status": "ACCEPTED_BY_MERCHANT",
        "revision": line_id,
        "currency": "KZT",
        "totalPrice": 1499,
        "creationDate": "2026-07-22T17:48:00Z",
        "entries": [
            {
                "id": line_id,
                "attributes": {
                    "name": title,
                    "quantity": 1,
                    "basePrice": 1499,
                    "totalPrice": 1499,
                },
            }
        ],
    }
    if sku is not None:
        attributes["entries"][0]["attributes"]["offerCode"] = sku
    if product_id is not None:
        attributes["entries"][0]["attributes"]["productId"] = product_id
    return {"id": "order-internal-1007942113", "attributes": attributes}


def test_raw_reimport_cannot_downgrade_enriched_title_or_sku(db_session) -> None:
    account = _account(db_session)

    first = import_kaspi_order(
        db_session,
        marketplace_account_id=account.id,
        payload=_payload(
            line_id="entry-real-1",
            title="Органайзер пластик",
            sku="854792406",
            product_id="master-product-1",
        ),
    )
    db_session.commit()

    import_kaspi_order(
        db_session,
        marketplace_account_id=account.id,
        payload=_payload(
            line_id="order-internal-1007942113:0",
            title="Unknown product",
            sku=None,
            product_id=None,
        ),
    )
    db_session.commit()

    order = db_session.scalar(select(MarketplaceOrder).where(MarketplaceOrder.id == first.order_id))
    assert order is not None
    assert len(order.lines) == 1
    line = order.lines[0]
    assert line.title == "Органайзер пластик"
    assert line.merchant_sku == "854792406"
    assert line.external_product_id == "master-product-1"


def test_single_line_order_is_merged_when_kaspi_changes_entry_identity(db_session) -> None:
    account = _account(db_session)

    first = import_kaspi_order(
        db_session,
        marketplace_account_id=account.id,
        payload=_payload(
            line_id="temporary-line",
            title="Unknown product",
            sku=None,
            product_id=None,
        ),
    )
    db_session.commit()

    import_kaspi_order(
        db_session,
        marketplace_account_id=account.id,
        payload=_payload(
            line_id="entry-real-1",
            title="Органайзер пластик",
            sku="854792406",
            product_id="master-product-1",
        ),
    )
    db_session.commit()

    order = db_session.get(MarketplaceOrder, first.order_id)
    assert order is not None
    assert len(order.lines) == 1
    assert order.lines[0].external_line_id == "entry-real-1"
    assert order.lines[0].title == "Органайзер пластик"
    assert order.lines[0].merchant_sku == "854792406"
