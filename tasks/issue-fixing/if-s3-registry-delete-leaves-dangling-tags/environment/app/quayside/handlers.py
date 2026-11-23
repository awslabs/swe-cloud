"""Manifest and tag API handlers."""

from concurrent.futures import ThreadPoolExecutor
import json
import logging

from . import errors
from .blobstore import DIGEST_RE, digest_of
from .errors import BlobUnknown, PathNotFoundError, RepositoryUnknown, TagUnknown
from .manifests import ManifestStore
from .tagstore import TagStore

log = logging.getLogger("quayside.handlers")

OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
DOCKER_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"
MANIFEST_TYPES = {OCI_MANIFEST, OCI_INDEX, DOCKER_MANIFEST, DOCKER_LIST}


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

        self.manifests(name).put(content)

        if tag is not None:
            try:
                self.tags(name).tag(tag, digest)
            except Exception as exc:
                log.exception("tag %s:%s", name, tag)
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
        try:
            manifests.delete(digest)
        except BlobUnknown:
            raise errors.manifest_unknown({"digest": digest})
        except Exception as exc:
            raise errors.unknown(str(exc))

        tags = self.tags(name)
        try:
            referenced = tags.lookup(digest)
        except Exception as exc:
            raise errors.unknown(str(exc))

        def untag(tag):
            try:
                tags.untag(tag)
            except Exception as exc:
                return exc
            return None

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            failures = [e for e in pool.map(untag, referenced) if e is not None]
        for exc in failures:
            log.error("delete %s@%s: untag: %s", name, digest, exc)

        return Response(202)

    # GET /v2/<name>/tags/list
    def tags_list(self, name):
        try:
            tags = self.tags(name).all()
        except RepositoryUnknown:
            raise errors.name_unknown(name)
        return _json(200, {"name": name, "tags": tags})
