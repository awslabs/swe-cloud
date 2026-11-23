"""Independent verifier: drives one failover window and grades real state.

The harness owns the tables, the fixture, the anchor, the body callback, the
processes and the audit table.  It never authorizes or suppresses a body on the
basis of its claim: it records every callback invocation, with a consistent
read of the running record taken when the body announces itself and again at
the moment it is permitted to begin, and asserts afterwards.  All groups run;
nothing here fails fast.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from cdk_seed.cfn_deploy import deploy

import boto3
import pytest

ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")

P = {
    "schedulers": 2, "workers": 3, "jobs": 2,
    "bootstrap_slot": 0, "first_periodic_slot": 1, "period_s": 4.0,
    "periodic_slots": 8,
    "startup_deadline_s": 15.0, "leader_lease_s": 3.0, "run_lease_s": 6.0,
    "heartbeat_s": 1.0, "fresh_heartbeat_s": 1.5, "poll_s": 0.1,
    "fault_settle_s": 1.0, "takeover_deadline_s": 5.0, "restart_at_s": 7.0,
    "orphan_reclaim_deadline_s": 10.0, "orphan_required_slot": 3,
    "live_release_at_s": 22.0, "live_required_slot": 6,
    "short_body_s": 0.5, "decision_deadline_s": 2.0, "finish_deadline_s": 2.0,
    "time_tolerance_s": 0.5, "case_deadline_s": 36.0,
    "max_starts_per_slot": 1, "max_live_per_job": 1,
    "epoch_increment": 1, "replay_handshake_s": 1.0, "replays": 1,
    "grant_confirm_s": 3.0, "thaw_settle_s": 1.5, "body_floor": 0.5,
}

CASE = {"done": False, "error": None}


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------
def db():
    return boto3.client("dynamodb", endpoint_url=ENDPOINT,
                        region_name=os.environ.get("AWS_DEFAULT_REGION",
                                                   "us-east-1"))


def enc(v):
    if isinstance(v, bool):
        return {"BOOL": v}
    if isinstance(v, (int, float)):
        return {"N": repr(v)}
    return {"S": str(v)}


def dec_item(item):
    out = {}
    for k, v in item.items():
        if "S" in v:
            out[k] = v["S"]
        elif "N" in v:
            f = float(v["N"])
            out[k] = int(f) if f.is_integer() else f
        elif "BOOL" in v:
            out[k] = v["BOOL"]
        elif "L" in v:
            out[k] = [x.get("S") for x in v["L"]]
    return out


class Harness:
    def __init__(self):
        self.run_id = uuid.uuid4().hex[:10]
        self.state_table = "cad_state_%s" % self.run_id
        self.audit_table = "cad_audit_%s" % self.run_id
        self.db = db()
        self.case_key = "CASE#%s" % self.run_id
        self.seq = 0
        self.lock = threading.Lock()
        self.events = []            # in-memory mirror of the audit ledger
        self.holds = {}             # run_id -> Event, bodies waiting to start
        self.holds_lock = threading.Lock()
        self.procs = {}             # label -> Popen
        self.control = {}           # label -> socket (parent end)
        self.logs = {}
        self.job = {}               # role -> job name
        self.t0 = None
        self.live_hold = None       # run_id of the surviving bootstrap body
        self.replay_done = False
        self.stop = threading.Event()

    # -- audit --------------------------------------------------------------
    def audit(self, kind, barrier="", **facts):
        with self.lock:
            self.seq += 1
            n = self.seq
        row = {"pk": self.case_key, "sk": "%s#%04d" % (kind, n),
               "kind": kind, "barrier": barrier, "ts": time.time()}
        row.update({k: v for k, v in facts.items() if v is not None})
        self.db.put_item(
            TableName=self.audit_table,
            Item={k: enc(v) for k, v in row.items()},
            ConditionExpression="attribute_not_exists(sk)",
        )
        with self.lock:
            self.events.append(row)

    def snapshot(self, barrier):
        actives = {}
        for role, job in self.job.items():
            actives[role] = self.read_active(job)
        lead = self.read_leader()
        self.audit("SNAPSHOT", barrier, actives=json.dumps(actives),
                   leader=json.dumps(lead))

    # -- state reads --------------------------------------------------------
    def read_active(self, job):
        resp = self.db.get_item(
            TableName=self.state_table,
            Key={"pk": {"S": "JOB#%s" % job}, "sk": {"S": "ACTIVE"}},
            ConsistentRead=True)
        return dec_item(resp["Item"]) if "Item" in resp else None

    def read_leader(self):
        resp = self.db.get_item(
            TableName=self.state_table,
            Key={"pk": {"S": "CLUSTER"}, "sk": {"S": "LEADER"}},
            ConsistentRead=True)
        return dec_item(resp["Item"]) if "Item" in resp else None

    def read_slots(self, job):
        out, exclusive = {}, None
        kwargs = {"TableName": self.state_table,
                  "KeyConditionExpression": "#p = :p AND begins_with(#s, :s)",
                  "ExpressionAttributeNames": {"#p": "pk", "#s": "sk"},
                  "ExpressionAttributeValues": {
                      ":p": {"S": "JOB#%s" % job}, ":s": {"S": "SLOT#"}},
                  "ConsistentRead": True}
        while True:
            if exclusive:
                kwargs["ExclusiveStartKey"] = exclusive
            resp = self.db.query(**kwargs)
            for it in resp.get("Items", []):
                row = dec_item(it)
                out[int(row["slot"])] = row
            exclusive = resp.get("LastEvaluatedKey")
            if not exclusive:
                return out

    def read_runs(self, job):
        out, exclusive = [], None
        kwargs = {"TableName": self.state_table,
                  "KeyConditionExpression": "#p = :p AND begins_with(#s, :s)",
                  "ExpressionAttributeNames": {"#p": "pk", "#s": "sk"},
                  "ExpressionAttributeValues": {
                      ":p": {"S": "JOB#%s" % job}, ":s": {"S": "RUN#"}},
                  "ConsistentRead": True}
        while True:
            if exclusive:
                kwargs["ExclusiveStartKey"] = exclusive
            resp = self.db.query(**kwargs)
            out.extend(dec_item(it) for it in resp.get("Items", []))
            exclusive = resp.get("LastEvaluatedKey")
            if not exclusive:
                return out

    def audit_rows(self):
        rows, exclusive = [], None
        kwargs = {"TableName": self.audit_table,
                  "KeyConditionExpression": "#p = :p",
                  "ExpressionAttributeNames": {"#p": "pk"},
                  "ExpressionAttributeValues": {":p": {"S": self.case_key}}}
        while True:
            if exclusive:
                kwargs["ExclusiveStartKey"] = exclusive
            resp = self.db.query(**kwargs)
            rows.extend(dec_item(it) for it in resp.get("Items", []))
            exclusive = resp.get("LastEvaluatedKey")
            if not exclusive:
                return sorted(rows, key=lambda r: r["sk"])

    # -- tables and fixture -------------------------------------------------
    def create_tables(self):
        cfn = boto3.client("cloudformation", endpoint_url=ENDPOINT,
                           region_name=self.db.meta.region_name)
        deploy(cfn, "cad-seed-%s" % self.run_id,
               Path(__file__).parent / "cdk_seed/seed.template.json",
               StateTable=self.state_table, AuditTable=self.audit_table)

    def write_fixture(self):
        start_at = time.time() + 3.0
        self.job["live"] = "j_%s" % uuid.uuid4().hex[:6]
        self.job["orphan"] = "j_%s" % uuid.uuid4().hex[:6]
        specs = {
            self.job["live"]: {"targets": ["w0"]},
            self.job["orphan"]: {"targets": ["w1", "w2"]},
        }
        for job, extra in specs.items():
            item = {"pk": {"S": "META"}, "sk": {"S": "JOB#%s" % job},
                    "job": {"S": job}, "period_s": {"N": repr(P["period_s"])},
                    "start_at": {"N": repr(start_at)},
                    "concurrency": {"S": "forbid"}}
            item.update({k: {"L": [{"S": x} for x in v]}
                         for k, v in extra.items()})
            self.db.put_item(TableName=self.state_table, Item=item)
        return start_at

    # -- processes ----------------------------------------------------------
    def launch(self, label, prog, extra_args=()):
        read, write = socket.socketpair()
        cwd = tempfile.mkdtemp(prefix="cwd_%s_" % label)
        logpath = "/logs/verifier/proc_%s.txt" % label
        log = open(logpath, "ab")
        cmd = [sys.executable, "/app/%s.py" % prog, "--id", label.split("#")[0],
               "--state-table", self.state_table, "--endpoint", ENDPOINT,
               "--control-fd", str(write.fileno())] + list(extra_args)
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONPATH": "/app"},
            pass_fds=(write.fileno(),))
        write.close()
        self.procs[label] = proc
        self.control[label] = read
        return proc

    def send(self, label, **msg):
        sock = self.control.get(label)
        if sock is None:
            return False
        try:
            sock.sendall((json.dumps(msg) + "\n").encode())
            return True
        except OSError:
            return False

    def recv(self, label, timeout=5.0):
        sock = self.control.get(label)
        if sock is None:
            return None
        sock.settimeout(timeout)
        try:
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = sock.recv(4096)
                if not chunk:
                    return None
                buf += chunk
            return json.loads(buf.decode())
        except (socket.timeout, OSError):
            return None

    def kill(self, label):
        proc = self.procs.get(label)
        if proc is None or proc.poll() is not None:
            return False
        proc.kill()
        proc.wait(timeout=10)
        return True

    def freeze(self, label):
        proc = self.procs.get(label)
        if proc is None or proc.poll() is not None:
            return False
        os.kill(proc.pid, signal.SIGSTOP)
        return True

    def thaw(self, label):
        proc = self.procs.get(label)
        if proc is None or proc.poll() is not None:
            return False
        os.kill(proc.pid, signal.SIGCONT)
        return True

    def stop_all(self):
        for label in list(self.procs):
            self.thaw(label)
        for label in list(self.procs):
            proc = self.procs.get(label)
            if proc and proc.poll() is None:
                self.send(label, type="STOP")
        deadline = time.time() + 5
        for label, proc in list(self.procs.items()):
            try:
                proc.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass

    # -- body callback ------------------------------------------------------
    def callback_server(self, path):
        if os.path.exists(path):
            os.unlink(path)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(64)
        self.cb_path = path
        threading.Thread(target=self._accept_loop, args=(srv,),
                         daemon=True).start()

    def _accept_loop(self, srv):
        while not self.stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=self._body_conn, args=(conn,),
                             daemon=True).start()

    def _body_conn(self, conn):
        """One body: announce, wait for permission, run, return."""
        current = None
        with conn:
            conn.settimeout(None)
            buf = b""
            while True:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    return
                if not chunk:
                    if current:
                        self.audit("EVENT", "lost", job=current["job"],
                                   run_id=current["run_id"])
                    return
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("type") == "START":
                        current = self._on_start(conn, msg)
                    elif msg.get("type") == "END" and current:
                        self.audit("EVENT", "end", job=current["job"],
                                   run_id=current["run_id"])
                        current = None

    def _on_start(self, conn, msg):
        """Record the invocation and its claim snapshot, then maybe hold it."""
        job = msg.get("job")
        role = self.role_of(job)
        active = self.read_active(job) if job else None
        self.audit("EVENT", "start", job=job, slot=msg.get("slot"),
                   run_id=msg.get("run_id"), token=msg.get("claim_token"),
                   pid=msg.get("pid"), role=role,
                   claim=json.dumps(active))
        hold = None
        if msg.get("slot") == P["bootstrap_slot"]:
            hold = "bootstrap"
        elif (role == "orphan" and msg.get("slot", 0) > P["bootstrap_slot"]
              and not self.replay_done):
            hold = "successor"
            self.replay_done = True
        if hold:
            ev = threading.Event()
            with self.holds_lock:
                self.holds[msg.get("run_id")] = ev
            if not ev.wait(timeout=60):
                pass
        # the running record as it stands at the moment work is permitted
        at_grant = self.read_active(job) if job else None
        granted_at = time.time()
        try:
            conn.sendall(b'{"granted": true}\n')
        except OSError:
            return msg
        self.audit("EVENT", "grant", job=job, slot=msg.get("slot"),
                   run_id=msg.get("run_id"), role=role,
                   claim=json.dumps(at_grant), ts=granted_at)
        return msg

    def role_of(self, job):
        for role, name in self.job.items():
            if name == job:
                return role
        return "unknown"

    def release(self, run_id):
        with self.holds_lock:
            ev = self.holds.get(run_id)
        if ev:
            ev.set()

    # -- the window ---------------------------------------------------------
    def wait_for(self, predicate, deadline, step=0.05):
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(step)
        return predicate()

    def _sample_loop(self, end):
        while time.time() < end and not self.stop.is_set():
            try:
                self.snapshot("sample")
            except Exception:                       # noqa: BLE001
                pass
            time.sleep(0.5)

    def run(self):
        os.makedirs("/logs/verifier", exist_ok=True)
        self.create_tables()
        self.write_fixture()
        cb_path = "/tmp/cb_%s.sock" % self.run_id
        self.callback_server(cb_path)

        for i in range(P["schedulers"]):
            self.launch("s%d" % i, "scheduler")
        for i in range(P["workers"]):
            self.launch("w%d" % i, "worker", ("--callback-socket", cb_path))

        ready_deadline = time.time() + P["startup_deadline_s"]
        for label in list(self.procs):
            msg = self.recv(label, timeout=max(1.0, ready_deadline - time.time()))
            self.audit("EVENT", "ready", who=label, detail=json.dumps(msg))

        # both opening bodies running, both claims freshly maintained
        def bootstrap_ready():
            for role in ("live", "orphan"):
                row = self.read_active(self.job[role])
                if not row or row.get("worker") is None:
                    return False
                if time.time() - row.get("heartbeat_at", 0) > P["fresh_heartbeat_s"]:
                    return False
            return True

        self.wait_for(bootstrap_ready, ready_deadline)
        live_row = self.read_active(self.job["live"])
        orphan_row = self.read_active(self.job["orphan"])
        self.snapshot("initial")

        lead = self.read_leader()
        owner = (lead or {}).get("owner")
        victim_sched = owner if owner in self.procs else "s0"
        victim_worker = (orphan_row or {}).get("worker", "w1")
        if victim_worker == (live_row or {}).get("worker"):
            victim_worker = "w1" if victim_worker != "w1" else "w2"

        # arm the periodic nominals and, in the same breath, take the fault
        self.t0 = time.time()
        self.db.put_item(
            TableName=self.state_table,
            Item={"pk": {"S": "META"}, "sk": {"S": "ARM"},
                  "anchor": {"N": repr(self.t0)},
                  "slots": {"N": repr(P["periodic_slots"])}})
        self.audit("EVENT", "arm", anchor=self.t0, victim_sched=victim_sched,
                   victim_worker=victim_worker,
                   live=json.dumps(live_row), orphan=json.dumps(orphan_row))
        self.snapshot("fault")

        self.kill(victim_sched)
        frozen_pid = self.procs[victim_worker].pid if victim_worker in self.procs else None
        frozen_at = time.time()
        self.freeze(victim_worker)
        self.audit("EVENT", "killed", who=victim_sched,
                   detail="scheduler", ts=self.t0)
        self.audit("EVENT", "frozen", who=victim_worker, pid=frozen_pid,
                   ts=frozen_at)

        sampler = threading.Thread(
            target=self._sample_loop, args=(self.t0 + P["case_deadline_s"] + 1,),
            daemon=True)
        sampler.start()

        # the dead scheduler comes back, same identity, empty memory
        while time.time() < self.t0 + P["restart_at_s"]:
            time.sleep(0.05)
        self.launch(victim_sched, "scheduler")
        msg = self.recv(victim_sched, timeout=10)
        self.audit("EVENT", "restart", who=victim_sched,
                   detail=json.dumps(msg))
        self.snapshot("restart")

        # a stale completion for the killed attempt, while its successor waits
        spare = [w for w in ("w0", "w1", "w2")
                 if w not in (victim_worker,)][0]
        replay_deadline = self.t0 + 16.0
        target = self.wait_for(
            lambda: any(e["kind"] == "EVENT" and e.get("barrier") == "start"
                        and self.role_of(e.get("job")) == "orphan"
                        and (e.get("slot") or 0) > P["bootstrap_slot"]
                        for e in list(self.events)),
            replay_deadline)
        self.send(spare, type="COMPLETE", job=self.job["orphan"],
                  run_id=(orphan_row or {}).get("run_id") or "",
                  claim_token=(orphan_row or {}).get("claim_token") or "",
                  result="ok")
        reply = self.recv(spare, timeout=P["replay_handshake_s"] + 4)
        self.snapshot("replay")
        self.audit("EVENT", "replay", job=self.job["orphan"],
                   run_id=(orphan_row or {}).get("run_id"),
                   successor_seen=bool(target),
                   reply=json.dumps(reply))

        # the frozen worker's host comes back: whatever it was waiting for is
        # already there when it resumes
        frozen_run = (orphan_row or {}).get("run_id")
        self.release(frozen_run)
        self.wait_for(
            lambda: any(e["kind"] == "EVENT" and e.get("barrier") == "grant"
                        and e.get("run_id") == frozen_run
                        for e in list(self.events)),
            time.time() + P["grant_confirm_s"])
        thawed_at = time.time()
        self.thaw(victim_worker)
        self.audit("EVENT", "thawed", who=victim_worker, pid=frozen_pid,
                   ts=thawed_at)
        time.sleep(P["thaw_settle_s"])
        if target:
            for e in list(self.events):
                if (e["kind"] == "EVENT" and e.get("barrier") == "start"
                        and self.role_of(e.get("job")) == "orphan"
                        and (e.get("slot") or 0) > P["bootstrap_slot"]):
                    self.release(e.get("run_id"))
                    break

        # the surviving body is let go
        while time.time() < self.t0 + P["live_release_at_s"]:
            time.sleep(0.05)
        if live_row:
            self.release(live_row.get("run_id"))
        self.audit("EVENT", "release", job=self.job["live"],
                   run_id=(live_row or {}).get("run_id"))

        while time.time() < self.t0 + P["case_deadline_s"]:
            time.sleep(0.1)
        self.snapshot("final")
        self.stop.set()
        self.stop_all()

    def collect(self):
        out = {"leader": self.read_leader(),
               "t0": self.t0, "job": dict(self.job), "error": CASE["error"]}
        for role, job in self.job.items():
            out[role] = {"slots": self.read_slots(job),
                         "runs": self.read_runs(job),
                         "active": self.read_active(job)}
        out["rows"] = self.audit_rows()
        return out


# ---------------------------------------------------------------------------
# run the window once, then grade it
# ---------------------------------------------------------------------------
H = Harness()
try:
    H.run()
except Exception as err:                                   # noqa: BLE001
    CASE["error"] = "%s: %s" % (type(err).__name__, err)
finally:
    H.stop.set()
    H.stop_all()
try:
    EV = H.collect()
except Exception as err:                                   # noqa: BLE001
    CASE["error"] = CASE["error"] or "collect: %s: %s" % (type(err).__name__, err)
    EV = {"rows": [], "t0": H.t0, "job": dict(H.job), "leader": None,
          "live": {"slots": {}, "runs": [], "active": None},
          "orphan": {"slots": {}, "runs": [], "active": None}}
CASE["done"] = True


def starts(role=None, slot=None, after=None, before=None):
    out = []
    for r in EV["rows"]:
        if not r["sk"].startswith("EVENT#") or r.get("barrier") != "start":
            continue
        if role is not None and H.role_of(r.get("job")) != role:
            continue
        if slot is not None and r.get("slot") != slot:
            continue
        if after is not None and r["ts"] < after:
            continue
        if before is not None and r["ts"] > before:
            continue
        out.append(r)
    return out


def ends_for(run_id):
    return [r["ts"] for r in EV["rows"]
            if r["sk"].startswith("EVENT#") and r.get("barrier") == "end"
            and r.get("run_id") == run_id]


def lost_for(run_id):
    return [r["ts"] for r in EV["rows"]
            if r["sk"].startswith("EVENT#") and r.get("barrier") == "lost"
            and r.get("run_id") == run_id]


def snaps(lo=None, hi=None, barrier=None):
    out = []
    for r in EV["rows"]:
        if not r["sk"].startswith("SNAPSHOT#"):
            continue
        if barrier is not None and r.get("barrier") != barrier:
            continue
        if lo is not None and r["ts"] < lo:
            continue
        if hi is not None and r["ts"] > hi:
            continue
        r = dict(r)
        r["actives"] = json.loads(r.get("actives") or "{}")
        r["leader"] = json.loads(r.get("leader") or "null")
        out.append(r)
    return out


def T0():
    assert EV["t0"] is not None, "the fault window never opened"
    return EV["t0"]


def nominal(slot):
    return T0() + slot * P["period_s"]


def arm_row():
    for r in EV["rows"]:
        if r["sk"].startswith("EVENT#") and r.get("barrier") == "arm":
            return r
    return None


def grants(role=None):
    return [r for r in EV["rows"]
            if r["sk"].startswith("EVENT#") and r.get("barrier") == "grant"
            and (role is None or H.role_of(r.get("job")) == role)]


def _event(barrier):
    return [r for r in EV["rows"]
            if r["sk"].startswith("EVENT#") and r.get("barrier") == barrier]


def earliest_begin(g):
    """First instant the permitted body could have begun.

    The permission is written at the grant; a process that was stopped at that
    moment cannot act on it before it is continued.
    """
    pid = None
    for s in starts():
        if s.get("run_id") == g.get("run_id"):
            pid = s.get("pid")
            break
    t = g["ts"]
    for fz in _event("frozen"):
        if pid is None or fz.get("pid") != pid or fz["ts"] > t:
            continue
        thawed = [th["ts"] for th in _event("thawed")
                  if th.get("pid") == pid and th["ts"] >= fz["ts"]]
        if not thawed:
            return None
        if min(thawed) > t:
            t = min(thawed)
    return t


def bodies(role=None):
    """Bodies that did their work: (began, returned, run_id, slot, grant).

    A body is the contract's short_body_s of work once permitted; a callback
    that is closed, with or without END, before that much time could have
    elapsed did not run it.
    """
    t_end = T0() + P["case_deadline_s"]
    out = []
    for g in grants(role=role):
        run_id = g.get("run_id")
        begin = earliest_begin(g)
        if begin is None:
            continue
        ends = [t for t in ends_for(run_id) + lost_for(run_id) if t >= g["ts"]]
        end = min(ends) if ends else t_end
        if ends and end - begin < P["short_body_s"] * P["body_floor"]:
            continue
        out.append((begin, end, run_id, g.get("slot"), g))
    return sorted(out, key=lambda b: b[:2])


def intervals(role):
    """Bodies of one job as (began, returned) intervals, harness-observed."""
    return [b[:4] for b in bodies(role)]


# ---------------------------------------------------------------------------
# groups
# ---------------------------------------------------------------------------
def test_processes_came_up():
    assert CASE["error"] is None, "harness error: %s" % CASE["error"]
    ready = [r for r in EV["rows"]
             if r.get("barrier") == "ready" and r.get("who")]
    assert len(ready) >= P["schedulers"] + P["workers"], \
        "expected %d ready processes, saw %d" % (
            P["schedulers"] + P["workers"], len(ready))
    for r in ready:
        assert r.get("detail") not in ("null", None), \
            "no READY from %s" % r.get("who")


def test_opening_bodies_were_running():
    arm = arm_row()
    assert arm is not None, "the window never opened"
    live0 = json.loads(arm["live"])
    orphan0 = json.loads(arm["orphan"])
    for row in (live0, orphan0):
        assert row and row.get("run_id"), "no running record at the fault"
        assert row.get("worker"), "running record had no worker at the fault"
        assert arm["ts"] - row.get("heartbeat_at", 0) <= P["fresh_heartbeat_s"], \
            "claim not maintained at the fault"
    assert len(starts(role="live", slot=P["bootstrap_slot"])) == 1
    assert len(starts(role="orphan", slot=P["bootstrap_slot"])) == 1


def test_epoch_advanced_and_never_rewound():
    fault = snaps(barrier="fault")
    assert fault, "no observation at the fault"
    base_epoch = (fault[0]["leader"] or {}).get("epoch", 0)
    after = snaps(lo=T0() + P["takeover_deadline_s"],
                  hi=T0() + P["takeover_deadline_s"] + 2.0)
    assert after, "no observation after the takeover deadline"
    epochs = [s["leader"].get("epoch") for s in snaps() if s["leader"]]
    epochs = [e for e in epochs if e is not None]
    assert epochs, "no leader was ever observed"
    assert all(b >= a for a, b in zip(epochs, epochs[1:])), \
        "leader epoch rewound: %s" % epochs
    assert max(e for e in epochs) >= base_epoch + P["epoch_increment"], \
        "no new term after the fault"


def test_forbid_held_across_replacement():
    arm = arm_row()
    live0 = json.loads(arm["live"])
    t0 = T0()
    release = t0 + P["live_release_at_s"]
    # no body of the surviving job between the fault and the release
    later = [s for s in starts(role="live") if s["ts"] > t0 + 0.5 and s["ts"] < release]
    assert not later, "job %s started again while its first body still ran: %s" % (
        EV["job"]["live"], [(s.get("slot"), round(s["ts"] - t0, 2)) for s in later])
    # and never two bodies of a job at once
    for role in ("live", "orphan"):
        iv = intervals(role)
        for (a0, a1, r0, s0), (b0, b1, r1, s1) in zip(iv, iv[1:]):
            assert b0 >= a1, "job %s bodies %s and %s overlapped (%.3f < %.3f)" % (
                EV["job"][role], r0, r1, b0, a1)
    # the durable record carried the same attempt the whole hold
    held = snaps(lo=t0 + 0.5, hi=release - 1.0)
    assert held, "no observations during the hold"
    for s in held:
        row = s["actives"].get("live")
        assert row and row.get("run_id") == live0["run_id"] and \
            row.get("status") == "LIVE", \
            "running record of the surviving job changed at t+%.1f: %s" % (
                s["ts"] - t0, row)
        assert row.get("lease_until", 0) >= s["ts"], \
            "claim of the surviving job lapsed at t+%.1f" % (s["ts"] - t0)
    renewed_by = live0.get("heartbeat_at", 0) + 2 * P["heartbeat_s"] + P["time_tolerance_s"]
    later = [s for s in held if s["ts"] > renewed_by]
    assert later, "no observations after the first renewal was due"
    for s in later:
        assert s["actives"]["live"].get("lease_until", 0) > live0.get("lease_until", 0), \
            "claim of the surviving job was not maintained past its first lease"


def test_bodies_ran_on_the_attempt_of_record():
    assert grants(), "no body was ever permitted"
    granted = {g.get("run_id") for g in grants()}
    bad = [(EV["job"].get(H.role_of(r.get("job"))), r.get("run_id"), "never permitted")
           for r in _event("end") if r.get("run_id") not in granted]
    ran = bodies()
    assert ran, "no body ever ran"
    for begin, end, run_id, slot, g in ran:
        row = json.loads(g.get("claim") or "null")
        if not row or row.get("run_id") != run_id or row.get("status") != "LIVE":
            bad.append((EV["job"].get(H.role_of(g.get("job"))), run_id,
                        "running record at t+%.2f: %s" % (
                            begin - T0(), row and {k: row.get(k) for k in
                                                   ("run_id", "status", "worker")})))
    assert not bad, "bodies ran that were not the attempt of record: %s" % bad


def test_abandoned_work_did_not_block():
    arm = arm_row()
    orphan0 = json.loads(arm["orphan"])
    t0 = T0()
    slots = EV["orphan"]["slots"]
    need = P["orphan_required_slot"]
    row = slots.get(need)
    assert row is not None, "slot %d of the abandoned job was never decided" % need
    assert row["disposition"] == "RUN", \
        "slot %d disposition was %s, the job stayed blocked" % (
            need, row and row.get("disposition"))
    st = starts(role="orphan", slot=need)
    assert len(st) == 1, "slot %d started %d times" % (need, len(st))
    assert st[0]["ts"] <= nominal(need) + P["decision_deadline_s"] + P["time_tolerance_s"], \
        "slot %d started %.2fs after its nominal" % (need, st[0]["ts"] - nominal(need))
    # the old attempt was released and a new one took the job, on a live worker
    handover = [s for s in snaps(lo=t0 + P["restart_at_s"], hi=t0 + P["live_release_at_s"] - 1)
                if s["actives"].get("orphan")
                and s["actives"]["orphan"].get("run_id") != orphan0["run_id"]
                and s["actives"].get("live")
                and s["actives"]["live"].get("run_id") == json.loads(arm["live"])["run_id"]]
    assert handover, "the abandoned job never changed hands while the other was held"
    for slot in range(need + 1, P["periodic_slots"] + 1):
        row = slots.get(slot)
        assert row and row["disposition"] == "RUN", \
            "slot %d of the abandoned job was %s after recovery" % (
                slot, row and row.get("disposition"))


def test_stale_completion_was_harmless():
    replay = [r for r in EV["rows"]
              if r["sk"].startswith("EVENT#") and r.get("barrier") == "replay"]
    assert replay, "no replay was attempted"
    arm = arm_row()
    orphan0 = json.loads(arm["orphan"])
    # the successor claim that was waiting when the replay landed
    successor = [s for s in starts(role="orphan") if s["ts"] > T0()]
    assert successor, "no successor attempt of the abandoned job ever started"
    run_id = successor[0]["run_id"]
    row = None
    for s in snaps(barrier="replay"):
        row = s["actives"].get("orphan")
    if row is not None:
        assert row.get("run_id") != orphan0["run_id"], \
            "the replayed completion still owned the record"
        assert row.get("run_id") == run_id or ends_for(run_id), \
            "the successor's record was disturbed by the replay: %s" % row
    # and the successor body itself finished normally afterwards
    assert ends_for(run_id), "successor body never returned"


def test_survivor_resumed_after_release():
    t0 = T0()
    release = t0 + P["live_release_at_s"]
    arm = arm_row()
    live0 = json.loads(arm["live"])
    end = ends_for(live0["run_id"])
    assert end, "the surviving body never returned after release"
    assert end[0] <= release + P["finish_deadline_s"], \
        "the surviving body took %.2fs to return after release" % (end[0] - release)
    gone = snaps(lo=release + P["finish_deadline_s"], hi=release + P["finish_deadline_s"] + 1.5)
    for s in gone:
        row = s["actives"].get("live")
        assert not row or row.get("run_id") != live0["run_id"] or \
            row.get("status") != "LIVE", "released claim still live after the deadline"
    need = P["live_required_slot"]
    row = EV["live"]["slots"].get(need)
    assert row and row["disposition"] == "RUN", \
        "slot %d of the surviving job was %s after release" % (
            need, row and row.get("disposition"))
    st = starts(role="live", slot=need)
    assert len(st) == 1
    assert st[0]["ts"] <= nominal(need) + P["decision_deadline_s"] + P["time_tolerance_s"]
    for slot in range(need + 1, P["periodic_slots"] + 1):
        row = EV["live"]["slots"].get(slot)
        assert row and row["disposition"] == "RUN", \
            "slot %d of the surviving job was %s" % (
                slot, row and row.get("disposition"))


def test_decision_ledger():
    for role in ("live", "orphan"):
        slots = EV[role]["slots"]
        for slot in range(P["periodic_slots"] + 1):
            row = slots.get(slot)
            assert row is not None, "job %s slot %d has no decision" % (
                EV["job"][role], slot)
            assert row.get("disposition") in ("RUN", "SKIPPED_BUSY", "MISSED_OUTAGE")
            if slot:
                assert row["nominal_at"] == pytest.approx(
                    nominal(slot), abs=0.25), \
                    "job %s slot %d nominal %.2f != anchor-derived %.2f" % (
                        EV["job"][role], slot, row["nominal_at"], nominal(slot))
            if row["disposition"] != "MISSED_OUTAGE":
                assert row["decided_at"] <= row["nominal_at"] + \
                    P["decision_deadline_s"] + P["time_tolerance_s"], \
                    "job %s slot %d decided %.2fs after nominal" % (
                        EV["job"][role], slot, row["decided_at"] - row["nominal_at"])
            else:
                assert row["decided_at"] > row["nominal_at"] + \
                    P["decision_deadline_s"] - P["time_tolerance_s"], \
                    "job %s slot %d discarded while still decidable" % (
                        EV["job"][role], slot)
        order = [slots[s]["decided_at"] for s in sorted(slots)]
        assert order == sorted(order), "job %s decided out of order" % EV["job"][role]
        # every admitted nominal ran at most once, nothing ran before its time
        for s in starts(role=role):
            if s.get("slot"):
                assert s["ts"] >= nominal(s["slot"]) - 0.1, \
                    "job %s slot %s started before its nominal" % (
                        EV["job"][role], s.get("slot"))
            assert len([x for x in starts(role=role, slot=s.get("slot"))]) <= \
                P["max_starts_per_slot"], \
                "job %s slot %s started more than once" % (EV["job"][role], s.get("slot"))
            row = EV[role]["slots"].get(s.get("slot"))
            assert row and row["disposition"] == "RUN", \
                "job %s slot %s ran without being admitted" % (
                    EV["job"][role], s.get("slot"))
            runs = [r for r in EV[role]["runs"] if r.get("run_id") == s.get("run_id")]
            assert runs, "job %s slot %s ran with no attempt record" % (
                EV["job"][role], s.get("slot"))


def test_no_decisions_were_invented():
    for role in ("live", "orphan"):
        for slot, row in EV[role]["slots"].items():
            if slot > P["periodic_slots"]:
                raise AssertionError("job %s decided unknown slot %d" % (
                    EV["job"][role], slot))


