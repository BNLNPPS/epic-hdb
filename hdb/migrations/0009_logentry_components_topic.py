# Adds the 'components' and 'user' log topics and re-files the automatic catalog log
# entries (component created / ownership transferred / alternate name /
# description) that were previously written under 'other'.

from django.db import migrations, models


AUTO_PREFIXES = (
    "Component ", "Ownership of ", "Alternate name of ", "Description of ",
)


def refile(apps, schema_editor):
    LogEntry = apps.get_model('hdb', 'LogEntry')
    from django.db.models import Q
    q = Q()
    for pre in AUTO_PREFIXES:
        q |= Q(entry__startswith=pre)
    LogEntry.objects.filter(
        topic='other', component__isnull=False,
        component_instance__isnull=True, design__isnull=True,
    ).filter(q).update(topic='components')


def unfile(apps, schema_editor):
    # Only the entries refile() could have touched go back to 'other'; not
    # distinguishable from entries written as 'components' afterwards, so
    # reverse is a no-op beyond the field choices.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('hdb', '0008_protect_design_fks'),
    ]

    operations = [
        migrations.AlterField(
            model_name='logentry',
            name='topic',
            field=models.CharField(blank=True, choices=[('', 'General'), ('installation', 'Installation'), ('inventory', 'Inventory'), ('components', 'Components'), ('user', 'User'), ('design', 'Design'), ('maintenance', 'Maintenance'), ('inspection', 'Inspection'), ('repair', 'Repair'), ('decommission', 'Decommission'), ('other', 'Other')], default='', max_length=32),
        ),
        migrations.RunPython(refile, unfile),
    ]
