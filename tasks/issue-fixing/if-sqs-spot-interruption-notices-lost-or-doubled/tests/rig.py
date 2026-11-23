"""Verifier rig: one shared queue, two clusters' NTH replicas, the batch fleet's handler.

Everything here is independent of /app: the cluster API is served by this module, the batch
fleet's handler is a thread of this process, and replicas are launched as `python3 -m nth`, each
under its own uid, with only the environment the README documents.
"""

import json
from pathlib import Path
from cdk_seed.cfn_deploy import deploy
import os
import secrets
import shutil
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import boto3

EVICT_SECONDS = 4.0
PICKUP_BOUND_S = 10.0          # OPERATIONS.md SLO 1
DEADLINE_S = 100.0             # OPERATIONS.md SLO 2
OWNER_START_S = 15.0           # batch-c's handler comes up this long after T0
OWNER_WINDOW_S = 30.0          # ... and must have its notice within this window
READY_TIMEOUT_S = 25.0
PUBLISH_1_AT = 3.0
PROD_A_START_AT = 6.0
PUB2_GRACE_S = 3.0
PUB2_LATEST = 110.0
KILL_AFTER_EVICTIONS = 2
CUT_AFTER_EVICTIONS = 2        # b-n1: the holder's cluster-API connection goes dead here
CUT_HOLD_S = 600.0             # a cut-off request is never answered (dropped at teardown)
KILL_WAIT_S = 40.0
REPLACEMENT_AFTER_KILL_S = 5.0
PUB3_AFTER_KILL_S = 10.0
SETTLE_S = 5.0
RUN_CAP_S = 260.0
STOP_GRACE_S = 5.0
QUEUE_VISIBILITY_TIMEOUT = "30"

LOG_DIR = os.environ.get("RIG_LOG_DIR", "/logs/verifier")
APP_DIR = os.environ.get("RIG_APP_DIR", "/app")
UID_BASE = int(os.environ.get("RIG_UID_BASE", "20000"))
PYTHON = os.environ.get("RIG_PYTHON") or shutil.which("python3") or "python3"
NAMESPACES = ["default", "payments", "search", "web"]
WORKLOADS = ["api", "worker", "cache", "render", "ledger", "ingest", "notify", "quote", "cart"]

# label -> (cluster, pods)
NODES = {
    "a-n1": ("prod-a", 10),
    "a-n5": ("prod-a", 18),
    "a-n2": ("prod-a", 10),
    "a-n3": ("prod-a", 14),
    "b-n1": ("prod-b", 10),
}
FOREIGN = "c-i1"
IS_ROOT = os.geteuid() == 0


def now():
    return time.time()


def sleep_until(t):
    while True:
        remaining = t - now()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.2))


def instance_id():
    return "i-0" + secrets.token_hex(8)


def node_name():
    b = secrets.randbelow(250) + 1
    return f"ip-10-{secrets.randbelow(4) + 1}-{b}-{secrets.randbelow(250) + 2}.ec2.internal"


def notice(instance):
    return {
        "version": "0",
        "id": str(uuid.uuid4()),
        "detail-type": "EC2 Spot Instance Interruption Warning",
        "source": "aws.ec2",
        "account": "000000000000",
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "region": "us-east-1",
        "resources": [f"arn:aws:ec2:us-east-1b:instance/{instance}"],
        "detail": {"instance-id": instance, "instance-action": "terminate"},
    }


def uid_pids(uid):
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/status") as fh:
                for line in fh:
                    if line.startswith("Uid:"):
                        if int(line.split()[1]) == uid:
                            pids.append(int(entry))
                        break
        except OSError:
            continue
    return pids


