"""
Verifier harness: an isolated deployment per test (role, queue, bucket, state
machine), a generator stand-in that holds every task until the test decides
how the attempt ends, a fleet of service processes, HTTP clients, and proxies
between the service processes and the emulator: one that throttles Step
Functions calls at the account's quotas, and one that can hold one AWS call of
one service process.
"""
import http.client
from pathlib import Path
from cdk_seed.cfn_deploy import deploy
import itertools
import json
import os
import random
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import (
    dataclass,
    field,
)
from http.server import (
    BaseHTTPRequestHandler,
    ThreadingHTTPServer,
)

import boto3
import botocore.config

FLOCI = os.environ.get('AWS_ENDPOINT_URL', 'http://localhost:4566')
APP_DIR = os.environ.get('APP_DIR', '/app')
LOG_DIR = os.environ.get('VERIFIER_LOG_DIR', '/logs/verifier/services')

REQUEST_TIMEOUT_S = 20
TASK_ARRIVAL_TIMEOUT_S = 10
STATUS_SETTLE_TIMEOUT_S = 15
HEALTH_TIMEOUT_S = 20
MAX_TOKEN_FOLLOWS = 5
QUIET_WINDOW_S = 2.0
POLL_S = 0.05

_local = threading.local()
_boto_config = botocore.config.Config(retries={'mode': 'standard', 'max_attempts': 5},
                                      connect_timeout=5, read_timeout=30,
                                      max_pool_connections=50)


def aws(service: str):
    clients = getattr(_local, 'clients', None)
    if clients is None:
        clients = _local.clients = {}
    if service not in clients:
        clients[service] = boto3.session.Session().client(service, endpoint_url=FLOCI,
                                                          config=_boto_config)
    return clients[service]


def floci_is_live() -> bool:
    try:
        with urllib.request.urlopen(f'{FLOCI}/_floci/health', timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


# ───────────────────────────── deployment ─────────────────────────────

TRUST = json.dumps({'Version': '2012-10-17', 'Statement': [{
    'Effect': 'Allow', 'Principal': {'Service': 'states.amazonaws.com'}, 'Action': 'sts:AssumeRole'}]})


@dataclass
class Deployment:
    name: str
    queue_url: str
    bucket: str
    machine_arn: str

    @classmethod
    def create(cls) -> 'Deployment':
        name = 'vf-' + uuid.uuid4().hex[:10]
        outputs = deploy(aws('cloudformation'), 'seed-' + name,
                         Path(__file__).parent / 'cdk_seed/seed.template.json', Name=name)
        queue_url, machine_arn = outputs['QueueUrl'], outputs['MachineArn']
        return cls(name=name, queue_url=queue_url, bucket=name, machine_arn=machine_arn)

    def service_env(self) -> dict[str, str]:
        return {
            'MANIFEST_STATE_MACHINE_ARN': self.machine_arn,
            'MANIFEST_BUCKET': self.bucket,
        }

    def executions(self) -> list[dict]:
        sfn = aws('stepfunctions')
        result, kwargs = [], {}
        while True:
            page = sfn.list_executions(stateMachineArn=self.machine_arn, **kwargs)
            result.extend(page['executions'])
            if not page.get('nextToken'):
                return result
            kwargs = {'nextToken': page['nextToken']}

    def execution_arns(self) -> set[str]:
        return {e['executionArn'] for e in self.executions()}

    def statuses(self) -> dict[str, str]:
        return {e['executionArn']: e['status'] for e in self.executions()}

    def describe(self, arn: str) -> dict | None:
        sfn = aws('stepfunctions')
        try:
            return sfn.describe_execution(executionArn=arn)
        except sfn.exceptions.ExecutionDoesNotExist:
            return None
        except sfn.exceptions.InvalidArn:
            return None

    def status(self, arn: str) -> str | None:
        execution = self.describe(arn)
        return None if execution is None else execution['status']

    def wait_closed(self, arn: str, timeout: float = STATUS_SETTLE_TIMEOUT_S) -> str:
        deadline = time.monotonic() + timeout
        while True:
            status = self.status(arn)
            if status != 'RUNNING' or time.monotonic() > deadline:
                return status
            time.sleep(POLL_S)

    def object_exists(self, key: str) -> bool:
        try:
            aws('s3').head_object(Bucket=self.bucket, Key=key)
            return True
        except aws('s3').exceptions.ClientError:
            return False

    def stop_running(self):
        for e in self.executions():
            if e['status'] == 'RUNNING':
                try:
                    aws('stepfunctions').stop_execution(executionArn=e['executionArn'])
                except Exception:
                    pass


# ───────────────────────────── generator stand-in ─────────────────────────────

@dataclass
class Task:
    execution_arn: str
    task_token: str
    input: dict
    received_at: float


class HeldWorker:
    """
    Consumes the generation tasks of one deployment and holds each one until
    the test releases it, so an attempt stays RUNNING exactly as long as the
    test wants. On success it writes the object named in the input first, as
    the real generator does.
    """

    def __init__(self, deployment: Deployment):
        self.deployment = deployment
        self.tasks: dict[str, Task] = {}
        self.duplicates: list[str] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name='held-worker', daemon=True)
        self._thread.start()

    def _run(self):
        sqs = aws('sqs')
        while not self._stop.is_set():
            try:
                response = sqs.receive_message(QueueUrl=self.deployment.queue_url,
                                               WaitTimeSeconds=1, MaxNumberOfMessages=10)
            except Exception:
                time.sleep(0.2)
                continue
            for message in response.get('Messages', []):
                body = json.loads(message['Body'])
                input = body['input']
                if isinstance(input, str):
                    input = json.loads(input)
                task = Task(execution_arn=body['execution_arn'], task_token=body['task_token'],
                            input=input, received_at=time.monotonic())
                with self._lock:
                    if task.execution_arn in self.tasks:
                        self.duplicates.append(task.execution_arn)
                    self.tasks[task.execution_arn] = task
                sqs.delete_message(QueueUrl=self.deployment.queue_url,
                                   ReceiptHandle=message['ReceiptHandle'])

    def wait_for(self, arn: str, timeout: float = TASK_ARRIVAL_TIMEOUT_S) -> Task | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if arn in self.tasks:
                    return self.tasks[arn]
            time.sleep(POLL_S)
        return None

    def release(self, arn: str, ok: bool):
        task = self.wait_for(arn)
        assert task is not None, f'the generation task of {arn} never reached the generator'
        sfn = aws('stepfunctions')
        if ok:
            object_key = task.input.get('object_key')
            assert object_key, f'execution input of {arn} has no object_key: {task.input!r}'
            aws('s3').put_object(Bucket=self.deployment.bucket, Key=object_key,
                                 Body=b'file_id\tproject\nf1\tp\n')
            sfn.send_task_success(taskToken=task.task_token,
                                  output=json.dumps({'object_key': object_key, 'rows': 1}))
        else:
            sfn.send_task_failure(taskToken=task.task_token, error='IndexUnavailable',
                                  cause='ConnectionTimeout: the catalog index did not answer')

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)


