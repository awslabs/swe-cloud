"""Driver, interposing proxy and state replay for the chambersm verifier.

Every writer is its own `python3 -m chambersm ...` process with a private HOME/TMPDIR whose
AWS_ENDPOINT_URL points at a per-writer proxy. The proxy forwards to Floci only the six calls
the writer role allows on the run's secret (AccessDeniedException otherwise), counts mutating
calls, and can
  * park the t-th mutating call (not forwarded) until the driver releases it, or
  * forward the t-th mutating call and, once Floci has answered, SIGKILL the writer before the
    answer is relayed, or
  * forward the t-th mutating call and, once Floci has answered, close the connection without
    relaying the answer (README "Dropped connections"); the writer's SDK retries it.
Mutating calls are forwarded one at a time, so the trace order is the order Floci applied them.
Floci does not enforce Secrets Manager's quota of 20 staging labels per secret; the proxy does,
answering LimitExceededException (and not forwarding) a call that would exceed it, as AWS does.
State is read back directly from Floci, never through the solver's code.
"""

import http.client
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3
from botocore.config import Config

FLOCI_ENDPOINT = os.environ.get("FLOCI_ENDPOINT", "http://localhost:4566")
APP_DIR = os.environ.get("CHAMBERSM_APP", "/app")
PYTHON = os.environ.get("CHAMBERSM_PYTHON", "python3")
LOG_DIR = os.environ.get("CHAMBERSM_LOG_DIR", "/logs/verifier/runs")

ALLOWED = {"GetSecretValue", "DescribeSecret", "ListSecretVersionIds",
           "CreateSecret", "PutSecretValue", "UpdateSecretVersionStage"}
MUTATING = {"CreateSecret", "PutSecretValue", "UpdateSecretVersionStage"}

GATE_TIMEOUT_S = 15       # B must reach its t-th mutating call (or exit) within this
WRITE_DEADLINE_S = 20     # the CI step timeout (ci/set-secret.sh)
READ_DEADLINE_S = 30
MAX_INDEX = 12            # sweep t = 1 .. MAX_INDEX
LABEL_QUOTA = 20          # AWS: staging labels attached across all versions of a secret

METADATA_KEY = "_chamber_metadata"


def floci_client():
    return boto3.client(
        "secretsmanager", endpoint_url=FLOCI_ENDPOINT, region_name="us-east-1",
        aws_access_key_id="test", aws_secret_access_key="test",
        config=Config(retries={"max_attempts": 3, "mode": "standard"}))


def floci_alive():
    try:
        parsed = urllib.parse.urlparse(FLOCI_ENDPOINT)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=5)
        conn.request("GET", "/_floci/health")
        return conn.getresponse().status == 200
    except OSError:
        return False


