"""Verifier for the delayed-task runner.

Drives worker processes against one queue and one table through a fault
schedule, then grades what the services themselves show: the original queue
message of the destroyed holder's task, the task rows, and the processes'
own liveness. Every observation recorded here comes from a real service call
or a process event; nothing is read from the workers' code.
"""

import json
import os
from pathlib import Path
import queue as queue_mod
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import boto3

from cdk_seed.cfn_deploy import deploy
from botocore.config import Config

ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

QUEUE_VIS_S = 4
RETENTION_S = 300
SETUP_BOUND_S = 3.0
KILL_OFFSET_S = 4.0
RECOVERY_BOUND_S = 8.0
PAUSE_OFFSET_S = 14.0
PAUSE_DURATION_S = 12.0
SECOND_COPY_OFFSET_S = 10.0
SECOND_COPY_LATEST_S = 12.5
REPLACEMENT_OFFSET_S = 15.5
REPLACEMENT_STOP_OFFSET_S = 35.5
REPLACEMENT_STOP_S = 1.4
ETA_C_S = 20.0
ETA_L_S = 36.0
LATE_TOL_S = 2.0
WATCH_S = 8.0
STEP_S = 0.2
MIN_RECEIVE_COUNT = 2

LOG_DIR = "/logs/verifier"


def client(service):
    return boto3.client(
        service,
        endpoint_url=ENDPOINT,
        region_name=REGION,
        config=Config(connect_timeout=5, read_timeout=5, retries={"max_attempts": 2}),
    )


