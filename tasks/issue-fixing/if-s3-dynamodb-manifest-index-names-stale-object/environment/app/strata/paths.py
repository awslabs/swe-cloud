"""Manifest naming.

A dataset `D` keeps one manifest per version under `D/_versions/`. The canonical (final) manifest
of version N is `D/_versions/N.manifest`; a committer first writes its manifest under a unique
staging name `D/_versions/N.manifest-<uuid>`.
"""

import re
import uuid

MANIFEST_EXTENSION = ".manifest"
_FINAL_RE = re.compile(r"/_versions/(\d+)\.manifest$")


def base_uri(bucket, dataset):
    return f"s3://{bucket}/{dataset}"


def manifest_path(dataset, version):
    return f"{dataset}/_versions/{version}{MANIFEST_EXTENSION}"


def make_staging_manifest_path(final_path):
    return f"{final_path}-{uuid.uuid4()}"


def is_finalized(path):
    """True for a canonical manifest path, False for a staging path."""
    return path.endswith(MANIFEST_EXTENSION)


def version_of(path):
    match = _FINAL_RE.search("/" + path)
    return int(match.group(1)) if match else None
