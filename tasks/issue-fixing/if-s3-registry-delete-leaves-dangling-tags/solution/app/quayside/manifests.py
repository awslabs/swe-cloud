"""Manifest revisions of a repository: blob data plus a revision link per digest.

The revision link holds the digest while the revision is live. While a delete of the revision is
in progress it holds "deleting:<token>", and after the delete completed "deleted:<token>". Readers
of the layout treat anything but the digest as absent.
"""

from . import paths
from .errors import BlobUnknown, PathNotFoundError

ABSENT, LIVE, DELETING, DELETED = "absent", "live", "deleting", "deleted"
FENCE_PREFIX = "deleting:"
GONE_PREFIX = "deleted:"


class ManifestStore:
    def __init__(self, name, blobs):
        self.name = name
        self.blobs = blobs
        self.driver = blobs.driver

    def _link_path(self, digest):
        return paths.manifest_revision_link_path(self.name, digest)

    def revision(self, digest):
        """Return (state, etag) of the revision link."""
        try:
            body, etag = self.driver.get_with_etag(self._link_path(digest))
        except PathNotFoundError:
            return ABSENT, None
        body = body.decode(errors="replace").strip()
        if body == digest:
            return LIVE, etag
        if body.startswith(FENCE_PREFIX):
            return DELETING, etag
        return DELETED, etag

    def write_revision(self, digest, body, if_match=None):
        return self.driver.put_content(self._link_path(digest), body.encode(), if_match=if_match)

    def stat(self, digest):
        """Raise BlobUnknown unless the repository links digest."""
        if self.revision(digest)[0] != LIVE:
            raise BlobUnknown(digest)

    def exists(self, digest):
        return self.revision(digest)[0] == LIVE

    def get(self, digest):
        self.stat(digest)
        try:
            return self.blobs.get(digest)
        except PathNotFoundError:
            raise BlobUnknown(digest) from None

    def put_blob(self, content):
        return self.blobs.put(content)
