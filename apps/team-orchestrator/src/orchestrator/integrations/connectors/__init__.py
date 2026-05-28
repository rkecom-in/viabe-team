"""VT-205 connector implementations namespace.

VT-207 ships ``google_sheet.py``; VT-208 ships ``shopify.py``. Each
concrete connector subclasses ``ConnectorBase`` and matches a
``ConnectorSpec`` entry from the registry.
"""

from orchestrator.integrations.connectors.base import ConnectorBase
from orchestrator.integrations.connectors.google_sheet import GoogleSheetConnector
from orchestrator.integrations.connectors.shopify import ShopifyConnector

__all__ = ["ConnectorBase", "GoogleSheetConnector", "ShopifyConnector"]
