from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0021_intuneimport_fix_persist_by_tag"),
    ]

    operations = [
        migrations.AddField(
            model_name="intunefixassignment",
            name="fixed",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="intunefixassignment",
            name="fixed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
