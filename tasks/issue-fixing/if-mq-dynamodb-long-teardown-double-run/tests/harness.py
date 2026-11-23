"""Verifier harness: one live run of the destroyer fleet against a fresh broker and fleet table.

The cloud API that vm-destroy talks to is served here, so every teardown attempt (begin, progress,
end) is recorded in this process's memory, where nothing the workers run can read or change it.
Faults are keyed to VMs: the first attempt of the CRASH VM has its worker's process group
SIGKILLed a few seconds in; the first attempt of the HUNG VM has its vm-destroy SIGSTOPped right
after it reports progress, and SIGKILLed a minute later. A sampler reads vm.destroy on the broker
once a second for the whole run.
"""

import base64
import json
import math
import os
import random
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote

import boto3
import pika
from botocore.config import Config

SOCKET_PATH = "/run/cloud-api.sock"
LOGS = Path("/logs/verifier")

N_WORKERS = 6
CONSUMER_TIMEOUT_MS = 60000
LONG_JOB_S = 180
MID_JOB_S = 30
SHORT_JOB_S = 5
N_SHORT = 8
PROGRESS_INTERVAL_S = 2.0
HANG_AT_PROGRESS = 2
CRASH_AFTER_S = 3
HUNG_BOUND_S = 45
CRASH_BOUND_S = 45
KILL_GRACE_S = 15
BARRIER_TIMEOUT_S = 90
RUN_DEADLINE_S = 300
DRAIN_S = 60
SAMPLE_INTERVAL_S = 1.0
STRANDED_GAP_S = 30
QUEUE = "vm.destroy"
QUEUE_ARGS = {"x-consumer-timeout": CONSUMER_TIMEOUT_MS}


class EnvironmentFailure(RuntimeError):
    """The emulator or the broker did not come up: not a verdict on the submission."""


def _log(message):
    line = f"{time.strftime('%H:%M:%S')} {message}"
    print(line, flush=True)
    with open(LOGS / "harness.log", "a") as handle:
        handle.write(line + "\n")


# ── the cloud API ──────────────────────────────────────────────────────────────────


class Attempt:
    def __init__(self, number, vm_id, pid, ppid):
        self.number = number
        self.vm_id = vm_id
        self.pid = pid
        self.ppid = ppid
        self.started = time.time()
        self.progress = []
        self.ended = None
        self.completed = False
        try:
            self.pgid = os.getpgid(pid)
            self.worker_pgid = os.getpgid(ppid)
            self.sid = os.getsid(pid)
        except OSError:
            self.pgid = self.worker_pgid = self.sid = None

    @property
    def last_progress(self):
        return self.progress[-1][1] if self.progress else self.started

    def as_dict(self):
        return {
            "number": self.number, "vm_id": self.vm_id, "pid": self.pid, "ppid": self.ppid,
            "pgid": self.pgid, "worker_pgid": self.worker_pgid, "sid": self.sid,
            "started": self.started, "last_progress": self.last_progress, "progress": len(self.progress),
            "progress_times": [ts for _, ts in self.progress],
            "ended": self.ended, "completed": self.completed,
        }


