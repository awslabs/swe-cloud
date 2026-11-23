"""Grades real S3 and DynamoDB state left by the store in /app.

Groups are independent: every group records its own findings and always runs
to the end, so one failure never hides the rest. Worker processes are driven
over the operations socket the store announces on; the reclaimer runs
throughout; every durable fact is read back from the services.
"""

import ast
from pathlib import Path
from cdk_seed.cfn_deploy import deploy
import importlib
import os
import sys
import threading
import time
import uuid

import pytest

from harness import (
    BARRIER_TIMEOUT,
    GRACE,
    OP_TIMEOUT,
    POLL,
    RETENTION,
    STALE_HOLD,
    TOLERANCE,
    OpsListener,
    Sweeper,
    Worker,
    aws_client,
    check,
    entries_of,
    APP,
    file_name,
    final_versions,
    get_raw_item,
    query_rows,
    s3_listing,
    s3_read,
    status_of,
    wait_until,
)


class Deployment:
    def __init__(self):
        self.run_id = uuid.uuid4().hex[:10]
        self.s3 = aws_client("s3")
        self.ddb = aws_client("dynamodb")
        self.bucket = "logstore-%s" % self.run_id
        self.log_table = "log_coord_%s" % self.run_id[:6]
        deploy(aws_client("cloudformation"), "seed-" + self.run_id,
               Path(__file__).parent / "cdk_seed/seed.template.json",
               BucketName=self.bucket, TableName=self.log_table)
        self.listener = OpsListener()
        self.sweeper = Sweeper(self.ddb, self.log_table)
        self.sweeper.start()
        self.workers = []

    def worker(self, table_path, name=""):
        worker = Worker(self.listener, self.bucket, self.log_table, table_path, name=name)
        self.workers.append(worker)
        return worker

    def teardown(self):
        for worker in self.workers:
            worker.kill()
        self.sweeper.halt()
        self.listener.stop()


@pytest.fixture(scope="module")
def deployment():
    # Preflight: both services must answer before anything is graded.
    probe = aws_client("s3")
    last = None
    for _ in range(30):
        try:
            probe.list_buckets()
            break
        except Exception as exc:  # emulator not live: environment, not a result
            last = exc
            time.sleep(1)
    else:
        pytest.fail("emulator not reachable at %s: %r" % (os.environ.get("AWS_ENDPOINT_URL"), last))
    table_probe = aws_client("dynamodb")
    try:
        table_probe.list_tables()
    except Exception as exc:
        pytest.fail("dynamodb not reachable at %s: %r" % (os.environ.get("AWS_ENDPOINT_URL"), exc))

    deployment = Deployment()
    yield deployment
    deployment.teardown()


def seed_genesis(deployment, table_path, payload):
    key = "%s/log/%s" % (table_path, file_name(0))
    deployment.s3.put_object(Bucket=deployment.bucket, Key=key, Body=payload.encode())
    deployment.sweeper.register(table_path)


def assert_expiry_matches(failures, raw_item, acknowledged_at, label):
    expire = raw_item.get("expireTime") if raw_item else None
    if not check(failures, expire is not None, "%s: completed entry carries an expiry" % label):
        return
    value = int(expire["N"])
    expected = acknowledged_at + RETENTION
    check(
        failures,
        abs(value - expected) <= TOLERANCE + 0.5,
        "%s: expiry %d is not within %ds of completion (%.1f)"
        % (label, value, RETENTION, expected),
    )


def wait_reclaimed(failures, deployment, table_path, files, since, label):
    def gone():
        rows = {row["fileName"] for row in query_rows(deployment.ddb, deployment.log_table, table_path)}
        return not (set(files) & rows)

    remaining = max(0.0, since + RETENTION + TOLERANCE + GRACE + 1.0 - time.time())
    wait_until(gone, remaining, POLL)
    check(failures, gone(), "%s: completed entries %s were reclaimed on schedule" % (label, list(files)))


# ----------------------------------------------------------------------
# Group 1: a reservation that is live still excludes a second writer
# ----------------------------------------------------------------------

