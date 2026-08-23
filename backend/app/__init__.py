"""LEO CRM backend package."""

import os


if os.getenv("DATABASE_URL"):
    from .kaspi_xml_runtime_patch import install_kaspi_xml_schema_patch

    install_kaspi_xml_schema_patch()

# Fast Dumping owns realtime offer state (price + stockCount + preOrder) for
# enabled Fast policies. Install the service compatibility layer before routers
# import its functions, reuse the proven delivery-premium/TOP-5 pricing engine
# for supplier preorder, then guard those SKUs from classic/hourly XML rollback.
from .fast_dumping_offer_runtime import install_fast_dumping_offer_runtime
from .fast_dumping_supplier_pricing import install_supplier_pricing
from .fast_dumping_xml_guard import install_fast_dumping_xml_guard

install_fast_dumping_offer_runtime()
install_supplier_pricing()
install_fast_dumping_xml_guard()
