"""Verifier harness: per-cluster forwarding proxies with fault rules, a target-group sampler, a
supervisor for the two controller processes, and the graded schedule.

Nothing here is imported from /app. Expectations are derived from the schedule itself.
"""
import http.client
import http.server
import json
import os
import re
import secrets
import signal
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
import urllib.parse

import boto3
from pathlib import Path
from cdk_seed.cfn_deploy import deploy
from botocore.config import Config

FLOCI = "http://127.0.0.1:4566"
FLOCI_HOST, FLOCI_PORT = "127.0.0.1", 4566
REGION = "us-east-1"
LOG_DIR = os.environ.get("TGB_VERIFIER_LOG_DIR", "/logs/verifier")
CONTROLLER = os.environ.get("TGB_CONTROLLER", "/app/controller.py")
APP_DIR = os.path.dirname(CONTROLLER)

TARGET_PORT = 8080
TARGET_AZ = "all"
LEDGER_TABLE = "tgb_ownership"
CLUSTERS = ("cluster-a", "cluster-b")

CONVERGENCE_DEADLINE_S = 15.0
STARTUP_DEADLINE_S = 20.0
STABLE_WINDOW_S = 5.0
SAMPLE_INTERVAL_S = 0.25
RESTART_DELAY_S = 2.0
OUTAGE_EDIT_DELAY_S = 0.5
FAULT_WAIT_S = 15.0
ARM_SETTLE_S = 1.0
DROP_QUIET_S = 3.0
DROP_MAX_S = 30.0
EXIT_WAIT_S = 15.0
HANDOFF_ITEM_WAIT_S = 8.0
# README: after a failover the standby endpoint agent first writes the snapshot it last had, and
# writes the current state up to 15 s later.
STALE_SNAPSHOT_S = 12.0
STALE_LEDGER_AT_S = 10.0
# README: a released pod address is handed out again only after the subnet's 30 s hold-down.
ADDRESS_HOLD_DOWN_S = 30.0
HOLD_DOWN_MARGIN_S = 0.5


def tid(host):
    return f"10.0.1.{host}:{TARGET_PORT}"


A = {h: tid(h) for h in (10, 13, 16, 19, 21, 24, 25)}
B = {h: tid(h) for h in (11, 14, 17, 20, 23)}
M = {h: tid(h) for h in (12, 15, 18, 22)}
OWNER = {**{t: "cluster-a" for t in A.values()}, **{t: "cluster-b" for t in B.values()},
         **{t: "manual" for t in M.values()}}


def owner_at(handoffs, target, ts):
    """Who owns `target` at `ts`: its first cluster, until the schedule handed its address to a pod
    of the other cluster."""
    owner = OWNER.get(target)
    for h in sorted(handoffs, key=lambda h: h["ts"]):
        if h["target"] == target and h["ts"] <= ts:
            owner = h["to"]
    return owner


def ids(table, *hosts):
    return {table[h] for h in hosts}


IDENT_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}:\d{1,5}\b")
TARGET_RE = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}:\d{1,5}")
DDB_MUTATING = {"PutItem", "UpdateItem", "DeleteItem", "BatchWriteItem", "TransactWriteItems"}
DDB_PARTIQL = {"ExecuteStatement", "BatchExecuteStatement", "ExecuteTransaction"}
HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "content-length", "proxy-connection",
               "upgrade", "te", "trailer"}


def client(service):
    return boto3.client(service, endpoint_url=FLOCI, region_name=REGION,
                        aws_access_key_id="test", aws_secret_access_key="test",
                        config=Config(retries={"max_attempts": 5, "mode": "standard"},
                                      connect_timeout=5, read_timeout=30))


# --------------------------------------------------------------------------------------------
# Proxy


def _service_of(headers):
    target = headers.get("X-Amz-Target", "")
    if target.startswith("DynamoDB_"):
        return "dynamodb"
    auth = headers.get("Authorization", "")
    m = re.search(r"Credential=[^/]+/\d+/[^/]+/([^/]+)/", auth)
    return m.group(1) if m else "unknown"


