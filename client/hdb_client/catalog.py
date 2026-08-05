"""
CatalogClient — query the Component Catalog.

User-scoped: pass the authenticated Django User in via the constructor.
`user=None` (used by the CLI / trusted shell) applies no scoping.
"""
from ._bootstrap import _m
from . import access
from . import serializers as ser

DEFAULT_LIMIT = 25
MAX_LIMIT = 100


def _clamp(limit: int) -> int:
    return max(1, min(int(limit), MAX_LIMIT))


class CatalogClient:
    def __init__(self, user=None):
        self.user = user

    def _qs(self):
        qs = _m().Component.objects.select_related("technical_system", "owner_group", "owner_user")
        return access.visible_to(qs, self.user)

    def all_components(self):
        """All visible components (used by the CLI's `find component` command)."""
        return self._qs()

    def search(self, query: str, limit: int = DEFAULT_LIMIT):
        from django.db.models import Q

        qs = self._qs().filter(
            Q(name__icontains=query)
            | Q(alternate_name__icontains=query)
            | Q(model_number__icontains=query)
            | Q(description__icontains=query)
        )
        return list(qs[: _clamp(limit)])

    def by_technical_system(self, system_name: str, limit: int = DEFAULT_LIMIT):
        return list(self._qs().filter(technical_system__name__iexact=system_name)[: _clamp(limit)])

    def by_project(self, project: str, limit: int = DEFAULT_LIMIT):
        return list(self._qs().filter(project__iexact=project)[: _clamp(limit)])

    def get(self, name: str | None = None, model_number: str | None = None, pk: str | None = None):
        qs = access.visible_to(_m().Component.objects, self.user)
        if pk:
            return qs.get(pk=pk)
        if model_number:
            return qs.get(model_number=model_number)
        return qs.get(name=name)

    def instance_count(self, component_name: str) -> int:
        return _m().ComponentInstance.objects.filter(component__name=component_name).count()

    # -- serialized outputs (what MCP tools call) --------------------------

    def search_brief(self, query: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
        return [ser.component_brief(c) for c in self.search(query, limit)]

    def summary(self, component_name: str) -> dict:
        return ser.component_detail(self.get(name=component_name))
