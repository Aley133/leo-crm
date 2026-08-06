from __future__ import annotations

from decimal import Decimal
from xml.etree import ElementTree

from fastapi.responses import Response as FastAPIResponse

from .kaspi_xml_schema import (
    catalog_store_id,
    ensure_offer_availability,
    normalize_kaspi_feed_xml,
    repair_kaspi_catalog_tree,
)


_PATCH_INSTALLED = False


def install_kaspi_xml_schema_patch() -> None:
    """Patch XML writers and the public response without rewriting feed history.

    The production database can already contain a generated XML document with
    ``availability`` directly under ``offer``. The response wrapper repairs
    that historical document immediately, while the patched writers guarantee
    that every subsequent publication is stored with the schema-safe
    ``availabilities/availability`` container and ordering.
    """
    global _PATCH_INSTALLED
    if _PATCH_INSTALLED:
        return

    from . import dumping_service as service

    def create_managed_offer(
        root: ElementTree.Element,
        *,
        product,
        city_id: str,
    ) -> ElementTree.Element:
        offers = next(
            (node for node in root.iter() if service._local_name(node.tag) == "offers"),
            root,
        )
        offer = ElementTree.SubElement(
            offers,
            service._qualified_tag(offers, "offer"),
            {"sku": (product.merchant_sku or product.kaspi_product_id).strip()},
        )
        model = ElementTree.SubElement(
            offer,
            service._qualified_tag(offer, "model"),
        )
        model.text = product.name
        if product.brand:
            brand = ElementTree.SubElement(
                offer,
                service._qualified_tag(offer, "brand"),
            )
            brand.text = product.brand

        availabilities = ElementTree.SubElement(
            offer,
            service._qualified_tag(offer, "availabilities"),
        )
        availability_attributes: dict[str, str] = {}
        store_id = catalog_store_id(root)
        if store_id:
            availability_attributes["storeId"] = store_id
        ElementTree.SubElement(
            availabilities,
            service._qualified_tag(availabilities, "availability"),
            availability_attributes,
        )

        cityprices = ElementTree.SubElement(
            offer,
            service._qualified_tag(offer, "cityprices"),
        )
        ElementTree.SubElement(
            cityprices,
            service._qualified_tag(cityprices, "cityprice"),
            {"cityId": city_id},
        )
        return offer

    def update_feed_xml(
        xml_text: str,
        *,
        sku_candidates: set[str],
        price_kzt: Decimal,
        preorder_days: int,
        stock_count: int,
        product=None,
        city_id: str = "750000000",
    ) -> str:
        root = ElementTree.fromstring(xml_text.encode("utf-8"))
        repair_kaspi_catalog_tree(root)
        offer = service._matching_offer(
            root,
            {value for value in sku_candidates if value},
        )
        if offer is None:
            if product is None:
                raise ValueError("Товар не найден в сохранённом XML по SKU/Kaspi ID")
            offer = create_managed_offer(
                root,
                product=product,
                city_id=city_id,
            )

        price_nodes = [
            node
            for node in offer.iter()
            if service._local_name(node.tag) == "cityprice"
        ]
        if not price_nodes:
            raise ValueError("В XML-предложении отсутствуют cityprice")
        rendered_price = (
            str(int(price_kzt))
            if price_kzt == price_kzt.to_integral_value()
            else format(price_kzt, "f")
        )
        for node in price_nodes:
            node.text = rendered_price

        availability = ensure_offer_availability(offer)
        store_id = catalog_store_id(root)
        if store_id and not str(availability.attrib.get("storeId") or "").strip():
            availability.set("storeId", store_id)
        availability.set("available", "yes")
        availability.set("preOrder", str(max(int(preorder_days), 0)))
        availability.set("stockCount", str(max(int(stock_count), 0)))

        return ElementTree.tostring(
            root,
            encoding="unicode",
            xml_declaration=True,
        )

    def set_feed_offer_availability(
        xml_text: str,
        *,
        sku_candidates: set[str],
        available: bool,
        stock_count: int | None = None,
        preorder_days: int | None = None,
    ) -> str:
        root = ElementTree.fromstring(xml_text.encode("utf-8"))
        repair_kaspi_catalog_tree(root)
        offer = service._matching_offer(
            root,
            {value for value in sku_candidates if value},
        )
        if offer is None:
            raise ValueError("Товар не найден в сохранённом XML по SKU/Kaspi ID")

        availability = ensure_offer_availability(offer)
        store_id = catalog_store_id(root)
        if store_id and not str(availability.attrib.get("storeId") or "").strip():
            availability.set("storeId", store_id)
        availability.set("available", "yes" if available else "no")
        if stock_count is not None:
            availability.set("stockCount", str(max(int(stock_count), 0)))
        if not available:
            availability.set("preOrder", "0")
            availability.set("stockCount", "0")
        elif preorder_days is not None:
            availability.set("preOrder", str(max(int(preorder_days), 0)))

        return ElementTree.tostring(
            root,
            encoding="unicode",
            xml_declaration=True,
        )

    service._create_managed_offer = create_managed_offer
    service.update_feed_xml = update_feed_xml
    service.set_feed_offer_availability = set_feed_offer_availability

    from . import dumping_api

    class KaspiXmlResponse(FastAPIResponse):
        def __init__(
            self,
            content=None,
            status_code: int = 200,
            headers=None,
            media_type: str | None = None,
            background=None,
        ) -> None:
            if (
                isinstance(content, str)
                and media_type is not None
                and "xml" in media_type.casefold()
            ):
                try:
                    content = normalize_kaspi_feed_xml(content)
                except ElementTree.ParseError:
                    # Preserve the original response for diagnostics if a feed
                    # is not even well-formed XML. The known production defect
                    # is schema placement, not XML parsing.
                    pass
            super().__init__(
                content=content,
                status_code=status_code,
                headers=headers,
                media_type=media_type,
                background=background,
            )

    dumping_api.Response = KaspiXmlResponse
    _PATCH_INSTALLED = True
