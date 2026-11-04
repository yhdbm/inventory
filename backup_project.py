#!/usr/bin/env python
"""Standalone full-project backup.

Zips the ENTIRE project folder (the folder this script lives in) into a dated
archive saved in the PARENT directory — no exclusions. Runs independently of the
Django server; it's launched detached by the app's hidden backup button, but can
also be run by hand:

    python backup_project.py

While zipping it writes to a ``.part`` file and only renames it to the final
``<project>_backup_<date>.zip`` once complete, so a half-written archive never
looks finished. A matching ``.log`` records the outcome.
"""

import os
import sys
import zipfile
from datetime import datetime
from pathlib import Path


def backup_dir(base):
    """Prefer the synced OneDrive folder (so the zip auto-uploads to the cloud);
    fall back to the project's parent folder when OneDrive isn't available."""
    owner = bool(os.environ.get("INVENTORY_BACKUP_TO_ONEDRIVE"))
    for var in ("OneDriveCommercial", "OneDrive", "OneDriveConsumer"):
        root = owner and os.environ.get(var)
        if root and os.path.isdir(root):
            dest = Path(root) / "InventoryBackups"
            try:
                dest.mkdir(parents=True, exist_ok=True)
                return dest
            except OSError:
                pass
    return base.parent


def main():
    base = Path(__file__).resolve().parent
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dest = backup_dir(base)
    final = dest / f"{base.name}_backup_{stamp}.zip"
    log = dest / f"{base.name}_backup_{stamp}.log"
    # Write the temp .part on the LOCAL parent folder so OneDrive doesn't sync a
    # half-written archive; the finished zip is moved into place (same C: volume,
    # so the rename stays atomic).
    part = base.parent / f"{base.name}_backup_{stamp}.zip.part"
    skip = {os.path.abspath(final), os.path.abspath(part)}

    started = datetime.now()
    count = 0
    try:
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(base):
                for f in files:
                    fp = os.path.join(root, f)
                    if os.path.abspath(fp) in skip:
                        continue  # never zip the archive into itself
                    arcname = os.path.join(base.name, os.path.relpath(fp, base))
                    try:
                        zf.write(fp, arcname)
                        count += 1
                    except (OSError, PermissionError):
                        pass  # skip a momentarily-locked/unreadable file, keep going
        os.replace(part, final)
        with open(log, "w", encoding="utf-8") as fh:
            fh.write("Backup OK\n"
                     f"started:  {started:%Y-%m-%d %H:%M:%S}\n"
                     f"finished: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
                     f"files:    {count}\n"
                     f"archive:  {final}\n")
        return 0
    except Exception as exc:  # noqa: BLE001
        try:
            if part.exists():
                part.unlink()
        except OSError:
            pass
        with open(log, "w", encoding="utf-8") as fh:
            fh.write(f"Backup FAILED\nstarted: {started:%Y-%m-%d %H:%M:%S}\nerror: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