def test_01_live_reservation_excludes_second_writer(deployment):
    failures = []
    label = "live-reservation"
    table_path = "runs/%s/%s" % (deployment.run_id, label)
    payloads = {n: "%s-%s" % (n, deployment.run_id) for n in ("G", "A1", "B1")}
    seed_genesis(deployment, table_path, payloads["G"])
    writer = deployment.worker(table_path, name="A")
    rival = deployment.worker(table_path, name="B")
    temp_path = None
    try:
        hold = deployment.listener.hold(writer.pid, "before_publish", file=file_name(1))
        writer.send("commit", version=1, actions=payloads["A1"])
        parked = deployment.listener.wait_for(
            lambda f: f.get("pid") == writer.pid
            and f.get("event") == "before_publish"
            and f.get("file") == file_name(1)
        )
        if check(failures, parked is not None, "%s: writer reached its publication point" % label):
            rows = query_rows(deployment.ddb, deployment.log_table, table_path)
            check(failures, len(rows) == 1, "%s: exactly one entry exists while parked, saw %d" % (label, len(rows)))
            raw = get_raw_item(deployment.ddb, deployment.log_table, table_path, file_name(1))
            check(failures, raw is not None, "%s: parked entry present" % label)
            if raw is not None:
                check(failures, raw.get("complete") == {"BOOL": False}, "%s: parked entry incomplete" % label)
                check(failures, "expireTime" not in raw, "%s: parked entry carries no expiry" % label)
                check(failures, raw.get("tempPath", {}).get("S", "").startswith(".tmp/"),
                      "%s: parked entry records its temp object" % label)
                temp_path = raw.get("tempPath", {}).get("S", "")
                if check(failures, bool(temp_path), "%s: parked entry names a temp object" % label):
                    temp_key = "%s/log/%s" % (table_path, temp_path)
                    check(failures, s3_read(deployment.s3, deployment.bucket, temp_key) == payloads["A1"].encode(),
                          "%s: temp object holds the writer's actions" % label)
            check(failures, s3_read(deployment.s3, deployment.bucket, "%s/log/%s" % (table_path, file_name(1))) is None,
                  "%s: version 1 not yet published" % label)

        rival.send("commit", version=1, actions=payloads["B1"])
        reply = rival.recv(OP_TIMEOUT)
        check(failures, status_of(reply) == "conflict",
              "%s: second writer reported %r, expected conflict" % (label, status_of(reply)))
        # The rival may leave the live claim alone or finish it from the
        # holder's recorded temp object; it must not take the claim over or
        # publish its own actions.
        raw = get_raw_item(deployment.ddb, deployment.log_table, table_path, file_name(1))
        published = s3_read(deployment.s3, deployment.bucket, "%s/log/%s" % (table_path, file_name(1)))
        if check(failures, raw is not None, "%s: entry still present after rival's attempt" % label):
            check(failures, raw.get("tempPath", {}).get("S", "") == temp_path,
                  "%s: rival's attempt left the writer's claim in place" % label)
            if raw.get("complete") == {"BOOL": True}:
                check(failures, published == payloads["A1"].encode(),
                      "%s: entry completed by the rival holds the writer's actions" % label)
        check(failures, published is None or published == payloads["A1"].encode(),
              "%s: rival did not publish its own actions as version 1" % label)

        deployment.listener.release(hold)
        acknowledged = time.time()
        reply = writer.recv(OP_TIMEOUT)
        check(failures, status_of(reply) == "committed",
              "%s: writer reported %r, expected committed" % (label, status_of(reply)))
        check(failures,
              s3_read(deployment.s3, deployment.bucket, "%s/log/%s" % (table_path, file_name(1))) == payloads["A1"].encode(),
              "%s: published version holds the writer's actions" % label)
        raw = get_raw_item(deployment.ddb, deployment.log_table, table_path, file_name(1))
        if check(failures, raw is not None, "%s: completed entry present" % label):
            check(failures, raw.get("complete") == {"BOOL": True}, "%s: entry completed after ack" % label)
            assert_expiry_matches(failures, raw, acknowledged, "%s ack" % label)
    finally:
        writer.kill()
        rival.kill()
    assert not failures, "\n".join(failures)


# ----------------------------------------------------------------------
# Groups 2 and 3: the reclaimed-entry schedule, entered two ways
# ----------------------------------------------------------------------