# --------------------------------------------------------------------------------- proxy

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _reply(self, status, body, headers=()):
        self.send_response(status)
        sent = set()
        for name, value in headers:
            self.send_header(name, value)
            sent.add(name.lower())
        if "content-type" not in sent:
            self.send_header("Content-Type", "application/x-amz-json-1.1")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._deny("GET " + self.path, {})

    def _deny(self, op, req):
        proxy = self.server.proxy
        proxy.run.record(proxy.name, op, req, 400, {"denied": True}, time.monotonic(),
                         time.monotonic(), None)
        body = json.dumps({
            "__type": "AccessDeniedException",
            "Message": f"User: arn:aws:sts::000000000000:assumed-role/chambersm-writer/"
                       f"{proxy.name} is not authorized to perform: {op}"}).encode()
        self._reply(400, body, [("x-amzn-ErrorType", "AccessDeniedException")])

    def do_POST(self):
        proxy = self.server.proxy
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        target = self.headers.get("X-Amz-Target", "")
        op = target.split(".", 1)[1] if target.startswith("secretsmanager.") else (target or "?")
        try:
            req = json.loads(body or b"{}")
            if not isinstance(req, dict):
                req = {}
        except ValueError:
            req = {}
        if not target.startswith("secretsmanager.") or op not in ALLOWED \
                or not proxy.run.addresses_service(op, req):
            self._deny(target or self.path, req)
            return

        index = None
        if op in MUTATING:
            with proxy.lock:
                proxy.mutations += 1
                index = proxy.mutations
            if proxy.park_at == index:
                proxy.parked.set()
                proxy.release.wait(timeout=600)
                if proxy.closing:
                    # The writer was given up on while parked: the call is never delivered.
                    self.close_connection = True
                    return

        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ("host", "content-length", "connection", "accept-encoding")}
        if op in MUTATING:
            with proxy.run.mutation_lock:
                t_start = time.monotonic()
                over = proxy.run.over_label_quota(op, req)
                if over:
                    resp = {"__type": "LimitExceededException", "Message": over}
                    proxy.run.record(proxy.name, op, req, 400, resp, t_start, time.monotonic(),
                                     index)
                    if proxy.kill_at == index:
                        proxy.kill_writer()
                        self.close_connection = True
                        return
                    self._reply(400, json.dumps(resp).encode(),
                                [("x-amzn-ErrorType", "LimitExceededException")])
                    return
                status, resp_headers, resp_body = proxy.run.forward(self.path, body, headers)
                t_end = time.monotonic()
                proxy.run.record(proxy.name, op, req, status, _json(resp_body), t_start, t_end,
                                 index)
        else:
            t_start = time.monotonic()
            status, resp_headers, resp_body = proxy.run.forward(self.path, body, headers)
            proxy.run.record(proxy.name, op, req, status, None, t_start, time.monotonic(), index)

        if op in MUTATING and proxy.kill_at == index:
            proxy.kill_writer()
            self.close_connection = True
            return
        if op in MUTATING and proxy.drop_at == index:
            # Floci applied the call; the answer is lost on the way back.
            proxy.dropped.set()
            self.close_connection = True
            return
        self._reply(status, resp_body, resp_headers)


def _json(raw):
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        return {"raw": raw.decode("utf-8", "replace")}


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class WriterProxy:
    def __init__(self, run, name, park_at=None, kill_at=None, drop_at=None):
        self.run = run
        self.name = name
        self.park_at = park_at
        self.kill_at = kill_at
        self.drop_at = drop_at
        self.dropped = threading.Event()
        self.lock = threading.Lock()
        self.mutations = 0
        self.parked = threading.Event()
        self.release = threading.Event()
        self.killed = threading.Event()
        self.proc_ready = threading.Event()
        self.closing = False
        self.proc = None
        self.server = _Server(("127.0.0.1", 0), _Handler)
        self.server.proxy = self
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def kill_writer(self):
        self.proc_ready.wait(timeout=30)
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self.killed.set()

    def close(self):
        if not self.release.is_set():
            self.closing = True
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


# ---------------------------------------------------------------------------------- writers