class Cloud:
    def __init__(self, specs, roles):
        self.specs = specs                 # vm_id -> seconds
        self.roles = roles                 # role -> vm_id for LONG, HUNG, CRASH
        self.lock = threading.Lock()
        self.attempts = {vm_id: [] for vm_id in specs}
        self.unknown = []
        self.crash_kill = None             # {"ts", "pgid"}
        self.hang = None                   # {"ts", "pid", "last_progress"}
        self.hung_killed_at = None
        self.server = None

    def start(self):
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(SOCKET_PATH)
        self.server.listen(128)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        attempt = None
        reader = conn.makefile("r")
        try:
            for line in reader:
                message = json.loads(line)
                if message["op"] == "begin":
                    attempt = self._begin(conn, message)
                    if attempt is None:
                        return
                elif message["op"] == "progress" and attempt is not None:
                    self._progress(attempt, int(message["n"]))
                elif message["op"] == "done" and attempt is not None:
                    with self.lock:
                        attempt.completed = True
                        attempt.ended = time.time()
                    _log(f"cloud: {attempt.vm_id} attempt {attempt.number} completed")
                    conn.sendall(b'{"ok": true}\n')
        except (OSError, ValueError):
            pass
        finally:
            if attempt is not None and attempt.ended is None:
                with self.lock:
                    attempt.ended = time.time()
                _log(f"cloud: {attempt.vm_id} attempt {attempt.number} ended without completing")
            conn.close()

    def _begin(self, conn, message):
        vm_id = message.get("vm_id")
        if vm_id not in self.specs:
            self.unknown.append(vm_id)
            conn.sendall(b'{"ok": false, "error": "no such VM"}\n')
            return None
        with self.lock:
            attempt = Attempt(len(self.attempts[vm_id]) + 1, vm_id, int(message["pid"]), int(message["ppid"]))
            self.attempts[vm_id].append(attempt)
        steps = max(1, math.ceil(self.specs[vm_id] / PROGRESS_INTERVAL_S))
        _log(f"cloud: {vm_id} attempt {attempt.number} began pid={attempt.pid} ppid={attempt.ppid} pgid={attempt.pgid} worker_pgid={attempt.worker_pgid}")
        if vm_id == self.roles["CRASH"] and attempt.number == 1:
            threading.Timer(CRASH_AFTER_S, self._crash, args=(attempt,)).start()
        conn.sendall((json.dumps({"ok": True, "steps": steps, "interval": PROGRESS_INTERVAL_S}) + "\n").encode())
        return attempt

    def _progress(self, attempt, n):
        now = time.time()
        with self.lock:
            attempt.progress.append((n, now))
        if attempt.vm_id == self.roles["HUNG"] and attempt.number == 1 and n == HANG_AT_PROGRESS and self.hang is None:
            try:
                os.kill(attempt.pid, signal.SIGSTOP)
            except OSError:
                pass
            self.hang = {"ts": now, "pid": attempt.pid, "last_progress": now}
            _log(f"fault: SIGSTOP vm-destroy pid {attempt.pid} ({attempt.vm_id}) after progress {n}")
            threading.Timer(HUNG_BOUND_S + KILL_GRACE_S, self._kill_hung, args=(attempt.pid,)).start()

    def _crash(self, attempt):
        pgid = attempt.worker_pgid
        self.crash_kill = {"ts": time.time(), "pgid": pgid}
        _log(f"fault: SIGKILL process group {pgid} (worker of {attempt.vm_id} attempt 1)")
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (OSError, TypeError):
            pass

    def _kill_hung(self, pid):
        self.hung_killed_at = time.time()
        _log(f"fault: SIGKILL stopped vm-destroy pid {pid}")
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    def snapshot(self):
        with self.lock:
            return {vm_id: [a.as_dict() for a in attempts] for vm_id, attempts in self.attempts.items()}


# ── the broker ─────────────────────────────────────────────────────────────────────


