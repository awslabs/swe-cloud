"""Continuous S3 replication of the local database, one generation at a time.

Port of libSQL bottomless' Replicator (bottomless/src/replicator.rs). WAL page frames are
replaced by logical commit records and SQLite's header change counter by the commit seq.
"""
import json
import logging
import os
import sqlite3
import threading
import time

from . import s3 as s3mod
from .generation import generate_generation, is_generation
from .localdb import LocalDb, apply_records, create_empty

log = logging.getLogger("strata.replicator")

# Maximum number of generations that can participate in database restore procedure.
# This effectively means that at least one in [MAX_RESTORE_STACK_DEPTH] number of
# consecutive generations has to have a snapshot included.
MAX_RESTORE_STACK_DEPTH = 100


class RestoreError(Exception):
    pass


class SnapshotMainDbFile:
    """Restore outcome: start a new generation and snapshot the local database into it."""

    def __repr__(self):
        return "SnapshotMainDbFile"


class ReuseGeneration:
    """Restore outcome: keep appending frames to an existing remote generation."""

    def __init__(self, generation, last_frame):
        self.generation = generation
        self.last_frame = last_frame

    def __repr__(self):
        return f"ReuseGeneration({self.generation})"


class Replicator:
    def __init__(self, db_dir, bucket, db_name, client=None):
        self.bucket = bucket
        self.db_name = db_name
        self.db = LocalDb(db_dir)
        self.client = client or s3mod.client()
        self.generation = None
        self.next_frame_no = 1
        self.lock = threading.RLock()
        self._snapshot = None  # (generation, done Event, [error])
        self._background = []

    # ---------------------------------------------------------------- S3 helpers

    def _put(self, key, body):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=body)

    def _get(self, key):
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def _spawn(self, target, name):
        t = threading.Thread(target=target, name=name, daemon=True)
        self._background.append(t)
        t.start()
        return t

    # ---------------------------------------------------------------- generations

    def new_generation(self):
        """Starts a new generation for this replicator instance."""
        curr = generate_generation()
        prev = self.set_generation(curr)
        if prev is not None and prev != curr:
            # try to store dependency between previous and current generation
            log.debug("New generation %s (parent: %s)", curr, prev)
            self.store_dependency(prev, curr)
        return prev

    def set_generation(self, generation, last_frame=0):
        """Sets a generation for this replicator instance. This should be called if a
        generation from S3 is reused in this session."""
        prev = self.generation
        self.generation = generation
        self.next_frame_no = last_frame + 1
        if prev is not None:
            log.debug("Generation changed from %s -> %s", prev, generation)
        else:
            log.debug("Generation set %s", generation)
        return prev

    def store_dependency(self, prev, curr):
        """Request to store dependency between current generation and its predecessor.
        This works asynchronously on best-effort rules, as putting an object to S3 introduces
        an extra undesired latency and this method is called during checkpoint."""
        key = s3mod.dep_key(self.db_name, curr)

        def send():
            try:
                self._put(key, prev.encode("ascii"))
                log.debug("Stored dependency between parent (%s) and child (%s)", prev, curr)
            except Exception as e:
                log.error("Failed to store dependency between generations %s -> %s: %s",
                          prev, curr, e)

        self._spawn(send, f"dep-{curr[:8]}")

    def get_dependency(self, generation):
        try:
            body = self._get(s3mod.dep_key(self.db_name, generation))
        except Exception as e:
            if s3mod.is_no_such_key(e):
                return None
            raise
        parent = body.decode("ascii").strip()
        if not is_generation(parent):
            raise RestoreError(f"generation {generation} has a malformed .dep")
        return parent

    # ---------------------------------------------------------------- write path

    def commit(self, row_id, payload):
        """Ships one commit as a frame batch into the current generation; returns its seq once
        the batch is in S3."""
        with self.lock:
            generation = self.generation
            seq = self.db.seq() + 1
            frame = self.next_frame_no
            record = {"frame": frame, "seq": seq, "id": row_id, "payload": payload}
            body = (json.dumps(record, separators=(",", ":")) + "\n").encode()
            if len(body) > s3mod.MAX_SMALL_OBJECT_BYTES:
                raise ValueError("commit too large for one frame batch")
            key = s3mod.batch_key(self.db_name, generation, frame, frame, int(time.time()))
            self._put(key, body)
            self.db.apply(record)
            self.next_frame_no = frame + 1
            return seq

    def checkpoint(self):
        """Rolls over to a new generation and starts its snapshot upload."""
        with self.lock:
            old = self.generation
            if self.next_frame_no == 1:
                log.debug("No committed changes in this generation, not snapshotting")
                return old, old
            self.wait_until_snapshotted()
            self.new_generation()
            self.snapshot_main_db_file()
            return old, self.generation

    def wait_until_snapshotted(self):
        snap = self._snapshot
        if snap is None or snap[0] != self.generation:
            return False
        log.debug("waiting for generation snapshot %s to complete", snap[0])
        snap[1].wait()
        if snap[2]:
            raise RuntimeError(f"Failed snapshot generation {snap[0]}: {snap[2][0]}")
        return True

    def snapshot_main_db_file(self):
        """Sends the main database file to S3 under the current generation, in the background:
        db.sqlite first, then .changecounter once the snapshot is stored."""
        generation = self.generation
        done, errors = threading.Event(), []
        self._snapshot = (generation, done, errors)
        if self.db.seq() == 0:
            log.debug("Not snapshotting %s, the main db file is empty", generation)
            done.set()
            return None
        data, seq = self.db.snapshot()

        def upload():
            start = time.monotonic()
            try:
                self._put(s3mod.snapshot_key(self.db_name, generation), data)
            except Exception as e:
                log.error("Failed to upload snapshot for generation %s: %s", generation, e)
                errors.append(e)
                done.set()
                return
            try:
                self._put(s3mod.change_counter_key(self.db_name, generation), str(seq).encode())
            except Exception as e:
                log.error("Failed to upload change counter for generation %s: %s", generation, e)
                errors.append(e)
                done.set()
                return
            done.set()
            log.debug("Snapshot upload finished (took %.3fs)", time.monotonic() - start)

        log.debug("Scheduled DB snapshot %s", generation)
        return self._spawn(upload, f"snapshot-{generation[:8]}")

    def drain(self, timeout):
        deadline = time.monotonic() + timeout
        for t in list(self._background):
            t.join(max(0.0, deadline - time.monotonic()))

    # ---------------------------------------------------------------- restore

    def latest_generation(self):
        """Returns the newest replicated generation, or None if there is none."""
        prefix = f"{self.db_name}-"
        keys = s3mod.list_keys(self.client, self.bucket, prefix, max_keys=1)
        if not keys:
            return None
        candidate = keys[0][len(prefix):].split("/", 1)[0]
        return candidate if is_generation(candidate) else None

    def get_remote_change_counter(self, generation):
        """Tries to fetch the remote change counter (snapshot seq) of the given generation."""
        try:
            return int(self._get(s3mod.change_counter_key(self.db_name, generation)))
        except Exception:
            return 0

    def get_last_consistent_frame(self, generation):
        last_frame = 0
        prefix = s3mod.generation_prefix(self.db_name, generation)
        for key in s3mod.list_keys(self.client, self.bucket, prefix):
            rng = s3mod.parse_frame_range(key)
            if rng:
                last_frame = rng[1]
        return last_frame

    def restore(self, generation=None):
        """Restores the database state from the newest remote generation.
        Returns (action, recovered)."""
        if generation is None:
            generation = self.latest_generation()
            if generation is None:
                log.debug("No generation found, nothing to restore")
                return SnapshotMainDbFile(), False
        action, recovered = self.restore_from(generation)
        log.info("Restoring from generation %s: action=%r, recovered=%s",
                 generation, action, recovered)
        return action, recovered

    def restore_from(self, generation):
        last_frame = self.get_last_consistent_frame(generation)
        log.debug("Last consistent remote frame in generation %s: %d.", generation, last_frame)
        action = self.compare_with_local(generation, last_frame)
        if action is not None:
            return action, False

        # at this point we know, we should do a full restore
        restore_path = self.db.path + ".restore"
        try:
            result = self.full_restore(restore_path, generation, last_frame)
        except Exception as e:
            log.error("failed to restore the database: %s. Rollback", e)
            if os.path.exists(restore_path):
                os.remove(restore_path)
            raise
        self.db.replace_with(restore_path)
        return result

    def compare_with_local(self, generation, last_consistent_frame):
        """Compares the generation's backup state against the local database file to determine
        if we are up to date (returned restore action) or should perform a restoration."""
        local_counter = self.db.seq()
        if local_counter != 0:
            # if a non-empty database file exists always treat it as new and more up to date,
            # skipping the restoration process and calling for a new generation to be made
            return SnapshotMainDbFile()

        remote_counter = self.get_remote_change_counter(generation)
        log.debug("Counters: l=%d, r=%d", local_counter, remote_counter)

        # We impersonate as a given generation, since we're comparing against local backup at
        # that generation. This is used later in new_generation to create a dependency between
        # this generation and a new one.
        self.generation = generation
        if local_counter == remote_counter:
            if last_consistent_frame == 0:
                log.info("Remote generation is up-to-date, reusing it in this session")
                return ReuseGeneration(generation, 0)
            return None
        if local_counter > remote_counter:
            return SnapshotMainDbFile()
        return None

    def full_restore(self, restore_path, generation, last_frame):
        create_empty(restore_path)
        restore_stack = []
        current = generation
        while current is not None:
            # stash current generation - we'll use it to replay frames across generations
            # since the last snapshot
            restore_stack.append(current)
            if self.restore_from_snapshot(current, restore_path):
                break
            if len(restore_stack) > MAX_RESTORE_STACK_DEPTH:
                raise RestoreError(
                    "Restoration failed: maximum number of generations to restore from was reached.")
            log.debug("No snapshot found on the generation %s", current)
            # there was no snapshot to restore from, it means that we either:
            # 1. Have only frames to restore from - case when we're at the initial generation
            #    of the database.
            # 2. Snapshot never existed - in that case try to reach for parent generation
            #    of the current one and read snapshot from there.
            current = self.get_dependency(current)
            if current is not None:
                log.debug("Rolling restore back from generation %s to %s",
                          restore_stack[-1], current)

        log.debug("Restoring database from %d generations", len(restore_stack))
        applied = False
        while restore_stack:
            gen = restore_stack.pop()
            # the last generation may still be written to, so constrain it to the frame
            # checked at the beginning of the restore procedure
            limit = last_frame if not restore_stack else None
            applied = self.restore_frames(gen, limit, restore_path) or applied

        if applied:
            log.info("Frames have been applied onto database file in generation %s. "
                     "Requesting snapshot.", generation)
            return SnapshotMainDbFile(), True
        log.info("Reusing generation %s.", generation)
        return ReuseGeneration(generation, last_frame), True

    def restore_from_snapshot(self, generation, path):
        try:
            data = self._get(s3mod.snapshot_key(self.db_name, generation))
        except Exception as e:
            if s3mod.is_no_such_key(e):
                return False
            raise
        with open(path, "wb") as f:
            f.write(data)
        log.debug("Restored snapshot of generation %s", generation)
        return True

    def restore_frames(self, generation, last_consistent_frame, path):
        prefix = s3mod.generation_prefix(self.db_name, generation)
        applied = False
        last_received_frame_no = 0
        conn = sqlite3.connect(path)
        try:
            for key in s3mod.list_keys(self.client, self.bucket, prefix):
                rng = s3mod.parse_frame_range(key)
                if rng is None:
                    continue
                first, last, _ = rng
                if first != last_received_frame_no + 1:
                    log.warning("Missing series of consecutive frames. Last applied frame: %d, "
                                "next found: %d. Stopping the restoration process",
                                last_received_frame_no, first)
                    break
                if last_consistent_frame is not None and last > last_consistent_frame:
                    log.warning("Remote log contains frame %d larger than last consistent frame "
                                "(%d), stopping the restoration process",
                                last, last_consistent_frame)
                    break
                records = [json.loads(line) for line in self._get(key).decode().splitlines()
                           if line.strip()]
                with conn:
                    apply_records(conn, records)
                applied = applied or bool(records)
                last_received_frame_no = last
        finally:
            conn.close()
        return applied
