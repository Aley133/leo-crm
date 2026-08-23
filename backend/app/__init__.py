"""LEO CRM backend package."""

import os


if os.getenv("DATABASE_URL"):
    from .kaspi_xml_runtime_patch import install_kaspi_xml_schema_patch

    install_kaspi_xml_schema_patch()

# Fast Dumping now owns realtime offer state (price + stockCount + preOrder) for
# enabled Fast policies. Install the compatibility layer before routers import
# the service functions so every API endpoint receives the patched single-flight
# implementation.
from .fast_dumping_offer_runtime import install_fast_dumping_offer_runtime

install_fast_dumping_offer_runtime()
