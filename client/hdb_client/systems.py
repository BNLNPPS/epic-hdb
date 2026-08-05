"""
SystemClient — query TechnicalSystem entries and their associated inventory.
"""
from ._bootstrap import _m


class SystemClient:
    """Query TechnicalSystem entries and their associated components/instances."""

    def all(self):
        """All TechnicalSystem rows, ordered by name."""
        return _m().TechnicalSystem.objects.all()

    def get(self, name=None, pk=None):
        m = _m()
        if pk:
            return m.TechnicalSystem.objects.get(pk=pk)
        return m.TechnicalSystem.objects.get(name__iexact=name)

    def components(self, system_name: str):
        """All components assigned to this technical system."""
        return _m().Component.objects.filter(
            technical_system__name__iexact=system_name
        ).select_related("owner_group", "owner_user")

    def instances(self, system_name: str):
        """All component instances directly tagged with this technical system."""
        return _m().ComponentInstance.objects.filter(
            technical_system__name__iexact=system_name
        ).select_related(
            "component", "location", "location__institution",
            "owner_group", "owner_user",
        )

    def instance_counts(self) -> list:
        """List of dicts with component and instance counts per technical system."""
        from django.db.models import Count
        rows = (
            _m().TechnicalSystem.objects
            .annotate(
                component_count=Count("components", distinct=True),
                instance_count=Count("component_instances", distinct=True),
            )
            .order_by("name")
        )
        return [
            {"id":         str(r.pk),
             "name":       r.name,
             "components": r.component_count,
             "instances":  r.instance_count}
            for r in rows
        ]

    def summary(self, system_name: str) -> dict:
        """Plain-dict summary of a technical system."""
        sys_obj = self.get(name=system_name)
        return {
            "id":              sys_obj.pk,
            "name":            sys_obj.name,
            "description":     sys_obj.description,
            "component_count": _m().Component.objects.filter(
                                   technical_system=sys_obj).count(),
            "instance_count":  _m().ComponentInstance.objects.filter(
                                   technical_system=sys_obj).count(),
        }
