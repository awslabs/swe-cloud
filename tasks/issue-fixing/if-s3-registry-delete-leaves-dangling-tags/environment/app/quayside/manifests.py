"""Manifest revisions of a repository: blob data plus a revision link per digest."""

from . import paths
from .errors import BlobUnknown, PathNotFoundError


class ManifestStore:
    def __init__(self, name, blobs):
        self.name = name
        self.blobs = blobs
        self.driver = blobs.driver

    def _link_path(self, digest):
        return paths.manifest_revision_link_path(self.name, digest)

    def stat(self, digest):
        """Raise BlobUnknown unless the repository links digest."""
        try:
            target = self.blobs.readlink(self._link_path(digest))
        except PathNotFoundError:
            raise BlobUnknown(digest) from None
        if target != digest:
            raise BlobUnknown(digest)

    def exists(self, digest):
        try:
            self.stat(digest)
        except BlobUnknown:
            return False
        return True

    def get(self, digest):
        self.stat(digest)
        try:
            return self.blobs.get(digest)
        except PathNotFoundError:
            raise BlobUnknown(digest) from None

    def put(self, content):
        digest = self.blobs.put(content)
        self.blobs.link(self._link_path(digest), digest)
        return digest

    def delete(self, digest):
        self.stat(digest)
        try:
            self.driver.delete(self._link_path(digest))
        except PathNotFoundError:
            raise BlobUnknown(digest) from None
