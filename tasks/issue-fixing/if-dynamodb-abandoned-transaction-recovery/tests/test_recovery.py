"""Grades real DynamoDB state left behind by the transaction program under
the incident schedule. Four independent groups; each builds its own
fixtures, drives its own processes, and fails on its own.

Group order matters: the coupled schedule runs first.
"""

import os

from support import (AGE_MARGIN_MS, PARTICIPANTS, Proc, ROLLBACK_AFTER_MS,
                     RECOVERY_DEADLINE, SHORT_RETENTION_MS, Dynamo, SeamHub,
                     poll_until)

MARKER_DIR = "/logs/verifier"


def mark_environment(reason):
    try:
        os.makedirs(MARKER_DIR, exist_ok=True)
        with open(os.path.join(MARKER_DIR, "environment.txt"), "w") as fh:
            fh.write(str(reason) + "\n")
    except OSError:
        pass


def describe(db, names, txids):
    lines = []
    for txid in txids:
        record = db.tx(names, txid)
        if record is None:
            lines.append("%s: <absent>" % txid)
        else:
            lines.append("%s: state=%s finalized=%s version=%s" % (
                txid, record.get("state"), record.get("finalized"),
                record.get("version")))
    locked = db.locked_items(names)
    lines.append("items still carrying lock markers: %s" % (locked or "none"))
    lines.append("image rows remaining: %d" % db.image_count(names))
    return "; ".join(lines)


def drive_pending(proc, txid, items, value, apply_count, await_applies=None):
    """Record the full intent, then apply the first apply_count requests.

    await_applies lets the driver leave the last apply unanswered, which
    happens when the process is parked at its post-apply pause.
    """
    if await_applies is None:
        await_applies = apply_count
    proc.send(cmd="begin", tx=txid)
    proc.response("begin")
    for item_id in items:
        proc.send(cmd="append", tx=txid, item=item_id, val=value)
    for _ in items:
        proc.response("append")
    for item_id in items[:apply_count]:
        proc.send(cmd="apply", tx=txid, item=item_id)
    for _ in range(await_applies):
        proc.response("apply")


def test_service_responds():
    db = Dynamo()
    try:
        names = db.create_tables("pre")
        db.seed(names["items"], ["probe"], 1)
        row = db.get(names["items"], {"item_id": "probe"})
        assert row is not None and int(row["val"]) == 1
        db.drop_tables(names)
    except Exception as err:
        mark_environment(err)
        raise


