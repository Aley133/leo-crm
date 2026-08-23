"""LEO CRM backend package."""

import os


if os.getenv("DATABASE_URL"):
    from .kaspi_xml_runtime_patch import install_kaspi_xml_schema_patch

    install_kaspi_xml_schema_patch()

# Fast Dumping owns realtime offer state (price + stockCount + preOrder) for
# enabled Fast policies. Install the service compatibility layer before routers
# import its functions, then install the XML ownership guard so hourly/classic
# XML writers cannot roll a realtime-managed SKU back to stale state.
from .fast_dumping_offer_runtime import install_fast_dumping_offer_runtime
from .fast_dumping_xml_guard import install_fast_dumping_xml_guard

install_fast_dumping_offer_runtime()
install_fast_dumping_xml_guard()
