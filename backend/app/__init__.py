"""LEO CRM backend package."""

import os


if os.getenv("DATABASE_URL"):
    from .kaspi_xml_runtime_patch import install_kaspi_xml_schema_patch
    from .fast_dumping_offer_runtime import install_fast_dumping_offer_runtime
    from .fast_dumping_supplier_pricing import install_supplier_pricing
    from .fast_dumping_inventory_sync import install_fast_dumping_inventory_sync
    from .fast_dumping_supplier_events import install_fast_dumping_supplier_events
    from .fast_dumping_xml_guard import install_fast_dumping_xml_guard

    install_kaspi_xml_schema_patch()
    install_fast_dumping_offer_runtime()
    install_supplier_pricing()
    install_fast_dumping_inventory_sync()
    install_fast_dumping_supplier_events()
    install_fast_dumping_xml_guard()