def test_coupled_schedule(tmp_path):
    db = Dynamo()
    names = db.create_tables("main")
    socket_path = str(tmp_path / "main.sock")
    hub = SeamHub(socket_path)
    tx_a = "txa-" + os.urandom(3).hex()
    tx_b = "txb-" + os.urandom(3).hex()
    tx_r = "txr-" + os.urandom(3).hex()
    a_items = ["pa%s%04d" % (os.urandom(2).hex(), i) for i in range(PARTICIPANTS)]
    b_items = ["pb%s%04d" % (os.urandom(2).hex(), i) for i in range(PARTICIPANTS)]
    db.seed(names["items"], a_items + b_items, 0)
    procs = []
    try:
        # --- setup: two live coordinators over disjoint item groups
        a = Proc("A", "coordinator", names, socket_path)
        b = Proc("B", "coordinator", names, socket_path)
        procs.extend([a, b])

        hub.set_rules([
            lambda f: f["seam"] == "before_decide" and f["pid"] == a.pid])
        drive_pending(a, tx_a, a_items, 11, PARTICIPANTS)
        a.send(cmd="commit", tx=tx_a)
        a_before_decide = hub.expect(
            lambda f: f["seam"] == "before_decide" and f["pid"] == a.pid,
            what="A pausing before its decision")
        assert int(a_before_decide["version"]) == PARTICIPANTS

        hub.set_rules([
            lambda f: f["seam"] == "after_apply" and f["pid"] == b.pid
            and f.get("rid") == 2])
        drive_pending(b, tx_b, b_items, 22, 2, await_applies=1)
        hub.expect(
            lambda f: f["seam"] == "after_apply" and f["pid"] == b.pid
            and f.get("rid") == 2,
            what="B pausing after its second apply")

        # the durable barriers the incident stands on
        record = db.tx(names, tx_a)
        assert record["state"] == "PENDING" and int(record["version"]) == PARTICIPANTS
        for item_id in a_items:
            row = db.get(names["items"], {"item_id": item_id})
            assert row.get("owner") == tx_a and "applied" in row, item_id
            assert int(row["val"]) == 11, item_id
        for item_id in b_items[:2]:
            row = db.get(names["items"], {"item_id": item_id})
            assert row.get("owner") == tx_b and "applied" in row, item_id
            assert int(row["val"]) == 22, item_id
        for item_id in b_items[2:]:
            row = db.get(names["items"], {"item_id": item_id})
            assert "owner" not in row and int(row["val"]) == 0, item_id
        for rid in (1, 2):
            image = db.get(names["images"], {"img": "%s#%d" % (tx_b, rid)})
            assert image is not None and int(image["val"]) == 0

        b.kill()  # no decision and no acknowledgement exists for B

        # --- both records age past the intervention threshold
        assert db.wait_age(names, [tx_a, tx_b], ROLLBACK_AFTER_MS + AGE_MARGIN_MS)

        # --- a recovery process snapshots the still-pending record
        s1 = Proc("S1", "sweeper", names, socket_path)
        procs.append(s1)
        hub.set_rules([
            lambda f: f["seam"] == "sweep_candidate" and f["pid"] == s1.pid
            and f["txid"] == tx_a])
        s1.send(cmd="sweep", tx=tx_a)
        s1_candidate = hub.expect(
            lambda f: f["seam"] == "sweep_candidate" and f["pid"] == s1.pid,
            what="S1 snapshotting its candidate")
        assert s1_candidate["state"] == "PENDING"
        assert int(s1_candidate["version"]) == PARTICIPANTS

        # --- the true coordinator wins its decision and dies before cleanup
        hub.set_rules([
            lambda f: f["seam"] == "after_decide" and f["pid"] == a.pid])
        hub.release(a_before_decide)
        hub.expect(lambda f: f["seam"] == "after_decide" and f["pid"] == a.pid,
                   what="A pausing after its decision")
        record = db.tx(names, tx_a)
        assert record["state"] == "COMMITTED", record
        assert "finalized" not in record
        for item_id in a_items:
            row = db.get(names["items"], {"item_id": item_id})
            assert row.get("owner") == tx_a and int(row["val"]) == 11, item_id
        a.kill()
        observed = dict(record)

        # --- stale recovery resumes: it must not undo what was observed
        hub.set_rules([])
        hub.release(s1_candidate)
        s1.response("sweep")
        record = db.tx(names, tx_a)
        assert record["state"] == "COMMITTED", (
            "a decision observed as %s with locks held later became %s"
            % (observed["state"], record.get("state")))

        # --- the same recovery process moves on to the abandoned record
        hub.set_rules([
            lambda f: f["seam"] == "before_restore" and f["pid"] == s1.pid
            and f["txid"] == tx_b and f.get("rid") == 1])
        s1.send(cmd="sweep", tx=tx_b)
        s1_restore0 = hub.expect(
            lambda f: f["seam"] == "before_restore" and f["pid"] == s1.pid
            and f.get("rid") == 1,
            what="S1 holding the first saved copy of the abandoned record")

        # --- a second recovery process finishes that item and stops short
        s2 = Proc("S2", "sweeper", names, socket_path)
        procs.append(s2)
        hub.set_rules([
            lambda f: f["seam"] == "before_restore" and f["pid"] == s2.pid
            and f["txid"] == tx_b and f.get("rid") == 2])
        s2.send(cmd="sweep", tx=tx_b)
        s2_restore1 = hub.expect(
            lambda f: f["seam"] == "before_restore" and f["pid"] == s2.pid
            and f.get("rid") == 2,
            what="S2 pausing before the second restore")
        row = db.get(names["items"], {"item_id": b_items[0]})
        assert int(row["val"]) == 0 and "owner" not in row, row
        assert db.get(names["images"], {"img": "%s#1" % tx_b}) is None

        # --- a fresh coordinator writes to that item and is acknowledged
        r = Proc("R", "coordinator", names, socket_path)
        procs.append(r)
        r.send(cmd="begin", tx=tx_r)
        r.response("begin")
        r.send(cmd="append", tx=tx_r, item=b_items[0], val=33)
        r.response("append")
        r.send(cmd="apply", tx=tx_r, item=b_items[0])
        r.response("apply")
        r.send(cmd="commit", tx=tx_r)
        acknowledged = r.response("commit")
        assert acknowledged.get("result") == "COMMITTED", acknowledged
        row = db.get(names["items"], {"item_id": b_items[0]})
        assert int(row["val"]) == 33 and "owner" not in row, row

        # --- the stale restore resumes against the acknowledged write
        hub.set_rules([
            lambda f: f["seam"] == "after_restore" and f["pid"] == s1.pid
            and f["txid"] == tx_b and f.get("rid") == 2])
        hub.release(s1_restore0)
        hub.expect(lambda f: f["seam"] == "after_restore" and f["pid"] == s1.pid
                   and f.get("rid") == 2,
                   what="S1 pausing after the second restore")
        row = db.get(names["items"], {"item_id": b_items[0]})
        assert int(row["val"]) == 33 and "owner" not in row, (
            "the acknowledged successor write was reverted: %s" % row)
        row = db.get(names["items"], {"item_id": b_items[1]})
        assert int(row["val"]) == 0 and "owner" not in row, row
        assert db.get(names["images"], {"img": "%s#2" % tx_b}) is not None
        s1.kill()  # dies between a restore and the drop of its saved copy

        # --- the survivor finishes everything within the window
        hub.set_rules([])
        hub.release(s2_restore1)
        s2.response("sweep")
        s2.send(cmd="run")

        def quiescent():
            record_a = db.tx(names, tx_a)
            record_b = db.tx(names, tx_b)
            record_r = db.tx(names, tx_r)
            if record_a is None or record_b is None or record_r is None:
                return "a record was deleted"
            if record_a.get("state") != "COMMITTED" or not record_a.get("finalized"):
                return describe(db, names, [tx_a, tx_b, tx_r])
            if record_b.get("state") != "ROLLED_BACK" or not record_b.get("finalized"):
                return describe(db, names, [tx_a, tx_b, tx_r])
            if record_r.get("state") != "COMMITTED" or not record_r.get("finalized"):
                return describe(db, names, [tx_a, tx_b, tx_r])
            if db.locked_items(names):
                return describe(db, names, [tx_a, tx_b, tx_r])
            if db.image_count(names):
                return describe(db, names, [tx_a, tx_b, tx_r])
            return True

        done, detail = poll_until(quiescent, RECOVERY_DEADLINE)
        assert done, "recovery unfinished after %.0fs: %s" % (
            RECOVERY_DEADLINE, detail)

        values = db.item_values(names)
        wrong = {item_id: values.get(item_id)
                 for item_id in a_items if values.get(item_id) != 11}
        assert not wrong, "committed values were not all preserved: %s" % wrong
        assert values[b_items[0]] == 33, values[b_items[0]]
        wrong = {item_id: values.get(item_id)
                 for item_id in b_items[1:] if values.get(item_id) != 0}
        assert not wrong, "abandoned values were not all restored: %s" % wrong
        assert not db.locked_items(names), db.locked_items(names)
        assert db.image_count(names) == 0
    finally:
        for proc in procs:
            proc.kill()
        hub.stop()
        db.drop_tables(names)