class Proc:
    """One chambersm process."""

    def __init__(self, run, name, argv, park_at=None, kill_at=None, drop_at=None):
        self.name = name
        self.argv = argv
        self.proxy = WriterProxy(run, name, park_at=park_at, kill_at=kill_at, drop_at=drop_at)
        self.home = tempfile.mkdtemp(prefix=f"w-{name}-", dir=run.scratch)
        os.makedirs(os.path.join(self.home, "tmp"))
        self.out_path = os.path.join(run.logdir, f"{run.seq_no():02d}-{name}.out")
        endpoint = f"http://127.0.0.1:{self.proxy.port}"
        env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
        env.update({
            "AWS_ENDPOINT_URL": endpoint,
            "AWS_ENDPOINT_URL_SECRETS_MANAGER": endpoint,
            "AWS_REGION": "us-east-1",
            "AWS_DEFAULT_REGION": "us-east-1",
            "AWS_ACCESS_KEY_ID": "test",
            "AWS_SECRET_ACCESS_KEY": "test",
            "AWS_CONFIG_FILE": os.path.join(self.home, "no-aws-config"),
            "AWS_SHARED_CREDENTIALS_FILE": os.path.join(self.home, "no-aws-credentials"),
            "CHAMBER_WRITER_ID": f"{run.service}/{name}",
            "HOME": self.home,
            "TMPDIR": os.path.join(self.home, "tmp"),
            "PYTHONPATH": APP_DIR,
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        self.out = open(self.out_path, "wb")
        self.err_path = self.out_path[:-4] + ".err"
        self.err = open(self.err_path, "wb")
        self.start = time.monotonic()
        self.proc = subprocess.Popen([PYTHON, "-m", "chambersm", *argv], cwd=self.home, env=env,
                                     stdout=self.out, stderr=self.err, stdin=subprocess.DEVNULL)
        self.proxy.proc = self.proc
        self.proxy.proc_ready.set()
        self.end = None
        self.rc = None
        self.timed_out = False

    def wait(self, timeout):
        try:
            self.rc = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.timed_out = True
            self.proc.kill()
            self.rc = self.proc.wait()
        self.end = time.monotonic()
        self.out.close()
        self.err.close()
        return self.rc

    def stdout(self):
        with open(self.out_path, "rb") as fh:
            return fh.read().decode("utf-8", "replace")

    def stderr_tail(self, n=600):
        with open(self.err_path, "rb") as fh:
            data = fh.read().decode("utf-8", "replace").strip()
        return data[-n:]


class _Done:
    """Timing of a write the driver made itself (seeded state)."""

    def __init__(self, start, end):
        self.start = start
        self.end = end


class Write:
    def __init__(self, label, key, value):
        self.label = label
        self.key = key
        self.value = value
        self.proc = None
        self.acked = False
        self.killed = False
        self.retained_at_start = True   # seeded writes the old code had already lost: False

    @property
    def start(self):
        return self.proc.start

    @property
    def end(self):
        return self.proc.end

    def describe(self):
        return f"{self.label} (write {self.key}={self.value})"


# ------------------------------------------------------------------------------------ runs

class Run:
    def __init__(self, service, logdir):
        self.service = service
        self.logdir = logdir
        os.makedirs(logdir, exist_ok=True)
        self.scratch = tempfile.mkdtemp(prefix="chambersm-run-")
        self.trace = []
        self.trace_lock = threading.Lock()
        self.mutation_lock = threading.Lock()
        self.writes = []
        self.violations = []        # (category, message)
        self.arn = None
        self._seq = 0
        self._client = None
        parsed = urllib.parse.urlparse(FLOCI_ENDPOINT)
        self._upstream = (parsed.hostname, parsed.port or 80)

    def over_label_quota(self, op, req):
        """The quota message if op would leave more than LABEL_QUOTA labels on the secret."""
        if op not in ("PutSecretValue", "UpdateSecretVersionStage"):
            return None
        sm = self.client()
        try:
            stages = sm.describe_secret(SecretId=req.get("SecretId")).get("VersionIdsToStages", {})
        except Exception:
            return None           # let Floci answer (not found, bad parameter, ...)
        in_use = {s for labels in stages.values() for s in labels}
        if op == "PutSecretValue":
            token = req.get("ClientRequestToken")
            if token and token in {v["VersionId"] for v in
                                   _paginate_versions(sm, req.get("SecretId"), True)}:
                return None       # an existing version: Floci moves no label either way
            new = set(req.get("VersionStages") or ["AWSCURRENT"])
        else:
            if not req.get("MoveToVersionId"):
                return None
            new = {req.get("VersionStage")}
        if "AWSCURRENT" in new and "AWSCURRENT" in in_use:
            new.add("AWSPREVIOUS")
        after = in_use | new
        if len(after) <= LABEL_QUOTA or len(after) <= len(in_use):
            return None
        return (f"You can't attach more than {LABEL_QUOTA} staging labels across all versions of "
                f"a secret. This request would attach {len(after)}.")

    def client(self):
        if self._client is None:
            self._client = floci_client()
        return self._client

    def seed(self, plan):
        """Write plan [(key, value), ...] the way chambersm wrote before this fix (per-key
        CHAMBER{version} label, unlabelled CreateSecret version), directly against Floci. Returns
        the seeded writes; their timing is the driver's own."""
        sm = self.client()
        out = []
        for key, value in plan:
            w = Write(f"seed-{len(out) + 1}", key, value)
            start = time.monotonic()
            try:
                doc = json.loads(sm.get_secret_value(SecretId=self.service)["SecretString"])
                exists = True
            except sm.exceptions.ResourceNotFoundException:
                doc, exists = {}, False
            meta = json.loads(doc.get(METADATA_KEY, "{}"))
            version = meta[key]["version"] + 1 if key in meta else 1
            # Months of daily rotations, oldest first, in chambersm's own timestamp format.
            when = time.time() - 86400 * (len(plan) - len(out)) - 3600
            meta[key] = {"created": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(when))
                         + f".{len(out):06d}Z",
                         "created_by": f"deploy/{4100 + len(out)}", "version": version}
            doc[key] = value
            doc[METADATA_KEY] = json.dumps(meta, separators=(",", ":"))
            with self.mutation_lock:
                t0 = time.monotonic()
                if not exists:
                    req = {"Name": self.service, "SecretString": json.dumps(doc)}
                    resp = sm.create_secret(**req)
                    op = "CreateSecret"
                else:
                    req = {"SecretId": self.service, "SecretString": json.dumps(doc),
                           "VersionStages": ["AWSCURRENT", f"CHAMBER{version}"]}
                    resp = sm.put_secret_value(**req)
                    op = "PutSecretValue"
                self.record("seed", op, req, 200, {"VersionId": resp["VersionId"]}, t0,
                            time.monotonic(), None)
            w.proc = _Done(start, time.monotonic())
            w.acked = True
            self.writes.append(w)
            out.append(w)
            time.sleep(0.02)      # distinct CreatedDate per version, as real history has
        return out

    def seq_no(self):
        self._seq += 1
        return self._seq

    def fail(self, category, message):
        self.violations.append((category, message))

    def addresses_service(self, op, req):
        ident = req.get("Name") if op == "CreateSecret" else req.get("SecretId")
        if not isinstance(ident, str):
            return False
        if ident == self.service:
            return True
        return re.fullmatch(r"arn:aws:secretsmanager:us-east-1:\d{12}:secret:"
                            + re.escape(self.service) + r"(-[A-Za-z0-9]{6})?", ident) is not None

    def forward(self, path, body, headers):
        for attempt in range(3):
            try:
                conn = http.client.HTTPConnection(*self._upstream, timeout=60)
                conn.request("POST", path, body=body, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
                out_headers = [(k, v) for k, v in resp.getheaders()
                               if k.lower() not in ("content-length", "transfer-encoding",
                                                    "connection", "keep-alive")]
                conn.close()
                return resp.status, out_headers, data
            except OSError:
                if attempt == 2:
                    raise
                time.sleep(0.5)

    def record(self, writer, op, req, status, resp, t_start, t_end, index):
        with self.trace_lock:
            self.trace.append({"writer": writer, "op": op, "request": req, "status": status,
                               "response": resp, "t_start": t_start, "t_end": t_end,
                               "mutation_index": index})

    # -- spawning --------------------------------------------------------------------------

    def spawn_write(self, label, key, value, park_at=None, kill_at=None, drop_at=None):
        w = Write(label, key, value)
        w.proc = Proc(self, label, ["write", self.service, key, value], park_at=park_at,
                      kill_at=kill_at, drop_at=drop_at)
        self.writes.append(w)
        return w

    def finish(self, w, timeout, phase):
        rc = w.proc.wait(timeout)
        w.proc.proxy.close()
        if w.proc.proxy.killed.is_set():
            w.killed = True
            return
        if w.proc.timed_out:
            self.fail("writers", f"{w.describe()} [{phase}] did not finish within {timeout:.0f} s "
                                 f"and was killed")
        elif rc != 0:
            self.fail("writers", f"{w.describe()} [{phase}] exited {rc}: {w.proc.stderr_tail()}")
        else:
            w.acked = True

    def sequential_write(self, label, key, value, phase):
        w = self.spawn_write(label, key, value)
        self.finish(w, WRITE_DEADLINE_S, phase)
        return w

    def cli(self, argv):
        p = Proc(self, "reader", argv)
        rc = p.wait(READ_DEADLINE_S)
        p.proxy.close()
        return rc, p.stdout(), p.stderr_tail(), p.timed_out

    def cleanup(self):
        shutil.rmtree(self.scratch, ignore_errors=True)


# --------------------------------------------------------------------------- state read-back

def _paginate_versions(sm, service, include_deprecated):
    out = []
    kwargs = {"SecretId": service, "IncludeDeprecated": include_deprecated, "MaxResults": 100}
    while True:
        resp = sm.list_secret_version_ids(**kwargs)
        out.extend(resp.get("Versions", []))
        if not resp.get("NextToken"):
            return out
        kwargs["NextToken"] = resp["NextToken"]


class State:
    """What Secrets Manager holds for a service, read directly from Floci."""

    def __init__(self, service):
        sm = floci_client()
        self.service = service
        self.exists = True
        try:
            desc = sm.describe_secret(SecretId=service)
        except sm.exceptions.ResourceNotFoundException:
            self.exists = False
            self.stages, self.listed, self.all_ids, self.docs, self.current = {}, [], set(), {}, None
            return
        self.arn = desc["ARN"]
        self.stages = {vid: sorted(st) for vid, st in desc.get("VersionIdsToStages", {}).items()}
        self.listed = [v["VersionId"] for v in _paginate_versions(sm, service, False)]
        self.all_ids = {v["VersionId"] for v in _paginate_versions(sm, service, True)}
        self.docs = {}
        for vid in self.listed:
            self.docs[vid] = sm.get_secret_value(SecretId=service, VersionId=vid).get("SecretString")
        cur = sm.get_secret_value(SecretId=service)
        self.current_id = cur["VersionId"]
        self.current = cur.get("SecretString")


def parse_doc(raw):
    """(top-level dict, metadata dict) or raise ValueError."""
    doc = json.loads(raw)
    if not isinstance(doc, dict):
        raise ValueError("SecretString is not a JSON object")
    meta_raw = doc.get(METADATA_KEY, "{}")
    if not isinstance(meta_raw, str):
        raise ValueError(f"{METADATA_KEY} is not a string")
    meta = json.loads(meta_raw) if meta_raw else {}
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise ValueError(f"{METADATA_KEY} is not a JSON object")
    return doc, meta


def replay(state, keys):
    """{key: {version: set(values)}} over every version Secrets Manager lists, plus problems."""
    out = {k: {} for k in keys}
    problems = []
    for vid in state.listed:
        raw = state.docs.get(vid)
        try:
            doc, meta = parse_doc(raw or "")
        except ValueError as err:
            problems.append(f"listed version {vid} is not a chambersm document ({err})")
            continue
        for key in keys:
            entry = meta.get(key)
            if not isinstance(entry, dict):
                continue
            version = entry.get("version")
            if not isinstance(version, int) or isinstance(version, bool):
                problems.append(f"listed version {vid}: metadata of {key} has version {version!r}")
                continue
            out[key].setdefault(version, set()).add(doc.get(key))
    return out, problems


# ------------------------------------------------------------- label accounting (Floci rules)

class LabelModel:
    """Floci 2.2.0's staging-label rules, applied to the proxied mutations in apply order."""

    def __init__(self):
        self.created = False
        self.versions = {}        # VersionId -> {"stages": [..], "data": str}
        self.order = []

    def _find(self, stage):
        for vid in self.order:
            if stage in self.versions[vid]["stages"]:
                return vid
        return None

    def apply(self, op, req, resp, status):
        if op == "CreateSecret":
            if status != 200:
                return
            if self.created:
                return            # idempotent repeat: returns the existing secret
            vid = resp.get("VersionId")
            self.created = True
            self.versions[vid] = {"stages": ["AWSCURRENT"], "data": req.get("SecretString")}
            self.order.append(vid)
        elif op == "PutSecretValue":
            if status != 200:
                return
            vid = resp.get("VersionId")
            if vid in self.versions:
                return            # same token, same data: no label moves
            stages = req.get("VersionStages")
            stages = list(stages) if stages is not None else ["AWSCURRENT"]
            previous_current = self._find("AWSCURRENT") if "AWSCURRENT" in stages else None
            for stage in stages:
                holder = self._find(stage)
                if holder is None or holder == vid:
                    continue
                new = list(self.versions[holder]["stages"])
                if stage == "AWSCURRENT":
                    prev = self._find("AWSPREVIOUS")
                    if prev is not None:
                        old = list(self.versions[prev]["stages"])
                        if "AWSPREVIOUS" in old:
                            old.remove("AWSPREVIOUS")
                        self.versions[prev]["stages"] = old
                    new.append("AWSPREVIOUS")
                if stage in new:
                    new.remove(stage)
                self.versions[holder]["stages"] = new
            if previous_current is not None:
                for v in self.order:
                    if "AWSPREVIOUS" in self.versions[v]["stages"]:
                        self.versions[v]["stages"] = [s for s in self.versions[v]["stages"]
                                                      if s != "AWSPREVIOUS"]
                self.versions[previous_current]["stages"] = (
                    self.versions[previous_current]["stages"] + ["AWSPREVIOUS"])
            self.versions[vid] = {"stages": list(stages), "data": req.get("SecretString")}
            self.order.append(vid)
        elif op == "UpdateSecretVersionStage":
            not_found_move = (status == 400 and isinstance(resp, dict)
                              and "ResourceNotFound" in str(resp.get("__type", ""))
                              and "VersionId" in str(resp.get("message", resp.get("Message", ""))))
            if status != 200 and not not_found_move:
                return
            label = req.get("VersionStage")
            move_to = req.get("MoveToVersionId")
            remove_from = req.get("RemoveFromVersionId")
            holder = self._find(label)
            if holder is not None and remove_from in self.versions:
                stages = list(self.versions[remove_from]["stages"])
                if label in stages:
                    stages.remove(label)
                if label == "AWSCURRENT":
                    stages.append("AWSPREVIOUS")
                    prev = self._find("AWSPREVIOUS")
                    if prev is not None:
                        ps = list(self.versions[prev]["stages"])
                        ps.remove("AWSPREVIOUS")
                        self.versions[prev]["stages"] = ps
                self.versions[remove_from]["stages"] = stages
            if status == 200 and move_to is not None and move_to in self.versions:
                self.versions[move_to]["stages"] = self.versions[move_to]["stages"] + [label]

    def labelled(self):
        return {vid: sorted(v["stages"]) for vid, v in self.versions.items() if v["stages"]}


def account(run, state):
    """Problems if the service's versions/labels are not explained by the proxied calls."""
    model = LabelModel()
    returned = set()
    # Mutating calls were forwarded one at a time, so start order is Floci's apply order.
    for entry in sorted((e for e in run.trace if e["op"] in MUTATING),
                        key=lambda e: e["t_start"]):
        model.apply(entry["op"], entry["request"], entry["response"] or {}, entry["status"])
        if entry["op"] in ("CreateSecret", "PutSecretValue") and entry["status"] == 200:
            returned.add((entry["response"] or {}).get("VersionId"))
    problems = []
    unexplained = state.all_ids - returned
    if unexplained:
        problems.append(f"versions {sorted(unexplained)} exist but no writer created them through "
                        f"its configured AWS endpoint")
    if model.labelled() != state.stages:
        problems.append("staging labels in Secrets Manager do not match the label moves the "
                        f"writers made through their configured AWS endpoint: expected "
                        f"{model.labelled()}, found {state.stages}")
    return problems


def nonce(prefix):
    return f"{prefix}-{secrets.token_hex(4)}"


def service_name(tag):
    return f"{tag}-{secrets.token_hex(3)}"


def dump_trace(run, path):
    with open(path, "w") as fh:
        for e in run.trace:
            fh.write(json.dumps(e, default=str) + "\n")

