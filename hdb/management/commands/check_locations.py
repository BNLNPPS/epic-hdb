"""
python manage.py check_locations

Lists Locations that violate the building > room > cabinet > shelf
hierarchy (see Location.validate_hierarchy) -- typically legacy rows
created before it was enforced, e.g. a room with no building. Read-only;
exits non-zero if anything is found. Fix them from the Institutions page
(superuser) or in the Django admin.
"""
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from hdb.models import Location


class Command(BaseCommand):
    help = "Report locations that violate the location hierarchy rules"

    def handle(self, *args, **options):
        bad = []
        for loc in Location.objects.select_related("institution", "parent").order_by("institution__name", "name"):
            try:
                loc.validate_hierarchy()
            except ValidationError as exc:
                bad.append((loc, " ".join(exc.messages)))
        for loc, why in bad:
            self.stdout.write(f"{loc.institution.name} / {loc.name} [{loc.location_type}] ({loc.pk}): {why}")
        if bad:
            raise CommandError(f"{len(bad)} location(s) violate the hierarchy.")
        self.stdout.write("All locations satisfy the hierarchy.")
