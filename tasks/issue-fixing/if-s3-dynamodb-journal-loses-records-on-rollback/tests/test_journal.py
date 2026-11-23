"""Independent verifier for the object journal incident.

The verifier is the operating harness: it owns its own AWS clients and its
own envelope handling, launches and replaces the slot processes, drives the
rollout, the rollback and the backfill, keeps a reference model of every
acknowledged operation, and reads the actual S3 objects and table items to
decide what really happened. Nothing under /app is imported or trusted.

Grading is by observation journal: the schedule runs once per repetition,
every stage records what it saw, and the test functions at the bottom each
assert one slice of it. A stage never aborts the run.
"""

import json
import os
import random
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from cdk_seed.cfn_deploy import deploy
import xml.etree.ElementTree as ET

import boto3
import pytest

ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")
REGION = "us-east-1"
REPETITIONS = 3
BASE_SEED = 20260915

LIVE_DEADLINE = 5.0
ROLLOUT_DEADLINE = 30.0
BACKFILL_DEADLINE = 30.0
ANNOUNCE_TIMEOUT = 30.0
SWEEP_JOIN_TIMEOUT = 60.0
TRAFFIC_ROUNDS = 4
RECORD_COUNT = 8
INITIAL_COUNTER = 10

SLOT_MODULES = {
    "v1": "service_v1.py",
    "v2": "service_v2.py",
    "v3": "service_v3.py",
}
XML_ONLY_READS = [1]
DUAL_READS = [1, 2]

ALL_IDS = ["rec-%02d" % i for i in range(RECORD_COUNT)]
HOT_IDS = ALL_IDS[:4]

VALUE_POOL = [
    "west-12",
    {"env": ["a", "b"], "rank": 3},
    [1, 4, 9],
    {"ttl": {"days": 30, "renew": True}},
    "owner=relay;stage=2",
    {"check": {"sum": "9f2a", "parts": [2, 5]}},
    {"edge": {"peers": ["n1", "n2"], "quota": 7}},
]


# --------------------------------------------------------------------------
# independent envelope handling
# --------------------------------------------------------------------------

