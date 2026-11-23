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
from .localdb import LocalDb, apply_records, create_empty, read_state

log = logging.getLogger("strata.replicator")

# Maximum number of generations that can participate in database restore procedure.
# This effectively means that at least one in [MAX_RESTORE_STACK_DEPTH] number of
# consecutive generations has to have a snapshot included.
MAX_RESTORE_STACK_DEPTH = 100

# How long a rollover keeps retrying the parent link before giving up on the rollover.
DEP_RETRY_SECONDS = 20.0

# How long a commit (or a checkpoint) keeps retrying its frame PUT before answering with an
# error; it must answer well inside the 2 s acknowledgement limit.
COMMIT_PUT_BUDGET_SECONDS = 1.5

# A background snapshot upload retries until it is stored (or this long has passed).
SNAPSHOT_RETRY_SECONDS = 600.0


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
        # A frame batch whose PUT failed without a definite answer: (key, body, record). It may
        # be stored already, so it keeps its frame number and is re-sent with the same key and
        # bytes before anything else is written to this generation.
        self._pending = None

    # ---------------------------------------------------------------- S3 helpers

    def _put(self, key, body):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=body)

    def _put_retrying(self, key, body, deadline):
        """PUTs the same key and bytes until it is stored or the deadline passes."""
        delay = 0.05
        while True:
            try:
                self._put(key, body)
                return
            except Exception as e:
                if time.monotonic() + delay >= deadline:
                    raise
                log.warning("PUT %s failed: %s; retrying", key, e)
                time.sleep(delay)
                delay = min(delay * 2, 0.4)

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
        prev = self.generation
        if prev is not None and prev != curr:
            # the parent link must be durable before the generation takes its first frame
            log.debug("New generation %s (parent: %s)", curr, prev)
            self.store_dependency(prev, curr)
        self.set_generation(curr)
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
        """Stores the dependency between the current generation and its predecessor.
        Until its snapshot lands, a generation's state is its parent's state plus its own
        frames, so the link is written (and retried) before any frame is acknowledged in it."""
        key = s3mod.dep_key(self.db_name, curr)
        deadline = time.monotonic() + DEP_RETRY_SECONDS
        delay = 0.05
        while True:
            try:
                self._put(key, prev.encode("ascii"))
                log.debug("Stored dependency between parent (%s) and child (%s)", prev, curr)
                return
            except Exception as e:
                if time.monotonic() >= deadline:
                    raise
                log.warning("Failed to store dependency between generations %s -> %s: %s; "
                            "retrying", prev, curr, e)
                time.sleep(delay)
                delay = min(delay * 2, 1.0)

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

    def _flush_pending(self, deadline):
        """Settles a frame batch whose earlier PUT has no known outcome: re-sends it unchanged
        and applies it locally. Until then nothing else may take its frame number or seq."""
        if self._pending is None:
            return
        key, body, record = self._pending
        self._put_retrying(key, body, deadline)
        self.db.apply(record)
        self.next_frame_no = record["frame"] + 1
        self._pending = None
        log.info("Settled frame %d (seq %d) of an unacknowledged commit", record["frame"],
                 record["seq"])

    def commit(self, row_id, payload):
        """Ships one commit as a frame batch into the current generation; returns its seq once
        the batch is in S3."""
        with self.lock:
            deadline = time.monotonic() + COMMIT_PUT_BUDGET_SECONDS
            self._flush_pending(deadline)
            generation = self.generation
            seq = self.db.seq() + 1
            frame = self.next_frame_no
            record = {"frame": frame, "seq": seq, "id": row_id, "payload": payload}
            body = (json.dumps(record, separators=(",", ":")) + "\n").encode()
            if len(body) > s3mod.MAX_SMALL_OBJECT_BYTES:
                raise ValueError("commit too large for one frame batch")
            key = s3mod.batch_key(self.db_name, generation, frame, frame, int(time.time()))
            try:
                self._put_retrying(key, body, deadline)
            except Exception:
                # The store may hold it anyway (a gateway timeout is not a refusal).
                self._pending = (key, body, record)
                raise
            self.db.apply(record)
            self.next_frame_no = frame + 1
            return seq

    def checkpoint(self):
        """Rolls over to a new generation and starts its snapshot upload."""
        with self.lock:
            old = self.generation
            # the old generation must be settled before its successor's snapshot is taken
            self._flush_pending(time.monotonic() + COMMIT_PUT_BUDGET_SECONDS)
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
            deadline = start + SNAPSHOT_RETRY_SECONDS
            for key, body in ((s3mod.snapshot_key(self.db_name, generation), data),
                              (s3mod.change_counter_key(self.db_name, generation),
                               str(seq).encode())):
                delay = 0.1
                while True:
                    try:
                        self._put(key, body)
                        break
                    except Exception as e:
                        if time.monotonic() + delay >= deadline:
                            log.error("Failed to upload %s: %s", key, e)
                            errors.append(e)
                            done.set()
                            return
                        log.warning("Snapshot PUT %s failed: %s; retrying", key, e)
                        time.sleep(delay)
                        delay = min(delay * 2, 1.0)
            done.set()
            log.debug("Snapshot upload finished (took %.3fs)", time.monotonic() - start)

        log.debug("Scheduled DB snapshot %s", generation)
        return self._spawn(upload, f"snapshot-{generation[:8]}")

    def drain(self, timeout):
        deadline = time.monotonic() + timeout
        try:
            self._flush_pending(time.monotonic() + min(5.0, timeout / 2))
        except Exception as e:
            log.error("Could not settle a pending frame batch before exit: %s", e)
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
        """Evaluates the head generation through its .dep chain and decides from that state.
        A head without a snapshot is normal (rollover never waits for it); its state is the
        nearest ancestor snapshot plus every generation's frames up to the head. Missing
        objects mean 'not there', never 'zero'."""
        last_frame = self.get_last_consistent_frame(generation)
        log.debug("Last consistent remote frame in generation %s: %d.", generation, last_frame)
        # Whatever we decide, the generation started next descends from the head.
        self.generation = generation

        restore_path = self.db.path + ".restore"
        try:
            self.full_restore(restore_path, generation, last_frame)
        except Exception as e:
            log.error("failed to restore the database: %s. Rollback", e)
            if os.path.exists(restore_path):
                os.remove(restore_path)
            raise
        remote_seq = read_state(restore_path)[0]
        local_seq = self.db.seq()
        log.debug("Chain state of %s: seq %d; local seq %d", generation, remote_seq, local_seq)
        if remote_seq > local_seq or local_seq == 0:
            self.db.replace_with(restore_path)
            return SnapshotMainDbFile(), remote_seq > 0
        # The local database holds at least everything the bucket does.
        os.remove(restore_path)
        return SnapshotMainDbFile(), False

    def full_restore(self, restore_path, generation, last_frame):
        create_empty(restore_path)
        restore_stack = []
        current = generation
        while current is not None:
            # stash current generation - we'll use it to replay frames across generations
            # since the last snapshot
            restore_stack.append(current)
            parent = self.get_dependency(current)
            if self.restore_from_snapshot(current, restore_path):
                if parent is None:
                    self.check_unlinked(current, read_state(restore_path))
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
            if parent is None:
                self.check_unlinked(current, None)
            current = parent
            if current is not None:
                if not s3mod.list_keys(self.client, self.bucket,
                                       s3mod.generation_prefix(self.db_name, current), max_keys=1):
                    raise RestoreError(f"generation {restore_stack[-1]} depends on {current}, "
                                       f"which is not in the bucket")
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

    def older_generation(self, generation):
        """The generation listed right after `generation`, i.e. the one created before it."""
        prefix = f"{self.db_name}-"
        for key in s3mod.list_keys(self.client, self.bucket, prefix):
            candidate = key[len(prefix):].split("/", 1)[0]
            if is_generation(candidate) and candidate > generation:
                return candidate
        return None

    def chain_state(self, generation):
        """(seq, head, ids) of the database as the chain ending in `generation` holds it."""
        path = f"{self.db.path}.chain-{generation}"
        try:
            self.full_restore(path, generation, None)
            return read_state(path)
        finally:
            if os.path.exists(path):
                os.remove(path)

    def check_unlinked(self, generation, base):
        """A generation without .dep is the database's first one, or one the old boot path
        published without its link. The latter continues the history only if its snapshot
        (`base`) is exactly the state the generation before it ended with; a snapshot of an old
        volume forks the history, and restoring it would drop acknowledged commits."""
        older = self.older_generation(generation)
        if older is None:
            return
        if base is None:
            raise RestoreError(f"generation {generation} has neither .dep nor a snapshot, and "
                               f"{older} is older: its base state is unknown")
        tip = self.chain_state(older)
        if tip != base:
            raise RestoreError(f"generation {generation} has no .dep and its snapshot (seq "
                               f"{base[0]}) is not the last state of {older} (seq {tip[0]}): the "
                               f"bucket holds two histories")
        log.warning("Generation %s has no .dep; its snapshot continues %s", generation, older)

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