def reclaimed_entry_schedule(deployment, label, park_mode):
    failures = []
    table_path = "runs/%s/%s" % (deployment.run_id, label)
    payloads = {n: "%s-%s" % (n, deployment.run_id) for n in ("G", "A1", "A2", "A3", "B1", "B4")}
    seed_genesis(deployment, table_path, payloads["G"])
    writer = deployment.worker(table_path, name="A")
    delayed = deployment.worker(table_path, name="B")
    reader = deployment.worker(table_path, name="R")

    def final_bytes(version):
        return s3_read(deployment.s3, deployment.bucket, "%s/log/%s" % (table_path, file_name(version)))

    try:
        # The delayed writer captures the table before anyone else commits.
        reply = delayed.call("snapshot")
        check(failures, reply is not None and reply.get("ok") and reply.get("version") == 0,
              "%s: delayed writer snapshotted the initial version (%r)" % (label, reply))

        # Park the delayed writer: either before its commit, or after it has
        # established for itself that the target version is absent but before
        # it reserves the version.
        hold = None
        if park_mode == "before_commit":
            pass
        else:
            hold = deployment.listener.hold(delayed.pid, "before_reserve", file=file_name(1))
            delayed.send("commit", version=1, actions=payloads["B1"])
            parked = deployment.listener.wait_for(
                lambda f: f.get("pid") == delayed.pid
                and f.get("event") == "before_reserve"
                and f.get("file") == file_name(1)
            )
            check(failures, parked is not None, "%s: delayed writer reached its reservation point" % label)

        # Two versions are committed and acknowledged while the delayed writer
        # is held back.
        first_ack = time.time()
        reply = writer.call("commit", version=1, actions=payloads["A1"])
        check(failures, status_of(reply) == "committed",
              "%s: version 1 committed (%r)" % (label, status_of(reply)))
        check(failures, final_bytes(1) == payloads["A1"].encode(),
              "%s: version 1 published with its own bytes" % label)
        raw = get_raw_item(deployment.ddb, deployment.log_table, table_path, file_name(1))
        if check(failures, raw is not None, "%s: entry for version 1 present after ack" % label):
            assert_expiry_matches(failures, raw, first_ack, "%s version 1" % label)

        second_ack = time.time()
        reply = writer.call("commit", version=2, actions=payloads["A2"])
        check(failures, status_of(reply) == "committed",
              "%s: version 2 committed (%r)" % (label, status_of(reply)))
        check(failures, final_bytes(2) == payloads["A2"].encode(),
              "%s: version 2 published with its own bytes" % label)
        raw = get_raw_item(deployment.ddb, deployment.log_table, table_path, file_name(2))
        if check(failures, raw is not None, "%s: entry for version 2 present after ack" % label):
            assert_expiry_matches(failures, raw, second_ack, "%s version 2" % label)

        check(failures,
              not [r for r in query_rows(deployment.ddb, deployment.log_table, table_path) if not r["complete"]],
              "%s: no incomplete entries while the table advances" % label)

        # The completed entries are reclaimed, their versions surviving.
        wait_reclaimed(failures, deployment, table_path, [file_name(1), file_name(2)], second_ack, label)
        check(failures, final_bytes(1) == payloads["A1"].encode(),
              "%s: version 1 bytes unchanged after its entry was reclaimed" % label)
        check(failures, final_bytes(2) == payloads["A2"].encode(),
              "%s: version 2 bytes unchanged after its entry was reclaimed" % label)

        # A reader still sees the table as it was committed.
        reply = reader.call("list_from", start=0)
        check(failures, entries_of(reply) == [[0, payloads["G"]], [1, payloads["A1"]], [2, payloads["A2"]]],
              "%s: reader listed the committed history (%r)" % (label, entries_of(reply)))

        # The delayed writer finishes its attempt.
        if park_mode == "before_commit":
            delayed.send("commit", version=1, actions=payloads["B1"])
        else:
            deployment.listener.release(hold)
        reply = delayed.recv(OP_TIMEOUT)
        check(failures, status_of(reply) == "conflict",
              "%s: delayed writer reported %r, expected conflict" % (label, status_of(reply)))
        check(failures, final_bytes(1) == payloads["A1"].encode(),
              "%s: the acknowledged version 1 still holds its original bytes" % label)
        check(failures, final_bytes(2) == payloads["A2"].encode(),
              "%s: the acknowledged version 2 still holds its original bytes" % label)

        # A third version is prepared and its writer dies before publication.
        kill_hold = deployment.listener.hold(writer.pid, "before_publish", file=file_name(3))
        writer.send("commit", version=3, actions=payloads["A3"])
        reached = deployment.listener.wait_for(
            lambda f: f.get("pid") == writer.pid
            and f.get("event") == "before_publish"
            and f.get("file") == file_name(3)
        )
        if check(failures, reached is not None, "%s: writer prepared version 3" % label):
            raw = get_raw_item(deployment.ddb, deployment.log_table, table_path, file_name(3))
            check(failures, raw is not None and raw.get("complete") == {"BOOL": False} and "expireTime" not in raw,
                  "%s: prepared entry is incomplete and carries no expiry" % label)
            temp_path = raw.get("tempPath", {}).get("S", "") if raw else ""
            if check(failures, bool(temp_path), "%s: prepared entry names a temp object" % label):
                temp_key = "%s/log/%s" % (table_path, temp_path)
                check(failures, s3_read(deployment.s3, deployment.bucket, temp_key) == payloads["A3"].encode(),
                      "%s: prepared temp object holds the writer's actions" % label)
            check(failures, final_bytes(3) is None, "%s: version 3 not yet published" % label)
            incomplete = [r for r in query_rows(deployment.ddb, deployment.log_table, table_path) if not r["complete"]]
            check(failures, len(incomplete) == 1,
                  "%s: one incomplete entry at the crash point, saw %d" % (label, len(incomplete)))
        writer.kill()
        time.sleep(STALE_HOLD)
        raw = get_raw_item(deployment.ddb, deployment.log_table, table_path, file_name(3))
        check(failures, raw is not None and raw.get("complete") == {"BOOL": False} and "expireTime" not in raw,
              "%s: the crashed writer's entry survived unreclaimed" % label)
        check(failures, final_bytes(3) is None, "%s: version 3 still unpublished during the hold" % label)

        # Reader and surviving writer are released together: the predecessor
        # must be finished before the successor is prepared.
        deployment.listener.release(kill_hold)
        violations = []
        stop_watch = threading.Event()

        def watch():
            while not stop_watch.is_set():
                rows = {r["fileName"]: r for r in query_rows(deployment.ddb, deployment.log_table, table_path)}
                if file_name(4) in rows:
                    third = rows.get(file_name(3))
                    if (third is None or not third["complete"]) or final_bytes(3) is None:
                        violations.append("entry for version 4 appeared before version 3 was finished")
                time.sleep(POLL)

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            reader.send("list_from", start=0)
            delayed.send("commit", version=4, actions=payloads["B4"])
            reply = delayed.recv(OP_TIMEOUT)
            reader_reply = reader.recv(OP_TIMEOUT)
        finally:
            stop_watch.set()
            watcher.join(timeout=5)
        failures.extend(violations)
        check(failures, status_of(reply) == "committed",
              "%s: successor committed after recovery (%r)" % (label, status_of(reply)))
        check(failures, final_bytes(3) == payloads["A3"].encode(),
              "%s: crashed writer's version recovered with its recorded actions" % label)
        check(failures, final_bytes(4) == payloads["B4"].encode(),
              "%s: successor published with its own bytes" % label)
        listed = entries_of(reader_reply)
        if check(failures, listed is not None, "%s: reader replied during recovery (%r)" % (label, reader_reply)):
            expected_start = [[0, payloads["G"]], [1, payloads["A1"]], [2, payloads["A2"]], [3, payloads["A3"]]]
            check(failures, listed[:4] == expected_start or listed == expected_start + [[4, payloads["B4"]]],
                  "%s: reader saw the recovered history (%r)" % (label, listed))

        # The log is a contiguous range with the known winners.
        expected = {0: payloads["G"], 1: payloads["A1"], 2: payloads["A2"], 3: payloads["A3"], 4: payloads["B4"]}
        versions = final_versions(deployment.s3, deployment.bucket, table_path)
        check(failures, sorted(versions) == sorted(expected),
              "%s: final versions are 0..4, saw %s" % (label, sorted(versions)))
        for version, payload in expected.items():
            check(failures, final_bytes(version) == payload.encode(),
                  "%s: version %d holds its winner's bytes" % (label, version))

        # When the run settles, the coordination table holds nothing.
        settle_deadline = RETENTION + TOLERANCE + GRACE + 3
        wait_until(lambda: query_rows(deployment.ddb, deployment.log_table, table_path) == [], settle_deadline, POLL)
        rows = query_rows(deployment.ddb, deployment.log_table, table_path)
        check(failures, rows == [], "%s: coordination table settled empty, still holds %s" % (label, [r["fileName"] for r in rows]))
    finally:
        writer.kill()
        delayed.kill()
        reader.kill()
    assert not failures, "\n".join(failures)


