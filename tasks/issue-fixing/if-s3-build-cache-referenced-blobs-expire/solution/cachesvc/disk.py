"""Local disk tier (port of bazel-remote's cache/disk), with the S3 proxy behind it.

CAS blobs are kept as files under <dir>/cas/<hash[:2]>/<hash> and indexed in a size-bounded LRU.
The directory is a persistent volume: on startup the index is rebuilt from the files already
there, oldest first.

Action-cache entries are not kept on local disk. They are small, and every replica must agree
with the bucket about which ones exist, so AC reads and writes always go to S3.
"""

import hashlib
import logging
import os
import tempfile
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from .s3proxy import AC, CAS

log = logging.getLogger("cachesvc.disk")
access_log = logging.getLogger("cachesvc.access")

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class BadRequest(Exception):
    pass


class DiskCache:
    def __init__(self, directory, max_size, proxy=None, proxy_contains_workers=16):
        self.dir = directory
        self.max_size = max_size
        self.proxy = proxy
        self._mu = threading.Lock()
        self._lru = OrderedDict()  # hash -> size, least recently used first
        self._size = 0
        os.makedirs(os.path.join(self.dir, "cas"), exist_ok=True)
        os.makedirs(os.path.join(self.dir, "tmp"), exist_ok=True)
        self._load_existing()
        # Bounded pool for proxy lookups of local misses (upstream: containsQueue workers).
        self._contains_pool = ThreadPoolExecutor(max_workers=proxy_contains_workers,
                                                 thread_name_prefix="proxy-contains")

    # -- index ------------------------------------------------------------------------------

    def _path(self, hash_):
        return os.path.join(self.dir, "cas", hash_[:2], hash_)

    def _load_existing(self):
        found = []
        root = os.path.join(self.dir, "cas")
        for sub in os.listdir(root):
            subdir = os.path.join(root, sub)
            if not os.path.isdir(subdir):
                continue
            for name in os.listdir(subdir):
                st = os.stat(os.path.join(subdir, name))
                found.append((st.st_atime, name, st.st_size))
        found.sort()
        for _, name, size in found:
            self._lru[name] = size
            self._size += size
        if found:
            log.info("loaded %d existing blobs (%d bytes) from %s", len(found), self._size, root)
        self._evict_locked()

    def _evict_locked(self):
        while self._size > self.max_size and self._lru:
            hash_, size = self._lru.popitem(last=False)
            self._size -= size
            try:
                os.remove(self._path(hash_))
            except FileNotFoundError:
                pass

    def _lookup_locked(self, hash_):
        size = self._lru.get(hash_)
        if size is not None:
            self._lru.move_to_end(hash_)
        return size

    def _local_hit(self, hash_):
        # Hits answered here never reach S3; tell the proxy so the blob's S3 copy stays alive.
        note = getattr(self.proxy, "note_local_hit", None)
        if note is not None:
            note(hash_)

    def stats(self):
        with self._mu:
            return {"items": len(self._lru), "bytes": self._size, "max_bytes": self.max_size}

    # -- lookups ----------------------------------------------------------------------------

    def contains(self, kind, hash_, size=-1):
        if kind == CAS and size <= 0 and hash_ == EMPTY_SHA256:
            return True, 0
        if kind == CAS:
            with self._mu:
                found = self._lookup_locked(hash_)
            if found is not None and (size < 0 or size == found):
                self._local_hit(hash_)
                return True, found
        if self.proxy is not None:
            exists, found = self.proxy.contains(kind, hash_)
            if exists and (size < 0 or found < 0 or size == found):
                return True, found
        return False, -1

    def find_missing(self, digests):
        """Return the subset of digests ({"hash", "size"}) that the cache does not have.

        Local index first; only local misses are looked up in the proxy, concurrently.
        """
        local_misses = []
        local_hits = []
        with self._mu:
            for d in digests:
                if d["size"] == 0 and d["hash"] == EMPTY_SHA256:
                    continue
                found = self._lookup_locked(d["hash"])
                if found is not None and found == d["size"]:
                    access_log.info("CAS HEAD %s OK", d["hash"])
                    local_hits.append(d["hash"])
                    continue
                local_misses.append(d)
        for hash_ in local_hits:
            self._local_hit(hash_)

        if not local_misses or self.proxy is None:
            return local_misses

        def check(d):
            exists, found = self.proxy.contains(CAS, d["hash"])
            return exists and (found < 0 or found == d["size"])

        results = list(self._contains_pool.map(check, local_misses))
        return [d for d, present in zip(local_misses, results) if not present]

    def get(self, kind, hash_):
        """Return bytes or None."""
        if kind == AC:
            return self.proxy.get(AC, hash_) if self.proxy is not None else None

        with self._mu:
            found = self._lookup_locked(hash_)
        if found is not None:
            try:
                with open(self._path(hash_), "rb") as f:
                    data = f.read()
                self._local_hit(hash_)
                return data
            except FileNotFoundError:
                with self._mu:
                    if self._lru.pop(hash_, None) is not None:
                        self._size -= found

        if self.proxy is None:
            return None
        data = self.proxy.get(CAS, hash_)
        if data is None:
            return None
        if hashlib.sha256(data).hexdigest() == hash_:
            self._commit(hash_, data)
        return data

    # -- writes -----------------------------------------------------------------------------

    def put(self, kind, hash_, data):
        if kind == AC:
            if self.proxy is not None:
                self.proxy.put(AC, hash_, data)
            return

        if hashlib.sha256(data).hexdigest() != hash_:
            raise BadRequest("hash mismatch")
        if len(data) > self.max_size:
            raise BadRequest("blob larger than the disk cache")
        self._commit(hash_, data)
        if self.proxy is not None:
            self.proxy.put(CAS, hash_, data)

    def _commit(self, hash_, data):
        final = self._path(hash_)
        os.makedirs(os.path.dirname(final), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.join(self.dir, "tmp"))
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, final)
        with self._mu:
            old = self._lru.pop(hash_, None)
            if old is not None:
                self._size -= old
            self._lru[hash_] = len(data)
            self._size += len(data)
            self._evict_locked()