def parse_request(headers, body):
    """Classify one AWS request. Returns a log entry without timing/outcome fields."""
    service = _service_of(headers)
    entry = {"service": service, "action": None, "targets": [], "members": [], "removal": False,
             "mutating": False}
    text = body.decode("utf-8", "replace")
    if service == "dynamodb":
        op = headers.get("X-Amz-Target", "").split(".", 1)[-1]
        entry["action"] = op
        entry["targets"] = sorted(set(IDENT_RE.findall(text)))
        entry["mutating"] = op in DDB_MUTATING
        entry["removal"] = (op in ("DeleteItem", "UpdateItem")
                            or (op == "BatchWriteItem" and "DeleteRequest" in text)
                            or (op == "TransactWriteItems" and '"Delete"' in text))
        if op in DDB_PARTIQL:
            upper = text.upper()
            entry["mutating"] = any(word in upper for word in ("INSERT", "UPDATE", "DELETE"))
            entry["removal"] = "DELETE" in upper or "UPDATE" in upper
        return entry
    form = urllib.parse.parse_qs(text, keep_blank_values=True)
    entry["action"] = (form.get("Action") or [None])[0]
    members = {}
    for key, values in form.items():
        m = re.fullmatch(r"Targets\.member\.(\d+)\.(\w+)", key)
        if m:
            members.setdefault(int(m.group(1)), {})[m.group(2)] = values[0]
    entry["members"] = [members[k] for k in sorted(members)]
    entry["targets"] = sorted({f"{mb.get('Id')}:{mb.get('Port', TARGET_PORT)}" for mb in entry["members"]})
    entry["mutating"] = entry["action"] in ("RegisterTargets", "DeregisterTargets")
    entry["removal"] = entry["action"] == "DeregisterTargets"
    return entry


def policy_violation(entry):
    """Egress policy on target descriptions (README: The shared target group)."""
    if entry["service"] != "elasticloadbalancing":
        return None
    action = entry["action"]
    if action not in ("RegisterTargets", "DeregisterTargets"):
        return None
    for mb in entry["members"]:
        extra = set(mb) - {"Id", "Port", "AvailabilityZone"}
        if extra:
            return f"unexpected target fields {sorted(extra)}"
        if action == "RegisterTargets":
            if mb.get("Port") != str(TARGET_PORT) or mb.get("AvailabilityZone") != TARGET_AZ or not mb.get("Id"):
                return f"target description {mb} violates egress policy"
        else:
            if "Port" in mb and mb["Port"] != str(TARGET_PORT):
                return f"target description {mb} violates egress policy"
            if "AvailabilityZone" in mb and mb["AvailabilityZone"] != TARGET_AZ:
                return f"target description {mb} violates egress policy"
    return None


class Rule:
    def __init__(self, match, mode, once=True):
        self.match = match
        self.mode = mode            # forward_then_kill | kill_before_forward | forward_then_drop
        self.once = once
        self.fired = threading.Event()
        self.fire_ts = None
        self.fire_entry = None
        self.fire_count = 0
        self.last_match_ts = None
        self.last_other_ts = None
        self.armed = True