class Node:
    def __init__(self, label, cluster, pods):
        self.label = label
        self.cluster = cluster
        self.name = node_name()
        self.instance_id = instance_id()
        self.pods = [(NAMESPACES[i % len(NAMESPACES)],
                      f"{WORKLOADS[i % len(WORKLOADS)]}-{secrets.token_hex(4)}-{secrets.token_hex(2)}")
                     for i in range(pods)]
        self.pod_count = pods
        self.unschedulable = False
        self.evicting = False
        self.evictions = 0
        self.drained_at = None
        self.published_at = None
        self.message_id = None

    def view(self):
        return {"name": self.name, "instanceID": self.instance_id,
                "providerID": f"aws:///us-east-1b/{self.instance_id}",
                "unschedulable": self.unschedulable, "podCount": len(self.pods)}


class Replica:
    def __init__(self, rig, cid, cluster, uid):
        self.rig = rig
        self.cid = cid
        self.cluster = cluster
        self.uid = uid
        self.token = secrets.token_hex(16)
        self.pod_name = f"aws-node-termination-handler-{secrets.token_hex(3)}-{secrets.token_hex(2)}"
        self.proc = None
        self.launched_at = None
        self.first_request_at = None
        self.killed_at = None
        self.cut_at = None
        self.exited_at = None
        self.returncode = None

    def alive(self):
        return self.proc is not None and self.exited_at is None

    def launch(self):
        home = os.path.join(self.rig.base, self.cid)
        os.makedirs(home, exist_ok=True)
        if IS_ROOT:
            subprocess.run(["pkill", "-KILL", "-U", str(self.uid)], check=False)
            os.chown(home, self.uid, self.uid)
        os.chmod(home, 0o700)
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "HOME": home,
            "TMPDIR": home,
            "PYTHONPATH": APP_DIR,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "AWS_ENDPOINT_URL": os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566"),
            "AWS_DEFAULT_REGION": "us-east-1",
            "AWS_ACCESS_KEY_ID": "test",
            "AWS_SECRET_ACCESS_KEY": "test",
            "QUEUE_URL": self.rig.queue_url,
            "CLUSTER_API_URL": self.rig.apis[self.cluster].url,
            "CLUSTER_API_TOKEN": self.token,
            "POD_NAME": self.pod_name,
        }
        log = open(os.path.join(self.rig.replica_log_dir, f"{self.cid}.log"), "ab")
        kwargs = {}
        if IS_ROOT:
            kwargs = {"user": self.uid, "group": self.uid, "extra_groups": []}
        self.proc = subprocess.Popen(
            [PYTHON, "-m", "nth"], cwd=home, env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, **kwargs)
        log.close()
        self.launched_at = now()
        threading.Thread(target=self._wait, daemon=True).start()

    def _wait(self):
        self.returncode = self.proc.wait()
        self.exited_at = now()

    def signal_all(self, sig):
        if self.proc is None:
            return
        if IS_ROOT:
            name = "KILL" if sig == signal.SIGKILL else "TERM"
            subprocess.run(["pkill", f"-{name}", "-U", str(self.uid)], check=False)
        else:
            try:
                os.killpg(self.proc.pid, sig)
            except ProcessLookupError:
                pass

    def kill(self):
        """The injected fault: every process of this replica dies at once, no notice."""
        self.signal_all(signal.SIGKILL)
        if IS_ROOT:
            deadline = now() + 2
            while uid_pids(self.uid) and now() < deadline:
                subprocess.run(["pkill", "-KILL", "-U", str(self.uid)], check=False)
                time.sleep(0.02)
        self.killed_at = now()