def test_decision_wins_over_late_commit(tmp_path):
    db = Dynamo()
    names = db.create_tables("rev")
    socket_path = str(tmp_path / "rev.sock")
    hub = SeamHub(socket_path)
    txid = "txc-" + os.urandom(3).hex()
    items = ["pc%s%04d" % (os.urandom(2).hex(), i) for i in range(PARTICIPANTS)]
    db.seed(names["items"], items, 0)
    procs = []
    try:
        coordinator = Proc("C", "coordinator", names, socket_path)
        sweeper = Proc("S", "sweeper", names, socket_path)
        procs.extend([coordinator, sweeper])

        hub.set_rules([
            lambda f: f["seam"] == "before_decide" and f["pid"] == coordinator.pid])
        drive_pending(coordinator, txid, items, 77, PARTICIPANTS)
        coordinator.send(cmd="commit", tx=txid)
        held = hub.expect(
            lambda f: f["seam"] == "before_decide" and f["pid"] == coordinator.pid,
            what="coordinator pausing before its decision")

        assert db.wait_age(names, [txid], ROLLBACK_AFTER_MS + AGE_MARGIN_MS)
        hub.set_rules([])
        sweeper.send(cmd="sweep", tx=txid)
        sweeper.response("sweep")
        record = db.tx(names, txid)
        assert record["state"] == "ROLLED_BACK", record
        assert record.get("finalized"), record
        values = db.item_values(names)
        wrong = {item_id: values.get(item_id)
                 for item_id in items if values.get(item_id) != 0}
        assert not wrong, wrong
        assert not db.locked_items(names)
        assert db.image_count(names) == 0

        hub.release(held)
        result = coordinator.response("commit")
        assert result.get("result") != "COMMITTED", (
            "a commit that lost its decision claimed success: %s" % result)
        record = db.tx(names, txid)
        assert record["state"] == "ROLLED_BACK" and record.get("finalized"), record
        values = db.item_values(names)
        wrong = {item_id: values.get(item_id)
                 for item_id in items if values.get(item_id) != 0}
        assert not wrong, wrong
        assert not db.locked_items(names)
        assert db.image_count(names) == 0
    finally:
        for proc in procs:
            proc.kill()
        hub.stop()
        db.drop_tables(names)


