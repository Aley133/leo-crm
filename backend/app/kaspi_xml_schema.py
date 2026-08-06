from __future__ import annotations

from xml.etree import ElementTree


def _local_name(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1].lower()


def _qualified_tag(parent: ElementTree.Element, local_name: str) -> str:
    tag = str(parent.tag)
    if tag.startswith("{") and "}" in tag:
        namespace = tag.split("}", 1)[0] + "}"
        return f"{namespace}{local_name}"
    return local_name


def catalog_store_id(root: ElementTree.Element) -> str | None:
    """Return the first non-empty storeId already present in a Kaspi catalog."""
    for node in root.iter():
        if _local_name(node.tag) != "availability":
            continue
        store_id = str(node.attrib.get("storeId") or "").strip()
        if store_id:
            return store_id
    return None


def ensure_offer_availability(offer: ElementTree.Element) -> ElementTree.Element:
    """Return a schema-ordered ``availabilities/availability`` node.

    Older LEO publications could create ``availability`` directly under
    ``offer`` and after ``cityprices``. Kaspi rejects the whole price list in
    that shape. This helper repairs that legacy form, preserves its attributes,
    qualifies new nodes with the catalog namespace and keeps ``availabilities``
    before ``cityprices`` as required by the Seller XML schema.
    """
    children = list(offer)
    availabilities = next(
        (
            child
            for child in children
            if _local_name(child.tag) == "availabilities"
        ),
        None,
    )
    direct_nodes = [
        child for child in children if _local_name(child.tag) == "availability"
    ]

    if availabilities is None:
        availabilities = ElementTree.Element(
            _qualified_tag(offer, "availabilities")
        )
        cityprices_index = next(
            (
                index
                for index, child in enumerate(children)
                if _local_name(child.tag) == "cityprices"
            ),
            len(children),
        )
        offer.insert(cityprices_index, availabilities)
    else:
        availabilities.tag = _qualified_tag(offer, "availabilities")
        current_children = list(offer)
        cityprices_index = next(
            (
                index
                for index, child in enumerate(current_children)
                if _local_name(child.tag) == "cityprices"
            ),
            None,
        )
        availabilities_index = current_children.index(availabilities)
        if cityprices_index is not None and availabilities_index > cityprices_index:
            offer.remove(availabilities)
            offer.insert(cityprices_index, availabilities)

    availability = next(
        (
            child
            for child in list(availabilities)
            if _local_name(child.tag) == "availability"
        ),
        None,
    )
    if availability is not None:
        availability.tag = _qualified_tag(availabilities, "availability")

    for direct in direct_nodes:
        offer.remove(direct)
        direct.tag = _qualified_tag(availabilities, "availability")
        if availability is None:
            availabilities.append(direct)
            availability = direct
        else:
            for key, value in direct.attrib.items():
                availability.attrib.setdefault(key, value)

    if availability is None:
        availability = ElementTree.SubElement(
            availabilities,
            _qualified_tag(availabilities, "availability"),
        )
    return availability


def repair_kaspi_catalog_tree(root: ElementTree.Element) -> ElementTree.Element:
    """Repair malformed availability placement in every offer of a catalog."""
    store_id = catalog_store_id(root)
    for offer in root.iter():
        if _local_name(offer.tag) != "offer":
            continue
        child_names = {_local_name(child.tag) for child in list(offer)}
        if "availability" not in child_names and "availabilities" not in child_names:
            continue
        availability = ensure_offer_availability(offer)
        if store_id and not str(availability.attrib.get("storeId") or "").strip():
            availability.set("storeId", store_id)
    return root


def normalize_kaspi_feed_xml(xml_text: str) -> str:
    """Return Kaspi XML with schema-safe availability containers and order."""
    xml_declaration = xml_text.lstrip().startswith("<?xml")
    root = ElementTree.fromstring(xml_text.encode("utf-8"))
    repair_kaspi_catalog_tree(root)
    return ElementTree.tostring(
        root,
        encoding="unicode",
        xml_declaration=xml_declaration,
    )
