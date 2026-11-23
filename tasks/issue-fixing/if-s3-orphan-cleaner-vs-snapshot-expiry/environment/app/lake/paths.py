"""Object layout of a table (see TABLE_FORMAT.md)."""

SNAPSHOT_DIR = "snapshot/"
SNAPSHOT_PREFIX = "snapshot-"
LATEST = SNAPSHOT_DIR + "LATEST"
EARLIEST = SNAPSHOT_DIR + "EARLIEST"
MANIFEST_DIR = "manifest/"
MANIFEST_LIST_PREFIX = "manifest-list-"
MANIFEST_PREFIX = "manifest-"
BUCKET_DIR = "bucket-0/"
DATA_PREFIX = "data-"


def snapshot_path(snapshot_id):
    return f"{SNAPSHOT_DIR}{SNAPSHOT_PREFIX}{snapshot_id}"


def snapshot_id_of(path):
    """The id of a snapshot path, or None for hints and anything else under snapshot/."""
    name = path[len(SNAPSHOT_DIR):] if path.startswith(SNAPSHOT_DIR) else path
    if not name.startswith(SNAPSHOT_PREFIX):
        return None
    suffix = name[len(SNAPSHOT_PREFIX):]
    return int(suffix) if suffix.isdigit() else None


def manifest_path(name):
    """Manifest lists and manifests both live in manifest/ and are referenced by name."""
    return MANIFEST_DIR + name