class ClusterAPI:
    def __init__(self, rig, cluster, nodes):
        self.rig = rig
        self.cluster = cluster
        self.nodes = {n.name: n for n in nodes}
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()

    def _handler(self):
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass

            def _send(self, status, body, headers=None):
                raw = json.dumps(body).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    for key, value in (headers or {}).items():
                        self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(raw)
                except OSError:
                    pass

            def _serve(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                status, body, headers = api.handle(method, self.path,
                                                   self.headers.get("Authorization", ""))
                if status is None:
                    # A dead connection: the request never reached the API server.
                    self.close_connection = True
                    return
                self._send(status, body, headers)

            def do_GET(self):
                self._serve("GET")

            def do_POST(self):
                self._serve("POST")

        return Handler

    def handle(self, method, path, auth):
        started = now()
        token = auth[7:] if auth.startswith("Bearer ") else ""
        replica = self.rig.replicas_by_token.get(token)
        url = urlparse(path)
        parts = [p for p in url.path.split("/") if p]
        query = parse_qs(url.query)
        if replica is None or replica.cluster != self.cluster:
            return 401, {"message": "Unauthorized"}, None
        if replica.first_request_at is None:
            replica.first_request_at = started
        if replica.killed_at is not None:
            self.rig.record(started, replica, method, url.path, None, "after-kill", 410)
            return 410, {"message": "Gone"}, None
        if replica.cut_at is not None and started >= replica.cut_at:
            # The replica's connection to the API is dead: nothing is executed, nothing is
            # answered, no error comes back; its client waits until it gives up.
            self.rig.record(started, replica, method, url.path, None, "cut-off", None)
            self.rig.hold_cut_request(started)
            return None, None, None

        if method == "GET" and parts == ["api", "v1", "nodes"]:
            if "instanceId" in query:
                wanted = query["instanceId"][0]
                if self.rig.take_lookup_fault(self.cluster, wanted):
                    self.rig.record(started, replica, method, url.path, wanted, "lookup", 503)
                    return 503, {"message": "the server is currently unable to handle the "
                                            "request"}, {"Retry-After": "1"}
                with self.lock:
                    items = [n.view() for n in self.nodes.values() if n.instance_id == wanted]
                return 200, {"items": items}, None
            with self.lock:
                return 200, {"items": [n.view() for n in self.nodes.values()]}, None

        if len(parts) >= 4 and parts[:3] == ["api", "v1", "nodes"]:
            node = self.nodes.get(parts[3])
            if node is None:
                return 404, {"message": f"nodes \"{parts[3]}\" not found"}, None
            if method == "GET" and len(parts) == 4:
                with self.lock:
                    return 200, node.view(), None
            if method == "POST" and parts[4:] == ["cordon"]:
                with self.lock:
                    node.unschedulable = True
                    view = node.view()
                self.rig.record(started, replica, method, url.path, node.label, "cordon", 200)
                return 200, view, None
            if method == "GET" and parts[4:] == ["pods"]:
                with self.lock:
                    items = [{"name": p, "namespace": ns} for ns, p in node.pods]
                return 200, {"items": items}, None
            return 404, {"message": "not found"}, None

        if (method == "POST" and len(parts) == 7 and parts[:3] == ["api", "v1", "namespaces"]
                and parts[4] == "pods" and parts[6] == "eviction"):
            namespace, pod = parts[3], parts[5]
            with self.lock:
                node = next((n for n in self.nodes.values() if (namespace, pod) in n.pods), None)
                if node is None:
                    status = 404
                elif node.evicting:
                    status = 429
                else:
                    status = 0
                    node.evicting = True
            if node is None:
                return 404, {"message": f"pods \"{pod}\" not found"}, None
            if status == 429:
                self.rig.record(started, replica, method, url.path, node.label, "evict", 429)
                return 429, {"message": "Cannot evict pod as it would violate the pod's "
                                        "disruption budget."}, {"Retry-After": "1"}
            # The eviction completes on the server whether or not the caller is still there.
            time.sleep(EVICT_SECONDS)
            with self.lock:
                node.pods.remove((namespace, pod))
                node.evicting = False
                node.evictions += 1
                count = node.evictions
                if not node.pods and node.drained_at is None:
                    node.drained_at = now()
            self.rig.record(started, replica, method, url.path, node.label, "evict", 201,
                            ended=now())
            self.rig.after_eviction(node, replica, count)
            return 201, {"kind": "Eviction", "pod": pod}, None

        return 404, {"message": "not found"}, None


class Rig:
    def __init__(self):
        self.sqs = boto3.client("sqs")
        self.base = f"/tmp/nth-rig-{secrets.token_hex(4)}"
        os.makedirs(self.base, exist_ok=True)
        os.chmod(self.base, 0o711)
        self.replica_log_dir = os.path.join(LOG_DIR, "replicas")
        os.makedirs(self.replica_log_dir, exist_ok=True)
        self.nodes = {label: Node(label, cluster, pods) for label, (cluster, pods) in NODES.items()}
        self.foreign_instance = instance_id()
        self.foreign_message_id = None
        self.foreign_published_at = None
        self.ops = []
        self.ops_lock = threading.Lock()
        self.queue_url = None
        self.apis = {}
        self.replicas = {}
        self.replicas_by_token = {}
        self.lookup_fault = ("prod-a", self.nodes["a-n1"].instance_id)
        self.victim = None
        self.t_kill = None
        self.cut_victim = None
        self.t_cut = None
        self.kill_event = threading.Event()
        self.t0 = None
        self.t_prod_a_launch = None
        self.t_pub2 = None
        self.teardown_at = None
        self.owner = {"receipts": [], "original_received_at": None, "copies": []}
        self.attrs_before = self.attrs_after = None
        self.resources_before = self.resources_after = None
        self.final_counts = None
        self.notes = []

    # --- bookkeeping -------------------------------------------------------------------

    def record(self, t, replica, method, path, node, op, status, ended=None):
        with self.ops_lock:
            self.ops.append({"t": t, "ended": ended or t, "replica": replica.cid,
                             "cluster": replica.cluster, "method": method, "path": path,
                             "node": node, "op": op, "status": status})

    def take_lookup_fault(self, cluster, instance):
        with self.ops_lock:
            if self.lookup_fault == (cluster, instance):
                self.lookup_fault = None
                return True
        return False

    def hold_cut_request(self, started):
        while self.teardown_at is None and now() < started + CUT_HOLD_S:
            time.sleep(0.2)

    def after_eviction(self, node, replica, count):
        if node.label == "b-n1" and count == CUT_AFTER_EVICTIONS and self.cut_victim is None:
            self.cut_victim = replica.cid
            replica.cut_at = now()
            self.t_cut = replica.cut_at
        if node.label == "a-n2" and count == KILL_AFTER_EVICTIONS and self.victim is None:
            self.victim = replica.cid
            replica.kill()
            self.t_kill = replica.killed_at
            self.kill_event.set()

    # --- AWS ---------------------------------------------------------------------------

    def snapshot(self):
        out = {}
        probes = {
            "sqs": lambda: sorted(boto3.client("sqs").list_queues().get("QueueUrls", [])),
            "dynamodb": lambda: sorted(boto3.client("dynamodb").list_tables().get("TableNames", [])),
            "s3": lambda: sorted(b["Name"] for b in boto3.client("s3").list_buckets().get("Buckets", [])),
            "ssm": lambda: sorted(p["Name"] for p in boto3.client("ssm").describe_parameters(
                MaxResults=50).get("Parameters", [])),
            "sns": lambda: sorted(t["TopicArn"] for t in boto3.client("sns").list_topics().get("Topics", [])),
        }
        for name, probe in probes.items():
            try:
                out[name] = probe()
            except Exception as err:  # noqa: BLE001 - a service the emulator cannot list
                out[name] = None
                self.notes.append(f"snapshot {name}: {err}")
        return out

    def queue_attributes(self):
        return self.sqs.get_queue_attributes(QueueUrl=self.queue_url,
                                             AttributeNames=["All"]).get("Attributes", {})

    def publish(self, label):
        node = self.nodes[label]
        result = self.sqs.send_message(QueueUrl=self.queue_url,
                                       MessageBody=json.dumps(notice(node.instance_id)))
        node.message_id = result["MessageId"]
        node.published_at = now()

    def publish_foreign(self):
        result = self.sqs.send_message(QueueUrl=self.queue_url,
                                       MessageBody=json.dumps(notice(self.foreign_instance)))
        self.foreign_message_id = result["MessageId"]
        self.foreign_published_at = now()

    def run_owner(self):
        """batch-c's own handler: a well-behaved consumer that owns the foreign instance."""
        sqs = boto3.client("sqs")
        sleep_until(self.t0 + OWNER_START_S)
        end = self.t0 + OWNER_START_S + OWNER_WINDOW_S
        while now() < end and self.owner["original_received_at"] is None:
            try:
                result = sqs.receive_message(QueueUrl=self.queue_url, MaxNumberOfMessages=10,
                                             VisibilityTimeout=2, WaitTimeSeconds=1)
            except Exception as err:  # noqa: BLE001
                self.notes.append(f"owner receive: {err}")
                time.sleep(0.5)
                continue
            for message in result.get("Messages", []):
                t = now()
                try:
                    instance = json.loads(message["Body"])["detail"]["instance-id"]
                except (ValueError, KeyError, TypeError):
                    instance = None
                if instance == self.foreign_instance:
                    original = message["MessageId"] == self.foreign_message_id
                    self.owner["receipts"].append({"t": t, "message_id": message["MessageId"],
                                                   "original": original})
                    if original:
                        self.owner["original_received_at"] = t
                    else:
                        self.owner["copies"].append(message["MessageId"])
                    sqs.delete_message(QueueUrl=self.queue_url,
                                       ReceiptHandle=message["ReceiptHandle"])
                else:
                    try:
                        sqs.change_message_visibility(QueueUrl=self.queue_url,
                                                      ReceiptHandle=message["ReceiptHandle"],
                                                      VisibilityTimeout=0)
                    except Exception as err:  # noqa: BLE001
                        self.notes.append(f"owner release: {err}")

    # --- the schedule ------------------------------------------------------------------

    def add_replica(self, cid, cluster, uid):
        replica = Replica(self, cid, cluster, uid)
        self.replicas[cid] = replica
        self.replicas_by_token[replica.token] = replica
        return replica

    def drained(self, *labels):
        return all(self.nodes[label].drained_at is not None for label in labels)

    def run(self):
        subprocess.run(["chmod", "-R", "a+rX", APP_DIR], check=False)
        name = f"spot-interruptions-{secrets.token_hex(4)}"
        self.queue_url = deploy(boto3.client("cloudformation"), "seed-" + name,
                                Path(__file__).parent / "cdk_seed/seed.template.json",
                                QueueName=name)["QueueUrl"]
        self.attrs_before = self.queue_attributes()
        self.resources_before = self.snapshot()

        for cluster in ("prod-a", "prod-b"):
            self.apis[cluster] = ClusterAPI(
                self, cluster, [n for n in self.nodes.values() if n.cluster == cluster])
        b = [self.add_replica("b1", "prod-b", UID_BASE + 11),
             self.add_replica("b2", "prod-b", UID_BASE + 12)]
        a = [self.add_replica(f"a{i}", "prod-a", UID_BASE + i) for i in (1, 2, 3)]
        replacement = self.add_replica("a4", "prod-a", UID_BASE + 4)

        for replica in b:
            replica.launch()
        ready_by = now() + READY_TIMEOUT_S
        while now() < ready_by and not all(r.first_request_at for r in b):
            time.sleep(0.1)
        self.t0 = now()
        owner = threading.Thread(target=self.run_owner, daemon=True)
        owner.start()

        sleep_until(self.t0 + PUBLISH_1_AT)
        self.publish("a-n1")
        self.publish("a-n5")
        self.publish_foreign()

        sleep_until(self.t0 + PROD_A_START_AT)
        self.t_prod_a_launch = now()
        for replica in a:
            replica.launch()

        while not self.drained("a-n1", "a-n5") and now() < self.t0 + PUB2_LATEST:
            time.sleep(0.2)
        if self.drained("a-n1", "a-n5"):
            sleep_until(max(self.nodes["a-n1"].drained_at, self.nodes["a-n5"].drained_at)
                        + PUB2_GRACE_S)
        self.t_pub2 = now()
        self.publish("a-n2")
        self.publish("b-n1")

        self.kill_event.wait(KILL_WAIT_S)
        base = self.t_kill if self.t_kill is not None else now()
        if self.t_kill is None:
            self.notes.append("no replica reached the second eviction on a-n2")
        sleep_until(base + REPLACEMENT_AFTER_KILL_S)
        replacement.launch()
        sleep_until(base + PUB3_AFTER_KILL_S)
        self.publish("a-n3")

        cap = self.t0 + RUN_CAP_S
        while now() < cap:
            if self.drained(*self.nodes):
                sleep_until(max(n.drained_at for n in self.nodes.values()) + SETTLE_S)
                break
            if all(now() > n.published_at + DEADLINE_S + SETTLE_S for n in self.nodes.values()
                   if n.drained_at is None):
                break
            if not any(r.alive() for r in self.replicas.values()):
                self.notes.append("every replica exited")
                break
            time.sleep(0.2)

        self.teardown(owner)

    def teardown(self, owner=None):
        self.teardown_at = now()
        for replica in self.replicas.values():
            if replica.alive():
                replica.signal_all(signal.SIGTERM)
        deadline = now() + STOP_GRACE_S
        while now() < deadline and any(r.alive() for r in self.replicas.values()):
            time.sleep(0.1)
        for replica in self.replicas.values():
            if replica.proc is not None:
                replica.signal_all(signal.SIGKILL)
        if owner is not None:
            owner.join(timeout=OWNER_WINDOW_S + 5)
        time.sleep(1)
        if self.queue_url:
            try:
                self.attrs_after = self.queue_attributes()
                self.final_counts = {
                    key: self.attrs_after.get(key) for key in (
                        "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible",
                        "ApproximateNumberOfMessagesDelayed")}
            except Exception as err:  # noqa: BLE001
                self.notes.append(f"final attributes: {err}")
        self.resources_after = self.snapshot()
        for api in self.apis.values():
            api.stop()

    def dump(self):
        def replica_view(r):
            return {k: getattr(r, k) for k in ("cid", "cluster", "uid", "pod_name", "launched_at",
                                                "first_request_at", "killed_at", "cut_at",
                                                "exited_at", "returncode")}
        data = {
            "t0": self.t0, "t_prod_a_launch": self.t_prod_a_launch, "t_pub2": self.t_pub2,
            "t_kill": self.t_kill, "victim": self.victim, "teardown_at": self.teardown_at,
            "t_cut": self.t_cut, "cut_victim": self.cut_victim,
            "queue_url": self.queue_url,
            "nodes": {label: {"name": n.name, "instance_id": n.instance_id, "cluster": n.cluster,
                              "pods": n.pod_count, "published_at": n.published_at,
                              "message_id": n.message_id, "drained_at": n.drained_at,
                              "remaining": len(n.pods)}
                      for label, n in self.nodes.items()},
            "foreign": {"instance_id": self.foreign_instance,
                        "message_id": self.foreign_message_id,
                        "published_at": self.foreign_published_at, "owner": self.owner},
            "replicas": {cid: replica_view(r) for cid, r in self.replicas.items()},
            "ops": self.ops,
            "attrs_before": self.attrs_before, "attrs_after": self.attrs_after,
            "final_counts": self.final_counts,
            "resources_before": self.resources_before, "resources_after": self.resources_after,
            "notes": self.notes,
        }
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "run.json"), "w") as fh:
            json.dump(data, fh, indent=1, default=str)
        return data
