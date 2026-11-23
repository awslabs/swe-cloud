"""Verifier harness: a Lambda-style event source mapping on a real SQS queue, plus the fleet API,
code-host API and an SSM forwarding proxy, all owned by the verifier and run inside this process.

The scale-up function under test is only ever run as a subprocess through its runtime adapter, in a
fresh working directory per invocation; nothing here imports solver code.
"""

from __future__ import annotations

import http.client
import json
import os
import random
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import boto3
from botocore.config import Config
from cdk_seed.cfn_deploy import deploy

APP = "/app"
LOG_DIR = Path(os.environ.get("VERIFIER_LOG_DIR", "/logs/verifier"))
ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "http://engines:4566")

HANDLER = "control_plane.lambda_handler.scale_up_handler"
HANDLER_TIMEOUT_S = 8.0
VISIBILITY_TIMEOUT_S = 15
MAX_RECEIVE_COUNT = 3
BATCH_SIZE = 10
BATCH_WINDOW_S = 2.0
POST_RESPONSE_READ_DELAY_S = 1.0
RUNNERS_MAXIMUM_COUNT = 4
DESCRIBE_LAG_S = 7.0  # README: a new instance can take up to 8 s to show up in describe-runners
JOB_RUN_S = 8.0  # a configured ephemeral runner runs its job this long, then its instance goes away
QUIESCENCE_STABLE_READS = 2
SCENARIO_DEADLINE_S = 150.0

SCALE_ERRORS = [
    "UnfulfillableCapacity",
    "MaxSpotInstanceCountExceeded",
    "TargetCapacityLimitExceededException",
    "RequestLimitExceeded",
    "ResourceLimitExceeded",
    "MaxSpotFleetRequestCountExceeded",
    "InsufficientInstanceCapacity",
    "InsufficientCapacityOnHost",
]

UNAVAILABLE = (503, {"__type": "Unavailable", "message": "The service is unavailable. Please try again shortly."})
REQUEST_LIMIT = (503, {"__type": "RequestLimitExceeded", "message": "Request limit exceeded."})
INSTANCE_ID = re.compile(r"i-[0-9a-f]{17}")

_AWS_CONFIG = Config(retries={"max_attempts": 4, "mode": "standard"}, connect_timeout=5, read_timeout=20)


def aws(service: str):
    return boto3.client(service, endpoint_url=ENDPOINT, config=_AWS_CONFIG)


def _job_key(value: Any) -> Any:
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


# ── a tiny JSON HTTP server ─────────────────────────────────────────────────────────