def test_02_delayed_writer_parked_before_commit(deployment):
    reclaimed_entry_schedule(deployment, "parked-before-commit", "before_commit")


def test_03_delayed_writer_parked_after_absence(deployment):
    reclaimed_entry_schedule(deployment, "parked-after-absence", "before_reserve")


# ----------------------------------------------------------------------
# Group 4: the operating surface itself
# ----------------------------------------------------------------------

def _local_imports(path):
    """Modules of /app that this file imports, including itself."""
    found, seen = [], set()
    stack = [path]
    while stack:
        current = stack.pop()
        if current in seen or not os.path.exists(current):
            continue
        seen.add(current)
        found.append(current)
        try:
            tree = ast.parse(open(current).read())
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for name in names:
                candidate = os.path.join(APP, name.split(".")[0] + ".py")
                if os.path.exists(candidate):
                    stack.append(candidate)
    return found


def test_04_operating_surface(deployment):
    failures = []
    label = "surface"

    # Store code reaches the services only through the storage channel.
    graph = _local_imports(os.path.join(APP, "worker.py"))
    check(failures, any(p.endswith("logstore.py") for p in graph),
          "%s: the worker still loads a log store module" % label)
    for source in graph:
        if os.path.basename(source) == "storage.py":
            continue
        try:
            tree = ast.parse(open(source).read())
        except (OSError, SyntaxError) as exc:
            check(failures, False, "%s: %s does not parse: %r" % (label, source, exc))
            continue
        for node in ast.walk(tree):
            imported = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = [node.module]
            for name in imported:
                if name.split(".")[0] in ("boto3", "botocore"):
                    check(failures, False,
                          "%s: %s calls the AWS SDK directly; the storage channel is the only supported surface"
                          % (label, source))

    # The documented session works without any driver attached.
    table_path = "runs/%s/%s" % (deployment.run_id, label)
    deployment.sweeper.register(table_path)
    plain = Worker(deployment.listener, deployment.bucket, deployment.log_table,
                   table_path, name="plain", announce=False)
    deployment.workers.append(plain)
    try:
        payload_x = "X-%s" % deployment.run_id
        payload_y = "Y-%s" % deployment.run_id
        reply = plain.call("snapshot", timeout=BARRIER_TIMEOUT)
        check(failures, reply is not None and reply.get("ok") and reply.get("version") is None,
              "%s: snapshot of a new table is empty (%r)" % (label, reply))
        reply = plain.call("commit", version=0, actions=payload_x)
        check(failures, status_of(reply) == "committed",
              "%s: first version committed (%r)" % (label, status_of(reply)))
        reply = plain.call("snapshot")
        check(failures, reply is not None and reply.get("version") == 0,
              "%s: snapshot sees the first version (%r)" % (label, reply))
        reply = plain.call("commit", version=1, actions=payload_y)
        check(failures, status_of(reply) == "committed",
              "%s: second version committed (%r)" % (label, status_of(reply)))
        reply = plain.call("list_from", start=0)
        check(failures, entries_of(reply) == [[0, payload_x], [1, payload_y]],
              "%s: listing returns the versions in order (%r)" % (label, entries_of(reply)))
    finally:
        plain.kill()
    assert not failures, "\n".join(failures)


