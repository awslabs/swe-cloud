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

    def _put(self, dataset, version, staging_path, size, generation):
        """Claim `version` for the staged manifest and return its final location."""
        base_uri = self._base_uri(dataset)

        # Step 1: record the staging path atomically.
        self.index.put_if_not_exists(base_uri, version, staging_path, size, generation)

        # Step 2: copy staging to the final path.
        final_path = manifest_path(dataset, version)
        try:
            self.objects.copy(staging_path, final_path)
            copied = True
        except ObjectNotFound:
            copied = False

        # A copy creates a new object whose metadata may differ from the source.
        # Read the destination metadata before publishing the final path.
        final_meta = self.objects.head(final_path)
        location = ManifestLocation(
            version=version,
            path=final_path,
            size=final_meta.size,
            generation=final_meta.generation,
        )
        if not copied:
            return location

        # Step 3: point the index at the final path.
        self.index.put_if_exists(base_uri, version, final_path, final_meta.size, final_meta.generation)

        # Step 4: delete the staging manifest.
        try:
            self.objects.delete(staging_path)
        except ObjectNotFound:
            pass
        return location

    def commit(self, dataset, version, manifest_bytes):
        base_uri = self._base_uri(dataset)

        # Step 1: write the manifest we want to commit under a temporary name.
        staging_path = make_staging_manifest_path(manifest_path(dataset, version))
        staged = self.objects.put(staging_path, manifest_bytes)

        # Steps 2 & 3: put the manifest to the index.
        try:
            return self._put(dataset, version, staging_path, staged.size, staged.generation)
        except Exception as error:
            # A different recorded path proves this staging manifest lost the version and is
            # safe to remove. Otherwise the index may have recorded our staging path before its
            # response was lost, so retain it for outcome verification/finalization.
            try:
                recorded = self.index.get_manifest_location(base_uri, version)
            except StrataError:
                recorded = None
            if recorded is not None and recorded.path != staging_path:
                try:
                    self.objects.delete(staging_path)
                except StrataError as delete_error:
                    log.warning("Failed to delete losing staging manifest '%s': %s", staging_path, delete_error)
                raise CommitConflict(f"version {version} of {dataset} is held by another writer") from error
            log.warning(
                "Commit for version %s failed; retaining staging manifest '%s' until the commit "
                "outcome is resolved: %s", version, staging_path, error,
            )
            raise CommitConflict(f"commit of {dataset} version {version} failed: {error}") from error

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

        if location.generation is not None:
            if meta.generation != location.generation:
                raise CorruptManifest(
                    f"Manifest generation mismatch for version {location.version}: index row "
                    f"records {location.generation!r}, object store returned {meta.generation!r}"
                )
            generation = location.generation
        else:
            generation = meta.generation

        return replace(location, size=size, generation=generation)

    def finalize_manifest(self, dataset, staging_path, version, size):
        """Materialize a reserved version.

        The manifest is considered committed once the staging manifest is written to the object
        store and that path is committed to the index.

        However, to fully complete this, the staging manifest should be materialized into the
        final path, the final path should be committed to the index and the staging manifest
        should be deleted. These steps may be completed by any number of readers or writers, so
        care should be taken to ensure that the manifest is not lost nor any errors occur due to
        duplicate operations.
        """
        base_uri = self._base_uri(dataset)

        # step 1: copy the manifest to the final location
        final_path = manifest_path(dataset, version)
        try:
            self.objects.copy(staging_path, final_path)
            copied = True
        except ObjectNotFound:
            copied = False  # Another writer beat us to it.

        # A copy creates a new object whose metadata may differ from the source.
        # Read the destination metadata before publishing the final path.
        final_meta = self.objects.head(final_path)
        location = ManifestLocation(
            version=version,
            path=final_path,
            size=final_meta.size,
            generation=final_meta.generation,
        )
        if not copied:
            return location

        # step 2: flip the index to point to the final location
        self.index.put_if_exists(base_uri, version, location.path, final_meta.size, location.generation)

        # step 3: delete the staging manifest
        try:
            self.objects.delete(staging_path)
        except ObjectNotFound:
            pass
        return location

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
                self.index.put_if_not_exists(base_uri, version, path, meta.size, meta.generation)
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