def test_decision_covers_only_verified_requests(tmp_path):
    db = Dynamo()
    names = db.create_tables("ver")
    socket_path = str(tmp_path / "ver.sock")
    hub = SeamHub(socket_path)
    txid = "txv-" + os.urandom(3).hex()
    items = ["pv%s%04d" % (os.urandom(2).hex(), i) for i in range(PARTICIPANTS)]
    db.seed(names["items"], items, 0)
    extra = items[-1]
    settled = items[:-1]
    procs = []
    try:
        first = Proc("V1", "coordinator", names, socket_path)
        second = Proc("V2", "coordinator", names, socket_path)
        procs.extend([first, second])

        hub.set_rules([
            lambda f: f["seam"] == "before_decide" and f["pid"] == first.pid])
        drive_pending(first, txid, settled, 55, len(settled))
        first.send(cmd="commit", tx=txid)
        held = hub.expect(
            lambda f: f["seam"] == "before_decide" and f["pid"] == first.pid,
            what="first coordinator pausing before its decision")
        assert int(held["version"]) == len(settled)

        hub.set_rules([
            lambda f: f["seam"] == "after_append" and f["pid"] == second.pid])
        second.send(cmd="append", tx=txid, item=extra, val=55)
        appended = hub.expect(
            lambda f: f["seam"] == "after_append" and f["pid"] == second.pid,
            what="second coordinator appending one more request")
        assert int(appended["version"]) == PARTICIPANTS

        hub.set_rules([
            lambda f: f["seam"] == "after_decide_attempt" and f["pid"] == first.pid])
        hub.release(held)
        attempt = hub.expect(
            lambda f: f["seam"] == "after_decide_attempt" and f["pid"] == first.pid,
            what="the decision attempt landing")
        assert attempt.get("succeeded") is False, (
            "a decision landed for a request set the caller never verified: %s" % attempt)
        record = db.tx(names, txid)
        assert record["state"] == "PENDING" and int(record["version"]) == PARTICIPANTS
        row = db.get(names["items"], {"item_id": extra})
        assert int(row.get("val", 0)) == 0, row

        hub.set_rules([])
        hub.release(appended)
        second.send(cmd="apply", tx=txid, item=extra)
        second.response("apply")
        hub.release(attempt)
        result = first.response("commit")
        assert result.get("result") == "COMMITTED", result

        def complete():
            record = db.tx(names, txid)
            if (record is None or record.get("state") != "COMMITTED"
                    or not record.get("finalized")):
                return describe(db, names, [txid])
            if db.locked_items(names) or db.image_count(names):
                return describe(db, names, [txid])
            return True

        done, detail = poll_until(complete, RECOVERY_DEADLINE)
        assert done, "transaction did not complete after the race: %s" % detail
        values = db.item_values(names)
        wrong = {item_id: values.get(item_id)
                 for item_id in items if values.get(item_id) != 55}
        assert not wrong, wrong
    finally:
        for proc in procs:
            proc.kill()
        hub.stop()
        db.drop_tables(names)


