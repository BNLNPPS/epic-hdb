# Generated for Fix 2 (PROTECT instead of SET_NULL/CASCADE on design FKs)

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('hdb', '0007_alter_location_parent'),
    ]

    operations = [
        migrations.AlterField(
            model_name='designelement',
            name='child_design',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='parent_elements', to='hdb.design'),
        ),
        migrations.AlterField(
            model_name='designelement',
            name='component',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='design_memberships', to='hdb.component'),
        ),
        migrations.AlterField(
            model_name='designtemplateelement',
            name='component',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='template_memberships', to='hdb.component'),
        ),
    ]