class Worker:
    """One managed worker process group."""

    def __init__(self, worker_id, queue_url, table, run_id):
        self.worker_id = worker_id
        self.events = queue_mod.Queue()
        self.lines = []
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "/app/worker.py",
                "--worker-id",
                worker_id,
                "--queue-url",
                queue_url,
                "--table",
                table,
                "--run-id",
                run_id,
                "--endpoint",
                ENDPOINT,
                "--region",
                REGION,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=tempfile.mkdtemp(prefix="w-%s-" % worker_id),
            env={**os.environ, "PYTHONPATH": "/app"},
            start_new_session=True,
            text=True,
            bufsize=1,
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            self.lines.append(event)
            self.events.put(event)

    def send(self, command):
        try:
            self.proc.stdin.write(json.dumps(command) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass

    def wait_for(self, kind, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = [e for e in self.lines if e.get("event") == kind]
            if found:
                return found[-1]
            time.sleep(0.02)
        return None

    def signal_group(self, signum):
        try:
            os.killpg(os.getpgid(self.proc.pid), signum)
        except (ProcessLookupError, PermissionError):
            pass

    def alive(self):
        return self.proc.poll() is None


def probe_receive(sqs, queue_url):
    """One plain receive on the original queue, no visibility override."""
    resp = sqs.receive_message(
        QueueUrl=queue_url,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=0,
        AttributeNames=["All"],
    )
    return resp.get("Messages", []) or []


def drive():
    out = {
        "run_id": None,
        "setup_ok": False,
        "setup_note": None,
        "environment_live": True,
        "queue_attributes": {},
        "message_ids": {},
        "task_ids": {},
        "due": {},
        "samples": [],
        "observations": [],
        "first_count": {},
        "max_count": {},
        "first_standby": {},
        "final": {},
        "kill_rel": None,
        "survivors_alive": {},
        "worker_lines": {},
        "faults_applied": [],
        "copies": [],
        "copy_note": None,
        "replacement_ready_rel": None,
        "replacement_took_rel": None,
        "replacement_stop_rel": None,
        "replacement_cont_rel": None,
    }
    run_id = "r%s" % uuid.uuid4().hex[:10]
    out["run_id"] = run_id
    try:
        sqs = client("sqs")
        ddb = client("dynamodb")
        sqs.list_queues()
        ddb.list_tables()
    except Exception as exc:
        out["environment_live"] = False
        out["setup_note"] = "services unreachable: %s" % exc
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(os.path.join(LOG_DIR, "environment.txt"), "w") as handle:
                handle.write("services unreachable: %s\n" % exc)
        except OSError:
            pass
        return out

    table = "tasks-%s" % run_id
    seed_outputs = deploy(client('cloudformation'), 'seed-' + run_id,
                          Path(__file__).parent / 'cdk_seed/seed.template.json',
                          TableName=table, QueueName='tasks-' + run_id,
                          VisibilityTimeout=QUEUE_VIS_S, RetentionSeconds=RETENTION_S)
    wait = time.monotonic() + 30
    while time.monotonic() < wait:
        if ddb.describe_table(TableName=table)["Table"]["TableStatus"] == "ACTIVE":
            break
        time.sleep(0.2)

    queue_name = "tasks-%s" % run_id
    queue_url = seed_outputs['QueueUrl']
    attrs = sqs.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=[
            "VisibilityTimeout",
            "DelaySeconds",
            "MessageRetentionPeriod",
            "RedrivePolicy",
        ],
    ).get("Attributes", {})
    out["queue_attributes"] = attrs

    task_l = "L-%s" % uuid.uuid4().hex[:6]
    task_c = "C-%s" % uuid.uuid4().hex[:6]
    out["task_ids"] = {"L": task_l, "C": task_c}

    workers = {}
    try:
        for wid in ("A", "B", "S"):
            workers[wid] = Worker(wid, queue_url, table, run_id)
        for wid in ("A", "B", "S"):
            if not workers[wid].wait_for("READY", 20):
                out["setup_note"] = "%s never became ready" % wid
                return out

        t0 = time.time()
        out["t0"] = t0
        out["due"] = {"L": t0 + ETA_L_S, "C": t0 + ETA_C_S}

        def publish(task_id, due_at):
            body = {
                "run_id": run_id,
                "task_id": task_id,
                "due_at": due_at,
                "payload": {"note": task_id},
            }
            resp = sqs.send_message(
                QueueUrl=queue_url, MessageBody=json.dumps(body)
            )
            ddb.put_item(
                TableName=table,
                Item={
                    "pk": {"S": "RUN#%s#TASK#%s" % (run_id, task_id)},
                    "task_id": {"S": task_id},
                    "message_id": {"S": resp["MessageId"]},
                    "due_at": {"N": repr(float(due_at))},
                    "phase": {"S": "PENDING"},
                    "effect_count": {"N": "0"},
                },
            )
            return resp["MessageId"]

        out["message_ids"]["L"] = publish(task_l, t0 + ETA_L_S)
        granted = {"L": False, "C": False}
        while time.time() < t0 + SETUP_BOUND_S and not granted["L"]:
            workers["A"].send({"cmd": "RECEIVE"})
            event = workers["A"].wait_for("HELD", 2.0)
            if event and event.get("task_id") == task_l:
                granted["L"] = True
        out["message_ids"]["C"] = publish(task_c, t0 + ETA_C_S)
        while time.time() < t0 + SETUP_BOUND_S and not granted["C"]:
            workers["B"].send({"cmd": "RECEIVE"})
            event = workers["B"].wait_for("HELD", 2.0)
            if event and event.get("task_id") == task_c:
                granted["C"] = True
        if not (granted["L"] and granted["C"]):
            out["setup_note"] = "holders did not take their deliveries in time"
            return out
        out["setup_ok"] = True

        def note_observation(task_id, message, kind="observations"):
            attrs_in = message.get("Attributes", {})
            record = {
                "task_id": task_id,
                "message_id": message.get("MessageId"),
                "receipt_handle": message.get("ReceiptHandle"),
                "receive_count": int(attrs_in.get("ApproximateReceiveCount", "0")),
                "observed_at": time.time(),
                "body": message.get("Body"),
            }
            out[kind].append(record)
            ddb.put_item(
                TableName=table,
                Item={
                    "pk": {
                        "S": "RUN#%s#OBS#%s#%s"
                        % (run_id, task_id, uuid.uuid4().hex[:8])
                    },
                    "task_id": {"S": task_id},
                    "message_id": {"S": str(record["message_id"])},
                    "receive_count": {"N": str(record["receive_count"])},
                    "observed_at": {"N": repr(record["observed_at"])},
                    "receipt_handle": {"S": str(record["receipt_handle"])},
                },
            )
            return record

        def deliver_again(raw):
            for _ in range(5):
                try:
                    sqs.change_message_visibility(
                        QueueUrl=queue_url,
                        ReceiptHandle=raw["ReceiptHandle"],
                        VisibilityTimeout=0,
                    )
                except Exception as exc:
                    return "could not deliver again: %s" % type(exc).__name__
                for message in probe_receive(sqs, queue_url):
                    body = json.loads(message["Body"])
                    if body.get("run_id") != run_id:
                        continue
                    if body["task_id"] == task_c:
                        note_observation(task_c, message, kind="copies")
                        workers["A"].send({"cmd": "ADOPT", "message": message})
                        return "delivered"
                    note_observation(body["task_id"], message)
                    workers["S"].send({"cmd": "ADOPT", "message": message})
                time.sleep(0.05)
            return "not received again"

        kill_at = time.monotonic() + KILL_OFFSET_S
        probe_until = None
        paused = False
        resumed = False
        replacement_spawned = False
        latest_raw = {}
        latest_rows = {}
        copy_done = False
        r_stopped = False
        r_continued = False
        last_probe = 0.0
        last_sample = 0.0
        end_at = time.monotonic() + ETA_L_S + LATE_TOL_S + WATCH_S

        while time.monotonic() < end_at:
            now_mono = time.monotonic()
            rel = time.time() - t0

            if now_mono >= kill_at and "kill" not in out["faults_applied"]:
                workers["B"].signal_group(signal.SIGKILL)
                workers["B"].proc.wait(timeout=10)
                out["kill_rel"] = time.time() - t0
                out["faults_applied"].append("kill")
                probe_until = time.monotonic() + RECOVERY_BOUND_S

            if probe_until is not None and now_mono < probe_until:
                if now_mono - last_probe >= STEP_S:
                    last_probe = now_mono
                    for message in probe_receive(sqs, queue_url):
                        body = json.loads(message["Body"])
                        if body.get("run_id") != run_id:
                            continue
                        note_observation(body["task_id"], message)
                        latest_raw[body["task_id"]] = message
                        workers["S"].send({"cmd": "ADOPT", "message": message})

            # The queue hands the recovered task out once more while its
            # standby holds it and is running; the copy goes to A.
            if not copy_done and rel >= SECOND_COPY_OFFSET_S:
                c_row = latest_rows.get("C") or {}
                raw = latest_raw.get(task_c)
                if rel >= SECOND_COPY_LATEST_S:
                    copy_done = True
                    out["copy_note"] = "no held copy to deliver again"
                elif raw is not None and c_row.get("holder") == "S":
                    copy_done = True
                    out["copy_note"] = deliver_again(raw)

            if rel >= PAUSE_OFFSET_S and not paused:
                workers["A"].signal_group(signal.SIGSTOP)
                paused = True
                out["faults_applied"].append("pause")
            if rel >= PAUSE_OFFSET_S + PAUSE_DURATION_S and paused and not resumed:
                workers["A"].signal_group(signal.SIGCONT)
                resumed = True
                out["faults_applied"].append("resume")

            # A holder that has stopped answering is replaced: a new process
            # under the same worker id, taking work like any other worker.
            # The suspended holder's delivery comes back to the queue and the
            # replacement is the one that receives it.
            if not replacement_spawned and rel >= REPLACEMENT_OFFSET_S:
                replacement_spawned = True
                workers["A2"] = Worker("A", queue_url, table, run_id)
                workers["A2"].send({"cmd": "POLL", "enable": True})
                out["faults_applied"].append("replace")
            if replacement_spawned:
                lines = workers["A2"].lines
                if out["replacement_ready_rel"] is None and any(
                    e.get("event") == "READY" for e in lines
                ):
                    out["replacement_ready_rel"] = time.time() - t0
                if out["replacement_took_rel"] is None and any(
                    e.get("event") == "HELD" and e.get("task_id") == task_l
                    for e in lines
                ):
                    out["replacement_took_rel"] = time.time() - t0

            if (
                rel >= REPLACEMENT_STOP_OFFSET_S
                and out["replacement_took_rel"] is not None
                and not r_stopped
            ):
                workers["A2"].signal_group(signal.SIGSTOP)
                r_stopped = True
                out["replacement_stop_rel"] = time.time() - t0
                out["faults_applied"].append("replacement-stop")
            if (
                r_stopped
                and not r_continued
                and rel >= REPLACEMENT_STOP_OFFSET_S + REPLACEMENT_STOP_S
            ):
                workers["A2"].signal_group(signal.SIGCONT)
                r_continued = True
                out["replacement_cont_rel"] = time.time() - t0
                out["faults_applied"].append("replacement-continue")

            if now_mono - last_sample >= STEP_S:
                last_sample = now_mono
                sample = {"rel": rel, "rows": {}}
                for label, task_id in (("L", task_l), ("C", task_c)):
                    item = ddb.get_item(
                        TableName=table,
                        Key={"pk": {"S": "RUN#%s#TASK#%s" % (run_id, task_id)}},
                        ConsistentRead=True,
                    ).get("Item")
                    if not item:
                        sample["rows"][label] = None
                        continue
                    count = int(item.get("effect_count", {"N": "0"})["N"])
                    holder = item.get("holder", {}).get("S")
                    sample["rows"][label] = {
                        "count": count,
                        "holder": holder,
                        "phase": item.get("phase", {}).get("S"),
                        "done_at": (
                            float(item["done_at"]["N"]) if "done_at" in item else None
                        ),
                    }
                    latest_rows[label] = sample["rows"][label]
                    out["max_count"][label] = max(
                        out["max_count"].get(label, 0), count
                    )
                    if count > 0 and label not in out["first_count"]:
                        out["first_count"][label] = {
                            "rel": rel,
                            "at": time.time(),
                        }
                    if (
                        label == "C"
                        and holder == "S"
                        and "C" not in out["first_standby"]
                    ):
                        out["first_standby"]["C"] = rel
                out["samples"].append(sample)

            time.sleep(0.05)

        out["survivors_alive"] = {
            wid: workers[wid].alive() for wid in ("A", "S", "A2") if wid in workers
        }
        for label, task_id in (("L", task_l), ("C", task_c)):
            item = ddb.get_item(
                TableName=table,
                Key={"pk": {"S": "RUN#%s#TASK#%s" % (run_id, task_id)}},
                ConsistentRead=True,
            ).get("Item")
            if item:
                out["final"][label] = {
                    "count": int(item.get("effect_count", {"N": "0"})["N"]),
                    "holder": item.get("holder", {}).get("S"),
                    "phase": item.get("phase", {}).get("S"),
                    "done_at": (
                        float(item["done_at"]["N"]) if "done_at" in item else None
                    ),
                }
            else:
                out["final"][label] = None
        out["worker_lines"] = {
            wid: [e for e in workers[wid].lines] for wid in workers
        }
    finally:
        for worker in workers.values():
            worker.signal_group(signal.SIGKILL)
            try:
                worker.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    return out


def _drive():
    try:
        result = drive()
    except Exception as exc:  # a driver failure is still a scored run
        return {"driver_error": "%s: %s" % (type(exc).__name__, exc)}
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "timeline.json"), "w") as handle:
            json.dump(
                {
                    k: v
                    for k, v in result.items()
                    if k not in ("worker_lines", "samples")
                },
                handle,
                indent=1,
                default=str,
            )
        with open(os.path.join(LOG_DIR, "samples.json"), "w") as handle:
            json.dump(result.get("samples", []), handle, default=str)
    except OSError:
        pass
    return result


