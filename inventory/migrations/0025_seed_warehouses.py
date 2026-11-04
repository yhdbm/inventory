"""Seed the two sites and attribute the existing data to WH1.

The app was single-site before this, so everything already in the database
belongs to Cedarport Main / Annex — with one exception noted below.

The WH1 row reproduces the exact filter the views hard-coded until now
(``location__iregex=r"\\bCPT\\b"`` OR ``location__icontains="Annex"``) plus the
``WH1|`` Station ID prefix, so the Station report's asset set is unchanged
apart from ~23 assets tagged ``WH1|…`` while sitting at another location,
which now (correctly) count as WH1.

HR has no ServiceNow location sys_id here: the GUIDs are not derivable from
anything we store and only the CPT + Annex pair was ever supplied. Until one
is pasted in (admin → Warehouses → HR), HR's SNOW deep links scope themselves
with ``u_station_idSTARTSWITHHR|`` instead — exact for tagged assets, blind to
untagged ones.
"""

from django.db import migrations

# Supplied with the original Main/Annex ServiceNow list URL; see views.py.
SN_MAIN_LOC = "00000000000000000000000000000001"
SN_ANNEX_LOC = "00000000000000000000000000000002"

WAREHOUSES = [
    {
        "code": "WH1",
        "name": "Cedarport Main / Annex",
        "station_prefixes": "WH1",
        "location_regex": r"\bCPT\b|Annex",
        "snow_location_sys_ids": f"{SN_MAIN_LOC},{SN_ANNEX_LOC}",
        "is_default": True,
        "position": 0,
    },
    {
        "code": "HR",
        "name": "Lakeside Rd",
        "station_prefixes": "HR",
        "location_regex": "Lakeside",
        "snow_location_sys_ids": "",
        "is_default": False,
        "position": 1,
    },
]


def forwards(apps, schema_editor):
    Warehouse = apps.get_model("inventory", "Warehouse")
    UserProfile = apps.get_model("inventory", "UserProfile")
    User = apps.get_model("auth", "User")
    ReportSnapshot = apps.get_model("inventory", "ReportSnapshot")
    RfSnapshot = apps.get_model("inventory", "RfSnapshot")

    for spec in WAREHOUSES:
        Warehouse.objects.update_or_create(code=spec["code"], defaults=spec)

    default = Warehouse.objects.get(code="WH1")

    # Every existing snapshot was taken against the Main/Annex scope.
    ReportSnapshot.objects.filter(warehouse__isnull=True).update(warehouse=default)
    RfSnapshot.objects.filter(warehouse__isnull=True).update(warehouse=default)
    # ImpressionSnapshot is deliberately NOT backfilled: that page was
    # cross-site (Main/Annex AND WH2) until now, so its saved readings mix
    # both warehouses and calling them WH1 would be a lie. They stay null and
    # render as "all sites (pre-split)".

    # Existing accounts predate warehouses; pin them to the default explicitly
    # so the assignment is visible in /admin/ rather than implied by a fallback.
    for user in User.objects.all():
        UserProfile.objects.get_or_create(user=user, defaults={"warehouse": default})


def backwards(apps, schema_editor):
    Warehouse = apps.get_model("inventory", "Warehouse")
    ReportSnapshot = apps.get_model("inventory", "ReportSnapshot")
    RfSnapshot = apps.get_model("inventory", "RfSnapshot")

    ReportSnapshot.objects.update(warehouse=None)
    RfSnapshot.objects.update(warehouse=None)
    Warehouse.objects.filter(code__in=[w["code"] for w in WAREHOUSES]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0024_warehouse_userprofile_impressionsnapshot_warehouse_and_more"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
