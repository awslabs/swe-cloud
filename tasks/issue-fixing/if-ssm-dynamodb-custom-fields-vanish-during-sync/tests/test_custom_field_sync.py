"""Verifier for the account custom-field sync.

Drives the deployed CLI (`aft-sync --payload <file>`) against the local AWS
endpoint through a fault-injection proxy that can delay, hold and drop
requests, commits account-request record versions, runs a consumer that
polls the custom-field path and reconciles a pool of objects sized from one
field, and grades the resulting AWS state and the ordered log of forwarded
SSM mutations.

Graded groups, all independent (never fail-fast):
  * overlapping invocations where one request is stalled in flight,
  * a late invocation carrying an older record snapshot,
  * an invocation killed mid-work and retried,
  * a single invocation whose puts are slowed down.
"""

import json
import os
import socketserver
import subprocess
import threading
import time
import uuid
from pathlib import Path

from cdk_seed.cfn_deploy import deploy
from http.client import HTTPConnection

import boto3
from botocore.config import Config

FLOCI = "http://127.0.0.1:4566"
REGION = "us-east-1"

PREFIX = "/aft/account-request/custom-fields/"
ACCOUNT_REQUEST_TABLE = "aft-request"
SYNC_STATE_TABLE = "aft-sync-state"
BUCKET = "aft-member-resources"
SUBNET_PREFIX = "subnets/subnet-"
SIZING_FIELD = "subnet_count"

SEED_PUTS = 3                 # each seeded parameter is put this many times
READER_POLL_S = 0.025
PUT_DELAY_S = 0.25
HOLD_S = 21.0
TAKEOVER_DEADLINE_S = 20.0
RUN_DEADLINE_S = 30.0
QUIESCE_S = 3.0
RUN_KILL_GRACE_S = 1.0

USER_A = "sync-run-a"
USER_B = "sync-run-b"

V0 = {"subnet_count": "3", "ipam_pool": "pool-a", "legacy_flag": "on"}
V1 = {"subnet_count": "3"}
V1R = {"subnet_count": "3", "ipam_pool": "pool-a"}
V2 = {"subnet_count": "4", "ipam_pool": "pool-b"}
V3 = {"subnet_count": "4", "owner": "team-x"}

TRIAL_ROOT = "/tmp/verifier-trials"


# --------------------------------------------------------------------------
# AWS clients
# --------------------------------------------------------------------------

def client_for(service, endpoint=FLOCI, **config):
    return boto3.client(
        service,
        region_name=REGION,
        endpoint_url=endpoint,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(retries={"max_attempts": 5, "mode": "standard"}, **config),
    )


DDB = client_for("dynamodb")
SSM = client_for("ssm")
S3 = client_for("s3", s3={"addressing_style": "path"})


# --------------------------------------------------------------------------
# Fault-injection proxy
# --------------------------------------------------------------------------

class Rules:
    """Per-trial forwarding rules shared by every proxy port."""

    def __init__(self):
        self.put_delay_s = 0.0
        self.hold_port = None
        self.hold_s = 0.0
        self.hold_started = threading.Event()
        self.hold_started_at = None
        self._held_ports = set()
        self.drop_port = None
        self.drop_after = None
        self.dropped = threading.Event()
        self.mutations = []
        self.drops = []
        self.seen = {}
        self.seq = 0
        self.lock = threading.Lock()

    def next_seq(self):
        with self.lock:
            self.seq += 1
            return self.seq

    def seen_count(self, port):
        with self.lock:
            self.seen[port] = self.seen.get(port, 0) + 1
            return self.seen[port]

    def log_mutation(self, entry):
        with self.lock:
            self.mutations.append(entry)

    def snapshot_mutations(self):
        with self.lock:
            return list(self.mutations)


SSM_MUTATING_OPS = (
    "PutParameter",
    "DeleteParameter",
    "DeleteParameters",
    "LabelParameterVersion",
)


