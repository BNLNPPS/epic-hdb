import django.db.models.deletion
from django.db import migrations, models


def refuse_if_unlocated(apps, schema_editor):
    """Fail early, with a readable message, instead of letting the database
    reject the NOT NULL change: every inventory item must already have a
    location. Fix any listed item (give it a location in the admin or UI)
    and re-run the migration."""
    ComponentInstance = apps.get_model("hdb", "ComponentInstance")
    missing = list(ComponentInstance.objects.filter(location__isnull=True)
                   .values_list("pk", "tag", "serial_number")[:20])
    if missing:
        total = ComponentInstance.objects.filter(location__isnull=True).count()
        listing = "; ".join(f"{pk} (tag={tag!r}, serial={serial!r})" for pk, tag, serial in missing)
        raise RuntimeError(
            f"Cannot make ComponentInstance.location required: {total} inventory "
            f"item(s) have no location. Assign one to each first. "
            f"First {len(missing)}: {listing}"
        )


class Migration(migrations.Migration):
    """An inventory item always has a location: make the column NOT NULL."""

    dependencies = [
        ("hdb", "0010_remove_designtemplate_owner_user"),
    ]

    operations = [
        migrations.RunPython(refuse_if_unlocated, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="componentinstance",
            name="location",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="instances",
                to="hdb.location",
            ),
        ),
    ]
