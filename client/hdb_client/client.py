"""
HDBClient — unified, user-scoped entry point aggregating all domain clients.

    client = HDBClient(user=django_user)   # MCP: always pass the authenticated user
    client = HDBClient()                   # CLI / trusted shell: no scoping

`locations` and `systems` are unchanged from the original client — Location,
Institution, and TechnicalSystem carry no OwnedModel fields, so there's no
ownership to scope.
"""
from .locations import LocationClient
from .systems import SystemClient
from .catalog import CatalogClient
from .inventory import InventoryClient
from .designs import DesignClient

DEFAULT_LIMIT = 15


class HDBClient:
    def __init__(self, user=None):
        self.user = user
        self.locations = LocationClient()
        self.systems = SystemClient()
        self.catalog = CatalogClient(user=user)
        self.inventory = InventoryClient(user=user)
        self.designs = DesignClient(user=user)

    def search_all(self, query: str, limit: int = DEFAULT_LIMIT) -> dict:
        """Cross-domain search: components, instances, and designs."""
        return {
            "components": self.catalog.search_brief(query, limit),
            "instances": self.inventory.search_brief(query, limit),
            "designs": self.designs.search_brief(query, limit),
        }

    def where_is(self, pk: str) -> dict:
        """Location + ownership dict for a single instance, looked up by UUID."""
        inst = self.inventory.get(pk)
        loc = inst.location
        return {
            "id": str(pk),
            "component": inst.component.name if inst.component else None,
            "technical_system": str(inst.technical_system) if inst.technical_system else None,
            "location": str(loc) if loc else None,
            "institution": str(loc.institution) if loc else None,
            "city": loc.institution.city if loc else None,
            "country": loc.institution.country if loc else None,
            "owner_user": inst.owner_user.username if inst.owner_user else None,
            "owner_group": str(inst.owner_group) if inst.owner_group else None,
        }