class ProxyHandler(socketserver.BaseRequestHandler):
    """Forward one HTTP request at a time, applying this trial's rules."""

    def _recv_request(self):
        conn = self.request
        conn.settimeout(40.0)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                return None
            data += chunk
        head, rest = data.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        method, path, _ = lines[0].split(" ", 2)
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        length = int(headers.get("content-length", "0") or 0)
        if not rest and length and headers.get("expect", "") == "100-continue":
            conn.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
        while len(rest) < length:
            chunk = conn.recv(65536)
            if not chunk:
                break
            rest += chunk
        return method, path, headers, rest[:length]

    def _reply(self, status_line, headers, body):
        out = [status_line]
        for k, v in headers:
            lk = k.lower()
            if lk in ("connection", "transfer-encoding", "content-length"):
                continue
            out.append(f"{k}: {v}")
        out.append(f"Content-Length: {len(body)}")
        out.append("Connection: close")
        out.append("")
        payload = ("\r\n".join(out) + "\r\n").encode("latin-1") + body
        self.request.sendall(payload)

    def _forward(self, method, path, headers, body):
        upstream = HTTPConnection("127.0.0.1", 4566, timeout=40)
        try:
            # The original Host header is preserved: requests are signed for it.
            upstream.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            if "host" in headers:
                upstream.putheader("Host", headers["host"])
            for k, v in headers.items():
                if k in ("host", "content-length", "connection"):
                    continue
                upstream.putheader(k, v)
            upstream.putheader("Content-Length", str(len(body)))
            upstream.endheaders(message_body=body if body else None)
            resp = upstream.getresponse()
            resp_body = resp.read()
            return resp.status, resp.reason, resp.getheaders(), resp_body
        finally:
            upstream.close()

    def handle(self):
        request = self._recv_request()
        if request is None:
            return
        method, path, headers, body = request
        rules = self.server.rules
        port = self.server.proxy_port
        target = headers.get("x-amz-target", "")
        op = target.split(".")[-1] if target else ""

        if target.startswith("AmazonSSM.") and op in SSM_MUTATING_OPS:
            count = rules.seen_count(port)
            if (
                rules.drop_port == port
                and rules.drop_after is not None
                and count >= rules.drop_after
            ):
                with rules.lock:
                    rules.drops.append({"port": port, "op": op, "count": count,
                                        "t": time.time()})
                rules.dropped.set()
                # never forwarded: the client sees the connection die
                self.request.close()
                return
            if rules.hold_port == port and port not in rules._held_ports:
                with rules.lock:
                    rules._held_ports.add(port)
                    rules.hold_started_at = time.time()
                rules.hold_started.set()
                time.sleep(rules.hold_s)
            if op == "PutParameter" and rules.put_delay_s:
                time.sleep(rules.put_delay_s)

        status, reason, resp_headers, resp_body = self._forward(
            method, path, headers, body
        )

        if target.startswith("AmazonSSM.") and op in SSM_MUTATING_OPS and status < 300:
            entry = {
                "seq": rules.next_seq(),
                "port": port,
                "op": op,
                "t": time.time(),
                "status": status,
            }
            try:
                parsed = json.loads(body.decode("utf-8")) if body else {}
            except ValueError:
                parsed = {}
            if op == "PutParameter":
                entry["names"] = [parsed.get("Name", "")]
                entry["values"] = {parsed.get("Name", ""): parsed.get("Value")}
            elif op == "DeleteParameters":
                entry["names"] = list(parsed.get("Names", []))
                entry["values"] = {}
            else:
                entry["names"] = [parsed.get("Name", "")]
                entry["values"] = {}
            rules.log_mutation(entry)

        self._reply(f"HTTP/1.1 {status} {reason}", resp_headers, resp_body)
        self.request.close()


class ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_proxies():
    """One proxy per role, each on whatever port the kernel hands out.

    The solve phase runs in this container before the verifier does and may
    leave its own listeners behind, so no port number is reserved: each proxy
    binds port 0 and the runs are pointed at the port it was given."""
    servers = {}
    for role in ("a", "b", "reader"):
        server = ProxyServer(("127.0.0.1", 0), ProxyHandler)
        server.proxy_port = server.server_address[1]
        server.rules = Rules()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers[role] = server
    return servers


PROXIES = start_proxies()
PORT_A = PROXIES["a"].proxy_port
PORT_B = PROXIES["b"].proxy_port
PORT_READER = PROXIES["reader"].proxy_port


# --------------------------------------------------------------------------
# Harness: record writer, seeder, consumer
# --------------------------------------------------------------------------

class Trial:
    def __init__(self, index, seed_fields, seed_version):
        self.index = index
        self.email = f"dev-{index:02d}-{uuid.uuid4().hex[:6]}@example.com"
        self.account_id = "".join(str(int(uuid.uuid4().hex[:8], 16) % 10) for _ in range(12))
        self.dir = os.path.join(TRIAL_ROOT, f"trial-{index:02d}-{uuid.uuid4().hex[:8]}")
        os.makedirs(self.dir, exist_ok=True)
        os.chmod(self.dir, 0o755)
        self.seed_fields = dict(seed_fields)
        self.seed_version = seed_version
        self.transitions = []          # (old_fields, new_fields, wall_time)
        self.latest = dict(seed_fields)
        self.runs = []                 # (tag, rc, elapsed, timed_out)
        self.killed_tags = set()
        self.reader = None
        self.g0 = {}
        self.rules = None

    def payload_path(self, version):
        return os.path.join(self.dir, f"payload-v{version}.json")


def wait_table_gone(name, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            DDB.describe_table(TableName=name)
        except DDB.exceptions.ResourceNotFoundException:
            return
        except Exception:
            return
        time.sleep(0.05)


def recreate_table(name, key_name, run_id):
    try:
        DDB.delete_table(TableName=name)
    except Exception:
        pass
    wait_table_gone(name)
    deploy(client_for("cloudformation"), "aft-table-%s-%s" % (name, run_id),
           Path(__file__).parent / "cdk_seed/seed.template.json",
           TableName=name, KeyName=key_name)
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        try:
            if DDB.describe_table(TableName=name)["Table"]["TableStatus"] == "ACTIVE":
                return
        except Exception:
            pass
        time.sleep(0.05)
    raise RuntimeError(f"table {name} did not become active")


def purge_parameters():
    paginator = SSM.get_paginator("get_parameters_by_path")
    names = []
    for page in paginator.paginate(Path=PREFIX, Recursive=True):
        names.extend(p["Name"] for p in page["Parameters"])
    for name in ("/aft-verifier-version-probe/field",):
        names.append(name)
    for start in range(0, len(names), 10):
        try:
            SSM.delete_parameters(Names=names[start : start + 10])
        except Exception:
            pass


def purge_bucket():
    try:
        deploy(client_for("cloudformation"), "aft-bucket-%s" % BUCKET,
               Path(__file__).parent / "cdk_seed/bucket.template.json", BucketName=BUCKET)
    except Exception:
        pass
    paginator = S3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=BUCKET):
        keys.extend(o["Key"] for o in page.get("Contents", []))
    for key in keys:
        try:
            S3.delete_object(Bucket=BUCKET, Key=key)
        except Exception:
            pass


