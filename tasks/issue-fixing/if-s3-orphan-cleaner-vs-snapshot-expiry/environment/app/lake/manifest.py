"""Manifest lists name manifests; manifests name data files. Both are immutable JSON objects."""

import uuid

from .paths import BUCKET_DIR, DATA_PREFIX, MANIFEST_LIST_PREFIX, MANIFEST_PREFIX, manifest_path


def read_manifest_list(fileio, name):
    """Manifest names in a manifest list. FileNotFoundError if the list does not exist."""
    return list(fileio.read_json(manifest_path(name))["manifests"])


def read_manifest(fileio, name):
    """Data file paths (relative to the table) in a manifest. FileNotFoundError if absent."""
    return list(fileio.read_json(manifest_path(name))["files"])


def new_manifest_list_name():
    return f"{MANIFEST_LIST_PREFIX}{uuid.uuid4()}"


def new_manifest_name():
    return f"{MANIFEST_PREFIX}{uuid.uuid4()}"


def new_data_file_path():
    return f"{BUCKET_DIR}{DATA_PREFIX}{uuid.uuid4()}.parquet"


def write_manifest_list(fileio, manifests):
    name = new_manifest_list_name()
    fileio.write_json(manifest_path(name), {"manifests": list(manifests)})
    return name


def write_manifest(fileio, files):
    name = new_manifest_name()
    fileio.write_json(manifest_path(name), {"files": list(files)})
    return name
