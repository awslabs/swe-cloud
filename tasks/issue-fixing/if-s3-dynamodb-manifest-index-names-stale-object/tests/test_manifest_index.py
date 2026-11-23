"""Grades the strata commit/resolve protocol on live Floci, one role per process.

Every scenario runs on a fresh dataset prefix and is repeated; the fault proxy (faultproxy.py)
stands between each role and Floci and stalls, drops or fails classified requests. Observations
are made directly against Floci (DynamoDB GetItem with ConsistentRead, S3 HeadObject and
ListObjectVersions), never through solver code.
"""

import time

import pytest

from harness import (
    COMMIT_DEADLINE_S,
    CORRUPT_SIZE_DELTA_BYTES,
    HOLD_ENGAGE_TIMEOUT_S,
    SLOT_R,
    SLOT_R2,
    SLOT_W,
    RESOLVE_DEADLINE_S,
    bytes_problems,
    generation_problems,
    manifest_bytes,
    row_truth_problems,
)

REPEATS = range(3)


def canonical_write(request, proxy):
    return request.kind == "s3_canonical_write"


def any_s3(request, proxy):
    return request.kind.startswith("s3_")


def staging_delete(request, proxy):
    return request.kind == "s3_staging" and request.method == "DELETE"


def ddb_write(request, proxy):
    return request.kind == "ddb_write"


def ddb_write_after_own_copy(request, proxy):
    return request.kind == "ddb_write" and proxy.completed(request.slot, "s3_canonical_write")


# ---------------------------------------------------------------------- helpers


def expect_exit(s, role, deadline, expected, what):
    rc = role.wait(deadline)
    if rc is None:
        s.fail(f"{what} did not return within {deadline}s")
        role.kill()
        return None
    if rc != expected:
        s.fail(f"{what} exited {rc}, expected {expected}")
        return None
    out = role.output()
    if out is None:
        s.fail(f"{what} printed no JSON result")
    return out


def check_location(s, label, out, dataset, version, size, snap, must_be_current):
    if out is None:
        return
    key = s.world.canonical(dataset, version)
    if out.get("version") != version:
        s.fail(f"{label} printed version {out.get('version')!r}, expected {version}")
    if out.get("path") != key:
        s.fail(f"{label} printed path {out.get('path')!r}, expected {key}")
    if out.get("size") != size:
        s.fail(f"{label} printed size {out.get('size')!r}, expected {size}")
    s.extend(generation_problems(label, out.get("generation"), snap, must_be_current))


def check_committed(s, label, out, dataset, version, size, snap):
    if out is None:
        return
    if out.get("status") != "committed":
        s.fail(f"{label} reported status {out.get('status')!r} for a version it committed")
        return
    check_location(s, label, out, dataset, version, size, snap, must_be_current=False)


def check_settled(s, dataset, version, data):
    """After every role has exited: finalized, true, only the selected bytes."""
    snap = s.world.sample(dataset, version)
    if snap.head is None:
        s.fail(f"no canonical manifest for version {version}: {snap.describe()}")
        return snap
    if not snap.finalized():
        s.fail(f"index row for version {version} still does not name the canonical manifest "
               f"after every process returned: {snap.describe()}")
    s.extend(row_truth_problems(snap))
    s.extend(bytes_problems(s.world, snap, data))
    return snap


def check_fresh_reader(s, dataset, version, data, label="a later reader", latest=False):
    role = s.resolve(SLOT_R2, dataset, None if latest else version)
    out = expect_exit(s, role, RESOLVE_DEADLINE_S, 0, f"{label} (`resolve`)")
    snap = s.world.sample(dataset, version)
    check_location(s, label, out, dataset, version, len(data), snap, must_be_current=True)
    s.extend(row_truth_problems(snap))
    return snap


