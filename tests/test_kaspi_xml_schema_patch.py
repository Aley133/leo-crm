from decimal import Decimal
from types import SimpleNamespace
from xml.etree import ElementTree

from backend.app.dumping_api import Response
from backend.app.dumping_service import update_feed_xml
from backend.app.kaspi_xml_schema import normalize_kaspi_feed_xml


SOURCE_XML = """<?xml version='1.0' encoding='utf-8'?>
<kaspi_catalog xmlns='kaspiShopping'>
  <offers>
    <offer sku='BASE'>
      <model>Base</model>
      <brand>base</brand>
      <availabilities>
        <availability available='yes' storeId='11843018_041600' preOrder='0' stockCount='1'/>
      </availabilities>
      <cityprices><cityprice cityId='196220100'>1000</cityprice></cityprices>
    </offer>
    <offer sku='BROKEN'>
      <model>Broken</model>
      <brand>broken</brand>
      <cityprices><cityprice cityId='196220100'>2000</cityprice></cityprices>
      <availability available='yes' preOrder='5' stockCount='2'/>
    </offer>
  </offers>
</kaspi_catalog>
"""


def _local_name(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _offer(root: ElementTree.Element, sku: str) -> ElementTree.Element:
    return next(
        node
        for node in root.iter()
        if _local_name(node.tag) == "offer" and node.attrib.get("sku") == sku
    )


def _availability(offer: ElementTree.Element) -> ElementTree.Element:
    return next(
        node for node in offer.iter() if _local_name(node.tag) == "availability"
    )


def test_normalizer_repairs_direct_availability_and_schema_order() -> None:
    normalized = normalize_kaspi_feed_xml(SOURCE_XML)
    root = ElementTree.fromstring(normalized.encode("utf-8"))
    broken = _offer(root, "BROKEN")

    child_names = [_local_name(child.tag) for child in list(broken)]
    assert child_names == ["model", "brand", "availabilities", "cityprices"]
    assert "availability" not in child_names
    assert _availability(broken).attrib["storeId"] == "11843018_041600"


def test_missing_managed_offer_is_created_in_kaspi_xsd_order() -> None:
    product = SimpleNamespace(
        merchant_sku="NEW-SKU",
        kaspi_product_id="123456789",
        name="New managed product",
        brand="new-brand",
    )

    generated = update_feed_xml(
        SOURCE_XML,
        sku_candidates={"NEW-SKU", "123456789"},
        price_kzt=Decimal("6411"),
        preorder_days=0,
        stock_count=35,
        product=product,
        city_id="196220100",
    )

    root = ElementTree.fromstring(generated.encode("utf-8"))
    created = _offer(root, "NEW-SKU")
    child_names = [_local_name(child.tag) for child in list(created)]

    assert child_names == ["model", "brand", "availabilities", "cityprices"]
    availability = _availability(created)
    assert availability.attrib == {
        "storeId": "11843018_041600",
        "available": "yes",
        "preOrder": "0",
        "stockCount": "35",
    }
    cityprice = next(
        node for node in created.iter() if _local_name(node.tag) == "cityprice"
    )
    assert cityprice.attrib["cityId"] == "196220100"
    assert cityprice.text == "6411"


def test_public_xml_response_repairs_previously_saved_malformed_feed() -> None:
    response = Response(
        content=SOURCE_XML,
        media_type="application/xml; charset=utf-8",
    )
    root = ElementTree.fromstring(response.body)
    broken = _offer(root, "BROKEN")

    assert [_local_name(child.tag) for child in list(broken)] == [
        "model",
        "brand",
        "availabilities",
        "cityprices",
    ]
    assert _availability(broken).attrib["storeId"] == "11843018_041600"
