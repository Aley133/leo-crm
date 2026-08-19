from __future__ import annotations

from urllib.parse import urlparse


_KASPI_IMAGE_HOSTS = (
    "kaspi.kz",
    "kaspi-img.kz",
    "kaspi-images.kz",
    "cdn-kaspi.kz",
)


def normalize_product_image_url(value: str | None) -> str | None:
    """Keep a browser-safe remote image URL without storing image bytes.

    Kaspi changes CDN subdomains over time, so we accept only HTTPS hosts whose
    registrable suffix is a Kaspi-owned image/domain suffix. Data/file URLs and
    credentials in URLs are rejected.
    """

    text = str(value or "").strip()
    if not text or len(text) > 2048:
        return None
    try:
        parsed = urlparse(text)
    except ValueError:
        return None
    host = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme.casefold() != "https" or not host or parsed.username or parsed.password:
        return None
    if not any(host == suffix or host.endswith(f".{suffix}") for suffix in _KASPI_IMAGE_HOSTS):
        return None
    return text
