"""
InventoryClient — query and (permission-checked) mutate Component Inventory.

User-scoped: pass the authenticated Django User in via the constructor.
`create()` requires a user and always derives ownership from it — a
caller can never assign a record to someone else or to a group they
don't belong to (see access.resolve_owner_group_for_create).
"""
from ._bootstrap import _m
from . import access
from . import serializers as ser

DEFAULT_LIMIT = 25
MAX_LIMIT = 100


def _clamp(limit: int) -> int:
    return max(1, min(int(limit), MAX_LIMIT))


class InventoryClient:
    def __init__(self, user=None):
        self.user = user

    def _qs(self):
        qs = _m().ComponentInstance.objects.select_related(
            "component", "technical_system", "location",
            "location__institution", "owner_group", "owner_user",
        )
        return access.visible_to(qs, self.user)

    def all_instances(self):
        """All visible instances (used by the CLI's `find instance` command)."""
        return self._qs()

    def get(self, pk: str):
        return self._qs().get(pk=pk)

    def instances_of(self, component_name: str, limit: int = DEFAULT_LIMIT):
        return list(self._qs().filter(component__name=component_name)[: _clamp(limit)])

    def at_institution(self, abbreviation: str, limit: int = DEFAULT_LIMIT):
        return list(self._qs().filter(location__institution__abbreviation=abbreviation)[: _clamp(limit)])

    def at_location(self, location_name: str, institution_abbr: str | None = None, limit: int = DEFAULT_LIMIT):
        qs = self._qs().filter(location__name=location_name)
        if institution_abbr:
            qs = qs.filter(location__institution__abbreviation=institution_abbr)
        return list(qs[: _clamp(limit)])

    def by_group(self, group_name: str, limit: int = DEFAULT_LIMIT):
        return list(self._qs().filter(owner_group__name=group_name)[: _clamp(limit)])

    def search(self, query: str, limit: int = DEFAULT_LIMIT):
        from django.db.models import Q

        qs = self._qs().filter(
            Q(tag__icontains=query)
            | Q(serial_number__icontains=query)
            | Q(component__name__icontains=query)
        )
        return list(qs[: _clamp(limit)])

    def installed_in_design(self, design_name: str, limit: int = DEFAULT_LIMIT):
        return list(self._qs().filter(installed_at__design__name=design_name)[: _clamp(limit)])

    def institution_summary(self) -> list[dict]:
        from django.db.models import Count

        rows = (
            _m().ComponentInstance.objects.values("location__institution__abbreviation")
            .annotate(count=Count("pk"))
            .order_by("-count")
        )
        return [
            {"institution": r["location__institution__abbreviation"] or "Unknown", "count": r["count"]}
            for r in rows
        ]

    # -- serialized outputs --------------------------------------------

    def search_brief(self, query: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
        return [ser.instance_brief(i) for i in self.search(query, limit)]

    def detail(self, pk: str) -> dict:
        return ser.instance_detail(self.get(pk))

    # -- write path -------------------------------------------------------

    def create(
        self,
        component_name: str | None = None,
        component_pk: str | None = None,
        tag: str = "",
        serial_number: str = "",
        description: str = "",
        location_name: str | None = None,
        owner_group_name: str | None = None,
    ) -> dict:
        """
        Create a ComponentInstance.

        Ownership is NEVER taken from caller input:
          - owner_user is always the authenticated self.user
          - owner_group must be a Group self.user actually belongs to
            (or None), enforced by access.resolve_owner_group_for_create
        Anonymous callers (self.user is None) are rejected outright.
        """
        if self.user is None:
            raise PermissionError("Authentication required to create inventory records.")

        m = _m()
        if not component_name and not component_pk:
            raise ValueError("Supply component_name or component_pk.")
        try:
            component = (
                m.Component.objects.get(pk=component_pk)
                if component_pk
                else m.Component.objects.get(name=component_name)
            )
        except m.Component.DoesNotExist:
            raise ValueError(f"Component not found: {component_pk or component_name!r}")

        location = None
        if location_name:
            try:
                location = m.Location.objects.get(name=location_name)
            except m.Location.DoesNotExist:
                raise ValueError(f"Location not found: {location_name!r}")

        owner_group = access.resolve_owner_group_for_create(owner_group_name, self.user)

        import uuid as _uuid

        inst = m.ComponentInstance(
            id=str(_uuid.uuid4()),
            component=component,
            tag=tag,
            serial_number=serial_number,
            description=description,
            location=location,
            owner_group=owner_group,
            owner_user=self.user,
            group_writeable=bool(owner_group),
        )
        inst.save()
        return ser.instance_detail(inst)
    