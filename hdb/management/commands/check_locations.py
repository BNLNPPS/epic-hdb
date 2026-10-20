"""
python manage.py check_locations

Lists Locations that violate the building > room > cabinet > shelf
hierarchy (see Location.validate_hierarchy) -- typically legacy rows
created before it was enforced, e.g. a room with no building. Read-only;
exits non-zero if anything is found.
Also lists inventory items stored somewhere other than a room (or a
cabinet/shelf in one). Fix them from the Institutions page
(superuser) or in the Django admin.
"""
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from hdb.models import ComponentInstance, Location


class Command(BaseCommand):
    help = "Report locations that violate the location hierarchy rules"

    def handle(self, *args, **options):
        bad = []
        for loc in Location.objects.select_related("institution", "parent").order_by("institution__name", "name"):
            try:
                loc.validate_hierarchy()
            except ValidationError as exc:
                bad.append((loc, " ".join(exc.messages)))
        # Items must be stored in a room or inside one (not in a building).
        stranded = [i for i in ComponentInstance.objects.filter(location__isnull=False)
                    .select_related("location", "location__institution", "component")
                    if not i.location.can_hold_items()]
        for loc, why in bad:
            self.stdout.write(f"{loc.institution.name} / {loc.name} [{loc.location_type}] ({loc.pk}): {why}")
        for i in stranded:
            self.stdout.write(
                f"Item {i.tag or i.pk} ({i.component.name}, {i.pk}) is stored at "
                f"{i.location.institution.name} / {i.location.name} [{i.location.location_type}], "
                f"not in a room.")
        if bad or stranded:
            raise CommandError(f"{len(bad)} location(s) violate the hierarchy; "
                               f"{len(stranded)} item(s) are not stored in a room.")
        self.stdout.write("All locations satisfy the hierarchy and all items are in rooms.")