RESULT = _drive()


def _rel(label):
    entry = RESULT.get("first_count", {}).get(label)
    return entry["rel"] if entry else None


def test_fixture_setup_completed():
    assert RESULT.get("driver_error") is None, "driver error: %s" % RESULT.get(
        "driver_error"
    )
    assert RESULT.get("environment_live", True), "the emulator was not reachable"
    assert RESULT.get("setup_ok"), "setup failed: %s" % RESULT.get("setup_note")


def test_original_message_returned_to_the_queue():
    """The destroyed holder's original delivery must come back, itself."""
    assert RESULT.get("setup_ok")
    kill_rel = RESULT.get("kill_rel")
    assert kill_rel is not None, "the holder was never destroyed"
    wanted = RESULT.get("message_ids", {}).get("C")
    seen = [
        o
        for o in RESULT.get("observations", [])
        if o["task_id"] == RESULT.get("task_ids", {}).get("C")
    ]
    assert seen, "nothing was returned to the queue during the recovery window"
    in_window = [
        o
        for o in seen
        if o["observed_at"] - (RESULT.get("t0", 0) + kill_rel) <= RECOVERY_BOUND_S
    ]
    assert in_window, "the delivery came back only after the recovery window"
    assert any(
        o["message_id"] == wanted for o in in_window
    ), "the returned delivery was not the published one"
    assert (
        max(o["receive_count"] for o in in_window) >= MIN_RECEIVE_COUNT
    ), "the returned delivery had never been held"