class Broker:
    def __init__(self, name):
        self.name = name
        self.user = "verifier"
        self.password = "".join(random.choice("abcdefghijkmnpqrstuvwxyz23456789") for _ in range(20))
        self.amqp_url = None
        self.host = None

    def create(self):
        mq = boto3.client("mq", config=Config(read_timeout=600, retries={"max_attempts": 1}))
        try:
            broker_id = mq.create_broker(
                BrokerName=self.name, EngineType="RABBITMQ", EngineVersion="3.13", HostInstanceType="mq.m5.large",
                DeploymentMode="SINGLE_INSTANCE", PubliclyAccessible=False, AutoMinorVersionUpgrade=True,
                Users=[{"Username": self.user, "Password": self.password}],
            )["BrokerId"]
            deadline = time.time() + 420
            while True:
                described = mq.describe_broker(BrokerId=broker_id)
                if described["BrokerState"] == "RUNNING":
                    break
                if time.time() > deadline:
                    raise EnvironmentFailure(f"broker {self.name} still {described['BrokerState']}")
                time.sleep(2)
        except EnvironmentFailure:
            raise
        except Exception as error:
            raise EnvironmentFailure(f"cannot create broker: {error!r}") from error
        endpoint = described["BrokerInstances"][0]["Endpoints"][0]
        self.host = endpoint.split("://", 1)[1].rsplit(":", 1)[0]
        self.amqp_url = endpoint.replace("amqp://", f"amqp://{self.user}:{quote(self.password)}@") + "/%2F"
        deadline = time.time() + 90
        while True:
            try:
                self.connect().close()
                break
            except pika.exceptions.AMQPError as error:
                if time.time() > deadline:
                    raise EnvironmentFailure(f"broker {self.name} not reachable: {error!r}") from error
                time.sleep(1)
        _log(f"broker {self.name} at {endpoint}")

    def connect(self):
        params = pika.URLParameters(self.amqp_url)
        params.heartbeat = 30
        params.blocked_connection_timeout = 30
        return pika.BlockingConnection(params)

    def management(self, path):
        request = urllib.request.Request(f"http://{self.host}:15672/api/{path}")
        token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)


def passive_counts(channel):
    ok = channel.queue_declare(QUEUE, passive=True)
    return ok.method.message_count, ok.method.consumer_count


def redeclare(broker):
    """Declare vm.destroy exactly as the platform does; a changed queue fails with PRECONDITION_FAILED."""
    try:
        connection = broker.connect()
        try:
            connection.channel().queue_declare(QUEUE, durable=True, auto_delete=False, arguments=QUEUE_ARGS)
        finally:
            if connection.is_open:
                connection.close()
        return None
    except pika.exceptions.AMQPError as error:
        return repr(error)


class Sampler(threading.Thread):
    def __init__(self, broker):
        super().__init__(daemon=True)
        self.broker = broker
        self.samples = []
        self.errors = []
        self.stop = threading.Event()

    def run(self):
        connection = channel = None
        while not self.stop.is_set():
            try:
                if connection is None or not connection.is_open:
                    connection = self.broker.connect()
                    channel = connection.channel()
                if not channel.is_open:
                    channel = connection.channel()
                messages, consumers = passive_counts(channel)
                self.samples.append((time.time(), messages, consumers))
                connection.process_data_events(0)
            except pika.exceptions.AMQPError as error:
                self.errors.append((time.time(), repr(error)))
                connection = None
            self.stop.wait(SAMPLE_INTERVAL_S)
        if connection is not None and connection.is_open:
            connection.close()


# ── workers ────────────────────────────────────────────────────────────────────────


def _cmdline(pid):
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return ""


def clear_leftovers():
    """Stop processes left from development runs, so only this run's workers touch this run's VMs."""
    me = os.getpid()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        cmdline = _cmdline(entry.name)
        if any(marker in cmdline for marker in ("destroyer.worker", "destroyer/worker", "sandbox-cloud", "vm-destroy")):
            try:
                os.kill(int(entry.name), signal.SIGKILL)
            except OSError:
                pass


