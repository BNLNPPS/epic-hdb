"""
LocationClient — query Institutions and Locations.
"""
from ._bootstrap import _m


class LocationClient:
    """Query Institutions and Locations."""

    # ------------------------------------------------------------------
    # Institutions
    # ------------------------------------------------------------------

    def all_institutions(self):
        """All Institution rows, ordered by name."""
        return _m().Institution.objects.all()

    def get_institution(self, abbreviation=None, name=None):
        m = _m()
        if abbreviation:
            return m.Institution.objects.get(abbreviation=abbreviation)
        return m.Institution.objects.get(name=name)

    def institutions_by_country(self, country: str):
        return _m().Institution.objects.filter(country__iexact=country)

    def users_at_institution(self, abbreviation: str):
        """UserProfile queryset for all users attached to an institution."""
        return _m().UserProfile.objects.filter(
            institution__abbreviation=abbreviation
        ).select_related("user", "institution")

    # ------------------------------------------------------------------
    # Locations
    # ------------------------------------------------------------------

    def all_locations(self):
        return _m().Location.objects.select_related("institution", "parent")

    def locations_at_institution(self, abbreviation: str):
        """All locations belonging to an institution."""
        return _m().Location.objects.filter(
            institution__abbreviation=abbreviation
        ).select_related("parent")

    def buildings(self, institution_abbr=None):
        qs = _m().Location.objects.filter(location_type="building")
        if institution_abbr:
            qs = qs.filter(institution__abbreviation=institution_abbr)
        return qs.select_related("institution")

    def rooms_in_building(self, building_name: str, institution_abbr=None):
        qs = _m().Location.objects.filter(
            location_type="room", parent__name=building_name)
        if institution_abbr:
            qs = qs.filter(institution__abbreviation=institution_abbr)
        return qs

    def location_tree(self, institution_abbr: str) -> list:
        """
        Nested list-of-dicts for the full location hierarchy at an institution.
        Each node: {"id", "name", "type", "children": [...]}.
        """
        locs = list(
            _m().Location.objects.filter(
                institution__abbreviation=institution_abbr
            ).select_related("parent").order_by("location_type", "name")
        )
        id_map = {
            loc.pk: {"id": loc.pk, "name": loc.name,
                     "type": loc.location_type, "children": []}
            for loc in locs
        }
        roots = []
        for loc in locs:
            node = id_map[loc.pk]
            if loc.parent_id and loc.parent_id in id_map:
                id_map[loc.parent_id]["children"].append(node)
            else:
                roots.append(node)
        return roots