def test_standby_holds_the_recovered_task():
    assert RESULT.get("setup_ok")
    first = RESULT.get("first_standby", {}).get("C")
    kill_rel = RESULT.get("kill_rel")
    assert kill_rel is not None
    assert first is not None, "no standby ever held the recovered task"
    assert first - kill_rel <= RECOVERY_BOUND_S, (
        "the standby took over %.1fs after the holder died" % (first - kill_rel)
    )


def test_recovered_task_committed_in_its_window():
    assert RESULT.get("setup_ok")
    when = _rel("C")
    assert when is not None, "the recovered task never committed"
    assert ETA_C_S <= when <= ETA_C_S + LATE_TOL_S, (
        "committed at t0+%.2fs, window is [%.1f, %.1f]"
        % (when, ETA_C_S, ETA_C_S + LATE_TOL_S)
    )


def test_long_wait_task_committed_in_its_window():
    assert RESULT.get("setup_ok")
    when = _rel("L")
    assert when is not None, "the long-wait task never committed"
    assert ETA_L_S <= when <= ETA_L_S + LATE_TOL_S, (
        "committed at t0+%.2fs, window is [%.1f, %.1f]"
        % (when, ETA_L_S, ETA_L_S + LATE_TOL_S)
    )


def test_no_effect_before_its_due_time():
    assert RESULT.get("setup_ok")
    for label, due in (("C", ETA_C_S), ("L", ETA_L_S)):
        early = [
            s["rel"]
            for s in RESULT["samples"]
            if s["rel"] < due and (s["rows"].get(label) or {}).get("count", 0) > 0
        ]
        assert not early, "task %s committed before its due time at %s" % (
            label,
            early[:3],
        )