# ----------------------------------------------------------------------
# Groups 5 and 6: the writer resumes after its publication was finished
# ----------------------------------------------------------------------

def resumed_writer_schedule(deployment, label, reclaimed_first):
    failures = []
    table_path = "runs/%s/%s" % (deployment.run_id, label)
    payload = "A1-%s" % deployment.run_id
    genesis = "G-%s" % deployment.run_id
    seed_genesis(deployment, table_path, genesis)
    writer = deployment.worker(table_path, name="A")
    reader = deployment.worker(table_path, name="R")

    def final_bytes(version):
        return s3_read(deployment.s3, deployment.bucket, "%s/log/%s" % (table_path, file_name(version)))

    try:
        # The writer is held at its publication point, its reservation made.
        hold = deployment.listener.hold(writer.pid, "before_publish", file=file_name(1))
        writer.send("commit", version=1, actions=payload)
        parked = deployment.listener.wait_for(
            lambda f: f.get("pid") == writer.pid
            and f.get("event") == "before_publish"
            and f.get("file") == file_name(1)
        )
        if check(failures, parked is not None, "%s: writer reached its publication point" % label):
            incomplete = [r for r in query_rows(deployment.ddb, deployment.log_table, table_path) if not r["complete"]]
            check(failures, len(incomplete) == 1,
                  "%s: the held writer's entry is the one incomplete entry, saw %d" % (label, len(incomplete)))
            check(failures, final_bytes(1) is None, "%s: version 1 not yet published" % label)

        # A reader lists the table: recovery finishes the held publication
        # from the recorded temp object and completes the entry.
        reader.send("list_from", start=0)
        reply = reader.recv(OP_TIMEOUT)
        check(failures, entries_of(reply) == [[0, genesis], [1, payload]],
              "%s: reader listed the finished publication (%r)" % (label, entries_of(reply)))
        check(failures, final_bytes(1) == payload.encode(),
              "%s: recovery published the recorded actions" % label)
        repaired_at = time.time()
        raw = get_raw_item(deployment.ddb, deployment.log_table, table_path, file_name(1))
        check(failures, raw is not None and raw.get("complete") == {"BOOL": True},
              "%s: the finished publication's entry is complete" % label)

        if reclaimed_first:
            # The completed entry is reclaimed while its writer is still held.
            wait_reclaimed(failures, deployment, table_path, [file_name(1)], repaired_at, label)

        deployment.listener.release(hold)
        reply = writer.recv(OP_TIMEOUT)
        check(failures, status_of(reply) == "committed",
              "%s: held writer reported %r, expected committed" % (label, status_of(reply)))
        check(failures, final_bytes(1) == payload.encode(),
              "%s: the version holds the writer's actions after it resumed" % label)

        settle_deadline = RETENTION + TOLERANCE + GRACE + 3
        wait_until(lambda: query_rows(deployment.ddb, deployment.log_table, table_path) == [],
                   settle_deadline, POLL)
        rows = query_rows(deployment.ddb, deployment.log_table, table_path)
        check(failures, rows == [], "%s: coordination table settled empty, still holds %s"
              % (label, [r["fileName"] for r in rows]))
    finally:
        writer.kill()
        reader.kill()
    assert not failures, "\n".join(failures)


