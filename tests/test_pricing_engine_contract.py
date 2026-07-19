from datetime import UTC, datetime
from decimal import Decimal

from backend.app.main import app
from backend.app.monitoring import offer_fingerprint
from backend.app.pricing_service import _round_up
from backend.app.supplier_adapters.base import NormalizedOffer


def test_pricing_routes_are_registered() -> None:
    paths = {route.path for route in app.routes}
    assert "/api/pricing/products/{product_id}/policy" in paths
    assert "/api/pricing/fx" in paths
    assert "/api/pricing/products/{product_id}/calculate" in paths
    assert "/api/pricing/products/{product_id}/latest" in paths


def test_rounding_is_always_up_to_configured_step() -> None:
    assert _round_up(Decimal("12345.01"), 100) == Decimal("12400")
    assert _round_up(Decimal("12400"), 100) == Decimal("12400")


def test_offer_currency_changes_fingerprint() -> None:
    common = dict(
        supplier_product_id=7,
        price=Decimal("3734"),
        available=True,
        stock=None,
        delivery_days=2,
        seller="Ozon",
        adapter_schema_version="v1",
    )
    rub = offer_fingerprint(**common, currency="RUB")
    kzt = offer_fingerprint(**common, currency="KZT")
    assert rub != kzt


def test_normalized_offer_validates_and_normalizes_currency() -> None:
    offer = NormalizedOffer(
        supplier_product_id=7,
        price=Decimal("3734"),
        old_price=None,
        available=True,
        stock=None,
        delivery_days=None,
        seller="Ozon",
        adapter_schema_version="v1",
        observed_at=datetime.now(UTC),
        currency="rub",
    )
    assert offer.currency == "RUB"
