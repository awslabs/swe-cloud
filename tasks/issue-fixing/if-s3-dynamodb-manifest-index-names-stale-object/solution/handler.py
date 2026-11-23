"""Commit handler for datasets whose versions are coordinated through the version index.

The object store has no atomic create-if-absent we can rely on, so concurrent writers coordinate
through the DynamoDB index instead: the index remembers (base_uri, version) -> manifest path and
can run a put-if-not-exists on it. Every manifest committed this way is ultimately materialized
in the object store at its canonical path. See docs/PROTOCOL.md for the commit and reader
protocols.
"""

import logging
from dataclasses import replace

from .errors import CommitConflict, CorruptManifest, NotFound, ObjectNotFound, StrataError
from .index import ManifestLocation
from .paths import base_uri as make_base_uri
from .paths import is_finalized, make_staging_manifest_path, manifest_path, version_of

log = logging.getLogger("strata.handler")


class ManifestCommitHandler:
    def __init__(self, index, objects):
        self.index = index
        self.objects = objects

    def _base_uri(self, dataset):
        return make_base_uri(self.objects.bucket, dataset)

    # ------------------------------------------------------------------ write path

    def _copy_or_verify_final_manifest(self, staging_path, final_path, version, selected_size):
        """Materialize the selected staging manifest at its canonical path.

        Returns the object generation this caller observed at the canonical path. Every helper
        copies the same immutable staging object, so repeated copies are content-idempotent, but
        each one is a new physical generation: the observation is for the caller only.

        A missing staging object means another helper already finalized (and deleted) it, or the
        commit is unrecoverable; then the canonical object must exist with the selected size.
        """
        try:
            self.objects.copy(staging_path, final_path)
        except ObjectNotFound:
            pass
        final_meta = self.objects.head(final_path)
        if final_meta.size != selected_size:
            raise CorruptManifest(
                f"Manifest size mismatch for version {version}: selected staging manifest had "
                f"{selected_size}, object store returned {final_meta.size}"
            )
        return final_meta.generation

    def _publish_final(self, dataset, version, staging_path, size):
        """Point the index at the canonical path, then garbage-collect staging.

        Only generation-independent metadata is published: COPY and this index write are not
        atomic, so a generation observed above can already be superseded by another helper's
        copy when the write lands. Failures here never undo the commit; staging is kept when the
        index write fails so a later reader can repair the row.
        """
        final_path = manifest_path(dataset, version)
        try:
            self.index.put_if_exists(self._base_uri(dataset), version, final_path, size, None)
        except StrataError as error:
            log.warning(
                "Final manifest '%s' is committed, but the index could not be updated; retaining "
                "staging manifest '%s' for repair: %s", final_path, staging_path, error,
            )
            return
        try:
            self.objects.delete(staging_path)
        except StrataError as error:
            log.warning("Failed to delete finalized staging manifest '%s': %s", staging_path, error)

    def _selected_if_ours(self, dataset, version, staging_path, manifest_bytes, error):
        """After a failed reservation: the row, if the version holds this manifest.

        The reservation may have been applied with its reply lost, or this is a rerun of a job
        step whose killed attempt reserved the version under another staging path. Helpers of any
        release may already have finalized that row (0.14 replaces the whole item) and deleted
        the selected staging object, so neither the path nor any attribute we wrote survives:
        the selected bytes themselves are the only identity.
        """
        base_uri = self._base_uri(dataset)
        try:
            recorded = self.index.get_manifest_location(base_uri, version)
        except NotFound:
            raise StrataError(f"commit of {dataset} version {version} failed: {error}") from error
        if recorded.path == staging_path:
            return recorded
        try:
            selected = self.objects.get(recorded.path)
        except ObjectNotFound:
            # The selected staging object was finalized and removed since our read.
            recorded = self.index.get_manifest_location(base_uri, version)
            selected = self.objects.get(manifest_path(dataset, version))
        try:
            self.objects.delete(staging_path)
        except StrataError as delete_error:
            log.warning("Failed to delete unneeded staging manifest '%s': %s", staging_path, delete_error)
        if selected != manifest_bytes:
            raise CommitConflict(f"version {version} of {dataset} is held by another writer") from error
        return recorded

    def commit(self, dataset, version, manifest_bytes):
        base_uri = self._base_uri(dataset)

        # Step 1: write the manifest we want to commit under a temporary name.
        staging_path = make_staging_manifest_path(manifest_path(dataset, version))
        staged = self.objects.put(staging_path, manifest_bytes)

        # Step 2: reserve the version. The reservation selects one immutable staging object;
        # path and size identify it.
        try:
            self.index.put_if_not_exists(base_uri, version, staging_path, staged.size, None)
            selected = ManifestLocation(version, staging_path, staged.size, None)
        except StrataError as error:
            selected = self._selected_if_ours(dataset, version, staging_path, manifest_bytes, error)

        # Steps 3 & 4: materialize (the commit point), then index repair and staging cleanup.
        if is_finalized(selected.path):
            return self.verify_finalized_location(selected)
        return self.finalize_manifest(dataset, selected.path, version, selected.size)

    # ------------------------------------------------------------------ read path

    def verify_finalized_location(self, location):
        try:
            meta = self.objects.head(location.path)
        except ObjectNotFound as error:
            raise NotFound(f"manifest {location.path} is missing from the object store") from error

        size = location.size
        if size is not None and size != meta.size:
            raise CorruptManifest(
                f"Manifest size mismatch for version {location.version}: index row records "
                f"{size}, object store returned {meta.size}"
            )
        if size is None:
            size = meta.size

        # Any generation stored on the row (written by earlier releases) is ignored: another
        # helper's equivalent copy can supersede it after it was published. The HEAD result is
        # returned to the caller as the generation it observed.
        return replace(location, size=size, generation=meta.generation)

    def finalize_manifest(self, dataset, staging_path, version, size):
        """Materialize a reserved version.

        Recording the staging path in the index reserves the version for one immutable manifest.
        The commit becomes authoritative when those bytes are materialized at the canonical
        path. Pointing the row at the canonical path and deleting staging are repair and
        garbage-collection steps; any number of readers or writers may perform them and they
        never roll back an already materialized manifest.
        """
        final_path = manifest_path(dataset, version)
        observed = self._copy_or_verify_final_manifest(staging_path, final_path, version, size)
        self._publish_final(dataset, version, staging_path, size)
        return ManifestLocation(version=version, path=final_path, size=size, generation=observed)

    def resolve_latest_location(self, dataset):
        base_uri = self._base_uri(dataset)
        location = self.index.get_latest_manifest_location(base_uri)

        if location is None:
            # Dataset not found in the index: it may never have committed through it. Search the
            # object store for the latest manifest.
            return self._latest_from_object_store(dataset)

        if is_finalized(location.path):
            return self.verify_finalized_location(location)

        size = location.size
        if size is None:
            try:
                size = self.objects.head(location.path).size
            except ObjectNotFound:
                # there may be other processes that have finished executing finalize_manifest.
                return self.index.get_manifest_location(base_uri, location.version)

        return self.finalize_manifest(dataset, location.path, location.version, size)

    def resolve_version_location(self, dataset, version):
        base_uri = self._base_uri(dataset)
        try:
            location = self.index.get_manifest_location(base_uri, version)
        except NotFound:
            # not on the index yet; go to the object store and onboard the version
            path = manifest_path(dataset, version)
            try:
                meta = self.objects.head(path)
            except ObjectNotFound as error:
                raise NotFound(f"{base_uri}@{version}") from error
            try:
                self.index.put_if_not_exists(base_uri, version, path, meta.size, None)
            except StrataError as error:
                log.warning("could not update the index during load, with error: %s", error)
            return ManifestLocation(version, path, meta.size, meta.generation)

        if is_finalized(location.path):
            return self.verify_finalized_location(location)

        size = location.size
        if size is None:
            size = self.objects.head(location.path).size

        return self.finalize_manifest(dataset, location.path, version, size)

    def _latest_from_object_store(self, dataset):
        versions = [
            v for v in (version_of(key) for key in self.objects.list(f"{dataset}/_versions/"))
            if v is not None
        ]
        if not versions:
            raise NotFound(f"no versions of {dataset}")
        path = manifest_path(dataset, max(versions))
        meta = self.objects.head(path)
        return ManifestLocation(max(versions), path, meta.size, meta.generation)
