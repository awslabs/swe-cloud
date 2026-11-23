"""Tags of a repository. Each tag is a directory holding current/link and an index of every
digest the tag has pointed at."""

from concurrent.futures import ThreadPoolExecutor
import os

from . import paths
from .errors import PathNotFoundError, RepositoryUnknown, TagUnknown


class TagStore:
    def __init__(self, name, blobs, concurrency=10):
        self.name = name
        self.blobs = blobs
        self.driver = blobs.driver
        self.concurrency = concurrency

    def all(self):
        """Return every tag directory in the repository, sorted."""
        try:
            entries = self.driver.list(paths.manifest_tags_path(self.name))
        except PathNotFoundError:
            raise RepositoryUnknown(self.name) from None
        return sorted(os.path.basename(e) for e in entries)

    def tag(self, tag, digest):
        """Point tag at digest: link it into the tag's index, then overwrite current/link."""
        self.blobs.link(paths.manifest_tag_index_entry_link_path(self.name, tag, digest), digest)
        self.blobs.link(paths.manifest_tag_current_path(self.name, tag), digest)

    def get(self, tag):
        try:
            return self.blobs.readlink(paths.manifest_tag_current_path(self.name, tag))
        except PathNotFoundError:
            raise TagUnknown(tag) from None

    def untag(self, tag):
        """Remove the tag directory."""
        self.driver.delete(paths.manifest_tag_path(self.name, tag))

    def lookup(self, digest):
        """Recover the tags which refer to digest. When a manifest is deleted by digest, tag
        entries which point to it need to be recovered to avoid dangling tags.

        The result is a snapshot of each tag's current/link at the moment it was read. A tag
        directory without a current/link is skipped.
        """
        try:
            all_tags = self.all()
        except RepositoryUnknown:
            return []

        def check(tag):
            try:
                target = self.blobs.readlink(paths.manifest_tag_current_path(self.name, tag))
            except PathNotFoundError:
                return None
            return tag if target == digest else None

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            return [t for t in pool.map(check, all_tags) if t is not None]