# ───────────────────────────── service fleet ─────────────────────────────

def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Fleet:
    """
    Service instances as separate processes, each in its own fresh working
    directory, started as `python3 -m manifest_service --port <port>`.
    """

    def __init__(self, deployment: Deployment, count: int, label: str, extra_env: dict | None = None):
        self.deployment = deployment
        self.label = label
        self.ports = [free_port() for _ in range(count)]
        self.extra_env = extra_env or {}
        self.procs: dict[int, subprocess.Popen] = {}
        self.generation = 0
        os.makedirs(LOG_DIR, exist_ok=True)
        self.start()

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith('MANIFEST_')}
        env.update(self.deployment.service_env())
        env.update({'PYTHONPATH': APP_DIR, 'AWS_ENDPOINT_URL': FLOCI,
                    'AWS_DEFAULT_REGION': os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'),
                    'AWS_REGION': os.environ.get('AWS_DEFAULT_REGION', 'us-east-1'),
                    'AWS_ACCESS_KEY_ID': os.environ.get('AWS_ACCESS_KEY_ID', 'test'),
                    'AWS_SECRET_ACCESS_KEY': os.environ.get('AWS_SECRET_ACCESS_KEY', 'test'),
                    'PYTHONDONTWRITEBYTECODE': '1'})
        env.update(self.extra_env)
        return env

    def log_path(self, port: int) -> str:
        return os.path.join(LOG_DIR, f'{self.label}-{port}-{self.generation}.log')

    def start(self):
        env = self._env()
        for port in self.ports:
            log = open(self.log_path(port), 'w')
            self.procs[port] = subprocess.Popen([sys.executable, '-m', 'manifest_service', '--port', str(port)],
                                                cwd=tempfile.mkdtemp(prefix='svc-'), env=env,
                                                stdout=log, stderr=subprocess.STDOUT,
                                                start_new_session=True)
        deadline = time.monotonic() + HEALTH_TIMEOUT_S
        pending = set(self.ports)
        while pending and time.monotonic() < deadline:
            if any(self.procs[port].poll() is not None for port in pending):
                break
            for port in list(pending):
                try:
                    if http_call('GET', port, '/health', timeout=2)[0] == 200:
                        pending.discard(port)
                except Exception:
                    pass
            time.sleep(0.1)
        assert not pending, (f'service instance(s) on {sorted(pending)} did not answer GET /health: '
                             + ' | '.join(self.log_tail(p) for p in pending))

    def log_tail(self, port: int, lines: int = 15) -> str:
        try:
            with open(self.log_path(port)) as f:
                return ''.join(f.readlines()[-lines:])
        except OSError:
            return ''

    def kill(self):
        for proc in self.procs.values():
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        for proc in self.procs.values():
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        self.procs = {}

    def restart(self):
        self.kill()
        self.generation += 1
        self.start()

    def port(self, i: int) -> int:
        return self.ports[i % len(self.ports)]