class Reader(threading.Thread):
    """Consumer: polls the custom-field path and sizes an object pool from it."""

    def __init__(self, trial):
        super().__init__(daemon=True)
        self.trial = trial
        self.stop_event = threading.Event()
        self.observations = []          # (t, {name: value})
        self.actions = []               # (t, "put"|"delete", key, generation)
        self.view = {}                  # key -> generation believed present
        self.errors = []
        self.ssm = client_for("ssm", f"http://127.0.0.1:{PORT_READER}")
        self.s3 = client_for(
            "s3", f"http://127.0.0.1:{PORT_READER}", s3={"addressing_style": "path"}
        )

    def run(self):
        while not self.stop_event.is_set():
            started = time.time()
            try:
                paginator = self.ssm.get_paginator("get_parameters_by_path")
                values = {}
                for page in paginator.paginate(Path=PREFIX, Recursive=True):
                    for param in page["Parameters"]:
                        values[param["Name"]] = param["Value"]
                self.observations.append((started, values))
                desired = 0
                sizing = values.get(PREFIX + SIZING_FIELD)
                if sizing is not None:
                    try:
                        desired = int(sizing)
                    except ValueError:
                        desired = 0
                wanted = {f"{SUBNET_PREFIX}{i:02d}" for i in range(desired)}
                for key in sorted(wanted - set(self.view)):
                    generation = str(uuid.uuid4())
                    self.s3.put_object(
                        Bucket=BUCKET, Key=key, Metadata={"generation": generation}
                    )
                    self.actions.append((started, "put", key, generation))
                    self.view[key] = generation
                for key in sorted(set(self.view) - wanted):
                    self.s3.delete_object(Bucket=BUCKET, Key=key)
                    self.actions.append((started, "delete", key, None))
                    del self.view[key]
            except Exception as error:      # a failed poll is not an absence
                self.errors.append(f"{type(error).__name__}: {error}")
            remaining = READER_POLL_S - (time.time() - started)
            if remaining > 0:
                self.stop_event.wait(remaining)

    def stop(self):
        self.stop_event.set()
        self.join(timeout=10)


def seed_trial(index, seed_fields, seed_version):
    trial = Trial(index, seed_fields, seed_version)
    purge_parameters()
    recreate_table(ACCOUNT_REQUEST_TABLE, "id", os.path.basename(trial.dir))
    recreate_table(SYNC_STATE_TABLE, "pk", os.path.basename(trial.dir))
    purge_bucket()

    DDB.put_item(
        TableName=ACCOUNT_REQUEST_TABLE,
        Item={
            "id": {"S": trial.email},
            "custom_fields": {"S": json.dumps(seed_fields)},
            "record_version": {"N": str(seed_version)},
        },
    )
    for _ in range(SEED_PUTS):
        for key, value in seed_fields.items():
            SSM.put_parameter(
                Name=PREFIX + key, Value=value, Type="String", Overwrite=True
            )

    rules = Rules()
    for server in PROXIES.values():
        server.rules = rules
    trial.rules = rules

    reader = Reader(trial)
    reader.start()
    trial.reader = reader

    warmup = int(seed_fields[SIZING_FIELD])
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and len(reader.view) < warmup:
        time.sleep(0.02)
    for i in range(warmup):
        key = f"{SUBNET_PREFIX}{i:02d}"
        head = S3.head_object(Bucket=BUCKET, Key=key)
        trial.g0[key] = head["Metadata"].get("generation")
    return trial


def commit(trial, version, fields):
    previous = trial.latest
    DDB.update_item(
        TableName=ACCOUNT_REQUEST_TABLE,
        Key={"id": {"S": trial.email}},
        UpdateExpression="SET custom_fields = :cf, record_version = :n",
        ConditionExpression="record_version = :prev",
        ExpressionAttributeValues={
            ":cf": {"S": json.dumps(fields)},
            ":n": {"N": str(version)},
            ":prev": {"N": str(version - 1)},
        },
    )
    payload = {
        "customization_request_id": str(uuid.uuid4()),
        "account_info": {"account": {"id": trial.account_id}},
        "account_request": {
            "id": trial.email,
            "custom_fields": json.dumps(fields),
            "record_version": version,
        },
    }
    path = trial.payload_path(version)
    with open(path, "w") as f:
        json.dump(payload, f)
    os.chmod(path, 0o644)
    trial.transitions.append((previous, fields, time.time()))
    trial.latest = dict(fields)
    return path


