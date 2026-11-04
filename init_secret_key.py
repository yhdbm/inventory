"""Create this machine's Django SECRET_KEY file. Run ONCE per serving machine.

    python init_secret_key.py            # create if missing
    python init_secret_key.py --force    # rotate (signs everyone out)
    python init_secret_key.py --show     # print the path and whether it exists

Deliberately a STANDALONE script, not a manage.py command: ``manage.py`` loads
``config/settings.py`` before it does anything, and settings is exactly what
fails when the key is missing. A management command could therefore never run in
the one situation it's needed for. This file imports nothing from Django, so it
works on any Python 3 and with no virtualenv set up.

Why the key isn't in settings.py: the project folder lives on the network share
\\\\fileserver\\share, readable by anyone with share access. Sessions are signed
with SECRET_KEY, so whoever can read the key can forge a session cookie —
including one that authenticates as a superuser. So it's stored machine-locally
instead, and settings.py refuses to start without it rather than falling back to
a default.

Each serving machine needs the SAME key value if users are to stay signed in
across them: generate once, then COPY the file (don't re-run this) to the others.
If only one machine serves the app, just run this there.
"""

import os
import secrets
import sys
from pathlib import Path

# Same alphabet Django's get_random_secret_key() uses.
_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*(-_=+)"


def key_path():
    """Resolve the key location exactly the way config/settings.py does."""
    override = os.environ.get("INVENTORY_SECRET_KEY_FILE")
    if override:
        return Path(override)
    base = os.environ.get("PROGRAMDATA") or "C:/ProgramData"
    return Path(base) / "inventory" / "secret_key"


def main(argv):
    force = "--force" in argv
    path = key_path()

    if "--show" in argv:
        print(f"key path : {path}")
        print(f"exists   : {path.exists()}")
        if os.environ.get("INVENTORY_SECRET_KEY"):
            print("note     : INVENTORY_SECRET_KEY is set in the environment and wins "
                  "over this file.")
        return 0

    if path.exists() and not force:
        print(f"A key already exists at {path} — nothing to do.")
        print("Re-run with --force only if you mean to rotate it (that signs everyone out).")
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    key = "".join(secrets.choice(_ALPHABET) for _ in range(50))
    path.write_text(key, encoding="utf-8")

    print(f"Wrote a new SECRET_KEY to {path}")
    print("Keep it off the share and out of version control.")
    if force:
        print("Rotated: all existing sessions are now invalid, so everyone must sign in again.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