def test_05_writer_resumed_before_reclaim(deployment):
    resumed_writer_schedule(deployment, "resumed-before-reclaim", False)


def test_06_writer_resumed_after_reclaim(deployment):
    resumed_writer_schedule(deployment, "resumed-after-reclaim", True)


# ----------------------------------------------------------------------
# Group 7: two writers, one version, the same actions
# ----------------------------------------------------------------------

def test_07_second_writer_with_the_same_actions(deployment):
    failures = []
    label = "same-actions"
    table_path = "runs/%s/%s" % (deployment.run_id, label)
    actions = "shared-%s" % deployment.run_id
    seed_genesis(deployment, table_path, "G-%s" % deployment.run_id)
    writer = deployment.worker(table_path, name="A")
    rival = deployment.worker(table_path, name="B")
    final_key = "%s/log/%s" % (table_path, file_name(1))
    try:
        # The rival is held after it has prepared its commit of version 1.
        hold = deployment.listener.hold(rival.pid, "before_reserve", file=file_name(1))
        rival.send("commit", version=1, actions=actions)
        parked = deployment.listener.wait_for(
            lambda f: f.get("pid") == rival.pid
            and f.get("event") == "before_reserve"
            and f.get("file") == file_name(1)
        )
        check(failures, parked is not None, "%s: rival reached its reservation point" % label)

        # The first writer commits the same actions and is acknowledged.
        acknowledged = time.time()
        reply = writer.call("commit", version=1, actions=actions)
        check(failures, status_of(reply) == "committed",
              "%s: first writer reported %r, expected committed" % (label, status_of(reply)))
        check(failures, s3_read(deployment.s3, deployment.bucket, final_key) == actions.encode(),
              "%s: version 1 published with those actions" % label)

        # The completed entry is reclaimed before the rival is let further.
        wait_reclaimed(failures, deployment, table_path, [file_name(1)], acknowledged, label)

        deployment.listener.release(hold)
        reply = rival.recv(OP_TIMEOUT)
        check(failures, status_of(reply) == "conflict",
              "%s: rival reported %r, expected conflict" % (label, status_of(reply)))
        check(failures, s3_read(deployment.s3, deployment.bucket, final_key) == actions.encode(),
              "%s: the acknowledged version still holds its bytes" % label)

        settle_deadline = RETENTION + TOLERANCE + GRACE + 3
        wait_until(lambda: query_rows(deployment.ddb, deployment.log_table, table_path) == [],
                   settle_deadline, POLL)
        rows = query_rows(deployment.ddb, deployment.log_table, table_path)
        check(failures, rows == [], "%s: coordination table settled empty, still holds %s"
              % (label, [r["fileName"] for r in rows]))
    finally:
        writer.kill()
        rival.kill()
    assert not failures, "\n".join(failures)


# ----------------------------------------------------------------------
# Group 8: the temp object a held writer recorded is gone when it resumes
# ----------------------------------------------------------------------