def test_parked_writer_outlives_its_record(tmp_path):
    """A coordinator is parked between taking a participant's lock and
    saving its backup. Recovery finishes the transaction, the record is
    reaped at the retention time, and only then does the parked writer
    wake up and finish its request against tables that no longer know the
    transaction."""
    db = Dynamo()
    names = db.create_tables("zk")
    socket_path = str(tmp_path / "zk.sock")
    hub = SeamHub(socket_path)
    tx_z = "txz-" + os.urandom(3).hex()
    tx_r = "txw-" + os.urandom(3).hex()
    z_items = ["pz%s%04d" % (os.urandom(2).hex(), i) for i in range(3)]
    db.seed(names["items"], z_items, 0)
    procs = []
    try:
        # --- a live coordinator records three participants and applies one
        z = Proc("Z", "coordinator", names, socket_path)
        procs.append(z)
        hub.set_rules([
            lambda f: f["seam"] == "after_lock" and f["pid"] == z.pid
            and f.get("rid") == 2])
        drive_pending(z, tx_z, z_items, 44, 1)
        z.send(cmd="commit", tx=tx_z)
        z_park = hub.expect(
            lambda f: f["seam"] == "after_lock" and f["pid"] == z.pid
            and f.get("rid") == 2,
            what="Z parked after locking its second participant")

        # the durable barriers the schedule stands on
        record = db.tx(names, tx_z)
        assert record["state"] == "PENDING" and int(record["version"]) == 3
        row = db.get(names["items"], {"item_id": z_items[0]})
        assert row.get("owner") == tx_z and "applied" in row, row
        assert int(row["val"]) == 44, row
        row = db.get(names["items"], {"item_id": z_items[1]})
        assert row.get("owner") == tx_z and "applied" not in row, row
        assert db.get(names["images"], {"img": "%s#2" % tx_z}) is None
        assert db.get(names["images"], {"img": "%s#1" % tx_z}) is not None

        # --- recovery finishes the transaction while its writer is parked
        hub.set_rules([])
        assert db.wait_age(names, [tx_z], ROLLBACK_AFTER_MS + AGE_MARGIN_MS)
        s = Proc("S", "sweeper", names, socket_path,
                 extra_args=["--retention-ms", str(SHORT_RETENTION_MS)])
        procs.append(s)
        s.send(cmd="sweep", tx=tx_z)
        s.response("sweep")
        record = db.tx(names, tx_z)
        assert record["state"] == "ROLLED_BACK", record
        row = db.get(names["items"], {"item_id": z_items[0]})
        assert int(row["val"]) == 0 and "owner" not in row, row

        # --- the finished record is reaped at the retention time
        assert db.wait_age(names, [tx_z], SHORT_RETENTION_MS + AGE_MARGIN_MS)
        s.send(cmd="sweep", tx=tx_z)
        s.response("sweep")

        # --- only now does the parked writer wake up
        hub.release(z_park)
        z.response("commit")

        # --- a fresh transaction must be able to use the participant
        r = Proc("W", "coordinator", names, socket_path)
        procs.append(r)
        r.send(cmd="begin", tx=tx_r)
        r.response("begin")
        r.send(cmd="append", tx=tx_r, item=z_items[1], val=77)
        r.response("append")
        r.send(cmd="apply", tx=tx_r, item=z_items[1])
        r.response("apply")
        r.send(cmd="commit", tx=tx_r)
        acknowledged = r.response("commit")
        assert acknowledged.get("result") == "COMMITTED", (
            "a participant whose owning record is gone refused writes for "
            "good: %s" % acknowledged)

        # --- the tables must come back to a state anyone can account for
        s.send(cmd="run")

        def quiescent():
            record_r = db.tx(names, tx_r)
            if record_r is not None and not record_r.get("finalized"):
                return describe(db, names, [tx_z, tx_r])
            if db.locked_items(names):
                return describe(db, names, [tx_z, tx_r])
            if db.image_count(names):
                return describe(db, names, [tx_z, tx_r])
            return True

        done, detail = poll_until(quiescent, RECOVERY_DEADLINE)
        assert done, "recovery unfinished after %.0fs: %s" % (
            RECOVERY_DEADLINE, detail)

        values = db.item_values(names)
        assert values[z_items[0]] == 0, values[z_items[0]]
        assert values[z_items[1]] == 77, values[z_items[1]]
        assert values[z_items[2]] == 0, values[z_items[2]]
        assert not db.locked_items(names), db.locked_items(names)
        assert db.image_count(names) == 0
    finally:
        for proc in procs:
            proc.kill()
        hub.stop()
        db.drop_tables(names)


