"""Rename the WH2 site from "HR" to "WH2", prefix included.

Supersedes the ``HR`` row seeded by ``0025`` (whose docstring still says HR —
migrations are append-only history, so it is left as written). Everything the
app shows or derives from the WH2 site now uses **WH2**:

* the warehouse ``code`` — navbar brand, page headings, export filenames, the
  staff picker, the impressions report subject, ``set_warehouse <user> WH2``;
* the Station ID prefix — scope matching becomes ``WH2|…`` and the SNOW
  fallback becomes ``u_station_idSTARTSWITHWH2|``.

This is also a better fit for the data than ``HR`` ever was: **45 assets already
carry ``WH2|…`` Station IDs** (``WH2|Production`` ×37, ``WH2|Vortex Cage``
×6, ``WH2|DMS`` ×2) whereas **nothing** was tagged ``HR|``. So the site goes
from 0 tagged / 300 untagged to 45 tagged / 255 untagged without anyone
retagging a single record in ServiceNow.

The row is updated in place, so ``UserProfile.warehouse`` (the WH2 user) and the
``warehouse`` FKs on the snapshot tables keep pointing at it — no reassignment
needed. ``Warehouse.code`` is unique, so the forward step is a no-op if a
"WH2" row somehow already exists.
"""

from django.db import migrations

OLD_CODE = "HR"
NEW_CODE = "WH2"


def forwards(apps, schema_editor):
    Warehouse = apps.get_model("inventory", "Warehouse")
    if Warehouse.objects.filter(code=NEW_CODE).exists():
        return                                  # already renamed / fresh seed
    Warehouse.objects.filter(code=OLD_CODE).update(
        code=NEW_CODE, station_prefixes=NEW_CODE
    )


def backwards(apps, schema_editor):
    Warehouse = apps.get_model("inventory", "Warehouse")
    if Warehouse.objects.filter(code=OLD_CODE).exists():
        return
    Warehouse.objects.filter(code=NEW_CODE).update(
        code=OLD_CODE, station_prefixes=OLD_CODE
    )


class Migration(migrations.Migration):

    dependencies = [("inventory", "0025_seed_warehouses")]

    operations = [migrations.RunPython(forwards, backwards)]
