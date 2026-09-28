# Adds source-file provenance fields to DesignTemplate -- see the field
# help_text in hdb/models.py and client/hdb_client/designs.py's
# load_templates_from_yaml()/verify_templates_from_yaml() for how they're
# populated and used.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("hdb", "0003_async_template_upload"),
    ]

    operations = [
        migrations.AddField(
            model_name="designtemplate",
            name="source_path",
            field=models.CharField(blank=True, default="", max_length=512),
        ),
        migrations.AddField(
            model_name="designtemplate",
            name="source_sha256",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="designtemplate",
            name="source_git_commit",
            field=models.CharField(blank=True, default="", max_length=40),
        ),
    ]