def wait_for(condition, timeout, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return bool(condition())


def engage(s, rule, timeout, what):
    if not s.proxy.wait_held(rule, timeout):
        s.fail(what)
        return False
    return True


# ---------------------------------------------------------------------- scenarios


@pytest.mark.parametrize("rep", REPEATS)
def test_commit_then_resolve(scenario, rep):
    s = scenario
    dataset, version = s.world.dataset("baseline"), 1 + rep
    data = manifest_bytes(f"ingest-{rep}")
    w = s.commit(SLOT_W, dataset, version, data, f"ingest-{rep}")
    out = expect_exit(s, w, COMMIT_DEADLINE_S, 0, "commit")
    snap = check_settled(s, dataset, version, data)
    check_committed(s, "commit", out, dataset, version, len(data), snap)
    if out and out.get("generation") != snap.current_id:
        s.fail(f"commit printed generation {out.get('generation')!r} but it was the only writer "
               f"and the canonical object is {snap.current_id!r}")
    check_fresh_reader(s, dataset, version, data)
    check_fresh_reader(s, dataset, version, data, label="a later `--latest` reader", latest=True)
    s.report()


@pytest.mark.parametrize("rep", REPEATS)
def test_reader_finishes_while_committer_publish_stalls(scenario, rep):
    s = scenario
    dataset, version = s.world.dataset("stalled-publish"), 3 + rep
    data = manifest_bytes(f"nightly-merge-{rep}")
    publish = s.proxy.hold(SLOT_W, ddb_write_after_own_copy, name="committer index write")
    w = s.commit(SLOT_W, dataset, version, data, f"nightly-merge-{rep}")
    if engage(s, publish, HOLD_ENGAGE_TIMEOUT_S,
              "the committer never wrote the index after materializing its manifest"):
        r = s.resolve(SLOT_R, dataset, version)
        out = expect_exit(s, r, RESOLVE_DEADLINE_S, 0, "a reader that found the version pending")
        snap = s.world.sample(dataset, version)
        check_location(s, "the helping reader", out, dataset, version, len(data), snap, True)
        s.extend(row_truth_problems(snap))
    s.proxy.release_all()
    wout = expect_exit(s, w, COMMIT_DEADLINE_S, 0, "commit (after its stalled index write)")
    snap = check_settled(s, dataset, version, data)
    check_committed(s, "commit", wout, dataset, version, len(data), snap)
    check_fresh_reader(s, dataset, version, data)
    s.report()


@pytest.mark.parametrize("rep", REPEATS)
def test_reader_copy_lands_after_committer_publish(scenario, rep):
    s = scenario
    world, proxy = s.world, s.proxy
    dataset, version = world.dataset("late-copy"), 7 + rep
    data = manifest_bytes(f"stream-ingest-{rep}")
    w_copy = proxy.hold(SLOT_W, canonical_write, name="committer canonical write")
    w = s.commit(SLOT_W, dataset, version, data, f"stream-ingest-{rep}")
    if not engage(s, w_copy, HOLD_ENGAGE_TIMEOUT_S,
                  "the committer never wrote the canonical manifest"):
        s.report()
    r_copy = proxy.hold(SLOT_R, canonical_write, name="reader canonical write")
    r = s.resolve(SLOT_R, dataset, version)
    if not engage(s, r_copy, HOLD_ENGAGE_TIMEOUT_S,
                  "a reader that found the version pending (committer stalled) did not "
                  f"materialize it within {HOLD_ENGAGE_TIMEOUT_S}s"):
        s.report()

    w_delete = proxy.hold(SLOT_W, staging_delete, name="committer staging delete")
    proxy.release(w_copy)
    published = lambda: world.sample(dataset, version).finalized()  # noqa: E731
    wait_for(lambda: published() or not w.alive() or w_delete.held, HOLD_ENGAGE_TIMEOUT_S)
    if not published() and w_delete.held:
        proxy.release(w_delete)
        wait_for(lambda: published() or not w.alive(), HOLD_ENGAGE_TIMEOUT_S)

    r_index = proxy.hold(SLOT_R, ddb_write, name="reader index write")
    proxy.release(r_copy)
    if not wait_for(lambda: r_index.held or not r.alive(), RESOLVE_DEADLINE_S):
        s.fail("the helping reader neither wrote the index nor returned after its copy")
    snap = world.sample(dataset, version)
    s.extend(row_truth_problems(snap))
    check_fresh_reader(s, dataset, version, data, label="a reader arriving mid-finalization")

    proxy.release_all()
    wout = expect_exit(s, w, COMMIT_DEADLINE_S, 0, "commit")
    rout = expect_exit(s, r, RESOLVE_DEADLINE_S, 0, "the helping reader")
    snap = check_settled(s, dataset, version, data)
    check_committed(s, "commit", wout, dataset, version, len(data), snap)
    check_location(s, "the helping reader", rout, dataset, version, len(data), snap, False)
    check_fresh_reader(s, dataset, version, data)
    s.report()


@pytest.mark.parametrize("rep", REPEATS)
def test_staging_removed_under_a_reader(scenario, rep):
    s = scenario
    dataset, version = s.world.dataset("staging-gone"), 12 + rep
    data = manifest_bytes(f"backfill-{rep}")
    w_copy = s.proxy.hold(SLOT_W, canonical_write, name="committer canonical write")
    w = s.commit(SLOT_W, dataset, version, data, f"backfill-{rep}")
    if not engage(s, w_copy, HOLD_ENGAGE_TIMEOUT_S, "the committer never wrote the canonical manifest"):
        s.report()
    r_first = s.proxy.hold(SLOT_R, any_s3, name="reader first object-store request")
    r = s.resolve(SLOT_R, dataset, version)
    if not engage(s, r_first, HOLD_ENGAGE_TIMEOUT_S,
                  "a reader that found the version pending never touched the object store"):
        s.report()
    s.proxy.release(w_copy)
    wout = expect_exit(s, w, COMMIT_DEADLINE_S, 0, "commit")
    s.proxy.release(r_first)
    rout = expect_exit(s, r, RESOLVE_DEADLINE_S, 0, "a reader whose pending row was finalized under it")
    snap = check_settled(s, dataset, version, data)
    check_committed(s, "commit", wout, dataset, version, len(data), snap)
    check_location(s, "the reader", rout, dataset, version, len(data), snap, True)
    s.report()


@pytest.mark.parametrize("window", ["before-copy", "before-index-write"])
@pytest.mark.parametrize("rep", REPEATS)
def test_committer_killed_mid_commit(scenario, rep, window):
    s = scenario
    dataset = s.world.dataset("crash")
    first = manifest_bytes("hourly-0")
    w0 = s.commit(SLOT_R2, dataset, 1, first, "hourly-0")
    expect_exit(s, w0, COMMIT_DEADLINE_S, 0, "the first commit")
    version = 2
    data = manifest_bytes(f"hourly-{rep}")
    match = canonical_write if window == "before-copy" else ddb_write_after_own_copy
    stall = s.proxy.hold(SLOT_W, match, name=f"committer {window}")
    w = s.commit(SLOT_W, dataset, version, data, f"hourly-{rep}")
    if not engage(s, stall, HOLD_ENGAGE_TIMEOUT_S, f"the committer never reached the {window} point"):
        s.report()
    w.kill()
    s.proxy.drop(stall)
    r = s.resolve(SLOT_R, dataset, None)
    out = expect_exit(s, r, RESOLVE_DEADLINE_S, 0, "`resolve --latest` after the committer died")
    snap = check_settled(s, dataset, version, data)
    check_location(s, "`resolve --latest`", out, dataset, version, len(data), snap, True)
    s.report()


@pytest.mark.parametrize("rep", REPEATS)
def test_losing_committer(scenario, rep):
    s = scenario
    dataset, version = s.world.dataset("contended"), 20 + rep
    # Two replicas of one compactor job: same committer name, same-size manifests, other bytes.
    job = f"compactor-{rep}"
    winner, loser = manifest_bytes(job), manifest_bytes(job)
    reserve = s.proxy.hold(SLOT_R, ddb_write, name="loser's first index write")
    w2 = s.commit(SLOT_R, dataset, version, loser, job)
    if not engage(s, reserve, HOLD_ENGAGE_TIMEOUT_S, "the second committer never wrote the index"):
        s.report()
    w1 = s.commit(SLOT_W, dataset, version, winner, job)
    w1out = expect_exit(s, w1, COMMIT_DEADLINE_S, 0, "the winning commit")
    s.proxy.release(reserve)
    w2out = expect_exit(s, w2, COMMIT_DEADLINE_S, 3, "the losing commit")
    if w2out is not None and w2out.get("status") != "conflict":
        s.fail(f"the losing commit reported status {w2out.get('status')!r}; it lost the version")
    snap = check_settled(s, dataset, version, winner)
    check_committed(s, "the winning commit", w1out, dataset, version, len(winner), snap)
    check_fresh_reader(s, dataset, version, winner)
    s.report()


@pytest.mark.parametrize("rep", REPEATS)
def test_mismatched_canonical_bytes_refused(scenario, rep):
    s = scenario
    dataset, version = s.world.dataset("tampered"), 30 + rep
    data = manifest_bytes(f"ingest-{rep}")
    w = s.commit(SLOT_W, dataset, version, data, f"ingest-{rep}")
    expect_exit(s, w, COMMIT_DEADLINE_S, 0, "commit")
    r = s.resolve(SLOT_R, dataset, version)
    expect_exit(s, r, RESOLVE_DEADLINE_S, 0, "resolve before the overwrite")
    s.world.s3.put_object(Bucket=s.world.bucket, Key=s.world.canonical(dataset, version),
                          Body=data + b"x" * CORRUPT_SIZE_DELTA_BYTES)
    for label, which in (("`resolve --version`", version), ("`resolve --latest`", None)):
        bad = s.resolve(SLOT_R2, dataset, which)
        rc = bad.wait(RESOLVE_DEADLINE_S)
        if rc != 2:
            s.fail(f"{label} exited {rc} on a canonical manifest that is not the selected "
                   f"manifest (size {len(data) + CORRUPT_SIZE_DELTA_BYTES}, index selected "
                   f"{len(data)}); expected 2")
    s.report()


@pytest.mark.parametrize("rep", REPEATS)
def test_existing_row_naming_superseded_generation(scenario, rep):
    s = scenario
    world = s.world
    dataset, version = world.dataset("legacy"), 41 + rep
    data = manifest_bytes(f"orders-loader-{rep}")
    key = world.canonical(dataset, version)
    staging = f"{key}-{rep:04d}aa11-5c0e-4d3a-9b1f-6d2e8c7a{rep:04d}"
    world.s3.put_object(Bucket=world.bucket, Key=staging, Body=data)
    first = world.s3.copy_object(Bucket=world.bucket, Key=key,
                                 CopySource={"Bucket": world.bucket, "Key": staging})["VersionId"]
    world.s3.copy_object(Bucket=world.bucket, Key=key,
                         CopySource={"Bucket": world.bucket, "Key": staging})
    world.s3.delete_object(Bucket=world.bucket, Key=staging)
    world.ddb.put_item(TableName=world.table, Item={
        "base_uri": {"S": world.base_uri(dataset)}, "version": {"N": str(version)},
        "path": {"S": key}, "size": {"N": str(len(data))},
        "committer": {"S": "reader-query-7f9c"}, "generation": {"S": first},
    })
    for label, which in (("`resolve --version`", version), ("`resolve --latest`", None)):
        r = s.resolve(SLOT_R, dataset, which)
        out = expect_exit(s, r, RESOLVE_DEADLINE_S, 0, f"{label} of a version whose bytes are intact")
        snap = world.sample(dataset, version)
        check_location(s, label, out, dataset, version, len(data), snap, True)
        s.extend(bytes_problems(world, snap, data))
    s.report()


@pytest.mark.parametrize("rep", REPEATS)
def test_index_write_fails_after_copy(scenario, rep):
    s = scenario
    dataset, version = s.world.dataset("flaky-index"), 50 + rep
    data = manifest_bytes(f"nightly-merge-{rep}")
    failing = s.proxy.fail500(SLOT_W, ddb_write_after_own_copy, name="index write after copy")
    w = s.commit(SLOT_W, dataset, version, data, f"nightly-merge-{rep}")
    out = expect_exit(s, w, COMMIT_DEADLINE_S, 0,
                      "a commit whose manifest reached the canonical key but whose index update failed")
    if failing.fired == 0 and w.wait(0) is not None:
        s.fail("the committer never wrote the index after materializing its manifest")
    s.proxy.disable(failing)
    snap = s.world.sample(dataset, version)
    check_committed(s, "commit", out, dataset, version, len(data), snap)
    s.extend(row_truth_problems(snap))
    r = s.resolve(SLOT_R, dataset, None)
    rout = expect_exit(s, r, RESOLVE_DEADLINE_S, 0, "`resolve --latest` after the failed index update")
    snap = check_settled(s, dataset, version, data)
    check_location(s, "`resolve --latest`", rout, dataset, version, len(data), snap, True)
    s.report()


@pytest.mark.parametrize("window", ["before-copy", "before-index-write"])
@pytest.mark.parametrize("meanwhile", ["nothing", "reader", "batch-reader", "other-replica"])
def test_rerun_of_killed_commit(scenario, meanwhile, window):
    """The scheduler reruns a killed commit step with the same command line."""
    s = scenario
    dataset, version = s.world.dataset("rerun"), 60
    job = "stream-ingest"
    data = manifest_bytes(job)
    match = canonical_write if window == "before-copy" else ddb_write_after_own_copy
    stall = s.proxy.hold(SLOT_W, match, name=f"first attempt {window}")
    first = s.commit(SLOT_W, dataset, version, data, job)
    if not engage(s, stall, HOLD_ENGAGE_TIMEOUT_S, f"the first attempt never reached the {window} point"):
        s.report()
    first.kill()
    s.proxy.drop(stall)

    if meanwhile == "reader":
        r = s.resolve(SLOT_R, dataset, version)
        expect_exit(s, r, RESOLVE_DEADLINE_S, 0, "a reader that found the killed attempt's version")
    elif meanwhile == "batch-reader":
        r = s.resolve(SLOT_R, dataset, None, legacy=True)
        out = expect_exit(s, r, RESOLVE_DEADLINE_S, 0,
                          "the batch fleet's 0.14 reader (`resolve --latest`) on the pending version")
        if out is not None and (out.get("path") != s.world.canonical(dataset, version)
                                or out.get("size") != len(data)):
            s.fail(f"the 0.14 reader printed {out!r} for version {version}")
    elif meanwhile == "other-replica":
        other = manifest_bytes(job)
        w2 = s.commit(SLOT_R, dataset, version, other, job)
        w2out = expect_exit(s, w2, COMMIT_DEADLINE_S, 3,
                            "a replica committing a different manifest to the version the killed "
                            "attempt reserved")
        if w2out is not None and w2out.get("status") != "conflict":
            s.fail(f"that replica reported status {w2out.get('status')!r}; its manifest is not the version")

    rerun = s.commit(SLOT_W, dataset, version, data, job)
    out = expect_exit(s, rerun, COMMIT_DEADLINE_S, 0,
                      "the rerun of the killed commit (same command line; its manifest holds the version)")
    snap = s.world.sample(dataset, version)
    check_committed(s, "the rerun", out, dataset, version, len(data), snap)
    s.extend(row_truth_problems(snap))
    check_fresh_reader(s, dataset, version, data)
    check_settled(s, dataset, version, data)
    s.report()
