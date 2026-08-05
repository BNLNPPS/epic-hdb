"""
DesignClient — query the Design Library.

User-scoped: pass the authenticated Django User in via the constructor.
`user=None` (used by the CLI / trusted shell) applies no scoping.
"""
from ._bootstrap import _m
from . import access
from . import serializers as ser

DEFAULT_LIMIT = 25
MAX_LIMIT = 100
MAX_BOM_DEPTH = 10


def _clamp(limit: int) -> int:
    return max(1, min(int(limit), MAX_LIMIT))


class DesignClient:
    def __init__(self, user=None):
        self.user = user

    def _qs(self):
        qs = _m().Design.objects.select_related("owner_group", "owner_user")
        return access.visible_to(qs, self.user)

    def all_designs(self):
        """All visible designs (used by the CLI's `find design` command)."""
        return self._qs()

    def get(self, name: str | None = None, pk: str | None = None):
        qs = access.visible_to(_m().Design.objects, self.user)
        if pk:
            return qs.get(pk=pk)
        return qs.get(name=name)

    def by_project(self, project: str, limit: int = DEFAULT_LIMIT):
        return list(self._qs().filter(project__iexact=project)[: _clamp(limit)])

    def search(self, query: str, limit: int = DEFAULT_LIMIT):
        from django.db.models import Q

        qs = self._qs().filter(Q(name__icontains=query) | Q(description__icontains=query))
        return list(qs[: _clamp(limit)])

    def elements_of(self, design_name: str):
        return _m().DesignElement.objects.filter(design__name=design_name).select_related(
            "component", "child_design"
        ).prefetch_related("installed_instances__instance")

    def bom(self, design_name: str, _depth: int = 0, _max: int = MAX_BOM_DEPTH) -> list[dict]:
        if _depth > _max:
            return [{"error": "max depth exceeded"}]
        rows = []
        for el in self.elements_of(design_name):
            entry = {
                "element": el.element_name,
                "type": el.element_type(),
                "qty": el.quantity,
                "description": el.description,
            }
            if el.child_design:
                entry["ref"] = el.child_design.name
                entry["children"] = self.bom(el.child_design.name, _depth + 1, _max)
            else:
                entry["ref"] = el.component.name if el.component else None
                entry["model_number"] = el.component.model_number if el.component else None
                # A DesignElement with quantity > 1 can have more than one
                # installed instance (one per slot) -- installed_instances is
                # the plural reverse relation, there is no singular FK here.
                entry["installed_ids"] = [
                    str(dei.instance_id) for dei in el.installed_instances.all()
                ]
                entry["children"] = []
            rows.append(entry)
        return rows

    def flat_component_list(self, design_name: str) -> list[dict]:
        def _flat(rows):
            out = []
            for r in rows:
                if r["type"] == "COMPONENT":
                    out.append(r)
                out.extend(_flat(r.get("children", [])))
            return out

        return _flat(self.bom(design_name))

    def designs_using_component(self, component_name: str, limit: int = DEFAULT_LIMIT):
        return list(self._qs().filter(elements__component__name=component_name).distinct()[: _clamp(limit)])

    # -- serialized outputs --------------------------------------------

    def search_brief(self, query: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
        return [ser.design_brief(d) for d in self.search(query, limit)]

    def summary(self, design_name: str) -> dict:
        return ser.design_detail(self.get(name=design_name), self.bom)