def build_xml(counter, attrs):
    parts = ['<record format_version="1"><counter>%d</counter>' % counter]
    for name in sorted(attrs):
        text = json.dumps(attrs[name], sort_keys=True)
        escaped = (
            text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        parts.append('<attr name="%s">%s</attr>' % (name, escaped))
    parts.append("</record>")
    return "".join(parts)


def build_json(counter, attrs):
    return json.dumps(
        {"format_version": 2, "counter": counter, "attrs": attrs}, sort_keys=True
    )


def parse_body(text, fmt):
    """Return {'counter','attrs'} from a body in the given format."""
    if fmt == 2:
        doc = json.loads(text)
        attrs = doc.get("attrs") or {}
        if not isinstance(attrs, dict):
            raise ValueError("attrs is not an object")
        return {"counter": doc["counter"], "attrs": attrs}
    root = ET.fromstring(text)
    if root.tag != "record":
        raise ValueError("bad root")
    counter = int(root.find("counter").text.strip())
    attrs = {}
    for node in root.findall("attr"):
        attrs[node.get("name")] = json.loads(node.text)
    return {"counter": counter, "attrs": attrs}


def random_attrs(rng, low, high):
    attrs = {}
    for _ in range(rng.randint(low, high)):
        name = "x%s" % uuid.uuid4().hex[:8]
        attrs[name] = rng.choice(VALUE_POOL)
    return attrs


# --------------------------------------------------------------------------
# aws access (verifier-owned)
# --------------------------------------------------------------------------

class Aws:
    def __init__(self, bucket, table):
        self.bucket = bucket
        self.table = table
        self.s3 = boto3.client("s3", endpoint_url=ENDPOINT, region_name=REGION)
        self.ddb = boto3.client("dynamodb", endpoint_url=ENDPOINT, region_name=REGION)

    def create(self, rng):
        cfn = boto3.client("cloudformation", endpoint_url=ENDPOINT, region_name=REGION)
        deploy(cfn, "journal-seed-%s" % self.bucket,
               Path(__file__).parent / "cdk_seed/seed.template.json",
               BucketName=self.bucket, TableName=self.table)
        self.ddb.get_waiter("table_exists").wait(TableName=self.table)
        self.ddb.put_item(
            TableName=self.table,
            Item={
                "pk": {"S": "CTRL"},
                "phase": {"S": "idle"},
                "epoch": {"N": "1"},
                "ceiling": {"N": "1"},
            },
        )
        state = {}
        for rid in ALL_IDS:
            attrs = random_attrs(rng, 2, 4)
            blob = "seed/%s/%s" % (rid, uuid.uuid4().hex)
            self.s3.put_object(
                Bucket=self.bucket, Key=blob, Body=build_xml(INITIAL_COUNTER, attrs).encode()
            )
            self.ddb.put_item(
                TableName=self.table,
                Item={
                    "pk": {"S": "HEAD#%s" % rid},
                    "revision": {"N": "0"},
                    "blob_key": {"S": blob},
                    "fmt": {"N": "1"},
                    "op_id": {"S": "seed"},
                },
            )
            state[rid] = {"counter": INITIAL_COUNTER, "attrs": attrs}
        return state

    def head(self, rid):
        resp = self.ddb.get_item(
            TableName=self.table,
            Key={"pk": {"S": "HEAD#%s" % rid}},
            ConsistentRead=True,
        )
        item = resp.get("Item")
        if not item:
            return None
        return {
            "revision": int(item["revision"]["N"]),
            "blob_key": item["blob_key"]["S"],
            "fmt": int(item["fmt"]["N"]),
        }

    def record(self, rid):
        """The authoritative current object for a record, decoded here."""
        head = self.head(rid)
        if head is None:
            return None
        try:
            resp = self.s3.get_object(Bucket=self.bucket, Key=head["blob_key"])
            text = resp["Body"].read().decode("utf-8")
            parsed = parse_body(text, head["fmt"])
        except Exception as exc:
            parsed = {"counter": None, "attrs": {},
                      "unreadable": "%s: %s" % (type(exc).__name__, exc)}
            text = ""
        parsed["fmt"] = head["fmt"]
        parsed["revision"] = head["revision"]
        parsed["text"] = text
        return parsed

    def ctrl(self):
        resp = self.ddb.get_item(
            TableName=self.table,
            Key={"pk": {"S": "CTRL"}},
            ConsistentRead=True,
        )
        item = resp.get("Item")
        if not item:
            return None
        return {
            "phase": item["phase"]["S"],
            "epoch": int(item["epoch"]["N"]),
            "ceiling": int(item["ceiling"]["N"]),
        }

    def op(self, op_id):
        resp = self.ddb.get_item(
            TableName=self.table,
            Key={"pk": {"S": "OP#%s" % op_id}},
            ConsistentRead=True,
        )
        item = resp.get("Item")
        if not item:
            return None
        return {
            "rid": item["rid"]["S"],
            "revision": int(item["revision"]["N"]),
            "blob_key": item["blob_key"]["S"],
            "fmt": int(item["fmt"]["N"]),
        }


# --------------------------------------------------------------------------
# processes
# --------------------------------------------------------------------------

def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Proc:
    def __init__(self, name, args, workdir):
        self.name = name
        self.port = None
        self.reads = None
        self.build = None
        self.proc = subprocess.Popen(
            ["python3", args["module"]] + args["flags"],
            cwd=workdir,
            env={**os.environ, "PYTHONPATH": "/app"},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def wait_ready(self, port, expect_build, expect_generation, timeout=ROLLOUT_DEADLINE):
        self.port = port
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            if self.proc.poll() is not None:
                out = self.proc.stdout.read().decode("utf-8", "replace")
                raise RuntimeError("process %s exited: %s" % (self.name, out[-500:]))
            try:
                doc, _, _ = self.get("/ready", timeout=2.0)
            except Exception as exc:
                last = str(exc)
                time.sleep(0.1)
                continue
            last = doc
            if (
                doc.get("build") == expect_build
                and doc.get("generation") == expect_generation
            ):
                self.build = doc["build"]
                self.reads = sorted(doc.get("reads") or [])
                self.decode_contract()
                return doc
            time.sleep(0.1)
        raise RuntimeError("process %s never became ready: %s" % (self.name, last))

    def decode_contract(self):
        """A slot decodes exactly the formats its build claims, no more."""
        xml_body = build_xml(3, {})
        json_body = build_json(3, {})
        _, status, _ = self.post(
            "/decode", {"body": xml_body, "format_version": 1}, timeout=LIVE_DEADLINE
        )
        if status != 200:
            raise RuntimeError("slot %s rejects the XML envelope" % self.name)
        _, status, _ = self.post(
            "/decode", {"body": json_body, "format_version": 2}, timeout=LIVE_DEADLINE
        )
        wants_json = 2 in (self.reads or [])
        if (status == 200) != wants_json:
            raise RuntimeError(
                "slot %s build %s answers JSON decode with %d" % (self.name, self.build, status)
            )

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def get(self, path, timeout=LIVE_DEADLINE):
        return self._call("GET", path, None, timeout)

    def post(self, path, doc, timeout=LIVE_DEADLINE):
        return self._call("POST", path, doc, timeout)

    def _call(self, method, path, doc, timeout):
        data = json.dumps(doc).encode() if doc is not None else None
        request = urllib.request.Request(
            self.url(path),
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return body, resp.status, time.time() - started
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8"))
            except Exception:
                body = {}
            return body, exc.code, time.time() - started
        except Exception as exc:
            return {"error": "%s: %s" % (type(exc).__name__, exc)}, 0, time.time() - started

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                pass


class Control:
    """Control socket peer: records announcements, holds or releases them."""

    def __init__(self, path):
        self.path = path
        self.cond = threading.Condition()
        self.announcements = []
        self.held = {}
        self.auto = True
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(16)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        data = b""
        while not data.endswith(b"\n"):
            chunk = conn.recv(4096)
            if not chunk:
                conn.close()
                return
            data += chunk
        try:
            msg = json.loads(data.decode("utf-8"))
        except ValueError:
            conn.close()
            return
        with self.cond:
            self.announcements.append(msg)
            if self.auto:
                try:
                    conn.sendall(b'{"action": "release"}\n')
                except OSError:
                    pass
                conn.close()
            else:
                self.held[msg.get("barrier_id")] = conn
            self.cond.notify_all()

    def wait_announcement(self, timeout=ANNOUNCE_TIMEOUT):
        deadline = time.time() + timeout
        with self.cond:
            while not self.announcements and time.time() < deadline:
                self.cond.wait(max(0.05, deadline - time.time()))
            return self.announcements[0] if self.announcements else None

    def announcements_for(self, record_id):
        with self.cond:
            return [a for a in self.announcements
                    if a.get("record_id") == record_id]

    def release(self, barrier_id):
        with self.cond:
            conn = self.held.pop(barrier_id, None)
        if conn is not None:
            try:
                conn.sendall(b'{"action": "release"}\n')
            except OSError:
                pass
            conn.close()

    def set_auto(self, flag):
        with self.cond:
            self.auto = flag
            if flag:
                for barrier_id, conn in list(self.held.items()):
                    try:
                        conn.sendall(b'{"action": "release"}\n')
                    except OSError:
                        pass
                    conn.close()
                self.held.clear()

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# one repetition of the schedule
# --------------------------------------------------------------------------

GROUPS = (
    "run_errors", "live", "reads", "acks", "decode", "activate", "release",
    "converts", "announce", "burst", "rollback", "formats", "content",
    "receipts", "reached",
)

MILESTONES = (
    "s0-traffic", "s1-blocked-probe", "s2-activate", "s3-mixed",
    "hold-announced", "s5-attr", "s5-inc", "s6-rollback", "s7-released",
    "s8-activate", "s8-burst", "s8-sweep", "final",
)


class Run:
    def __init__(self, rep):
        self.rep = rep
        self.rng = random.Random(BASE_SEED + 7919 * rep)
        self.journal = {name: [] for name in GROUPS}
        self.procs = []
        self.deployer = None
        self.sweeper = None
        self.control = None
        self.aws = None
        self.model = {}
        self.acks = []
        self.gen = 0
        self.run_tag = "verify%d" % rep
        self.last_sweep = None

    # -- plumbing ----------------------------------------------------------
    def note(self, group, entry):
        self.journal[group].append({"rep": self.rep, **entry})

    def launch(self, name, module, flags, port):
        proc = Proc(name, {"module": os.path.join("/app", module), "flags": flags},
                    tempfile.mkdtemp(prefix="j-"))
        proc.port = port
        self.procs.append(proc)
        return proc

    def common_flags(self, port):
        return [
            "--run", self.run_tag,
            "--endpoint", ENDPOINT, "--region", REGION,
            "--bucket", self.aws.bucket, "--table", self.aws.table,
            "--port", str(port),
        ]

    def start_slot(self, build, slot):
        self.gen += 1
        port = free_port()
        proc = self.launch("%s/%s" % (slot, build), SLOT_MODULES[build],
                           ["--slot", slot, "--generation", str(self.gen)]
                           + self.common_flags(port), port)
        proc.wait_ready(port, build, self.gen)
        return proc

    def replace(self, proc, slot, build):
        proc.kill()
        return self.start_slot(build, slot)

    def wait_http(self, proc, path, doc=None, timeout=ROLLOUT_DEADLINE):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                if doc is None:
                    body, status, _ = proc.get(path, timeout=3.0)
                else:
                    body, status, _ = proc.post(path, doc, timeout=3.0)
                if status == 200:
                    return body
                last = (status, body)
            except Exception as exc:
                last = repr(exc)
            time.sleep(0.1)
        raise RuntimeError("%s never answered %s: %s" % (proc.name, path, last))

    def phase(self, action):
        doc, status, elapsed = self.deployer.post("/phase", {"action": action},
                                                  timeout=ROLLOUT_DEADLINE)
        if status != 200 or elapsed > ROLLOUT_DEADLINE:
            self.note("live", {"stage": "phase-" + action, "status": status,
                               "elapsed": round(elapsed, 3)})
        if action == "activate" and doc.get("status") == "blocked" and elapsed > LIVE_DEADLINE:
            self.note("live", {"stage": "phase-blocked-slow", "elapsed": round(elapsed, 3)})
        return doc

    # -- traffic -----------------------------------------------------------
    def call_mutate(self, proc, rid, kind, arguments, stage):
        op_id = "d-%s" % uuid.uuid4().hex[:12]
        doc, status, elapsed = proc.post(
            "/mutate",
            {"record_id": rid, "op_id": op_id, "kind": kind, "arguments": arguments},
        )
        if status != 200 or elapsed > LIVE_DEADLINE:
            self.note("live", {"stage": stage, "slot": proc.name, "kind": kind,
                               "rid": rid, "status": status,
                               "elapsed": round(elapsed, 3), "detail": doc})
        if status != 200:
            return None
        return {"op_id": op_id, "revision": doc.get("revision")}

    def mutate(self, proc, rid, kind, arguments, stage):
        ack = self.call_mutate(proc, rid, kind, arguments, stage)
        if ack is None:
            return None
        if kind == "increment":
            self.model[rid]["counter"] += arguments.get("delta", 1)
        elif kind == "set_attr":
            self.model[rid]["attrs"][arguments["name"]] = arguments["value"]
        self.acks.append((ack["op_id"], rid, ack["revision"]))
        self.observe(stage, rid)
        return ack

    def observe(self, stage, rid, group="acks"):
        try:
            got = self.aws.record(rid)
        except Exception as exc:
            self.note(group, {"stage": stage, "rid": rid, "error": repr(exc)})
            return
        exp = self.model[rid]
        if got is None:
            self.note(group, {"stage": stage, "rid": rid, "missing": True})
            return
        lost = {
            name: value
            for name, value in exp["attrs"].items()
            if got["attrs"].get(name) != value
        }
        if got["counter"] != exp["counter"] or lost:
            self.note(group, {
                "stage": stage, "rid": rid,
                "expected": {"counter": exp["counter"], "attrs": exp["attrs"]},
                "got": {"counter": got["counter"], "attrs": got["attrs"],
                        "fmt": got["fmt"], "revision": got["revision"]},
            })

    def traffic(self, stage, procs, rounds=TRAFFIC_ROUNDS):
        for rnd in range(rounds):
            for idx, proc in enumerate(procs):
                rid = HOT_IDS[(rnd + idx) % len(HOT_IDS)]
                doc, status, elapsed = proc.post("/read", {"record_id": rid})
                if status != 200 or elapsed > LIVE_DEADLINE:
                    self.note("live", {"stage": stage, "slot": proc.name,
                                       "kind": "read", "rid": rid, "status": status,
                                       "elapsed": round(elapsed, 3)})
                elif doc.get("counter") != self.model[rid]["counter"]:
                    self.note("reads", {"stage": stage, "slot": proc.name, "rid": rid,
                                        "served": doc.get("counter"),
                                        "expected": self.model[rid]["counter"]})
                self.mutate(proc, rid, "increment", {"delta": 1}, stage)
        self.note("reached", {"stage": stage})

    def probe(self, stage, procs):
        for rid in ALL_IDS:
            try:
                rec = self.aws.record(rid)
            except Exception as exc:
                self.note("decode", {"stage": stage, "rid": rid, "error": repr(exc)})
                continue
            for proc in procs:
                doc, status, _ = proc.post(
                    "/decode", {"body": rec["text"], "format_version": rec["fmt"]}
                )
                want = rec["fmt"] in (proc.reads or [])
                if (status == 200) != want:
                    self.note("decode", {"stage": stage, "rid": rid, "slot": proc.name,
                                         "build": proc.build, "fmt": rec["fmt"],
                                         "status": status})

    def burst(self, stage, rid, procs):
        results = []
        lock = threading.Lock()

        def one(proc, gate):
            op_id = "b-%s" % uuid.uuid4().hex[:12]
            gate.wait(10)
            doc, status, elapsed = proc.post(
                "/mutate",
                {"record_id": rid, "op_id": op_id, "kind": "increment",
                 "arguments": {"delta": 1}},
            )
            with lock:
                results.append({"op_id": op_id, "status": status,
                                "revision": doc.get("revision"),
                                "elapsed": elapsed, "slot": proc.name})

        gate = threading.Barrier(len(procs))
        threads = [threading.Thread(target=one, args=(proc, gate)) for proc in procs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        for result in results:
            if result["status"] != 200 or result["elapsed"] > LIVE_DEADLINE:
                self.note("live", {"stage": stage, "slot": result["slot"],
                                   "kind": "burst", "rid": rid,
                                   "status": result["status"],
                                   "elapsed": round(result["elapsed"], 3)})
        acked = [r for r in results if r["status"] == 200]
        for result in acked:
            self.model[rid]["counter"] += 1
            self.acks.append((result["op_id"], rid, result["revision"]))
        revisions = [r["revision"] for r in acked]
        if len(set(revisions)) != len(revisions):
            self.note("burst", {"stage": stage, "rid": rid,
                                "revisions": revisions})
        if len(acked) != len(procs):
            self.note("burst", {"stage": stage, "rid": rid,
                                "acked": len(acked), "wanted": len(procs)})
        self.observe(stage + "-burst", rid, group="burst")
        self.note("reached", {"stage": stage})

    def sweep_call(self, record_ids, timeout=None):
        try:
            doc, status, elapsed = self.sweeper.post(
                "/sweep", {"record_ids": record_ids},
                timeout=timeout or (SWEEP_JOIN_TIMEOUT + 20),
            )
            self.last_sweep = {"status": status, "doc": doc,
                               "elapsed": round(elapsed, 3)}
        except Exception as exc:
            self.last_sweep = {"error": repr(exc)}

    # -- the schedule --------------------------------------------------------
    def drive(self):
        tag = "v%d%s" % (self.rep, uuid.uuid4().hex[:8])
        self.aws = Aws("journal-%s" % tag, "journal-%s" % tag)
        self.model = self.aws.create(self.rng)
        workroot = tempfile.mkdtemp(prefix="run%d-" % self.rep)
        self.control = Control(os.path.join(workroot, "control.sock"))

        dep_port = free_port()
        self.deployer = self.launch("deployer", "deployer.py",
                                    self.common_flags(dep_port), dep_port)
        self.wait_http(self.deployer, "/status")

        sweep_port = free_port()
        self.sweeper = self.launch(
            "sweeper", "sweeper.py",
            self.common_flags(sweep_port)
            + ["--control", os.path.join(workroot, "control.sock")],
            sweep_port,
        )
        self.wait_http(self.sweeper, "/sweep", {"record_ids": []})

        slots = {slot: self.start_slot("v1", slot) for slot in ("s0", "s1", "s2")}
        fleet = lambda: [slots["s0"], slots["s1"], slots["s2"]]

        # a quiet baseline: the released fleet serves and updates
        self.traffic("s0-traffic", fleet())
        self.probe("s0", fleet())
        self.note("reached", {"stage": "s0-traffic-done"})

        # partial prepare: two slots replaced, one still on the released build
        self.phase("prepare")
        slots["s0"] = self.replace(slots["s0"], "s0", "v2")
        slots["s1"] = self.replace(slots["s1"], "s1", "v2")
        self.traffic("s1-traffic", fleet())

        answer = self.phase("activate")
        if answer.get("status") != "blocked":
            self.note("activate", {"where": "while an XML-only slot serves",
                                   "answer": answer})
        ctrl = self.aws.ctrl()
        if ctrl["phase"] != "prepare" or ctrl["ceiling"] != 1:
            self.note("activate", {"where": "control moved anyway", "ctrl": ctrl})
        for rid in ALL_IDS:
            if self.aws.record(rid)["fmt"] != 1:
                self.note("activate", {"where": "new format published early", "rid": rid})
                break
        self.note("reached", {"stage": "s1-blocked-probe"})
        self.traffic("s1-traffic-after", fleet())

        # prepare completes, activation applies
        slots["s2"] = self.replace(slots["s2"], "s2", "v2")
        answer = self.phase("activate")
        if answer.get("status") != "applied":
            self.note("activate", {"where": "with the whole fleet prepared",
                                   "answer": answer})
        self.note("reached", {"stage": "s2-activate"})

        # one slot runs the activated build next to prepared builds
        slots["s1"] = self.replace(slots["s1"], "s1", "v3")
        self.traffic("s3-traffic", fleet())
        self.probe("s3", fleet())
        self.note("reached", {"stage": "s3-mixed"})

        # a cold record is converted while the fleet keeps writing it
        cold = [rid for rid in ALL_IDS if rid not in HOT_IDS]
        key = self.rng.choice(cold)
        self.control.set_auto(False)
        held = threading.Thread(target=self.sweep_call, args=([key],))
        held.start()
        announcement = self.control.wait_announcement()
        if announcement is None:
            self.note("run_errors", {"what": "no snapshot announcement", "rid": key})
        elif announcement.get("record_id") != key:
            self.note("run_errors", {"what": "announced another record",
                                     "got": announcement.get("record_id"), "rid": key})
        else:
            self.note("reached", {"stage": "hold-announced"})

        attr_name = "e%s" % uuid.uuid4().hex[:8]
        attr_value = self.rng.choice(VALUE_POOL)
        self.mutate(slots["s1"], key, "set_attr",
                    {"name": attr_name, "value": attr_value}, "s5-attr")
        self.note("reached", {"stage": "s5-attr"})
        increments = self.rng.randint(1, 2)
        for _ in range(increments):
            self.mutate(slots["s0"], key, "increment", {"delta": 1}, "s5-inc")
        self.note("reached", {"stage": "s5-inc"})

        # rollback: fence, reinstall the prepared build, keep serving
        before = self.aws.ctrl()
        answer = self.phase("rollback")
        after = self.aws.ctrl()
        if (
            answer.get("status") != "applied"
            or answer.get("phase") != "prepare"
            or after["phase"] != "prepare"
            or after["epoch"] != before["epoch"] + 1
        ):
            self.note("rollback", {"answer": answer, "before": before, "after": after})
        slots["s1"] = self.replace(slots["s1"], "s1", "v2")
        self.traffic("s6-traffic", fleet())
        self.note("reached", {"stage": "s6-rollback"})

        # the held snapshot resumes against a record that has moved on
        self.control.set_auto(True)
        if announcement is not None:
            self.control.release(announcement.get("barrier_id"))
        held.join(SWEEP_JOIN_TIMEOUT)
        if held.is_alive():
            self.note("run_errors", {"what": "sweep never returned after release",
                                     "rid": key})
        else:
            rec = self.aws.record(key)
            if rec["fmt"] != 2:
                self.note("converts", {"stage": "s7-released", "rid": key,
                                       "fmt": rec["fmt"],
                                       "result": self.last_sweep})
            seen = self.control.announcements_for(key)
            if not any(a.get("revision") == rec["revision"] - 1 for a in seen):
                self.note("announce", {
                    "stage": "s7-released", "rid": key,
                    "published": rec["revision"],
                    "announced": [a.get("revision") for a in seen],
                })
            self.observe("s7-released", key, group="release")
        self.probe("s7", fleet())
        self.note("reached", {"stage": "s7-released"})

        # reactivate, finish the rollout, convert everything under traffic
        answer = self.phase("activate")
        if answer.get("status") != "applied":
            self.note("activate", {"where": "on reactivation", "answer": answer})
        slots["s1"] = self.replace(slots["s1"], "s1", "v3")
        slots["s0"] = self.replace(slots["s0"], "s0", "v3")
        slots["s2"] = self.replace(slots["s2"], "s2", "v3")
        self.note("reached", {"stage": "s8-activate"})
        self.traffic("s8-traffic", fleet())
        self.burst("s8-burst", HOT_IDS[0], fleet())
        self.burst("s8-burst", HOT_IDS[1], fleet())

        started = time.time()
        self.control.set_auto(True)
        full = threading.Thread(target=self.sweep_call, args=(ALL_IDS,))
        full.start()
        self.traffic("s8-concurrent", fleet(), rounds=2)
        full.join(SWEEP_JOIN_TIMEOUT)
        deadline = started + BACKFILL_DEADLINE
        while time.time() < deadline:
            pending = [rid for rid in ALL_IDS if self.aws.record(rid)["fmt"] != 2]
            if not pending:
                break
            self.sweep_call(pending, timeout=BACKFILL_DEADLINE)
        if full.is_alive():
            self.note("run_errors", {"what": "full sweep never returned"})
        elapsed = time.time() - started
        if elapsed > BACKFILL_DEADLINE:
            self.note("formats", {"what": "backfill overran", "seconds": round(elapsed, 3)})
        self.note("reached", {"stage": "s8-sweep"})

        # every record, touched or not, ends in the new envelope, exactly
        for rid in ALL_IDS:
            rec = self.aws.record(rid)
            if rec["fmt"] != 2:
                self.note("formats", {"rid": rid, "fmt": rec["fmt"]})
            self.observe("final", rid, group="content")
        self.probe("final", fleet())
        self.note("reached", {"stage": "final"})

        for op_id, rid, revision in self.acks:
            receipt = self.aws.op(op_id)
            if receipt is None or receipt["revision"] != revision:
                self.note("receipts", {"op_id": op_id, "rid": rid,
                                       "acknowledged": revision,
                                       "receipt": receipt})

    def cleanup(self):
        for proc in self.procs:
            proc.kill()
        if self.control is not None:
            self.control.close()


# --------------------------------------------------------------------------
# fixture and graded groups
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def journal():
    try:
        probe = boto3.client("dynamodb", endpoint_url=ENDPOINT, region_name=REGION)
        probe.list_tables()
    except Exception as exc:
        pytest.fail("ENVIRONMENT: floci data plane not reachable: %r" % exc,
                    pytrace=False)

    combined = {name: [] for name in GROUPS}
    for rep in range(REPETITIONS):
        run = Run(rep)
        try:
            run.drive()
        except Exception as exc:
            run.note("run_errors", {"what": "schedule raised", "error": repr(exc)})
        finally:
            run.cleanup()
        for name in GROUPS:
            combined[name].extend(run.journal[name])

    seen = {entry["stage"] for entry in combined["reached"]}
    for milestone in MILESTONES:
        if milestone not in seen:
            combined["run_errors"].append({"what": "stage never reached",
                                           "stage": milestone})
    return combined


def test_activate_refused_while_an_xml_only_slot_serves(journal):
    assert not journal["activate"], journal["activate"][:3]


def test_acknowledged_updates_and_attributes_persist(journal):
    assert not journal["acks"], journal["acks"][:3]


def test_held_snapshot_never_reverts_live_state(journal):
    assert not journal["release"], journal["release"][:3]


def test_held_record_is_converted_by_the_held_request(journal):
    assert not journal["converts"], journal["converts"][:3]


def test_publication_supersedes_an_announced_snapshot(journal):
    assert not journal["announce"], journal["announce"][:3]


def test_concurrent_updates_on_one_record_are_not_lost(journal):
    assert not journal["burst"], journal["burst"][:3]


def test_live_requests_stay_within_bound(journal):
    assert not journal["live"], journal["live"][:3]


def test_reads_serve_current_state(journal):
    assert not journal["reads"], journal["reads"][:3]


def test_current_records_decode_on_every_serving_build(journal):
    assert not journal["decode"], journal["decode"][:3]


def test_rollback_lands_on_the_prepared_regime(journal):
    assert not journal["rollback"], journal["rollback"][:3]


def test_migration_completes_for_every_record(journal):
    assert not journal["formats"], journal["formats"][:3]


def test_final_records_match_the_operation_ledger(journal):
    assert not journal["content"], journal["content"][:3]


def test_receipts_agree_with_acknowledgements(journal):
    assert not journal["receipts"], journal["receipts"][:3]


def test_schedule_completed(journal):
    assert not journal["run_errors"], journal["run_errors"][:3]
