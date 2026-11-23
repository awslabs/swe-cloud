"""Manifest and tag API handlers."""

import json
import logging
import uuid

from . import errors
from .blobstore import DIGEST_RE, digest_of
from .driver import PreconditionFailed
from .errors import BlobUnknown, PathNotFoundError, RepositoryUnknown, TagUnknown
from .manifests import ABSENT, DELETED, FENCE_PREFIX, GONE_PREFIX, LIVE, ManifestStore
from .tagstore import TagStore

log = logging.getLogger("quayside.handlers")

OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
DOCKER_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"
MANIFEST_TYPES = {OCI_MANIFEST, OCI_INDEX, DOCKER_MANIFEST, DOCKER_LIST}

MAX_ROUNDS = 8


class Response:
    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self.body = body
        self.headers = headers or {}


def _json(status, obj, headers=None):
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    return Response(status, json.dumps(obj).encode(), hdrs)


def is_digest(reference):
    return bool(DIGEST_RE.match(reference))


def _media_type(content, header):
    try:
        doc = json.loads(content)
    except ValueError as exc:
        raise errors.manifest_invalid(f"invalid JSON: {exc}")
    if not isinstance(doc, dict) or doc.get("schemaVersion") != 2:
        raise errors.manifest_invalid("schemaVersion must be 2")
    media_type = doc.get("mediaType") or header
    if media_type not in MANIFEST_TYPES:
        raise errors.manifest_invalid(f"unsupported media type {media_type!r}")
    return media_type


def _conflict(detail):
    return errors.APIError(409, "UNKNOWN", "concurrent change, retry", detail)


class Registry:
    def __init__(self, blobs, replica_id, concurrency=10):
        self.blobs = blobs
        self.replica_id = replica_id
        self.concurrency = concurrency

    def manifests(self, name):
        return ManifestStore(name, self.blobs)

    def tags(self, name):
        return TagStore(name, self.blobs, self.concurrency)

    # GET /v2/<name>/manifests/<reference>
    def get_manifest(self, name, reference):
        manifests = self.manifests(name)
        digest = reference
        if not is_digest(reference):
            try:
                digest = self.tags(name).get(reference)
            except TagUnknown as exc:
                raise errors.manifest_unknown({"tag": exc.tag})
        try:
            content = manifests.get(digest)
        except BlobUnknown:
            raise errors.manifest_unknown({"digest": digest})
        media_type = _media_type(content, None)
        return Response(200, content, {
            "Content-Type": media_type,
            "Docker-Content-Digest": digest,
        })

    def _make_live(self, manifests, digest):
        """Make the revision link hold digest, taking it back from a delete if necessary."""
        for _ in range(MAX_ROUNDS):
            state, etag = manifests.revision(digest)
            if state == LIVE:
                return
            try:
                if state == ABSENT:
                    manifests.write_revision(digest, digest)
                else:
                    manifests.write_revision(digest, digest, if_match=etag)
                return
            except (PreconditionFailed, PathNotFoundError):
                continue
        raise _conflict({"digest": digest})

    # PUT /v2/<name>/manifests/<reference>
    def put_manifest(self, name, reference, content, content_type):
        _media_type(content, content_type)
        digest = digest_of(content)
        tag = None
        if is_digest(reference):
            if reference != digest:
                raise errors.digest_invalid({"expected": reference, "actual": digest})
        else:
            tag = reference

        manifests = self.manifests(name)
        try:
            manifests.put_blob(content)
            self._make_live(manifests, digest)
            if tag is not None:
                tags = self.tags(name)
                etag = tags.tag(tag, digest)
                # A delete of digest that fenced the revision after we made it live may have
                # scanned the tags before our current/link existed. Only a revision that is
                # still live after the tag write guarantees a later delete will see the tag.
                if not manifests.exists(digest):
                    tags.drop_current(tag, etag)
                    raise _conflict({"digest": digest, "tag": tag,
                                     "reason": "manifest deleted while tagging"})
        except errors.APIError:
            raise
        except Exception as exc:
            log.exception("put %s:%s", name, reference)
            raise errors.unknown(str(exc))

        return Response(201, b"", {
            "Location": f"/v2/{name}/manifests/{digest}",
            "Docker-Content-Digest": digest,
        })

    # DELETE /v2/<name>/manifests/<reference>
    def delete_manifest(self, name, reference):
        if not is_digest(reference):
            try:
                self.tags(name).untag(reference)
            except (TagUnknown, PathNotFoundError):
                raise errors.manifest_unknown({"tag": reference})
            return Response(202)

        digest = reference
        manifests = self.manifests(name)
        tags = self.tags(name)
        try:
            for _ in range(MAX_ROUNDS):
                state, etag = manifests.revision(digest)
                if state in (ABSENT, DELETED):
                    raise errors.manifest_unknown({"digest": digest})
                # LIVE, or DELETING left by a delete that died or is still running: take it.
                token = uuid.uuid4().hex
                try:
                    fence = manifests.write_revision(digest, FENCE_PREFIX + token, if_match=etag)
                except (PreconditionFailed, PathNotFoundError):
                    continue
                tags.untag_all(digest)
                try:
                    manifests.write_revision(digest, GONE_PREFIX + token, if_match=fence)
                except (PreconditionFailed, PathNotFoundError):
                    # A push took the revision back, or another delete took it over.
                    continue
                return Response(202)
        except errors.APIError:
            raise
        except Exception as exc:
            log.exception("delete %s@%s", name, digest)
            raise errors.unknown(str(exc))
        raise _conflict({"digest": digest})

    # GET /v2/<name>/tags/list
    def tags_list(self, name):
        manifests = self.manifests(name)
        live = {}

        def is_live(digest):
            if digest not in live:
                live[digest] = manifests.exists(digest)
            return live[digest]

        try:
            tags = self.tags(name).resolved(is_live)
        except RepositoryUnknown:
            raise errors.name_unknown(name)
        return _json(200, {"name": name, "tags": sorted(tags)})
