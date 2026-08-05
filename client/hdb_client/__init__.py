"""
hdb_client — user-scoped programmatic query client for the Component
Database (HDB). Same import surface as the original package; every
sub-client now accepts an optional `user` kwarg (a Django auth User)
that scopes writes and provides the hook for read scoping. See access.py.
"""
from .client import HDBClient
from .locations import LocationClient
from .systems import SystemClient
from .catalog import CatalogClient
from .inventory import InventoryClient
from .designs import DesignClient

__all__ = [
    "HDBClient",
    "LocationClient",
    "SystemClient",
    "CatalogClient",
    "InventoryClient",
    "DesignClient",
]