# ───────────────────────────── HTTP clients ─────────────────────────────

def http_call(method: str, port: int, path: str, timeout: float = REQUEST_TIMEOUT_S) -> tuple[int, dict]:
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    try:
        conn.request(method, path, headers={'Content-Length': '0'} if method == 'PUT' else {})
        response = conn.getresponse()
        raw = response.read()
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {'_raw': raw[:500].decode(errors='replace')}
        return response.status, body
    finally:
        conn.close()


def manifest_path(catalog: str, format: str, filters_json: str) -> str:
    query = urllib.parse.urlencode({'catalog': catalog, 'format': format, 'filters': filters_json})
    return f'/v1/manifests?{query}'


@dataclass
class Manifest:
    """One manifest, requested with its filter fields in every order."""
    catalog: str
    format: str
    fields: list[tuple[str, dict]]

    @classmethod
    def random(cls) -> 'Manifest':
        project = 'p-' + uuid.uuid4().hex[:12]
        fields = [('project', {'is': [project]}),
                  ('organ', {'is': [random.choice(['brain', 'heart', 'lung'])]}),
                  ('fileFormat', {'is': [random.choice(['fastq.gz', 'bam', 'loom'])]})]
        return cls(catalog='dcp', format=random.choice(['compact', 'verbatim.jsonl']), fields=fields)

    def variants(self) -> list[str]:
        return [json.dumps(dict(order)) for order in itertools.permutations(self.fields)]

    def path(self, variant: int) -> str:
        variants = self.variants()
        return manifest_path(self.catalog, self.format, variants[variant % len(variants)])


@dataclass
class Outcome:
    """Where one client ended up after its request and the redirects it followed."""
    kind: str  # '301' (naming a RUNNING execution), '302', '500', 'error', 'loop'
    status: int
    body: dict
    execution: str | None = None
    hops: list = field(default_factory=list)

    def describe(self) -> str:
        return f'{self.kind} after {len(self.hops) - 1} redirect(s): HTTP {self.status} {json.dumps(self.body)[:300]}'


def settle(deployment: Deployment, fleet: Fleet, first: tuple[int, dict], start: int) -> Outcome:
    """
    Follow 301s until the client holds a 301 that names a running execution,
    a 302, or an error. Polls go to other instances than the one that
    answered, as they would behind the load balancer.
    """
    status, body = first
    hops = []
    for hop in range(MAX_TOKEN_FOLLOWS + 1):
        hops.append((status, body))
        if status == 500:
            return Outcome('500', status, body, hops=hops)
        if status != 200:
            return Outcome('error', status, body, hops=hops)
        if body.get('Status') == 302:
            return Outcome('302', status, body, hops=hops)
        if body.get('Status') != 301 or 'Location' not in body:
            return Outcome('error', status, body, hops=hops)
        execution = body.get('Execution')
        if execution and deployment.status(execution) == 'RUNNING':
            return Outcome('301', status, body, execution=execution, hops=hops)
        if hop == MAX_TOKEN_FOLLOWS:
            break
        status, body = http_call('GET', fleet.port(start + hop + 1), body['Location'])
    return Outcome('loop', status, body, hops=hops)


def fresh_request(deployment: Deployment, fleet: Fleet, manifest: Manifest, i: int) -> Outcome:
    first = http_call('PUT', fleet.port(i), manifest.path(i))
    return settle(deployment, fleet, first, i)


def burst(deployment: Deployment, fleet: Fleet, manifest: Manifest, size: int = 8) -> list[Outcome]:
    """
    `size` concurrent fresh requests for one manifest, released together,
    spread round-robin over the instances, each with its own field order.
    """
    barrier = threading.Barrier(size)
    outcomes: list[Outcome | None] = [None] * size

    def client(i):
        try:
            barrier.wait(timeout=10)
            outcomes[i] = fresh_request(deployment, fleet, manifest, i)
        except Exception as e:
            outcomes[i] = Outcome('error', 0, {'exception': repr(e)})

    threads = [threading.Thread(target=client, args=(i,)) for i in range(size)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=REQUEST_TIMEOUT_S * (MAX_TOKEN_FOLLOWS + 2))
    return [o if o is not None else Outcome('error', 0, {'exception': 'client hung'}) for o in outcomes]