def test_08_recorded_temp_object_removed(deployment):
    failures = []
    label = "removed-temp"
    table_path = "runs/%s/%s" % (deployment.run_id, label)
    payload = "B1-%s" % deployment.run_id
    seed_genesis(deployment, table_path, "G-%s" % deployment.run_id)
    writer = deployment.worker(table_path, name="B")
    temp_prefix = "%s/log/%s/" % (table_path, ".tmp")
    try:
        hold = deployment.listener.hold(writer.pid, "before_reserve", file=file_name(1))
        writer.send("commit", version=1, actions=payload)
        parked = deployment.listener.wait_for(
            lambda f: f.get("pid") == writer.pid
            and f.get("event") == "before_reserve"
            and f.get("file") == file_name(1)
        )
        if check(failures, parked is not None, "%s: writer reached its reservation point" % label):
            # Everything the writer prepared under the temp prefix is removed
            # from the bucket before its reservation is made.
            prepared = s3_listing(deployment.s3, deployment.bucket, temp_prefix)
            for key in prepared:
                deployment.s3.delete_object(Bucket=deployment.bucket, Key=key)
            check(failures, bool(prepared), "%s: the held writer had prepared its temp object" % label)
            check(failures, s3_listing(deployment.s3, deployment.bucket, temp_prefix) == [],
                  "%s: the temp prefix is empty" % label)

        deployment.listener.release(hold)
        reply = writer.recv(OP_TIMEOUT)
        check(failures, status_of(reply) == "error",
              "%s: writer reported %r, expected error" % (label, status_of(reply)))
        check(failures,
              s3_read(deployment.s3, deployment.bucket, "%s/log/%s" % (table_path, file_name(1))) is None,
              "%s: version 1 was not published" % label)
        anywhere = [key for key in s3_listing(deployment.s3, deployment.bucket, "%s/log/" % table_path)
                    if s3_read(deployment.s3, deployment.bucket, key) == payload.encode()]
        check(failures, not anywhere, "%s: the writer's actions reached no object (%s)" % (label, anywhere))
    finally:
        writer.kill()
    assert not failures, "\n".join(failures)


# ----------------------------------------------------------------------
# Group 9: a reader finishes the publication while the writer is held
# between its reservation and its look at the version's object
# ----------------------------------------------------------------------

def park_past_reservation(deployment, worker, table_path, version, actions, failures, label):
    """Send `worker` a commit and hold it after it has claimed the version.

    The writer is held at its probe of the version's object once its claim is
    in the table; a probe announced before any claim exists is an earlier one,
    and the writer is let past it. Writers that never look at the version's
    object after claiming it are held at their publication point instead.
    """
    key = "%s/log/%s" % (table_path, file_name(version))
    name = file_name(version)
    probe_hold = deployment.listener.hold(worker.pid, "probe", key=key)
    publish_hold = deployment.listener.hold(worker.pid, "before_publish", file=name)
    worker.send("commit", version=version, actions=actions)
    parked = None
    seen = []
    for _ in range(6):
        parked = deployment.listener.wait_for(
            lambda f: f.get("pid") == worker.pid
            and not any(f is s for s in seen)
            and (
                (f.get("event") == "probe" and f.get("key") == key)
                or (f.get("event") == "before_publish" and f.get("file") == name)
            )
        )
        if parked is None:
            check(failures, False, "%s: writer reached the version's publication point" % label)
            deployment.listener.release(probe_hold)
            deployment.listener.release(publish_hold)
            return None
        seen.append(parked)
        if parked.get("event") == "before_publish":
            deployment.listener.release(probe_hold)
            return publish_hold
        if get_raw_item(deployment.ddb, deployment.log_table, table_path, name) is not None:
            deployment.listener.release(publish_hold)
            return probe_hold
        # An earlier look at the same object: let the writer past it and take
        # the next one.
        fresh = deployment.listener.hold(worker.pid, "probe", key=key)
        deployment.listener.release(probe_hold)
        probe_hold = fresh
    check(failures, False, "%s: writer never reached the version with its claim held" % label)
    deployment.listener.release(probe_hold)
    deployment.listener.release(publish_hold)
    return None