def test_writer_locks_after_its_record_is_reaped(tmp_path):
    """A coordinator is parked mid catch-up while recovery rolls its record
    back and the retention reaps it. Released, it takes the lock of its next
    participant and dies there, leaving a marker for a record nobody has."""
    db = Dynamo()
    names = db.create_tables("zr")
    socket_path = str(tmp_path / "zr.sock")
    hub = SeamHub(socket_path)
    tx_z = "txy-" + os.urandom(3).hex()
    tx_r = "txq-" + os.urandom(3).hex()
    z_items = ["py%s%04d" % (os.urandom(2).hex(), i) for i in range(3)]
    db.seed(names["items"], z_items, 0)
    procs = []
    try:
        # --- the coordinator is parked after applying its second participant
        z = Proc("Z", "coordinator", names, socket_path)
        procs.append(z)
        hub.set_rules([
            lambda f: f["seam"] == "after_apply" and f["pid"] == z.pid
            and f.get("rid") == 2])
        drive_pending(z, tx_z, z_items, 44, 1)
        z.send(cmd="commit", tx=tx_z)
        z_park = hub.expect(
            lambda f: f["seam"] == "after_apply" and f["pid"] == z.pid
            and f.get("rid") == 2,
            what="Z parked after applying its second participant")
        for item_id in z_items[:2]:
            row = db.get(names["items"], {"item_id": item_id})
            assert row.get("owner") == tx_z and "applied" in row, row
        row = db.get(names["items"], {"item_id": z_items[2]})
        assert "owner" not in row, row

        # --- recovery rolls it back and the retention reaps the record
        hub.set_rules([])
        assert db.wait_age(names, [tx_z], ROLLBACK_AFTER_MS + AGE_MARGIN_MS)
        s = Proc("S", "sweeper", names, socket_path,
                 extra_args=["--retention-ms", str(SHORT_RETENTION_MS)])
        procs.append(s)
        s.send(cmd="sweep", tx=tx_z)
        s.response("sweep")
        record = db.tx(names, tx_z)
        assert record["state"] == "ROLLED_BACK", record
        assert db.wait_age(names, [tx_z], SHORT_RETENTION_MS + AGE_MARGIN_MS)
        s.send(cmd="sweep", tx=tx_z)
        s.response("sweep")
        assert db.tx(names, tx_z) is None

        # --- the writer wakes, locks its next participant and is killed there
        hub.set_rules([
            lambda f: f["seam"] == "after_lock" and f["pid"] == z.pid
            and f.get("rid") == 3])
        hub.release(z_park)
        hub.expect(
            lambda f: f["seam"] == "after_lock" and f["pid"] == z.pid
            and f.get("rid") == 3,
            what="Z locking its third participant after the reap")
        z.kill()
        hub.set_rules([])

        # --- recovery alone must hand the participant back
        s.send(cmd="run")

        def reclaimed():
            if db.locked_items(names) or db.image_count(names):
                return describe(db, names, [tx_z])
            return True

        done, detail = poll_until(reclaimed, RECOVERY_DEADLINE)
        assert done, "recovery left a lock or image of a reaped record after %.0fs: %s" % (
            RECOVERY_DEADLINE, detail)
        s.send(cmd="stop")
        s.response("stop")

        r = Proc("W", "coordinator", names, socket_path)
        procs.append(r)
        r.send(cmd="begin", tx=tx_r)
        r.response("begin")
        r.send(cmd="append", tx=tx_r, item=z_items[2], val=77)
        r.response("append")
        r.send(cmd="apply", tx=tx_r, item=z_items[2])
        r.response("apply")
        r.send(cmd="commit", tx=tx_r)
        acknowledged = r.response("commit")
        assert acknowledged.get("result") == "COMMITTED", acknowledged

        values = db.item_values(names)
        assert [values[item_id] for item_id in z_items] == [0, 0, 77], values
        assert not db.locked_items(names), db.locked_items(names)
        assert db.image_count(names) == 0
    finally:
        for proc in procs:
            proc.kill()
        hub.stop()
        db.drop_tables(names)
