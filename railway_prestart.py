"""Run before migrate on every Railway deploy (see railway.json).

Railway's container filesystem is rebuilt on each deploy, so the database and
uploads live on a mounted volume instead (INVENTORY_DB_PATH, INVENTORY_MEDIA_ROOT).
A fresh volume is empty: the first time, copy the bundled db.sqlite3 onto it so
the app starts with the demo data. After that the volume copy is the real one and
this script leaves it alone — redeploying never overwrites live data.

Standalone (no Django import), for the same reason as init_secret_key.py.
"""

import os
import shutil
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def main():
    db_path = os.environ.get("INVENTORY_DB_PATH")
    if db_path:
        target = Path(db_path)
        if target.exists():
            print(f"prestart: using existing database {target}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(BASE_DIR / "db.sqlite3", target)
            print(f"prestart: seeded {target} from the bundled db.sqlite3")
    else:
        print("prestart: INVENTORY_DB_PATH not set — using the bundled db.sqlite3 "
              "(changes are LOST on the next deploy)")

    media_root = os.environ.get("INVENTORY_MEDIA_ROOT")
    if media_root:
        Path(media_root).mkdir(parents=True, exist_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