def test_09_writer_overtaken_at_its_probe(deployment):
    failures = []
    label = "overtaken-probe"
    table_path = "runs/%s/%s" % (deployment.run_id, label)
    payload = "A1-%s" % deployment.run_id
    genesis = "G-%s" % deployment.run_id
    seed_genesis(deployment, table_path, genesis)
    writer = deployment.worker(table_path, name="A")
    reader = deployment.worker(table_path, name="R")

    def final_bytes(version):
        return s3_read(deployment.s3, deployment.bucket, "%s/log/%s" % (table_path, file_name(version)))

    try:
        hold = park_past_reservation(deployment, writer, table_path, 1, payload, failures, label)
        if hold is not None:
            incomplete = [r for r in query_rows(deployment.ddb, deployment.log_table, table_path) if not r["complete"]]
            check(failures, len(incomplete) == 1,
                  "%s: the held writer's entry is the one incomplete entry, saw %d" % (label, len(incomplete)))
            check(failures, final_bytes(1) is None, "%s: version 1 not yet published" % label)

            # A reader lists the table: recovery finishes the held publication
            # from the recorded temp object and completes the entry.
            reader.send("list_from", start=0)
            reply = reader.recv(OP_TIMEOUT)
            check(failures, entries_of(reply) == [[0, genesis], [1, payload]],
                  "%s: reader listed the finished publication (%r)" % (label, entries_of(reply)))
            check(failures, final_bytes(1) == payload.encode(),
                  "%s: recovery published the recorded actions" % label)
            raw = get_raw_item(deployment.ddb, deployment.log_table, table_path, file_name(1))
            check(failures, raw is not None and raw.get("complete") == {"BOOL": True},
                  "%s: the finished publication's entry is complete" % label)

            deployment.listener.release(hold)
            reply = writer.recv(OP_TIMEOUT)
            check(failures, status_of(reply) == "committed",
                  "%s: overtaken writer reported %r, expected committed" % (label, status_of(reply)))
            check(failures, final_bytes(1) == payload.encode(),
                  "%s: the version holds the writer's actions after it resumed" % label)

            settle_deadline = RETENTION + TOLERANCE + GRACE + 3
            wait_until(lambda: query_rows(deployment.ddb, deployment.log_table, table_path) == [],
                       settle_deadline, POLL)
            rows = query_rows(deployment.ddb, deployment.log_table, table_path)
            check(failures, rows == [], "%s: coordination table settled empty, still holds %s"
                  % (label, [r["fileName"] for r in rows]))
    finally:
        writer.kill()
        reader.kill()
    assert not failures, "\n".join(failures)


# ----------------------------------------------------------------------
# Group 10: an announced pause is a point in the flow, not a report after it
# ----------------------------------------------------------------------

def test_10_announced_pause_holds_the_process_before_its_look(deployment):
    failures = []
    label = "announced-pause"
    table_path = "runs/%s/%s" % (deployment.run_id, label)
    key = "%s/log/%s" % (table_path, file_name(0))
    payload = ("P-%s" % deployment.run_id).encode()

    # Operations drives these processes by holding them at the pauses the
    # store's channel announces, acting, and letting them go. A pause that
    # fires once the operation it precedes is already done cannot be driven
    # that way, so the channel is loaded from /app the way the worker loads
    # it and held exactly as a deployment holds it: stopped at the pause, the
    # world changed, released.
    channel = None
    hold = None
    watcher = None
    saved_env = {name: os.environ.get(name) for name in ("SEAM_SOCKET", "BUCKET", "LOG_TABLE", "TABLE_PATH")}
    os.environ["SEAM_SOCKET"] = deployment.listener.path
    os.environ["BUCKET"] = deployment.bucket
    os.environ["LOG_TABLE"] = deployment.log_table
    os.environ["TABLE_PATH"] = table_path
    try:
        try:
            sys.path.insert(0, APP)
            try:
                channel = importlib.import_module("storage").Storage.from_env()
            finally:
                sys.path.remove(APP)
        except Exception as exc:
            check(failures, False, "%s: the store's channel loads: %r" % (label, exc))

        if channel is not None:
            hold = deployment.listener.hold(os.getpid(), "probe", key=key)
            observed = {}

            def look():
                observed["present"] = channel.object_exists(key)

            watcher = threading.Thread(target=look)
            watcher.start()
            parked = deployment.listener.wait_for(
                lambda f: f.get("pid") == os.getpid()
                and f.get("event") == "probe"
                and f.get("key") == key
            )
            if check(failures, parked is not None, "%s: the channel announced its pause" % label):
                deployment.s3.put_object(Bucket=deployment.bucket, Key=key, Body=payload)
                deployment.listener.release(hold)
                watcher.join(OP_TIMEOUT)
                check(failures, not watcher.is_alive(), "%s: the held look completed" % label)
                check(failures, observed.get("present") is True,
                      "%s: a process held at the pause sees what is there when it is released" % label)
    finally:
        if hold is not None:
            deployment.listener.release(hold)
        if watcher is not None:
            watcher.join(OP_TIMEOUT)
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    assert not failures, "\n".join(failures)