class RunHandle:
    def __init__(self, tag, process, log, log_path, deadline_s):
        self.tag = tag
        self.process = process
        self.log = log
        self.log_path = log_path
        self.deadline_s = deadline_s
        self.started = time.monotonic()


class RunResult:
    def __init__(self, tag, rc, elapsed, timed_out, log_path):
        self.tag = tag
        self.rc = rc
        self.elapsed = elapsed
        self.timed_out = timed_out
        self.log_path = log_path

    @property
    def ok(self):
        return self.rc == 0 and not self.timed_out


def start_run(trial, tag, user, port, payload_version, deadline_s=RUN_DEADLINE_S):
    payload = trial.payload_path(payload_version)
    log_path = os.path.join(trial.dir, f"run-{tag}.log")
    env_pairs = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": f"/home/{user}",
        "TMPDIR": f"/home/{user}/tmp",
        "AWS_ENDPOINT_URL": f"http://127.0.0.1:{port}",
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "PYTHONPATH": "/app",
        "PYTHONUNBUFFERED": "1",
    }
    command = [
        "runuser", "-u", user, "--", "/usr/bin/env",
        *[f"{k}={v}" for k, v in env_pairs.items()],
        "aft-sync", "--payload", payload,
    ]
    log = open(log_path, "wb")
    process = subprocess.Popen(
        command,
        stdout=log,
        stderr=subprocess.STDOUT,
        cwd=trial.dir,
        start_new_session=True,
    )
    return RunHandle(tag, process, log, log_path, deadline_s)


def finish_run(trial, handle):
    process = handle.process
    timed_out = False
    remaining = handle.deadline_s - (time.monotonic() - handle.started)
    try:
        process.wait(timeout=max(remaining, 0) + RUN_KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, 9)
        except Exception:
            pass
        try:
            process.wait(timeout=5)
        except Exception:
            pass
    try:
        os.killpg(process.pid, 9)
    except Exception:
        pass
    handle.log.close()
    elapsed = time.monotonic() - handle.started
    result = RunResult(handle.tag, process.poll(), elapsed, timed_out, handle.log_path)
    trial.runs.append((handle.tag, result.rc, result.elapsed, result.timed_out))
    return result


def run_sync(trial, tag, user, port, payload_version):
    return finish_run(trial, start_run(trial, tag, user, port, payload_version))


# --------------------------------------------------------------------------
# Version probe (capability check, not a graded rule)
# --------------------------------------------------------------------------

_VERSIONS_TRACKED = None


def versions_tracked():
    global _VERSIONS_TRACKED
    if _VERSIONS_TRACKED is not None:
        return _VERSIONS_TRACKED
    name = "/aft-verifier-version-probe/field"
    tracked = False
    try:
        try:
            SSM.delete_parameters(Names=[name])
        except Exception:
            pass
        seen = []
        for i in range(3):
            response = SSM.put_parameter(
                Name=name, Value=f"v{i}", Type="String", Overwrite=True
            )
            seen.append(int(response.get("Version", 0)))
        page = SSM.get_parameters_by_path(
            Path="/aft-verifier-version-probe/", Recursive=True
        )
        has_version = bool(page.get("Parameters")) and "Version" in page["Parameters"][0]
        tracked = seen == [1, 2, 3] and has_version
    except Exception:
        tracked = False
    try:
        SSM.delete_parameters(Names=[name])
    except Exception:
        pass
    _VERSIONS_TRACKED = tracked
    return tracked


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------

def final_listing():
    paginator = SSM.get_paginator("get_parameters_by_path")
    listing = {}
    for page in paginator.paginate(Path=PREFIX, Recursive=True):
        for param in page["Parameters"]:
            listing[param["Name"]] = (param.get("Value"), param.get("Version"))
    return listing