def test_one_effect_per_task_identity():
    assert RESULT.get("setup_ok")
    for label in ("C", "L"):
        top = RESULT.get("max_count", {}).get(label, 0)
        final = (RESULT.get("final", {}).get(label) or {}).get("count", 0)
        assert top == 1, "task %s committed %d times" % (label, top)
        assert final == 1, "task %s ended at %d commits" % (label, final)


def test_completion_was_recorded_once():
    assert RESULT.get("setup_ok")
    for label, due in (("C", ETA_C_S), ("L", ETA_L_S)):
        row = RESULT.get("final", {}).get(label) or {}
        done_at = row.get("done_at")
        assert done_at is not None, "task %s has no completion time" % label
        assert done_at >= RESULT["due"][label] - 0.05, (
            "task %s records a completion before its due time" % label
        )
        assert done_at <= RESULT["due"][label] + LATE_TOL_S + 1.0, (
            "task %s records a completion at %.2f" % (label, done_at)
        )


def test_surviving_processes_stayed_up():
    assert RESULT.get("setup_ok")
    alive = RESULT.get("survivors_alive", {})
    assert alive.get("A"), "the paused holder did not survive to the end"
    assert alive.get("S"), "the standby did not survive to the end"
    assert alive.get("A2"), "the replacement did not survive to the end"


def _holders(label, start, end):
    return [
        (round(s["rel"], 2), (s["rows"].get(label) or {}).get("holder"))
        for s in RESULT.get("samples", [])
        if start <= s["rel"] < end
    ]


def test_running_holders_keep_their_tasks():
    """A task stays with a holder that is up and running."""
    assert RESULT.get("setup_ok")
    moved = [
        h for h in _holders("L", SETUP_BOUND_S, PAUSE_OFFSET_S - 0.3) if h[1] != "A"
    ]
    assert not moved, "the long-wait task left its running holder: %s" % moved[:3]
    taken = RESULT.get("first_standby", {}).get("C")
    assert taken is not None, "no standby ever held the recovered task"
    note = RESULT.get("copy_note")
    assert note == "delivered", (
        "the queue never handed a second copy of the recovered task to the"
        " fleet: %s" % note
    )
    committed = _rel("C")
    end = committed if committed is not None else ETA_C_S + LATE_TOL_S
    moved = [h for h in _holders("C", taken + 0.3, end) if h[1] != "S"]
    assert not moved, "the recovered task left its running holder: %s" % moved[:3]


def test_effect_committed_by_the_holder_the_fleet_has():
    """While the process holding a task is stopped across its due time,
    nobody else commits it — not even a process sharing the holder's id."""
    assert RESULT.get("setup_ok")
    took = RESULT.get("replacement_took_rel")
    assert took is not None, (
        "the replacement never took over the suspended holder's task"
    )
    stop = RESULT.get("replacement_stop_rel")
    cont = RESULT.get("replacement_cont_rel")
    assert stop is not None and cont is not None, "the replacement was never stopped"
    before = [s for s in RESULT.get("samples", []) if s["rel"] < stop]
    assert before, "no sample of the long-wait task before its due time"
    row = before[-1]["rows"].get("L") or {}
    assert row.get("holder") is not None, "the long-wait task had no holder"
    assert row.get("count", 0) == 0, (
        "the long-wait task had already committed before the replacement was"
        " stopped"
    )
    during = [
        round(s["rel"], 2)
        for s in RESULT.get("samples", [])
        if stop <= s["rel"] < cont - 0.1
        and (s["rows"].get("L") or {}).get("count", 0) > 0
    ]
    assert not during, (
        "the long-wait task committed at t0+%s while the process holding it"
        " was stopped" % during[:1]
    )
