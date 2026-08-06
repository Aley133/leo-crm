"""LEO CRM backend package."""

import os


if os.getenv("DATABASE_URL"):
    from .kaspi_xml_runtime_patch import install_kaspi_xml_schema_patch

    install_kaspi_xml_schema_patch()
