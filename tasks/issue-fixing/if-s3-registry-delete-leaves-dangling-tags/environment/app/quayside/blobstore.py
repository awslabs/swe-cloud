"""Content-addressed blob storage and link files."""

import hashlib
import re

from . import paths

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def digest_of(content):
    return "sha256:" + hashlib.sha256(content).hexdigest()


def parse_digest(value):
    if not DIGEST_RE.match(value):
        raise ValueError(f"invalid digest {value!r}")
    return value


class BlobStore:
    def __init__(self, driver):
        self.driver = driver

    def put(self, content):
        digest = digest_of(content)
        self.driver.put_content(paths.blob_data_path(digest), content)
        return digest

    def get(self, digest):
        return self.driver.get_content(paths.blob_data_path(digest))

    def link(self, path, digest):
        """Write a link file pointing at digest."""
        self.driver.put_content(path, digest.encode())

    def readlink(self, path):
        """Return the digest a link file points at."""
        content = self.driver.get_content(path)
        return parse_digest(content.decode())