def poll(deployment: Deployment, fleet: Fleet, outcome: Outcome, i: int = 0) -> Outcome:
    """A client following the token it holds, once more."""
    first = http_call('GET', fleet.port(i), outcome.body['Location'])
    return settle(deployment, fleet, first, i)


# ───────────────────────────── AWS proxies ─────────────────────────────

#: The account's Step Functions API quotas, as `/app/README.md` states them
#: (the AWS defaults for us-east-1): API name -> (bucket size, refill per second)
SFN_QUOTAS = {
    'StartExecution': (1300, 300),
    'DescribeExecution': (300, 15),
    'ListExecutions': (200, 5),
    'GetExecutionHistory': (400, 20),
    'DescribeStateMachine': (200, 20),
    'ListStateMachines': (100, 5),
}


class TokenBucket:

    def __init__(self, size: int, rate: float):
        self.size, self.rate = size, rate
        self.tokens = float(size)
        self.stamp = time.monotonic()

    def take(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.size, self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class Proxy:
    """
    Forwards a service process's AWS calls to `upstream`.

    With `quotas`, it throttles Step Functions calls the way the account does:
    one token bucket per API for everything that goes through it, and a
    `ThrottlingException` for a call that finds its bucket empty.

    Once armed, it holds the next non-S3 call (any request carrying
    X-Amz-Target) until released, so the process is stopped between whatever
    it did before that call and the call itself.
    """
    hop_by_hop = {'connection', 'keep-alive', 'proxy-connection', 'transfer-encoding', 'content-length', 'upgrade'}

    def __init__(self, upstream: str = FLOCI, quotas: dict[str, tuple[int, float]] | None = None):
        upstream = urllib.parse.urlsplit(upstream)
        self.upstream = (upstream.hostname, upstream.port or 80)
        self._lock = threading.Lock()
        self._armed = False
        self.held = threading.Event()
        self._released = threading.Event()
        self.held_target: str | None = None
        self.buckets = {api: TokenBucket(*quota) for api, quota in (quotas or {}).items()}
        self.calls: dict[str, int] = {}
        self.throttled: dict[str, int] = {}
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def _forward(self):
                length = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(length) if length else None
                target = self.headers.get('X-Amz-Target')
                hold = False
                if target:
                    api = target.rpartition('.')[2]
                    with proxy._lock:
                        proxy.calls[api] = proxy.calls.get(api, 0) + 1
                        bucket = proxy.buckets.get(api)
                        throttle = bucket is not None and not bucket.take()
                        if throttle:
                            proxy.throttled[api] = proxy.throttled.get(api, 0) + 1
                        elif proxy._armed:
                            proxy._armed = False
                            hold = True
                    if throttle:
                        self._throttle()
                        return
                if hold:
                    proxy.held_target = target
                    proxy.held.set()
                    proxy._released.wait(timeout=60)
                headers = {k: v for k, v in self.headers.items() if k.lower() not in self.hop_by_hop}
                if body is not None:
                    headers['Content-Length'] = str(len(body))
                conn = http.client.HTTPConnection(*proxy.upstream, timeout=60)
                try:
                    conn.request(self.command, self.path, body=body, headers=headers)
                    response = conn.getresponse()
                    data = response.read()
                    self.send_response_only(response.status, response.reason)
                    for k, v in response.getheaders():
                        if k.lower() not in self.hop_by_hop:
                            self.send_header(k, v)
                    if self.command == 'HEAD':
                        self.send_header('Content-Length', response.getheader('Content-Length') or '0')
                    else:
                        self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    if self.command != 'HEAD':
                        self.wfile.write(data)
                finally:
                    conn.close()

            def _throttle(self):
                payload = json.dumps({'__type': 'ThrottlingException', 'message': 'Rate exceeded'}).encode()
                self.send_response_only(400, 'Bad Request')
                self.send_header('Content-Type', 'application/x-amz-json-1.0')
                self.send_header('x-amzn-RequestId', str(uuid.uuid4()))
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_PUT = do_POST = do_HEAD = do_DELETE = _forward

            def log_message(self, format, *args):
                pass

            hop_by_hop = Proxy.hop_by_hop

        self._server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f'http://127.0.0.1:{self.port}'

    def usage(self) -> str:
        with self._lock:
            return ', '.join(f'{api} {n} call(s), {self.throttled.get(api, 0)} throttled'
                             for api, n in sorted(self.calls.items())) or 'no Step Functions calls'

    def arm(self):
        with self._lock:
            self.held.clear()
            self._released.clear()
            self.held_target = None
            self._armed = True

    def release(self):
        with self._lock:
            self._armed = False
        self._released.set()

    def close(self):
        self.release()
        self._server.shutdown()