def replay_states(seed_fields, mutations):
    state = {PREFIX + k: v for k, v in seed_fields.items()}
    states = [dict(state)]
    for entry in mutations:
        if entry["op"] == "PutParameter":
            for name in entry["names"]:
                state[name] = entry["values"].get(name)
        elif entry["op"] in ("DeleteParameter", "DeleteParameters"):
            for name in entry["names"]:
                state.pop(name, None)
        # anything else (labels, tags) does not change values
        states.append(dict(state))
    return states


def evaluate(trial, failures, case_label):
    mutations = trial.rules.snapshot_mutations()
    states = replay_states(trial.seed_fields, mutations)
    # only names under PREFIX are the consumer's contract
    states = [{n: v for n, v in s.items() if n.startswith(PREFIX)} for s in states]
    listing = final_listing()
    listing_values = {name: value for name, (value, _) in listing.items()}
    target = {PREFIX + k: v for k, v in trial.latest.items()}
    where = f"{case_label} trial {trial.index}"

    # (d) final state equals the current record exactly
    if listing_values != target:
        failures.append(
            f"{where} [final-state] parameters {listing_values} != record {target}"
        )

    # (a) replaying the forwarded mutations reproduces the final listing
    if states[-1] != listing_values:
        failures.append(
            f"{where} [replay] replayed mutations end at {states[-1]} but the "
            f"live listing is {listing_values}"
        )

    # (b) a field kept by both sides of a committed change is never absent
    for old_fields, new_fields, committed_at in trial.transitions:
        retained = {PREFIX + k for k in old_fields.keys() & new_fields.keys()}
        for index, entry in enumerate(mutations):
            if entry["t"] < committed_at:
                continue
            missing = retained - set(states[index + 1])
            if missing:
                failures.append(
                    f"{where} [kept-field] {sorted(missing)} absent after "
                    f"{entry['op']} on port {entry['port']} (seq {entry['seq']})"
                )

    # (c) nothing changes or disappears once the current record is applied
    convergence = None
    for index, state in enumerate(states):
        if state == target:
            convergence = index
            break
    if convergence is not None:
        for index in range(convergence + 1, len(states)):
            if states[index] != states[convergence]:
                entry = mutations[index - 1]
                failures.append(
                    f"{where} [settled] {entry['op']} on port {entry['port']} "
                    f"(seq {entry['seq']}) changed the parameters after the "
                    f"current record was applied"
                )

    # (e) damage: the consumer's object pool
    reader = trial.reader
    if len(reader.errors) > 50:
        failures.append(
            f"{where} [pool] consumer polling was unreliable "
            f"({len(reader.errors)} failed polls)"
        )
    for _, action, key, _gen in reader.actions:
        if action == "delete":
            failures.append(f"{where} [pool] consumer deleted {key}")
    desired_final = 0
    sizing = trial.latest.get(SIZING_FIELD)
    if sizing is not None:
        try:
            desired_final = int(sizing)
        except ValueError:
            desired_final = 0
    expected_keys = {f"{SUBNET_PREFIX}{i:02d}" for i in range(desired_final)}
    try:
        present = {}
        paginator = S3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=BUCKET, Prefix="subnets/"):
            for obj in page.get("Contents", []):
                present[obj["Key"]] = None
        for key in sorted(present):
            head = S3.head_object(Bucket=BUCKET, Key=key)
            present[key] = head["Metadata"].get("generation")
        if set(present) != expected_keys:
            failures.append(
                f"{where} [pool] object set {sorted(present)} != expected "
                f"{sorted(expected_keys)}"
            )
        for key, generation in trial.g0.items():
            if key not in present:
                failures.append(f"{where} [pool] {key} from the warm-up set is gone")
            elif present[key] != generation:
                failures.append(f"{where} [pool] {key} was recreated (generation)")
        created = {key: gen for _, action, key, gen in reader.actions if action == "put"}
        for key, generation in created.items():
            if key in present and present[key] != generation:
                failures.append(f"{where} [pool] {key} was recreated after creation")
    except Exception as error:
        failures.append(f"{where} [pool] could not read object pool: {error!r}")

    max_seen = None
    for _, values in reader.observations:
        sizing_value = values.get(PREFIX + SIZING_FIELD)
        if sizing_value is None:
            failures.append(f"{where} [pool] consumer observed {SIZING_FIELD} absent")
            continue
        try:
            observed = int(sizing_value)
        except ValueError:
            failures.append(
                f"{where} [pool] consumer observed {SIZING_FIELD}={sizing_value!r}"
            )
            continue
        if max_seen is not None and observed < max_seen:
            failures.append(
                f"{where} [pool] consumer observed {SIZING_FIELD} go "
                f"{max_seen} -> {observed}"
            )
        max_seen = observed if max_seen is None else max(max_seen, observed)
    if max_seen is not None and max_seen != desired_final:
        failures.append(
            f"{where} [pool] consumer last sized the pool from "
            f"{SIZING_FIELD}={max_seen}, record says {desired_final}"
        )

    # (f) exit codes and deadlines
    for tag, rc, elapsed, timed_out in trial.runs:
        if tag in trial.killed_tags:
            continue
        if timed_out:
            failures.append(f"{where} [exit] run {tag} did not finish in {RUN_DEADLINE_S}s")
        elif rc != 0:
            failures.append(f"{where} [exit] run {tag} exited {rc}")

    # (versions) a kept, seeded parameter was never deleted and recreated
    if versions_tracked():
        retained_everywhere = set(trial.seed_fields)
        for old_fields, new_fields, _ in trial.transitions:
            retained_everywhere &= old_fields.keys() & new_fields.keys()
        retained_everywhere &= set(trial.latest)
        for key in sorted(retained_everywhere):
            name = PREFIX + key
            if name in listing:
                version = listing[name][1]
                if version is not None and int(version) < SEED_PUTS:
                    failures.append(
                        f"{where} [versions] {name} at version {version}; it was "
                        f"deleted and recreated"
                    )
    return failures


