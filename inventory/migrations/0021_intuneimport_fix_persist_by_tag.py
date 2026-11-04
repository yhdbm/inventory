from django.db import migrations, models


def populate_asset_tag(apps, schema_editor):
    """Backfill asset_tag/serial_number from the (about-to-be-removed) asset FK.

    Ordered newest-first so that if two assignments ever resolved to the same
    normalized tag (duplicate Asset.asset_tag values are possible — no unique
    constraint exists on that field), the most recently assigned one wins and
    older duplicates are dropped rather than violating the new unique
    constraint. Assignments whose asset has no tag can't be keyed at all and
    are dropped too.
    """
    IntuneFixAssignment = apps.get_model("inventory", "IntuneFixAssignment")
    seen = set()
    for row in IntuneFixAssignment.objects.select_related("asset").order_by("-assigned_at"):
        tag = (row.asset.asset_tag or "").strip().upper() if row.asset_id else ""
        if not tag or tag in seen:
            row.delete()
            continue
        seen.add(tag)
        row.asset_tag = tag
        row.serial_number = (row.asset.serial_number or "").strip().upper() if row.asset_id else ""
        row.save(update_fields=["asset_tag", "serial_number"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0020_fixperson_intunefixassignment"),
    ]

    operations = [
        migrations.CreateModel(
            name="IntuneImport",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("filename", models.CharField(max_length=255)),
                ("row_count", models.PositiveIntegerField(default=0)),
                ("imported_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["-imported_at"],
            },
        ),
        migrations.AddField(
            model_name="intunefixassignment",
            name="asset_tag",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="intunefixassignment",
            name="serial_number",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.RunPython(populate_asset_tag, noop_reverse),
        migrations.RemoveField(
            model_name="intunefixassignment",
            name="asset",
        ),
        migrations.AlterField(
            model_name="intunefixassignment",
            name="asset_tag",
            field=models.CharField(db_index=True, max_length=200, unique=True),
        ),
        migrations.AlterField(
            model_name="intunefixassignment",
            name="serial_number",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AlterModelOptions(
            name="intunefixassignment",
            options={"ordering": ["-assigned_at"]},
        ),
    ]
