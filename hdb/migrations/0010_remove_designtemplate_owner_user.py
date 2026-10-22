from django.db import migrations


class Migration(migrations.Migration):
    """Design templates are owned by a group only; drop the individual owner."""

    dependencies = [
        ('hdb', '0009_logentry_components_topic'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='designtemplate',
            name='owner_user',
        ),
    ]
