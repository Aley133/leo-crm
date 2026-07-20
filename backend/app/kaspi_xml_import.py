from __future__ import annotations

from dataclasses import dataclass
from xml.etree import ElementTree


MAX_XML_BYTES = 25 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class KaspiXmlProduct:
    kaspi_product_id: str
    merchant_sku: str | None
    name: str
    brand: str | None


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1].lower()


def _clean(value: str | None, *, limit: int) -> str | None:
    if value is None:
        return None
    result = " ".join(value.split()).strip()
    return result[:limit] or None


def _child_text(element: ElementTree.Element, *names: str, limit: int) -> str | None:
    accepted = {name.lower() for name in names}
    for child in element.iter():
        if child is element:
            continue
        if _local_name(child.tag) in accepted:
            value = _clean(child.text, limit=limit)
            if value:
                return value
    return None


def _attribute(element: ElementTree.Element, *names: str, limit: int) -> str | None:
    attrs = {_local_name(key): value for key, value in element.attrib.items()}
    for name in names:
        value = _clean(attrs.get(name.lower()), limit=limit)
        if value:
            return value
    return None


def parse_kaspi_products(xml_bytes: bytes) -> tuple[list[KaspiXmlProduct], list[str]]:
    if not xml_bytes:
        raise ValueError("XML-файл пуст")
    if len(xml_bytes) > MAX_XML_BYTES:
        raise ValueError("XML-файл превышает лимит 25 МБ")
    upper_prefix = xml_bytes[:2048].upper()
    if b"<!DOCTYPE" in upper_prefix or b"<!ENTITY" in upper_prefix:
        raise ValueError("DOCTYPE и внешние XML-сущности запрещены")

    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError as exc:
        raise ValueError(f"Некорректный XML: {exc}") from exc

    products_by_id: dict[str, KaspiXmlProduct] = {}
    warnings: list[str] = []
    offer_number = 0

    for element in root.iter():
        if _local_name(element.tag) not in {"offer", "product", "item"}:
            continue
        offer_number += 1
        attribute_sku = _attribute(element, "sku", limit=128)
        kaspi_id = (
            _attribute(element, "kaspi_product_id", "kaspiid", "productid", "code", "id", limit=64)
            or _child_text(element, "kaspi_product_id", "kaspiid", "productid", "code", "id", "sku", limit=64)
            or attribute_sku
        )
        if not kaspi_id:
            warnings.append(f"Позиция #{offer_number}: отсутствует Kaspi ID/SKU")
            continue

        name = _child_text(element, "model", "name", "title", "productname", limit=500)
        if not name:
            name = kaspi_id
            warnings.append(f"Товар {kaspi_id}: название не найдено, использован Kaspi ID")

        brand = _child_text(element, "brand", "vendor", limit=255)
        merchant_sku = _child_text(element, "merchantsku", "merchant_sku", limit=128) or attribute_sku
        products_by_id[kaspi_id] = KaspiXmlProduct(
            kaspi_product_id=kaspi_id,
            merchant_sku=merchant_sku,
            name=name,
            brand=brand,
        )

    if not products_by_id:
        raise ValueError("В XML не найдено ни одной товарной позиции")
    return list(products_by_id.values()), warnings[:100]