class _Server:
    def __init__(self, handler_cls: type[BaseHTTPRequestHandler]) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _json_handler(route: Callable[[str, str, dict], tuple[int, dict]]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _do(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                status, payload = 400, {"__type": "MalformedRequest", "message": "body is not a JSON object"}
            else:
                try:
                    status, payload = route(method, self.path, body)
                except Exception as error:  # a harness bug must not look like a fault
                    status, payload = 500, {"__type": "HarnessError", "message": repr(error)}
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:  # noqa: N802
            self._do("POST")

        def do_GET(self) -> None:  # noqa: N802
            self._do("GET")

        def log_message(self, *args: Any) -> None:
            pass

    return Handler


# ── scenario state shared by the stubs ──────────────────────────────────────────────


@dataclass
class Instance:
    instance_id: str
    environment: str
    owner: str
    job_id: Any
    state: str
    launch_seq: int
    invocation: int
    invocation_seq: int
    seeded: bool = False
    completed: bool = False  # ran its job and was terminated as an ephemeral runner does
    launched_at: float = 0.0

    def launch_time(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.launched_at))


class Fleet:
    """The EC2 stand-in. Instances live here; only this object changes their state."""

    def __init__(self, run: "Run") -> None:
        self.run = run
        self.lock = threading.RLock()
        self.instances: dict[str, Instance] = {}
        self.launch_seq = 0
        self.per_invocation_seq: Counter[int] = Counter()
        self.peak: Counter[str] = Counter()
        self.peak_at: dict[str, list[str]] = {}
        self.calls: list[dict] = []
        self.create_fleet_calls: Counter[int] = Counter()
        self.server = _Server(_json_handler(self.route))

    def running(self, owner: str | None = None) -> list[Instance]:
        with self.lock:
            return [
                i
                for i in self.instances.values()
                if i.state == "running" and i.environment == self.run.environment and (owner is None or i.owner == owner)
            ]

    def visible(self, owner: str) -> list[Instance]:
        """What describe-runners lists: DescribeInstances is eventually consistent, so an instance
        shows up only DESCRIBE_LAG_S after its launch; terminated ones drop out at once."""
        now = time.time()
        return [i for i in self.running(owner) if now >= i.launched_at + DESCRIBE_LAG_S]

    def launch(self, owner: str, job_id: Any, seeded: bool = False) -> Instance:
        with self.lock:
            self.launch_seq += 1
            invocation = self.run.invocation
            if not seeded:
                self.per_invocation_seq[invocation] += 1
            instance = Instance(
                instance_id="i-" + secrets.token_hex(9)[:17],
                environment=self.run.environment,
                owner=owner,
                job_id=job_id,
                state="running",
                launch_seq=self.launch_seq,
                invocation=invocation,
                invocation_seq=0 if seeded else self.per_invocation_seq[invocation],
                seeded=seeded,
                launched_at=time.time() - (3600 if seeded else 0),
            )
            self.instances[instance.instance_id] = instance
            running = self.running(owner)
            if len(running) > self.peak[owner]:
                self.peak[owner] = len(running)
                self.peak_at[owner] = [f"{i.instance_id} (job {i.job_id}, {'busy before the run' if i.seeded else f'launched in invocation {i.invocation}'})" for i in running]
            return instance

    def terminate_now(self, instance_id: str) -> None:
        with self.lock:
            self.instances[instance_id].state = "terminated"

    def route(self, method: str, path: str, body: dict) -> tuple[int, dict]:
        with self.lock:
            invocation = self.run.invocation
            self.calls.append({"invocation": invocation, "path": path, "body": body, "t": time.time()})
            if method != "POST" or path not in ("/describe-runners", "/create-fleet", "/terminate"):
                return 404, {"__type": "UnknownOperation", "message": path}
            if body.get("environment", self.run.environment) != self.run.environment and path != "/terminate":
                return 200, {"instances": [], "errors": []} if path == "/create-fleet" else {"instances": []}
            fault = self.run.scenario.fleet_fault(self.run, path, body)
            if fault is not None:
                self.calls[-1]["fault"] = fault[1]["__type"]
                return fault
            if path == "/describe-runners":
                owner = body.get("owner")
                return 200, {
                    "instances": [
                        {"instance_id": i.instance_id, "owner": i.owner, "job_id": i.job_id, "state": i.state, "launch_time": i.launch_time()}
                        for i in self.visible(owner)
                    ]
                }
            if path == "/terminate":
                instance = self.instances.get(str(body.get("instance_id")))
                if instance is None or instance.environment != self.run.environment:
                    return 400, {"__type": "InvalidInstanceID.NotFound", "message": f"The instance ID '{body.get('instance_id')}' does not exist"}
                instance.state = "terminated"
                return 200, {}
            # create-fleet
            owner, job_ids = body.get("owner"), body.get("job_ids")
            if not owner or not isinstance(job_ids, list):
                return 400, {"__type": "InvalidParameterValue", "message": "owner and job_ids are required"}
            self.create_fleet_calls[invocation] += 1
            unfulfilled, errors = self.run.scenario.fleet_shortfall(self.run, job_ids)
            launched = []
            for index, job_id in enumerate(job_ids):
                if index in unfulfilled:
                    continue
                instance = self.launch(owner, _job_key(job_id))
                launched.append({"instance_id": instance.instance_id, "job_id": job_id})
            self.run.scenario.after_create_fleet(self.run)
            return 200, {"instances": launched, "errors": errors}


class GitHub:
    def __init__(self, run: "Run") -> None:
        self.run = run
        self.lock = threading.Lock()
        self.issued: dict[str, set[str]] = defaultdict(set)  # instance id -> configs issued for it
        self.ids = iter(range(5000, 10**6))
        self.server = _Server(_json_handler(self.route))

    def issue(self, org: str, name: str, labels: list) -> dict:
        with self.lock:
            runner = {"id": next(self.ids), "name": name, "labels": labels, "os": "linux", "status": "offline"}
            encoded = secrets.token_urlsafe(24)
            match = INSTANCE_ID.search(name)
            if match:
                self.issued[match.group(0)].add(encoded)
            return {"runner": runner, "encoded_jit_config": encoded}

    def route(self, method: str, path: str, body: dict) -> tuple[int, dict]:
        match = re.match(r"^/orgs/([^/]+)/actions/runners/generate-jitconfig$", path)
        if method != "POST" or not match:
            return 404, {"message": "Not Found"}
        name = str(body.get("name") or "")
        if not name:
            return 422, {"message": "Invalid request. name is required"}
        fault = self.run.scenario.github_fault(self.run, name)
        if fault is not None:
            return fault
        return 201, self.issue(match.group(1), name, body.get("labels") or [])


class SsmProxy:
    """Forwards every request to Floci's SSM; PutParameter may be answered with a throttle."""

    def __init__(self, run: "Run") -> None:
        self.run = run
        self.lock = threading.Lock()
        self.forwarded_puts: dict[str, str] = {}
        self.put_at: dict[str, float] = {}  # first time each name was written
        self.refused_puts: list[str] = []
        upstream = urllib.parse.urlparse(ENDPOINT)
        self.upstream = (upstream.hostname, upstream.port or 80)
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                target = self.headers.get("X-Amz-Target", "")
                name = value = None
                if target.endswith(".PutParameter"):
                    try:
                        request = json.loads(raw)
                        name, value = request.get("Name"), request.get("Value")
                    except ValueError:
                        pass
                    if name and proxy.run.scenario.ssm_put_fault(proxy.run, name):
                        with proxy.lock:
                            proxy.refused_puts.append(name)
                        return self._reply(400, {"Content-Type": "application/x-amz-json-1.1"},
                                           json.dumps({"__type": "ThrottlingException", "message": "Rate exceeded"}).encode())
                headers = {k: v for k, v in self.headers.items() if k.lower() not in ("connection", "keep-alive", "transfer-encoding", "proxy-connection")}
                try:
                    connection = http.client.HTTPConnection(*proxy.upstream, timeout=15)
                    connection.request("POST", self.path, body=raw, headers=headers)
                    response = connection.getresponse()
                    data = response.read()
                    status = response.status
                    reply_headers = {k: v for k, v in response.getheaders() if k.lower() not in ("connection", "keep-alive", "transfer-encoding", "content-length")}
                    connection.close()
                except OSError as error:
                    return self._reply(502, {"Content-Type": "text/plain"}, str(error).encode())
                if name and status == 200:
                    with proxy.lock:
                        proxy.forwarded_puts[name] = value
                        proxy.put_at.setdefault(name, time.time())
                self._reply(status, reply_headers, data)

            def _reply(self, status: int, headers: dict, data: bytes) -> None:
                self.send_response(status)
                for key, val in headers.items():
                    self.send_header(key, val)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = _Server(Handler)


class SqsProxy:
    """The function's SQS endpoint: forwards to Floci, and refuses what its IAM role does not grant.

    deploy/scale-up.json gives the role receive/delete/get-attributes on the build queue and no
    sqs:SendMessage, so a send is answered the way SQS answers an unauthorised call.
    """

    DENIED = ("SendMessage", "SendMessageBatch")

    def __init__(self, run: "Run") -> None:
        self.run = run
        self.lock = threading.Lock()
        self.refused: list[str] = []
        upstream = urllib.parse.urlparse(ENDPOINT)
        self.upstream = (upstream.hostname, upstream.port or 80)
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _do(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                target = self.headers.get("X-Amz-Target", "")
                if target:
                    action = target.rsplit(".", 1)[-1]
                else:
                    query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    form = urllib.parse.parse_qs(raw.decode("utf-8", "replace")) if raw else {}
                    action = (form.get("Action") or query.get("Action") or [""])[0]
                if action in SqsProxy.DENIED:
                    with proxy.lock:
                        proxy.refused.append(action)
                    message = (f"User: arn:aws:sts::000000000000:assumed-role/ghr-scale-up-lambda/ghr-scale-up "
                               f"is not authorized to perform: sqs:{action} on resource: {proxy.run.queue_arn} "
                               "because no identity-based policy allows the sqs:SendMessage action")
                    if target:
                        return self._reply(400, {"Content-Type": "application/x-amz-json-1.0", "x-amzn-query-error": "AccessDenied;Sender"},
                                           json.dumps({"__type": "com.amazonaws.sqs#AccessDenied", "message": message}).encode())
                    xml = ("<ErrorResponse><Error><Type>Sender</Type><Code>AccessDenied</Code>"
                           f"<Message>{message}</Message></Error><RequestId>00000000-0000-0000-0000-000000000000</RequestId></ErrorResponse>")
                    return self._reply(403, {"Content-Type": "text/xml"}, xml.encode())
                headers = {k: v for k, v in self.headers.items() if k.lower() not in ("connection", "keep-alive", "transfer-encoding", "proxy-connection")}
                try:
                    connection = http.client.HTTPConnection(*proxy.upstream, timeout=25)
                    connection.request(method, self.path, body=raw if method == "POST" else None, headers=headers)
                    response = connection.getresponse()
                    data = response.read()
                    status = response.status
                    reply_headers = {k: v for k, v in response.getheaders() if k.lower() not in ("connection", "keep-alive", "transfer-encoding", "content-length")}
                    connection.close()
                except OSError as error:
                    return self._reply(502, {"Content-Type": "text/plain"}, str(error).encode())
                self._reply(status, reply_headers, data)

            def do_POST(self) -> None:  # noqa: N802
                self._do("POST")

            def do_GET(self) -> None:  # noqa: N802
                self._do("GET")

            def _reply(self, status: int, headers: dict, data: bytes) -> None:
                self.send_response(status)
                for key, val in headers.items():
                    self.send_header(key, val)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = _Server(Handler)


# ── scenarios ───────────────────────────────────────────────────────────────────────


@dataclass
class Job:
    job_id: int
    owner: str
    owner_type: str = "Organization"

    @property
    def ignored(self) -> bool:
        return self.owner_type != "Organization"

    def body(self) -> dict:
        return {
            "id": self.job_id,
            "eventType": "workflow_job",
            "repositoryName": "app",
            "repositoryOwner": self.owner,
            "installationId": 1,
            "repoOwnerType": self.owner_type,
            "labels": ["self-hosted", "linux", "x64"],
        }


class Scenario:
    """Fault schedule hooks; the defaults inject nothing."""

    name = "base"

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.jobs: list[Job] = []
        self.later_jobs: list[Job] = []  # part of self.jobs, queued once invocation 1 has returned
        self.seed_runners: dict[str, int] = {}

    def fleet_fault(self, run: "Run", path: str, body: dict) -> tuple[int, dict] | None:
        return None

    def fleet_shortfall(self, run: "Run", job_ids: list) -> tuple[set[int], list[dict]]:
        return set(), []

    def after_create_fleet(self, run: "Run") -> None:
        pass

    def github_fault(self, run: "Run", name: str) -> tuple[int, dict] | None:
        return None

    def ssm_put_fault(self, run: "Run", name: str) -> bool:
        return False

    def after_invocation(self, run: "Run", n: int) -> None:
        pass

    def _ids(self, count: int) -> list[int]:
        return self.rng.sample(range(10_000, 99_999), count)


class Brownout(Scenario):
    """Two owners; once the first create-fleet of invocation 1 has answered, the fleet API answers
    503 Unavailable to everything for the rest of that invocation, and SSM throttles every
    PutParameter for the second instance launched in it."""

    name = "brownout"

    def __init__(self, rng: random.Random) -> None:
        super().__init__(rng)
        a, b = self._ids(3), self._ids(2)
        self.jobs = [Job(j, "octo-web") for j in a] + [Job(j, "octo-data") for j in b]
        self.browning_out = False

    def after_create_fleet(self, run: "Run") -> None:
        if run.invocation == 1:
            self.browning_out = True

    def fleet_fault(self, run, path, body):
        if run.invocation == 1 and self.browning_out:
            return UNAVAILABLE
        return None

    def ssm_put_fault(self, run, name):
        if run.invocation != 1:
            return False
        second = [i for i in run.fleet.instances.values() if i.invocation == 1 and i.invocation_seq == 2]
        return bool(second) and name == f"{run.token_path}/{second[0].instance_id}"


class CapCheck(Scenario):
    """One owner is already at 3 of 4 runners (busy for the whole run) and its cap check answers 503
    for all of invocation 1; a User-owned repository's event rides in the batch."""

    name = "cap_check"

    def __init__(self, rng: random.Random) -> None:
        super().__init__(rng)
        a, b = self._ids(2), self._ids(2)
        self.busy_owner = "acme-labs"
        self.jobs = [Job(j, "acme") for j in a] + [Job(j, self.busy_owner) for j in b] + [Job(self._ids(1)[0], "jdoe", "User")]
        self.seed_runners = {self.busy_owner: 3}

    def fleet_fault(self, run, path, body):
        if run.invocation == 1 and path == "/describe-runners" and body.get("owner") == self.busy_owner:
            return UNAVAILABLE
        return None


class PartialFleet(Scenario):
    """The first create-fleet of invocation 1 leaves its second requested job unfulfilled
    (InsufficientInstanceCapacity); the code host answers 503 for the third instance launched in
    invocation 1, and terminating that instance is throttled for the rest of the invocation."""

    name = "partial_fleet"

    def __init__(self, rng: random.Random) -> None:
        super().__init__(rng)
        c, d = self._ids(4), self._ids(2)
        self.jobs = [Job(j, "globex") for j in c] + [Job(j, "globex-ml") for j in d]

    def _third(self, run):
        third = [i for i in run.fleet.instances.values() if i.invocation == 1 and i.invocation_seq == 3]
        return third[0] if third else None

    def fleet_shortfall(self, run, job_ids):
        if run.invocation == 1 and run.fleet.create_fleet_calls[1] == 1 and job_ids:
            return {1 if len(job_ids) > 1 else 0}, [
                {"error_code": "InsufficientInstanceCapacity",
                 "error_message": "We currently do not have sufficient capacity in the Availability Zone you requested."}
            ]
        return set(), []

    def github_fault(self, run, name):
        third = self._third(run)
        if run.invocation == 1 and third is not None and third.instance_id in name:
            return 503, {"message": "Service Unavailable"}
        return None

    def fleet_fault(self, run, path, body):
        third = self._third(run)
        if run.invocation == 1 and path == "/terminate" and third is not None and body.get("instance_id") == third.instance_id:
            return REQUEST_LIMIT
        return None


class Backlog(Scenario):
    """One owner already has 2 of its 4 runners busy; 3 of its jobs and 2 of another owner's arrive
    together, and one more job for each owner is queued right after invocation 1 returns, while the
    runners it launched are still running and not yet listed by describe-runners. No faults."""

    name = "backlog"

    def __init__(self, rng: random.Random) -> None:
        super().__init__(rng)
        self.busy_owner = "initech"
        a, b = self._ids(4), self._ids(3)
        busy = [Job(j, self.busy_owner) for j in a]
        other = [Job(j, "hooli") for j in b]
        self.jobs = busy + other
        self.later_jobs = [busy[-1], other[-1]]
        self.seed_runners = {self.busy_owner: 2}

    def after_invocation(self, run, n):
        if n == 1:
            run.send(self.later_jobs)


class BookkeepingThrottle(Scenario):
    """One owner, two jobs, no fleet or code-host faults. Once the first runner config of invocation 1
    has been written, SSM throttles every other PutParameter of that invocation that is not itself a
    runner config (`<SSM_TOKEN_PATH>/<instance-id>`): whatever the function keeps for itself in
    Parameter Store fails to save while the runners it has just configured are already up."""

    name = "bookkeeping_throttle"

    def __init__(self, rng: random.Random) -> None:
        super().__init__(rng)
        self.jobs = [Job(j, "umbrella") for j in self._ids(2)]

    def ssm_put_fault(self, run, name):
        if run.invocation != 1:
            return False
        with run.fleet.lock:
            configs = {f"{run.token_path}/{i.instance_id}": i for i in run.fleet.instances.values()}
        if name in configs:
            return False
        return any(i.invocation == 1 and not i.seeded and run.configured_ever(i) for i in configs.values())


SCENARIOS: dict[str, type[Scenario]] = {
    s.name: s for s in (Brownout, CapCheck, PartialFleet, Backlog, BookkeepingThrottle)
}


# ── one scenario run ────────────────────────────────────────────────────────────────


@dataclass
class Invocation:
    n: int
    records: list[dict]  # {"messageId", "job_id", "owner", "ignored", "receive_count"}
    outcome: str = ""
    listed: list[str] | None = None  # None: complete failure
    expected: list[str] = field(default_factory=list)
    configured_jobs: list = field(default_factory=list)
    duration_s: float = 0.0
    queue_after: dict = field(default_factory=dict)


class Run:
    def __init__(self, scenario_name: str, seed: int | None = None) -> None:
        self.seed = seed if seed is not None else secrets.randbelow(10**9)
        self.rng = random.Random(self.seed)
        self.scenario = SCENARIOS[scenario_name](self.rng)
        self.sfx = f"{scenario_name.replace('_', '-')}-{secrets.token_hex(3)}"
        self.environment = f"ghr-{self.sfx}"
        self.token_path = f"/ghr/{self.sfx}/runners"
        self.invocation = 0
        self.invocations: list[Invocation] = []
        self.foreign: list[str] = []
        self.unresolved: list[str] = []
        self.sqs = aws("sqs")
        self.ssm = aws("ssm")
        self.fleet = Fleet(self)
        self.github = GitHub(self)
        self.proxy = SsmProxy(self)
        self.sqs_proxy = SqsProxy(self)
        self.jobs = {job.job_id: job for job in self.scenario.jobs}
        self.message_job: dict[str, int] = {}
        self.log_dir = LOG_DIR / self.scenario.name
        self.log_dir.mkdir(parents=True, exist_ok=True)

    # setup ------------------------------------------------------------------------
    def create_queues(self) -> None:
        outputs = deploy(aws("cloudformation"), "runner-queues-" + self.sfx,
                         Path(__file__).parent / "cdk_seed/seed.template.json",
                         DlqName=f"ghr-queued-builds-dlq-{self.sfx}",
                         QueueName=f"ghr-queued-builds-{self.sfx}",
                         VisibilityTimeout=VISIBILITY_TIMEOUT_S, RetentionPeriod=86400,
                         MaxReceiveCount=MAX_RECEIVE_COUNT)
        self.dlq_url, self.dlq_arn = outputs["DlqUrl"], outputs["DlqArn"]
        self.queue_url, self.queue_arn = outputs["QueueUrl"], outputs["QueueArn"]

    def seed_busy_runners(self) -> None:
        for owner, count in self.scenario.seed_runners.items():
            for _ in range(count):
                instance = self.fleet.launch(owner, _job_key(self.rng.randrange(1_000, 9_999)), seeded=True)
                config = self.github.issue(owner, f"ghr-{instance.instance_id}", [])["encoded_jit_config"]
                self.ssm.put_parameter(Name=f"{self.token_path}/{instance.instance_id}", Value=config, Type="SecureString")
                self.proxy.forwarded_puts[f"{self.token_path}/{instance.instance_id}"] = config

    def send(self, jobs: list[Job] | None = None) -> None:
        if jobs is None:
            jobs = [j for j in self.scenario.jobs if j not in self.scenario.later_jobs]
        jobs = list(jobs)
        self.rng.shuffle(jobs)
        for job in jobs:
            sent = self.sqs.send_message(QueueUrl=self.queue_url, MessageBody=json.dumps(job.body()))
            self.message_job[sent["MessageId"]] = job.job_id

    # state ------------------------------------------------------------------------
    def ssm_values(self) -> dict[str, str]:
        values: dict[str, str] = {}
        kwargs = {"Path": self.token_path, "Recursive": True, "WithDecryption": True}
        while True:
            page = self.ssm.get_parameters_by_path(**kwargs)
            for parameter in page.get("Parameters", []):
                values[parameter["Name"]] = parameter["Value"]
            if not page.get("NextToken"):
                return values
            kwargs["NextToken"] = page["NextToken"]

    def configured(self, instance: Instance, ssm_values: dict[str, str]) -> bool:
        value = ssm_values.get(f"{self.token_path}/{instance.instance_id}")
        return instance.state == "running" and value is not None and value in self.github.issued.get(instance.instance_id, set())

    def configured_ever(self, instance: Instance) -> bool:
        """The instance got a runner config through the SSM endpoint at some point in the run."""
        value = self.proxy.forwarded_puts.get(f"{self.token_path}/{instance.instance_id}")
        return value is not None and value in self.github.issued.get(instance.instance_id, set())

    def served(self, instance: Instance) -> bool:
        """The instance got a runner config and was not terminated before running its job."""
        return not instance.seeded and self.configured_ever(instance) and (instance.state == "running" or instance.completed)

    def finish_jobs(self) -> None:
        """Ephemeral runners: a configured runner runs its job for JOB_RUN_S, then its instance goes away."""
        now = time.time()
        with self.fleet.lock:
            for instance in self.fleet.running():
                put_at = self.proxy.put_at.get(f"{self.token_path}/{instance.instance_id}")
                if instance.seeded or put_at is None or now < put_at + JOB_RUN_S or not self.configured_ever(instance):
                    continue
                instance.completed = True
                self.fleet.terminate_now(instance.instance_id)

    def _job_clock(self) -> None:
        while not self._stop.wait(0.2):
            self.finish_jobs()

    def configured_jobs(self) -> set:
        with self.fleet.lock:
            return {i.job_id for i in self.fleet.instances.values() if self.served(i)}

    def queue_counts(self, url: str) -> dict[str, int]:
        attributes = self.sqs.get_queue_attributes(
            QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible", "ApproximateNumberOfMessagesDelayed"]
        )["Attributes"]
        return {k.replace("ApproximateNumberOfMessages", "") or "Visible": int(v) for k, v in attributes.items()}

    # the event source mapping ----------------------------------------------------
    def receive(self, max_messages: int, wait_s: int) -> list[dict]:
        return self.sqs.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=max_messages,
            WaitTimeSeconds=wait_s,
            AttributeNames=["All"],
            MessageAttributeNames=["All"],
        ).get("Messages", [])

    def handler_env(self, sandbox: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST", "VERIFIER"))}
        env.update(
            PYTHONPATH=APP,
            PYTHONDONTWRITEBYTECODE="1",
            HOME=sandbox,
            TMPDIR=sandbox,
            AWS_ENDPOINT_URL=ENDPOINT,
            AWS_ENDPOINT_URL_SSM=self.proxy.server.url,
            AWS_ENDPOINT_URL_SQS=self.sqs_proxy.server.url,
            AWS_DEFAULT_REGION="us-east-1",
            AWS_REGION="us-east-1",
            AWS_LAMBDA_FUNCTION_NAME="ghr-scale-up",
            FLEET_API_URL=self.fleet.server.url,
            GITHUB_API_URL=self.github.server.url,
            ENVIRONMENT=self.environment,
            ENABLE_ORGANIZATION_RUNNERS="true",
            ENABLE_EPHEMERAL_RUNNERS="true",
            ENABLE_JIT_CONFIG="true",
            RUNNERS_MAXIMUM_COUNT=str(RUNNERS_MAXIMUM_COUNT),
            RUNNER_LABELS="self-hosted,linux,x64",
            RUNNER_GROUP_NAME="Default",
            RUNNER_NAME_PREFIX="ghr-",
            SSM_TOKEN_PATH=self.token_path,
            SCALE_ERRORS=json.dumps(SCALE_ERRORS),
            LAMBDA_TIMEOUT_S=str(int(HANDLER_TIMEOUT_S)),
            LAMBDA_DEADLINE_MS=str(int((time.time() + HANDLER_TIMEOUT_S) * 1000)),
        )
        return env

    def invoke(self, batch: list[dict]) -> None:
        n = len(self.invocations) + 1
        records = []
        for message in batch:
            job_id = self.message_job[message["MessageId"]]
            job = self.jobs[job_id]
            records.append({
                "messageId": message["MessageId"],
                "job_id": job_id,
                "owner": job.owner,
                "ignored": job.ignored,
                "receive_count": int(message.get("Attributes", {}).get("ApproximateReceiveCount", 0)),
            })
        invocation = Invocation(n=n, records=records)
        self.invocations.append(invocation)
        event = {
            "Records": [
                {
                    "messageId": m["MessageId"],
                    "receiptHandle": m["ReceiptHandle"],
                    "body": m["Body"],
                    "attributes": m.get("Attributes", {}),
                    "messageAttributes": m.get("MessageAttributes", {}),
                    "md5OfBody": m.get("MD5OfBody"),
                    "eventSource": "aws:sqs",
                    "eventSourceARN": self.queue_arn,
                    "awsRegion": "us-east-1",
                }
                for m in batch
            ]
        }
        sandbox = tempfile.mkdtemp(prefix=f"scale-up-{self.scenario.name}-{n}-")
        started = time.monotonic()
        self.invocation = n
        process = subprocess.Popen(
            [sys.executable, "-m", "control_plane.runtime", HANDLER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=sandbox, env=self.handler_env(sandbox), start_new_session=True,
        )
        timed_out = False
        try:
            stdout, stderr = process.communicate(json.dumps(event).encode(), timeout=HANDLER_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
        finally:
            self.invocation = 0
        invocation.duration_s = round(time.monotonic() - started, 2)
        (self.log_dir / f"invocation-{n}.stderr.log").write_bytes(stderr or b"")
        (self.log_dir / f"invocation-{n}.stdout.log").write_bytes(stdout or b"")

        # what the response should have been, from the state at return
        configured = self.configured_jobs()
        invocation.configured_jobs = sorted(configured)
        invocation.expected = sorted(r["messageId"] for r in records if not r["ignored"] and r["job_id"] not in configured)

        # what Lambda makes of the response
        known = {r["messageId"] for r in records}
        if timed_out:
            invocation.outcome = f"timed out after {HANDLER_TIMEOUT_S:.0f} s (killed)"
        elif process.returncode != 0:
            invocation.outcome = f"function error (exit {process.returncode})"
        else:
            try:
                response = json.loads(stdout.decode())
                failures = response.get("batchItemFailures") if isinstance(response, dict) else None
                if failures is None and isinstance(response, dict):
                    failures = []
                listed = [f["itemIdentifier"] for f in failures]
                if any(not isinstance(i, str) or i not in known for i in listed):
                    invocation.outcome = "invalid itemIdentifier in response"
                else:
                    invocation.outcome = "ok"
                    invocation.listed = sorted(set(listed))
            except (ValueError, TypeError, KeyError, AttributeError, UnicodeDecodeError):
                invocation.outcome = "invalid response (stdout is not one SQSBatchResponse JSON object)"

        if invocation.listed is not None:
            delete = [m for m in batch if m["MessageId"] not in invocation.listed]
            for start in range(0, len(delete), 10):
                chunk = delete[start:start + 10]
                answer = self.sqs.delete_message_batch(
                    QueueUrl=self.queue_url,
                    Entries=[{"Id": str(k), "ReceiptHandle": m["ReceiptHandle"]} for k, m in enumerate(chunk)],
                )
                if answer.get("Failed"):
                    raise RuntimeError(f"harness: DeleteMessageBatch failed: {answer['Failed']}")
        time.sleep(POST_RESPONSE_READ_DELAY_S)
        self.scenario.after_invocation(self, n)
        invocation.queue_after = self.queue_counts(self.queue_url)

    def drive(self) -> None:
        deadline = time.monotonic() + SCENARIO_DEADLINE_S
        quiet = 0
        while time.monotonic() < deadline:
            batch = self.receive(BATCH_SIZE, 1)
            if not batch:
                counts = self.queue_counts(self.queue_url)
                quiet = quiet + 1 if sum(counts.values()) == 0 else 0
                if quiet >= QUIESCENCE_STABLE_READS:
                    return
                continue
            quiet = 0
            window_end = time.monotonic() + BATCH_WINDOW_S
            while len(batch) < BATCH_SIZE and time.monotonic() < window_end:
                batch += self.receive(BATCH_SIZE - len(batch), 1)
            ours = []
            for message in batch:
                if message["MessageId"] in self.message_job:
                    ours.append(message)
                else:
                    self.foreign.append(message.get("Body", "")[:200])
                    self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=message["ReceiptHandle"])
            if ours:
                self.invoke(ours)
        self.unresolved.append(f"queue did not drain within {SCENARIO_DEADLINE_S:.0f} s")

    def execute(self) -> "Run":
        self._stop = threading.Event()
        clock = threading.Thread(target=self._job_clock, daemon=True)
        clock.start()
        try:
            self.create_queues()
            self.seed_busy_runners()
            self.send()
            self.drive()
            self.final_ssm = self.ssm_values()
            self.final_queue = self.queue_counts(self.queue_url)
            self.final_dlq = self.queue_counts(self.dlq_url)
            self.final_attributes = self.sqs.get_queue_attributes(QueueUrl=self.queue_url, AttributeNames=["All"])["Attributes"]
        finally:
            self._stop.set()
            clock.join(timeout=5)
            self.write_report()
            for server in (self.fleet.server, self.github.server, self.proxy.server, self.sqs_proxy.server):
                server.close()
        return self

    # reporting --------------------------------------------------------------------
    def describe_jobs(self, message_ids) -> str:
        return ", ".join(f"{self.message_job[m]}({self.jobs[self.message_job[m]].owner})" for m in sorted(message_ids)) or "none"

    def write_report(self) -> None:
        report = {
            "scenario": self.scenario.name,
            "seed": self.seed,
            "environment": self.environment,
            "jobs": [vars(j) for j in self.scenario.jobs],
            "invocations": [vars(i) for i in self.invocations],
            "instances": [vars(i) for i in self.fleet.instances.values()],
            "peak": dict(self.fleet.peak),
            "peak_at": self.fleet.peak_at,
            "fleet_calls": self.fleet.calls,
            "ssm_refused": self.proxy.refused_puts,
            "sqs_refused": self.sqs_proxy.refused,
            "foreign_messages": self.foreign,
            "unresolved": self.unresolved,
        }
        (self.log_dir / "report.json").write_text(json.dumps(report, indent=2, default=str))
