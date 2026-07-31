from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse


OZON_HOSTS = frozenset({"ozon.ru", "ozon.kz"})
WB_HOSTS = frozenset({"wildberries.ru", "wb.ru"})


class UnsupportedSupplierUrl(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SupplierUrlIdentity:
    supplier_code: str
    supplier_name: str
    external_id: str


def _host_matches(host: str, supported_hosts: frozenset[str]) -> bool:
    return any(host == item or host.endswith(f".{item}") for item in supported_hosts)


def parse_supplier_url(url: str) -> SupplierUrlIdentity:
    """Return the marketplace product identity encoded in a supplier URL.

    Product slugs, query strings and the Ozon country domain are presentation
    details. The marketplace product ID is the durable identity used to prevent
    duplicate SupplierProduct rows.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = parsed.path.rstrip("/")

    if _host_matches(host, OZON_HOSTS):
        match = re.search(
            r"(?:product|context/detail/id)/(?:[^/]*-)?(\d+)(?:/|$)",
            path,
        )
        external_id = match.group(1) if match else path.split("/")[-1]
        if not external_id:
            raise UnsupportedSupplierUrl("Не удалось определить Ozon ID из ссылки")
        return SupplierUrlIdentity("ozon", "Ozon", external_id)

    if _host_matches(host, WB_HOSTS):
        match = re.search(r"/catalog/(\d+)(?:/|$)", path)
        external_id = match.group(1) if match else path.split("/")[-1]
        if not external_id:
            raise UnsupportedSupplierUrl("Не удалось определить WB ID из ссылки")
        return SupplierUrlIdentity("wb", "Wildberries", external_id)

    raise UnsupportedSupplierUrl(
        "Поддерживаются ссылки Ozon (ozon.ru, ozon.kz) и Wildberries"
    )


def canonical_supplier_product_identity(
    *,
    supplier_code: str,
    external_id: str,
    url: str,
) -> str:
    """Return one stable identity for current and legacy supplier rows."""
    normalized_code = supplier_code.strip().casefold()
    try:
        parsed = parse_supplier_url(url)
    except UnsupportedSupplierUrl:
        return external_id.strip().casefold()
    if parsed.supplier_code != normalized_code:
        return external_id.strip().casefold()
    return parsed.external_id.strip().casefold()
