"""Tags of a repository. Each tag is a directory holding current/link and an index of every
digest the tag has pointed at."""

from concurrent.futures import ThreadPoolExecutor
import os

from . import paths
from .driver import PreconditionFailed
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
        """Point tag at digest: link it into the tag's index, then overwrite current/link.
        Returns the ETag of the current/link written."""
        self.blobs.link(paths.manifest_tag_index_entry_link_path(self.name, tag, digest), digest)
        return self.driver.put_content(paths.manifest_tag_current_path(self.name, tag),
                                       digest.encode())

    def drop_current(self, tag, etag):
        """Remove tag's current/link if it is still the version with etag."""
        try:
            self.driver.delete_object(paths.manifest_tag_current_path(self.name, tag),
                                      if_match=etag)
        except (PreconditionFailed, PathNotFoundError):
            return False
        return True

    def get(self, tag):
        try:
            return self.blobs.readlink(paths.manifest_tag_current_path(self.name, tag))
        except (PathNotFoundError, ValueError):
            raise TagUnknown(tag) from None

    def untag(self, tag):
        """Remove the tag directory."""
        self.driver.delete(paths.manifest_tag_path(self.name, tag))

    def _drop_index(self, tag):
        try:
            self.driver.delete(paths.manifest_tag_path(self.name, tag) + "/index")
        except PathNotFoundError:
            pass

    def untag_digest(self, tag, digest):
        """Remove tag if, and only while, it points at digest. A tag directory without a
        current/link is leftover from an interrupted push or delete; its index is removed."""
        current = paths.manifest_tag_current_path(self.name, tag)
        while True:
            try:
                body, etag = self.driver.get_with_etag(current)
            except PathNotFoundError:
                self._drop_index(tag)
                return
            if body.decode(errors="replace").strip() != digest:
                return
            try:
                self.driver.delete_object(current, if_match=etag)
            except (PreconditionFailed, PathNotFoundError):
                continue
            self._drop_index(tag)
            return

    def untag_all(self, digest):
        """Remove every tag that points at digest. Raises on any storage error."""
        try:
            all_tags = self.all()
        except RepositoryUnknown:
            return
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            for _ in pool.map(lambda t: self.untag_digest(t, digest), all_tags):
                pass

    def resolved(self, is_live):
        """Return the tags whose current/link names a live revision."""
        all_tags = self.all()

        def check(tag):
            try:
                target = self.get(tag)
            except TagUnknown:
                return None
            return tag if is_live(target) else None

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            return [t for t in pool.map(check, all_tags) if t is not None]