def worker_consumers(broker, workers):
    """{worker id: number of consumers on vm.destroy held by that worker's session}."""
    details = broker.management(f"queues/%2F/{quote(QUEUE, safe='')}").get("consumer_details", [])
    ports = {}
    for detail in details:
        port = (detail.get("channel_details") or {}).get("peer_port")
        if port:
            ports[int(port)] = ports.get(int(port), 0) + 1
    owner = {}
    out = subprocess.run(["ss", "-tnpH", "state", "established"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        fields = line.split()
        if len(fields) < 4 or not fields[3].endswith(":5672"):
            continue
        local_port = int(fields[2].rsplit(":", 1)[1])
        for pid in re.findall(r"pid=(\d+)", line):
            try:
                owner[local_port] = os.getsid(int(pid))
            except OSError:
                pass
    by_session = {}
    for port, count in ports.items():
        sid = owner.get(port)
        if sid is not None:
            by_session[sid] = by_session.get(sid, 0) + count
    return {wid: by_session.get(proc.pid, 0) for wid, proc in workers.items()}, len(details)


# ── the run ────────────────────────────────────────────────────────────────────────


def run_scenario():
    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / "workers").mkdir(exist_ok=True)
    rng = random.Random()
    suffix = "".join(rng.choice("0123456789abcdef") for _ in range(8))
    result = {"suffix": suffix, "problems": []}

    clear_leftovers()
    shutil.copy("/tests/vm-destroy", "/usr/local/bin/vm-destroy")
    os.chmod("/usr/local/bin/vm-destroy", 0o755)

    vm_ids = [f"vm-{rng.getrandbits(32):08x}" for _ in range(3 + N_SHORT)]
    roles = {"LONG": vm_ids[0], "HUNG": vm_ids[1], "CRASH": vm_ids[2]}
    fillers = vm_ids[3:]
    specs = {roles["LONG"]: LONG_JOB_S, roles["HUNG"]: MID_JOB_S, roles["CRASH"]: MID_JOB_S}
    specs.update({vm_id: SHORT_JOB_S for vm_id in fillers})
    head = [roles["LONG"], roles["HUNG"], roles["CRASH"]]
    rng.shuffle(head)
    rng.shuffle(fillers)
    publish_order = head + fillers
    result.update(roles=roles, fillers=fillers, publish_order=publish_order)
    _log(f"roles {roles}; publish order {publish_order}")

    sqs = boto3.client("sqs")
    try:
        queues_before = set(sqs.list_queues().get("QueueUrls", []))
    except Exception as error:
        raise EnvironmentFailure(f"SQS not answering: {error!r}") from error

    table = f"vm-fleet-{suffix}"
    ddb = boto3.client("dynamodb")
    try:
        ddb.create_table(
            TableName=table, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "vm_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "vm_id", "AttributeType": "S"}],
        )
        ddb.get_waiter("table_exists").wait(TableName=table)
        for vm_id in vm_ids:
            ddb.put_item(TableName=table, Item={"vm_id": {"S": vm_id}, "state": {"S": "active"}, "launched_at": {"N": str(int(time.time() * 1000) - 3600_000)}})
    except Exception as error:
        raise EnvironmentFailure(f"DynamoDB not answering: {error!r}") from error

    broker = Broker(f"verify-{suffix}")
    broker.create()
    connection = broker.connect()
    connection.channel().queue_declare(QUEUE, durable=True, auto_delete=False, arguments=QUEUE_ARGS)
    connection.close()

    cloud = Cloud(specs, roles)
    cloud.start()

    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST")}
    env.update(AMQP_URL=broker.amqp_url, WORK_QUEUE=QUEUE, FLEET_TABLE=table, PYTHONPATH="/app",
               PATH="/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin", PYTHONUNBUFFERED="1")
    workers, handles = {}, []
    for k in range(1, N_WORKERS + 1):
        wid = f"W{k}"
        handle = open(LOGS / "workers" / f"{wid}.log", "w")
        handles.append(handle)
        workers[wid] = subprocess.Popen(
            ["python3", "-m", "destroyer.worker", "--worker-id", wid],
            cwd=tempfile.mkdtemp(prefix=f"worker-{wid}-"), env=env, stdout=handle, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    result["worker_pids"] = {wid: proc.pid for wid, proc in workers.items()}
    _log(f"workers {result['worker_pids']}")

    sampler = None
    try:
        monitor = broker.connect()
        monitor_channel = monitor.channel()
        deadline = time.time() + BARRIER_TIMEOUT_S
        consumers = 0
        while time.time() < deadline:
            _, consumers = passive_counts(monitor_channel)
            if consumers >= N_WORKERS:
                break
            monitor.sleep(0.5)
        result["barrier_consumers"] = consumers
        if consumers < N_WORKERS:
            result["problems"].append(f"only {consumers} of {N_WORKERS} workers subscribed to {QUEUE} within {BARRIER_TIMEOUT_S} s of start")
            _log(result["problems"][-1])
            result["ran"] = False
            return result
        result["ran"] = True

        sampler = Sampler(broker)
        sampler.start()
        result["redeclare_at_publish"] = redeclare(broker)
        publish_channel = monitor.channel()
        publish_channel.confirm_delivery()
        t2 = time.time()
        for vm_id in publish_order:
            publish_channel.basic_publish(
                "", QUEUE, json.dumps({"vm_id": vm_id}).encode(),
                pika.BasicProperties(delivery_mode=2, content_type="application/json"), mandatory=True,
            )
        result["t2"] = t2
        _log("published")

        def settled():
            snapshot = cloud.snapshot()
            return all(any(a["completed"] for a in attempts) for attempts in snapshot.values())

        while time.time() < t2 + RUN_DEADLINE_S and not settled():
            monitor.sleep(1)
        snapshot = cloud.snapshot()
        completions = [a["ended"] for attempts in snapshot.values() for a in attempts if a["completed"]]
        last = max(completions + [cloud.hang["ts"] + HUNG_BOUND_S + KILL_GRACE_S if cloud.hang else t2])
        while time.time() < max(last, t2) + DRAIN_S:
            monitor.sleep(1)

        # drain: the queue is empty and every worker the verifier did not kill is subscribed again
        killed = {wid for wid, proc in workers.items() if cloud.crash_kill and proc.pid == cloud.crash_kill["pgid"]}
        expected = {wid for wid in workers if wid not in killed}
        drain = {}
        for _ in range(20):
            try:
                messages, consumers = passive_counts(monitor_channel)
            except pika.exceptions.AMQPError as error:
                drain["queue_error"] = repr(error)
                monitor_channel = monitor.channel()
                messages, consumers = None, 0
            alive = {wid for wid in expected if workers[wid].poll() is None}
            try:
                per_worker, total = worker_consumers(broker, {wid: workers[wid] for wid in expected})
            except Exception as error:
                per_worker, total = None, None
                drain["management_error"] = repr(error)
            drain.update(messages=messages, consumers=consumers, alive=sorted(alive), expected=sorted(expected),
                         per_worker=per_worker, consumer_details=total, killed=sorted(killed))
            subscribed = per_worker is not None and all(per_worker.get(wid, 0) > 0 for wid in expected)
            if per_worker is None:
                subscribed = consumers >= len(expected)
            if messages == 0 and alive == expected and subscribed:
                break
            monitor.sleep(1)
        result["drain"] = drain
        result["redeclare_at_end"] = redeclare(broker)
        monitor.close()
    finally:
        if sampler is not None:
            sampler.stop.set()
            sampler.join(10)
            result["samples"] = sampler.samples
            result["sampler_errors"] = sampler.errors
        for proc in workers.values():
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except OSError:
                    pass
        time.sleep(5)
        for proc in workers.values():
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()
        for handle in handles:
            handle.close()
        result["attempts"] = cloud.snapshot()
        result["unknown_vms"] = cloud.unknown
        result["crash_kill"] = cloud.crash_kill
        result["hang"] = cloud.hang
        try:
            items = ddb.scan(TableName=table, ConsistentRead=True)["Items"]
            result["fleet"] = {item["vm_id"]["S"]: {k: next(iter(v.values())) for k, v in item.items()} for item in items}
        except Exception as error:
            result["fleet_error"] = repr(error)
        try:
            result["new_sqs_queues"] = sorted(set(sqs.list_queues().get("QueueUrls", [])) - queues_before)
        except Exception as error:
            result["sqs_error"] = repr(error)
        with open(LOGS / "scenario.json", "w") as handle:
            json.dump(result, handle, indent=1, default=str)
    return result