class FaultProxy:
    def __init__(self, cluster, kill_fn, log_path):
        self.cluster = cluster
        self.kill_fn = kill_fn
        self.entries = []
        self.lock = threading.Lock()
        self.rule = None
        self.log_fh = open(log_path, "a", buffering=1)
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                proxy.handle(self)

            do_GET = do_POST
            do_PUT = do_POST
            do_DELETE = do_POST

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.log_fh.close()

    def arm(self, rule):
        with self.lock:
            self.rule = rule
        return rule

    def disarm(self):
        with self.lock:
            if self.rule is not None:
                self.rule.armed = False
            self.rule = None

    def snapshot(self):
        with self.lock:
            return list(self.entries)

    def _log(self, entry):
        with self.lock:
            self.entries.append(entry)
        try:
            self.log_fh.write(json.dumps(entry) + "\n")
        except ValueError:
            pass

    def _matching_rule(self, entry):
        with self.lock:
            rule = self.rule
            if rule is None or not rule.armed:
                return None
            if not rule.match(entry):
                rule.last_other_ts = time.time()
                return None
            rule.last_match_ts = time.time()
            if rule.once:
                rule.armed = False
                self.rule = None
            return rule

    def handle(self, req):
        length = int(req.headers.get("Content-Length") or 0)
        body = req.rfile.read(length) if length else b""
        entry = parse_request(req.headers, body)
        entry.update({"ts": time.time(), "cluster": self.cluster, "forwarded": False,
                      "rejected": False, "http_status": None, "fault": None, "path": req.path})
        violation = policy_violation(entry)
        if violation:
            entry.update({"rejected": True, "http_status": 400, "reason": violation})
            self._log(entry)
            payload = ("<ErrorResponse xmlns=\"http://elasticloadbalancing.amazonaws.com/doc/2015-12-01/\">"
                       "<Error><Type>Sender</Type><Code>ValidationError</Code>"
                       f"<Message>egress policy: {violation}</Message></Error>"
                       f"<RequestId>{secrets.token_hex(8)}</RequestId></ErrorResponse>").encode()
            req.send_response(400)
            req.send_header("Content-Type", "text/xml")
            req.send_header("Content-Length", str(len(payload)))
            req.end_headers()
            req.wfile.write(payload)
            return
        rule = self._matching_rule(entry)
        if rule is not None and rule.mode == "kill_before_forward":
            entry["fault"] = rule.mode
            self._log(entry)
            self._fire(rule, entry)
            self.kill_fn()
            self._abort(req)
            return
        try:
            status, headers, data = self._forward(req, body)
        except OSError as exc:
            entry.update({"http_status": 502, "error": str(exc)})
            self._log(entry)
            self._abort(req)
            return
        entry.update({"forwarded": True, "http_status": status})
        if rule is not None:
            entry["fault"] = rule.mode
            self._log(entry)
            if rule.mode == "forward_then_kill":
                self.kill_fn()
            self._fire(rule, entry)
            self._abort(req)
            return
        self._log(entry)
        req.send_response(status)
        for key, value in headers:
            if key.lower() not in HOP_HEADERS:
                req.send_header(key, value)
        req.send_header("Content-Length", str(len(data)))
        req.end_headers()
        req.wfile.write(data)

    def _fire(self, rule, entry):
        rule.fire_count += 1
        if rule.fire_ts is None:
            rule.fire_ts = entry["ts"]
            rule.fire_entry = entry
        rule.fired.set()

    def _forward(self, req, body):
        conn = http.client.HTTPConnection(FLOCI_HOST, FLOCI_PORT, timeout=30)
        try:
            headers = {k: v for k, v in req.headers.items() if k.lower() not in HOP_HEADERS}
            headers["Content-Length"] = str(len(body))
            conn.request(req.command, req.path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, resp.getheaders(), data
        finally:
            conn.close()

    @staticmethod
    def _abort(req):
        req.close_connection = True
        try:
            req.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


# --------------------------------------------------------------------------------------------
# Sampler


class Sampler:
    def __init__(self, tg_arn):
        self.tg_arn = tg_arn
        self.samples = []           # (ts, frozenset(ids), [target dicts]) or (ts, None, error)
        self.lock = threading.Lock()
        self.stop_evt = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.elbv2 = client("elbv2")

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_evt.set()
        self.thread.join(timeout=10)

    def _run(self):
        while not self.stop_evt.is_set():
            started = time.time()
            try:
                resp = self.elbv2.describe_target_health(TargetGroupArn=self.tg_arn)
                descs = resp.get("TargetHealthDescriptions", [])
                present = frozenset(
                    f"{d['Target']['Id']}:{d['Target']['Port']}" for d in descs
                    if d.get("TargetHealth", {}).get("State") != "draining")
                with self.lock:
                    self.samples.append((time.time(), present, [d["Target"] for d in descs]))
            except Exception as exc:  # keep sampling; a gap is visible in the record
                with self.lock:
                    self.samples.append((time.time(), None, repr(exc)))
            self.stop_evt.wait(max(0.0, SAMPLE_INTERVAL_S - (time.time() - started)))

    def snapshot(self):
        with self.lock:
            return list(self.samples)

    def latest(self):
        with self.lock:
            for sample in reversed(self.samples):
                if sample[1] is not None:
                    return sample
        return None


def wait_converged(sampler, expected, t_from, deadline_s, stable_s=STABLE_WINDOW_S):
    """Wait until the group equals `expected` (first reached within deadline_s of t_from) and
    stays equal for stable_s. Returns a dict describing the outcome."""
    expected = frozenset(expected)
    hard_end = t_from + deadline_s + stable_s + 2.0
    while True:
        now = time.time()
        samples = [s for s in sampler.snapshot() if s[0] >= t_from and s[1] is not None]
        start = None
        for ts, present, _ in samples:
            if present == expected:
                if start is None and ts <= t_from + deadline_s:
                    start = ts
            else:
                start = None
        if start is not None and samples and samples[-1][0] - start >= stable_s:
            return {"ok": True, "converged_after_s": round(start - t_from, 2)}
        if start is None and now > t_from + deadline_s:
            break
        if now > hard_end:
            break
        time.sleep(SAMPLE_INTERVAL_S)
    last = sampler.latest()
    seen = set(last[1]) if last else set()
    return {"ok": False, "expected": sorted(expected), "last_seen": sorted(seen),
            "missing": sorted(expected - seen), "unexpected": sorted(seen - expected)}


# --------------------------------------------------------------------------------------------
# Controllers


class Controllers:
    """Spawns, kills and supervises the two controller processes."""

    def __init__(self, tg_arn, binding_files, proxies):
        self.tg_arn = tg_arn
        self.binding_files = binding_files
        self.proxies = proxies
        self.procs = {}
        self.all_procs = {}
        self.expected_exit = {}
        self.events = []            # (ts, cluster, what, detail)
        self.exit_codes = {}
        self.lock = threading.RLock()
        self.stop_evt = threading.Event()
        self.supervisor = threading.Thread(target=self._supervise, daemon=True)

    def spawn(self, cluster):
        scratch = tempfile.mkdtemp(prefix=f"tgb-{cluster}-")
        proxy = self.proxies[cluster]
        cmd = ["python3", CONTROLLER, "--cluster-id", cluster, "--target-group-arn", self.tg_arn,
               "--binding-file", self.binding_files[cluster], "--aws-endpoint", proxy.url,
               "--ledger-table", LEDGER_TABLE, "--availability-zone", TARGET_AZ,
               "--scratch-dir", scratch]
        env = dict(os.environ)
        env.update({"PYTHONPATH": APP_DIR, "AWS_ENDPOINT_URL": proxy.url, "AWS_ACCESS_KEY_ID": "test",
                    "AWS_SECRET_ACCESS_KEY": "test", "AWS_DEFAULT_REGION": REGION,
                    "PYTHONUNBUFFERED": "1"})
        for key in list(env):
            if key.startswith("AWS_ENDPOINT_URL_"):
                env.pop(key)
        logf = open(os.path.join(LOG_DIR, f"controller-{cluster}.log"), "ab")
        logf.write(f"\n===== spawn {time.strftime('%H:%M:%S')} {' '.join(cmd)}\n".encode())
        logf.flush()
        proc = subprocess.Popen(cmd, cwd=scratch, env=env, stdout=logf, stderr=logf,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        logf.close()
        with self.lock:
            self.procs[cluster] = proc
            self.all_procs[cluster] = proc
            self.expected_exit[cluster] = False
            self.events.append((time.time(), cluster, "spawn", proc.pid))
        return proc

    def kill(self, cluster, reason="fault"):
        with self.lock:
            proc = self.procs.get(cluster)
            if proc is None:
                return
            self.expected_exit[cluster] = True
            self.events.append((time.time(), cluster, "kill", reason))
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    def kill_fn(self, cluster):
        return lambda: self.kill(cluster)

    def wait_dead(self, cluster, timeout=5.0):
        proc = self.procs.get(cluster)
        if proc is None:
            return
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass

    def expect_exit(self, cluster):
        with self.lock:
            self.expected_exit[cluster] = True

    def last_proc(self, cluster):
        with self.lock:
            return self.procs.get(cluster) or self.all_procs.get(cluster)

    def alive(self, cluster):
        proc = self.procs.get(cluster)
        return proc is not None and proc.poll() is None

    def start_supervisor(self):
        self.supervisor.start()

    def _supervise(self):
        while not self.stop_evt.wait(0.2):
            with self.lock:
                for cluster, proc in list(self.procs.items()):
                    rc = proc.poll()
                    if rc is None:
                        continue
                    self.exit_codes.setdefault(cluster, []).append(rc)
                    if self.expected_exit.get(cluster):
                        self.events.append((time.time(), cluster, "exit", rc))
                        self.procs.pop(cluster)
                        continue
                    # The supervisor restarts a controller that died on its own.
                    self.events.append((time.time(), cluster, "unexpected-exit", rc))
                    self.procs.pop(cluster)
                    threading.Timer(RESTART_DELAY_S, self._restart_if_absent, args=(cluster,)).start()

    def _restart_if_absent(self, cluster):
        if self.stop_evt.is_set():
            return
        with self.lock:
            if cluster not in self.procs:
                self.spawn(cluster)

    def shutdown(self):
        self.stop_evt.set()
        for cluster in list(self.procs):
            self.kill(cluster, reason="teardown")
            self.wait_dead(cluster)


# --------------------------------------------------------------------------------------------
# Schedule


def write_binding(path, generation, endpoints, deleted=False):
    tmp = f"{path}.tmp-{secrets.token_hex(4)}"
    with open(tmp, "w") as fh:
        json.dump({"generation": generation, "deleted": deleted, "endpoints": sorted(endpoints)}, fh)
    os.rename(tmp, path)


def query_owner(ddb, owner):
    items, kwargs = [], {}
    while True:
        resp = ddb.query(TableName=LEDGER_TABLE, KeyConditionExpression="#o = :o",
                         ExpressionAttributeNames={"#o": "owner"},
                         ExpressionAttributeValues={":o": {"S": owner}}, ConsistentRead=True, **kwargs)
        # Only items that name a target are ownership items; anything else a controller keeps
        # under its id is its own bookkeeping and is not graded.
        items.extend(it["target"]["S"] for it in resp.get("Items", [])
                     if TARGET_RE.fullmatch(it.get("target", {}).get("S", "")))
        if "LastEvaluatedKey" not in resp:
            return sorted(items)
        kwargs = {"ExclusiveStartKey": resp["LastEvaluatedKey"]}


def wait_item(ddb, owner, target, timeout):
    """Seconds until `owner` has an item for `target` (README: every target a cluster has in the
    group has an item), or None if it never appears within timeout."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if target in query_owner(ddb, owner):
            return round(time.time() - t0, 2)
        time.sleep(0.2)
    return None


def reset_table(ddb):
    try:
        ddb.delete_table(TableName=LEDGER_TABLE)
        for _ in range(50):
            try:
                ddb.describe_table(TableName=LEDGER_TABLE)
                time.sleep(0.2)
            except ddb.exceptions.ResourceNotFoundException:
                break
    except ddb.exceptions.ResourceNotFoundException:
        pass
    deploy(client("cloudformation"),
           "seed-ledger-" + secrets.token_hex(8),
           Path(__file__).parent / "cdk_seed/seed.template.json",
           TableName=LEDGER_TABLE)
    for _ in range(100):
        if ddb.describe_table(TableName=LEDGER_TABLE)["Table"].get("TableStatus") == "ACTIVE":
            return
        time.sleep(0.2)


def kill_strays():
    subprocess.run(["pkill", "-9", "-f", r"controller\.py.*--cluster-id"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)


def manual_register(elbv2, tg_arn, target):
    ip, port = target.rsplit(":", 1)
    elbv2.register_targets(TargetGroupArn=tg_arn,
                           Targets=[{"Id": ip, "Port": int(port), "AvailabilityZone": TARGET_AZ}])


def _contains(target):
    return lambda entry: target in entry["targets"]


def run_schedule():
    os.makedirs(LOG_DIR, exist_ok=True)
    kill_strays()
    elbv2, ddb = client("elbv2"), client("dynamodb")
    res = {"phases": {}, "ledger_snapshots": [], "ledger_at_kill": None, "handoffs": []}

    tg_name = f"web-{secrets.token_hex(4)}"
    tg_arn = deploy(client("cloudformation"), "seed-" + tg_name,
                    Path(__file__).parent / "cdk_seed/target.template.json",
                    GroupName=tg_name, Port=TARGET_PORT)["TargetGroupArn"]
    reset_table(ddb)
    res["tg_arn"] = tg_arn
    manual_t0 = time.time()
    for h in (12, 15, 18):
        manual_register(elbv2, tg_arn, M[h])
    res["manual_registered"] = {M[h]: manual_t0 for h in (12, 15, 18)}

    workdir = tempfile.mkdtemp(prefix="tgb-run-")
    files = {}
    for cluster in CLUSTERS:
        os.makedirs(os.path.join(workdir, cluster), exist_ok=True)
        files[cluster] = os.path.join(workdir, cluster, "binding.json")
    gen = {"cluster-a": 1, "cluster-b": 1}
    a_eps = ids(A, 10, 13, 16, 19)
    b_eps = ids(B, 11, 14, 17, 20)
    write_binding(files["cluster-a"], gen["cluster-a"], a_eps)
    write_binding(files["cluster-b"], gen["cluster-b"], b_eps)

    proxies, ctl = {}, None
    holder = {}
    for cluster in CLUSTERS:
        proxies[cluster] = FaultProxy(cluster, lambda c=cluster: holder["ctl"].kill(c),
                                      os.path.join(LOG_DIR, f"proxy-{cluster}.jsonl"))
        proxies[cluster].start()
    ctl = Controllers(tg_arn, files, proxies)
    holder["ctl"] = ctl
    sampler = Sampler(tg_arn)
    sampler.start()

    def bump(cluster, endpoints, deleted=False):
        gen[cluster] += 1
        write_binding(files[cluster], gen[cluster], endpoints, deleted=deleted)

    def listed_now():
        descs = elbv2.describe_target_health(TargetGroupArn=tg_arn)["TargetHealthDescriptions"]
        return {f"{d['Target']['Id']}:{d['Target']['Port']}" for d in descs
                if d.get("TargetHealth", {}).get("State") != "draining"}

    def snapshot_ledger(label):
        # listed before and after the query: a target listed at both instants was registered
        # throughout, so its item must be visible to the query in between.
        before = listed_now()
        rows = {c: query_owner(ddb, c) for c in CLUSTERS}
        after = listed_now()
        snap = {"label": label, "ts": time.time(), "rows": rows, "listed": sorted(before & after)}
        res["ledger_snapshots"].append(snap)
        return snap

    def sleep_until(t):
        time.sleep(max(0.0, t - time.time()))

    try:
        # P1: both clusters and the legacy hosts coexist.
        t0 = time.time()
        ctl.spawn("cluster-a")
        ctl.spawn("cluster-b")
        ctl.start_supervisor()
        e1 = a_eps | b_eps | ids(M, 12, 15, 18)
        res["phases"]["P1"] = {"expected": sorted(e1),
                               **wait_converged(sampler, e1, t0, STARTUP_DEADLINE_S)}
        snapshot_ledger("P1")

        # P2: RegisterTargets applied, controller killed before it sees the response; the pod
        # goes away while it is down; a legacy host is added and cluster-b churns meanwhile.
        rule = proxies["cluster-a"].arm(Rule(
            lambda e: e["service"] == "elasticloadbalancing" and e["action"] == "RegisterTargets"
            and A[21] in e["targets"], "forward_then_kill"))
        time.sleep(ARM_SETTLE_S)
        bump("cluster-a", a_eps | {A[21]})
        fired = rule.fired.wait(FAULT_WAIT_S)
        proxies["cluster-a"].disarm()
        p2 = {"fired": fired}
        if fired:
            ctl.wait_dead("cluster-a")
            res["ledger_at_kill"] = {"targets": rule.fire_entry["targets"],
                                     "rows": query_owner(ddb, "cluster-a"), "ts": time.time()}
            base = rule.fire_ts
        else:
            base = time.time()
        sleep_until(base + OUTAGE_EDIT_DELAY_S)
        bump("cluster-a", a_eps)
        manual_register(elbv2, tg_arn, M[22])
        res["manual_registered"][M[22]] = time.time()
        b_eps = ids(B, 14, 17, 20, 23)
        bump("cluster-b", b_eps)
        t_edit = time.time()
        if fired:
            sleep_until(base + RESTART_DELAY_S)
            ctl.spawn("cluster-a")
        t_from = time.time() if fired else t_edit
        e2 = a_eps | b_eps | ids(M, 12, 15, 18, 22)
        p2.update({"expected": sorted(e2), **wait_converged(sampler, e2, t_from, CONVERGENCE_DEADLINE_S)})
        res["phases"]["P2"] = p2
        snapshot_ledger("P2")

        # P3: a DeregisterTargets that never reaches ELB.
        rule = proxies["cluster-a"].arm(Rule(
            lambda e: e["service"] == "elasticloadbalancing" and e["action"] == "DeregisterTargets"
            and A[13] in e["targets"], "kill_before_forward"))
        time.sleep(ARM_SETTLE_S)
        a_eps = ids(A, 10, 16, 19)
        bump("cluster-a", a_eps)
        t_edit = time.time()
        fired = rule.fired.wait(FAULT_WAIT_S)
        proxies["cluster-a"].disarm()
        if fired:
            ctl.wait_dead("cluster-a")
            sleep_until(rule.fire_ts + RESTART_DELAY_S)
            ctl.spawn("cluster-a")
        t_from = time.time() if fired else t_edit
        e3 = a_eps | b_eps | ids(M, 12, 15, 18, 22)
        res["phases"]["P3"] = {"fired": fired, "expected": sorted(e3),
                               **wait_converged(sampler, e3, t_from, CONVERGENCE_DEADLINE_S)}
        snapshot_ledger("P3")

        # P4: RegisterTargets applied but every response dropped (controller stays up); the pod
        # goes away while the call is failing.
        rule = proxies["cluster-a"].arm(Rule(
            lambda e: e["service"] == "elasticloadbalancing" and e["action"] == "RegisterTargets"
            and A[24] in e["targets"], "forward_then_drop", once=False))
        time.sleep(ARM_SETTLE_S)
        bump("cluster-a", a_eps | {A[24]})
        t_edit = time.time()
        fired = rule.fired.wait(FAULT_WAIT_S)
        if fired:
            sleep_until(rule.fire_ts + OUTAGE_EDIT_DELAY_S)
        bump("cluster-a", a_eps)
        t_shrink = time.time()
        if fired:
            # Keep dropping until the controller has given up on the call (it has moved on to other
            # requests since the last matching one) and has stayed quiet on it, or DROP_MAX_S.
            while time.time() < rule.fire_ts + DROP_MAX_S:
                last_match = rule.last_match_ts or 0
                moved_on = (rule.last_other_ts or 0) > last_match
                if (moved_on and time.time() - last_match >= DROP_QUIET_S
                        and time.time() - t_shrink >= 2.0):
                    break
                time.sleep(0.1)
        proxies["cluster-a"].disarm()
        t_from = time.time()
        e4 = a_eps | b_eps | ids(M, 12, 15, 18, 22)
        res["phases"]["P4"] = {"fired": fired, "drops": rule.fire_count,
                               "drop_window_s": round(t_from - (rule.fire_ts or t_edit), 2),
                               "expected": sorted(e4),
                               **wait_converged(sampler, e4, t_from, CONVERGENCE_DEADLINE_S)}
        snapshot_ledger("P4")

        # P5: cluster-a's pod goes away and cluster-a is killed before its DeregisterTargets reaches
        # ELB; after the hold-down the address is handed to a new cluster-b pod while cluster-a is
        # still down.
        rule = proxies["cluster-a"].arm(Rule(
            lambda e: e["service"] == "elasticloadbalancing" and e["action"] == "DeregisterTargets"
            and A[16] in e["targets"], "kill_before_forward"))
        time.sleep(ARM_SETTLE_S)
        a_eps = ids(A, 10, 19)
        bump("cluster-a", a_eps)
        t_edit = time.time()
        fired = rule.fired.wait(FAULT_WAIT_S)
        proxies["cluster-a"].disarm()
        p5 = {"fired": fired}
        if fired:
            ctl.wait_dead("cluster-a")
            sleep_until(rule.fire_ts + OUTAGE_EDIT_DELAY_S)
        sleep_until(t_edit + ADDRESS_HOLD_DOWN_S + HOLD_DOWN_MARGIN_S)
        b_eps = b_eps | {A[16]}
        bump("cluster-b", b_eps)
        res["handoffs"].append({"target": A[16], "from": "cluster-a", "to": "cluster-b",
                                "ts": time.time()})
        p5["b_item_after_s"] = wait_item(ddb, "cluster-b", A[16], HANDOFF_ITEM_WAIT_S)
        if fired:
            sleep_until(max(rule.fire_ts + RESTART_DELAY_S, time.time() + ARM_SETTLE_S))
            ctl.spawn("cluster-a")
        t_from = time.time() if fired else t_edit
        e5 = a_eps | b_eps | ids(M, 12, 15, 18, 22)
        p5.update({"expected": sorted(e5), **wait_converged(sampler, e5, t_from, CONVERGENCE_DEADLINE_S)})
        p5["end_ts"] = time.time()
        res["phases"]["P5"] = p5
        snapshot_ledger("P5")

        # P6: cluster-a's node fails while cluster-a registers a new pod, and takes the active
        # endpoint agent with it. The standby agent first writes the snapshot it last had (the
        # previous generation, without the new pod), then the current state a few seconds later.
        stale_gen, stale_eps = gen["cluster-a"], set(a_eps)
        rule = proxies["cluster-a"].arm(Rule(
            lambda e: e["service"] == "elasticloadbalancing" and e["action"] == "RegisterTargets"
            and A[25] in e["targets"], "forward_then_kill"))
        time.sleep(ARM_SETTLE_S)
        a_eps = a_eps | {A[25]}
        bump("cluster-a", a_eps)
        t_edit = time.time()
        fired = rule.fired.wait(FAULT_WAIT_S)
        proxies["cluster-a"].disarm()
        p6 = {"fired": fired, "stale_generation": stale_gen, "generation": gen["cluster-a"]}
        t_from = t_edit
        if fired:
            p6["registered_ts"] = rule.fire_ts
            ctl.wait_dead("cluster-a")
            write_binding(files["cluster-a"], stale_gen, stale_eps)
            p6["stale_written_ts"] = time.time()
            sleep_until(rule.fire_ts + RESTART_DELAY_S)
            ctl.spawn("cluster-a")
            t_respawn = time.time()
            sleep_until(t_respawn + STALE_LEDGER_AT_S)
            snapshot_ledger("P6-stale")
            sleep_until(t_respawn + STALE_SNAPSHOT_S)
            write_binding(files["cluster-a"], gen["cluster-a"], a_eps)
            t_from = p6["current_written_ts"] = time.time()
        e6 = a_eps | b_eps | ids(M, 12, 15, 18, 22)
        p6.update({"expected": sorted(e6), "a_endpoints": sorted(a_eps),
                   **wait_converged(sampler, e6, t_from, CONVERGENCE_DEADLINE_S)})
        p6["end_ts"] = time.time()
        res["phases"]["P6"] = p6
        snapshot_ledger("P6")

        # R1: cluster-a goes down, its pod goes away while it is down, and after the hold-down the
        # address is handed to a new cluster-b pod. cluster-b's controller then stops (its pod stays
        # up). cluster-a comes back after an endpoint-agent failover: the standby first writes the
        # snapshot cluster-a was last running on (still listing the address), then the current one.
        y = A[19]
        stale_gen, stale_eps = gen["cluster-a"], set(a_eps)
        ctl.kill("cluster-a", reason="outage")
        ctl.wait_dead("cluster-a")
        a_eps = a_eps - {y}
        bump("cluster-a", a_eps)
        t_released = time.time()
        sleep_until(t_released + ADDRESS_HOLD_DOWN_S + HOLD_DOWN_MARGIN_S)
        b_eps = b_eps | {y}
        bump("cluster-b", b_eps)
        res["handoffs"].append({"target": y, "from": "cluster-a", "to": "cluster-b",
                                "ts": time.time()})
        r1 = {"target": y, "stale_generation": stale_gen, "generation": gen["cluster-a"],
              "b_item_after_s": wait_item(ddb, "cluster-b", y, HANDOFF_ITEM_WAIT_S)}
        time.sleep(ARM_SETTLE_S)
        ctl.kill("cluster-b", reason="outage")
        ctl.wait_dead("cluster-b")
        write_binding(files["cluster-a"], stale_gen, stale_eps)
        ctl.spawn("cluster-a")
        t_respawn = time.time()
        sleep_until(t_respawn + STALE_SNAPSHOT_S)
        write_binding(files["cluster-a"], gen["cluster-a"], a_eps)
        t_from = time.time()
        e_r1 = a_eps | b_eps | ids(M, 12, 15, 18, 22)
        r1.update({"expected": sorted(e_r1),
                   **wait_converged(sampler, e_r1, t_from, CONVERGENCE_DEADLINE_S)})
        r1["end_ts"] = time.time()
        res["phases"]["R1"] = r1
        snapshot_ledger("R1")
        ctl.spawn("cluster-b")

        # R2: cluster-a is down again and misses a whole tenancy of one of its addresses: its pod
        # goes away, after the hold-down a cluster-b pod gets the address, that pod goes away too,
        # and after another hold-down a new cluster-a pod gets it. cluster-a then comes back.
        x = A[10]
        ctl.kill("cluster-a", reason="outage")
        ctl.wait_dead("cluster-a")
        a_eps = a_eps - {x}
        bump("cluster-a", a_eps)
        t_released = time.time()
        sleep_until(t_released + ADDRESS_HOLD_DOWN_S + HOLD_DOWN_MARGIN_S)
        b_eps = b_eps | {x}
        bump("cluster-b", b_eps)
        res["handoffs"].append({"target": x, "from": "cluster-a", "to": "cluster-b",
                                "ts": time.time()})
        r2 = {"target": x, "b_item_after_s": wait_item(ddb, "cluster-b", x, HANDOFF_ITEM_WAIT_S)}
        time.sleep(ARM_SETTLE_S)
        b_eps = b_eps - {x}
        bump("cluster-b", b_eps)
        t_released = time.time()
        sleep_until(t_released + ADDRESS_HOLD_DOWN_S + HOLD_DOWN_MARGIN_S)
        a_eps = a_eps | {x}
        bump("cluster-a", a_eps)
        res["handoffs"].append({"target": x, "from": "cluster-b", "to": "cluster-a",
                                "ts": time.time()})
        ctl.spawn("cluster-a")
        t_from = time.time()
        e_r2 = a_eps | b_eps | ids(M, 12, 15, 18, 22)
        r2.update({"expected": sorted(e_r2),
                   **wait_converged(sampler, e_r2, t_from, CONVERGENCE_DEADLINE_S)})
        res["phases"]["R2"] = r2
        snapshot_ledger("R2")

        # P7: cluster-b's binding is deleted while its controller is down, and after the hold-down
        # one of its addresses goes to a new cluster-a pod meanwhile. The restarted cluster-b is
        # killed right after its first removal is applied, then restarted again.
        ctl.kill("cluster-b", reason="outage")
        ctl.wait_dead("cluster-b")
        bump("cluster-b", set(), deleted=True)
        t_released = time.time()
        sleep_until(t_released + ADDRESS_HOLD_DOWN_S + HOLD_DOWN_MARGIN_S)
        a_eps = a_eps | {B[17]}
        bump("cluster-a", a_eps)
        res["handoffs"].append({"target": B[17], "from": "cluster-b", "to": "cluster-a",
                                "ts": time.time()})
        p7 = {"a_item_after_s": wait_item(ddb, "cluster-a", B[17], HANDOFF_ITEM_WAIT_S)}
        b_targets = set(B.values()) | {A[16], A[19]}
        rule = proxies["cluster-b"].arm(Rule(
            lambda e: e["removal"] and bool(b_targets & set(e["targets"])), "forward_then_kill"))
        sleep_until(time.time() + ARM_SETTLE_S)
        first = ctl.spawn("cluster-b")
        ctl.expect_exit("cluster-b")
        t_edit = time.time()
        fired = rule.fired.wait(FAULT_WAIT_S)
        proxies["cluster-b"].disarm()
        respawned = None
        if fired:
            ctl.wait_dead("cluster-b")
            sleep_until(rule.fire_ts + RESTART_DELAY_S)
            respawned = ctl.spawn("cluster-b")
            ctl.expect_exit("cluster-b")
        t_from = time.time() if fired else t_edit
        e7 = a_eps | ids(M, 12, 15, 18, 22)
        p7.update({"fired": fired, "fault_action": rule.fire_entry["action"] if rule.fire_entry else None,
                   "expected": sorted(e7), **wait_converged(sampler, e7, t_from, CONVERGENCE_DEADLINE_S)})
        proc = respawned or first
        exit_code = None
        if proc is not None:
            try:
                exit_code = proc.wait(timeout=max(0.5, t_from + EXIT_WAIT_S - time.time()))
            except subprocess.TimeoutExpired:
                exit_code = None
        p7["exit_code"] = exit_code
        p7["rows_b"] = query_owner(ddb, "cluster-b")
        res["phases"]["P7"] = p7
        snapshot_ledger("P7")
    finally:
        sampler.stop()
        ctl.shutdown()
        for proxy in proxies.values():
            proxy.stop()

    res["samples"] = sampler.snapshot()
    res["proxy"] = {c: proxies[c].snapshot() for c in CLUSTERS}
    res["events"] = ctl.events
    with open(os.path.join(LOG_DIR, "schedule.json"), "w") as fh:
        json.dump({k: v for k, v in res.items() if k not in ("samples", "proxy")}, fh, indent=1,
                  default=str)
    with open(os.path.join(LOG_DIR, "samples.jsonl"), "w") as fh:
        for ts, present, _ in res["samples"]:
            fh.write(json.dumps({"ts": ts, "present": sorted(present) if present is not None else None}) + "\n")
    return res