def prefix_mutations(trial):
    return [
        entry for entry in trial.rules.snapshot_mutations()
        if any(name.startswith(PREFIX) for name in entry.get("names", []))
    ]


def quiesce(trial):
    # a held write is graded in its own trial, not leaked into the next one
    if trial.rules.hold_started_at is not None:
        wait = trial.rules.hold_started_at + trial.rules.hold_s + 1.0 - time.time()
        if wait > 0:
            time.sleep(wait)
    before = len(prefix_mutations(trial))
    time.sleep(QUIESCE_S)
    first = final_listing()
    time.sleep(1.0)
    second = final_listing()
    after = len(prefix_mutations(trial))
    failures = []
    if before != after:
        failures.append(
            f"trial {trial.index} [settled] mutations continued after the runs exited"
        )
    if first != second:
        failures.append(
            f"trial {trial.index} [settled] the listing changed after the runs exited"
        )
    return failures


# --------------------------------------------------------------------------
# Case schedules
# --------------------------------------------------------------------------

def case_overlap_inflight(index, failures):
    """Run A's first SSM mutation is held while a newer record is committed
    and run B starts; A's request is forwarded only afterwards."""
    trial = seed_trial(index, V0, 0)
    try:
        commit(trial, 1, V1)
        trial.rules.hold_port = PORT_A
        trial.rules.hold_s = HOLD_S

        handle_a = start_run(trial, "A", USER_A, PORT_A, 1)
        deadline = time.monotonic() + 25.0
        held = trial.rules.hold_started.is_set()
        while (
            not held
            and handle_a.process.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
            held = trial.rules.hold_started.is_set()

        if held:
            commit(trial, 2, V2)
            handle_b = start_run(trial, "B", USER_B, PORT_B, 2)
            finish_run(trial, handle_a)
            finish_run(trial, handle_b)
        else:
            # the run finished without touching the parameters; apply the newer
            # record with a fresh run and grade the same expectations
            finish_run(trial, handle_a)
            commit(trial, 2, V2)
            run_sync(trial, "B", USER_B, PORT_B, 2)
        failures += quiesce(trial)
        trial.reader.stop()
        evaluate(trial, failures, "overlap")
    finally:
        trial.reader.stop()


def case_late_stale_run(index, failures):
    """The newer record's run finishes first; an invocation carrying the older
    snapshot runs afterwards."""
    trial = seed_trial(index, V0, 0)
    try:
        commit(trial, 1, V1)
        commit(trial, 2, V2)
        run_sync(trial, "newer", USER_A, PORT_A, 2)
        run_sync(trial, "older", USER_B, PORT_B, 1)
        failures += quiesce(trial)
        trial.reader.stop()
        evaluate(trial, failures, "late")
    finally:
        trial.reader.stop()


def case_interrupted_retry(index, failures, drop_after):
    """A run is killed mid-work; the same payload is retried immediately."""
    trial = seed_trial(index, V2, 2)
    try:
        commit(trial, 3, V3)
        trial.rules.drop_port = PORT_A
        trial.rules.drop_after = drop_after

        handle = start_run(trial, "A", USER_A, PORT_A, 3)
        killed = trial.rules.dropped.wait(timeout=25.0)
        kill_time = time.monotonic()
        if killed:
            trial.killed_tags.add("A")
            try:
                os.killpg(handle.process.pid, 9)
            except Exception:
                pass
        finish_run(trial, handle)

        result = run_sync(trial, "retry", USER_B, PORT_B, 3)
        retry_elapsed = time.monotonic() - kill_time
        if killed and result.ok and retry_elapsed > TAKEOVER_DEADLINE_S:
            failures.append(
                f"interrupted trial {trial.index} [exit] retry needed "
                f"{retry_elapsed:.1f}s after the kill"
            )
        failures += quiesce(trial)
        trial.reader.stop()
        evaluate(trial, failures, "interrupted")
    finally:
        trial.reader.stop()


def case_single_run_window(index, failures):
    """One invocation for one record change, with every put slowed down."""
    trial = seed_trial(index, V0, 0)
    try:
        commit(trial, 1, V1R)
        trial.rules.put_delay_s = PUT_DELAY_S
        run_sync(trial, "A", USER_A, PORT_A, 1)
        failures += quiesce(trial)
        trial.reader.stop()
        evaluate(trial, failures, "single")
    finally:
        trial.reader.stop()


# --------------------------------------------------------------------------
# Tests (independent groups; the coupled schedules run first)
# --------------------------------------------------------------------------

def _run_case(fn, count, label):
    failures = []
    for i in range(count):
        try:
            fn(i, failures)
        except Exception as error:
            failures.append(f"{label} trial {i}: harness error {error!r}")
    seen = {}
    for line in failures:
        seen[line] = seen.get(line, 0) + 1
    summary = [
        line if count == 1 else f"{line} (x{count})" for line, count in seen.items()
    ]
    assert not summary, "\n".join(summary)


def test_overlap_inflight():
    _run_case(case_overlap_inflight, 3, "overlap")


def test_late_stale_run():
    _run_case(case_late_stale_run, 2, "late")


def test_interrupted_retry():
    def first_mutation(i, failures):
        case_interrupted_retry(i, failures, drop_after=1)

    def second_mutation(i, failures):
        case_interrupted_retry(i, failures, drop_after=2)

    _run_case(first_mutation, 1, "interrupted-1")
    _run_case(second_mutation, 1, "interrupted-2")


def test_single_run_window():
    _run_case(case_single_run_window, 2, "single")
