from __future__ import annotations

from dataclasses import replace

from .base import AdapterRequest, NormalizedOffer
from .delivery_normalizer import DeliveryNormalizer
from .ozon_browser_access import OzonBrowserAccessAdapter


class OzonDeliveryAwareAdapter(OzonBrowserAccessAdapter):
    """Ozon adapter that enriches missing structured delivery from visible text."""

    code = "ozon-browser-delivery-v1"

    async def fetch(self, request: AdapterRequest) -> NormalizedOffer:
        offer = await super().fetch(request)
        if offer.delivery_days is not None:
            return offer

        # Ozon often renders delivery only in visible page text while price and
        # availability are available in structured JSON. Reuse the same browser
        # pool and normalize the visible promise without touching domain state.
        response = await self._pool.fetch_html(
            request.url,
            timeout_seconds=self._timeout_seconds,
        )
        delivery_days = DeliveryNormalizer.from_text(response.body_text)
        if delivery_days is None:
            return offer

        metadata = dict(offer.raw_metadata)
        metadata["delivery_source"] = "visible_text_normalized"
        metadata["delivery_response_url"] = response.final_url
        return replace(
            offer,
            delivery_days=delivery_days,
            adapter_schema_version=self.code,
            raw_metadata=metadata,
        )
