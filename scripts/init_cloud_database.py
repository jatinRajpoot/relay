"""Create the cloud database snapshot for a new deployment. Run this once, by hand.

Why this exists
---------------
A service configured with ``DATABASE_BUCKET`` never creates a database. If the
snapshot is missing it fails closed at startup, because "missing" cannot be told
apart from "someone deleted the live object" once Cloud Run's ephemeral disk is
gone. Creating the first snapshot is therefore an explicit, one-shot operator
action, and this script is the only supported way to do it.

Usage (from the repository root, with the same credentials the service uses):

    DATABASE_BUCKET=my-private-bucket python scripts/init_cloud_database.py

The upload uses ``if_generation_match=0`` (create-if-absent). If a snapshot already
exists the command reports that and changes nothing, so running it twice is safe.
It never overwrites, deletes, or migrates an existing database.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relay.store import StorageError, Store


def main() -> int:
    bucket = os.getenv("DATABASE_BUCKET", "").strip()
    if not bucket:
        print("DATABASE_BUCKET is not set; nothing to initialize.", file=sys.stderr)
        return 2
    encryption_key = os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()
    if not encryption_key:
        print("TOKEN_ENCRYPTION_KEY is not set; refusing to create a database.", file=sys.stderr)
        return 2
    database_path = Path(os.getenv("DATABASE_PATH", "data/relay.sqlite3"))
    try:
        created = Store.initialize_cloud_snapshot(database_path, encryption_key)
    except StorageError as error:
        print(f"Initialization failed: {error.detail} (outcome: {error.outcome})", file=sys.stderr)
        return 1
    if created:
        print(f"Created gs://{bucket}/relay.sqlite3. The service can now start.")
    else:
        print(
            f"gs://{bucket}/relay.sqlite3 already exists; nothing was changed. "
            "Initialization never replaces an existing database."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
